"""Actual Direct scan: unknown write facts, drained cancellation and no replay."""
from datetime import datetime, timezone
import argparse
import asyncio
from functools import partial
import json
import secrets
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.package import Ed25519TrustStore
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.first_party import build_production_first_party_core_handler_map
from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, DailyAccounts
from tests.v32_acceptance.daily_stats_protocol import DailyStatsProtocol
from tests.v32_acceptance.daily_scan import ACTOR, ROOT, connect, prepare_database, setup_scan, signed_request
from tests.v32_acceptance.first_party_fixture import bootstrap, isolated_migration_accounts
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.direct_invocation_fixture import DirectFixture
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


def invoke(management, runner, *, preview_invocation_id=None, automation_id='scan_codes'):
    payload = {'request_id': str(uuid4())}
    if preview_invocation_id:
        payload['preview_invocation_id'] = preview_invocation_id
    receipt = signed_request(management, '/internal/v1/automation-projects/' + automation_id + '/invoke', payload=payload)
    return runner.service.wait_sync(receipt['invocation_id'])


def write_receipts(invocation_id):
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT invocation_id,orchestration_run_id,step_id,outcome,action FROM automation_write_attempt_receipts WHERE invocation_id=%s', (invocation_id,))
        rows = cursor.fetchall()
    assert rows and all(row['orchestration_run_id'] is None and row['step_id'] is None for row in rows), rows
    return rows


def run_case(management, runner, boundary, unrelated, *, mode, round_number):
    boundary.mode = mode
    boundary.ledger.clear()
    preview = invoke(management, runner)
    assert preview['status'] == 'COMPLETED' and len(boundary.ledger) == 0, preview
    result = invoke(management, runner, preview_invocation_id=preview['invocation_id'])
    assert result['status'] == 'WRITE_OUTCOME_UNKNOWN', result
    expected_writes = 0 if mode == 'NOT_APPLIED' else 1
    assert len(boundary.ledger) == expected_writes
    receipts = write_receipts(result['invocation_id'])
    independent = invoke(management, runner, automation_id=unrelated)
    assert independent['status'] == 'COMPLETED', independent
    # History is immutable evidence. It is neither a pending job nor a reason
    # to replay a platform request or block a separately requested preview.
    assert not runner.service.active_invocations()
    before = list(boundary.ledger)
    for _ in range(20):
        assert runner.service.get(result['invocation_id'])['status'] == 'WRITE_OUTCOME_UNKNOWN'
    assert boundary.ledger == before
    boundary.mode = 'SUCCESS'
    fresh_preview = invoke(management, runner)
    assert fresh_preview['status'] == 'COMPLETED', fresh_preview
    assert fresh_preview['invocation_id'] != preview['invocation_id']
    safe_retry = None
    if mode == 'NOT_APPLIED':
        safe_retry = invoke(management, runner, preview_invocation_id=fresh_preview['invocation_id'])
        assert safe_retry['status'] == 'COMPLETED', safe_retry
        assert len(boundary.ledger) == 1
    else:
        # The already committed outbound scan is found by the current source
        # read; the new preview never resends that scan.
        assert boundary.ledger == before
    assert runner.service.get(result['invocation_id'])['status'] == 'WRITE_OUTCOME_UNKNOWN'
    return {'mode': mode, 'round': round_number, 'result': result,
        'write_receipts': receipts, 'independent_invocation': independent,
        'fresh_preview': fresh_preview, 'safe_retry': safe_retry,
        'external_writes': len(boundary.ledger), 'automatic_recovery_or_replay': False}


def run_cancel_case(management, runner, boundary, unrelated, round_number):
    boundary.mode = 'CANCEL'
    boundary.ledger.clear()
    boundary.write_started.clear()
    boundary.write_release.clear()
    preview = invoke(management, runner)
    assert preview['status'] == 'COMPLETED'
    formal = signed_request(management, '/internal/v1/automation-projects/scan_codes/invoke',
        payload={'request_id': str(uuid4()), 'preview_invocation_id': preview['invocation_id']})
    assert boundary.write_started.wait(12), 'real external write never started'
    assert len(boundary.ledger) == 1
    async def begin_cancel():
        cancellation = asyncio.create_task(runner.service.cancel(formal['invocation_id']))
        await asyncio.sleep(.05)
        assert not cancellation.done(), 'must drain the actual outstanding platform operation'
        assert runner.service.get(formal['invocation_id'])['status'] == 'CANCELLING'
        return cancellation
    cancellation = asyncio.run_coroutine_threadsafe(begin_cancel(), runner.loop).result(timeout=2)
    blocked = signed_request(management, '/internal/v1/automation-projects/scan_codes/invoke', payload={'request_id': str(uuid4())})
    assert blocked['status'] == 'FAILED' and blocked['error_code'] == 'EXECUTION_RESOURCE_BUSY', blocked
    released_at = time.monotonic()
    boundary.write_release.set()
    async def settle():
        return await cancellation
    cancelled = asyncio.run_coroutine_threadsafe(settle(), runner.loop).result(timeout=5)
    cancellation_seconds = time.monotonic() - released_at
    assert cancelled['status'] == 'WRITE_OUTCOME_UNKNOWN', cancelled
    assert cancellation_seconds <= 2, cancellation_seconds
    independent = invoke(management, runner, automation_id=unrelated)
    assert independent['status'] == 'COMPLETED'
    boundary.mode = 'SUCCESS'
    fresh = invoke(management, runner)
    assert fresh['status'] == 'COMPLETED' and len(boundary.ledger) == 1
    assert runner.service.get(formal['invocation_id'])['status'] == 'WRITE_OUTCOME_UNKNOWN'
    return {'round': round_number, 'cancelled': cancelled, 'busy_call_ended': blocked,
        'fresh_preview': fresh, 'independent_invocation': independent,
        'write_receipts': write_receipts(formal['invocation_id']),
        'cancellation_seconds_after_actual_port_drained': cancellation_seconds,
        'external_writes': len(boundary.ledger), 'automatic_recovery_or_replay': False}


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
            with DirectFixture(management, saved_resource_provider=boundary.resource_loader) as runner:
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
