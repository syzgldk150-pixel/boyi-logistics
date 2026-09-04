from __future__ import annotations

import unittest
from types import SimpleNamespace

from shared.automation_plugin_repository import AutomationPluginRepository
from shared.orchestration_repository import OrchestrationUnitOfWork
from shared.orchestration_repository_support import _json_hash
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginRuntimeAdapter,
)


class _Plugins:
    def __init__(self, *, outcome: str, receipts: list[dict]):
        self.lease = {
            "automation_id": "arrival_stats",
            "generation": 2,
            "lease_id": "lease-1",
            "orchestration_run_id": "run-1",
            "outcome": outcome,
            "verification_evidence_sha256": None,
        }
        self.receipts = receipts
        self.settled = []

    def lock_unknown_write_recovery_context_row(self, **_kwargs):
        return {"project": {}, "generation": {}, "lease": dict(self.lease)}

    def peek_unknown_write_receipt_identity_rows(self, _lease_id):
        return [
            {key: row[key] for key in ("receipt_id", "orchestration_run_id", "step_id")}
            for row in self.receipts
        ]

    def lock_unknown_write_receipt_rows(self, _lease_id):
        return [dict(row) for row in self.receipts]

    def mark_locked_unknown_write_receipts_verified_row(
        self, *, lease_id, expected_count, evidence_sha256
    ):
        assert lease_id == "lease-1"
        changed = [
            row
            for row in self.receipts
            if row["outcome"] == "WRITE_OUTCOME_UNKNOWN"
        ]
        if len(changed) != expected_count:
            raise ValueError("receipt count changed")
        for row in changed:
            row["outcome"] = "WRITE_VERIFIED"
            row["evidence_sha256"] = evidence_sha256

    def mark_locked_unknown_write_receipts_not_applied_row(
        self, *, lease_id, expected_count, evidence_sha256
    ):
        assert lease_id == "lease-1"
        changed = [
            row
            for row in self.receipts
            if row["outcome"] == "WRITE_OUTCOME_UNKNOWN"
        ]
        if len(changed) != expected_count:
            raise ValueError("receipt count changed")
        for row in changed:
            row["outcome"] = "NOT_APPLIED"
            row["evidence_sha256"] = evidence_sha256

    def settle_unknown_write_recovery_row(self, *, recovery_status, evidence_sha256, **_kwargs):
        outcome = "WRITE_VERIFIED" if recovery_status == "APPLIED" else "FAILED_BEFORE_WRITE"
        if self.lease["outcome"] == outcome:
            if self.lease["verification_evidence_sha256"] not in {None, evidence_sha256}:
                raise ValueError("evidence conflict")
            changed = self.lease["verification_evidence_sha256"] is None
        else:
            changed = True
        self.lease["outcome"] = outcome
        self.lease["verification_evidence_sha256"] = evidence_sha256
        self.settled.append((outcome, evidence_sha256))
        return {"transitioned": changed, "outcome": outcome}


class _CurrentRecoveryUow:
    def __init__(self, candidate):
        self.candidate = candidate
        self.calls = []
        self.committed = False
        self.automation_plugins = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def unique_unknown_write_recovery_lease_row(self, **kwargs):
        self.calls.append(("candidate", kwargs))
        return dict(self.candidate)

    def recover_unknown_automation_write(self, **kwargs):
        self.calls.append(("recover", kwargs))
        return {
            "recovery_status": "APPLIED",
            "reason": "ALL_RECEIPTS_WRITE_VERIFIED",
            "run_id": "run-1",
            "step_id": "step-1",
            "transitioned": True,
            "idempotent": False,
            "evidence": {"receipt_count": 1},
        }

    def commit(self):
        self.committed = True


class _CurrentRecoveryRepository:
    def __init__(self, candidate):
        self.uow = _CurrentRecoveryUow(candidate)

    def unit_of_work(self):
        return self.uow


class _Runs:
    def __init__(self):
        self.row = {
            "run_id": "run-1", "command_id": "command-1", "work_item_id": "work-1",
            "correlation_id": "correlation-1", "causation_id": None,
            "status": "BLOCKED_DATA", "version": 1,
            "worker_id": "stale-worker", "lease_expires_at": "stale-lease",
            "error_code": "WRITE_OUTCOME_UNKNOWN",
        }
        self.releases = 0
        self.get_lock_modes = []

    def get(self, _run_id, *, for_update=False):
        self.get_lock_modes.append(for_update)
        return dict(self.row)

    def release_recovered(self, _run_id, *, status, **kwargs):
        self.releases += 1
        self.row["status"] = status
        self.row["version"] += 1
        self.row["worker_id"] = None
        self.row["lease_expires_at"] = None
        self.row["error_code"] = kwargs.get("error_code")
        return dict(self.row)


class _Steps:
    def __init__(self, *, retry_safe: bool):
        self.row = {
            "step_id": "step-1", "run_id": "run-1", "status": "BLOCKED_DATA",
            "version": 1, "retry_safe": retry_safe,
        }
        self.transitions = []

    def get(self, _step_id, *, for_update=False):
        del for_update
        return dict(self.row)

    def list_interrupted_for_run(self, _run_id):
        return [dict(self.row)] if self.row["status"] in {
            "RUNNING", "VERIFYING", "BLOCKED_DATA", "FAILED_RETRYABLE",
        } else []

    def transition(self, _step_id, *, status, **_kwargs):
        self.transitions.append({"status": status, **_kwargs})
        self.row["status"] = status
        self.row["version"] += 1
        return dict(self.row)


