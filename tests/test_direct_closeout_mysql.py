"""Repeated current-process admission, deduplication and genuine drain proofs."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from uuid import uuid4

import pytest

from agent.orchestration.models import OrchestrationError
from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from tests.test_direct_plugin_invocation_mysql import ACTOR, direct_runtime, direct_repository  # noqa: F401


@pytest.mark.parametrize('round_index', range(20))
def test_concurrent_request_replays_and_busy_identity_remain_terminal(direct_runtime, monkeypatch, round_index, record_property):
    host, runtime, identity = direct_runtime
    entered, release, started = threading.Event(), threading.Event(), threading.Event()
    original_verify, original_execute, original_start = runtime.service.verifier.verify, runtime.router.execute, runtime.service.start
    executions, captured, start_times = [], [], []
    async def execute(*args, **kwargs):
        executions.append(True)
        start_times.append(time.monotonic())
        started.set()
        return await original_execute(*args, **kwargs)
    def verify(*args, **kwargs):
        entered.set()
        assert release.wait(20), 'real verification never released'
        return original_verify(*args, **kwargs)
    def capture(**kwargs):
        captured.append(kwargs)
        return original_start(**kwargs)
    monkeypatch.setattr(runtime.router, 'execute', execute)
    monkeypatch.setattr(runtime.service.verifier, 'verify', verify)
    monkeypatch.setattr(runtime.service, 'start', capture)
    request = str(uuid4())
    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            calls = list(pool.map(lambda _: host.policy.invoke_console(identity, request_id=request, actor=ACTOR), range(20)))
        identities = {row['invocation_id'] for row in calls}
        assert len(identities) == 1
        assert entered.wait(10)
        assert len(executions) == 1
        with pytest.raises(OrchestrationError, match='同一请求'):
            original_start(**{**captured[0], 'arguments': {'different': True}})
        busy_request = str(uuid4())
        busy = host.policy.invoke_console(identity, request_id=busy_request, actor=ACTOR)
        assert busy['status'] == 'FAILED' and busy['error_code'] == 'EXECUTION_RESOURCE_BUSY'
        assert busy['finished_at'] is not None
        assert len(executions) == 1
    finally:
        release.set()
    result = runtime.service.wait_sync(calls[0]['invocation_id'])
    assert result['status'] == 'COMPLETED'
    replay = host.policy.invoke_console(identity, request_id=busy_request, actor=ACTOR)
    assert replay['invocation_id'] == busy['invocation_id'] and replay['status'] == 'FAILED'
    started.clear()
    accepted_at = time.monotonic()
    new = host.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert started.wait(2), 'idle current runtime did not start a new request within two seconds'
    elapsed = start_times[-1] - accepted_at
    assert elapsed <= 2
    assert runtime.service.wait_sync(new['invocation_id'])['status'] == 'COMPLETED'
    assert len(executions) == 2 and not runtime.service.active_invocations()
    record_property('round', round_index)
    record_property('concurrent_submissions', len(calls))
    record_property('actual_executions_for_replays', len(executions) - 1)
    record_property('new_start_seconds', elapsed)
    record_property('runtime_model', 'SERVICE_V2')


@pytest.mark.parametrize('round_index', range(20))
def test_repeated_cancel_drains_real_verification_before_reuse(direct_runtime, monkeypatch, round_index, record_property):
    host, runtime, identity = direct_runtime
    entered, release = threading.Event(), threading.Event()
    verify = runtime.service.verifier.verify
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return verify(*args, **kwargs)
    monkeypatch.setattr(runtime.service.verifier, 'verify', held)
    call = host.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert entered.wait(10)
    cancellations = [asyncio.run_coroutine_threadsafe(runtime.service.cancel(call['invocation_id']), runtime.loop) for _ in range(2)]
    try:
        deadline = time.monotonic() + 2
        while runtime.service.get(call['invocation_id'])['status'] != 'CANCELLING':
            assert time.monotonic() < deadline
            threading.Event().wait(.01)
        assert all(not future.done() for future in cancellations)
        busy = host.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
        assert busy['error_code'] == 'EXECUTION_RESOURCE_BUSY'
    finally:
        release.set()
    assert all(future.result(10)['status'] == 'COMPLETED' for future in cancellations)
    assert not runtime.service.active_invocations()
    new = host.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert runtime.service.wait_sync(new['invocation_id'])['status'] == 'COMPLETED'
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)


def test_slow_admission_database_does_not_hold_control_or_read_loop(direct_runtime, monkeypatch):
    _host, runtime, _identity = direct_runtime
    entered, release = threading.Event(), threading.Event()
    lookup = runtime.service.repository.by_request
    def slow_lookup(*args):
        entered.set()
        assert release.wait(10)
        return lookup(*args)
    monkeypatch.setattr(runtime.service.repository, 'by_request', slow_lookup)
    async def business():
        return {'ok': True, 'data': {'synthetic': 'actual completed handler'}}
    pending = asyncio.run_coroutine_threadsafe(runtime.service.call_business(
        operation='slow-admission', request_id=str(uuid4()), actor_id=ACTOR.actor_id,
        source='console', arguments={}, handler=business, write=False), runtime.loop)
    assert entered.wait(5)
    async def control_read():
        assert runtime.service.active_invocations() == []
        return await runtime.service.call_read(operation='independent-read', handler=business)
    try:
        read = asyncio.run_coroutine_threadsafe(control_read(), runtime.loop)
        assert read.result(timeout=2)['ok'] is True
        assert not pending.done()
    finally:
        release.set()
    assert pending.result(timeout=10)['status'] == 'COMPLETED'


@pytest.mark.parametrize('round_index', range(20))
@pytest.mark.parametrize('stage', ['before_execution', 'after_execution'])
def test_restart_fences_actual_inflight_invocation_and_late_completion(direct_runtime, monkeypatch, round_index, stage, record_property):
    host, runtime, identity = direct_runtime
    entered, release = threading.Event(), threading.Event()
    original = runtime.service._prepare_arguments if stage == 'before_execution' else runtime.service.verifier.verify
    publications = []
    publish = runtime.service._publish_result
    def observed_publish(*args, **kwargs):
        publications.append(args[0])
        return publish(*args, **kwargs)
    monkeypatch.setattr(runtime.service, '_publish_result', observed_publish)
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return original(*args, **kwargs)
    if stage == 'before_execution':
        monkeypatch.setattr(runtime.service, '_prepare_arguments', held)
    else:
        monkeypatch.setattr(runtime.service.verifier, 'verify', held)
    receipt = host.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert entered.wait(10)
    # A new actual Direct service performs its production startup settlement.
    # The prior callback remains paused to exercise the late-owner boundary.
    monkeypatch.setattr(runtime.issuer, 'host_operation_guard', runtime.service.host_operation)
    monkeypatch.setattr(runtime.router, 'direct_invocations', runtime.service)
    fresh = DirectPluginInvocationService(runtime.service.repository, runtime.router,
        runtime.service.verifier, release_hold_provider=lambda: False)
    try:
        asyncio.run_coroutine_threadsafe(fresh.startup(), runtime.loop).result(5)
        settled = fresh.get(receipt['invocation_id'])
        assert settled['status'] == 'FAILED' and settled['error_code'] == 'SERVICE_INTERRUPTED', settled
        assert fresh.active_invocations() == []
        with host.repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
            cursor.execute('SELECT outcome,released_at FROM automation_project_generation_leases WHERE invocation_id=%s', (receipt['invocation_id'],))
            before_leases = cursor.fetchall()
    finally:
        release.set()
    old_result = runtime.service.wait_sync(receipt['invocation_id'])
    assert old_result == settled, old_result
    assert not publications
    with host.repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        cursor.execute('SELECT outcome,released_at FROM automation_project_generation_leases WHERE invocation_id=%s', (receipt['invocation_id'],))
        leases = cursor.fetchall()
        assert leases == before_leases and all(row['released_at'] is not None for row in leases), leases
        if stage == 'before_execution':
            assert not leases, leases
    assert not runtime.service.active_invocations()
    record_property('round', round_index)
    record_property('runtime_model', 'SERVICE_V2')
    record_property('scenario', stage)
