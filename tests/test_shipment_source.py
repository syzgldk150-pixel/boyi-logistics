"""Native HTTP boundary fixtures; real pagination, binding and metric code."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from agent.tms_runtime.scripts import Send_order
from plugin_core_adapters import shipment_source as source
from shared.shipment_metrics import ShipmentQueryError


DAY = date(2026, 9, 24)
STATION = "邵阳大祥站"


def row(identity="R12345678901", weight="1250.5", **changes):
    return {"BILL_CODE": identity, "FEE_WEIGHT": weight, "BILL_WEIGHT": "99",
            "REGISTER_DATE": "2026-09-24 10:00:00", "SEND_SITE_CODE": "site-a",
            "SEND_SITE": STATION, "DESTINATION": "测试目的网点", **changes}


def arguments():
    return {"station": STATION, "period": "range", "start_date": DAY.isoformat(),
            "end_date": DAY.isoformat(), "group_by": "total", "comparison": "none"}


class Manager:
    def __init__(self):
        self.accounts = [{"account_id": "chosen", "system": "ronghui", "account_purpose": "price", "is_active": True},
                         {"account_id": "other", "system": "ronghui", "account_purpose": "daxiang_s", "is_active": True}]
        self.profile = "explicit-profile"

    def list_accounts(self, **kwargs):
        assert kwargs == {"include_status": False}
        return self.accounts

    def require_active_binding_descriptor(self, identity):
        return {"account_id": identity, "system": "ronghui", "session_profile": self.profile}


@pytest.fixture
def native(monkeypatch):
    state = SimpleNamespace(rows=[row()], totals=None, requests=[], site=("site-a", STATION))
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
    def session(descriptor):
        assert descriptor["account_id"] == "chosen"
        return Session()
    def fetch(_session, dates, **kwargs):
        assert dates["start"] == "2026/09/24 00:00:00"
        assert kwargs["extra_filters"] == {"SEND_SITE_CODE": "site-a"}
        state.requests.append(kwargs)
        page = kwargs["page_index"]
        total = state.totals[page] if state.totals else len(state.rows)
        return {"total": total, "data": state.rows[page*100:(page+1)*100]}
    monkeypatch.setattr(source, "source_session", session)
    monkeypatch.setattr(source, "read_ronghui_site", lambda _: state.site)
    monkeypatch.setattr(Send_order, "fetch_send_orders", fetch)
    return state


def test_complete_native_pages_chargeable_weight_and_refresh_removal(native):
    native.rows = [row("R" + str(12345678901+i)) for i in range(101)]
    accounts, query = source.prepare_shipment_query(STATION, manager=Manager())
    assert accounts == ("chosen",)
    result = query(arguments())
    assert result["ok"] is True
    assert result["current"]["tickets"] == 101
    assert result["current"]["weight_kg"] == "126300.5"
    assert [call["page_index"] for call in native.requests] == [0, 1]
    native.rows = [row()]
    assert query(arguments())["current"]["tonnes"] == "1.2505"
    native.rows = []
    empty = query(arguments())
    assert empty["current"]["tickets"] == 0 and empty["current"]["status"] == "zero"


@pytest.mark.parametrize("changes,code", [
    ({"FEE_WEIGHT": None}, "SHIPMENT_WEIGHT_MISSING"),
    ({"SEND_SITE_CODE": "another"}, "SHIPMENT_SCOPE_MISMATCH"),
    ({"REGISTER_DATE": "2026-09-23 23:59:59"}, "SHIPMENT_SCOPE_MISMATCH"),
])
def test_missing_weight_and_wrong_scope_fail(native, changes, code):
    native.rows = [row(**changes)]
    _, query = source.prepare_shipment_query(STATION, manager=Manager())
    assert query(arguments())["code"] == code


def test_page_total_drift_and_duplicate_fail(native):
    native.rows = [row("R" + str(12345678901+i)) for i in range(101)]
    native.totals = [101, 102]
    _, query = source.prepare_shipment_query(STATION, manager=Manager())
    assert query(arguments())["ok"] is False
    native.totals = None
    native.rows = [row(), row()]
    assert query(arguments())["ok"] is False


def test_accounts_are_exact_and_rechecked(native):
    manager = Manager()
    _, query = source.prepare_shipment_query(STATION, manager=manager)
    manager.profile = "changed"
    assert query(arguments())["code"] == "SHIPMENT_SCOPE_MISMATCH"
    manager.accounts.append(manager.accounts[0].copy())
    with pytest.raises(ShipmentQueryError, match="不唯一"):
        source.prepare_shipment_query(STATION, manager=manager)
    with pytest.raises(ShipmentQueryError):
        source.prepare_shipment_query("unknown", manager=Manager())


def test_real_site_mismatch_and_postread_change_fail(native, monkeypatch):
    _, query = source.prepare_shipment_query(STATION, manager=Manager())
    native.site = ("site-b", "邵阳大祥S站")
    assert query(arguments())["code"] == "SHIPMENT_SCOPE_MISMATCH"
    observations = iter([("site-a", STATION), ("site-b", STATION)])
    monkeypatch.setattr(source, "read_ronghui_site", lambda _: next(observations))
    assert query(arguments())["code"] == "SHIPMENT_SCOPE_MISMATCH"


def test_receipt_and_child_use_existing_tracking_rules():
    from shared.shipment_metrics import aggregate, BUSINESS_ZONE
    rows = [row(), row("H12345678901"), row("R123456789010001")]
    day = source.shipment_day(rows, len(rows), day=DAY, site=("site-a", STATION), captured_at=datetime.now(timezone.utc))
    result = aggregate([day], start=datetime(2026, 9, 24, tzinfo=BUSINESS_ZONE),
                       end=datetime(2026, 9, 25, tzinfo=BUSINESS_ZONE), group_by="total")
    assert result["tickets"] == 1 and result["excluded"]["receipt"] == result["excluded"]["child"] == 1


def test_harness_composition_binds_selected_account_and_respects_release_hold(native, monkeypatch):
    import asyncio
    from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
    from agent.orchestration.models import OrchestrationError
    from harness_composition import build_read_only_harness_gateway

    manager = Manager()
    monkeypatch.setattr("agent.tms_runtime.account_manager.get_account_manager", lambda: manager)
    async def exercise():
        held = [True]
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: held[0])
        runtime = SimpleNamespace(memory=SimpleNamespace(connection_factory=lambda: None))
        repository = SimpleNamespace(list_work_items=lambda **kw: [], get_run=lambda _: None, get_evidence=lambda _: None)
        gateway = build_read_only_harness_gateway(runtime, repository, invocations=lifecycle)
        query = gateway.handlers()["shipment.query"]
        with pytest.raises(OrchestrationError):
            await asyncio.to_thread(query, arguments())
        assert not native.requests
        original_fetch = Send_order.fetch_send_orders
        def fetch(*args, **kwargs):
            with pytest.raises(OrchestrationError):
                lifecycle.begin_credentials_change("chosen")
            return original_fetch(*args, **kwargs)
        monkeypatch.setattr(Send_order, "fetch_send_orders", fetch)
        held[0] = False
        result = await asyncio.to_thread(query, arguments())
        assert result["ok"] and result["current"]["tonnes"] == "1.2505"
        assert lifecycle.active_read_count() == 0
    asyncio.run(exercise())
