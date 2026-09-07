"""Repair or recreate the reviewed Yunda send-waybill spreadsheet target.

This is an explicit release-maintenance entrypoint.  It never guesses another
business sheet: an existing target must match the reviewed title and headers;
otherwise a new reviewed target is created and independently read back before
the managed resource binding is changed.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent.feishu_resource_catalog import (
    FeishuResourceCatalogError,
    refresh_feishu_resource_catalog,
    resolve_live_feishu_resource_config,
)
from agent.phase7_resource_import import BUILTIN_RESOURCES
from agent.runtime_config import load_agent_environment
from agent.workflow_resource_store import upsert_workflow_resource
from tools.feishu_cli_tool import feishu_operation
from tools.yunda_send_waybills_sync_tool import FIELD_NAMES


RESOURCE_KEY = "phase7.yunda_send_waybills_sheet"
_READBACK_DELAYS = (0.0, 0.5, 1.0, 2.0)


def _error_text(result: Any) -> str:
    if not isinstance(result, Mapping):
        return "飞书资源操作返回了无效结果"
    return str(result.get("error") or "").strip()


def _is_not_found_error(message: str) -> bool:
    text = str(message or "").lower()
    return "404" in text or "not found" in text or "不存在" in text


def _find_spreadsheet_token(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("spreadsheet_token", "spreadsheetToken"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        spreadsheet = value.get("spreadsheet")
        if isinstance(spreadsheet, Mapping):
            token = str(spreadsheet.get("token") or "").strip()
            if token:
                return token
        for nested in value.values():
            token = _find_spreadsheet_token(nested)
            if token:
                return token
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            token = _find_spreadsheet_token(nested)
            if token:
                return token
    return ""


def _find_sheet_values(value: Any) -> list[list[Any]] | None:
    if isinstance(value, Mapping):
        direct = value.get("values")
        if isinstance(direct, list) and all(isinstance(row, list) for row in direct):
            return direct
        for nested in value.values():
            rows = _find_sheet_values(nested)
            if rows is not None:
                return rows
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            rows = _find_sheet_values(nested)
            if rows is not None:
                return rows
    return None


def _read_headers(
    config: Mapping[str, Any],
    operation: Callable[[str, dict[str, Any]], Mapping[str, Any]],
) -> tuple[str, ...] | None:
    sheet_id = str(config.get("sheet_id") or "").strip()
    result = operation(
        "read_sheet",
        {
            "spreadsheet_token": str(config.get("spreadsheet_token") or "").strip(),
            "range": f"{sheet_id}!A1:Y1",
            "sheet_id": sheet_id,
            "value_render_option": "FormattedValue",
        },
    )
    error = _error_text(result)
    if error:
        raise RuntimeError(f"韵达寄件结果表表头读取失败：{error}")
    rows = _find_sheet_values(result)
    if rows is None or not rows or not any(str(value or "").strip() for value in rows[0]):
        return None
    return tuple(str(value or "").strip() for value in rows[0])


def _write_headers(
    config: Mapping[str, Any],
    operation: Callable[[str, dict[str, Any]], Mapping[str, Any]],
) -> None:
    sheet_id = str(config.get("sheet_id") or "").strip()
    result = operation(
        "write_sheet",
        {
            "spreadsheet_token": str(config.get("spreadsheet_token") or "").strip(),
            "range": f"{sheet_id}!A1:Y1",
            "sheet_id": sheet_id,
            "values": [list(FIELD_NAMES)],
        },
    )
    error = _error_text(result)
    if error or result.get("ok") is not True:
        raise RuntimeError(f"韵达寄件结果表初始化失败：{error or '写入未确认'}")


def _read_headers_with_readback(
    config: Mapping[str, Any],
    operation: Callable[[str, dict[str, Any]], Mapping[str, Any]],
    *,
    sleeper: Callable[[float], None],
) -> tuple[str, ...] | None:
    """Observe delayed Sheet visibility without repeating a preceding write."""

    observed: tuple[str, ...] | None = None
    for delay in _READBACK_DELAYS:
        if delay:
            sleeper(delay)
        observed = _read_headers(config, operation)
        if observed == tuple(FIELD_NAMES):
            return observed
    return observed


def _resolve_with_readback(
    config: Mapping[str, Any],
    *,
    resolver: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    cache_refresher: Callable[[], None],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    last_error: FeishuResourceCatalogError | None = None
    for delay in _READBACK_DELAYS:
        if delay:
            sleeper(delay)
        cache_refresher()
        try:
            return resolver(RESOURCE_KEY, config)
        except FeishuResourceCatalogError as exc:
            last_error = exc
            if exc.code not in {
                "RESOURCE_NOT_FOUND",
                "RESOURCE_TEMPORARILY_UNAVAILABLE",
            }:
                raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("韵达寄件结果表无法定位")


def repair_yunda_send_waybills_sheet(
    *,
    operation: Callable[[str, dict[str, Any]], Mapping[str, Any]] = feishu_operation,
    resolver: Callable[[str, Mapping[str, Any]], dict[str, Any]] = (
        resolve_live_feishu_resource_config
    ),
    persister: Callable[[str, dict[str, Any], str], None] = upsert_workflow_resource,
    cache_refresher: Callable[[], None] = refresh_feishu_resource_catalog,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    config = dict(BUILTIN_RESOURCES[RESOURCE_KEY])
    action = "rebound"
    try:
        resolved = _resolve_with_readback(
            config,
            resolver=resolver,
            cache_refresher=cache_refresher,
            sleeper=sleeper,
        )
    except FeishuResourceCatalogError as exc:
        if exc.code != "RESOURCE_NOT_FOUND":
            raise
        add_result = operation(
            "add_sheet",
            {
                "spreadsheet_token": config["spreadsheet_token"],
                "title": config["sheet_title"],
            },
        )
        add_error = _error_text(add_result)
        if add_error:
            if not _is_not_found_error(add_error):
                raise RuntimeError(f"韵达寄件结果表新建工作表失败：{add_error}")
            create_result = operation(
                "create_spreadsheet",
                {
                    "title": "韵达寄件结果表",
                    "headers": list(FIELD_NAMES),
                },
            )
            create_error = _error_text(create_result)
            if create_error:
                raise RuntimeError(f"韵达寄件结果表重建失败：{create_error}")
            spreadsheet_token = _find_spreadsheet_token(create_result)
            if not spreadsheet_token:
                raise RuntimeError("韵达寄件结果表重建结果缺少文档身份")
            config["spreadsheet_token"] = spreadsheet_token
            action = "recreated"
        else:
            action = "created_sheet"
        resolved = _resolve_with_readback(
            config,
            resolver=resolver,
            cache_refresher=cache_refresher,
            sleeper=sleeper,
        )

    headers = _read_headers_with_readback(
        resolved,
        operation,
        sleeper=sleeper,
    )
    if headers is None:
        _write_headers(resolved, operation)
        headers = _read_headers_with_readback(
            resolved,
            operation,
            sleeper=sleeper,
        )
        action = "initialized" if action == "rebound" else action
    if headers != tuple(FIELD_NAMES):
        raise RuntimeError("韵达寄件结果表字段结构与已审核合同不一致")

    persister(RESOURCE_KEY, resolved, source="reviewed-yunda-sheet-repair")
    return action


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        print("yunda_send_waybills_sheet=blocked reason=APPLY_REQUIRED")
        return 2
    load_agent_environment()
    action = repair_yunda_send_waybills_sheet()
    print(f"yunda_send_waybills_sheet=ok action={action}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
