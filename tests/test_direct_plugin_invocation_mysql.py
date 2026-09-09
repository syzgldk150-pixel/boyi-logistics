"""Direct Invocation persistence and genuine Service-v2 subprocess proof."""
import asyncio
from hashlib import sha256
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.orchestration.models import Actor, ActorType
from tests.direct_invocation_fixture import DirectFixture, direct_repository  # noqa: F401
from tests.v32_acceptance.management_fixture import ManagementFixture
from agent.orchestration.models import OrchestrationError

ROOT = Path(__file__).resolve().parents[1]
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "direct-test-admin", ("super_admin",), authenticated_by="mysql_admin_session")


@pytest.fixture(scope="module")
def direct_runtime(direct_repository):  # noqa: F811 - imported pytest fixture
    root = ROOT / ".t" / ("direct-" + uuid4().hex[:6])
    root.mkdir(parents=True)
    source, archive = root / "source", root / "package.zip"
    init_service_v2_source(source, plugin_id="direct_identity", name="直接执行测试", version="1.0.0")
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    with ManagementFixture(connection_factory=direct_repository._connection_factory, runtime_root=root / "runtime", enable_directory_faults=False) as management:
        installed = management.management.install_service_v2(package, request_id=str(uuid4()), transport_package_sha256=sha256(package).hexdigest(), raw_intent=json.dumps({"instance_name": "直接执行测试", "permissions_confirmed": True}), actor=ACTOR)
        identity = installed["automation_id"]
        entry = management.catalog.require(identity)
        management.management.save_plugin_settings(identity, config={}, account_bindings={}, resource_bindings={}, request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
        entry = management.catalog.require(identity)
        management.management.set_enabled(identity, enabled=True, request_id=str(uuid4()), expected_record_version=entry.record_version, actor=ACTOR)
        management.targets.reconcile_project(identity)
        with DirectFixture(management, directory=root / "ipc") as runtime:
            yield management, runtime, identity


def _legacy_counts(repository):
    with repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
        result = {}
        for table in ("agent_commands", "work_items", "agent_runs", "agent_run_steps"):
            cursor.execute("SELECT COUNT(*) AS n FROM " + table)
            result[table] = cursor.fetchone()["n"]
        return result


def test_real_service_plugin_uses_invocation_foreign_key_without_legacy_rows(direct_runtime):
    management, runtime, identity = direct_runtime
    before = _legacy_counts(management.repository)
    request_id = str(uuid4())
    receipt = management.policy.invoke_console(identity, request_id=request_id, actor=ACTOR)
    assert "run_id" not in receipt and "invocation_id" in receipt
    result = runtime.service.wait_sync(receipt["invocation_id"])
    assert result["status"] == "COMPLETED", result
    assert result["result"]["data"] == {"message": "Service v2 example is ready."}
    replay = management.policy.invoke_console(identity, request_id=request_id, actor=ACTOR)
    assert replay["invocation_id"] == receipt["invocation_id"]
    assert _legacy_counts(management.repository) == before
    with management.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
        cursor.execute("SELECT invocation_id,orchestration_run_id,outcome FROM automation_project_generation_leases WHERE invocation_id=%s", (receipt["invocation_id"],))
        leases = cursor.fetchall()
    assert len(leases) == 1 and leases[0]["orchestration_run_id"] is None
    assert leases[0]["outcome"] == "SUCCEEDED"


def test_business_request_dedup_and_explicit_new_call(direct_runtime):
    management, runtime, _identity = direct_runtime
    before = _legacy_counts(management.repository)
    calls = []
    async def work():
        calls.append("called")
        return {"status": "SUCCESS", "data": {"value": len(calls)}}
    async def execute():
        request = str(uuid4())
        args = dict(operation="isolated.business.query", request_id=request, actor_id=ACTOR.actor_id, source="console", arguments={}, handler=work, write=False)
        first = await runtime.service.call_business(**args)
        repeated = await runtime.service.call_business(**args)
        args["request_id"] = str(uuid4())
        second = await runtime.service.call_business(**args)
        return first, repeated, second
    first, repeated, second = asyncio.run_coroutine_threadsafe(execute(), runtime.loop).result(timeout=10)
    assert first["status"] == second["status"] == "COMPLETED"
    assert first["invocation_id"] == repeated["invocation_id"] != second["invocation_id"]
    assert calls == ["called", "called"]
    assert _legacy_counts(management.repository) == before


def _service_package(root, *, plugin_id, version="1.0.0", sleep_seconds=0, fail=False, message=None):
    source = root / (plugin_id + "-" + version)
    archive = root / (source.name + ".zip")
    init_service_v2_source(source, plugin_id=plugin_id, name="隔离生命周期测试", version=version)
    main = source / "payload" / "main.py"
    content = main.read_text()
    trigger = "        _read_request()"
    assert content.count(trigger) == 1
    extra = (f"\n        import time\n        time.sleep({sleep_seconds!r})" if sleep_seconds else "") + ('\n        raise ValueError("isolated deliberate failure")' if fail else "")
    content = content.replace(trigger, trigger + extra)
    if message is not None:
        content = content.replace("Service v2 example is ready.", message)
    main.write_text(content)
    build_service_v2_package(source, archive)
    return archive.read_bytes()


def _install(management, package, *, account_bindings=None):
    installed = management.management.install_service_v2(package, request_id=str(uuid4()), transport_package_sha256=sha256(package).hexdigest(), raw_intent=json.dumps({"instance_name": "隔离生命周期", "permissions_confirmed": True}), actor=ACTOR)
    identity = installed["automation_id"]
    entry = management.catalog.require(identity)
    management.management.save_plugin_settings(identity, config={}, account_bindings=account_bindings or {}, resource_bindings={}, request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    entry = management.catalog.require(identity)
    management.management.set_enabled(identity, enabled=True, request_id=str(uuid4()), expected_record_version=entry.record_version, actor=ACTOR)
    management.targets.reconcile_project(identity)
    return identity


def test_real_subprocess_cancel_releases_only_after_exit_and_new_call_runs(direct_runtime):
    management, runtime, _ = direct_runtime
    identity = _install(management, _service_package(management.task_env, plugin_id="cancel_direct", sleep_seconds=1))
    before = _legacy_counts(management.repository)
    first = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    async def cancel_after_process_start():
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            entries = [item for item in runtime.router._running.values() if item.get("automation_id") == identity and item.get("proc") is not None]
            if entries:
                process = entries[0]["proc"]
                assert process.returncode is None
                result = await runtime.service.cancel(first["invocation_id"])
                assert process.returncode is not None
                return result
            await asyncio.sleep(.01)
        raise AssertionError("actual subprocess did not start")
    cancelled = asyncio.run_coroutine_threadsafe(cancel_after_process_start(), runtime.loop).result(timeout=10)
    assert cancelled["status"] == "CANCELLED"
    second = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert second["invocation_id"] != first["invocation_id"]
    assert runtime.service.wait_sync(second["invocation_id"])["status"] == "COMPLETED"
    assert _legacy_counts(management.repository) == before


def test_failed_package_has_no_backlog_or_automatic_retry(direct_runtime):
    management, runtime, working = direct_runtime
    identity = _install(management, _service_package(management.task_env, plugin_id="failed_direct", fail=True))
    receipts = [management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)]
    assert runtime.service.wait_sync(receipts[0]["invocation_id"])["status"] == "FAILED"
    receipts.append(management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR))
    assert runtime.service.wait_sync(receipts[1]["invocation_id"])["status"] == "FAILED"
    assert receipts[0]["invocation_id"] != receipts[1]["invocation_id"]
    successful = management.policy.invoke_console(working, request_id=str(uuid4()), actor=ACTOR)
    assert runtime.service.wait_sync(successful["invocation_id"])["status"] == "COMPLETED"
    assert len(runtime.service.list_recent(identity)) == 2
    assert not any(item["automation_id"] == identity for item in runtime.service.active_invocations())


