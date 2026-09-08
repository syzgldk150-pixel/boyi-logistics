"""Real typed Commands and Runner in an independently owned MySQL schema.

The registered workload is a small local control-plane probe, not a substitute
for any daily business plugin. Only its account and project policy providers
are synthetic; persistence, planning, approval, execution and cancellation are
the production implementations. No Runner method is patched.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
import threading
from uuid import uuid4

import pytest

from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.automation_run_supersession import supersede_safely_suspended_runs
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.control_plane_service import ControlPlaneService
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import Actor, ActorType, Command, OrchestrationError
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.policy_engine import PolicyEngine, ProjectPolicyEvaluation
from agent.orchestration.result_verifier import ResultVerifier
from agent.orchestration.workflow_runner import WorkflowRunner
from shared.automation_project_authorization import AutomationEntrypoint, AutomationProjectInvocation
from shared.execution_resource_journal import unknown_execution_keys
from tests.test_legacy_unknown_scope_migration_mysql import _Database, _digest
from tests.test_workflow_runner_durable_admission import _Catalog, _until


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8",
)
TOOL = "automation.isolated_legacy_scope.run"
PEER_PROJECT = "isolated_fail_fast_peer"
PEER_TOOL = f"automation.{PEER_PROJECT}.run"
ACCOUNT = "isolated-fail-fast-account"
ADMIN = Actor(ActorType.CONSOLE_ADMIN, "isolated-fail-fast-admin", ("admin", "super_admin"),
              authenticated_by="mysql_admin_session")


@pytest.fixture(scope="module")
def database():
    import pymysql
    from tests import test_mysql_orchestration_integration as support

    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    helper = type("FailFastDatabase", (support.MySqlOrchestrationIntegrationTests,), {})
    helper.pymysql, helper.runner = pymysql, support._load_migration_runner()
    helper.host, helper.port = os.environ["AGENT_DB_HOST"], int(os.environ["AGENT_DB_PORT"])
    helper.user, helper.password = os.environ["AGENT_DB_USER"], os.environ["AGENT_DB_PASS"]
    helper.database = "automation_fail_fast_" + uuid4().hex + "_test"
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{helper.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    try:
        helper._run_migrations(helper.database)
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("AGENT_DB_NAME", helper.database)
            yield _Database(helper)
    finally:
        with helper._server_connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{helper.database}`")


class _ProbeCatalog(_Catalog):
    def __init__(self, *, writes=False):
        self.writes = writes

    def get_capability(self, tool_name):
        assert tool_name in {TOOL, PEER_TOOL}
        result = super().get_capability("v32_local_workload")
        result["name"] = tool_name
        if self.writes:
            result["operation_type"] = "internal_projection_write"
        return result


class _Probe:
    def __init__(self, database, *, failure=None, writes=False):
        self.database, self.failure, self.writes = database, failure, writes
        self.calls = Counter()
        self.entered, self.release = threading.Event(), threading.Event()

    def execute(self, arguments):
        job = arguments["job"]
        self.calls[job] += 1
        if job == "holder":
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("isolated holder was not released")
        if self.failure and job == "failure":
            code, retryable = self.failure
            return {"status": "FAILED", "data": {}, "warnings": [],
                    "meta": {"observed_at": datetime.now(timezone.utc).isoformat()},
                    "error": {"code": code, "message": "isolated failure", "retryable": retryable}}
        value = sum(range(len(job) + 1))
        if self.writes:
            with self.database.helper._connection() as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO isolated_scope_results(job,value) VALUES(%s,%s)", (job, value))
                connection.commit()
        return {"job": job, "rows": [{"value": value}]}


def _command(database, job, *, project=None):
    request = str(uuid4())
    invocation = AutomationProjectInvocation(
        automation_id=project or database.project_id, automation_generation=1,
        entrypoint=AutomationEntrypoint.CONSOLE, contract_id="console", contract_hash="a" * 64,
        policy_version=1, project_configuration_version=1, request_id=request,
    )
    return Command(command_type="automation.project.invoke", source="console", actor=ADMIN,
        parameters={"tool_name": f"automation.{invocation.automation_id}.run", "arguments": {"job": job}, "account_id": ACCOUNT,
                    "execution_context": {}},
        automation_invocation=invocation, idempotency_key=request)


def _runner(database, probe, *, context_failure=None):
    catalog = _ProbeCatalog(writes=probe.writes)

    def project_policy(plan, actor, source, context, invocation, transaction):
        assert plan.automation_id == invocation.automation_id
        assert invocation.automation_id in {database.project_id, PEER_PROJECT}
        assert plan.automation_generation == invocation.automation_generation == 1
        assert plan.automation_contract_hash == invocation.contract_hash == "a" * 64
        assert source == "console" and actor.actor_id == ADMIN.actor_id
        # This local probe delegates approval to the real tool ceiling. In
        # particular the SQL-writing probe still needs a real saved approval.
        return ProjectPolicyEvaluation(True, None, "ALLOWED", "isolated closed project contract")

    def accounts(command):
        if context_failure and command.parameters["arguments"]["job"] == "failure":
            code, status = context_failure
            raise OrchestrationError(code, "isolated context failure", details={"status": status})
        return [{"account_id": ACCOUNT, "is_active": True}]

    policy = PolicyEngine(catalog, project_policy_provider=project_policy)
    return WorkflowRunner(repository=database.repository, catalog=catalog,
        execution_port=RegisteredToolExecutionAdapter(catalog=catalog, executor=None,
            direct_runners={TOOL: probe.execute, PEER_TOOL: probe.execute}),
        context_builder=ContextBuilder(account_resolver=accounts),
        planner=DeterministicPlanner(catalog), validator=PlanValidator(catalog), policy=policy,
        approval_service=ApprovalService(database.repository, policy), verifier=ResultVerifier(),
        worker_id="fail-fast-" + uuid4().hex, worker_concurrency=2, browser_concurrency=2,
        browser_tool_names=(TOOL, PEER_TOOL), poll_interval_seconds=0.1)


async def _submit(database, runner, job, *, guarded=False, project=None):
    command = _command(database, job, project=project)

    def guard(uow, successor):
        supersede_safely_suspended_runs(uow, automation_id=command.automation_invocation.automation_id,
            successor=successor, source="console", request_id=command.idempotency_key)

    return await asyncio.to_thread(CommandGateway(database.repository, wake_runner=runner.wake).submit,
        command, **({"uow_acceptance_guard": guard} if guarded else {}))


async def _terminal(database, receipt):
    return await _until(lambda: database.repository.get_run(receipt.run_id),
        lambda row: row["status"] in {"COMPLETED", "FAILED_TERMINAL", "CANCELLED"}, timeout=8)


def _read_approval(database, receipt):
    with database.repository.unit_of_work() as uow:
        return uow.approvals.get_latest_for_run(receipt.run_id)


async def _approve(database, runner, receipt):
    approval = await _until(lambda: _read_approval(database, receipt),
                            lambda row: bool(row and row["status"] == "PENDING"))
    await asyncio.to_thread(runner._approval_service.decide, approval_id=approval["approval_id"],
        plan_hash=approval["plan_hash"], actor=ADMIN, source="console", decision="APPROVED")
    runner.wake(receipt.run_id)


def test_actual_resource_contention_ends_once_then_new_request_executes(database):
    async def exercise():
        probe = _Probe(database)
        runner = _runner(database, probe)
        await runner.start()
        try:
            holder = await _submit(database, runner, "holder", guarded=True)
            assert await asyncio.to_thread(probe.entered.wait, 5)
            busy = await _submit(database, runner, "busy", project=PEER_PROJECT, guarded=True)
            failed = await _terminal(database, busy)
            assert (failed["status"], failed["error_code"]) == ("FAILED_TERMINAL", "EXECUTION_RESOURCE_BUSY")
            assert failed["execution_attempt_count"] == 0 and failed["started_at"] is None
            assert failed["worker_id"] is None and failed["lease_expires_at"] is None
            assert failed["finished_at"] is not None and not failed["retryable"]
            probe.release.set()
            assert (await _terminal(database, holder))["status"] == "COMPLETED"
            fresh = await _submit(database, runner, "after-busy", project=PEER_PROJECT, guarded=True)
            assert (await _terminal(database, fresh))["status"] == "COMPLETED"
            runner.wake(busy.run_id)
            await asyncio.sleep(0.2)
            assert probe.calls == Counter({"holder": 1, "after-busy": 1})
            assert database.repository.get_run(busy.run_id) == failed
        finally:
            probe.release.set()
            await runner.stop()
    asyncio.run(exercise())


@pytest.mark.parametrize("code,retryable,blocked_status", [
    ("AUTH_REQUIRED", False, "BLOCKED_LOGIN"),
    ("MISSING_FIELD", False, "BLOCKED_DATA"),
    ("TEMPORARY_IO", True, "FAILED_RETRYABLE"),
])
@pytest.mark.parametrize("phase", ["context", "executor"])
def test_automation_failures_are_terminal_and_not_replayed(database, code, retryable, blocked_status, phase):
    async def exercise():
        probe = _Probe(database, failure=(code, retryable) if phase == "executor" else None)
        runner = _runner(database, probe,
                         context_failure=(code, blocked_status) if phase == "context" else None)
        await runner.start()
        try:
            receipt = await _submit(database, runner, "failure")
            final = await _terminal(database, receipt)
            assert (final["status"], final["error_code"]) == ("FAILED_TERMINAL", code)
            assert final["execution_attempt_count"] == int(phase == "executor")
            assert final["finished_at"] is not None and not final["retryable"]
            fresh = await _submit(database, runner, "after-failure", guarded=True)
            fresh_final = await _terminal(database, fresh)
            assert fresh_final["status"] == "COMPLETED", {
                key: fresh_final[key] for key in ("status", "error_code", "error_summary")
            }
            service = ControlPlaneService(database.repository, runner._approval_service, wake_runner=runner.wake)
            assert (await service.publish_session_restored(ACCOUNT))["resumed_count"] == 0
            runner.wake(receipt.run_id)
            await asyncio.sleep(0.2)
            assert probe.calls["failure"] == int(phase == "executor")
            assert probe.calls["after-failure"] == 1
            assert database.repository.get_run(receipt.run_id) == final
        finally:
            await runner.stop()
    asyncio.run(exercise())


@pytest.mark.parametrize("held", [False, True])
def test_startup_and_release_activation_close_old_request_but_run_new_request(database, held):
    async def exercise():
        probe = _Probe(database)
        runner = _runner(database, probe)
        if held:
            await runner.start(held_for_release=True)
        old = await _submit(database, runner, "before-activation")
        if held:
            assert database.repository.get_run(old.run_id)["status"] == "RECEIVED"
            assert not probe.calls
            runner.resume_after_release()
        else:
            await runner.start()
        try:
            final = await _terminal(database, old)
            assert (final["status"], final["error_code"]) == ("FAILED_TERMINAL", "AUTOMATION_REQUEST_INTERRUPTED")
            assert final["execution_attempt_count"] == 0 and final["started_at"] is None
            fresh = await _submit(database, runner, "after-activation", guarded=True)
            assert (await _terminal(database, fresh))["status"] == "COMPLETED"
            assert probe.calls == Counter({"after-activation": 1})
        finally:
            await runner.stop()
    asyncio.run(exercise())


def test_pending_approval_survives_restart_until_explicit_decision(database):
    async def exercise():
        probe = _Probe(database, writes=True)
        runner = _runner(database, probe)
        await runner.start()
        receipt = await _submit(database, runner, "approved-after-restart")
        await _until(lambda: _read_approval(database, receipt),
                     lambda row: bool(row and row["status"] == "PENDING"))
        await runner.stop()
        before = _read_approval(database, receipt)
        await runner.start()
        try:
            runner.wake(receipt.run_id)
            await asyncio.sleep(0.2)
            assert database.repository.get_run(receipt.run_id)["status"] == "WAITING_APPROVAL"
            assert _read_approval(database, receipt) == before
            assert not probe.calls
            await _approve(database, runner, receipt)
            assert (await _terminal(database, receipt))["status"] == "COMPLETED"
            assert probe.calls == Counter({"approved-after-restart": 1})
        finally:
            await runner.stop()
    asyncio.run(exercise())


def _seed_unknown_history(database, receipt):
    """Persist only synthetic unknown-write facts on a normally typed Command."""
    step_id, lease_id, request_id, receipt_id = (str(uuid4()) for _ in range(4))
    target = {"fixture": "unresolved original write"}
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE agent_runs SET status='BLOCKED_DATA',error_code='WRITE_OUTCOME_UNKNOWN' WHERE run_id=%s",
                       (receipt.run_id,))
        cursor.execute("""INSERT INTO agent_run_steps(step_id,run_id,step_key,step_order,tool_name,tool_version,
            operation_type,risk_level,status,idempotency_key,error_code) VALUES(%s,%s,'old',1,%s,'1.0.0',
            'INTERNAL_PROJECTION_WRITE','LOW','BLOCKED_DATA',%s,'WRITE_OUTCOME_UNKNOWN')""",
                       (step_id, receipt.run_id, TOOL, step_id))
        cursor.execute("""INSERT INTO automation_project_generation_leases(lease_id,automation_id,generation,
            orchestration_run_id,lease_owner,runtime_metadata_json,runtime_metadata_sha256,outcome,expires_at)
            VALUES(%s,%s,1,%s,'isolated-history','{}',%s,'WRITE_OUTCOME_UNKNOWN',UTC_TIMESTAMP(6)-INTERVAL 1 DAY)""",
                       (lease_id, database.project_id, receipt.run_id, _digest({})))
        cursor.execute("""INSERT INTO automation_write_attempt_receipts(receipt_id,automation_id,generation,lease_id,
            orchestration_run_id,step_id,request_id,operation,action,argument_sha256,target_ref_sha256,target_ref_json,
            outcome,evidence_sha256,execution_resource_keys_json,created_at,updated_at) VALUES(%s,%s,1,%s,%s,%s,%s,'projection.invoke',
            'scan.snapshot.replace',%s,%s,%s,'WRITE_OUTCOME_UNKNOWN',%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                       (receipt_id, database.project_id, lease_id, receipt.run_id, step_id, request_id,
                        _digest(target), _digest(target), json.dumps(target), _digest({"unresolved": True}),
                        json.dumps([["account-write", ACCOUNT]])))
        connection.commit()
    return receipt_id, lease_id


