"""Closed read-only bindings for audited production unknown-write incidents.

This is deliberately not a recovery mechanism.  It only recognizes the
captured production journal identities, so callers can quarantine only those
projects while every other unknown write remains a release blocker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID = "delivery_status"
DELIVERY_STATUS_QUARANTINE_PLUGIN_ID = "sync_delivery_status"
DELIVERY_STATUS_QUARANTINE_GENERATION = 1
DELIVERY_STATUS_QUARANTINE_LEASE_ID = "9918420e-b5c1-41c7-a4ee-543e131272be"
DELIVERY_STATUS_QUARANTINE_RECONCILE_STATE = "BLOCKED_UNKNOWN_WRITE"
DELIVERY_STATUS_QUARANTINE_GENERATION_STATE = "BLOCKED"
DELIVERY_STATUS_QUARANTINE_STATUS = "QUARANTINED_UNKNOWN_WRITE"


@dataclass(frozen=True)
class UnknownWriteQuarantineIdentity:
    automation_id: str
    plugin_id: str
    generation: int
    lease_id: str


REVIEWED_UNKNOWN_WRITE_QUARANTINES = {
    "arrive_list": UnknownWriteQuarantineIdentity(
        automation_id="arrive_list",
        plugin_id="sync_arrive_list",
        generation=1,
        lease_id="265143cd-bc4f-4e67-b843-869d67af27b8",
    ),
    "daily_sign": UnknownWriteQuarantineIdentity(
        automation_id="daily_sign",
        plugin_id="sync_daily_should_sign",
        generation=1,
        lease_id="294ba57d-adcb-4314-bc80-dc61db7b13bf",
    ),
    DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID: UnknownWriteQuarantineIdentity(
        automation_id=DELIVERY_STATUS_QUARANTINE_AUTOMATION_ID,
        plugin_id=DELIVERY_STATUS_QUARANTINE_PLUGIN_ID,
        generation=DELIVERY_STATUS_QUARANTINE_GENERATION,
        lease_id=DELIVERY_STATUS_QUARANTINE_LEASE_ID,
    ),
}
REVIEWED_UNKNOWN_WRITE_QUARANTINE_IDS = frozenset(
    REVIEWED_UNKNOWN_WRITE_QUARANTINES
)
UNKNOWN_WRITE_QUARANTINE_STATUS = DELIVERY_STATUS_QUARANTINE_STATUS


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
    generation_count: object,
    active_lease_count: object,
) -> bool:
    """Match only the reviewed project and complete runtime topology.

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
        and _exact_int(generation_count, 1)
        and _exact_int(active_lease_count, 0)
    )


def matches_reviewed_unknown_write_quarantine_project(
    *,
    automation_id: object,
    plugin_id: object,
    target_generation: object,
    committed_generation: object,
    reconcile_state: object,
    generation: object,
    generation_state: object,
    generation_count: object,
    active_lease_count: object,
) -> bool:
    """Match one reviewed incident's complete project topology."""

    identity = REVIEWED_UNKNOWN_WRITE_QUARANTINES.get(automation_id)
    if identity is None:
        return False
    return bool(
        plugin_id == identity.plugin_id
        and _exact_int(target_generation, identity.generation)
        and _exact_int(committed_generation, identity.generation)
        and _state_value(reconcile_state)
        == DELIVERY_STATUS_QUARANTINE_RECONCILE_STATE
        and _exact_int(generation, identity.generation)
        and _state_value(generation_state)
        == DELIVERY_STATUS_QUARANTINE_GENERATION_STATE
        and _exact_int(generation_count, 1)
        and _exact_int(active_lease_count, 0)
    )


def matches_reviewed_unknown_write_quarantine(
    *,
    automation_id: object,
    plugin_id: object,
    target_generation: object,
    committed_generation: object,
    reconcile_state: object,
    generation: object,
    generation_state: object,
    generation_count: object,
    active_lease_count: object,
    lease: Mapping[str, Any] | None,
) -> bool:
    """Match one reviewed incident including its sole unknown-write lease."""

    identity = REVIEWED_UNKNOWN_WRITE_QUARANTINES.get(automation_id)
    return bool(
        identity is not None
        and matches_reviewed_unknown_write_quarantine_project(
            automation_id=automation_id,
            plugin_id=plugin_id,
            target_generation=target_generation,
            committed_generation=committed_generation,
            reconcile_state=reconcile_state,
            generation=generation,
            generation_state=generation_state,
            generation_count=generation_count,
            active_lease_count=active_lease_count,
        )
        and isinstance(lease, Mapping)
        and set(lease) == {"generation", "lease_id"}
        and _exact_int(lease.get("generation"), identity.generation)
        and lease.get("lease_id") == identity.lease_id
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
    generation_count: object,
    active_lease_count: object,
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
            generation_count=generation_count,
            active_lease_count=active_lease_count,
        )
        and isinstance(lease, Mapping)
        and set(lease) == {"generation", "lease_id"}
        and _exact_int(
            lease.get("generation"),
            DELIVERY_STATUS_QUARANTINE_GENERATION,
        )
        and lease.get("lease_id") == DELIVERY_STATUS_QUARANTINE_LEASE_ID
    )