def test_cancel_during_actual_verification_waits_for_result_and_keeps_instance_owned(direct_runtime, monkeypatch):
    management, runtime, identity = direct_runtime
    entered, release = threading.Event(), threading.Event()
    original = runtime.service.verifier.verify
    def verify(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise AssertionError('verification test barrier timed out')
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.service.verifier, 'verify', verify)
    receipt = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert entered.wait(10), 'real plugin did not reach verification'
    futures = []
    try:
        futures = [asyncio.run_coroutine_threadsafe(runtime.service.cancel(receipt['invocation_id']), runtime.loop) for _ in range(2)]
        async def observed_cancellation():
            for _ in range(100):
                if runtime.service.get(receipt['invocation_id'])['status'] == 'CANCELLING':
                    await asyncio.sleep(.02)
                    return
                await asyncio.sleep(.01)
            raise AssertionError('cancellation never reached the active invocation')
        asyncio.run_coroutine_threadsafe(observed_cancellation(), runtime.loop).result(timeout=5)
        assert all(not future.done() for future in futures)
        assert any(row['invocation_id'] == receipt['invocation_id'] for row in runtime.service.active_invocations())
        rejected = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
        assert rejected['status'] == 'FAILED' and rejected['error_code'] == 'EXECUTION_RESOURCE_BUSY'
    finally:
        release.set()
        results = [future.result(timeout=10) for future in futures]
    assert all(result['status'] == 'COMPLETED' for result in results)
    assert all(result['result']['data'] == {'message': 'Service v2 example is ready.'} for result in results)
    assert not any(row['invocation_id'] == receipt['invocation_id'] for row in runtime.service.active_invocations())
    next_call = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert runtime.service.wait_sync(next_call['invocation_id'])['status'] == 'COMPLETED'


