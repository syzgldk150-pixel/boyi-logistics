"""A01 scan: real signed Console receipt, Invocation, package and loopback TMS."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pymysql

from agent.orchestration.models import Actor, ActorType
from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.scan_connectors_v2 import build_scan_connectors
from agent.automation_plugins.arrival_connectors_v2 import build_arrival_connectors
from tests.v32_acceptance.service_v2_artifacts import build_artifact
from plugin_core_adapters.first_party import build_production_first_party_core_handler_map
from shared.service_identity import build_console_identity_headers
from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, CHILD_CODE, DailyAccounts, DailyProtocol
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.direct_invocation_fixture import DirectFixture

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


def setup_scan(management, artifact=None):
    from tests.v32_acceptance.service_v2_artifacts import install_artifact
    automation_id = (install_artifact(management, artifact, actor=ACTOR, name='隔离扫描 V2')
                     if artifact is not None else 'scan_codes')
    entry = management.catalog.require(automation_id)
    management.configuration.save(automation_id, config={
        'target_date': datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(), 'batch_size': 1, 'max_batches': 2,
    }, account_bindings={'scan_ronghui' if artifact else 'account_id': [ACCOUNT_ID]}, resource_bindings={},
        enabled_entrypoints=('execute_console',) if artifact else ('console',),
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
    return automation_id


def run_scan(management, runner, boundary, *, browser=None, configure=True, automation_id='scan_codes'):
    if configure:
        setup_scan(management)
    path = f'/internal/v1/automation-projects/{automation_id}/invoke'
    preview = browser.run(automation_id) if browser else signed_request(management, path, payload={'request_id': str(uuid4())})
    preview_result = runner.service.wait_sync(preview['invocation_id'])
    if preview_result['status'] != 'COMPLETED':
        raise AssertionError(preview_result)
    assert boundary.ledger == []
    projection = browser.scan_projection() if browser else signed_request(management, f'/internal/v1/automation-projects/{automation_id}/scan-previews/' + preview['invocation_id'])
    assert projection['selection_count'] == 1 and projection['can_confirm'] is True
    request_id = str(uuid4())
    formal = browser.confirm_scan() if browser else signed_request(management, path, payload={'request_id': request_id, 'preview_invocation_id': preview['invocation_id']})
    formal_result = runner.service.wait_sync(formal['invocation_id'])
    if formal_result['status'] != 'COMPLETED':
        raise AssertionError(formal_result)
    replay = browser.replay() if browser else signed_request(management, path, payload={'request_id': request_id, 'preview_invocation_id': preview['invocation_id']})
    assert replay['invocation_id'] == formal['invocation_id']
    assert [row['BILL_CODE'] for row in boundary.ledger] == [CHILD_CODE]
    assert formal_result['result']['status'] == 'SUCCESS' and formal_result['result']['error'] is None
    return {'status': 'PASS', 'entry': 'actual Console browser controls' if browser else 'signed Console Agent HTTP',
        'preview': preview_result, 'formal': formal_result, 'projection': projection,
        'replay_invocation_id': replay['invocation_id'], 'external_side_effect_rows': len(boundary.ledger), 'external_requests': boundary.requests}


def main():
    prepare_database()
    account_manager = DailyAccounts()
    artifacts = {plugin: build_artifact(plugin, RUNTIME / 'artifacts') for plugin in ('sync_scan_codes_v2',)}
    with DailyProtocol() as boundary, boundary.authentication_boundaries():
        handlers = build_production_first_party_core_handler_map(cursor_secret=secrets.token_bytes(32),
            account_manager=account_manager, capability_authorizer=boundary.authorize)
        connectors = ConnectorRegistry((*build_scan_connectors(handlers), *build_arrival_connectors(handlers)))
        with ManagementFixture(connection_factory=connect, runtime_root=RUNTIME, account_manager=account_manager,
                broker_handlers=handlers, enable_directory_faults=False,
                resource_provider=boundary.resource_loader, connector_registry=connectors) as management:
            with DirectFixture(management, saved_resource_provider=boundary.resource_loader) as runner:
                identity = setup_scan(management, artifacts['sync_scan_codes_v2'])
                report = run_scan(management, runner, boundary, configure=False, automation_id=identity)
                report['runtime_model'] = 'SERVICE_V2'
                report.update(executed_at=datetime.now(timezone.utc).isoformat(), runtime=runner.snapshot())
    output = RUNTIME.parent / 'a01-scan.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'evidence': str(output)}))


if __name__ == '__main__':
    main()
