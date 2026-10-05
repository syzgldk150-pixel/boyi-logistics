"""Shared automation reads and slow operations keep the API responsive."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.automation_plugins.capability_proxy_v2 import ServiceV2CapabilityProxy
from agent.automation_plugins.management_api import create_automation_plugin_management_router
from agent.orchestration.automation_project_api import create_automation_project_router
from tests.automation_plugin_management_api_support import (
    _ApiService, _configuration_request_payload, _console_actor,
)
from tests.test_automation_plugin_capability_proxy_v2 import (
    _Documents, _Orchestration, _context, _service_consumer_manifest, _service_registry,
)


@pytest.mark.parametrize("matching", [True, False])
def test_status_route_reuses_only_the_authorized_invocation(matching):
    reads = []
    snapshot = {"invocation_id": "current" if matching else "other", "status": "RUNNING"}

    def read(identity):
        reads.append(identity)
        return {"invocation_id": identity, "status": "COMPLETED"}

    app = FastAPI()

    @app.middleware("http")
    async def authorize(request, call_next):
        request.state.authorized_invocation = snapshot
        return await call_next(request)

    app.include_router(create_automation_project_router(
        service_provider=lambda: SimpleNamespace(direct_invocations=SimpleNamespace(get=read)),
        actor_provider=lambda _request: _console_actor(),
    ))
    result = TestClient(app).get("/internal/v1/automation-invocations/current")
    assert result.status_code == 200
    assert result.json()["data"]["invocation_id"] == "current"
    assert reads == ([] if matching else ["current"])
    assert result.json()["data"]["status"] == ("RUNNING" if matching else "COMPLETED")


@pytest.mark.parametrize("route", ["configuration", "schedule"])
def test_slow_scheduler_refresh_does_not_block_status_requests(route):
    entered, release = threading.Event(), threading.Event()
    service = _ApiService()
    service.configuration_result.update({
        "runtime_model": "SERVICE_V2", "entrypoint_kinds": {"scheduler": "scheduler"},
    })
    service.save_console_schedule = lambda *_args, **_kwargs: service.configuration_result
    app = FastAPI()

    def refresh():
        entered.set()
        assert release.wait(5), "scheduler barrier was not released"
        return {"initialized": True, "jobs": 1}

    app.include_router(create_automation_plugin_management_router(
        service_provider=lambda: service,
        actor_provider=lambda _request: _console_actor(),
        scheduler_refresh_provider=refresh,
    ))

    @app.get("/probe")
    async def probe():
        return {"responsive": True}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = _configuration_request_payload()
            if route == "schedule":
                payload = {key: payload[key] for key in (
                    "schedule", "request_id", "expected_project_configuration_version",
                )}
            saving = asyncio.create_task(client.put(
                f"/internal/v1/automation/instances/automation-1/{route}", json=payload,
            ))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                assert not saving.done()
                probe_result = await asyncio.wait_for(client.get("/probe"), 1)
                assert probe_result.json() == {"responsive": True}
            finally:
                release.set()
            response = await saving
            assert response.status_code == 200
            assert response.json()["data"]["scheduler_refresh_completed"] is True
            assert not any(name == "catalog" for name, _values in service.calls)

    asyncio.run(exercise())


def test_service_manifest_and_write_marker_run_off_event_loop():
    loop_thread = threading.get_ident()
    threads = {}
    documents = _Documents(manifest=_service_consumer_manifest())
    original = documents.get_version

    def get_version(*args, **kwargs):
        threads["manifest"] = threading.get_ident()
        return original(*args, **kwargs)

    documents.get_version = get_version
    registry = _service_registry()
    original_lookup = registry.require_operation

    def lookup(*args, **kwargs):
        threads["lookup"] = threading.get_ident()
        return original_lookup(*args, **kwargs)

    registry.require_operation = lookup

    def mark():
        threads["write"] = threading.get_ident()

    async def execute(**_kwargs):
        threads["provider"] = threading.get_ident()
        assert "write" in threads
        return {"status": "SUCCESS", "data": {}, "meta": {}, "warnings": [], "error": None}

    proxy = ServiceV2CapabilityProxy(
        _Orchestration(documents), service_registry=registry, service_executor=execute,
    )
    asyncio.run(proxy.service_invoke(
        _context("service.invoke", "run", marker=mark),
        {"service": "plugin.base.runner@1", "operation": "run", "arguments": {}},
    ))
    assert threads["manifest"] != loop_thread
    assert threads["lookup"] != loop_thread
    assert threads["write"] != loop_thread
    assert threads["provider"] == loop_thread


def test_cancel_waits_for_write_marker_thread_before_unwinding():
    entered, release = threading.Event(), threading.Event()
    dispatched = []

    def mark():
        entered.set()
        assert release.wait(5), "write marker barrier was not released"

    async def execute(**_kwargs):
        dispatched.append(True)
        return {"status": "SUCCESS", "data": {}, "meta": {}, "warnings": [], "error": None}

    proxy = ServiceV2CapabilityProxy(
        _Orchestration(_Documents(manifest=_service_consumer_manifest())),
        service_registry=_service_registry(), service_executor=execute,
    )

    async def exercise():
        task = asyncio.create_task(proxy.service_invoke(
            _context("service.invoke", "run", marker=mark),
            {"service": "plugin.base.runner@1", "operation": "run", "arguments": {}},
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert dispatched == []

    asyncio.run(exercise())


@pytest.mark.parametrize("blocked", ["project_reference", "get_generation"])
def test_provider_lookup_does_not_stall_other_tasks(blocked):
    from agent.automation_plugins.production import ProductionServiceV2ProviderExecutor
    from tests.test_automation_plugin_service_invoke_v2 import (
        _Router, _RuntimeGenerations, _provider_snapshot, _registry,
    )

    entered, release = threading.Event(), threading.Event()
    registry, provider = _registry()
    generations = _RuntimeGenerations(_provider_snapshot("base-project", 1))
    target = registry if blocked == "project_reference" else generations
    original = getattr(target, blocked)

    def slow_lookup(*args, **kwargs):
        entered.set()
        assert release.wait(5), "provider lookup barrier was not released"
        return original(*args, **kwargs)

    setattr(target, blocked, slow_lookup)
    executor = ProductionServiceV2ProviderExecutor(service_registry=registry, generation_repository=generations)
    router = _Router()
    executor.bind_router(router)

    async def exercise():
        task = asyncio.create_task(executor(
            provider=provider, caller_automation_id="consumer-project", operation="run",
            arguments={}, call_chain=("plugin.base.runner@1",),
            invocation_id="22222222-2222-4222-8222-222222222222",
        ))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert not task.done()
            assert router.calls == []
        finally:
            release.set()
        assert await task == {"status": "SUCCESS"}
        assert len(router.calls) == 1

    asyncio.run(exercise())