@pytest.fixture
def bound_direct_runtime(direct_runtime):
    from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, DailyAccounts
    original, _, _ = direct_runtime
    directory = ROOT / ".t" / ("bound-" + uuid4().hex[:6])
    directory.mkdir(parents=True)
    source, archive = directory / "source", directory / "package.zip"
    init_service_v2_source(source, plugin_id="bound_" + uuid4().hex[:8], name="Isolated bound compute", version="1.0.0")
    path = source / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["account_roles"] = [{"role": "reader", "allowed_systems": ["ronghui"], "required": True}]
    path.write_text(json.dumps(manifest))
    build_service_v2_package(source, archive)
    with ManagementFixture(connection_factory=original.repository._connection_factory, runtime_root=directory / "runtime", account_manager=DailyAccounts(), enable_directory_faults=False) as management:
        identity = _install(management, archive.read_bytes(), account_bindings={"reader": [ACCOUNT_ID]})
        with DirectFixture(management, directory=directory / "ipc") as runtime:
            yield management, runtime, identity, ACCOUNT_ID


@pytest.mark.parametrize("stage", ["startup_state", "account_validation", "argument_preparation", "business_startup_state"])
def test_preflight_cancellation_drains_threads_before_releasing_ownership(bound_direct_runtime, monkeypatch, stage):
    management, runtime, identity, account_id = bound_direct_runtime
    entered, release = threading.Event(), threading.Event()
    executor_calls, business_calls = [], []
    original_execute = runtime.router.execute
    async def actual_execute(*args, **kwargs):
        executor_calls.append(True)
        return await original_execute(*args, **kwargs)
    monkeypatch.setattr(runtime.router, "execute", actual_execute)
    def barrier():
        entered.set()
        if not release.wait(10):
            raise AssertionError("preflight test barrier timed out")
    if stage in {"startup_state", "business_startup_state"}:
        original = runtime.service.repository.update
        def update(*args, **kwargs):
            if kwargs.get("status") == "RUNNING":
                barrier()
            return original(*args, **kwargs)
        monkeypatch.setattr(runtime.service.repository, "update", update)
    elif stage == "account_validation":
        original = runtime.service._account_validator
        def validate(value):
            assert value == account_id
            barrier()
            return original(value)
        monkeypatch.setattr(runtime.service, "_account_validator", validate)
    else:
        original = runtime.service._prepare_arguments
        def prepare(*args):
            barrier()
            return original(*args)
        monkeypatch.setattr(runtime.service, "_prepare_arguments", prepare)
    async def handler():
        business_calls.append(True)
        return {"status": "SUCCESS", "data": {"saved": True}}
    async def business_call():
        return await runtime.service.call_business(operation="isolated.preflight.write", request_id=str(uuid4()), actor_id=ACTOR.actor_id, source="console", arguments={}, handler=handler, resource_keys=(("plugin-instance", identity),), account_ids=(account_id,))
    business = stage == "business_startup_state"
    pending = asyncio.run_coroutine_threadsafe(business_call(), runtime.loop) if business else None
    receipt = None if business else management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    cancellations = []
    try:
        assert entered.wait(10), "real invocation did not reach the setup barrier"
        active = runtime.service.active_invocations()
        assert len(active) == 1
        invocation_id = active[0]["invocation_id"]
        if receipt:
            assert invocation_id == receipt["invocation_id"]
        cancellations = [asyncio.run_coroutine_threadsafe(runtime.service.cancel(invocation_id), runtime.loop) for _ in range(2)]
        async def cancelled_but_still_owned():
            for _ in range(100):
                if runtime.service.get(invocation_id)["status"] == "CANCELLING":
                    await asyncio.sleep(.02)
                    return
                await asyncio.sleep(.01)
            raise AssertionError("cancellation did not reach the current invocation")
        asyncio.run_coroutine_threadsafe(cancelled_but_still_owned(), runtime.loop).result(timeout=5)
        assert all(not future.done() for future in cancellations)
        assert [row["invocation_id"] for row in runtime.service.active_invocations()] == [invocation_id]
        with pytest.raises(OrchestrationError) as blocked:
            runtime.service.begin_credentials_change(account_id)
        assert blocked.value.code == "ACCOUNT_EXECUTION_BUSY"
        rejected = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
        assert rejected["status"] == "FAILED" and rejected["error_code"] == "EXECUTION_RESOURCE_BUSY"
        assert executor_calls == business_calls == []
    finally:
        release.set()
        results = [future.result(timeout=10) for future in cancellations]
    assert all(result["status"] == "CANCELLED" for result in results)
    if pending is not None:
        assert pending.result(timeout=5)["status"] == "CANCELLED"
    assert executor_calls == business_calls == []
    assert runtime.service.active_invocations() == []
    with management.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM automation_project_generation_leases WHERE invocation_id=%s", (invocation_id,))
        assert cursor.fetchone()["n"] == 0
    release_account = runtime.service.begin_credentials_change(account_id)
    release_account()
    if business:
        completed = asyncio.run_coroutine_threadsafe(business_call(), runtime.loop).result(timeout=10)
        assert completed["status"] == "COMPLETED" and business_calls == [True]
    else:
        fresh = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
        completed = runtime.service.wait_sync(fresh["invocation_id"])
        assert completed["status"] == "COMPLETED", completed
        assert executor_calls == [True]


