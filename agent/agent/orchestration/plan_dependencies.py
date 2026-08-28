"""Schema-specific dependency slices used by plan creation and validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent.orchestration.models import (
    ContextSnapshot,
    OrchestrationError,
    PlanStep,
    sha256_json,
)
from agent.orchestration.ports import ToolCatalogPort


_CAPABILITY_DEPENDENCY_FIELDS = (
    "name",
    "version",
    "operation_type",
    "risk_level",
    "approval",
    "permissions",
    "account_scope",
    "evidence",
    "postconditions",
    "idempotency",
    "retry",
)
_ACCOUNT_BINDING_FIELDS = (
    "account_id",
    "system",
    "account_purpose",
    "session_profile",
    "is_active",
)


def plan_tool_catalog_hash(
    *,
    schema_version: int,
    steps: Sequence[PlanStep],
    catalog: ToolCatalogPort,
) -> str:
    """Return the catalog dependency hash for the requested plan schema."""

    if type(schema_version) is not int:
        raise OrchestrationError(
            "UNSUPPORTED_PLAN_SCHEMA",
            f"Unsupported plan schema: {schema_version}",
        )
    if schema_version == 1:
        return catalog.catalog_hash
    if schema_version != 2:
        raise OrchestrationError(
            "UNSUPPORTED_PLAN_SCHEMA",
            f"Unsupported plan schema: {schema_version}",
        )

    sliced_capabilities: list[dict[str, Any]] = []
    for tool_name in sorted({step.tool_name for step in steps}):
        capability = catalog.get_capability(tool_name)
        if capability is None:
            raise OrchestrationError("UNKNOWN_TOOL", f"Unknown tool: {tool_name}")
        sliced = {
            field_name: capability[field_name]
            for field_name in _CAPABILITY_DEPENDENCY_FIELDS
            if field_name in capability
        }
        sliced.setdefault("name", tool_name)
        sliced_capabilities.append(sliced)
    return sha256_json({"capabilities": sliced_capabilities})


def plan_context_fingerprint(
    *,
    schema_version: int,
    steps: Sequence[PlanStep],
    context: ContextSnapshot,
    catalog: ToolCatalogPort,
    automation_project: bool,
) -> str:
    """Return only the authoritative context dependencies used by a plan.

    Automation project account/resource bindings are already covered by the
    immutable project contract hash stored on ``Plan``. Duplicating the whole
    account catalog here would make unrelated account changes invalidate an
    already approved project invocation.
    """

    if type(schema_version) is not int:
        raise OrchestrationError(
            "UNSUPPORTED_PLAN_SCHEMA",
            f"Unsupported plan schema: {schema_version}",
        )
    if schema_version == 1:
        return context.fingerprint
    if schema_version != 2:
        raise OrchestrationError(
            "UNSUPPORTED_PLAN_SCHEMA",
            f"Unsupported plan schema: {schema_version}",
        )

    selected_ids = (
        set()
        if automation_project
        else _selected_account_ids(steps=steps, context=context, catalog=catalog)
    )
    descriptors = _public_account_descriptors(context)
    selected_bindings = [
        descriptors.get(account_id, {"account_id": account_id})
        for account_id in sorted(selected_ids)
    ]
    return sha256_json({"account_bindings": selected_bindings})


def _selected_account_ids(
    *,
    steps: Sequence[PlanStep],
    context: ContextSnapshot,
    catalog: ToolCatalogPort,
) -> set[str]:
    selected: set[str] = set()
    configured = tuple(str(value) for value in context.account_ids)
    for step in steps:
        explicit = _explicit_account_ids(step)
        if explicit:
            selected.update(explicit)
            continue

        capability = catalog.get_capability(step.tool_name)
        if capability is None:
            continue
        mode = _account_scope_mode(capability.get("account_scope"))
        if mode == "single" and len(configured) == 1:
            selected.add(configured[0])
        elif mode in {"all_configured", "single_or_all_configured"}:
            selected.update(configured)
    return selected


def _explicit_account_ids(step: PlanStep) -> set[str]:
    values: list[Any] = [step.account_id, step.arguments.get("account_id")]
    account_ids = step.arguments.get("account_ids")
    if isinstance(account_ids, (list, tuple)):
        values.extend(account_ids)
    return {
        str(value).strip()
        for value in values
        if value not in (None, "") and str(value).strip()
    }


def _account_scope_mode(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return "none"
    if "mode" in value:
        return str(value.get("mode") or "none")
    return "single" if bool(value.get("required")) else "optional"


def _public_account_descriptors(context: ContextSnapshot) -> dict[str, dict[str, Any]]:
    raw_accounts = context.values.get("accounts")
    if raw_accounts in (None, ()):
        return {}
    if not isinstance(raw_accounts, (list, tuple)):
        raise OrchestrationError(
            "INVALID_ACCOUNT_CONTEXT",
            "Account context must be an object array",
        )

    descriptors: dict[str, dict[str, Any]] = {}
    for raw in raw_accounts:
        if not isinstance(raw, Mapping):
            raise OrchestrationError(
                "INVALID_ACCOUNT_CONTEXT",
                "Account context contains a non-object value",
            )
        account_id = str(raw.get("account_id") or "").strip()
        if not account_id:
            raise OrchestrationError(
                "INVALID_ACCOUNT_CONTEXT",
                "Account context contains an empty account_id",
            )
        if account_id in descriptors:
            raise OrchestrationError(
                "DUPLICATE_ACCOUNT_CONTEXT",
                f"Account was resolved more than once: {account_id}",
            )
        descriptor = {
            field_name: raw[field_name]
            for field_name in _ACCOUNT_BINDING_FIELDS
            if field_name in raw
        }
        descriptor["account_id"] = account_id
        descriptors[account_id] = descriptor
    return descriptors
