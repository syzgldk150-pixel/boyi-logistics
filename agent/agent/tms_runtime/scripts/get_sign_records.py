"""Fetch complete Ronghui TMS sign records from the real sign-query page."""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.tms_runtime.scripts import customer_service_problem as page_support


SIGN_SUMMARY_CALL_ID = "FIND_SIGNED_TOTAL"
SIGN_DETAIL_CALL_ID = "FIND_SIGNED_DETAIL_ALL_EXCEL"
SIGN_SUMMARY_URL = f"{page_support.RONGHUI_ORIGIN}/dataQuery/findPageByCallId?id={SIGN_SUMMARY_CALL_ID}"
SIGN_DETAIL_URL = f"{page_support.RONGHUI_ORIGIN}/dataQuery/findPageByCallId?id={SIGN_DETAIL_CALL_ID}"
DATE_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 500


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _parse_datetime(value: Any, *, default: dt.datetime | None = None) -> dt.datetime:
    text = _clean(value)
    if not text:
        if default is None:
            raise ValueError("签收查询缺少时间范围")
        return default
    for fmt in (DATE_TIME_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            if fmt in {"%Y/%m/%d", "%Y-%m-%d"}:
                return dt.datetime.combine(parsed.date(), dt.time.min)
            return parsed
        except ValueError:
            continue
    raise ValueError(f"签收查询时间格式无效: {text}")


def _int(value: Any, *, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"签收查询响应 {field} 不是整数") from exc


def build_query_payload(
    start: dt.datetime,
    end: dt.datetime,
    *,
    login_site_code: str,
    page_index: int,
    page_size: int,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("签收查询开始时间不能晚于结束时间")
    if not _clean(login_site_code):
        raise RuntimeError("签收查询无法从真实登录态解析登录网点编号")
    date_range = json.dumps(
        {
            "start": start.strftime(DATE_TIME_FORMAT),
            "end": end.strftime(DATE_TIME_FORMAT),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "searchOrderType": "BILL_CODE",
        "searchOrderInput": "",
        "searchDateType": "SIGN_DATE",
        "SEARCH_DATE_RANGE": date_range,
        "SIGN_DATE": date_range,
        "LOGIN_SITE_CODE": login_site_code,
        "pageIndex": page_index,
        "pageSize": page_size,
        "sortField": "",
        "sortOrder": "",
        "totalColumns": "[]",
    }


def normalize_sign_row(row: dict[str, Any]) -> dict[str, Any]:
    bill_code = _clean(row.get("BILL_CODE"))
    signed_at = _clean(row.get("SIGN_DATE"))
    sign_site = _clean(row.get("SIGN_SITE"))
    if not bill_code or not signed_at or not sign_site:
        raise RuntimeError("TMS签收明细缺BILL_CODE、SIGN_DATE或SIGN_SITE")
    return {
        "扫描单号": bill_code,
        "扫描类型": "签收",
        "扫描时间": signed_at,
        "扫描网点": sign_site,
        "签收录入网点": _clean(row.get("RECORD_SITE")),
        "签收录入时间": _clean(row.get("RECORD_DATE")),
        "source": "ronghui_sign_query",
    }


def _collect_complete_pages(
    session: Any,
    *,
    url: str,
    label: str,
    headers: dict[str, str],
    base_payload: dict[str, Any],
    page_size: int,
    max_pages: int,
    timeout: float,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    expected_total: int | None = None
    for page_index in range(max_pages):
        response = session.post(
            url,
            data={
                **base_payload,
                "pageIndex": page_index,
                "pageSize": page_size,
            },
            headers=headers,
            timeout=timeout,
        )
        page_label = f"{label}第{page_index + 1}页"
        payload = page_support._response_json(response, label=page_label)
        page_support._raise_if_source_failed(payload, label=page_label)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError(f"{page_label}缺少data列表")
        page_rows = payload["data"]
        page_total = _int(payload.get("total"), field=f"{label} total")
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise RuntimeError(f"{label}分页过程中total发生变化")
        for raw in page_rows:
            if not isinstance(raw, dict):
                raise RuntimeError(f"{page_label}包含非对象行")
            rows.append(raw)
        if len(rows) >= expected_total:
            if len(rows) != expected_total:
                raise RuntimeError(f"{label}取得行数超过响应total")
            return rows, expected_total
        if not page_rows or len(page_rows) < page_size:
            raise RuntimeError(f"{label}提前结束: 已取得{len(rows)}行，响应total为{expected_total}")
    raise RuntimeError(f"{label}达到max_pages={max_pages}仍未完整结束")


def collect_sign_rows(
    session: Any,
    page_context: dict[str, str],
    *,
    start: dt.datetime,
    end: dt.datetime,
    login_site_code: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: float = 30,
) -> list[dict[str, Any]]:
    if page_size <= 0 or page_size > 200:
        raise ValueError("签收查询 page_size 必须在 1 到 200 之间")
    if max_pages <= 0:
        raise ValueError("签收查询 max_pages 必须大于 0")
    headers = page_support._ronghui_headers(
        page_context,
        content_type="application/x-www-form-urlencoded; charset=UTF-8",
    )
    base_payload = build_query_payload(
        start,
        end,
        login_site_code=login_site_code,
        page_index=0,
        page_size=page_size,
    )
    summary_rows, _ = _collect_complete_pages(
        session,
        url=SIGN_SUMMARY_URL,
        label="融辉签收汇总",
        headers=headers,
        base_payload=base_payload,
        page_size=page_size,
        max_pages=max_pages,
        timeout=timeout,
    )
    result: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    seen_groups: set[tuple[str, str]] = set()
    for group_index, summary_row in enumerate(summary_rows, start=1):
        sign_site_code = _clean(summary_row.get("SIGN_SITE_CODE"))
        area_name = _clean(summary_row.get("AREA_NAME"))
        if not sign_site_code or not area_name:
            raise RuntimeError(f"TMS签收汇总第{group_index}行缺SIGN_SITE_CODE或AREA_NAME")
        group_identity = (sign_site_code, area_name)
        if group_identity in seen_groups:
            raise RuntimeError(f"TMS签收汇总分组重复: {sign_site_code} {area_name}")
        seen_groups.add(group_identity)
        summary_total = _int(summary_row.get("TOTAL_NUM"), field="签收汇总 TOTAL_NUM")
        detail_rows, detail_total = _collect_complete_pages(
            session,
            url=SIGN_DETAIL_URL,
            label=f"融辉签收明细分组{group_index}",
            headers=headers,
            base_payload={
                **base_payload,
                "SIGN_SITE_CODE": sign_site_code,
                "AREA_NAME": area_name,
            },
            page_size=page_size,
            max_pages=max_pages,
            timeout=timeout,
        )
        if detail_total != summary_total:
            raise RuntimeError(
                f"TMS签收汇总与明细不一致: 分组{group_index}汇总{summary_total}行，明细{detail_total}行"
            )
        for raw in detail_rows:
            row = normalize_sign_row(raw)
            identity = (row["扫描单号"], row["扫描时间"])
            previous = seen.get(identity)
            if previous is not None:
                if previous != row:
                    raise RuntimeError(f"TMS签收明细重复冲突: {identity[0]} {identity[1]}")
                continue
            seen[identity] = row
            result.append(row)
    return result


def run_once(params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    params = params if isinstance(params, dict) else {}
    now = dt.datetime.now()
    start = _parse_datetime(params.get("start"), default=now - dt.timedelta(days=2))
    end = _parse_datetime(params.get("end"), default=now)
    page_size = int(params.get("page_size") or params.get("pageSize") or DEFAULT_PAGE_SIZE)
    max_pages = int(params.get("max_pages") or params.get("maxPages") or DEFAULT_MAX_PAGES)
    timeout = float(params.get("timeout") or 30)
    session = page_support._build_session("ronghui", params)
    context = page_support._resolve_ronghui_page_context(session, "签收查询")
    login_site_code = page_support._resolve_ronghui_login_site_code(session, params)
    return collect_sign_rows(
        session,
        context,
        start=start,
        end=end,
        login_site_code=login_site_code,
        page_size=page_size,
        max_pages=max_pages,
        timeout=timeout,
    )


def main() -> None:
    print(json.dumps(run_once(json.loads(sys.stdin.read() or "{}")), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
