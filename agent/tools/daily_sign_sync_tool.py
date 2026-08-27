"""Phase 7 第二批：每日应签 -> 多维表格 + 电子表格。"""

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from agent.tms_runtime.account_manager import get_account_manager
from agent.workflow_resource_store import get_workflow_resource
from tools.daily_sign_rules import (
    BUSINESS_TIMEZONE,
    MANUAL_POSTPONE_TYPES,
    build_ledger_row,
    business_now,
    clean_text,
    is_before_problem_cutoff,
    ledger_row_is_due,
    parse_datetime,
)
from tools.daily_sign_store import (
    DailySignPersistenceReadbackError,
    build_daily_sign_persistence_marker,
    finish_sync_run,
    latest_successful_sync_at,
    load_daily_sign_state,
    persist_daily_sign_snapshot,
    snapshot_fingerprint,
    start_sync_run,
    upsert_ledger_rows,
    upsert_problem_events,
    upsert_sign_events,
    upsert_sign_verification_states,
    verify_daily_sign_completed_run,
    verify_daily_sign_persistence,
)
from tools.daily_sign_readback import (
    DailySignReadbackError,
    verify_bitable_schema,
    verify_bitable_snapshot,
    verify_sheet_snapshot,
)
from tools.phase7_sync_common import (
    build_range_from_template,
    parse_a1_range,
    resolve_bitable_target,
    resolve_sheet_target,
    tms_auth_error_result,
)
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import get_waybill_tracking_cache
from tools.phase7_mysql_store import is_child_like_tracking, main_tracking_from_scan_code
from tools.tms_tool import call_http_service

DAILY_SIGN_SHEET_RESOURCE_KEY = "phase7.daily_sign_sheet"
DAILY_SIGN_BITABLE_RESOURCE_KEY = "phase7.daily_sign_bitable"
DAILY_SIGN_LEGACY_SHEET_COL_COUNT = 8
DAILY_SIGN_SHEET_COL_COUNT = 9
SHEET_HEADERS = [
    "运单编号",
    "R13应签收时间",
    "问题件后应签时间",
    "货物品名",
    "包装类型",
    "货物件数",
    "收件人地址",
    "送货方式",
    "到货件数",
]
LEGACY_VERBOSE_SHEET_HEADERS = [
    "运单编号",
    "融辉R13系统显示应签收时间",
    "实际到货问题件后计算应签时间",
    "货物品名",
    "包装类型",
    "货物件数",
    "收件人地址",
    "送货方式",
    "到货件数",
]
ARRIVED_QUANTITY_KEYS = (
    "arrived_quantity",
    "arrivedQuantity",
    "arrival_quantity",
    "arrivalQuantity",
    "arrival_count",
    "arrivalCount",
    "arrive_count",
    "arriveCount",
    "arrivePcs",
    "arrivedPcs",
    "到达件数",
    "到货件数",
    "已到货件数",
    "累计到货件数",
)
R13_REQUEST_KEYS = (
    "disp_site_code",
    "dispSiteCode",
    "start",
    "end",
    "days",
    "page_size",
    "pageSize",
    "page",
    "fetch_all",
    "fetchAll",
    "max_pages",
    "maxPages",
)
FORBIDDEN_R13_REQUEST_KEYS = frozenset(
    {
        "username",
        "password",
        "user",
        "pass",
        "config_path",
        "account_id",
        "accountId",
        "account_key",
        "accountKey",
        "r13_account_id",
        "r13AccountId",
    }
)


def _write_outcome_unknown(message: str, **details: Any) -> dict[str, Any]:
    return {
        "error": clean_text(message) or "每日应签写入结果无法核验。",
        "error_code": "WRITE_OUTCOME_UNKNOWN",
        **details,
    }


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _required_account_id(params: dict[str, Any], field: str) -> str:
    account_id = clean_text(params.get(field))
    if not account_id:
        raise ValueError(f"每日应签同步必须显式提供 {field}，禁止选择默认账号")
    return account_id


def _resolve_qianshou_request_body(params: dict) -> dict:
    request_body: dict[str, Any] = {}
    explicit_request_body = params.get("request_body")
    if isinstance(explicit_request_body, dict):
        forbidden = sorted(FORBIDDEN_R13_REQUEST_KEYS.intersection(explicit_request_body))
        if forbidden:
            raise ValueError(
                "request_body must not contain credentials or account selectors: "
                + ", ".join(forbidden)
            )
        request_body.update(explicit_request_body)

    for key in R13_REQUEST_KEYS:
        value = params.get(key)
        if _has_value(value):
            request_body[key] = value
    return request_body


def build_daily_sign_request_body(params: dict | None = None) -> dict:
    """Build a /get_qianshou request body without running any write-side sync work."""
    return _apply_r13_account_binding(_resolve_qianshou_request_body(params or {}), params or {})


