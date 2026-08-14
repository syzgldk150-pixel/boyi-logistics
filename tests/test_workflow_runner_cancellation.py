from __future__ import annotations

import asyncio
import copy
from datetime import datetime

import pytest

from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
    RunStatus,
)
from agent.orchestration.result_verifier import ResultVerifier
from agent.orchestration.workflow_runner import WorkflowRunner


class _Catalog:
    def __init__(self, capability):
        self.capability = capability

    def get_capability(self, tool_name):
        return copy.deepcopy(self.capability) if tool_name == "cancel_tool" else None


class _ProcessExecutor:
    def __init__(self, result):
        self.result = result

    async def execute(self, _capability, _arguments):
        return copy.deepcopy(self.result)

    def running_tool_info(self, _tool_name):
        return {"started_at": ""}


class _CapturingExecutionPort:
    def __init__(self):
        self.execute_contexts = []
        self.reconcile_contexts = []

    async def execute_step(self, _step, **kwargs):
        self.execute_contexts.append(copy.deepcopy(kwargs["execution_context"]))
        return {
            "status": "FAILED",
            "data": {},
            "meta": {},
            "warnings": [],
            "error": {
                "code": "CAPTURED_FAILURE",
                "message": "stop after capturing execution context",
                "retryable": False,
            },
        }

    async def reconcile_step(self, _step, **kwargs):
        self.reconcile_contexts.append(copy.deepcopy(kwargs["execution_context"]))
        return {"resolution": "NOT_APPLIED"}


class _Steps:
    def __init__(self, repository):
        self.repository = repository

    def create_or_get(self, row):
        if self.repository.step is None:
            self.repository.step = {
                **copy.deepcopy(dict(row)),
                "status": "PENDING",
                "version": 1,
            }
        return copy.deepcopy(self.repository.step)

    def transition(self, step_id, *, expected_version, expected_statuses, status, **values):
        row = self.repository.step
        assert row is not None
        if (
            row["step_id"] != step_id
            or row["version"] != expected_version
            or row["status"] not in expected_statuses
        ):
            raise RuntimeError("step CAS conflict")
        row.update(copy.deepcopy(values))
        row["status"] = status
        row["version"] += 1
        return copy.deepcopy(row)


class _Runs:
    def __init__(self, repository):
        self.repository = repository

    def get(self, run_id, *, for_update=False):
        del for_update
        if self.repository.run["run_id"] != run_id:
            return None
        return copy.deepcopy(self.repository.run)

    def release_or_schedule(self, run_id, *, worker_id, status, **values):
        row = self.repository.run
        if row["run_id"] != run_id or row["worker_id"] != worker_id:
            raise RuntimeError("run lease conflict")
        row.update(copy.deepcopy(values))
        row["status"] = status
        row["worker_id"] = None
        row["lease_expires_at"] = None
        row["version"] += 1
        return copy.deepcopy(row)


class _WorkItems:
    def __init__(self, repository):
        self.repository = repository

    def get(self, work_item_id, *, for_update=False):
        del for_update
        if self.repository.work_item["work_item_id"] != work_item_id:
            return None
        return copy.deepcopy(self.repository.work_item)

    def transition(self, work_item_id, *, expected_version, expected_statuses, status, **values):
        row = self.repository.work_item
        if (
            row["work_item_id"] != work_item_id
            or row["version"] != expected_version
            or row["status"] not in expected_statuses
        ):
            raise RuntimeError("work item CAS conflict")
        row.update(copy.deepcopy(values))
        row["status"] = status
        row["version"] += 1
        return copy.deepcopy(row)


class _Events:
    def __init__(self, repository):
        self.repository = repository

    def append_with_outbox(self, event, outbox):
        self.repository.events.append(
            (copy.deepcopy(event), copy.deepcopy(tuple(outbox)))
        )


class _Uow:
    def __init__(self, repository):
        self.steps = _Steps(repository)
        self.runs = _Runs(repository)
        self.work_items = _WorkItems(repository)
        self.events = _Events(repository)
        self.repository = repository

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def commit(self):
        self.repository.commits += 1


class _Repository:
    def __init__(self):
        self.run = {
            "run_id": "run-id",
            "work_item_id": "work-id",
            "command_id": "command-id",
            "correlation_id": "correlation-id",
            "causation_id": None,
            "status": "RUNNING",
            "version": 3,
            "worker_id": "worker-1",
            "lease_expires_at": datetime(2026, 8, 13, 12, 0, 0),
            "cancel_requested_at": None,
            "cancel_reason": None,
            "execution_attempt_count": 1,
        }
        self.work_item = {
            "work_item_id": "work-id",
            "status": "IN_PROGRESS",
            "version": 2,
        }
        self.step = None
        self.events = []
        self.commits = 0

    def unit_of_work(self):
        return _Uow(self)

    def get_run(self, run_id):
        return copy.deepcopy(self.run) if self.run["run_id"] == run_id else None


class _PilotProjection:
    def record_incomplete_attempt(self, **_kwargs):
        return None


def _capability():
    return {
        "name": "cancel_tool",
        "version": "1.0.0",
        "operation_type": "read",
        "retry": {"safe": True},
        "idempotency": {"mode": "key"},
        "evidence": [],
        "postconditions": [],
    }


def _step(operation_type: OperationType = OperationType.READ) -> PlanStep:
    return PlanStep(
        step_key="cancel",
        tool_name="cancel_tool",
        tool_version="1.0.0",
        operation_type=operation_type,
        arguments={},
        account_id=None,
        depends_on=(),
        idempotency_key="cancel-step",
        expected_evidence=(),
        postconditions=(),
        risk_level=RiskLevel.LOW,
    )


