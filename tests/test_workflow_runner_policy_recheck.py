from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

from agent.orchestration.models import (
    ActorType,
    OperationType,
    Plan,
    PlanStep,
    RiskLevel,
    RunStatus,
)
from agent.orchestration.policy_engine import PolicyDecision
from agent.orchestration.workflow_runner import WorkflowRunner


class _Commands:
    def __init__(self, repository):
        self.repository = repository

    def get(self, command_id):
        if self.repository.command["command_id"] != command_id:
            return None
        return copy.deepcopy(self.repository.command)


class _Runs:
    def __init__(self, repository):
        self.repository = repository

    def get(self, run_id, *, for_update=False):
        row = self.repository.run
        if row["run_id"] != run_id:
            return None
        self.repository.trace.append(("run_get", bool(for_update), row["status"]))
        return copy.deepcopy(row)

    def transition(
        self,
        run_id,
        *,
        expected_version,
        expected_statuses,
        status,
        plan=None,
        plan_hash=None,
        plan_schema_version=None,
        tool_catalog_sha256=None,
        context_fingerprint_sha256=None,
        **values,
    ):
        row = self.repository.run
        if (
            row["run_id"] != run_id
            or row["version"] != expected_version
            or row["status"] not in expected_statuses
        ):
            raise RuntimeError("run CAS conflict")
        previous = row["status"]
        if plan is not None:
            row["plan_json"] = copy.deepcopy(plan)
        if plan_hash is not None:
            row["plan_hash"] = plan_hash
        if plan_schema_version is not None:
            row["plan_schema_version"] = plan_schema_version
        if tool_catalog_sha256 is not None:
            row["tool_catalog_sha256"] = tool_catalog_sha256
        if context_fingerprint_sha256 is not None:
            row["context_fingerprint_sha256"] = context_fingerprint_sha256
        if values.pop("increment_execution_attempt", False):
            row["execution_attempt_count"] += 1
        row.update(copy.deepcopy(values))
        row["status"] = status
        row["version"] += 1
        self.repository.trace.append(("run_transition", previous, status))
        return copy.deepcopy(row)

    def release_or_schedule(self, run_id, *, worker_id, status, **values):
        row = self.repository.run
        if row["run_id"] != run_id or row["worker_id"] != worker_id:
            raise RuntimeError("run lease conflict")
        row.update(copy.deepcopy(values))
        row["status"] = status
        row["worker_id"] = None
        row["lease_expires_at"] = None
        row["version"] += 1
        self.repository.trace.append(("run_release", status))
        return copy.deepcopy(row)


class _Steps:
    def __init__(self, repository):
        self.repository = repository

    def list_for_run(self, run_id):
        self.repository.trace.append(("steps_list", run_id))
        return [
            copy.deepcopy(row)
            for row in self.repository.steps
            if row["run_id"] == run_id
        ]

    def create_or_get(self, row):
        for persisted in self.repository.steps:
            if (
                persisted["run_id"] == row["run_id"]
                and persisted["step_key"] == row["step_key"]
            ):
                return copy.deepcopy(persisted)
        persisted = {
            **copy.deepcopy(dict(row)),
            "status": "PENDING",
            "version": 1,
            "attempt_count": 0,
        }
        self.repository.steps.append(persisted)
        self.repository.trace.append(("step_create", persisted["step_key"]))
        return copy.deepcopy(persisted)

    def transition(
        self,
        step_id,
        *,
        expected_version,
        expected_statuses,
        status,
        **values,
    ):
        row = next(item for item in self.repository.steps if item["step_id"] == step_id)
        if row["version"] != expected_version or row["status"] not in expected_statuses:
            raise RuntimeError("step CAS conflict")
        previous = row["status"]
        if values.pop("increment_attempt", False):
            row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
        row.update(copy.deepcopy(values))
        row["status"] = status
        row["version"] += 1
        self.repository.trace.append(("step_transition", previous, status))
        return copy.deepcopy(row)


class _WorkItems:
    def __init__(self, repository):
        self.repository = repository

    def get(self, work_item_id, *, for_update=False):
        del for_update
        if self.repository.work_item["work_item_id"] != work_item_id:
            return None
        return copy.deepcopy(self.repository.work_item)

    def transition(
        self,
        work_item_id,
        *,
        expected_version,
        expected_statuses,
        status,
        **values,
    ):
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
        self.repository.trace.append(("work_item_transition", status))
        return copy.deepcopy(row)


