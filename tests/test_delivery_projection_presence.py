"""Missing local rows are reported without weakening actual write verification."""
from copy import deepcopy

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.delivery_connectors_v2 import build_delivery_connectors
from agent.automation_plugins.delivery_site_handlers import build_delivery_site_handler_map
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from plugin_core_adapters import delivery_site
from shared.invocation_summary import invocation_count_summary
from tests.test_delivery_site_production_adapter import _Manager, _bitable_row, _exact_bitable_record, _projection_row
from tests.test_send_delivery_v2_packaged_protocol import _host


@pytest.mark.parametrize("existing", [[], ["WB-1"], ["WB-1", "WB-2"]])
@pytest.mark.parametrize("preview", [False, True])
def test_zip_reports_missing_rows_and_updates_only_existing(tmp_path, monkeypatch, existing, preview):
    bills = ["WB-1", "WB-2"]
    rows = {code: _projection_row(code, "in_transit") for code in existing}
    bitable = [_bitable_row("rec-" + code, code, "未签收") for code in bills]
    sql_writes, feishu_writes = [], []

    def read(codes):
        return [deepcopy(rows[code]) for code in codes if code in rows]

    def write(codes, status, marker):
        marker()
        sql_writes.append(list(codes))
        for code in codes:
            rows[code]["status"] = status
        return {"ok": True, "updated": len(codes)}

    def feishu(action, params):
        if action == "get_record":
            return _exact_bitable_record(bitable, params)
        assert action == "write_records"
        feishu_writes.extend(deepcopy(params["records"]))
        for update in params["records"]:
            match = [row for row in bitable if row["record_id"] == update["record_id"]]
            assert len(match) == 1
            match[0]["fields"].update(update["fields"])
        return {"ok": True, "written": len(params["records"])}

    monkeypatch.setattr(delivery_site, "_default_projection_read", read)
    manager = _Manager()
    readers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
        describe_account=manager.require_active_binding_descriptor,
        delivery_list_views=lambda *_: [{"view_id": "pending", "view_name": "未签收明细"}],
        delivery_list_records=lambda *_: {"items": [
            {"record_id": "rec-" + code, "waybill_no": code, "status": "未签收"} for code in bills
        ], "returned": len(bills), "total": len(bills)},
        delivery_status_read=lambda *_: [{"bill_code": code, "status": "已签收"} for code in bills],
        delivery_projection_lookup=delivery_site.read_delivery_projection_identities,
    ))
    readers.update(build_delivery_site_handler_map(delivery_site.build_production_delivery_site_ports(
        account_manager=manager, projection_read=read, projection_write=write, feishu_operation=feishu,
        resource_loader=lambda resource_id: {"resource_kind": "feishu_bitable", "base_token": "test-base",
            "table_id": "test-table", "_meta": {"resource_key": resource_id}},
    ), cursor_secret=b"delivery-presence-test-key-32bytes"))
    host = _host(tmp_path, "sync_delivery_status_v2", ConnectorRegistry(build_delivery_connectors(readers)),
                 "delivery", "delivery_status_bitable")
    result = host.execute({}, operation="preview" if preview else "run", entrypoint="harness" if preview else "console")
    assert result["status"] == "SUCCESS", result
    data = result["data"]
    assert data["not_in_database_bill_codes"] == sorted(set(bills) - set(existing))
    assert data["not_in_database_count"] == len(bills) - len(existing)
    assert data["database_updated"] == (0 if preview else len(existing))
    assert data["database_checked"] == len(bills)
    assert set(rows) == set(existing), "status sync must never create a waybill"
    if preview:
        assert not sql_writes and not feishu_writes and not host.receipts
    else:
        assert len(feishu_writes) == len(bills)
        assert sql_writes == ([existing] if existing else [])
        assert all(row["status"] == "signed" for row in rows.values())
        assert all(row["fields"]["签收状态"] == "已签收" for row in bitable)
        assert len(host.receipts) == (2 if existing else 1)
        assert f"未入库：{len(bills) - len(existing)} 条" in invocation_count_summary({"result": result})


@pytest.mark.parametrize("rows", [
    [{"waybill_no": "WB-1"}],
    [_projection_row("WB-1", "signed"), _projection_row("WB-1", "signed")],
    [_projection_row("OTHER", "signed")],
])
def test_presence_lookup_rejects_malformed_duplicate_and_extra_rows(monkeypatch, rows):
    monkeypatch.setattr(delivery_site, "_default_projection_read", lambda _: rows)
    with pytest.raises(PluginExecutionError, match="projection"):
        delivery_site.read_delivery_projection_identities(["WB-1"])


def test_presence_lookup_does_not_treat_database_failure_as_missing(monkeypatch):
    def fail(_):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(delivery_site, "_default_projection_read", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        delivery_site.read_delivery_projection_identities(["WB-1"])
