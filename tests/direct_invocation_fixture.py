"""Real MySQL + signed package runtime with no WorkflowRunner construction."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer, LocalCoreAutomationBroker
from agent.automation_plugins.core_adapter import AccountManagerSessionResolver, RegisteredCoreAutomationBrokerAdapter
from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox
from agent.orchestration.result_verifier import ResultVerifier
from agent.plugin_business_results import PluginBusinessResults
from shared.plugin_invocation_repository import PluginInvocationRepository
from tests.v32_acceptance.management_fixture import UnavailablePort


@pytest.fixture(scope="module")
def direct_repository():
    import pymysql
    from tests import test_mysql_orchestration_integration as mysql_support
    if os.environ.get("RUN_MYSQL_INTEGRATION") != "1":
        pytest.skip("requires explicitly isolated MySQL")
    helper = type("DirectPluginDatabase", (mysql_support.MySqlOrchestrationIntegrationTests,), {})
    helper.pymysql = pymysql
    helper.host, helper.port = os.environ["AGENT_DB_HOST"], int(os.environ["AGENT_DB_PORT"])
    helper.user, helper.password = os.environ["AGENT_DB_USER"], os.environ["AGENT_DB_PASS"]
    helper.database = "direct_plugin_" + uuid4().hex + "_test"
    assert helper.host == "127.0.0.1"
    helper.runner = mysql_support._load_migration_runner()
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{helper.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    helper._apply_through(helper.database, "017")
    helper._seed_required_project_resources(helper.database)
    # This fixture serves both Agent and Console; use the actual current schema,
    # including shared identity permissions, rather than stopping at Invocation.
    helper._run_migrations(helper.database)
    result = helper._repository()
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("AGENT_DB_NAME", helper.database)
        yield result
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"DROP DATABASE `{helper.database}`")


class DirectFixture:
    def __init__(self, management, *, directory: Path | None = None, handlers=None, account_manager=None, saved_resource_provider=None):
        self.management = management
        if directory is None:
            directory = Path(__file__).resolve().parents[1] / ".t" / ("inv-" + uuid4().hex[:8])
        directory.mkdir(parents=True, exist_ok=True)
        self.issuer = LocalBrokerCapabilityIssuer(directory / "b.sock", write_attempt_recorder=management.runtime_repository.record_write_attempt)
        effective_handlers = dict(management.broker_handlers if handlers is None else handlers)
        connectors = getattr(management, "connector_registry", None)
        self.broker_errors = []
        if connectors is not None:
            from agent.automation_plugins.capability_proxy_v2 import build_service_v2_capability_handler_map
            effective_handlers.update(build_service_v2_capability_handler_map(management.repository, connector_registry=connectors))
            invoke_service = effective_handlers[("service.invoke", "*")]
            async def observed_service(context, arguments):
                try:
                    return await invoke_service(context, arguments)
                except Exception as error:
                    self.broker_errors.append(f"{type(error).__name__}: {error}")
                    raise
            effective_handlers[("service.invoke", "*")] = observed_service
        self.broker = LocalCoreAutomationBroker(issuer=self.issuer, adapter=RegisteredCoreAutomationBrokerAdapter(handlers=effective_handlers, account_resolver=AccountManagerSessionResolver(account_manager or management.account_manager), resource_resolver=management.binding_resolver, connector_registry=connectors))
        self.router = PluginExecutionRouter(core_executor=UnavailablePort(), capability_issuer=self.issuer, sandbox_launcher=BubblewrapPluginSandbox("/usr/bin/bwrap"), generation_leases=management.runtime_repository, release_hold_provider=lambda: False)
        results = PluginBusinessResults(management.repository)
        self.service = DirectPluginInvocationService(PluginInvocationRepository(management.repository), self.router, ResultVerifier(management.runtime_repository), release_hold_provider=lambda: False, saved_resource_provider=saved_resource_provider, account_validator=getattr(account_manager or management.account_manager, "require_active_binding_descriptor", None), prepare_arguments=results.prepare, publish_result=results.publish)
        management.policy.direct_invocations = self.service
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    async def _start(self):
        canary = self.canary = await self.router.startup_sandbox_canary()
        if not canary.healthy:
            raise RuntimeError(f"sandbox unavailable: {canary.code}")
        await self.broker.start()
        await self.service.startup()

    def __enter__(self):
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(timeout=30)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        async def stop():
            await self.service.stop()
            await self.broker.stop()
        if self.thread.is_alive():
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout=60)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
        self.loop.close()

    def snapshot(self):
        return {"sandbox": {"healthy": self.canary.healthy, "code": self.canary.code},
            "max_concurrency": self.service.max_concurrency,
            "execution": "DirectPluginInvocationService -> PluginExecutionRouter -> bwrap/prlimit subprocess",
            "broker": "real local Unix socket, signed grant, host-operation resource scope",
            "persistence": "Invocation facts, actual generation leases and write receipts; no Command/Run/Step"}