class _Approvals:
    def __init__(self, repository):
        self.repository = repository

    def prepare_approved_execution(self, run_id, *, expected_plan_hash):
        approval = self.repository.approval
        run = self.repository.run
        if run["run_id"] != run_id or run["status"] != "WAITING_APPROVAL":
            return {"outcome": "PLAN_STALE", "run": copy.deepcopy(run), "approval": None}
        if approval is None:
            return {"outcome": "MISSING", "run": copy.deepcopy(run), "approval": None}
        if approval["plan_hash"] != expected_plan_hash:
            return {
                "outcome": "PLAN_STALE",
                "run": copy.deepcopy(run),
                "approval": copy.deepcopy(approval),
            }
        if approval["status"] != "APPROVED":
            return {
                "outcome": approval["status"],
                "run": copy.deepcopy(run),
                "approval": copy.deepcopy(approval),
            }
        return {
            "outcome": "APPROVED",
            "run": copy.deepcopy(run),
            "approval": copy.deepcopy(approval),
        }


class _Events:
    def __init__(self, repository):
        self.repository = repository

    def append_with_outbox(self, event, outbox):
        self.repository.events.append(
            (copy.deepcopy(event), copy.deepcopy(tuple(outbox)))
        )
        self.repository.trace.append(("event", event["event_type"]))


class _Uow:
    def __init__(self, repository):
        self.repository = repository
        self.commands = _Commands(repository)
        self.runs = _Runs(repository)
        self.steps = _Steps(repository)
        self.work_items = _WorkItems(repository)
        self.approvals = _Approvals(repository)
        self.events = _Events(repository)

    def __enter__(self):
        self.repository.trace.append(("uow_enter",))
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        self.repository.trace.append(("uow_exit",))
        return False

    def commit(self):
        self.repository.trace.append(("commit",))


class _Repository:
    def __init__(self, plan: Plan, *, step: dict | None = None):
        now = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
        self.run = {
            "run_id": "run-id",
            "work_item_id": "work-id",
            "command_id": "command-id",
            "status": "RUNNING",
            "version": 4,
            "worker_id": "worker-1",
            "lease_expires_at": now.replace(tzinfo=None),
            "execution_attempt_count": 1,
            "plan_json": plan.to_dict(),
            "plan_hash": plan.plan_hash,
            "plan_schema_version": plan.schema_version,
            "tool_catalog_sha256": plan.tool_catalog_hash,
            "context_fingerprint_sha256": plan.context_fingerprint,
            "correlation_id": "correlation-id",
            "causation_id": None,
            "cancel_requested_at": None,
            "cancel_reason": None,
        }
        self.command = {
            "command_id": "command-id",
            "command_type": "tool.execute",
            "source": "scheduler",
            "actor_type": ActorType.SCHEDULER.value,
            "actor_id": "scheduler:task-1",
            "actor_roles_json": ["automation"],
            "parameters_json": {
                "execution_context": {
                    "scheduled_task_id": "task-1",
                    "configuration_version": 1,
                }
            },
            "entity_refs_json": [],
            "idempotency_key": "command-key",
            "correlation_id": "correlation-id",
            "requested_at": now,
        }
        self.work_item = {
            "work_item_id": "work-id",
            "status": "IN_PROGRESS",
            "version": 2,
        }
        self.steps = [copy.deepcopy(step)] if step is not None else []
        self.approval = None
        self.events = []
        self.trace = []

    def unit_of_work(self):
        return _Uow(self)

    def get_run(self, run_id):
        if self.run["run_id"] != run_id:
            return None
        return copy.deepcopy(self.run)

    def claim_current(self):
        if self.run["worker_id"] is not None:
            raise RuntimeError("run is already leased")
        self.run["worker_id"] = "worker-1"
        self.run["version"] += 1
        return copy.deepcopy(self.run)


class _ContextBuilder:
    def build(self, _command):
        return SimpleNamespace(fingerprint="context")


class _Planner:
    def __init__(self, plan):
        self._persisted_plan = plan

    def plan(self, *_args, **_kwargs):
        return self._persisted_plan


class _Validator:
    def validate(self, *_args, **_kwargs):
        return None


class _Policy:
    def __init__(self):
        self.requires_approval = True

    def evaluate(self, *_args, **_kwargs):
        return PolicyDecision(
            allowed=True,
            requires_approval=self.requires_approval,
            required_role="super_admin" if self.requires_approval else None,
            risk_level=RiskLevel.HIGH,
            code="APPROVAL_REQUIRED" if self.requires_approval else "ALLOWED",
            reason="fresh scheduled-task policy requires approval",
        )


class _ApprovalService:
    def __init__(self, repository):
        self.repository = repository
        self.requests = 0

    def request(self, *, run, plan, policy_decision, requested_by):
        del policy_decision, requested_by
        self.requests += 1
        if (
            self.repository.approval is not None
            and self.repository.approval["plan_hash"] == plan.plan_hash
        ):
            return copy.deepcopy(self.repository.approval)
        self.repository.approval = {
            "approval_id": "approval-id",
            "approval_round": 1,
            "run_id": run["run_id"],
            "plan_hash": plan.plan_hash,
            "status": "PENDING",
        }
        return copy.deepcopy(self.repository.approval)

    def invalidate_for_stale_plan(self, _run_id):
        if self.repository.approval is not None:
            self.repository.approval["status"] = "INVALIDATED"


