from copy import deepcopy
from datetime import date

import pytest

from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.scripts import yunda_send_waybills as runtime
from plugin_core_adapters import first_party as adapters
from service_v2_plugins.sync_yunda_send_waybills_v2.payload.yunda_send_fields import (
    FIELD_NAMES,
    normalize_record,
)
from tools import feishu_cli_tool
from tools import yunda_send_waybills_sync_tool as sink


def _sources():
    return (
        {
            "Logistics_Id": "FIXTURE-1", "Mail_Date": "2026-08-15 10:00:00",
            "Sign_State": "未签收", "Shipping_Methods": "180", "Payment_Type": "103", "Freight": "0.00",
            "Total_Cost_Money": "*", "Gross_Weight": 0, "Volume": "0.5",
            "Settlement_Total_Number": "101.5", "Insured_Amount": "2000",
        },
        {
            "Logistics_Id": "FIXTURE-1", "Extend_Field1": "100", "COD": 0,
            "Collection_Money": "12.50", "Scan_Description": "原站扫描文字",
        },
        {"Sender_Name": "寄件人", "Sender_Address": "寄件地址", "Sender_Phone": "000"},
        {"Logistics_Id": "FIXTURE-1", "price": {"Total": "0.00"}, "Delivers_Return": 0, "Return_Type": 1},
    )


def test_fresh_renderer_cost_preserves_zero_and_separates_payment_fields():
    record = normalize_record(*_sources(), target_date="2026-08-15")
    assert record["录单运费"] == "0.00"
    assert record["运费"] == record["提付"] == "0.00"
    assert record["代收货款"] == "12.50"
    assert record["到付款"] == "0"
    assert [record[key] for key in ("实际重量", "体积", "结算重量", "体积重量")] == ["0", "0.5", "101.5", "100"]
    assert record["需要回单"] == "否"
    assert record["寄件地址"] == "寄件地址"
    assert record["scan_status"] == "原站扫描文字"
    assert tuple(key for key in record if key not in {"scan_status", "status"}) == FIELD_NAMES
    projected = sink._console_waybill_records([record], target_date=date(2026, 8, 15))[0]
    assert projected["recorded_cost"] == "0.00"
    assert projected["freight_fee"] == "0.00"
    assert projected["transfer_fee"] == ""
    assert projected["cod_amount"] == "12.50"
    assert projected["to_pay_amount"] == "0.00"
    assert projected["receipt_required"] == "0"
    assert projected["scan_status"] == "原站扫描文字"
    assert projected["status"] == "in_transit"


@pytest.mark.parametrize("cost", [None, "", "*", "NaN", "Infinity", "-Infinity"])
def test_missing_or_nonfinite_renderer_cost_never_uses_list_amount(cost):
    row, detail, original, renderer = _sources()
    row["Total_Cost_Money"] = "999"
    renderer["price"]["Total"] = cost
    with pytest.raises(ValueError, match="cost|numeric"):
        normalize_record(row, detail, original, renderer, target_date="2026-08-15")


def test_special_line_cost_comes_from_current_list_and_receipt_is_not_return_type():
    row, detail, original, renderer = _sources()
    row["Total_Cost_Money"] = "27.50"
    detail["Scan_Flag_Keys"] = ["send"]
    renderer = {"Return_Type": "1"}
    record = normalize_record(row, detail, original, renderer, target_date="2026-08-15", source="special_line")
    assert record["录单运费"] == "27.50"
    assert record["需要回单"] == ""


@pytest.mark.parametrize("renderer_identity", [None, "WRONG-WAYBILL"])
def test_renderer_identity_is_required_by_mapping_and_direct_reader(monkeypatch, renderer_identity):
    row, detail, original, renderer = _sources()
    renderer["Logistics_Id"] = renderer_identity
    with pytest.raises(ValueError, match="renderer identity"):
        normalize_record(row, detail, original, renderer, target_date="2026-08-15")
    monkeypatch.setattr(runtime, "fetch_waybill_detail", lambda *_: detail)
    monkeypatch.setattr(runtime, "fetch_original_data", lambda *_: original)
    monkeypatch.setattr(runtime, "fetch_send_waybill_renderer", lambda *_: renderer)
    tagged = {**row, runtime.INTERNAL_SOURCE_FIELD: runtime.SOURCE_SEND_WAYBILL}
    with pytest.raises(ValueError, match="renderer identity"):
        runtime.enrich_records(None, [tagged], {}, target_date=date(2026, 8, 15))


@pytest.mark.parametrize("sign_state,expected", [("未签收", "in_transit"), ("已签收", "signed")])
def test_original_signing_text_preserves_signed_status(sign_state, expected):
    row, detail, original, renderer = _sources()
    row["Sign_State"] = sign_state
    record = normalize_record(row, detail, original, renderer, target_date="2026-08-15")
    assert record["status"] == expected
    assert sink._console_waybill_records([record], target_date=date(2026, 8, 15))[0]["status"] == expected


