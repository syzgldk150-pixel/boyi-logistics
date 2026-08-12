"""Phase 7 第二批：每日应签 -> 多维表格 + 电子表格。"""

import json
import os
import sys
from typing import Any

from agent.tms_runtime.account_manager import get_account_manager
from agent.workflow_resource_store import get_workflow_resource
from tools.phase7_sync_common import (
    build_range_from_template,
    parse_a1_range,
    sync_bitable_snapshot,
    sync_sheet_snapshot,
    tms_auth_error_result,
)
from tools.phase7_mysql_store import get_waybill_tracking_cache
from tools.tms_tool import call_http_service

R13_CREDENTIAL_RESOURCE_KEY = "phase7.r13_credentials"
DAILY_SIGN_SHEET_RESOURCE_KEY = "phase7.daily_sign_sheet"
DAILY_SIGN_SHEET_COL_COUNT = 8
NO_FETCHED_ROWS_REASON = "no_fetched_rows"
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
    "username",
    "password",
    "user",
    "pass",
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
    "config_path",
    "account_id",
    "accountId",
    "account_key",
    "accountKey",
)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _extract_r13_request_defaults(params: dict) -> dict:
    resource = params.get("r13_credentials")
    if not isinstance(resource, dict):
        try:
            resource = get_workflow_resource(R13_CREDENTIAL_RESOURCE_KEY) or {}
        except Exception:
            resource = {}
    if not isinstance(resource, dict):
        return {}

    defaults: dict[str, Any] = {}
    nested_request_body = resource.get("request_body")
    if isinstance(nested_request_body, dict):
        for key, value in nested_request_body.items():
            if _has_value(value):
                defaults[str(key)] = value

    for key in R13_REQUEST_KEYS:
        value = resource.get(key)
        if _has_value(value):
            defaults[key] = value
    return defaults


def _resolve_qianshou_request_body(params: dict) -> dict:
    request_body = _extract_r13_request_defaults(params)
    explicit_request_body = params.get("request_body")
    if isinstance(explicit_request_body, dict):
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
        DAILY_SIGN_SHEET_COL_COUNT,
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
    enrich_addresses = _coerce_bool(params.get("enrich_addresses"), default=True)
    request_params: dict[str, Any] = {
        "items": [{"bill_code": bill_code} for bill_code in bill_codes],
        "max_workers": _coerce_int(params.get("detail_max_workers"), default=1),
        "decrypt_masked": _coerce_bool(params.get("detail_decrypt_masked"), default=enrich_addresses),
        "browser_headless": _coerce_bool(params.get("browser_headless"), default=True),
        "browser_timeout_ms": _coerce_int(params.get("browser_timeout_ms"), default=30_000),
        "browser_batch_size": _coerce_int(params.get("browser_batch_size"), default=10),
        "browser_max_workers": _coerce_int(params.get("browser_max_workers"), default=1),
        "include_sign_status": True,
    }
    if params.get("detail_session_profile") not in (None, ""):
        request_params["session_profile"] = params["detail_session_profile"]
    detail_account_id = params.get("detail_account_id") or params.get("detailAccountId")
    if detail_account_id not in (None, ""):
        request_params["account_id"] = detail_account_id
    return {
        "params": request_params,
        "timeout_sec": _coerce_int(params.get("waybill_timeout_sec"), default=2400),
    }


def _fetch_waybill_details(rows: list[dict], params: dict) -> tuple[dict[str, dict[str, Any]] | None, dict]:
    bill_codes = _unique_bill_codes(rows)
    if not bill_codes:
        return {}, {"ok": True, "requested": 0, "fetched": 0}

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

    details: dict[str, dict[str, Any]] = {}
    for row in detail_rows:
        if not isinstance(row, dict):
            continue
        code = _detail_code(row)
        if code:
            details[code] = row
    return details, {"ok": True, "requested": len(bill_codes), "fetched": len(details)}


