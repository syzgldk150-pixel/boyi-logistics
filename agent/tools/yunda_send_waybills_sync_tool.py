"""Sync Yunda send waybills into Feishu Bitable."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.workflow_resource_store import get_workflow_resource
from shared.yunda_console_waybill import build_console_waybill_from_yunda_data
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import sync_console_waybills
from tools.phase7_sync_common import (
    bind_explicit_account_id,
    build_range_from_template,
    require_explicit_account_id,
    sync_sheet_snapshot,
    tms_auth_error_result,
)
from tools.tms_tool import call_http_service


RESOURCE_KEY = "phase7.yunda_send_waybills_bitable"
SHEET_RESOURCE_KEY = "phase7.yunda_send_waybills_sheet"
DEFAULT_BASE_TOKEN = "Fcm8b2H7wayK1UsYLjlcFmWhnMh"
DEFAULT_TABLE_ID = "tblNHfIVVeaTBB7Y"
DEFAULT_SPREADSHEET_TOKEN = "GILYss6KhhBBuRt9FPWcXbben7c"
DEFAULT_SHEET_REF = "Sheet1"
DEFAULT_SHEET_TEMPLATE_RANGE = f"{DEFAULT_SHEET_REF}!A2:A2"
DEFAULT_SHEET_CLEAR_END_ROW = 5000
DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
NO_FETCHED_ROWS_REASON = "no_fetched_rows"
INDEX_FIELD_NAME = "5.14编号"
INDEX_FIELD_ALIASES = (INDEX_FIELD_NAME, "运单编号", "运单号")
DATE_FIELD_NAME = "日期"

FIELD_NAMES = (
    INDEX_FIELD_NAME,
    "目的网点",
    "收件区/县",
    "收件地址",
    "寄件人",
    "寄件手机",
    "收货人",
    "收货电话",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "实际重量",
    "现付",
    "月结",
    "提付",
    "中转运费",
    "回单号",
    "备注",
    "结算重量",
    "体积",
    "支付类型",
    "体积重",
    "到付款",
    DATE_FIELD_NAME,
)

NUMBER_FIELDS = {
    "件数",
    "实际重量",
    "现付",
    "月结",
    "提付",
    "中转运费",
    "结算重量",
    "体积",
    "体积重",
    "到付款",
}
DATE_FIELDS = {DATE_FIELD_NAME}
FIELD_TYPES = {
    name: 5 if name in DATE_FIELDS else (2 if name in NUMBER_FIELDS else 1)
    for name in FIELD_NAMES
}


def _emit_progress(message: str, **extra: Any) -> None:
    payload = " | ".join(f"{key}={value}" for key, value in extra.items() if value not in (None, ""))
    text = f"[progress] {message}"
    if payload:
        text = f"{text} | {payload}"
    print(text, file=sys.stderr, flush=True)


def _target_date(params: dict[str, Any]) -> dt.date:
    raw = str(params.get("target_date") or "").strip()
    if raw:
        return dt.date.fromisoformat(raw[:10])
    return dt.datetime.now(DEFAULT_TZ).date()


def _parse_date_param(params: dict[str, Any], key: str) -> dt.date | None:
    raw = str(params.get(key) or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ValueError(f"{key} 必须是 YYYY-MM-DD 格式") from exc


def _date_range(params: dict[str, Any]) -> list[dt.date]:
    start_date = _parse_date_param(params, "start_date")
    end_date = _parse_date_param(params, "end_date")
    if start_date is None and end_date is None:
        return [_target_date(params)]
    if start_date is None:
        start_date = end_date
    if end_date is None:
        end_date = start_date
    if start_date is None or end_date is None:
        return [_target_date(params)]
    if start_date > end_date:
        raise ValueError("start_date 不能晚于 end_date")
    return [start_date + dt.timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_number(value: Any) -> int | float | None:
    if value in (None, "", "null"):
        return None
    text = str(value).replace(",", "").strip()
    if not text or text == "*":
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _to_date_timestamp_ms(value: Any) -> int | None:
    if isinstance(value, dt.datetime):
        value_dt = value
    elif isinstance(value, dt.date):
        value_dt = dt.datetime.combine(value, dt.time.min)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            value_dt = dt.datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if value_dt.tzinfo is None:
        value_dt = value_dt.replace(tzinfo=DEFAULT_TZ)
    return int(value_dt.timestamp() * 1000)


def _field_value(name: str, value: Any) -> Any:
    if name in NUMBER_FIELDS:
        return _to_number(value)
    if name in DATE_FIELDS:
        return _to_date_timestamp_ms(value)
    if value is None:
        return ""
    return str(value).strip()


def _resolve_bitable_target(params: dict[str, Any]) -> tuple[str, str]:
    base_token = params.get("base_token") or params.get("app_token")
    table_id = params.get("table_id")
    if base_token and table_id:
        return str(base_token), str(table_id)
    try:
        resource = get_workflow_resource(RESOURCE_KEY)
    except Exception:
        resource = None
    if resource and resource.get("base_token") and resource.get("table_id"):
        return str(resource["base_token"]), str(resource["table_id"])
    return DEFAULT_BASE_TOKEN, DEFAULT_TABLE_ID


def _config_value(params: dict[str, Any], resource: dict[str, Any], *keys: str) -> str:
    for source in (params, resource):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return str(value).strip()
    return ""


def _qualify_sheet_range(value_range: str, sheet_ref: str) -> str:
    text = str(value_range or "").strip()
    if not text or "!" in text:
        return text
    return f"{sheet_ref}!{text}"


def _resolve_sheet_params(params: dict[str, Any], row_count: int) -> dict[str, Any]:
    try:
        resource = get_workflow_resource(SHEET_RESOURCE_KEY) or {}
    except Exception:
        resource = {}
    if not isinstance(resource, dict):
        resource = {}

    sheet_ref = _config_value(params, resource, "sheet_id", "sheet_name") or DEFAULT_SHEET_REF
    template_range = (
        _config_value(params, resource, "sheet_range", "range")
        or DEFAULT_SHEET_TEMPLATE_RANGE
    )
    template_range = _qualify_sheet_range(template_range, sheet_ref)
    write_range = build_range_from_template(
        template_range,
        max(row_count, 1),
        len(FIELD_NAMES),
    )

    clear_range = _config_value(params, resource, "sheet_clear_range", "clear_range")
    if clear_range:
        clear_range = _qualify_sheet_range(clear_range, sheet_ref)
    else:
        clear_range = build_range_from_template(
            template_range,
            max(DEFAULT_SHEET_CLEAR_END_ROW - 1, 1),
            len(FIELD_NAMES),
        )

    return {
        "spreadsheet_token": (
            _config_value(params, resource, "sheet_spreadsheet_token", "spreadsheet_token")
            or DEFAULT_SPREADSHEET_TOKEN
        ),
        "range": write_range,
        "clear_range": clear_range,
    }


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for value in (
        payload.get("data", {}).get("items") if isinstance(payload.get("data"), dict) else None,
        payload.get("items"),
        payload.get("records"),
    ):
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _field_name(item: dict[str, Any]) -> str:
    return str(item.get("field_name") or item.get("name") or "").strip()


def _primary_field_name(items: list[dict[str, Any]]) -> str | None:
    for item in items:
        if item.get("is_primary") is True:
            name = _field_name(item)
            if name:
                return name
    if items:
        name = _field_name(items[0])
        if name:
            return name
    return None


def _resolve_index_field_name(existing_fields: set[str], primary_field_name: str | None) -> str:
    for alias in INDEX_FIELD_ALIASES:
        if alias in existing_fields:
            return alias
    return primary_field_name or INDEX_FIELD_NAME


def _build_field_name_map(
    existing_fields: set[str],
    *,
    primary_field_name: str | None,
    create_missing: bool,
) -> dict[str, str]:
    field_name_map: dict[str, str] = {}
    index_field_name = _resolve_index_field_name(existing_fields, primary_field_name)
    field_name_map[INDEX_FIELD_NAME] = index_field_name
    for name in FIELD_NAMES:
        if name == INDEX_FIELD_NAME:
            continue
        if create_missing or name in existing_fields:
            field_name_map[name] = name
    return field_name_map


def _ensure_fields(base_token: str, table_id: str, params: dict[str, Any]) -> dict[str, Any]:
    if params.get("dry_run"):
        return {
            "ok": True,
            "skipped": True,
            "planned_fields": [{"field_name": name, "type": FIELD_TYPES[name]} for name in FIELD_NAMES],
            "primary_field_name": INDEX_FIELD_NAME,
            "has_explicit_index_field": True,
            "index_field_name": INDEX_FIELD_NAME,
            "field_name_map": {name: name for name in FIELD_NAMES},
        }

    create_missing = params.get("ensure_fields") is not False
    list_result = feishu_operation(
        "list_fields",
        {
            "base_token": base_token,
            "table_id": table_id,
            "as": params.get("as", "bot"),
        },
    )
    if "error" in list_result:
        return {"error": "飞书读取多维表字段失败", "feishu_result": list_result}

    field_items = _extract_items(list_result)
    existing = {_field_name(item) for item in field_items}
    primary_field_name = _primary_field_name(field_items)
    index_field_name = _resolve_index_field_name(existing, primary_field_name)
    has_explicit_index_field = index_field_name == INDEX_FIELD_NAME
    created: list[dict[str, Any]] = []
    for name in FIELD_NAMES if create_missing else ():
        if name == INDEX_FIELD_NAME and primary_field_name and not has_explicit_index_field:
            continue
        if name in existing:
            continue
        create_result = feishu_operation(
            "create_field",
            {
                "base_token": base_token,
                "table_id": table_id,
                "field_name": name,
                "type": FIELD_TYPES[name],
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in create_result:
            return {
                "error": f"飞书创建字段失败: {name}",
                "field_name": name,
                "feishu_result": create_result,
            }
        created.append({"field_name": name, "result": create_result})
        existing.add(name)
    field_name_map = _build_field_name_map(
        existing,
        primary_field_name=primary_field_name,
        create_missing=create_missing,
    )
    return {
        "ok": True,
        "skipped": not create_missing,
        "created": created,
        "primary_field_name": primary_field_name,
        "has_explicit_index_field": has_explicit_index_field,
        "index_field_name": field_name_map.get(INDEX_FIELD_NAME) or index_field_name,
        "field_name_map": field_name_map,
        "existing_fields": sorted(existing),
    }


def _text_from_field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, list):
        parts = [_text_from_field_value(item) for item in value]
        return "".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "value", "name", "link"):
            text = _text_from_field_value(value.get(key))
            if text:
                return text
        return "".join(_text_from_field_value(item) for item in value.values()).strip()
    return str(value).strip()


def _normalize_waybill(value: Any) -> str:
    text = _text_from_field_value(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def _date_text_from_field_value(value: Any) -> str:
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value) / 1000, DEFAULT_TZ).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = _text_from_field_value(value)
    if not text:
        return ""
    if text.isdigit() and len(text) >= 12:
        try:
            return dt.datetime.fromtimestamp(int(text) / 1000, DEFAULT_TZ).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        try:
            return dt.date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return ""
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def _console_date_text(value: Any, *, target_date: dt.date) -> str:
    text = _text_from_field_value(value)
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        try:
            return dt.date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            pass
    return target_date.isoformat()


def _sheet_cell_value(name: str, value: Any, *, target_date: dt.date) -> Any:
    if name in NUMBER_FIELDS:
        return _to_number(value) if _to_number(value) is not None else ""
    if name in DATE_FIELDS:
        return _console_date_text(value, target_date=target_date)
    if value is None:
        return ""
    return str(value).strip()


def _build_sheet_values(rows: list[dict[str, Any]], *, target_date: dt.date) -> list[list[Any]]:
    values: list[list[Any]] = []
    for row in rows:
        waybill_no = _normalize_waybill(row.get(INDEX_FIELD_NAME))
        if not waybill_no:
            continue
        line: list[Any] = []
        for name in FIELD_NAMES:
            value = waybill_no if name == INDEX_FIELD_NAME else row.get(name)
            if name == DATE_FIELD_NAME and value in (None, ""):
                value = target_date
            line.append(_sheet_cell_value(name, value, target_date=target_date))
        values.append(line)
    return values


def _sync_sheet_copy(rows: list[dict[str, Any]], params: dict[str, Any], *, target_date: dt.date) -> dict[str, Any]:
    if params.get("sync_sheet") is False or params.get("_skip_sheet_sync") is True:
        return {"ok": True, "skipped": True, "rows": 0}
    values = _build_sheet_values(rows, target_date=target_date)
    if not values:
        _emit_progress(
            "Skip Yunda send waybill sheet sync",
            rows=0,
            reason=NO_FETCHED_ROWS_REASON,
        )
        return {"ok": True, "skipped": True, "rows": 0, "reason": NO_FETCHED_ROWS_REASON}
    sheet_params = {
        **params,
        **_resolve_sheet_params(params, len(values)),
    }
    _emit_progress(
        "开始同步韵达寄件运单电子表格",
        rows=len(values),
        range=sheet_params.get("range"),
        clear_range=sheet_params.get("clear_range"),
    )
    return sync_sheet_snapshot(SHEET_RESOURCE_KEY, values, sheet_params)


def _weight_volume_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for label, field_name in (
        ("实际重量", "实际重量"),
        ("体积", "体积"),
        ("结算重量", "结算重量"),
        ("体积重", "体积重"),
    ):
        value = _text_from_field_value(row.get(field_name))
        if value:
            parts.append(f"{label} {value}")
    return " / ".join(parts)


def _freight_fee_text(row: dict[str, Any]) -> str:
    for field_name in ("现付", "月结", "提付"):
        value = _text_from_field_value(row.get(field_name))
        if value:
            return value
    return ""


def _scan_status_text(row: dict[str, Any]) -> str:
    for field_name in (
        "当前扫描状态",
        "最新扫描状态",
        "扫描状态",
        "最新扫描类型",
        "scan_status",
        "current_scan_status",
        "scan_type",
        "SCAN_TYPE",
    ):
        value = _text_from_field_value(row.get(field_name))
        if value:
            return value
    return ""


def _console_waybill_records(rows: list[dict[str, Any]], *, target_date: dt.date) -> list[dict[str, Any]]:
    shared_records: list[dict[str, Any]] = []
    for row in rows:
        mapped = build_console_waybill_from_yunda_data(row, target_date=target_date)
        if mapped:
            shared_records.append(mapped)
    if shared_records:
        return shared_records
    records: list[dict[str, Any]] = []
    for row in rows:
        waybill_no = _normalize_waybill(row.get(INDEX_FIELD_NAME))
        if not waybill_no:
            continue
        records.append(
            {
                "waybill_no": waybill_no,
                "destination_site": row.get("目的网点", ""),
                "open_date": _console_date_text(row.get(DATE_FIELD_NAME), target_date=target_date),
                "receiver_address": row.get("收件地址", ""),
                "receiver_name": row.get("收货人", ""),
                "receiver_phone": row.get("收货电话", ""),
                "sender_name": row.get("寄件人", ""),
                "sender_phone": row.get("寄件手机", ""),
                "goods_name_lines": row.get("货物名称", ""),
                "package_type_lines": row.get("包装类型", ""),
                "quantity_lines": row.get("件数", ""),
                "weight_volume": _weight_volume_text(row),
                "delivery_method": row.get("派送方式", ""),
                "freight_fee": _freight_fee_text(row),
                "pickup_fee": "",
                "delivery_fee": "",
                "transfer_fee": row.get("中转运费", ""),
                "payment_method": row.get("支付类型", ""),
                "insurance_amount": "",
                "cod_amount": row.get("到付款", ""),
                "remark": row.get("备注", ""),
                "scan_status": _scan_status_text(row),
                "status": "in_transit",
            }
        )
    return records


def _existing_record_index(
    base_token: str,
    table_id: str,
    params: dict[str, Any],
    *,
    index_field_name: str,
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    limit = int(params.get("list_limit") or 500)
    max_pages = int(params.get("list_max_pages") or 50)
    offset = 0
    pages: list[dict[str, Any]] = []
    index: dict[str, str] = {}
    for _page_index in range(max_pages):
        list_result = feishu_operation(
            "list_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "limit": limit,
                "offset": offset,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        pages.append(list_result)
        if "error" in list_result:
            return None, {"pages": pages, "error": list_result.get("error")}
        items = _extract_items(list_result)
        for item in items:
            record_id = str(item.get("record_id") or "").strip()
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            waybill_no = _normalize_waybill(fields.get(index_field_name))
            if record_id and waybill_no and waybill_no not in index:
                index[waybill_no] = record_id
        if len(items) < limit:
            break
        offset += limit
    return index, {"pages": pages, "indexed": len(index)}


def _existing_record_ids_for_date(
    base_token: str,
    table_id: str,
    params: dict[str, Any],
    *,
    date_field_name: str,
    target_date: dt.date,
) -> tuple[list[str] | None, dict[str, Any]]:
    limit = int(params.get("list_limit") or 500)
    max_pages = int(params.get("list_max_pages") or 50)
    offset = 0
    pages: list[dict[str, Any]] = []
    record_ids: list[str] = []
    seen_record_ids: set[str] = set()
    target_date_text = target_date.isoformat()
    for _page_index in range(max_pages):
        list_result = feishu_operation(
            "list_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "limit": limit,
                "offset": offset,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        pages.append(list_result)
        if "error" in list_result:
            return None, {"pages": pages, "error": list_result.get("error")}
        items = _extract_items(list_result)
        for item in items:
            record_id = str(item.get("record_id") or "").strip()
            if not record_id or record_id in seen_record_ids:
                continue
            seen_record_ids.add(record_id)
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            if _date_text_from_field_value(fields.get(date_field_name)) == target_date_text:
                record_ids.append(record_id)
        if len(items) < limit:
            break
        offset += limit
    return record_ids, {
        "pages": pages,
        "scanned": len(seen_record_ids),
        "target_date_record_ids": record_ids,
        "date_field_name": date_field_name,
    }


def _build_records(
    rows: list[dict[str, Any]],
    *,
    existing_by_waybill: dict[str, str],
    field_name_map: dict[str, str],
    target_date: dt.date,
) -> tuple[list[dict[str, Any]], int, int]:
    records: list[dict[str, Any]] = []
    update_count = 0
    create_count = 0
    for row in rows:
        waybill_no = _normalize_waybill(row.get(INDEX_FIELD_NAME))
        if not waybill_no:
            continue
        fields: dict[str, Any] = {}
        for name in FIELD_NAMES:
            actual_name = field_name_map.get(name)
            if not actual_name:
                continue
            value = waybill_no if name == INDEX_FIELD_NAME else row.get(name)
            if name == DATE_FIELD_NAME and value in (None, ""):
                value = target_date
            fields[actual_name] = _field_value(name, value)
        record: dict[str, Any] = {"fields": fields}
        record_id = existing_by_waybill.get(waybill_no)
        if record_id:
            record["record_id"] = record_id
            update_count += 1
        else:
            create_count += 1
        records.append(record)
    return records, update_count, create_count


def _extract_tms_payload(tms_result: Any) -> dict[str, Any] | None:
    if isinstance(tms_result, dict):
        data = tms_result.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(tms_result.get("records"), list):
            return tms_result
    return None


def _tms_service_error(tms_result: Any) -> dict[str, Any] | None:
    if not isinstance(tms_result, dict):
        return None
    if tms_result.get("ok") is not False and not tms_result.get("error"):
        return None
    message = str(
        tms_result.get("error")
        or tms_result.get("message")
        or tms_result.get("detail")
        or "TMS 服务执行失败"
    ).strip()
    result = {"error": f"yunda_send_waybills 执行失败: {message}", "raw": tms_result}
    if tms_result.get("error_code"):
        result["error_code"] = tms_result.get("error_code")
    if tms_result.get("error_type"):
        result["error_type"] = tms_result.get("error_type")
    if tms_result.get("http_status"):
        result["http_status"] = tms_result.get("http_status")
    return result


def _build_request_body(params: dict[str, Any], target_date: dt.date) -> dict[str, Any]:
    request_body = params.get("request_body") if isinstance(params.get("request_body"), dict) else {}
    request_params = dict(request_body.get("params") or {})
    request_params = bind_explicit_account_id(
        request_params,
        require_explicit_account_id(params, label="韵达寄件运单同步"),
        label="韵达寄件运单请求",
    )
    request_params["target_date"] = target_date.isoformat()
    for key in ("page_size", "max_pages", "session_profile", "request_timeout_sec"):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
    return {
        "params": request_params,
        "timeout_sec": int(request_body.get("timeout_sec", params.get("timeout_sec", 1800)) or 1800),
        "client_timeout_sec": int(request_body.get("client_timeout_sec", params.get("client_timeout_sec", 1860)) or 1860),
    }


def _run_yunda_send_waybills_sync_for_date(params: dict[str, Any], target_date: dt.date) -> dict[str, Any]:
    _emit_progress("开始拉取韵达寄件运单", target_date=target_date.isoformat())
    tms_result = call_http_service("/yunda_send_waybills", _build_request_body(params, target_date))
    if auth_error := tms_auth_error_result(tms_result):
        return auth_error
    if service_error := _tms_service_error(tms_result):
        return service_error
    payload = _extract_tms_payload(tms_result)
    if payload is None or not isinstance(payload.get("records"), list):
        return {"error": "yunda_send_waybills 返回格式异常", "raw": tms_result}
    rows = [row for row in payload.get("records", []) if isinstance(row, dict)]
    _emit_progress("韵达寄件运单拉取完成", rows=len(rows), total=payload.get("total"))

    if params.get("sql_only"):
        console_records = _console_waybill_records(rows, target_date=target_date)
        if params.get("dry_run"):
            return {
                "ok": True,
                "source": "yunda_send_waybills",
                "dry_run": True,
                "sql_only": True,
                "target_date": target_date.isoformat(),
                "total": payload.get("total"),
                "fetched": len(rows),
                "planned_sql_upserts": len(console_records),
            }

        sql_result: dict[str, Any] = {"ok": True, "skipped": True, "upserted": 0, "deleted_stale": 0}
        if params.get("sync_sql") is not False:
            try:
                sql_result = sync_console_waybills(
                    console_records,
                    source="yunda",
                    target_date=target_date,
                    replace_date=True,
                )
            except Exception as exc:
                return {
                    "error": "SQL 写入韵达寄件运单失败",
                    "source": "yunda_send_waybills",
                    "sql_only": True,
                    "target_date": target_date.isoformat(),
                    "total": payload.get("total"),
                    "fetched": len(rows),
                    "sql_error": str(exc),
                }
        _emit_progress(
            "韵达寄件运单 SQL 回填完成",
            sql_upserted=sql_result.get("upserted", 0),
            sql_deleted_stale=sql_result.get("deleted_stale", 0),
        )
        return {
            "ok": True,
            "source": "yunda_send_waybills",
            "sql_only": True,
            "target_date": target_date.isoformat(),
            "total": payload.get("total"),
            "fetched": len(rows),
            "updates": 0,
            "creates": 0,
            "deleted": 0,
            "written": 0,
            "sql_upserted": sql_result.get("upserted", 0),
            "sql_updates": sql_result.get("updates", 0),
            "sql_creates": sql_result.get("creates", 0),
            "sql_deleted_stale": sql_result.get("deleted_stale", 0),
            "sql_result": sql_result,
        }

    base_token, table_id = _resolve_bitable_target(params)
    field_result = _ensure_fields(base_token, table_id, params)
    if "error" in field_result:
        return {"error": field_result["error"], "field_result": field_result, "fetched": len(rows)}

    primary_field_name = str(field_result.get("primary_field_name") or "").strip() or None
    field_name_map = field_result.get("field_name_map") if isinstance(field_result.get("field_name_map"), dict) else {}
    if not field_name_map:
        field_name_map = _build_field_name_map(
            set(FIELD_NAMES),
            primary_field_name=primary_field_name,
            create_missing=True,
        )
    if params.get("dry_run"):
        records, update_count, create_count = _build_records(
            rows,
            existing_by_waybill={},
            field_name_map=field_name_map,
            target_date=target_date,
        )
        console_records = _console_waybill_records(rows, target_date=target_date)
        sheet_values = _build_sheet_values(rows, target_date=target_date)
        return {
            "ok": True,
            "source": "yunda_send_waybills",
            "dry_run": True,
            "target_date": target_date.isoformat(),
            "total": payload.get("total"),
            "fetched": len(rows),
            "planned": len(records),
            "planned_updates": update_count,
            "planned_creates": create_count,
            "planned_sql_upserts": len(console_records),
            "planned_sheet_rows": len(sheet_values),
            "field_result": field_result,
            "records": records,
        }

    date_field_name = str(field_name_map.get(DATE_FIELD_NAME) or DATE_FIELD_NAME).strip()
    target_date_record_ids, list_result = _existing_record_ids_for_date(
        base_token,
        table_id,
        params,
        date_field_name=date_field_name,
        target_date=target_date,
    )
    if target_date_record_ids is None:
        return {"error": "飞书读取现有寄件运单记录失败", "list_result": list_result, "fetched": len(rows)}

    records, update_count, create_count = _build_records(
        rows,
        existing_by_waybill={},
        field_name_map=field_name_map,
        target_date=target_date,
    )

    delete_result: dict[str, Any] = {"ok": True, "requested": 0, "deleted": 0, "results": []}
    if target_date_record_ids:
        delete_result = feishu_operation(
            "delete_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "record_ids": target_date_record_ids,
                "as": params.get("as", "bot"),
            },
        )
        if "error" in delete_result or delete_result.get("errors"):
            return {
                "error": "delete target-date yunda send waybill records failed",
                "fetched": len(rows),
                "updates": update_count,
                "creates": create_count,
                "target_date_record_ids": target_date_record_ids,
                "delete_result": delete_result,
                "list_result": list_result,
            }

    write_result: dict[str, Any] = {"ok": True, "requested": 0, "written": 0, "results": []}
    if records:
        _emit_progress("开始写入韵达寄件运单", rows=len(records), updates=update_count, creates=create_count)
        write_result = feishu_operation(
            "write_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "records": records,
                "as": params.get("as", "bot"),
            },
        )
        if "error" in write_result or write_result.get("errors"):
            return {
                "error": "飞书写入韵达寄件运单失败",
                "fetched": len(rows),
                "updates": update_count,
                "creates": create_count,
                "deleted": delete_result.get("deleted", 0),
                "delete_result": delete_result,
                "write_result": write_result,
            }

    sql_result: dict[str, Any] = {"ok": True, "skipped": True, "upserted": 0, "deleted_stale": 0}
    if params.get("sync_sql") is not False:
        console_records = _console_waybill_records(rows, target_date=target_date)
        try:
            sql_result = sync_console_waybills(
                console_records,
                source="yunda",
                target_date=target_date,
                replace_date=True,
            )
        except Exception as exc:
            return {
                "error": "SQL 写入韵达寄件运单失败",
                "fetched": len(rows),
                "updates": update_count,
                "creates": create_count,
                "written": write_result.get("written", 0),
                "deleted": delete_result.get("deleted", 0),
                "sql_error": str(exc),
                "delete_result": delete_result,
                "write_result": write_result,
            }

    sheet_result = _sync_sheet_copy(rows, params, target_date=target_date)
    if "error" in sheet_result:
        return {
            "error": sheet_result["error"],
            "fetched": len(rows),
            "updates": update_count,
            "creates": create_count,
            "written": write_result.get("written", 0),
            "deleted": delete_result.get("deleted", 0),
            "sql_upserted": sql_result.get("upserted", 0),
            "sheet_result": sheet_result,
            "delete_result": delete_result,
            "write_result": write_result,
            "sql_result": sql_result,
        }

    _emit_progress(
        "韵达寄件运单同步完成",
        written=write_result.get("written", 0),
        sql_upserted=sql_result.get("upserted", 0),
        sheet_rows=sheet_result.get("rows", 0),
    )
    return {
        "ok": True,
        "source": "yunda_send_waybills",
        "target_date": target_date.isoformat(),
        "total": payload.get("total"),
        "fetched": len(rows),
        "updates": update_count,
        "creates": create_count,
        "deleted": delete_result.get("deleted", 0),
        "written": write_result.get("written", 0),
        "sql_upserted": sql_result.get("upserted", 0),
        "sql_updates": sql_result.get("updates", 0),
        "sql_creates": sql_result.get("creates", 0),
        "sql_deleted_stale": sql_result.get("deleted_stale", 0),
        "sheet_rows": sheet_result.get("rows", 0),
        "field_result": field_result,
        "list_result": list_result,
        "delete_result": delete_result,
        "write_result": write_result,
        "sql_result": sql_result,
        "sheet_result": sheet_result,
    }


def _summarize_date_result(result: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "target_date": result.get("target_date"),
        "ok": bool(result.get("ok")) and not result.get("error"),
        "total": result.get("total"),
        "fetched": result.get("fetched"),
        "updates": result.get("updates"),
        "creates": result.get("creates"),
        "deleted": result.get("deleted"),
        "written": result.get("written"),
        "sql_upserted": result.get("sql_upserted"),
        "sql_deleted_stale": result.get("sql_deleted_stale"),
        "sheet_rows": result.get("sheet_rows"),
    }
    if result.get("sql_only"):
        summary["sql_only"] = True
    if result.get("dry_run"):
        summary.update(
            {
                "dry_run": True,
                "planned": result.get("planned"),
                "planned_updates": result.get("planned_updates"),
                "planned_creates": result.get("planned_creates"),
                "planned_sql_upserts": result.get("planned_sql_upserts"),
                "planned_sheet_rows": result.get("planned_sheet_rows"),
            }
        )
    if result.get("skip_reason"):
        summary["skip_reason"] = result.get("skip_reason")
    if result.get("error"):
        summary["error"] = result.get("error")
        if result.get("error_code"):
            summary["error_code"] = result.get("error_code")
    return {key: value for key, value in summary.items() if value not in (None, "")}


def _range_result(params: dict[str, Any], dates: list[dt.date]) -> dict[str, Any]:
    _emit_progress(
        "开始范围同步韵达寄件运单",
        start_date=dates[0].isoformat(),
        end_date=dates[-1].isoformat(),
        days=len(dates),
    )
    date_results: list[dict[str, Any]] = []
    raw_results: list[dict[str, Any]] = []
    per_date_params = {**params, "_skip_sheet_sync": True}
    for target_date in dates:
        result = _run_yunda_send_waybills_sync_for_date(per_date_params, target_date)
        raw_results.append(result)
        date_results.append(_summarize_date_result(result))
        if result.get("error") or not result.get("ok"):
            return {
                "error": f"韵达寄件运单范围同步失败，失败日期：{target_date.isoformat()}",
                "source": "yunda_send_waybills",
                "start_date": dates[0].isoformat(),
                "end_date": dates[-1].isoformat(),
                "failed_date": target_date.isoformat(),
                "completed_days": len(date_results) - 1,
                "per_date": date_results,
                "raw_error": result,
            }

    total = sum(_safe_int(result.get("total")) for result in raw_results)
    fetched = sum(_safe_int(result.get("fetched")) for result in raw_results)
    updates = sum(_safe_int(result.get("updates")) for result in raw_results)
    creates = sum(_safe_int(result.get("creates")) for result in raw_results)
    deleted = sum(_safe_int(result.get("deleted")) for result in raw_results)
    written = sum(_safe_int(result.get("written")) for result in raw_results)
    sql_upserted = sum(_safe_int(result.get("sql_upserted")) for result in raw_results)
    sql_deleted_stale = sum(_safe_int(result.get("sql_deleted_stale")) for result in raw_results)
    range_payload: dict[str, Any] = {
        "ok": True,
        "source": "yunda_send_waybills",
        "start_date": dates[0].isoformat(),
        "end_date": dates[-1].isoformat(),
        "days": len(dates),
        "total": total,
        "fetched": fetched,
        "updates": updates,
        "creates": creates,
        "deleted": deleted,
        "written": written,
        "sql_upserted": sql_upserted,
        "sql_deleted_stale": sql_deleted_stale,
        "per_date": date_results,
    }
    if params.get("sql_only"):
        range_payload["sql_only"] = True
    if params.get("dry_run"):
        range_payload.update(
            {
                "dry_run": True,
                "planned": sum(_safe_int(result.get("planned")) for result in raw_results),
                "planned_updates": sum(_safe_int(result.get("planned_updates")) for result in raw_results),
                "planned_creates": sum(_safe_int(result.get("planned_creates")) for result in raw_results),
                "planned_sql_upserts": sum(_safe_int(result.get("planned_sql_upserts")) for result in raw_results),
            }
        )
    return range_payload


def run_yunda_send_waybills_sync(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    try:
        dates = _date_range(params)
    except ValueError as exc:
        return {"error": str(exc), "source": "yunda_send_waybills"}
    if len(dates) == 1:
        return _run_yunda_send_waybills_sync_for_date(params, dates[0])
    return _range_result(params, dates)


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_yunda_send_waybills_sync(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
