"""Phase 7 arrival statistics sync and snapshot archiving."""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import (
    _clear_spreadsheet_sheet_cache,
    _spreadsheet_sheet_info,
    _spreadsheet_sheet_ref_map,
    feishu_operation,
)
from tools.daily_sign_store import save_arrival_stat_snapshot
from tools.daily_sign_rules import business_now
from tools.phase7_mysql_store import (
    cleanup_scan_codes,
    has_waybill_detail,
    list_pending_waybills,
    list_scan_codes,
    list_waybill_records,
    main_tracking_from_scan_code,
    main_trackings_from_scan_rows,
    normalize_scan_rows,
    normalize_waybill_record,
    render_pending_sheet_values,
    render_stats_sheet_values,
    replace_scan_codes,
    upsert_waybill_records,
)
from tools.phase7_sync_common import (
    TMSAuthSyncError,
    build_range_from_template,
    get_required_resource,
    parse_a1_range,
    raise_tms_auth_error_if_present,
)
from tools.split_pending_snapshot import refresh_snapshot as refresh_split_pending_snapshot
from tools.tms_tool import call_http_service

_MASK_ONLY_RE = re.compile(r"^[\.\-·。…\s]+$")


def _emit_progress(message: str, **extra: Any) -> None:
    payload = " | ".join(f"{key}={value}" for key, value in extra.items() if value not in (None, ""))
    text = f"[progress] {message}"
    if payload:
        text = f"{text} | {payload}"
    print(text, file=sys.stderr, flush=True)