def test_credential_change_guard_and_business_call_share_atomic_admission(direct_runtime):
    _management, runtime, _ = direct_runtime
    touched = []
    async def handler():
        touched.append(True)
        return {"status": "SUCCESS", "data": {}}
    release = runtime.service.begin_credentials_change("isolated-account")
    async def invoke():
        return await runtime.service.call_business(operation="isolated.account.operation", request_id=str(uuid4()), actor_id=ACTOR.actor_id, source="console", arguments={}, handler=handler, write=False, account_ids=("isolated-account",))
    try:
        denied = asyncio.run_coroutine_threadsafe(invoke(), runtime.loop).result(timeout=5)
        assert denied["status"] == "FAILED" and denied["error_code"] == "EXECUTION_RESOURCE_BUSY"
        assert touched == []
    finally:
        release()
    accepted = asyncio.run_coroutine_threadsafe(invoke(), runtime.loop).result(timeout=5)
    assert accepted["status"] == "COMPLETED" and touched == [True]


def test_business_cancellation_during_result_persistence_drains_and_returns_actual_result(direct_runtime, monkeypatch):
    _management, runtime, _identity = direct_runtime
    entered, release = threading.Event(), threading.Event()
    original = runtime.service.repository.update
    def update(*args, **kwargs):
        if kwargs.get("status") == "COMPLETED":
            entered.set()
            if not release.wait(10):
                raise AssertionError("business result persistence barrier timed out")
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.service.repository, "update", update)
    actual_results = []
    async def handler():
        result = {"status": "SUCCESS", "data": {"saved": True}}
        actual_results.append(result)
        return result
    async def business_call():
        return await runtime.service.call_business(operation="isolated.business.persist", request_id=str(uuid4()), actor_id=ACTOR.actor_id, source="console", arguments={}, handler=handler, account_ids=("persist-account",))
    pending = asyncio.run_coroutine_threadsafe(business_call(), runtime.loop)
    cancellations = []
    try:
        assert entered.wait(10)
        assert len(actual_results) == 1
        active = runtime.service.active_invocations()
        assert len(active) == 1
        invocation_id = active[0]["invocation_id"]
        cancellations = [asyncio.run_coroutine_threadsafe(runtime.service.cancel(invocation_id), runtime.loop) for _ in range(2)]
        async def observe():
            for _ in range(100):
                if runtime.service.get(invocation_id)["status"] == "CANCELLING":
                    await asyncio.sleep(.02)
                    return
                await asyncio.sleep(.01)
            raise AssertionError("business cancellation was not recorded")
        asyncio.run_coroutine_threadsafe(observe(), runtime.loop).result(timeout=5)
        assert all(not future.done() for future in cancellations) and not pending.done()
        assert runtime.service.active_invocations() == active
        with pytest.raises(OrchestrationError) as blocked:
            runtime.service.begin_credentials_change("persist-account")
        assert blocked.value.code == "ACCOUNT_EXECUTION_BUSY"
    finally:
        release.set()
        results = [future.result(timeout=10) for future in cancellations]
    assert all(result["status"] == "COMPLETED" and result["result"] == actual_results[0] for result in results)
    assert pending.result(timeout=5)["status"] == "COMPLETED"
    assert runtime.service.active_invocations() == []
    release_account = runtime.service.begin_credentials_change("persist-account")
    release_account()


