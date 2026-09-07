"""Manual verification must not continue or recreate a historical Run."""
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.automation_plugins.management_api import UnknownWriteRecoveryRequest
from agent.automation_plugins.production import MySQLRuntimeTargetService
from shared.automation_unknown_write_recovery import recover_unknown_automation_write
from shared.orchestration_repository_support import IdempotencyConflict
from tests.test_automation_unknown_write_recovery_transaction import _uow


def receipt(outcome):
    return {"receipt_id": "receipt-1", "orchestration_run_id": "run-1", "step_id": "step-1",
            "operation": "network.request", "action": "feishu.sheet.replace",
            "argument_sha256": "a" * 64, "target_ref_sha256": "b" * 64,
            "outcome": outcome, "evidence_sha256": "c" * 64 if outcome != "WRITE_OUTCOME_UNKNOWN" else ""}


def verify(uow, **overrides):
    return recover_unknown_automation_write(uow, **{
        "automation_id": "arrival_stats", "generation": 2, "lease_id": "lease-1",
        "request_id": "manual-1", "actor_id": "admin-1", "actor_role": "super_admin",
        "resume_run": False, "expected_run_id": "run-1", "expected_work_item_id": "work-1",
        **overrides,
    })


@pytest.mark.parametrize("status", ["BLOCKED_DATA", "CANCELLED"])
@pytest.mark.parametrize("outcome,expected", [("WRITE_VERIFIED", "APPLIED"), ("NOT_APPLIED", "NOT_APPLIED")])
def test_exact_proven_scope_is_closed_without_restarting_old_run(status, outcome, expected):
    uow, plugins, runs, steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt(outcome)])
    runs.row.update(status=status, worker_id=None, lease_expires_at=None)
    uow.work_items.row["status"] = "CANCELLED" if status == "CANCELLED" else "BLOCKED_DATA"
    original = copy.deepcopy((runs.row, steps.row, uow.work_items.row))
    result = verify(uow)
    assert result["recovery_status"] == expected
    assert result["resume_run"] is False
    assert (runs.row, steps.row, uow.work_items.row) == original
    assert runs.releases == 0 and steps.transitions == []
    assert plugins.settled
    assert {entry["consumer_name"] for entry in uow.events.outbox} == {"orchestration.audit"}
    assert verify(uow)["idempotent"] is True


def test_unknown_evidence_stays_unknown_and_makes_no_state_changes():
    uow, plugins, runs, steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt("WRITE_OUTCOME_UNKNOWN")])
    runs.row.update(worker_id=None, lease_expires_at=None)
    result = verify(uow)
    assert result["recovery_status"] == "UNKNOWN"
    assert plugins.settled == [] and uow.events.outbox == []
    assert plugins.receipts[0]["outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert runs.releases == 0 and steps.transitions == []


def test_manual_failed_before_write_does_not_require_permission_to_retry():
    uow, plugins, runs, steps = _uow(outcome="FAILED_BEFORE_WRITE", receipts=[], retry_safe=False)
    runs.row.update(status="CANCELLED", worker_id=None, lease_expires_at=None)
    original = copy.deepcopy((runs.row, steps.row, uow.work_items.row))
    assert verify(uow)["recovery_status"] == "NOT_APPLIED"
    assert plugins.settled and (runs.row, steps.row, uow.work_items.row) == original
    assert {entry["consumer_name"] for entry in uow.events.outbox} == {"orchestration.audit"}


@pytest.mark.parametrize("field", ["expected_run_id", "expected_work_item_id"])
def test_foreign_run_or_work_item_is_rejected_before_settlement(field):
    uow, plugins, runs, _steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt("WRITE_VERIFIED")])
    runs.row.update(worker_id=None, lease_expires_at=None)
    with pytest.raises(IdempotencyConflict):
        verify(uow, **{field: "another-record"})
    assert plugins.settled == [] and uow.events.outbox == []


def test_live_claim_is_not_modified_but_expired_claim_does_not_block_verification():
    uow, plugins, runs, _steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt("WRITE_VERIFIED")])
    runs.row["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert verify(uow)["recovery_status"] == "UNKNOWN"
    assert plugins.settled == []
    runs.row["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert verify(uow)["recovery_status"] == "APPLIED"
    assert runs.releases == 0


def test_manual_api_requires_closed_identity_and_rejects_actor_proof():
    base = {"generation": 2, "lease_id": "lease-1", "request_id": "request-1", "resume_run": False}
    with pytest.raises(ValueError):
        UnknownWriteRecoveryRequest(**base)
    manual = {**base, "expected_run_id": "run-1", "expected_work_item_id": "work-1"}
    assert UnknownWriteRecoveryRequest(**manual).resume_run is False
    with pytest.raises(ValueError):
        UnknownWriteRecoveryRequest(**manual, evidence_sha256="a" * 64)


def test_production_manual_verification_never_wakes_runner():
    calls, wakes = [], []
    target = SimpleNamespace(
        _runtime=SimpleNamespace(resolve_unknown_write_recovery=lambda **kwargs: (
            calls.append(kwargs) or {"recovery_status": "APPLIED", "run_id": "run-1", "transitioned": True}
        )), _wake_runner=wakes.append,
    )
    result = MySQLRuntimeTargetService.recover_unknown_write(
        target, automation_id="arrival_stats", generation=2, lease_id="lease-1", request_id="request-1",
        actor_id="admin-1", actor_role="super_admin", resume_run=False,
        expected_run_id="run-1", expected_work_item_id="work-1",
    )
    assert result["recovery_status"] == "APPLIED" and wakes == []
    assert calls[0]["resume_run"] is False
