"""
Reusable waybill-detail bridge using the backend API behind the R7/TMS
"快件跟踪" page.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
from typing import Any, Dict, Iterable, List, Optional

from agent.tms_runtime.scripts.login_manager import TMSAuth
from agent.tms_runtime.scripts.waybill_tracking import run_once as run_waybill_tracking_once


DETAIL_URL = "https://tms.ronghuiwl.com/billEntity/getBillByCode"

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

TRACKING_FIELD_MAP = {
    "运单编号": "tracking_number",
    "货物名称": "goods_name",
    "包装类型": "package_type",
    "派送方式": "delivery_method",
    "件数": "quantity",
    "回单号": "receipt_number",
    "实际重量": "actual_weight",
    "体积": "volume",
    "备注": "remarks",
    "目的站点": "destination_station",
    "发货人": "sender_name",
    "寄件人": "sender_name",
    "发货电话": "sender_phone",
    "寄件电话": "sender_phone",
    "发货地址": "sender_address",
    "寄件地址": "sender_address",
    "收件人": "recipient_name",
    "收件电话": "recipient_phone",
    "收件地址": "recipient_address",
    "结算重量": "settlement_weight",
    "体积重": "volumetric_weight",
    "运费": "shipping_fee",
    "支付类型": "payment_type",
    "到付款": "pay_on_arrival",
}

OVERLAY_OVERRIDE_FIELDS = {
    "recipient_name",
    "recipient_phone",
    "recipient_address",
    "sender_name",
    "sender_phone",
    "sender_address",
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


def _prefer_address(base_value: Any, overlay_value: Any) -> str:
    base_text = str(base_value or "").strip()
    overlay_text = str(overlay_value or "").strip()
    if not overlay_text:
        return base_text
    if not base_text:
        return overlay_text
    base_score = _address_quality_score(base_text)
    overlay_score = _address_quality_score(overlay_text)
    if overlay_score > base_score:
        return overlay_text
    return base_text


def _address_quality_score(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    score = len(_compact_text(text))
    if any(token in text for token in ("省", "市", "区", "县", "镇", "街道", "大道", "路", "号")):
        score += 20
    if "|" in text:
        score -= 5
    return score


def _needs_browser_overlay(row: dict[str, Any] | None) -> bool:
    if not row:
        return True
    if _is_masked(row.get("recipient_name")) or _is_masked(row.get("recipient_phone")):
        return True
    required_keys = ("goods_name", "recipient_name", "recipient_phone", "recipient_address", "destination_station")
    return any(not _has_value(row.get(key)) for key in required_keys)


def _map_tracking_row(row: dict[str, Any], requested_code: str) -> dict[str, Any]:
    mapped: dict[str, Any] = {
        "requested_bill_code": requested_code,
        "tracking_number": _normalize_bill_code(row.get("运单编号")) or requested_code,
    }
    for source_key, target_key in TRACKING_FIELD_MAP.items():
        value = row.get(source_key)
        if value not in (None, ""):
            mapped[target_key] = value
    return mapped


def _merge_rows(base_row: dict[str, Any] | None, overlay_row: dict[str, Any] | None, requested_code: str) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "requested_bill_code": requested_code,
        "tracking_number": requested_code,
    }
    if isinstance(base_row, dict):
        merged.update({key: value for key, value in base_row.items() if value not in (None, "")})
    if isinstance(overlay_row, dict):
        for key, value in overlay_row.items():
            if value in (None, ""):
                continue
            if key == "recipient_address":
                merged[key] = _prefer_address(merged.get(key), value)
                continue
            if key in OVERLAY_OVERRIDE_FIELDS or merged.get(key) in (None, ""):
                merged[key] = value
    if not merged.get("tracking_number"):
        merged["tracking_number"] = requested_code
    return merged


def _extract_tracking_result_code(row: dict[str, Any]) -> str:
    tracking_label = next(
        (source_key for source_key, target_key in TRACKING_FIELD_MAP.items() if target_key == "tracking_number"),
        "",
    )
    candidates = (
        row.get("requested_bill_code"),
        row.get("tracking_number"),
        row.get(tracking_label) if tracking_label else None,
        row.get("bill_code"),
        row.get("billCode"),
    )
    for candidate in candidates:
        code = _normalize_bill_code(candidate)
        if code:
            return code
    return ""


def _overlay_with_browser(
    *,
    bill_codes: list[str],
    headless: bool,
    timeout_ms: int,
    batch_size: int,
    max_workers: int,
) -> dict[str, dict[str, Any]]:
    if not bill_codes:
        return {}
    overlays: dict[str, dict[str, Any]] = {}

    # Delegate batching to waybill_tracking.py so one browser session can
    # decrypt multiple waybills instead of paying bootstrap cost per code.
    result = run_waybill_tracking_once(
        {
            "items": [{"bill_code": code} for code in bill_codes],
            "headless": headless,
            "timeout_ms": timeout_ms,
            "batch_size": max(1, batch_size),
            "max_workers": max(1, min(int(max_workers or 1), len(bill_codes))),
            "action_delay_sec": 0.2,
        }
    )
    if not isinstance(result, list):
        return overlays

    for row in result:
        if not isinstance(row, dict):
            continue
        code = _extract_tracking_result_code(row)
        if not code:
            continue
        mapped = _map_tracking_row(row, code)
        if any(
            value not in (None, "")
            for key, value in mapped.items()
            if key not in {"requested_bill_code", "tracking_number"}
        ):
            overlays[code] = mapped
    return overlays


def _query_one(session, bill_code: str) -> dict[str, Any] | None:
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
    return _map_row(row, bill_code)


def _unique_codes(codes: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        ordered.append(code)
    return ordered


def _run_single_session(codes: list[str]) -> list[dict[str, Any]]:
    auth = TMSAuth()
    session = auth.login_and_get_session()
    results: list[dict[str, Any]] = []
    for code in codes:
        row = _query_one(session, code)
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
) -> list[dict[str, Any]]:
    codes = _unique_codes(_normalize_bill_code(code) for code in bill_codes if _normalize_bill_code(code))
    if not codes:
        return []

    worker_count = max(1, min(int(max_workers or 1), len(codes)))
    ordered_map: dict[str, dict[str, Any]] = {}
    if worker_count == 1 or len(codes) == 1:
        for row in _run_single_session(codes):
            tracking = _normalize_bill_code(row.get("tracking_number"))
            if tracking:
                ordered_map[tracking] = row
    else:
        chunks: list[list[str]] = [[] for _ in range(worker_count)]
        for index, code in enumerate(codes):
            chunks[index % worker_count].append(code)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_run_single_session, chunk) for chunk in chunks if chunk]
            for future in as_completed(futures):
                for row in future.result():
                    tracking = _normalize_bill_code(row.get("tracking_number"))
                    if tracking:
                        ordered_map[tracking] = row

    browser_overlays: dict[str, dict[str, Any]] = {}
    if decrypt_masked:
        # 地址字段在接口里经常只有缩略值，浏览器详情页才是最终完整值。
        # 这里统一走浏览器补齐，再由合并逻辑覆盖姓名/电话/地址三项。
        overlay_codes = [
            code
            for code in codes
            if _needs_browser_overlay(ordered_map.get(code))
        ]
        browser_overlays = _overlay_with_browser(
            bill_codes=overlay_codes,
            headless=browser_headless,
            timeout_ms=max(5_000, int(browser_timeout_ms or 30_000)),
            batch_size=max(0, int(browser_batch_size or 0)),
            max_workers=max(1, int(browser_max_workers or 1)),
        )

    merged_rows: list[dict[str, Any]] = []
    for code in codes:
        base_row = ordered_map.get(code)
        overlay_row = browser_overlays.get(code)
        if base_row is None and overlay_row is None:
            continue
        merged_rows.append(_merge_rows(base_row, overlay_row, code))
    return merged_rows


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
    )


if __name__ == "__main__":
    import sys

    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    print(json.dumps(run_once(payload), ensure_ascii=False))
