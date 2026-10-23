"""Capture one month of Ronghui finance statements for the three finance accounts.

Protocol confirmed read-only on the real 结算明细查询 page on 2026-10-23:
the 统计查询 tab (`FIND_BALANCE_QRY_TJ_WST`) returns one row per settlement
type and site; its 明细数据 tab (`FIND_BALANCE_QRY_TJ_DETAIL`) without a type
filter returns every detail row of the period, paged by `pageIndex` from 0.
Both take the period in `BALANCE_DATE` (结算日期) or `FINANCE_DATE` (账单日期)
as `{"start": "YYYY/MM/DD 00:00:00", "end": "YYYY/MM/DD 23:59:59"}` and the
site in `SITE_NAME_CODE`; responses are `{"total": n, "data": [...]}`.

This package only collects. The Host proves every submitted page is a response
it observed, keeps the capture as returned and publishes it into the ledger.
"""

from __future__ import annotations

import calendar
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

ROLES = (
    ("finance_quote_source", "quote"),
    ("finance_daxiang_s_source", "daxiang_s"),
    ("finance_self_pickup_source", "self_pickup"),
)
PAGE_SIZE = 500
MAX_PAGES = 40
CHINA = timezone(timedelta(hours=8))
FIELD_MAPPING = {
    "detail": {
        "source_key": "GUID", "settled_at": "BALANCE_DATE", "billed_at": "FINANCE_DATE",
        "item_name": "BALANCE_TYPE", "amount": "BALANCE_CUR_MONEY",
        "before_balance": "BALANCE_PRE_CONFIRM_MONEY", "after_balance": "BALANCE_BACK_CONFIRM_MONEY",
        "waybill_no": "BILL_CODE", "order_no": "BALANCE_ORDER", "site_code": "SITE_NAME_CODE",
        "site_name": "SITE_NAME", "source_type": "SOURCE_TYPE", "remark": "REMARK",
        "customer_name": "CUSTOMER_NAME", "goods_name": "GOODS_NAME",
    },
    "statement": {"item_name": "BALANCE_TYPE", "amount": "BALANCE_CUR_MONEY",
                  "site_code": "SITE_NAME_CODE", "site_name": "SITE_NAME"},
}
REQUEST_PROFILE = {
    "rows_pointer": "/data", "total_pointer": "/total", "page_fields": ["pageIndex", "pageSize"],
    "site_field": "SITE_NAME_CODE", "settlement_period_field": "BALANCE_DATE",
    "billing_period_field": "FINANCE_DATE",
    # 2026-10 实测：R+11 位与 10 位数字是运单号；S 开头的系统单号、17 位充值单号、
    # 19 位采购单号属于其他单据。只作为分类提示，归类仍由人工确认。
    "waybill_number_shapes": ["R+11", "10"],
}
ERROR_MESSAGES = {
    "MONTH_INVALID": "月份格式应为 YYYY-MM。",
    "MONTH_NOT_FINISHED": "只能拉取已经结束的月份。",
    "BLOCKED_LOGIN": "融辉财务账号未登录或登录态已失效，请在业务账号中重新登录后再拉取。",
    "SITE_CONTEXT_MISSING": "融辉账号登录信息里没有网点编码，请重新登录该账号后再拉取。",
    "PAGINATION_INCOMPLETE": "融辉返回的分页数据不完整，本次未保存，请稍后重试。",
    "STATEMENT_MISMATCH": "明细加总与统计查询不一致，本次未保存，请稍后重试或联系维护人员。",
}


class CaptureError(RuntimeError):
    pass


def _code(exc: BaseException) -> str:
    code = str(exc).strip()
    return code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) else "FINANCE_CAPTURE_FAILED"


