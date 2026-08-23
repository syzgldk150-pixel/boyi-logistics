"""Production scan-snapshot write with an independent authoritative readback."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes


_FIELDS = ("raw_code", "destination", "code_type", "main_tracking")


def _unknown(message: str, cause: BaseException | None = None) -> NoReturn:
    error = PluginExecutionError(message, code="WRITE_OUTCOME_UNKNOWN")
    if cause is None:
        raise error
    raise error from cause


def _normalized(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in rows:
        if set(raw) != set(_FIELDS):
            _unknown("scan snapshot readback schema changed")
        row = {field: str(raw.get(field) or "").strip() for field in _FIELDS}
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

    del target_date
    from tools.phase7_mysql_store import list_scan_codes, replace_scan_codes_snapshot

    expected = sorted(_normalized(records), key=lambda row: row["raw_code"])
    write_error: BaseException | None = None
    try:
        replace_scan_codes_snapshot(records)
    except Exception as exc:
        write_error = exc
    try:
        observed = sorted(_normalized(list_scan_codes()), key=lambda row: row["raw_code"])
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
