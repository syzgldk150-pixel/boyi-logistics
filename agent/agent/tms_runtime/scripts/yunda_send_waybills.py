"""Fetch Yunda same-day send waybills with decrypted contact details."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from zoneinfo import ZoneInfo

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker

from agent.tms_runtime.scripts.yunda_original_data import ORIGINAL_DATA_URL, fetch_yunda_original_data
from shared.waybill_pagination import authoritative_total, collect_complete_pages
from service_v2_plugins.sync_yunda_send_waybills_v2.payload.yunda_send_fields import (
    FIELD_NAMES, delivery_method as _delivery_method, normalize_record,
)


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
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 50
# Per-waybill detail reads in flight at once. Sequential until concurrent reads
# are verified against the live platform (the 2026-11-11 live-query failure was
# the 100-row list limit, not concurrency, but 4 at once is still unverified).
ENRICH_MAX_WORKERS = 1
DATE_FIELD_NAME = "发件日期"
SOURCE_SEND_WAYBILL = "send_waybill"
SOURCE_SPECIAL_LINE = "special_line"
INTERNAL_SOURCE_FIELD = "_sync_source"


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


def _complete_page_shape(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """A successful paged snapshot needs actual rows and an exact total."""
    if not isinstance(payload, dict):
        raise ValueError("WAYBILL_PAGE_ROWS_INVALID")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = [value for value in (payload.get("rows"), data.get("rows"), data.get("list"),
        data.get("records"), payload.get("records"), payload.get("items")) if value is not None]
    if not candidates or any(not isinstance(value, list) or value != candidates[0] for value in candidates):
        raise ValueError("WAYBILL_PAGE_ROWS_INVALID")
    rows = candidates[0]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("WAYBILL_PAGE_ROWS_INVALID")
    totals = [authoritative_total(value) for value in
        (payload.get("total"), data.get("total"), payload.get("count"), data.get("count"))
        if value is not None]
    if not totals or len(set(totals)) != 1:
        raise ValueError("WAYBILL_PAGE_TOTAL_MISSING_OR_AMBIGUOUS")
    return rows, totals[0]


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


# Yunda rejects list requests above 100 rows ("rows必须小于等于 100", HTTP 400,
# observed 2026-11-11). Callers keep their page size; a larger logical page is
# read as consecutive 100-row upstream pages and merged.
YUNDA_LIST_MAX_ROWS = 100


def _post_list_page(
    session: Any,
    params: dict[str, Any],
    *,
    url: str,
    referer: str,
    form: dict[str, Any],
    label: str,
) -> Any:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": YUNDA_INMS_ORIGIN,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        url,
        data=form,
        headers=headers,
        allow_redirects=False,
        timeout=_coerce_int(params.get("request_timeout_sec"), 30),
    )
    return _decode_json_response(response, label=label)


def _capped_list_page(fetch_upstream: Any, *, page: int, page_size: int) -> Any:
    """One logical page of ``page_size`` rows from upstream pages of at most 100."""
    if page_size <= YUNDA_LIST_MAX_ROWS:
        return fetch_upstream(page, page_size)
    if page_size % YUNDA_LIST_MAX_ROWS:
        raise ValueError("WAYBILL_PAGE_SIZE_UNSUPPORTED")
    parts = page_size // YUNDA_LIST_MAX_ROWS
    rows: list[dict[str, Any]] = []
    total: int | None = None
    footer: Any = None
    for upstream_page in range((page - 1) * parts + 1, page * parts + 1):
        payload = fetch_upstream(upstream_page, YUNDA_LIST_MAX_ROWS)
        part_rows, part_total = _complete_page_shape(payload)
        if total is None:
            total, footer = part_total, payload.get("footer")
        elif part_total != total:
            raise ValueError("WAYBILL_PAGE_TOTAL_CHANGED")
        rows.extend(part_rows)
        if len(part_rows) < YUNDA_LIST_MAX_ROWS:
            break
    return {"rows": rows, "total": total, "footer": footer}


def fetch_send_page(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page: int,
    page_size: int,
) -> Any:
    return _capped_list_page(
        lambda upstream_page, rows: _post_list_page(
            session, params, url=SEND_LIST_URL, referer=SEND_INDEX_URL,
            form=_send_list_form(target_date, page=upstream_page, rows=rows), label="寄件运单列表"),
        page=page, page_size=page_size,
    )


def fetch_special_line_page(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page: int,
    page_size: int,
) -> Any:
    return _capped_list_page(
        lambda upstream_page, rows: _post_list_page(
            session, params, url=SPECIAL_LINE_LIST_URL, referer=SPECIAL_LINE_INDEX_URL,
            form=_special_line_list_form(target_date, page=upstream_page, rows=rows), label="寄件填仓列表"),
        page=page, page_size=page_size,
    )


def collect_send_rows(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page_size: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int | None]:
    return collect_complete_pages(
        lambda page: fetch_send_page(session, params, target_date=target_date, page=page, page_size=page_size),
        rows_from=lambda payload: _complete_page_shape(payload)[0],
        total_from=lambda payload: _complete_page_shape(payload)[1], identity="Logistics_Id",
        first_page=1, page_size=page_size, max_pages=max_pages,
    )


def collect_special_line_rows(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page_size: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], int | None]:
    return collect_complete_pages(
        lambda page: fetch_special_line_page(session, params, target_date=target_date, page=page, page_size=page_size),
        rows_from=lambda payload: _complete_page_shape(payload)[0],
        total_from=lambda payload: _complete_page_shape(payload)[1], identity="Logistics_Id",
        first_page=1, page_size=page_size, max_pages=max_pages,
    )


def _with_source(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    return [{**row, INTERNAL_SOURCE_FIELD: source} for row in rows]


def _merge_rows_by_waybill(*row_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rows in row_groups:
        source_seen: set[str] = set()
        for row in rows:
            bill_code = _clean_str(row.get("Logistics_Id"))
            if not bill_code or bill_code in source_seen:
                raise ValueError("WAYBILL_PAGE_IDENTITY_MISSING_OR_DUPLICATED")
            source_seen.add(bill_code)
            if bill_code not in seen:
                seen.add(bill_code)
                merged.append(row)
    return merged


def _extract_logistics(payload: Any, bill_code: str) -> dict[str, Any]:
    # Native mail rows include parent and child waybills. Their Logistics_Id
    # can all be the parent number, while the enclosing object key is the
    # actual requested entity. Never choose the first logistics object.
    matches: list[dict[str, Any]] = []
    def visit(value):
        if isinstance(value, dict):
            if bill_code in value and isinstance(value[bill_code], dict):
                matches.append(value[bill_code])
            for key, item in value.items():
                if key != "logistics":
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)
    if len(matches) != 1 or not isinstance(matches[0].get("logistics"), dict):
        raise ValueError("WAYBILL_EXACT_QUERY_EMPTY_OR_AMBIGUOUS")
    logistics = matches[0]["logistics"]
    if _clean_str(logistics.get("Logistics_Id")) != bill_code:
        raise ValueError("WAYBILL_EXACT_QUERY_IDENTITY_MISMATCH")
    smi = matches[0].get("smi")
    info = smi.get("info") if isinstance(smi, dict) else None
    scan_flags = info.get("scanFlag") if isinstance(info, dict) else None
    result = {key: value for key, value in logistics.items() if key != "Scan_Flag_Keys"}
    if isinstance(scan_flags, dict):
        result["Scan_Flag_Keys"] = list(scan_flags)
    return result


# Mail detail accepts several Ids[] at once; verified 2026-11-11 on the live
# account (5 waybills in one request matched 5 single requests exactly).
DETAIL_BATCH_SIZE = 5


def _post_mail_detail(session: Any, bill_codes: list[str], params: dict[str, Any], *, referer: str) -> Any:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": YUNDA_INMS_ORIGIN,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        MAIL_LIST_URL,
        data={"Ids[]": bill_codes if len(bill_codes) > 1 else bill_codes[0],
              "bringSub": "true", "page": 1, "history": "extra"},
        headers=headers,
        allow_redirects=False,
        timeout=_coerce_int(params.get("request_timeout_sec"), 30),
    )
    return _decode_json_response(response, label="快件跟踪详情")


def fetch_waybill_detail(session: Any, bill_code: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = _post_mail_detail(session, [bill_code], params, referer=f"{MAIL_INDEX_URL}?state={bill_code}&all=1")
    return _extract_logistics(payload, bill_code)


def fetch_waybill_details(session: Any, bill_codes: list[str], params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Mail details for many waybills, ``DETAIL_BATCH_SIZE`` per request.

    Each waybill is still matched by its exact object key. One the batch answer
    does not contain exactly once is read again on its own, never guessed.
    """
    details: dict[str, dict[str, Any]] = {}
    for start in range(0, len(bill_codes), DETAIL_BATCH_SIZE):
        chunk = bill_codes[start:start + DETAIL_BATCH_SIZE]
        payload = _post_mail_detail(session, chunk, params, referer=f"{MAIL_INDEX_URL}?all=1")
        for code in chunk:
            try:
                details[code] = _extract_logistics(payload, code)
            except ValueError:
                details[code] = fetch_waybill_detail(session, code, params)
    return details


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



