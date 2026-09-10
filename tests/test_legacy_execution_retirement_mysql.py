"""Real migration 046: no replay, no erased errors and no inferred write success."""
from __future__ import annotations

from datetime import datetime, timezone
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


def _step(database, run_id, *, status="PENDING", operation="READ", attempts=0,
          order=1, postcondition_status=None):
    identity = str(uuid4())
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("""INSERT INTO agent_run_steps(step_id,run_id,step_key,step_order,tool_name,tool_version,
            operation_type,risk_level,status,idempotency_key,attempt_count,postcondition_status,error_code,error_summary)
            VALUES(%s,%s,%s,%s,'isolated.test','1.0.0',%s,'LOW',%s,%s,%s,%s,'ORIGINAL_ERROR','original detail')""",
            (identity, run_id, f'original-{order}', order, operation, status, identity, attempts, postcondition_status))
    return identity


@pytest.mark.parametrize('postcondition_status', ['VERIFIED', 'VERIFIED_AFTER_RECOVERY'])
@pytest.mark.parametrize('blocked_status', ['BLOCKED_LOGIN', 'BLOCKED_DATA'])
def test_cutover_preserves_verified_non_plugin_write_before_blocked_read(
    database, postcondition_status, blocked_status,
):
    run_id = _new_run(database, status=blocked_status, error_code='ORIGINAL_ERROR')
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        # Older ordinary business commands do not have plugin write receipts.
        cursor.execute('UPDATE agent_commands SET automation_id=NULL WHERE command_id='
                       '(SELECT command_id FROM agent_runs WHERE run_id=%s)', (run_id,))
    write_step = _step(database, run_id, status='RUNNING', operation='EXTERNAL_WRITE', attempts=1)
    read_step = _step(database, run_id, status=blocked_status, operation='READ', order=2)
    result = {'success': True, 'data': {'record_id': 'isolated-written-record'},
              'evidence': {'verified': True, 'source': 'isolated-independent-readback'}}
    run = _runs(database)[run_id]
    # Match WorkflowRunner's actual successful and recovered result persistence.
    with database.repository.unit_of_work() as uow:
        uow.steps.transition(
            write_step, expected_version=1, expected_statuses=('RUNNING',), status='COMPLETED',
            result_summary=result, postcondition_status=postcondition_status,
            postcondition={'reconciliation': 'APPLIED'} if postcondition_status == 'VERIFIED_AFTER_RECOVERY' else None,
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        uow.evidence.add({
            'evidence_id': str(uuid4()), 'work_item_id': run['work_item_id'],
            'run_id': run_id, 'step_id': write_step, 'source_system': 'isolated',
            'source_record_type': 'write_readback', 'source_record_id': 'isolated-written-record',
            'entity_type': 'isolated_record', 'entity_id': 'isolated-written-record',
            'completeness_status': 'COMPLETE', 'record_count': 1,
            'summary': result['evidence'],
        })
        uow.commit()
    steps_before = {row['step_id']: row for row in _table(database, 'agent_run_steps', 'step_id')}
    evidence_before = _table(database, 'evidence_records', 'evidence_id')
    receipts_before = _table(database, 'automation_write_attempt_receipts', 'receipt_id')
    assert len([row for row in evidence_before if row['step_id'] == write_step]) == 1
    assert not [row for row in receipts_before if row['orchestration_run_id'] == run_id]

    _apply(database)

    after = _runs(database)[run_id]
    assert after['status'] == 'CANCELLED' and after['error_code'] == run['error_code']
    assert after['plan_json'] == run['plan_json']
    steps_after = {row['step_id']: row for row in _table(database, 'agent_run_steps', 'step_id')}
    assert steps_after[write_step] == steps_before[write_step]
    assert json.loads(steps_after[write_step]['result_summary_json']) == result
    assert steps_after[read_step]['status'] == 'CANCELLED'
    assert _table(database, 'evidence_records', 'evidence_id') == evidence_before
    assert _table(database, 'automation_write_attempt_receipts', 'receipt_id') == receipts_before


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


@pytest.mark.parametrize('busy_kind', [
    'running', 'verifying', 'owner', 'generation', 'step', 'invocation', 'unknown', 'unverified_write',
    'failed_postcondition', 'incorrect_passed_postcondition', 'verified_incomplete_step',
    'verified_step_unknown_receipt', 'verified_step_started_receipt', 'verified_step_unknown_run',
])
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
    elif busy_kind in {'failed_postcondition', 'incorrect_passed_postcondition', 'verified_incomplete_step'}:
        _step(database, blocked, status='BLOCKED_DATA' if busy_kind == 'verified_incomplete_step' else 'COMPLETED',
              operation='EXTERNAL_WRITE', attempts=1,
              postcondition_status={'failed_postcondition': 'FAILED', 'incorrect_passed_postcondition': 'passed',
                                    'verified_incomplete_step': 'VERIFIED'}[busy_kind])
    elif busy_kind in {'verified_step_unknown_receipt', 'verified_step_started_receipt'}:
        seeded = database.seed(run_status='BLOCKED_DATA', step_status='COMPLETED',
            receipt_outcome='STARTED' if busy_kind == 'verified_step_started_receipt' else 'WRITE_OUTCOME_UNKNOWN')
        with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_run_steps SET attempt_count=1,postcondition_status='VERIFIED' WHERE step_id=%s",
                           (seeded['step_id'],))
    elif busy_kind == 'verified_step_unknown_run':
        _step(database, blocked, status='COMPLETED', operation='EXTERNAL_WRITE', attempts=1,
              postcondition_status='VERIFIED_AFTER_RECOVERY')
        with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_runs SET error_code='WRITE_OUTCOME_UNKNOWN' WHERE run_id=%s", (blocked,))
    before = _runs(database)
    steps = _table(database, 'agent_run_steps', 'step_id')
    evidence = _table(database, 'evidence_records', 'evidence_id')
    receipts = _table(database, 'automation_write_attempt_receipts', 'receipt_id')
    with pytest.raises(database.helper.pymysql.Error, match='(?i)cp046_'):
        _apply(database)
    assert _runs(database) == before and _runs(database)[ordinary]['status'] == 'RECEIVED'
    assert _table(database, 'agent_run_steps', 'step_id') == steps
    assert _table(database, 'evidence_records', 'evidence_id') == evidence
    assert _table(database, 'automation_write_attempt_receipts', 'receipt_id') == receipts
    assert not [row for row in _table(database, 'domain_events', 'event_id') if row['event_type'] == 'agent.legacy_execution.retired']
    assert not [row for row in _table(database, 'schema_migrations', 'version') if row['version'] == '046']
