"""A01 scan and statistics on real signed packages, MySQL and HTTP ledgers."""
from datetime import datetime, timezone
import json
import secrets
from uuid import uuid4
from zoneinfo import ZoneInfo

from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.package import Ed25519TrustStore
from agent.orchestration.context_builder import ContextBuilder
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.first_party import build_production_first_party_core_handler_map
from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, MAIN_CODE, DailyAccounts
from tests.v32_acceptance.daily_scan import ACTOR, RUNTIME, connect, prepare_database, run_scan, setup_scan, signed_request, wait_result
from tests.v32_acceptance.daily_stats_protocol import DailyStatsProtocol
from tests.v32_acceptance.daily_browser import DailyBrowser
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.first_party_fixture import bootstrap, isolated_migration_accounts
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.runner_fixture import RunnerFixture


def setup_stats(management):
    automation_id = 'arrival_stats'
    entry = management.catalog.require(automation_id)
    resources = {role: identity for role, identity in entry.resource_bindings.items() if role not in {'feishu_route', 'webhook_route'}}
    target_date = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    management.configuration.save(automation_id, config={'target_date': target_date,
        'pending_sheet_disabled': True, 'archive_snapshot': False, 'dry_run': False},
        account_bindings={'account_id': [ACCOUNT_ID]}, resource_bindings=resources, enabled_entrypoints=('console',),
        schedule={'kind': 'none', 'times': [], 'enabled': False}, device_id=None,
        actor_id=ACTOR.actor_id, actor_role='super_admin', request_id=str(uuid4()),
        expected_project_configuration_version=entry.project_config_version)
    management.targets.reconcile_project(automation_id)
    entry = management.catalog.require(automation_id)
    management.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
        expected_record_version=entry.record_version, actor=ACTOR)
    management.targets.reconcile_project(automation_id)
    policy = management.policy.list_policies(automation_ids=(automation_id,))['items'][0]
    entry = management.catalog.require(automation_id)
    management.policy.update_policy(automation_id, mode='PROJECT_FULL_AUTO', request_id=str(uuid4()), comment='isolated A01 only',
        expected_policy_version=policy['policy_version'], expected_project_configuration_version=entry.project_config_version, actor=ACTOR)


def run_stats(management, runner, boundary, *, browser=None, configure=True):
    automation_id = 'arrival_stats'
    if configure:
        setup_stats(management)
    target_date = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    path, request_id = '/internal/v1/automation-projects/arrival_stats/invoke', str(uuid4())
    receipt = browser.run(automation_id) if browser else signed_request(management, path, payload={'request_id': request_id})
    runner.runner.wake(receipt['run_id'])
    result = wait_result(receipt['run_id'])
    if result['status'] != 'COMPLETED':
        raise AssertionError(result)
    from tools.phase7_mysql_store import list_arrival_progress, list_scan_codes_for_date, list_split_pending_problem_items, list_waybill_records
    arrivals, split = list_arrival_progress(), list_split_pending_problem_items()
    assert len(arrivals) == len(split) == 1
    assert arrivals[0]['tracking_number'] == split[0]['tracking_number'] == MAIN_CODE
    assert int(arrivals[0]['expected_quantity']) == int(split[0]['expected_quantity']) == 2
    assert int(arrivals[0]['arrived_quantity']) == int(split[0]['arrived_quantity']) == 1
    assert int(split[0]['pending_quantity']) == 1
    assert len(list_scan_codes_for_date(target_date)) == len(list_waybill_records()) == 1
    assert {write['range'].split('!')[0] for write in boundary.sheet_writes} == {'primary', 'secondary', 'split'}
    sheets = {sheet: boundary.sheet_request('read_sheet', {'spreadsheet_token': 'isolated-daily-workbook',
        'range': sheet + '!A1:S30'})['data']['values'] for sheet in ('primary', 'secondary', 'split')}
    assert sheets['primary'] == sheets['secondary']
    assert any(MAIN_CODE in row for row in sheets['primary']) and any(MAIN_CODE in row for row in sheets['split'])
    writes_before = len(boundary.sheet_writes)
    replay = browser.replay() if browser else signed_request(management, path, payload={'request_id': request_id})
    assert replay['run_id'] == receipt['run_id'] and len(boundary.sheet_writes) == writes_before
    return {'status': 'PASS', 'entry': 'actual Console browser control' if browser else 'signed Console Agent HTTP',
        'actual_run': result, 'replay_run_id': replay['run_id'], 'expected_quantity': int(arrivals[0]['expected_quantity']),
        'arrived_quantity': int(arrivals[0]['arrived_quantity']), 'pending_quantity': int(split[0]['pending_quantity']),
        'sheet_write_count': writes_before, 'sheet_values': sheets,
        'external_requests': boundary.requests, 'optional_settings': {'archive_snapshot': False, 'pending_sheet_disabled': True}}


def main():
    prepare_database()
    account_manager = DailyAccounts()
    private_key = ECC.generate(curve='Ed25519')
    trust = Ed25519TrustStore({'v32-daily': private_key.public_key().export_key(format='raw')})
    manifests = resolve_first_party_manifests(ToolRegistry(), _plugin_ids=frozenset({'sync_scan_codes', 'sync_arrival_stats'}))
    keys = {(item['operation'], item['action']) for manifest in manifests.values() for item in manifest.runtime_permissions['broker_operations']}
    migration_accounts = isolated_migration_accounts()
    for identity in ('scan_codes', 'arrival_stats'):
        migration_accounts[identity] = {'account_id': [ACCOUNT_ID]}
    with DailyStatsProtocol() as boundary, boundary.authentication_boundaries():
        handlers = build_production_first_party_core_handler_map(cursor_secret=secrets.token_bytes(32),
            account_manager=account_manager, allowed_action_keys=keys, capability_authorizer=boundary.authorize)
        with ManagementFixture(connection_factory=connect, runtime_root=RUNTIME, account_manager=account_manager,
                broker_handlers=handlers, upload_signature_verifier=trust, enable_directory_faults=False,
                resource_provider=boundary.resource_loader, migration_account_bindings=migration_accounts) as management:
            bootstrap(management, private_key=private_key, trust=trust, key_id='v32-daily')
            for identity in ('scan_codes', 'arrival_stats'):
                reconciled = management.targets.reconcile_project(identity)
                if management.catalog.require(identity).committed_snapshot is None:
                    raise AssertionError(f'actual {identity} reconciliation did not commit: {reconciled}')
            with RunnerFixture(management, context_builder=ContextBuilder(account_resolver=lambda _command: account_manager.list_accounts())) as runner:
                setup_scan(management)
                setup_stats(management)
                with ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                        signing_secret=management.signing_secret, runtime_root=RUNTIME/'console') as console, DailyBrowser(console) as browser:
                    scan = run_scan(management, runner, boundary, browser=browser, configure=False)
                    stats = run_stats(management, runner, boundary, browser=browser, configure=False)
                    assert browser.errors == [], browser.errors
                report = {'status': 'PASS', 'scan': scan, 'statistics': stats, 'runtime': runner.snapshot(),
                    'executed_at': datetime.now(timezone.utc).isoformat()}
    output = RUNTIME.parent / 'a01-scan-statistics.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'evidence': str(output)}))


if __name__ == '__main__':
    main()
