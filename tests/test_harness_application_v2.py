from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pytest

from agent.harness.catalog import ManagedToolHandle
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness.sidecar import SidecarResult
from agent.harness_application import (
    FIXED_HARNESS_TOOL_IDS,
    HarnessConversationService,
    MEMORY_ONLY,
    ProductionGatedHarnessSidecar,
    ProductionGatedHarnessSidecarFactory,
    TrustedHarnessInvocationAdapter,
    build_fixed_harness_tools,
)
from agent.orchestration.models import Actor, ActorType


REQUEST_ONE = "00000000-0000-4000-8000-000000000001"
REQUEST_TWO = "00000000-0000-4000-8000-000000000002"
SESSION_REQUEST = "00000000-0000-4000-8000-000000000010"


def admin_actor(
    actor_id: str = "admin-one",
    *,
    roles: tuple[str, ...] = (" Admin ",),
    authenticated_by: str = "mysql_admin_session",
) -> Actor:
    return Actor(
        ActorType.CONSOLE_ADMIN,
        actor_id,
        roles=roles,
        display_name="Offline admin",
        authenticated_by=authenticated_by,
    )


class FakeSidecar:
    def __init__(self, content: str = "offline answer") -> None:
        self.content = content
        self.calls: list[tuple[tuple[HarnessMessage, ...], int]] = []

    def run(
        self,
        *,
        messages: tuple[HarnessMessage, ...] | list[HarnessMessage],
        timeout_seconds: int,
    ) -> SidecarResult:
        self.calls.append((tuple(messages), timeout_seconds))
        return SidecarResult(content=self.content, tool_calls=0)


def test_conversation_binds_exact_admin_and_replays_without_sidecar_rerun() -> None:
    sidecar = FakeSidecar()
    factory_calls: list[tuple[Actor, str]] = []

    def factory(actor: Actor, request_id: str) -> FakeSidecar:
        factory_calls.append((actor, request_id))
        return sidecar

    service = HarnessConversationService(
        repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=factory,
    )
    created = service.create_session(actor=admin_actor(), request_id=SESSION_REQUEST)
    assert created.persistence_status == MEMORY_ONLY
    assert created.replayed is False

    first = service.send_message(
        actor=admin_actor(),
        session_id=created.session_id,
        request_id=REQUEST_ONE,
        message="What is ready?",
    )
    replay = service.send_message(
        actor=admin_actor(roles=("admin",)),
        session_id=created.session_id,
        request_id=REQUEST_ONE,
        message="What is ready?",
    )

    assert len(sidecar.calls) == 1
    assert first.replayed is False
    assert replay.replayed is True
    assert first.user_message.message_id == replay.user_message.message_id
    assert first.assistant_message.message_id == replay.assistant_message.message_id
    assert first.assistant_message.content == replay.assistant_message.content
    assert factory_calls == [(admin_actor(roles=("admin",)), REQUEST_ONE)]
    assert tuple(item.role for item in first.session.messages) == ("user", "assistant")
    assert first.to_dict()["persistence_status"] == MEMORY_ONLY
    assert "principal_id" not in first.to_dict()


def test_conversation_principal_isolation_and_rejects_unsafe_admin_bindings() -> None:
    sidecar = FakeSidecar()
    service = HarnessConversationService(
        repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=lambda _actor, _request_id: sidecar,
    )
    created = service.create_session(actor=admin_actor(), request_id=SESSION_REQUEST)

    with pytest.raises(HarnessError) as mismatch:
        service.send_message(
            actor=admin_actor("admin-two"),
            session_id=created.session_id,
            request_id=REQUEST_ONE,
            message="cross-principal",
        )
    assert mismatch.value.code == "HARNESS_PRINCIPAL_MISMATCH"

    with pytest.raises(HarnessError) as basic:
        service.create_session(
            actor=admin_actor(authenticated_by="basic_auth"),
            request_id=REQUEST_TWO,
        )
    assert basic.value.code == "HARNESS_PRINCIPAL_INVALID"

    with pytest.raises(HarnessError) as non_admin:
        service.create_session(
            actor=admin_actor(roles=("viewer",)),
            request_id=REQUEST_TWO,
        )
    assert non_admin.value.code == "HARNESS_PRINCIPAL_INVALID"


