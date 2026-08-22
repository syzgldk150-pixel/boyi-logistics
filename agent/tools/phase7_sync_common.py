"""Phase 7 列表型工作流的共享同步逻辑。"""

from __future__ import annotations

import re
from typing import Any, Callable

from agent.workflow_resource_store import get_workflow_resource
from tools.feishu_cli_tool import feishu_operation

TMS_AUTH_ERROR_CODES = {"AUTH_REQUIRED", "AUTH_PENDING_CODE"}
TMS_AUTH_REQUIRED_KEYWORDS = (
    "AUTH_REQUIRED",
    "当前未登录",
    "登录态已过期",
    "登录态已失效",
    "登录已过期",
    "共享 storage state 不存在",
)
TMS_AUTH_PENDING_CODE_KEYWORDS = (
    "AUTH_PENDING_CODE",
    "短信验证码已发送",
    "等待人工提交验证码",
)


class TMSAuthSyncError(Exception):
    """Raised inside sync helpers so the top-level tool can return structured auth errors."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__(str(result.get("error") or result.get("error_code") or "TMS auth required"))


def require_explicit_account_id(params: dict[str, Any], *, label: str) -> str:
    """Return the approved account id and reject aliases that disagree with it."""
    account_id = str(params.get("account_id") or "").strip()
    if not account_id:
        raise ValueError(f"{label}必须提供唯一 account_id，禁止选择隐式默认账号")
    account_alias = str(params.get("accountId") or "").strip()
    if account_alias and account_alias != account_id:
        raise ValueError(f"{label}的 accountId 与 account_id 不一致")
    return account_id


def bind_explicit_account_id(
    request_params: dict[str, Any],
    account_id: str,
    *,
    label: str,
) -> dict[str, Any]:
    """Bind one approved account to a nested request without overwriting conflicts."""
    bound = dict(request_params)
    nested_ids = {
        str(bound.get(key) or "").strip()
        for key in ("account_id", "accountId")
        if str(bound.get(key) or "").strip()
    }
    if nested_ids and nested_ids != {account_id}:
        raise ValueError(f"{label}中的账号与控制平面批准的 account_id 不一致")
    bound.pop("accountId", None)
    bound["account_id"] = account_id
    return bound


def normalize_explicit_account_params(params: dict[str, Any], *, label: str) -> dict[str, Any]:
    """Canonicalize the top-level account field before any external call."""
    account_id = require_explicit_account_id(params, label=label)
    normalized = dict(params)
    normalized.pop("accountId", None)
    normalized["account_id"] = account_id
    return normalized


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _auth_code_from_text(text: str) -> str:
    if any(keyword in text for keyword in TMS_AUTH_PENDING_CODE_KEYWORDS):
        return "AUTH_PENDING_CODE"
    if any(keyword in text for keyword in TMS_AUTH_REQUIRED_KEYWORDS):
        return "AUTH_REQUIRED"
    return ""


def tms_auth_error_result(payload: Any) -> dict[str, Any] | None:
    """Return a structured auth error if a TMS gateway payload contains one."""
    stack = [payload]
    seen: set[int] = set()
    while stack:
        item = stack.pop(0)
        item_id = id(item)
        if item_id in seen:
            continue
        seen.add(item_id)

        if isinstance(item, dict):
            code = str(item.get("error_code") or item.get("code") or "").strip()
            message = _first_text(
                item.get("error"),
                item.get("message"),
                item.get("last_error_summary"),
                item.get("detail"),
            )
            inferred_code = code if code in TMS_AUTH_ERROR_CODES else _auth_code_from_text(message)
            if inferred_code:
                return {
                    "error": message or inferred_code,
                    "error_code": inferred_code,
                    "raw": payload,
                }
            stack.extend(item.values())
            continue

        if isinstance(item, (list, tuple)):
            stack.extend(item)
            continue

        if isinstance(item, str):
            inferred_code = _auth_code_from_text(item)
            if inferred_code:
                return {
                    "error": item.strip() or inferred_code,
                    "error_code": inferred_code,
                    "raw": payload,
                }
    return None


def raise_tms_auth_error_if_present(payload: Any) -> None:
    auth_error = tms_auth_error_result(payload)
    if auth_error:
        raise TMSAuthSyncError(auth_error)


def _resource_error(resource_key: str) -> ValueError:
    return ValueError(f"未找到 {resource_key}，请先导入到 MySQL")


def get_required_resource(resource_key: str) -> dict:
    resource = get_workflow_resource(resource_key)
    if not resource:
        raise _resource_error(resource_key)
    return resource


def resolve_bitable_target(params: dict, resource_key: str) -> tuple[str, str]:
    base_token = params.get("base_token") or params.get("app_token")
    table_id = params.get("table_id")
    if base_token and table_id:
        return str(base_token), str(table_id)

    resource = get_workflow_resource(resource_key)
    if not resource or not resource.get("base_token") or not resource.get("table_id"):
        raise _resource_error(resource_key)
    return str(resource["base_token"]), str(resource["table_id"])


def resolve_sheet_target(params: dict, resource_key: str) -> tuple[str, str]:
    spreadsheet_token = params.get("spreadsheet_token")
    value_range = params.get("range")
    if spreadsheet_token and value_range:
        return str(spreadsheet_token), str(value_range)

    resource = get_workflow_resource(resource_key)
    if not resource or not resource.get("spreadsheet_token") or not resource.get("range"):
        raise _resource_error(resource_key)
    return str(resource["spreadsheet_token"]), str(resource["range"])


def _extract_record_items(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for candidate in (
        payload.get("data", {}).get("items"),
        payload.get("items"),
        payload.get("records"),
    ):
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def list_record_ids(base_token: str, table_id: str, params: dict) -> tuple[list[str] | None, dict]:
    list_result = feishu_operation(
        "list_records",
        {
            "base_token": base_token,
            "table_id": table_id,
            "limit": params.get("list_limit", 500),
            "as": params.get("as", "bot"),
            "dry_run": bool(params.get("dry_run", False)),
        },
    )
    if "error" in list_result:
        return None, list_result

    record_ids = [
        str(item["record_id"])
        for item in _extract_record_items(list_result)
        if item.get("record_id")
    ]
    return record_ids, list_result


def sync_bitable_snapshot(
    resource_key: str,
    records: list[dict],
    params: dict,
    *,
    mark_write_started: Callable[[], None] | None = None,
) -> dict:
    base_token, table_id = resolve_bitable_target(params, resource_key)
    existing_record_ids, list_result = list_record_ids(base_token, table_id, params)
    if existing_record_ids is None:
        return {"error": "飞书读取现有多维表记录失败", "feishu_result": list_result}

    write_started = False

    def mark_mutation_started() -> None:
        nonlocal write_started
        if not write_started and mark_write_started is not None:
            mark_write_started()
        write_started = True

    delete_result: dict[str, Any] | None = None
    if existing_record_ids:
        mark_mutation_started()
        delete_result = feishu_operation(
            "delete_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "record_ids": existing_record_ids,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in delete_result or delete_result.get("errors"):
            return {
                "error": "飞书删除旧多维表记录失败",
                "existing_record_ids": existing_record_ids,
                "feishu_result": delete_result,
            }

    write_result: dict[str, Any] = {
        "ok": True,
        "requested": 0,
        "written": 0,
        "results": [],
    }
    if records:
        mark_mutation_started()
        write_result = feishu_operation(
            "write_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "records": records,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in write_result or write_result.get("errors"):
            return {
                "error": "飞书写入多维表失败",
                "existing_record_ids": existing_record_ids,
                "delete_result": delete_result,
                "feishu_result": write_result,
            }

    return {
        "ok": True,
        "existing_record_ids": existing_record_ids,
        "deleted": len(existing_record_ids),
        "written": write_result.get("written", 0),
        "delete_result": delete_result,
        "write_result": write_result,
    }


_A1_RANGE_RE = re.compile(r"^(?P<sheet>[^!]+)!(?P<start_col>[A-Z]+)(?P<start_row>\d+):(?P<end_col>[A-Z]+)(?P<end_row>\d+)$")


def _column_to_number(col: str) -> int:
    total = 0
    for char in col:
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total


def _parse_range_shape(value_range: str) -> tuple[int, int] | None:
    match = _A1_RANGE_RE.match(value_range.strip())
    if not match:
        return None
    row_count = int(match.group("end_row")) - int(match.group("start_row")) + 1
    col_count = _column_to_number(match.group("end_col")) - _column_to_number(match.group("start_col")) + 1
    if row_count <= 0 or col_count <= 0:
        return None
    return row_count, col_count


def _number_to_column(number: int) -> str:
    value = ""
    current = number
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        value = chr(ord("A") + remainder) + value
    return value


def parse_a1_range(value_range: str) -> dict:
    match = _A1_RANGE_RE.match(value_range.strip())
    if not match:
        raise ValueError(f"不支持的 A1 范围: {value_range}")
    start_col = match.group("start_col")
    end_col = match.group("end_col")
    start_row = int(match.group("start_row"))
    end_row = int(match.group("end_row"))
    return {
        "sheet": match.group("sheet"),
        "start_col": start_col,
        "end_col": end_col,
        "start_row": start_row,
        "end_row": end_row,
        "col_count": _column_to_number(end_col) - _column_to_number(start_col) + 1,
        "row_count": end_row - start_row + 1,
    }


def build_range_from_template(template_range: str, row_count: int, col_count: int) -> str:
    info = parse_a1_range(template_range)
    start_col_num = _column_to_number(info["start_col"])
    end_col = _number_to_column(start_col_num + max(col_count, 1) - 1)
    end_row = info["start_row"] + max(row_count, 1) - 1
    return f"{info['sheet']}!{info['start_col']}{info['start_row']}:{end_col}{end_row}"


def _append_feishu_error(base: str, result: dict[str, Any]) -> str:
    detail = ""
    if isinstance(result, dict):
        detail = str(result.get("error") or "").strip()
    return f"{base}: {detail}" if detail else base


def sync_sheet_snapshot(resource_key: str, values: list[list[Any]], params: dict) -> dict:
    spreadsheet_token, value_range = resolve_sheet_target(params, resource_key)
    clear_result: dict[str, Any] | None = None
    clear_range = str(params.get("clear_range") or "").strip()
    if not clear_range:
        resource = get_workflow_resource(resource_key) or {}
        clear_range = str(resource.get("clear_range") or "").strip()
    if not clear_range:
        clear_range = value_range

    clear_shape = _parse_range_shape(clear_range)
    if clear_shape is not None:
        clear_result = feishu_operation(
            "clear_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": clear_range,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
        if "error" in clear_result:
            return {
                "error": _append_feishu_error("飞书清空电子表格失败", clear_result),
                "feishu_result": clear_result,
            }

    write_result: dict[str, Any]
    if values:
        write_result = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": value_range,
                "values": values,
                "as": params.get("as", "bot"),
                "dry_run": bool(params.get("dry_run", False)),
            },
        )
    else:
        write_result = {"ok": True, "skipped": True, "rows": 0}
    if "error" in write_result:
        return {
            "error": _append_feishu_error("飞书写入电子表格失败", write_result),
            "clear_result": clear_result,
            "feishu_result": write_result,
        }

    return {
        "ok": True,
        "rows": len(values),
        "clear_result": clear_result,
        "write_result": write_result,
    }
