"""Current ZIP/Direct/MySQL with append-only remote writes and faulted replies.

Only the remote TMS transport/login ports are replaced. Selection, creation,
fingerprint readback, Broker, receipts and business target guard are real.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from uuid import uuid4

import httpx
import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.problem_connectors_v2 import build_problem_connectors
from agent.tms_runtime.scripts import ronghui_problem_upload as remote
from plugin_core_adapters import problem_actions
from tests.direct_invocation_fixture import DirectFixture, direct_repository  # noqa: F401
from tests.v32_acceptance.daily_scan import signed_request
from tests.v32_acceptance.decision_maintenance import ACTOR, setup_instance
from tests.v32_acceptance.finance_maintenance_drill import setup_unrelated
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.problem_fixture import ACCOUNTS, ProblemAccounts, ProblemSupplier
from tests.v32_acceptance.service_v2_artifacts import build_artifact

ROOT = Path(__file__).resolve().parents[1]


class RawProblemBoundary:
    def __init__(self):
        self.rows, self.pending, self.calls = [], [], []
        self.mode = 'normal'
        self.bill_modes = {}
        boundary = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_POST(self):
                args = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                boundary.calls.append(args)
                if self.path == '/query':
                    result = [row for row in boundary.rows if row['BILL_CODE'] == args['bill']]
                elif self.path == '/save':
                    row = args['operations'][0]['data'][0]
                    mode = boundary.bill_modes.get(row['BILL_CODE'], boundary.mode)
                    if mode == 'rejected':
                        result = {'success': False, 'message': 'isolated explicit rejection'}
                    else:
                        # No upstream idempotency or unique index hides duplicates.
                        row['REGISTER_SAVE_DATE'] = row['REGISTER_DATE']
                        if mode == 'late':
                            boundary.pending.append(row)
                        elif mode != 'unresolved':
                            boundary.rows.append(row)
                        result = {'success': True}
                        if mode in {'applied_lost', 'late', 'unresolved'}:
                            self.send_error(503)
                            return
                else:
                    self.send_error(403)
                    return
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def post(self, path, payload):
        response = httpx.post(f'http://127.0.0.1:{self.server.server_port}' + path, json=payload, timeout=3)
        response.raise_for_status()
        return response.json()

    def close(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()


@pytest.fixture(scope='module')
def problem_runtime(direct_repository):  # noqa: F811
    root = ROOT / '.task_tmp/phase1/unknown' / uuid4().hex
    root.mkdir(parents=True)
    artifact = build_artifact('self_pickup_problem_upload_v2', root / 'artifacts')
    accounts, boundary = ProblemAccounts(), RawProblemBoundary()
    try:
        with pytest.MonkeyPatch.context() as patch, ProblemSupplier(root / 'sheets') as sheets:
            patch.setattr(problem_actions, '_login_session', lambda descriptor: descriptor['account_id'])
            patch.setattr(remote, 'fetch_login_context', lambda _session: {
                'site_code': 'ISOLATED-SAME-SITE', 'site_name': '隔离站点', 'emp_code': 'TEST', 'emp_name': '隔离操作员'})
            patch.setattr(remote, 'resolve_problem_page_context', lambda _session: {})
            patch.setattr(remote, 'resolve_registered_problem_query_context', lambda _session: {})
            patch.setattr(remote, 'fetch_bill_info', lambda *_args: {'SEND_SITE_CODE': 'OTHER', 'SEND_SITE': '隔离其他站'})
            patch.setattr(remote, 'fetch_guid', lambda *_args: str(uuid4()))
            patch.setattr(remote, 'update_postpone_days', lambda *_args: True)
            patch.setattr(remote, 'query_registered_problem_items', lambda _session, *, bill_code, **_kw:
                          boundary.post('/query', {'bill': bill_code}))
            patch.setattr(remote, 'save_tables', lambda _session, operations, _context:
                          boundary.post('/save', {'operations': operations}))
            handlers = problem_actions.build_production_problem_handler_map(cursor_secret=secrets.token_bytes(32),
                account_manager=accounts, resource_loader=sheets.resource_loader,
                feishu_operation=sheets.feishu_operation, capability_authorizer=sheets.authorize)
            registry = ConnectorRegistry(build_problem_connectors(handlers))
            with ManagementFixture(connection_factory=direct_repository._connection_factory, runtime_root=root / 'host',
                    account_manager=accounts, broker_handlers=handlers, resource_provider=sheets.resource_loader,
                    connector_registry=registry, enable_directory_faults=False) as host:
                primary, alias = (setup_instance(host, artifact) for _ in range(2))
                entry = host.catalog.require(alias)
                host.management.save_plugin_settings(alias, config=entry.project_config,
                    account_bindings={'self_pickup_primary': [ACCOUNTS['account_id']],
                                      'self_pickup_daxiang_s': [ACCOUNTS['account_id']]},
                    resource_bindings=entry.resource_bindings, request_id=str(uuid4()),
                    expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
                host.targets.reconcile_project(alias)
                unrelated = setup_unrelated(host)
                with DirectFixture(host, saved_resource_provider=sheets.resource_loader) as runtime:
                    yield host, runtime, sheets, boundary, primary, alias, unrelated
    finally:
        boundary.close()


def execute(host, runtime, identity, bill):
    path = f'/internal/v1/automation-projects/{identity}/selection-previews'
    preview = signed_request(host, path, payload={'request_id': str(uuid4())})
    result = runtime.service.wait_sync(preview['invocation_id'])
    assert result['status'] == 'COMPLETED', result
    call = signed_request(host, path + '/' + preview['invocation_id'] + '/confirm',
        payload={'request_id': str(uuid4()), 'selected_bill_codes': bill if isinstance(bill, list) else [bill]})
    return runtime.service.wait_sync(call['invocation_id'])


@pytest.mark.parametrize('round_index', range(20))
@pytest.mark.parametrize('mode', ['applied_lost', 'rejected', 'late', 'unresolved'])
def test_new_uuid_and_alias_instance_never_duplicate_uncertain_problem(problem_runtime, round_index, mode, record_property):
    host, runtime, sheets, boundary, primary, alias, unrelated = problem_runtime
    bill = f'R_UNKNOWN_{mode.upper()}_{round_index}'
    sheets.rows = [sheets.rows[0], [bill, '合成配件', '自提', '1', '邵阳大祥S站', '1']]
    boundary.mode = mode
    start = len(boundary.calls)
    first = execute(host, runtime, primary, bill)
    # Applied response loss is already resolved by the real authoritative reader.
    assert first['status'] == ('COMPLETED' if mode == 'applied_lost' else 'WRITE_OUTCOME_UNKNOWN'), first
    second = execute(host, runtime, alias, bill)
    saves = [call for call in boundary.calls[start:] if 'operations' in call]
    assert len(saves) == (2 if mode == 'rejected' else 1)
    assert second['status'] == ('COMPLETED' if mode == 'applied_lost' else 'WRITE_OUTCOME_UNKNOWN'), second
    independent = host.policy.invoke_console(unrelated, request_id=str(uuid4()), actor=ACTOR)
    assert runtime.service.wait_sync(independent['invocation_id'])['status'] == 'COMPLETED'
    if mode == 'late':
        boundary.rows.extend(boundary.pending)
        boundary.pending.clear()
    boundary.mode = 'normal'
    third = execute(host, runtime, alias, bill)
    assert third['status'] == ('WRITE_OUTCOME_UNKNOWN' if mode == 'unresolved' else 'COMPLETED'), third
    rows = [row for row in boundary.rows if row['BILL_CODE'] == bill]
    assert len(rows) == (0 if mode == 'unresolved' else 1)
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)
    record_property('scenario', mode)
    record_property('actual_external_rows', len(rows))
    record_property('new_request_ids', json.dumps([row['request_id'] for row in (first, second, third)]))


@pytest.mark.parametrize('round_index', range(20))
def test_partial_batch_does_not_replay_verified_item_or_uncertain_tail(problem_runtime, round_index, record_property):
    host, runtime, sheets, boundary, primary, alias, unrelated = problem_runtime
    first_bill, tail_bill = (f'R_PARTIAL_{round_index}_{part}' for part in ('A', 'B'))
    sheets.rows = [sheets.rows[0], *[[bill, '合成配件', '自提', '1', '邵阳大祥S站', '1']
                                  for bill in (first_bill, tail_bill)]]
    boundary.mode = 'normal'
    boundary.bill_modes[tail_bill] = 'late'
    start = len(boundary.calls)
    first = execute(host, runtime, primary, [first_bill, tail_bill])
    assert first['status'] == 'WRITE_OUTCOME_UNKNOWN', first
    assert [row['BILL_CODE'] for row in boundary.rows if row['BILL_CODE'] in {first_bill, tail_bill}] == [first_bill]
    retry = execute(host, runtime, alias, [first_bill, tail_bill])
    assert retry['status'] == 'WRITE_OUTCOME_UNKNOWN', retry
    assert len([call for call in boundary.calls[start:] if 'operations' in call]) == 2
    continued = host.policy.invoke_console(unrelated, request_id=str(uuid4()), actor=ACTOR)
    assert runtime.service.wait_sync(continued['invocation_id'])['status'] == 'COMPLETED'
    boundary.rows.extend(boundary.pending)
    boundary.pending.clear()
    boundary.bill_modes.pop(tail_bill)
    checked = execute(host, runtime, alias, [first_bill, tail_bill])
    assert checked['status'] == 'COMPLETED', checked
    assert len([call for call in boundary.calls[start:] if 'operations' in call]) == 2
    assert sorted(row['BILL_CODE'] for row in boundary.rows if row['BILL_CODE'] in {first_bill, tail_bill}) == [first_bill, tail_bill]
    assert runtime.service.get(first['invocation_id'])['status'] == 'WRITE_OUTCOME_UNKNOWN'
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)
    record_property('actual_external_rows', 2)
    record_property('actual_create_calls', 2)
