"""Stopped history is independent; live leases retain exact write protection."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json

from agent.orchestration.models import OperationType
from agent.orchestration.workflow_runner import _ResourceWait
from shared.execution_resource_journal import unknown_execution_keys
from tests.test_workflow_runner_durable_admission import _runner, _Workload, _command
from tests.v32_acceptance import scan_recovery


def exercise(management, runner, boundary, unrelated, connection_factory):
    boundary.mode = 'SUCCESS'
    preview = scan_recovery.invoke(management, runner)
    actual_scan = scan_recovery.invoke(management, runner, preview_run_id=preview['run_id'])
    assert actual_scan['status'] == 'COMPLETED'
    boundary.mode = 'SHEET_UNKNOWN'
    stopped = scan_recovery.invoke(management, runner, automation_id='arrival_stats')
    assert stopped['status'] == 'FAILED_TERMINAL', stopped
    assert boundary.sheet_writes
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT execution_resource_keys_json FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s AND outcome='WRITE_OUTCOME_UNKNOWN'", (stopped['run_id'],))
        journals = [json.loads(row['execution_resource_keys_json']) for row in cursor.fetchall()]
    assert journals and all(journal for journal in journals)
    assert unknown_execution_keys(management.repository) == ()
    # Missing scope on stopped history must not manufacture a global lock.
    # The exact same missing scope still fails while a fixture lease is live.
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT receipt_id,execution_resource_keys_json,updated_at FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s AND outcome='WRITE_OUTCOME_UNKNOWN' ORDER BY receipt_id", (stopped['run_id'],))
        saved = cursor.fetchone()
        cursor.execute('UPDATE automation_write_attempt_receipts SET execution_resource_keys_json=NULL WHERE receipt_id=%s', (saved['receipt_id'],))
        connection.commit()
    try:
        assert unknown_execution_keys(management.repository) == ()
    finally:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute('UPDATE automation_write_attempt_receipts SET execution_resource_keys_json=%s,updated_at=%s WHERE receipt_id=%s', (saved['execution_resource_keys_json'], saved['updated_at'], saved['receipt_id']))
            connection.commit()
    physical = boundary.resources['phase7.arrive_primary_sheet']
    boundary._resource('synthetic-different-role-alias', {key: value for key, value in physical.items() if key != '_meta'})
    boundary._resource('synthetic-independent-sheet', {'resource_kind': 'feishu_sheet', 'spreadsheet_token': 'independent-workbook', 'sheet_id': 'independent'})
    boundary.mode = 'SUCCESS'
    continued = scan_recovery.invoke(management, runner, automation_id=unrelated)
    assert continued['status'] == 'COMPLETED'
    # Start a new external-ledger scenario for this independent scan. The
    # fixture has one fixed child code; replaying it inside the readback time
    # window would correctly be rejected as two matching server records.
    # Keep all actual control-plane history and unknown receipts untouched.
    with boundary.lock:
        boundary.ledger.clear()
    fresh_preview = scan_recovery.invoke(management, runner)
    fresh_scan = scan_recovery.invoke(management, runner, preview_run_id=fresh_preview['run_id'])
    assert fresh_scan['status'] == 'COMPLETED', fresh_scan
    assert fresh_scan['run_id'] != actual_scan['run_id']
    asyncio.run_coroutine_threadsafe(runner.runner.stop(), runner.loop).result(timeout=20)

    # This is an isolated execution-lease fixture, not a recovery of the old
    # business Run. It lets a second actual Runner exercise the live boundary.
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT worker_id,lease_expires_at,updated_at FROM agent_runs WHERE run_id=%s', (stopped['run_id'],))
        saved_run = cursor.fetchone()
        cursor.execute('SELECT * FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s ORDER BY receipt_id', (stopped['run_id'],))
        saved_receipts = cursor.fetchall()

    def fixture_lease(live):
        with connection_factory() as connection, connection.cursor() as cursor:
            if live:
                cursor.execute("UPDATE agent_runs SET worker_id='isolated-scope-probe',lease_expires_at=UTC_TIMESTAMP(6)+INTERVAL 1 DAY WHERE run_id=%s", (stopped['run_id'],))
            else:
                cursor.execute('UPDATE agent_runs SET worker_id=%s,lease_expires_at=%s,updated_at=%s WHERE run_id=%s',
                               (saved_run['worker_id'], saved_run['lease_expires_at'], saved_run['updated_at'], stopped['run_id']))
            connection.commit()

    async def check():
        workload = _Workload()
        other = _runner(management.repository, workload)
        other._saved_resource_provider = boundary.resource_loader
        await other.start()
        command = _command('cross-instance-scope', account='different-account')
        plan = other._planner.plan(command, other._context_builder.build(command))
        step = replace(plan.steps[0], operation_type=OperationType.EXTERNAL_WRITE)
        def capability(resource):
            return {'_plugin_runtime': {'plugin_id': 'different-installed-plugin', 'account_bindings': {'account_id': ['different-account']},
                'resource_bindings': {'different_role': resource}, 'runtime_permissions': {'browser': False,
                    'broker_operations': [{'operation': 'network.request', 'action': 'feishu.sheet.replace', 'effect': 'write', 'roles': ['different_role']}]}}}
        checks = []
        original = ()
        try:
            fixture_lease(True)
            original = unknown_execution_keys(management.repository)
            assert any(key[0] == 'physical-write' for key in original), original
            with connection_factory() as connection, connection.cursor() as cursor:
                cursor.execute('UPDATE automation_write_attempt_receipts SET execution_resource_keys_json=NULL WHERE receipt_id=%s', (saved['receipt_id'],))
                connection.commit()
            try:
                try:
                    unknown_execution_keys(management.repository)
                except ValueError as error:
                    assert str(error) == 'UNKNOWN_WRITE_SCOPE_UNAVAILABLE:' + saved['receipt_id']
                else:
                    raise AssertionError('live unknown write silently guessed its scope')
            finally:
                with connection_factory() as connection, connection.cursor() as cursor:
                    cursor.execute('UPDATE automation_write_attempt_receipts SET execution_resource_keys_json=%s,updated_at=%s WHERE receipt_id=%s', (saved['execution_resource_keys_json'], saved['updated_at'], saved['receipt_id']))
                    connection.commit()
            for number in range(20):
                same_account = replace(step, account_id='v32-daily-account', arguments={**step.arguments, 'account_id': 'v32-daily-account'})
                for target_step, target_capability in (
                    (step, capability('synthetic-different-role-alias')), (same_account, {}),
                ):
                    try:
                        release = await other._acquire_execution_slot(target_step, plan, target_capability)
                    except _ResourceWait:
                        pass
                    else:
                        release()
                        raise AssertionError('an actual live execution lease lost write protection')
                release = await other._acquire_execution_slot(step, plan, capability('synthetic-independent-sheet'))
                try:
                    workload.execute({'job': 'independent-scope'})
                finally:
                    release()
                fixture_lease(False)
                assert unknown_execution_keys(management.repository) == ()
                for target_step, target_capability in (
                    (step, capability('synthetic-different-role-alias')), (same_account, {}),
                ):
                    release = await other._acquire_execution_slot(target_step, plan, target_capability)
                    try:
                        workload.execute({'job': 'stopped-history-independent'})
                    finally:
                        release()
                fixture_lease(True)
                checks.append({'round': number, 'live_alias_blocked': True, 'live_same_account_blocked': True,
                               'stopped_alias_accepted': True, 'stopped_same_account_accepted': True,
                               'independent_registered_workload_calls': workload.calls['independent-scope'],
                               'stopped_history_workload_calls': workload.calls['stopped-history-independent']})
            return checks, original
        finally:
            fixture_lease(False)
            await other.stop()
    evidence, original = asyncio.run(check())
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT * FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s ORDER BY receipt_id', (stopped['run_id'],))
        after_receipts = cursor.fetchall()
    assert saved_receipts == after_receipts
    return {'status': 'PASS', 'original_run': stopped, 'original_nonempty_journals': len(journals),
        'scope': 'Actual installed scan/statistics plugins and original MySQL receipts; second Runner resource admission with an explicit isolated live-lease fixture and registered local workloads.',
        'stopped_history_missing_scope_ignored': True, 'live_missing_original_scope_rejected': True,
        'original_keys_sha256': sha256(json.dumps(original, sort_keys=True).encode()).hexdigest(),
        'cross_runner_checks': evidence, 'independent_real_run': continued, 'fresh_scan_after_stopped_history': fresh_scan,
        'historical_receipts_unchanged': True, 'sheet_side_effects': len(boundary.sheet_writes)}