@pytest.mark.parametrize("sign_state", [None, "", "1", "未知状态"])
def test_unknown_signing_status_cannot_overwrite_history(sign_state):
    row, detail, original, renderer = _sources()
    row["Sign_State"] = sign_state
    detail["Sign_State"] = "已签收"
    detail["Scan_Flag_Keys"] = ["signed"]
    with pytest.raises(ValueError, match="signing status"):
        normalize_record(row, detail, original, renderer, target_date="2026-08-15")


@pytest.mark.parametrize("change", ["date_missing", "date_mismatch", "delivery_missing", "nonfinite_weight"])
def test_incomplete_source_fails_before_publication(change):
    row, detail, original, renderer = _sources()
    if change == "date_missing":
        row.pop("Mail_Date")
    elif change == "date_mismatch":
        detail["Mail_Date"] = "2026-08-16"
    elif change == "delivery_missing":
        row.pop("Shipping_Methods")
    else:
        row["Gross_Weight"] = "NaN"
    with pytest.raises(ValueError):
        normalize_record(row, detail, original, renderer, target_date="2026-08-15")


@pytest.mark.parametrize("payload", [{}, {"rows": []}, {"rows": [], "total": 0, "count": 1}, {"rows": [], "total": 0, "data": {"rows": [{"Logistics_Id": "X"}]}}])
def test_incomplete_or_conflicting_source_page_is_not_an_empty_success(payload):
    with pytest.raises(ValueError):
        runtime._complete_page_shape(payload)


def test_same_source_duplicates_rejected_while_normal_source_wins_cross_source_overlap():
    row = {"Logistics_Id": "FIXTURE-1"}
    with pytest.raises(ValueError):
        runtime._merge_rows_by_waybill([row, row])
    assert runtime._merge_rows_by_waybill([row], [row]) == [row]


def test_tracking_detail_explicitly_queries_all_history(monkeypatch):
    calls = []

    class Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return object()

    detail = {"Logistics_Id": "FIXTURE-1"}
    monkeypatch.setattr(runtime, "_decode_json_response", lambda *_args, **_kwargs: {"rows": [{"FIXTURE-1": {"logistics": detail}}]})
    assert runtime.fetch_waybill_detail(Session(), "FIXTURE-1", {}) == detail
    assert calls[0][1]["data"] == {"Ids[]": "FIXTURE-1", "bringSub": "true", "page": 1, "history": "extra"}
    assert len(calls) == 1


def test_tracking_flags_belong_to_exact_waybill_root_not_child_or_sibling():
    payload = {"rows": [{
        "FIXTURE-1-child": {
            "logistics": {"Logistics_Id": "FIXTURE-1"},
            "smi": {"info": {"scanFlag": {"signed": True}}},
        },
        "FIXTURE-1": {
            "logistics": {"Logistics_Id": "FIXTURE-1"},
            "smi": {"info": {"scanFlag": {"send": False}}},
        },
    }]}
    detail = runtime._extract_logistics(payload, "FIXTURE-1")
    assert detail["Scan_Flag_Keys"] == ["send"]
    payload["rows"][0]["FIXTURE-1"]["logistics"]["Logistics_Id"] = "WRONG"
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        runtime._extract_logistics(payload, "FIXTURE-1")


def test_ambiguous_exact_tracking_roots_cannot_supply_flags():
    root = {"FIXTURE-1": {"logistics": {"Logistics_Id": "FIXTURE-1"}, "smi": {"info": {"scanFlag": {"signed": True}}}}}
    with pytest.raises(ValueError, match="EMPTY_OR_AMBIGUOUS"):
        runtime._extract_logistics({"rows": [root, deepcopy(root)]}, "FIXTURE-1")


def test_tracking_flags_are_not_taken_from_unreviewed_logistics_field():
    detail = runtime._extract_logistics({"rows": [{"FIXTURE-1": {
        "logistics": {"Logistics_Id": "FIXTURE-1", "Scan_Flag_Keys": ["signed"]},
    }}]}, "FIXTURE-1")
    assert "Scan_Flag_Keys" not in detail


@pytest.mark.parametrize("scan_flags,expected", [
    ({"send": 0, "signed": False}, "signed"),
    ({"transfer": 0, "signed": None}, "signed"),
    ({"deliver": 0, "problem": 0, "signed": {}}, "signed"),
    ({"send": 0}, "in_transit"),
])
def test_special_line_uses_original_scan_flag_presence(scan_flags, expected):
    row, detail, original, _renderer = _sources()
    row.pop("Sign_State")
    row["Total_Cost_Money"] = "27.50"
    detail = runtime._extract_logistics({"rows": [{"FIXTURE-1": {
        "logistics": detail, "smi": {"info": {"scanFlag": scan_flags}},
    }}]}, "FIXTURE-1")
    record = normalize_record(row, detail, original, {}, target_date="2026-08-15", source="special_line")
    assert record["status"] == expected


@pytest.mark.parametrize("flags", [None, [], ["problem"], ["unrecognized"], "signed", [1]])
def test_missing_or_unknown_special_line_flags_cannot_publish(flags):
    row, detail, original, _renderer = _sources()
    row["Total_Cost_Money"] = "27.50"
    detail["Scan_Flag_Keys"] = flags
    with pytest.raises(ValueError, match="special-line"):
        normalize_record(row, detail, original, {}, target_date="2026-08-15", source="special_line")


