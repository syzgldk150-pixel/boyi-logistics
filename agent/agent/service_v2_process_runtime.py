"""Composition boundary for process-local Service v2 runtime backends."""

from __future__ import annotations

from threading import RLock
from typing import Mapping

from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness_application import HarnessConversationService, ReadOnlyFixedHandler
from agent.llm_client import LLMClient
from agent.harness_runtime import HarnessRuntime, HarnessRuntimeStatus
from agent.orchestration.models import Actor
from agent.orchestration.service_v2_managed_ingress import (
    ServiceV2ManagedIngress,
    bind_service_v2_managed_ingress,
    unbind_service_v2_managed_ingress,
)


class ServiceV2ProcessRuntime:
    """Own the online read-only assistant and managed-ingress lifecycle."""

    def __init__(
        self,
        *,
        policy_service: object,
        contribution_registry: object,
        backend_availability: RuntimeContributionBackendAvailability,
        llm_client: LLMClient,
        harness_fixed_handlers: Mapping[str, ReadOnlyFixedHandler],
    ) -> None:
        self._availability = backend_availability
        self._harness = HarnessRuntime(
            policy_service=policy_service,
            contribution_registry=contribution_registry,
            backend_availability=backend_availability,
            llm_client=llm_client,
            fixed_handlers=harness_fixed_handlers,
        )
        self._conversations = HarnessConversationService(
            repository=InMemoryHarnessSessionRepository(),
            sidecar_factory=self._harness.sidecar_factory,
            timeout_seconds=30,
        )
        self._ingress = ServiceV2ManagedIngress(
            policy_service=policy_service,
            contribution_registry=contribution_registry,
            backend_availability=backend_availability,
        )
        self._started = False

    @property
    def conversations(self) -> HarnessConversationService:
        return self._conversations

    def harness_tools(self, actor: Actor, request_id: str) -> list[dict[str, str]]:
        return self._harness.public_tools(actor, request_id)

    def harness_status(self) -> HarnessRuntimeStatus:
        return self._harness.status()

    def start(self) -> HarnessRuntimeStatus:
        if self._started:
            return self._harness.status()
        harness_status = self._harness.start()
        ingress_bound = False
        try:
            bind_service_v2_managed_ingress(self._ingress)
            ingress_bound = True
            self._availability.mark_available("webhook", "events")
            bind_service_v2_process_runtime(self)
        except Exception:
            self._availability.mark_unavailable(
                "webhook",
                "events",
                reason_detail="MANAGED_INGRESS_BIND_FAILED",
            )
            if ingress_bound:
                unbind_service_v2_managed_ingress(self._ingress)
            self._harness.stop()
            raise
        self._started = True
        return harness_status

    def stop(self) -> None:
        if not self._started:
            return
        self._availability.mark_unavailable(
            "harness",
            "webhook",
            "events",
            reason_detail="AGENT_SHUTDOWN",
        )
        self._harness.stop()
        unbind_service_v2_managed_ingress(self._ingress)
        unbind_service_v2_process_runtime(self)
        self._started = False


_LOCK = RLock()
_PROCESS_RUNTIME: ServiceV2ProcessRuntime | None = None


def bind_service_v2_process_runtime(runtime: ServiceV2ProcessRuntime) -> None:
    global _PROCESS_RUNTIME
    if not isinstance(runtime, ServiceV2ProcessRuntime):
        raise TypeError("runtime must be ServiceV2ProcessRuntime")
    with _LOCK:
        if _PROCESS_RUNTIME is not None and _PROCESS_RUNTIME is not runtime:
            raise RuntimeError("Service v2 process runtime is already bound")
        _PROCESS_RUNTIME = runtime


def unbind_service_v2_process_runtime(
    runtime: ServiceV2ProcessRuntime | None = None,
) -> None:
    global _PROCESS_RUNTIME
    if runtime is not None and not isinstance(runtime, ServiceV2ProcessRuntime):
        raise TypeError("runtime must be ServiceV2ProcessRuntime or None")
    with _LOCK:
        if runtime is not None and _PROCESS_RUNTIME not in {None, runtime}:
            raise RuntimeError("Service v2 process runtime binding does not match")
        _PROCESS_RUNTIME = None


def _bound_runtime() -> ServiceV2ProcessRuntime:
    with _LOCK:
        runtime = _PROCESS_RUNTIME
    if runtime is None:
        raise RuntimeError("Service v2 process runtime is not initialized")
    return runtime


def service_v2_harness_conversations() -> HarnessConversationService:
    return _bound_runtime().conversations


def service_v2_harness_tools(actor: Actor, request_id: str) -> list[dict[str, str]]:
    return _bound_runtime().harness_tools(actor, request_id)


def service_v2_harness_status() -> HarnessRuntimeStatus:
    return _bound_runtime().harness_status()


__all__ = [
    "ServiceV2ProcessRuntime",
    "bind_service_v2_process_runtime",
    "service_v2_harness_conversations",
    "service_v2_harness_status",
    "service_v2_harness_tools",
    "unbind_service_v2_process_runtime",
]
