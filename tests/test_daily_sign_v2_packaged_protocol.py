"""Real isolated package + real MySQL, with deterministic platform test servers."""
from copy import deepcopy
import base64
from datetime import date, datetime
from types import SimpleNamespace
import os
import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.daily_sign_connectors_v2 import build_daily_sign_connectors
from plugin_core_adapters.daily_sign_ports import build_daily_sign_port_handlers
from tests.direct_invocation_fixture import direct_repository  # noqa: F401
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tools import daily_sign_store as store
from tools.daily_sign_sync_tool import SHEET_HEADERS


class BoundAccounts:
    def require_active_binding_descriptor(self, account_id, allowed_systems=None):
        system = {"test-r13":"r13", "test-tms":"ronghui"}[account_id]
        assert allowed_systems is None or system in allowed_systems
        return {"account_id":account_id, "system":system}

    def resolve_role_account_params(self, values, **_):
        assert values["r13_account_id"] == "test-r13"
        return values


class BoundResources:
    def require_active(self, *, resource_id, allowed_kinds):
        kind = {"test-sheet":"feishu_sheet", "test-bitable":"feishu_bitable"}[resource_id]
        assert kind in allowed_kinds
        return {"resource_id":resource_id, "kind":kind}

    def read(self, resource_id, *, kind, fields):
        self.require_active(resource_id=resource_id, allowed_kinds=[kind])
        values = {"spreadsheet_token":"test-sheet-target", "range":"test-sheet-tab!A2:I10"} if kind == "feishu_sheet" else {"base_token":"test-base", "table_id":"test-table"}
        assert all(field in values for field in fields)
        return values


class FeishuTables:
    def __init__(self, *, corrupt=False):
        self.records = []
        self.sheet = [list(SHEET_HEADERS)] + [[""]*9 for _ in range(9)]
        self.calls = []
        self.corrupt = corrupt

    def __call__(self, name, values):
        self.calls.append(name)
        if name == "list_fields":
            return {"items":[{"field_id":f"field-{index}", "field_name":field, "type":2 if field in {"货物件数", "到货件数"} else 1} for index, field in enumerate(SHEET_HEADERS)]}
        if name == "list_records":
            items = [{**deepcopy(row), "data": list(row["fields"].values())} for row in self.records]
            return {"ok":True, "identity":"bot", "items":items,
                "data":{"items":items, "has_more":False, "query_context":{"revision":{"id":"test"}}}}
        if name == "write_records":
            self.records = [{"record_id":f"record-{index}", **deepcopy(row)} for index,row in enumerate(values["records"])]
            if self.corrupt:
                self.records[0]["fields"]["到货件数"] = 999
            return {"ok":True, "written":len(self.records),
                "results":[{"result":{"data":{"record":{"fields":row["fields"]}}}} for row in self.records]}
        if name in {"read_sheet", "write_sheet", "clear_sheet"}:
            from tools.phase7_sync_common import parse_a1_range
            info = parse_a1_range(values["range"])
            assert info["sheet"] == "test-sheet-tab"
            start, end = info["start_row"]-1, info["end_row"]
            if name == "read_sheet":
                return {"ok":True, "identity":"bot", "data":{"spreadsheetToken":values["spreadsheet_token"],
                    "valueRange":{"range":values["range"], "majorDimension":"ROWS", "values":deepcopy(self.sheet[start:end])}}}
            rows = values["values"] if name == "write_sheet" else [[""]*9 for _ in range(end-start)]
            self.sheet[start:end] = deepcopy(rows)
            return {"ok":True, "range":values["range"], "results":[{"data":{"spreadsheetToken":values["spreadsheet_token"]}}]}
        raise AssertionError(f"Unexpected Feishu operation {name}")


