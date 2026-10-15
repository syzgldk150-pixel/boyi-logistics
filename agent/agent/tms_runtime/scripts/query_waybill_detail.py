"""
Reusable waybill-detail bridge using the backend API behind the R7/TMS
"快件跟踪" page.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from agent.tms_runtime.scripts.login_manager import TMSAuth


DETAIL_URL = "https://tms.ronghuiwl.com/billEntity/getBillByCode"
CUSTOMER_FIELDS = (
    "sender_name",
    "sender_phone",
    "sender_address",
    "recipient_name",
    "recipient_phone",
    "recipient_address",
)

logger = logging.getLogger(__name__)

DETAIL_FIELD_MAP = {
    "tracking_number": "BILL_CODE",
    "goods_name": "GOODS_NAME",
    "package_type": "PACK_TYPE",
    "delivery_method": "DISPATCH_MODE",
    "quantity": "PIECE_NUMBER",
    "receipt_number": "R_BILLCODE",
    "actual_weight": "BILL_WEIGHT",
    "volume": "VOLUME",
    "remarks": "REMARK",
    "destination_station": "DESTINATION",
    "sender_name": "SEND_MAN",
    "sender_phone": "SEND_MAN_PHONE",
    "sender_address": "SEND_MAN_ADDRESS",
    "recipient_name": "ACCEPT_MAN",
    "recipient_phone": "ACCEPT_MAN_PHONE",
    "recipient_address": "ACCEPT_MAN_ADDRESS",
    "settlement_weight": "SETTLEMENT_WEIGHT",
    "volumetric_weight": "VOLUME_WEIGHT",
    "shipping_fee": "FREIGHT",
    "payment_type": "PAYMENT_TYPE",
    "pay_on_arrival": "TOPAYMENT",
}

def _normalize_bill_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_bill_codes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if parsed is not None:
            return _coerce_bill_codes(parsed)
        parts = [part.strip() for part in text.replace("；", ",").replace(";", ",").replace("\n", ",").split(",")]
        return [_normalize_bill_code(part) for part in parts if _normalize_bill_code(part)]
    if isinstance(raw, list):
        codes: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                candidate = (
                    item.get("bill_code")
                    or item.get("billCode")
                    or item.get("tracking_number")
                    or item.get("trackingNumber")
                    or item.get("mainWaybillNo")
                    or item.get("waybillNo")
                )
            else:
                candidate = item
            code = _normalize_bill_code(candidate)
            if code:
                codes.append(code)
        return codes
    if isinstance(raw, dict):
        for key in ("items", "bill_codes", "billCodes", "waybillNoList", "mainWaybillNoList"):
            if key in raw:
                return _coerce_bill_codes(raw.get(key))
        code = _normalize_bill_code(raw.get("bill_code") or raw.get("billCode"))
        return [code] if code else []
    return []


def _build_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://tms.ronghuiwl.com",
        "Referer": "https://tms.ronghuiwl.com/widget/home",
        "X-Requested-With": "XMLHttpRequest",
    }


def _extract_row(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    rows = ((payload.get("result") or {}).get("data")) if isinstance(payload.get("result"), dict) else None
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict):
            return first
    return None


def _map_row(row: dict[str, Any], requested_code: str) -> dict[str, Any]:
    mapped: dict[str, Any] = {"requested_bill_code": requested_code}
    for target_key, source_key in DETAIL_FIELD_MAP.items():
        mapped[target_key] = row.get(source_key)
    mapped["sender_address"] = _compose_address(
        row,
        province_key="SEND_PROVINCE",
        city_key="SEND_CITY",
        county_key="SEND_COUNTY",
        address_key="SEND_MAN_ADDRESS",
    ) or mapped.get("sender_address")
    mapped["recipient_address"] = _compose_address(
        row,
        province_key="ACCEPT_PROVINCE",
        city_key="ACCEPT_CITY",
        county_key="ACCEPT_COUNTY",
        address_key="ACCEPT_MAN_ADDRESS",
    ) or mapped.get("recipient_address")
    if not mapped.get("tracking_number"):
        mapped["tracking_number"] = requested_code
    return mapped


def _sign_status_fields(route_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not route_rows:
        return {
            "sign_status_checked": False,
            "is_signed": None,
            "actual_sign_time": "",
            "sign_site_name": "",
        }
    signed_rows = [
        row
        for row in route_rows
        if isinstance(row, dict) and str(row.get("SCAN_TYPE") or "").strip() == "签收"
    ]
    signed_rows.sort(key=lambda row: str(row.get("SCAN_DATE") or ""))
    latest = signed_rows[-1] if signed_rows else {}
    return {
        "sign_status_checked": True,
        "is_signed": bool(signed_rows),
        "actual_sign_time": str(latest.get("SCAN_DATE") or "").strip(),
        "sign_site_name": str(latest.get("SCAN_SITE") or "").strip(),
    }


def _is_masked(value: Any) -> bool:
    if value is None:
        return False
    return "*" in str(value)


def _has_value(value: Any) -> bool:
    return value not in (None, "")


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _normalize_region_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "|" in text:
        parts = [part.strip() for part in text.split("|") if part.strip()]
        if parts:
            return parts[-1]
    return text


def _compose_address(
    row: dict[str, Any] | None,
    *,
    province_key: str,
    city_key: str,
    county_key: str,
    address_key: str,
) -> str:
    if not isinstance(row, dict):
        return ""
    detail = str(row.get(address_key) or "").strip()
    detail_compact = _compact_text(detail)
    prefixes: list[str] = []
    for raw_part in (
        row.get(province_key),
        row.get(city_key),
        _normalize_region_value(row.get(county_key)),
    ):
        part = str(raw_part or "").strip()
        if not part or part in prefixes:
            continue
        part_compact = _compact_text(part)
        if part_compact and part_compact in detail_compact:
            continue
        prefixes.append(part)
    return "".join(prefixes + ([detail] if detail else []))


def _has_masked_customer(row: dict[str, Any]) -> bool:
    return any(_is_masked(row.get(key)) for key in CUSTOMER_FIELDS)


def _merge_rows(base_row: dict[str, Any], requested_code: str) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "requested_bill_code": requested_code,
        "tracking_number": requested_code,
    }
    merged.update({key: value for key, value in base_row.items() if value not in (None, "")})
    if not merged.get("tracking_number"):
        merged["tracking_number"] = requested_code
    return merged


def _query_one(
    session,
    bill_code: str,
    *,
    include_sign_status: bool = False,
    tracking_url: str = "",
) -> dict[str, Any] | None:
    response = session.post(
        DETAIL_URL,
        data={"billCode": bill_code, "isView": "true"},
        headers=_build_headers(),
        allow_redirects=False,
        timeout=20,
    )
    response.raise_for_status()
    row = _extract_row(response.json())
    if not row:
        return None
    mapped = _map_row(row, bill_code)
    if include_sign_status:
        from agent.tms_runtime.scripts import ronghui_tms_tracking

        route_rows = ronghui_tms_tracking.fetch_main_route_rows(
            session,
            bill_code,
            tracking_url=tracking_url,
        )
        mapped.update(_sign_status_fields(route_rows))
    return mapped


def _unique_codes(codes: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        ordered.append(code)
    return ordered


def _decrypt_masked_row(session, row: dict[str, Any], code: str, tracking_url: str) -> dict[str, Any]:
    """Re-read a masked row through the original page's decrypt request.

    A refused or failed decryption keeps the masked row, as callers already
    treat masked values as missing detail.
    """
    from agent.tms_runtime.scripts import ronghui_tms_tracking

    try:
        raw = ronghui_tms_tracking.fetch_view_detail_row(session, code, tracking_url=tracking_url)
    except ronghui_tms_tracking.RonghuiTmsTrackingError as exc:
        logger.warning("ronghui detail decryption unavailable bill=%s reason=%s", code, exc)
        return row
    decrypted = _map_row(raw, code)
    return {**row, **{key: decrypted.get(key) for key in CUSTOMER_FIELDS if _has_value(decrypted.get(key))}}


def _run_single_session(
    codes: list[str],
    *,
    include_sign_status: bool = False,
    decrypt_masked: bool = True,
) -> list[dict[str, Any]]:
    from agent.tms_runtime.scripts import ronghui_tms_tracking

    auth = TMSAuth()
    session = auth.login_and_get_session()
    tracking_url = ""
    if include_sign_status:
        tracking_url = ronghui_tms_tracking.resolve_tracking_page_url(session)
    results: list[dict[str, Any]] = []
    for code in codes:
        row = _query_one(
            session,
            code,
            include_sign_status=include_sign_status,
            tracking_url=tracking_url,
        )
        if row and decrypt_masked and _has_masked_customer(row):
            tracking_url = tracking_url or ronghui_tms_tracking.resolve_tracking_page_url(session)
            row = _decrypt_masked_row(session, row, code, tracking_url)
        if row:
            results.append(row)
    return results


def query_waybill_details(
    *,
    bill_codes: list[str],
    max_workers: int = 1,
    decrypt_masked: bool = True,
    browser_headless: bool = True,
    browser_timeout_ms: int = 30_000,
    browser_batch_size: int = 1,
    browser_max_workers: int = 1,
    include_sign_status: bool = False,
) -> list[dict[str, Any]]:
    # browser_* stay accepted for existing callers; decryption calls the page's
    # own decrypt request and no longer opens a browser.
    del browser_headless, browser_timeout_ms, browser_batch_size, browser_max_workers
    codes = _unique_codes(_normalize_bill_code(code) for code in bill_codes if _normalize_bill_code(code))
    if not codes:
        return []

    worker_count = max(1, min(int(max_workers or 1), len(codes)))
    ordered_map: dict[str, dict[str, Any]] = {}
    if worker_count == 1:
        rows = _run_single_session(codes, include_sign_status=include_sign_status, decrypt_masked=decrypt_masked)
    else:
        chunks: list[list[str]] = [[] for _ in range(worker_count)]
        for index, code in enumerate(codes):
            chunks[index % worker_count].append(code)
        rows = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    _run_single_session,
                    chunk,
                    include_sign_status=include_sign_status,
                    decrypt_masked=decrypt_masked,
                )
                for chunk in chunks
                if chunk
            ]
            for future in as_completed(futures):
                rows.extend(future.result())
    for row in rows:
        tracking = _normalize_bill_code(row.get("tracking_number"))
        if tracking:
            ordered_map[tracking] = row

    return [_merge_rows(ordered_map[code], code) for code in codes if code in ordered_map]


def run_once(params: Dict[str, Any]) -> Any:
    params = params or {}
    bill_codes = _coerce_bill_codes(
        params.get("bill_codes")
        or params.get("billCodes")
        or params.get("items")
        or params.get("waybillNoList")
        or params
    )
    max_workers = params.get("max_workers", 1)
    return query_waybill_details(
        bill_codes=bill_codes,
        max_workers=int(max_workers or 1),
        decrypt_masked=_coerce_bool(params.get("decrypt_masked"), default=True),
        browser_headless=_coerce_bool(params.get("browser_headless"), default=True),
        browser_timeout_ms=int(params.get("browser_timeout_ms", 30_000) or 30_000),
        browser_batch_size=int(params.get("browser_batch_size", 1) or 1),
        browser_max_workers=int(params.get("browser_max_workers", 1) or 1),
        include_sign_status=_coerce_bool(params.get("include_sign_status"), default=False),
    )


if __name__ == "__main__":
    import sys

    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    print(json.dumps(run_once(payload), ensure_ascii=False))
