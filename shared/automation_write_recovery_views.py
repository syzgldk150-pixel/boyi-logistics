"""Minimal, configuration-free identities for one work item's unknown writes."""

from collections.abc import Mapping
from datetime import datetime
import json
import re

from shared.execution_resource_journal import closed_execution_keys


_SCOPE_KEY_KINDS = frozenset({
    "account-write", "account-resource", "physical-write", "resource-write", "browser-account",
    "browser-write", "projection-write",
})
_MAX_WRITE_ATTEMPTS = 1000


def _scope_summary(raw) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if value is None or value == []:
            return {"original_scope_key_kinds": [], "scope_missing": True, "scope_malformed": False}
        keys = closed_execution_keys(value)
        unknown = any(key[0] not in _SCOPE_KEY_KINDS for key in keys)
        kinds = sorted({key[0] if key[0] in _SCOPE_KEY_KINDS else "unknown" for key in keys})
        return {"original_scope_key_kinds": kinds, "scope_missing": False, "scope_malformed": unknown}
    except (ValueError, TypeError):
        return {"original_scope_key_kinds": [], "scope_missing": False, "scope_malformed": True}


def _write_attempt(row: Mapping) -> dict:
    count = row.get("record_count")
    if isinstance(count, (str, bytes)):
        try:
            count = json.loads(count)
        except (ValueError, TypeError):
            count = None
    result = {}
    for name, maximum in (("receipt_id", 36), ("operation", 64), ("action", 128), ("outcome", 32)):
        value = row[name]
        if not isinstance(value, str) or not 1 <= len(value) <= maximum or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise ValueError("WRITE_ATTEMPT_PUBLIC_METADATA_INVALID")
        result[name] = value
    result["record_count"] = count if type(count) is int and count >= 0 else None
    for name in ("created_at", "updated_at"):
        value = row.get(name)
        # Receipt times retain their stored database timebase. In particular,
        # updated_at is not a verified-at timestamp and must not be renamed.
        result[name] = value.isoformat() if isinstance(value, datetime) else None
    result.update(_scope_summary(row.get("execution_resource_keys_json")))
    result["scope_quarantined"] = row.get("legacy_scope_quarantined_at") is not None
    return result


def list_work_item_unknown_writes(repository, work_item_id: str) -> list[dict]:
    if not isinstance(work_item_id, str) or not work_item_id.strip():
        raise ValueError("work_item_id is required")
    with repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT l.lease_id, l.automation_id, l.generation,
                      r.run_id, r.work_item_id, l.outcome,
                      c.automation_id AS command_automation_id,
                      c.automation_generation AS command_generation,
                      p.plugin_id,
                      EXISTS (SELECT 1 FROM automation_write_attempt_receipts a
                              WHERE a.lease_id=l.lease_id
                                AND a.legacy_scope_quarantined_at IS NOT NULL
                      ) AS legacy_scope_unavailable
               FROM automation_project_generation_leases l
               JOIN agent_runs r ON r.run_id=l.orchestration_run_id
               JOIN agent_commands c ON c.command_id=r.command_id
               LEFT JOIN automation_projects p ON p.automation_id=l.automation_id
               WHERE r.work_item_id=%s AND l.outcome='WRITE_OUTCOME_UNKNOWN'
               ORDER BY r.run_no, l.generation, l.acquired_at, l.lease_id
               LIMIT 101""",
            (work_item_id,),
        )
        raw_rows = cursor.fetchall()
        columns = [column[0] for column in cursor.description]
        if len(raw_rows) > 100:
            raise ValueError("WORK_ITEM_RECOVERY_LIMIT_EXCEEDED")
        rows = [dict(raw) if isinstance(raw, Mapping) else dict(zip(columns, raw)) for raw in raw_rows]
        if not rows:
            return []
        cursor.execute(
            """SELECT a.receipt_id, a.lease_id, a.automation_id, a.generation,
                      a.orchestration_run_id, r.work_item_id,
                      a.operation, a.action, a.outcome, a.created_at, a.updated_at,
                      CASE WHEN JSON_TYPE(JSON_EXTRACT(a.target_ref_json, '$.record_count'))='INTEGER'
                           THEN JSON_EXTRACT(a.target_ref_json, '$.record_count') END AS record_count,
                      a.execution_resource_keys_json, a.legacy_scope_quarantined_at
               FROM automation_write_attempt_receipts a
               JOIN automation_project_generation_leases l
                 ON l.lease_id=a.lease_id AND l.automation_id=a.automation_id
                AND l.generation=a.generation AND l.orchestration_run_id=a.orchestration_run_id
               JOIN agent_runs r ON r.run_id=l.orchestration_run_id
               WHERE r.work_item_id=%s AND l.outcome='WRITE_OUTCOME_UNKNOWN'
               ORDER BY a.lease_id, a.created_at, a.receipt_id LIMIT 1001""",
            (work_item_id,),
        )
        attempts = cursor.fetchall()
        if len(attempts) > _MAX_WRITE_ATTEMPTS:
            raise ValueError("WORK_ITEM_WRITE_ATTEMPT_LIMIT_EXCEEDED")
        attempt_columns = [column[0] for column in cursor.description]
        by_lease = {row["lease_id"]: row for row in rows}
        grouped = {lease_id: [] for lease_id in by_lease}
        for raw in attempts:
            attempt = dict(raw) if isinstance(raw, Mapping) else dict(zip(attempt_columns, raw))
            lease = by_lease.get(attempt["lease_id"])
            if lease is None or any(attempt[field] != expected for field, expected in (
                ("work_item_id", work_item_id), ("automation_id", lease["automation_id"]),
                ("generation", lease["generation"]), ("orchestration_run_id", lease["run_id"]),
            )):
                raise ValueError("WORK_ITEM_WRITE_ATTEMPT_IDENTITY_MISMATCH")
            grouped[attempt["lease_id"]].append(_write_attempt(attempt))
    result = []
    for row in rows:
        row["identity_valid"] = (
            row["command_automation_id"] == row["automation_id"]
            and row["command_generation"] == row["generation"]
        )
        row["legacy_scope_unavailable"] = bool(row["legacy_scope_unavailable"])
        # Do not expose any unselected fields even if a test adapter returns more.
        result.append({**{name: row[name] for name in (
            "lease_id", "automation_id", "generation", "run_id", "work_item_id",
            "outcome", "plugin_id", "identity_valid", "legacy_scope_unavailable",
        )}, "write_attempts": grouped[row["lease_id"]]})
    return result
