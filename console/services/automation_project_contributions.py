"""Closed Console projection for active Service v2 contributions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
AUTOMATION_PLUGIN_CONTRIBUTION_PROJECTION_STATES = frozenset(
    {"ACTIVE", "STALE", "INACTIVE"}
)
AUTOMATION_PLUGIN_ACTIVE_CONTRIBUTION_FIELDS = frozenset(
    {
        "contribution_id",
        "contribution_kind",
        "generation",
        "phase",
        "backend_status",
    }
)


def normalize_plugin_active_contributions(
    value: Any,
    *,
    projection_state: Any,
    committed_generation: Any,
    entrypoints: list[str],
    entrypoint_kinds: Mapping[str, str],
    enabled_entrypoints: list[str],
) -> tuple[list[dict[str, Any]], str]:
    """Return only exact, committed runtime routes from the host projection.

    The declaration remains available through ``entrypoints`` for settings UI,
    but a Service v2 contribution is executable only when the Agent reports an
    ACTIVE, closed runtime projection for the exact committed generation.
    Malformed rows are ignored individually so an unrelated managed record
    cannot withdraw a valid Console route, while the malformed contribution
    itself remains unavailable.
    """

    normalized_state = (
        projection_state.strip().upper()
        if isinstance(projection_state, str)
        else ""
    )
    if normalized_state not in AUTOMATION_PLUGIN_CONTRIBUTION_PROJECTION_STATES:
        return [], "UNKNOWN"
    if (
        not isinstance(value, list)
        or len(value) > 100
        or isinstance(committed_generation, bool)
        or not isinstance(committed_generation, int)
        or committed_generation < 1
    ):
        return [], normalized_state

    normalized: list[dict[str, Any]] = []
    contribution_counts: dict[str, int] = {}
    for raw in value:
        if (
            not isinstance(raw, Mapping)
            or set(raw) != AUTOMATION_PLUGIN_ACTIVE_CONTRIBUTION_FIELDS
        ):
            continue
        contribution_id = str(raw.get("contribution_id") or "").strip().lower()
        contribution_kind = str(raw.get("contribution_kind") or "").strip().lower()
        generation = raw.get("generation")
        phase = str(raw.get("phase") or "").strip().upper()
        backend_status = str(raw.get("backend_status") or "").strip().upper()
        if (
            not AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE.fullmatch(contribution_id)
            or contribution_kind
            not in {"console", "scheduler", "webhook", "feishu", "events"}
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or not phase
            or len(phase) > 40
            or not backend_status
            or len(backend_status) > 40
        ):
            continue
        contribution_counts[contribution_id] = (
            contribution_counts.get(contribution_id, 0) + 1
        )
        normalized.append(
            {
                "contribution_id": contribution_id,
                "contribution_kind": contribution_kind,
                "generation": generation,
                "phase": phase,
                "backend_status": backend_status,
            }
        )

    if normalized_state != "ACTIVE":
        return [], normalized_state
    return [
        contribution
        for contribution in normalized
        if contribution_counts[contribution["contribution_id"]] == 1
        and contribution["contribution_id"] in entrypoints
        and contribution["contribution_id"] in enabled_entrypoints
        and entrypoint_kinds.get(contribution["contribution_id"])
        == contribution["contribution_kind"]
        and contribution["generation"] == committed_generation
        and contribution["phase"] == "COMMITTED"
        and contribution["backend_status"] == "READY"
    ], normalized_state
