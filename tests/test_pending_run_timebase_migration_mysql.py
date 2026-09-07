"""Recover only untouched SQL-default queue times in an owned MySQL database."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_legacy_unknown_scope_migration_mysql import database as legacy_database


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8",
)
MIGRATION = Path(__file__).resolve().parents[1] / "agent/migrations/043_correct_unclaimed_run_timebase.sql"
SNAPSHOT_TABLE = "pending_run_timebase_snapshot_043"


@pytest.fixture
def database(legacy_database):
    database = legacy_database
    # The shared fixture boots the complete real schema. Only this UUID-owned
    # database is rewound to the instant before 043, without changing old history.
    assert database.helper.database.startswith("legacy_scope_")
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM schema_migrations WHERE version='043'")
        cursor.execute(f"DROP TABLE {SNAPSHOT_TABLE}")
    return database


def _connect(database, zone):
    helper = database.helper
    connection = helper.pymysql.connect(
        host=helper.host, port=helper.port, user=helper.user, password=helper.password,
        database=helper.database, charset="utf8mb4", autocommit=True,
        cursorclass=helper.pymysql.cursors.DictCursor,
    )
    with connection.cursor() as cursor:
        cursor.execute("SET SESSION time_zone=%s", (zone,))
    return connection


def _new_run(database, *, zone="+08:00", **changes):
    command_id, item_id, run_id = (str(uuid4()) for _ in range(3))
    with _connect(database, zone) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO agent_commands(command_id,command_type,source,actor_type,
                actor_roles_json,entity_refs_json,parameters_json,idempotency_key,
                correlation_id,requested_at,automation_id,status)
                VALUES(%s,'integration_probe','console','console_admin','[]','[]','{}',
                    %s,%s,UTC_TIMESTAMP(6),%s,'ACCEPTED')""",
            (command_id, command_id, command_id, database.project_id),
        )
        cursor.execute(
            """INSERT INTO work_items(work_item_id,command_id,type,title,source,dedupe_key)
                VALUES(%s,%s,'AUTOMATION','isolated queue timebase','console',%s)""",
            (item_id, command_id, item_id),
        )
        # Actual old SQL defaults, not a mocked clock or the now-fixed writer.
        cursor.execute(
            """INSERT INTO agent_runs(run_id,work_item_id,command_id,run_no,status,
                mode,planner_kind,correlation_id)
                VALUES(%s,%s,%s,1,'RECEIVED','COMMAND','DETERMINISTIC',%s)""",
            (run_id, item_id, command_id, command_id),
        )
        if changes:
            allowed = {"status", "run_no", "retry_of_run_id", "version", "execution_attempt_count",
                "started_at", "finished_at", "worker_id", "lease_expires_at", "cancel_requested_at",
                "plan_json", "plan_hash", "error_code", "retryable", "next_attempt_at", "created_at"}
            assert set(changes) <= allowed
            cursor.execute(
                "UPDATE agent_runs SET " + ",".join(f"{field}=%s" for field in changes) + " WHERE run_id=%s",
                (*changes.values(), run_id),
            )
    return run_id


def _runs(database):
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM agent_runs ORDER BY run_id")
        return {row["run_id"]: row for row in cursor.fetchall()}


def _clock_snapshot(database):
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {SNAPSHOT_TABLE}")
        rows = cursor.fetchall()
        assert len(rows) == 1
        return rows[0]


def _apply(database, zone="+08:00", *, replay_sql=False, statement_limit=None):
    runner = database.helper.runner
    if replay_sql or statement_limit is not None:
        statements = runner.split_sql_statements(MIGRATION.read_text(encoding="utf-8"))
        with _connect(database, zone) as connection, connection.cursor() as cursor:
            for statement in statements[:statement_limit]:
                cursor.execute(statement)
        return
    # Exercise the real checksum/history runner and its autocommit connection,
    # not an imitation of deployment or an environment-dependent default zone.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_connect", lambda: _connect(database, zone))
        assert runner.run(check_only=False) == 0
        assert runner.run(check_only=True) == 0


