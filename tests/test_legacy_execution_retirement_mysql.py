"""Real migration 046: no replay, no erased errors and no inferred write success."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_legacy_unknown_scope_migration_mysql import database as legacy_database
from tests.test_pending_run_timebase_migration_mysql import _new_run, _runs, _add_lease
from tests.test_pending_run_timebase_migration_mysql import _connect

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL")
MIGRATION = Path(__file__).resolve().parents[1] / "agent/migrations/046_retire_legacy_execution_queue.sql"


@pytest.fixture
def database(legacy_database):
    assert legacy_database.helper.database.startswith("legacy_scope_")
    with legacy_database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM schema_migrations WHERE version='046'")
    return legacy_database


def _apply(database, *, raw=False):
    runner = database.helper.runner
    if raw:
        with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
            for statement in runner.split_sql_statements(MIGRATION.read_text(encoding="utf-8")):
                cursor.execute(statement)
        return
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_connect", lambda: _connect(database, "+00:00"))
        assert runner.run(check_only=False) == 0
        assert runner.run(check_only=True) == 0


def _table(database, name, order):
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {name} ORDER BY {order}")
        return cursor.fetchall()


def _step(database, run_id, *, status="PENDING", operation="READ", attempts=0):
    identity = str(uuid4())
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("""INSERT INTO agent_run_steps(step_id,run_id,step_key,step_order,tool_name,tool_version,
            operation_type,risk_level,status,idempotency_key,attempt_count,error_code,error_summary)
            VALUES(%s,%s,'original',1,'isolated.test','1.0.0',%s,'LOW',%s,%s,%s,'ORIGINAL_ERROR','original detail')""",
            (identity, run_id, operation, status, identity, attempts))
    return identity


def test_cutover_closes_only_resumable_history_once_and_does_not_replay(database):
    states = ('RECEIVED','CONTEXT_READY','PLANNED','VALIDATED','WAITING_APPROVAL',
        'FAILED_RETRYABLE','NEEDS_CLARIFICATION','BLOCKED_LOGIN','BLOCKED_DATA')
    identities = [_new_run(database, status=status, error_code="ORIGINAL_ERROR", retryable=True) for status in states]
    step_id = _step(database, identities[-1])
    terminal_ids = [_new_run(database, status=status) for status in ('COMPLETED','PARTIAL','FAILED_TERMINAL','CANCELLED')]
    unknown = database.seed(run_status="CANCELLED", receipt_outcome="WRITE_OUTCOME_UNKNOWN")
    before = _runs(database)
    receipts = _table(database, 'automation_write_attempt_receipts', 'receipt_id')
    leases = _table(database, 'automation_project_generation_leases', 'lease_id')
    commands = _table(database, 'agent_commands', 'command_id')
    schedules = _table(database, 'scheduled_tasks', 'id')
    outbox = _table(database, 'outbox_events', 'event_id')
    _apply(database)
    after = _runs(database)
    for identity in identities:
        assert after[identity]['status'] == 'CANCELLED' and after[identity]['finished_at']
        assert after[identity]['retryable'] == 0 and after[identity]['error_code'] == 'ORIGINAL_ERROR'
        assert after[identity]['error_summary'] == before[identity]['error_summary']
        assert after[identity]['plan_json'] == before[identity]['plan_json']
    for identity in [*terminal_ids, unknown['run_id']]:
        assert before[identity] == after[identity]
    assert _table(database, 'automation_write_attempt_receipts', 'receipt_id') == receipts
    assert _table(database, 'automation_project_generation_leases', 'lease_id') == leases
    assert _table(database, 'agent_commands', 'command_id') == commands
    assert _table(database, 'scheduled_tasks', 'id') == schedules
    assert _table(database, 'outbox_events', 'event_id') == outbox
    events = [row for row in _table(database, 'domain_events', 'event_id') if row['event_type'] == 'agent.legacy_execution.retired']
    assert {row['run_id'] for row in events} == set(identities)
    assert all(json.loads(row['payload_json'])['reason_code'] == 'LEGACY_EXECUTION_RETIRED' for row in events)
    steps = {row['step_id']: row for row in _table(database, 'agent_run_steps', 'step_id')}
    assert steps[step_id]['status'] == 'CANCELLED' and steps[step_id]['error_summary'] == 'original detail'
    assert database.repository.claim_runs('must-not-replay', states) == []
    _apply(database, raw=True)
    assert _runs(database) == after
    assert [row for row in _table(database, 'domain_events', 'event_id') if row['event_type'] == 'agent.legacy_execution.retired'] == events


@pytest.mark.parametrize('busy_kind', ['running', 'verifying', 'owner', 'generation', 'step', 'invocation', 'unknown', 'unverified_write'])
def test_cutover_preflight_is_atomic_for_any_live_or_unverified_execution(database, busy_kind):
    ordinary = _new_run(database)
    blocked = _new_run(database)
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        if busy_kind in {'running','verifying'}:
            cursor.execute('UPDATE agent_runs SET status=%s WHERE run_id=%s', (busy_kind.upper(), blocked))
        elif busy_kind == 'owner':
            cursor.execute("UPDATE agent_runs SET worker_id='still-owned' WHERE run_id=%s", (blocked,))
        elif busy_kind == 'invocation':
            identity = str(uuid4())
            cursor.execute("""INSERT INTO automation_plugin_invocations(invocation_id,request_key_sha256,request_sha256,
                request_id,operation,source,actor_id,owner_id,status,invocation_json,arguments_json,started_at,updated_at)
                VALUES(%s,%s,%s,%s,'isolated','console','isolated','worker','RUNNING','{}','{}',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                (identity, 'a'*64, 'b'*64, identity))
    if busy_kind == 'generation':
        _add_lease(database, blocked, outcome='RUNNING')  # expired time alone is not proof that I/O stopped
    elif busy_kind == 'step':
        _step(database, blocked, status='VERIFYING', operation='EXTERNAL_WRITE')
    elif busy_kind == 'unknown':
        database.seed(run_status='BLOCKED_DATA', receipt_outcome='WRITE_OUTCOME_UNKNOWN')
    elif busy_kind == 'unverified_write':
        _step(database, blocked, status='COMPLETED', operation='EXTERNAL_WRITE', attempts=1)
    before = _runs(database)
    receipts = _table(database, 'automation_write_attempt_receipts', 'receipt_id')
    with pytest.raises(database.helper.pymysql.Error, match='(?i)cp046_'):
        _apply(database)
    assert _runs(database) == before and _runs(database)[ordinary]['status'] == 'RECEIVED'
    assert _table(database, 'automation_write_attempt_receipts', 'receipt_id') == receipts
    assert not [row for row in _table(database, 'domain_events', 'event_id') if row['event_type'] == 'agent.legacy_execution.retired']
    assert not [row for row in _table(database, 'schema_migrations', 'version') if row['version'] == '046']
