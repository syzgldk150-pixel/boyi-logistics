"""Durable call facts and request deduplication; no polling or claiming API."""
from __future__ import annotations

from typing import Any, Mapping

from shared.orchestration_repository_support import (
    IdempotencyConflict, _decode_row, _json_param, _row_dict, _rows,
)
from shared.redaction import redact_sensitive, redact_text

ACTIVE_INVOCATION_STATUSES = frozenset({"STARTING", "RUNNING", "CANCELLING"})
TERMINAL_INVOCATION_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "WRITE_OUTCOME_UNKNOWN"})
_JSON_FIELDS = ("invocation_json", "arguments_json", "result_json")


def invocation_write_receipts(connection, invocation_id: str) -> list[dict]:
    """Read only this call's payload-free write evidence, including failures."""
    with connection.cursor() as cursor:
        cursor.execute("""SELECT r.receipt_id,r.operation,r.action,r.argument_sha256,
            r.outcome,r.evidence_sha256,
            JSON_UNQUOTE(JSON_EXTRACT(r.target_ref_json,'$.role_sha256')) AS role_sha256,
            JSON_UNQUOTE(JSON_EXTRACT(r.target_ref_json,'$.binding_sha256')) AS binding_sha256
            FROM automation_write_attempt_receipts r
            JOIN automation_project_generation_leases l ON l.lease_id=r.lease_id
                AND l.invocation_id=r.invocation_id AND l.automation_id=r.automation_id
                AND l.generation=r.generation
            WHERE r.invocation_id=%s ORDER BY r.created_at,r.receipt_id""", (invocation_id,))
        return _rows(cursor)


