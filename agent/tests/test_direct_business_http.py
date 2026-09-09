"""Actual localhost HTTP across chat/Console, capability, route and dispatcher.

External TMS I/O alone is replaced by a controlled provider fixture. This is
transport/boundary evidence, not a claim that a production TMS query was run.
"""
import asyncio
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import uvicorn
from fastapi import FastAPI

from agent.direct_readers import invoke_registered_reader
from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.tms_runtime.direct_execution import call_blocking
from agent.orchestration.models import Actor, ActorType
from agent.tool_registry import ToolRegistry
from agent.tms_runtime import dispatch, routes
from agent.tms_runtime.direct_business import DirectBusinessService
from console.services.agent_api import AgentApiServiceMixin
from tools import query_tool, tms_tool, track_waybill_tool


@pytest.fixture
def business_http():
    registry = ToolRegistry()
    service = DirectBusinessService(registry=registry, invocation_service=DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False))
    routes.bind_direct_business_service(service)
    app = FastAPI()
    @app.middleware("http")
    async def isolated_principal(request, call_next):
        if request.url.path.startswith("/internal/v1/business/"):
            request.state.console_principal = {"actor_id": "isolated-admin", "roles": ["super_admin"]}
        return await call_next(request)
    app.include_router(routes.router)
    app.include_router(routes.router, prefix="/internal/v1")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    calls = []
    def provider(params):
        calls.append(params)
        if "bill_codes" in params:
            return [{"waybill_no": code, "status": "fixture-status"} for code in params["bill_codes"]]
        result = {"type": "waybill", "waybill_no": params["tracking_number"]}
        if params["tracking_number"] == "R12345678901":
            result.update(route_rows=[{"scan_time": "2026-09-09 10:00:00", "site_name": "隔离网点", "status": "已到达"}],
                arrival_progress={"expected_quantity": 2, "arrived_quantity": 2},
                waybill_stub={"recipient_name": "隔离收件人", "recipient_phone": "13800000000"})
        return result
    try:
        with patch.object(dispatch, "resolve_account_params", side_effect=lambda params, **kwargs: {**params, "account_id": "isolated-provider"}), \
             patch.object(dispatch, "_load_callable", return_value=provider), \
             patch.object(query_tool, "HTTP_SERVICE_URL", f"http://127.0.0.1:{port}/tms"), \
             patch.object(tms_tool, "HTTP_SERVICE_URL", f"http://127.0.0.1:{port}/tms"):
            yield registry, f"http://127.0.0.1:{port}", calls
    finally:
        routes.bind_direct_business_service(None)
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_chat_fixed_read_preserves_scope_through_actual_http_and_formatter_data(business_http):
    registry, _, calls = business_http
    actor = Actor(ActorType.CONSOLE_ADMIN, "isolated-admin", ("super_admin",), authenticated_by="mysql_admin_session")
    result = asyncio.run(invoke_registered_reader(catalog=registry, name="query_waybill",
        arguments={"waybill_no": "isolated-001", "query_type": "status"},
        handler=lambda params: query_tool.query_status([params["waybill_no"]]), actor=actor, source="console"))
    assert result["success"] is True, result
    assert result["data"] == {"query_type": "status", "records": [{"waybill_no": "isolated-001", "status": "fixture-status"}], "count": 1}
    assert calls == [{"bill_codes": ["isolated-001"], "account_id": "isolated-provider"}]
    assert "run_id" not in result


def test_console_actual_http_returns_single_business_envelope(business_http):
    _, base_url, calls = business_http
    console = AgentApiServiceMixin()
    console.settings = SimpleNamespace(agent_base_url=base_url, agent_timeout_seconds=5, agent_internal_api_token="isolated-fixture-key")
    result = console._agent_request("POST", "/internal/v1/business/tracking_query", payload={
        "request_id": str(uuid4()), "params": {"tracking_number": "isolated-002"}, "timeout_sec": 5})
    assert result["ok"] is True, result
    assert result["data"] == {"type": "waybill", "waybill_no": "isolated-002"}
    assert calls == [{"tracking_number": "isolated-002", "account_id": "isolated-provider"}]


def test_busy_direct_target_does_not_queue_or_execute_after_rejection():
    async def run():
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        with patch.dict(dispatch._SEMAPHORES, {"tracking_query": semaphore}), \
             patch.object(dispatch, "_load_callable") as loader:
            status, response = await dispatch.execute_target("tracking_query", dispatch.TaskRequest(params={}), wait_for_stop=True)
            assert status == 409 and response["error_code"] == "BUSINESS_RESOURCE_BUSY"
            semaphore.release()
            await asyncio.sleep(0)
            loader.assert_not_called()
    asyncio.run(run())


