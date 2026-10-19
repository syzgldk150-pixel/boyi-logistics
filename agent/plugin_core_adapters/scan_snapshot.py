"""Production scan-snapshot write with an independent authoritative readback."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes


_FIELDS = ("raw_code", "destination", "code_type", "main_tracking")
# Producers that capture the real scan time send it on every row.
_TIMED_FIELDS = (*_FIELDS, "scanned_at")


def _unknown(message: str, cause: BaseException | None = None) -> NoReturn:
    error = PluginExecutionError(message, code="WRITE_OUTCOME_UNKNOWN")
    if cause is None:
        raise error
    raise error from cause


def _row_fields(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return _TIMED_FIELDS if rows and "scanned_at" in rows[0] else _FIELDS


def _normalized(
    rows: Sequence[Mapping[str, Any]],
    fields: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    fields = fields or _row_fields(rows)
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in rows:
        if set(raw) != set(fields):
            _unknown("scan snapshot readback schema changed")
        row = {field: str(raw.get(field) or "").strip() for field in fields}
        identity = row["raw_code"]
        if not identity or identity in seen:
            _unknown("scan snapshot readback found zero or multiple exact identities")
        seen.add(identity)
        output.append(row)
    return output


def scan_snapshot_identities_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    material = sorted(_normalized(rows), key=lambda item: item["raw_code"])
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def replace_scan_snapshot_verified(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    """Replace then freshly prove the complete intended scan snapshot."""

    from tools.phase7_mysql_store import (
        list_scan_codes_for_date,
        replace_scan_codes_snapshot,
    )

    fields = _row_fields(records)
    expected = sorted(_normalized(records, fields), key=lambda row: row["raw_code"])
    write_error: BaseException | None = None
    try:
        replace_scan_codes_snapshot(records, target_date)
    except Exception as exc:
        write_error = exc
    try:
        readback = (
            list_scan_codes_for_date(target_date, include_scanned_at=True)
            if fields == _TIMED_FIELDS
            else list_scan_codes_for_date(target_date)
        )
        observed = sorted(
            _normalized(readback, fields),
            key=lambda row: row["raw_code"],
        )
    except PluginExecutionError:
        raise
    except Exception as exc:
        _unknown("scan snapshot fresh readback is unavailable", exc)
    if observed != expected:
        _unknown(
            "scan snapshot fresh readback does not match the complete intended snapshot",
            write_error,
        )
    return {
        "ok": True,
        "verified": True,
        "record_count": len(expected),
        "readback_count": len(observed),
        "identities_sha256": scan_snapshot_identities_sha256(expected),
    }


__all__ = ["replace_scan_snapshot_verified", "scan_snapshot_identities_sha256"]
