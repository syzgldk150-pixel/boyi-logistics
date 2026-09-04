"""Configuration-free transactional unknown-write recovery helper.

The owning Unit of Work supplies repositories already bound to its active
transaction.  This module deliberately has no database, runtime, or package
imports, which keeps the recovery state machine independently testable.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Mapping

from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
    OrchestrationPersistenceError,
    _json_hash,
    _required_text,
)


def recover_unknown_automation_write(
    uow: Any,
    *,
    automation_id: str,
    generation: int,
    lease_id: str,
    request_id: str,
    actor_id: str,
    actor_role: str,
    authoritative_applied_proof: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Resolve one receipt-backed interrupted write in the caller's UoW."""

    uow._require_active()
    safe_request_id = _required_text(request_id, "request_id")
    safe_actor_id = _required_text(actor_id, "actor_id")
    safe_actor_role = _required_text(actor_role, "actor_role")
    context = uow.automation_plugins.lock_unknown_write_recovery_context_row(
        automation_id=automation_id, generation=generation, lease_id=lease_id,
    )
    lease = dict(context["lease"])
    run_id = _required_text(lease.get("orchestration_run_id"), "orchestration_run_id")
    # The duplicate-command gateway locks Work Item before Run.  Read the
    # immutable Run identity first, then take those mutable orchestration rows
    # in that same order.  Command identity is immutable after acceptance and
    # is intentionally read without FOR UPDATE, so recovery never forms the
    # former Run -> Command -> Work Item cycle.
    run_identity = uow.runs.get(run_id)
    if run_identity is None:
        raise OrchestrationPersistenceError("runtime recovery Run does not exist")
    work_item_id = _required_text(run_identity.get("work_item_id"), "work_item_id")
    item = uow.work_items.get(work_item_id, for_update=True)
    if item is None:
        raise OrchestrationPersistenceError("recovery Run work item does not exist")
    run = uow.runs.get(run_id, for_update=True)
    if run is None:
        raise OrchestrationPersistenceError("runtime recovery Run does not exist")
    if str(run.get("work_item_id") or "") != work_item_id:
        raise IdempotencyConflict("runtime recovery Run changed work item identity")
    command = uow.commands.get(_required_text(run.get("command_id"), "command_id"))
    if command is None or (
        str(command.get("automation_id") or "") != str(automation_id)
        or int(command.get("automation_generation") or 0) != int(generation)
    ):
        raise IdempotencyConflict("runtime recovery Run does not match automation identity")

    receipt_identities = uow.automation_plugins.peek_unknown_write_receipt_identity_rows(str(lease_id))
    step: dict[str, Any] | None = None
    if receipt_identities:
        pairs = {
            (str(item.get("orchestration_run_id") or ""), str(item.get("step_id") or ""))
            for item in receipt_identities
        }
        if len(pairs) != 1 or next(iter(pairs))[0] != run_id:
            raise IdempotencyConflict("write receipts do not identify one exact Run step")
        step = uow.steps.get(next(iter(pairs))[1], for_update=True)
        if step is None or str(step.get("run_id") or "") != run_id:
            raise IdempotencyConflict("write receipt step identity is invalid")
    elif str(lease.get("outcome") or "") == "FAILED_BEFORE_WRITE":
        interrupted = uow.steps.list_interrupted_for_run(run_id)
        if len(interrupted) != 1:
            return _unknown("FAILED_BEFORE_WRITE_STEP_AMBIGUOUS", run_id, None)
        step = interrupted[0]
    else:
        return _unknown("HISTORICAL_RECEIPT_UNAVAILABLE", run_id, None)

    receipts = uow.automation_plugins.lock_unknown_write_receipt_rows(str(lease_id))
    if step is None:
        raise AssertionError("recovery step must be resolved before receipt locking")
    step_id = str(step.get("step_id") or "")
    if any(
        str(item.get("orchestration_run_id") or "") != run_id
        or str(item.get("step_id") or "") != step_id
        for item in receipts
    ):
        raise IdempotencyConflict("locked write receipt identity changed")
    receipt_identity_sha256 = _json_hash([
        {field: str(item.get(field) or "") for field in (
            "receipt_id", "operation", "action", "argument_sha256",
            "target_ref_sha256",
        )}
        for item in receipts
    ])
    if authoritative_applied_proof is not None:
        proof = dict(authoritative_applied_proof)
        if set(proof) != {"receipt_identity_sha256", "evidence_sha256"}:
            raise ValueError("authoritative unknown-write proof is invalid")
        proof_identity = str(proof.get("receipt_identity_sha256") or "")
        proof_evidence = str(proof.get("evidence_sha256") or "")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", proof_identity)
            or not re.fullmatch(r"[0-9a-f]{64}", proof_evidence)
        ):
            raise ValueError("authoritative unknown-write proof digest is invalid")
        if proof_identity != receipt_identity_sha256:
            return _unknown(
                "AUTHORITATIVE_READBACK_PROOF_STALE",
                run_id,
                step_id,
                {
                    "receipt_count": len(receipts),
                    "receipt_digest": receipt_identity_sha256,
                },
            )
        if not receipts or any(
            str(item.get("outcome") or "") != "WRITE_OUTCOME_UNKNOWN"
            for item in receipts
        ):
            return _unknown(
                "AUTHORITATIVE_READBACK_RECEIPTS_CHANGED",
                run_id,
                step_id,
                {
                    "receipt_count": len(receipts),
                    "receipt_digest": receipt_identity_sha256,
                },
            )
        marker = getattr(
            uow.automation_plugins,
            "mark_locked_unknown_write_receipts_verified_row",
            None,
        )
        if not callable(marker):
            raise OrchestrationPersistenceError(
                "authoritative unknown-write recovery is unavailable"
            )
        marker(
            lease_id=str(lease_id),
            expected_count=len(receipts),
            evidence_sha256=proof_evidence,
        )
        receipts = [
            {
                **dict(item),
                "outcome": "WRITE_VERIFIED",
                "evidence_sha256": proof_evidence,
            }
            for item in receipts
        ]
    receipt_digest = _json_hash([
        {field: str(item.get(field) or "") for field in (
            "receipt_id", "operation", "action", "argument_sha256",
            "target_ref_sha256", "outcome", "evidence_sha256",
        )}
        for item in receipts
    ])
    evidence = {"receipt_count": len(receipts), "receipt_digest": receipt_digest}
    applied = bool(receipts) and all(
        str(item.get("outcome") or "") == "WRITE_VERIFIED"
        and re.fullmatch(r"[0-9a-f]{64}", str(item.get("evidence_sha256") or ""))
        for item in receipts
    )
    receipt_not_applied = (
        bool(receipts)
        and all(
            str(item.get("outcome") or "") == "NOT_APPLIED"
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("evidence_sha256") or ""))
            for item in receipts
        )
    )
    not_applied = receipt_not_applied or (
        not receipts and str(lease.get("outcome") or "") == "FAILED_BEFORE_WRITE"
    )
    if not applied and not not_applied:
        return _unknown("RECEIPTS_NOT_AUTHORITATIVELY_RESOLVED", run_id, step_id, evidence)
    if not_applied and not receipt_not_applied and step.get("retry_safe") is not True:
        return _unknown("UNSAFE_WRITE_RETRY_BLOCKED", run_id, step_id, evidence)
    recovery_status = "APPLIED" if applied else "NOT_APPLIED"
    if str(run.get("status") or "") in {"RUNNING", "VERIFYING"}:
        # Management recovery never steals a live Runner claim.  Runner first
        # persists its unknown-write boundary as BLOCKED_DATA, then recovery
        # owns the exact durable Run/Step pair below.
        return _unknown("RUN_RECOVERY_NOT_SETTLED", run_id, step_id, evidence)
    terminal = str(lease.get("outcome") or "") in {"WRITE_VERIFIED", "FAILED_BEFORE_WRITE"}
    event_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"boyi:automation-write-recovery:{safe_request_id}",
    ))
    retry_safe = step.get("retry_safe") is True
    if _is_persisted_recovery(
        run,
        step,
        recovery_status,
        terminal,
        retry_safe=retry_safe,
    ):
        _require_same_recovery_event(
            uow.events, event_id, run_id, step_id, recovery_status, receipt_digest,
        )
    step_transitioned, run_transitioned, run = _transition_recovery(
        uow, run, step, run_id, recovery_status, terminal, receipt_digest,
        retry_safe=retry_safe,
    )
    settled = uow.automation_plugins.settle_unknown_write_recovery_row(
        automation_id=automation_id, generation=generation, lease_id=lease_id,
        recovery_status=recovery_status, evidence_sha256=receipt_digest,
        locked_context=context,
    )
    current_item_status = str(item.get("status") or "")
    desired_item_status = (
        "CANCELLED"
        if current_item_status == "CANCELLED"
        else "IN_PROGRESS"
        if str(run.get("status") or "") == "CONTEXT_READY" or recovery_status == "APPLIED"
        else "OPEN"
    )
    if str(item.get("status") or "") != desired_item_status:
        uow.work_items.transition(
            str(item["work_item_id"]), expected_version=int(item["version"]),
            expected_statuses=(str(item["status"]),), status=desired_item_status,
            reason_code=None if recovery_status == "APPLIED" else "RECONCILED_NOT_APPLIED",
        )
    event_receipt = uow.events.append_with_outbox(
        _event(run, step_id, automation_id, generation, lease_id, recovery_status,
               receipt_digest, safe_request_id, safe_actor_id, safe_actor_role),
        (
            {"consumer_name": "orchestration.audit", "topic": "automation_plugin.write_recovered",
             "partition_key": str(run["work_item_id"]), "max_attempts": 10},
            {"consumer_name": "orchestration.run_worker", "topic": "automation_plugin.write_recovered",
             "partition_key": str(run["work_item_id"]), "max_attempts": 10},
        ),
    )
    transitioned = bool(step_transitioned or run_transitioned or settled["transitioned"])
    return {
        "recovery_status": recovery_status,
        "reason": (
            "ALL_RECEIPTS_WRITE_VERIFIED"
            if applied
            else "ALL_RECEIPTS_AUTHORITATIVELY_NOT_APPLIED"
            if receipt_not_applied
            else "ZERO_STARTED_WRITES_FAILED_BEFORE_WRITE"
        ),
        "run_id": run_id, "step_id": step_id, "transitioned": transitioned,
        "idempotent": not transitioned and not bool(event_receipt["event"].get("_created")),
        "evidence": evidence,
    }