def test_generation_upgrade_and_rollback_use_real_invocation_leases(direct_runtime):
    management, runtime, _ = direct_runtime
    packages = [_service_package(management.task_env, plugin_id="upgrade_direct", version=version, message=message) for version, message in (("1.0.0", "baseline"), ("1.0.1", "candidate"))]
    identity = _install(management, packages[0])
    observed = []
    for package, message in ((None, "baseline"), (packages[1], "candidate"), (packages[0], "baseline")):
        if package is not None:
            entry = management.catalog.require(identity)
            management.management.upgrade(identity, package, request_id=str(uuid4()), expected_record_version=entry.record_version, transport_package_sha256=sha256(package).hexdigest(), actor=ACTOR)
        receipt = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
        result = runtime.service.wait_sync(receipt["invocation_id"])
        assert result["status"] == "COMPLETED", result
        assert result["output"] == {"message": message}
        observed.append(result["generation"])
    assert observed[0] < observed[1] < observed[2]


def test_trusted_entrypoint_rejects_forged_actor_before_execution(direct_runtime):
    management, runtime, identity = direct_runtime
    before = runtime.service.list_recent(identity)
    forged = Actor(ActorType.CONSOLE_ADMIN, "forged", ("super_admin",), authenticated_by="client_claim")
    with pytest.raises(OrchestrationError) as rejected:
        management.policy.invoke_console(identity, request_id=str(uuid4()), actor=forged)
    assert rejected.value.code == "ACTION_FORBIDDEN"
    assert runtime.service.list_recent(identity) == before


def test_plain_read_drains_actual_work_without_persisting_invocation(direct_runtime):
    management, runtime, _ = direct_runtime
    with management.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM automation_plugin_invocations")
        before = cursor.fetchone()["n"]
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        async def read():
            started.set()
            await release.wait()
            return {"value": "actual-read-finished"}
        task = asyncio.create_task(runtime.service.call_read(operation="isolated.query", handler=read, account_ids=("read-account",)))
        await started.wait()
        assert runtime.service.active_read_count() == 1
        with pytest.raises(OrchestrationError) as conflict:
            runtime.service.begin_credentials_change("read-account")
        assert conflict.value.code == "ACCOUNT_EXECUTION_BUSY"
        task.cancel()
        await asyncio.sleep(.02)
        assert runtime.service.active_read_count() == 1 and not task.done()
        original_hold = runtime.service._hold
        runtime.service._hold = lambda: True
        try:
            with pytest.raises(OrchestrationError) as held:
                await runtime.service.call_read(operation="isolated.query", handler=read)
            assert held.value.code == "PLUGIN_RELEASE_HELD"
        finally:
            runtime.service._hold = original_hold
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.service.active_read_count() == 0
        release_change = runtime.service.begin_credentials_change("read-account")
        release_change()
    asyncio.run_coroutine_threadsafe(exercise(), runtime.loop).result(timeout=5)
    with management.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM automation_plugin_invocations")
        assert cursor.fetchone()["n"] == before


def test_restart_settles_interrupted_facts_without_execution_or_recovery(direct_runtime):
    from types import SimpleNamespace
    from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
    management, runtime, identity = direct_runtime
    entry = management.catalog.require(identity)
    row = runtime.service._row(operation="automation." + identity + ".run", source="console", actor_id=ACTOR.actor_id, request_id=str(uuid4()), request_key=str(uuid4()), arguments={})
    row.update(automation_id=identity, plugin_id=entry.plugin_id, plugin_version=entry.installed_version, generation=entry.committed_snapshot.generation, status="RUNNING", owner_id="previous-process")
    runtime.service.repository.create(row)
    before = _legacy_counts(management.repository)
    async def forbidden(*args, **kwargs):
        raise AssertionError("restart must not execute or retry interrupted calls")
    fresh = DirectPluginInvocationService(runtime.service.repository, SimpleNamespace(execute=forbidden), SimpleNamespace(verify=forbidden), release_hold_provider=lambda: False)
    asyncio.run_coroutine_threadsafe(fresh.startup(), runtime.loop).result(timeout=5)
    settled = fresh.get(row["invocation_id"])
    assert settled["status"] == "FAILED" and settled["error_code"] == "SERVICE_INTERRUPTED"
    assert fresh.active_invocations() == [] and _legacy_counts(management.repository) == before
    receipt = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
    assert runtime.service.wait_sync(receipt["invocation_id"])["status"] == "COMPLETED"
