"""Real MySQL legacy scope quarantine and actual local Runner regression.

Only a uniquely owned test database is used. Historical records model the
pre-journal release boundary; no business outcome or recovery proof is forged.
The Runner test registers a small SQL-writing tool, not a mock business script.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import Actor, ActorType
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.policy_engine import PolicyEngine
from agent.orchestration.result_verifier import ResultVerifier
from agent.orchestration.workflow_runner import WorkflowRunner, _ResourceWait
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.execution_resource_journal import unknown_execution_keys
from tests.test_workflow_runner_durable_admission import _Catalog, _command, _until


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8",
)
ROOT = Path(__file__).resolve().parents[1]
# schema_migrations.applied_at is a database-local DATETIME without fractions.
RELEASE_CUTOFF = datetime(2026, 9, 7, 13, 7, 53)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class _Database:
    def __init__(self, helper):
        self.helper = helper
        self.repository = helper._repository()
        self.project_id = "isolated_legacy_scope"
        self.plugin_id = "isolated_legacy_scope_plugin"
        with helper._connection() as connection:
            repository = AutomationPluginRepository(connection)
            digest = _digest({"synthetic": "migration identity fixture"})
            hashes = {
                field: digest for field in (
                    "package_sha256", "manifest_sha256", "tool_contract_sha256", "config_schema_sha256",
                    "allowed_entrypoints_sha256", "invocation_contracts_sha256", "worker_requirement_sha256",
                    "runtime_sha256", "scheduling_sha256", "install_root_metadata_sha256",
                )
            }
            repository.register_package_version(
                package={"plugin_id": self.plugin_id, "display_name": "isolated", "description": "migration fixture"},
                version={**hashes, "version": "1.0.0", "manifest_json": {"runtime": {"kind": "python_subprocess"}},
                    "trust_source": "ed25519_first_party", "install_root_metadata_json": {},
                    "installed_by_actor_id": "isolated-admin", "project_full_auto_allowed": False},
            )
            repository.install_project_instance({
                "automation_id": self.project_id, "plugin_id": self.plugin_id, "plugin_version": "1.0.0",
                "display_name": "isolated", "install_request_id": str(uuid4()), "install_payload_sha256": digest,
                "installed_by_actor_id": "isolated-admin", "migration_authority": False,
            })
            columns = ["package_sha256", "manifest_sha256", "project_config_sha256", "account_bindings_sha256",
                "resource_bindings_sha256", "device_binding_sha256", "schedule_sha256", "core_registry_sha256",
                "tool_contract_sha256", "invocation_contracts_sha256", "compiled_invocations_sha256",
                "runtime_descriptor_sha256", "governance_anchor_sha256", "policy_contract_sha256",
                "enabled_entrypoints_sha256", "snapshot_sha256"]
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO automation_project_generations(automation_id,generation,request_id,state,plugin_id,"
                    "plugin_version,trust_source,snapshot_json," + ",".join(columns) + ") "
                    "VALUES(%s,1,%s,'COMMITTED',%s,'1.0.0','ed25519_first_party','{}'," + ",".join(["%s"] * len(columns)) + ")",
                    (self.project_id, str(uuid4()), self.plugin_id, *([digest] * len(columns))),
                )
                cursor.execute("CREATE TABLE isolated_scope_results(job VARCHAR(80) PRIMARY KEY, value INT NOT NULL) ENGINE=InnoDB")
                # This uniquely owned synthetic database models the original
                # migration event, not the wall clock when pytest bootstraps it.
                cursor.execute("UPDATE schema_migrations SET applied_at=%s WHERE version='041'", (RELEASE_CUTOFF,))
            connection.commit()

    def seed(self, *, run_status="CANCELLED", receipt_outcome="WRITE_OUTCOME_UNKNOWN", keys=None,
             created_at=None, lease_outcome="WRITE_OUTCOME_UNKNOWN", live_generation_lease=False,
             live_worker=False, step_status="BLOCKED_DATA", run_created_at=None, live_sibling_lease=False,
             raw_keys_json=None, lease_expires_at=None):
        accepted = CommandGateway(self.repository).submit(_command("old-" + uuid4().hex))
        lease_id, step_id, request_id = (str(uuid4()) for _ in range(3))
        receipt_id = str(uuid5(NAMESPACE_URL, f"boyi:write-attempt:{lease_id}:{request_id}"))
        original_time = created_at or RELEASE_CUTOFF - timedelta(days=1)
        expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1) if live_generation_lease else original_time
        if lease_expires_at is not None:
            expires = lease_expires_at
        worker_expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1) if live_worker else None
        target = {"schema": 1, "automation_id": self.project_id, "operation": "projection.invoke",
            "action": "scan.snapshot.replace", "role_sha256": hashlib.sha256(b"account_id").hexdigest(),
            "binding_sha256": hashlib.sha256(b"isolated-account").hexdigest(),
            "request_sha256": hashlib.sha256(request_id.encode()).hexdigest(), "business_date_sha256": "",
            "batch_sha256": "", "run_sha256": "", "idempotency_key_sha256": "", "record_count": 0,
            "content_sha256": _digest({"records": []})}
        with self.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_commands SET automation_id=%s,automation_generation=1 WHERE command_id=%s",
                (self.project_id, accepted.command_id))
            cursor.execute("UPDATE agent_runs SET status=%s,worker_id=%s,lease_expires_at=%s,created_at=%s WHERE run_id=%s",
                (run_status, "isolated-live-worker" if live_worker else None, worker_expires,
                    run_created_at or original_time, accepted.run_id))
            cursor.execute("""INSERT INTO agent_run_steps(step_id,run_id,step_key,step_order,tool_name,tool_version,
                operation_type,risk_level,status,idempotency_key) VALUES(%s,%s,'original',1,'v32_local_workload',
                '1.0.0','INTERNAL_PROJECTION_WRITE','LOW',%s,%s)""", (step_id, accepted.run_id, step_status, step_id))
            cursor.execute("""INSERT INTO automation_project_generation_leases(lease_id,automation_id,generation,
                orchestration_run_id,lease_owner,runtime_metadata_json,runtime_metadata_sha256,outcome,acquired_at,expires_at)
                VALUES(%s,%s,1,%s,'isolated-fixture','{}',%s,%s,%s,%s)""",
                (lease_id, self.project_id, accepted.run_id, _digest({}), lease_outcome, original_time, expires))
            if live_sibling_lease:
                cursor.execute("""INSERT INTO automation_project_generation_leases(lease_id,automation_id,generation,
                    orchestration_run_id,lease_owner,runtime_metadata_json,runtime_metadata_sha256,outcome,expires_at)
                    VALUES(%s,%s,1,%s,'isolated-active-sibling','{}',%s,'RUNNING',UTC_TIMESTAMP(6)+INTERVAL 1 DAY)""",
                    (str(uuid4()), self.project_id, accepted.run_id, _digest({})))
            cursor.execute("""INSERT INTO automation_write_attempt_receipts(receipt_id,automation_id,generation,
                lease_id,orchestration_run_id,step_id,request_id,operation,action,argument_sha256,target_ref_sha256,
                target_ref_json,outcome,evidence_sha256,created_at,updated_at,execution_resource_keys_json)
                VALUES(%s,%s,1,%s,%s,%s,%s,'projection.invoke','scan.snapshot.replace',%s,%s,%s,%s,%s,%s,%s,%s)""",
                (receipt_id, self.project_id, lease_id, accepted.run_id, step_id, request_id, target["content_sha256"],
                    _digest(target), json.dumps(target), receipt_outcome, _digest({"unresolved": "retained audit"}),
                    original_time, original_time,
                    raw_keys_json if raw_keys_json is not None else json.dumps(keys) if keys is not None else None))
            connection.commit()
        return {"receipt_id": receipt_id, "lease_id": lease_id, "run_id": accepted.run_id,
            "step_id": step_id, "command_id": accepted.command_id}

    def apply(self, *, session_time_zone=None):
        migrations = [path for version, path in self.helper.runner.discover_migrations() if version == "042"]
        assert len(migrations) == 1, "the exact reviewed 042 migration must exist"
        with self.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
            if session_time_zone is not None:
                cursor.execute("SET SESSION time_zone=%s", (session_time_zone,))
            for statement in self.helper.runner.split_sql_statements(migrations[0].read_text(encoding="utf-8")):
                cursor.execute(statement)

    def snapshot(self):
        with self.helper._connection() as connection, connection.cursor() as cursor:
            result = {}
            for table, identity in (("automation_write_attempt_receipts", "receipt_id"),
                ("automation_project_generation_leases", "lease_id"), ("agent_runs", "run_id"),
                ("agent_run_steps", "step_id"), ("agent_commands", "command_id")):
                cursor.execute(f"SELECT * FROM {table} ORDER BY {identity}")
                result[table] = cursor.fetchall()
            return result


@pytest.fixture
def database():
    import pymysql
    from tests import test_mysql_orchestration_integration as support

    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    assert re.fullmatch(r"(?:test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)", os.environ["AGENT_DB_NAME"])
    helper = type("LegacyScopeDatabase", (support.MySqlOrchestrationIntegrationTests,), {})
    helper.pymysql, helper.runner = pymysql, support._load_migration_runner()
    helper.host, helper.port = os.environ["AGENT_DB_HOST"], int(os.environ["AGENT_DB_PORT"])
    helper.user, helper.password = os.environ["AGENT_DB_USER"], os.environ["AGENT_DB_PASS"]
    helper.database = "legacy_scope_" + uuid4().hex + "_test"
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


def test_migration_is_idempotent_and_only_labels_stopped_pre_release_missing_scope(database):
    eligible = [database.seed(run_status=status, keys=keys)["receipt_id"]
        for status in ("CANCELLED", "PARTIAL", "FAILED_TERMINAL", "BLOCKED_DATA") for keys in (None, [])]
    excluded = [database.seed(**case)["receipt_id"] for case in (
        {"created_at": RELEASE_CUTOFF}, {"created_at": RELEASE_CUTOFF + timedelta(microseconds=1)},
        {"run_created_at": RELEASE_CUTOFF}, {"live_sibling_lease": True},
        {"live_generation_lease": True}, {"live_worker": True}, {"run_status": "RUNNING"},
        {"run_status": "VERIFYING"}, {"run_status": "FAILED_RETRYABLE"},
        {"receipt_outcome": "STARTED", "lease_outcome": "RUNNING"},
        {"receipt_outcome": "WRITE_VERIFIED"}, {"keys": [["account-write", "captured-account"]]},
        {"keys": {}}, {"keys": ""}, {"raw_keys_json": "null"},
    )]
    before = database.snapshot()
    database.apply()
    after = database.snapshot()
    labels = {row["receipt_id"]: row["legacy_scope_quarantined_at"] for row in after["automation_write_attempt_receipts"]}
    assert all(labels[identity] is not None for identity in eligible)
    assert all(labels[identity] is None for identity in excluded)
    for rows in after.values():
        for row in rows:
            if "legacy_scope_quarantined_at" in row:
                row["legacy_scope_quarantined_at"] = None
    assert after == before, "migration must preserve all outcomes, evidence, Run/Step and identity data"
    first = database.snapshot()
    database.apply()
    assert database.snapshot() == first, "reapplying must retain the original quarantine timestamp"


def test_post_release_stopped_missing_scope_is_history_without_a_global_lock(database):
    database.seed()
    database.apply()
    assert unknown_execution_keys(database.repository) == ()
    database.seed(created_at=RELEASE_CUTOFF + timedelta(seconds=1))
    database.apply()
    before = database.snapshot()
    assert unknown_execution_keys(database.repository) == ()
    assert database.snapshot() == before


@pytest.mark.parametrize("original_zone,hours,later_zone", [("+00:00", 0, "+08:00"), ("+08:00", 8, "+00:00")])
def test_persisted_database_local_boundary_survives_session_timezone_changes(database, original_zone, hours, later_zone):
    boundary = RELEASE_CUTOFF + timedelta(hours=hours)
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE schema_migrations SET applied_at=%s WHERE version='041'", (boundary,))
        connection.commit()
    expired_utc_lease = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    old = database.seed(created_at=boundary - timedelta(microseconds=1), lease_expires_at=expired_utc_lease)
    database.apply(session_time_zone=original_zone)
    assert unknown_execution_keys(database.repository) == ()
    # Equality is excluded too: a DATETIME boundary must not widen by a second
    # or an offset simply because this migration runs in a different session.
    excluded = [database.seed(created_at=boundary + delta, lease_expires_at=expired_utc_lease)
        for delta in (timedelta(), timedelta(microseconds=1))]
    database.apply(session_time_zone=original_zone)
    before = database.snapshot()
    labels = {row["receipt_id"]: row["legacy_scope_quarantined_at"]
        for row in before["automation_write_attempt_receipts"]}
    assert labels[old["receipt_id"]] is not None
    assert all(labels[row["receipt_id"]] is None for row in excluded)
    database.apply(session_time_zone=later_zone)
    assert database.snapshot() == before
    assert unknown_execution_keys(database.repository) == ()


def test_recovered_original_scope_only_protects_a_live_execution(database):
    old = database.seed()
    database.apply()
    assert unknown_execution_keys(database.repository) == ()
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE automation_write_attempt_receipts SET execution_resource_keys_json=%s WHERE receipt_id=%s",
            (json.dumps([["account-write", "recovered-original-account"]]), old["receipt_id"]))
        connection.commit()
    assert unknown_execution_keys(database.repository) == ()
    database.apply()
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE agent_runs SET worker_id='isolated-live-worker',"
                       "lease_expires_at=UTC_TIMESTAMP(6)+INTERVAL 1 DAY WHERE run_id=%s", (old["run_id"],))
        connection.commit()
    assert unknown_execution_keys(database.repository) == (("account-write", "recovered-original-account"),)
    for malformed in ("{}", "null", '""'):
        with database.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE automation_write_attempt_receipts SET execution_resource_keys_json=%s WHERE receipt_id=%s",
                (malformed, old["receipt_id"]))
            connection.commit()
        with pytest.raises(ValueError):
            unknown_execution_keys(database.repository)


@pytest.mark.parametrize("active", [
    {"live_generation_lease": True, "lease_outcome": "RUNNING"},
    {"live_generation_lease": True, "lease_outcome": "VERIFYING"},
    {"live_worker": True},
])
def test_active_missing_scope_is_not_quarantined(database, active):
    old = database.seed(**active)
    database.apply()
    with pytest.raises(ValueError, match="UNKNOWN_WRITE_SCOPE_UNAVAILABLE:" + old["receipt_id"]):
        unknown_execution_keys(database.repository)


@pytest.mark.parametrize("run_status", ["CANCELLED", "FAILED_TERMINAL", "BLOCKED_DATA", "RUNNING", "VERIFYING"])
@pytest.mark.parametrize("raw_keys", [None, "[]", "{}", '[["account-write","isolated-account"]]'])
def test_stopped_history_never_locks_new_execution_even_with_stale_step_markers(database, run_status, raw_keys):
    database.seed(run_status=run_status, step_status="VERIFYING", raw_keys_json=raw_keys)
    before = database.snapshot()
    assert unknown_execution_keys(database.repository) == ()
    assert database.snapshot() == before, "reading execution scope must not rewrite unknown outcomes or audit history"


@pytest.mark.parametrize("active", [
    {"live_worker": True},
    {"live_generation_lease": True, "lease_outcome": "RUNNING"},
    {"live_generation_lease": True, "lease_outcome": "VERIFYING"},
])
def test_actual_live_lease_protects_scope_until_it_expires_without_settling_receipt(database, active):
    keys = (("account-write", "isolated-account"),)
    old = database.seed(keys=keys, **active)
    before = database.snapshot()
    assert unknown_execution_keys(database.repository) == keys
    assert database.snapshot() == before
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE agent_runs SET lease_expires_at=UTC_TIMESTAMP(6)-INTERVAL 1 DAY WHERE run_id=%s", (old["run_id"],))
        cursor.execute("UPDATE automation_project_generation_leases SET expires_at=UTC_TIMESTAMP(6)-INTERVAL 1 DAY WHERE lease_id=%s", (old["lease_id"],))
        connection.commit()
    after_expiry = database.snapshot()
    assert unknown_execution_keys(database.repository) == ()
    assert database.snapshot() == after_expiry
    assert before["automation_write_attempt_receipts"] == after_expiry["automation_write_attempt_receipts"]


@pytest.mark.parametrize("mismatch", ["command_project", "command_generation", "step_run", "lease_run"])
def test_unrelated_live_lease_cannot_turn_broken_history_into_a_global_lock(database, mismatch):
    old = database.seed(keys=[["account-write", "isolated-account"]],
                        live_generation_lease=True, lease_outcome="RUNNING")
    other = database.seed()
    with database.helper._connection() as connection, connection.cursor() as cursor:
        if mismatch == "command_project":
            cursor.execute("UPDATE agent_commands SET automation_id=NULL WHERE command_id=%s", (old["command_id"],))
        elif mismatch == "command_generation":
            cursor.execute("UPDATE agent_commands SET automation_generation=NULL WHERE command_id=%s", (old["command_id"],))
        elif mismatch == "step_run":
            cursor.execute("UPDATE automation_write_attempt_receipts SET step_id=%s WHERE receipt_id=%s",
                           (other["step_id"], old["receipt_id"]))
        else:
            cursor.execute("UPDATE automation_project_generation_leases SET orchestration_run_id=%s WHERE lease_id=%s",
                           (other["run_id"], old["lease_id"]))
        connection.commit()
    before = database.snapshot()
    assert unknown_execution_keys(database.repository) == ()
    assert database.snapshot() == before


def test_scope_reader_pages_all_valid_receipts_and_retains_real_conflicts(database):
    old = database.seed(keys=[["account-write", "recorded-account"]], live_worker=True)
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT target_ref_json FROM automation_write_attempt_receipts WHERE receipt_id=%s", (old["receipt_id"],))
        original_target = json.loads(cursor.fetchone()["target_ref_json"])
        for number in range(1001):
            request_id = str(uuid4())
            receipt_id = str(uuid5(NAMESPACE_URL, f"boyi:write-attempt:{old['lease_id']}:{request_id}"))
            target = {**original_target, "request_sha256": hashlib.sha256(request_id.encode()).hexdigest()}
            cursor.execute("""INSERT INTO automation_write_attempt_receipts(receipt_id,automation_id,generation,
                lease_id,orchestration_run_id,step_id,request_id,operation,action,argument_sha256,target_ref_sha256,
                target_ref_json,outcome,evidence_sha256,created_at,updated_at,execution_resource_keys_json)
                SELECT %s,automation_id,generation,lease_id,orchestration_run_id,step_id,%s,operation,action,
                argument_sha256,%s,%s,outcome,evidence_sha256,created_at,updated_at,%s
                FROM automation_write_attempt_receipts WHERE receipt_id=%s""",
                (receipt_id, request_id, _digest(target), json.dumps(target),
                    json.dumps([["account-write", f"recorded-{number}"]]), old["receipt_id"]))
        connection.commit()
    database.apply()
    keys = unknown_execution_keys(database.repository)
    assert len(keys) == 1002
    assert ("account-write", "recorded-account") in keys
    runner = _projection_runner(database)
    async def exercise():
        command = _command("conflict", account="recorded-account")
        plan = runner._planner.plan(command, runner._context_builder.build(command))
        capability = runner._catalog.get_capability(plan.steps[0].tool_name)
        with pytest.raises(_ResourceWait):
            await runner._acquire_execution_slot(plan.steps[0], plan, capability)
        other = _command("independent", account="independent-account")
        other_plan = runner._planner.plan(other, runner._context_builder.build(other))
        slot = await runner._acquire_execution_slot(other_plan.steps[0], other_plan, capability)
        slot()
    asyncio.run(exercise())


def test_live_and_newly_queued_runs_sort_ahead_of_more_than_one_cleanup_batch(database):
    for number in range(120):
        database.seed(run_status="FAILED_RETRYABLE" if number % 2 else "RUNNING",
                      step_status="VERIFYING", keys=[["account-write", "historical-account"]])
    queued = database.seed(run_status="RECEIVED")
    active = database.seed(run_status="RUNNING", live_worker=True, step_status="RUNNING")
    before = database.snapshot()
    with database.repository.unit_of_work() as uow:
        candidates = uow.runs.list_unfinished_for_automation(database.project_id, limit=100)
    assert len(candidates) == 100
    assert set(candidates[:2]) == {queued["run_id"], active["run_id"]}
    assert database.snapshot() == before


class _ProjectionCatalog(_Catalog):
    def get_capability(self, tool_name):
        return {**super().get_capability(tool_name), "operation_type": "internal_projection_write"}


def _projection_runner(database, calls=None):
    catalog = _ProjectionCatalog()
    policy = PolicyEngine(catalog)
    def execute(arguments):
        job = arguments["job"]
        value = sum(range(len(job) + 1))
        with database.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO isolated_scope_results(job,value) VALUES(%s,%s)", (job, value))
            connection.commit()
            cursor.execute("SELECT value FROM isolated_scope_results WHERE job=%s", (job,))
            observed = cursor.fetchone()["value"]
        if calls is not None:
            calls[job] += 1
        return {"job": job, "rows": [{"value": observed}]}
    return WorkflowRunner(repository=database.repository, catalog=catalog,
        execution_port=RegisteredToolExecutionAdapter(catalog=catalog, executor=None,
            direct_runners={"v32_local_workload": execute}),
        context_builder=ContextBuilder(account_resolver=lambda command: [
            {"account_id": command.parameters["account_id"], "is_active": True}]),
        planner=DeterministicPlanner(catalog), validator=PlanValidator(catalog), policy=policy,
        approval_service=ApprovalService(database.repository, policy), verifier=ResultVerifier(),
        worker_id="isolated-scope-regression", poll_interval_seconds=0.1)


@pytest.mark.parametrize("run_status", ["CANCELLED", "FAILED_TERMINAL"])
@pytest.mark.parametrize("keys", [None, [["account-write", "isolated-account"]]])
def test_real_runner_performs_new_sql_write_once_without_replaying_stopped_history(database, run_status, keys):
    old = database.seed(run_status=run_status, keys=keys)
    before = database.snapshot()
    calls = Counter()
    runner = _projection_runner(database, calls)
    async def exercise():
        await runner.start()
        try:
            gateway = CommandGateway(database.repository, wake_runner=runner.wake)
            accepted = await asyncio.to_thread(gateway.submit, _command("new-sql-result", account="isolated-account"))
            def pending_approval():
                with database.repository.unit_of_work() as uow:
                    return uow.approvals.get_latest_for_run(accepted.run_id)
            approval = await _until(pending_approval, lambda row: bool(row and row["status"] == "PENDING"))
            # Exercise the normal durable approval transition; this test's
            # service-level actor models a previously authenticated admin.
            await asyncio.to_thread(runner._approval_service.decide,
                approval_id=approval["approval_id"], plan_hash=approval["plan_hash"],
                actor=Actor(ActorType.CONSOLE_ADMIN, "isolated-approver", ("admin", "super_admin"),
                    authenticated_by="mysql_admin_session"), source="console", decision="APPROVED")
            runner.wake(accepted.run_id)
            completed = await _until(lambda: database.repository.get_run(accepted.run_id),
                lambda row: row["status"] == "COMPLETED")
            assert completed["execution_attempt_count"] == 1
            assert calls == Counter({"new-sql-result": 1})
            assert completed["run_id"] != old["run_id"]
        finally:
            await runner.stop()
    asyncio.run(exercise())
    after = database.snapshot()
    for table, rows in before.items():
        assert all(row in after[table] for row in rows), "historical records must not be replayed or mutated"
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT job,value FROM isolated_scope_results")
        assert cursor.fetchall() == [{"job": "new-sql-result", "value": sum(range(len("new-sql-result") + 1))}]