class _Catalog:
    def __init__(self, step: PlanStep):
        self.step = step

    def get_capability(self, tool_name):
        if tool_name != self.step.tool_name:
            return None
        replay_safe = self.step.operation_type is OperationType.INTERNAL_PROJECTION_WRITE
        return {
            "name": tool_name,
            "version": "1.0.0",
            "operation_type": self.step.operation_type.value,
            "retry": {"safe": replay_safe},
            "idempotency": {"mode": "key" if replay_safe else "none"},
        }


class _Execution:
    def __init__(self, *, reconciliation=None):
        self.execute_calls = 0
        self.reconcile_calls = 0
        self.reconciliation = reconciliation or {"resolution": "UNSUPPORTED"}

    async def execute_step(self, *_args, **_kwargs):
        self.execute_calls += 1
        return {"status": "SUCCESS", "data": {}, "meta": {}}

    async def reconcile_step(self, *_args, **_kwargs):
        self.reconcile_calls += 1
        return copy.deepcopy(self.reconciliation)


class _Verifier:
    def verify(self, *_args, **_kwargs):
        return SimpleNamespace(accepted=True, result=None)


class _PilotProjection:
    def record_incomplete_attempt(self, **_kwargs):
        return None


def _plan(
    operation_type: OperationType = OperationType.EXTERNAL_WRITE,
) -> Plan:
    tool_name = (
        "projection_tool"
        if operation_type is OperationType.INTERNAL_PROJECTION_WRITE
        else "external_write_tool"
    )
    return Plan(
        command_type="tool.execute",
        context_fingerprint="context",
        tool_catalog_hash="catalog",
        steps=(
            PlanStep(
                step_key="write",
                tool_name=tool_name,
                tool_version="1.0.0",
                operation_type=operation_type,
                arguments={"record_id": "record-1"},
                account_id="account-1",
                depends_on=(),
                idempotency_key="step-key",
                expected_evidence=(),
                postconditions=(),
                risk_level=RiskLevel.HIGH,
            ),
        ),
    )


def _runner(
    repository,
    plan,
    execution,
    *,
    policy=None,
    protected_step_start_guard=None,
):
    effective_policy = policy or _Policy()
    runner = WorkflowRunner(
        repository=repository,
        catalog=_Catalog(plan.steps[0]),
        execution_port=execution,
        context_builder=_ContextBuilder(),
        planner=_Planner(plan),
        validator=_Validator(),
        policy=effective_policy,
        approval_service=_ApprovalService(repository),
        verifier=_Verifier(),
        worker_id="worker-1",
        pilot_projection=_PilotProjection(),
        protected_step_start_guard=protected_step_start_guard,
    )
    return runner


def test_recovered_running_run_rechecks_revoked_exact_policy_before_executor():
    plan = _plan()
    repository = _Repository(plan)
    execution = _Execution()
    runner = _runner(repository, plan, execution)

    asyncio.run(runner._process_claimed(copy.deepcopy(repository.run)))

    assert execution.execute_calls == 0
    assert repository.run["status"] == RunStatus.WAITING_APPROVAL.value
    assert repository.work_item["status"] == "WAITING_APPROVAL"
    assert repository.approval is not None
    assert repository.approval["status"] == "PENDING"
    assert all(
        step["requires_approval"] is True
        for step in repository.run["plan_json"]["steps"]
    )
    transition_index = repository.trace.index(
        ("run_transition", "RUNNING", "WAITING_APPROVAL")
    )
    assert repository.trace[transition_index - 2] == ("run_get", True, "RUNNING")
    assert repository.trace[transition_index - 1] == ("steps_list", "run-id")
    assert ("commit",) in repository.trace[transition_index:]

    repository.approval["status"] = "APPROVED"
    claimed = repository.claim_current()
    asyncio.run(runner._process_claimed(claimed))

    assert execution.execute_calls == 1
    assert repository.run["status"] == RunStatus.COMPLETED.value
    assert repository.work_item["status"] == "RESOLVED"
    step_start_index = repository.trace.index(
        ("step_transition", "PENDING", "RUNNING")
    )
    assert repository.trace[step_start_index - 1] == ("run_get", True, "RUNNING")


