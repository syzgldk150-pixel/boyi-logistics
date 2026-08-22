"""飞书协作工具：优先调用 lark-cli，缺失能力时直接请求飞书 OpenAPI。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlencode
from typing import Callable

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LARK_CLI = "lark-cli"
_TOKEN_CACHE: dict[str, float | str | None] = {"token": None, "expires_at": 0.0}
_SHEET_REF_CACHE: dict[str, dict[str, str]] = {}
_SHEET_INFO_CACHE: dict[str, dict[str, dict]] = {}
_SHEET_TITLE_COUNTS_CACHE: dict[str, dict[str, int]] = {}

_A1_RANGE_RE = re.compile(
    r"^(?:(?P<sheet>[^!]+)!)?(?P<start_col>[A-Z]+)(?P<start_row>\d+):(?P<end_col>[A-Z]+)(?P<end_row>\d+)$"
)
_MAX_CELLS_PER_WRITE = 4000


def _clear_spreadsheet_sheet_cache(spreadsheet_token: str) -> None:
    _SHEET_REF_CACHE.pop(spreadsheet_token, None)
    _SHEET_INFO_CACHE.pop(spreadsheet_token, None)
    _SHEET_TITLE_COUNTS_CACHE.pop(spreadsheet_token, None)


def run_lark_cli(args: list[str], timeout: int = 20) -> dict:
    """执行 lark-cli 命令。"""
    cmd = [LARK_CLI] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"error": "lark-cli 未安装，请运行 npm install -g @larksuite/cli"}
    except subprocess.TimeoutExpired:
        return {"error": f"lark-cli 执行超时({timeout}s)"}

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        return {"error": f"lark-cli 失败: {stderr[:500]}"}

    stdout = result.stdout.strip()
    if not stdout:
        return {"ok": True}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"output": stdout[:4000]}


def _append_identity(args: list[str], params: dict, default: str | None = None) -> list[str]:
    identity = params.get("as") or default
    if identity:
        args.extend(["--as", str(identity)])
    return args


def _append_common_flags(args: list[str], params: dict) -> list[str]:
    if params.get("dry_run"):
        args.append("--dry-run")
    return args


def _normalize_receive_target(params: dict) -> tuple[str | None, str | None]:
    chat_id = params.get("chat_id")
    user_id = params.get("user_id")
    receive_id = params.get("receive_id")
    if chat_id:
        return "chat_id", str(chat_id)
    if user_id:
        return "user_id", str(user_id)
    if not receive_id:
        return None, None
    receive_id = str(receive_id)
    if receive_id.startswith("oc_"):
        return "chat_id", receive_id
    return "user_id", receive_id


def _upsert_record(base_token: str, table_id: str, record: dict, params: dict) -> dict:
    record_payload = record.get("fields") if isinstance(record, dict) and "fields" in record else record
    record_id = params.get("record_id")
    if not record_id and isinstance(record, dict):
        record_id = record.get("record_id")

    args = [
        "base",
        "+record-upsert",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--json",
        json.dumps(record_payload, ensure_ascii=False),
    ]
    if record_id:
        args.extend(["--record-id", str(record_id)])
    _append_identity(args, params)
    _append_common_flags(args, params)
    return run_lark_cli(args, timeout=30)


def _delete_record(base_token: str, table_id: str, record_id: str, params: dict) -> dict:
    args = [
        "base",
        "+record-delete",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--record-id",
        record_id,
        "--yes",
    ]
    _append_identity(args, params)
    _append_common_flags(args, params)
    return run_lark_cli(args, timeout=30)


def _normalize_bitable_record_list(result: dict) -> dict:
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    record_ids = data.get("record_id_list")
    rows = data.get("data")
    fields = data.get("fields")
    if not isinstance(record_ids, list):
        return result

    items: list[dict] = []
    if isinstance(rows, list) and isinstance(fields, list):
        for index, record_id in enumerate(record_ids):
            row = rows[index] if index < len(rows) and isinstance(rows[index], list) else []
            row_fields = {
                str(field): row[field_index] if field_index < len(row) else None
                for field_index, field in enumerate(fields)
            }
            items.append({"record_id": str(record_id), "fields": row_fields, "data": row})
    else:
        items = [{"record_id": str(record_id)} for record_id in record_ids]

    result["items"] = items
    data["items"] = items
    return result


def _list_bitable_fields(base_token: str, table_id: str) -> dict:
    page_token = ""
    items: list[dict] = []
    while True:
        query = "page_size=100"
        if page_token:
            query += f"&page_token={page_token}"
        result = _call_open_api(
            "GET",
            f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/fields?{query}",
            timeout=30,
        )
        if "error" in result:
            return result
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        page_items = data.get("items") if isinstance(data, dict) else None
        if isinstance(page_items, list):
            items.extend([item for item in page_items if isinstance(item, dict)])
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "").strip()
        if not page_token:
            break
    return {"ok": True, "data": {"items": items}, "items": items}


def _list_bitable_views(base_token: str, table_id: str) -> dict:
    page_token = ""
    items: list[dict] = []
    while True:
        query = {"page_size": 100}
        if page_token:
            query["page_token"] = page_token
        result = _call_open_api(
            "GET",
            f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/views?{urlencode(query)}",
            timeout=30,
        )
        if "error" in result:
            return result
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        page_items = data.get("items") if isinstance(data, dict) else None
        if isinstance(page_items, list):
            items.extend([item for item in page_items if isinstance(item, dict)])
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "").strip()
        if not page_token:
            break
    return {"ok": True, "data": {"items": items}, "items": items}


def _search_bitable_records(base_token: str, table_id: str, params: dict) -> dict:
    try:
        page_size = int(params.get("page_size") or params.get("limit") or 20)
    except (TypeError, ValueError):
        return {"error": "search_records page_size/limit 必须是整数"}
    page_size = max(1, min(page_size, 500))
    query = {"page_size": page_size}
    if params.get("page_token"):
        query["page_token"] = str(params["page_token"])
    payload: dict[str, object] = {}
    for key in ("view_id", "filter", "sort", "automatic_fields"):
        if params.get(key) is not None:
            payload[key] = params[key]
    field_names = params.get("field_names")
    if field_names is not None:
        if isinstance(field_names, str):
            field_names = [part.strip() for part in field_names.split(",") if part.strip()]
        if not isinstance(field_names, list):
            return {"error": "search_records field_names 必须是数组或逗号分隔字符串"}
        payload["field_names"] = [str(item) for item in field_names if str(item).strip()]
    api = {
        "method": "POST",
        "url": f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records/search?{urlencode(query)}",
        "body": payload,
    }
    if params.get("dry_run"):
        return {"ok": True, "api": api, "items": [], "data": {"items": []}}
    result = _call_open_api(api["method"], api["url"], payload=payload, timeout=30)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    items = data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), list) else []
    result["items"] = items
    return result


def search_bitable_records(base_token: str, table_id: str, params: dict) -> dict:
    """Public read-only primitive for narrow governed Bitable adapters."""

    return _search_bitable_records(base_token, table_id, params)


def _create_bitable_field(base_token: str, table_id: str, params: dict) -> dict:
    field_name = str(params.get("field_name") or params.get("name") or "").strip()
    field_type = params.get("type", params.get("field_type", 1))
    if not field_name:
        return {"error": "create_field 缺少 field_name/name"}
    try:
        field_type = int(field_type)
    except (TypeError, ValueError):
        return {"error": "create_field type/field_type 必须是整数"}
    payload: dict[str, object] = {
        "field_name": field_name,
        "type": field_type,
    }
    if isinstance(params.get("property"), dict):
        payload["property"] = params["property"]
    if params.get("dry_run"):
        return {
            "ok": True,
            "api": {
                "method": "POST",
                "url": f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
                "body": payload,
            },
        }
    return _call_open_api(
        "POST",
        f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
        payload=payload,
        timeout=30,
    )


def _update_bitable_field(base_token: str, table_id: str, params: dict) -> dict:
    field_id = str(params.get("field_id") or "").strip()
    field_name = str(params.get("field_name") or params.get("name") or "").strip()
    field_type = params.get("type", params.get("field_type"))
    if not field_id or not field_name:
        return {"error": "update_field 缺少 field_id 或 field_name/name"}
    try:
        field_type = int(field_type)
    except (TypeError, ValueError):
        return {"error": "update_field type/field_type 必须是整数"}
    payload: dict[str, object] = {"field_name": field_name, "type": field_type}
    if isinstance(params.get("property"), dict):
        payload["property"] = params["property"]
    path = f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/fields/{field_id}"
    if params.get("dry_run"):
        return {"ok": True, "api": {"method": "PUT", "url": path, "body": payload}}
    return _call_open_api("PUT", path, payload=payload, timeout=30)


def _tenant_access_token() -> str:
    cached = _TOKEN_CACHE.get("token")
    expires_at = float(_TOKEN_CACHE.get("expires_at") or 0.0)
    if cached and time.time() < expires_at - 60:
        return str(cached)

    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise ValueError("FEISHU_APP_ID 或 FEISHU_APP_SECRET 未配置")

    response = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise ValueError(f"飞书鉴权失败: {payload.get('msg') or payload}")
    token = payload.get("tenant_access_token")
    if not token:
        raise ValueError("飞书鉴权返回缺少 tenant_access_token")
    expire = int(payload.get("expire", 7200))
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = time.time() + expire
    return str(token)


def _call_open_api(method: str, path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    headers = {
        "Authorization": f"Bearer {_tenant_access_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }
    response = httpx.request(
        method=method,
        url=f"https://open.feishu.cn{path}",
        json=payload,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") not in (0, None):
        return {"error": f"飞书 OpenAPI 失败: {data.get('msg') or data}", "response": data}
    return data


def _spreadsheet_sheet_ref_map(
    spreadsheet_token: str,
    *,
    require_fresh_metadata: bool = False,
) -> dict[str, str]:
    cached = _SHEET_REF_CACHE.get(spreadsheet_token)
    if cached is not None:
        return cached

    result = _call_open_api(
        "GET",
        f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        timeout=30,
    )
    if "error" in result:
        if require_fresh_metadata:
            raise RuntimeError(str(result["error"]))
        return {}

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    sheets = data.get("sheets") if isinstance(data, dict) else None
    if not isinstance(sheets, list):
        if require_fresh_metadata:
            raise RuntimeError("sheet metadata response is invalid")
        return {}

    refs: dict[str, str] = {}
    sheet_infos: dict[str, dict] = {}
    title_counts: dict[str, int] = {}
    sheet_ids: list[str] = []
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        sheet_id = str(sheet.get("sheet_id") or "").strip()
        title = str(sheet.get("title") or "").strip()
        if not sheet_id:
            continue
        sheet_ids.append(sheet_id)
        refs[sheet_id] = sheet_id
        if title:
            title_counts[title] = title_counts.get(title, 0) + 1
            refs.setdefault(title, sheet_id)

        grid_properties = sheet.get("grid_properties")
        if not isinstance(grid_properties, dict):
            grid_properties = sheet.get("gridProperties")
        row_count = None
        if isinstance(grid_properties, dict):
            try:
                row_count = int(
                    grid_properties.get("row_count")
                    or grid_properties.get("rowCount")
                    or 0
                )
            except (TypeError, ValueError):
                row_count = None
        if row_count is None:
            try:
                row_count = int(sheet.get("row_count") or sheet.get("rowCount") or 0)
            except (TypeError, ValueError):
                row_count = None
        sheet_info = {
            "sheet_id": sheet_id,
            "title": title,
            "row_count": row_count,
        }
        sheet_infos[sheet_id] = sheet_info
        if title:
            sheet_infos.setdefault(title, sheet_info)

    if len(sheet_ids) == 1:
        only_sheet_id = sheet_ids[0]
        refs.setdefault("__only_sheet_id__", only_sheet_id)
        if only_sheet_id in sheet_infos:
            sheet_infos.setdefault("__only_sheet_info__", sheet_infos[only_sheet_id])

    _SHEET_REF_CACHE[spreadsheet_token] = refs
    _SHEET_INFO_CACHE[spreadsheet_token] = sheet_infos
    _SHEET_TITLE_COUNTS_CACHE[spreadsheet_token] = title_counts
    return refs


def _spreadsheet_sheet_title_count(spreadsheet_token: str, title: str) -> int:
    lookup = str(title or "").strip()
    if not spreadsheet_token or not lookup:
        return 0
    _spreadsheet_sheet_ref_map(spreadsheet_token)
    return int(_SHEET_TITLE_COUNTS_CACHE.get(spreadsheet_token, {}).get(lookup, 0))


def _spreadsheet_sheet_info(
    spreadsheet_token: str,
    sheet_ref: str,
    *,
    require_fresh_metadata: bool = False,
) -> dict | None:
    lookup_ref = str(sheet_ref or "").strip()
    if len(lookup_ref) >= 2 and lookup_ref.startswith("'") and lookup_ref.endswith("'"):
        lookup_ref = lookup_ref[1:-1]
    if not lookup_ref:
        return None

    _spreadsheet_sheet_ref_map(
        spreadsheet_token,
        require_fresh_metadata=require_fresh_metadata,
    )
    info_map = _SHEET_INFO_CACHE.get(spreadsheet_token, {})
    return info_map.get(lookup_ref) or info_map.get("__only_sheet_info__")


def _resolve_sheet_ref_in_range(
    spreadsheet_token: str,
    value_range: str,
    *,
    require_fresh_metadata: bool = False,
) -> str:
    match = _A1_RANGE_RE.match(value_range.strip())
    if not spreadsheet_token or not match:
        return value_range

    sheet_ref = str(match.group("sheet") or "").strip()
    if not sheet_ref:
        return value_range

    lookup_ref = sheet_ref
    if len(lookup_ref) >= 2 and lookup_ref.startswith("'") and lookup_ref.endswith("'"):
        lookup_ref = lookup_ref[1:-1]

    try:
        ref_map = _spreadsheet_sheet_ref_map(
            spreadsheet_token,
            require_fresh_metadata=require_fresh_metadata,
        )
        resolved_sheet_id = ref_map.get(lookup_ref) or ref_map.get("__only_sheet_id__")
    except Exception:
        if require_fresh_metadata:
            raise
        return value_range
    if not resolved_sheet_id or resolved_sheet_id == sheet_ref:
        return value_range

    range_body = (
        f"{match.group('start_col')}{match.group('start_row')}:"
        f"{match.group('end_col')}{match.group('end_row')}"
    )
    return f"{resolved_sheet_id}!{range_body}"


def _ensure_sheet_rows_for_range(
    spreadsheet_token: str,
    value_range: str,
    *,
    require_fresh_metadata: bool = False,
) -> dict:
    match = _A1_RANGE_RE.match(value_range.strip())
    if not spreadsheet_token or not match or not match.group("sheet"):
        return {"ok": True, "skipped": True, "reason": "range without sheet ref", "range": value_range}

    end_row = int(match.group("end_row"))
    sheet_id = str(match.group("sheet")).strip()
    sheet_info = _spreadsheet_sheet_info(
        spreadsheet_token,
        sheet_id,
        require_fresh_metadata=require_fresh_metadata,
    )
    row_count = sheet_info.get("row_count") if isinstance(sheet_info, dict) else None
    if not isinstance(row_count, int) or row_count <= 0:
        return {"ok": True, "skipped": True, "reason": "unknown sheet row count", "range": value_range}
    if row_count >= end_row:
        return {"ok": True, "range": value_range, "sheet_row_count": row_count, "added": 0}

    return {
        "ok": True,
        "range": value_range,
        "sheet_row_count": row_count,
        "added": end_row - row_count,
        "add_payload": {
            "dimension": {
                "sheetId": sheet_id,
                "majorDimension": "ROWS",
                "length": end_row - row_count,
            }
        },
    }


def _row_dimension_requests_from_range(
    spreadsheet_token: str,
    value_range: str,
    *,
    resolve_sheet_ref: bool = True,
    require_fresh_metadata: bool = False,
) -> dict | None:
    resolved_range = (
        _resolve_sheet_ref_in_range(
            spreadsheet_token,
            value_range,
            require_fresh_metadata=require_fresh_metadata,
        )
        if resolve_sheet_ref
        else value_range
    )
    match = _A1_RANGE_RE.match(resolved_range.strip())
    if not match or not match.group("sheet"):
        return None

    if match.group("start_col") != "A":
        return None

    start_row = int(match.group("start_row"))
    end_row = int(match.group("end_row"))
    if start_row < 1 or end_row < start_row:
        return None

    sheet_id = str(match.group("sheet")).strip()
    sheet_info = (
        _spreadsheet_sheet_info(
            spreadsheet_token,
            sheet_id,
            require_fresh_metadata=require_fresh_metadata,
        )
        if resolve_sheet_ref
        else None
    )
    row_count = sheet_info.get("row_count") if isinstance(sheet_info, dict) else None
    if isinstance(row_count, int) and row_count > 0:
        end_row = min(end_row, row_count)
        if start_row > end_row:
            return {
                "range": resolved_range,
                "skipped": True,
                "reason": "clear range starts after current sheet row count",
            }
        resolved_range = f"{sheet_id}!{match.group('start_col')}{start_row}:{match.group('end_col')}{end_row}"

    row_dimension = {
        "sheetId": sheet_id,
        "majorDimension": "ROWS",
    }
    delete_payload = {
        "dimension": {
            **row_dimension,
            "startIndex": start_row,
            "endIndex": end_row,
        }
    }
    return {
        "range": resolved_range,
        "requested_range": value_range,
        "sheet_row_count": row_count,
        "delete_payload": delete_payload,
    }


def _ensure_values_list(values):
    if isinstance(values, str):
        try:
            return json.loads(values)
        except json.JSONDecodeError:
            return None
    return values


def _qualify_range(value_range: str, sheet_id) -> str:
    if not sheet_id or "!" in value_range:
        return value_range
    return f"{sheet_id}!{value_range}"


def _split_values_by_rows(values: list, max_cells: int = _MAX_CELLS_PER_WRITE) -> list[list]:
    if not values:
        return [values]
    cols = max((len(row) for row in values if isinstance(row, list)), default=1) or 1
    rows_per_chunk = max(1, max_cells // cols)
    if len(values) <= rows_per_chunk:
        return [values]
    return [values[i : i + rows_per_chunk] for i in range(0, len(values), rows_per_chunk)]


def _split_values_for_write(
    value_range: str, values: list, max_cells: int = _MAX_CELLS_PER_WRITE
) -> list[tuple[str, list]]:
    """write 场景：切 values 同时按行平移 range；range 不可解析则回退为单块。"""
    blocks = _split_values_by_rows(values, max_cells)
    if len(blocks) <= 1:
        return [(value_range, values)]
    match = _A1_RANGE_RE.match(value_range.strip())
    if not match:
        return [(value_range, values)]
    sheet = match.group("sheet")
    start_col = match.group("start_col")
    end_col = match.group("end_col")
    cursor = int(match.group("start_row"))
    sheet_prefix = f"{sheet}!" if sheet else ""

    out: list[tuple[str, list]] = []
    for block in blocks:
        block_end = cursor + len(block) - 1
        out.append((f"{sheet_prefix}{start_col}{cursor}:{end_col}{block_end}", block))
        cursor = block_end + 1
    return out


def feishu_operation(
    action: str,
    params: dict,
    *,
    mark_write_started: Callable[[], None] | None = None,
) -> dict:
    """执行飞书操作。"""
    if action == "send_message":
        target_type, target_value = _normalize_receive_target(params)
        content = params.get("content", params.get("text", ""))
        markdown = params.get("markdown")
        if not target_type or not target_value:
            return {"error": "send_message 缺少 chat_id / user_id / receive_id"}
        if not content and not markdown:
            return {"error": "send_message 缺少 content/text 或 markdown"}

        args = ["im", "+messages-send"]
        if target_type == "chat_id":
            args.extend(["--chat-id", target_value])
        else:
            args.extend(["--user-id", target_value])
        if markdown:
            args.extend(["--markdown", str(markdown)])
        else:
            args.extend(["--text", str(content)])
        if params.get("idempotency_key"):
            args.extend(["--idempotency-key", str(params["idempotency_key"])])
        _append_identity(args, params, default="bot")
        _append_common_flags(args, params)
        return run_lark_cli(args)

    if action == "create_sheet":
        title = str(params.get("title", "未命名表格"))
        args = ["base", "+base-create", "--name", title]
        if params.get("time_zone"):
            args.extend(["--time-zone", str(params["time_zone"])])
        if params.get("folder_token"):
            args.extend(["--folder-token", str(params["folder_token"])])
        _append_identity(args, params)
        _append_common_flags(args, params)
        return run_lark_cli(args)

    if action == "write_records":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        table_id = str(params.get("table_id") or "")
        records = params.get("records", [])
        if not base_token or not table_id:
            return {"error": "write_records 缺少 base_token/app_token 或 table_id"}
        if not isinstance(records, list) or not records:
            return {"error": "write_records 需要非空 records 列表"}

        results = []
        errors = []
        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                errors.append({"index": index, "error": "record 必须是对象"})
                continue
            result = _upsert_record(base_token, table_id, record, params)
            results.append({"index": index, "result": result})
            if "error" in result:
                errors.append({"index": index, "error": result["error"]})

        response = {
            "ok": not errors,
            "requested": len(records),
            "written": len(records) - len(errors),
            "results": results,
        }
        if errors:
            response["errors"] = errors
        return response

    if action == "list_fields":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        table_id = str(params.get("table_id") or "")
        if not base_token or not table_id:
            return {"error": "list_fields 缺少 base_token/app_token 或 table_id"}
        if params.get("dry_run"):
            return {"ok": True, "items": [], "data": {"items": []}}
        try:
            return _list_bitable_fields(base_token, table_id)
        except Exception as exc:
            return {"error": f"list_fields 调用失败: {str(exc)[:300]}"}

    if action == "list_views":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        table_id = str(params.get("table_id") or "")
        if not base_token or not table_id:
            return {"error": "list_views 缺少 base_token/app_token 或 table_id"}
        if params.get("dry_run"):
            return {"ok": True, "items": [], "data": {"items": []}}
        try:
            return _list_bitable_views(base_token, table_id)
        except Exception as exc:
            return {"error": f"list_views 调用失败: {str(exc)[:300]}"}

    if action == "create_field":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        table_id = str(params.get("table_id") or "")
        if not base_token or not table_id:
            return {"error": "create_field 缺少 base_token/app_token 或 table_id"}
        try:
            return _create_bitable_field(base_token, table_id, params)
        except Exception as exc:
            return {"error": f"create_field 调用失败: {str(exc)[:300]}"}

    if action == "update_field":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        table_id = str(params.get("table_id") or "")
        if not base_token or not table_id:
            return {"error": "update_field 缺少 base_token/app_token 或 table_id"}
        try:
            return _update_bitable_field(base_token, table_id, params)
        except Exception as exc:
            return {"error": f"update_field 调用失败: {str(exc)[:300]}"}

    if action == "list_records":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        table_id = str(params.get("table_id") or "")
        if not base_token or not table_id:
            return {"error": "list_records 缺少 base_token/app_token 或 table_id"}

        args = [
            "base",
            "+record-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
        ]
        if params.get("view_id"):
            args.extend(["--view-id", str(params["view_id"])])
        if params.get("limit") is not None:
            args.extend(["--limit", str(params["limit"])])
        if params.get("offset") is not None:
            args.extend(["--offset", str(params["offset"])])
        _append_identity(args, params)
        _append_common_flags(args, params)
        return _normalize_bitable_record_list(run_lark_cli(args, timeout=30))

    if action == "search_records":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        table_id = str(params.get("table_id") or "")
        if not base_token or not table_id:
            return {"error": "search_records 缺少 base_token/app_token 或 table_id"}
        if not isinstance(params.get("filter"), dict):
            return {"error": "search_records 缺少 filter 精确筛选条件"}
        try:
            return _search_bitable_records(base_token, table_id, params)
        except Exception as exc:
            return {"error": f"search_records 调用失败: {str(exc)[:300]}"}

    if action == "query_records":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        dsl = params.get("dsl")
        if not base_token or dsl is None:
            return {"error": "query_records 缺少 base_token/app_token 或 dsl"}

        dsl_json = dsl if isinstance(dsl, str) else json.dumps(dsl, ensure_ascii=False)
        args = [
            "base",
            "+data-query",
            "--base-token",
            base_token,
            "--dsl",
            dsl_json,
        ]
        _append_identity(args, params)
        _append_common_flags(args, params)
        return run_lark_cli(args, timeout=30)

    if action == "delete_records":
        base_token = str(params.get("base_token") or params.get("app_token") or "")
        table_id = str(params.get("table_id") or "")
        record_ids = params.get("record_ids", [])
        if not base_token or not table_id:
            return {"error": "delete_records 缺少 base_token/app_token 或 table_id"}
        if not isinstance(record_ids, list) or not record_ids:
            return {"error": "delete_records 需要非空 record_ids 列表"}

        results = []
        errors = []
        for index, record_id in enumerate(record_ids, start=1):
            result = _delete_record(base_token, table_id, str(record_id), params)
            results.append({"index": index, "record_id": record_id, "result": result})
            if "error" in result:
                errors.append({"index": index, "record_id": record_id, "error": result["error"]})

        response = {
            "ok": not errors,
            "requested": len(record_ids),
            "deleted": len(record_ids) - len(errors),
            "results": results,
        }
        if errors:
            response["errors"] = errors
        return response

    if action == "write_sheet":
        spreadsheet_token = str(params.get("spreadsheet_token") or "")
        value_range = str(params.get("range") or "")
        values = params.get("values")
        if not spreadsheet_token or not value_range or values is None:
            return {"error": "write_sheet 缺少 spreadsheet_token、range 或 values"}

        parsed_values = _ensure_values_list(values)
        if parsed_values is None:
            return {"error": "write_sheet values 不是合法 JSON"}
        if not isinstance(parsed_values, list):
            return {"error": "write_sheet values 必须是二维数组"}

        qualified_range = _qualify_range(value_range, params.get("sheet_id"))
        try:
            if not params.get("dry_run"):
                qualified_range = _resolve_sheet_ref_in_range(
                    spreadsheet_token,
                    qualified_range,
                    require_fresh_metadata=True,
                )
            chunks = _split_values_for_write(qualified_range, parsed_values)
        except Exception as exc:
            return {"error": f"write_sheet metadata resolution failed: {str(exc)[:300]}"}
        url = f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values"
        dimension_url = f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/dimension_range"

        write_started = False

        def mark_mutation_started() -> None:
            nonlocal write_started
            if not write_started and mark_write_started is not None:
                mark_write_started()
            write_started = True

        if params.get("dry_run"):
            return {
                "ok": True,
                "api": [
                    {
                        "method": "PUT",
                        "url": url,
                        "body": {"valueRange": {"range": cr, "values": cv}},
                    }
                    for cr, cv in chunks
                ],
            }

        try:
            ensure_rows_result = _ensure_sheet_rows_for_range(
                spreadsheet_token,
                qualified_range,
                require_fresh_metadata=True,
            )
        except Exception as exc:
            return {"error": f"write_sheet row validation failed: {str(exc)[:300]}"}
        if "error" in ensure_rows_result:
            return {"error": ensure_rows_result["error"], "ensure_rows_result": ensure_rows_result}
        add_rows_result: dict | None = None
        if ensure_rows_result.get("add_payload"):
            try:
                mark_mutation_started()
                add_rows_result = _call_open_api(
                    "POST",
                    dimension_url,
                    payload=ensure_rows_result["add_payload"],
                    timeout=60,
                )
            except Exception as exc:
                return {
                    "error": f"write_sheet è¡¥è¡Œè°ƒç”¨å¤±è´¥: {str(exc)[:300]}",
                    "ensure_rows_result": ensure_rows_result,
                }
            if "error" in add_rows_result:
                return {
                    "error": add_rows_result["error"],
                    "ensure_rows_result": ensure_rows_result,
                    "response": add_rows_result,
                }
            _clear_spreadsheet_sheet_cache(spreadsheet_token)

        chunk_results = []
        for cr, cv in chunks:
            try:
                mark_mutation_started()
                result = _call_open_api(
                    "PUT",
                    url,
                    payload={"valueRange": {"range": cr, "values": cv}},
                    timeout=60,
                )
            except Exception as exc:
                return {
                    "error": f"write_sheet 调用失败: {str(exc)[:300]}",
                    "chunks_done": len(chunk_results),
                }
            if "error" in result:
                return {
                    "error": result["error"],
                    "chunks_done": len(chunk_results),
                    "response": result,
                }
            chunk_results.append(result)

        return {
            "ok": True,
            "chunks": len(chunk_results),
            "rows": len(parsed_values),
            "ensure_rows_result": ensure_rows_result,
            "add_rows_result": add_rows_result,
            "results": chunk_results,
        }

    if action == "clear_sheet":
        spreadsheet_token = str(params.get("spreadsheet_token") or "")
        value_range = str(params.get("range") or "")
        if not spreadsheet_token or not value_range:
            return {"error": "clear_sheet 缺少 spreadsheet_token 或 range"}

        qualified_range = _qualify_range(value_range, params.get("sheet_id"))
        try:
            clear_requests = _row_dimension_requests_from_range(
                spreadsheet_token,
                qualified_range,
                resolve_sheet_ref=not params.get("dry_run"),
                require_fresh_metadata=not params.get("dry_run"),
            )
        except Exception as exc:
            return {"error": f"clear_sheet metadata resolution failed: {str(exc)[:300]}"}
        if clear_requests is None:
            return {"error": f"clear_sheet only supports row snapshot ranges starting at column A: {qualified_range}"}

        qualified_range = str(clear_requests["range"])
        if clear_requests.get("skipped"):
            return {
                "ok": True,
                "range": qualified_range,
                "skipped": True,
                "reason": clear_requests.get("reason"),
            }
        delete_url = f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/dimension_range"
        if params.get("dry_run"):
            return {
                "ok": True,
                "api": [
                    {
                        "method": "DELETE",
                        "url": delete_url,
                        "body": clear_requests["delete_payload"],
                    },
                ],
            }

        try:
            if mark_write_started is not None:
                mark_write_started()
            delete_result = _call_open_api(
                "DELETE",
                delete_url,
                payload=clear_requests["delete_payload"],
                timeout=60,
            )
            if "error" in delete_result:
                return {
                    "error": delete_result["error"],
                    "response": delete_result,
                    "range": qualified_range,
                    "requested_range": clear_requests.get("requested_range"),
                    "sheet_row_count": clear_requests.get("sheet_row_count"),
                    "delete_payload": clear_requests.get("delete_payload"),
                }
            _clear_spreadsheet_sheet_cache(spreadsheet_token)
        except Exception as exc:
            return {"error": f"clear_sheet 调用失败: {str(exc)[:300]}"}
        return {
            "ok": True,
            "range": qualified_range,
            "delete_result": delete_result,
        }

    if action == "append_sheet":
        spreadsheet_token = str(params.get("spreadsheet_token") or "")
        value_range = str(params.get("range") or "")
        values = params.get("values")
        if not spreadsheet_token or not value_range or values is None:
            return {"error": "append_sheet 缺少 spreadsheet_token、range 或 values"}

        parsed_values = _ensure_values_list(values)
        if parsed_values is None:
            return {"error": "append_sheet values 不是合法 JSON"}
        if not isinstance(parsed_values, list):
            return {"error": "append_sheet values 必须是二维数组"}

        qualified_range = _qualify_range(value_range, params.get("sheet_id"))
        if not params.get("dry_run"):
            qualified_range = _resolve_sheet_ref_in_range(spreadsheet_token, qualified_range)
        # values_append 自动找空行追加，range 保持不变，只切 values。
        blocks = _split_values_by_rows(parsed_values)
        url = f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_append"

        if params.get("dry_run"):
            return {
                "ok": True,
                "api": [
                    {
                        "method": "POST",
                        "url": url,
                        "body": {"valueRange": {"range": qualified_range, "values": block}},
                    }
                    for block in blocks
                ],
            }

        chunk_results = []
        for block in blocks:
            try:
                result = _call_open_api(
                    "POST",
                    url,
                    payload={"valueRange": {"range": qualified_range, "values": block}},
                    timeout=60,
                )
            except Exception as exc:
                return {
                    "error": f"append_sheet 调用失败: {str(exc)[:300]}",
                    "chunks_done": len(chunk_results),
                }
            if "error" in result:
                return {
                    "error": result["error"],
                    "chunks_done": len(chunk_results),
                    "response": result,
                }
            chunk_results.append(result)

        return {
            "ok": True,
            "chunks": len(chunk_results),
            "rows": len(parsed_values),
            "results": chunk_results,
        }

    if action == "read_sheet":
        spreadsheet_token = str(params.get("spreadsheet_token") or "")
        value_range = str(params.get("range") or "")
        if not spreadsheet_token or not value_range:
            return {"error": "read_sheet 缺少 spreadsheet_token 或 range"}

        args = [
            "sheets",
            "+read",
            "--spreadsheet-token",
            spreadsheet_token,
            "--range",
            value_range,
        ]
        if params.get("sheet_id"):
            args.extend(["--sheet-id", str(params["sheet_id"])])
        if params.get("value_render_option"):
            args.extend(["--value-render-option", str(params["value_render_option"])])
        _append_identity(args, params)
        _append_common_flags(args, params)
        return run_lark_cli(args, timeout=30)

    if action == "create_spreadsheet":
        title = str(params.get("title", "未命名电子表格"))
        args = ["sheets", "+create", "--title", title]
        if params.get("folder_token"):
            args.extend(["--folder-token", str(params["folder_token"])])
        if params.get("headers") is not None:
            headers = params["headers"]
            args.extend(["--headers", headers if isinstance(headers, str) else json.dumps(headers, ensure_ascii=False)])
        if params.get("data") is not None:
            data = params["data"]
            args.extend(["--data", data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)])
        _append_identity(args, params)
        _append_common_flags(args, params)
        return run_lark_cli(args, timeout=30)

    if action == "add_sheet":
        spreadsheet_token = str(params.get("spreadsheet_token") or "")
        title = str(params.get("title") or "")
        if not spreadsheet_token or not title:
            return {"error": "add_sheet 缺少 spreadsheet_token 或 title"}
        body = {
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": title,
                            **({"index": int(params["index"])} if params.get("index") is not None else {}),
                        }
                    }
                }
            ]
        }
        if params.get("dry_run"):
            return {
                "ok": True,
                "api": [
                    {
                        "method": "POST",
                        "url": f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/sheets_batch_update",
                        "body": body,
                    }
                ],
            }
        try:
            return _call_open_api(
                "POST",
                f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/sheets_batch_update",
                payload=body,
                timeout=30,
            )
        except Exception as exc:
            return {"error": f"add_sheet 调用失败: {str(exc)[:300]}"}

    if action == "upload_file":
        file_path = params.get("file_path", "")
        if not file_path:
            return {"error": "upload_file 缺少 file_path"}
        args = ["drive", "+upload", "--file", str(file_path)]
        if params.get("name"):
            args.extend(["--name", str(params["name"])])
        if params.get("folder_token"):
            args.extend(["--folder-token", str(params["folder_token"])])
        _append_identity(args, params)
        _append_common_flags(args, params)
        return run_lark_cli(args, timeout=60)

    if action == "trigger_webhook":
        url = str(params.get("url") or "")
        if not url:
            return {"error": "trigger_webhook 缺少 url"}
        if params.get("dry_run"):
            return {
                "ok": True,
                "api": [
                    {
                        "method": "POST",
                        "url": url,
                        "body": params.get("payload"),
                    }
                ],
            }
        try:
            response = httpx.post(url, json=params.get("payload"), timeout=int(params.get("timeout", 20)))
            response.raise_for_status()
            text = response.text.strip()
            payload = None
            if text:
                try:
                    payload = response.json()
                except Exception:
                    payload = {"text": text[:500]}
            return {"ok": True, "status_code": response.status_code, "response": payload}
        except Exception as exc:
            return {"error": f"trigger_webhook 调用失败: {str(exc)[:300]}"}

    return {"error": f"不支持的操作: {action}"}


def main():
    params = json.loads(sys.stdin.read())
    action = params.get("action", "")
    action_params = params.get("params", {})

    if not action:
        print(json.dumps({"error": "缺少 action 参数"}, ensure_ascii=False))
        sys.exit(1)

    result = feishu_operation(action, action_params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
