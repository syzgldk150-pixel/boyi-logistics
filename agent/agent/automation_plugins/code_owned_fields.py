"""Closed first-party fields controlled by Agent code, never by Console input."""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence


_FIRST_PARTY_TRUST_SOURCE = "ed25519_first_party"
_SCAN_AUTOMATION_ID = "scan_codes"
_SCAN_PLUGIN_ID = "sync_scan_codes"
_SCAN_PREVIEW_BINDING_FIELD = "_scan_preview_binding"
_SCAN_PREVIEW_BROKER_OPERATION = "browser.invoke"
_SCAN_PREVIEW_BROKER_ACTION = "ronghui.scan.read_page"

SCAN_PHASE_PREVIEW = "PREVIEW"
SCAN_PHASE_FORMAL = "FORMAL"
SCAN_PREVIEW_POSTCONDITION = "authoritative_scan_preview_returned"
SCAN_FORMAL_POSTCONDITION = "scan_formal_execution_verified"

SELECTION_PHASE_PREVIEW = "PREVIEW"
SELECTION_PHASE_FORMAL = "FORMAL"
_SELECTION_PROJECTS = frozenset(
    {
        ("self_pickup_problem_upload", "self_pickup_problem_upload"),
        ("split_pending_problem_upload", "split_pending_problem_upload"),
    }
)
_SELECTION_PREVIEW_BROKER_ACTIONS: Mapping[
    tuple[str, str], frozenset[tuple[str, str]]
] = {
    (
        "self_pickup_problem_upload",
        "self_pickup_problem_upload",
    ): frozenset(
        {
            ("network.request", "feishu.sheet.read_rows"),
        }
    ),
    (
        "split_pending_problem_upload",
        "split_pending_problem_upload",
    ): frozenset(
        {
            ("network.request", "feishu.sheet.read_rows"),
            ("projection.invoke", "split_pending.snapshot.read"),
        }
    ),
}
_SELECTION_FIELDS = (
    "dry_run",
    "preview_fingerprint",
    "selected_bill_codes",
)

_CODE_OWNED_CONFIG_FIELDS: Mapping[tuple[str, str], tuple[str, ...]] = {
    (
        "scan_codes",
        "sync_scan_codes",
    ): (
        "_scan_preview_binding",
        "dry_run",
    ),
    (
        "customer_problems_shadow",
        "sync_customer_service_problems",
    ): ("recheck_items",),
    (
        "finance_bills",
        "sync_finance_bills",
    ): ("_startup_catchup",),
    (
        "finance_startup_catchup",
        "sync_finance_bills",
    ): ("_startup_catchup",),
    (
        "self_pickup_problem_upload",
        "self_pickup_problem_upload",
    ): (
        "dry_run",
        "preview_fingerprint",
        "selected_bill_codes",
    ),
    (
        "split_pending_problem_upload",
        "split_pending_problem_upload",
    ): (
        "dry_run",
        "preview_fingerprint",
        "selected_bill_codes",
    ),
}

_CODE_OWNED_PLAN_FIELDS: Mapping[tuple[str, str], tuple[str, ...]] = {
    (
        "scan_codes",
        "sync_scan_codes",
    ): (
        "_scan_preview_binding",
        "dry_run",
    ),
    (
        "customer_problems_shadow",
        "sync_customer_service_problems",
    ): ("recheck_items",),
}

# These signed releases own the exact entrypoint transitions for the two
# selection-based problem-item actions.  Version 1.0.21 removed Console until
# it could carry the same preview, explicit selection and fingerprint contract
# as Feishu.  Version 1.0.22 is the compatibility bridge: its signed manifest
# declares Console, while the persisted 1.0.21 projects remain Feishu-only so
# the old manifest can validate the pre-upgrade configuration.  Version 1.0.23
# enables Console after the installed manifest already permits that entrypoint.
_CODE_OWNED_ENTRYPOINT_TRANSITIONS: Mapping[
    tuple[str, str, str, str],
    tuple[frozenset[str], tuple[str, ...]],
] = {
    (
        "self_pickup_problem_upload",
        "self_pickup_problem_upload",
        "1.0.20",
        "1.0.21",
    ): (frozenset({"console", "feishu"}), ("feishu",)),
    (
        "split_pending_problem_upload",
        "split_pending_problem_upload",
        "1.0.20",
        "1.0.21",
    ): (frozenset({"console", "feishu", "scheduler"}), ("feishu",)),
    (
        "self_pickup_problem_upload",
        "self_pickup_problem_upload",
        "1.0.21",
        "1.0.22",
    ): (frozenset({"feishu"}), ("feishu",)),
    (
        "split_pending_problem_upload",
        "split_pending_problem_upload",
        "1.0.21",
        "1.0.22",
    ): (frozenset({"feishu"}), ("feishu",)),
    (
        "self_pickup_problem_upload",
        "self_pickup_problem_upload",
        "1.0.22",
        "1.0.23",
    ): (frozenset({"feishu"}), ("console", "feishu")),
    (
        "split_pending_problem_upload",
        "split_pending_problem_upload",
        "1.0.22",
        "1.0.23",
    ): (frozenset({"feishu"}), ("console", "feishu")),
}