def test_explicit_cancel_then_fresh_typed_execution_preserves_unknown_facts(database):
    async def exercise():
        probe = _Probe(database, writes=True)
        runner = _runner(database, probe)
        old = await _submit(database, runner, "unknown-old-never-replay")
        receipt_id, lease_id = _seed_unknown_history(database, old)

        def original_facts():
            with database.helper._connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT * FROM automation_write_attempt_receipts WHERE receipt_id=%s", (receipt_id,))
                receipt = cursor.fetchone()
                cursor.execute("SELECT * FROM automation_project_generation_leases WHERE lease_id=%s", (lease_id,))
                return receipt, cursor.fetchone()

        before = original_facts()
        service = ControlPlaneService(database.repository, runner._approval_service, wake_runner=runner.wake)
        cancelled = await service.cancel_run(old.run_id, actor=ADMIN, comment="explicit isolated cancellation; preserve unknown receipt")
        assert cancelled["run"]["status"] == "CANCELLED"
        assert original_facts() == before
        assert unknown_execution_keys(database.repository) == ()
        await runner.start()
        try:
            fresh = await _submit(database, runner, "sql-after-cancel", guarded=True)
            await _approve(database, runner, fresh)
            final = await _terminal(database, fresh)
            assert final["status"] == "COMPLETED" and final["execution_attempt_count"] == 1
            assert probe.calls == Counter({"sql-after-cancel": 1})
            assert database.repository.get_run(old.run_id)["status"] == "CANCELLED"
            assert original_facts() == before
            with database.helper._connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT job,value FROM isolated_scope_results WHERE job=%s", ("sql-after-cancel",))
                assert cursor.fetchall() == [{"job": "sql-after-cancel", "value": sum(range(len("sql-after-cancel") + 1))}]
        finally:
            await runner.stop()
    asyncio.run(exercise())