def month_period(arguments, today: date) -> tuple[date, date]:
    text = str((arguments or {}).get("month") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", text):
        raise CaptureError("MONTH_INVALID")
    year, month = int(text[:4]), int(text[5:7])
    if not 1 <= month <= 12:
        raise CaptureError("MONTH_INVALID")
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    if end >= today:
        raise CaptureError("MONTH_NOT_FINISHED")
    return start, end


def _amount(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CaptureError("AMOUNT_INVALID") from exc


def check_capture(statement_pages, detail_pages) -> dict[str, object]:
    """The package's own completeness check; the Host repeats it independently."""

    def rows(pages):
        collected = [row for page in pages for row in page["response"]["data"]]
        if len(collected) != int(pages[0]["response"]["total"]):
            raise CaptureError("PAGINATION_INCOMPLETE")
        return collected

    statement, details = rows(statement_pages), rows(detail_pages)
    by_item = defaultdict(Decimal)
    for row in details:
        by_item[str(row.get("BALANCE_TYPE") or "")] += _amount(row.get("BALANCE_CUR_MONEY"))
    totals = defaultdict(Decimal)
    for row in statement:
        totals[str(row.get("BALANCE_TYPE") or "")] += _amount(row.get("BALANCE_CUR_MONEY"))
    if any(by_item.get(name, Decimal(0)) != totals.get(name, Decimal(0)) for name in set(by_item) | set(totals)):
        raise CaptureError("STATEMENT_MISMATCH")
    return {"items": len(statement), "rows": len(details), "net": f"{sum(totals.values(), Decimal(0)):.2f}"}


class _Client:
    def __init__(self, broker):
        self.broker = broker
        self.refs: list[str] = []
        self.requests = 0

    def _ref(self, result) -> str:
        reference = getattr(result, "host_evidence_ref", None)
        if not isinstance(reference, str) or not reference or len(reference) > 512:
            raise CaptureError("HOST_EVIDENCE_MISSING")
        self.refs.append(reference)
        return reference

    def site_code(self, role: str) -> str:
        result = self.broker("account.context", action="read", role=role, arguments={})
        self._ref(result)
        code = str(result.get("loginSiteCode") or "").strip()
        if not code:
            raise CaptureError("SITE_CONTEXT_MISSING")
        return code

    def pages(self, request: str, role: str, period_field: str, period: str, site: str) -> list[dict[str, object]]:
        result, collected, total = [], 0, None
        for index in range(MAX_PAGES):
            body = {period_field: period, "SITE_NAME_CODE": site, "pageIndex": str(index),
                    "pageSize": str(PAGE_SIZE), "sortField": "", "sortOrder": "", "totalColumns": "[]"}
            self.requests += 1
            response = self.broker("http.request", action="read_json", role=role,
                                   arguments={"request": request, "body": body})
            reference = self._ref(response)
            data = response.get("response")
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise CaptureError("PAGE_SHAPE_DRIFT")
            page_total = int(str(data.get("total")))
            if total is None:
                total = page_total
            elif page_total != total:
                raise CaptureError("PAGINATION_INCOMPLETE")
            result.append({"request": request, "body": body, "evidence_ref": reference, "response": data})
            collected += len(data["data"])
            if collected >= total:
                return result
            if not data["data"]:
                raise CaptureError("PAGINATION_INCOMPLETE")
        raise CaptureError("PAGINATION_INCOMPLETE")

    def connector(self, role: str, operation: str, arguments: dict[str, object]):
        result = self.broker("service.invoke", action=operation, role="__system__",
                             arguments={"service": f"connector.boyi.{role}@1", "operation": operation,
                                        "arguments": arguments})
        self._ref(result)
        return result


def run(broker, arguments, today: date | None = None):
    client = _Client(broker)
    today = today or datetime.now(CHINA).date()
    sources: list[dict[str, object]] = []
    try:
        start, end = month_period(arguments, today)
        period = json.dumps({"start": f"{start:%Y/%m/%d} 00:00:00", "end": f"{end:%Y/%m/%d} 23:59:59"},
                            ensure_ascii=False)
        for role, suffix in ROLES:
            site = client.site_code(role)
            statement = client.pages(f"statement_{suffix}", role, "BALANCE_DATE", period, site)
            billing = client.pages(f"statement_{suffix}", role, "FINANCE_DATE", period, site)
            details = client.pages(f"detail_{suffix}", role, "BALANCE_DATE", period, site)
            checked = check_capture(statement, details)
            written = client.connector(role, "write_platform_capture", {
                "schema_version": 1, "period_start": start.isoformat(), "period_end": end.isoformat(),
                "field_mapping": FIELD_MAPPING, "request_profile": REQUEST_PROFILE,
                "statement_pages": statement, "billing_statement_pages": billing, "detail_pages": details,
            })
            new_items = 0
            for day in written["days"]:
                receipt = client.connector(role, "publish_capture_day", {
                    "schema_version": 1, "capture_key": written["capture_key"], "business_date": day})
                new_items += int(receipt.get("new_fee_item_count") or 0)
            final = client.connector(role, "finalize_capture",
                                     {"schema_version": 1, "capture_key": written["capture_key"]})
            sources.append({
                "site_name": written["site_name"], "statement_items": checked["items"],
                "detail_rows": checked["rows"], "statement_net": checked["net"],
                "published_days": final["published_days"], "ledger_rows": final["record_count"],
                "new_fee_items": new_items, "batch_status": final["status"],
            })
        rows = sum(int(item["detail_rows"]) for item in sources)
        message = f"{start:%Y-%m} 已拉取 {len(sources)} 个网点，共 {rows} 行明细；" + "；".join(
            f"{item['site_name']} {item['statement_items']} 个项目、净额 {item['statement_net']}" for item in sources)
        return {
            "status": "SUCCESS",
            "data": {"month": f"{start:%Y-%m}", "sources": sources, "requests": client.requests, "message": message},
            "warnings": [], "error": None,
            "meta": {"source_system": "ronghui", "observed_at": datetime.now(timezone.utc).isoformat(),
                     "record_count": rows, "pagination_complete": True, "evidence_refs": client.refs},
        }
    except Exception as exc:
        code = _code(exc)
        message = ERROR_MESSAGES.get(code) or (
            "宿主核对抓取数据未通过：" + code if code.startswith("CAPTURE_") else "财务抓取未完成，失败原因：" + code)
        return {
            "status": "FAILED",
            "data": {"sources": sources, "requests": client.requests},
            "warnings": [],
            "error": {"code": code, "message": message, "retryable": False},
            "meta": {"source_system": "ronghui", "observed_at": datetime.now(timezone.utc).isoformat(),
                     "record_count": 0, "pagination_complete": False, "evidence_refs": client.refs},
        }