class PluginInvocationRepository:
    def __init__(self, orchestration_repository: Any) -> None:
        self._repository = orchestration_repository

    def _read(self, clause: str, params: tuple, *, many: bool = False) -> Any:
        with self._repository.unit_of_work() as uow:
            with uow.automation_plugins.cursor() as cursor:
                cursor.execute("SELECT * FROM automation_plugin_invocations " + clause, params)
                if many:
                    return [_decode_row(row, _JSON_FIELDS) for row in _rows(cursor)]
                return _decode_row(_row_dict(cursor, cursor.fetchone()), _JSON_FIELDS)

    def get(self, invocation_id: str) -> dict | None:
        return self._read("WHERE invocation_id=%s", (invocation_id,))

    def by_request(self, request_key_sha256: str) -> dict | None:
        return self._read("WHERE request_key_sha256=%s", (request_key_sha256,))

    def list_recent(self, automation_id: str, *, limit: int = 30) -> list[dict]:
        return self._read("WHERE automation_id=%s ORDER BY status IN ('STARTING','RUNNING','CANCELLING') DESC, started_at DESC, invocation_id DESC LIMIT %s", (automation_id, max(1, min(limit, 100))), many=True)

    def create(self, row: Mapping[str, Any], *, admission_guard=None) -> dict:
        """Reserve a request exactly once, consuming a preview in the same transaction."""
        fields = ("invocation_id", "request_key_sha256", "request_sha256", "request_id", "automation_id", "plugin_id", "plugin_version", "generation", "operation", "source", "actor_id", "owner_id", "status", "invocation_json", "arguments_json", "preview_invocation_id")
        values = tuple(_json_param(row.get(key), {}) if key.endswith("_json") else row.get(key) for key in fields)
        with self._repository.unit_of_work() as uow:
            if admission_guard is not None and row["status"] == "STARTING":
                admission_guard(uow)
            with uow.automation_plugins.cursor() as cursor:
                cursor.execute("INSERT INTO automation_plugin_invocations (" + ",".join(fields) + ",started_at,updated_at) VALUES (" + ",".join(["%s"] * len(fields)) + ",UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE invocation_id=invocation_id", values)
                cursor.execute("SELECT * FROM automation_plugin_invocations WHERE request_key_sha256=%s FOR UPDATE", (row["request_key_sha256"],))
                stored = _decode_row(_row_dict(cursor, cursor.fetchone()), _JSON_FIELDS)
                if not stored or stored["request_sha256"] != row["request_sha256"]:
                    raise IdempotencyConflict("request identity was reused with different input")
                if stored["invocation_id"] == row["invocation_id"] and row.get("preview_invocation_id") and row["status"] == "STARTING":
                    cursor.execute("UPDATE automation_plugin_invocations SET preview_consumed_by=%s WHERE invocation_id=%s AND status='COMPLETED' AND preview_consumed_by IS NULL AND automation_id=%s AND generation=%s", (row["invocation_id"], row["preview_invocation_id"], row["automation_id"], row["generation"]))
                    if cursor.rowcount != 1:
                        raise IdempotencyConflict("preview was consumed or changed")
            uow.commit()
        return stored

    def update(self, invocation_id: str, *, status: str, result: Mapping | None = None, error_code: str | None = None, error_summary: str | None = None) -> dict:
        if status not in ACTIVE_INVOCATION_STATUSES | TERMINAL_INVOCATION_STATUSES:
            raise ValueError("invalid invocation status")
        with self._repository.unit_of_work() as uow:
            with uow.automation_plugins.cursor() as cursor:
                cursor.execute("UPDATE automation_plugin_invocations SET status=%s,result_json=%s,error_code=%s,error_summary=%s,finished_at=CASE WHEN %s THEN UTC_TIMESTAMP(6) ELSE NULL END,updated_at=UTC_TIMESTAMP(6) WHERE invocation_id=%s AND (status IN ('STARTING','RUNNING','CANCELLING') OR (status=%s AND finished_at IS NULL))", (status, _json_param(redact_sensitive(result), {}) if result is not None else None, error_code, redact_text(error_summary)[:1000] if error_summary else None, status in TERMINAL_INVOCATION_STATUSES, invocation_id, status))
            uow.commit()
        return self.get(invocation_id) or {}

    def has_started_write(self, invocation_id: str) -> bool:
        with self._repository.unit_of_work() as uow:
            with uow.automation_plugins.cursor() as cursor:
                cursor.execute("SELECT 1 FROM automation_write_attempt_receipts WHERE invocation_id=%s LIMIT 1", (invocation_id,))
                return cursor.fetchone() is not None

    def has_unverified_write(self, invocation_id: str) -> bool:
        """A known business failure differs from an unresolved external write."""
        with self._repository.unit_of_work() as uow:
            with uow.automation_plugins.cursor() as cursor:
                cursor.execute("SELECT 1 FROM automation_write_attempt_receipts r LEFT JOIN automation_project_generation_leases l ON l.lease_id=r.lease_id AND l.invocation_id=r.invocation_id WHERE r.invocation_id=%s AND (r.outcome<>'WRITE_VERIFIED' OR l.outcome IS NULL OR l.outcome<>'WRITE_VERIFIED') LIMIT 1", (invocation_id,))
                return cursor.fetchone() is not None

    def close_interrupted(self, owner_id: str) -> None:
        """Called only after exclusive service startup; never dispatches work."""
        with self._repository.unit_of_work() as uow:
            with uow.automation_plugins.cursor() as cursor:
                cursor.execute("UPDATE automation_plugin_invocations AS i SET i.status=CASE WHEN i.plugin_id IS NULL OR EXISTS (SELECT 1 FROM automation_write_attempt_receipts r WHERE r.invocation_id=i.invocation_id) THEN 'WRITE_OUTCOME_UNKNOWN' ELSE 'FAILED' END,i.error_code='SERVICE_INTERRUPTED',i.error_summary='Previous service stopped; this call will not be resumed',i.finished_at=UTC_TIMESTAMP(6),i.updated_at=UTC_TIMESTAMP(6) WHERE i.owner_id<>%s AND i.status IN ('STARTING','RUNNING','CANCELLING')", (owner_id,))
                cursor.execute("UPDATE automation_project_generation_leases l JOIN automation_plugin_invocations i ON i.invocation_id=l.invocation_id SET l.outcome=CASE WHEN i.status='WRITE_OUTCOME_UNKNOWN' THEN 'WRITE_OUTCOME_UNKNOWN' ELSE 'FAILED_BEFORE_WRITE' END,l.released_at=UTC_TIMESTAMP(6),l.updated_at=UTC_TIMESTAMP(6) WHERE i.owner_id<>%s AND i.status IN ('FAILED','WRITE_OUTCOME_UNKNOWN') AND l.outcome IN ('RUNNING','VERIFYING')", (owner_id,))
                cursor.execute("UPDATE automation_write_attempt_receipts r JOIN automation_plugin_invocations i ON i.invocation_id=r.invocation_id SET r.outcome='WRITE_OUTCOME_UNKNOWN',r.updated_at=UTC_TIMESTAMP(6) WHERE i.owner_id<>%s AND i.status='WRITE_OUTCOME_UNKNOWN' AND r.outcome='STARTED'", (owner_id,))
            uow.commit()
