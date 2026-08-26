"""Pure, fail-closed readback checks for the authoritative daily-sign sinks.

The caller owns all I/O.  This module only compares a freshly read resource
with the exact rows that the existing daily-sign business rules rendered.
It deliberately knows neither account IDs nor managed-resource identities.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


class DailySignReadbackError(RuntimeError):
    """The terminal state of a write cannot be proven from a fresh read."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sheet_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def verify_sheet_snapshot(
    expected_rows: Sequence[Sequence[object]],
    observed_rows: Sequence[Sequence[object]],
    *,
    observed_row_capacity: int,
    columns: int,
) -> dict[str, object]:
    """Require every target cell and every cleared tail cell to match."""

    if observed_row_capacity < len(expected_rows) or columns <= 0:
        raise DailySignReadbackError("daily-sign Sheet readback range is incomplete")
    expected = [
        [_sheet_cell(cell) for cell in row]
        for row in expected_rows
    ]
    if any(len(row) != columns for row in expected):
        raise DailySignReadbackError("daily-sign Sheet expected row shape is invalid")
    observed: list[list[str]] = []
    for index in range(observed_row_capacity):
        raw = observed_rows[index] if index < len(observed_rows) else []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise DailySignReadbackError("daily-sign Sheet readback row is invalid")
        if len(raw) > columns:
            raise DailySignReadbackError("daily-sign Sheet readback has extra columns")
        observed.append(
            [_sheet_cell(raw[column]) if column < len(raw) else "" for column in range(columns)]
        )
    target = expected + [
        [""] * columns for _ in range(observed_row_capacity - len(expected))
    ]
    if observed != target:
        raise DailySignReadbackError("daily-sign Sheet readback does not match the target snapshot")
    return {
        "verified": True,
        "record_count": len(expected),
        "snapshot_sha256": _sha256(expected),
    }


def _record_items(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    direct = payload.get("items")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, Mapping)]
    data = payload.get("data")
    if isinstance(data, Mapping) and isinstance(data.get("items"), list):
        return [item for item in data["items"] if isinstance(item, Mapping)]
    raise DailySignReadbackError("daily-sign Bitable readback items are unavailable")


def _pagination_incomplete(payload: Mapping[str, object]) -> bool:
    candidates: list[Mapping[str, object]] = [payload]
    data = payload.get("data")
    if isinstance(data, Mapping):
        candidates.append(data)
    return any(
        candidate.get("has_more") is True
        or candidate.get("hasMore") is True
        or bool(candidate.get("page_token"))
        or bool(candidate.get("pageToken"))
        for candidate in candidates
    )


def verify_bitable_snapshot(
    expected_records: Sequence[Mapping[str, object]],
    payload: Mapping[str, object],
    *,
    identity_field: str,
) -> dict[str, object]:
    """Require the exact identity set and exact expected fields after a write."""

    if _pagination_incomplete(payload):
        raise DailySignReadbackError("daily-sign Bitable readback is incomplete")

    expected_by_identity: dict[str, dict[str, object]] = {}
    for raw in expected_records:
        if set(raw) != {"fields"} or not isinstance(raw.get("fields"), Mapping):
            raise DailySignReadbackError("daily-sign Bitable expected record is invalid")
        fields = dict(raw["fields"])
        identity = str(fields.get(identity_field) or "").strip()
        if not identity or identity in expected_by_identity:
            raise DailySignReadbackError("daily-sign Bitable expected identity is invalid")
        expected_by_identity[identity] = fields

    observed_by_identity: dict[str, dict[str, object]] = {}
    items = _record_items(payload)
    for item in items:
        fields = item.get("fields")
        if not isinstance(fields, Mapping):
            raise DailySignReadbackError("daily-sign Bitable readback record is incomplete")
        identity = str(fields.get(identity_field) or "").strip()
        if not identity or identity in observed_by_identity:
            raise DailySignReadbackError("daily-sign Bitable readback found zero or multiple identities")
        observed_by_identity[identity] = dict(fields)
    if set(observed_by_identity) != set(expected_by_identity):
        raise DailySignReadbackError("daily-sign Bitable readback identity set changed")
    for identity, expected_fields in expected_by_identity.items():
        observed_fields = observed_by_identity[identity]
        mismatched = False
        for field, expected_value in expected_fields.items():
            if expected_value in (None, ""):
                # Feishu omits empty cells from a record's ``fields`` object.
                # An omitted field, JSON null, and an empty string therefore
                # describe the same stored blank cell.
                if observed_fields.get(field) not in (None, ""):
                    mismatched = True
                    break
                continue
            if field not in observed_fields or observed_fields[field] != expected_value:
                mismatched = True
                break
        if mismatched:
            raise DailySignReadbackError("daily-sign Bitable readback field changed")
    canonical = [
        expected_by_identity[identity]
        for identity in sorted(expected_by_identity)
    ]
    return {
        "verified": True,
        "record_count": len(canonical),
        "snapshot_sha256": _sha256(canonical),
    }


def verify_bitable_schema(
    expected_types: Mapping[str, int],
    items: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Require one exact field definition for every daily-sign column."""

    by_name: dict[str, Mapping[str, object]] = {}
    for item in items:
        name = str(item.get("field_name") or "").strip()
        if not name:
            raise DailySignReadbackError("daily-sign Bitable field identity is missing")
        if name in by_name:
            raise DailySignReadbackError("daily-sign Bitable field identity is duplicated")
        by_name[name] = item
    for name, expected_type in expected_types.items():
        item = by_name.get(name)
        raw_type = item.get("type") if item is not None else None
        if isinstance(raw_type, bool):
            raise DailySignReadbackError("daily-sign Bitable field type is invalid")
        try:
            actual_type = int(raw_type)
        except (TypeError, ValueError) as exc:
            raise DailySignReadbackError("daily-sign Bitable field is missing") from exc
        if actual_type != expected_type:
            raise DailySignReadbackError("daily-sign Bitable field type changed")
    material = [
        {"field_name": name, "type": expected_types[name]}
        for name in sorted(expected_types)
    ]
    return {
        "verified": True,
        "field_count": len(material),
        "schema_sha256": _sha256(material),
    }


__all__ = [
    "DailySignReadbackError",
    "verify_bitable_schema",
    "verify_bitable_snapshot",
    "verify_sheet_snapshot",
]