class _Events:
    def __init__(self):
        self.source_ids = set()
        self.outbox = []
        self.rows = {}

    def get(self, event_id):
        row = self.rows.get(event_id)
        return dict(row) if row is not None else None

    def append_with_outbox(self, event, outbox):
        created = event["source_event_id"] not in self.source_ids
        self.source_ids.add(event["source_event_id"])
        self.rows.setdefault(event["event_id"], dict(event))
        self.outbox.extend(outbox)
        return {"event": {"_created": created}, "outbox": list(outbox)}


class _WorkItems:
    def __init__(self):
        self.row = {"work_item_id": "work-1", "status": "IN_PROGRESS", "version": 1}
        self.get_lock_modes = []

    def get(self, _work_item_id, *, for_update=False):
        self.get_lock_modes.append(for_update)
        return dict(self.row)

    def transition(self, _work_item_id, *, status, **_kwargs):
        self.row["status"] = status
        self.row["version"] += 1
        return dict(self.row)


def _uow(*, outcome: str, receipts: list[dict], retry_safe: bool = True):
    plugins = _Plugins(outcome=outcome, receipts=receipts)
    runs = _Runs()
    steps = _Steps(retry_safe=retry_safe)
    uow = SimpleNamespace(
        _require_active=lambda: None,
        automation_plugins=plugins,
        runs=runs,
        steps=steps,
        commands=SimpleNamespace(get=lambda *_args, **_kwargs: {
            "automation_id": "arrival_stats", "automation_generation": 2,
        }),
        work_items=_WorkItems(),
        events=_Events(),
    )
    return uow, plugins, runs, steps


