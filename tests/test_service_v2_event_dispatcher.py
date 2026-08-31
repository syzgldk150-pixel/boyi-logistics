from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from unittest import IsolatedAsyncioTestCase

from agent.automation_plugins.manifest import canonical_json_bytes
from agent.orchestration.automation_project_entrypoints import (
    ServiceV2EventDispatcher,
)
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.automation_project_authorization import AutomationEntrypoint


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class _Target:
    automation_id: str = "service-instance"
    generation: int = 7
    contribution_id: str = "handle_created"


class _Registry:
    def __init__(self, *, target: object | None = _Target(), error: bool = False) -> None:
        self.target = target
        self.error = error
        self.calls: list[str] = []

    def resolve_active_event(self, *, event_name: str) -> object | None:
        self.calls.append(event_name)
        if self.error:
            raise RuntimeError("synthetic registry failure")
        return self.target


class _Policy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []
        self.result: object = {
            "success": True,
            "status": "COMPLETED",
            "command_id": "must-not-leak",
            "run_id": "must-not-leak",
        }

    async def invoke_trusted_and_wait(
        self,
        automation_id: object,
        **kwargs: object,
    ) -> object:
        self.calls.append((automation_id, dict(kwargs)))
        return self.result


class ServiceV2EventDispatcherTests(IsolatedAsyncioTestCase):
    @staticmethod
    def _dispatcher(
        registry: _Registry,
    ) -> tuple[ServiceV2EventDispatcher, _Policy]:
        policy = _Policy()
        dispatcher = ServiceV2EventDispatcher(
            policy_service=policy,  # type: ignore[arg-type]
            contribution_registry=registry,
        )
        return dispatcher, policy

    def test_registry_must_expose_exact_event_resolution(self):
        with self.assertRaises(TypeError):
            ServiceV2EventDispatcher(
                policy_service=_Policy(),  # type: ignore[arg-type]
                contribution_registry=object(),
            )

    async def test_unknown_event_returns_none_before_source_id_validation(self):
        registry = _Registry(target=None)
        dispatcher, policy = self._dispatcher(registry)

        result = await dispatcher.dispatch(
            event_name="shipment.created",
            source_event_id="",
        )

        self.assertIsNone(result)
        self.assertEqual(["shipment.created"], registry.calls)
        self.assertEqual([], policy.calls)

    async def test_interface_rejects_transport_and_business_overrides(self):
        dispatcher, policy = self._dispatcher(_Registry())
        self.assertEqual(
            {"self", "event_name", "source_event_id"},
            set(inspect.signature(ServiceV2EventDispatcher.dispatch).parameters),
        )
        for field in (
            "payload",
            "envelope",
            "dynamic_inputs",
            "actor",
            "automation_id",
            "project",
            "service",
            "operation",
            "arguments",
            "account_id",
            "resource_id",
            "durable",
            "occurred_at",
        ):
            with self.subTest(field=field), self.assertRaises(TypeError):
                await dispatcher.dispatch(
                    event_name="shipment.created",
                    source_event_id="event-one",
                    **{field: "override"},
                )
        self.assertEqual([], policy.calls)

    async def test_exact_event_uses_registry_identity_and_returns_safe_result(self):
        registry = _Registry()
        dispatcher, policy = self._dispatcher(registry)

        result = await dispatcher.dispatch(
            event_name="shipment.created",
            source_event_id="event-one",
        )

        owner = {
            "automation_id": "service-instance",
            "contribution_id": "handle_created",
            "event_name": "shipment.created",
        }
        owner_digest = _digest(owner)
        event_digest = _digest({"owner": owner, "source_event_id": "event-one"})
        self.assertEqual({"success": True, "status": "COMPLETED"}, result)
        self.assertEqual(["shipment.created"], registry.calls)
        self.assertEqual(1, len(policy.calls))
        automation_id, kwargs = policy.calls[0]
        self.assertEqual("service-instance", automation_id)
        self.assertEqual(
            {
                "entrypoint": AutomationEntrypoint.EVENTS,
                "request_id": "event-one",
                "actor": Actor(
                    ActorType.EVENT,
                    f"event:{owner_digest}",
                    authenticated_by="managed_event_dispatcher",
                ),
                "trusted_context": {
                    "event_name": "shipment.created",
                    "source_event_id": "event-one",
                },
                "idempotency_key": f"event:v2:{event_digest}",
                "expected_automation_generation": 7,
                "contribution_id": "handle_created",
            },
            kwargs,
        )

    async def test_matching_event_requires_exact_bounded_identities(self):
        for field_name in ("event_name", "source_event_id"):
            for invalid in (None, "", " value", "value ", "x" * 192):
                with self.subTest(field_name=field_name, invalid=invalid):
                    dispatcher, policy = self._dispatcher(_Registry())
                    values = {
                        "event_name": "shipment.created",
                        "source_event_id": "event-one",
                    }
                    values[field_name] = invalid  # type: ignore[assignment]
                    with self.assertRaises(OrchestrationError) as raised:
                        await dispatcher.dispatch(**values)  # type: ignore[arg-type]
                    self.assertEqual("STABLE_EVENT_ID_REQUIRED", raised.exception.code)
                    self.assertEqual([], policy.calls)

    async def test_registry_failure_and_malformed_target_fail_closed(self):
        dispatcher, policy = self._dispatcher(_Registry(error=True))
        with self.assertRaises(OrchestrationError) as raised:
            await dispatcher.dispatch(
                event_name="shipment.created",
                source_event_id="event-one",
            )
        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual([], policy.calls)

        malformed_targets = (
            object(),
            {"automation_id": "", "generation": 7, "contribution_id": "event"},
            {"automation_id": "project", "generation": True, "contribution_id": "event"},
            {"automation_id": "project", "generation": 7, "contribution_id": " event"},
        )
        for target in malformed_targets:
            with self.subTest(target=target):
                dispatcher, policy = self._dispatcher(_Registry(target=target))
                with self.assertRaises(OrchestrationError) as raised:
                    await dispatcher.dispatch(
                        event_name="shipment.created",
                        source_event_id="event-one",
                    )
                self.assertEqual(
                    "PROJECT_RUNTIME_PROJECTION_STALE",
                    raised.exception.code,
                )
                self.assertEqual([], policy.calls)

    async def test_replay_is_cross_generation_stable_and_owner_scoped(self):
        registry = _Registry(target=_Target(generation=7))
        dispatcher, policy = self._dispatcher(registry)
        await dispatcher.dispatch(
            event_name="shipment.created",
            source_event_id="event-one",
        )
        first = policy.calls[-1][1]

        registry.target = _Target(generation=8)
        await dispatcher.dispatch(
            event_name="shipment.created",
            source_event_id="event-one",
        )
        upgraded = policy.calls[-1][1]
        self.assertEqual(first["idempotency_key"], upgraded["idempotency_key"])
        self.assertEqual(first["actor"], upgraded["actor"])

        registry.target = _Target(
            automation_id="next-owner",
            generation=1,
            contribution_id="handle_created",
        )
        await dispatcher.dispatch(
            event_name="shipment.created",
            source_event_id="event-one",
        )
        next_owner = policy.calls[-1][1]
        self.assertNotEqual(first["idempotency_key"], next_owner["idempotency_key"])
        self.assertNotEqual(first["actor"], next_owner["actor"])

    async def test_malformed_policy_result_is_not_exposed(self):
        dispatcher, policy = self._dispatcher(_Registry())
        policy.result = {"success": "yes", "status": "COMPLETED", "secret": "x"}
        with self.assertRaises(OrchestrationError) as raised:
            await dispatcher.dispatch(
                event_name="shipment.created",
                source_event_id="event-one",
            )
        self.assertEqual("PROJECT_INVOKE_UNAVAILABLE", raised.exception.code)
