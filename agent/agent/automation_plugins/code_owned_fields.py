"""Closed first-party fields controlled by Agent code, never by Console input."""

from __future__ import annotations

import copy
from typing import Any, Mapping


_FIRST_PARTY_TRUST_SOURCE = "ed25519_first_party"

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
