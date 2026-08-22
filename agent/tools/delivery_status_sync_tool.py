"""Phase 7 第一批：查询签收状态并更新飞书多维表格。"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent.workflow_resource_store import get_workflow_resource
from tools.delivery_status_common import (
    PENDING_STATUS,
    STATUS_FIELD_NAME,
    WAYBILL_FIELD_NAME,
    is_signed_status as _is_signed_status,
    normalize_status as _normalize_status,
    normalize_waybill as _normalize_waybill,
    query_delivery_status as _query_delivery_status_common,
)
from tools.feishu_cli_tool import feishu_operation
from tools.phase7_mysql_store import update_console_waybill_statuses
from tools.phase7_sync_common import normalize_explicit_account_params
from tools.tms_tool import call_http_service


RESOURCE_KEY = "phase7.delivery_status_bitable"
DEFAULT_BASE_TOKEN = "Fcm8b2H7wayK1UsYLjlcFmWhnMh"
DEFAULT_TABLE_ID = "tblX96gGAuBfJrtW"
DEFAULT_VIEW_ID = "veweDmbdIS"
DEFAULT_VIEW_NAME = "未签收明细"
def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]


def _query_delivery_status(codes: list[str], params: dict[str, Any]) -> tuple[dict[str, str] | None, dict[str, Any]]:
    return _query_delivery_status_common(codes, params, service_call=call_http_service)


def _query_value(params: dict[str, Any], key: str) -> Any:
    query = params.get("query")
    if isinstance(query, dict):
        return query.get(key)
    return None


def _config_value(params: dict[str, Any], resource: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = params.get(key)
        if value:
            return value
    for key in keys:
        value = resource.get(key)
        if value:
            return value
    return None


def _extract_view_id(view: dict[str, Any]) -> str:
    return str(view.get("view_id") or view.get("id") or view.get("viewId") or "").strip()


def _extract_view_name(view: dict[str, Any]) -> str:
    return str(view.get("view_name") or view.get("name") or view.get("viewName") or "").strip()


def _resolve_view_id_by_name(base_token: str, table_id: str, view_name: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    result = feishu_operation(
        "list_views",
        {
            "base_token": base_token,
            "table_id": table_id,
            "as": params.get("as", "bot"),
        },
    )
    if "error" in result:
        return "", {"error": result.get("error"), "list_views_result": result}

    items = _extract_items(result)
    for item in items:
        if _extract_view_name(item) == view_name:
            return _extract_view_id(item), {"view_name": view_name, "list_views_result": result}
    return "", {
        "error": f"未找到飞书多维表格视图：{view_name}",
        "available_views": [_extract_view_name(item) for item in items],
        "list_views_result": result,
    }


def _resolve_bitable_config(params: dict[str, Any]) -> dict[str, str]:
    resource: dict[str, Any] = {}
    try:
        resource = get_workflow_resource(RESOURCE_KEY) or {}
    except Exception:
        resource = {}

    base_token = _config_value(params, resource, "base_token", "app_token") or DEFAULT_BASE_TOKEN
    table_id = _config_value(params, resource, "table_id") or DEFAULT_TABLE_ID
    configured_view_id = _config_value(params, resource, "view_id", "viewId")
    configured_view_name = _config_value(params, resource, "view_name", "viewName")
    view_name = str(configured_view_name or DEFAULT_VIEW_NAME).strip()
    view_id = str(configured_view_id or "").strip()
    if view_name and (not view_id or view_id == DEFAULT_VIEW_ID or configured_view_name):
        resolved_view_id, view_meta = _resolve_view_id_by_name(str(base_token), str(table_id), view_name, params)
        if resolved_view_id:
            view_id = resolved_view_id
        elif not view_id:
            raise ValueError(str(view_meta.get("error") or f"无法解析飞书视图：{view_name}"))
    if not view_id:
        view_id = DEFAULT_VIEW_ID

    return {
        "base_token": str(base_token),
        "table_id": str(table_id),
        "view_id": view_id,
        "view_name": view_name,
    }


def _resolve_bitable_target(params: dict[str, Any]) -> tuple[str, str]:
    resource: dict[str, Any] = {}
    try:
        resource = get_workflow_resource(RESOURCE_KEY) or {}
    except Exception:
        resource = {}
    base_token = _config_value(params, resource, "base_token", "app_token") or DEFAULT_BASE_TOKEN
    table_id = _config_value(params, resource, "table_id") or DEFAULT_TABLE_ID
    if not base_token or not table_id:
        raise ValueError(f"未找到 {RESOURCE_KEY}，请先导入到 MySQL")
    return str(base_token), str(table_id)


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


def _list_bitable_records(params: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    try:
        config = _resolve_bitable_config(params)
    except Exception as exc:
        return None, {"error": str(exc)}
    # 飞书多维表格记录列表单页上限通常为 200；请求更大的页长会被截断，
    # 如果仍按原请求值判断是否到底，会把 200/958 这类结果误判成已读完。
    limit = min(int(params.get("list_limit") or 200), 200)
    max_pages = int(params.get("list_max_pages") or 50)
    records: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    offset = 0

    for _page_index in range(max_pages):
        request = {
            "base_token": config["base_token"],
            "table_id": config["table_id"],
            "limit": limit,
            "offset": offset,
            "as": params.get("as", "bot"),
        }
        if config.get("view_id"):
            request["view_id"] = config["view_id"]
        list_result = feishu_operation("list_records", request)
        pages.append(list_result)
        if "error" in list_result:
            return None, {"pages": pages, "error": list_result.get("error")}

        items = _extract_items(list_result)
        new_items = []
        for item in items:
            record_id = str(item.get("record_id") or "").strip()
            if record_id and record_id in seen_record_ids:
                continue
            if record_id:
                seen_record_ids.add(record_id)
            new_items.append(item)
        records.extend(new_items)
        if items and not new_items:
            break
        if len(items) < limit:
            break
        offset += limit

    return records, {
        "pages": pages,
        "scanned": len(records),
        "list_limit": limit,
        "view_id": config.get("view_id", ""),
        "view_name": config.get("view_name", ""),
    }


def _pending_records_from_bitable(items: list[dict[str, Any]]) -> tuple[list[dict[str, str]], int]:
    pending: list[dict[str, str]] = []
    skipped_empty_waybill = 0
    for item in items:
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        waybill_no = _normalize_waybill(fields.get(WAYBILL_FIELD_NAME))
        status = _normalize_status(fields.get(STATUS_FIELD_NAME))
        if not waybill_no:
            skipped_empty_waybill += 1
            continue
        if record_id and status == PENDING_STATUS:
            pending.append({"record_id": record_id, "bill_code": waybill_no})
    return pending, skipped_empty_waybill


def _unique_codes(records: list[dict[str, str]]) -> list[str]:
    seen: set[str] = set()
    codes: list[str] = []
    for record in records:
        code = record["bill_code"]
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _write_status_records(records: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    if not records:
        return {"ok": True, "requested": 0, "written": 0, "results": []}
    base_token, table_id = _resolve_bitable_target(params)
    return feishu_operation(
        "write_records",
        {
            "base_token": base_token,
            "table_id": table_id,
            "records": records,
            "as": params.get("as", "bot"),
        },
    )


def _sync_signed_console_status(status_map: dict[str, str]) -> dict[str, Any]:
    signed_codes = [code for code, status in status_map.items() if _is_signed_status(status)]
    return _sync_signed_console_codes(signed_codes)


def _sync_signed_console_codes(signed_codes: list[str]) -> dict[str, Any]:
    if not signed_codes:
        return {"ok": True, "updated": 0, "status": "signed"}
    try:
        return update_console_waybill_statuses(signed_codes, "signed")
    except Exception as exc:
        return {"ok": False, "updated": 0, "status": "signed", "error": str(exc)}


def _run_explicit_delivery_status_sync(params: dict[str, Any], bill_codes: list[str], record_ids: list[str]) -> dict[str, Any]:
    if len(bill_codes) != len(record_ids):
        return {"error": "bill_codes 与 record_ids 数量不一致"}

    status_map, query_result = _query_delivery_status(bill_codes, params)
    if status_map is None:
        return query_result

    records = []
    unmatched = []
    for bill_code, record_id in zip(bill_codes, record_ids):
        status = status_map.get(bill_code)
        if status is None:
            unmatched.append(bill_code)
            continue
        records.append(
            {
                "record_id": record_id,
                "fields": {
                    STATUS_FIELD_NAME: status,
                },
            }
        )

    if params.get("dry_run"):
        return {
            "ok": True,
            "dry_run": True,
            "requested": len(bill_codes),
            "matched": len(records),
            "scanned": len(bill_codes),
            "pending": len(bill_codes),
            "queried": len(_unique_codes([{"bill_code": code} for code in bill_codes])),
            "updated": len(records),
            "unchanged": 0,
            "unmatched": len(unmatched),
            "unmatched_bill_codes": unmatched,
            "planned_records": records,
            "query_result": query_result,
        }

    write_result = _write_status_records(records, params)
    if "error" in write_result or write_result.get("errors"):
        return {
            "error": "飞书写入失败",
            "requested": len(bill_codes),
            "matched": len(records),
            "scanned": len(bill_codes),
            "pending": len(bill_codes),
            "queried": len(_unique_codes([{"bill_code": code} for code in bill_codes])),
            "updated": len(records),
            "unchanged": 0,
            "unmatched": len(unmatched),
            "unmatched_bill_codes": unmatched,
            "feishu_result": write_result,
        }
    sql_status_result = _sync_signed_console_status(status_map)
    return {
        "ok": True,
        "requested": len(bill_codes),
        "matched": len(records),
        "scanned": len(bill_codes),
        "pending": len(bill_codes),
        "queried": len(_unique_codes([{"bill_code": code} for code in bill_codes])),
        "updated": write_result.get("written", len(records)),
        "unchanged": 0,
        "unmatched": len(unmatched),
        "unmatched_bill_codes": unmatched,
        "feishu_result": write_result,
        "sql_status_result": sql_status_result,
    }


def _run_bitable_scan_delivery_status_sync(params: dict[str, Any]) -> dict[str, Any]:
    items, list_result = _list_bitable_records(params)
    if items is None:
        return {"error": "飞书读取待查询记录失败", "list_result": list_result}

    pending_records, skipped_empty_waybill = _pending_records_from_bitable(items)
    query_codes = _unique_codes(pending_records)
    if not pending_records:
        return {
            "ok": True,
            "dry_run": bool(params.get("dry_run", False)),
            "scanned": len(items),
            "pending": 0,
            "queried": 0,
            "updated": 0,
            "unchanged": 0,
            "unmatched": 0,
            "skipped_empty_waybill": skipped_empty_waybill,
            "list_result": list_result,
        }

    status_map, query_result = _query_delivery_status(query_codes, params)
    if status_map is None:
        return query_result

    update_records: list[dict[str, Any]] = []
    signed_console_codes: list[str] = []
    unchanged = 0
    unmatched_codes: list[str] = []
    for pending in pending_records:
        code = pending["bill_code"]
        status = status_map.get(code)
        if status is None:
            unmatched_codes.append(code)
            continue
        if _is_signed_status(status):
            update_records.append(
                {
                    "record_id": pending["record_id"],
                    "fields": {
                        STATUS_FIELD_NAME: "已签收",
                    },
                }
            )
            signed_console_codes.append(code)
        else:
            unchanged += 1

    if params.get("dry_run"):
        return {
            "ok": True,
            "dry_run": True,
            "scanned": len(items),
            "pending": len(pending_records),
            "queried": len(query_codes),
            "updated": len(update_records),
            "unchanged": unchanged,
            "unmatched": len(unmatched_codes),
            "unmatched_bill_codes": unmatched_codes,
            "skipped_empty_waybill": skipped_empty_waybill,
            "planned_records": update_records,
            "list_result": list_result,
            "query_result": query_result,
        }

    write_result = _write_status_records(update_records, params)
    if "error" in write_result or write_result.get("errors"):
        return {
            "error": "飞书写入失败",
            "scanned": len(items),
            "pending": len(pending_records),
            "queried": len(query_codes),
            "updated": len(update_records),
            "unchanged": unchanged,
            "unmatched": len(unmatched_codes),
            "unmatched_bill_codes": unmatched_codes,
            "write_result": write_result,
        }

    sql_status_result = _sync_signed_console_codes(signed_console_codes)
    return {
        "ok": True,
        "dry_run": False,
        "scanned": len(items),
        "pending": len(pending_records),
        "queried": len(query_codes),
        "updated": write_result.get("written", len(update_records)),
        "unchanged": unchanged,
        "unmatched": len(unmatched_codes),
        "unmatched_bill_codes": unmatched_codes,
        "skipped_empty_waybill": skipped_empty_waybill,
        "list_result": list_result,
        "query_result": query_result,
        "write_result": write_result,
        "sql_status_result": sql_status_result,
    }


def run_delivery_status_sync(params: dict[str, Any]) -> dict[str, Any]:
    params = normalize_explicit_account_params(params or {}, label="签收状态同步")
    bill_codes = _split_csv(
        params.get("bill_codes")
        or params.get("BILL_CODE")
        or _query_value(params, "BILL_CODE")
        or _query_value(params, "bill_codes")
    )
    record_ids = _split_csv(
        params.get("record_ids")
        or params.get("RECORD_ID")
        or _query_value(params, "RECORD_ID")
        or _query_value(params, "record_ids")
    )
    if bill_codes or record_ids:
        if not bill_codes or not record_ids:
            return {"error": "缺少 BILL_CODE/bill_codes 或 RECORD_ID/record_ids"}
        return _run_explicit_delivery_status_sync(params, bill_codes, record_ids)

    return _run_bitable_scan_delivery_status_sync(params)


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_delivery_status_sync(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