def _add_step(database, run_id):
    step_id = str(uuid4())
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO agent_run_steps(step_id,run_id,step_key,step_order,tool_name,tool_version,
                operation_type,risk_level,status,idempotency_key)
                VALUES(%s,%s,'existing',1,'integration.read','1.0.0','READ','LOW','PENDING',%s)""",
            (step_id, run_id, step_id),
        )


def _add_lease(database, run_id, *, outcome="FAILED_BEFORE_WRITE"):
    lease_id = str(uuid4())
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO automation_project_generation_leases(lease_id,automation_id,generation,
                orchestration_run_id,lease_owner,runtime_metadata_json,runtime_metadata_sha256,outcome,expires_at)
                VALUES(%s,%s,1,%s,'isolated-test','{}',%s,%s,UTC_TIMESTAMP(6)-INTERVAL 1 DAY)""",
            (lease_id, database.project_id, run_id, "a" * 64, outcome),
        )
    return lease_id


@pytest.mark.parametrize("zone", ("+08:00", "+00:00"))
def test_default_queue_time_is_corrected_only_when_blocked_and_then_claimable(database, zone):
    run_id = _new_run(database, zone=zone)
    before = _runs(database)[run_id]
    assert before["next_attempt_at"] == before["created_at"]
    if zone == "+08:00":
        assert database.repository.claim_runs("before-migration", ("RECEIVED",)) == []
    _apply(database, zone)
    capture = _clock_snapshot(database)
    assert json.loads(capture["candidate_run_ids"]) == ([run_id] if zone == "+08:00" else [])
    expected_offset = int((capture["captured_database_at"] - capture["captured_utc_at"]).total_seconds())
    assert capture["offset_seconds"] == expected_offset
    assert expected_offset == (int(timedelta(hours=8).total_seconds()) if zone == "+08:00" else 0)
    after = _runs(database)[run_id]
    if expected_offset:
        assert after["next_attempt_at"] == before["next_attempt_at"] - timedelta(seconds=expected_offset)
        assert after["version"] == 2
        assert after["updated_at"] >= before["updated_at"]  # SQL ON UPDATE remains database-local.
        assert {k: v for k, v in after.items() if k not in {"next_attempt_at", "version", "updated_at"}} == {
            k: v for k, v in before.items() if k not in {"next_attempt_at", "version", "updated_at"}}
    else:
        assert after == before
    _apply(database, "+00:00", replay_sql=True)
    _apply(database, "+08:00", replay_sql=True)
    assert _clock_snapshot(database) == capture
    assert _runs(database)[run_id] == after
    claimed = database.repository.claim_runs("after-migration", ("RECEIVED",))
    assert [row["run_id"] for row in claimed] == [run_id]
    assert claimed[0]["status"] == "RECEIVED"


