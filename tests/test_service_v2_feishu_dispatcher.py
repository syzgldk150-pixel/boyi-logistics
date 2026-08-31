from __future__ import annotations

from dataclasses import dataclass
from unittest import IsolatedAsyncioTestCase

from agent.automation_plugins.errors import PluginConflictError
from agent.orchestration.automation_project_entrypoints import (
    ServiceV2FeishuDispatcher,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    OrchestrationError,
)
from shared.automation_project_authorization import AutomationEntrypoint


@dataclass(frozen=True)
class _Target:
    automation_id: str = "service-instance"
    generation: int = 7
    contribution_id: str = "lookup_command"


class _Registry:
    def __init__(
        self,
        *,
        target: _Target | None = _Target(),
        error_code: str | None = None,
    ) -> None:
        self.target = target
        self.error_code = error_code
        self.commands: list[str] = []

    def resolve_active_feishu_command(self, command: str) -> _Target | None:
        self.commands.append(command)
        if self.error_code is not None:
            raise PluginConflictError(
                "synthetic registry resolution failure",
                code=self.error_code,
            )
        return self.target


class _Policy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []

    async def invoke_trusted_and_wait(
        self,
        automation_id: object,
        **kwargs: object,
    ) -> dict[str, object]:
        self.calls.append((automation_id, dict(kwargs)))
        return {"status": "COMPLETED", "success": True}


class ServiceV2FeishuDispatcherTests(IsolatedAsyncioTestCase):
    def _dispatcher(
        self,
        registry: _Registry,
        *,
        resolve_actor=None,
    ) -> tuple[ServiceV2FeishuDispatcher, _Policy, list[str]]:
        policy = _Policy()
        actor_calls: list[str] = []

        def default_resolver(sender_id: str) -> Actor:
            actor_calls.append(sender_id)
            return Actor(
                ActorType.FEISHU_USER,
                sender_id,
                authenticated_by="feishu_verified_event",
            )

        dispatcher = ServiceV2FeishuDispatcher(
            policy_service=policy,
            contribution_registry=registry,
            resolve_actor=resolve_actor or default_resolver,
        )
        return dispatcher, policy, actor_calls

    async def test_unknown_command_returns_none_without_resolving_identity(self):
        registry = _Registry(error_code="CAPABILITY_UNAVAILABLE")
        dispatcher, policy, actor_calls = self._dispatcher(registry)

        result = await dispatcher.dispatch(
            command_text="unknown",
            event_id="",
            sender_id="",
            chat_id="",
        )

        self.assertIsNone(result)
        self.assertEqual(["unknown"], registry.commands)
        self.assertEqual([], actor_calls)
        self.assertEqual([], policy.calls)

    async def test_dispatch_passes_only_registry_identity_and_transport_facts(self):
        registry = _Registry()
        dispatcher, policy, actor_calls = self._dispatcher(registry)

        result = await dispatcher.dispatch(
            command_text="精确命令",
            event_id="event-one",
            sender_id="sender-one",
            chat_id="chat-one",
        )

        self.assertEqual({"status": "COMPLETED", "success": True}, result)
        self.assertEqual(["精确命令"], registry.commands)
        self.assertEqual(["sender-one"], actor_calls)
        self.assertEqual(1, len(policy.calls))
        automation_id, kwargs = policy.calls[0]
        self.assertEqual("service-instance", automation_id)
        self.assertEqual(
            {
                "entrypoint": AutomationEntrypoint.FEISHU,
                "request_id": "event-one",
                "actor": Actor(
                    ActorType.FEISHU_USER,
                    "sender-one",
                    authenticated_by="feishu_verified_event",
                ),
                "trusted_context": {
                    "event_id": "event-one",
                    "chat_id": "chat-one",
                },
                "idempotency_key": "feishu:event-one",
                "expected_automation_generation": 7,
                "contribution_id": "lookup_command",
            },
            kwargs,
        )

    async def test_matching_command_requires_bounded_stable_transport_identity(self):
        for field_name in ("event_id", "sender_id", "chat_id"):
            for invalid_value in ("", "x" * 192):
                with self.subTest(field_name=field_name, value_length=len(invalid_value)):
                    dispatcher, policy, _actor_calls = self._dispatcher(_Registry())
                    values = {
                        "event_id": "event-one",
                        "sender_id": "sender-one",
                        "chat_id": "chat-one",
                    }
                    values[field_name] = invalid_value

                    with self.assertRaises(OrchestrationError) as raised:
                        await dispatcher.dispatch(
                            command_text="精确命令",
                            **values,
                        )

                    self.assertEqual("STABLE_EVENT_ID_REQUIRED", raised.exception.code)
                    self.assertEqual([], policy.calls)

    async def test_actor_must_come_from_injected_matching_sender_resolver(self):
        resolver_calls: list[str] = []

        def wrong_actor(sender_id: str) -> Actor:
            resolver_calls.append(sender_id)
            return Actor(
                ActorType.FEISHU_USER,
                "different-sender",
                authenticated_by="feishu_verified_event",
            )

        dispatcher, policy, _actor_calls = self._dispatcher(
            _Registry(),
            resolve_actor=wrong_actor,
        )

        with self.assertRaises(OrchestrationError) as raised:
            await dispatcher.dispatch(
                command_text="精确命令",
                event_id="event-one",
                sender_id="sender-one",
                chat_id="chat-one",
            )

        self.assertEqual("TRUSTED_ENTRYPOINT_REQUIRED", raised.exception.code)
        self.assertEqual(["sender-one"], resolver_calls)
        self.assertEqual([], policy.calls)

    async def test_ambiguous_runtime_projection_fails_closed(self):
        dispatcher, policy, actor_calls = self._dispatcher(
            _Registry(error_code="RUNTIME_PROJECTION_AMBIGUOUS")
        )

        with self.assertRaises(OrchestrationError) as raised:
            await dispatcher.dispatch(
                command_text="精确命令",
                event_id="event-one",
                sender_id="sender-one",
                chat_id="chat-one",
            )

        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual([], actor_calls)
        self.assertEqual([], policy.calls)