def _plan(step: PlanStep) -> Plan:
    return Plan(
        command_type="tool.execute",
        context_fingerprint="context",
        tool_catalog_hash="catalog",
        steps=(step,),
    )


def _command() -> Command:
    return Command(
        command_type="tool.execute",
        source="console",
        actor=Actor(ActorType.CONSOLE_ADMIN, "17", ("admin",)),
        parameters={},
        idempotency_key="command-key",
        command_id="command-id",
        correlation_id="correlation-id",
    )


def _runner(repository, process_result):
    capability = _capability()
    catalog = _Catalog(capability)
    adapter = RegisteredToolExecutionAdapter(
        catalog=catalog,
        executor=_ProcessExecutor(process_result),
    )
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository
    runner._catalog = catalog
    runner._execution_port = adapter
    runner._verifier = ResultVerifier()
    runner._pilot_projection = _PilotProjection()
    runner._worker_id = "worker-1"
    runner._lease_seconds = 120
    runner._active = {}
    return runner


async def _execute_and_release_failure(runner, repository):
    step = _step()
    with pytest.raises(OrchestrationError) as raised:
        await runner._execute_plan(
            copy.deepcopy(repository.run),
            _plan(step),
            _command(),
        )
    await asyncio.to_thread(
        runner._fail_claimed,
        repository.run["run_id"],
        raised.value,
    )
    return raised.value


@pytest.mark.parametrize("flag", ["canceled", "cancelled"])
def test_cancelled_process_result_ends_step_work_item_and_run_as_cancelled(flag):
    repository = _Repository()
    runner = _runner(
        repository,
        {
            "success": False,
            flag: True,
            "error": "Tool execution cancelled",
        },
    )

    error = asyncio.run(_execute_and_release_failure(runner, repository))

    assert error.code == "CANCELLED"
    assert repository.step["status"] == "CANCELLED"
    assert repository.run["status"] == "CANCELLED"
    assert repository.run["error_code"] == "CANCELLED"
    assert repository.work_item["status"] == "CANCELLED"


def test_cancel_request_wins_race_before_terminal_failure_release():
    repository = _Repository()
    runner = _runner(
        repository,
        {
            "success": False,
            "error": "permanent tool failure",
            "error_code": "PERMANENT_TOOL_FAILURE",
        },
    )
    step = _step()

    async def execute():
        return await runner._execute_plan(
            copy.deepcopy(repository.run),
            _plan(step),
            _command(),
        )

    with pytest.raises(OrchestrationError) as raised:
        asyncio.run(execute())
    assert raised.value.details["status"] == "FAILED_TERMINAL"
    repository.run["cancel_requested_at"] = datetime(2026, 8, 13, 11, 59, 0)
    repository.run["cancel_reason"] = "operator requested cancellation"

    runner._fail_claimed(repository.run["run_id"], raised.value)

    assert repository.run["status"] == "CANCELLED"
    assert repository.run["error_code"] == "CANCELLED_BY_ACTOR"
    assert repository.run["error_summary"] == "operator requested cancellation"
    assert repository.work_item["status"] == "CANCELLED"


def test_cancel_request_is_checked_before_failed_step_commit():
    repository = _Repository()
    runner = _runner(
        repository,
        {
            "success": False,
            "error": "permanent tool failure",
            "error_code": "PERMANENT_TOOL_FAILURE",
        },
    )
    repository.run["cancel_requested_at"] = datetime(2026, 8, 13, 11, 59, 0)
    repository.run["cancel_reason"] = "cancel won the result race"

    error = asyncio.run(_execute_and_release_failure(runner, repository))

    assert error.code == "CANCELLED_BY_ACTOR"
    assert repository.step["status"] == "CANCELLED"
    assert repository.step["error_code"] == "CANCELLED_BY_ACTOR"
    assert repository.run["status"] == "CANCELLED"
    assert repository.work_item["status"] == "CANCELLED"


def test_forged_execution_context_cannot_override_command_identity_during_recovery_and_execute():
    repository = _Repository()
    repository.step = {
        "step_id": "step-id",
        "run_id": "run-id",
        "status": "RUNNING",
        "version": 1,
    }
    runner = _runner(repository, {})
    runner._catalog.capability = {
        **_capability(),
        "operation_type": "external_write",
        "retry": {"safe": True},
        "idempotency": {"mode": "key"},
    }
    execution = _CapturingExecutionPort()
    runner._execution_port = execution
    command = Command(
        command_type="tool.execute",
        source="console",
        actor=Actor(ActorType.CONSOLE_ADMIN, "17", ("admin",)),
        parameters={
            "execution_context": {
                "source": "scheduler",
                "actor": {
                    "actor_type": "system",
                    "actor_id": "forged-system",
                    "roles": ["system"],
                },
                "request_marker": "preserved",
            }
        },
        idempotency_key="command-key",
        command_id="command-id",
        correlation_id="correlation-id",
    )

    with pytest.raises(OrchestrationError, match="stop after capturing execution context"):
        asyncio.run(
            runner._execute_plan(
                copy.deepcopy(repository.run),
                _plan(_step(OperationType.EXTERNAL_WRITE)),
                command,
            )
        )

    expected_identity = {
        "source": command.source,
        "actor": command.actor.to_dict(),
        "request_marker": "preserved",
    }
    assert execution.reconcile_contexts == [expected_identity]
    assert execution.execute_contexts == [expected_identity]
