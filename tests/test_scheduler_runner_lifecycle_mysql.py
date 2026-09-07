"""A11: actual scheduler entry, HTTP timeouts, MySQL Runner and restart.

The registered read workload talks only to an owned loopback HTTP fixture.
No scheduler occurrence, Runner state transition or retry result is mocked.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from uuid import uuid4

import httpx

from agent.core import AgentCore
from agent.scheduler import _execute_scheduled_tool
from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.policy_engine import PolicyEngine
from agent.orchestration.result_verifier import ResultVerifier
from agent.orchestration.workflow_runner import WorkflowRunner
from tests.test_module_data_sources_mysql import database  # noqa: F401
from tests.test_workflow_runner_durable_admission import _Catalog, _until


class SchedulerCatalog(_Catalog):
    def get_capability(self, tool_name):
        value = super().get_capability(tool_name)
        value['permissions'] = {'required_roles': ['system']}
        value['input_schema']['properties']['account_id'] = {'type': 'string'}
        value['input_schema']['required'].append('account_id')
        return value


class HttpReadWorkload:
    def __init__(self):
        self.calls = Counter()
        self.timeouts = Counter()
        self.available = False
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
                if not owner.available:
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
        with httpx.Client(trust_env=False, timeout=.05) as client:
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


def make_runner(repository, workload):
    catalog = SchedulerCatalog()
    policy = PolicyEngine(catalog)
    runner = WorkflowRunner(repository=repository, catalog=catalog,
        execution_port=RegisteredToolExecutionAdapter(catalog=catalog, executor=None,
            direct_runners={'v32_local_workload': workload.execute}),
        context_builder=ContextBuilder(account_resolver=lambda command: [
            {'account_id': command.parameters['account_id'], 'is_active': True}]),
        planner=DeterministicPlanner(catalog), validator=PlanValidator(catalog), policy=policy,
        approval_service=ApprovalService(repository, policy), verifier=ResultVerifier(),
        worker_id='v32-scheduler-' + uuid4().hex, worker_concurrency=4, browser_concurrency=3,
        poll_interval_seconds=.1)
    # Use the real fixed scheduler -> AgentCore -> CommandGateway path without
    # constructing unrelated LLM/production account clients.
    core = AgentCore.__new__(AgentCore)
    core.registry = catalog
    core._orchestration_repository = repository
    core._command_gateway = CommandGateway(repository, wake_runner=runner.wake)
    return runner, core


def test_timeout_cancel_future_occurrence_and_restart(database, record_property):
    fixture, name = database
    repository = fixture._repository(database=name)

    def schedule_rows():
        with fixture._connection(name) as connection, connection.cursor() as cursor:
            cursor.execute('SELECT * FROM scheduled_tasks ORDER BY id')
            return cursor.fetchall()

    schedules_before = schedule_rows()
    task_id = 'v32-timeout-' + uuid4().hex
    occurrence = datetime.now(timezone.utc).replace(microsecond=0)

    async def exercise(workload):
        runner, core = make_runner(repository, workload)

        async def invoke(job, at):
            return await _execute_scheduled_tool(core, task_id=task_id,
                tool_name='v32_local_workload', arguments={'job': workload.jobs[job], 'account_id': 'v32-http-reader'},
                scheduled_for=at, cron_expression='0 8 * * *')

        await runner.start()
        try:
            timed_out = await invoke('timeout', occurrence)
            row = await _until(lambda: repository.get_run(timed_out['run_id']),
                lambda value: value['status'] == 'FAILED_TERMINAL', timeout=20)
            assert row['execution_attempt_count'] == workload.calls['timeout'] == workload.timeouts['timeout'] == 3
            assert row['retryable'] == 0 and row['worker_id'] is None
            pending = asyncio.create_task(invoke('cancel', occurrence + timedelta(days=1)))
            await _until(lambda: workload.calls['cancel'], bool)
            with repository.unit_of_work() as uow:
                command = uow.commands.get_by_idempotency('scheduler',
                    f'scheduler:{task_id}:{(occurrence + timedelta(days=1)).isoformat()}')
            assert command is not None
            with fixture._connection(name) as connection, connection.cursor() as cursor:
                cursor.execute('SELECT run_id FROM agent_runs WHERE command_id=%s', (command['command_id'],))
                cancelled_id = cursor.fetchone()['run_id']
            await _until(lambda: repository.get_run(cancelled_id), lambda value: value['status'] == 'FAILED_RETRYABLE')
            repository.request_run_cancel(cancelled_id, requested_by_type='console_admin',
                requested_by_id='v32-isolated-admin', reason='Cancel only this occurrence')
            runner.wake(cancelled_id)
            await pending
            cancelled = await _until(lambda: repository.get_run(cancelled_id),
                lambda value: value['status'] == 'CANCELLED')
            assert cancelled['status'] == 'CANCELLED', cancelled
            assert workload.calls['cancel'] == 1
        finally:
            await runner.stop()
        # Fresh Runner and scheduler entry object, same durable database and
        # exact occurrence: no new execution after the restart.
        workload.available = True
        runner, core = make_runner(repository, workload)
        await runner.start()
        try:
            replay = await invoke('timeout', occurrence)
            cancelled_replay = await invoke('cancel', occurrence + timedelta(days=1))
            assert replay['run_id'] == timed_out['run_id']
            assert cancelled_replay['run_id'] == cancelled['run_id']
            assert workload.calls == Counter({'timeout': 3, 'cancel': 1})
            future = await invoke('future', occurrence + timedelta(days=2))
            assert future['status'] == 'COMPLETED', future
            assert workload.calls['future'] == 1
            assert schedule_rows() == schedules_before
            record_property('actual_http_calls', dict(workload.calls))
            record_property('actual_read_timeouts', dict(workload.timeouts))
            record_property('preserved_schedule_rows', len(schedules_before))
            record_property('run_ids', [timed_out['run_id'], cancelled['run_id'], future['run_id']])
        finally:
            await runner.stop()

    with HttpReadWorkload() as workload:
        asyncio.run(exercise(workload))
