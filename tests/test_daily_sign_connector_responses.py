"""Production envelope shapes must not leak locators or hide partial reads."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.automation_plugins.connector_registry import ConnectorSensitiveDataDenied, _reject_sensitive_result, _validate_schema_value
from agent.automation_plugins.daily_sign_connectors_v2 import _schemas
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from plugin_core_adapters.daily_sign_ports import _public_feishu_result, _public_tms_result, build_daily_sign_port_handlers


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


@pytest.mark.parametrize("source,code", [
    ({"ok":False,"error_code":"ReadTimeout","message":"HTTPSConnectionPool(host='origin.invalid'): /private/path"}, "SOURCE_QUERY_TIMEOUT"),
    ({"ok":False,"data":{"ok":False,"error_code":"ReadTimeout","message":"https://origin.invalid/query"}}, "SOURCE_QUERY_TIMEOUT"),
    ({"error":"tms service timeout: http://127.0.0.1:9000/tms/customer_service_problem"}, "SOURCE_QUERY_FAILED"),
    ({"ok":False,"error_code":"AUTH_PENDING_CODE","message":"https://origin.invalid/login"}, "AUTH_PENDING_CODE"),
    ({"ok":False,"data":{"ok":False,"error_code":"HTTPError","message":"504 Server Error for https://origin.invalid/query"}}, "SOURCE_HTTP_ERROR"),
])
def test_tms_failure_stays_a_source_failure_without_private_transport_text(source, code):
    original = deepcopy(source)
    result = _public_tms_result(source)
    assert source == original
    assert result["ok"] is False
    assert result["error_code"] == code
    assert "rows" not in result
    _validate_schema_value({"value":result}, _schemas("daily_sign_tms", "read_problems")[1], subject="result")
    _reject_sensitive_result(result, sensitive_identifiers=("host-private-target",), reject_wrapped_identifiers=False)


def test_source_timeout_projection_preserves_existing_read_retry(monkeypatch):
    from datetime import datetime
    from tools import daily_sign_pipeline as pipeline

    success = {"ok":True,"rows":[],"stats":{"total":0,"total_authoritative":True}}
    responses = iter([
        _public_tms_result({"ok":False,"error_code":"ReadTimeout","message":"https://origin.invalid/query"}),
        _public_tms_result(success),
    ])
    calls = []
    def read(endpoint, values):
        calls.append((endpoint, deepcopy(values)))
        return next(responses)
    monkeypatch.setattr(pipeline, "call_http_service", read)
    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)
    rows, _ = pipeline._collect_problem_events({}, account_id="isolated-tms",
        start=datetime(2026,9,14), end=datetime(2026,9,14,12))
    assert rows == []
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_problem_windows_cover_full_interval_without_overlap_or_skipped_pages(monkeypatch):
    from datetime import datetime, timedelta
    import json
    from agent.tms_runtime.scripts.customer_service_problem import build_ronghui_query_payload
    from tools import daily_sign_pipeline as pipeline

    calls = []
    def read(endpoint, values):
        assert endpoint == "/customer_service_problem"
        filters = values["params"]["filters"]
        native = json.loads(build_ronghui_query_payload(filters, direction="registered")["REGISTER_DATE"])
        start, end = (datetime.strptime(native[key], "%Y/%m/%d %H:%M:%S") for key in ("start", "end"))
        calls.append((start, end, filters["page"]))
        page = filters["page"]
        return {"ok": True, "rows": [{"external_id": f"{start.date()}-{page}",
            "waybill_no": f"R0002100000{page}", "problem_type": "少货/分批",
            "registered_at": str(start if page == 1 else end)}],
            "stats": {"total": 2, "total_authoritative": True}}
    monkeypatch.setattr(pipeline, "call_http_service", read)
    start, end = datetime(2026, 8, 31, 13, 5, 6), datetime(2026, 9, 14, 17, 39, 26)
    rows, evidence = pipeline._collect_problem_events({"problem_page_size": 1}, account_id="test", start=start, end=end)
    assert len(rows) == evidence["declared_total"] == 4
    assert evidence["pages"] == 4 and evidence["complete"] is True
    assert [call[2] for call in calls] == [1, 2, 1, 2]
    assert calls[0][0] == start and calls[-1][1] == end
    assert calls[0][1] + timedelta(seconds=1) == calls[2][0]
    with pytest.raises(pipeline.DailySignSyncError, match="总页数限制"):
        pipeline._collect_problem_events({"problem_page_size": 1, "problem_max_pages": 2},
            account_id="test", start=start, end=end)


def test_failed_problem_window_never_returns_partial_history(monkeypatch):
    from datetime import datetime
    from tools import daily_sign_pipeline as pipeline
    calls = []
    def read(endpoint, values):
        calls.append(values["params"]["filters"])
        if len(calls) == 1:
            return {"ok": True, "rows": [], "stats": {"total": 0, "total_authoritative": True}}
        return {"ok": False, "error_code": "SOURCE_HTTP_ERROR", "message": "Upstream failed"}
    monkeypatch.setattr(pipeline, "call_http_service", read)
    with pytest.raises(pipeline.DailySignSyncError) as error:
        pipeline._collect_problem_events({"problem_retry_attempts": 1}, account_id="test",
            start=datetime(2026, 8, 1), end=datetime(2026, 9, 14))
    assert error.value.code == "SOURCE_HTTP_ERROR" and len(calls) == 2


def test_tracking_port_keeps_scan_facts_without_ui_descriptions():
    facts = {"scan_type":"签收", "scan_time":"2026-09-12 10:00:00", "scan_station":"测试网点", "scan_code":"R00021000001"}
    source = {"ok":True, "data":{"ok":True,"summary":{"latest_description":"/private/attachment"},
        "waybill_info":[{"private":"customer details"}], "waybill_stub":{"private":"customer details"},
        "route_rows":[{**facts, "description":"/private/attachment"}]}}
    original = deepcopy(source)
    accounts = SimpleNamespace(require_active_binding_descriptor=lambda _: {"system":"ronghui"})
    tms = Mock(return_value=source)
    handlers = build_daily_sign_port_handlers(account_manager=accounts, tms=tms)
    context = CoreBrokerInvocationContext(automation_id="isolated", plugin_version="2.0.1",
        tool_name="sync_daily_should_sign", operation="daily_sign.port", action="daily_sign_tms.read_tracking",
        role="daily_sign_tms", account_ids=("isolated-tms",))
    value = handlers[("daily_sign.port",context.action)](context,{"values":{"params":{"tracking_number":"R00021000001"}}})["value"]
    assert source == original
    assert value == {"ok":True,"data":{"ok":True,"route_rows":[facts]}}
    assert tms.call_args.args[1]["params"]["decrypt_masked"] is False
    _validate_schema_value({"value":value}, _schemas(context.role,"read_tracking")[1], subject="result")
    _reject_sensitive_result(value, sensitive_identifiers=("isolated-tms",), reject_wrapped_identifiers=False)


def test_tracking_port_accepts_real_scans_when_customer_detail_is_masked(monkeypatch):
    from agent.tms_runtime.scripts import ronghui_tms_tracking as tracking

    source_rows = [{"scan_type":"签收", "scan_time":"2026-09-12 10:00:00", "scan_station":"测试网点", "scan_code":"R00021000001"}]
    monkeypatch.setattr(tracking.TMSAuth, "login_and_get_session", lambda _: object())
    monkeypatch.setattr(tracking, "_resolve_widget_menu_url", lambda _: "https://origin.invalid/tracking")
    monkeypatch.setattr(tracking, "_collect_api_tracking_rows", lambda *_args, **_kwargs:
        (source_rows, [], {"billCode":"R00021000001", "sendMan":"*", "sendManMobile":"***"}))
    # Keep the real adapter and its decryption decision; accessing the private
    # detail reader in a scan-only request is the regression being exercised.
    monkeypatch.setattr(tracking, "_needs_decrypted_detail", lambda _: True)
    private_reader = Mock(side_effect=AssertionError("scan verification must not decrypt customer details"))
    monkeypatch.setattr(tracking, "query_waybill_detail", private_reader)
    accounts = SimpleNamespace(require_active_binding_descriptor=lambda _: {"system":"ronghui"})
    handlers = build_daily_sign_port_handlers(account_manager=accounts,
        tms=lambda _endpoint, request: tracking.query_ronghui_tms_tracking(request["params"]))
    context = CoreBrokerInvocationContext(automation_id="isolated", plugin_version="2.0.3",
        tool_name="sync_daily_should_sign", operation="daily_sign.port", action="daily_sign_tms.read_tracking",
        role="daily_sign_tms", account_ids=("isolated-tms",))
    result = handlers[("daily_sign.port", context.action)](context,
        {"values":{"params":{"tracking_number":"R00021000001", "decrypt_masked":True}}})["value"]
    assert result["ok"] is True
    assert result["route_rows"][0]["scan_type"] == "签收"
    assert "waybill_info" not in result
    private_reader.assert_not_called()
