"""Pure compute example for a new automation capability."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone


ACTION_ID = "example_compute_automation"
_MAX_LABELS = 100
_MAX_LABEL_LENGTH = 128


def run_action(arguments: dict[str, object]) -> dict[str, object]:
    if not isinstance(arguments, Mapping) or set(arguments) != {"labels"}:
        raise ValueError("arguments must contain only labels")
    raw_labels = arguments.get("labels")
    if not isinstance(raw_labels, list) or len(raw_labels) > _MAX_LABELS:
        raise ValueError("labels must be a bounded array")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_label in raw_labels:
        if not isinstance(raw_label, str):
            raise ValueError("every label must be text")
        label = raw_label.strip()
        if not label or len(label) > _MAX_LABEL_LENGTH:
            raise ValueError("every label must be non-empty and bounded")
        if label not in seen:
            seen.add(label)
            normalized.append(label)

    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "status": "SUCCESS",
        "data": {
            "labels": normalized,
            "input_count": len(raw_labels),
            "unique_count": len(normalized),
        },
        "meta": {
            "source_system": "local_compute",
            "observed_at": observed_at,
            "record_count": len(normalized),
            "pagination_complete": True,
            "evidence_refs": [],
            "postconditions": {"0": True},
        },
        "warnings": [],
        "error": None,
    }
