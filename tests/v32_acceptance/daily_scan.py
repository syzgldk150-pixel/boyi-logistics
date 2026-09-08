"""A01 scan: real signed Console receipt, Runner, package and loopback TMS."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pymysql
from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.package import Ed25519TrustStore
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.models import Actor, ActorType
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.first_party import build_production_first_party_core_handler_map
from shared.service_identity import build_console_identity_headers
from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, CHILD_CODE, DailyAccounts, DailyProtocol
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.runner_fixture import RunnerFixture

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / '.task_tmp' / 'v32' / 'reliability' / ('daily-scan-' + uuid4().hex[:8])
DATABASE = 'v32_a01_test'
ACTOR = Actor(ActorType.CONSOLE_ADMIN, 'v32-daily-admin', roles=('super_admin',), authenticated_by='mysql_admin_session')


def connect(*, database=DATABASE):
    if database not in {'v32_a01_test', 'v32_recovery_test', 'v32_scan_cancel_test'} or os.environ.get('AGENT_DB_NAME') != database or os.environ.get('AGENT_DB_HOST') != '127.0.0.1' or os.environ.get('PYTHON_DOTENV_DISABLED') != '1':
        raise RuntimeError('A01 requires its explicit isolated database')
    return pymysql.connect(host='127.0.0.1', port=int(os.environ['AGENT_DB_PORT']),
        user=os.environ['AGENT_DB_USER'], password=os.environ['AGENT_DB_PASS'], database=database,
        charset='utf8mb4', autocommit=False, cursorclass=pymysql.cursors.DictCursor)


def prepare_database(*, database=DATABASE):
    from tests.test_mysql_orchestration_integration import MySqlOrchestrationIntegrationTests, _load_migration_runner
    if database not in {'v32_a01_test', 'v32_recovery_test', 'v32_scan_cancel_test'} or os.environ.get('AGENT_DB_NAME') != database or os.environ.get('AGENT_DB_HOST') != '127.0.0.1':
        raise RuntimeError('A01 cannot initialize another database')
    fixture = MySqlOrchestrationIntegrationTests
    fixture.pymysql, fixture.host = pymysql, '127.0.0.1'
    fixture.port, fixture.user = int(os.environ['AGENT_DB_PORT']), os.environ['AGENT_DB_USER']
    fixture.password, fixture.runner = os.environ['AGENT_DB_PASS'], _load_migration_runner()
    with pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute('DROP DATABASE IF EXISTS `' + database + '`')
        cursor.execute('CREATE DATABASE `' + database + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    fixture._run_migrations(database)
    fixture._run_migrations(database, check_only=True)
    with connect(database=database) as connection, connection.cursor() as cursor:
        cursor.execute('UPDATE scheduled_tasks SET enabled=0')
        connection.commit()


def wait_result(run_id, *, connection_factory=connect, allow_blocked=False, allow_resource_wait=False):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute('SELECT status,error_code,error_summary FROM agent_runs WHERE run_id=%s', (run_id,))
            run = cursor.fetchone()
            if run and (run['status'] in ({'COMPLETED', 'FAILED_TERMINAL', 'CANCELLED', 'PARTIAL', 'WAITING_APPROVAL'} | ({'BLOCKED_DATA'} if allow_blocked else set()))
                    or (allow_resource_wait and run['error_code'] == 'RESOURCE_WAIT')):
                cursor.execute('SELECT result_summary_json,postcondition_status FROM agent_run_steps WHERE run_id=%s ORDER BY step_order', (run_id,))
                return {'run_id': run_id, **run, 'steps': cursor.fetchall()}
        time.sleep(0.1)
    raise RuntimeError('real scan Runner did not reach a terminal result')


def signed_request(management, path, *, payload=None):
    body = json.dumps(payload).encode() if payload is not None else b''
    method = 'POST' if payload is not None else 'GET'
    principal = {'actor_type': ACTOR.actor_type.value, 'actor_id': ACTOR.actor_id,
        'roles': list(ACTOR.roles), 'authenticated_by': ACTOR.authenticated_by}
    headers = build_console_identity_headers(secret=management.signing_secret, method=method,
        request_target=path, body=body, principal=principal, nonce=uuid4().hex)
    headers.update({'X-Agent-Internal-Token': management.internal_token, 'Content-Type': 'application/json'})
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
        response = client.request(method, management.url + path, headers=headers, content=body)
    if response.status_code not in {200, 202}:
        raise AssertionError(f'actual signed entry rejected: HTTP {response.status_code}: {response.text}')
    return response.json()['data']


def setup_scan(management):
    automation_id = 'scan_codes'
    entry = management.catalog.require(automation_id)
    management.configuration.save(automation_id, config={
        'target_date': datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(), 'batch_size': 1, 'max_batches': 2,
    }, account_bindings={'account_id': [ACCOUNT_ID]}, resource_bindings={}, enabled_entrypoints=('console',),
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


def run_scan(management, runner, boundary, *, browser=None, configure=True):
    automation_id = 'scan_codes'
    if configure:
        setup_scan(management)
    path = '/internal/v1/automation-projects/scan_codes/invoke'
    preview = browser.run(automation_id) if browser else signed_request(management, path, payload={'request_id': str(uuid4())})
    runner.runner.wake(preview['run_id'])
    preview_result = wait_result(preview['run_id'])
    if preview_result['status'] != 'COMPLETED':
        raise AssertionError(preview_result)
    assert boundary.ledger == []
    projection = browser.scan_projection() if browser else signed_request(management, '/internal/v1/automation-projects/scan_codes/scan-previews/' + preview['run_id'])
    assert projection['selection_count'] == 1 and projection['can_confirm'] is True
    request_id = str(uuid4())
    formal = browser.confirm_scan() if browser else signed_request(management, path, payload={'request_id': request_id, 'preview_run_id': preview['run_id']})
    runner.runner.wake(formal['run_id'])
    formal_result = wait_result(formal['run_id'])
    if formal_result['status'] != 'COMPLETED':
        raise AssertionError(formal_result)
    replay = browser.replay() if browser else signed_request(management, path, payload={'request_id': request_id, 'preview_run_id': preview['run_id']})
    assert replay['run_id'] == formal['run_id']
    assert [row['BILL_CODE'] for row in boundary.ledger] == [CHILD_CODE]
    assert all(step['postcondition_status'] == 'VERIFIED' for step in formal_result['steps'])
    return {'status': 'PASS', 'entry': 'actual Console browser controls' if browser else 'signed Console Agent HTTP',
        'preview': preview_result, 'formal': formal_result, 'projection': projection,
        'replay_run_id': replay['run_id'], 'external_side_effect_rows': len(boundary.ledger), 'external_requests': boundary.requests}


def main():
    from tests.v32_acceptance.first_party_fixture import bootstrap, isolated_migration_accounts
    prepare_database()
    account_manager = DailyAccounts()
    private_key = ECC.generate(curve='Ed25519')
    trust = Ed25519TrustStore({'v32-daily': private_key.public_key().export_key(format='raw')})
    manifest = resolve_first_party_manifests(ToolRegistry(), _plugin_ids=frozenset({'sync_scan_codes'}))['sync_scan_codes']
    keys = {(item['operation'], item['action']) for item in manifest.runtime_permissions['broker_operations']}
    migration_accounts = isolated_migration_accounts()
    migration_accounts['scan_codes'] = {'account_id': [ACCOUNT_ID]}
    with DailyProtocol() as boundary, boundary.authentication_boundaries():
        handlers = build_production_first_party_core_handler_map(cursor_secret=secrets.token_bytes(32),
            account_manager=account_manager, allowed_action_keys=keys, capability_authorizer=boundary.authorize)
        with ManagementFixture(connection_factory=connect, runtime_root=RUNTIME, account_manager=account_manager,
                broker_handlers=handlers, upload_signature_verifier=trust, enable_directory_faults=False,
                resource_provider=boundary.resource_loader,
                migration_account_bindings=migration_accounts) as management:
            bootstrap(management, private_key=private_key, trust=trust, key_id='v32-daily')
            reconciled = management.targets.reconcile_project('scan_codes')
            if management.catalog.require('scan_codes').committed_snapshot is None:
                raise AssertionError('actual first-party reconciliation did not commit: ' + str(reconciled))
            with RunnerFixture(management, context_builder=ContextBuilder(account_resolver=lambda _command: account_manager.list_accounts())) as runner:
                report = run_scan(management, runner, boundary)
                report.update(executed_at=datetime.now(timezone.utc).isoformat(), runtime=runner.snapshot())
    output = RUNTIME.parent / 'a01-scan.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'evidence': str(output)}))


if __name__ == '__main__':
    main()