def test_sidecar_factory_receives_each_canonical_request_and_bound_principal() -> None:
    calls: list[tuple[str, str]] = []

    class BoundSidecar(FakeSidecar):
        pass

    def factory(actor: Actor, request_id: str) -> BoundSidecar:
        calls.append((actor.actor_id, request_id))
        return BoundSidecar(actor.actor_id)

    service = HarnessConversationService(
        repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=factory,
    )
    first_session = service.create_session(actor=admin_actor("admin-one"), request_id=SESSION_REQUEST)
    second_session = service.create_session(actor=admin_actor("admin-two"), request_id=REQUEST_TWO)
    service.send_message(
        actor=admin_actor("admin-one"),
        session_id=first_session.session_id,
        request_id=REQUEST_ONE,
        message="one",
    )
    service.send_message(
        actor=admin_actor("admin-two"),
        session_id=second_session.session_id,
        request_id=REQUEST_TWO,
        message="two",
    )
    assert calls == [("admin-one", REQUEST_ONE), ("admin-two", REQUEST_TWO)]


def test_conversation_rejects_reused_request_with_changed_message() -> None:
    service = HarnessConversationService(
        repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=lambda _actor, _request_id: FakeSidecar(),
    )
    created = service.create_session(actor=admin_actor(), request_id=SESSION_REQUEST)
    service.send_message(
        actor=admin_actor(),
        session_id=created.session_id,
        request_id=REQUEST_ONE,
        message="first",
    )

    with pytest.raises(HarnessError) as conflict:
        service.send_message(
            actor=admin_actor(),
            session_id=created.session_id,
            request_id=REQUEST_ONE,
            message="changed",
        )
    assert conflict.value.code == "HARNESS_IDEMPOTENCY_CONFLICT"


def test_production_gated_sidecar_never_falls_back() -> None:
    direct = ProductionGatedHarnessSidecar()
    with pytest.raises(HarnessError) as direct_error:
        direct.run(messages=(HarnessMessage("user", "hello", REQUEST_ONE),), timeout_seconds=5)
    assert direct_error.value.code == "HARNESS_RUNTIME_PRODUCTION_GATED"

    service = HarnessConversationService(
        repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=ProductionGatedHarnessSidecarFactory(),
    )
    created = service.create_session(actor=admin_actor(), request_id=SESSION_REQUEST)
    with pytest.raises(HarnessError) as service_error:
        service.send_message(
            actor=admin_actor(),
            session_id=created.session_id,
            request_id=REQUEST_ONE,
            message="hello",
        )
    assert service_error.value.code == "HARNESS_RUNTIME_PRODUCTION_GATED"


def test_fixed_host_tool_definitions_are_exactly_six_closed_read_only_tools() -> None:
    tools = build_fixed_harness_tools()
    assert tuple(tool.descriptor.tool_id for tool in tools) == FIXED_HARNESS_TOOL_IDS
    assert set(FIXED_HARNESS_TOOL_IDS) == {
        "knowledge.search",
        "waybill.lookup",
        "tracking.lookup",
        "work_items.list_open",
        "runs.get_summary",
        "artifact.inspect",
    }
    for tool in tools:
        schema = tool.descriptor.input_schema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema) == {"type", "properties", "required", "additionalProperties"}
        assert tool.effect in {"read", "compute"}
        assert tool.harness_allowed is True
        assert tool.broker_effect == "read"
        assert repr(tool.opaque_handle) == "<private-harness-handle>"


@dataclass
class Receipt:
    value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.value)


class PolicyRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_harness(self, automation_id: str, **kwargs: Any) -> Receipt:
        self.calls.append({"automation_id": automation_id, **kwargs})
        return Receipt({"ok": True, "command_id": "command-one"})


