from __future__ import annotations

import copy
import unittest

from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    OperationType,
    OrchestrationError,
    PlanStep,
    RiskLevel,
)
from agent.orchestration.workflow_runner import WorkflowRunner


class _Steps:
    def __init__(self, repository):
        self.repository = repository

    def transition(self, step_id, *, expected_version, expected_statuses, status, **values):
        row = self.repository.step
        if (
            row["step_id"] != step_id
            or row["version"] != expected_version
            or row["status"] not in expected_statuses
        ):
            raise RuntimeError("step CAS conflict")
        row.update(values)
        row["status"] = status
        row["version"] += 1
        return copy.deepcopy(row)


class _Events:
    def __init__(self, repository):
        self.repository = repository

    def append_with_outbox(self, event, outbox):
        self.repository.events.append((copy.deepcopy(event), copy.deepcopy(tuple(outbox))))


class _Uow:
    def __init__(self, repository):
        self.steps = _Steps(repository)
        self.events = _Events(repository)
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def commit(self):
        self.committed = True
        self.steps.repository.commits += 1


class _Repository:
    def __init__(self):
        self.step = {
            "step_id": "step-id",
            "run_id": "run-id",
            "status": "RUNNING",
            "version": 3,
        }
        self.events = []
        self.commits = 0

    def unit_of_work(self):
        return _Uow(self)


class _Catalog:
    def __init__(self, capability):
        self.capability = capability

    def get_capability(self, _tool_name):
        return copy.deepcopy(self.capability)


class _Execution:
    def __init__(self, reconciliation=None):
        self.reconciliation = reconciliation or {
            "resolution": "UNSUPPORTED",
            "code": "RECONCILER_NOT_CONFIGURED",
        }

    async def reconcile_step(self, *args, **kwargs):
        del args, kwargs
        return copy.deepcopy(self.reconciliation)


