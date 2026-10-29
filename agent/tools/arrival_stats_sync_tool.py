"""Phase 7 arrival statistics sync and snapshot archiving."""

from __future__ import annotations

import re
import sys
from datetime import date
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import (
    _clear_spreadsheet_sheet_cache,
    _spreadsheet_sheet_info,
    _spreadsheet_sheet_ref_map,
    _spreadsheet_sheet_title_count,
    feishu_operation,
)
from tools.daily_sign_values import business_now
from tools.phase7_sync_common import (
    build_range_from_template,
    get_required_resource,
    parse_a1_range,
    require_explicit_account_id,
)

_MASK_ONLY_RE = re.compile(r"^[\.\-·。…\s]+$")


def _required_account_id(params: dict[str, Any]) -> str:
    return require_explicit_account_id(params, label="到货统计")


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


def _is_masked_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return "*" in text or bool(_MASK_ONLY_RE.fullmatch(text))


def _merge_records(
    existing_records: list[dict[str, Any]], fetched_records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
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


def _write_optional_pending_sheet(values: list[list[Any]], params: dict) -> dict:
    resource_key = "phase7.pending_arrivals_sheet"
    try:
        result = _write_stats_sheet(resource_key, values, params)
    except ValueError as exc:
        detail = str(exc)
        _emit_progress("未齐货物表资源未配置，已跳过", reason=detail)
        return {
            "ok": False,
            "skipped": True,
            "reason": "missing_resource",
            "detail": detail,
        }

    if "error" not in result:
        return result

    detail = str(result.get("error") or "未齐货物表写入失败")
    _emit_progress("未齐货物表写入失败，已跳过", error=detail)
    return {
        "ok": False,
        "skipped": True,
        "reason": "write_failed",
        "detail": detail,
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
    title_count = _spreadsheet_sheet_title_count(spreadsheet_token, title)
    if title_count > 1:
        raise ValueError("arrival archive title is ambiguous")
    if title_count != 1:
        return None
    sheet_id = str(ref_map.get(title) or "").strip()
    if not sheet_id:
        return None
    sheet_info = (
        _spreadsheet_sheet_info(spreadsheet_token, sheet_id) or _spreadsheet_sheet_info(spreadsheet_token, title) or {}
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


def _sheet_id_from_add_result(add_result: dict) -> str | None:
    replies = add_result.get("data", {}).get("replies", [])
    if not replies or not isinstance(replies[0], dict):
        return None
    sheet_id = replies[0].get("addSheet", {}).get("properties", {}).get("sheetId")
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

