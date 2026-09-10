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


@pytest.mark.parametrize('parent_status', ['COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED'])
@pytest.mark.parametrize('step_status', ['RUNNING', 'VERIFYING'])
def test_cutover_preserves_stale_steps_of_fully_finished_unowned_parent(database, parent_status, step_status):
    ordinary = _new_run(database)
    finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for receipt_outcome in ('WRITE_VERIFIED', 'WRITE_OUTCOME_UNKNOWN', 'STARTED'):
        seeded = database.seed(
            run_status=parent_status, step_status=step_status, receipt_outcome=receipt_outcome,
            lease_outcome='WRITE_VERIFIED' if receipt_outcome == 'WRITE_VERIFIED' else 'WRITE_OUTCOME_UNKNOWN',
        )
        with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute('UPDATE agent_runs SET finished_at=%s,error_code=%s WHERE run_id=%s',
                (finished_at, 'WRITE_OUTCOME_UNKNOWN', seeded['run_id']))
            cursor.execute("""UPDATE agent_run_steps SET attempt_count=1,started_at=%s,
                result_summary_json=%s,postcondition_json=%s,error_code='ORIGINAL_STEP_ERROR'
                WHERE step_id=%s""", (finished_at, json.dumps({'partial_result': 'preserve without claiming success'}),
                    json.dumps({'unresolved': True}), seeded['step_id']))
    # Older ordinary writes may have no plugin receipt. Parent completion is
    # execution ownership evidence, not evidence that any of these writes passed.
    no_receipt = _new_run(database, status=parent_status, finished_at=finished_at)
    _step(database, no_receipt, status=step_status, operation='INTERNAL_PROJECTION_WRITE', attempts=1)
    _step(database, no_receipt, status=step_status, operation='READ', attempts=1, order=2)
    with database.repository.unit_of_work() as uow:
        run = uow.runs.get(seeded['run_id'])
        uow.evidence.add({
            'evidence_id': str(uuid4()), 'work_item_id': run['work_item_id'],
            'run_id': seeded['run_id'], 'step_id': seeded['step_id'], 'source_system': 'isolated',
            'source_record_type': 'partial_write_observation', 'source_record_id': 'isolated-observation',
            'entity_type': 'isolated_record', 'entity_id': 'isolated-observation',
            'completeness_status': 'INCOMPLETE', 'record_count': 1, 'summary': {'unresolved': True},
        })
        uow.commit()
    before = database.snapshot()
    evidence = _table(database, 'evidence_records', 'evidence_id')
    outbox = _table(database, 'outbox_events', 'event_id')

    _apply(database)

    after = database.snapshot()
    assert _runs(database)[ordinary]['status'] == 'CANCELLED'
    for table in before:
        if table == 'agent_runs':
            assert [row for row in after[table] if row['run_id'] != ordinary] == [
                row for row in before[table] if row['run_id'] != ordinary]
        else:
            assert after[table] == before[table]
    assert _table(database, 'evidence_records', 'evidence_id') == evidence
    assert _table(database, 'outbox_events', 'event_id') == outbox
    events = [row for row in _table(database, 'domain_events', 'event_id') if row['event_type'] == 'agent.legacy_execution.retired']
    assert [row['run_id'] for row in events] == [ordinary]
    _apply(database, raw=True)
    assert database.snapshot() == after


@pytest.mark.parametrize('missing_proof', ['finished_at', 'worker', 'expired_lease', 'active_generation'])
def test_stale_step_exclusion_requires_complete_parent_termination_proof(database, missing_proof):
    ordinary = _new_run(database)
    changes = {'finished_at': datetime.now(timezone.utc).replace(tzinfo=None)}
    if missing_proof == 'finished_at':
        changes['finished_at'] = None
    elif missing_proof == 'worker':
        changes['worker_id'] = 'still-owned'
    elif missing_proof == 'expired_lease':
        changes['lease_expires_at'] = datetime(2000, 1, 1)
    historical = _new_run(database, status='FAILED_TERMINAL', **changes)
    _step(database, historical, status='RUNNING', operation='INTERNAL_PROJECTION_WRITE', attempts=1)
    if missing_proof == 'active_generation':
        _add_lease(database, historical, outcome='RUNNING')
    before = database.snapshot()
    with pytest.raises(database.helper.pymysql.Error, match='(?i)cp046_execution_not_quiescent'):
        _apply(database)
    assert database.snapshot() == before
    assert _runs(database)[ordinary]['status'] == 'RECEIVED'
    assert not [row for row in _table(database, 'schema_migrations', 'version') if row['version'] == '046']