@pytest.mark.parametrize("count,corrupt", [(514,False), (2,True), (2,False)])
def test_daily_sign_zip_calculates_and_publishes_verified_mysql_snapshot(tmp_path, direct_repository, monkeypatch, count, corrupt):  # noqa: F811
    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1" and os.environ["AGENT_DB_NAME"].endswith("_test")
    # This UUID database belongs only to this test module. Reset its daily-sign
    # inputs between scenarios so the corruption case has no historical data.
    with store._connect() as connection, connection.cursor() as cursor:
        for table in ("daily_sign_ledger", "waybill_problem_events", "waybill_sign_events", "waybill_sign_verification_state"):
            cursor.execute(f"DELETE FROM {table}")
    monkeypatch.setattr(store, "business_now", lambda: datetime(2026,9,11,12))
    arrivals = [{"tracking_number":code, "destination_station":"邵阳大祥S站", "expected_quantity":3,
        "arrived_quantity":2, "goods_name":"隔离货物", "package_type":"纸箱", "delivery_method":"派送",
        "recipient_address":"湖南省邵阳市大祥区隔离测试路1号"} for code in (f"R{21000000+index:011d}" for index in range(1,count+1))]
    historical = count == 2 and not corrupt
    identity = base64.b64encode(b"\xff" * 16).decode()
    problem = {"external_id":identity, "waybill_no":"R00021000002", "problem_type":"少货/分批",
        "registered_at":"2026-09-10 12:00:00", "registered_site":"邵阳大祥S站", "source_direction":"registered",
        "account_id":"host-private-account", "raw":{"FILE_PATH":"/host/private/attachment"}}
    if historical:
        arrivals[0]["recipient_address"] = "/湖南省邵阳市隔离测试路1号"
        store.upsert_problem_events([{"source":"ronghui_problem:abcdef123456", "external_id":identity,
            "tracking_number":"R00021000002", "problem_type":"少货/分批",
            "registered_at":"2026-09-10 12:00:00", "upload_complete":True, "payload":problem}])
    store.save_arrival_stat_snapshot(date(2026,9,10), arrivals)
    source_calls = []

    def tms(endpoint, values):
        source_calls.append(endpoint)
        if endpoint == "/get_qianshou":
            assert values["r13_account_id"] == "test-r13"
            return {"data":[{"billNumberMain":row["tracking_number"], "planSignTime":"2026-09-11 23:59:59"} for row in arrivals]}
        assert values["params"]["account_id"] == "test-tms"
        if endpoint == "/customer_service_problem":
            rows = [problem] if historical else []
            return {"ok":True, "data":{"ok":True, "account_id":"test-tms", "account_label":"Host account",
                "rows":rows, "stats":{"total":len(rows), "returned":len(rows), "total_authoritative":True}}}
        if endpoint == "/get_sign_records":
            return {"data":[{"扫描单号":"R00021000002", "扫描类型":"签收", "扫描时间":"2026-09-11 10:00:00", "扫描网点":"邵阳大祥S站"}]}
        raise AssertionError(endpoint)

    accounts, resources, tables = BoundAccounts(), BoundResources(), FeishuTables(corrupt=corrupt)
    reviewed = build_daily_sign_port_handlers(account_manager=accounts, store=store, tms=tms, feishu=tables, resource_reader=resources.read)
    registry = ConnectorRegistry(build_daily_sign_connectors(reviewed))
    context = CoreBrokerInvocationContext(automation_id="isolated-daily-sign", plugin_version="2.0.0", tool_name="sync_daily_should_sign_v2",
        operation="service.invoke", action="run", role="__system__",
        account_bindings={"daily_sign_r13":("test-r13",), "daily_sign_tms":("test-tms",)},
        resource_bindings={"daily_sign_sheet":"test-sheet", "daily_sign_bitable":"test-bitable"})
    host = PackagedConnectorHost(tmp_path, "sync_daily_should_sign_v2", registry, context, account_resolver=accounts, resource_resolver=resources)
    result = host.execute({"days":1}, operation="run")
    if corrupt:
        assert result["status"] == "FAILED" and result["error"]["code"] == "WRITE_OUTCOME_UNKNOWN", result
        assert tables.calls.count("write_records") == 1
        assert "write_sheet" not in tables.calls
        assert result["meta"]["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
        return
    assert result["status"] == "SUCCESS", (result.get("error"), source_calls, tables.calls)
    assert len(tables.records) == count-1
    assert tables.records[0]["fields"]["运单编号"] == "R00021000001"
    assert tables.records[0]["fields"]["到货件数"] == 2
    assert tables.sheet[1][0] == "R00021000001" and tables.sheet[1][-1] == 2
    state = store.load_daily_sign_state()
    assert state["ledger"]["R00021000002"]["tms_signed"]
    assert state["ledger"]["R00021000001"]["arrived_quantity"] == 2
    if historical:
        assert state["ledger"]["R00021000001"]["recipient_address"] == arrivals[0]["recipient_address"]
        assert any(row["external_id"] == identity for row in state["problems"]["R00021000002"])
    assert result["meta"]["write_outcome"] == "WRITE_VERIFIED"
