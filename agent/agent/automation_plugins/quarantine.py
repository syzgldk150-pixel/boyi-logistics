"""Closed read-only binding for the one audited delivery write incident.

This is deliberately not a recovery mechanism.  It only recognizes the
captured production journal identity, so callers can quarantine that one
project while every other unknown write remains a release blocker.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID = "delivery_status"
DELIVERY_STATUS_QUARANTINE_PLUGIN_ID = "sync_delivery_status"
DELIVERY_STATUS_QUARANTINE_GENERATION = 1
DELIVERY_STATUS_QUARANTINE_LEASE_ID = "9918420e-b5c1-41c7-a4ee-543e131272be"
DELIVERY_STATUS_QUARANTINE_RECONCILE_STATE = "BLOCKED_UNKNOWN_WRITE"
DELIVERY_STATUS_QUARANTINE_GENERATION_STATE = "BLOCKED"
DELIVERY_STATUS_QUARANTINE_STATUS = "QUARANTINED_UNKNOWN_WRITE"


def _exact_int(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _state_value(value: object) -> object:
    """Accept the persisted string enum without accepting an arbitrary object."""

    candidate = getattr(value, "value", value)
    return candidate if type(candidate) is str else None


def matches_delivery_status_quarantine_project(
    *,
    automation_id: object,
    plugin_id: object,
    target_generation: object,
    committed_generation: object,
    reconcile_state: object,
    generation: object,
    generation_state: object,
) -> bool:
    """Match only the reviewed project and generation states.

    The caller must separately obtain the lease through the typed
    ``find_current_unknown_generation_write`` repository operation before it
    may treat this as a quarantine.  Keeping this helper free of Agent imports
    also lets the read-only manifest preflight use the exact same identity
    contract.
    """

    return bool(
        automation_id == DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID
        and plugin_id == DELIVERY_STATUS_QUARANTINE_PLUGIN_ID
        and _exact_int(target_generation, DELIVERY_STATUS_QUARANTINE_GENERATION)
        and _exact_int(committed_generation, DELIVERY_STATUS_QUARANTINE_GENERATION)
        and _state_value(reconcile_state)
        == DELIVERY_STATUS_QUARANTINE_RECONCILE_STATE
        and _exact_int(generation, DELIVERY_STATUS_QUARANTINE_GENERATION)
        and _state_value(generation_state)
        == DELIVERY_STATUS_QUARANTINE_GENERATION_STATE
    )


def matches_delivery_status_unknown_write_quarantine(
    *,
    automation_id: object,
    plugin_id: object,
    target_generation: object,
    committed_generation: object,
    reconcile_state: object,
    generation: object,
    generation_state: object,
    lease: Mapping[str, Any] | None,
) -> bool:
    """Match the complete incident binding, including its sole unknown lease."""

    return bool(
        matches_delivery_status_quarantine_project(
            automation_id=automation_id,
            plugin_id=plugin_id,
            target_generation=target_generation,
            committed_generation=committed_generation,
            reconcile_state=reconcile_state,
            generation=generation,
            generation_state=generation_state,
        )
        and isinstance(lease, Mapping)
        and set(lease) == {"generation", "lease_id"}
        and _exact_int(
            lease.get("generation"),
            DELIVERY_STATUS_QUARANTINE_GENERATION,
        )
        and lease.get("lease_id") == DELIVERY_STATUS_QUARANTINE_LEASE_ID
    )