class CurrentUnknownWriteResolutionTests(unittest.TestCase):
    def test_empty_readback_siblings_resolve_in_one_commit(self):
        class BatchUow:
            def __init__(self):
                self.calls = []
                self.committed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def recover_unknown_automation_write(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "recovery_status": "NOT_APPLIED",
                    "run_id": f"run-{len(self.calls)}",
                    "transitioned": True,
                }

            def commit(self):
                self.committed = True

        uow = BatchUow()
        adapter = MySQLAutomationPluginRuntimeAdapter(
            SimpleNamespace(unit_of_work=lambda: uow)
        )

        result = adapter.resolve_unknown_writes_not_applied(
            automation_id="arrival_stats",
            generation=2,
            recoveries=[
                {
                    "lease_id": f"lease-{index}",
                    "request_id": f"request-{index}",
                    "authoritative_not_applied_proof": {
                        "receipt_identity_sha256": str(index) * 64,
                        "evidence_sha256": "e" * 64,
                    },
                }
                for index in (1, 2)
            ],
            actor_id="system:arrival-stats-readback",
            actor_role="system",
        )

        self.assertEqual("NOT_APPLIED", result["recovery_status"])
        self.assertTrue(result["transitioned"])
        self.assertEqual(["lease-1", "lease-2"], [
            call["lease_id"] for call in uow.calls
        ])
        self.assertTrue(uow.committed)

    def test_empty_readback_sibling_failure_does_not_commit(self):
        class BatchUow:
            def __init__(self):
                self.calls = 0
                self.committed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def recover_unknown_automation_write(self, **_kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("second recovery failed")
                return {"recovery_status": "NOT_APPLIED", "transitioned": True}

            def commit(self):
                self.committed = True

        uow = BatchUow()
        adapter = MySQLAutomationPluginRuntimeAdapter(
            SimpleNamespace(unit_of_work=lambda: uow)
        )
        recoveries = [
            {
                "lease_id": f"lease-{index}",
                "request_id": f"request-{index}",
                "authoritative_not_applied_proof": {
                    "receipt_identity_sha256": str(index) * 64,
                    "evidence_sha256": "e" * 64,
                },
            }
            for index in (1, 2)
        ]

        with self.assertRaisesRegex(RuntimeError, "second recovery failed"):
            adapter.resolve_unknown_writes_not_applied(
                automation_id="arrival_stats",
                generation=2,
                recoveries=recoveries,
                actor_id="system:arrival-stats-readback",
                actor_role="system",
            )

        self.assertFalse(uow.committed)

    def test_bounded_sibling_inspection_returns_each_snapshot_in_order(self):
        repository = _CurrentRecoveryRepository(
            {"state": "FOUND", "lease_id": "unused"}
        )
        repository.uow.bounded_unknown_write_recovery_lease_rows = (
            lambda **_kwargs: [
                {"lease_id": "lease-1"},
                {"lease_id": "lease-2"},
            ]
        )
        repository.uow.unknown_write_recovery_snapshot_row = (
            lambda **kwargs: {
                "state": "RECEIPTS_IDENTIFIED",
                "lease_id": kwargs["lease_id"],
                "receipt_count": 1,
                "receipts": [],
            }
        )
        adapter = MySQLAutomationPluginRuntimeAdapter(repository)

        result = adapter.inspect_current_unknown_write_recoveries(
            automation_id="arrival_stats",
            generation=2,
        )

        self.assertEqual("RECOVERY_LEASES_IDENTIFIED", result["state"])
        self.assertEqual(2, result["lease_count"])
        self.assertEqual(
            ["lease-1", "lease-2"],
            [snapshot["lease_id"] for snapshot in result["snapshots"]],
        )

    def test_unique_server_candidate_is_recovered_without_actor_lease(self):
        repository = _CurrentRecoveryRepository(
            {"state": "FOUND", "lease_id": "lease-1"}
        )
        adapter = MySQLAutomationPluginRuntimeAdapter(repository)

        result = adapter.resolve_current_unknown_write_recovery(
            automation_id="arrival_stats",
            generation=2,
            request_id="request-1",
            actor_id="admin-1",
            actor_role="super_admin",
        )

        self.assertEqual("APPLIED", result["recovery_status"])
        self.assertEqual(
            ("candidate", {"automation_id": "arrival_stats", "generation": 2}),
            repository.uow.calls[0],
        )
        self.assertEqual("lease-1", repository.uow.calls[1][1]["lease_id"])
        self.assertTrue(repository.uow.committed)

    def test_missing_or_ambiguous_candidate_remains_unknown_without_recovery(self):
        for state, reason in (
            ("MISSING", "RECOVERY_LEASE_MISSING"),
            ("AMBIGUOUS", "RECOVERY_LEASE_AMBIGUOUS"),
        ):
            with self.subTest(state=state):
                repository = _CurrentRecoveryRepository(
                    {"state": state, "lease_id": ""}
                )
                adapter = MySQLAutomationPluginRuntimeAdapter(repository)

                result = adapter.resolve_current_unknown_write_recovery(
                    automation_id="arrival_stats",
                    generation=2,
                    request_id="request-1",
                    actor_id="admin-1",
                    actor_role="super_admin",
                )

                self.assertEqual("UNKNOWN", result["recovery_status"])
                self.assertEqual(reason, result["reason"])
                self.assertEqual(["candidate"], [call[0] for call in repository.uow.calls])
                self.assertFalse(repository.uow.committed)


class UnknownWriteRecoveryTransactionTests(unittest.TestCase):
    def _recover(self, uow, request_id="request-1"):
        return OrchestrationUnitOfWork.recover_unknown_automation_write(
            uow,
            automation_id="arrival_stats",
            generation=2,
            lease_id="lease-1",
            request_id=request_id,
            actor_id="admin-1",
            actor_role="super_admin",
        )

    def test_authoritative_exact_readback_verifies_unknown_receipt_atomically(self):
        receipt = {
            "receipt_id": "receipt-1",
            "orchestration_run_id": "run-1",
            "step_id": "step-1",
            "operation": "network.request",
            "action": "feishu.sheet.replace",
            "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64,
            "outcome": "WRITE_OUTCOME_UNKNOWN",
            "evidence_sha256": "",
        }
        identity_sha256 = _json_hash([
            {
                field: receipt[field]
                for field in (
                    "receipt_id", "operation", "action", "argument_sha256",
                    "target_ref_sha256",
                )
            }
        ])
        uow, plugins, runs, steps = _uow(
            outcome="WRITE_OUTCOME_UNKNOWN",
            receipts=[receipt],
        )

        result = OrchestrationUnitOfWork.recover_unknown_automation_write(
            uow,
            automation_id="arrival_stats",
            generation=2,
            lease_id="lease-1",
            request_id="request-readback",
            actor_id="system:arrival-stats-readback",
            actor_role="system",
            authoritative_applied_proof={
                "receipt_identity_sha256": identity_sha256,
                "evidence_sha256": "c" * 64,
            },
        )

        self.assertEqual("APPLIED", result["recovery_status"])
        self.assertEqual("WRITE_VERIFIED", plugins.receipts[0]["outcome"])
        self.assertEqual("c" * 64, plugins.receipts[0]["evidence_sha256"])
        self.assertEqual("COMPLETED", steps.row["status"])
        self.assertEqual("CONTEXT_READY", runs.row["status"])

    def test_authoritative_empty_readback_closes_unknown_receipt_atomically(self):
        receipt = {
            "receipt_id": "receipt-1",
            "orchestration_run_id": "run-1",
            "step_id": "step-1",
            "operation": "network.request",
            "action": "feishu.sheet.replace",
            "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64,
            "outcome": "WRITE_OUTCOME_UNKNOWN",
            "evidence_sha256": "",
        }
        identity_sha256 = _json_hash([
            {
                field: receipt[field]
                for field in (
                    "receipt_id", "operation", "action", "argument_sha256",
                    "target_ref_sha256",
                )
            }
        ])
        uow, plugins, runs, steps = _uow(
            outcome="WRITE_OUTCOME_UNKNOWN",
            receipts=[receipt],
            retry_safe=False,
        )

        result = OrchestrationUnitOfWork.recover_unknown_automation_write(
            uow,
            automation_id="arrival_stats",
            generation=2,
            lease_id="lease-1",
            request_id="request-empty-readback",
            actor_id="system:arrival-stats-readback",
            actor_role="system",
            authoritative_not_applied_proof={
                "receipt_identity_sha256": identity_sha256,
                "evidence_sha256": "c" * 64,
            },
        )

        self.assertEqual("NOT_APPLIED", result["recovery_status"])
        self.assertEqual("NOT_APPLIED", plugins.receipts[0]["outcome"])
        self.assertEqual("c" * 64, plugins.receipts[0]["evidence_sha256"])
        self.assertEqual("FAILED_TERMINAL", steps.row["status"])
        self.assertEqual("FAILED_TERMINAL", runs.row["status"])
        self.assertEqual("CANCELLED", uow.work_items.row["status"])

    def test_applied_completes_exact_step_and_wakes_runner(self):
        receipt = {
            "receipt_id": "receipt-1", "orchestration_run_id": "run-1", "step_id": "step-1",
            "operation": "write", "action": "sync", "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64, "outcome": "WRITE_VERIFIED",
            "evidence_sha256": "c" * 64,
        }
        uow, plugins, runs, steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt])

        result = self._recover(uow)

        self.assertEqual("APPLIED", result["recovery_status"])
        self.assertTrue(result["transitioned"])
        self.assertEqual("COMPLETED", steps.row["status"])
        self.assertEqual("CONTEXT_READY", runs.row["status"])
        self.assertEqual("WRITE_VERIFIED", plugins.lease["outcome"])
        self.assertIsNone(runs.row["worker_id"])
        self.assertIsNone(runs.row["lease_expires_at"])
        self.assertTrue(any(
            item["consumer_name"] == "orchestration.run_worker"
            for item in uow.events.outbox
        ))

    def test_recovery_locks_work_item_before_run_and_reads_command_without_lock(self):
        receipt = {
            "receipt_id": "receipt-1", "orchestration_run_id": "run-1", "step_id": "step-1",
            "operation": "write", "action": "sync", "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64, "outcome": "WRITE_VERIFIED",
            "evidence_sha256": "c" * 64,
        }
        uow, _plugins, runs, _steps = _uow(
            outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt],
        )
        trace, command_locks = [], []
        original_context = uow.automation_plugins.lock_unknown_write_recovery_context_row
        original_run_get = runs.get
        original_item_get = uow.work_items.get

        def lock_context(**kwargs):
            trace.append("project-generation-lease")
            return original_context(**kwargs)

        def get_run(run_id, *, for_update=False):
            trace.append("run-lock" if for_update else "run-read")
            return original_run_get(run_id, for_update=for_update)

        def get_item(work_item_id, *, for_update=False):
            trace.append("work-item-lock" if for_update else "work-item-read")
            return original_item_get(work_item_id, for_update=for_update)

        def get_command(_command_id, *, for_update=False):
            trace.append("command-lock" if for_update else "command-read")
            command_locks.append(for_update)
            return {"automation_id": "arrival_stats", "automation_generation": 2}

        uow.automation_plugins.lock_unknown_write_recovery_context_row = lock_context
        runs.get = get_run
        uow.work_items.get = get_item
        uow.commands = SimpleNamespace(get=get_command)
        self._recover(uow)

        self.assertEqual(
            ["project-generation-lease", "run-read", "work-item-lock", "run-lock", "command-read"],
            trace[:5],
        )
        self.assertEqual([False, True], runs.get_lock_modes)
        self.assertEqual([True], uow.work_items.get_lock_modes)
        self.assertEqual([False], command_locks)

    def test_not_applied_retries_only_contractually_safe_step(self):
        uow, plugins, runs, steps = _uow(outcome="FAILED_BEFORE_WRITE", receipts=[])

        result = self._recover(uow)

        self.assertEqual("NOT_APPLIED", result["recovery_status"])
        self.assertEqual("FAILED_RETRYABLE", steps.row["status"])
        self.assertEqual("CONTEXT_READY", runs.row["status"])
        self.assertEqual("FAILED_BEFORE_WRITE", plugins.lease["outcome"])
        self.assertIsNone(runs.row["worker_id"])
        self.assertIsNone(runs.row["lease_expires_at"])
        self.assertEqual("IN_PROGRESS", uow.work_items.row["status"])

        repeated = self._recover(uow)
        self.assertEqual("NOT_APPLIED", repeated["recovery_status"])
        self.assertTrue(repeated["idempotent"])
        self.assertFalse(repeated["transitioned"])

    def test_repeat_same_request_is_idempotent(self):
        receipt = {
            "receipt_id": "receipt-1", "orchestration_run_id": "run-1", "step_id": "step-1",
            "operation": "write", "action": "sync", "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64, "outcome": "WRITE_VERIFIED",
            "evidence_sha256": "c" * 64,
        }
        uow, _plugins, runs, steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt])
        self._recover(uow)
        repeated = self._recover(uow)

        self.assertTrue(repeated["idempotent"])
        self.assertFalse(repeated["transitioned"])
        self.assertEqual(1, runs.releases)
        self.assertEqual(["COMPLETED"], [row["status"] for row in steps.transitions])

    def test_live_runner_claim_remains_unknown_without_mutation(self):
        receipt = {
            "receipt_id": "receipt-1", "orchestration_run_id": "run-1", "step_id": "step-1",
            "operation": "write", "action": "sync", "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64, "outcome": "WRITE_VERIFIED",
            "evidence_sha256": "c" * 64,
        }
        for status in ("RUNNING", "VERIFYING"):
            with self.subTest(status=status):
                uow, plugins, runs, steps = _uow(
                    outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt],
                )
                runs.row["status"] = status
                steps.row["status"] = status
                result = self._recover(uow)
                self.assertEqual("UNKNOWN", result["recovery_status"])
                self.assertEqual("RUN_RECOVERY_NOT_SETTLED", result["reason"])
                self.assertEqual(status, runs.row["status"])
                self.assertEqual(status, steps.row["status"])
                self.assertEqual([], plugins.settled)
                self.assertEqual(0, runs.releases)

    def test_reused_request_with_changed_receipt_evidence_conflicts(self):
        receipt = {
            "receipt_id": "receipt-1", "orchestration_run_id": "run-1", "step_id": "step-1",
            "operation": "write", "action": "sync", "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64, "outcome": "WRITE_VERIFIED",
            "evidence_sha256": "c" * 64,
        }
        uow, _plugins, runs, steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=[receipt])
        self._recover(uow)
        uow.automation_plugins.receipts[0]["evidence_sha256"] = "d" * 64

        with self.assertRaisesRegex(Exception, "evidence"):
            self._recover(uow)
        self.assertEqual(1, runs.releases)
        self.assertEqual(["COMPLETED"], [row["status"] for row in steps.transitions])

    def test_mixed_or_malformed_receipts_remain_unknown_without_mutation(self):
        receipts = [
            {
                "receipt_id": "receipt-1", "orchestration_run_id": "run-1", "step_id": "step-1",
                "operation": "write", "action": "sync", "argument_sha256": "a" * 64,
                "target_ref_sha256": "b" * 64, "outcome": "WRITE_VERIFIED",
                "evidence_sha256": "c" * 64,
            },
            {
                "receipt_id": "receipt-2", "orchestration_run_id": "run-1", "step_id": "step-1",
                "operation": "write", "action": "sync", "argument_sha256": "d" * 64,
                "target_ref_sha256": "e" * 64, "outcome": "STARTED",
                "evidence_sha256": "",
            },
        ]
        uow, plugins, runs, steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=receipts)
        result = self._recover(uow)
        self.assertEqual("UNKNOWN", result["recovery_status"])
        self.assertEqual("BLOCKED_DATA", runs.row["status"])
        self.assertEqual("BLOCKED_DATA", steps.row["status"])
        self.assertEqual([], plugins.settled)

        uow.automation_plugins.receipts = [
            {**receipts[0], "evidence_sha256": "not-a-sha"}
        ]
        malformed = self._recover(uow, request_id="request-2")
        self.assertEqual("UNKNOWN", malformed["recovery_status"])
        self.assertEqual([], plugins.settled)

    def test_historical_unknown_lease_without_receipt_remains_unknown(self):
        uow, plugins, runs, steps = _uow(outcome="WRITE_OUTCOME_UNKNOWN", receipts=[])
        result = self._recover(uow)
        self.assertEqual("UNKNOWN", result["recovery_status"])
        self.assertEqual("HISTORICAL_RECEIPT_UNAVAILABLE", result["reason"])
        self.assertEqual("BLOCKED_DATA", runs.row["status"])
        self.assertEqual("BLOCKED_DATA", steps.row["status"])
        self.assertEqual([], plugins.settled)

    def test_authoritative_not_applied_receipt_terminates_non_replay_safe_run(self):
        receipt = {
            "receipt_id": "receipt-not-applied",
            "orchestration_run_id": "run-1",
            "step_id": "step-1",
            "operation": "ledger.invoke",
            "action": "daily_sign.authoritative_sync",
            "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64,
            "outcome": "NOT_APPLIED",
            "evidence_sha256": "c" * 64,
        }
        uow, plugins, runs, steps = _uow(
            outcome="WRITE_OUTCOME_UNKNOWN",
            receipts=[receipt],
            retry_safe=False,
        )

        result = self._recover(uow)

        self.assertEqual("NOT_APPLIED", result["recovery_status"])
        self.assertEqual(
            "ALL_RECEIPTS_AUTHORITATIVELY_NOT_APPLIED",
            result["reason"],
        )
        self.assertEqual("FAILED_TERMINAL", runs.row["status"])
        self.assertEqual("FAILED_TERMINAL", steps.row["status"])
        self.assertEqual("NOT_APPLIED", steps.transitions[0]["postcondition_status"])
        self.assertEqual("FAILED_BEFORE_WRITE", plugins.lease["outcome"])

    def test_cancelled_run_and_work_item_stay_cancelled_after_recovery(self):
        receipt = {
            "receipt_id": "receipt-cancelled",
            "orchestration_run_id": "run-1",
            "step_id": "step-1",
            "operation": "write",
            "action": "sync",
            "argument_sha256": "a" * 64,
            "target_ref_sha256": "b" * 64,
            "outcome": "WRITE_VERIFIED",
            "evidence_sha256": "c" * 64,
        }
        uow, plugins, runs, steps = _uow(
            outcome="WRITE_OUTCOME_UNKNOWN",
            receipts=[receipt],
        )
        runs.row["status"] = "CANCELLED"
        uow.work_items.row["status"] = "CANCELLED"

        result = self._recover(uow)

        self.assertEqual("APPLIED", result["recovery_status"])
        self.assertEqual("CANCELLED", runs.row["status"])
        self.assertEqual("CANCELLED", uow.work_items.row["status"])
        self.assertEqual("COMPLETED", steps.row["status"])
        self.assertEqual("WRITE_VERIFIED", plugins.lease["outcome"])

    def test_non_replay_safe_and_illegal_run_make_no_mutation(self):
        uow, plugins, runs, steps = _uow(
            outcome="FAILED_BEFORE_WRITE", receipts=[], retry_safe=False,
        )
        unknown = self._recover(uow)
        self.assertEqual("UNKNOWN", unknown["recovery_status"])
        self.assertEqual("BLOCKED_DATA", runs.row["status"])
        self.assertEqual("BLOCKED_DATA", steps.row["status"])
        self.assertEqual([], plugins.settled)

        safe_uow, safe_plugins, safe_runs, safe_steps = _uow(
            outcome="FAILED_BEFORE_WRITE", receipts=[], retry_safe=True,
        )
        safe_runs.row["status"] = "COMPLETED"
        with self.assertRaisesRegex(Exception, "recovery Run is not retryable"):
            self._recover(safe_uow)
        self.assertEqual("BLOCKED_DATA", safe_steps.row["status"])
        self.assertEqual([], safe_plugins.settled)


