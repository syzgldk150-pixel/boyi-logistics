from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from agent.automation_plugins.manifest import canonical_json_bytes
from agent.orchestration.models import (
    Actor,
    ActorType,
    CommandReceipt,
    OrchestrationError,
    RunStatus,
)
from agent.orchestration.service_v2_waybill_entry_extension_host import (
    ServiceV2WaybillEntryExtensionHost,
)
from shared.automation_project_authorization import AutomationEntrypoint
from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_ACTIONS_SLOT,
    WAYBILL_ENTRY_DRAFT_FIELDS,
    WAYBILL_ENTRY_VALIDATORS_SLOT,
)


REQUEST_ID = "11111111-1111-4111-8111-111111111111"
HANDLE = "a" * 64


def _admin() -> Actor:
    return Actor(
        ActorType.CONSOLE_ADMIN,
        "admin-1",
        roles=("admin",),
        authenticated_by="mysql_admin_session",
    )


def _draft(**overrides: str) -> dict[str, str]:
    result = {field: "" for field in WAYBILL_ENTRY_DRAFT_FIELDS}
    result.update(overrides)
    return result


def _target(slot: str) -> SimpleNamespace:
    declaration = {
        "id": "waybill-extension",
        "slot": slot,
        "title": "Waybill extension",
        "service": "plugin.waybill@1",
        "operation": "invoke",
        "default_enabled": True,
    }
    return SimpleNamespace(
        automation_id="waybill-project",
        generation=7,
        contribution_id="waybill-extension",
        contribution_kind="module_slots",
        slot=slot,
        handle=HANDLE,
        declaration_sha256=hashlib.sha256(canonical_json_bytes(declaration)).hexdigest(),
        service="plugin.waybill@1",
        operation="invoke",
        declaration=declaration,
    )


class _Registry:
    def __init__(self, slot: str) -> None:
        self.target = _target(slot)
        self.resolve_calls: list[dict[str, str]] = []

    def active_module_slot_snapshot(self) -> tuple[dict[str, str], ...]:
        return (
            {
                "slot": self.target.slot,
                "handle": self.target.handle,
                "title": self.target.declaration["title"],
            },
        )

    def resolve_active_module_slot(self, *, slot: str, handle: str) -> SimpleNamespace:
        self.resolve_calls.append({"slot": slot, "handle": handle})
        if slot != self.target.slot or handle != self.target.handle:
            raise RuntimeError("stale target")
        return self.target


class _Policy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_trusted(self, automation_id: str, **kwargs: Any) -> CommandReceipt:
        self.calls.append((automation_id, kwargs))
        return CommandReceipt(
            command_id="command-1",
            work_item_id="work-1",
            run_id="run-1",
            status=RunStatus.RECEIVED,
            reused=False,
        )


