"""Real isolated SQL: signature updates neither insert missing bills nor alter cargo."""
import os

import pytest

from agent.automation_plugins.delivery_site_handlers import build_delivery_site_handler_map
from plugin_core_adapters.delivery_site import build_production_delivery_site_ports, read_delivery_projection_identities
from shared.waybill_source_coverage import WaybillSourceRepository
from tests.test_delivery_site_production_adapter import _Manager, _projection_context
from tests.test_waybill_source_coverage_mysql import CAPTURED, DAY, SCOPE, database, record, rows  # noqa: F401
from tools import phase7_mysql_store

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")


def test_real_sql_presence_and_status_update_preserve_missing_and_unrelated_rows(database, monkeypatch):
    WaybillSourceRepository(database).publish([record("WB-1", piece="2"), record("KEEP", piece="3")],
        scope=SCOPE, business_date=DAY, captured_at=CAPTURED, complete=True, expected_total=2)
    monkeypatch.setattr(phase7_mysql_store, "_connect", database)
    before = rows(database)
    existing = read_delivery_projection_identities(["WB-1", "MISSING"])
    assert existing == ["WB-1"]
    assert read_delivery_projection_identities(["MISSING"]) == []
    marks = []
    handlers = build_delivery_site_handler_map(build_production_delivery_site_ports(account_manager=_Manager()),
        cursor_secret=b"delivery-mysql-existing-only-test")
    result = handlers[("projection.invoke", "waybill.delivery_status.update")](
        _projection_context(mark_write_started=lambda: marks.append(True)), {"bill_codes": existing, "status": "signed"})
    assert result["committed"] and result["updated"] == 1 and marks == [True]
    after = rows(database)
    assert len(after) == len(before)
    for old, new in zip(before, after, strict=True):
        assert old["id"] == new["id"]
        expected = "signed" if old["waybill_no"] == "WB-1" else old["status"]
        assert new["status"] == expected
        assert {k: v for k, v in old.items() if k not in {"status", "updated_at"}} == {
            k: v for k, v in new.items() if k not in {"status", "updated_at"}}
