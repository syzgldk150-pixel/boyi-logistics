"""Offline contract tests for the credential-free Harness domain core."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import pytest

from agent.harness.catalog import FixedHarnessTool, HarnessToolCatalog, ManagedToolHandle, ToolDescriptor
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness.sidecar import (
    DeterministicHarnessSidecar,
    RestrictedSidecarLauncher,
    SidecarResult,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _descriptor(tool_id: str = "knowledge.search") -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=tool_id,
        title="Knowledge search",
        description="Searches a read-only knowledge projection.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 40}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


class _InvocationPort:
    def __init__(self) -> None:
        self.calls: list[tuple[object, Mapping[str, Any]]] = []

    def invoke(self, *, handle: object, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((handle, dict(arguments)))
        return {"items": ["offline"]}


@dataclass
class _SnapshotProvider:
    records: tuple[Mapping[str, Any], ...]
    resolved: Mapping[str, Any] | None = None
    fail_resolve: bool = False

    def active_snapshot(self) -> tuple[Mapping[str, Any], ...]:
        return self.records

    def resolve_active(
        self,
        automation_id: str,
        generation: int,
        contribution_kind: str,
        contribution_id: str,
    ) -> Mapping[str, Any]:
        assert (automation_id, generation, contribution_kind, contribution_id) == (
            "automation-a",
            3,
            "harness",
            "lookup",
        )
        if self.fail_resolve:
            raise RuntimeError("stale")
        return self.resolved or self.records[0]


def _dynamic_record(**changes: object) -> dict[str, Any]:
    record: dict[str, Any] = {
        "automation_id": "automation-a",
        "generation": 3,
        "contribution_id": "lookup",
        "contribution_kind": "harness",
        "runtime_model": "SERVICE_V2",
        "runtime_permissions": {
            "network": False,
            "browser": False,
            "office": False,
            "file_roles": [],
            "broker_operations": [],
            "max_broker_calls": 0,
        },
        "harness_contract": {
            "id": "lookup",
            "title": "Managed lookup",
            "description": "Reads a managed offline projection.",
            "service": "plugin.offline.lookup@1",
            "operation": "lookup",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "effect": "read",
            "operation_type": "read",
            "harness_allowed": True,
            "broker_effect": "read",
        },
    }
    record.update(changes)
    return record


def _message(role: str, content: str = "hello") -> HarnessMessage:
    return HarnessMessage(role=role, content=content, message_id=_uuid())


def test_sessions_enforce_exact_principal_uuid_idempotency_and_memory_only_status() -> None:
    repo = InMemoryHarnessSessionRepository(max_sessions=2, max_sessions_per_principal=1)
    request = _uuid()
    created = repo.create_or_get(principal_id="signed:one", request_id=request)
    repeated = repo.create_or_get(principal_id="signed:one", request_id=request)

    assert created == repeated
    assert created.persistence_status == "MEMORY_ONLY"
    assert repo.persistence_status == "MEMORY_ONLY"
    with pytest.raises(HarnessError, match="session is unavailable") as denied:
        repo.get(principal_id="signed:two", session_id=created.session_id)
    assert denied.value.code == "HARNESS_SESSION_NOT_FOUND"
    with pytest.raises(HarnessError) as malformed:
        repo.create_or_get(principal_id="signed:one", request_id=request.upper())
    assert malformed.value.code == "HARNESS_ID_INVALID"

    message_request = _uuid()
    message = _message("user")
    assert repo.append_message(
        principal_id="signed:one",
        session_id=created.session_id,
        request_id=message_request,
        message=message,
    ) == message
    assert repo.append_message(
        principal_id="signed:one",
        session_id=created.session_id,
        request_id=message_request,
        message=message,
    ) == message
    with pytest.raises(HarnessError) as conflict:
        repo.append_message(
            principal_id="signed:one",
            session_id=created.session_id,
            request_id=message_request,
            message=_message("user", "different"),
        )
    assert conflict.value.code == "HARNESS_IDEMPOTENCY_CONFLICT"


def test_repository_bounds_are_explicit() -> None:
    repo = InMemoryHarnessSessionRepository(max_sessions=1, max_sessions_per_principal=1)
    repo.create_or_get(principal_id="signed:one", request_id=_uuid())
    with pytest.raises(HarnessError) as exceeded:
        repo.create_or_get(principal_id="signed:two", request_id=_uuid())
    assert exceeded.value.code == "HARNESS_LIMIT_EXCEEDED"


def test_catalog_exposes_only_safe_descriptor_and_sanitized_opaque_invocation() -> None:
    port = _InvocationPort()
    handle = object()
    catalog = HarnessToolCatalog(
        invocation_port=port,
        fixed_tools=(FixedHarnessTool(descriptor=_descriptor(), opaque_handle=handle),),
    )

    assert catalog.public_tools() == (
        {
            "tool_id": "knowledge.search",
            "title": "Knowledge search",
            "description": "Searches a read-only knowledge projection.",
            "input_schema": _descriptor().input_schema,
        },
    )
    assert catalog.invoke(tool_id="knowledge.search", arguments={"query": "status"}) == {
        "items": ["offline"]
    }
    assert port.calls == [(handle, {"query": "status"})]
    with pytest.raises(HarnessError) as identity:
        catalog.invoke(
            tool_id="knowledge.search",
            arguments={"query": "status", "account_id": "not-allowed"},
        )
    assert identity.value.code == "HARNESS_ARGUMENT_INVALID"

    array_descriptor = ToolDescriptor(
        tool_id="knowledge.filters",
        title="Knowledge filters",
        description="Reads a filtered knowledge projection.",
        input_schema={
            "type": "object",
            "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
            "required": ["terms"],
            "additionalProperties": False,
        },
    )
    nested_catalog = HarnessToolCatalog(
        invocation_port=port,
        fixed_tools=(FixedHarnessTool(descriptor=array_descriptor, opaque_handle=handle),),
    )
    with pytest.raises(HarnessError) as nested_identity:
        nested_catalog.invoke(tool_id="knowledge.filters", arguments={"terms": [{"account_id": "no"}]})
    assert nested_identity.value.code == "HARNESS_ARGUMENT_INVALID"


def test_catalog_translates_gateway_failure_without_fallback() -> None:
    class FailingPort:
        def invoke(self, *, handle: object, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            del handle, arguments
            raise RuntimeError("offline fake failure")

    catalog = HarnessToolCatalog(
        invocation_port=FailingPort(),
        fixed_tools=(FixedHarnessTool(descriptor=_descriptor(), opaque_handle=object()),),
    )
    with pytest.raises(HarnessError) as failed:
        catalog.invoke(tool_id="knowledge.search", arguments={"query": "status"})
    assert failed.value.code == "HARNESS_GATEWAY_FAILED"

    with pytest.raises(HarnessError) as unsafe_fixed:
        FixedHarnessTool(descriptor=_descriptor(), opaque_handle=object(), effect="internal_write")
    assert unsafe_fixed.value.code == "HARNESS_TOOL_INVALID"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"runtime_model": "ACTION_V1"}, "HARNESS_TOOL_INVALID"),
        ({"runtime_model": None}, "HARNESS_TOOL_INVALID"),
        ({"runtime_permissions": {}}, "HARNESS_TOOL_INVALID"),
        ({"runtime_permissions": None}, "HARNESS_TOOL_INVALID"),
        (
            {
                "runtime_permissions": {
                    **_dynamic_record()["runtime_permissions"],
                    "network": True,
                }
            },
            "HARNESS_TOOL_INVALID",
        ),
        (
            {
                "runtime_permissions": {
                    **_dynamic_record()["runtime_permissions"],
                    "broker_operations": [{"operation": "http.request"}],
                }
            },
            "HARNESS_TOOL_INVALID",
        ),
        ({"harness_contract": {**_dynamic_record()["harness_contract"], "effect": "internal_write"}}, "HARNESS_TOOL_INVALID"),
        ({"harness_contract": {**_dynamic_record()["harness_contract"], "broker_effect": "write"}}, "HARNESS_TOOL_INVALID"),
    ],
)
def test_dynamic_catalog_rejects_unsafe_package_or_governance(
    changes: Mapping[str, object], code: str
) -> None:
    port = _InvocationPort()
    provider = _SnapshotProvider(records=(_dynamic_record(**changes),))
    with pytest.raises(HarnessError) as blocked:
        HarnessToolCatalog(invocation_port=port, snapshot_provider=provider)
    assert blocked.value.code == code


def test_dynamic_catalog_rejects_collision_and_rechecks_exact_active_generation() -> None:
    port = _InvocationPort()
    record = _dynamic_record()
    provider = _SnapshotProvider(records=(record,))
    catalog = HarnessToolCatalog(invocation_port=port, snapshot_provider=provider)

    public_tool_id = catalog.public_tools()[0]["tool_id"]
    assert public_tool_id.startswith("managed.")
    assert "automation-a" not in public_tool_id
    assert "lookup" not in public_tool_id
    catalog.invoke(tool_id=public_tool_id, arguments={})
    assert port.calls[0] == (ManagedToolHandle("automation-a", 3, "lookup"), {})

    stale_contract = {**record["harness_contract"], "title": "Different title"}
    provider.resolved = {**record, "harness_contract": stale_contract}
    with pytest.raises(HarnessError) as stale:
        catalog.invoke(tool_id=public_tool_id, arguments={})
    assert stale.value.code == "HARNESS_TOOL_STALE"

    with pytest.raises(HarnessError) as collision:
        HarnessToolCatalog(
            invocation_port=port,
            fixed_tools=(
                FixedHarnessTool(
                    descriptor=_descriptor(public_tool_id),
                    opaque_handle=object(),
                ),
            ),
            snapshot_provider=_SnapshotProvider(records=(record,)),
        )
    assert collision.value.code == "HARNESS_TOOL_COLLISION"


class _SequencedModel:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[Mapping[str, Any]] = []

    def respond(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.requests.append(request)
        return self.responses.pop(0)


def test_restricted_sidecar_runs_offline_tool_loop_without_identity_surfaces() -> None:
    port = _InvocationPort()
    catalog = HarnessToolCatalog(
        invocation_port=port,
        fixed_tools=(FixedHarnessTool(descriptor=_descriptor(), opaque_handle="gateway:knowledge"),),
    )
    model = _SequencedModel(
        [
            {"type": "tool_call", "tool_id": "knowledge.search", "arguments": {"query": "offline"}},
            {"type": "final", "content": "done"},
        ]
    )
    result = DeterministicHarnessSidecar(catalog=catalog, model=model).run(messages=(_message("user"),))

    assert result.content == "done"
    assert result.tool_calls == 1
    assert port.calls == [("gateway:knowledge", {"query": "offline"})]
    assert "automation_id" not in str(model.requests)
    assert "account_id" not in str(model.requests)


def test_sidecar_result_enforces_utf8_byte_limit() -> None:
    assert SidecarResult(content="x" * 8_192, tool_calls=0).content == "x" * 8_192
    assert SidecarResult(content="界" * 2_730, tool_calls=0).content == "界" * 2_730

    for content in ("x" * 8_193, "界" * 2_731):
        with pytest.raises(HarnessError) as oversized:
            SidecarResult(content=content, tool_calls=0)
        assert oversized.value.code == "HARNESS_PROTOCOL_INVALID"


def test_sidecar_fails_closed_on_identity_protocol_limit_and_sandbox() -> None:
    port = _InvocationPort()
    catalog = HarnessToolCatalog(
        invocation_port=port,
        fixed_tools=(FixedHarnessTool(descriptor=_descriptor(), opaque_handle=object()),),
    )
    forbidden = _SequencedModel(
        [{"type": "tool_call", "tool_id": "knowledge.search", "arguments": {"automation_id": "no"}}]
    )
    with pytest.raises(HarnessError) as identity:
        DeterministicHarnessSidecar(catalog=catalog, model=forbidden).run(messages=(_message("user"),))
    assert identity.value.code == "HARNESS_PROTOCOL_INVALID"

    loop = _SequencedModel(
        [{"type": "tool_call", "tool_id": "knowledge.search", "arguments": {"query": "q"}}] * 9
    )
    with pytest.raises(HarnessError) as limited:
        DeterministicHarnessSidecar(catalog=catalog, model=loop).run(messages=(_message("user"),))
    assert limited.value.code == "HARNESS_LIMIT_EXCEEDED"

    launcher = RestrictedSidecarLauncher()
    assert launcher.profile.inherited_environment == {}
    assert launcher.profile.network_enabled is False
    assert launcher.profile.repository_mounts == ()
    with pytest.raises(HarnessError) as unavailable:
        launcher.launch({"irrelevant": True})
    assert unavailable.value.code == "HARNESS_SANDBOX_UNAVAILABLE"
