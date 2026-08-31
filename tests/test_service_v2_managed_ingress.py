from __future__ import annotations

import inspect
from dataclasses import dataclass
from unittest import IsolatedAsyncioTestCase

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.orchestration.models import OrchestrationError
from agent.orchestration.service_v2_managed_ingress import (
    ServiceV2ManagedIngress,
    bind_service_v2_managed_ingress,
    dispatch_verified_event,
    dispatch_verified_webhook,
    service_v2_managed_ingress_is_bound,
    unbind_service_v2_managed_ingress,
)


@dataclass(frozen=True)
class _Target:
    automation_id: str = "service-instance"
    generation: int = 7
    contribution_id: str = "managed-contribution"


class _Registry:
    def __init__(self) -> None:
        self.webhook_target: object | None = _Target(
            contribution_id="receive-status"
        )
        self.event_target: object | None = _Target(
            contribution_id="shipment-created"
        )
        self.webhook_error_code: str | None = None
        self.event_error_code: str | None = None

    def resolve_active_webhook_route(
        self,
        *,
        method: str,
        route: str,
    ) -> object | None:
        del method, route
        if self.webhook_error_code is not None:
            raise PluginConflictError(
                "synthetic Webhook projection failure",
                code=self.webhook_error_code,
            )
        return self.webhook_target

    def resolve_active_event(self, *, event_name: str) -> object | None:
        del event_name
        if self.event_error_code is not None:
            raise PluginConflictError(
                "synthetic Event projection failure",
                code=self.event_error_code,
            )
        return self.event_target


class _Policy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []

    async def invoke_trusted_and_wait(
        self,
        automation_id: object,
        **kwargs: object,
    ) -> object:
        self.calls.append((automation_id, dict(kwargs)))
        return {
            "success": True,
            "status": "COMPLETED",
            "command_id": "must-not-leak",
        }


class ServiceV2ManagedIngressTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        unbind_service_v2_managed_ingress()
        self.registry = _Registry()
        self.policy = _Policy()
        self.availability = RuntimeContributionBackendAvailability()
        self.ingress = ServiceV2ManagedIngress(
            policy_service=self.policy,  # type: ignore[arg-type]
            contribution_registry=self.registry,
            backend_availability=self.availability,
        )

    def tearDown(self) -> None:
        unbind_service_v2_managed_ingress()

    async def test_unbound_module_calls_fail_closed(self):
        self.assertFalse(service_v2_managed_ingress_is_bound())

        with self.assertRaises(OrchestrationError) as webhook_error:
            await dispatch_verified_webhook(
                method="POST",
                route="status_update",
                source_event_id="webhook-one",
            )
        self.assertEqual(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            webhook_error.exception.code,
        )

        with self.assertRaises(OrchestrationError) as event_error:
            await dispatch_verified_event(
                event_name="shipment.created",
                source_event_id="event-one",
            )
        self.assertEqual(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            event_error.exception.code,
        )

    async def test_bound_calls_delegate_through_closed_dispatchers(self):
        bind_service_v2_managed_ingress(self.ingress)
        bind_service_v2_managed_ingress(self.ingress)
        self.assertTrue(service_v2_managed_ingress_is_bound())

        webhook_result = await dispatch_verified_webhook(
            method="POST",
            route="status_update",
            source_event_id="webhook-one",
        )
        event_result = await dispatch_verified_event(
            event_name="shipment.created",
            source_event_id="event-one",
        )

        self.assertEqual(
            {"success": True, "status": "COMPLETED"},
            webhook_result,
        )
        self.assertEqual(
            {"success": True, "status": "COMPLETED"},
            event_result,
        )
        self.assertEqual(2, len(self.policy.calls))

    async def test_unknown_and_disabled_identities_return_none(self):
        bind_service_v2_managed_ingress(self.ingress)
        self.registry.webhook_target = None
        self.registry.event_target = None

        self.assertIsNone(
            await dispatch_verified_webhook(
                method="POST",
                route="unknown",
                source_event_id="",
            )
        )
        self.assertIsNone(
            await dispatch_verified_event(
                event_name="shipment.disabled",
                source_event_id="",
            )
        )
        self.assertEqual([], self.policy.calls)

    async def test_stale_and_ambiguous_projection_fail_closed(self):
        bind_service_v2_managed_ingress(self.ingress)
        cases = (
            ("webhook", "RUNTIME_PROJECTION_STALE"),
            ("webhook", "RUNTIME_PROJECTION_AMBIGUOUS"),
            ("event", "RUNTIME_PROJECTION_STALE"),
            ("event", "RUNTIME_PROJECTION_AMBIGUOUS"),
        )
        for ingress_kind, error_code in cases:
            with self.subTest(ingress_kind=ingress_kind, error_code=error_code):
                self.registry.webhook_error_code = None
                self.registry.event_error_code = None
                if ingress_kind == "webhook":
                    self.registry.webhook_error_code = error_code
                    call = dispatch_verified_webhook(
                        method="POST",
                        route="status_update",
                        source_event_id="webhook-one",
                    )
                else:
                    self.registry.event_error_code = error_code
                    call = dispatch_verified_event(
                        event_name="shipment.created",
                        source_event_id="event-one",
                    )
                with self.assertRaises(OrchestrationError) as raised:
                    await call
                self.assertEqual(
                    "PROJECT_RUNTIME_PROJECTION_STALE",
                    raised.exception.code,
                )
        self.assertEqual([], self.policy.calls)

    async def test_malformed_generation_fails_closed(self):
        bind_service_v2_managed_ingress(self.ingress)
        self.registry.webhook_target = _Target(generation=True)  # type: ignore[arg-type]

        with self.assertRaises(OrchestrationError) as raised:
            await dispatch_verified_webhook(
                method="POST",
                route="status_update",
                source_event_id="webhook-one",
            )

        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual([], self.policy.calls)

    async def test_unbind_revokes_module_entrypoints(self):
        self.availability.mark_available("webhook", "events")
        self.assertFalse(self.availability.is_available("webhook"))
        self.assertFalse(self.availability.is_available("events"))
        bind_service_v2_managed_ingress(self.ingress)
        self.assertTrue(self.availability.is_available("webhook"))
        self.assertTrue(self.availability.is_available("events"))
        unbind_service_v2_managed_ingress(self.ingress)
        self.assertFalse(self.availability.is_available("webhook"))
        self.assertFalse(self.availability.is_available("events"))

        with self.assertRaises(OrchestrationError) as raised:
            await dispatch_verified_event(
                event_name="shipment.created",
                source_event_id="event-one",
            )
        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual([], self.policy.calls)

    def test_binding_identity_and_closed_interfaces_are_enforced(self):
        with self.assertRaises(TypeError):
            bind_service_v2_managed_ingress(object())  # type: ignore[arg-type]

        bind_service_v2_managed_ingress(self.ingress)
        other = ServiceV2ManagedIngress(
            policy_service=self.policy,  # type: ignore[arg-type]
            contribution_registry=self.registry,
            backend_availability=self.availability,
        )
        with self.assertRaises(RuntimeError):
            bind_service_v2_managed_ingress(other)
        with self.assertRaises(RuntimeError):
            unbind_service_v2_managed_ingress(other)

        self.assertEqual(
            {"self", "method", "route", "source_event_id"},
            set(
                inspect.signature(
                    ServiceV2ManagedIngress.dispatch_verified_webhook
                ).parameters
            ),
        )
        self.assertEqual(
            {"self", "event_name", "source_event_id"},
            set(
                inspect.signature(
                    ServiceV2ManagedIngress.dispatch_verified_event
                ).parameters
            ),
        )