def _verify_and_enrich_unsigned_rows(
    rows: list[dict],
    params: dict,
) -> tuple[list[dict] | None, dict[str, Any], dict[str, Any]]:
    missing_code_rows = [index for index, row in enumerate(rows) if not _clean_text(row.get("billNumberMain"))]
    if missing_code_rows:
        verification = {
            "error": "每日应签候选记录缺少运单编号，已停止写表",
            "requested": len(rows),
            "missing_code_rows": missing_code_rows,
        }
        return None, verification, {"ok": True, "skipped": True, "updated": 0}

    bill_codes = _unique_bill_codes(rows)
    details, detail_result = _fetch_waybill_details(rows, params)
    if details is None:
        verification = {
            "error": detail_result["error"],
            "requested": len(bill_codes),
            "detail_result": detail_result,
        }
        return None, verification, detail_result

    signed_codes = [
        code
        for code in bill_codes
        if details.get(code, {}).get("sign_status_checked") is True
        and details.get(code, {}).get("is_signed") is True
    ]
    pending_codes = [
        code
        for code in bill_codes
        if details.get(code, {}).get("sign_status_checked") is True
        and details.get(code, {}).get("is_signed") is False
    ]
    classified_codes = set(signed_codes) | set(pending_codes)
    unknown_codes = [code for code in bill_codes if code not in classified_codes]
    if unknown_codes:
        verification = {
            "error": "TMS 快件跟踪签收扫描结果缺失，已停止写表",
            "requested": len(bill_codes),
            "signed": len(signed_codes),
            "pending": len(pending_codes),
            "unknown": len(unknown_codes),
            "unknown_bill_codes": unknown_codes,
        }
        return None, verification, {**detail_result, "updated": 0}

    enrich_addresses = _coerce_bool(params.get("enrich_addresses"), default=True)
    pending_set = set(pending_codes)
    verified_rows: list[dict] = []
    updated_count = 0
    for row in rows:
        bill_code = _clean_text(row.get("billNumberMain"))
        if bill_code not in pending_set:
            continue
        next_row = dict(row)
        if enrich_addresses:
            merged_address = _prefer_address(next_row.get("dispAddress"), _detail_address(details[bill_code]))
            if merged_address != _clean_text(next_row.get("dispAddress")):
                next_row["dispAddress"] = merged_address
                updated_count += 1
        verified_rows.append(next_row)

    verification = {
        "ok": True,
        "source": "tms_tracking_main_scans",
        "requested": len(bill_codes),
        "pending": len(pending_codes),
        "excluded_signed": len(signed_codes),
        "unknown": 0,
    }
    address_result = {
        **detail_result,
        "skipped": not enrich_addresses,
        "updated": updated_count,
    }
    return verified_rows, verification, address_result


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


def run_daily_sign_sync(params: dict) -> dict:
    request_body = build_daily_sign_request_body(params)
    tms_result = call_http_service("/get_qianshou", request_body)
    if auth_error := tms_auth_error_result(tms_result):
        return auth_error
    rows = _extract_rows(tms_result)
    if rows is None and isinstance(tms_result, dict) and tms_result.get("error"):
        return {"error": f"get_qianshou 执行失败: {tms_result.get('error')}", "raw": tms_result}
    if rows is None:
        return {"error": "get_qianshou 返回格式异常", "raw": tms_result}

    if not rows:
        skip_result = {"ok": True, "skipped": True, "reason": NO_FETCHED_ROWS_REASON}
        return {
            "ok": True,
            "fetched": 0,
            "skip_reason": NO_FETCHED_ROWS_REASON,
            "address_enrichment": {**skip_result, "updated": 0},
            "bitable_result": {**skip_result, "written": 0, "deleted": 0},
            "sheet_result": {**skip_result, "rows": 0},
        }

    r13_fetched = len(rows)
    rows, sign_verification, address_enrichment = _verify_and_enrich_unsigned_rows(rows, params)
    if rows is None:
        return {
            "error": sign_verification["error"],
            "r13_fetched": r13_fetched,
            "sign_verification": sign_verification,
            "address_enrichment": address_enrichment,
        }

    rows, arrival_enrichment = _enrich_rows_with_arrival_quantities(rows, params)
    if "error" in arrival_enrichment:
        return {
            "error": arrival_enrichment["error"],
            "fetched": len(rows),
            "r13_fetched": r13_fetched,
            "sign_verification": sign_verification,
            "address_enrichment": address_enrichment,
            "arrival_enrichment": arrival_enrichment,
        }

    rows = _sort_rows_by_plan_sign_time(rows)
    records = _build_records(rows)
    sheet_values = _build_sheet_values(rows)

    bitable_result = sync_bitable_snapshot("phase7.daily_sign_bitable", records, params)
    if "error" in bitable_result:
        return {
            "error": bitable_result["error"],
            "fetched": len(rows),
            "r13_fetched": r13_fetched,
            "sign_verification": sign_verification,
            "address_enrichment": address_enrichment,
            "arrival_enrichment": arrival_enrichment,
            "bitable_result": bitable_result,
        }

    sheet_result = sync_sheet_snapshot(
        DAILY_SIGN_SHEET_RESOURCE_KEY,
        sheet_values,
        _sheet_params_for_values(params, sheet_values),
    )
    if "error" in sheet_result:
        return {
            "error": sheet_result["error"],
            "fetched": len(rows),
            "r13_fetched": r13_fetched,
            "sign_verification": sign_verification,
            "address_enrichment": address_enrichment,
            "arrival_enrichment": arrival_enrichment,
            "bitable_result": bitable_result,
            "sheet_result": sheet_result,
        }

    return {
        "ok": True,
        "fetched": len(rows),
        "r13_fetched": r13_fetched,
        "sign_verification": sign_verification,
        "address_enrichment": address_enrichment,
        "arrival_enrichment": arrival_enrichment,
        "bitable_result": bitable_result,
        "sheet_result": sheet_result,
    }


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_daily_sign_sync(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