def test_recovered_inflight_external_write_reconciles_unknown_without_replay():
    plan = _plan()
    for interrupted_status in ("RUNNING", "VERIFYING"):
        repository = _Repository(
            plan,
            step={
                "step_id": "step-id",
                "run_id": "run-id",
                "step_key": "write",
                "step_order": 1,
                "tool_name": "external_write_tool",
                "tool_version": "1.0.0",
                "operation_type": "EXTERNAL_WRITE",
                "risk_level": "HIGH",
                "status": interrupted_status,
                "version": 2,
                "attempt_count": 1,
            },
        )
        execution = _Execution(reconciliation={"resolution": "UNKNOWN"})
        runner = _runner(repository, plan, execution)

        asyncio.run(runner._process_claimed(copy.deepcopy(repository.run)))

        assert execution.execute_calls == 0
        assert execution.reconcile_calls == 1
        assert repository.steps[0]["status"] == RunStatus.BLOCKED_DATA.value
        assert repository.run["status"] == RunStatus.BLOCKED_DATA.value
        assert repository.work_item["status"] == "BLOCKED_DATA"
        assert runner._approval_service.requests == 0


def test_recovered_replay_safe_projection_waits_for_approval_before_retry():
    plan = _plan(OperationType.INTERNAL_PROJECTION_WRITE)
    repository = _Repository(
        plan,
        step={
            "step_id": "step-id",
            "run_id": "run-id",
            "step_key": "write",
            "step_order": 1,
            "tool_name": "projection_tool",
            "tool_version": "1.0.0",
            "operation_type": "INTERNAL_PROJECTION_WRITE",
            "risk_level": "HIGH",
            "status": "RUNNING",
            "version": 2,
            "attempt_count": 1,
        },
    )
    execution = _Execution()
    runner = _runner(repository, plan, execution)

    asyncio.run(runner._process_claimed(copy.deepcopy(repository.run)))

    assert execution.execute_calls == 0
    assert execution.reconcile_calls == 0
    assert repository.steps[0]["status"] == RunStatus.FAILED_RETRYABLE.value
    assert repository.run["status"] == RunStatus.WAITING_APPROVAL.value
    assert repository.approval is not None
    assert repository.approval["status"] == "PENDING"

    repository.approval["status"] = "APPROVED"
    claimed = repository.claim_current()
    asyncio.run(runner._process_claimed(claimed))

    assert execution.execute_calls == 1
    assert repository.run["status"] == RunStatus.COMPLETED.value


def test_normal_running_exact_run_rechecks_policy_under_account_start_guard():
    plan = _plan()
    repository = _Repository(plan)
    repository.run["status"] = RunStatus.VALIDATED.value
    execution = _Execution()
    policy = _Policy()
    policy.requires_approval = False

    def begin_guard(step):
        assert step.account_id == "account-1"
        repository.trace.append(("account_guard_acquire",))
        policy.requires_approval = True

        def finish():
            repository.trace.append(("account_guard_release",))

        return finish

    runner = _runner(
        repository,
        plan,
        execution,
        policy=policy,
        protected_step_start_guard=begin_guard,
    )

    asyncio.run(runner._process_claimed(copy.deepcopy(repository.run)))

    assert execution.execute_calls == 0
    assert repository.run["status"] == RunStatus.WAITING_APPROVAL.value
    assert repository.approval is not None
    assert repository.approval["status"] == "PENDING"
    assert not any(
        item == ("step_transition", "PENDING", "RUNNING")
        for item in repository.trace
    )
    acquire_index = repository.trace.index(("account_guard_acquire",))
    policy_transition_index = repository.trace.index(
        ("run_transition", "RUNNING", "WAITING_APPROVAL")
    )
    release_index = repository.trace.index(("account_guard_release",))
    assert acquire_index < policy_transition_index < release_index


def test_account_start_guard_is_held_until_step_running_commit():
    plan = _plan(OperationType.INTERNAL_PROJECTION_WRITE)
    repository = _Repository(plan)
    repository.run["status"] = RunStatus.VALIDATED.value
    execution = _Execution()
    policy = _Policy()
    policy.requires_approval = False

    def begin_guard(step):
        assert step.operation_type is OperationType.INTERNAL_PROJECTION_WRITE
        repository.trace.append(("account_guard_acquire",))

        def finish():
            repository.trace.append(("account_guard_release",))

        return finish

    runner = _runner(
        repository,
        plan,
        execution,
        policy=policy,
        protected_step_start_guard=begin_guard,
    )

    asyncio.run(runner._process_claimed(copy.deepcopy(repository.run)))

    assert execution.execute_calls == 1
    assert repository.run["status"] == RunStatus.COMPLETED.value
    acquire_index = repository.trace.index(("account_guard_acquire",))
    step_start_index = repository.trace.index(
        ("step_transition", "PENDING", "RUNNING")
    )
    step_start_commit_index = repository.trace.index(
        ("commit",),
        step_start_index,
    )
    release_index = repository.trace.index(("account_guard_release",))
    assert acquire_index < step_start_index < step_start_commit_index < release_index