_SELF_PICKUP_SOURCE_ROLE = "self_pickup_source_sheet"
_LEGACY_SELF_PICKUP_SOURCE_RESOURCE = "phase7.arrive_secondary_sheet"
_REVIEWED_SELF_PICKUP_SOURCE_RESOURCE = "phase7.self_pickup_source_sheet"
_CODE_OWNED_RESOURCE_BINDING_REPAIRS = frozenset(
    {
        (
            "self_pickup_problem_upload",
            "self_pickup_problem_upload",
            "1.0.26",
            "1.0.26",
        ),
    }
)


def _declared_fields(
    declarations: Mapping[tuple[str, str], tuple[str, ...]],
    *,
    automation_id: str,
    plugin_id: str,
    trust_source: str,
) -> tuple[str, ...]:
    if trust_source != _FIRST_PARTY_TRUST_SOURCE:
        return ()
    return declarations.get((automation_id, plugin_id), ())


def first_party_code_owned_config_fields(
    *,
    automation_id: str,
    plugin_id: str,
    trust_source: str,
) -> tuple[str, ...]:
    """Return fields hidden from and rejected in administrator config input."""

    return _declared_fields(
        _CODE_OWNED_CONFIG_FIELDS,
        automation_id=str(automation_id or "").strip(),
        plugin_id=str(plugin_id or "").strip(),
        trust_source=str(trust_source or "").strip(),
    )


def first_party_code_owned_plan_fields(
    *,
    automation_id: str,
    plugin_id: str,
    trust_source: str,
) -> tuple[str, ...]:
    """Return service-produced plan fields admitted beyond saved config."""

    return _declared_fields(
        _CODE_OWNED_PLAN_FIELDS,
        automation_id=str(automation_id or "").strip(),
        plugin_id=str(plugin_id or "").strip(),
        trust_source=str(trust_source or "").strip(),
    )


