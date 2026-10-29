"""Sync Yunda dispatch forecast master-bill rows into Feishu Bitable."""

from __future__ import annotations

import datetime as dt
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_sync_common import (
    bind_explicit_account_id,
    require_explicit_account_id,
)


RESOURCE_KEY = "phase7.yunda_dispatch_forecast_bitable"
DEFAULT_BASE_TOKEN = "Et8sboZiSahfhYsa0i3c6hkwnXg"
DEFAULT_TABLE_ID = "tblT43ay2KjeXdC0"
DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
MAIN_FIELD_NAME = "主单号"

FIELD_NAMES = (
    MAIN_FIELD_NAME,
    "开单件数",
    "扫描件数",
    "重量/kg",
    "体积/m3",
    "包装类型",
    "清场时间",
    "规划时效",
    "开单目的地址",
    "预计到达时间",
    "应派时间",
)

FIELD_TYPES = {
    "主单号": 1,
    "开单件数": 2,
    "扫描件数": 2,
    "重量/kg": 2,
    "体积/m3": 2,
    "包装类型": 1,
    "清场时间": 1,
    "规划时效": 2,
    "开单目的地址": 1,
    "预计到达时间": 1,
    "应派时间": 1,
}

NUMBER_FIELDS = {"开单件数", "扫描件数", "重量/kg", "体积/m3", "规划时效"}
DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def _emit_progress(message: str, **extra: Any) -> None:
    payload = " | ".join(f"{key}={value}" for key, value in extra.items() if value not in (None, ""))
    text = f"[progress] {message}"
    if payload:
        text = f"{text} | {payload}"
    print(text, file=sys.stderr, flush=True)


def _target_date(params: dict[str, Any]) -> dt.date:
    raw = str(params.get("target_date") or params.get("due_delv_dt") or "").strip()
    if raw:
        return dt.date.fromisoformat(raw[:10])
    return dt.datetime.now(DEFAULT_TZ).date() + dt.timedelta(days=1)


def _to_number(value: Any) -> int | float | None:
    if value in (None, "", "null"):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _field_value(name: str, value: Any) -> Any:
    if name in NUMBER_FIELDS:
        return _to_number(value)
    if value is None:
        return ""
    return str(value).strip()


def _build_records(
    rows: list[dict[str, Any]],
    *,
    primary_field_name: str | None = None,
    has_explicit_main_field: bool = True,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        fields = {
            name: _field_value(name, row.get(name))
            for name in FIELD_NAMES
            if not (
                name == MAIN_FIELD_NAME
                and primary_field_name
                and not has_explicit_main_field
            )
        }
        if primary_field_name and primary_field_name != MAIN_FIELD_NAME:
            fields[primary_field_name] = _field_value(MAIN_FIELD_NAME, row.get(MAIN_FIELD_NAME))
        records.append({"fields": fields})
    return records


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


def _ensure_fields(base_token: str, table_id: str, params: dict[str, Any]) -> dict[str, Any]:
    if params.get("ensure_fields") is False:
        return {"ok": True, "skipped": True, "created": []}
    if params.get("dry_run"):
        return {
            "ok": True,
            "skipped": True,
            "planned_fields": [{"field_name": name, "type": FIELD_TYPES[name]} for name in FIELD_NAMES],
        }
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
    existing = {
        _field_name(item)
        for item in field_items
    }
    primary_field_name = _primary_field_name(field_items)
    has_explicit_main_field = MAIN_FIELD_NAME in existing
    created: list[dict[str, Any]] = []
    for name in FIELD_NAMES:
        if name == MAIN_FIELD_NAME and primary_field_name and not has_explicit_main_field:
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
            },
        )
        if "error" in create_result:
            return {
                "error": f"飞书创建字段失败: {name}",
                "field_name": name,
                "feishu_result": create_result,
            }
        created.append({"field_name": name, "result": create_result})
    return {
        "ok": True,
        "created": created,
        "primary_field_name": primary_field_name,
        "has_explicit_main_field": has_explicit_main_field,
    }


def _date_from_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return dt.datetime.fromtimestamp(timestamp, DEFAULT_TZ).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    if isinstance(value, dict):
        for key in ("text", "value", "name"):
            parsed = _date_from_value(value.get(key))
            if parsed:
                return parsed
        return ""
    if isinstance(value, list):
        for item in value:
            parsed = _date_from_value(item)
            if parsed:
                return parsed
        return ""
    match = DATE_RE.search(str(value))
    if not match:
        return ""
    year, month, day = match.groups()
    try:
        return dt.date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list):
        return all(_is_blank_value(item) for item in value)
    if isinstance(value, dict):
        return all(_is_blank_value(item) for item in value.values())
    return False


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
    result = {"error": f"yunda_dispatch_forecast 执行失败: {message}", "raw": tms_result}
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
        require_explicit_account_id(params, label="韵达派件预测同步"),
        label="韵达派件预测请求",
    )
    request_params["target_date"] = target_date.isoformat()
    for key in (
        "dest_brch",
        "dest_branch",
        "page_size",
        "limit",
        "max_pages",
        "session_profile",
        "two_brch_check",
        "prod_typ",
        "if_same_city",
    ):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
    return {
        "params": request_params,
        "timeout_sec": int(request_body.get("timeout_sec", params.get("timeout_sec", 900)) or 900),
        "client_timeout_sec": int(request_body.get("client_timeout_sec", params.get("client_timeout_sec", 960)) or 960),
    }

