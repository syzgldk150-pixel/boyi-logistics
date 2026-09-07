from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import Mock

import pytest

from agent.feishu_resource_catalog import FeishuResourceCatalogError
from scripts.repair_yunda_send_waybills_sheet import (
    RESOURCE_KEY,
    repair_yunda_send_waybills_sheet,
)
from tools.yunda_send_waybills_sync_tool import FIELD_NAMES


def _resolved_config(token: str = "document-token") -> dict:
    return {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": token,
        "sheet_id": "live-sheet",
        "sheet_title": "Sheet1",
        "sheet_header_constraints": {"A": ["5.14编号"], "Y": ["日期"]},
        "sheet_range": "live-sheet!A2:A2",
        "clear_range": "live-sheet!A2:Y5000",
    }


def _read_result(headers: tuple[str, ...] | None) -> dict:
    return {
        "data": {
            "valueRange": {
                "values": [] if headers is None else [list(headers)],
            }
        }
    }


def test_rebinds_unique_existing_reviewed_sheet() -> None:
    persisted: list[tuple[str, dict, str]] = []
    operation = Mock(return_value=_read_result(FIELD_NAMES))

    action = repair_yunda_send_waybills_sheet(
        operation=operation,
        resolver=lambda _key, _config: _resolved_config(),
        persister=lambda key, config, source: persisted.append((key, config, source)),
        cache_refresher=lambda: None,
        sleeper=lambda _delay: None,
    )

    assert action == "rebound"
    assert persisted == [
        (RESOURCE_KEY, _resolved_config(), "reviewed-yunda-sheet-repair")
    ]
    assert [call.args[0] for call in operation.call_args_list] == ["read_sheet"]


def test_creates_missing_child_and_initializes_exact_headers() -> None:
    resolve_calls = 0
    read_calls = 0
    actions: list[str] = []
    persisted: list[tuple[str, dict, str]] = []

    def resolver(_key: str, _config: Mapping[str, object]) -> dict:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls <= 4:
            raise FeishuResourceCatalogError(
                "missing",
                code="RESOURCE_NOT_FOUND",
            )
        return _resolved_config()

    def operation(action: str, _params: dict) -> dict:
        nonlocal read_calls
        actions.append(action)
        if action == "add_sheet":
            return {"code": 0}
        if action == "read_sheet":
            read_calls += 1
            return _read_result(None if read_calls <= 4 else FIELD_NAMES)
        if action == "write_sheet":
            return {"ok": True}
        raise AssertionError(action)

    action = repair_yunda_send_waybills_sheet(
        operation=operation,
        resolver=resolver,
        persister=lambda key, config, source: persisted.append((key, config, source)),
        cache_refresher=lambda: None,
        sleeper=lambda _delay: None,
    )

    assert action == "created_sheet"
    assert persisted[0][0] == RESOURCE_KEY
    assert persisted[0][2] == "reviewed-yunda-sheet-repair"
    assert actions.count("write_sheet") == 1
    assert actions.count("read_sheet") == 5


def test_delayed_header_readback_does_not_repeat_initialization_write() -> None:
    read_calls = 0
    actions: list[str] = []

    def operation(action: str, _params: dict) -> dict:
        nonlocal read_calls
        actions.append(action)
        if action == "read_sheet":
            read_calls += 1
            if read_calls <= 4:
                return _read_result(None)
            if read_calls <= 6:
                return _read_result(None)
            return _read_result(FIELD_NAMES)
        if action == "write_sheet":
            return {"ok": True}
        raise AssertionError(action)

    action = repair_yunda_send_waybills_sheet(
        operation=operation,
        resolver=lambda _key, _config: _resolved_config(),
        persister=lambda _key, _config, source: None,
        cache_refresher=lambda: None,
        sleeper=lambda _delay: None,
    )

    assert action == "initialized"
    assert actions.count("write_sheet") == 1
    assert actions.count("read_sheet") == 7


def test_recreates_missing_document_with_reviewed_schema() -> None:
    resolve_calls = 0
    persisted: list[tuple[str, dict, str]] = []

    def resolver(_key: str, config: Mapping[str, object]) -> dict:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls <= 4:
            raise FeishuResourceCatalogError("missing", code="RESOURCE_NOT_FOUND")
        assert config["spreadsheet_token"] == "new-document-token"
        return _resolved_config("new-document-token")

    def operation(action: str, _params: dict) -> dict:
        if action == "add_sheet":
            return {"error": "404 document not found"}
        if action == "create_spreadsheet":
            return {
                "data": {
                    "spreadsheet": {"spreadsheet_token": "new-document-token"}
                }
            }
        if action == "read_sheet":
            return _read_result(FIELD_NAMES)
        raise AssertionError(action)

    action = repair_yunda_send_waybills_sheet(
        operation=operation,
        resolver=resolver,
        persister=lambda key, config, source: persisted.append((key, config, source)),
        cache_refresher=lambda: None,
        sleeper=lambda _delay: None,
    )

    assert action == "recreated"
    assert persisted[0][1]["spreadsheet_token"] == "new-document-token"


def test_refuses_existing_sheet_with_wrong_headers() -> None:
    operation = Mock(return_value=_read_result(("错误字段",)))
    persister = Mock()

    with pytest.raises(RuntimeError, match="字段结构"):
        repair_yunda_send_waybills_sheet(
            operation=operation,
            resolver=lambda _key, _config: _resolved_config(),
            persister=persister,
            cache_refresher=lambda: None,
            sleeper=lambda _delay: None,
        )

    persister.assert_not_called()
