"""Every explicit query reaches the platforms; only concurrent identical requests share one call."""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock

import pytest

from console.tests.test_manual_waybill_edit import JsonHandler, build_app
from console.services import waybills_receipts as refresh

PLAN = {"source": "yunda", "date_from": "2026-10-13", "date_to": "2026-10-13"}


@pytest.fixture(autouse=True)
def clear_flights():
    refresh._WAYBILL_REFRESH_FLIGHTS.clear()
    yield
    refresh._WAYBILL_REFRESH_FLIGHTS.clear()


def run_refresh(app, plan=None, **handler_options):
    handler = JsonHandler(PLAN if plan is None else plan, **handler_options)
    app._handle_waybill_source_refresh(handler)
    return handler.status, handler.result()


def test_refresh_calls_direct_business_with_real_principal():
    app = build_app()
    app._agent_request = Mock(return_value={"ok": True, "data": {"complete": True}})
    status, result = run_refresh(app)
    assert status == 200 and result["data"]["complete"] and not result["data"]["reused"]
    args, kwargs = app._agent_request.call_args
    assert args == ("POST", "/internal/v1/business/send-waybills-query")
    assert kwargs["payload"]["params"] == PLAN
    assert kwargs["console_principal"]["actor_id"] == "7"


def test_concurrent_identical_refresh_shares_one_agent_call():
    app = build_app()
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    original_flight = refresh._waybill_refresh_flight

    def select_flight(key):
        flight, owner = original_flight(key)
        if not owner:
            joined.set()
        return flight, owner

    def query(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "data": {"complete": True}}

    app._agent_request = Mock(side_effect=query)
    from unittest.mock import patch
    with patch.object(refresh, "_waybill_refresh_flight", side_effect=select_flight), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_refresh, app)
        assert entered.wait(5)
        second = pool.submit(run_refresh, app)
        try:
            assert joined.wait(5)
        finally:
            release.set()
        results = [first.result()[1]["data"], second.result()[1]["data"]]
    assert app._agent_request.call_count == 1
    assert [result["reused"] for result in results] == [False, True]
    assert all(result["complete"] for result in results)


def test_completed_query_is_never_reused_by_a_later_query():
    app = build_app()
    app._agent_request = Mock(return_value={"ok": True, "data": {"complete": True}})
    first = run_refresh(app)[1]["data"]
    second = run_refresh(app)[1]["data"]
    assert not first["reused"] and not second["reused"]
    assert (first["date_from"], first["date_to"]) == (PLAN["date_from"], PLAN["date_to"])
    assert app._agent_request.call_count == 2


def test_missing_dates_query_today(monkeypatch):
    app = build_app()
    app._agent_request = Mock(return_value={"ok": True, "data": {"complete": True}})
    monkeypatch.setattr(type(app), "_waybill_default_date", staticmethod(lambda: "2026/10/13"))
    result = run_refresh(app, {**PLAN, "date_from": "", "date_to": ""})[1]["data"]
    assert result["complete"] and (result["date_from"], result["date_to"]) == ("2026-10-13", "2026-10-13")
    assert app._agent_request.call_args.kwargs["payload"]["params"] == PLAN


@pytest.mark.parametrize("failure", [{"ok": False}, {"ok": True, "data": {"complete": False}}, None, TimeoutError()])
def test_partial_invalid_or_failed_refresh_never_cached(failure):
    app = build_app()
    app._agent_request = Mock(side_effect=[failure, {"ok": True, "data": {"complete": True}}])
    first = run_refresh(app)[1]["data"]
    assert not first["complete"] and "没有完整返回" in first["warnings"][0]
    second = run_refresh(app)[1]["data"]
    assert second["complete"] and not second["reused"]
    assert app._agent_request.call_count == 2


@pytest.mark.parametrize("plan,warning", [
    ({**PLAN, "date_from": "2026-09-01"}, "最多按 31 天"),
    ({**PLAN, "date_from": "2026-10-14"}, "不能晚于"),
    ({**PLAN, "date_from": "2026-02-30"}, "日期格式无效"),
])
def test_unavailable_date_range_is_explained_without_agent_call(plan, warning):
    app = build_app()
    app._agent_request = Mock()
    status, result = run_refresh(app, plan)
    assert status == 200 and not result["data"]["refreshed"]
    assert warning in result["data"]["warnings"][0]
    app._agent_request.assert_not_called()


@pytest.mark.parametrize("user,origin", [
    ({"id": 7, "username": "fixture", "is_legacy_basic_auth": True}, "http://console.test"),
    ({"id": 7, "username": "fixture"}, "http://other.test"),
])
def test_refresh_requires_same_origin_mysql_session(user, origin):
    app = build_app()
    app._agent_request = Mock()
    status, _ = run_refresh(app, user=user, origin=origin)
    assert status == 403
    app._agent_request.assert_not_called()


@pytest.mark.parametrize("plan", [{**PLAN, "source": "unknown"}, {**PLAN, "date_from": []}, {**PLAN, "extra": "x"}])
def test_refresh_rejects_invalid_closed_contract(plan):
    app = build_app()
    app._agent_request = Mock()
    assert run_refresh(app, plan)[0] == 400
    app._agent_request.assert_not_called()


def test_failed_platform_is_named_with_its_reason():
    app = build_app()
    app._agent_request = Mock(return_value={"ok": False, "error_code": "WAYBILL_SOURCE_QUERY_FAILED", "data": {
        "complete": False, "errors": [
            {"source": "yunda", "code": "WAYBILL_SOURCE_LOGIN_REQUIRED", "reason": "TMSAuthStateError"},
            {"source": "ronghui", "code": "WAYBILL_SOURCE_QUERY_FAILED", "reason": "ConnectionError"}]}})
    warnings = run_refresh(app, {**PLAN, "source": "all"})[1]["data"]["warnings"]
    assert warnings[0].startswith("韵达账号未登录") and "TMSAuthStateError" in warnings[0]
    assert warnings[1] == "融辉查询失败（WAYBILL_SOURCE_QUERY_FAILED，ConnectionError）"