class _FinalizationCursor:
    def __init__(self, outcome, *, committed_generation=3):
        self.outcome = outcome
        self.committed_generation = committed_generation
        self.executed = []
        self.rowcount = 1
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _params=()):
        compact = " ".join(statement.split())
        self.executed.append(compact)
        self.rowcount = 1
        if compact.startswith("SELECT automation_id, generation FROM automation_project_generation_leases"):
            self._row = {"automation_id": "arrival_stats", "generation": 2}
        elif "FROM automation_projects" in compact:
            self._row = {
                "automation_id": "arrival_stats",
                "committed_generation": self.committed_generation,
            }
        elif "FROM automation_project_generations" in compact:
            self._row = {"automation_id": "arrival_stats", "generation": 2}
        elif compact.startswith("SELECT * FROM automation_project_generation_leases"):
            self._row = {
                "automation_id": "arrival_stats", "generation": 2,
                "outcome": "VERIFYING", "verification_evidence_sha256": None,
            }
            if "FOR UPDATE" not in compact:
                self._row["outcome"] = self.outcome
                self._row["verification_evidence_sha256"] = "a" * 64
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _FinalizationSqlFake:
    def __init__(self, outcome, *, committed_generation=3):
        self.recording_cursor = _FinalizationCursor(
            outcome,
            committed_generation=committed_generation,
        )

    def cursor(self):
        return self.recording_cursor