class _Gateway:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, float]] = []

    async def wait_for_run(self, run_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        self.calls.append((run_id, timeout_seconds))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _SnapshotRegistry:
    def __init__(
        self,
        *snapshots: tuple[dict[str, str], ...],
    ) -> None:
        self.snapshots = snapshots
        self.calls: list[str | None] = []

    def active_module_slot_snapshot(
        self,
        *,
        slot: str | None = None,
    ) -> tuple[dict[str, str], ...]:
        self.calls.append(slot)
        index = min(len(self.calls) - 1, len(self.snapshots) - 1)
        return self.snapshots[index]


def _host(
    slot: str,
    *,
    run: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> tuple[ServiceV2WaybillEntryExtensionHost, _Registry, _Policy, _Gateway]:
    registry = _Registry(slot)
    policy = _Policy()
    gateway = _Gateway(run, error)
    host = ServiceV2WaybillEntryExtensionHost(
        policy_service=policy,  # type: ignore[arg-type]
        contribution_registry=registry,
        command_gateway=gateway,  # type: ignore[arg-type]
        validator_timeout_seconds=2.5,
    )
    return host, registry, policy, gateway


def test_host_lists_only_safe_flat_module_slot_projection() -> None:
    host, _registry, _policy, _gateway = _host(WAYBILL_ENTRY_ACTIONS_SLOT)

    assert host.list_module_slots(actor=_admin()) == {
        "module_slots": [
            {
                "slot": WAYBILL_ENTRY_ACTIONS_SLOT,
                "handle": HANDLE,
                "title": "Waybill extension",
            }
        ]
    }


def test_action_derives_target_and_idempotency_from_signed_actor_and_route() -> None:
    host, _registry, policy, gateway = _host(WAYBILL_ENTRY_ACTIONS_SLOT)

    first = asyncio.run(
        host.invoke(
            slot=WAYBILL_ENTRY_ACTIONS_SLOT,
            handle=HANDLE,
            request_id=REQUEST_ID,
            waybill=_draft(goods_name_lines="first"),
            actor=_admin(),
        )
    )
    second = asyncio.run(
        host.invoke(
            slot=WAYBILL_ENTRY_ACTIONS_SLOT,
            handle=HANDLE,
            request_id=REQUEST_ID,
            waybill=_draft(goods_name_lines="changed"),
            actor=_admin(),
        )
    )

    assert (
        first
        == second
        == {
            "kind": "action",
            "receipt": {
                "command_id": "command-1",
                "work_item_id": "work-1",
                "run_id": "run-1",
                "status": "RECEIVED",
                "reused": False,
                "next_poll_after_ms": 1000,
            },
        }
    )
    assert gateway.calls == []
    assert policy.calls[0][0] == "waybill-project"
    first_call = policy.calls[0][1]
    second_call = policy.calls[1][1]
    assert first_call["entrypoint"] is AutomationEntrypoint.MODULE_SLOTS
    assert first_call["expected_automation_generation"] == 7
    assert first_call["contribution_id"] == "waybill-extension"
    assert first_call["idempotency_key"] == second_call["idempotency_key"]
    assert first_call["trusted_context"] == {
        "module_slot": {"slot": WAYBILL_ENTRY_ACTIONS_SLOT, "handle": HANDLE},
        "dynamic_inputs": {"waybill": _draft(goods_name_lines="first")},
    }
    assert second_call["trusted_context"]["dynamic_inputs"]["waybill"]["goods_name_lines"] == "changed"


def test_validator_waits_for_unique_closed_result() -> None:
    run = {
        "status": "COMPLETED",
        "steps": [
            {
                "status": "COMPLETED",
                "result_summary_json": {
                    "status": "SUCCESS",
                    "data": {
                        "valid": False,
                        "issues": [
                            {
                                "code": "DESTINATION_REQUIRED",
                                "message": "Destination is required",
                                "field": "destination_site",
                                "severity": "error",
                            }
                        ],
                    },
                    "meta": {},
                    "warnings": [],
                    "error": None,
                },
            }
        ],
    }
    host, _registry, _policy, gateway = _host(WAYBILL_ENTRY_VALIDATORS_SLOT, run=run)

    result = asyncio.run(
        host.invoke(
            slot=WAYBILL_ENTRY_VALIDATORS_SLOT,
            handle=HANDLE,
            request_id=REQUEST_ID,
            waybill=_draft(),
            actor=_admin(),
        )
    )

    assert result == {"kind": "validator", "validation": run["steps"][0]["result_summary_json"]["data"]}
    assert gateway.calls == [("run-1", 2.5)]


def test_active_validator_set_runs_one_snapshot_and_returns_closed_aggregate() -> None:
    first = {
        "slot": WAYBILL_ENTRY_VALIDATORS_SLOT,
        "handle": "a" * 64,
        "title": "First validator",
    }
    second = {
        "slot": WAYBILL_ENTRY_VALIDATORS_SLOT,
        "handle": "b" * 64,
        "title": "Second validator",
    }
    snapshot = (first, second)
    registry = _SnapshotRegistry(snapshot, snapshot)
    host = ServiceV2WaybillEntryExtensionHost(
        policy_service=_Policy(),  # type: ignore[arg-type]
        contribution_registry=registry,
        command_gateway=_Gateway(),  # type: ignore[arg-type]
    )
    calls: list[dict[str, Any]] = []

    async def invoke(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["handle"] == first["handle"]:
            return {
                "kind": "validator",
                "validation": {
                    "valid": True,
                    "issues": [
                        {
                            "code": "CHECKED",
                            "message": "Checked by first validator",
                            "field": None,
                            "severity": "warning",
                        }
                    ],
                },
            }
        return {
            "kind": "validator",
            "validation": {
                "valid": False,
                "issues": [
                    {
                        "code": "DESTINATION_REQUIRED",
                        "message": "Destination is required",
                        "field": "destination_site",
                        "severity": "error",
                    }
                ],
            },
        }

    host.invoke = invoke  # type: ignore[method-assign]

    result = asyncio.run(
        host.invoke_active_validators(
            request_id=REQUEST_ID,
            waybill=_draft(),
            actor=_admin(),
        )
    )

    assert result == {
        "kind": "validator_set",
        "validation": {
            "valid": False,
            "issues": [
                {
                    "code": "CHECKED",
                    "message": "Checked by first validator",
                    "field": None,
                    "severity": "warning",
                },
                {
                    "code": "DESTINATION_REQUIRED",
                    "message": "Destination is required",
                    "field": "destination_site",
                    "severity": "error",
                },
            ],
        },
    }
    assert registry.calls == [WAYBILL_ENTRY_VALIDATORS_SLOT, WAYBILL_ENTRY_VALIDATORS_SLOT]
    assert [call["handle"] for call in calls] == [first["handle"], second["handle"]]
    assert all(call["request_id"] == REQUEST_ID and call["actor"] == _admin() for call in calls)


def test_active_validator_set_accepts_one_stable_empty_snapshot() -> None:
    registry = _SnapshotRegistry((), ())
    host = ServiceV2WaybillEntryExtensionHost(
        policy_service=_Policy(),  # type: ignore[arg-type]
        contribution_registry=registry,
        command_gateway=_Gateway(),  # type: ignore[arg-type]
    )

    result = asyncio.run(
        host.invoke_active_validators(
            request_id=REQUEST_ID,
            waybill=_draft(),
            actor=_admin(),
        )
    )

    assert result == {
        "kind": "validator_set",
        "validation": {"valid": True, "issues": []},
    }


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (("a",), ("a", "b")),
        (("a",), ()),
        (("a",), ("b",)),
    ],
    ids=("activation", "uninstall", "generation-switch"),
)
def test_active_validator_set_fails_closed_when_snapshot_drifts(
    before: tuple[str, ...],
    after: tuple[str, ...],
) -> None:
    def rows(handles: tuple[str, ...]) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "slot": WAYBILL_ENTRY_VALIDATORS_SLOT,
                "handle": handle * 64,
                "title": f"Validator {handle}",
            }
            for handle in handles
        )

    registry = _SnapshotRegistry(rows(before), rows(after))
    host = ServiceV2WaybillEntryExtensionHost(
        policy_service=_Policy(),  # type: ignore[arg-type]
        contribution_registry=registry,
        command_gateway=_Gateway(),  # type: ignore[arg-type]
    )

    async def invoke(**_kwargs: Any) -> dict[str, Any]:
        return {"kind": "validator", "validation": {"valid": True, "issues": []}}

    host.invoke = invoke  # type: ignore[method-assign]

    with pytest.raises(OrchestrationError) as raised:
        asyncio.run(
            host.invoke_active_validators(
                request_id=REQUEST_ID,
                waybill=_draft(),
                actor=_admin(),
            )
        )

    assert raised.value.code == "PROJECT_RUNTIME_PROJECTION_STALE"
    assert registry.calls == [WAYBILL_ENTRY_VALIDATORS_SLOT, WAYBILL_ENTRY_VALIDATORS_SLOT]


