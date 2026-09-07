"""Cross-instance physical/account protection using actual original receipts."""
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
    assert stopped['status'] == 'BLOCKED_DATA', stopped
    assert boundary.sheet_writes
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT execution_resource_keys_json FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s AND outcome='WRITE_OUTCOME_UNKNOWN'", (stopped['run_id'],))
        journals = [json.loads(row['execution_resource_keys_json']) for row in cursor.fetchall()]
    assert journals and all(journal for journal in journals)
    original = unknown_execution_keys(management.repository)
    assert any(key[0] == 'physical-write' for key in original), original
    # Historical receipts without original keys must fail explicitly. This is
    # an isolated persistence fault, restored before exercising normal scopes.
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT receipt_id,execution_resource_keys_json FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s AND outcome='WRITE_OUTCOME_UNKNOWN' ORDER BY receipt_id", (stopped['run_id'],))
        saved = cursor.fetchone()
        cursor.execute('UPDATE automation_write_attempt_receipts SET execution_resource_keys_json=NULL WHERE receipt_id=%s', (saved['receipt_id'],))
        connection.commit()
    try:
        try:
            unknown_execution_keys(management.repository)
        except ValueError as error:
            assert str(error) == 'UNKNOWN_WRITE_SCOPE_UNAVAILABLE:' + saved['receipt_id']
        else:
            raise AssertionError('historical unknown write silently guessed its scope')
    finally:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute('UPDATE automation_write_attempt_receipts SET execution_resource_keys_json=%s WHERE receipt_id=%s', (saved['execution_resource_keys_json'], saved['receipt_id']))
            connection.commit()
    physical = boundary.resources['phase7.arrive_primary_sheet']
    boundary._resource('synthetic-different-role-alias', {key: value for key, value in physical.items() if key != '_meta'})
    boundary._resource('synthetic-independent-sheet', {'resource_kind': 'feishu_sheet', 'spreadsheet_token': 'independent-workbook', 'sheet_id': 'independent'})
    continued = scan_recovery.invoke(management, runner, automation_id=unrelated)
    assert continued['status'] == 'COMPLETED'
    asyncio.run_coroutine_threadsafe(runner.runner.stop(), runner.loop).result(timeout=20)
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
        for number in range(20):
            try:
                release = await other._acquire_execution_slot(step, plan, capability('synthetic-different-role-alias'))
            except _ResourceWait:
                pass
            else:
                release()
                raise AssertionError('different credentials/role/plugin bypassed the original physical sheet')
            release = await other._acquire_execution_slot(step, plan, capability('synthetic-independent-sheet'))
            try:
                workload.execute({'job': 'independent-scope'})
            finally:
                release()
            same_account = replace(step, account_id='v32-daily-account', arguments={**step.arguments, 'account_id': 'v32-daily-account'})
            try:
                release = await other._acquire_execution_slot(same_account, plan, {})
            except _ResourceWait:
                pass
            else:
                release()
                raise AssertionError('same physical account bypassed the original unknown write')
            checks.append({'round': number, 'alias_blocked': True, 'same_account_blocked': True, 'independent_registered_workload_calls': workload.calls['independent-scope']})
        await other.stop()
        return checks
    evidence = asyncio.run(check())
    return {'status': 'PASS', 'original_run': stopped, 'original_nonempty_journals': len(journals),
        'scope': 'Actual original MySQL receipts; second Runner production resource admission with controlled metadata and registered local workloads. Installed daily-plugin concurrency is covered separately by daily_concurrency.',
        'missing_original_scope_rejected': True,
        'original_keys_sha256': sha256(json.dumps(original, sort_keys=True).encode()).hexdigest(),
        'cross_runner_checks': evidence, 'independent_real_run': continued, 'sheet_side_effects': len(boundary.sheet_writes)}
