from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from unittest import IsolatedAsyncioTestCase

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.orchestration.automation_project_entrypoints import (
    ServiceV2WebhookDispatcher,
)
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.automation_project_authorization import AutomationEntrypoint


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class _Target:
    automation_id: str = "service-instance"
    generation: int = 7
    contribution_id: str = "receive_status"


class _Registry:
    def __init__(
        self,
        *,
        target: object | None = _Target(),
        error_code: str | None = None,
    ) -> None:
        self.target = target
        self.error_code = error_code
        self.calls: list[dict[str, object]] = []

    def resolve_active_webhook_route(
        self,
        *,
        method: str,
        route: str,
    ) -> object | None:
        self.calls.append({"method": method, "route": route})
        if self.error_code is not None:
            raise PluginConflictError(
                "synthetic registry failure",
                code=self.error_code,
            )
        return self.target


class _Policy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.result: object = {
            "success": True,
            "status": "COMPLETED",
            "command_id": "must-not-leak",
            "run_id": "must-not-leak",
            "correlation_id": "must-not-leak",
            "error_summary": "must-not-leak",
        }

    async def invoke_trusted_and_wait(
        self,
        automation_id: object,
        **kwargs: object,
    ) -> object:
        self.calls.append((automation_id, dict(kwargs)))
        return self.result


