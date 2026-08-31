"""Process-local trusted ingress for managed Service v2 contributions.

Transport adapters must verify their native request before calling this module.
Only closed transport identities are accepted here; project and invocation
identity remain owned by the active contribution registry and policy service.
"""

from __future__ import annotations

from threading import RLock
from typing import Any

from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.orchestration.automation_project_entrypoints import (
    ServiceV2EventDispatcher,
    ServiceV2WebhookDispatcher,
)
from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import OrchestrationError


class ServiceV2ManagedIngress:
    """Closed process-local facade over the managed Webhook/Event dispatchers."""

    def __init__(
        self,
        *,
        policy_service: AutomationProjectPolicyService,
        contribution_registry: Any,
        backend_availability: RuntimeContributionBackendAvailability,
    ) -> None:
        if not isinstance(
            backend_availability,
            RuntimeContributionBackendAvailability,
        ):
            raise TypeError("backend_availability is invalid")
        self._backend_availability = backend_availability
        self._webhook_dispatcher = ServiceV2WebhookDispatcher(
            policy_service=policy_service,
            contribution_registry=contribution_registry,
        )
        self._event_dispatcher = ServiceV2EventDispatcher(
            policy_service=policy_service,
            contribution_registry=contribution_registry,
        )

    async def dispatch_verified_webhook(
        self,
        *,
        method: str,
        route: str,
        source_event_id: str,
    ) -> dict[str, Any] | None:
        """Dispatch one already-verified Webhook identity without a payload."""

        return await self._webhook_dispatcher.dispatch(
            method=method,
            route=route,
            source_event_id=source_event_id,
        )

    async def dispatch_verified_event(
        self,
        *,
        event_name: str,
        source_event_id: str,
    ) -> dict[str, Any] | None:
        """Dispatch one already-verified non-durable Event identity."""

        return await self._event_dispatcher.dispatch(
            event_name=event_name,
            source_event_id=source_event_id,
        )

    def _bind_backend_readiness(self) -> None:
        self._backend_availability.bind_managed_ingress(self)

    def _unbind_backend_readiness(self) -> None:
        self._backend_availability.unbind_managed_ingress(self)

    def _backend_readiness_is_bound(self) -> bool:
        return self._backend_availability.managed_ingress_is_bound(self)


_SERVICE_V2_MANAGED_INGRESS: ServiceV2ManagedIngress | None = None
_SERVICE_V2_MANAGED_INGRESS_LOCK = RLock()


def bind_service_v2_managed_ingress(service: ServiceV2ManagedIngress) -> None:
    """Bind the sole managed ingress for this process.

    Binding the same object is idempotent. Replacing an active binding requires
    an explicit unbind first so overlapping composition-root lifecycles cannot
    silently redirect trusted events.
    """

    global _SERVICE_V2_MANAGED_INGRESS
    if not isinstance(service, ServiceV2ManagedIngress):
        raise TypeError("service must be ServiceV2ManagedIngress")
    with _SERVICE_V2_MANAGED_INGRESS_LOCK:
        if (
            _SERVICE_V2_MANAGED_INGRESS is not None
            and _SERVICE_V2_MANAGED_INGRESS is not service
        ):
            raise RuntimeError("Service v2 managed ingress is already bound")
        if _SERVICE_V2_MANAGED_INGRESS is service:
            return
        _SERVICE_V2_MANAGED_INGRESS = service
        try:
            service._bind_backend_readiness()
        except Exception:
            _SERVICE_V2_MANAGED_INGRESS = None
            raise


def unbind_service_v2_managed_ingress(
    service: ServiceV2ManagedIngress | None = None,
) -> None:
    """Remove the process binding, optionally requiring its exact identity."""

    global _SERVICE_V2_MANAGED_INGRESS
    if service is not None and not isinstance(service, ServiceV2ManagedIngress):
        raise TypeError("service must be ServiceV2ManagedIngress or None")
    with _SERVICE_V2_MANAGED_INGRESS_LOCK:
        current = _SERVICE_V2_MANAGED_INGRESS
        if (
            service is not None
            and current is not None
            and current is not service
        ):
            raise RuntimeError("Service v2 managed ingress binding does not match")
        if current is None:
            return
        current._unbind_backend_readiness()
        _SERVICE_V2_MANAGED_INGRESS = None


def service_v2_managed_ingress_is_bound() -> bool:
    """Return whether the trusted process-local ingress is currently bound."""

    with _SERVICE_V2_MANAGED_INGRESS_LOCK:
        service = _SERVICE_V2_MANAGED_INGRESS
        return service is not None and service._backend_readiness_is_bound()


def _bound_ingress() -> ServiceV2ManagedIngress:
    with _SERVICE_V2_MANAGED_INGRESS_LOCK:
        service = _SERVICE_V2_MANAGED_INGRESS
        if service is None or not service._backend_readiness_is_bound():
            raise OrchestrationError(
                "PROJECT_RUNTIME_PROJECTION_STALE",
                "Automation project managed ingress is unavailable",
            )
        return service


async def dispatch_verified_webhook(
    *,
    method: str,
    route: str,
    source_event_id: str,
) -> dict[str, Any] | None:
    """Dispatch through the currently bound trusted Webhook ingress."""

    return await _bound_ingress().dispatch_verified_webhook(
        method=method,
        route=route,
        source_event_id=source_event_id,
    )


async def dispatch_verified_event(
    *,
    event_name: str,
    source_event_id: str,
) -> dict[str, Any] | None:
    """Dispatch through the currently bound trusted Event ingress."""

    return await _bound_ingress().dispatch_verified_event(
        event_name=event_name,
        source_event_id=source_event_id,
    )
