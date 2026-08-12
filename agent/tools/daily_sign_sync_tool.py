"""Synchronize the persistent daily-sign ledger to Feishu."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.tms_runtime.account_manager import get_account_manager
from agent.workflow_resource_store import get_workflow_resource
from tools.daily_sign_rules import (
    BUSINESS_TIMEZONE,
    MANUAL_POSTPONE_TYPES,
    business_now,
    build_ledger_row,
    clean_text,
    is_before_problem_cutoff,
    parse_datetime,
)
from tools.daily_sign_store import (
    finish_sync_run,
    latest_successful_sync_at,
    load_daily_sign_state,
    snapshot_fingerprint,
    start_sync_run,
    upsert_ledger_rows,
    upsert_problem_events,
    upsert_sign_events,
)
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import main_tracking_from_scan_code
from tools.phase7_sync_common import (
    build_range_from_template,
    parse_a1_range,
    resolve_bitable_target,
    resolve_sheet_target,
    tms_auth_error_result,
)
from tools.tms_tool import call_http_service

R13_CREDENTIAL_RESOURCE_KEY = "phase7.r13_credentials"
DAILY_SIGN_SHEET_RESOURCE_KEY = "phase7.daily_sign_sheet"
DAILY_SIGN_BITABLE_RESOURCE_KEY = "phase7.daily_sign_bitable"
DAILY_SIGN_SHEET_COL_COUNT = 9
NO_FETCHED_ROWS_REASON = "no_fetched_rows"
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
R13_REQUEST_KEYS = (
    "username", "password", "user", "pass", "disp_site_code", "dispSiteCode",
    "start", "end", "days", "page_size", "pageSize", "page", "fetch_all",
    "fetchAll", "max_pages", "maxPages", "config_path", "account_id",
    "accountId", "account_key", "accountKey",
)


def _has_value(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _extract_r13_request_defaults(params: dict[str, Any]) -> dict[str, Any]:
    resource = params.get("r13_credentials")
    if not isinstance(resource, dict):
        try:
            resource = get_workflow_resource(R13_CREDENTIAL_RESOURCE_KEY) or {}
        except Exception:
            resource = {}
    defaults: dict[str, Any] = {}
    nested = resource.get("request_body") if isinstance(resource, dict) else None
    if isinstance(nested, dict):
        defaults.update({str(key): value for key, value in nested.items() if _has_value(value)})
    if isinstance(resource, dict):
        defaults.update({key: resource[key] for key in R13_REQUEST_KEYS if _has_value(resource.get(key))})
    return defaults


def _apply_r13_account_binding(request_body: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    account_id = params.get("r13_account_id") or params.get("r13AccountId")
    if account_id in (None, ""):
        return request_body
    request_body["r13_account_id"] = account_id
    return get_account_manager().resolve_role_account_params(
        request_body,
        account_field="r13_account_id",
        output_account_field="",
        output_session_profile_field="",
    )


def build_daily_sign_request_body(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    request_body = _extract_r13_request_defaults(params)
    if isinstance(params.get("request_body"), dict):
        request_body.update(params["request_body"])
    request_body.update({key: params[key] for key in R13_REQUEST_KEYS if _has_value(params.get(key))})
    request_body.setdefault("fetch_all", True)
    request_body.setdefault("max_pages", 500)
    return _apply_r13_account_binding(request_body, params)


def _extract_rows(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [row for row in payload["data"] if isinstance(row, dict)]
    return None


def _normalize_time(value: Any) -> str:
    parsed = parse_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else ""


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _address_quality_score(value: Any) -> int:
    text = clean_text(value)
    if not text:
        return 0
    score = len(text.replace("*", "").replace(" ", ""))
    if "*" in text:
        score -= 100
    return score


def _needs_address_detail(value: Any) -> bool:
    text = clean_text(value)
    return not text or "*" in text or "＊" in text


def _detail_code(row: dict[str, Any]) -> str:
    for key in ("tracking_number", "requested_bill_code", "billNumberMain", "bill_code", "billCode", "运单编号"):
        if clean_text(row.get(key)):
            return clean_text(row.get(key))
    return ""


def _detail_address(row: dict[str, Any]) -> str:
    for key in ("recipient_address", "收件地址", "dispAddress", "ACCEPT_MAN_ADDRESS"):
        if clean_text(row.get(key)):
            return clean_text(row.get(key))
    return ""


def _enrich_missing_addresses(rows: list[dict[str, Any]], params: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if params.get("enrich_addresses") is False:
        return rows, {"ok": True, "skipped": True, "requested": 0, "updated": 0}
    requested = [row["tracking_number"] for row in rows if _needs_address_detail(row.get("recipient_address"))]
    if not requested:
        return rows, {"ok": True, "requested": 0, "updated": 0}
    detail_result = call_http_service(
        "/query_waybill_detail",
        {
            "params": {
                "items": [{"bill_code": code} for code in requested],
                "account_id": params.get("detail_account_id") or "ronghui_daxiang_s",
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
    by_code = {_detail_code(row): _detail_address(row) for row in details if _detail_code(row)}
    updated = 0
    output: list[dict[str, Any]] = []
    for row in rows:
        next_row = dict(row)
        detail = by_code.get(row["tracking_number"], "")
        if _address_quality_score(detail) > _address_quality_score(next_row.get("recipient_address")):
            next_row["recipient_address"] = detail
            updated += 1
        output.append(next_row)
    return output, {"ok": True, "requested": len(requested), "fetched": len(by_code), "updated": updated}


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
        start = now - timedelta(days=int(params.get("problem_backfill_days") or 3650))
    else:
        start = latest - timedelta(days=2)
    return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def _sync_manual_problem_events(params: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    if params.get("skip_problem_sync"):
        return [], {"ok": True, "skipped": True, "complete": True, "rows": 0}
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
                        "account_id": params.get("problem_account_id") or "ronghui_daxiang_s",
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
        start = now - timedelta(days=int(params.get("sign_backfill_days") or 3650))
    else:
        start = latest - timedelta(days=2)
    return start.strftime("%Y/%m/%d %H:%M:%S"), now.strftime("%Y/%m/%d %H:%M:%S")


def _sync_sign_events(params: dict[str, Any], known_main_codes: set[str]) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    start, end = _sign_query_window(params)
    response = call_http_service(
        "/get_scan",
        {
            "params": {
                "start": start,
                "end": end,
                "scan_type": "签收",
                "site_code": params.get("sign_site_code"),
                "use_login_site_code": True,
                "output_format": "json",
                "page_size": int(params.get("sign_page_size") or 500),
                "max_pages": int(params.get("sign_max_pages") or 500),
                "account_id": params.get("sign_account_id") or "ronghui_daxiang_s",
            },
            "timeout_sec": int(params.get("sign_timeout_sec") or 1200),
        },
    )
    if auth_error := tms_auth_error_result(response):
        return None, {"error": auth_error.get("error"), "complete": False}
    rows = _extract_rows(response)
    if rows is None:
        return None, {"error": "get_scan 签收查询返回格式异常", "complete": False, "raw": response}
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


def _query_exact_main_sign(code: str, params: dict[str, Any]) -> dict[str, Any]:
    response = call_http_service(
        "/ronghui_tms_tracking",
        {
            "params": {
                "tracking_number": code,
                "account_id": params.get("sign_account_id") or "ronghui_daxiang_s",
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


def _sync_r13_sign_conflicts(
    params: dict[str, Any],
    r13_by_code: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conflicts = sorted(
        code
        for code, row in r13_by_code.items()
        if _r13_reports_signed(row) and code not in state["signs"]
    )
    if not conflicts:
        return [], {"ok": True, "complete": True, "queried": 0, "confirmed": 0, "errors": []}
    conflict_limit = min(max(int(params.get("exact_sign_conflict_limit") or 50), 1), 500)
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
    workers = min(max(int(params.get("exact_sign_workers") or 4), 1), 8, len(conflicts))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_query_exact_main_sign, code, params): code for code in conflicts}
        for future in as_completed(futures):
            code = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"tracking_number": code, "error": str(exc)[:300]})
    events = [result["sign_event"] for result in results if result.get("sign_event")]
    errors = [
        {"tracking_number": result.get("tracking_number"), "error": result.get("error")}
        for result in results
        if result.get("error")
    ]
    store_result = upsert_sign_events(events, dry_run=bool(params.get("dry_run", False)))
    return events, {
        **store_result,
        "complete": not errors,
        "queried": len(conflicts),
        "confirmed": len(events),
        "errors": errors,
    }


def _bitable_time(value: Any, field_type: int) -> Any:
    parsed = parse_datetime(value)
    if parsed is None:
        return None if field_type == 5 else ""
    if field_type == 5:
        return int(parsed.replace(tzinfo=BUSINESS_TIMEZONE).timestamp() * 1000)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _build_records(rows: list[dict[str, Any]], date_field_type: int = 1) -> list[dict[str, Any]]:
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


def _build_sheet_values(rows: list[dict[str, Any]]) -> list[list[Any]]:
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
    if actual_headers[:DAILY_SIGN_SHEET_COL_COUNT] != SHEET_HEADERS:
        return {"error": "应签明细表头不一致，停止写入", "expected_headers": SHEET_HEADERS, "actual_headers": actual_headers}
    sheet_values = _build_sheet_values(rows)
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
            return {"error": f"写入应签明细失败: {write_result.get('error')}", "write_result": write_result}
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
            return {"error": f"清理应签明细旧行失败: {clear_result.get('error')}", "write_result": write_result, "clear_result": clear_result}
    return {"ok": True, "rows": len(sheet_values), "write_result": write_result, "clear_result": clear_result}


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
            return {"error": f"旧应签收时间字段重命名失败: {rename.get('error')}"}
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
            return {"error": f"创建多维表字段失败: {name}: {created.get('error')}"}
    return {"ok": True, "fields": required_types}


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
    target_records = _build_records(rows, date_field_type=date_field_type)
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
            return {"error": "多维表差异写入失败", "write_result": write_result}
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
            return {"error": "多维表旧记录清理失败", "write_result": write_result, "delete_result": delete_result}
    return {"ok": True, "written": len(writes), "unchanged": unchanged, "deleted": len(delete_ids), "schema_result": schema_result}


def run_daily_sign_sync(params: dict[str, Any]) -> dict[str, Any]:
    dry_run = bool(params.get("dry_run", False))
    r13_request_body = build_daily_sign_request_body(params)
    r13_response = call_http_service("/get_qianshou", r13_request_body)
    if auth_error := tms_auth_error_result(r13_response):
        return auth_error
    if isinstance(r13_response, dict) and r13_response.get("error"):
        return {"error": f"get_qianshou 执行失败: {r13_response.get('error')}", "raw": r13_response}
    r13_rows = _extract_rows(r13_response)
    if r13_rows is None:
        return {"error": "get_qianshou 返回格式异常", "raw": r13_response}
    try:
        r13_by_code = _r13_by_code(r13_rows)
    except ValueError as exc:
        return {"error": str(exc), "raw": r13_response}

    run_id = "dry-run"
    started_at = business_now()
    if not dry_run:
        run_id, started_at = start_sync_run()
    diagnostics: dict[str, Any] = {
        "run_id": run_id,
        "r13_rows": len(r13_rows),
        "r13_complete": True,
        "problems_complete": False,
        "signs_complete": False,
    }
    try:
        _problem_events, problem_result = _sync_manual_problem_events(params)
        if _problem_events is None:
            raise RuntimeError(f"问题件同步不完整: {problem_result.get('error')}")
        diagnostics["problems_complete"] = True

        state = load_daily_sign_state()
        known_codes = set(state["ledger"]) | set(state["arrivals"]) | set(r13_by_code)
        _new_sign_events, bulk_sign_result = _sync_sign_events(params, known_codes)
        sign_degraded = _new_sign_events is None
        if not sign_degraded:
            state = load_daily_sign_state()
        exact_sign_events, exact_sign_result = _sync_r13_sign_conflicts(params, r13_by_code, state)
        sign_degraded = sign_degraded or not bool(exact_sign_result.get("complete"))
        if exact_sign_events:
            state = load_daily_sign_state()
        sign_result = {
            "ok": not sign_degraded,
            "complete": not sign_degraded,
            "bulk": bulk_sign_result,
            "exact_conflicts": exact_sign_result,
        }
        diagnostics["signs_complete"] = not sign_degraded

        candidate_codes = (
            set(r13_by_code)
            | {code for code, row in state["ledger"].items() if not bool(row.get("tms_signed"))}
            | set(state["target_station_codes"])
        )
        ledger_rows = [
            build_ledger_row(
                code,
                r13_row=r13_by_code.get(code),
                previous_row=state["ledger"].get(code),
                arrival_history=state["arrivals"].get(code, []),
                problem_events=state["problems"].get(code, []),
                sign_event=state["signs"].get(code),
                observed_at=started_at,
            )
            for code in sorted(candidate_codes)
        ]
        open_rows = _sort_rows([row for row in ledger_rows if not row.get("tms_signed")])
        open_rows, address_result = _enrich_missing_addresses(open_rows, params)
        if address_result.get("error"):
            raise RuntimeError(f"地址补全失败: {address_result.get('error')}")
        by_code = {row["tracking_number"]: row for row in open_rows}
        for row in ledger_rows:
            if row["tracking_number"] in by_code:
                row["recipient_address"] = by_code[row["tracking_number"]].get("recipient_address")
        ledger_result = upsert_ledger_rows(ledger_rows, dry_run=dry_run)

        bitable_result = _sync_bitable(open_rows, params)
        if bitable_result.get("error"):
            raise RuntimeError(bitable_result["error"])
        sheet_result = _sync_sheet(open_rows, params)
        if sheet_result.get("error"):
            raise RuntimeError(sheet_result["error"])

        fingerprint = snapshot_fingerprint(open_rows)
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
                "arrival_rows": sum(len(items) for items in state["arrivals"].values()),
                "problem_rows": sum(len(items) for items in state["problems"].values()),
                "sign_rows": len(state["signs"]),
                "candidate_rows": len(candidate_codes),
                "published_rows": len(open_rows),
                "unmatched_rows": sum(1 for row in open_rows if "r13_without_arrival_history" in row.get("data_quality_flags", [])),
                "closed_by_tms_rows": sum(1 for row in ledger_rows if row.get("tms_signed")),
                "r13_current_rows": sum(1 for row in ledger_rows if row.get("r13_current")),
                "state_counts": dict(sorted(state_counts.items())),
                "quality_flag_counts": dict(sorted(quality_flag_counts.items())),
                "fingerprint": fingerprint,
            }
        )
        status = "degraded" if sign_degraded else "success"
        if not dry_run:
            finish_sync_run(
                run_id,
                {
                    "status": status,
                    "degraded": sign_degraded,
                    "r13_complete": True,
                    "problems_complete": True,
                    "signs_complete": not sign_degraded,
                    **diagnostics,
                    "diagnostics_json": {
                        **diagnostics,
                        "problem_result": problem_result,
                        "sign_result": sign_result,
                    },
                    "error_summary": clean_text(sign_result.get("error")) if sign_degraded else None,
                },
            )
        return {
            "ok": True,
            "status": status,
            "degraded": sign_degraded,
            "fetched": len(r13_rows),
            "address_enrichment": address_result,
            "problem_result": problem_result,
            "sign_result": sign_result,
            "ledger_result": ledger_result,
            "bitable_result": bitable_result,
            "sheet_result": sheet_result,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        if not dry_run:
            finish_sync_run(
                run_id,
                {
                    "status": "failed",
                    "degraded": False,
                    "r13_complete": bool(diagnostics.get("r13_complete")),
                    "problems_complete": bool(diagnostics.get("problems_complete")),
                    "signs_complete": bool(diagnostics.get("signs_complete")),
                    "r13_rows": diagnostics.get("r13_rows", 0),
                    "arrival_rows": diagnostics.get("arrival_rows", 0),
                    "problem_rows": diagnostics.get("problem_rows", 0),
                    "sign_rows": diagnostics.get("sign_rows", 0),
                    "candidate_rows": diagnostics.get("candidate_rows", 0),
                    "published_rows": 0,
                    "unmatched_rows": diagnostics.get("unmatched_rows", 0),
                    "fingerprint": diagnostics.get("fingerprint"),
                    "diagnostics_json": diagnostics,
                    "error_summary": str(exc)[:500],
                },
            )
        return {"error": str(exc), "run_id": run_id, "diagnostics": diagnostics}


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(run_daily_sign_sync(params), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
