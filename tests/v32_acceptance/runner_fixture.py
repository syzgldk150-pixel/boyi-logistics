"""Real MySQL Runner, plugin subprocess, generation lease and local Broker.

Only the external primitive handlers and authoritative context providers are
injectable. No direct runner or manufactured completion result is installed.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import threading

from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer, LocalCoreAutomationBroker
from agent.automation_plugins.catalog import CompositeToolRegistry
from agent.automation_plugins.core_adapter import AccountManagerSessionResolver, RegisteredCoreAutomationBrokerAdapter
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.migration import PluginMigrationRuntimeCoordinator
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox
from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.control_plane_service import ControlPlaneService
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.policy_engine import PolicyEngine
from agent.orchestration.result_verifier import ResultVerifier
from agent.orchestration.workflow_runner import WorkflowRunner
from tests.v32_acceptance.management_fixture import UnavailablePort
from shared.contracts import api_success


class RunnerFixture:
    def __init__(self, management, *, handlers=None, account_manager=None, context_builder=None, saved_resource_provider=None):
        self.management = management
        # The real Linux Broker needs a short Unix socket path. Own this
        # directory explicitly instead of requiring an optional TMPDIR value
        # or inheriting a checkout path that can exceed the socket limit.
        self.temporary = tempfile.TemporaryDirectory(prefix="v32-runner-", dir="/tmp")
        self.catalog = CompositeToolRegistry(management.core_catalog, management.catalog)
        self.issuer = LocalBrokerCapabilityIssuer(Path(self.temporary.name) / "broker.sock",
            write_attempt_recorder=management.runtime_repository.record_write_attempt)
        self.broker = LocalCoreAutomationBroker(issuer=self.issuer,
            adapter=RegisteredCoreAutomationBrokerAdapter(handlers=management.broker_handlers if handlers is None else handlers,
                account_resolver=AccountManagerSessionResolver(account_manager or management.account_manager),
                resource_resolver=management.binding_resolver))
        self.migration = PluginMigrationRuntimeCoordinator(management.packages)
        self.router = PluginExecutionRouter(core_executor=UnavailablePort(), capability_issuer=self.issuer,
            sandbox_launcher=BubblewrapPluginSandbox("/usr/bin/bwrap"),
            generation_leases=management.runtime_repository, migration_runtime=self.migration,
            release_hold_provider=lambda: False)
        self.execution = RegisteredToolExecutionAdapter(catalog=self.catalog, executor=self.router)
        self.policy = PolicyEngine(self.catalog, project_policy_provider=management.policy.evaluate_invocation)
        self.approval_service = ApprovalService(management.repository, self.policy)
        self.runner = WorkflowRunner(repository=management.repository, catalog=self.catalog,
            saved_resource_provider=saved_resource_provider,
            execution_port=self.execution, context_builder=context_builder or ContextBuilder(),
            planner=DeterministicPlanner(self.catalog), validator=PlanValidator(self.catalog),
            policy=self.policy, approval_service=self.approval_service,
            verifier=ResultVerifier(management.runtime_repository, self.migration),
            worker_id=f"v32-isolated-{os.getpid()}", worker_concurrency=4, browser_concurrency=3,
            poll_interval_seconds=0.1)
        self.control_plane = ControlPlaneService(management.repository, self.approval_service,
            wake_runner=self.runner.wake)
        # The exact production read service and signed admin middleware serve
        # browser polling; no manufactured run status is returned.
        management.app.add_api_route('/internal/v1/runs/{run_id}',
            lambda run_id: api_success(self.control_plane.get_run(run_id)), methods=['GET'])
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="v32-real-runner", daemon=True)
        self.canary = None

    async def _start(self):
        self.canary = await self.router.startup_sandbox_canary()
        if not self.canary.healthy:
            raise RuntimeError(f"real sandbox canary failed: {self.canary.code}")
        await self.broker.start()
        await self.runner.start()
        self.management.targets.set_wake_runner(self.runner.wake)

    async def _stop(self):
        await self.runner.stop()
        await self.broker.stop()

    def __enter__(self):
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(timeout=30)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_error):
        try:
            if self.thread.is_alive():
                asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(timeout=30)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            self.loop.close()
            self.temporary.cleanup()

    def snapshot(self):
        return {"sandbox": {"healthy": self.canary.healthy, "code": self.canary.code},
            "worker_concurrency": 4, "browser_concurrency": 3,
            "execution": "RegisteredToolExecutionAdapter -> PluginExecutionRouter -> bwrap/prlimit subprocess",
            "broker": "real local Unix socket, signed one-use grant",
            "persistence": "same isolated MySQL repository, generation leases and receipts"}
