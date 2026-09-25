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
        monkeypatch.setattr(runtime.service, 'resource_wait_seconds', .2)
        waiting = host.policy.invoke_console(identity, request_id=busy_request, actor=ACTOR)
        assert waiting['status'] == 'STARTING' and waiting['waiting_for_resource']
        retry = host.policy.invoke_console(identity, request_id=busy_request, actor=ACTOR)
        assert retry['invocation_id'] == waiting['invocation_id']
        busy = runtime.service.wait_sync(waiting['invocation_id'], timeout_seconds=2)
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
        assert busy['status'] == 'STARTING' and busy['waiting_for_resource']
        started = time.monotonic()
        waiting_cancelled = asyncio.run_coroutine_threadsafe(runtime.service.cancel(busy['invocation_id']), runtime.loop).result(2)
        cancel_seconds = time.monotonic() - started
        assert waiting_cancelled['status'] == 'CANCELLED' and cancel_seconds <= 2
    finally:
        release.set()
    assert all(future.result(10)['status'] == 'COMPLETED' for future in cancellations)
    assert not runtime.service.active_invocations()
    new = host.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert runtime.service.wait_sync(new['invocation_id'])['status'] == 'COMPLETED'
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)
    record_property('waiting_cancel_seconds', cancel_seconds)


@pytest.mark.parametrize('round_index', range(20))
def test_waiter_cancellation_drains_settlement_before_releasing_ownership(direct_runtime, monkeypatch, round_index, record_property):
    host, runtime, identity = direct_runtime
    admission_entered, admit = threading.Event(), threading.Event()
    settlement_entered, settle = threading.Event(), threading.Event()
    cancelling_persisted = threading.Event()
    wait_for_admission = runtime.service._wait_for_admission
    update = runtime.service.repository.update
    has_started_write = runtime.service.repository.has_started_write
    executed = []
    execute = runtime.router.execute

    async def gated_admission(call_id):
        admission_entered.set()
        assert await asyncio.to_thread(admit.wait, 10)
        await wait_for_admission(call_id)

    def cancelling_update(call_id, **kwargs):
        result = update(call_id, **kwargs)
        if kwargs['status'] == 'CANCELLING':
            # Cancellation is delivered before this database write. Track its
            # completion independently from entry into failure settlement.
            cancelling_persisted.set()
            admit.set()
            assert settlement_entered.wait(10)
        return result

    def held_settlement(call_id):
        settlement_entered.set()
        assert settle.wait(10)
        return has_started_write(call_id)

    async def observed_execute(*args, **kwargs):
        executed.append(True)
        return await execute(*args, **kwargs)

    monkeypatch.setattr(runtime.service, '_wait_for_admission', gated_admission)
    monkeypatch.setattr(runtime.service.repository, 'update', cancelling_update)
    monkeypatch.setattr(runtime.service.repository, 'has_started_write', held_settlement)
    monkeypatch.setattr(runtime.router, 'execute', observed_execute)
    call = host.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert admission_entered.wait(5)
    cancelled = asyncio.run_coroutine_threadsafe(runtime.service.cancel(call['invocation_id']), runtime.loop)
    try:
        assert settlement_entered.wait(5)
        assert cancelling_persisted.wait(5)

        async def interrupt_settlement():
            task = runtime.service._active[call['invocation_id']]['task']
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            assert call['invocation_id'] in runtime.service._active

        asyncio.run_coroutine_threadsafe(interrupt_settlement(), runtime.loop).result(2)
        assert runtime.service.get(call['invocation_id'])['status'] == 'CANCELLING'
        assert not cancelled.done() and not executed
    finally:
        admit.set()
        settle.set()
    result = cancelled.result(5)
    assert result['status'] == 'CANCELLED' and result['finished_at'] is not None
    assert not executed and not runtime.service.active_invocations()
    assert runtime.service.wait_sync(call['invocation_id'])['status'] == 'CANCELLED'
    record_property('runtime_model', 'SERVICE_V2')
    record_property('round', round_index)
    record_property('cancellation_during_settlement', True)


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
