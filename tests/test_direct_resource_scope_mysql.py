"""Real V2 ZIP, Broker, HTTP mutations and MySQL physical resource exclusion."""
import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import httpx
import pytest

from agent.automation_plugins.connector_registry import ConnectorBindingKind, ConnectorDescriptor, ConnectorOperation, ConnectorRegistry
from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from tests.direct_invocation_fixture import DirectFixture, direct_repository  # noqa: F401
from tests.test_automation_plugin_connector_runtime_v2 import _manifest
from tests.v32_acceptance.daily_scan import ACTOR
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.problem_fixture import ACCOUNTS, ProblemAccounts

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value'], 'additionalProperties': False}
PAYLOAD = '''import json,sys
from datetime import datetime,timezone
from boyi_plugin_sdk import broker_call
request=json.load(sys.stdin)
refs=[]
for side in ('left','right'):
    try:
        result=broker_call('service.invoke',action='query',role='__system__',arguments={
            'service':'connector.boyi.guard_'+side+'@1','operation':'query','arguments':{'value':side}})
    except RuntimeError as error:
        json.dump({'status':'FAILED','data':{},'meta':{'source_system':'isolated-http-sheet',
            'observed_at':datetime.now(timezone.utc).isoformat(),'record_count':len(refs),
            'pagination_complete':False,'evidence_refs':refs,
            'write_outcome':'NOT_APPLIED' if str(error)=='EXECUTION_RESOURCE_BUSY' and not refs else 'WRITE_OUTCOME_UNKNOWN'},
            'warnings':[],'error':{'code':str(error),'message':'Host rejected the operation','retryable':False}},sys.stdout)
        sys.exit(0)
    assert result['value'] == side
    refs.append(result.host_evidence_ref)
observed=datetime.now(timezone.utc).isoformat()
data={'evidence':{'outcome':'WRITE_VERIFIED','service':'plugin.connector_consumer.runner@1','operation':'run'}}
proof={'condition':'plugin_result_contract_valid','verified':True,'observed_at':observed,
    'evidence_ref':refs[-1],'details':{'evidence_refs':refs,'result_summary':data}}
json.dump({'status':'SUCCESS','data':data,
    'meta':{'source_system':'isolated-http-sheet','observed_at':observed,
    'record_count':2,'pagination_complete':True,'evidence_refs':refs,'write_outcome':'WRITE_VERIFIED',
    'postconditions':{'0':True},'postcondition_evidence':{'0':proof}},
    'warnings':[],'error':None},sys.stdout)
'''