def _unknown(reason: str, run_id: str, step_id: str | None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "recovery_status": "UNKNOWN", "reason": reason, "run_id": run_id,
        "step_id": step_id, "transitioned": False, "idempotent": False,
        "evidence": evidence or {"receipt_count": 0, "receipt_digest": _json_hash([])},
    }


def _transition_recovery(uow: Any, run: dict[str, Any], step: dict[str, Any], run_id: str,
                         recovery_status: str, terminal: bool, receipt_digest: str,
                         *, retry_safe: bool) -> tuple[bool, bool, dict[str, Any]]:
    run_status, step_status = str(run.get("status") or ""), str(step.get("status") or "")
    applied = recovery_status == "APPLIED"
    complete_status = (
        "COMPLETED" if applied else "FAILED_RETRYABLE" if retry_safe else "FAILED_TERMINAL"
    )
    if run_status == "BLOCKED_DATA" and step_status == "BLOCKED_DATA":
        kwargs: dict[str, Any] = {
            "expected_version": int(step["version"]),
            "expected_statuses": ("BLOCKED_DATA",),
            "status": complete_status,
            "result_summary": {"reconciliation": recovery_status, "receipt_digest": receipt_digest},
            "postcondition_status": (
                "VERIFIED_RECEIPT"
                if applied
                else "RETRY_ALLOWED"
                if retry_safe
                else "NOT_APPLIED"
            ),
            "finished_at": datetime.now(),
        }
        if applied:
            kwargs["postcondition"] = {"receipt_digest": receipt_digest}
        else:
            kwargs.update(
                error_code="RECONCILED_NOT_APPLIED",
                error_summary="Server evidence proved no broker write started",
            )
        uow.steps.transition(str(step["step_id"]), **kwargs)
        recovered_run_status = (
            "CONTEXT_READY" if applied or retry_safe else "FAILED_TERMINAL"
        )
        run = uow.runs.release_recovered(
            run_id, expected_version=int(run["version"]), expected_statuses=("BLOCKED_DATA",),
            status=recovered_run_status,
            error_code=None if applied else "RECONCILED_NOT_APPLIED",
            error_summary=None if applied else "Server evidence proved no intended write applied",
            retryable=bool(not applied and retry_safe),
        )
        return True, True, run
    if run_status == "CANCELLED" and step_status == "BLOCKED_DATA":
        kwargs = {
            "expected_version": int(step["version"]),
            "expected_statuses": ("BLOCKED_DATA",),
            "status": complete_status,
            "result_summary": {"reconciliation": recovery_status, "receipt_digest": receipt_digest},
            "postcondition_status": "VERIFIED_RECEIPT" if applied else "NOT_APPLIED",
            "finished_at": datetime.now(),
        }
        if applied:
            kwargs["postcondition"] = {"receipt_digest": receipt_digest}
        else:
            kwargs.update(
                error_code="RECONCILED_NOT_APPLIED",
                error_summary="Server evidence proved no intended write applied",
            )
        uow.steps.transition(str(step["step_id"]), **kwargs)
        return True, False, run
    if terminal and step_status == complete_status and run_status != "BLOCKED_DATA":
        return False, False, run
    if step_status != "BLOCKED_DATA":
        raise ConcurrentUpdateError("recovery step is not interrupted or completed" if applied else "recovery step is not retryable")
    raise ConcurrentUpdateError("recovery Run is not runnable" if applied else "recovery Run is not retryable")


