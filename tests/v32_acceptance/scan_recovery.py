"""Actual scan receipt/Runner recovery with a lost external write response."""
from datetime import datetime, timezone
import argparse
import asyncio
from functools import partial
import json
import secrets
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.package import Ed25519TrustStore
from agent.orchestration.context_builder import ContextBuilder
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.first_party import build_production_first_party_core_handler_map, recover_scan_codes_unknown_write
from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, DailyAccounts
from tests.v32_acceptance.daily_stats_protocol import DailyStatsProtocol
from tests.v32_acceptance.daily_scan import ACTOR, ROOT, connect, prepare_database, setup_scan, signed_request, wait_result
from tests.v32_acceptance.first_party_fixture import bootstrap, isolated_migration_accounts
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.runner_fixture import RunnerFixture
from tests.v32_acceptance.scan_preview_observation import checkpoint_case, observe_preview_timestamps
from tests.v32_acceptance.unrelated_fixture import install_unrelated

DATABASE = 'v32_recovery_test'
connection_factory = partial(connect, database=DATABASE)


class LostReplyProtocol(DailyStatsProtocol):
    """Only the external response fails after the real server-side commit."""
    mode = 'APPLIED'

    def __init__(self):
        super().__init__()
        self.write_started = threading.Event()
        self.write_release = threading.Event()

    def handle(self, path, query, arguments):
        if path == '/dataOperation/saveTables' and self.mode == 'NOT_APPLIED':
            return {'success': False, 'message': 'isolated request was not committed'}
        result = super().handle(path, query, arguments)
        if path == '/dataOperation/saveTables' and self.mode == 'CANCEL':
            self.write_started.set()
            if not self.write_release.wait(15):
                raise TimeoutError('isolated cancellation boundary deadline')
        if path == '/dataOperation/saveTables' and self.mode != 'SUCCESS':
            return {'success': False, 'message': 'isolated response unavailable after commit'}
        if query.get('id') == ['FIND_SEND_SCAN_RECORD'] and self.mode == 'UNKNOWN':
            return {'data': [], 'total': 1}
        return result

    def sheet_request(self, action, params):
        result = super().sheet_request(action, params)
        if self.mode == 'SHEET_UNKNOWN' and action in {'write_sheet', 'clear_sheet'}:
            return {'ok': False, 'code': 503, 'error': 'isolated successful sheet response unavailable'}
        return result


def invoke(management, runner, *, preview_run_id=None, automation_id='scan_codes'):
    payload = {'request_id': str(uuid4())}
    if preview_run_id:
        payload['preview_run_id'] = preview_run_id
    receipt = signed_request(management, '/internal/v1/automation-projects/' + automation_id + '/invoke', payload=payload)
    runner.runner.wake(receipt['run_id'])
    return wait_result(receipt['run_id'], connection_factory=connection_factory, allow_blocked=True)


def recover(management):
    return recover_scan_codes_unknown_write(SimpleNamespace(catalog=management.catalog, target_service=management.targets), 'scan_codes', str(uuid4()))


def scan_run_ids():
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT r.run_id FROM agent_runs r JOIN agent_commands c ON c.command_id=r.command_id WHERE c.automation_id='scan_codes'")
        return {row['run_id'] for row in cursor.fetchall()}


def unstarted_confirmation(run_id):
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT status,started_at FROM agent_run_steps WHERE run_id=%s ORDER BY step_order', (run_id,))
        steps = cursor.fetchall()
        cursor.execute('SELECT COUNT(*) AS count FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s', (run_id,))
        receipt_count = cursor.fetchone()['count']
    assert len(steps) == 1 and steps[0]['status'] == 'PENDING' and steps[0]['started_at'] is None, steps
    assert receipt_count == 0, {'run_id': run_id, 'write_receipt_count': receipt_count}
    return {'steps': steps, 'write_receipt_count': receipt_count}