class _IncompletePilotProjection:
    def __init__(self):
        self.calls = []

    def record_incomplete_attempt(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return None


class _ApprovalConsumptionUow:
    def __init__(self):
        self.calls = []
        self.approvals = self
        self.runs = self
        self.work_items = self
        self.events = self

    def __enter__(self):
        self.calls.append("enter")
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        self.calls.append("exit")
        return False

    def prepare_approved_execution(self, run_id, *, expected_plan_hash):
        self.calls.append(("prepare", run_id, expected_plan_hash))
        return {
            "outcome": "APPROVED",
            "run": {
                "run_id": run_id,
                "work_item_id": "work-id",
                "status": "WAITING_APPROVAL",
                "version": 5,
                "correlation_id": "correlation-id",
            },
            "approval": {"approval_id": "approval-id", "approval_round": 3},
        }

    def transition(self, row_id, **values):
        if row_id == "run-id":
            self.calls.append(
                (
                    "run_transition",
                    values["status"],
                    values.get("increment_execution_attempt", False),
                )
            )
            return {
                "run_id": row_id,
                "work_item_id": "work-id",
                "status": values["status"],
                "version": 6,
                "correlation_id": "correlation-id",
            }
        self.calls.append(("item_transition", values["status"]))
        return {"work_item_id": row_id, "status": values["status"], "version": 3}

    def get(self, work_item_id, *, for_update=False):
        del for_update
        self.calls.append(("work_item_lock", work_item_id))
        return {"work_item_id": work_item_id, "status": "WAITING_APPROVAL", "version": 2}

    def append_with_outbox(self, event, outbox):
        del event, outbox
        self.calls.append("event")

    def commit(self):
        self.calls.append("commit")


class _ApprovalConsumptionRepository:
    def __init__(self):
        self.uow = _ApprovalConsumptionUow()

    def unit_of_work(self):
        return self.uow


def _step(operation_type: OperationType) -> PlanStep:
    return PlanStep(
        step_key="write",
        tool_name="governed_tool",
        tool_version="1.0.0",
        operation_type=operation_type,
        arguments={"record_id": "record-1"},
        account_id="account-1",
        depends_on=(),
        idempotency_key="step-key",
        expected_evidence=(),
        postconditions=(),
        risk_level=RiskLevel.HIGH,
    )


def _command() -> Command:
    return Command(
        command_type="tool.execute",
        source="console",
        actor=Actor(ActorType.CONSOLE_ADMIN, "17", ("super_admin",)),
        parameters={},
        idempotency_key="command-key",
    )


class WorkflowRunnerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _runner(self, repository, capability, execution):
        runner = WorkflowRunner.__new__(WorkflowRunner)
        runner._repository = repository
        runner._catalog = _Catalog(capability)
        runner._execution_port = execution
        runner._pilot_projection = None
        runner._worker_id = "worker-1"
        runner._lease_seconds = 120
        return runner

    async def test_interrupted_external_write_without_reconciler_blocks_instead_of_replaying(self):
        repository = _Repository()
        runner = self._runner(
            repository,
            {
                "operation_type": "external_write",
                "retry": {"safe": False},
                "idempotency": {"mode": "none"},
            },
            _Execution(),
        )

        with self.assertRaises(OrchestrationError) as raised:
            await runner._recover_interrupted_step(
                {
                    "run_id": "run-id",
                    "work_item_id": "work-id",
                    "correlation_id": "correlation-id",
                    "causation_id": None,
                },
                _step(OperationType.EXTERNAL_WRITE),
                copy.deepcopy(repository.step),
                _command(),
            )

        self.assertEqual("WRITE_OUTCOME_UNKNOWN", raised.exception.code)
        self.assertEqual("BLOCKED_DATA", repository.step["status"])
        self.assertEqual(1, len(repository.events))

    async def test_interrupted_idempotent_projection_is_the_only_write_replayed_without_reconciliation(self):
        repository = _Repository()
        runner = self._runner(
            repository,
            {
                "operation_type": "internal_projection_write",
                "retry": {"safe": True},
                "idempotency": {"mode": "key"},
            },
            _Execution(),
        )

        recovered = await runner._recover_interrupted_step(
            {
                "run_id": "run-id",
                "work_item_id": "work-id",
                "correlation_id": "correlation-id",
            },
            _step(OperationType.INTERNAL_PROJECTION_WRITE),
            copy.deepcopy(repository.step),
            _command(),
        )

        self.assertEqual("FAILED_RETRYABLE", recovered["status"])
        self.assertEqual("RETRY_ALLOWED", recovered["postcondition_status"])
        self.assertEqual([], repository.events)

    async def test_live_approval_and_waiting_run_transition_share_one_transaction(self):
        repository = _ApprovalConsumptionRepository()
        runner = self._runner(repository, {}, _Execution())

        run, outcome = runner._consume_approved_plan("run-id", "plan-hash")

        self.assertEqual("APPROVED", outcome)
        self.assertEqual("RUNNING", run["status"])
        calls = repository.uow.calls
        self.assertEqual(1, calls.count("enter"))
        run_transition = ("run_transition", "RUNNING", True)
        self.assertLess(calls.index(("prepare", "run-id", "plan-hash")), calls.index(run_transition))
        self.assertLess(calls.index(run_transition), calls.index("commit"))

    async def test_projection_failure_evidence_commits_with_blocked_step(self):
        repository = _Repository()
        runner = self._runner(repository, {}, _Execution())
        pilot_projection = _IncompletePilotProjection()
        runner._pilot_projection = pilot_projection
        result = type(
            "Result",
            (),
            {"to_dict": lambda self: {"status": "SUCCESS", "data": {}, "meta": {}}},
        )()

        runner._persist_blocked_pilot_projection(
            run={
                "run_id": "run-id",
                "work_item_id": "work-id",
                "correlation_id": "correlation-id",
            },
            started_step=copy.deepcopy(repository.step),
            step=_step(OperationType.READ),
            command=_command(),
            raw_result={"status": "SUCCESS", "data": {}, "meta": {}},
            result=result,
            error=OrchestrationError(
                "PAGINATION_INCOMPLETE",
                "source pagination is incomplete",
                details={"status": "BLOCKED_DATA"},
            ),
        )

        self.assertEqual("BLOCKED_DATA", repository.step["status"])
        self.assertEqual("PROJECTION_INCOMPLETE", repository.step["postcondition_status"])
        self.assertEqual(1, repository.commits)
        self.assertEqual(1, len(repository.events))
        self.assertEqual(1, len(pilot_projection.calls))
        self.assertEqual(
            "PAGINATION_INCOMPLETE",
            pilot_projection.calls[0]["failure_code"],
        )


if __name__ == "__main__":
    unittest.main()