def test_release_hold_rejects_console_read_before_provider_io(business_http):
    _, base_url, calls = business_http
    lifecycle = routes._direct_business_service.invocation_service
    lifecycle._hold = lambda: True
    console = AgentApiServiceMixin()
    console.settings = SimpleNamespace(agent_base_url=base_url, agent_timeout_seconds=5, agent_internal_api_token="isolated-fixture-key")
    result = console._agent_request("POST", "/internal/v1/business/tracking_query", payload={
        "request_id": str(uuid4()), "params": {"tracking_number": "isolated-002"}, "timeout_sec": 5})
    assert result["ok"] is False and result["error_code"] == "PLUGIN_RELEASE_HELD"
    assert calls == [] and lifecycle.active_read_count() == 0


def test_cancelled_reader_remains_counted_until_actual_blocking_io_stops():
    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        entered, finish = threading.Event(), threading.Event()
        def provider():
            entered.set()
            assert finish.wait(timeout=3)
            return {"rows": []}
        task = asyncio.create_task(lifecycle.call_read(operation="isolated-query",
            handler=lambda: call_blocking(provider, timeout_sec=1)))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and lifecycle.active_read_count() == 1
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lifecycle.active_read_count() == 0
    asyncio.run(exercise())


def test_harness_factory_tracking_reaches_actual_http_capability_and_normalizer(business_http):
    from harness_composition import build_read_only_harness_gateway
    _, _, calls = business_http
    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        runtime = SimpleNamespace(memory=SimpleNamespace(search_knowledge=lambda **kwargs: [], connection_factory=lambda: None))
        repository = SimpleNamespace(list_work_items=lambda **kwargs: [], get_run=lambda _: None, get_evidence=lambda _: None)
        gateway = build_read_only_harness_gateway(runtime, repository, invocations=lifecycle)
        # Only the optional database enrichment boundary is empty. The actual
        # tracking tool, capability, HTTP client, route, dispatcher and formatter run.
        with patch.object(track_waybill_tool, "get_waybill_tracking_cache", return_value=None):
            result = await asyncio.to_thread(gateway.handlers()["tracking.lookup"], {"tracking_number": "R12345678901"})
        assert result["可用"] is True and result["找到"] is True, result
        assert result["轨迹"][0]["网点"] == "隔离网点"
        assert lifecycle.active_read_count() == 0
    asyncio.run(exercise())
    assert len(calls) == 1 and calls[0]["account_id"] == "isolated-provider"


def test_default_resolved_account_is_busy_until_actual_read_finishes():
    from agent.orchestration.models import OrchestrationError
    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        entered, finish = threading.Event(), threading.Event()
        def provider(params):
            assert params["account_id"] == "resolved-default"
            entered.set()
            assert finish.wait(3)
            return {"rows": []}
        with patch.object(dispatch, "resolve_account_params", return_value={"tracking_number": "R12345678901", "account_id": "resolved-default"}), \
             patch.object(dispatch, "_load_callable", return_value=provider):
            task = asyncio.create_task(dispatch.execute_target("tracking_query", dispatch.TaskRequest(params={"tracking_number": "R12345678901"}), read_lifecycle=lifecycle))
            assert await asyncio.to_thread(entered.wait, 1)
            try:
                with pytest.raises(OrchestrationError, match="正在执行"):
                    lifecycle.begin_credentials_change("resolved-default")
                assert lifecycle.active_read_count() == 1
            finally:
                finish.set()
            status, _ = await task
            assert status == 200 and lifecycle.active_read_count() == 0
            lifecycle.begin_credentials_change("resolved-default")()
    asyncio.run(exercise())


def test_receipts_query_registers_each_resolved_provider_account():
    from agent.orchestration.models import OrchestrationError
    from agent.tms_runtime import receipts_query
    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        entered, finish = threading.Event(), threading.Event()
        def source(params, **kwargs):
            assert params["account_id"] == "receipt-account"
            entered.set()
            assert finish.wait(3)
            return "ronghui", "send", [], {"total": 0, "fetched": 0}, []
        with patch.object(receipts_query, "resolve_account_params", return_value={"account_id": "receipt-account"}), \
             patch.object(receipts_query.receipts_sync, "_fetch_source", side_effect=source):
            task = asyncio.create_task(receipts_query.query_receipts_async({"platform": "ronghui",
                "date_from": "2026-09-09", "date_to": "2026-09-09"}, 5, read_lifecycle=lifecycle))
            assert await asyncio.to_thread(entered.wait, 1)
            try:
                with pytest.raises(OrchestrationError, match="正在执行"):
                    lifecycle.begin_credentials_change("receipt-account")
            finally:
                finish.set()
            assert (await task)["complete"] is True
            assert lifecycle.active_read_count() == 0
    asyncio.run(exercise())
