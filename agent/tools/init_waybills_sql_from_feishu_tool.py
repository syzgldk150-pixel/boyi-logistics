"""Initialize console waybill SQL rows from existing Feishu Bitable records."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from typing import Any
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import delete_receipt_like_console_waybills, sync_console_waybills
from tools import send_order_sync_tool, yunda_send_waybills_sync_tool


DEFAULT_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_LIST_LIMIT = 200
MAX_FEISHU_LIST_LIMIT = 200
DEFAULT_MAX_PAGES = 200


def _emit_progress(message: str, **extra: Any) -> None:
    payload = " | ".join(f"{key}={value}" for key, value in extra.items() if value not in (None, ""))
    text = f"[progress] {message}"
    if payload:
        text = f"{text} | {payload}"
    print(text, file=sys.stderr, flush=True)


def _normalize_providers(value: Any) -> list[str]:
    if value in (None, "", []):
        return ["ronghui", "yunda"]
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.replace("，", ",").split(",")]
    elif isinstance(value, list):
        raw_items = [str(item).strip() for item in value]
    else:
        raw_items = [str(value).strip()]
    providers: list[str] = []
    aliases = {
        "rh": "ronghui",
        "融辉": "ronghui",
        "融辉寄件": "ronghui",
        "yd": "yunda",
        "韵达": "yunda",
        "韵达寄件": "yunda",
    }
    for item in raw_items:
        if not item:
            continue
        provider = aliases.get(item, item).lower()
        if provider not in {"ronghui", "yunda"}:
            raise ValueError(f"不支持的 provider: {item}")
        if provider not in providers:
            providers.append(provider)
    return providers or ["ronghui", "yunda"]


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidates = (
        payload.get("items"),
        payload.get("records"),
        payload.get("data", {}).get("items") if isinstance(payload.get("data"), dict) else None,
    )
    for value in candidates:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _list_all_records(base_token: str, table_id: str, params: dict[str, Any], provider: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limit = min(int(params.get("list_limit") or DEFAULT_LIST_LIMIT), MAX_FEISHU_LIST_LIMIT)
    max_pages = int(params.get("list_max_pages") or DEFAULT_MAX_PAGES)
    items: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    offset = 0
    for page_index in range(max_pages):
        result = feishu_operation(
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
        page_items = _extract_items(result)
        pages.append(
            {
                "page_index": page_index + 1,
                "offset": offset,
                "ok": bool(result.get("ok")) or "error" not in result,
                "count": len(page_items),
                "error": result.get("error"),
            }
        )
        if "error" in result:
            raise RuntimeError(f"{provider} 飞书记录读取失败: {result.get('error')}")
        items.extend(page_items)
        _emit_progress("飞书记录分页读取", provider=provider, page=page_index + 1, rows=len(page_items), total=len(items))
        if len(page_items) < limit:
            break
        offset += limit
    return items, {"pages": pages, "fetched": len(items)}


def _fields_from_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        fields = item.get("fields")
        if isinstance(fields, dict):
            rows.append(fields)
    return rows


def _resolve_ronghui_target(params: dict[str, Any]) -> tuple[str, str]:
    base_token = params.get("ronghui_base_token") or params.get("base_token")
    table_id = params.get("ronghui_table_id") or params.get("table_id")
    if base_token and table_id:
        return str(base_token), str(table_id)
    resource = get_workflow_resource("phase7.send_order_bitable")
    if not resource or not resource.get("base_token") or not resource.get("table_id"):
        raise ValueError("未找到 phase7.send_order_bitable")
    return str(resource["base_token"]), str(resource["table_id"])


def _resolve_yunda_target(params: dict[str, Any]) -> tuple[str, str]:
    base_token = params.get("yunda_base_token")
    table_id = params.get("yunda_table_id")
    if base_token and table_id:
        return str(base_token), str(table_id)
    resource = get_workflow_resource(yunda_send_waybills_sync_tool.RESOURCE_KEY)
    if resource and resource.get("base_token") and resource.get("table_id"):
        return str(resource["base_token"]), str(resource["table_id"])
    return yunda_send_waybills_sync_tool.DEFAULT_BASE_TOKEN, yunda_send_waybills_sync_tool.DEFAULT_TABLE_ID


def _fallback_date() -> dt.date:
    return dt.datetime.now(DEFAULT_TZ).date()


def _prepare_yunda_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        normalized = dict(row)
        waybill_no = ""
        for field_name in yunda_send_waybills_sync_tool.INDEX_FIELD_ALIASES:
            waybill_no = yunda_send_waybills_sync_tool._normalize_waybill(row.get(field_name))
            if waybill_no:
                break
        if waybill_no:
            normalized[yunda_send_waybills_sync_tool.INDEX_FIELD_NAME] = waybill_no
            prepared.append(normalized)
    return prepared


def _filter_ronghui_receipt_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        waybill_no = send_order_sync_tool._normalize_waybill(row.get(send_order_sync_tool.INDEX_FIELD_NAME))
        if send_order_sync_tool.is_receipt_like_tracking(waybill_no):
            skipped += 1
            continue
        kept.append(row)
    return kept, skipped


def _normalize_provider_rows(provider: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    if provider == "ronghui":
        kept_rows, skipped_receipt_like = _filter_ronghui_receipt_rows(rows)
        return send_order_sync_tool._console_waybill_records(kept_rows, target_date=_fallback_date()), skipped_receipt_like
    prepared = _prepare_yunda_rows(rows)
    return yunda_send_waybills_sync_tool._console_waybill_records(prepared, target_date=_fallback_date()), 0


def _run_provider(provider: str, params: dict[str, Any]) -> dict[str, Any]:
    if provider == "ronghui":
        base_token, table_id = _resolve_ronghui_target(params)
        source = "ronghui"
    else:
        base_token, table_id = _resolve_yunda_target(params)
        source = "yunda"

    _emit_progress("开始从飞书初始化 SQL", provider=provider, table_id=table_id)
    items, list_result = _list_all_records(base_token, table_id, params, provider)
    rows = _fields_from_items(items)
    console_records, skipped_receipt_like = _normalize_provider_rows(provider, rows)
    unique_waybills = {str(row.get("waybill_no") or "").strip() for row in console_records if row.get("waybill_no")}

    if params.get("dry_run"):
        return {
            "ok": True,
            "provider": provider,
            "source": source,
            "dry_run": True,
            "feishu_records": len(items),
            "normalized": len(console_records),
            "unique_waybills": len(unique_waybills),
            "skipped_receipt_like": skipped_receipt_like,
            "sql_upserted": 0,
            "sql_updates": 0,
            "sql_creates": 0,
            "sql_deleted_stale": 0,
            "list_result": list_result,
        }

    sql_result = sync_console_waybills(console_records, source=source, replace_date=False)
    cleanup_result = {"ok": True, "deleted": 0, "skipped": True}
    if provider == "ronghui" and params.get("cleanup_receipt_like") is not False:
        cleanup_result = delete_receipt_like_console_waybills(source=source)
    _emit_progress("飞书初始化 SQL 完成", provider=provider, sql_upserted=sql_result.get("upserted", 0))
    return {
        "ok": True,
        "provider": provider,
        "source": source,
        "feishu_records": len(items),
        "normalized": len(console_records),
        "unique_waybills": len(unique_waybills),
        "skipped_receipt_like": skipped_receipt_like,
        "sql_upserted": sql_result.get("upserted", 0),
        "sql_updates": sql_result.get("updates", 0),
        "sql_creates": sql_result.get("creates", 0),
        "sql_deleted_stale": sql_result.get("deleted_stale", 0),
        "list_result": list_result,
        "sql_result": sql_result,
        "cleanup_result": cleanup_result,
    }


def run_init_waybills_sql_from_feishu(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    try:
        providers = _normalize_providers(params.get("providers"))
    except ValueError as exc:
        return {"error": str(exc)}
    results: list[dict[str, Any]] = []
    for provider in providers:
        try:
            result = _run_provider(provider, params)
        except Exception as exc:
            return {
                "error": f"{provider} 飞书初始化 SQL 失败",
                "provider": provider,
                "raw_error": str(exc),
                "results": results,
            }
        results.append(result)
    return {
        "ok": True,
        "providers": providers,
        "feishu_records": sum(int(result.get("feishu_records") or 0) for result in results),
        "normalized": sum(int(result.get("normalized") or 0) for result in results),
        "unique_waybills": sum(int(result.get("unique_waybills") or 0) for result in results),
        "skipped_receipt_like": sum(int(result.get("skipped_receipt_like") or 0) for result in results),
        "sql_upserted": sum(int(result.get("sql_upserted") or 0) for result in results),
        "sql_updates": sum(int(result.get("sql_updates") or 0) for result in results),
        "sql_creates": sum(int(result.get("sql_creates") or 0) for result in results),
        "sql_deleted_stale": sum(int(result.get("sql_deleted_stale") or 0) for result in results),
        "sql_deleted_receipt_like": sum(int(result.get("cleanup_result", {}).get("deleted") or 0) for result in results),
        "dry_run": bool(params.get("dry_run", False)),
        "results": results,
    }


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_init_waybills_sql_from_feishu(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
