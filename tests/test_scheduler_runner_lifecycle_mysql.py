"""A11: real scheduler, installed plugin, HTTP timeout and direct lifecycle.

The package reads an isolated network-backed key/value fixture through the real
Broker. No invocation, scheduler admission, subprocess or HTTP outcome is mocked.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from uuid import uuid4

import httpx

from agent.scheduler import _execute_scheduled_tool
from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from tests.direct_invocation_fixture import DirectFixture, direct_repository  # noqa: F401
from tests.test_direct_plugin_invocation_mysql import ACTOR, _install, _legacy_counts
from tests.v32_acceptance.management_fixture import ManagementFixture


class HttpReadWorkload:
    def __init__(self):
        self.calls = Counter()
        self.timeouts = Counter()
        self.available = False
        self.current_label = 'timeout'
        self.release_cancel = threading.Event()
        self.jobs = {label: uuid4().hex + '-' + label for label in ('timeout', 'cancel', 'future')}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                job = self.path.removeprefix('/')
                labels = [label for label, identity in owner.jobs.items() if identity == job]
                if len(labels) != 1:
                    self.send_error(404)
                    return
                owner.calls[labels[0]] += 1
                if labels[0] == 'cancel':
                    owner.release_cancel.wait(5)
                elif not owner.available:
                    threading.Event().wait(.2)
                body = json.dumps({'job': job, 'rows': [{'value': len(job)}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The test client really exceeded its read deadline.

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def execute(self, arguments):
        with httpx.Client(trust_env=False, timeout=5 if self.current_label == 'cancel' else .05) as client:
            try:
                response = client.get(f'http://127.0.0.1:{self.server.server_port}/' + arguments['job'])
                response.raise_for_status()
                return response.json()
            except httpx.ReadTimeout:
                labels = [label for label, identity in self.jobs.items() if identity == arguments['job']]
                if len(labels) != 1:
                    raise ValueError('Unknown owned workload identity')
                self.timeouts[labels[0]] += 1
                return {'error_code': 'TIMEOUT', 'error': {
                    'message': 'Owned loopback read deadline expired', 'retryable': True}}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()



def make_package(directory):
    source, archive = directory / "source", directory / "read.zip"
    init_service_v2_source(source, plugin_id="scheduler_direct_read", name="Isolated scheduler read", version="1.0.0")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    service = manifest["provides"][0]["service"]
    manifest["provides"][0]["operations"][0]["effect"] = "read"
    manifest["capabilities"] = [{"name": "storage.kv", "operations": ["get"], "account_role": None, "resource_role": None}]
    manifest["storage"]["kv"] = True
    manifest["contributes"]["harness"] = []
    manifest["contributes"]["scheduler"] = [{"id": "scheduled_read", "title": "Isolated scheduled read", "service": service, "operation": "run", "default_enabled": True, "schedule": {"kind": "cron", "expression": "0 8 * * *", "timezone": "Asia/Shanghai"}}]
    manifest_path.write_text(json.dumps(manifest))
    (source / "payload/main.py").write_text('''import json, sys
from datetime import datetime, timezone
from boyi_plugin_sdk import broker_call
request = json.load(sys.stdin)
assert request['schema_version'] == 2 and request['entrypoint'] == 'scheduler'
meta = {'source_system': 'isolated_network_storage', 'observed_at': datetime.now(timezone.utc).isoformat(), 'record_count': 0, 'pagination_complete': True, 'evidence_refs': []}
try:
    response = broker_call('storage.kv', action='get', role='__system__', arguments={'key': 'current-workload'})
    assert response['found'] is True and response['version'] == 1
    data = response['value']
    meta['record_count'] = len(data['rows'])
    meta['evidence_refs'] = [response.host_evidence_ref]
    result = {'status': 'SUCCESS', 'data': data, 'meta': meta, 'warnings': [], 'error': None}
except Exception as exc:
    result = {'status': 'FAILED', 'data': {}, 'meta': meta, 'warnings': [], 'error': {'code': str(exc), 'message': 'Isolated source read failed'}}
json.dump(result, sys.stdout)
''')
    build_service_v2_package(source, archive)
    return archive.read_bytes()


def test_timeout_cancel_future_occurrence_and_restart(direct_repository, record_property):  # noqa: F811 - imported pytest fixture
    directory = Path(__file__).resolve().parents[1] / '.t' / ('sched-' + uuid4().hex[:6])
    directory.mkdir(parents=True)
    occurrence = datetime.now(timezone.utc).replace(microsecond=0)
    with HttpReadWorkload() as workload:
        def read_storage(_context, arguments):
            assert _context.operation == 'storage.kv' and _context.action == 'get'
            assert arguments == {'key': 'current-workload'}
            raw = workload.execute({'job': workload.jobs[workload.current_label]})
            if 'error_code' in raw:
                raise RuntimeError(raw['error_code'])
            return {'found': True, 'version': 1, 'value': raw}
        with ManagementFixture(connection_factory=direct_repository._connection_factory, runtime_root=directory / 'runtime', broker_handlers={('storage.kv', '*'): read_storage}, enable_directory_faults=False) as management:
            identity = _install(management, make_package(directory))
            entry = management.catalog.require(identity)
            management.management.save_console_schedule(identity, schedule={'kind': 'daily_times', 'times': ['08:00'], 'enabled': True}, request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
            management.targets.reconcile_project(identity)
            entry = management.catalog.require(identity)
            def schedules():
                with management.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
                    cursor.execute('SELECT * FROM scheduled_tasks ORDER BY id')
                    return cursor.fetchall()
            schedules_before, legacy_before = schedules(), _legacy_counts(management.repository)
            scheduled = [row for row in schedules_before if row['automation_id'] == identity]
            assert len(scheduled) == 1, scheduled
            task_id = scheduled[0]['id']
            async def invoke(at):
                return await _execute_scheduled_tool(None, task_id=task_id, tool_name=f'automation.{identity}.run', arguments={}, scheduled_for=at, cron_expression='0 8 * * *', configuration_version=entry.project_config_version, automation_id=identity, automation_generation=entry.committed_snapshot.generation, automation_project_invoker=management.policy)
            with DirectFixture(management, directory=directory / 'ipc') as runtime:
                timed_out = asyncio.run_coroutine_threadsafe(invoke(occurrence), runtime.loop).result(timeout=15)
                assert timed_out['status'] == 'FAILED', timed_out
                assert workload.calls['timeout'] == workload.timeouts['timeout'] == 1, json.dumps(timed_out)
                workload.current_label = 'cancel'
                pending = asyncio.run_coroutine_threadsafe(invoke(occurrence + timedelta(days=1)), runtime.loop)
                async def cancel_actual_read():
                    deadline = asyncio.get_running_loop().time() + 5
                    while not workload.calls['cancel'] and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(.01)
                    assert workload.calls['cancel'] == 1
                    calls = runtime.service.active_invocations()
                    assert len(calls) == 1
                    cancelling = asyncio.create_task(runtime.service.cancel(calls[0]['invocation_id']))
                    await asyncio.sleep(.05)
                    assert not cancelling.done(), 'The actual host read must drain before releasing the call.'
                    workload.release_cancel.set()
                    return await cancelling
                cancelled = asyncio.run_coroutine_threadsafe(cancel_actual_read(), runtime.loop).result(timeout=10)
                assert cancelled['status'] == 'CANCELLED', cancelled
                assert pending.result(timeout=5)['invocation_id'] == cancelled['invocation_id']
            # Restart only the direct process service; no history is submitted.
            workload.available, workload.current_label = True, 'future'
            with DirectFixture(management, directory=directory / 'restart') as runtime:
                replay = asyncio.run_coroutine_threadsafe(invoke(occurrence), runtime.loop).result(timeout=10)
                cancel_replay = asyncio.run_coroutine_threadsafe(invoke(occurrence + timedelta(days=1)), runtime.loop).result(timeout=10)
                assert replay['invocation_id'] == timed_out['invocation_id']
                assert cancel_replay['invocation_id'] == cancelled['invocation_id']
                assert workload.calls == Counter({'timeout': 1, 'cancel': 1})
                future = asyncio.run_coroutine_threadsafe(invoke(occurrence + timedelta(days=2)), runtime.loop).result(timeout=15)
                assert future['status'] == 'COMPLETED', future
                assert workload.calls['future'] == 1
                assert schedules() == schedules_before
                assert _legacy_counts(management.repository) == legacy_before
                record_property('actual_http_calls', dict(workload.calls))
                record_property('actual_read_timeouts', dict(workload.timeouts))
                record_property('preserved_schedule_rows', len(schedules_before))
                record_property('invocation_ids', [timed_out['invocation_id'], cancelled['invocation_id'], future['invocation_id']])
