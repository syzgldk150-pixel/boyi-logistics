"""Scan snapshot ownership heads and the write identity of the scan projection."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Mapping

from shared.orchestration_repository_support import _json_hash

# Every VALUES item is a placeholder so PyMySQL's executemany sends one
# multi-row INSERT; a literal here would make it round-trip once per row.
SNAPSHOT_INITIAL_SEEN_COUNT = 1
SNAPSHOT_UPSERT_SQL = """
    INSERT INTO scan_codes (
        raw_code, destination, code_type, main_tracking,
        snapshot_date, last_seen_at, seen_count, scanned_at
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        destination = VALUES(destination), code_type = VALUES(code_type),
        main_tracking = VALUES(main_tracking), last_seen_at = VALUES(last_seen_at),
        seen_count = seen_count + 1, scanned_at = VALUES(scanned_at)
"""
_WRITE_IDENTITY: ContextVar[Mapping[str, Any] | None] = ContextVar("scan_snapshot_write_identity", default=None)


@contextmanager
def scan_snapshot_write_identity(identity):
    token = _WRITE_IDENTITY.set(identity)
    try:
        yield
    finally:
        _WRITE_IDENTITY.reset(token)


def lock_snapshot_head(cursor, target_date):
    cursor.execute("INSERT INTO automation_scan_snapshot_heads (snapshot_date,snapshot_sha256,owner_run_id,owner_lease_id) VALUES (%s,%s,'','') ON DUPLICATE KEY UPDATE snapshot_date=snapshot_date", (target_date, _json_hash([])))
    cursor.execute("SELECT * FROM automation_scan_snapshot_heads WHERE snapshot_date=%s FOR UPDATE", (target_date,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("scan snapshot ownership row is unavailable")
    return dict(row) if isinstance(row, Mapping) else dict(zip([item[0] for item in cursor.description], row))


def record_snapshot_head(cursor, target_date, records):
    identity = _WRITE_IDENTITY.get() or {}
    cursor.execute("UPDATE automation_scan_snapshot_heads SET snapshot_sha256=%s,owner_run_id=%s,owner_invocation_id=%s,owner_lease_id=%s,revision=revision+1,updated_at=NOW(6) WHERE snapshot_date=%s",
        (_json_hash(sorted(records, key=lambda row: row["raw_code"])), str(identity.get("orchestration_run_id") or ""), identity.get("invocation_id"), str(identity.get("lease_id") or ""), target_date))