class GenerationWriteFinalizationSqlTests(unittest.TestCase):
    def _finalize(self, outcome, *, committed_generation=3):
        fake = _FinalizationSqlFake(
            outcome,
            committed_generation=committed_generation,
        )
        AutomationPluginRepository.finalize_generation_write_row(
            fake,
            automation_id="arrival_stats",
            generation=2,
            lease_id="lease-1",
            outcome=outcome,
            evidence_sha256="a" * 64,
        )
        return fake.recording_cursor.executed

    def test_verified_finalization_does_not_block_project(self):
        statements = self._finalize("WRITE_VERIFIED")
        self.assertFalse(any(
            "UPDATE automation_projects" in statement
            and "BLOCKED_UNKNOWN_WRITE" in statement
            for statement in statements
        ))
        self.assertTrue(any(
            "UPDATE automation_write_attempt_receipts" in statement
            and "WRITE_VERIFIED" in statement
            for statement in statements
        ))

    def test_unknown_finalization_blocks_historical_generation_only(self):
        statements = self._finalize("WRITE_OUTCOME_UNKNOWN")
        self.assertTrue(any(
            "UPDATE automation_project_generations" in statement
            and "BLOCKED" in statement
            for statement in statements
        ))
        self.assertFalse(any(
            "UPDATE automation_projects" in statement
            and "BLOCKED_UNKNOWN_WRITE" in statement
            for statement in statements
        ))

    def test_unknown_finalization_keeps_current_generation_runnable(self):
        statements = self._finalize(
            "WRITE_OUTCOME_UNKNOWN",
            committed_generation=2,
        )
        self.assertFalse(any(
            "UPDATE automation_project_generations" in statement
            and "BLOCKED" in statement
            for statement in statements
        ))
        self.assertFalse(any(
            "UPDATE automation_projects" in statement
            and "BLOCKED_UNKNOWN_WRITE" in statement
            for statement in statements
        ))

    def test_finalization_locks_project_generation_lease_before_receipts(self):
        for outcome in ("WRITE_VERIFIED", "WRITE_OUTCOME_UNKNOWN"):
            with self.subTest(outcome=outcome):
                statements = self._finalize(outcome)
                locked = [
                    statement for statement in statements
                    if "FOR UPDATE" in statement
                ]
                self.assertIn("FROM automation_projects", locked[0])
                self.assertIn("FROM automation_project_generations", locked[1])
                self.assertIn("FROM automation_project_generation_leases", locked[2])
                self.assertGreater(
                    statements.index(next(statement for statement in statements if "automation_write_attempt_receipts" in statement)),
                    statements.index(locked[2]),
                )


