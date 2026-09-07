"""Real claim/recovery/acceptance regressions using isolated local read work."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.automation_run_supersession import supersede_safely_suspended_runs
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import OrchestrationError, RunStatus
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.policy_engine import PolicyEngine
from agent.orchestration.result_verifier import ResultVerifier
from agent.orchestration.workflow_runner import WorkflowRunner
from tests.test_module_data_sources_mysql import database  # noqa: F401
from tests.test_workflow_runner_durable_admission import _Catalog, _Workload, _command, _until


class _RecoveryCatalog(_Catalog):
    def __init__(self, operation):
        self.operation = operation
        self.approval_mode = "none"

    def get_capability(self, tool_name):
        capability = super().get_capability(tool_name)
        capability["operation_type"] = self.operation
        capability["approval"] = {"mode": self.approval_mode, "required_role": "admin"}
        return capability


class _Accounts:
    error_code = None

    def __call__(self, command):
        if self.error_code:
            raise OrchestrationError(self.error_code, "Synthetic context source became unavailable")
        return [{"account_id": command.parameters["account_id"], "is_active": True}]


def _recovery_runner(repository, catalog, accounts, workload):
    policy = PolicyEngine(catalog)
    return WorkflowRunner(
        repository=repository, catalog=catalog,
        execution_port=RegisteredToolExecutionAdapter(
            catalog=catalog, executor=None, direct_runners={"v32_local_workload": workload.execute},
        ),
        context_builder=ContextBuilder(account_resolver=accounts),
        planner=DeterministicPlanner(catalog), validator=PlanValidator(catalog), policy=policy,
        approval_service=ApprovalService(repository, policy), verifier=ResultVerifier(),
        worker_id="read-recovery", worker_concurrency=1, browser_concurrency=1,
        browser_tool_names=("v32_local_workload",), poll_interval_seconds=0.05,
    )


def _interrupted_run(database, repository, runner, *, step_status, live=False):
    fixture, name = database
    command = _command("interrupted-read", account=str(uuid4()))
    receipt = CommandGateway(repository).submit(command)
    context = runner._context_builder.build(command)
    plan = runner._planner.plan(command, context)
    run = repository.get_run(receipt.run_id)
    run = runner._transition(run, RunStatus.CONTEXT_READY)
    run = runner._transition(
        run, RunStatus.PLANNED, plan=plan.to_dict(), plan_hash=plan.plan_hash,
        plan_schema_version=plan.schema_version, tool_catalog_sha256=plan.tool_catalog_hash,
        context_fingerprint_sha256=plan.context_fingerprint,
    )
    run = runner._transition(run, RunStatus.VALIDATED)
    runner._transition(run, RunStatus.RUNNING)
    step = plan.steps[0]
    with repository.unit_of_work() as uow:
        uow.steps.create_or_get({
            "step_id": str(uuid4()), "run_id": receipt.run_id,
            "step_key": step.step_key, "step_order": 1,
            "tool_name": step.tool_name, "tool_version": step.tool_version,
            "operation_type": step.operation_type, "risk_level": step.risk_level,
            "status": step_status, "retry_safe": True,
            "idempotency_key": step.idempotency_key, "account_id": step.account_id,
            "attempt_count": 1, "started_at": datetime.utcnow() - timedelta(minutes=2),
        })
        uow.commit()
    automation_id = "synthetic_read_recovery_" + uuid4().hex
    # Model persisted crash state. The local read uses its genuine untyped
    # Command contract; attach project lookup identity only after recovery.
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        if live:
            cursor.execute("UPDATE agent_commands SET automation_id=%s WHERE command_id=%s",
                           (automation_id, receipt.command_id))
        cursor.execute("UPDATE agent_runs SET worker_id=%s,lease_expires_at=%s WHERE run_id=%s",
                       ("prior-worker", datetime.utcnow() + timedelta(minutes=5 if live else -1), receipt.run_id))
        connection.commit()
    return receipt, automation_id


def _submit_successor(repository, automation_id):
    return CommandGateway(repository).submit(
        _command("successor", account=str(uuid4())),
        uow_acceptance_guard=lambda uow, successor: supersede_safely_suspended_runs(
            uow, automation_id=automation_id, successor=successor,
            source="console", request_id=str(uuid4()),
        ),
    )


@pytest.mark.parametrize("operation", ["read", "compute"])
@pytest.mark.parametrize("step_status", ["RUNNING", "VERIFYING"])
@pytest.mark.parametrize("pause", ["approval", "SESSION_EXPIRED", "SOURCE_INCOMPLETE"])
def test_claim_closes_interrupted_read_before_recovery_pause(database, monkeypatch, operation, step_status, pause):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    repository = fixture._repository(name)

    async def exercise():
        catalog, accounts, workload = _RecoveryCatalog(operation), _Accounts(), _Workload()
        runner = _recovery_runner(repository, catalog, accounts, workload)
        receipt, automation_id = _interrupted_run(database, repository, runner, step_status=step_status)
        if pause == "approval":
            catalog.approval_mode = "required"
            expected_status = "WAITING_APPROVAL"
        else:
            accounts.error_code = pause
            expected_status = "BLOCKED_LOGIN" if pause == "SESSION_EXPIRED" else "BLOCKED_DATA"
        await runner.start()
        try:
            recovered = await _until(
                lambda: repository.get_run(receipt.run_id),
                lambda row: row["status"] == expected_status and row["worker_id"] is None,
            )
        finally:
            await runner.stop()
        assert workload.calls == {}
        assert recovered["lease_expires_at"] is None
        step = recovered["steps"][0]
        assert step["status"] == "FAILED_RETRYABLE"
        assert step["error_code"] == "INTERRUPTED_READ_RETRY"
        assert step["finished_at"] is not None
        assert step["attempt_count"] == 1
        with repository.unit_of_work() as uow:
            assert not uow.runs.get_automation_supersession_facts(receipt.run_id)["has_inflight_step"]
        with fixture._connection(name) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_commands SET automation_id=%s WHERE command_id=%s",
                           (automation_id, receipt.command_id))
            connection.commit()
        successor = _submit_successor(repository, automation_id)
        assert successor.run_id != receipt.run_id
        old = repository.get_run(receipt.run_id)
        assert old["status"] == "CANCELLED"
        assert old["error_code"] == "SUPERSEDED_BY_NEW_INVOCATION"
        if pause == "approval":
            with repository.unit_of_work() as uow:
                assert uow.approvals.get_latest_for_run(receipt.run_id)["status"] == "INVALIDATED"
        # Later parameterized runs must not claim this independent new command.
        await asyncio.to_thread(repository.request_run_cancel, successor.run_id,
                                requested_by_type="console_admin", requested_by_id="isolated-admin",
                                reason="synthetic test complete")

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["read", "compute"])
def test_valid_prior_claim_keeps_read_step_and_project_mutex(database, monkeypatch, operation):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    repository = fixture._repository(name)

    async def exercise():
        workload = _Workload()
        runner = _recovery_runner(repository, _RecoveryCatalog(operation), _Accounts(), workload)
        receipt, automation_id = _interrupted_run(database, repository, runner, step_status="RUNNING", live=True)
        before = repository.get_run(receipt.run_id)
        await runner.start()
        try:
            with pytest.raises(OrchestrationError) as blocked:
                await asyncio.to_thread(_submit_successor, repository, automation_id)
            assert blocked.value.code == "AUTOMATION_ALREADY_RUNNING"
            assert blocked.value.details["blocking_kind"] == "ACTIVE"
            # An actual independent workload proves the same pool polled and
            # executed while it left the other worker's still-valid claim alone.
            probe = CommandGateway(repository, wake_runner=runner.wake).submit(_command("probe", account=str(uuid4())))
            await _until(lambda: repository.get_run(probe.run_id), lambda row: row["status"] == "COMPLETED")
            after = repository.get_run(receipt.run_id)
            assert after["worker_id"] == "prior-worker"
            assert after["steps"] == before["steps"]
            assert workload.calls == {"probe": 1}
        finally:
            await runner.stop()

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["read", "compute"])
def test_unowned_interrupted_read_resumes_once_through_runner(database, monkeypatch, operation):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    repository = fixture._repository(name)

    async def exercise():
        workload = _Workload()
        runner = _recovery_runner(repository, _RecoveryCatalog(operation), _Accounts(), workload)
        receipt, _ = _interrupted_run(database, repository, runner, step_status="RUNNING")
        with fixture._connection(name) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_runs SET worker_id=NULL,lease_expires_at=NULL WHERE run_id=%s",
                           (receipt.run_id,))
            connection.commit()
        await runner.start()
        try:
            completed = await _until(lambda: repository.get_run(receipt.run_id),
                                     lambda row: row["status"] == "COMPLETED" and row["worker_id"] is None)
            assert workload.calls == {"interrupted-read": 1}
            assert completed["steps"][0]["status"] == "COMPLETED"
            assert completed["steps"][0]["attempt_count"] == 2
        finally:
            await runner.stop()

    asyncio.run(exercise())


def test_large_unknown_history_preserves_audit_and_cannot_hide_project_blocker(database):
    from tests.test_legacy_unknown_scope_migration_mysql import _Database

    fixture, name = database
    helper = type("SupersessionDatabase", (fixture,), {"database": name})
    history = _Database(helper)
    repository = history.repository
    # Terminal unknown rows may retain the old in-flight marker. Their
    # classification is historical; those markers must not outrank a current
    # queued Run in the bounded acceptance query.
    historical_ids = {
        history.seed(step_status="RUNNING")["run_id"] for _ in range(101)
    }
    before = history.snapshot()
    for status, live, kind in (
        ("RECEIVED", False, "ACTIVE"),
        ("FAILED_RETRYABLE", False, "RETRY_PENDING"),
        ("RUNNING", True, "ACTIVE"),
        ("COMPLETED", True, "ACTIVE"),
        ("PARTIAL", True, "ACTIVE"),
        ("FAILED_TERMINAL", True, "ACTIVE"),
        ("CANCELLED", True, "ACTIVE"),
    ):
        queued = CommandGateway(repository).submit(_command("current-blocker"))
        with helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_commands SET automation_id=%s WHERE command_id=%s",
                           (history.project_id, queued.command_id))
            cursor.execute("UPDATE agent_runs SET status=%s,worker_id=%s,lease_expires_at=%s WHERE run_id=%s",
                           (status, "current-worker" if live else None,
                            datetime.utcnow() + timedelta(minutes=5) if live else None, queued.run_id))
            connection.commit()
        with repository.unit_of_work() as uow:
            discovered = uow.runs.list_unfinished_for_automation(history.project_id)
        assert len(discovered) == 100
        assert discovered[0] == queued.run_id
        with pytest.raises(OrchestrationError) as blocked:
            _submit_successor(repository, history.project_id)
        assert blocked.value.code == "AUTOMATION_ALREADY_RUNNING"
        assert blocked.value.details["blocking_kind"] == kind
        assert blocked.value.details["active_run_id"] == queued.run_id
        with helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_runs SET status='CANCELLED',worker_id=NULL,lease_expires_at=NULL WHERE run_id=%s",
                           (queued.run_id,))
            connection.commit()
    for status in ("COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"):
        leased = history.seed(run_status=status, receipt_outcome="WRITE_VERIFIED",
                              lease_outcome="RUNNING", live_generation_lease=True)
        with repository.unit_of_work() as uow:
            assert uow.runs.list_unfinished_for_automation(history.project_id)[0] == leased["run_id"]
        with pytest.raises(OrchestrationError) as blocked:
            _submit_successor(repository, history.project_id)
        assert blocked.value.details["blocking_kind"] == "ACTIVE"
        assert blocked.value.details["active_run_id"] == leased["run_id"]
        with helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE automation_project_generation_leases SET expires_at=%s WHERE lease_id=%s",
                           (datetime.utcnow() - timedelta(minutes=1), leased["lease_id"]))
            connection.commit()
    accepted = _submit_successor(repository, history.project_id)
    assert accepted.run_id not in historical_ids
    after = history.snapshot()
    for table, rows in before.items():
        identity = {
            "automation_write_attempt_receipts": "receipt_id",
            "automation_project_generation_leases": "lease_id",
            "agent_runs": "run_id", "agent_run_steps": "step_id", "agent_commands": "command_id",
        }[table]
        old_ids = {row[identity] for row in rows}
        assert [row for row in after[table] if row[identity] in old_ids] == rows
