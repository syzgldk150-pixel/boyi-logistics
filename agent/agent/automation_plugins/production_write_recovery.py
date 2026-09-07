"""Evidence recovery and conditional Runner wake for the production target.

The public bound methods remain on MySQLRuntimeTargetService. This helper has
no configuration or service creation; it uses the target's existing adapter.
"""

from __future__ import annotations

from typing import Any, Mapping


def recover_unknown_write(
    self,
    *,
    automation_id: str,
    generation: int,
    lease_id: str,
    request_id: str,
    actor_id: str,
    actor_role: str,
    authoritative_applied_proof: Mapping[str, object] | None = None,
    authoritative_not_applied_proof: Mapping[str, object] | None = None,
    scan_applied_recovery: Mapping[str, object] | None = None,
    resume_run: bool = True,
    expected_run_id: str | None = None,
    expected_work_item_id: str | None = None,
) -> dict[str, Any]:
    """Resolve only from server-owned durable receipt evidence."""

    manual = {} if resume_run else {
        "resume_run": False, "expected_run_id": expected_run_id,
        "expected_work_item_id": expected_work_item_id,
    }
    result = self._runtime.resolve_unknown_write_recovery(
        automation_id=automation_id,
        generation=generation,
        lease_id=lease_id,
        request_id=request_id,
        actor_id=actor_id,
        actor_role=actor_role,
        authoritative_applied_proof=authoritative_applied_proof,
        authoritative_not_applied_proof=authoritative_not_applied_proof,
        scan_applied_recovery=scan_applied_recovery,
        **manual,
    )
    run_id = str(result.get("run_id") or "")
    if resume_run and result.get("transitioned") is True and run_id and self._wake_runner:
        self._wake_runner(run_id)
    return result

def recover_current_unknown_write(
    self,
    *,
    automation_id: str,
    generation: int,
    request_id: str,
    actor_id: str,
    actor_role: str,
    authoritative_applied_proof: Mapping[str, object] | None = None,
    authoritative_not_applied_proof: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Resolve the sole current unknown lease from server-owned evidence."""

    result = self._runtime.resolve_current_unknown_write_recovery(
        automation_id=automation_id,
        generation=generation,
        request_id=request_id,
        actor_id=actor_id,
        actor_role=actor_role,
        authoritative_applied_proof=authoritative_applied_proof,
        authoritative_not_applied_proof=authoritative_not_applied_proof,
    )
    run_id = str(result.get("run_id") or "")
    if result.get("transitioned") is True and run_id and self._wake_runner:
        self._wake_runner(run_id)
    return result
