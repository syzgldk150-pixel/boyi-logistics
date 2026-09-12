"""Production envelope shapes must not leak locators or hide partial reads."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.automation_plugins.connector_registry import ConnectorSensitiveDataDenied, _reject_sensitive_result, _validate_schema_value
from agent.automation_plugins.daily_sign_connectors_v2 import _schemas
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from plugin_core_adapters.daily_sign_ports import _public_feishu_result, build_daily_sign_port_handlers


def checked(name, source):
    original = deepcopy(source)
    value = _public_feishu_result(name, source)
    assert source == original
    role = "daily_sign_sheet" if name.endswith("sheet") else "daily_sign_bitable"
    _validate_schema_value({"value":value}, _schemas(role, name)[1], subject="result")
    _reject_sensitive_result(value, sensitive_identifiers=("host-private-target",), reject_wrapped_identifiers=False)
    return value


def test_actual_sheet_envelope_keeps_values_and_discards_host_locators():
    values = [["运单编号", "件数"], ["R00021000001", 3]]
    assert checked("read_sheet", {"ok":True, "identity":"bot", "data":{
        "spreadsheetToken":"host-private-target", "valueRange":{
            "range":"host-private-target!A1:B2", "majorDimension":"ROWS", "values":values}}}) == {"valueRange":{"values":values}}
    assert checked("read_sheet", {"data":{"valueRange":{"values":[]}}}) == {"valueRange":{"values":[]}}


@pytest.mark.parametrize("source", [None, {"ok":True}, {"data":{"valueRange":{}}},
    {"values":None}, {"values":[3]}, {"values":[[1]], "data":{"valueRange":{"values":[[2]]}}}])
def test_missing_or_conflicting_sheet_payload_never_becomes_empty(source):
    with pytest.raises(PluginExecutionError):
        checked("read_sheet", source)


def test_actual_record_envelope_is_projected_without_losing_business_fields():
    row = {"record_id":"record-1", "fields":{"运单编号":"R00021000001", "件数":3}}
    source = {"ok":True, "items":[{**row, "data":["R00021000001",3]}],
        "data":{"items":[row], "has_more":False, "query_context":{"private_locator":"host-private-target"}}}
    assert checked("list_records", source) == {"items":[row]}


@pytest.mark.parametrize("source", [{"items":[], "data":{"has_more":True}},
    {"items":[], "has_more":True}, {"items":[], "data":{"has_more":"false"}}, {"ok":True}, {"items":[None]}])
def test_incomplete_or_invalid_record_response_is_rejected_before_sync(source):
    with pytest.raises(PluginExecutionError):
        checked("list_records", source)


@pytest.mark.parametrize("name,source,expected", [
    ("write_sheet", {"ok":True,"rows":2,"chunks":1,"results":[{"data":{"spreadsheetToken":"host-private-target"}}]}, {"ok":True,"rows":2,"chunks":1}),
    ("clear_sheet", {"ok":True,"range":"host-private-target!A1:I3","delete_result":{"code":0}}, {"ok":True}),
    ("write_records", {"ok":True,"requested":2,"written":2,"results":[{"result":{"data":{"record":{}}}}]}, {"ok":True,"requested":2,"written":2}),
    ("delete_records", {"ok":True,"deleted":2,"requested":2,"results":[]}, {"ok":True,"deleted":2,"requested":2}),
    ("create_field", {"code":0,"data":{"field":{"field_id":"field-1"}}}, {"ok":True}),
    ("update_field", {"code":0,"data":{"field":{"field_id":"field-1"}}}, {"ok":True}),
])
def test_mutation_acknowledgement_preserves_only_actual_counts(name, source, expected):
    assert checked(name, source) == expected


@pytest.mark.parametrize("source", [{"ok":False}, {"ok":True,"errors":[{"error":"failed"}]},
    {"ok":True,"data":{"error":"failed"}}, {"ok":True,"code":1254000},
    {"written":2}, {"ok":True,"written":-1}, {"ok":True,"written":True}])
def test_projection_does_not_turn_failures_into_success(source):
    with pytest.raises(PluginExecutionError):
        checked("write_records", source)


def test_business_fields_are_still_checked_for_sensitive_content():
    with pytest.raises(ConnectorSensitiveDataDenied):
        checked("list_records", {"items":[{"record_id":"record-1", "fields":{"password":"do-not-release"}}]})


def test_tracking_port_keeps_scan_facts_without_ui_descriptions():
    facts = {"scan_type":"签收", "scan_time":"2026-09-12 10:00:00", "scan_station":"测试网点", "scan_code":"R00021000001"}
    source = {"ok":True, "data":{"ok":True,"summary":{"latest_description":"/private/attachment"},
        "route_rows":[{**facts, "description":"/private/attachment"}]}}
    original = deepcopy(source)
    accounts = SimpleNamespace(require_active_binding_descriptor=lambda _: {"system":"ronghui"})
    handlers = build_daily_sign_port_handlers(account_manager=accounts, tms=lambda *_: source)
    context = CoreBrokerInvocationContext(automation_id="isolated", plugin_version="2.0.1",
        tool_name="sync_daily_should_sign", operation="daily_sign.port", action="daily_sign_tms.read_tracking",
        role="daily_sign_tms", account_ids=("isolated-tms",))
    value = handlers[("daily_sign.port",context.action)](context,{"values":{"params":{"tracking_number":"R00021000001"}}})["value"]
    assert source == original
    assert value == {"ok":True,"data":{"ok":True,"route_rows":[facts]}}
    _validate_schema_value({"value":value}, _schemas(context.role,"read_tracking")[1], subject="result")
    _reject_sensitive_result(value, sensitive_identifiers=("isolated-tms",), reject_wrapped_identifiers=False)