class _SettlementCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 1
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _params=()):
        compact = " ".join(statement.split())
        self.executed.append(compact)
        if "FROM automation_projects" in compact:
            self._row = {
                "target_generation": 3,
                "committed_generation": 3,
                "reconcile_state": "BLOCKED_UNKNOWN_WRITE",
            }
        elif "FROM automation_project_generations" in compact:
            self._row = {"state": "BLOCKED"}
        elif "FROM automation_project_generation_leases" in compact:
            self._row = {
                "lease_id": "lease-1", "automation_id": "arrival_stats",
                "generation": 2, "outcome": "WRITE_OUTCOME_UNKNOWN",
                "verification_evidence_sha256": None,
            }
        else:
            self._row = None

    def fetchone(self):
        return self._row


class SettlementTargetIdentityTests(unittest.TestCase):
    def test_old_generation_cannot_unblock_current_project(self):
        cursor = _SettlementCursor()
        fake = SimpleNamespace(cursor=lambda: cursor)

        with self.assertRaisesRegex(Exception, "current committed target"):
            AutomationPluginRepository.settle_unknown_write_recovery_row(
                fake,
                automation_id="arrival_stats",
                generation=2,
                lease_id="lease-1",
                recovery_status="APPLIED",
                evidence_sha256="a" * 64,
            )

        self.assertFalse(any(statement.startswith("UPDATE ") for statement in cursor.executed))