def run_case(management, runner, boundary, unrelated, *, mode, round_number):
    boundary.mode = mode
    boundary.ledger.clear()
    preview = invoke(management, runner)
    assert preview['status'] == 'COMPLETED', preview
    assert len(boundary.ledger) == 0
    blocked = invoke(management, runner, preview_run_id=preview['run_id'])
    assert blocked['status'] == 'BLOCKED_DATA', blocked
    expected_writes = 0 if mode == 'NOT_APPLIED' else 1
    assert len(boundary.ledger) == expected_writes
    independent = invoke(management, runner, automation_id=unrelated)
    assert independent['status'] == 'COMPLETED', independent
    if mode == 'UNKNOWN':
        assert recover(management) is None
        result = wait_result(blocked['run_id'], connection_factory=connection_factory, allow_blocked=True)
        assert result['status'] == 'BLOCKED_DATA'
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute('SELECT outcome FROM automation_project_generation_leases WHERE orchestration_run_id=%s', (blocked['run_id'],))
            assert {row['outcome'] for row in cursor.fetchall()} == {'WRITE_OUTCOME_UNKNOWN'}
        fresh_preview = invoke(management, runner)
        assert fresh_preview['status'] == 'COMPLETED' and len(boundary.ledger) == expected_writes
        previous_run_ids = scan_run_ids()
        blocked_confirmations = []
        accepted_confirmations = []
        for _ in range(20):
            try:
                receipt = signed_request(management, '/internal/v1/automation-projects/scan_codes/invoke',
                    payload={'request_id': str(uuid4()), 'preview_run_id': fresh_preview['run_id']})
            except AssertionError as error:
                response = json.loads(str(error)[str(error).index('{'):])
                code = response['error']['code']
                if code == 'SCAN_PREVIEW_ALREADY_CONSUMED':
                    assert accepted_confirmations, response
                else:
                    assert code == 'AUTOMATION_ALREADY_RUNNING' and response['data']['blocking_kind'] == 'UNKNOWN_WRITE', response
                blocked_confirmations.append({'error_code': code, 'response': response})
            else:
                runner.runner.wake(receipt['run_id'])
                protected = wait_result(receipt['run_id'], connection_factory=connection_factory,
                    allow_blocked=True, allow_resource_wait=True)
                assert protected['status'] == 'RUNNING' and protected['error_code'] == 'RESOURCE_WAIT', protected
                assert protected['error_summary'] == 'execution resource has an unresolved external write', protected
                before_cancel = unstarted_confirmation(receipt['run_id'])
                assert len(boundary.ledger) == expected_writes
                while_waiting = invoke(management, runner, automation_id=unrelated)
                assert while_waiting['status'] == 'COMPLETED', while_waiting
                cancellation_started = time.monotonic()
                cancellation = asyncio.run_coroutine_threadsafe(
                    runner.control_plane.cancel_run(receipt['run_id'], actor=ACTOR), runner.loop).result(timeout=2)
                cancelled = wait_result(receipt['run_id'], connection_factory=connection_factory)
                cancellation_seconds = time.monotonic() - cancellation_started
                assert cancelled['status'] == 'CANCELLED' and cancellation_seconds <= 2, (cancelled, cancellation_seconds)
                after_cancel = unstarted_confirmation(receipt['run_id'])
                assert len(boundary.ledger) == expected_writes
                confirmation = {'protected': protected, 'before_cancel': before_cancel,
                    'independent_while_waiting': while_waiting, 'cancellation': cancellation,
                    'cancelled': cancelled, 'after_cancel': after_cancel, 'cancellation_seconds': cancellation_seconds}
                accepted_confirmations.append(confirmation)
                blocked_confirmations.append(confirmation)
        assert len(boundary.ledger) == expected_writes and len(blocked_confirmations) == 20
        assert len(accepted_confirmations) == 1, accepted_confirmations
        assert scan_run_ids() - previous_run_ids == {item['protected']['run_id'] for item in accepted_confirmations}
        # The source becomes reachable; re-read it rather than guessing success.
        boundary.mode = 'APPLIED'
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT snapshot_date,owner_run_id,owner_lease_id FROM automation_scan_snapshot_heads')
        head = cursor.fetchone()
        assert head['owner_run_id'] == blocked['run_id'], head
        if mode != 'NOT_APPLIED':
            cursor.execute('DELETE FROM scan_codes WHERE snapshot_date=%s', (head['snapshot_date'],))
        connection.commit()
    resolution = recover(management)
    assert resolution is not None, 'trusted production readback did not resolve the original Run'
    runner.runner.wake(blocked['run_id'])
    result = wait_result(blocked['run_id'], connection_factory=connection_factory, allow_blocked=True)
    assert result['status'] == ('FAILED_TERMINAL' if mode == 'NOT_APPLIED' else 'COMPLETED'), result
    assert len(boundary.ledger) == expected_writes
    # Retrying the management observation cannot replay the business write.
    assert recover(management) is None
    assert len(boundary.ledger) == expected_writes
    if mode == 'UNKNOWN':
        for confirmation in accepted_confirmations:
            run_id = confirmation['protected']['run_id']
            settled = wait_result(run_id, connection_factory=connection_factory)
            assert settled['status'] == 'CANCELLED', settled
            confirmation['after_original_recovery'] = {'run': settled, **unstarted_confirmation(run_id)}
        assert scan_run_ids() - previous_run_ids == {item['protected']['run_id'] for item in accepted_confirmations}
        assert len(boundary.ledger) == expected_writes
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT COUNT(*) AS count FROM scan_codes WHERE snapshot_date=%s', (head['snapshot_date'],))
        restored_count = cursor.fetchone()['count']
    assert restored_count == 1
    safe_retry = None
    if mode == 'NOT_APPLIED':
        boundary.mode = 'SUCCESS'
        retry_preview = invoke(management, runner)
        assert retry_preview['status'] == 'COMPLETED', retry_preview
        safe_retry = invoke(management, runner, preview_run_id=retry_preview['run_id'])
        assert safe_retry['status'] == 'COMPLETED' and len(boundary.ledger) == 1, safe_retry
    else:
        data = json.loads(result['steps'][0]['result_summary_json'])['data']
        assert data['recovery']['external_write_replayed'] is False and data['recovery']['projection']['restored'] is True
        assert data['scanned'] == 1
    return {'mode': mode, 'round': round_number, 'blocked_run_id': blocked['run_id'], 'resolution': resolution,
        'result': result, 'independent_run': independent, 'safe_retry': safe_retry,
        'restored_count': restored_count, 'external_writes': len(boundary.ledger),
        'blocked_confirmation_count': len(blocked_confirmations) if mode == 'UNKNOWN' else 0,
        'confirmation_evidence': blocked_confirmations if mode == 'UNKNOWN' else []}


