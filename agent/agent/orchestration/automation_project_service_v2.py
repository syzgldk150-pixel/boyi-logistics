"""Closed helpers for service-v2 automation contribution routing.

These helpers keep contribution identity and committed target validation out of
the already-large policy service while preserving that service as the single
authorization authority.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from agent.automation_plugins.manifest import canonical_json_bytes
from agent.orchestration.models import OrchestrationError
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectContractError,
    AutomationProjectPolicyMode,
    CompiledAutomationProjectContract,
)
from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD,
    WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID,
    normalize_waybill_entry_draft,
    normalize_waybill_entry_extension_handle,
    normalize_waybill_entry_slot,
)


_CONTRIBUTION_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.-")
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MODULE_SLOT_DECLARATION_FIELDS = frozenset({"id", "slot", "title", "service", "operation", "default_enabled"})


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


def normalize_service_v2_module_slot_context(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize the only two code-owned facts accepted from the module host."""

    if set(context) != {"module_slot", "dynamic_inputs"}:
        raise OrchestrationError(
            "TRUSTED_CONTEXT_INVALID",
            "Module-slot context must contain only its route and waybill snapshot",
        )
    route = context.get("module_slot")
    dynamic_inputs = context.get("dynamic_inputs")
    if (
        not isinstance(route, Mapping)
        or set(route) != {"slot", "handle"}
        or not isinstance(dynamic_inputs, Mapping)
        or set(dynamic_inputs) != {WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD}
    ):
        raise OrchestrationError(
            "TRUSTED_CONTEXT_INVALID",
            "Module-slot route or dynamic inputs are invalid",
        )
    try:
        slot = normalize_waybill_entry_slot(route.get("slot"))
        handle = normalize_waybill_entry_extension_handle(route.get("handle"))
        waybill = normalize_waybill_entry_draft(dynamic_inputs.get(WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD))
    except ValueError as exc:
        raise OrchestrationError(
            "TRUSTED_CONTEXT_INVALID",
            "Module-slot route or waybill snapshot is invalid",
        ) from exc
    return {
        "module_slot": {"slot": slot, "handle": handle},
        "dynamic_inputs": {WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD: waybill},
    }


def _record_value(record: Any, field: str) -> Any:
    return record.get(field) if isinstance(record, Mapping) else getattr(record, field, None)


def resolve_active_service_v2_module_slot(
    registry: Any,
    *,
    slot: str,
    handle: str,
    automation_id: str | None = None,
    generation: int | None = None,
    contribution_id: str | None = None,
) -> Any:
    """Resolve and verify one exact active module-slot dispatch target."""

    try:
        safe_slot = normalize_waybill_entry_slot(slot)
        safe_handle = normalize_waybill_entry_extension_handle(handle)
    except ValueError as exc:
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation module-slot identity is invalid",
        ) from exc
    resolve = getattr(registry, "resolve_active_module_slot", None)
    if not callable(resolve):
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation module-slot projection is unavailable",
        )
    try:
        target = resolve(slot=safe_slot, handle=safe_handle)
    except Exception as exc:
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation module-slot projection changed before invocation",
        ) from exc
    declaration = _record_value(target, "declaration")
    target_id = _record_value(target, "contribution_id")
    target_generation = _record_value(target, "generation")
    target_automation = _record_value(target, "automation_id")
    target_service = _record_value(target, "service")
    target_operation = _record_value(target, "operation")
    declared_digest = _record_value(target, "declaration_sha256")
    observed_digest = (
        hashlib.sha256(canonical_json_bytes(dict(declaration))).hexdigest() if isinstance(declaration, Mapping) else ""
    )
    if (
        _record_value(target, "contribution_kind") != "module_slots"
        or _record_value(target, "slot") != safe_slot
        or _record_value(target, "handle") != safe_handle
        or type(target_generation) is not int
        or target_generation <= 0
        or not isinstance(target_automation, str)
        or not target_automation
        or not isinstance(target_id, str)
        or not target_id
        or not isinstance(target_service, str)
        or not target_service
        or not isinstance(target_operation, str)
        or not target_operation
        or not isinstance(declaration, Mapping)
        or set(declaration) != _MODULE_SLOT_DECLARATION_FIELDS
        or declaration.get("id") != target_id
        or declaration.get("slot") != safe_slot
        or declaration.get("service") != target_service
        or declaration.get("operation") != target_operation
        or type(declaration.get("default_enabled")) is not bool
        or not isinstance(declaration.get("title"), str)
        or not declaration.get("title")
        or declared_digest != observed_digest
        or (automation_id is not None and target_automation != automation_id)
        or (generation is not None and target_generation != generation)
        or (contribution_id is not None and target_id != contribution_id)
    ):
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation module-slot projection identity does not match",
        )
    return target


def require_active_service_v2_module_slot(
    registry: Any,
    *,
    entry: Any,
    automation_id: str,
    generation: int,
    invocation_contract: Any,
    context: Mapping[str, Any],
) -> None:
    """Bind an active slot to the same signed invocation before acceptance."""

    route = context.get("module_slot")
    contribution_id = str(getattr(invocation_contract, "contribution_id", "") or "")
    signed = getattr(entry, "invocation_contracts", {}).get(contribution_id)
    if (
        getattr(entry, "runtime_model", "ACTION_V1") != "SERVICE_V2"
        or getattr(invocation_contract, "entrypoint", "") != "module_slots"
        or not contribution_id
        or not isinstance(route, Mapping)
        or not isinstance(signed, Mapping)
        or signed.get("contribution_kind") != "module_slots"
        or signed.get("dynamic_resolvers") != {WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD: WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID}
    ):
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation module-slot contract is unavailable",
        )
    target = resolve_active_service_v2_module_slot(
        registry,
        slot=str(route.get("slot") or ""),
        handle=str(route.get("handle") or ""),
        automation_id=automation_id,
        generation=generation,
        contribution_id=contribution_id,
    )
    if _record_value(target, "service") != signed.get("service") or _record_value(target, "operation") != signed.get(
        "operation"
    ):
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation module-slot target does not match its signed contract",
        )


def require_active_service_v2_dispatch(
    registry: Any,
    *,
    source: AutomationEntrypoint,
    entry: Any,
    automation_id: str,
    generation: int,
    invocation_contract: Any,
    context: Mapping[str, Any],
    expected_event_name: str | None = None,
) -> None:
    """Recheck either a regular contribution or an exact module slot."""

    if source is AutomationEntrypoint.MODULE_SLOTS:
        require_active_service_v2_module_slot(
            registry,
            entry=entry,
            automation_id=automation_id,
            generation=generation,
            invocation_contract=invocation_contract,
            context=context,
        )
        return
    require_active_service_v2_contribution(
        registry,
        automation_id=automation_id,
        generation=generation,
        contribution_kind=source.value,
        contribution_id=invocation_contract.contribution_id,
        expected_event_name=expected_event_name,
    )


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
