"""Process binding for the restricted offline Harness runtime."""

from __future__ import annotations

from threading import RLock

from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness_application import HarnessConversationService
from agent.harness_runtime import HarnessRuntime, HarnessRuntimeStatus
from agent.orchestration.models import Actor


class HarnessProcessRuntime:
    """Own one process-level Harness catalog, canary, and conversation store."""

    def __init__(
        self,
        *,
        policy_service: object,
        contribution_registry: object,
        backend_availability: RuntimeContributionBackendAvailability,
    ) -> None:
        self._harness = HarnessRuntime(
            policy_service=policy_service,
            contribution_registry=contribution_registry,
            backend_availability=backend_availability,
        )
        self._conversations = HarnessConversationService(
            repository=InMemoryHarnessSessionRepository(),
            sidecar_factory=self._harness.sidecar_factory,
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
        status = self._harness.start()
        try:
            bind_harness_process_runtime(self)
        except Exception:
            self._harness.stop()
            raise
        self._started = True
        return status

    def stop(self) -> None:
        if not self._started:
            return
        self._harness.stop()
        unbind_harness_process_runtime(self)
        self._started = False


_LOCK = RLock()
_PROCESS_RUNTIME: HarnessProcessRuntime | None = None


def bind_harness_process_runtime(runtime: HarnessProcessRuntime) -> None:
    global _PROCESS_RUNTIME
    if not isinstance(runtime, HarnessProcessRuntime):
        raise TypeError("runtime must be HarnessProcessRuntime")
    with _LOCK:
        if _PROCESS_RUNTIME is not None and _PROCESS_RUNTIME is not runtime:
            raise RuntimeError("Harness process runtime is already bound")
        _PROCESS_RUNTIME = runtime


def unbind_harness_process_runtime(
    runtime: HarnessProcessRuntime | None = None,
) -> None:
    global _PROCESS_RUNTIME
    if runtime is not None and not isinstance(runtime, HarnessProcessRuntime):
        raise TypeError("runtime must be HarnessProcessRuntime or None")
    with _LOCK:
        if runtime is not None and _PROCESS_RUNTIME not in {None, runtime}:
            raise RuntimeError("Harness process runtime binding does not match")
        _PROCESS_RUNTIME = None


def _bound_runtime() -> HarnessProcessRuntime:
    with _LOCK:
        runtime = _PROCESS_RUNTIME
    if runtime is None:
        raise RuntimeError("Harness process runtime is not initialized")
    return runtime


def harness_conversations() -> HarnessConversationService:
    return _bound_runtime().conversations


def harness_tools(actor: Actor, request_id: str) -> list[dict[str, str]]:
    return _bound_runtime().harness_tools(actor, request_id)


def harness_status() -> HarnessRuntimeStatus:
    return _bound_runtime().harness_status()


__all__ = [
    "HarnessProcessRuntime",
    "bind_harness_process_runtime",
    "harness_conversations",
    "harness_status",
    "harness_tools",
    "unbind_harness_process_runtime",
]