@pytest.mark.parametrize(
    ("run", "error", "code"),
    [
        (
            {"status": "COMPLETED", "steps": []},
            None,
            "WAYBILL_EXTENSION_RESULT_INVALID",
        ),
        (
            {
                "status": "COMPLETED",
                "steps": [
                    {
                        "status": "COMPLETED",
                        "result_summary_json": {
                            "status": "SUCCESS",
                            "data": {"valid": True, "issues": [], "html": "<script>"},
                            "meta": {},
                            "warnings": [],
                            "error": None,
                        },
                    }
                ],
            },
            None,
            "WAYBILL_EXTENSION_RESULT_INVALID",
        ),
        (
            None,
            OrchestrationError("RUN_WAIT_TIMEOUT", "synthetic timeout"),
            "WAYBILL_EXTENSION_TIMEOUT",
        ),
    ],
)
def test_validator_malformed_or_timeout_fails_closed(
    run: dict[str, Any] | None,
    error: Exception | None,
    code: str,
) -> None:
    host, _registry, _policy, _gateway = _host(WAYBILL_ENTRY_VALIDATORS_SLOT, run=run, error=error)

    with pytest.raises(OrchestrationError) as raised:
        asyncio.run(
            host.invoke(
                slot=WAYBILL_ENTRY_VALIDATORS_SLOT,
                handle=HANDLE,
                request_id=REQUEST_ID,
                waybill=_draft(),
                actor=_admin(),
            )
        )

    assert raised.value.code == code


def test_host_rejects_unsigned_actor_and_non_closed_waybill_before_policy() -> None:
    host, _registry, policy, _gateway = _host(WAYBILL_ENTRY_ACTIONS_SLOT)
    unsigned = Actor(ActorType.CONSOLE_ADMIN, "admin-1", roles=("admin",))

    with pytest.raises(OrchestrationError) as forbidden:
        host.list_module_slots(actor=unsigned)
    assert forbidden.value.code == "ACTION_FORBIDDEN"

    with pytest.raises(OrchestrationError) as invalid:
        asyncio.run(
            host.invoke(
                slot=WAYBILL_ENTRY_ACTIONS_SLOT,
                handle=HANDLE,
                request_id=REQUEST_ID,
                waybill={**_draft(), "automation_id": "forged"},
                actor=_admin(),
            )
        )
    assert invalid.value.code == "WAYBILL_EXTENSION_REQUEST_INVALID"
    assert policy.calls == []
