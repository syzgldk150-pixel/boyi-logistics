"""Write-attempt receipt persistence kept separate from generation lifecycle SQL.

The functions accept the repository instance so cursor ownership remains with
the caller's Unit of Work; they do not commit, open connections, or alter the
project → generation → lease → receipt lock order.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from shared import automation_plugin_repository as _repository

IdempotencyConflict = _repository.IdempotencyConflict
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
_decode_row = _repository._decode_row
_json_hash = _repository._json_hash
_json_param = _repository._json_param
_positive_int = _repository._positive_int
_required_text = _repository._required_text
_row_dict = _repository._row_dict
_sha256 = _repository._sha256


def record_generation_write_attempt_row(
    repository: Any,
    receipt: Mapping[str, object],
) -> None:
    """Insert or exactly replay one STARTED receipt under the global locks."""

    required = {
        "automation_id", "generation", "lease_id", "orchestration_run_id",
        "step_id", "request_id", "operation", "action", "argument_sha256",
        "target_ref_sha256", "target_ref_json",
    }
    if set(receipt) != required:
        raise ValueError("write attempt receipt fields are invalid")
    automation_id = _required_text(receipt["automation_id"], "automation_id")
    generation = _positive_int(receipt["generation"], "generation")
    lease_id = _required_text(receipt["lease_id"], "lease_id")
    run_id = _required_text(receipt["orchestration_run_id"], "orchestration_run_id")
    step_id = _required_text(receipt["step_id"], "step_id")
    request_id = _required_text(receipt["request_id"], "request_id")
    operation = _required_text(receipt["operation"], "operation")
    action = _required_text(receipt["action"], "action")
    argument_sha256 = _sha256(receipt["argument_sha256"], "argument_sha256")
    target_ref_sha256 = _sha256(receipt["target_ref_sha256"], "target_ref_sha256")
    target_ref = receipt["target_ref_json"]
    target_fields = {
        "schema", "automation_id", "operation", "action", "role_sha256",
        "binding_sha256", "request_sha256", "business_date_sha256",
        "batch_sha256", "run_sha256", "idempotency_key_sha256",
        "record_count", "content_sha256",
    }
    if not isinstance(target_ref, Mapping) or set(target_ref) != target_fields:
        raise ValueError("write attempt target reference is invalid")
    if target_ref_sha256 != _json_hash(target_ref):
        raise ValueError("write attempt target reference digest does not match receipt")
    if (
        target_ref.get("schema") != 1
        or str(target_ref.get("automation_id") or "") != automation_id
        or str(target_ref.get("operation") or "") != operation
        or str(target_ref.get("action") or "") != action
        or str(target_ref.get("content_sha256") or "") != argument_sha256
    ):
        raise ValueError("write attempt target reference does not match receipt")
    for field in ("role_sha256", "binding_sha256", "request_sha256"):
        _sha256(target_ref[field], field)
    for field in (
        "business_date_sha256", "batch_sha256", "run_sha256",
        "idempotency_key_sha256",
    ):
        if target_ref[field]:
            _sha256(target_ref[field], field)
    if type(target_ref["record_count"]) is not int or target_ref["record_count"] < 0:
        raise ValueError("write attempt target record count is invalid")

    receipt_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"boyi:write-attempt:{lease_id}:{request_id}",
    ))
    expected = {
        "receipt_id": receipt_id, "automation_id": automation_id,
        "generation": generation, "lease_id": lease_id,
        "orchestration_run_id": run_id, "step_id": step_id,
        "request_id": request_id, "operation": operation, "action": action,
        "argument_sha256": argument_sha256, "target_ref_sha256": target_ref_sha256,
    }

    def require_exact(existing: Mapping[str, Any] | None) -> None:
        if existing is None or any(existing.get(key) != value for key, value in expected.items()):
            raise IdempotencyConflict("write attempt receipt conflicts with an existing request")
        existing_target = existing.get("target_ref_json")
        if not isinstance(existing_target, Mapping) or _json_hash(existing_target) != _json_hash(target_ref):
            raise IdempotencyConflict("write attempt target reference conflicts with an existing request")

    receipt_select = (
        "SELECT receipt_id, automation_id, generation, lease_id, orchestration_run_id, "
        "step_id, request_id, operation, action, argument_sha256, target_ref_sha256, "
        "target_ref_json FROM automation_write_attempt_receipts "
        "WHERE receipt_id=%s FOR UPDATE"
    )
    with repository.cursor() as cursor:
        cursor.execute(
            "SELECT automation_id, generation FROM automation_project_generation_leases WHERE lease_id=%s",
            (lease_id,),
        )
        lease_identity = _row_dict(cursor, cursor.fetchone())
        if (
            lease_identity is None
            or str(lease_identity.get("automation_id") or "") != automation_id
            or int(lease_identity.get("generation") or 0) != generation
        ):
            raise IdempotencyConflict("write attempt does not match its generation lease")
        cursor.execute(
            "SELECT automation_id FROM automation_projects WHERE automation_id=%s FOR UPDATE",
            (automation_id,),
        )
        if _row_dict(cursor, cursor.fetchone()) is None:
            raise OrchestrationPersistenceError("automation project disappeared during write attempt")
        cursor.execute(
            "SELECT automation_id, generation FROM automation_project_generations "
            "WHERE automation_id=%s AND generation=%s FOR UPDATE",
            (automation_id, generation),
        )
        if _row_dict(cursor, cursor.fetchone()) is None:
            raise OrchestrationPersistenceError("automation generation disappeared during write attempt")
        cursor.execute(
            "SELECT * FROM automation_project_generation_leases WHERE lease_id=%s FOR UPDATE",
            (lease_id,),
        )
        locked_lease = _row_dict(cursor, cursor.fetchone())
        if (
            locked_lease is None
            or str(locked_lease.get("automation_id") or "") != automation_id
            or int(locked_lease.get("generation") or 0) != generation
            or str(locked_lease.get("orchestration_run_id") or "") != run_id
        ):
            raise IdempotencyConflict("write attempt generation lease changed")
        if str(locked_lease.get("outcome") or "") != "RUNNING":
            cursor.execute(receipt_select, (receipt_id,))
            require_exact(_decode_row(_row_dict(cursor, cursor.fetchone()), ("target_ref_json",)))
            return
        cursor.execute(
            """
            INSERT INTO automation_write_attempt_receipts (
                receipt_id, automation_id, generation, lease_id,
                orchestration_run_id, step_id, request_id, operation, action,
                argument_sha256, target_ref_sha256, target_ref_json, outcome, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      'STARTED', NOW(6), NOW(6))
            ON DUPLICATE KEY UPDATE receipt_id=receipt_id
            """,
            (
                receipt_id, automation_id, generation, lease_id, run_id, step_id,
                request_id, operation, action, argument_sha256, target_ref_sha256,
                _json_param(dict(target_ref), {}),
            ),
        )
        cursor.execute(receipt_select, (receipt_id,))
        require_exact(_decode_row(_row_dict(cursor, cursor.fetchone()), ("target_ref_json",)))