@pytest.mark.parametrize('busy_kind', [
    'running', 'verifying', 'owner', 'expired_run_lease', 'generation', 'step', 'invocation', 'unknown', 'unverified_write',
    'failed_postcondition', 'incorrect_passed_postcondition', 'verified_incomplete_step',
    'verified_step_unknown_receipt', 'verified_step_started_receipt', 'verified_step_unknown_run',
])
def test_cutover_classifies_stopped_uncertainty_but_atomically_rejects_live_execution(database, busy_kind):
    ordinary = _new_run(database)
    blocked = _new_run(database)
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        if busy_kind in {'running','verifying'}:
            cursor.execute('UPDATE agent_runs SET status=%s WHERE run_id=%s', (busy_kind.upper(), blocked))
        elif busy_kind == 'owner':
            cursor.execute("UPDATE agent_runs SET worker_id='still-owned' WHERE run_id=%s", (blocked,))
        elif busy_kind == 'expired_run_lease':
            cursor.execute('UPDATE agent_runs SET lease_expires_at=%s WHERE run_id=%s', (datetime(2000, 1, 1), blocked))
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
        blocked = database.seed(run_status='BLOCKED_DATA', receipt_outcome='WRITE_OUTCOME_UNKNOWN')['run_id']
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
        blocked = seeded['run_id']
        with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_run_steps SET attempt_count=1,postcondition_status='VERIFIED' WHERE step_id=%s",
                           (seeded['step_id'],))
    elif busy_kind == 'verified_step_unknown_run':
        _step(database, blocked, status='COMPLETED', operation='EXTERNAL_WRITE', attempts=1,
              postcondition_status='VERIFIED_AFTER_RECOVERY')
        with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE agent_runs SET error_code='WRITE_OUTCOME_UNKNOWN' WHERE run_id=%s", (blocked,))
    uncertain_kinds = {
        'unknown': 'WRITE_RECEIPT_UNCONFIRMED',
        'unverified_write': 'ATTEMPTED_WRITE_WITHOUT_VERIFICATION',
        'failed_postcondition': 'ATTEMPTED_WRITE_WITHOUT_VERIFICATION',
        'incorrect_passed_postcondition': 'ATTEMPTED_WRITE_WITHOUT_VERIFICATION',
        'verified_incomplete_step': 'ATTEMPTED_WRITE_WITHOUT_VERIFICATION',
        'verified_step_unknown_receipt': 'WRITE_RECEIPT_UNCONFIRMED',
        'verified_step_started_receipt': 'WRITE_RECEIPT_UNCONFIRMED',
        'verified_step_unknown_run': 'RUN_WRITE_OUTCOME_UNCONFIRMED',
    }
    if busy_kind not in uncertain_kinds:
        # A live execution also blocks retirement of unrelated stopped unknown
        # writes; classification must not leave any partial migration behind.
        database.seed(run_status='BLOCKED_DATA', receipt_outcome='WRITE_OUTCOME_UNKNOWN')
    before = _runs(database)
    steps = _table(database, 'agent_run_steps', 'step_id')
    evidence = _table(database, 'evidence_records', 'evidence_id')
    receipts = _table(database, 'automation_write_attempt_receipts', 'receipt_id')
    leases = _table(database, 'automation_project_generation_leases', 'lease_id')
    commands = _table(database, 'agent_commands', 'command_id')
    outbox = _table(database, 'outbox_events', 'event_id')
    if busy_kind in uncertain_kinds:
        _apply(database)
        after = _runs(database)
        assert after[ordinary]['status'] == 'CANCELLED'
        assert after[blocked]['status'] == 'FAILED_TERMINAL' and after[blocked]['finished_at']
        assert not after[blocked]['retryable']
        for field in before[blocked]:
            if field not in {'status', 'finished_at', 'version', 'updated_at', 'retryable'}:
                assert after[blocked][field] == before[blocked][field]
        after_steps = {row['step_id']: row for row in _table(database, 'agent_run_steps', 'step_id')}
        for step in steps:
            updated = after_steps[step['step_id']]
            if step['run_id'] == blocked and step['status'] in {'PENDING', 'WAITING_APPROVAL', 'BLOCKED_LOGIN', 'BLOCKED_DATA', 'FAILED_RETRYABLE'}:
                assert updated['status'] == 'FAILED_TERMINAL' and updated['finished_at']
                for field in step:
                    if field not in {'status', 'finished_at', 'version', 'updated_at'}:
                        assert updated[field] == step[field]
            else:
                assert updated == step
        assert _table(database, 'evidence_records', 'evidence_id') == evidence
        assert _table(database, 'automation_write_attempt_receipts', 'receipt_id') == receipts
        assert _table(database, 'automation_project_generation_leases', 'lease_id') == leases
        assert _table(database, 'agent_commands', 'command_id') == commands
        assert _table(database, 'outbox_events', 'event_id') == outbox
        retired = [row for row in _table(database, 'domain_events', 'event_id')
                   if row['event_type'] == 'agent.legacy_execution.retired' and row['run_id'] == blocked]
        assert len(retired) == 1
        payload = json.loads(retired[0]['payload_json'])
        assert payload['execution_retired'] is True and payload['write_outcome_unconfirmed'] is True
        assert payload['to'] == 'FAILED_TERMINAL' and payload['from'] == before[blocked]['status']
        assert payload['original_error_code'] == before[blocked]['error_code']
        assert uncertain_kinds[busy_kind] in payload['unconfirmed_reasons']
        assert database.repository.claim_runs('must-not-replay', ('BLOCKED_DATA', 'RECEIVED', 'FAILED_RETRYABLE')) == []
        snapshot = database.snapshot()
        _apply(database, raw=True)
        assert database.snapshot() == snapshot
        assert [row for row in _table(database, 'domain_events', 'event_id')
                if row['event_type'] == 'agent.legacy_execution.retired' and row['run_id'] == blocked] == retired
        return
    with pytest.raises(database.helper.pymysql.Error, match='(?i)cp046_'):
        _apply(database)
    assert _runs(database) == before and _runs(database)[ordinary]['status'] == 'RECEIVED'
    assert _table(database, 'agent_run_steps', 'step_id') == steps
    assert _table(database, 'evidence_records', 'evidence_id') == evidence
    assert _table(database, 'automation_write_attempt_receipts', 'receipt_id') == receipts
    assert _table(database, 'automation_project_generation_leases', 'lease_id') == leases
    assert _table(database, 'agent_commands', 'command_id') == commands
    assert _table(database, 'outbox_events', 'event_id') == outbox
    assert not [row for row in _table(database, 'domain_events', 'event_id') if row['event_type'] == 'agent.legacy_execution.retired']
    assert not [row for row in _table(database, 'schema_migrations', 'version') if row['version'] == '046']
