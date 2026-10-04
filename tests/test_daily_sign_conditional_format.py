from copy import deepcopy
from unittest.mock import Mock
import json

import pytest

from agent.automation_plugins.connector_registry import (
    ConnectorSensitiveDataDenied, _reject_sensitive_result, _validate_schema_value,
)
from agent.automation_plugins.daily_sign_connectors_v2 import _schemas
from service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_format import (
    QUANTITY_COLOR, QUANTITY_FORMULA, sync_quantity_format,
)
from tests.daily_sign_format_fixture import SheetFormats
from tools.feishu_sheet_formats import sheet_format_operation


def sync(server, rows):
    return sync_quantity_format(server, "bound-sheet", "bound", rows)


def test_growth_shrink_repeat_zero_and_return_preserve_one_rule_and_other_formats():
    server = SheetFormats()
    other = {"rule_id": "other", "properties": {"rule_type": "containsBlanks", "ranges": ["C2:C5"]}}
    server.rules.append(deepcopy(other))
    for count in [26, 30, 5, 5, 0, 3]:
        result = sync(server, count)
        assert result["verified"]
        assert len(server.rules) == 2
        assert server.rules[0] == other
        if count:
            assert server.rules[1]["properties"] == {
                "rule_type": "expression", "attrs": [{"formula": [QUANTITY_FORMULA]}],
                "style": {"back_color": QUANTITY_COLOR}, "ranges": [f"A2:L{count+1}"]}
    assert len(server.writes) == 4
    assert all(w.get("rule_id") == "rule-2" for w in server.writes[1:])
    server.rules[1]["properties"]["style"]["font"] = "bold"
    sync(server, 12)
    assert server.rules[1]["properties"]["style"]["font"] == "bold"


def test_lost_create_ack_is_read_back_without_second_write():
    server = SheetFormats()
    def operation(name, params):
        result = server(name, params)
        if name == "write_sheet_format":
            raise TimeoutError("response lost after commit")
        return result
    assert sync(operation, 26)["verified"]
    assert len(server.writes) == len(server.rules) == 1


def test_sheet_refresh_grows_and_deletes_rows_before_updating_format(monkeypatch):
    from service_v2_plugins.sync_daily_should_sign_v2.payload.business import daily_sign_sync_tool as report
    from tools.phase7_sync_common import parse_a1_range
    server = SheetFormats()
    sheet = [report.SHEET_HEADERS] + [[""] * 12 for _ in range(100)]
    def operation(name, params):
        if name in {"read_sheet_formats", "write_sheet_format"}:
            assert len(sheet) == expected_rows + 1
            return server(name, params)
        info = parse_a1_range(params["range"])
        start, end = info["start_row"] - 1, info["end_row"]
        if name == "read_sheet":
            return {"values": deepcopy(sheet[start:end])}
        if name == "write_sheet":
            sheet[start:end] = deepcopy(params["values"])
        elif name == "clear_sheet":
            del sheet[start:end]
        return {"ok": True}
    monkeypatch.setattr(report, "resolve_sheet_target", lambda *_: ("bound", "tab!A2:L101"))
    monkeypatch.setattr(report, "feishu_operation", operation)
    for expected_rows in [26, 30, 5]:
        rows = [{"tracking_number": f"R{i}", "expected_quantity": 10, "arrived_quantity": 5}
                for i in range(expected_rows)]
        result = report._sync_sheet(rows, {})
        assert result["ok"] and result["readback"]["record_count"] == expected_rows
        assert result["format_readback"]["range"] == f"A2:L{expected_rows+1}"
        assert len(server.rules) == 1


def test_missing_read_and_ambiguous_existing_rules_fail_before_format_write():
    with pytest.raises(RuntimeError, match="READ_FAILED"):
        sync(lambda *_: {"ok": True}, 26)
    server = SheetFormats()
    sync(server, 26)
    server.rules.append({**deepcopy(server.rules[0]), "rule_id": "duplicate"})
    with pytest.raises(RuntimeError, match="AMBIGUOUS"):
        sync(server, 30)
    assert len(server.writes) == 1


def test_ack_without_saved_format_never_reports_success(monkeypatch):
    from agent import feishu_readback
    monkeypatch.setattr(feishu_readback.time, "sleep", lambda _: None)
    with pytest.raises(Exception):
        sync(lambda name, _: {"rules": []} if name == "read_sheet_formats" else {"ok": True}, 5)


def test_real_sheet_ai_envelopes_and_exact_bound_write():
    from tools.phase7_sync_common import parse_a1_range
    cells = parse_a1_range("bound!A2:L29")
    cells.pop("sheet")
    _validate_schema_value({"values": {"cells": cells}},
        _schemas("daily_sign_sheet", "read_sheet_formats")[0], subject="input")
    rule = {"rule_type": "expression", "ranges": ["A2:L29"],
        "attrs": [{"formula": [QUANTITY_FORMULA]}], "style": {"back_color": QUANTITY_COLOR}}
    output = {"sheets": [{"sheet_id": "actual-tab", "sheet_name": "应签明细", "conditional_formats": [
        {"conditional_format_id": "format-id", "details": rule}]}], "total": 1, "revision": 13126}
    api = Mock(return_value={"code": 0, "data": {"output": json.dumps(output)}})
    info = Mock(return_value={"sheet_id": "actual-tab"})
    params = {"spreadsheet_token": "bound-token", "range": "title!A2:L29"}
    result = sheet_format_operation("read_sheet_formats", params, call_api=api, sheet_info=info)
    assert result == {"rules": [{"rule_id": "format-id", "properties": rule}]}
    _reject_sensitive_result(result, sensitive_identifiers=("bound-token",), reject_wrapped_identifiers=False)
    _validate_schema_value({"value": result}, _schemas("daily_sign_sheet", "read_sheet_formats")[1], subject="result")
    api.return_value = {"code": 0, "data": {"output": '{"conditional_format_id":"format-id","revision":13127}'}}
    marked = Mock()
    sheet_format_operation("write_sheet_format", {**params, "rule_id": "format-id",
        "properties": {**rule, "ranges": ["wrong-sheet!A1:Z100"]}},
        call_api=api, sheet_info=info, mark_write_started=marked)
    marked.assert_called_once()
    body = json.loads(api.call_args.args[2]["input"])
    assert body["sheet_id"] == "actual-tab" and body["operation"] == "update"
    assert body["properties"]["ranges"] == ["A2:L29"]
    assert "invoke_write" in api.call_args.args[1]


@pytest.mark.parametrize("output", [{}, {"sheets": [], "total": 1},
    {"sheets": [{"sheet_id": "different-tab", "conditional_formats": []}], "total": 0}])
def test_invalid_or_partial_api_read_cannot_look_empty(output):
    with pytest.raises(RuntimeError):
        sheet_format_operation("read_sheet_formats", {"spreadsheet_token": "bound", "range": "tab!A2:L3"},
            call_api=lambda *_a, **_k: {"code": 0, "data": {"output": json.dumps(output)}},
            sheet_info=lambda *_a, **_k: {"sheet_id": "tab"})


@pytest.mark.parametrize("value", ["https://private.example", "/private/file", "private-tab!A2:L29"])
def test_format_range_exception_does_not_allow_private_targets(value):
    with pytest.raises(ConnectorSensitiveDataDenied):
        _reject_sensitive_result({"ranges": [value]}, sensitive_identifiers=("private-tab",), reject_wrapped_identifiers=False)