def test_legacy_bitable_field_renames_preserve_field_id(monkeypatch):
    aliases = {canonical: names[-1] for canonical, names in sink.FIELD_ALIASES.items()}
    fields = [{"field_id": f"field-{index}", "field_name": aliases.get(name, name), "type": sink.FIELD_TYPES[name]} for index, name in enumerate(FIELD_NAMES)]
    calls = []

    def operation(action, params):
        calls.append((action, params))
        if action == "list_fields":
            return {"items": deepcopy(fields)}
        assert action == "update_field"
        original = next(item for item in fields if item["field_id"] == params["field_id"])
        assert original["field_name"] in sink.FIELD_ALIASES[params["field_name"]]
        return {"ok": True}

    monkeypatch.setattr(sink, "feishu_operation", operation)
    result = sink._ensure_fields("fixture-base", "fixture-table", {})
    assert result["ok"] is True
    assert result["field_name_map"] == {name: name for name in FIELD_NAMES}
    assert [action for action, _ in calls].count("update_field") == 4


def test_numeric_identity_column_converts_to_text_with_same_field_id(monkeypatch):
    fields = [{"field_id": f"field-{index}", "field_name": name, "type": sink.FIELD_TYPES[name]} for index, name in enumerate(FIELD_NAMES)]
    fields[0]["type"] = 2
    calls = []

    def operation(action, params):
        calls.append((action, params))
        if action == "list_fields":
            return {"items": deepcopy(fields)}
        assert action == "update_field"
        assert params["field_id"] == "field-0"
        assert params["field_name"] == sink.INDEX_FIELD_NAME
        assert params["type"] == 1
        return {"ok": True}

    monkeypatch.setattr(sink, "feishu_operation", operation)
    assert sink._ensure_fields("fixture", "fixture", {})["ok"] is True
    assert [action for action, _ in calls] == ["list_fields", "update_field"]


def test_nonidentity_field_type_mismatch_stops_record_write(monkeypatch):
    fields = [{"field_id": f"field-{index}", "field_name": name, "type": sink.FIELD_TYPES[name]} for index, name in enumerate(FIELD_NAMES)]
    next(item for item in fields if item["field_name"] == "录单运费")["type"] = 1
    calls = []

    def operation(action, _params):
        calls.append(action)
        assert action == "list_fields"
        return {"items": fields}

    monkeypatch.setattr(sink, "feishu_operation", operation)
    assert "录单运费" in sink._ensure_fields("fixture", "fixture", {})["error"]
    assert calls == ["list_fields"]


def test_missing_field_type_is_not_assumed_to_match(monkeypatch):
    monkeypatch.setattr(sink, "feishu_operation", lambda *_: {"items": [{"field_id": "fixture", "field_name": sink.INDEX_FIELD_NAME}]})
    assert "类型缺失" in sink._ensure_fields("fixture", "fixture", {})["error"]


@pytest.mark.parametrize("identities", [["DUPLICATE", "DUPLICATE"], ["", "VALID"]])
def test_existing_bitable_identity_error_stops_record_mutation(monkeypatch, identities):
    monkeypatch.setattr(adapters, "_exact_workflow_resource", lambda *_args, **_kwargs: {"base_token": "fixture", "table_id": "fixture"})
    monkeypatch.setattr(sink, "_ensure_fields", lambda *_args: {"ok": True, "field_name_map": {name: name for name in FIELD_NAMES}})
    calls = []

    def operation(action, _params):
        calls.append(action)
        assert action == "list_records"
        return {"data": {"items": [{"record_id": f"r-{i}", "fields": {sink.INDEX_FIELD_NAME: identity}} for i, identity in enumerate(identities)], "has_more": False}}

    monkeypatch.setattr(feishu_cli_tool, "feishu_operation", operation)
    with pytest.raises(PluginExecutionError) as error:
        adapters._replace_yunda_send_bitable("fixture", [], "2026-08-15", True)
    assert error.value.code == "BROKER_RESOURCE_INVALID"
    assert calls == ["list_records"]


def test_sheet_header_migration_preserves_original_positions_and_appends_new_fields(monkeypatch):
    old = [sink.FIELD_ALIASES.get(name, (name,))[-1] for name in FIELD_NAMES[:25]]
    state = [old]
    calls = []

    def operation(action, params):
        assert params["range"] == "Sheet1!A1:AD1"
        calls.append(action)
        if action == "write_sheet":
            state[:] = params["values"]
            return {"ok": True}
        return {"ok": True, "data": {"valueRange": {"values": deepcopy(state)}}}

    monkeypatch.setattr(feishu_cli_tool, "feishu_operation", operation)
    adapters._ensure_yunda_send_sheet_headers("fixture", "Sheet1")
    assert state == [list(FIELD_NAMES)]
    assert state[0][24] == "发件日期"
    assert calls == ["read_sheet", "write_sheet", "read_sheet"]