class ServiceV2WebhookDispatcherTests(IsolatedAsyncioTestCase):
    @staticmethod
    def _dispatcher(
        registry: _Registry,
    ) -> tuple[ServiceV2WebhookDispatcher, _Policy]:
        policy = _Policy()
        dispatcher = ServiceV2WebhookDispatcher(
            policy_service=policy,  # type: ignore[arg-type]
            contribution_registry=registry,
        )
        return dispatcher, policy

    async def test_unknown_route_returns_none_before_event_validation(self):
        registry = _Registry(target=None)
        dispatcher, policy = self._dispatcher(registry)

        result = await dispatcher.dispatch(
            method="POST",
            route="unknown",
            source_event_id="",
        )

        self.assertIsNone(result)
        self.assertEqual([{"method": "POST", "route": "unknown"}], registry.calls)
        self.assertEqual([], policy.calls)

    async def test_interface_rejects_transport_and_business_overrides(self):
        dispatcher, policy = self._dispatcher(_Registry())
        self.assertEqual(
            {"self", "method", "route", "source_event_id"},
            set(inspect.signature(ServiceV2WebhookDispatcher.dispatch).parameters),
        )

        for field in (
            "envelope",
            "body",
            "query",
            "headers",
            "actor",
            "automation_id",
            "service",
            "operation",
            "arguments",
            "account_id",
            "resource_id",
        ):
            with self.subTest(field=field), self.assertRaises(TypeError):
                await dispatcher.dispatch(
                    method="POST",
                    route="status_update",
                    source_event_id="event-one",
                    **{field: "override"},
                )
        self.assertEqual([], policy.calls)

    async def test_exact_route_uses_registry_identity_and_returns_safe_result(self):
        registry = _Registry()
        dispatcher, policy = self._dispatcher(registry)

        result = await dispatcher.dispatch(
            method="POST",
            route="status_update",
            source_event_id="event-one",
        )

        owner = {
            "automation_id": "service-instance",
            "contribution_id": "receive_status",
            "method": "POST",
            "route": "status_update",
        }
        owner_digest = _digest(owner)
        event_digest = _digest({"owner": owner, "source_event_id": "event-one"})
        self.assertEqual({"success": True, "status": "COMPLETED"}, result)
        self.assertEqual(
            [{"method": "POST", "route": "status_update"}],
            registry.calls,
        )
        self.assertEqual(1, len(policy.calls))
        automation_id, kwargs = policy.calls[0]
        self.assertEqual("service-instance", automation_id)
        self.assertEqual(
            {
                "entrypoint": AutomationEntrypoint.WEBHOOK,
                "request_id": "event-one",
                "actor": Actor(
                    ActorType.WEBHOOK,
                    f"webhook:{owner_digest}",
                    authenticated_by="signed_webhook_route",
                ),
                "trusted_context": {
                    "route_id": owner_digest,
                    "route_revision": 7,
                    "source_event_id": "event-one",
                    "webhook_path": "webhook/status_update",
                    "webhook_method": "POST",
                },
                "idempotency_key": f"webhook:v2:{event_digest}",
                "expected_automation_generation": 7,
                "contribution_id": "receive_status",
            },
            kwargs,
        )
        self.assertEqual(75, len(kwargs["idempotency_key"]))
        self.assertEqual(72, len(kwargs["actor"].actor_id))

    async def test_matching_route_requires_bounded_stable_event_identity(self):
        for source_event_id in (None, "", " event-one", "event-one ", "x" * 192):
            with self.subTest(source_event_id=source_event_id):
                dispatcher, policy = self._dispatcher(_Registry())

                with self.assertRaises(OrchestrationError) as raised:
                    await dispatcher.dispatch(
                        method="POST",
                        route="status_update",
                        source_event_id=source_event_id,  # type: ignore[arg-type]
                    )

                self.assertEqual("STABLE_EVENT_ID_REQUIRED", raised.exception.code)
                self.assertEqual([], policy.calls)

    async def test_registry_failures_and_malformed_targets_fail_closed(self):
        for error_code in (
            "RUNTIME_PROJECTION_AMBIGUOUS",
            "RUNTIME_PROJECTION_STALE",
            "CONTRIBUTION_REGISTRATION_CONFLICT",
            "CAPABILITY_UNAVAILABLE",
        ):
            with self.subTest(error_code=error_code):
                dispatcher, policy = self._dispatcher(
                    _Registry(error_code=error_code)
                )
                with self.assertRaises(OrchestrationError) as raised:
                    await dispatcher.dispatch(
                        method="POST",
                        route="status_update",
                        source_event_id="event-one",
                    )
                self.assertEqual(
                    "PROJECT_RUNTIME_PROJECTION_STALE",
                    raised.exception.code,
                )
                self.assertEqual([], policy.calls)

        malformed_targets = (
            object(),
            {"automation_id": "", "generation": 7, "contribution_id": "route"},
            {"automation_id": "project", "generation": True, "contribution_id": "route"},
            {"automation_id": "project", "generation": 7, "contribution_id": " route"},
        )
        for target in malformed_targets:
            with self.subTest(target=target):
                dispatcher, policy = self._dispatcher(_Registry(target=target))
                with self.assertRaises(OrchestrationError) as raised:
                    await dispatcher.dispatch(
                        method="POST",
                        route="status_update",
                        source_event_id="event-one",
                    )
                self.assertEqual(
                    "PROJECT_RUNTIME_PROJECTION_STALE",
                    raised.exception.code,
                )
                self.assertEqual([], policy.calls)

    async def test_replay_is_stable_across_generation_and_scoped_to_owner(self):
        registry = _Registry(target=_Target(generation=7))
        dispatcher, policy = self._dispatcher(registry)
        await dispatcher.dispatch(
            method="POST",
            route="status_update",
            source_event_id="event-one",
        )
        first_kwargs = policy.calls[-1][1]

        registry.target = _Target(generation=8)
        await dispatcher.dispatch(
            method="POST",
            route="status_update",
            source_event_id="event-one",
        )
        upgraded_kwargs = policy.calls[-1][1]
        self.assertEqual(
            first_kwargs["idempotency_key"],
            upgraded_kwargs["idempotency_key"],
        )
        self.assertEqual(first_kwargs["actor"], upgraded_kwargs["actor"])

        registry.target = _Target(
            automation_id="next-owner",
            generation=1,
            contribution_id="receive_status",
        )
        await dispatcher.dispatch(
            method="POST",
            route="status_update",
            source_event_id="event-one",
        )
        next_owner_kwargs = policy.calls[-1][1]
        self.assertNotEqual(
            first_kwargs["idempotency_key"],
            next_owner_kwargs["idempotency_key"],
        )
        self.assertNotEqual(first_kwargs["actor"], next_owner_kwargs["actor"])

    async def test_malformed_policy_result_is_not_exposed(self):
        dispatcher, policy = self._dispatcher(_Registry())
        policy.result = {
            "success": "yes",
            "status": "COMPLETED",
            "error_summary": "sensitive",
        }

        with self.assertRaises(OrchestrationError) as raised:
            await dispatcher.dispatch(
                method="POST",
                route="status_update",
                source_event_id="event-one",
            )

        self.assertEqual("PROJECT_INVOKE_UNAVAILABLE", raised.exception.code)