def _is_persisted_recovery(
    run: dict[str, Any],
    step: dict[str, Any],
    recovery_status: str,
    terminal: bool,
    *,
    retry_safe: bool,
) -> bool:
    expected_step = (
        "COMPLETED"
        if recovery_status == "APPLIED"
        else "FAILED_RETRYABLE"
        if retry_safe
        else "FAILED_TERMINAL"
    )
    return (
        terminal
        and str(step.get("status") or "") == expected_step
        and str(run.get("status") or "") != "BLOCKED_DATA"
    )


def _require_same_recovery_event(events: Any, event_id: str, run_id: str, step_id: str,
                                 recovery_status: str, receipt_digest: str) -> None:
    getter = getattr(events, "get", None)
    if not callable(getter):
        raise ConcurrentUpdateError("recovery event lookup is unavailable")
    event = getter(event_id)
    payload = event.get("payload_json", event.get("payload", {})) if isinstance(event, dict) else {}
    if not isinstance(payload, dict) or (
        str(event.get("run_id") or "") != run_id
        or str(event.get("step_id") or "") != step_id
        or str(payload.get("recovery_status") or "") != recovery_status
        or str(payload.get("receipt_digest") or "") != receipt_digest
    ):
        raise IdempotencyConflict("recovery state lacks matching request evidence")


def _event(run: dict[str, Any], step_id: str, automation_id: str, generation: int, lease_id: str,
           recovery_status: str, receipt_digest: str, request_id: str, actor_id: str, actor_role: str) -> dict[str, Any]:
    now = datetime.now()
    return {
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"boyi:automation-write-recovery:{request_id}")),
        "event_type": "automation_plugin.write_recovered", "schema_version": 1,
        "source_system": "agent", "source_event_id": f"automation-write-recovery:{request_id}",
        "entity_type": "agent_run", "entity_id": run["run_id"], "work_item_id": run["work_item_id"],
        "run_id": run["run_id"], "step_id": step_id, "occurred_at": now, "observed_at": now,
        "correlation_id": run["correlation_id"], "causation_id": run.get("causation_id"),
        "payload": {"automation_id": automation_id, "generation": generation, "lease_id": lease_id,
                    "recovery_status": recovery_status, "receipt_digest": receipt_digest,
                    "actor_id": actor_id, "actor_role": actor_role},
    }
