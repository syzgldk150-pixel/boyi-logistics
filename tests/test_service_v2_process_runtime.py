from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.harness_application import FIXED_HARNESS_TOOL_IDS
from agent.llm_client import LLMClient
from agent.orchestration.service_v2_managed_ingress import (
    ServiceV2ManagedIngress,
    bind_service_v2_managed_ingress,
    service_v2_managed_ingress_is_bound,
    unbind_service_v2_managed_ingress,
)
from agent.service_v2_process_runtime import (
    ServiceV2ProcessRuntime,
    service_v2_harness_conversations,
    service_v2_harness_status,
    unbind_service_v2_process_runtime,
)


class _Registry:
    def active_snapshot(self) -> tuple[Mapping[str, Any], ...]:
        return ()

    def resolve_active(self, *_args: object) -> object:
        raise RuntimeError("no active Harness contribution")

    def resolve_active_webhook_route(self, *, method: str, route: str) -> None:
        del method, route
        return None

    def resolve_active_event(self, *, event_name: str) -> None:
        del event_name
        return None


class _Policy:
    pass


class _ConfiguredLLM(LLMClient):
    def __init__(self) -> None:
        pass

    def public_status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "provider": "deepseek",
            "model": "deepseek-chat",
            "health": "ready",
        }


def _fixed_handlers() -> dict[str, Any]:
    return {
        tool_id: (lambda _arguments, current=tool_id: {"查询": current})
        for tool_id in FIXED_HARNESS_TOOL_IDS
    }


def _reset_bindings() -> None:
    unbind_service_v2_process_runtime()
    unbind_service_v2_managed_ingress()


def test_process_runtime_binds_and_revokes_all_live_backends() -> None:
    _reset_bindings()
    availability = RuntimeContributionBackendAvailability()
    runtime = ServiceV2ProcessRuntime(
        policy_service=_Policy(),
        contribution_registry=_Registry(),
        backend_availability=availability,
        llm_client=_ConfiguredLLM(),
        harness_fixed_handlers=_fixed_handlers(),
    )
    try:
        assert runtime.start().status == "READY"
        assert service_v2_harness_status().availability == "ONLINE_READ_ONLY"
        assert service_v2_harness_conversations() is runtime.conversations
        assert service_v2_managed_ingress_is_bound() is True
        assert all(
            availability.is_available(kind)
            for kind in ("harness", "webhook", "events")
        )
    finally:
        runtime.stop()
    assert service_v2_managed_ingress_is_bound() is False
    assert all(
        not availability.is_available(kind)
        for kind in ("harness", "webhook", "events")
    )
    with pytest.raises(RuntimeError, match="not initialized"):
        service_v2_harness_status()


def test_conflicting_ingress_binding_blocks_startup_and_revokes_harness() -> None:
    _reset_bindings()
    registry = _Registry()
    existing_availability = RuntimeContributionBackendAvailability()
    existing = ServiceV2ManagedIngress(
        policy_service=_Policy(),  # type: ignore[arg-type]
        contribution_registry=registry,
        backend_availability=existing_availability,
    )
    bind_service_v2_managed_ingress(existing)
    availability = RuntimeContributionBackendAvailability()
    runtime = ServiceV2ProcessRuntime(
        policy_service=_Policy(),
        contribution_registry=registry,
        backend_availability=availability,
        llm_client=_ConfiguredLLM(),
        harness_fixed_handlers=_fixed_handlers(),
    )
    try:
        with pytest.raises(RuntimeError, match="already bound"):
            runtime.start()
        assert availability.is_available("harness") is False
        assert availability.is_available("webhook") is False
        assert availability.is_available("events") is False
        assert service_v2_managed_ingress_is_bound() is True
    finally:
        unbind_service_v2_managed_ingress(existing)
        unbind_service_v2_process_runtime()


def test_composition_root_owns_lifecycle_without_adding_an_http_transport() -> None:
    root = Path(__file__).resolve().parents[1]
    main_source = (root / "agent" / "main.py").read_text(encoding="utf-8")
    lifespan = main_source[
        main_source.index("@asynccontextmanager") : main_source.index(
            "app = FastAPI"
        )
    ]
    assert lifespan.index("ServiceV2ProcessRuntime(") < lifespan.index(
        "process_service_v2_runtime.start"
    )
    assert lifespan.index("process_service_v2_runtime.start") < lifespan.index(
        "    yield"
    )
    assert lifespan.index("    yield") < lifespan.index(
        "process_service_v2_runtime.stop"
    )
    assert '@app.post("/webhook/{path:path}")' in main_source

    ingress_source = (
        root
        / "agent"
        / "agent"
        / "orchestration"
        / "service_v2_managed_ingress.py"
    ).read_text(encoding="utf-8")
    assert "fastapi" not in ingress_source.lower()
    assert "APIRouter" not in ingress_source