def run_cancel_case(management, runner, boundary, unrelated, round_number):
    boundary.mode = 'CANCEL'
    boundary.ledger.clear()
    boundary.write_started.clear()
    boundary.write_release.clear()
    preview = invoke(management, runner)
    assert preview['status'] == 'COMPLETED'
    formal = signed_request(management, '/internal/v1/automation-projects/scan_codes/invoke',
        payload={'request_id': str(uuid4()), 'preview_run_id': preview['run_id']})
    runner.runner.wake(formal['run_id'])
    assert boundary.write_started.wait(12), 'real external write never started'
    assert len(boundary.ledger) == 1
    cancellation = asyncio.run_coroutine_threadsafe(runner.control_plane.cancel_run(formal['run_id'], actor=ACTOR), runner.loop).result(timeout=5)
    boundary.write_release.set()
    cancelled = wait_result(formal['run_id'], connection_factory=connection_factory, allow_blocked=True)
    assert cancelled['status'] == 'CANCELLED', cancelled
    independent = invoke(management, runner, automation_id=unrelated)
    assert independent['status'] == 'COMPLETED'
    boundary.mode = 'APPLIED'
    resolved = recover(management)
    assert resolved is not None, cancelled
    settled = wait_result(formal['run_id'], connection_factory=connection_factory, allow_blocked=True)
    assert settled['status'] == 'CANCELLED' and len(boundary.ledger) == 1
    data = json.loads(settled['steps'][0]['result_summary_json'])['data']
    assert data['scanned'] == 1 and data['recovery']['external_write_replayed'] is False
    return {'round': round_number, 'cancellation': cancellation, 'cancelled': cancelled,
        'resolved': resolved, 'settled': settled, 'independent_run': independent, 'external_writes': len(boundary.ledger)}