def _apply_r13_account_binding(request_body: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    account_id = (
        params.get("r13_account_id")
        or params.get("r13AccountId")
        or request_body.get("r13_account_id")
        or request_body.get("r13AccountId")
    )
    if account_id in (None, ""):
        return request_body
    request_body["r13_account_id"] = account_id
    return get_account_manager().resolve_role_account_params(
        request_body,
        account_field="r13_account_id",
        output_account_field="",
        output_session_profile_field="",
    )


def _to_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_time(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("T", " ").replace(".000Z", "")


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _coerce_int(value: Any, *, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _arrived_quantity_from_mapping(row: dict[str, Any]) -> int | None:
    for key in ARRIVED_QUANTITY_KEYS:
        if key not in row:
            continue
        value = _to_int(row.get(key))
        if value is not None:
            return value
    return None


def _arrived_quantity_cell(row: dict[str, Any]) -> int | str:
    value = _arrived_quantity_from_mapping(row)
    return value if value is not None else ""


def _a1_row_count(value_range: str, *, default: int) -> int:
    try:
        return int(parse_a1_range(value_range)["row_count"])
    except Exception:
        return default


def _expand_a1_range(value_range: str, *, row_count: int, col_count: int) -> str:
    try:
        return build_range_from_template(value_range, row_count, col_count)
    except Exception:
        return value_range


def _sheet_params_for_values(params: dict, values: list[list[Any]]) -> dict:
    if not values:
        return params

    col_count = max(
        DAILY_SIGN_LEGACY_SHEET_COL_COUNT,
        max((len(row) for row in values if isinstance(row, list)), default=1),
    )
    resource: dict[str, Any] = {}
    value_range = _clean_text(params.get("range"))
    spreadsheet_token = _clean_text(params.get("spreadsheet_token"))

    if not value_range or not spreadsheet_token:
        try:
            resource = get_workflow_resource(DAILY_SIGN_SHEET_RESOURCE_KEY) or {}
        except Exception:
            resource = {}
        if not value_range:
            value_range = _clean_text(resource.get("range"))
        if not spreadsheet_token:
            spreadsheet_token = _clean_text(resource.get("spreadsheet_token"))

    if not value_range:
        return params

    next_params = dict(params)
    if spreadsheet_token:
        next_params["spreadsheet_token"] = spreadsheet_token
    next_params["range"] = _expand_a1_range(
        value_range,
        row_count=len(values),
        col_count=col_count,
    )

    clear_range = _clean_text(params.get("clear_range")) or _clean_text(resource.get("clear_range")) or value_range
    next_params["clear_range"] = _expand_a1_range(
        clear_range,
        row_count=_a1_row_count(clear_range, default=len(values)),
        col_count=col_count,
    )
    return next_params


def _address_quality_score(value: Any) -> int:
    text = _clean_text(value)
    if not text:
        return 0
    compact = text.replace(" ", "")
    score = len(compact.replace("*", ""))
    if "*" in compact:
        score -= 100
    if any(token in compact for token in ("省", "市", "区", "县", "镇", "街道", "大道", "路", "号")):
        score += 20
    return score


def _prefer_address(base_value: Any, detail_value: Any) -> str:
    base_text = _clean_text(base_value)
    detail_text = _clean_text(detail_value)
    if not detail_text:
        return base_text
    if not base_text:
        return detail_text
    if _address_quality_score(detail_text) > _address_quality_score(base_text):
        return detail_text
    return base_text


def _extract_rows(tms_result: dict) -> list[dict] | None:
    rows = tms_result.get("data") if isinstance(tms_result, dict) else None
    if isinstance(rows, list):
        return rows
    if isinstance(tms_result, list):
        return tms_result
    return None


def _unique_bill_codes(rows: list[dict]) -> list[str]:
    seen: set[str] = set()
    bill_codes: list[str] = []
    for row in rows:
        code = _clean_text(row.get("billNumberMain"))
        if not code or code in seen:
            continue
        seen.add(code)
        bill_codes.append(code)
    return bill_codes


def _detail_code(row: dict[str, Any]) -> str:
    for key in ("tracking_number", "requested_bill_code", "billNumberMain", "bill_code", "billCode", "运单编号"):
        code = _clean_text(row.get(key))
        if code:
            return code
    return ""


def _detail_address(row: dict[str, Any]) -> str:
    for key in ("recipient_address", "收件地址", "dispAddress", "ACCEPT_MAN_ADDRESS"):
        address = _clean_text(row.get(key))
        if address:
            return address
    return ""


def _build_detail_request(bill_codes: list[str], params: dict) -> dict:
    request_params: dict[str, Any] = {
        "items": [{"bill_code": bill_code} for bill_code in bill_codes],
        "max_workers": _coerce_int(params.get("detail_max_workers"), default=1),
        "decrypt_masked": _coerce_bool(params.get("detail_decrypt_masked"), default=True),
        "browser_headless": _coerce_bool(params.get("browser_headless"), default=True),
        "browser_timeout_ms": _coerce_int(params.get("browser_timeout_ms"), default=30_000),
        "browser_batch_size": _coerce_int(params.get("browser_batch_size"), default=10),
        "browser_max_workers": _coerce_int(params.get("browser_max_workers"), default=1),
    }
    if params.get("detail_session_profile") not in (None, ""):
        request_params["session_profile"] = params["detail_session_profile"]
    account_id = params.get("account_id") or params.get("accountId")
    if account_id not in (None, ""):
        request_params["account_id"] = account_id
    return {
        "params": request_params,
        "timeout_sec": _coerce_int(params.get("waybill_timeout_sec"), default=2400),
    }


def _fetch_address_details(rows: list[dict], params: dict) -> tuple[dict[str, str] | None, dict]:
    bill_codes = _unique_bill_codes(rows)
    if not bill_codes:
        return {}, {"ok": True, "requested": 0, "fetched": 0, "updated": 0}

    tms_result = call_http_service("/query_waybill_detail", _build_detail_request(bill_codes, params))
    if auth_error := tms_auth_error_result(tms_result):
        return None, {
            "error": f"query_waybill_detail 执行失败: {auth_error.get('error') or auth_error.get('error_code')}",
            "error_code": auth_error.get("error_code"),
            "raw": auth_error.get("raw", tms_result),
            "requested": len(bill_codes),
        }
    detail_rows = _extract_rows(tms_result)
    if detail_rows is None and isinstance(tms_result, dict) and tms_result.get("error"):
        return None, {
            "error": f"query_waybill_detail 执行失败: {tms_result.get('error')}",
            "raw": tms_result,
            "requested": len(bill_codes),
        }
    if detail_rows is None:
        return None, {
            "error": "query_waybill_detail 返回格式异常",
            "raw": tms_result,
            "requested": len(bill_codes),
        }

    addresses: dict[str, str] = {}
    for row in detail_rows:
        if not isinstance(row, dict):
            continue
        code = _detail_code(row)
        address = _detail_address(row)
        if code and address:
            addresses[code] = address
    return addresses, {"ok": True, "requested": len(bill_codes), "fetched": len(addresses)}


def _enrich_rows_with_detail_addresses(rows: list[dict], params: dict) -> tuple[list[dict], dict]:
    if not _coerce_bool(params.get("enrich_addresses"), default=True):
        return rows, {"ok": True, "skipped": True, "updated": 0}

    detail_addresses, result = _fetch_address_details(rows, params)
    if detail_addresses is None:
        return rows, result

    updated_rows: list[dict] = []
    updated_count = 0
    for row in rows:
        bill_code = _clean_text(row.get("billNumberMain"))
        next_row = dict(row)
        merged_address = _prefer_address(next_row.get("dispAddress"), detail_addresses.get(bill_code))
        if merged_address != _clean_text(next_row.get("dispAddress")):
            next_row["dispAddress"] = merged_address
            updated_count += 1
        updated_rows.append(next_row)

    return updated_rows, {**result, "updated": updated_count}


def _enrich_rows_with_arrival_quantities(rows: list[dict], params: dict) -> tuple[list[dict], dict]:
    if not _coerce_bool(params.get("enrich_arrival_counts"), default=True):
        return rows, {"ok": True, "skipped": True, "updated": 0, "found": 0, "missing": 0}

    cache_by_code: dict[str, dict[str, Any]] = {}
    updated_rows: list[dict] = []
    updated_count = 0
    found_count = 0
    missing_count = 0

    for row in rows:
        next_row = dict(row)
        existing_quantity = _arrived_quantity_from_mapping(next_row)
        if existing_quantity is not None:
            next_row["arrived_quantity"] = existing_quantity
            found_count += 1
            updated_rows.append(next_row)
            continue

        bill_code = _clean_text(next_row.get("billNumberMain"))
        if not bill_code:
            missing_count += 1
            updated_rows.append(next_row)
            continue

        if bill_code not in cache_by_code:
            try:
                cache_by_code[bill_code] = get_waybill_tracking_cache(bill_code)
            except Exception as exc:
                return rows, {
                    "error": f"到达件数查询失败: {type(exc).__name__}: {exc}",
                    "bill_code": bill_code,
                    "updated": updated_count,
                    "found": found_count,
                    "missing": missing_count,
                }

        cached_quantity = _arrived_quantity_from_mapping(cache_by_code[bill_code])
        if cached_quantity is None:
            missing_count += 1
            updated_rows.append(next_row)
            continue

        next_row["arrived_quantity"] = cached_quantity
        updated_count += 1
        found_count += 1
        updated_rows.append(next_row)

    return updated_rows, {
        "ok": True,
        "updated": updated_count,
        "found": found_count,
        "missing": missing_count,
    }


def _build_records(rows: list[dict]) -> list[dict]:
    return [
        {
            "fields": {
                "运单编号": row.get("billNumberMain", ""),
                "应签收时间": row.get("planSignTime", ""),
                "货物品名": row.get("goodsName", ""),
                "货物件数": _to_int(row.get("pcs")),
                "收件人地址": row.get("dispAddress", ""),
                "送货方式": row.get("dispatchMode", ""),
                "包装类型": row.get("packTypeDesc", ""),
            }
        }
        for row in rows
    ]


def _build_sheet_values(rows: list[dict]) -> list[list[Any]]:
    return [
        [
            str(row.get("billNumberMain", "")),
            _normalize_time(row.get("planSignTime")),
            str(row.get("goodsName", "")),
            str(row.get("packTypeDesc", "")),
            _to_int(row.get("pcs")) or 0,
            str(row.get("dispAddress", "")),
            str(row.get("dispatchMode", "")),
            _arrived_quantity_cell(row),
        ]
        for row in rows
    ]


def _sort_rows_by_plan_sign_time(rows: list[dict]) -> list[dict]:
    return [
        row
        for index, row in sorted(
            enumerate(rows),
            key=lambda item: (
                1 if not _normalize_time(item[1].get("planSignTime")) else 0,
                _normalize_time(item[1].get("planSignTime")),
                item[0],
            ),
        )
    ]


def _needs_address_detail(value: Any) -> bool:
    text = clean_text(value)
    return not text or "*" in text or "＊" in text


def _needs_waybill_detail(row: dict[str, Any], *, enrich_address: bool) -> bool:
    if enrich_address and _needs_address_detail(row.get("recipient_address")):
        return True
    return any(
        row.get(field) in (None, "")
        for field in (
            "goods_name",
            "package_type",
            "expected_quantity",
            "delivery_method",
        )
    )


def _enrich_missing_addresses(
    rows: list[dict[str, Any]],
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    enrich_address = params.get("enrich_addresses") is not False
    requested = [
        clean_text(row.get("tracking_number"))
        for row in rows
        if clean_text(row.get("tracking_number"))
        and _needs_waybill_detail(row, enrich_address=enrich_address)
    ]
    if not requested:
        return rows, {"ok": True, "requested": 0, "updated": 0}
    account_id = _required_account_id(params, "account_id")
    detail_result = call_http_service(
        "/query_waybill_detail",
        {
            "params": {
                "items": [{"bill_code": code} for code in requested],
                "account_id": account_id,
                "max_workers": int(params.get("detail_max_workers") or 1),
                "decrypt_masked": True,
                "browser_headless": bool(params.get("browser_headless", True)),
                "browser_batch_size": int(params.get("browser_batch_size") or 10),
            },
            "timeout_sec": int(params.get("waybill_timeout_sec") or 2400),
        },
    )
    if auth_error := tms_auth_error_result(detail_result):
        return rows, {"error": auth_error.get("error"), "raw": auth_error}
    details = _extract_rows(detail_result)
    if details is None:
        return rows, {"error": "query_waybill_detail 返回格式异常", "raw": detail_result}
    by_code = {_detail_code(row): row for row in details if _detail_code(row)}
    updated = 0
    output: list[dict[str, Any]] = []
    for row in rows:
        next_row = dict(row)
        detail = by_code.get(clean_text(row.get("tracking_number")), {})
        row_updated = False
        for target, source in (
            ("goods_name", "goods_name"),
            ("package_type", "package_type"),
            ("delivery_method", "delivery_method"),
        ):
            if next_row.get(target) in (None, "") and detail.get(source) not in (None, ""):
                next_row[target] = detail[source]
                row_updated = True
        if next_row.get("expected_quantity") in (None, ""):
            quantity = _to_int(detail.get("quantity"))
            if quantity is not None:
                next_row["expected_quantity"] = quantity
                row_updated = True
        detail_address = _detail_address(detail)
        if enrich_address and _address_quality_score(detail_address) > _address_quality_score(
            next_row.get("recipient_address")
        ):
            next_row["recipient_address"] = detail_address
            row_updated = True
        if row_updated:
            updated += 1
        output.append(next_row)
    return output, {
        "ok": True,
        "requested": len(requested),
        "fetched": len(by_code),
        "updated": updated,
    }


def _r13_by_code(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = clean_text(row.get("billNumberMain"))
        if not code:
            raise ValueError("R13 返回记录缺少 billNumberMain")
        if code in result and result[code] != row:
            raise ValueError(f"R13 返回重复冲突单号: {code}")
        result[code] = row
    return result


def _problem_query_window(params: dict[str, Any]) -> tuple[str, str]:
    now = business_now()
    explicit_start = clean_text(params.get("problem_start_date"))
    explicit_end = clean_text(params.get("problem_end_date"))
    if explicit_start or explicit_end:
        if not explicit_start or not explicit_end:
            raise ValueError("problem_start_date 与 problem_end_date 必须同时提供")
        return explicit_start, explicit_end
    latest = latest_successful_sync_at()
    if latest is None:
        backfill_days = params.get("problem_backfill_days")
        if backfill_days in (None, ""):
            raise ValueError(
                "首次问题件同步必须显式提供 problem_start_date/problem_end_date "
                "或 problem_backfill_days"
            )
        start = now - timedelta(days=int(backfill_days))
    else:
        start = latest - timedelta(days=2)
    return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def _sync_manual_problem_events(params: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    if params.get("skip_problem_sync"):
        return [], {"ok": True, "skipped": True, "complete": True, "rows": 0}
    account_id = _required_account_id(params, "account_id")
    date_from, date_to = _problem_query_window(params)
    page_size = min(max(int(params.get("problem_page_size") or 200), 1), 200)
    max_pages = max(int(params.get("problem_max_pages") or 500), 1)
    page_retries = min(max(int(params.get("problem_page_retries") or 3), 1), 5)
    retry_delay = max(float(params.get("problem_retry_delay_sec") or 1), 0)
    rows: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        failure: dict[str, Any] = {}
        for attempt in range(1, page_retries + 1):
            response = call_http_service(
                "/customer_service_problem",
                {
                    "params": {
                        "action": "query",
                        "platform": "ronghui",
                        "direction": "registered",
                        "account_id": account_id,
                        "filters": {
                            "direction": "registered",
                            "date_from": date_from,
                            "date_to": date_to,
                            "page": page,
                            "page_size": page_size,
                        },
                    },
                    "timeout_sec": int(params.get("problem_timeout_sec") or 600),
                },
            )
            if auth_error := tms_auth_error_result(response):
                return None, {"error": auth_error.get("error"), "complete": False, "page": page}
            payload = (
                response.get("data")
                if isinstance(response, dict) and isinstance(response.get("data"), dict)
                else response
            )
            page_rows = payload.get("rows") if isinstance(payload, dict) else None
            if isinstance(page_rows, list):
                break
            source_error = payload if isinstance(payload, dict) and payload.get("ok") is False else response
            source_error = source_error if isinstance(source_error, dict) else {}
            failure = {
                "error": clean_text(
                    source_error.get("message")
                    or source_error.get("error")
                    or "customer_service_problem 返回格式异常"
                )[:500],
                "error_code": clean_text(source_error.get("error_code")),
                "complete": False,
                "page": page,
                "attempts": attempt,
                "response_keys": sorted(response) if isinstance(response, dict) else [],
                "payload_keys": sorted(payload) if isinstance(payload, dict) else [],
            }
            if attempt < page_retries and retry_delay:
                time.sleep(retry_delay * attempt)
        else:
            return None, failure
        for raw in page_rows:
            if not isinstance(raw, dict):
                continue
            external_id = clean_text(raw.get("external_id"))
            registered_at = clean_text(raw.get("registered_at"))
            problem_type = clean_text(raw.get("problem_type"))
            tracking = clean_text(raw.get("waybill_no"))
            if not external_id or not registered_at or not tracking or not problem_type:
                return None, {"error": "问题件缺唯一ID、单号、准确类型或TMS登记时间", "complete": False}
            if external_id in seen and seen[external_id] != raw:
                return None, {"error": f"问题件重复冲突: {external_id}", "complete": False}
            seen[external_id] = raw
        rows.extend(page_rows)
        stats = payload.get("stats") if isinstance(payload, dict) and isinstance(payload.get("stats"), dict) else {}
        total = _to_int(stats.get("total"))
        if len(page_rows) < page_size or (total is not None and len(seen) >= total):
            break
    else:
        return None, {"error": f"问题件分页达到上限 {max_pages}", "complete": False}

    events = [
        {
            "source": "tms_manual_problem",
            "external_id": clean_text(row.get("external_id")),
            "tracking_number": clean_text(row.get("waybill_no")),
            "problem_type": clean_text(row.get("problem_type")),
            "registered_at": clean_text(row.get("registered_at")),
            "registered_site": clean_text(row.get("registered_site")),
            "upload_complete": True,
            "before_cutoff": is_before_problem_cutoff(row.get("registered_at")),
            "postpones_sign": clean_text(row.get("problem_type")) in MANUAL_POSTPONE_TYPES,
            "payload": row,
        }
        for row in seen.values()
    ]
    result = upsert_problem_events(events, dry_run=bool(params.get("dry_run", False)))
    return events, {**result, "complete": True, "rows": len(events)}


def _sign_query_window(params: dict[str, Any]) -> tuple[str, str]:
    now = business_now()
    explicit_start = clean_text(params.get("sign_start"))
    explicit_end = clean_text(params.get("sign_end"))
    if explicit_start or explicit_end:
        if not explicit_start or not explicit_end:
            raise ValueError("sign_start 与 sign_end 必须同时提供")
        return explicit_start, explicit_end
    latest = latest_successful_sync_at()
    if latest is None:
        backfill_days = params.get("sign_backfill_days")
        if backfill_days in (None, ""):
            raise ValueError(
                "首次签收同步必须显式提供 sign_start/sign_end 或 sign_backfill_days"
            )
        start = now - timedelta(days=int(backfill_days))
    else:
        start = latest - timedelta(days=2)
    return start.strftime("%Y/%m/%d %H:%M:%S"), now.strftime("%Y/%m/%d %H:%M:%S")


def _sync_sign_events(params: dict[str, Any], known_main_codes: set[str]) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    account_id = _required_account_id(params, "account_id")
    start, end = _sign_query_window(params)
    response = call_http_service(
        "/get_sign_records",
        {
            "params": {
                "start": start,
                "end": end,
                "page_size": int(params.get("sign_page_size") or 200),
                "max_pages": int(params.get("sign_max_pages") or 500),
                "account_id": account_id,
            },
            "timeout_sec": int(params.get("sign_timeout_sec") or 1200),
        },
    )
    if auth_error := tms_auth_error_result(response):
        return None, {"error": auth_error.get("error"), "complete": False}
    rows = _extract_rows(response)
    if rows is None:
        return None, {"error": "get_sign_records 签收查询返回格式异常", "complete": False, "raw": response}
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        scan_code = clean_text(row.get("扫描单号") or row.get("bill_code"))
        scan_type = clean_text(row.get("扫描类型") or row.get("scan_type"))
        scanned_at = clean_text(row.get("扫描时间") or row.get("scan_time"))
        scan_site = clean_text(row.get("扫描网点") or row.get("scan_site"))
        if not scan_code or scan_type != "签收" or not scanned_at or not scan_site:
            return None, {"error": "签收扫描缺单号、准确类型、扫描时间或扫描网点", "complete": False}
        main_code = main_tracking_from_scan_code(scan_code, known_main_codes)
        is_main = main_code == scan_code
        if not is_main:
            continue
        external_id = hashlib.sha256(
            "|".join((scan_code, scan_type, scanned_at, scan_site)).encode("utf-8")
        ).hexdigest()
        if external_id in seen:
            continue
        seen.add(external_id)
        events.append(
            {
                "source": "tms_scan",
                "external_id": external_id,
                "tracking_number": main_code,
                "scan_code": scan_code,
                "scan_type": scan_type,
                "scanned_at": scanned_at,
                "scan_site": scan_site,
                "is_main_waybill": True,
                "payload": row,
            }
        )
    result = upsert_sign_events(events, dry_run=bool(params.get("dry_run", False)))
    return events, {**result, "complete": True, "rows": len(events)}


def _r13_reports_signed(row: dict[str, Any]) -> bool:
    if clean_text(row.get("signTime") or row.get("signSiteName")):
        return True
    status = clean_text(row.get("isSigns"))
    return bool(status and status not in {"未签", "未签收", "否", "0", "false", "False"})


def _tracking_payload(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if isinstance(data, dict):
        return data
    if response.get("type") or isinstance(response.get("route_rows"), list):
        return response
    return None


def _persistence_readback_failure_message(exc: Exception) -> str:
    if isinstance(exc, DailySignPersistenceReadbackError):
        return f"每日应签权威持久化回读不匹配：{clean_text(exc)}"
    return "每日应签权威事件、账本或发布集合的新鲜回读不匹配。"


def _query_exact_main_sign(code: str, params: dict[str, Any]) -> dict[str, Any]:
    account_id = _required_account_id(params, "account_id")
    response = call_http_service(
        "/ronghui_tms_tracking",
        {
            "params": {
                "tracking_number": code,
                "account_id": account_id,
            },
            "timeout_sec": int(params.get("exact_sign_timeout_sec") or 180),
        },
    )
    if auth_error := tms_auth_error_result(response):
        return {"tracking_number": code, "error": auth_error.get("error")}
    payload = _tracking_payload(response)
    if payload is None or payload.get("ok") is False or payload.get("error"):
        error = payload.get("error") if isinstance(payload, dict) else "返回格式异常"
        return {"tracking_number": code, "error": clean_text(error) or "返回格式异常"}
    route_rows = payload.get("route_rows")
    if not isinstance(route_rows, list):
        return {"tracking_number": code, "error": "缺少主单扫描轨迹 route_rows"}
    sign_rows = [
        row
        for row in route_rows
        if isinstance(row, dict) and clean_text(row.get("scan_type")) == "签收"
    ]
    if not sign_rows:
        return {"tracking_number": code, "sign_event": None}
    sign_rows.sort(key=lambda row: parse_datetime(row.get("scan_time")) or datetime.min)
    row = sign_rows[-1]
    scanned_at = clean_text(row.get("scan_time"))
    scan_site = clean_text(row.get("scan_station"))
    if parse_datetime(scanned_at) is None or not scan_site:
        return {"tracking_number": code, "error": "主单签收轨迹缺扫描时间或扫描网点"}
    external_id = hashlib.sha256(
        "|".join((code, "签收", scanned_at, scan_site, "exact")).encode("utf-8")
    ).hexdigest()
    return {
        "tracking_number": code,
        "sign_event": {
            "source": "tms_tracking_exact",
            "external_id": external_id,
            "tracking_number": code,
            "scan_code": code,
            "scan_type": "签收",
            "scanned_at": scanned_at,
            "scan_site": scan_site,
            "is_main_waybill": True,
            "payload": row,
        },
    }


def _query_exact_sign_results(codes: list[str], params: dict[str, Any]) -> list[dict[str, Any]]:
    if not codes:
        return []
    workers = min(max(int(params.get("exact_sign_workers") or 4), 1), 8, len(codes))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # ``ContextVar`` values do not cross ``ThreadPoolExecutor`` boundaries
        # automatically.  Preserve the execution capability issued by the
        # governed tool runner so every nested read-only tracking query remains
        # authorized without broadening the target allowlist.
        futures = {
            executor.submit(copy_context().run, _query_exact_main_sign, code, params): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(
                    {
                        "tracking_number": code,
                        "error": f"精确主单轨迹查询异常：{type(exc).__name__}",
                    }
                )
    return results


def _sync_r13_sign_conflicts(
    params: dict[str, Any],
    r13_by_code: dict[str, dict[str, Any]],
    state: dict[str, Any],
    *,
    persist: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conflicts = sorted(
        code
        for code, row in r13_by_code.items()
        if _r13_reports_signed(row) and code not in state["signs"]
    )
    if not conflicts:
        return [], {"ok": True, "complete": True, "queried": 0, "confirmed": 0, "errors": []}
    raw_conflict_limit = params.get("exact_sign_conflict_limit")
    conflict_limit = min(
        max(int(50 if raw_conflict_limit in (None, "") else raw_conflict_limit), 0),
        500,
    )
    if len(conflicts) > conflict_limit:
        return [], {
            "ok": False,
            "complete": False,
            "queried": 0,
            "confirmed": 0,
            "errors": [
                {
                    "error": (
                        f"R13签收冲突单共 {len(conflicts)} 票，超过精确轨迹核验上限 "
                        f"{conflict_limit}；本次不得按R13状态关闭"
                    )
                }
            ],
        }
    results = _query_exact_sign_results(conflicts, params)
    confirmed_events = [result["sign_event"] for result in results if result.get("sign_event")]
    errors = [
        {"tracking_number": result.get("tracking_number"), "error": result.get("error")}
        for result in results
        if result.get("error")
    ]
    events = [] if errors else confirmed_events
    store_result: dict[str, Any]
    if persist:
        store_result = upsert_sign_events(
            events,
            dry_run=bool(params.get("dry_run", False)),
        )
    else:
        store_result = {"ok": True, "skipped": True, "upserted": 0}
    return events, {
        **store_result,
        "complete": not errors,
        "queried": len(conflicts),
        "confirmed": len(events),
        "errors": errors,
    }


def _has_historical_candidate_evidence(row: dict[str, Any]) -> bool:
    flags = {clean_text(flag) for flag in (row.get("data_quality_flags") or [])}
    return bool(
        parse_datetime(row.get("first_seen_r13_at"))
        or parse_datetime(row.get("last_seen_r13_at"))
        or parse_datetime(row.get("r13_plan_sign_at"))
        or clean_text(row.get("r13_sign_status"))
        or parse_datetime(row.get("first_arrival_date"))
        or parse_datetime(row.get("completion_date"))
        or (_to_int(row.get("arrived_quantity")) or 0) > 0
        or "backfilled_current_sign_sheet" in flags
    )


def _daily_sign_candidate_codes(
    r13_by_code: dict[str, dict[str, Any]],
    ledger_by_code: dict[str, dict[str, Any]],
    target_station_codes: set[str],
) -> tuple[set[str], set[str]]:
    raw_codes = (
        set(r13_by_code)
        | {
            code
            for code, row in ledger_by_code.items()
            if not bool(row.get("tms_signed")) and _has_historical_candidate_evidence(row)
        }
        | set(target_station_codes)
    )
    excluded_child_codes = {code for code in raw_codes if is_child_like_tracking(code)}
    return raw_codes - excluded_child_codes, excluded_child_codes


def _verification_backoff_days(consecutive_not_signed: int) -> int:
    if consecutive_not_signed <= 1:
        return 1
    if consecutive_not_signed == 2:
        return 3
    return 7


def _sync_historical_sign_verifications(
    params: dict[str, Any],
    r13_by_code: dict[str, dict[str, Any]],
    state: dict[str, Any],
    *,
    observed_at: datetime,
    persist: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidate_codes, excluded_child_codes = _daily_sign_candidate_codes(
        r13_by_code,
        state["ledger"],
        set(state["target_station_codes"]),
    )
    verification_by_code = state.get("sign_verifications") or {}
    due: list[tuple[bool, datetime, str]] = []
    for code in candidate_codes:
        if code in r13_by_code or code in state["signs"]:
            continue
        previous = state["ledger"].get(code) or {}
        verification = verification_by_code.get(code) or {}
        transitioned_out = bool(previous.get("r13_current"))
        next_check_at = parse_datetime(verification.get("next_check_at"))
        if transitioned_out or not verification or next_check_at is None or next_check_at <= observed_at:
            oldest_evidence = (
                parse_datetime(previous.get("first_seen_r13_at"))
                or parse_datetime(previous.get("first_arrival_date"))
                or datetime.min
            )
            due.append((transitioned_out, oldest_evidence, code))

    raw_verification_limit = params.get("exact_historical_sign_limit")
    verification_limit = min(
        max(int(8 if raw_verification_limit in (None, "") else raw_verification_limit), 0),
        500,
    )
    due.sort(key=lambda item: (not item[0], item[1], item[2]))
    selected = [code for _transitioned, _evidence, code in due[:verification_limit]]
    results = _query_exact_sign_results(selected, params)
    confirmed_events = [result["sign_event"] for result in results if result.get("sign_event")]
    errors = [
        {"tracking_number": result.get("tracking_number"), "error": result.get("error")}
        for result in results
        if result.get("error")
    ]
    verification_rows: list[dict[str, Any]] = []
    for result in results:
        code = clean_text(result.get("tracking_number"))
        previous = verification_by_code.get(code) or {}
        if result.get("error"):
            verification_rows.append(
                {
                    "tracking_number": code,
                    "last_checked_at": observed_at,
                    "last_result": "error",
                    "next_check_at": observed_at + timedelta(hours=6),
                    "consecutive_not_signed": _to_int(previous.get("consecutive_not_signed")) or 0,
                    "last_error": clean_text(result.get("error")),
                }
            )
            continue
        if result.get("sign_event"):
            verification_rows.append(
                {
                    "tracking_number": code,
                    "last_checked_at": observed_at,
                    "last_result": "signed",
                    "next_check_at": None,
                    "consecutive_not_signed": 0,
                    "last_error": None,
                }
            )
            continue
        consecutive = (_to_int(previous.get("consecutive_not_signed")) or 0) + 1
        verification_rows.append(
            {
                "tracking_number": code,
                "last_checked_at": observed_at,
                "last_result": "not_signed",
                "next_check_at": observed_at + timedelta(days=_verification_backoff_days(consecutive)),
                "consecutive_not_signed": consecutive,
                "last_error": None,
            }
        )

    # Deletion protection is batch-atomic: any exact-tracking error means no newly
    # confirmed event from this batch may close an existing published row.
    if errors:
        for row in verification_rows:
            if row["last_result"] == "signed":
                row.update(
                    {
                        "last_result": "error",
                        "next_check_at": observed_at + timedelta(hours=6),
                        "last_error": "batch_deferred_after_peer_query_error",
                    }
                )
    events = [] if errors else confirmed_events
    if persist:
        event_store = upsert_sign_events(
            events,
            dry_run=bool(params.get("dry_run", False)),
        )
        verification_store = upsert_sign_verification_states(
            verification_rows,
            dry_run=bool(params.get("dry_run", False)),
        )
    else:
        event_store = {"ok": True, "skipped": True, "upserted": 0}
        verification_store = {"ok": True, "skipped": True, "upserted": 0}
    return events, {
        "ok": not errors,
        "complete": not errors,
        "eligible": len(due),
        "queried": len(selected),
        "deferred_by_limit": max(len(due) - len(selected), 0),
        "confirmed": len(events),
        "confirmed_but_deferred_on_error": len(confirmed_events) if errors else 0,
        "not_signed": sum(1 for row in verification_rows if row["last_result"] == "not_signed"),
        "excluded_child_codes": sorted(excluded_child_codes),
        "errors": errors,
        "event_store": event_store,
        "verification_store": verification_store,
        "verification_rows": verification_rows,
    }

def _bitable_time(value: Any, field_type: int) -> Any:
    parsed = parse_datetime(value)
    if parsed is None:
        return None if field_type == 5 else ""
    if field_type == 5:
        return int(parsed.replace(tzinfo=BUSINESS_TIMEZONE).timestamp() * 1000)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _build_ledger_records(rows: list[dict[str, Any]], date_field_type: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "fields": {
                "运单编号": row.get("tracking_number", ""),
                "R13应签收时间": _bitable_time(row.get("r13_plan_sign_at"), date_field_type),
                "问题件后应签时间": _bitable_time(row.get("system_sign_due_at"), date_field_type),
                "货物品名": clean_text(row.get("goods_name")),
                "包装类型": clean_text(row.get("package_type")),
                "货物件数": _to_int(row.get("expected_quantity")),
                "收件人地址": clean_text(row.get("recipient_address")),
                "送货方式": clean_text(row.get("delivery_method")),
                "到货件数": _to_int(row.get("arrived_quantity")),
            }
        }
        for row in rows
    ]


def _build_ledger_sheet_values(rows: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            clean_text(row.get("tracking_number") or row.get("billNumberMain")),
            _normalize_time(row.get("r13_plan_sign_at") or row.get("planSignTime")),
            _normalize_time(row.get("system_sign_due_at")),
            clean_text(row.get("goods_name") or row.get("goodsName")),
            clean_text(row.get("package_type") or row.get("packTypeDesc")),
            _to_int(row.get("expected_quantity") if "expected_quantity" in row else row.get("pcs")) or "",
            clean_text(row.get("recipient_address") or row.get("dispAddress")),
            clean_text(row.get("delivery_method") or row.get("dispatchMode")),
            _to_int(row.get("arrived_quantity")) if _to_int(row.get("arrived_quantity")) is not None else "",
        ]
        for row in rows
    ]


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            _normalize_time(row.get("system_sign_due_at")) or _normalize_time(row.get("r13_plan_sign_at")) or "9999",
            clean_text(row.get("tracking_number")),
        ),
    )


def _sheet_values(payload: Any) -> list[list[Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    value_range = payload.get("valueRange") if isinstance(payload.get("valueRange"), dict) else {}
    nested = data.get("valueRange") if isinstance(data.get("valueRange"), dict) else {}
    for candidate in (nested.get("values"), value_range.get("values"), data.get("values"), payload.get("values")):
        if isinstance(candidate, list):
            return [row if isinstance(row, list) else [] for row in candidate]
    return []


def _sync_sheet(rows: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    spreadsheet_token, configured_range = resolve_sheet_target(params, DAILY_SIGN_SHEET_RESOURCE_KEY)
    info = parse_a1_range(configured_range)
    header_range = f"{info['sheet']}!A1:I1"
    read_result = feishu_operation(
        "read_sheet",
        {"spreadsheet_token": spreadsheet_token, "range": header_range, "as": params.get("as", "bot")},
    )
    if read_result.get("error"):
        return {"error": f"读取应签表头失败: {read_result.get('error')}"}
    values = _sheet_values(read_result)
    actual_headers = [clean_text(value) for value in (values[0] if values else [])]
    legacy_eight_headers_match = (
        actual_headers[:DAILY_SIGN_LEGACY_SHEET_COL_COUNT]
        == SHEET_HEADERS[:DAILY_SIGN_LEGACY_SHEET_COL_COUNT]
        and (
            len(actual_headers) < DAILY_SIGN_SHEET_COL_COUNT
            or not actual_headers[DAILY_SIGN_SHEET_COL_COUNT - 1]
        )
    )
    legacy_verbose_headers_match = (
        actual_headers[:DAILY_SIGN_SHEET_COL_COUNT] == LEGACY_VERBOSE_SHEET_HEADERS
    )
    if legacy_eight_headers_match or legacy_verbose_headers_match:
        header_write = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": header_range,
                "values": [SHEET_HEADERS],
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if header_write.get("error"):
            return _write_outcome_unknown("每日应签电子表格补充到货件数表头后的终态未知。")
        fresh_header = feishu_operation(
            "read_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": header_range,
                "as": params.get("as", "bot"),
            },
        )
        if fresh_header.get("error"):
            return _write_outcome_unknown("每日应签电子表格表头新鲜回读不可用。")
        fresh_values = _sheet_values(fresh_header)
        actual_headers = [
            clean_text(value) for value in (fresh_values[0] if fresh_values else [])
        ]
    if actual_headers[:DAILY_SIGN_SHEET_COL_COUNT] != SHEET_HEADERS:
        actual_summary = "、".join(actual_headers[:DAILY_SIGN_SHEET_COL_COUNT]) or "未读取到表头"
        return {
            "error": f"应签明细表头不一致，停止写入；当前表头：{actual_summary}",
            "expected_headers": SHEET_HEADERS,
            "actual_headers": actual_headers,
        }
    sheet_values = _build_ledger_sheet_values(rows)
    write_range = build_range_from_template(
        f"{info['sheet']}!A2:I2", max(len(sheet_values), 1), DAILY_SIGN_SHEET_COL_COUNT
    )
    write_result: dict[str, Any] = {"ok": True, "skipped": True, "rows": 0}
    if sheet_values:
        write_result = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": write_range,
                "values": sheet_values,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if write_result.get("error"):
            return _write_outcome_unknown("写入应签明细后的终态未知。")
    old_end_row = max(info["end_row"], 2)
    tail_start = 2 + len(sheet_values)
    clear_result: dict[str, Any] = {"ok": True, "skipped": True}
    if tail_start <= old_end_row:
        clear_result = feishu_operation(
            "clear_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": f"{info['sheet']}!A{tail_start}:I{old_end_row}",
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if clear_result.get("error"):
            return _write_outcome_unknown("清理应签明细旧行后的终态未知。")
    readback_end = max(old_end_row, 1 + len(sheet_values))
    readback_range = f"{info['sheet']}!A2:I{readback_end}"
    readback_result = feishu_operation(
        "read_sheet",
        {
            "spreadsheet_token": spreadsheet_token,
            "range": readback_range,
            "as": params.get("as", "bot"),
        },
    )
    if readback_result.get("error"):
        return _write_outcome_unknown("每日应签电子表格新鲜回读不可用。")
    try:
        readback = verify_sheet_snapshot(
            sheet_values,
            _sheet_values(readback_result),
            observed_row_capacity=readback_end - 1,
            columns=DAILY_SIGN_SHEET_COL_COUNT,
        )
    except DailySignReadbackError:
        return _write_outcome_unknown("每日应签电子表格新鲜回读不匹配。")
    return {
        "ok": True,
        "rows": len(sheet_values),
        "write_result": write_result,
        "clear_result": clear_result,
        "readback": readback,
    }


def _field_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(result.get("items"), list):
        return [item for item in result["items"] if isinstance(item, dict)]
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return [item for item in data.get("items", []) if isinstance(item, dict)]


def _ensure_bitable_schema(base_token: str, table_id: str, params: dict[str, Any]) -> dict[str, Any]:
    result = feishu_operation("list_fields", {"base_token": base_token, "table_id": table_id, "as": params.get("as", "bot")})
    if result.get("error"):
        return {"error": result["error"]}
    by_name = {clean_text(item.get("field_name")): item for item in _field_items(result)}
    old = by_name.get("应签收时间")
    schema_changed = False
    if old and "R13应签收时间" not in by_name:
        rename = feishu_operation(
            "update_field",
            {
                "base_token": base_token,
                "table_id": table_id,
                "field_id": old.get("field_id"),
                "field_name": "R13应签收时间",
                "type": old.get("type"),
                "property": old.get("property") if isinstance(old.get("property"), dict) else {},
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if rename.get("error"):
            return _write_outcome_unknown("每日应签多维表字段重命名后的终态未知。")
        schema_changed = True
        by_name["R13应签收时间"] = {**old, "field_name": "R13应签收时间"}
    date_type = _to_int((by_name.get("R13应签收时间") or {}).get("type")) or 1
    required_types = {
        "运单编号": 1,
        "R13应签收时间": date_type,
        "问题件后应签时间": date_type,
        "货物品名": 1,
        "包装类型": 1,
        "货物件数": 2,
        "收件人地址": 1,
        "送货方式": 1,
        "到货件数": 2,
    }
    for name, expected_type in required_types.items():
        existing = by_name.get(name)
        if existing:
            if _to_int(existing.get("type")) != expected_type:
                return {"error": f"多维表字段类型不匹配: {name}", "expected_type": expected_type, "actual_type": existing.get("type")}
            continue
        created = feishu_operation(
            "create_field",
            {
                "base_token": base_token,
                "table_id": table_id,
                "field_name": name,
                "type": expected_type,
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if created.get("error"):
            return _write_outcome_unknown("每日应签多维表字段创建后的终态未知。")
        schema_changed = True
    fresh_result = feishu_operation(
        "list_fields",
        {
            "base_token": base_token,
            "table_id": table_id,
            "as": params.get("as", "bot"),
        },
    )
    if fresh_result.get("error"):
        code = "WRITE_OUTCOME_UNKNOWN" if schema_changed else "PROJECTION_READ_FAILED"
        return {
            "error": "每日应签多维表字段新鲜回读不可用。",
            "error_code": code,
        }
    try:
        readback = verify_bitable_schema(
            required_types,
            _field_items(fresh_result),
        )
    except DailySignReadbackError:
        code = "WRITE_OUTCOME_UNKNOWN" if schema_changed else "PROJECTION_READ_FAILED"
        return {
            "error": "每日应签多维表字段新鲜回读不匹配。",
            "error_code": code,
        }
    return {"ok": True, "fields": required_types, "readback": readback}


def _record_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(result.get("items"), list):
        return [item for item in result["items"] if isinstance(item, dict)]
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return [item for item in data.get("items", []) if isinstance(item, dict)]


def _sync_bitable(rows: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    base_token, table_id = resolve_bitable_target(params, DAILY_SIGN_BITABLE_RESOURCE_KEY)
    schema_result = _ensure_bitable_schema(base_token, table_id, params)
    if schema_result.get("error"):
        return schema_result
    existing_result = feishu_operation(
        "list_records",
        {"base_token": base_token, "table_id": table_id, "limit": 5000, "as": params.get("as", "bot")},
    )
    if existing_result.get("error"):
        return {"error": f"读取多维表记录失败: {existing_result.get('error')}"}
    existing_by_code: dict[str, dict[str, Any]] = {}
    for item in _record_items(existing_result):
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        code = clean_text(fields.get("运单编号"))
        if not code:
            return {"error": "多维表存在缺少运单编号的记录，停止差异更新"}
        if code in existing_by_code:
            return {"error": f"多维表存在重复运单编号: {code}"}
        existing_by_code[code] = item
    date_field_type = int(schema_result["fields"]["R13应签收时间"])
    target_records = _build_ledger_records(rows, date_field_type=date_field_type)
    writes: list[dict[str, Any]] = []
    unchanged = 0
    target_codes: set[str] = set()
    for record in target_records:
        fields = record["fields"]
        code = clean_text(fields.get("运单编号"))
        target_codes.add(code)
        existing = existing_by_code.get(code)
        if existing and existing.get("fields") == fields:
            unchanged += 1
            continue
        writes.append({**record, **({"record_id": existing.get("record_id")} if existing else {})})
    write_result: dict[str, Any] = {"ok": True, "written": 0, "skipped": True}
    if writes:
        write_result = feishu_operation(
            "write_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "records": writes,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if write_result.get("error") or write_result.get("errors"):
            return _write_outcome_unknown("每日应签多维表差异写入后的终态未知。")
    delete_ids = [
        clean_text(item.get("record_id"))
        for code, item in existing_by_code.items()
        if code not in target_codes and clean_text(item.get("record_id"))
    ]
    delete_result: dict[str, Any] = {"ok": True, "deleted": 0, "skipped": True}
    if delete_ids:
        delete_result = feishu_operation(
            "delete_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "record_ids": delete_ids,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if delete_result.get("error") or delete_result.get("errors"):
            return _write_outcome_unknown("每日应签多维表旧记录清理后的终态未知。")
    readback_result = feishu_operation(
        "list_records",
        {
            "base_token": base_token,
            "table_id": table_id,
            "limit": max(len(target_records) + 1, 1),
            "as": params.get("as", "bot"),
        },
    )
    if readback_result.get("error"):
        return _write_outcome_unknown("每日应签多维表新鲜回读不可用。")
    try:
        readback = verify_bitable_snapshot(
            target_records,
            readback_result,
            identity_field="运单编号",
        )
    except DailySignReadbackError:
        return _write_outcome_unknown("每日应签多维表新鲜回读不匹配。")
    return {
        "ok": True,
        "written": len(writes),
        "unchanged": unchanged,
        "deleted": len(delete_ids),
        "schema_result": schema_result,
        "readback": readback,
    }



def run_daily_sign_sync(params: dict[str, Any]) -> dict[str, Any]:
    from tools.daily_sign_pipeline import (
        DailySignSyncError,
        _collect_problem_events,
        _collect_sign_events,
        _extract_rows as _extract_authoritative_rows,
        _finish_failed_run,
        _legacy_candidate_keys,
        _merge_problem_events,
        _merge_sign_events,
        _required_account_id as _required_pipeline_account_id,
        _resolve_r13_request,
        _r13_rows_by_code,
        _source_query_window,
        _unified_failure,
        _unified_success,
    )

    params = params if isinstance(params, dict) else {}
    observed_at = business_now()
    run_id: str | None = None
    diagnostics: dict[str, Any] = {
        "r13_complete": False,
        "problems_complete": False,
        "signs_complete": False,
    }
    try:
        if params.get("dry_run"):
            raise DailySignSyncError(
                "INVALID_ARGUMENT",
                "权威每日应签同步不支持跳过持久化的 dry_run。",
            )
        r13_account_id = _required_pipeline_account_id(params, "r13_account_id")
        account_id = _required_pipeline_account_id(params, "account_id")

        try:
            run_id, started_at = start_sync_run()
        except Exception as exc:
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签运行记录创建后的终态未知。",
                retryable=False,
            ) from exc
        observed_at = started_at
        diagnostics["run_id"] = run_id
        state = load_daily_sign_state()
        arrival_source_proof = state.get("arrival_source_proof")
        if (
            not isinstance(arrival_source_proof, dict)
            or arrival_source_proof.get("complete") is not True
        ):
            raise DailySignSyncError(
                "INCOMPLETE_SOURCE_EVIDENCE",
                "到货快照没有可验证的成功运行，禁止生成每日应签权威投影。",
            )
        diagnostics.update(
            {
                "arrival_rows": sum(
                    len(rows) for rows in state.get("arrivals", {}).values()
                ),
                "arrival_source_proof": arrival_source_proof,
            }
        )

        r13_request = _resolve_r13_request(params, r13_account_id)
        r13_response = call_http_service("/get_qianshou", r13_request)
        r13_rows = _extract_authoritative_rows(r13_response, label="R13 应签查询")
        r13_by_code = _r13_rows_by_code(r13_rows)
        diagnostics.update({"r13_complete": True, "r13_rows": len(r13_rows)})

        source_start, source_end = _source_query_window(
            params,
            r13_rows=r13_rows,
            state=state,
            observed_at=observed_at,
        )
        problem_events, problem_proof = _collect_problem_events(
            params,
            account_id=account_id,
            start=source_start,
            end=source_end,
        )
        problems_by_code = _merge_problem_events(
            state.get("problems", {}),
            problem_events,
        )
        diagnostics.update(
            {
                "problems_complete": True,
                "problem_rows": len(problem_events),
                "problem_proof": problem_proof,
            }
        )

        known_codes = (
            set(r13_by_code)
            | set(state.get("ledger", {}))
            | set(state.get("target_station_codes", set()))
        )
        bulk_sign_events, bulk_sign_proof = _collect_sign_events(
            params,
            account_id=account_id,
            start=source_start,
            end=source_end,
            known_codes=known_codes,
        )
        signs_by_code = _merge_sign_events(
            state.get("signs", {}),
            bulk_sign_events,
        )
        working_state = {
            **state,
            "problems": problems_by_code,
            "signs": signs_by_code,
        }

        exact_sign_events, exact_sign_result = _sync_r13_sign_conflicts(
            params,
            r13_by_code,
            working_state,
            persist=False,
        )
        if exact_sign_result.get("complete") is not True:
            raise DailySignSyncError(
                "INCOMPLETE_SOURCE_EVIDENCE",
                "R13 签收状态与主单轨迹冲突，精确签收核验未完整。",
                retryable=True,
            )
        signs_by_code = _merge_sign_events(signs_by_code, exact_sign_events)
        working_state["signs"] = signs_by_code

        historical_sign_events, historical_sign_result = _sync_historical_sign_verifications(
            params,
            r13_by_code,
            working_state,
            observed_at=observed_at,
            persist=False,
        )
        verification_rows = list(historical_sign_result.pop("verification_rows", []))
        if historical_sign_result.get("complete") is not True:
            raise DailySignSyncError(
                "INCOMPLETE_SOURCE_EVIDENCE",
                "历史每日应签候选的主单轨迹核验未完整。",
                retryable=True,
            )
        signs_by_code = _merge_sign_events(signs_by_code, historical_sign_events)
        working_state["signs"] = signs_by_code
        sign_proof = {
            "complete": True,
            "bulk": bulk_sign_proof,
            "exact_conflicts": exact_sign_result,
            "historical_exact": historical_sign_result,
        }
        diagnostics.update(
            {
                "signs_complete": True,
                "sign_rows": len(signs_by_code),
                "sign_proof": sign_proof,
            }
        )

        candidate_codes, excluded_child_codes = _daily_sign_candidate_codes(
            r13_by_code,
            state.get("ledger", {}),
            set(state.get("target_station_codes", set())),
        )
        ledger_rows = [
            build_ledger_row(
                code,
                r13_row=r13_by_code.get(code),
                previous_row=state.get("ledger", {}).get(code),
                arrival_history=state.get("arrivals", {}).get(code, []),
                problem_events=problems_by_code.get(code, []),
                sign_event=signs_by_code.get(code),
                observed_at=observed_at,
            )
            for code in sorted(candidate_codes)
        ]
        open_rows = _sort_rows(
            [
                row
                for row in ledger_rows
                if ledger_row_is_due(row, observed_at.date())
            ]
        )
        open_rows, address_result = _enrich_missing_addresses(open_rows, params)
        if address_result.get("error"):
            raise DailySignSyncError(
                clean_text(address_result.get("error_code")) or "SOURCE_QUERY_FAILED",
                clean_text(address_result.get("error")) or "运单地址补全失败。",
                retryable=True,
            )
        required_publication_fields = {
            "goods_name": "货物品名",
            "package_type": "包装类型",
            "expected_quantity": "货物件数",
            "delivery_method": "送货方式",
        }
        missing_publication_fields = Counter(
            label
            for row in open_rows
            for field, label in required_publication_fields.items()
            if row.get(field) in (None, "")
        )
        if missing_publication_fields:
            summary = "、".join(
                f"{label}{count}条"
                for label, count in sorted(missing_publication_fields.items())
            )
            raise DailySignSyncError(
                "INCOMPLETE_SOURCE_EVIDENCE",
                f"主单详情缺少应签表必填信息（{summary}），停止写入。",
                retryable=True,
            )
        by_code = {row["tracking_number"]: row for row in open_rows}
        for row in ledger_rows:
            if row["tracking_number"] in by_code:
                enriched = by_code[row["tracking_number"]]
                for field in (
                    "goods_name",
                    "package_type",
                    "expected_quantity",
                    "delivery_method",
                    "recipient_address",
                ):
                    row[field] = enriched.get(field)
        all_sign_events = bulk_sign_events + exact_sign_events + historical_sign_events
        persistence_marker = build_daily_sign_persistence_marker(
            problem_events=problem_events,
            sign_events=all_sign_events,
            ledger_rows=ledger_rows,
            sign_verification_states=verification_rows,
            publication_rows=open_rows,
        )
        try:
            ledger_result = persist_daily_sign_snapshot(
                problem_events=problem_events,
                sign_events=all_sign_events,
                ledger_rows=ledger_rows,
                sign_verification_states=verification_rows,
                publication_rows=open_rows,
                run_id=run_id,
                persistence_marker=persistence_marker,
            )
        except Exception as exc:
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签权威事件与账本提交后的终态未知。",
                retryable=False,
            ) from exc
        if ledger_result.get("persistence_marker") != persistence_marker:
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签权威事件与账本提交缺少完整证明。",
                retryable=False,
            )
        try:
            persistence_readback = verify_daily_sign_persistence(
                run_id=run_id,
                problem_events=problem_events,
                sign_events=all_sign_events,
                ledger_rows=ledger_rows,
                sign_verification_states=verification_rows,
                publication_rows=open_rows,
                persistence_marker=persistence_marker,
            )
        except Exception as exc:
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                _persistence_readback_failure_message(exc),
                retryable=False,
            ) from exc
        if (
            persistence_readback.get("verified") is not True
            or persistence_readback.get("record_count") != len(ledger_rows)
            or persistence_readback.get("publication_rows", {}).get("record_count")
            != len(open_rows)
            or persistence_readback.get("persistence_sha256")
            != persistence_marker.get("marker_sha256")
        ):
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签权威持久化的新鲜回读证明不完整。",
                retryable=False,
            )
        diagnostics.update(
            {
                "persistence_commit": persistence_marker,
                "persistence_readback": persistence_readback,
            }
        )

        bitable_result = _sync_bitable(open_rows, params)
        if bitable_result.get("error"):
            error_code = clean_text(bitable_result.get("error_code"))
            raise DailySignSyncError(
                error_code or "PROJECTION_WRITE_FAILED",
                clean_text(bitable_result.get("error")) or "每日应签多维表写入失败。",
                retryable=False,
            )
        bitable_readback = bitable_result.get("readback")
        if (
            not isinstance(bitable_readback, dict)
            or bitable_readback.get("verified") is not True
            or bitable_readback.get("record_count") != len(open_rows)
            or len(clean_text(bitable_readback.get("snapshot_sha256"))) != 64
        ):
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签多维表缺少完整的新鲜回读证明。",
                retryable=False,
            )
        diagnostics.update(
            {
                "bitable_written": bitable_result.get("written", 0),
                "bitable_readback": bitable_readback,
            }
        )
        sheet_result = _sync_sheet(open_rows, params)
        if sheet_result.get("error"):
            error_code = clean_text(sheet_result.get("error_code"))
            raise DailySignSyncError(
                error_code or "PROJECTION_WRITE_FAILED",
                clean_text(sheet_result.get("error")) or "每日应签电子表格写入失败。",
                retryable=False,
            )
        sheet_readback = sheet_result.get("readback")
        if (
            not isinstance(sheet_readback, dict)
            or sheet_readback.get("verified") is not True
            or sheet_readback.get("record_count") != len(open_rows)
            or len(clean_text(sheet_readback.get("snapshot_sha256"))) != 64
        ):
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签电子表格缺少完整的新鲜回读证明。",
                retryable=False,
            )
        diagnostics.update(
            {
                "sheet_rows": sheet_result.get("rows", len(open_rows)),
                "sheet_readback": sheet_readback,
            }
        )

        fingerprint = clean_text(
            persistence_marker.get("publication_rows", {}).get("sha256")
        )
        if (
            len(fingerprint) != 64
            or persistence_readback.get("publication_sha256") != fingerprint
        ):
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签发布集合与权威账本绑定不一致。",
                retryable=False,
            )
        state_counts = Counter(clean_text(row.get("arrival_status")) or "unknown" for row in ledger_rows)
        quality_flag_counts = Counter(
            clean_text(flag)
            for row in ledger_rows
            for flag in (row.get("data_quality_flags") or [])
            if clean_text(flag)
        )
        diagnostics.update(
            {
                "r13_rows": len(r13_rows),
                "candidate_rows": len(candidate_codes),
                "excluded_child_candidate_rows": len(excluded_child_codes),
                "excluded_child_candidate_codes": sorted(excluded_child_codes),
                "published_rows": len(open_rows),
                "unmatched_rows": sum(1 for row in open_rows if "r13_without_arrival_history" in row.get("data_quality_flags", [])),
                "closed_by_tms_rows": sum(1 for row in ledger_rows if row.get("tms_signed")),
                "r13_current_rows": sum(1 for row in ledger_rows if row.get("r13_current")),
                "state_counts": dict(sorted(state_counts.items())),
                "quality_flag_counts": dict(sorted(quality_flag_counts.items())),
                "fingerprint": fingerprint,
                "source_window": {
                    "start": source_start.isoformat(),
                    "end": source_end.isoformat(),
                },
                "ledger_result": ledger_result,
                "address_enrichment": address_result,
            }
        )
        legacy_keys = _legacy_candidate_keys(r13_rows, observed_at)
        diagnostics.update(
            {
                "legacy_candidate_rows": len(legacy_keys),
                "legacy_candidate_hash": snapshot_fingerprint(
                    [{"dedupe_key": key} for key in legacy_keys]
                ),
            }
        )
        completion_values = {
            "status": "success",
            "degraded": False,
            "r13_complete": True,
            "problems_complete": True,
            "signs_complete": True,
            "r13_rows": len(r13_rows),
            "arrival_rows": diagnostics["arrival_rows"],
            "problem_rows": len(problem_events),
            "sign_rows": len(signs_by_code),
            "candidate_rows": len(candidate_codes),
            "published_rows": len(open_rows),
            "unmatched_rows": diagnostics["unmatched_rows"],
            "fingerprint": fingerprint,
            "diagnostics_json": diagnostics,
            "error_summary": None,
        }
        try:
            finish_sync_run(run_id, completion_values)
        except Exception as exc:
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签成功运行记录提交后的终态未知。",
                retryable=False,
            ) from exc
        try:
            completion_readback = verify_daily_sign_completed_run(
                run_id=run_id,
                expected_values=completion_values,
            )
        except Exception as exc:
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签成功运行记录的新鲜回读不匹配。",
                retryable=False,
            ) from exc
        if (
            completion_readback.get("verified") is not True
            or completion_readback.get("record_count") != len(open_rows)
            or completion_readback.get("publication_sha256") != fingerprint
            or completion_readback.get("persistence_sha256")
            != persistence_marker.get("marker_sha256")
        ):
            raise DailySignSyncError(
                "WRITE_OUTCOME_UNKNOWN",
                "每日应签成功运行记录缺少完整的新鲜回读证明。",
                retryable=False,
            )
        evidence_refs = sorted(
            set(state.get("source_refs", []))
            | {
                f"mysql:daily_sign_sync_runs:{run_id}",
                f"r13:complete:{snapshot_fingerprint(r13_rows)}",
                f"ronghui_problems:complete:{snapshot_fingerprint(problem_events)}",
                f"ronghui_signs:complete:{snapshot_fingerprint(all_sign_events)}",
                f"mysql:daily_sign_persistence:{persistence_marker['marker_sha256']}",
                f"mysql:daily_sign_ledger:{persistence_readback['ledger_sha256']}",
                f"feishu:daily_sign_bitable:{bitable_readback['snapshot_sha256']}",
                f"feishu:daily_sign_sheet:{sheet_readback['snapshot_sha256']}",
            }
        )
        result = _unified_success(
            run_id=run_id,
            observed_at=observed_at,
            ledger_rows=ledger_rows,
            legacy_candidate_keys=legacy_keys,
            diagnostics=diagnostics,
            evidence_refs=evidence_refs,
        )
        result["meta"]["postcondition_evidence"]["0"].update(
            {
                "condition": "authoritative_snapshot_committed",
                "details": {
                    "source_run_id": run_id,
                    "persistence_sha256": persistence_marker["marker_sha256"],
                    "bitable_snapshot_sha256": bitable_readback[
                        "snapshot_sha256"
                    ],
                    "sheet_snapshot_sha256": sheet_readback["snapshot_sha256"],
                },
            }
        )
        return result
    except DailySignSyncError as exc:
        if exc.code == "WRITE_OUTCOME_UNKNOWN":
            return _unified_failure(
                code="WRITE_OUTCOME_UNKNOWN",
                message=str(exc),
                observed_at=observed_at,
                run_id=run_id,
                retryable=False,
            )
        if run_id:
            try:
                failed_values = _finish_failed_run(
                    run_id,
                    diagnostics,
                    message=str(exc),
                )
                failed_readback = verify_daily_sign_completed_run(
                    run_id=run_id,
                    expected_values=failed_values,
                )
                if failed_readback.get("verified") is not True:
                    raise RuntimeError("daily-sign failed run proof is incomplete")
            except Exception:
                return _unified_failure(
                    code="WRITE_OUTCOME_UNKNOWN",
                    message="每日应签失败运行记录提交后的终态未知。",
                    observed_at=observed_at,
                    run_id=run_id,
                    retryable=False,
                )
        return _unified_failure(
            code=exc.code,
            message=str(exc),
            observed_at=observed_at,
            run_id=run_id,
            retryable=exc.retryable,
        )
    except Exception as exc:
        safe_message = f"每日应签同步发生未分类错误：{type(exc).__name__}。"
        if run_id:
            return _unified_failure(
                code="WRITE_OUTCOME_UNKNOWN",
                message=safe_message,
                observed_at=observed_at,
                run_id=run_id,
                retryable=False,
            )
        return _unified_failure(
            code="DAILY_SIGN_SYNC_FAILED",
            message=safe_message,
            observed_at=observed_at,
            run_id=run_id,
            retryable=False,
        )


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_daily_sign_sync(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