def _public_result(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lower_key = str(key).lower()
            if "token" in lower_key or lower_key in {"url", "webhook"}:
                continue
            result[str(key)] = _public_result(item)
        return result
    if isinstance(value, list):
        return [_public_result(item) for item in value]
    return value


def _extract_rows(tms_result: dict) -> list[Any] | None:
    rows = tms_result.get("data") if isinstance(tms_result, dict) else None
    if isinstance(rows, list):
        return rows
    if isinstance(tms_result, list):
        return tms_result
    return None


def _target_date(params: dict) -> date | None:
    raw = str(params.get("target_date") or "").strip()
    if not raw:
        return None
    return date.fromisoformat(raw)


def _apply_scan_window(request_params: dict, scan_window_days: int, target_date: date | None = None) -> dict:
    if any(request_params.get(key) for key in ("start", "end", "date")):
        return request_params
    if scan_window_days <= 1:
        if target_date is not None:
            request_params["date"] = target_date.strftime("%Y/%m/%d")
        return request_params
    end_date = target_date or business_now().date()
    start_date = end_date - timedelta(days=max(scan_window_days - 1, 0))
    request_params["start"] = f"{start_date.strftime('%Y/%m/%d')} 00:00:00"
    request_params["end"] = f"{end_date.strftime('%Y/%m/%d')} 23:59:59"
    return request_params


def _refresh_scan_index(params: dict) -> tuple[list[dict[str, str]], dict]:
    scan_window_days = int(params.get("scan_window_days") or 1)
    _emit_progress("开始刷新扫描索引", scan_window_days=scan_window_days)
    request_body = dict(params.get("scan_request_body") or params.get("request_body") or {})
    request_params = dict(request_body.get("params") or {})
    request_params.setdefault("output_format", "json")
    for key in ("session_profile", "account_id", "accountId"):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
    request_params = _apply_scan_window(request_params, scan_window_days, _target_date(params))
    tms_result = call_http_service(
        "/get_scan",
        {
            "params": request_params,
            "timeout_sec": int(request_body.get("timeout_sec") or 600),
        },
    )
    raise_tms_auth_error_if_present(tms_result)
    rows = _extract_rows(tms_result)
    if rows is None:
        raise ValueError(f"get_scan 返回格式异常: {tms_result}")
    normalized = normalize_scan_rows(rows)
    if bool(params.get("dry_run", False)):
        replace_result = {"ok": True, "replaced": len(normalized), "skipped": True}
    else:
        replace_result = replace_scan_codes(normalized)
    _emit_progress(
        "扫描索引刷新完成",
        upserted=replace_result.get("upserted") or replace_result.get("replaced"),
        scan_window_days=scan_window_days,
    )
    return normalized, replace_result


def _is_masked_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return "*" in text or bool(_MASK_ONLY_RE.fullmatch(text))


def _looks_incomplete_address(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    return len(text) < 8 or "|" in text or text.endswith(("省", "市", "区", "县"))


def _needs_waybill_refresh(record: dict[str, Any]) -> bool:
    if not has_waybill_detail(record):
        return True
    return any(
        (
            _is_masked_text(record.get("recipient_name")),
            _is_masked_text(record.get("recipient_phone")),
            _looks_incomplete_address(record.get("recipient_address")),
        )
    )


def _unique_tracking_numbers(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        tracking = str(value or "").strip()
        if not tracking or tracking in seen:
            continue
        seen.add(tracking)
        result.append(tracking)
    return result


def _detail_tracking_numbers(
    existing_records: list[dict[str, Any]],
    missing_tracking_numbers: list[str],
    params: dict,
) -> tuple[list[str], dict[str, int]]:
    stale_tracking_numbers = [
        str(record.get("tracking_number") or "").strip()
        for record in existing_records
        if _needs_waybill_refresh(record)
    ]
    tracking_numbers = _unique_tracking_numbers([*missing_tracking_numbers, *stale_tracking_numbers])
    if params.get("detail_refresh_limit") not in (None, ""):
        tracking_numbers = tracking_numbers[: int(params.get("detail_refresh_limit"))]
    return tracking_numbers, {
        "missing": len(_unique_tracking_numbers(missing_tracking_numbers)),
        "stale": len(_unique_tracking_numbers(stale_tracking_numbers)),
        "total": len(tracking_numbers),
    }


def _missing_trackings_from_current_scan(
    current_scan_rows: list[dict[str, str]],
    existing_records: list[dict[str, Any]],
    params: dict,
) -> list[str]:
    existing_tracking_numbers = {
        str(record.get("tracking_number") or "").strip()
        for record in existing_records
        if str(record.get("tracking_number") or "").strip()
    }
    missing = [
        tracking
        for tracking in main_trackings_from_scan_rows(current_scan_rows)
        if tracking not in existing_tracking_numbers
    ]
    if params.get("missing_limit") not in (None, ""):
        missing = missing[: int(params.get("missing_limit"))]
    return missing


def _fetch_waybill_details(bill_codes: list[str], params: dict) -> tuple[list[dict[str, Any]], dict]:
    if not bill_codes:
        _emit_progress("没有缺失主单需要补抓")
        return [], {"ok": True, "requested": 0, "fetched": 0}
    _emit_progress("开始补抓缺失主单", requested=len(bill_codes))
    request_body = {
        "params": {
            "items": [{"bill_code": bill_code} for bill_code in bill_codes],
            "max_workers": int(params.get("detail_max_workers", 1)),
            "browser_headless": params.get("browser_headless", True),
            "browser_timeout_ms": int(params.get("browser_timeout_ms", 30_000)),
            "browser_batch_size": int(params.get("browser_batch_size", 10)),
            "browser_max_workers": int(params.get("browser_max_workers", 1)),
        },
        "timeout_sec": int(params.get("waybill_timeout_sec", 2400)),
    }
    for key in ("session_profile", "account_id", "accountId"):
        if params.get(key) not in (None, ""):
            request_body["params"][key] = params[key]
    tms_result = call_http_service("/query_waybill_detail", request_body)
    raise_tms_auth_error_if_present(tms_result)
    rows = _extract_rows(tms_result)
    if rows is None:
        raise ValueError(f"waybill_tracking 返回格式异常: {tms_result}")
    records = [record for row in rows if (record := normalize_waybill_record(row))]
    _emit_progress("缺失主单补抓完成", fetched=len(records))
    return records, {"ok": True, "requested": len(bill_codes), "fetched": len(records)}


def _merge_records(existing_records: list[dict[str, Any]], fetched_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in existing_records:
        tracking_number = str(record.get("tracking_number") or "").strip()
        if tracking_number:
            merged[tracking_number] = dict(record)
    for record in fetched_records:
        tracking_number = str(record.get("tracking_number") or "").strip()
        if not tracking_number:
            continue
        base = merged.get(tracking_number, {})
        payload = dict(base)
        for key, value in record.items():
            if value not in (None, ""):
                payload[key] = value
        merged[tracking_number] = payload
    return sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("destination_station") or ""),
            str(item.get("tracking_number") or ""),
        ),
    )


def _safe_int(value: Any) -> int:
    if value in (None, "", "null"):
        return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _count_arrivals_from_scan_rows(
    scan_rows: list[dict[str, str]],
    records: list[dict[str, Any]],
    tracking_numbers: list[str],
) -> tuple[dict[str, Any], dict]:
    known_tracking_numbers = {
        str(row.get("raw_code") or "").strip()
        for row in scan_rows
        if str(row.get("raw_code") or "").strip()
    }
    child_counts: Counter[str] = Counter()
    direct_main_seen: set[str] = set()

    for row in scan_rows:
        raw_code = str(row.get("raw_code") or "").strip()
        main_tracking = main_tracking_from_scan_code(raw_code, known_tracking_numbers)
        if not main_tracking:
            continue
        if raw_code == main_tracking:
            direct_main_seen.add(main_tracking)
        else:
            child_counts[main_tracking] += 1

    quantity_by_tracking = {
        str(record.get("tracking_number") or "").strip(): _safe_int(record.get("quantity"))
        for record in records
        if str(record.get("tracking_number") or "").strip()
    }

    count_map: dict[str, Any] = {}
    quantity_gaps = 0
    for tracking_number in tracking_numbers:
        tracking = str(tracking_number or "").strip()
        if not tracking:
            continue
        child_count = child_counts.get(tracking, 0)
        quantity_count = quantity_by_tracking.get(tracking) or 0
        if quantity_count > child_count and (child_count > 0 or tracking in direct_main_seen):
            quantity_gaps += 1
        count_map[tracking] = child_count

    arrived_nonzero = sum(1 for value in count_map.values() if _safe_int(value) > 0)
    return count_map, {
        "ok": True,
        "source": "scan_index",
        "requested": len(tracking_numbers),
        "counted": len(count_map),
        "arrived_nonzero": arrived_nonzero,
        "scan_rows": len(scan_rows),
        "child_scan_rows": sum(child_counts.values()),
        "direct_main_rows": len(direct_main_seen),
        "quantity_adjustments": 0,
        "quantity_gaps": quantity_gaps,
    }


def _count_arrivals(tracking_numbers: list[str], params: dict) -> tuple[dict[str, Any], dict]:
    if not tracking_numbers:
        _emit_progress("没有需要统计的到货主单")
        return {}, {"ok": True, "requested": 0, "counted": 0}
    _emit_progress("开始统计到货件数", requested=len(tracking_numbers))
    request_body = {
        "timeout_sec": int(params.get("child_count_timeout_sec", 7200)),
        "params": {
            "items": tracking_numbers,
            "keywords": params.get("keywords", "装车,卸车,发往"),
            "include_list": True,
            "action_delay_sec": float(params.get("action_delay_sec", 1.0)),
            "relogin_attempts": int(params.get("relogin_attempts", 0)),
            "headless": params.get("headless", True),
        },
    }
    for key in ("session_profile", "account_id", "accountId"):
        if params.get(key) not in (None, ""):
            request_body["params"][key] = params[key]
    tms_result = call_http_service("/child_count", request_body)
    raise_tms_auth_error_if_present(tms_result)
    payload = tms_result.get("data") if isinstance(tms_result, dict) else None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    results = detail.get("results") if isinstance(detail, dict) else None
    if not isinstance(results, list):
        raise ValueError(f"child_count 返回格式异常: {tms_result}")
    count_map = {
        str(item.get("bill_code")): item.get("count", "")
        for item in results
        if item.get("bill_code")
    }
    _emit_progress("到货件数统计完成", counted=len(count_map))
    return count_map, {
        "ok": True,
        "source": "child_count",
        "requested": len(tracking_numbers),
        "counted": len(count_map),
        "summary": {
            "response_keys": sorted(tms_result.keys()) if isinstance(tms_result, dict) else [],
            "result_count": len(results),
        },
    }


def _values_for_stats_write(template_range: str, values: list[list[Any]]) -> list[list[Any]]:
    if not values:
        return values
    shape = parse_a1_range(str(template_range))
    if shape["start_row"] > 1:
        return values[1:]
    return values


def _column_to_number(col: str) -> int:
    total = 0
    for char in col:
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total


def _number_to_column(number: int) -> str:
    value = ""
    current = number
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        value = chr(ord("A") + remainder) + value
    return value


def _stats_header_range(template_range: str, values: list[list[Any]]) -> str | None:
    if not values:
        return None
    shape = parse_a1_range(str(template_range))
    if shape["start_row"] <= 1:
        return None
    start_col_num = _column_to_number(shape["start_col"])
    end_col = _number_to_column(start_col_num + max(len(values[0]), 1) - 1)
    return f"{shape['sheet']}!{shape['start_col']}1:{end_col}1"


def _stats_title_range(title_range: str | None, template_range: str, values: list[list[Any]]) -> str | None:
    if not values:
        return None
    if not title_range:
        return _stats_header_range(template_range, values)
    shape = parse_a1_range(str(title_range))
    start_col_num = _column_to_number(shape["start_col"])
    end_col = _number_to_column(start_col_num + max(len(values[0]), shape["col_count"], 1) - 1)
    return f"{shape['sheet']}!{shape['start_col']}{shape['start_row']}:{end_col}{shape['start_row']}"


def _stats_clear_range(clear_range: str, template_range: str, values: list[list[Any]]) -> str:
    clear_shape = parse_a1_range(str(clear_range))
    write_shape = parse_a1_range(str(template_range))
    start_row = max(clear_shape["start_row"], write_shape["start_row"])
    if start_row > clear_shape["end_row"]:
        start_row = write_shape["start_row"]

    start_col = write_shape["start_col"]
    start_col_num = _column_to_number(start_col)
    clear_width = clear_shape["col_count"]
    write_width = max(len(values[0]) if values else 1, write_shape["col_count"])
    end_col = _number_to_column(start_col_num + max(clear_width, write_width) - 1)
    return f"{write_shape['sheet']}!{start_col}{start_row}:{end_col}{clear_shape['end_row']}"


def _write_stats_sheet(resource_key: str, values: list[list[Any]], params: dict) -> dict:
    _emit_progress("开始写入统计表", resource_key=resource_key)
    resource = get_required_resource(resource_key)
    template_range = resource.get("snapshot_range") or resource.get("range")
    clear_range = resource.get("clear_range")
    clear_result = None
    if clear_range:
        clear_target = _stats_clear_range(str(clear_range), str(template_range), values)
        shape = parse_a1_range(clear_target)
        blank_values = [["" for _ in range(shape["col_count"])] for _ in range(shape["row_count"])]
        clear_result = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": clear_target,
                "values": blank_values,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in clear_result:
            _emit_progress("清空统计表失败", resource_key=resource_key, error=clear_result.get("error", ""))
            return {"error": "清空统计表失败", "feishu_result": clear_result}

    title_result = None
    title_range = _stats_title_range(resource.get("title_range"), str(template_range), values)
    if title_range and values:
        title_result = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": title_range,
                "values": [values[0]],
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in title_result:
            _emit_progress("写入统计表标题失败", resource_key=resource_key, error=title_result.get("error", ""))
            return {"error": "写入统计表标题失败", "clear_result": clear_result, "feishu_result": title_result}

    write_values = _values_for_stats_write(str(template_range), values)
    if not write_values:
        _emit_progress("统计表无数据行需写入", resource_key=resource_key)
        return {
            "ok": True,
            "rows": 0,
            "clear_result": clear_result,
            "title_result": title_result,
            "write_result": {"ok": True, "skipped": True},
        }

    write_range = build_range_from_template(
        template_range,
        len(write_values),
        max(len(row) for row in write_values) if write_values else 1,
    )
    write_result = feishu_operation(
        "write_sheet",
        {
            "spreadsheet_token": resource["spreadsheet_token"],
            "range": write_range,
            "values": write_values,
            "as": params.get("as", "bot"),
            "dry_run": bool(params.get("dry_run", False)),
        },
    )
    if "error" in write_result:
        _emit_progress("写入统计表失败", resource_key=resource_key, error=write_result.get("error", ""))
        return {"error": "写入统计表失败", "clear_result": clear_result, "feishu_result": write_result}
    _emit_progress("统计表写入完成", resource_key=resource_key, rows=len(write_values))
    return {
        "ok": True,
        "rows": len(write_values),
        "clear_result": clear_result,
        "title_result": title_result,
        "write_result": write_result,
    }


def _archive_title(params: dict | None = None) -> str:
    params = params or {}
    resolved = _target_date(params)
    if resolved is not None:
        return resolved.strftime("%Y-%m-%d")
    return business_now().strftime("%Y-%m-%d")


def _build_archive_resource(params: dict) -> dict:
    resource = get_required_resource("phase7.stats_archive_sheet")
    spreadsheet_token = params.get("archive_spreadsheet_token") or resource.get("spreadsheet_token")
    if not spreadsheet_token:
        raise ValueError("缺少归档 spreadsheet_token")
    return {**resource, "spreadsheet_token": spreadsheet_token}


def _is_archive_sheet_conflict(add_result: dict) -> bool:
    error_text = str(add_result.get("error") or "")
    lower_text = error_text.lower()
    if (
        "already exists" in lower_text
        or "duplicate" in lower_text
        or "\u5df2\u5b58\u5728" in error_text
        or ("\u5df2" in error_text and "\u5b58\u5728" in error_text)
    ):
        return True
    return "already exists" in error_text.lower() or "已存在" in error_text


def _resolve_archive_template_range(resource: dict, sheet_ref: str) -> str:
    template = str(resource.get("default_write_range") or resource.get("source_snapshot_range") or "A1:S199")
    if "!" in template:
        template = template.split("!", 1)[1]
    return f"{sheet_ref}!{template}"


def _archive_sheet_row_count(sheet_info: dict | None) -> int:
    if not isinstance(sheet_info, dict):
        return 0
    try:
        return max(int(sheet_info.get("row_count") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _find_archive_sheet(resource: dict, archive_title: str, *, refresh: bool = False) -> dict | None:
    spreadsheet_token = str(resource.get("spreadsheet_token") or "")
    title = str(archive_title or "").strip()
    if not spreadsheet_token or not title:
        return None
    if refresh:
        _clear_spreadsheet_sheet_cache(spreadsheet_token)
    ref_map = _spreadsheet_sheet_ref_map(spreadsheet_token)
    sheet_id = str(ref_map.get(title) or "").strip()
    if not sheet_id:
        return None
    sheet_info = (
        _spreadsheet_sheet_info(spreadsheet_token, sheet_id)
        or _spreadsheet_sheet_info(spreadsheet_token, title)
        or {}
    )
    return {
        "sheet_id": sheet_id,
        "title": str(sheet_info.get("title") or title),
        "row_count": _archive_sheet_row_count(sheet_info),
    }


def _archive_clear_range(
    resource: dict,
    sheet_ref: str,
    values: list[list[Any]],
    sheet_info: dict | None = None,
) -> str:
    clear_template = _resolve_archive_template_range(resource, sheet_ref)
    shape = parse_a1_range(clear_template)
    clear_rows = max(shape["row_count"], len(values), _archive_sheet_row_count(sheet_info), 1)
    return build_range_from_template(clear_template, clear_rows, shape["col_count"])


def _clear_archive_sheet(
    resource: dict,
    sheet_ref: str,
    params: dict,
    values: list[list[Any]] | None = None,
    sheet_info: dict | None = None,
) -> dict:
    clear_template = _archive_clear_range(resource, sheet_ref, values or [], sheet_info)
    shape = parse_a1_range(clear_template)
    blank_values = [["" for _ in range(shape["col_count"])] for _ in range(shape["row_count"])]
    return feishu_operation(
        "write_sheet",
        {
            "spreadsheet_token": resource["spreadsheet_token"],
            "range": clear_template,
            "values": blank_values,
            "as": params.get("as", "bot"),
            "dry_run": bool(params.get("dry_run", False)),
        },
    )


def _write_archive_sheet(
    resource: dict,
    sheet_ref: str,
    values: list[list[Any]],
    params: dict,
    *,
    clear_first: bool = False,
    sheet_info: dict | None = None,
) -> dict:
    clear_result = None
    if clear_first:
        clear_result = _clear_archive_sheet(resource, sheet_ref, params, values, sheet_info)
        if "error" in clear_result:
            return {"error": "清空归档工作表失败", "clear_result": clear_result}
    template_range = _resolve_archive_template_range(resource, sheet_ref)
    write_range = build_range_from_template(
        template_range,
        len(values),
        max(len(row) for row in values) if values else 1,
    )
    write_result = feishu_operation(
        "write_sheet",
        {
            "spreadsheet_token": resource["spreadsheet_token"],
            "range": write_range,
            "values": values,
            "as": params.get("as", "bot"),
            "dry_run": bool(params.get("dry_run", False)),
        },
    )
    if "error" in write_result:
        return {"error": "写入归档工作表失败", "clear_result": clear_result, "write_result": write_result}
    return {"ok": True, "clear_result": clear_result, "write_result": write_result}


def _archive_snapshot_legacy(values: list[list[Any]], params: dict) -> dict:
    _emit_progress("开始写入归档表", rows=len(values))
    resource = _build_archive_resource(params)
    archive_title = str(params.get("archive_title") or _archive_title(params)).strip()
    add_result = feishu_operation(
        "add_sheet",
        {
            "spreadsheet_token": resource["spreadsheet_token"],
            "title": archive_title,
            "dry_run": bool(params.get("dry_run", False)),
        },
    )
    if params.get("dry_run", False):
        _emit_progress("归档表 dry_run 跳过写入", archive_title=archive_title)
        return {"ok": True, "skipped": True, "archive_title": archive_title, "add_result": add_result}

    reused_existing_sheet = False
    sheet_ref = ""
    if "error" in add_result:
        if not _is_archive_sheet_conflict(add_result):
            _emit_progress("新增归档工作表失败", error=add_result.get("error", ""))
            return {"error": "新增归档工作表失败", "feishu_result": add_result}
        reused_existing_sheet = True
        sheet_ref = archive_title
    else:
        replies = add_result.get("data", {}).get("replies", [])
        sheet_id = None
        if replies and isinstance(replies[0], dict):
            sheet_id = (
                replies[0].get("addSheet", {})
                .get("properties", {})
                .get("sheetId")
            )
        if not sheet_id:
            _emit_progress("归档工作表缺少 sheetId")
            return {"error": "新增归档工作表返回缺少 sheetId", "feishu_result": add_result}
        sheet_ref = str(sheet_id)

    write_bundle = _write_archive_sheet(
        resource,
        sheet_ref,
        values,
        params,
        clear_first=reused_existing_sheet,
    )
    if "error" in write_bundle:
        _emit_progress("写入归档工作表失败", error=write_bundle.get("error", ""))
        return {
            "error": write_bundle["error"],
            "archive_title": archive_title,
            "reused_existing_sheet": reused_existing_sheet,
            "add_result": add_result,
            **{key: value for key, value in write_bundle.items() if key != "error"},
        }
    _emit_progress("归档表写入完成", archive_title=archive_title, reused_existing_sheet=reused_existing_sheet)
    return {
        "ok": True,
        "archive_title": archive_title,
        "reused_existing_sheet": reused_existing_sheet,
        "add_result": add_result,
        **write_bundle,
    }


def _sheet_id_from_add_result(add_result: dict) -> str | None:
    replies = add_result.get("data", {}).get("replies", [])
    if not replies or not isinstance(replies[0], dict):
        return None
    sheet_id = (
        replies[0].get("addSheet", {})
        .get("properties", {})
        .get("sheetId")
    )
    return str(sheet_id) if sheet_id else None


def _archive_snapshot(values: list[list[Any]], params: dict) -> dict:
    _emit_progress("start writing archive sheet", rows=len(values))
    resource = _build_archive_resource(params)
    archive_title = str(params.get("archive_title") or _archive_title(params)).strip()

    if params.get("dry_run", False):
        add_result = feishu_operation(
            "add_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "title": archive_title,
                "dry_run": True,
            },
        )
        _emit_progress("archive sheet dry_run skipped", archive_title=archive_title)
        return {"ok": True, "skipped": True, "archive_title": archive_title, "add_result": add_result}

    reused_existing_sheet = False
    sheet_info = _find_archive_sheet(resource, archive_title, refresh=True)
    add_result: dict[str, Any] = {
        "ok": True,
        "skipped": True,
        "reason": "sheet_exists",
    }

    if sheet_info:
        reused_existing_sheet = True
        sheet_id = str(sheet_info["sheet_id"])
    else:
        add_result = feishu_operation(
            "add_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "title": archive_title,
                "dry_run": False,
            },
        )
        if "error" in add_result:
            if not _is_archive_sheet_conflict(add_result):
                _emit_progress("failed to add archive sheet", error=add_result.get("error", ""))
                return {"error": "failed to add archive sheet", "feishu_result": add_result}
            sheet_info = _find_archive_sheet(resource, archive_title, refresh=True)
            if not sheet_info:
                _emit_progress("archive sheet conflict but sheet was not found", archive_title=archive_title)
                return {
                    "error": "archive sheet already exists but could not be resolved",
                    "archive_title": archive_title,
                    "feishu_result": add_result,
                }
            reused_existing_sheet = True
            sheet_id = str(sheet_info["sheet_id"])
        else:
            sheet_id = _sheet_id_from_add_result(add_result) or ""
            if not sheet_id:
                _emit_progress("archive add_sheet response missing sheetId")
                return {"error": "add_sheet response missing sheetId", "feishu_result": add_result}
            _clear_spreadsheet_sheet_cache(str(resource["spreadsheet_token"]))
            sheet_info = {"sheet_id": sheet_id, "title": archive_title, "row_count": 0}

    sheet_ref = sheet_id
    if add_result.get("skipped"):
        add_result["sheet_id"] = sheet_id

    write_bundle = _write_archive_sheet(
        resource,
        sheet_ref,
        values,
        params,
        clear_first=reused_existing_sheet,
        sheet_info=sheet_info,
    )
    if "error" in write_bundle:
        _emit_progress("failed to write archive sheet", error=write_bundle.get("error", ""))
        return {
            "error": write_bundle["error"],
            "archive_title": archive_title,
            "sheet_ref": sheet_ref,
            "sheet_id": sheet_id,
            "reused_existing_sheet": reused_existing_sheet,
            "add_result": add_result,
            **{key: value for key, value in write_bundle.items() if key != "error"},
        }

    _emit_progress("archive sheet written", archive_title=archive_title, reused_existing_sheet=reused_existing_sheet)
    return {
        "ok": True,
        "archive_title": archive_title,
        "sheet_ref": sheet_ref,
        "sheet_id": sheet_id,
        "reused_existing_sheet": reused_existing_sheet,
        "add_result": add_result,
        **write_bundle,
    }


def _trigger_stats_flow(params: dict) -> dict:
    resource = get_workflow_resource("phase7.stats_flow_webhook")
    if not resource or not resource.get("url"):
        return {"ok": False, "skipped": True, "reason": "missing_resource"}
    return feishu_operation(
        "trigger_webhook",
        {
            "url": resource["url"],
            "payload": params.get("flow_payload"),
            "timeout": params.get("flow_timeout", 20),
            "dry_run": bool(params.get("dry_run", False)),
        },
    )


def _refresh_split_pending_after_stats(values: list[list[Any]], params: dict) -> dict[str, Any]:
    """Refresh the split/not-arrived snapshot without uploading any business action."""
    _emit_progress("开始刷新分批及有发未到表", source_rows=max(len(values) - 1, 0))
    result = refresh_split_pending_snapshot(values, dry_run=bool(params.get("dry_run", False)))
    quantity_summary = result.get("quantity_summary") or {}
    _emit_progress(
        "分批及有发未到表刷新完成",
        source_rows=result.get("source_rows"),
        complete_rows=result.get("complete_rows"),
        pending_rows=result.get("rows"),
        expected_min=quantity_summary.get("expected_min"),
        expected_max=quantity_summary.get("expected_max"),
        arrived_min=quantity_summary.get("arrived_min"),
        arrived_max=quantity_summary.get("arrived_max"),
        pending_min=quantity_summary.get("pending_min"),
        pending_max=quantity_summary.get("pending_max"),
        dry_run=bool(params.get("dry_run", False)),
    )
    return result


def run_arrival_stats_sync(params: dict) -> dict:
    try:
        return _run_arrival_stats_sync(params)
    except TMSAuthSyncError as exc:
        return exc.result


def _run_arrival_stats_sync(params: dict) -> dict:
    _emit_progress("统计到货数据任务开始")
    if params.get("refresh_disabled"):
        scan_rows = list_scan_codes()
        current_scan_rows = scan_rows
        scan_result = {"ok": True, "skipped": True, "reason": "refresh_disabled", "loaded": len(scan_rows)}
        _emit_progress("跳过刷新扫描索引")
    else:
        current_scan_rows, scan_result = _refresh_scan_index(params)
        scan_rows = list_scan_codes()
        scan_result["accumulated"] = len(scan_rows)
        _emit_progress("加载累计扫描索引", accumulated=len(scan_rows))

    if not bool(params.get("dry_run", False)):
        retention_days = params.get("scan_codes_retention_days", 30)
        try:
            cleanup_result = cleanup_scan_codes(int(retention_days))
            _emit_progress(
                "清理过期扫描记录",
                deleted=cleanup_result.get("deleted"),
                retention_days=cleanup_result.get("retention_days"),
            )
            scan_result["cleanup"] = cleanup_result
        except Exception as exc:
            _emit_progress("清理扫描记录失败", error=str(exc))
            scan_result["cleanup_error"] = str(exc)

    existing_records = list_waybill_records(include_receipt_like=False)
    missing_tracking_numbers = _missing_trackings_from_current_scan(current_scan_rows, existing_records, params)
    detail_tracking_numbers, detail_plan = _detail_tracking_numbers(existing_records, missing_tracking_numbers, params)
    _emit_progress(
        "缺失/需补抓主单已识别",
        missing=detail_plan.get("missing"),
        stale=detail_plan.get("stale"),
        tracking_number=detail_plan.get("total"),
    )
    _emit_progress("缺失主单已识别", tracking_number=len(missing_tracking_numbers))

    if params.get("skip_waybill_refresh"):
        fetched_records = []
        fetch_result = {
            "ok": True,
            "requested": len(detail_tracking_numbers),
            "fetched": 0,
            "skipped": True,
            "reason": "skip_waybill_refresh",
            "detail_plan": detail_plan,
        }
        _emit_progress("跳过缺失主单补抓")
    else:
        fetched_records, fetch_result = _fetch_waybill_details(detail_tracking_numbers, params)
        fetch_result["detail_plan"] = detail_plan
    if bool(params.get("dry_run", False)):
        upsert_result = {"ok": True, "upserted": len(fetched_records), "skipped": True}
    else:
        upsert_result = upsert_waybill_records(fetched_records)
    _emit_progress("主单数据已写入 MySQL", upserted=upsert_result.get("upserted"))

    merged_records = _merge_records(existing_records, fetched_records)
    export_records = merged_records
    if params.get("export_limit") not in (None, ""):
        export_records = export_records[: int(params.get("export_limit"))]
    _emit_progress("导出数据已准备", tracking_number=len(export_records))

    tracking_numbers = [str(item.get("tracking_number") or "") for item in export_records if item.get("tracking_number")]
    if params.get("child_count_limit") not in (None, ""):
        tracking_numbers = tracking_numbers[: int(params.get("child_count_limit"))]
    if str(params.get("count_source") or "scan_index").strip().lower() in {"browser", "child_count"}:
        count_map, count_result = _count_arrivals(tracking_numbers, params)
    else:
        _emit_progress("开始按扫描索引统计到货件数", requested=len(tracking_numbers))
        count_map, count_result = _count_arrivals_from_scan_rows(scan_rows, export_records, tracking_numbers)
        _emit_progress(
            "扫描索引到货件数统计完成",
            counted=count_result.get("counted"),
            arrived_nonzero=count_result.get("arrived_nonzero"),
        )

    values = render_stats_sheet_values(export_records, count_map, target_date=params.get("target_date"))
    _emit_progress("统计结果已渲染", rows=len(values))
    debug_tracking = str(params.get("debug_tracking") or "").strip()
    debug_count = None
    if debug_tracking:
        debug_count = {
            "tracking": debug_tracking,
            "count": count_map.get(debug_tracking),
        }

    primary_result = _write_stats_sheet("phase7.arrive_primary_sheet", values, params)
    if "error" in primary_result:
        return {"error": "主统计表写入失败", "sheet_result": primary_result}
    secondary_result = _write_stats_sheet("phase7.arrive_secondary_sheet", values, params)
    if "error" in secondary_result:
        return {"error": "副统计表写入失败", "sheet_result": secondary_result}

    pending_result: dict[str, Any]
    if params.get("pending_sheet_disabled"):
        pending_result = {"ok": False, "skipped": True, "reason": "disabled"}
        _emit_progress("跳过未齐货物表")
    else:
        pending_records = list_pending_waybills(include_receipt_like=False)
        pending_values = render_pending_sheet_values(pending_records)
        _emit_progress("未齐货物清单已渲染", rows=len(pending_records))
        try:
            pending_result = _write_stats_sheet(
                "phase7.pending_arrivals_sheet", pending_values, params
            )
            if "error" in pending_result:
                _emit_progress("未齐货物表写入失败", error=pending_result.get("error", ""))
        except ValueError as exc:
            pending_result = {"error": str(exc), "stage": "missing_resource"}
        if pending_result.get("error") or not pending_result.get("ok"):
            return {
                "error": f"未齐货物表写入失败: {pending_result.get('error') or pending_result}",
                "stage": "pending_sheet_failed",
                "primary_result": _public_result(primary_result),
                "secondary_result": _public_result(secondary_result),
                "pending_result": _public_result(pending_result),
            }

    if params.get("archive_snapshot", True):
        archive_result = _archive_snapshot(values, params)
        _emit_progress("归档结果已返回", ok=archive_result.get("ok"))
        if archive_result.get("error") or not archive_result.get("ok"):
            return {
                "error": f"统计到货归档失败: {archive_result.get('error') or archive_result}",
                "stage": "archive_snapshot_failed",
                "primary_result": _public_result(primary_result),
                "secondary_result": _public_result(secondary_result),
                "pending_result": _public_result(pending_result),
                "archive_result": _public_result(archive_result),
            }
    else:
        archive_result = {"ok": False, "skipped": True, "reason": "disabled"}
        _emit_progress("跳过归档快照")

    if params.get("trigger_flow"):
        _emit_progress("触发遗留后续流程")
        flow_result = _trigger_stats_flow(params)
        _emit_progress("遗留后续流程返回", ok=flow_result.get("ok"), skipped=flow_result.get("skipped"))
        if flow_result.get("error") or not flow_result.get("ok"):
            return {
                "error": f"统计到货后续流程失败: {flow_result.get('error') or flow_result}",
                "stage": "flow_failed",
                "primary_result": _public_result(primary_result),
                "secondary_result": _public_result(secondary_result),
                "pending_result": _public_result(pending_result),
                "archive_result": _public_result(archive_result),
                "flow_result": _public_result(flow_result),
            }
    else:
        flow_result = {"ok": False, "skipped": True, "reason": "disabled"}
        _emit_progress("跳过遗留后续流程")

    try:
        split_pending_result = _refresh_split_pending_after_stats(values, params)
    except Exception as exc:
        detail = str(exc)[:500]
        _emit_progress("分批及有发未到表刷新失败", error=detail)
        return {
            "error": f"分批及有发未到表刷新失败: {detail}",
            "stage": "split_pending_snapshot_failed",
            "scan_result": _public_result(scan_result),
            "fetch_result": _public_result(fetch_result),
            "upsert_result": _public_result(upsert_result),
            "count_result": _public_result(count_result),
            "primary_result": _public_result(primary_result),
            "secondary_result": _public_result(secondary_result),
            "pending_result": _public_result(pending_result),
            "archive_result": _public_result(archive_result),
            "flow_result": _public_result(flow_result),
        }

    snapshot_records = [
        {
            "tracking_number": record.get("tracking_number"),
            "destination_station": record.get("destination_station"),
            "expected_quantity": record.get("quantity"),
            "arrived_quantity": count_map.get(str(record.get("tracking_number") or "")),
            "goods_name": record.get("goods_name"),
            "package_type": record.get("package_type"),
            "delivery_method": record.get("delivery_method"),
            "recipient_address": record.get("recipient_address"),
        }
        for record in export_records
        if record.get("tracking_number")
    ]
    try:
        arrival_snapshot_result = save_arrival_stat_snapshot(
            _target_date(params) or business_now().date(),
            snapshot_records,
            dry_run=bool(params.get("dry_run", False)),
        )
    except Exception as exc:
        detail = str(exc)[:500]
        _emit_progress("实际到货共享快照写入失败", error=detail)
        return {
            "error": f"实际到货共享快照写入失败: {detail}",
            "stage": "arrival_snapshot_failed",
            "primary_result": _public_result(primary_result),
            "secondary_result": _public_result(secondary_result),
            "pending_result": _public_result(pending_result),
            "split_pending_result": _public_result(split_pending_result),
            "archive_result": _public_result(archive_result),
        }

    _emit_progress("统计到货数据任务结束")
    return {
        "ok": True,
        "scan_result": _public_result(scan_result),
        "fetch_result": _public_result(fetch_result),
        "upsert_result": _public_result(upsert_result),
        "count_result": _public_result(count_result),
        "primary_result": _public_result(primary_result),
        "secondary_result": _public_result(secondary_result),
        "pending_result": _public_result(pending_result),
        "split_pending_result": _public_result(split_pending_result),
        "arrival_snapshot_result": _public_result(arrival_snapshot_result),
        "archive_result": _public_result(archive_result),
        "flow_result": _public_result(flow_result),
        "debug_tracking": debug_count,
        "main_trackings": len(main_trackings_from_scan_rows(scan_rows)),
        "records": len(export_records),
    }


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_arrival_stats_sync(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
