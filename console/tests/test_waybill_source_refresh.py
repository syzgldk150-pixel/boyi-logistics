"""Explicit backfill uses one in-flight call and reuses successful results."""
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


def test_complete_refresh_reused_until_exact_120_second_expiry(monkeypatch):
    app = build_app()
    app._agent_request = Mock(return_value={"ok": True, "data": {"complete": True}})
    clock = [1000.0]
    monkeypatch.setattr(refresh.time, "monotonic", lambda: clock[0])
    assert not run_refresh(app)[1]["data"]["reused"]
    clock[0] += 119.99
    assert run_refresh(app)[1]["data"]["reused"]
    clock[0] += 0.01
    assert not run_refresh(app)[1]["data"]["reused"]
    assert app._agent_request.call_count == 2


@pytest.mark.parametrize("failure", [{"ok": False}, {"ok": True, "data": {"complete": False}}, None, TimeoutError()])
def test_partial_invalid_or_failed_refresh_never_cached(failure):
    app = build_app()
    app._agent_request = Mock(side_effect=[failure, {"ok": True, "data": {"complete": True}}])
    first = run_refresh(app)[1]["data"]
    assert not first["complete"] and "仅代表已保存的服务器数据" in first["warnings"][0]
    second = run_refresh(app)[1]["data"]
    assert second["complete"] and not second["reused"]
    assert app._agent_request.call_count == 2


@pytest.mark.parametrize("plan,warning", [
    ({**PLAN, "date_from": "", "date_to": ""}, "未指定日期"),
    ({**PLAN, "date_from": "2026-09-01"}, "超过 31 天"),
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