class _WriteLockOrderCursor:
    def __init__(self, *, lease_outcome="RUNNING", remaining_unknown=False):
        self.executed = []
        self.rowcount = 1
        self._row = None
        self._rows = []
        self.lease_outcome = lease_outcome
        self.remaining_unknown = remaining_unknown

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _params=()):
        compact = " ".join(statement.split())
        self.executed.append(compact)
        self._rows = []
        if compact.startswith("SELECT automation_id, generation FROM automation_project_generation_leases"):
            self._row = {"automation_id": "arrival_stats", "generation": 2}
        elif "FROM automation_projects" in compact:
            self._row = {
                "automation_id": "arrival_stats", "target_generation": 2,
                "committed_generation": 2, "reconcile_state": "BLOCKED_UNKNOWN_WRITE",
            }
        elif "FROM automation_project_generations" in compact:
            self._row = {"automation_id": "arrival_stats", "generation": 2, "state": "BLOCKED"}
        elif "COUNT(*) AS unknown_count" in compact:
            self._row = {"unknown_count": 1}
        elif compact.startswith("SELECT lease_id FROM automation_project_generation_leases"):
            self._row = {"lease_id": "lease-1"}
            self._rows = [self._row] if self.remaining_unknown else []
        elif compact.startswith("SELECT * FROM automation_project_generation_leases"):
            self._row = {
                "lease_id": "lease-1", "automation_id": "arrival_stats", "generation": 2,
                "orchestration_run_id": "run-1", "outcome": self.lease_outcome,
                "verification_evidence_sha256": None,
            }
        elif "FROM automation_write_attempt_receipts" in compact and compact.startswith("SELECT"):
            self._row = {
                "receipt_id": _params[0], "automation_id": "arrival_stats", "generation": 2,
                "lease_id": "lease-1", "orchestration_run_id": "run-1", "step_id": "step-1",
                "request_id": "request-1", "operation": "write", "action": "sync",
                "argument_sha256": "a" * 64, "target_ref_sha256": _json_hash(_receipt_target()),
                "target_ref_json": _receipt_target(),
            }
        else:
            self._row = None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


def _receipt_target():
    return {
        "schema": 1, "automation_id": "arrival_stats", "operation": "write", "action": "sync",
        "role_sha256": "c" * 64, "binding_sha256": "d" * 64, "request_sha256": "e" * 64,
        "business_date_sha256": "", "batch_sha256": "", "run_sha256": "",
        "idempotency_key_sha256": "", "record_count": 0, "content_sha256": "a" * 64,
    }


def _receipt():
    return {
        "automation_id": "arrival_stats", "generation": 2, "lease_id": "lease-1",
        "orchestration_run_id": "run-1", "step_id": "step-1", "request_id": "request-1",
        "operation": "write", "action": "sync", "argument_sha256": "a" * 64,
        "target_ref_sha256": _json_hash(_receipt_target()), "target_ref_json": _receipt_target(),
    }