def _masked_matches(listed: Any, stored: str) -> bool:
    """A masked list value like 188****4321 agrees with the stored full value."""
    shown = _clean_str(listed)
    if not shown:
        return True
    if "*" not in shown:
        return shown == stored
    head, tail = shown.split("*", 1)[0], shown.rsplit("*", 1)[-1]
    return len(stored) >= len(head) + len(tail) and stored.startswith(head) and stored.endswith(tail)


def _same_money(left: Any, right: Any) -> bool:
    try:
        return Decimal(_clean_str(left) or "0") == Decimal(_clean_str(right) or "0")
    except InvalidOperation:
        return False


def reuse_decrypted(row: dict[str, Any], stored: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Already decrypted contact and cost values for an unchanged waybill.

    Only when Yunda shows no modification (empty ``Modify_Date``), the stored
    values are complete and unmasked, the masked list phones agree with them
    and the freight is unchanged. Otherwise the caller decrypts again. Scan,
    signing and to-pay values always come from the current read.
    """
    if not stored or _clean_str(row.get("Modify_Date")):
        return None
    values = {key: _clean_str(stored.get(key)) for key in (
        "sender_name", "sender_phone", "sender_address", "receiver_name", "receiver_phone",
        "receiver_address", "recorded_cost", "receipt_required", "freight_fee")}
    needed = ("sender_phone", "sender_address", "receiver_phone", "receiver_address")
    if any(not values[key] or "*" in values[key] for key in needed):
        return None
    if not all(_masked_matches(row.get(key), values["receiver_phone"]) for key in ("Buyer_Mobile", "Buyer_Phone")):
        return None
    if not any(_masked_matches(row.get(key), values["sender_phone"]) and _clean_str(row.get(key))
               for key in ("Sender_Mobile", "Sender_Phone")):
        return None
    if not _same_money(row.get("Freight"), values["freight_fee"]):
        return None
    original = {
        "Sender_Name": values["sender_name"] or row.get("Sender_Name"), "Sender_Mobile": values["sender_phone"],
        "Sender_Address": values["sender_address"], "Buyer_Name": values["receiver_name"] or row.get("Buyer_Name"),
        "Buyer_Mobile": values["receiver_phone"], "Buyer_Address": values["receiver_address"],
    }
    renderer: dict[str, Any] = {}
    if row.get(INTERNAL_SOURCE_FIELD) == SOURCE_SEND_WAYBILL:
        if not values["recorded_cost"] or "*" in values["recorded_cost"] or values["receipt_required"] not in {"0", "1"}:
            return None
        renderer = {"Logistics_Id": _clean_str(row.get("Logistics_Id")),
                    "price": {"Total": values["recorded_cost"]}, "Delivers_Return": values["receipt_required"]}
    return original, renderer


def enrich_records(
    session: Any,
    rows: list[dict[str, Any]],
    params: dict[str, Any],
    *,
    target_date: dt.date,
    known: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    wanted = [row for row in rows if _clean_str(row.get("Logistics_Id"))]
    details = fetch_waybill_details(session, [_clean_str(row.get("Logistics_Id")) for row in wanted], params)

    def enrich(row: dict[str, Any]) -> dict[str, Any]:
        bill_code = _clean_str(row.get("Logistics_Id"))
        detail = details[bill_code]
        reused = reuse_decrypted(row, (known or {}).get(bill_code))
        if reused is not None:
            original, renderer = reused
        else:
            original = fetch_original_data(session, bill_code, params)
            renderer = (
                fetch_send_waybill_renderer(session, bill_code, row, params)
                if row.get(INTERNAL_SOURCE_FIELD) == SOURCE_SEND_WAYBILL
                else {}
            )
        return normalize_record(
            row, detail, original, renderer, target_date=target_date,
            source=str(row[INTERNAL_SOURCE_FIELD]),
        )

    if len(wanted) <= 1 or ENRICH_MAX_WORKERS <= 1:
        return [enrich(row) for row in wanted]
    # Results keep the list order; any failed waybill fails the whole read.
    with ThreadPoolExecutor(max_workers=min(ENRICH_MAX_WORKERS, len(wanted))) as pool:
        return list(pool.map(enrich, wanted))


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
