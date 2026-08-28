"""Fetch Yunda same-day send waybills with decrypted contact details."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker

try:
    from yunda_original_data import ORIGINAL_DATA_URL, fetch_yunda_original_data
except ImportError:  # pragma: no cover - package import fallback
    from agent.tms_runtime.scripts.yunda_original_data import ORIGINAL_DATA_URL, fetch_yunda_original_data


YUNDA_INMS_ORIGIN = "https://kyinms.yunda56.com"
SEND_LIST_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/sendwaybill/list.html"
SEND_INDEX_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/sendwaybill/index.html"
    "?page=tab&p=nil"
)
SEND_RENDERER_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/sendwaybill/renderer.html"
SPECIAL_LINE_INDEX_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/specialLine/specialLineManage/index.html"
    "?page=tab&p=nil"
)
SPECIAL_LINE_LIST_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/specialLine/specialLineManage/getList.html"
)
MAIL_INDEX_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/system/mail/index.html"
MAIL_LIST_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/system/mail/list.html"

DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 50
DATE_FIELD_NAME = "日期"
SOURCE_SEND_WAYBILL = "send_waybill"
SOURCE_SPECIAL_LINE = "special_line"
INTERNAL_SOURCE_FIELD = "_sync_source"

FIELD_NAMES = (
    "5.14编号",
    "目的网点",
    "收件区/县",
    "收件地址",
    "寄件人",
    "寄件手机",
    "收货人",
    "收货电话",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "实际重量",
    "现付",
    "月结",
    "提付",
    "中转运费",
    "回单号",
    "备注",
    "结算重量",
    "体积",
    "支付类型",
    "体积重",
    "到付款",
    DATE_FIELD_NAME,
)


def _target_date(params: dict[str, Any]) -> dt.date:
    raw = str(params.get("target_date") or "").strip()
    if raw:
        return dt.date.fromisoformat(raw[:10])
    return dt.datetime.now(DEFAULT_TZ).date()


def _coerce_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "") and _clean_str(value) != "":
            return value
    return ""


def _decimal_text_or_blank(value: Any) -> str:
    text = _clean_str(value).replace(",", "")
    if not text or text == "*":
        return ""
    try:
        Decimal(text)
    except (InvalidOperation, ValueError):
        return ""
    return text


def _auth_if_login_response(response: Any, body: str) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {301, 302, 401, 403}:
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录韵达账号。")
    content_type = str(getattr(response, "headers", {}).get("content-type") or "").lower()
    lower_body = body.lower()
    response_url = str(getattr(response, "url", "") or "").lower()
    location = str(getattr(response, "headers", {}).get("location") or "").lower()
    login_url = any(marker in response_url or marker in location for marker in ("ky-sso", "/login", "login.html"))
    password_form = any(
        marker in lower_body
        for marker in ('type="password"', "type='password'", 'name="password"', "name='password'")
    )
    sso_redirect = re.search(
        r"(?:window\.)?location(?:\.href)?\s*=\s*['\"][^'\"]*(?:ky-sso|sso\.yunda56\.com)[^'\"]*(?:login|passport)",
        lower_body,
    ) is not None
    explicit_login_page = (
        "login-form" in lower_body
        or "loginform" in lower_body
        or "\u9a8c\u8bc1\u7801\u767b\u5f55" in body
        or "\u5bc6\u7801\u767b\u5f55" in body
    )
    if "text/html" in content_type and (login_url or password_form or sso_redirect or explicit_login_page):
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录韵达账号。")
    if status_code == 200 and not body.strip():
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达接口返回空响应，请重新登录韵达账号。")


def _decode_json_response(response: Any, *, label: str) -> Any:
    body = str(getattr(response, "text", "") or "")
    _auth_if_login_response(response, body)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"韵达{label}接口返回非 JSON: {body[:120]}") from exc


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for value in (
        payload.get("rows"),
        data.get("rows"),
        data.get("list"),
        data.get("records"),
        payload.get("records"),
        payload.get("items"),
    ):
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _extract_total(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for value in (payload.get("total"), data.get("total"), payload.get("count"), data.get("count")):
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def _send_list_form(target_date: dt.date, *, page: int, rows: int) -> dict[str, Any]:
    date_text = target_date.isoformat()
    return {
        "timeType": "0",
        "start_date": date_text,
        "start_time": "00:00:00",
        "end_date": date_text,
        "end_time": "23:59:59",
        "sex": "0",
        "CreatedDotCode": "",
        "BuyerDestinationDotCodeSearch": "",
        "SearchSenderName": "",
        "SearchBuyerName": "",
        "IsSms": "0",
        "IsVipCustomer": "all",
        "PaymentType": "0",
        "PackageByCode": "",
        "ScanState": "1",
        "IsCod": "all",
        "SenderDistributionCodeSearch": "",
        "IsDiscount": "all",
        "MinFreight": "",
        "MaxFreight": "",
        "MinCollectionMoney": "",
        "MaxCollectionMoney": "",
        "OrderSource": "",
        "CreatedParentDotCode": "",
        "SearchShippingMethods": "all",
        "GoodsType": "all",
        "CreatedDotBusinessScope": "",
        "BusinessArea": "",
        "VipService": "0",
        "IsReturnLogistics": "all",
        "SignDotCode": "",
        "IsInProvince": "all",
        "ReturnType": "all",
        "ProductType": "all",
        "IsDPSP": "all",
        "IsYZH": "",
        "IsFixedCost": "all",
        "SignParentDotCode": "",
        "SearchShippingType": "all",
        "SearchNoElevator": "all",
        "IsDoubleNetWork": "all",
        "IsUnpacking": "all",
        "CustomerId": "",
        "IsHomeDecoration": "all",
        "IsSpecialArea": "all",
        "ShippingMethods": "all",
        "ShippingType": "all",
        "NoElevator": "all",
        "SenderName": "",
        "BuyerName": "",
        "page": page,
        "rows": rows,
    }


def _special_line_list_form(target_date: dt.date, *, page: int, rows: int) -> dict[str, Any]:
    date_text = target_date.isoformat()
    return {
        "timeType": "0",
        "start_date": date_text,
        "start_time": "00:00:00",
        "end_date": date_text,
        "end_time": "23:59:59",
        "sex": "0",
        "SendType": "1",
        "CreatedDotCode": "",
        "BuyerDestinationDotCode": "",
        "ScanState": "",
        "SpecialType": "ALL",
        "PaymentTypeSearch": "ALL",
        "DeliveryWay": "ALL",
        "SenderDistributionCodeSearch": "",
        "DestinationDotScope": "",
        "isReturnLogistics": "0",
        "IsCodSearch": "ALL",
        "ServiceTypes": "ALL",
        "page": page,
        "rows": rows,
    }


def fetch_send_page(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page: int,
    page_size: int,
) -> Any:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": YUNDA_INMS_ORIGIN,
        "Referer": SEND_INDEX_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        SEND_LIST_URL,
        data=_send_list_form(target_date, page=page, rows=page_size),
        headers=headers,
        allow_redirects=False,
        timeout=_coerce_int(params.get("request_timeout_sec"), 30),
    )
    return _decode_json_response(response, label="寄件运单列表")


def fetch_special_line_page(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page: int,
    page_size: int,
) -> Any:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": YUNDA_INMS_ORIGIN,
        "Referer": SPECIAL_LINE_INDEX_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        SPECIAL_LINE_LIST_URL,
        data=_special_line_list_form(target_date, page=page, rows=page_size),
        headers=headers,
        allow_redirects=False,
        timeout=_coerce_int(params.get("request_timeout_sec"), 30),
    )
    return _decode_json_response(response, label="寄件填仓列表")


def collect_send_rows(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page_size: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int | None]:
    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, max_pages + 1):
        payload = fetch_send_page(session, params, target_date=target_date, page=page, page_size=page_size)
        page_rows = _extract_rows(payload)
        if total is None:
            total = _extract_total(payload)
        if not page_rows:
            break
        rows.extend(page_rows)
        if total is not None and len(rows) >= total:
            break
        if len(page_rows) < page_size:
            break
    return rows, total


def collect_special_line_rows(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page_size: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int | None]:
    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, max_pages + 1):
        payload = fetch_special_line_page(session, params, target_date=target_date, page=page, page_size=page_size)
        page_rows = _extract_rows(payload)
        if total is None:
            total = _extract_total(payload)
        if not page_rows:
            break
        rows.extend(page_rows)
        if total is not None and len(rows) >= total:
            break
        if len(page_rows) < page_size:
            break
    return rows, total


def _with_source(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [{**row, INTERNAL_SOURCE_FIELD: source} for row in rows]


def _merge_rows_by_waybill(*row_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rows in row_groups:
        for row in rows:
            bill_code = _clean_str(row.get("Logistics_Id"))
            if not bill_code or bill_code in seen:
                continue
            seen.add(bill_code)
            merged.append(row)
    return merged


def _find_waybill_node(value: Any, bill_code: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        direct = value.get(bill_code)
        if isinstance(direct, dict):
            return direct
        if isinstance(value.get("logistics"), dict):
            return value
        for item in value.values():
            found = _find_waybill_node(item, bill_code)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_waybill_node(item, bill_code)
            if found is not None:
                return found
    return None


def _extract_logistics(payload: Any, bill_code: str) -> dict[str, Any]:
    node = _find_waybill_node(payload, bill_code)
    if not isinstance(node, dict):
        return {}
    logistics = node.get("logistics")
    return logistics if isinstance(logistics, dict) else {}


def fetch_waybill_detail(session: Any, bill_code: str, params: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": YUNDA_INMS_ORIGIN,
        "Referer": f"{MAIL_INDEX_URL}?state={bill_code}&all=1",
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        MAIL_LIST_URL,
        data={"Ids[]": bill_code, "bringSub": "true", "page": 1, "history": "now"},
        headers=headers,
        allow_redirects=False,
        timeout=_coerce_int(params.get("request_timeout_sec"), 30),
    )
    payload = _decode_json_response(response, label="快件跟踪详情")
    return _extract_logistics(payload, bill_code)


def fetch_original_data(session: Any, bill_code: str, params: dict[str, Any]) -> dict[str, Any]:
    return fetch_yunda_original_data(session, bill_code, params)


def fetch_send_waybill_renderer(
    session: Any,
    bill_code: str,
    row: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": YUNDA_INMS_ORIGIN,
        "Referer": SEND_INDEX_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        SEND_RENDERER_URL,
        data={
            "LogisticsId": bill_code,
            "createDotCode": _first(row.get("Created_Dot_Code"), row.get("CreatedDotCode")),
        },
        headers=headers,
        allow_redirects=False,
        timeout=_coerce_int(params.get("request_timeout_sec"), 30),
    )
    payload = _decode_json_response(response, label="寄件运单编辑详情")
    return payload if isinstance(payload, dict) else {}


def _delivery_method(row: dict[str, Any], detail: dict[str, Any]) -> str:
    raw = _clean_str(_first(row.get("Shipping_Methods"), detail.get("Shipping_Methods")))
    if raw == "231":
        return "送货进仓"
    if raw == "179":
        return "送货上楼"
    if raw in {"自提", "不上楼", "送货进仓", "送货上楼"}:
        return raw
    fallback = _clean_str(_first(row.get("Pickup_Method"), detail.get("Pickup_Method"), row.get("Shipping_Type_Name")))
    return fallback or "不上楼"


def normalize_record(
    row: dict[str, Any],
    detail: dict[str, Any],
    original: dict[str, Any],
    renderer: dict[str, Any],
    *,
    target_date: dt.date,
) -> dict[str, Any]:
    bill_code = _clean_str(_first(row.get("Logistics_Id"), detail.get("Logistics_Id")))
    payment_type = _clean_str(_first(row.get("Payment_Type"), detail.get("Payment_Type")))
    freight = _decimal_text_or_blank(_first(row.get("Special_Freight"), row.get("Freight"), detail.get("Freight")))
    cash_amount = freight if payment_type == "现金" else ""
    monthly_amount = freight if payment_type == "月结" else ""
    collect_amount = freight if payment_type == "到付" else ""
    renderer_price = renderer.get("price") if isinstance(renderer.get("price"), dict) else {}

    return {
        "5.14编号": bill_code,
        "目的网点": _first(row.get("Buyer_Destination_Dot_Name"), detail.get("Buyer_Destination_Dot_Code")),
        "收件区/县": _first(row.get("Buyer_Area_Name"), row.get("Buyer_Area"), detail.get("Buyer_Area_Name")),
        "收件地址": _first(original.get("Buyer_Address"), row.get("Buyer_Address"), detail.get("Buyer_Address")),
        "寄件人": _first(original.get("Sender_Name"), row.get("Sender_Name"), detail.get("Sender_Name")),
        "寄件手机": _first(
            original.get("Sender_Mobile"),
            row.get("Sender_Mobile"),
            detail.get("Sender_Mobile"),
            original.get("Sender_Phone"),
            row.get("Sender_Phone"),
            detail.get("Sender_Phone"),
        ),
        "收货人": _first(original.get("Buyer_Name"), row.get("Buyer_Name"), detail.get("Buyer_Name")),
        "收货电话": _first(
            original.get("Buyer_Mobile"),
            row.get("Buyer_Mobile"),
            detail.get("Buyer_Mobile"),
            original.get("Buyer_Phone"),
            row.get("Buyer_Phone"),
            detail.get("Buyer_Phone"),
        ),
        "货物名称": _first(row.get("Item_Name"), detail.get("Item_Name")),
        "包装类型": _first(row.get("Packing_Type"), detail.get("Packing_Type")),
        "派送方式": _delivery_method(row, detail),
        "件数": _first(row.get("Item_Total_Number"), detail.get("Item_Total_Number")),
        "实际重量": _first(row.get("Gross_Weight"), detail.get("Gross_Weight")),
        "现付": cash_amount,
        "月结": monthly_amount,
        "提付": collect_amount,
        # 编辑页“成本信息-总计”来自 renderer.price.Total；列表 Total_Money 是“实收总金额”。
        "中转运费": _decimal_text_or_blank(
            _first(
                renderer_price.get("Total"),
                row.get("Total_Cost_Money"),
                detail.get("Total_Cost_Money"),
            )
        ),
        "回单号": _first(row.get("Return_Logistics_Id"), detail.get("Return_Logistics_Id")),
        "备注": _first(row.get("Remarks"), detail.get("Remarks")),
        "结算重量": _first(row.get("Settlement_Total_Number"), detail.get("Settlement_Total_Number")),
        "体积": _first(row.get("Volume"), detail.get("Volume")),
        "支付类型": payment_type,
        "体积重": _first(detail.get("Extend_Field1"), row.get("Extend_Field1")),
        "到付款": _first(detail.get("COD"), row.get("COD")),
        DATE_FIELD_NAME: target_date.isoformat(),
    }


def enrich_records(
    session: Any,
    rows: list[dict[str, Any]],
    params: dict[str, Any],
    *,
    target_date: dt.date,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        bill_code = _clean_str(row.get("Logistics_Id"))
        if not bill_code:
            continue
        detail = fetch_waybill_detail(session, bill_code, params)
        original = fetch_original_data(session, bill_code, params)
        renderer = (
            fetch_send_waybill_renderer(session, bill_code, row, params)
            if row.get(INTERNAL_SOURCE_FIELD) == SOURCE_SEND_WAYBILL
            else {}
        )
        records.append(normalize_record(row, detail, original, renderer, target_date=target_date))
    return records


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    target_date = _target_date(params)
    page_size = _coerce_int(params.get("page_size") or params.get("rows"), DEFAULT_PAGE_SIZE)
    max_pages = _coerce_int(params.get("max_pages"), DEFAULT_MAX_PAGES)

    session_profile = str(params.get("session_profile") or "yunda").strip() or "yunda"
    broker = get_session_broker(session_profile)
    session = broker.build_requests_session(validate=False)
    send_rows, send_total = collect_send_rows(
        session,
        params,
        target_date=target_date,
        page_size=page_size,
        max_pages=max_pages,
    )
    special_rows, special_total = collect_special_line_rows(
        session,
        params,
        target_date=target_date,
        page_size=page_size,
        max_pages=max_pages,
    )
    rows = _merge_rows_by_waybill(
        _with_source(send_rows, SOURCE_SEND_WAYBILL),
        _with_source(special_rows, SOURCE_SPECIAL_LINE),
    )
    records = enrich_records(session, rows, params, target_date=target_date)
    total = sum(value for value in (send_total, special_total) if value is not None)
    return {
        "ok": True,
        "source": "yunda_send_waybills",
        "session_profile": session_profile,
        "target_date": target_date.isoformat(),
        "total": total if total else len(rows),
        "fetched": len(records),
        "source_counts": {
            SOURCE_SEND_WAYBILL: len(send_rows),
            SOURCE_SPECIAL_LINE: len(special_rows),
        },
        "records": records,
        "field_names": FIELD_NAMES,
    }


if __name__ == "__main__":
    import json
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run_once(json.loads(sys.stdin.read() or "{}")), ensure_ascii=False, default=str))