def test_dynamic_adapter_forwards_exact_identity_generation_and_stable_request_ids() -> None:
    policy = PolicyRecorder()
    actor = admin_actor(roles=("super_admin", "Admin"))
    handle = ManagedToolHandle("project-one", 7, "lookup-one")
    adapter = TrustedHarnessInvocationAdapter(
        policy_service=policy,
        actor=actor,
        base_request_id=REQUEST_ONE,
        allowed_dynamic_handles=(handle,),
    )

    first = adapter.invoke(handle=handle, arguments={})
    second = adapter.invoke(handle=handle, arguments={})
    replayed_adapter = TrustedHarnessInvocationAdapter(
        policy_service=policy,
        actor=actor,
        base_request_id=REQUEST_ONE,
        allowed_dynamic_handles=(handle,),
    )
    replayed_adapter.invoke(handle=handle, arguments={})

    assert first == {"ok": True, "command_id": "command-one"}
    assert second == first
    assert policy.calls[0]["actor"].roles == ("admin", "super_admin")
    assert policy.calls[0]["actor"].actor_id == "admin-one"
    assert policy.calls[0]["expected_automation_generation"] == 7
    assert policy.calls[0]["contribution_id"] == "lookup-one"
    assert policy.calls[0]["request_id"] != policy.calls[1]["request_id"]
    assert policy.calls[0]["request_id"] == policy.calls[2]["request_id"]
    assert adapter.call_count == 2


def test_dynamic_adapter_requires_empty_arguments_and_rejects_unknown_handles() -> None:
    policy = PolicyRecorder()
    handle = ManagedToolHandle("project-one", 7, "lookup-one")
    adapter = TrustedHarnessInvocationAdapter(
        policy_service=policy,
        actor=admin_actor(),
        base_request_id=REQUEST_ONE,
        allowed_dynamic_handles=(handle,),
    )

    with pytest.raises(HarnessError) as argument_error:
        adapter.invoke(handle=handle, arguments={"service": "forbidden"})
    assert argument_error.value.code == "HARNESS_ARGUMENT_INVALID"

    with pytest.raises(HarnessError) as unknown_type:
        adapter.invoke(handle=object(), arguments={})
    assert unknown_type.value.code == "HARNESS_TOOL_NOT_FOUND"

    unknown_handle = ManagedToolHandle("project-two", 7, "lookup-one")
    with pytest.raises(HarnessError) as unknown_dynamic:
        adapter.invoke(handle=unknown_handle, arguments={})
    assert unknown_dynamic.value.code == "HARNESS_TOOL_NOT_FOUND"


def test_fixed_adapter_requires_explicit_handler_and_passes_sanitized_arguments() -> None:
    tools = build_fixed_harness_tools()
    captured: list[Mapping[str, Any]] = []

    def knowledge_handler(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        captured.append(dict(arguments))
        return {"matches": []}

    adapter = TrustedHarnessInvocationAdapter(
        policy_service=PolicyRecorder(),
        actor=admin_actor(),
        base_request_id=REQUEST_ONE,
        fixed_handlers={"knowledge.search": knowledge_handler},
    )
    result = adapter.invoke(
        handle=tools[0].opaque_handle,
        arguments={"query": "status", "limit": 3},
    )
    assert result == {"matches": []}
    assert captured == [{"query": "status", "limit": 3}]

    gated_adapter = TrustedHarnessInvocationAdapter(
        policy_service=PolicyRecorder(),
        actor=admin_actor(),
        base_request_id=REQUEST_ONE,
    )
    with pytest.raises(HarnessError) as unavailable:
        gated_adapter.invoke(
            handle=tools[0].opaque_handle,
            arguments={"query": "status", "limit": 3},
        )
    assert unavailable.value.code == "HARNESS_GATEWAY_UNAVAILABLE"


def test_adapter_rejects_identity_fields_in_nested_receipts() -> None:
    class UnsafePolicy(PolicyRecorder):
        def invoke_harness(self, automation_id: str, **kwargs: Any) -> Mapping[str, Any]:
            del automation_id, kwargs
            return {"ok": True, "data": [{"source_code": "blocked"}]}

    handle = ManagedToolHandle("project-one", 7, "lookup-one")
    adapter = TrustedHarnessInvocationAdapter(
        policy_service=UnsafePolicy(),
        actor=admin_actor(),
        base_request_id=REQUEST_ONE,
        allowed_dynamic_handles=(handle,),
    )
    with pytest.raises(HarnessError) as error:
        adapter.invoke(handle=handle, arguments={})
    assert error.value.code == "HARNESS_PROTOCOL_INVALID"