def test_execution_history_and_explicit_schedules_are_unchanged(database):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    valid = _new_run(database)
    cases = [
        {"status": status} for status in (
            "CANCELLED", "PARTIAL", "FAILED_TERMINAL", "BLOCKED_DATA", "FAILED_RETRYABLE", "RUNNING")
    ] + [
        {"version": 2}, {"execution_attempt_count": 1}, {"run_no": 2},
        {"retry_of_run_id": valid}, {"started_at": now}, {"finished_at": now},
        {"worker_id": "existing-worker"}, {"lease_expires_at": now - timedelta(days=1)},
        {"worker_id": "live-worker", "lease_expires_at": now + timedelta(hours=1)},
        {"cancel_requested_at": now}, {"plan_json": "{}"}, {"plan_hash": "f" * 64},
        {"error_code": "WRITE_OUTCOME_UNKNOWN"}, {"retryable": True},
        {"next_attempt_at": now + timedelta(days=1)},  # Explicit future UTC schedule.
        {"next_attempt_at": now - timedelta(days=1)},  # Already due, no correction needed.
    ]
    for values in cases:
        _new_run(database, **values)
    step_run = _new_run(database)
    _add_step(database, step_run)
    lease_run = _new_run(database)
    _add_lease(database, lease_run)
    # A receipt may refer to another lease's Run in the schema. The independent
    # receipt check must still exclude its owning Run, even without any Step.
    receipt_run = _new_run(database)
    other_lease = _add_lease(database, step_run, outcome="WRITE_OUTCOME_UNKNOWN")
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO automation_write_attempt_receipts(receipt_id,automation_id,generation,lease_id,
                orchestration_run_id,step_id,request_id,operation,action,argument_sha256,target_ref_sha256,
                target_ref_json,outcome,created_at,updated_at)
                VALUES(%s,%s,1,%s,%s,%s,%s,'integration','write',%s,%s,'{}',
                    'WRITE_OUTCOME_UNKNOWN',NOW(6),NOW(6))""",
            (str(uuid4()), database.project_id, other_lease, receipt_run, str(uuid4()), str(uuid4()), "b" * 64, "c" * 64),
        )
    database.seed(run_status="CANCELLED", keys=["isolated:historical-write"])
    before = database.snapshot()
    _apply(database)
    assert json.loads(_clock_snapshot(database)["candidate_run_ids"]) == [valid]
    after = database.snapshot()
    assert [row for row in after["agent_runs"] if row["run_id"] != valid] == [
        row for row in before["agent_runs"] if row["run_id"] != valid]
    for table in before:
        if table != "agent_runs":
            assert after[table] == before[table]
    assert _runs(database)[valid]["version"] == 2, {
        "run": _runs(database)[valid], "capture": _clock_snapshot(database),
    }


def test_interrupted_capture_reuses_boundary_and_does_not_expand_on_reentry(database):
    original = _new_run(database)
    _apply(database, statement_limit=2)  # DDL and capture committed, UPDATE not yet executed.
    capture = _clock_snapshot(database)
    later = _new_run(database)
    before_later = _runs(database)[later]
    assert before_later["created_at"] > capture["captured_database_at"]
    # This later UTC-session default looks older in the previous +08 wall clock.
    # A timestamp-only cutoff would incorrectly add it during the restarted run.
    later_utc = _new_run(database, zone="+00:00")
    before_later_utc = _runs(database)[later_utc]
    assert capture["captured_utc_at"] < before_later_utc["created_at"] < capture["captured_database_at"]
    _apply(database, "+00:00")  # Same durable capture despite a changed session zone.
    after = _runs(database)
    assert after[original]["version"] == 2
    assert after[later] == before_later
    assert after[later_utc] == before_later_utc
    assert _clock_snapshot(database) == capture
    post_deployment = _new_run(database)
    frozen = _runs(database)
    _apply(database, "+08:00", replay_sql=True)
    assert _runs(database) == frozen
    assert _runs(database)[post_deployment]["version"] == 1


def test_zero_offset_capture_does_not_start_correcting_after_session_zone_changes(database):
    _apply(database, "+00:00", statement_limit=2)
    capture = _clock_snapshot(database)
    run_id = _new_run(database)
    before = _runs(database)[run_id]
    _apply(database, "+08:00")
    assert _runs(database)[run_id] == before
    assert _clock_snapshot(database) == capture
    assert json.loads(capture["candidate_run_ids"]) == []


def test_captured_runs_are_rechecked_before_update(database):
    with_step, with_worker, prior_claim = (_new_run(database) for _ in range(3))
    _apply(database, statement_limit=2)
    assert set(json.loads(_clock_snapshot(database)["candidate_run_ids"])) == {with_step, with_worker, prior_claim}
    _add_step(database, with_step)
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE agent_runs SET worker_id='isolated-worker' WHERE run_id=%s", (with_worker,))
        cursor.execute("UPDATE agent_runs SET version=2 WHERE run_id=%s", (prior_claim,))
    before = _runs(database)
    _apply(database)
    assert _runs(database) == before
