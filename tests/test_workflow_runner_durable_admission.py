"""Isolated MySQL queue acceptance, real Runner and registered local workloads.

These are scheduling/control-plane acceptance slices, not four-script business
acceptance. Workload entry/exit is observable; no Runner method is mocked.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import Actor, ActorType, Command, OperationType, OrchestrationError
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.policy_engine import PolicyEngine
from agent.orchestration.result_verifier import ResultVerifier
from agent.orchestration.workflow_runner import WorkflowRunner, _CLAIM_OWNER, _ResourceWait
from agent.tool_registry import validate_schema_instance
from shared.orchestration_repository_support import ConcurrentUpdateError, IdempotencyConflict


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8",
)


@pytest.fixture(scope="module")
def repository():
    import pymysql
    import test_mysql_orchestration_integration as mysql_support

    # Reuse the repository's reviewed migration/bootstrap helper, while owning
    # only this separate test database (never the full-CI database).
    helper = type("AdmissionDatabase", (mysql_support.MySqlOrchestrationIntegrationTests,), {})
    helper.pymysql = pymysql
    helper.host = os.environ["AGENT_DB_HOST"]
    helper.port = int(os.environ["AGENT_DB_PORT"])
    helper.user = os.environ["AGENT_DB_USER"]
    helper.password = os.environ["AGENT_DB_PASS"]
    helper.database = "v32_reliability_test"
    assert helper.host == "127.0.0.1"
    helper.runner = mysql_support._load_migration_runner()
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute("DROP DATABASE IF EXISTS v32_reliability_test")
        cursor.execute("CREATE DATABASE v32_reliability_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    helper._run_migrations(helper.database)
    result = helper._repository()
    result.validate_mysql8()
    # Runtime resource-store facades use the configured database directly.
    # Bind them to this fixture's real database too, then restore the caller's
    # environment so later modules cannot inherit this database after teardown.
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("AGENT_DB_NAME", helper.database)
        yield result
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute("DROP DATABASE v32_reliability_test")


class _Catalog:
    def get_capability(self, tool_name):
        assert tool_name == "v32_local_workload"
        return {
            "name": tool_name, "version": "1.0.0", "operation_type": "read",
            "risk_level": "low", "approval": {"mode": "none"},
            "permissions": {"required_roles": ["admin"]}, "account_scope": {"mode": "single"},
            "evidence": [], "postconditions": [], "llm_exposed": False,
            "retry": {"safe": True}, "idempotency": {"mode": "key"},
            "input_schema": {
                "type": "object", "properties": {"job": {"type": "string"}},
                "required": ["job"], "additionalProperties": False,
            },
            "output_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "status": {"type": "string", "enum": ["SUCCESS", "FAILED"]},
                    "data": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"job": {"type": "string"}, "rows": {
                            "type": "array", "items": {"type": "object", "properties": {
                                "value": {"type": "integer"},
                            }, "required": ["value"], "additionalProperties": False},
                        }}, "required": ["job", "rows"],
                    },
                    "meta": {"type": "object", "additionalProperties": True},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                    "error": {"type": "null"},
                },
                "required": ["status", "data", "meta", "warnings", "error"],
            },
        }

    def validate_arguments(self, tool_name, arguments):
        validate_schema_instance(tool_name, arguments, self.get_capability(tool_name)["input_schema"])


class _Workload:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = Counter()
        self.lock = threading.Lock()

    def execute(self, arguments):
        job = arguments["job"]
        with self.lock:
            self.calls[job] += 1
        if job == "holder":
            self.started.set()
            if not self.release.wait(10):
                raise TimeoutError("isolated holder deadline")
        if job == "failure":
            raise ValueError("isolated independent failure")
        return {"job": job, "rows": [{"value": sum(range(len(job) + 1))}]}


def test_runner_execution_context_reaches_real_broker_action_receipts(repository, tmp_path):
    """Real MySQL Runner -> registered local port -> thread -> Broker issuer.

    This is an admission/receipt slice; it performs no external business calls.
    Neither the Runner nor its task-context handoff is patched.
    """
    from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer
    from tests.test_execution_action_scopes import capability, issue_receipts, scopes

    cap, saved = capability()
    receipts = []
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "broker.sock", write_attempt_recorder=receipts.append)

    class ScopedCatalog(_Catalog):
        def get_capability(self, tool_name):
            # The registered workload writes only this local test's receipt
            # list. Its effect is internal; no external impact gate is bypassed.
            return {**super().get_capability(tool_name), **cap,
                "operation_type": OperationType.INTERNAL_PROJECTION_WRITE.value}

    def execute(arguments):
        issue_receipts(issuer, cap)
        return {"job": arguments["job"], "rows": [{"value": len(receipts)}]}

    async def exercise():
        catalog = ScopedCatalog()
        policy = PolicyEngine(catalog)
        runner = WorkflowRunner(
            repository=repository, catalog=catalog,
            execution_port=RegisteredToolExecutionAdapter(catalog=catalog, executor=None,
                direct_runners={"v32_local_workload": execute}),
            context_builder=ContextBuilder(account_resolver=lambda command: [
                {"account_id": command.parameters["account_id"], "is_active": True},
            ]),
            planner=DeterministicPlanner(catalog), validator=PlanValidator(catalog), policy=policy,
            approval_service=ApprovalService(repository, policy), verifier=ResultVerifier(),
            saved_resource_provider=saved.get, worker_id="action-scope-receipts", poll_interval_seconds=0.1,
        )
        gateway = CommandGateway(repository, wake_runner=runner.wake)
        await runner.start()
        try:
            receipt = await asyncio.to_thread(gateway.submit, _command("scope-receipts", account="account-a"))
            final = await _until(lambda: repository.get_run(receipt.run_id),
                lambda row: row["status"] in {"COMPLETED", "BLOCKED_DATA", "WAITING_APPROVAL", "FAILED_TERMINAL"})
            if final["status"] == "WAITING_APPROVAL":
                def latest_approval():
                    with repository.unit_of_work() as uow:
                        return uow.approvals.get_latest_for_run(receipt.run_id)
                approval = await _until(latest_approval, lambda row: row is not None)
                await asyncio.to_thread(runner._approval_service.decide,
                    approval_id=approval["approval_id"], plan_hash=approval["plan_hash"],
                    actor=Actor(ActorType.CONSOLE_ADMIN, "isolated-approver", (approval["required_role"],),
                        authenticated_by="mysql_admin_session"),
                    source="console", decision="APPROVED")
                runner.wake()
                final = await _until(lambda: repository.get_run(receipt.run_id),
                    lambda row: row["status"] in {"COMPLETED", "BLOCKED_DATA", "FAILED_TERMINAL"})
            assert final["status"] == "COMPLETED", {key: final[key] for key in ("status", "error_code", "error_summary")}
            assert len(receipts) == 2
            _keys, _bounded, actions = scopes(cap, saved)
            for item in receipts:
                role = "delivery_status_bitable" if item["operation"] == "network.request" else "account_id"
                assert item["execution_resource_keys_json"] == [list(key) for key in actions[(item["operation"], item["action"], role)]]
        finally:
            await runner.stop()
    asyncio.run(exercise())


def _command(job, *, key=None, account="shared-session"):
    return Command(
        command_type="tool.execute", source="console",
        actor=Actor(ActorType.CONSOLE_ADMIN, "isolated-admin", ("admin",)),
        parameters={"tool_name": "v32_local_workload", "arguments": {"job": job}, "account_id": account},
        idempotency_key=key or str(uuid4()),
    )


def _runner(repository, workload):
    catalog = _Catalog()
    policy = PolicyEngine(catalog)
    return WorkflowRunner(
        repository=repository, catalog=catalog,
        execution_port=RegisteredToolExecutionAdapter(
            catalog=catalog, executor=None, direct_runners={"v32_local_workload": workload.execute},
        ),
        context_builder=ContextBuilder(account_resolver=lambda command: [
            {"account_id": command.parameters["account_id"], "is_active": True},
        ]),
        planner=DeterministicPlanner(catalog), validator=PlanValidator(catalog), policy=policy,
        approval_service=ApprovalService(repository, policy), verifier=ResultVerifier(),
        worker_id="v32-admission", worker_concurrency=4, browser_concurrency=3,
        browser_tool_names=("v32_local_workload",), poll_interval_seconds=0.1,
    )


async def _until(read, predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await asyncio.to_thread(read)
        if predicate(value):
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"isolated state deadline: {value}")


@pytest.mark.parametrize("round_number", range(20))
def test_four_workers_return_resource_wait_claims_and_cancel(repository, round_number, record_property):
    async def exercise():
        workload = _Workload()
        runner = _runner(repository, workload)
        gateway = CommandGateway(repository, wake_runner=runner.wake)
        await runner.start()
        receipts = []
        try:
            holder = await asyncio.to_thread(gateway.submit, _command("holder"))
            receipts.append(holder)
            assert await asyncio.to_thread(workload.started.wait, 3), {
                key: repository.get_run(holder.run_id)[key]
                for key in ("status", "error_code", "error_summary")
            }
            for number in range(4):
                receipts.append(await asyncio.to_thread(gateway.submit, _command(f"wait-{number}")))
            for receipt in receipts[1:]:
                waiting = await _until(
                    lambda: repository.get_run(receipt.run_id),
                    lambda row: row["error_code"] == "RESOURCE_WAIT" and row["worker_id"] is None,
                )
                assert waiting["started_at"] is None
                assert waiting["execution_attempt_count"] == 0
                assert waiting["steps"][0]["status"] == "PENDING"
            begin = time.monotonic()
            independent = await asyncio.to_thread(gateway.submit, _command("independent", account="other-session"))
            receipts.append(independent)
            await _until(lambda: workload.calls["independent"], bool, timeout=2)
            start_seconds = time.monotonic() - begin
            assert start_seconds <= 2
            begin = time.monotonic()
            cancelled_id = receipts[1].run_id
            await asyncio.to_thread(
                repository.request_run_cancel, cancelled_id, requested_by_type="console_admin",
                requested_by_id="isolated-admin", reason="isolated resource cancellation",
            )
            runner.wake(cancelled_id)
            await _until(lambda: repository.get_run(cancelled_id), lambda row: row["status"] == "CANCELLED", timeout=2)
            cancel_seconds = time.monotonic() - begin
            assert cancel_seconds <= 2
            assert workload.calls["wait-0"] == 0
            workload.release.set()
            for receipt in receipts:
                expected = "CANCELLED" if receipt.run_id == cancelled_id else "COMPLETED"
                await _until(lambda: repository.get_run(receipt.run_id), lambda row: row["status"] == expected)
            assert workload.calls == Counter({"holder": 1, "wait-1": 1, "wait-2": 1, "wait-3": 1, "independent": 1})
            assert runner._browser_semaphore._value == 3
            assert not runner._active and not runner._execution_locks
            record_property("round", round_number)
            record_property("independent_start_seconds", start_seconds)
            record_property("waiting_cancel_seconds", cancel_seconds)
            record_property("business_calls", dict(workload.calls))
        finally:
            workload.release.set()
            await runner.stop()
    asyncio.run(exercise())


@pytest.mark.parametrize("round_number", range(20))
def test_twenty_concurrent_replays_execute_once_and_reject_changed_semantics(repository, round_number, record_property):
    async def exercise():
        workload = _Workload()
        runner = _runner(repository, workload)
        gateway = CommandGateway(repository, wake_runner=runner.wake)
        key = str(uuid4())
        await runner.start()
        try:
            receipts = await asyncio.gather(*[
                asyncio.to_thread(gateway.submit, _command("idempotent", key=key)) for _ in range(20)
            ])
            assert len({receipt.run_id for receipt in receipts}) == 1
            row = await _until(lambda: repository.get_run(receipts[0].run_id), lambda row: row["status"] == "COMPLETED")
            assert row["execution_attempt_count"] == 1
            assert workload.calls == Counter({"idempotent": 1})
            with pytest.raises(OrchestrationError) as conflict:
                await asyncio.to_thread(gateway.submit, _command("different-semantics", key=key))
            assert isinstance(conflict.value.__cause__, IdempotencyConflict)
            assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
            record_property("round", round_number)
            record_property("request_count", len(receipts))
            record_property("business_calls", dict(workload.calls))
        finally:
            await runner.stop()
    asyncio.run(exercise())


@pytest.mark.parametrize("round_number", range(20))
def test_claim_round_fences_same_process_late_start_and_release(repository, round_number):
    command = _command("fenced")
    receipt = CommandGateway(repository).submit(command)
    now = datetime.now()
    old = repository.claim_runs("same-process:old-round", ("RECEIVED",), limit=1, lease_seconds=1, now=now)[0]
    with pytest.raises(ConcurrentUpdateError):
        repository.renew_run_lease(receipt.run_id, worker_id=old["worker_id"], now=now + timedelta(seconds=2))
    recovered = repository.claim_runs("same-process:new-round", ("RECEIVED",), limit=1, lease_seconds=1, now=now + timedelta(seconds=3))[0]
    assert old["run_id"] == recovered["run_id"] == receipt.run_id
    with pytest.raises(ConcurrentUpdateError):
        repository.renew_run_lease(receipt.run_id, worker_id=old["worker_id"])
    runner = _runner(repository, _Workload())
    context = runner._context_builder.build(command)
    plan = runner._planner.plan(command, context)
    with repository.unit_of_work() as uow:
        recovered = uow.runs.transition(
            receipt.run_id, expected_version=recovered["version"],
            expected_statuses=("RECEIVED",), status="RUNNING",
        )
        uow.commit()
    new_token = _CLAIM_OWNER.set(recovered["worker_id"])
    try:
        step_row = runner._get_or_create_step(recovered, plan.steps[0], 1, command)
        started_step, waiting = runner._start_step_under_execution_slot(
            run=recovered, plan=plan, command=command, step=plan.steps[0], step_row=step_row,
        )
        assert waiting is None
        raw_result = asyncio.run(runner._execution_port.execute_step(
            plan.steps[0], run_id=receipt.run_id, step_id=step_row["step_id"],
            execution_context=runner._trusted_execution_context(command),
        ))
        runner._persist_step_result(
            run=recovered, plan=plan, command=command, step=plan.steps[0],
            step_row=step_row, started_step=started_step, raw_result=raw_result,
            capability=runner._catalog.get_capability(plan.steps[0].tool_name),
        )
        with repository.unit_of_work() as uow:
            committed_step = uow.steps.get(step_row["step_id"])
        assert committed_step["status"] == "COMPLETED"
    finally:
        _CLAIM_OWNER.reset(new_token)
    token = _CLAIM_OWNER.set(old["worker_id"])
    try:
        with pytest.raises(OrchestrationError, match="no longer owned"):
            runner._require_claim(recovered)
        with pytest.raises(OrchestrationError) as denied:
            runner._start_step_under_execution_slot(
                run=recovered, plan=plan, command=command, step=plan.steps[0],
                step_row={"step_id": str(uuid4()), "version": 1, "status": "PENDING"},
            )
        assert denied.value.code == "RUN_LEASE_LOST"
        with pytest.raises(OrchestrationError) as late_result:
            runner._persist_step_result(
                run=recovered, plan=plan, command=command, step=plan.steps[0],
                step_row=step_row, started_step=started_step,
                raw_result={**raw_result, "data": {"job": "fenced", "rows": [{"value": 999}]}},
                capability=runner._catalog.get_capability(plan.steps[0].tool_name),
            )
        assert late_result.value.code == "RUN_LEASE_LOST"
        with pytest.raises(ConcurrentUpdateError):
            runner._release(receipt.run_id, status="CANCELLED", finished=True)
    finally:
        _CLAIM_OWNER.reset(token)
    assert repository.get_run(receipt.run_id)["worker_id"] == recovered["worker_id"]
    with repository.unit_of_work() as uow:
        assert uow.steps.get(step_row["step_id"]) == committed_step
        uow.runs.release_or_schedule(receipt.run_id, worker_id=recovered["worker_id"], status="CANCELLED")
        uow.commit()


def test_preparation_and_heartbeat_progress_with_the_ordinary_io_pool_full(repository):
    from concurrent.futures import ThreadPoolExecutor

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=4))
        release = threading.Event()
        all_started = threading.Event()
        entered = set()
        mutex = threading.Lock()
        workload = _Workload()
        runner = _runner(repository, workload)
        runner._lease_seconds = 10

        def slow_accounts(command):
            with mutex:
                entered.add(command.command_id)
                if len(entered) == 4:
                    all_started.set()
            if not release.wait(15):
                raise TimeoutError("isolated preparation deadline")
            return [{"account_id": command.parameters["account_id"], "is_active": True}]

        runner._context_builder = ContextBuilder(account_resolver=slow_accounts)
        gateway = CommandGateway(repository)
        receipts = [gateway.submit(_command(f"slow-{n}", account=f"account-{n}")) for n in range(4)]
        await runner.start()
        try:
            deadline = time.monotonic() + 3
            while not all_started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert all_started.is_set()
            first = [await runner._control_io(repository.get_run, item.run_id) for item in receipts]
            # Two actual renewals, not a mocked heartbeat callback.
            deadline = time.monotonic() + 11
            while True:
                second = [await runner._control_io(repository.get_run, item.run_id) for item in receipts]
                if all((new["lease_expires_at"] - old["lease_expires_at"]).total_seconds() >= 6 for old, new in zip(first, second)):
                    break
                assert time.monotonic() < deadline, "actual two-renewal deadline expired"
                await asyncio.sleep(0.1)
            for old, new in zip(first, second):
                assert (new["lease_expires_at"] - old["lease_expires_at"]).total_seconds() >= 6
                assert new["worker_id"] == old["worker_id"]
                assert new["started_at"] is None
            begin = time.monotonic()
            await runner._control_io(
                repository.request_run_cancel, receipts[0].run_id,
                requested_by_type="console_admin", requested_by_id="isolated-admin",
            )
            assert time.monotonic() - begin <= 2
            release.set()
            for index, item in enumerate(receipts):
                expected = "CANCELLED" if index == 0 else "COMPLETED"
                await _until(lambda: repository.get_run(item.run_id), lambda row: row["status"] == expected)
            assert workload.calls["slow-0"] == 0
        finally:
            release.set()
            await runner.stop()
    asyncio.run(exercise())


@pytest.mark.parametrize("round_number", range(20))
def test_saved_mysql_resource_aliases_overlap_and_unknown_account_scope(repository, round_number, record_property):
    from agent.workflow_resource_store import get_saved_workflow_resource, upsert_workflow_resource

    async def exercise():
        prefix = "v32-lock-" + uuid4().hex
        records = {
            "sheet-a": {"resource_kind": "feishu_sheet", "spreadsheet_token": prefix, "sheet_id": "sheet-a", "sheet_range": "sheet-a!A1:Z99"},
            "sheet-a-alias": {"resource_kind": "feishu_sheet", "spreadsheet_token": prefix, "sheet_id": "sheet-a", "sheet_range": "sheet-a!B2:C3"},
            "sheet-b": {"resource_kind": "feishu_sheet", "spreadsheet_token": prefix, "sheet_id": "sheet-b"},
            "whole-document": {"resource_kind": "feishu_sheet", "spreadsheet_token": prefix},
            "table-a": {"resource_kind": "feishu_bitable", "base_token": prefix, "table_id": "table-a"},
            "table-alias": {"resource_kind": "feishu_bitable", "base_token": prefix, "table_id": "table-a"},
            "table-b": {"resource_kind": "feishu_bitable", "base_token": prefix, "table_id": "table-b"},
            "url-only": {"resource_kind": "feishu_sheet", "source_url": "https://example.invalid/saved-resource"},
        }
        for key, value in records.items():
            upsert_workflow_resource(prefix + key, value, source="isolated-v32-lock-acceptance")
        runner = _runner(repository, _Workload())
        runner._browser_tool_names = frozenset()
        runner._saved_resource_provider = get_saved_workflow_resource
        command = _command("physical-lock", account="credential-a")
        plan = runner._planner.plan(command, runner._context_builder.build(command))
        step = replace(plan.steps[0], operation_type=OperationType.EXTERNAL_WRITE)

        def capability(resource_keys, *, plugin="writer-a", role_prefix="output", action="feishu.sheet.replace"):
            roles = {f"{role_prefix}{index}": prefix + key for index, key in enumerate(resource_keys)}
            return {"_plugin_runtime": {"plugin_id": plugin, "resource_bindings": roles, "runtime_permissions": {
                "browser": False, "broker_operations": [{"operation": "network.request", "action": action, "roles": list(roles), "effect": "write"}],
            }}}

        alias_step = replace(step, account_id="credential-b", arguments={**step.arguments, "account_id": "credential-b"})
        first = await runner._acquire_execution_slot(step, plan, capability(["sheet-a"]))
        try:
            independent = await runner._acquire_execution_slot(step, plan, capability(["sheet-b"]))
            independent()
            with pytest.raises(_ResourceWait):
                await runner._acquire_execution_slot(alias_step, plan, capability(["sheet-a-alias"], plugin="other-plugin", role_prefix="different-role"))
            with pytest.raises(_ResourceWait):
                await runner._acquire_execution_slot(alias_step, plan, capability(["whole-document"]))
            with pytest.raises(_ResourceWait):
                await runner._acquire_execution_slot(step, plan, {})
        finally:
            first()
        unknown = await runner._acquire_execution_slot(step, plan, {})
        try:
            with pytest.raises(_ResourceWait):
                await runner._acquire_execution_slot(step, plan, capability(["sheet-a"]))
        finally:
            unknown()
        pair = await runner._acquire_execution_slot(step, plan, capability(["sheet-a", "sheet-b"]))
        try:
            with pytest.raises(_ResourceWait):
                await runner._acquire_execution_slot(alias_step, plan, capability(["sheet-b", "sheet-a"]))
            assert all(count == 1 for count in runner._execution_lock_users.values())
        finally:
            pair()
        table = await runner._acquire_execution_slot(step, plan, capability(["table-a"], action="feishu.bitable.write_records"))
        try:
            other_table = await runner._acquire_execution_slot(step, plan, capability(["table-b"], action="feishu.bitable.write_records"))
            other_table()
            with pytest.raises(_ResourceWait):
                await runner._acquire_execution_slot(alias_step, plan, capability(["table-alias"], action="feishu.bitable.delete_records"))
            for unresolved in (capability(["url-only"]), capability(["sheet-b"], action="feishu.sheet.unknown")):
                with pytest.raises(_ResourceWait):
                    await runner._acquire_execution_slot(step, plan, unresolved)
        finally:
            table()
        archive = await runner._acquire_execution_slot(step, plan, capability(["sheet-a"], action="feishu.sheet.add"))
        try:
            with pytest.raises(_ResourceWait):
                await runner._acquire_execution_slot(alias_step, plan, capability(["sheet-b"]))
        finally:
            archive()
        assert not runner._execution_locks and not runner._execution_lock_users
        assert runner._browser_semaphore._value == 3
        record_property("round", round_number)
        record_property("physical_scope_cases", "same-account-independent;cross-credential-alias;whole-sheet-overlap;whole-document-overlap;unknown-account-both-orders;reverse-multi-resource-no-leak;bitable-alias;archive-parent-scope;unknown-action-conservative;url-only-conservative")
    asyncio.run(exercise())
