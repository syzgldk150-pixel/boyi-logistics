"""Shared helpers for querying and classifying Ronghui TMS sign status."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tools.phase7_sync_common import require_explicit_account_id, tms_auth_error_result


WAYBILL_FIELD_NAME = "运单编号"
STATUS_FIELD_NAME = "签收状态"
PENDING_STATUS = "未签收"
SIGNED_STATUSES = {"签收", "已签收"}


def text_from_field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, list):
        return "".join(text_from_field_value(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "value", "name", "link"):
            text = text_from_field_value(value.get(key))
            if text:
                return text
        return "".join(text_from_field_value(item) for item in value.values()).strip()
    return str(value).strip()


def normalize_waybill(value: Any) -> str:
    text = text_from_field_value(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return "".join(text.split())


def normalize_status(value: Any) -> str:
    return text_from_field_value(value).replace(" ", "")


def is_signed_status(value: Any) -> bool:
    return normalize_status(value) in SIGNED_STATUSES


def is_pending_status(value: Any) -> bool:
    return normalize_status(value) == PENDING_STATUS


def status_by_code(rows: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        code = normalize_waybill(row.get(WAYBILL_FIELD_NAME))
        if not code:
            continue
        result[code] = normalize_status(row.get(STATUS_FIELD_NAME))
    return result


def query_delivery_status(
    codes: list[str],
    params: dict[str, Any],
    *,
    service_call: Callable[[str, dict[str, Any]], Any],
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    account_id = require_explicit_account_id(params, label="签收状态同步")
    batch_size = int(params.get("query_batch_size") or 100)
    timeout_sec = int(params.get("timeout_sec") or 600)
    status_map: dict[str, str] = {}
    raw_batches: list[dict[str, Any]] = []

    for start in range(0, len(codes), batch_size):
        batch = codes[start : start + batch_size]
        request_params = {"bill_codes": ",".join(batch), "account_id": account_id}
        for key in ("session_profile",):
            if params.get(key) not in (None, ""):
                request_params[key] = params[key]
        tms_result = service_call(
            "/delivery_status",
            {
                "params": request_params,
                "timeout_sec": timeout_sec,
            },
        )
        raw_batches.append(tms_result if isinstance(tms_result, dict) else {"raw": tms_result})
        if auth_error := tms_auth_error_result(tms_result):
            return None, auth_error
        rows = tms_result.get("data") if isinstance(tms_result, dict) else None
        if not isinstance(rows, list):
            return None, {"error": "delivery_status 返回格式异常", "raw": tms_result}
        status_map.update(status_by_code(rows))

    return status_map, {"batches": raw_batches, "batch_count": len(raw_batches)}