def normalize_first_party_code_owned_config(
    *,
    automation_id: str,
    plugin_id: str,
    trust_source: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Return detached persisted config with exact code-owned values applied."""

    normalized = copy.deepcopy(dict(config))
    fields = first_party_code_owned_config_fields(
        automation_id=automation_id,
        plugin_id=plugin_id,
        trust_source=trust_source,
    )
    for field_name in fields:
        normalized.pop(field_name, None)
    if (
        str(trust_source or "").strip() == _FIRST_PARTY_TRUST_SOURCE
        and str(automation_id or "").strip() == "finance_startup_catchup"
        and str(plugin_id or "").strip() == "sync_finance_bills"
    ):
        normalized["_startup_catchup"] = True
    return normalized


def normalize_first_party_code_owned_entrypoints(
    *,
    automation_id: str,
    plugin_id: str,
    current_version: str,
    target_version: str,
    enabled_entrypoints: Sequence[str],
) -> tuple[str, ...] | None:
    """Return one exact release-owned entrypoint transition, if declared.

    A declared transition is accepted only from its complete historical source
    set.  ``None`` means the generic upgrade gate remains authoritative.
    """

    transition = _CODE_OWNED_ENTRYPOINT_TRANSITIONS.get(
        (
            str(automation_id or "").strip(),
            str(plugin_id or "").strip(),
            str(current_version or "").strip(),
            str(target_version or "").strip(),
        )
    )
    if transition is None:
        return None
    expected_sources, target_sources = transition
    normalized_sources = tuple(str(item or "").strip() for item in enabled_entrypoints)
    if (
        any(not source for source in normalized_sources)
        or len(normalized_sources) != len(set(normalized_sources))
        or set(normalized_sources) != set(expected_sources)
    ):
        return None
    return target_sources


def normalize_first_party_code_owned_resource_bindings(
    *,
    automation_id: str,
    plugin_id: str,
    trust_source: str,
    current_version: str,
    target_version: str,
    resource_bindings: Mapping[str, str],
) -> dict[str, str]:
    """Apply one exact, reviewed first-party resource migration.

    The historical self-pickup project was installed with the general daily
    arrival sheet.  Release bootstrap moves only that exact legacy binding to
    the dedicated self-pickup resource.  Already-correct and administrator-
    chosen resource identities are preserved.
    """

    normalized = copy.deepcopy(dict(resource_bindings))
    identity = (
        str(automation_id or "").strip(),
        str(plugin_id or "").strip(),
        str(current_version or "").strip(),
        str(target_version or "").strip(),
    )
    if (
        first_party_code_owned_resource_binding_repair_applies(
            automation_id=identity[0],
            plugin_id=identity[1],
            trust_source=trust_source,
            current_version=identity[2],
            target_version=identity[3],
        )
        and normalized.get(_SELF_PICKUP_SOURCE_ROLE)
        == _LEGACY_SELF_PICKUP_SOURCE_RESOURCE
    ):
        normalized[_SELF_PICKUP_SOURCE_ROLE] = (
            _REVIEWED_SELF_PICKUP_SOURCE_RESOURCE
        )
    return normalized


def first_party_code_owned_resource_binding_repair_applies(
    *,
    automation_id: str,
    plugin_id: str,
    trust_source: str,
    current_version: str,
    target_version: str,
) -> bool:
    """Return whether release bootstrap owns this exact resource repair."""

    identity = (
        str(automation_id or "").strip(),
        str(plugin_id or "").strip(),
        str(current_version or "").strip(),
        str(target_version or "").strip(),
    )
    return bool(
        str(trust_source or "").strip() == _FIRST_PARTY_TRUST_SOURCE
        and identity in _CODE_OWNED_RESOURCE_BINDING_REPAIRS
    )


def resolve_scan_execution_phase(
    *,
    automation_id: str,
    plugin_id: str,
    trust_source: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Resolve the exact first-party scan phase from server-owned arguments.

    ``None`` means that the identity is not the governed scan project.  Exact
    matches are deliberately strict: preview and formal invocations must be
    unambiguous, and only the formal phase may carry a preview binding.
    """

    if (
        str(automation_id or "").strip() != _SCAN_AUTOMATION_ID
        or str(plugin_id or "").strip() != _SCAN_PLUGIN_ID
        or str(trust_source or "").strip() != _FIRST_PARTY_TRUST_SOURCE
    ):
        return None
    dry_run = arguments.get("dry_run")
    binding_present = _SCAN_PREVIEW_BINDING_FIELD in arguments
    binding = arguments.get(_SCAN_PREVIEW_BINDING_FIELD)
    if dry_run is True and not binding_present:
        return SCAN_PHASE_PREVIEW
    if (
        dry_run is False
        and binding_present
        and isinstance(binding, Mapping)
        and bool(binding)
    ):
        return SCAN_PHASE_FORMAL
    raise ValueError("scan execution phase is ambiguous or incomplete")


def resolve_scan_capability_phase(
    capability: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> str | None:
    runtime = capability.get("_plugin_runtime")
    if not isinstance(runtime, Mapping):
        return None
    return resolve_scan_execution_phase(
        automation_id=str(runtime.get("automation_id") or ""),
        plugin_id=str(runtime.get("plugin_id") or ""),
        trust_source=str(runtime.get("trust_source") or ""),
        arguments=arguments,
    )


def resolve_selection_execution_phase(
    *,
    automation_id: str,
    plugin_id: str,
    trust_source: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Resolve one exact server-owned selection preview or formal execution."""

    identity = (
        str(automation_id or "").strip(),
        str(plugin_id or "").strip(),
    )
    if (
        identity not in _SELECTION_PROJECTS
        or str(trust_source or "").strip() != _FIRST_PARTY_TRUST_SOURCE
    ):
        return None
    if any(field_name not in arguments for field_name in _SELECTION_FIELDS):
        raise ValueError("selection execution phase is ambiguous or incomplete")

    dry_run = arguments.get("dry_run")
    selected = arguments.get("selected_bill_codes")
    fingerprint = arguments.get("preview_fingerprint")
    if dry_run is True and selected == [] and fingerprint == "":
        return SELECTION_PHASE_PREVIEW
    if (
        dry_run is False
        and isinstance(selected, list)
        and bool(selected)
        and isinstance(fingerprint, str)
        and bool(fingerprint.strip())
    ):
        return SELECTION_PHASE_FORMAL
    raise ValueError("selection execution phase is ambiguous or incomplete")


def resolve_selection_capability_phase(
    capability: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> str | None:
    runtime = capability.get("_plugin_runtime")
    if not isinstance(runtime, Mapping):
        return None
    return resolve_selection_execution_phase(
        automation_id=str(runtime.get("automation_id") or ""),
        plugin_id=str(runtime.get("plugin_id") or ""),
        trust_source=str(runtime.get("trust_source") or ""),
        arguments=arguments,
    )


def apply_scan_execution_boundary(
    capability: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a detached capability with the PREVIEW boundary applied.

    Formal and unrelated capabilities remain byte-for-byte equivalent after a
    deep copy.  Preview keeps only the signed Ronghui scan-page read grant;
    every projection, submit, verification, file, network, and office effect
    is removed before the Broker token is issued.
    """

    resolved = copy.deepcopy(dict(capability))
    phase = resolve_scan_capability_phase(resolved, arguments)
    if phase != SCAN_PHASE_PREVIEW:
        return resolved
    metadata = resolved.get("_plugin_runtime")
    permissions = metadata.get("runtime_permissions") if isinstance(metadata, Mapping) else None
    operations = permissions.get("broker_operations") if isinstance(permissions, Mapping) else None
    if not isinstance(operations, list):
        raise ValueError("scan preview runtime permissions are missing")
    allowed = [
        copy.deepcopy(dict(item))
        for item in operations
        if isinstance(item, Mapping)
        and item.get("operation") == _SCAN_PREVIEW_BROKER_OPERATION
        and item.get("action") == _SCAN_PREVIEW_BROKER_ACTION
        and item.get("effect") == "read"
    ]
    if len(allowed) != 1:
        raise ValueError("scan preview read permission is missing or ambiguous")
    max_calls = permissions.get("max_broker_calls")
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls <= 0:
        raise ValueError("scan preview broker call limit is invalid")
    restricted_permissions = {
        "network": False,
        "browser": True,
        "office": False,
        "file_roles": [],
        "broker_operations": allowed,
        "max_broker_calls": max_calls,
    }
    restricted_metadata = dict(metadata)
    restricted_metadata["runtime_permissions"] = restricted_permissions
    resolved["_plugin_runtime"] = restricted_metadata
    resolved["operation_type"] = "read"
    resolved["risk_level"] = "low"
    resolved["postconditions"] = [{"name": SCAN_PREVIEW_POSTCONDITION}]
    return resolved


def apply_selection_execution_boundary(
    capability: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Restrict first-party selection previews to their exact read primitives."""

    resolved = copy.deepcopy(dict(capability))
    phase = resolve_selection_capability_phase(resolved, arguments)
    if phase != SELECTION_PHASE_PREVIEW:
        return resolved

    metadata = resolved.get("_plugin_runtime")
    identity = (
        str(metadata.get("automation_id") or "") if isinstance(metadata, Mapping) else "",
        str(metadata.get("plugin_id") or "") if isinstance(metadata, Mapping) else "",
    )
    expected_actions = _SELECTION_PREVIEW_BROKER_ACTIONS.get(identity)
    permissions = metadata.get("runtime_permissions") if isinstance(metadata, Mapping) else None
    operations = permissions.get("broker_operations") if isinstance(permissions, Mapping) else None
    if not expected_actions or not isinstance(operations, list):
        raise ValueError("selection preview runtime permissions are missing")

    allowed = [
        copy.deepcopy(dict(item))
        for item in operations
        if isinstance(item, Mapping)
        and (str(item.get("operation") or ""), str(item.get("action") or ""))
        in expected_actions
        and item.get("effect") == "read"
    ]
    actual_actions = {
        (str(item.get("operation") or ""), str(item.get("action") or ""))
        for item in allowed
    }
    if len(allowed) != len(expected_actions) or actual_actions != set(expected_actions):
        raise ValueError("selection preview read permissions are missing or ambiguous")

    restricted_permissions = {
        "network": any(item["operation"] == "network.request" for item in allowed),
        "browser": any(item["operation"] == "browser.invoke" for item in allowed),
        "office": False,
        "file_roles": [],
        "broker_operations": allowed,
        "max_broker_calls": len(allowed),
    }
    restricted_metadata = dict(metadata)
    restricted_metadata["runtime_permissions"] = restricted_permissions
    resolved["_plugin_runtime"] = restricted_metadata
    resolved["operation_type"] = "read"
    resolved["risk_level"] = "low"
    return resolved