def main():
    global DATABASE, connection_factory
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--cancel-only', action='store_true')
    parser.add_argument('--physical-scope', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.rounds <= 20:
        raise ValueError('rounds must be 1..20')
    if args.cancel_only:
        DATABASE = 'v32_scan_cancel_test'
        connection_factory = partial(connect, database=DATABASE)
    prepare_database(database=DATABASE)
    runtime_root = ROOT / '.task_tmp' / 'v32' / 'reliability' / ('scan-recovery-' + uuid4().hex[:8])
    accounts = DailyAccounts()
    private_key = ECC.generate(curve='Ed25519')
    trust = Ed25519TrustStore({'v32-recovery': private_key.public_key().export_key(format='raw')})
    manifests = resolve_first_party_manifests(ToolRegistry(), _plugin_ids=frozenset({'sync_scan_codes', 'sync_arrival_stats'}))
    keys = {(item['operation'], item['action']) for manifest in manifests.values() for item in manifest.runtime_permissions['broker_operations']}
    migration_accounts = isolated_migration_accounts()
    migration_accounts['scan_codes'] = {'account_id': [ACCOUNT_ID]}
    migration_accounts['arrival_stats'] = {'account_id': [ACCOUNT_ID]}
    with LostReplyProtocol() as boundary, boundary.authentication_boundaries(), patch('plugin_core_adapters.first_party.get_account_manager', return_value=accounts), observe_preview_timestamps(runtime_root / 'preview-timing.json'):
        handlers = build_production_first_party_core_handler_map(cursor_secret=secrets.token_bytes(32),
            account_manager=accounts, allowed_action_keys=keys, capability_authorizer=boundary.authorize)
        with ManagementFixture(connection_factory=connection_factory, runtime_root=runtime_root, account_manager=accounts,
                broker_handlers=handlers, upload_signature_verifier=trust, enable_directory_faults=False,
                resource_provider=boundary.resource_loader, migration_account_bindings=migration_accounts) as management:
            bootstrap(management, private_key=private_key, trust=trust, key_id='v32-recovery')
            management.targets.reconcile_project('scan_codes')
            setup_scan(management)
            if args.physical_scope:
                from tests.v32_acceptance.daily_stats import setup_stats

                management.targets.reconcile_project('arrival_stats')
                setup_stats(management)
            unrelated = install_unrelated(management, actor=ACTOR, plugin_id='scan_recovery_independent', name='隔离核验期间独立任务')
            with RunnerFixture(management, context_builder=ContextBuilder(account_resolver=lambda _command: accounts.list_accounts()), saved_resource_provider=boundary.resource_loader) as runner:
                cases = []
                for round_number in range(0 if args.physical_scope else args.rounds):
                    if args.cancel_only:
                        checkpoint_case(run_cancel_case, management, runner, boundary, unrelated, round_number=round_number,
                            completed=cases, path=runtime_root / 'case-progress.json')
                    else:
                        for mode in ('APPLIED', 'NOT_APPLIED', 'UNKNOWN'):
                            checkpoint_case(run_case, management, runner, boundary, unrelated, mode=mode, round_number=round_number,
                                completed=cases, path=runtime_root / 'case-progress.json')
                if args.physical_scope:
                    from tests.v32_acceptance.unknown_resource_scope import exercise

                    cases.append(exercise(management, runner, boundary, unrelated, connection_factory))
                report = {'status': 'PASS' if args.rounds == 20 or args.physical_scope else 'PREPARATION', 'cases': cases,
                    'executed_at': datetime.now(timezone.utc).isoformat()}
    output = ROOT / '.task_tmp' / 'v32' / 'reliability' / ('unknown-resource-scope.json' if args.physical_scope else 'scan-cancel-recovery.json' if args.cancel_only else 'scan-recovery.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'evidence': str(output)}))


if __name__ == '__main__':
    main()
