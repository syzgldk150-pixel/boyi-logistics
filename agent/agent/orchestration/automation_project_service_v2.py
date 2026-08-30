"""Closed helpers for service-v2 automation contribution routing.

These helpers keep contribution identity and committed target validation out of
the already-large policy service while preserving that service as the single
authorization authority.
"""

from __future__ import annotations

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
    invocation = entry.invocation_contracts.get(contribution_id)
    if (
        not isinstance(target, Mapping)
        or not isinstance(invocation, Mapping)
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