class GenerationWriteLockOrderSqlTests(unittest.TestCase):
    def _statements(self, method, *args, **kwargs):
        cursor = _WriteLockOrderCursor(
            lease_outcome=kwargs.pop("lease_outcome", "RUNNING"),
            remaining_unknown=kwargs.pop("remaining_unknown", False),
        )
        fake = SimpleNamespace(cursor=lambda: cursor)
        method(fake, *args, **kwargs)
        return cursor.executed

    def _assert_parent_order(self, statements):
        locked = [statement for statement in statements if "FOR UPDATE" in statement]
        self.assertIn("FROM automation_projects", locked[0])
        self.assertIn("FROM automation_project_generations", locked[1])
        self.assertIn("FROM automation_project_generation_leases", locked[2])

    def test_record_locks_parent_rows_before_inserting_or_comparing_receipt(self):
        statements = self._statements(
            AutomationPluginRepository.record_generation_write_attempt_row,
            _receipt(),
        )
        self._assert_parent_order(statements)
        receipt_index = next(index for index, statement in enumerate(statements) if "automation_write_attempt_receipts" in statement)
        lease_lock_index = next(index for index, statement in enumerate(statements) if "automation_project_generation_leases" in statement and "FOR UPDATE" in statement)
        self.assertGreater(receipt_index, lease_lock_index)

    def test_record_rejects_target_reference_digest_mismatch_before_locking(self):
        receipt = _receipt()
        receipt["target_ref_sha256"] = "0" * 64
        cursor = _WriteLockOrderCursor()
        fake = SimpleNamespace(cursor=lambda: cursor)

        with self.assertRaisesRegex(ValueError, "target reference digest"):
            AutomationPluginRepository.record_generation_write_attempt_row(fake, receipt)

        self.assertEqual([], cursor.executed)

    def test_release_and_old_not_applied_lock_parent_rows_before_lease_mutation(self):
        release = self._statements(
            AutomationPluginRepository.release_generation_lease_row,
            "lease-1", outcome="WRITE_OUTCOME_UNKNOWN",
        )
        self._assert_parent_order(release)
        old_not_applied = self._statements(
            AutomationPluginRepository.resolve_unknown_generation_write_not_applied_row,
            "arrival_stats", 2, "lease-1", evidence_sha256="a" * 64,
            lease_outcome="WRITE_OUTCOME_UNKNOWN",
        )
        self._assert_parent_order(old_not_applied)

    def test_unknown_write_block_locks_project_generation_then_leases(self):
        statements = self._statements(
            AutomationPluginRepository.block_generation_unknown_write_row,
            "arrival_stats", 2, remaining_unknown=True,
        )
        self._assert_parent_order(statements)

    def test_settlement_uses_no_inverse_for_update_when_recovery_context_holds_parents(self):
        context = {
            "project": {"automation_id": "arrival_stats", "target_generation": 2, "committed_generation": 2, "reconcile_state": "BLOCKED_UNKNOWN_WRITE"},
            "generation": {"automation_id": "arrival_stats", "generation": 2, "state": "BLOCKED"},
            "lease": {"lease_id": "lease-1", "automation_id": "arrival_stats", "generation": 2, "outcome": "WRITE_OUTCOME_UNKNOWN", "verification_evidence_sha256": None},
        }
        for recovery_status in ("APPLIED", "NOT_APPLIED"):
            with self.subTest(recovery_status=recovery_status):
                cursor = _WriteLockOrderCursor()
                fake = SimpleNamespace(cursor=lambda: cursor)
                AutomationPluginRepository.settle_unknown_write_recovery_row(
                    fake, automation_id="arrival_stats", generation=2, lease_id="lease-1",
                    recovery_status=recovery_status, evidence_sha256="a" * 64,
                    locked_context=context,
                )
                self.assertFalse(any(
                    "FOR UPDATE" in statement
                    and ("FROM automation_projects" in statement or "FROM automation_project_generations" in statement)
                    for statement in cursor.executed
                ))
                self.assertTrue(any(
                    "SELECT lease_id FROM automation_project_generation_leases" in statement
                    and "FOR UPDATE" in statement
                    for statement in cursor.executed
                ))

    def test_first_sibling_settlement_keeps_block_and_second_restores_project(self):
        context = {
            "project": {"automation_id": "arrival_stats", "target_generation": 2, "committed_generation": 2, "reconcile_state": "BLOCKED_UNKNOWN_WRITE"},
            "generation": {"automation_id": "arrival_stats", "generation": 2, "state": "BLOCKED"},
            "lease": {"lease_id": "lease-1", "automation_id": "arrival_stats", "generation": 2, "outcome": "WRITE_OUTCOME_UNKNOWN", "verification_evidence_sha256": None},
        }
        first_cursor = _WriteLockOrderCursor(remaining_unknown=True)
        first = SimpleNamespace(cursor=lambda: first_cursor)
        AutomationPluginRepository.settle_unknown_write_recovery_row(
            first, automation_id="arrival_stats", generation=2, lease_id="lease-1",
            recovery_status="APPLIED", evidence_sha256="a" * 64,
            locked_context=context,
        )
        self.assertFalse(any(
            "UPDATE automation_project_generations" in statement or "UPDATE automation_projects" in statement
            for statement in first_cursor.executed
        ))

        second_cursor = _WriteLockOrderCursor()
        second = SimpleNamespace(cursor=lambda: second_cursor)
        AutomationPluginRepository.settle_unknown_write_recovery_row(
            second, automation_id="arrival_stats", generation=2, lease_id="lease-1",
            recovery_status="APPLIED", evidence_sha256="a" * 64,
            locked_context=context,
        )
        self.assertTrue(any("UPDATE automation_project_generations" in statement for statement in second_cursor.executed))
        self.assertTrue(any("UPDATE automation_projects" in statement for statement in second_cursor.executed))

    def test_recovery_context_locks_project_generation_lease_before_run_step_receipts(self):
        cursor = _WriteLockOrderCursor(lease_outcome="WRITE_OUTCOME_UNKNOWN")
        fake = SimpleNamespace(cursor=lambda: cursor)
        AutomationPluginRepository.lock_unknown_write_recovery_context_row(
            fake, automation_id="arrival_stats", generation=2, lease_id="lease-1",
        )
        self._assert_parent_order(cursor.executed)

    def test_current_recovery_candidate_requires_exactly_one_unknown_lease(self):
        for rows, state, lease_id in (
            ([], "MISSING", ""),
            ([{"lease_id": "lease-1"}], "FOUND", "lease-1"),
            ([{"lease_id": "lease-1"}, {"lease_id": "lease-2"}], "AMBIGUOUS", ""),
        ):
            with self.subTest(state=state):
                cursor = _WriteLockOrderCursor()

                def execute(statement, params=()):
                    cursor.executed.append(" ".join(statement.split()))
                    cursor._rows = list(rows)
                    cursor._row = rows[0] if rows else None

                cursor.execute = execute
                fake = SimpleNamespace(cursor=lambda: cursor)

                result = AutomationPluginRepository.unique_unknown_write_recovery_lease_row(
                    fake,
                    automation_id="arrival_stats",
                    generation=2,
                )

                self.assertEqual(state, result["state"])
                self.assertEqual(lease_id, result["lease_id"])
                self.assertIn("LIMIT 2", cursor.executed[0])

    def test_bounded_recovery_candidates_preserve_acquisition_order(self):
        rows = [{"lease_id": "lease-1"}, {"lease_id": "lease-2"}]
        cursor = _WriteLockOrderCursor()
        cursor.params = []

        def execute(statement, params=()):
            cursor.executed.append(" ".join(statement.split()))
            cursor.params.append(params)
            cursor._rows = list(rows)

        cursor.execute = execute
        fake = SimpleNamespace(cursor=lambda: cursor)

        result = AutomationPluginRepository.bounded_unknown_write_recovery_lease_rows(
            fake,
            automation_id="arrival_stats",
            generation=2,
            limit=17,
        )

        self.assertEqual(rows, result)
        self.assertIn("ORDER BY acquired_at, lease_id", cursor.executed[0])
        self.assertEqual(("arrival_stats", 2, 17), cursor.params[0])


if __name__ == "__main__":
    unittest.main()
