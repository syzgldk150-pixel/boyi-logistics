"""Real isolated pickup ZIP/Broker with deterministic R13 and Feishu servers."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.daily_sign_connectors_v2 import build_daily_sign_connectors
from plugin_core_adapters.daily_sign_ports import build_daily_sign_port_handlers
from service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_format import (
    QUANTITY_COLOR,
    QUANTITY_FORMULA,
)
from service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool import SHEET_HEADERS
from tests.daily_sign_format_fixture import SheetFormats
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tools.phase7_sync_common import parse_a1_range


PLUGIN_ID = "sync_self_pickup_should_sign_v2"
TRACKING = "R00021000001"


def source_row(**changes):
    return {
        "billNumberMain": TRACKING,
        "planSignTime": "2000-01-01 23:59:59",
        "isSigns": 0,
        "problemType": "隔离原页问题",
        "problemRegisterDate": "2000-01-01 09:31:03",
        "problemCause": "隔离原页内容",
        "problemRegisterSite": "隔离自提部",
        "goodsName": "隔离货物",
        "packTypeDesc": "纸箱",
        "pcs": 10,
        "dispAddress": "隔离测试地址",
        "dispatchMode": "自提",
        **changes,
    }


def arrival_state(quantities=(5, 8)):
    # A complete successful source run may contain no rows for this waybill.
    source_run_count = max(1, len(quantities))
    return {
        # These old facts must not supply the R13 publication set or fields.
        "ledger": {"R00021000099": {"tracking_number": "R00021000099"}},
        "arrivals": {TRACKING: [
            {"tracking_number": TRACKING, "business_date": f"2000-01-{index + 1:02d}",
             "expected_quantity": 10, "arrived_quantity": quantity, "run_id": f"fixture-{index}"}
            for index, quantity in enumerate(quantities)
        ]},
        "problems": {TRACKING: [{"tracking_number": TRACKING, "problem_type": "历史问题"}]},
        "signs": {},
        "sign_verifications": {},
        "target_station_codes": [],
        "source_refs": [],
        "arrival_source_proof": {
            "complete": True, "active_stat_runs": source_run_count,
            "latest_forecast_runs": 0, "run_ids": [f"fixture-{index}" for index in range(source_run_count)],
        },
    }


class PickupAccounts:
    def require_active_binding_descriptor(self, account_id, allowed_systems=None):
        assert account_id == "isolated-pickup-r13"
        assert allowed_systems is None or "r13" in allowed_systems
        return {"account_id": account_id, "system": "r13"}

    def resolve_role_account_params(self, values, **fields):
        assert values["r13_account_id"] == "isolated-pickup-r13"
        assert fields == {
            "account_field": "r13_account_id", "output_account_field": "",
            "output_session_profile_field": "",
        }
        return values


class PickupResources:
    def require_active(self, *, resource_id, allowed_kinds):
        assert resource_id == "isolated-pickup-sheet"
        assert "feishu_sheet" in allowed_kinds
        return {"resource_id": resource_id, "kind": "feishu_sheet"}

    def read(self, resource_id, *, kind, fields):
        self.require_active(resource_id=resource_id, allowed_kinds=[kind])
        assert fields == ("spreadsheet_token", "range")
        return {"spreadsheet_token": "isolated-daily-arrivals-book", "range": "自提部应签明细!A2:L6"}


class PickupSheet:
    def __init__(self, *, corrupt_readback=False, drop_format_write=False):
        self.sheet = [list(SHEET_HEADERS)] + [[f"stale-{index}"] + [""] * 11 for index in range(5)]
        self.calls = []
        self.formats = SheetFormats()
        self.corrupt_readback = corrupt_readback
        self.drop_format_write = drop_format_write

    @property
    def writes(self):
        return [name for name, _ in self.calls if name in {"write_sheet", "clear_sheet", "write_sheet_format"}]

    def __call__(self, name, values):
        self.calls.append((name, deepcopy(values)))
        assert values["spreadsheet_token"] == "isolated-daily-arrivals-book"
        cells = parse_a1_range(values["range"])
        assert cells["sheet"] == "自提部应签明细"
        assert cells["start_col"] == "A" and cells["end_col"] == "L"
        if name in {"read_sheet_formats", "write_sheet_format"}:
            if name == "write_sheet_format" and self.drop_format_write:
                return {"ok": True}
            return self.formats(name, values)
        start, end = cells["start_row"] - 1, cells["end_row"]
        if name == "read_sheet":
            rows = deepcopy(self.sheet[start:end])
            if self.corrupt_readback and "write_sheet" in self.writes and rows:
                rows[0][-1] = 999
            return {"ok": True, "data": {"valueRange": {"values": rows}}}
        if name == "write_sheet":
            self.sheet[start:end] = deepcopy(values["values"])
            return {"ok": True, "rows": len(values["values"])}
        if name == "clear_sheet":
            self.sheet[start:end] = [[""] * 12 for _ in range(end - start)]
            return {"ok": True}
        raise AssertionError(f"Unexpected Feishu operation: {name}")


def make_host(tmp_path, *, response=None, state=None, sheet=None):
    accounts, resources = PickupAccounts(), PickupResources()
    sheet = sheet if sheet is not None else PickupSheet()
    state = state if state is not None else arrival_state()
    response = response if response is not None else {"data": [source_row()]}
    source_calls, state_calls = [], []

    def tms(endpoint, values):
        source_calls.append((endpoint, deepcopy(values)))
        assert endpoint == "/get_qianshou"
        assert values["r13_account_id"] == "isolated-pickup-r13"
        assert values["include_history"] is True
        assert values["fetch_all"] is True and values["page"] == 1
        return deepcopy(response)

    def load_daily_sign_state(**values):
        state_calls.append(deepcopy(values))
        return deepcopy(state)

    # No global ledger/run mutation method exists on this test store. Any use
    # of one fails through the actual production port, instead of being mocked.
    ports = build_daily_sign_port_handlers(
        account_manager=accounts, store=SimpleNamespace(load_daily_sign_state=load_daily_sign_state),
        tms=tms, feishu=sheet, resource_reader=resources.read,
    )
    context = CoreBrokerInvocationContext(
        automation_id="isolated-pickup-daily-sign", plugin_version="1.0.0", tool_name=PLUGIN_ID,
        operation="service.invoke", action="run", role="__system__",
        account_bindings={"daily_sign_r13": ("isolated-pickup-r13",)},
        resource_bindings={"daily_sign_sheet": "isolated-pickup-sheet"},
    )
    host = PackagedConnectorHost(
        tmp_path, PLUGIN_ID, ConnectorRegistry(build_daily_sign_connectors(ports)), context,
        account_resolver=accounts, resource_resolver=resources,
    )
    return host, sheet, source_calls, state_calls


def test_pickup_zip_publishes_exact_twelve_columns_and_only_bound_sheet(tmp_path):
    host, sheet, source_calls, state_calls = make_host(tmp_path)
    other_format = {"rule_id": "unrelated", "properties": {"rule_type": "containsBlanks", "ranges": ["C2:C5"]}}
    sheet.formats.rules.append(deepcopy(other_format))
    result = host.execute({}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert host.verified_outcome.accepted is True
    assert len(source_calls) == 1
    assert state_calls == [{"tracking_numbers": [TRACKING]}]
    assert sheet.sheet[:2] == [list(SHEET_HEADERS), [
        TRACKING, "2000-01-01 23:59:59", "隔离原页问题", "2000-01-01 09:31:03",
        "隔离原页内容", "隔离自提部", "隔离货物", "纸箱", 10, "隔离测试地址", "自提", 8,
    ]]
    assert all(not any(row) for row in sheet.sheet[2:])
    assert sheet.writes == ["write_sheet", "clear_sheet", "write_sheet_format"]
    assert result["data"]["sheet_rows"] == 1
    assert result["data"]["sheet_readback"]["verified"] is True
    assert result["data"]["sheet_format_readback"]["verified"] is True
    assert sheet.formats.rules[0] == other_format
    assert sheet.formats.rules[1]["properties"] == {
        "rule_type": "expression", "attrs": [{"formula": [QUANTITY_FORMULA]}],
        "style": {"back_color": QUANTITY_COLOR}, "ranges": ["A2:L2"],
    }
    assert result["meta"]["write_outcome"] == "WRITE_VERIFIED"

    # The second invocation still re-queries R13/state and checks the current
    # sheet; an identical current snapshot requires no new mutations.
    before = len(sheet.calls)
    second = host.execute({}, operation="run")
    assert second["status"] == "SUCCESS", second
    assert len(source_calls) == 2 and len(state_calls) == 2
    assert second["data"]["sheet_unchanged"] is True
    assert second["data"]["sheet_written"] == 0
    assert not any(name in {"write_sheet", "clear_sheet", "write_sheet_format"} for name, _ in sheet.calls[before:])

    assert {role["role"] for role in host.raw["account_roles"]} == {"daily_sign_r13"}
    assert {role["role"] for role in host.raw["resource_roles"]} == {"daily_sign_sheet"}
    assert {item["service"] for item in host.raw["requires"]} == {
        "connector.boyi.daily_sign_r13@1", "connector.boyi.daily_sign_store@1", "connector.boyi.daily_sign_sheet@1",
    }
    allowed_operations = {operation for capability in host.raw["capabilities"] for operation in capability["operations"]}
    assert allowed_operations == {
        "read_r13", "describe", "load_state", "read_sheet", "write_sheet", "clear_sheet",
        "read_sheet_formats", "write_sheet_format",
    }
    assert host.raw["storage"] == {"kv": False, "collections": []}


@pytest.mark.parametrize("quantities,expected", [
    ((5, 8), 8), ((10, 0), 0), ((10, None), "无数据"), ((), "无数据"),
])
def test_pickup_zip_uses_latest_cumulative_quantity_including_zero_and_no_data(tmp_path, quantities, expected):
    host, sheet, _, _ = make_host(tmp_path, state=arrival_state(quantities))
    result = host.execute({}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert sheet.sheet[1][-1] == expected


@pytest.mark.parametrize("case,expected_code", [
    ("source_failed", "SOURCE_QUERY_FAILED"),
    ("source_payload_missing", "SOURCE_RESPONSE_INVALID"),
    ("tracking_missing", "SOURCE_FIELD_MISSING"),
    ("plan_time_missing", "SOURCE_FIELD_MISSING"),
    ("problem_content_missing", None),
    ("source_duplicate_conflict", "SOURCE_DUPLICATE_CONFLICT"),
    ("arrival_proof_incomplete", None),
    ("arrival_duplicate_conflict", None),
])
def test_pickup_zip_incomplete_or_conflicting_sources_never_write(tmp_path, case, expected_code):
    response, state = {"data": [source_row()]}, arrival_state()
    if case == "source_failed":
        response = {"ok": False, "error_code": "SOURCE_QUERY_FAILED"}
    elif case == "source_payload_missing":
        response = {"ok": True}
    elif case in {"tracking_missing", "plan_time_missing", "problem_content_missing"}:
        field = {"tracking_missing": "billNumberMain", "plan_time_missing": "planSignTime",
                 "problem_content_missing": "problemCause"}[case]
        del response["data"][0][field]
    elif case == "source_duplicate_conflict":
        response["data"].append(source_row(problemCause="冲突原页内容"))
    elif case == "arrival_proof_incomplete":
        state["arrival_source_proof"]["complete"] = False
    elif case == "arrival_duplicate_conflict":
        state["arrivals"][TRACKING].append({**state["arrivals"][TRACKING][-1], "arrived_quantity": 9})
    host, sheet, _, _ = make_host(tmp_path, response=response, state=state)
    before = deepcopy(sheet.sheet)
    result = host.execute({}, operation="run")
    assert result["status"] == "FAILED", result
    if expected_code is not None:
        assert result["error"]["code"] == expected_code
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"
    assert not host.receipts and not sheet.writes
    assert sheet.sheet == before
    assert host.verified_outcome.accepted is False


def test_pickup_zip_authoritative_zero_clears_stale_rows_and_reads_them_back(tmp_path):
    host, sheet, source_calls, state_calls = make_host(tmp_path, response={"data": []})
    result = host.execute({}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert len(source_calls) == 1
    assert state_calls == [{"tracking_numbers": []}]
    assert sheet.writes == ["clear_sheet"]
    assert sheet.sheet[0] == list(SHEET_HEADERS)
    assert all(not any(row) for row in sheet.sheet[1:])
    assert result["data"]["sheet_rows"] == 0
    assert result["data"]["sheet_readback"]["verified"] is True
    assert result["data"]["sheet_readback"]["record_count"] == 0
    names = [name for name, _ in sheet.calls]
    assert names.index("clear_sheet") < len(names) - 1
    assert names[-1] == "read_sheet"


@pytest.mark.parametrize("failure", ["sheet_values", "conditional_format"])
def test_pickup_zip_unverified_write_never_reports_success_or_replays_mutation(tmp_path, failure):
    sheet = PickupSheet(corrupt_readback=failure == "sheet_values", drop_format_write=failure == "conditional_format")
    host, _, _, _ = make_host(tmp_path, sheet=sheet)
    result = host.execute({}, operation="run")
    assert result["status"] == "FAILED", result
    assert result["error"]["code"] == "WRITE_OUTCOME_UNKNOWN", result
    assert result["meta"]["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert host.verified_outcome.accepted is False
    assert host.receipts
    assert sheet.writes.count("write_sheet") == 1
    assert sheet.writes.count("clear_sheet") == 1
    assert sheet.writes.count("write_sheet_format") == (failure == "conditional_format")
