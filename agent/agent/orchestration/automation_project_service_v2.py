"""Closed helpers for service-v2 automation contribution routing.

These helpers keep contribution identity and committed target validation out of
the already-large policy service while preserving that service as the single
authorization authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agent.orchestration.models import OrchestrationError
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectContractError,
    AutomationProjectPolicyMode,
    CompiledAutomationProjectContract,
)


_CONTRIBUTION_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789_.-"
)
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def require_service_v2_policy_mode(entry: Any, policy_mode: str) -> None:
    """Keep service-v2 execution automatic while retaining audit evidence."""

    if (
        getattr(entry, "runtime_model", "ACTION_V1") == "SERVICE_V2"
        and policy_mode != AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value
    ):
        raise OrchestrationError(
            "SERVICE_V2_POLICY_FIXED",
            "Service v2 projects always use project full-auto policy; audit is not approval",
        )


def normalize_contribution_id(value: str | None) -> str:
    """Return one bounded contribution id or reject browser-controlled text."""

    contribution_id = str(value or "").strip()
    if contribution_id and (
        len(contribution_id) > 128
        or any(character not in _CONTRIBUTION_CHARACTERS for character in contribution_id)
    ):
        raise OrchestrationError(
            "PROJECT_CONTRIBUTION_INVALID",
            "Automation contribution identity is invalid",
        )
    return contribution_id


def validate_service_v2_event_context(
    context: Mapping[str, Any],
    *,
    request_id: str,
) -> str:
    """Require the two exact, transport-owned facts for managed events."""

    if set(context) != {"event_name", "source_event_id"}:
        raise OrchestrationError(
            "TRUSTED_CONTEXT_INVALID",
            "Managed Event context must contain its exact event and source identity",
        )
    event_name = context.get("event_name")
    source_event_id = context.get("source_event_id")
    if (
        not isinstance(event_name, str)
        or _EVENT_NAME_RE.fullmatch(event_name) is None
        or any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 191
            for value in (event_name, source_event_id)
        )
        or source_event_id != request_id
    ):
        raise OrchestrationError(
            "TRUSTED_CONTEXT_INVALID",
            "Managed Event identities must remain exact and stable",
        )
    return event_name


def validate_active_event_declaration(record: Any, *, event_name: str) -> None:
    """Bind a resolved Event record to one non-durable manifest declaration."""

    declaration = (
        record.get("declaration")
        if isinstance(record, Mapping)
        else getattr(record, "declaration", None)
    )
    declaration_event = (
        declaration.get("event") if isinstance(declaration, Mapping) else None
    )
    if (
        not isinstance(declaration, Mapping)
        or not isinstance(declaration_event, str)
        or _EVENT_NAME_RE.fullmatch(declaration_event) is None
        or declaration_event != event_name
        or declaration.get("durable") is not False
    ):
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation event declaration does not match its active projection",
        )


def require_active_service_v2_contribution(
    registry: Any,
    *,
    automation_id: str,
    generation: int,
    contribution_kind: str,
    contribution_id: str,
    expected_event_name: str | None = None,
) -> None:
    """Recheck one exact committed/ready contribution against the registry."""

    resolve_active = getattr(registry, "resolve_active", None)
    if not callable(resolve_active):
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation project runtime projection is unavailable",
        )
    try:
        record = resolve_active(
            automation_id=automation_id,
            generation=generation,
            contribution_kind=contribution_kind,
            contribution_id=contribution_id,
        )
    except Exception as exc:
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation project runtime projection changed before invocation",
        ) from exc
    fields = (
        "automation_id",
        "generation",
        "contribution_kind",
        "contribution_id",
        "phase",
        "backend_status",
    )
    observed = tuple(
        record.get(field) if isinstance(record, Mapping) else getattr(record, field, None)
        for field in fields
    )
    expected = (
        automation_id,
        generation,
        contribution_kind,
        contribution_id,
        "COMMITTED",
        "READY",
    )
    if observed != expected:
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation project runtime projection identity does not match",
        )
    if expected_event_name is not None:
        validate_active_event_declaration(record, event_name=expected_event_name)


def resolve_invocation_contract_id(
    contract: CompiledAutomationProjectContract,
    *,
    source: AutomationEntrypoint,
    contribution_id: str,
    context: Mapping[str, Any],
) -> str:
    """Resolve exactly one committed route without guessing among contributions."""

    if source is AutomationEntrypoint.SCHEDULER:
        task_id = str(context.get("task_id") or "").strip()
        if not task_id or len(task_id) > 191:
            raise OrchestrationError(
                "PROJECT_SCHEDULE_ID_REQUIRED",
                "Trusted Scheduler invocation requires an exact task identity",
            )
        contract_id = f"scheduler:{task_id}"
        scheduler_contract = contract.invocation_contracts.get(contract_id)
        if contribution_id and (
            scheduler_contract is None
            or scheduler_contract.contribution_id != contribution_id
        ):
            raise OrchestrationError(
                "PROJECT_CONTRIBUTION_REQUIRED",
                "Scheduler route must resolve one exact contribution",
            )
        return contract_id
    if contribution_id:
        return contribution_id
    direct = contract.invocation_contracts.get(source.value)
    if direct is not None and direct.entrypoint == source.value:
        return source.value
    candidates = [
        item.contract_id
        for item in contract.invocation_contracts.values()
        if item.entrypoint == source.value
    ]
    if len(candidates) != 1:
        raise OrchestrationError(
            "PROJECT_CONTRIBUTION_REQUIRED",
            "Requested entrypoint must name one enabled contribution",
        )
    return candidates[0]


def validate_service_v2_compiled_target(
    entry: Any,
    contribution_id: str,
    raw_contract: Mapping[str, Any],
) -> None:
    """Bind a compiled v2 invocation to its immutable manifest target."""

    if getattr(entry, "runtime_model", "ACTION_V1") != "SERVICE_V2":
        return
    target = raw_contract.get("target")
    governance = raw_contract.get("governance")
    invocation = entry.invocation_contracts.get(contribution_id)
    expected_governance = (
        invocation.get("governance") if isinstance(invocation, Mapping) else None
    )
    if (
        not isinstance(target, Mapping)
        or not isinstance(governance, Mapping)
        or not isinstance(invocation, Mapping)
        or not isinstance(expected_governance, Mapping)
        or dict(governance) != dict(expected_governance)
        or set(target)
        != {
            "service",
            "operation",
            "contribution_id",
            "contribution_kind",
        }
        or str(target.get("contribution_id") or "") != contribution_id
        or str(target.get("service") or "")
        != str(invocation.get("service") or "")
        or str(target.get("operation") or "")
        != str(invocation.get("operation") or "")
        or str(target.get("contribution_kind") or "")
        != str(invocation.get("contribution_kind") or "")
    ):
        raise AutomationProjectContractError("PLUGIN_RUNTIME_SNAPSHOT_INVALID")
