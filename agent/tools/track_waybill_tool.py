"""Feishu/Agent tool for unified waybill tracking."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tracking_number_validation import validate_tracking_number
from tools.feishu_cli_tool import _spreadsheet_sheet_ref_map, feishu_operation
from tools.phase7_sync_common import get_workflow_resource, tms_auth_error_result
from tools.phase7_mysql_store import get_waybill_tracking_cache
from tools.tms_tool import call_http_service

MASK_ONLY_RE = re.compile(r"^[*＊Xx]+$")


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_tracking_number(value: Any) -> str:
    text = _clean_str(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def _extract_payload(tms_result: Any) -> dict[str, Any] | None:
    if not isinstance(tms_result, dict):
        return None
    data = tms_result.get("data")
    if isinstance(data, dict):
        return data
    if tms_result.get("type") or tms_result.get("route_rows"):
        return tms_result
    return None


def _is_masked_text(value: Any) -> bool:
    text = _clean_str(value)
    if not text:
        return False
    return "*" in text or "＊" in text or bool(MASK_ONLY_RE.fullmatch(text))


def _piece_text(value: Any) -> str:
    text = _clean_str(value)
    if not text:
        return ""
    return text if text.endswith("件") else f"{text} 件"


def _count_from_text(value: Any) -> int | None:
    text = _clean_str(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    return int(match.group(0))


def _is_ronghui_payload(payload: dict[str, Any], tracking_number: str) -> bool:
    payload_type = _clean_str(payload.get("type")).lower()
    if payload_type.startswith("ronghui"):
        return True
    code = _clean_str(payload.get("tracking_number")) or tracking_number
    return bool(re.fullmatch(r"(?:R|RC)\d+", code, flags=re.IGNORECASE) or code.startswith("200"))


def _needs_waybill_detail(payload: dict[str, Any]) -> bool:
    stub = payload.get("waybill_stub")
    if not isinstance(stub, dict):
        return True
    return (
        not _clean_str(stub.get("recipient_name"))
        or _is_masked_text(stub.get("recipient_name"))
        or not _clean_str(stub.get("recipient_phone"))
        or _is_masked_text(stub.get("recipient_phone"))
    )


def _merge_waybill_cache(payload: dict[str, Any], tracking_number: str) -> dict[str, Any]:
    lookup_code = _clean_str(payload.get("tracking_number")) or tracking_number
    try:
        cache = get_waybill_tracking_cache(lookup_code)
    except Exception as exc:
        enriched = dict(payload)
        enriched["waybill_cache_error"] = str(exc)[:200]
        return enriched
    if not cache:
        return payload

    enriched = dict(payload)
    stub = enriched.get("waybill_stub")
    stub = dict(stub) if isinstance(stub, dict) else {}

    cached_name = _clean_str(cache.get("recipient_name"))
    if cached_name and (not _clean_str(stub.get("recipient_name")) or _is_masked_text(stub.get("recipient_name"))):
        stub["recipient_name"] = cached_name

    cached_phone = _clean_str(cache.get("recipient_phone"))
    if cached_phone and (not _clean_str(stub.get("recipient_phone")) or _is_masked_text(stub.get("recipient_phone"))):
        stub["recipient_phone"] = cached_phone

    cached_goods_name = _clean_str(cache.get("goods_name"))
    if cached_goods_name and not _clean_str(stub.get("goods_name")):
        stub["goods_name"] = cached_goods_name

    cached_address = _clean_str(cache.get("recipient_address"))
    if cached_address and not _clean_str(stub.get("recipient_address")):
        stub["recipient_address"] = cached_address

    cached_destination = _clean_str(cache.get("destination_station") or cache.get("destination_site"))
    if cached_destination and not _clean_str(stub.get("disp_site")):
        stub["disp_site"] = cached_destination

    cached_quantity = cache.get("expected_quantity")
    if cached_quantity in (None, ""):
        cached_quantity = cache.get("quantity")
    if cached_quantity not in (None, "") and not _clean_str(stub.get("pieces")):
        stub["pieces"] = _piece_text(cached_quantity)

    if stub:
        enriched["waybill_stub"] = stub

    progress_keys = (
        "expected_quantity",
        "arrived_quantity",
        "pending_quantity",
        "first_arrival_at",
        "last_arrival_at",
        "arrival_status",
    )
    cached_progress = {
        key: cache.get(key)
        for key in progress_keys
        if cache.get(key) not in (None, "")
    }
    live_progress = enriched.get("arrival_progress")
    progress = dict(cached_progress)
    if isinstance(live_progress, dict):
        progress.update(
            {
                key: value
                for key, value in live_progress.items()
                if value not in (None, "")
            }
        )
        live_arrived = _count_from_text(live_progress.get("arrived_quantity"))
        expected = _count_from_text(progress.get("expected_quantity"))
        if live_arrived is not None:
            if "pending_quantity" not in live_progress:
                if expected is not None and live_arrived <= expected:
                    progress["pending_quantity"] = expected - live_arrived
                else:
                    progress.pop("pending_quantity", None)
            if "arrival_status" not in live_progress:
                if expected is None or expected <= 0 or live_arrived > expected:
                    progress.pop("arrival_status", None)
                elif live_arrived == 0:
                    progress["arrival_status"] = "pending"
                elif live_arrived == expected:
                    progress["arrival_status"] = "completed"
                else:
                    progress["arrival_status"] = "partial"
    if progress:
        enriched["arrival_progress"] = progress
    return enriched


def _sheet_values(result: Any) -> list[list[Any]]:
    if not isinstance(result, dict):
        return []
    values = (((result.get("data") or {}).get("valueRange") or {}).get("values") or result.get("values"))
    if not isinstance(values, list):
        return []
    return [row if isinstance(row, list) else [] for row in values]


def _range_with_header(value_range: str) -> str:
    text = _clean_str(value_range)
    if not text:
        return ""
    return re.sub(r"(!?[A-Z]+)2:", r"\g<1>1:", text, count=1)


def _header_col(headers: list[Any], aliases: tuple[str, ...]) -> int | None:
    labels = [_clean_str(item) for item in headers]
    for alias in aliases:
        for index, label in enumerate(labels):
            if not label:
                continue
            if alias == "件数":
                if label == "件数":
                    return index
                continue
            if alias in label:
                return index
    return None


def _arrival_progress_from_values(values: list[list[Any]], tracking_number: str) -> dict[str, Any]:
    if not values:
        return {}
    headers = values[0]
    bill_col = _header_col(headers, ("运单编号", "单号"))
    arrival_col = _header_col(headers, ("累计到货件数", "已到货件数", "到货件数"))
    if bill_col is None or arrival_col is None:
        return {}
    expected_col = _header_col(headers, ("货物件数", "货物总件数", "总货物件数", "开单件数", "应到件数", "件数"))
    expected_code = _normalize_tracking_number(tracking_number)
    for row in values[1:]:
        bill = _normalize_tracking_number(row[bill_col] if len(row) > bill_col else "")
        if bill != expected_code:
            continue
        arrived_text = _clean_str(row[arrival_col] if len(row) > arrival_col else "")
        if not arrived_text:
            return {}
        progress: dict[str, Any] = {"arrived_quantity": arrived_text}
        if expected_col is not None:
            expected_quantity = _count_from_text(row[expected_col] if len(row) > expected_col else "")
            if expected_quantity is not None:
                progress["expected_quantity"] = expected_quantity
                arrived_quantity = _count_from_text(arrived_text)
                if arrived_quantity is not None:
                    progress["pending_quantity"] = max(expected_quantity - arrived_quantity, 0)
        return progress
    return {}


def _read_arrival_progress_from_sheet(spreadsheet_token: Any, value_range: str, tracking_number: str) -> dict[str, Any]:
    token = _clean_str(spreadsheet_token)
    target_range = _range_with_header(value_range)
    if not token or not target_range:
        return {}
    result = feishu_operation(
        "read_sheet",
        {
            "spreadsheet_token": token,
            "range": target_range,
            "as": "bot",
        },
    )
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(str(result.get("error"))[:200])
    return _arrival_progress_from_values(_sheet_values(result), tracking_number)


def _route_date_titles(payload: dict[str, Any]) -> list[str]:
    dates: list[str] = []
    for key in ("route_rows",):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in ("scan_time", "time", "create_time", "operate_time"):
                match = re.search(r"\d{4}-\d{2}-\d{2}", _clean_str(row.get(field)))
                if match and match.group(0) not in dates:
                    dates.append(match.group(0))
    today = dt.date.today().isoformat()
    if today not in dates:
        dates.append(today)
    return dates


def _lookup_feishu_arrival_progress(payload: dict[str, Any], tracking_number: str) -> tuple[dict[str, Any], str]:
    for resource_key in ("phase7.arrive_primary_sheet", "phase7.arrive_secondary_sheet"):
        try:
            resource = get_workflow_resource(resource_key) or {}
            progress = _read_arrival_progress_from_sheet(
                resource.get("spreadsheet_token"),
                resource.get("range") or resource.get("clear_range") or "",
                tracking_number,
            )
        except Exception as exc:
            return {}, str(exc)[:200]
        if progress:
            progress["source"] = resource_key
            return progress, ""

    try:
        archive_resource = get_workflow_resource("phase7.stats_archive_sheet") or {}
    except Exception as exc:
        return {}, str(exc)[:200]
    archive_token = _clean_str(archive_resource.get("spreadsheet_token"))
    if archive_token:
        try:
            sheet_refs = _spreadsheet_sheet_ref_map(archive_token)
            for title in _route_date_titles(payload):
                sheet_id = sheet_refs.get(title)
                if not sheet_id:
                    continue
                progress = _read_arrival_progress_from_sheet(
                    archive_token,
                    f"{sheet_id}!A1:S5000",
                    tracking_number,
                )
                if progress:
                    progress["source"] = "phase7.stats_archive_sheet"
                    progress["source_sheet"] = title
                    return progress, ""
        except Exception as exc:
            return {}, str(exc)[:200]
    return {}, ""


def _merge_feishu_arrival_if_needed(
    payload: dict[str, Any],
    tracking_number: str,
) -> dict[str, Any]:
    progress = payload.get("arrival_progress")
    if isinstance(progress, dict) and progress.get("arrived_quantity") not in (None, ""):
        return payload
    if not _is_ronghui_payload(payload, tracking_number):
        return payload
    lookup_code = _clean_str(payload.get("tracking_number")) or tracking_number
    sheet_progress, error = _lookup_feishu_arrival_progress(payload, lookup_code)
    if not sheet_progress:
        if error:
            enriched = dict(payload)
            enriched["feishu_arrival_error"] = error
            return enriched
        return payload
    enriched = dict(payload)
    merged_progress = dict(progress) if isinstance(progress, dict) else {}
    merged_progress.update(sheet_progress)
    enriched["arrival_progress"] = merged_progress
    return enriched


def _extract_detail_payload(detail_result: Any) -> list[dict[str, Any]]:
    if not isinstance(detail_result, dict):
        return []
    candidates = (
        detail_result.get("items"),
        detail_result.get("records"),
        detail_result.get("data"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = candidate.get("items") or candidate.get("records") or candidate.get("data")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _matching_detail_row(detail_result: Any, tracking_number: str) -> dict[str, Any] | None:
    expected = _normalize_tracking_number(tracking_number)
    for row in _extract_detail_payload(detail_result):
        candidates = (
            row.get("tracking_number"),
            row.get("requested_bill_code"),
            row.get("bill_code"),
            row.get("billCode"),
            row.get("waybill_no"),
        )
        if any(_normalize_tracking_number(candidate) == expected for candidate in candidates):
            return row
    return None


def _merge_waybill_detail(payload: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(payload)
    stub = enriched.get("waybill_stub")
    stub = dict(stub) if isinstance(stub, dict) else {}

    detail_name = _clean_str(detail.get("recipient_name"))
    if detail_name and not _is_masked_text(detail_name) and (
        not _clean_str(stub.get("recipient_name")) or _is_masked_text(stub.get("recipient_name"))
    ):
        stub["recipient_name"] = detail_name

    detail_phone = _clean_str(detail.get("recipient_phone"))
    if detail_phone and not _is_masked_text(detail_phone) and (
        not _clean_str(stub.get("recipient_phone")) or _is_masked_text(stub.get("recipient_phone"))
    ):
        stub["recipient_phone"] = detail_phone

    detail_address = _clean_str(detail.get("recipient_address"))
    if detail_address and not _clean_str(stub.get("recipient_address")):
        stub["recipient_address"] = detail_address

    detail_destination = _clean_str(detail.get("destination_station") or detail.get("destination_site"))
    if detail_destination and not _clean_str(stub.get("disp_site")):
        stub["disp_site"] = detail_destination

    detail_goods_name = _clean_str(detail.get("goods_name"))
    if detail_goods_name and not _clean_str(stub.get("goods_name")):
        stub["goods_name"] = detail_goods_name

    detail_quantity = detail.get("quantity")
    if detail_quantity not in (None, "") and not _clean_str(stub.get("pieces")):
        stub["pieces"] = _piece_text(detail_quantity)

    if stub:
        enriched["waybill_stub"] = stub
    return enriched


def _merge_waybill_detail_if_needed(
    payload: dict[str, Any],
    tracking_number: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    lookup_code = _clean_str(payload.get("tracking_number")) or tracking_number
    if not _is_ronghui_payload(payload, lookup_code) or not _needs_waybill_detail(payload):
        return payload

    detail_params = {
        "bill_codes": [lookup_code],
        "headless": True,
        "timeout_ms": int(params.get("detail_timeout_ms") or 60000),
        "batch_size": 1,
        "max_workers": 1,
        "client_timeout_sec": int(params.get("detail_client_timeout_sec") or 90),
    }
    detail_result = call_http_service("/query_waybill_detail", detail_params)
    if auth_error := tms_auth_error_result(detail_result):
        enriched = dict(payload)
        enriched["waybill_detail_error"] = auth_error.get("error") or auth_error.get("error_code") or "AUTH_REQUIRED"
        return enriched
    if isinstance(detail_result, dict) and (detail_result.get("error") or detail_result.get("ok") is False):
        enriched = dict(payload)
        enriched["waybill_detail_error"] = str(detail_result.get("error") or detail_result.get("message") or "详情查询失败")[:200]
        return enriched

    detail = _matching_detail_row(detail_result, lookup_code)
    if not detail:
        enriched = dict(payload)
        enriched["waybill_detail_error"] = "详情查询未返回匹配单号"
        return enriched
    return _merge_waybill_detail(payload, detail)


def run_track_waybill(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    tracking_number = _normalize_tracking_number(
        params.get("tracking_number")
        or params.get("waybill_no")
        or params.get("bill_code")
        or params.get("ship_id")
    )
    if not tracking_number:
        return {"error": "缺少单号"}

    validation = validate_tracking_number(tracking_number, provider_hint=params.get("provider"))
    if validation.error:
        return validation.error_result()
    tracking_number = validation.tracking_number

    request_params = dict(params)
    request_params["tracking_number"] = tracking_number
    if validation.provider and not request_params.get("provider"):
        request_params["provider"] = validation.provider
    request_body = {
        "params": request_params,
        "timeout_sec": int(params.get("timeout_sec") or 180),
        "client_timeout_sec": int(params.get("client_timeout_sec") or 195),
    }
    tms_result = call_http_service("/tms/tracking_query", request_body)
    if auth_error := tms_auth_error_result(tms_result):
        return auth_error
    payload = _extract_payload(tms_result)
    if payload is None:
        return {"error": "tracking_query 返回格式异常", "raw": tms_result}
    if payload.get("ok") is False or payload.get("error"):
        return {
            "error": str(payload.get("error") or payload.get("message") or "单号查询失败"),
            "raw": payload,
        }
    enriched = _merge_waybill_cache(payload, tracking_number)
    enriched = _merge_feishu_arrival_if_needed(enriched, tracking_number)
    return _merge_waybill_detail_if_needed(enriched, tracking_number, params)


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_track_waybill(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
