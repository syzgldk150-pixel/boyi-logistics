"""Current SERVICE_V2 requests wait in-process, without replaying business work."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.test_direct_plugin_invocation_mysql import (
    ACTOR, _install, _legacy_counts, _service_package, direct_runtime, direct_repository,  # noqa: F401
)


@pytest.mark.parametrize('round_index', range(20))
@pytest.mark.parametrize('reason', ['same_instance', 'capacity'])
def test_busy_request_waits_then_executes_once_without_resubmission(direct_runtime, monkeypatch, round_index, reason, record_property):
    management, runtime, identity = direct_runtime
    assert runtime.service.resource_wait_seconds == 30.0
    assert runtime.service.max_concurrency == 16
    before = _legacy_counts(management.repository)
    second_identity = identity
    if reason == 'capacity':
        second_identity = _install(management, _service_package(management.task_env,
            plugin_id='capacity_wait_' + uuid4().hex[:8]))
        monkeypatch.setattr(runtime.service, 'max_concurrency', 1)
        monkeypatch.setattr(runtime.service, 'max_waiting', 1)
    entered, release = threading.Event(), threading.Event()
    executions = []
    original_execute, original_verify = runtime.router.execute, runtime.service.verifier.verify
    async def execute(*args, **kwargs):
        executions.append(time.monotonic())
        return await original_execute(*args, **kwargs)
    def verify(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_verify(*args, **kwargs)
    monkeypatch.setattr(runtime.router, 'execute', execute)
    monkeypatch.setattr(runtime.service.verifier, 'verify', verify)
    first = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert entered.wait(10)
    request_id = str(uuid4())
    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            receipts = list(pool.map(lambda _: management.policy.invoke_console(second_identity,
                request_id=request_id, actor=ACTOR), range(20)))
        assert len({row['invocation_id'] for row in receipts}) == 1
        waiting = receipts[0]
        assert all(row['status'] == 'STARTING' and row['waiting_for_resource'] for row in receipts)
        assert len(executions) == 1
        if reason == 'capacity':
            excess = management.policy.invoke_console(second_identity, request_id=str(uuid4()), actor=ACTOR)
            assert excess['status'] == 'FAILED' and '等待请求过多' in excess['error_summary']
        with management.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) AS n FROM automation_project_generation_leases WHERE invocation_id=%s', (waiting['invocation_id'],))
            assert cursor.fetchone()['n'] == 0
        released_at = time.monotonic()
    finally:
        release.set()
    assert runtime.service.wait_sync(first['invocation_id'])['status'] == 'COMPLETED'
    completed = runtime.service.wait_sync(waiting['invocation_id'])
    assert completed['status'] == 'COMPLETED' and len(executions) == 2
    resumed_seconds = executions[1] - released_at
    assert 0 <= resumed_seconds <= 2
    replay = management.policy.invoke_console(second_identity, request_id=request_id, actor=ACTOR)
    assert replay['invocation_id'] == completed['invocation_id'] and replay['status'] == 'COMPLETED'
    assert len(executions) == 2 and not runtime.service.active_invocations()
    assert _legacy_counts(management.repository) == before
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)
    record_property('scenario', reason)
    record_property('concurrent_submissions', len(receipts))
    record_property('resume_after_release_seconds', resumed_seconds)
    record_property('resource_wait_budget_seconds', runtime.service.resource_wait_seconds)


@pytest.mark.parametrize('round_index', range(20))
def test_restart_ends_waiting_request_without_replaying_it(direct_runtime, monkeypatch, round_index, record_property):
    from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
    management, runtime, identity = direct_runtime
    entered, release = threading.Event(), threading.Event()
    original_prepare = runtime.service._prepare_arguments
    calls = []
    original_execute = runtime.router.execute
    def prepare(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_prepare(*args, **kwargs)
    async def execute(*args, **kwargs):
        calls.append(kwargs['trusted_invocation_context']['invocation_id'])
        return await original_execute(*args, **kwargs)
    async def forbidden(*args, **kwargs):
        raise AssertionError('restart cannot replay waiting business')
    monkeypatch.setattr(runtime.service, '_prepare_arguments', prepare)
    monkeypatch.setattr(runtime.router, 'execute', execute)
    first = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert entered.wait(10)
    waiting = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert waiting['status'] == 'STARTING' and waiting['waiting_for_resource']
    fresh = DirectPluginInvocationService(runtime.service.repository, SimpleNamespace(execute=forbidden),
        SimpleNamespace(verify=forbidden), release_hold_provider=lambda: False)
    try:
        asyncio.run_coroutine_threadsafe(fresh.startup(), runtime.loop).result(5)
        settled = fresh.get(waiting['invocation_id'])
        assert settled['status'] == 'FAILED' and settled['error_code'] == 'SERVICE_INTERRUPTED'
        assert fresh.active_invocations() == [] and not settled['waiting_for_resource']
    finally:
        release.set()
    runtime.service.wait_sync(first['invocation_id'])
    assert runtime.service.wait_sync(waiting['invocation_id']) == settled
    assert waiting['invocation_id'] not in calls
    with management.repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        cursor.execute('SELECT COUNT(*) AS n FROM automation_project_generation_leases WHERE invocation_id=%s', (waiting['invocation_id'],))
        assert cursor.fetchone()['n'] == 0
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)
    record_property('scenario', 'restart_waiter')