class Boundary:
    def __init__(self, resources):
        self.resources = resources
        self.lock = threading.Lock()
        self.active, self.maximum, self.calls = {}, {}, []
        self.held = set()
        self.entered, self.release = threading.Event(), threading.Event()
        boundary = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                physical = data['physical']
                with boundary.lock:
                    boundary.active[physical] = boundary.active.get(physical, 0) + 1
                    boundary.maximum[physical] = max(boundary.maximum.get(physical, 0), boundary.active[physical])
                    boundary.calls.append(data)
                try:
                    if physical in boundary.held:
                        boundary.entered.set()
                        assert boundary.release.wait(20)
                    payload = json.dumps({'value': data['value']}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    with boundary.lock:
                        boundary.active[physical] -= 1
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def write(self, binding, arguments):
        resource = self.resources[binding.resource_id]
        response = httpx.post(f'http://127.0.0.1:{self.server.server_port}',
            json={'physical': resource['sheet_id'], 'value': arguments['value'],
                  'invocation': binding.invocation_context.write_attempt_identity['invocation_id']}, timeout=22)
        response.raise_for_status()
        return response.json()

    def reset(self, held):
        assert not any(self.active.values())
        self.maximum.clear()
        self.calls.clear()
        self.held = held
        self.entered.clear()
        self.release.clear()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()


@pytest.fixture(scope='module')
def resources_runtime(direct_repository):  # noqa: F811
    root = ROOT / '.task_tmp/phase1/resource-scopes' / uuid4().hex
    root.mkdir(parents=True)
    source, archive = root / 'source', root / 'package.zip'
    init_service_v2_source(source, plugin_id='connector_consumer', name='真实写资源隔离', version='1.0.0')
    manifest = _manifest(requires=[{'service': f'connector.boyi.guard_{side}@1',
        'binding_kind': 'resource', 'resource_role': side}
        for side in ('left', 'right')], account_role='operator', allowed_systems=('ronghui',))
    manifest['provides'][0]['operations'][0]['effect'] = 'external_write'
    manifest['resource_roles'] = [{'role': side, 'allowed_kinds': ['feishu_sheet'], 'required': True} for side in ('left', 'right')]
    manifest['settings_ui'] = {'entry': 'settings/index.html', 'bridge_api': '1.0.0'}
    (source / 'settings').mkdir(exist_ok=True)
    (source / 'settings/index.html').write_text('<!doctype html><meta charset="utf-8"><h1>隔离写资源契约测试</h1>', encoding='utf-8')
    (source / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (source / 'payload/main.py').write_text(PAYLOAD, encoding='utf-8')
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    # A distinct installed plugin exercises cross-plugin/cross-credential aliases.
    (source / 'manifest.json').write_text(json.dumps(manifest).replace('connector_consumer', 'connector_consumer_alias'), encoding='utf-8')
    (source / 'payload/main.py').write_text(PAYLOAD.replace('connector_consumer', 'connector_consumer_alias'), encoding='utf-8')
    alias_archive = root / 'alias.zip'
    build_service_v2_package(source, alias_archive)
    alias_package = alias_archive.read_bytes()
    resources = {}
    for name, physical in (('first', 'a'), ('alias', 'a'), ('second', 'b'), ('third', 'c'), ('fourth', 'd')):
        resource = {'resource_kind': 'feishu_sheet', 'spreadsheet_token': 'owned-workbook', 'sheet_id': physical}
        if name == 'alias':
            resource['range'] = 'A1:A2'
        resource['_meta'] = {'resource_key': name, 'configuration_version': 1,
                             'config_sha256': sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest(),
                             'source': 'explicit isolated synthetic resource'}
        resources[name] = resource
    boundary = Boundary(resources)
    registry = ConnectorRegistry(tuple(ConnectorDescriptor(service=f'connector.boyi.guard_{side}@1',
        title='Owned sheet writer', account_role=None, allowed_systems=(), binding_kind=ConnectorBindingKind.RESOURCE,
        resource_role=side, allowed_resource_kinds=('feishu_sheet',), operations=(ConnectorOperation(
            name='query', effect=CapabilityEffect.EXTERNAL_WRITE, input_schema=SCHEMA, output_schema=SCHEMA, handler=boundary.write),))
        for side in ('left', 'right')))
    accounts = ProblemAccounts()
    try:
        with ManagementFixture(connection_factory=direct_repository._connection_factory, runtime_root=root / 'host',
                account_manager=accounts, resource_provider=resources.get, connector_registry=registry, enable_directory_faults=False) as host:
            ids = {}
            for name, left, right, account in (('first', 'first', 'second', ACCOUNTS['account_id']),
                    ('alias', 'alias', 'second', ACCOUNTS['daxiang_s_account_id']),
                    ('independent', 'third', 'fourth', ACCOUNTS['account_id']),
                    ('reverse', 'second', 'alias', ACCOUNTS['daxiang_s_account_id'])):
                selected_package = alias_package if name in {'alias', 'reverse'} else package
                installed = host.management.install_service_v2(selected_package, request_id=str(uuid4()), transport_package_sha256=sha256(selected_package).hexdigest(),
                    raw_intent=json.dumps({'instance_name': name, 'permissions_confirmed': True}), actor=ACTOR)
                identity = installed['automation_id']
                entry = host.catalog.require(identity)
                host.management.save_plugin_settings(identity, config={}, account_bindings={'operator': [account]},
                    resource_bindings={'left': left, 'right': right}, request_id=str(uuid4()),
                    expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
                entry = host.catalog.require(identity)
                host.management.set_enabled(identity, enabled=True, request_id=str(uuid4()), expected_record_version=entry.record_version, actor=ACTOR)
                host.targets.reconcile_project(identity)
                policy = host.policy.get_policy_projection(identity)
                host.policy.update_policy(identity, mode='PROJECT_FULL_AUTO', request_id=str(uuid4()), comment='Owned HTTP writes only',
                    expected_policy_version=policy['policy_version'], expected_project_configuration_version=policy['project_configuration_version'], actor=ACTOR)
                host.targets.reconcile_project(identity)
                ids[name] = identity
            with DirectFixture(host, saved_resource_provider=resources.get) as runtime:
                yield host, runtime, boundary, ids
    finally:
        boundary.close()


@pytest.mark.parametrize('round_index', range(20))
@pytest.mark.parametrize('scenario', ['alias', 'reverse', 'cancel_waiter', 'timeout'])
def test_actual_resource_scope_is_alias_safe_and_releases_without_deadlock(resources_runtime, monkeypatch, round_index, scenario, record_property):
    host, runtime, boundary, ids = resources_runtime
    if scenario == 'timeout':
        # Exercise the real deadline repeatedly with a short injected budget;
        # the production default is separately asserted by the admission tests.
        monkeypatch.setattr(runtime.service, 'resource_wait_seconds', 1.0)
    boundary.reset({'a'})
    def submit(name):
        return host.policy.invoke_console(ids[name], request_id=str(uuid4()), actor=ACTOR)
    first = submit('first')
    assert boundary.entered.wait(5)
    attempted = threading.Event()
    guard = runtime.service.host_operation
    @asynccontextmanager
    async def observed(prepared, **kwargs):
        if prepared.grant.automation_id == ids['reverse' if scenario == 'reverse' else 'alias']:
            attempted.set()
        async with guard(prepared, **kwargs):
            yield
    monkeypatch.setattr(runtime.issuer, 'host_operation_guard', observed)
    waiter = submit('reverse' if scenario == 'reverse' else 'alias')
    try:
        assert attempted.wait(5), json.dumps({'waiter': runtime.service.get(waiter['invocation_id']),
            'calls': boundary.calls, 'errors': runtime.broker_errors}, default=str)
        independent = submit('independent')
        result = runtime.service.wait_sync(independent['invocation_id'], timeout_seconds=3)
        assert result['status'] == 'COMPLETED', json.dumps({'result': result, 'errors': runtime.broker_errors, 'calls': boundary.calls}, default=str)
        assert not boundary.release.is_set()
        if scenario == 'cancel_waiter':
            started = time.monotonic()
            cancelled = asyncio.run_coroutine_threadsafe(runtime.service.cancel(waiter['invocation_id']), runtime.loop).result(2)
            assert cancelled['status'] == 'CANCELLED', cancelled
            cancel_seconds = time.monotonic() - started
            assert cancel_seconds <= 2
            record_property('waiting_cancel_seconds', cancel_seconds)
            assert not any(call['invocation'] == waiter['invocation_id'] for call in boundary.calls)
        elif scenario == 'timeout':
            timed_out = runtime.service.wait_sync(waiter['invocation_id'], timeout_seconds=12)
            assert timed_out['status'] == 'FAILED' and timed_out['error_code'] == 'EXECUTION_RESOURCE_BUSY', timed_out
            assert not any(call['invocation'] == waiter['invocation_id'] for call in boundary.calls)
        assert boundary.maximum.get('a') == 1, boundary.calls
    finally:
        boundary.release.set()
    assert runtime.service.wait_sync(first['invocation_id'])['status'] == 'COMPLETED'
    if scenario not in {'cancel_waiter', 'timeout'}:
        assert runtime.service.wait_sync(waiter['invocation_id'])['status'] == 'COMPLETED'
    if scenario == 'timeout':
        fresh = submit('alias')
        assert runtime.service.wait_sync(fresh['invocation_id'])['status'] == 'COMPLETED'
    assert all(value == 1 for value in boundary.maximum.values())
    assert not runtime.service.active_invocations() and not runtime.service._held_operations
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)
    record_property('scenario', scenario)
    record_property('actual_http_mutations', len(boundary.calls))
    record_property('resource_wait_budget_seconds', runtime.service.resource_wait_seconds)
