"""Real MySQL plugin lifecycle/catalog and signed HTTP fixture for list QA.

Execution and binding ports remain explicitly unavailable until a full runtime
is composed. This fixture never fabricates a successful business execution.
"""
from __future__ import annotations

import hashlib
import asyncio
import ast
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from collections import Counter
from uuid import uuid4

sys.stdout.reconfigure(encoding="utf-8")

import pymysql
import httpx
from Crypto.PublicKey import ECC
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.configuration import AutomationProjectConfigurationService
from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.management_api import create_automation_plugin_management_router
from agent.automation_plugins.management_repository import MySQLAutomationPluginManagementRepository
from agent.automation_plugins.mysql_repository import MySQLAutomationPluginRepositoryAdapter
from agent.automation_plugins.package import Ed25519TrustStore
from agent.automation_plugins.runtime_repository import MySQLAutomationPluginCatalogRepositoryAdapter, MySQLAutomationProjectConfigurationReadAdapter
from agent.automation_plugins.runtime_repository import MySQLAutomationPluginRuntimeAdapter
from agent.automation_plugins.production import (
    MySQLRuntimeTargetService, ProductionRuntimeCoeffectProvider,
    ProductionRuntimeEffectDriver, ProductionRuntimeEffectPlanner,
)
from agent.automation_plugins.generation import AutomationRuntimeReconciler
from agent.automation_plugins.service_registry import ServiceRegistry
from agent.automation_plugins.service_v2_projection import ManagedContributionRegistry
from agent.automation_plugins.storage import FilesystemPluginStorage, LockedVirtualEnvironmentBuilder
from agent.http_security import authenticate_internal_request
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.orchestration.automation_project_api import create_automation_project_router
from agent.orchestration.automation_project_policy_service import AutomationProjectPolicyService
from agent.orchestration.automation_project_entrypoints import TrustedDynamicArgumentResolver
from agent.orchestration.command_gateway import CommandGateway
from agent.tool_registry import ToolRegistry
from shared.contracts import api_failure
from shared.orchestration_repository import OrchestrationRepository
from shared.plugin_management import MODULE_DATASETS
from shared.redaction import redact_text
from shared.service_identity import ConsoleIdentityError, ConsoleIdentityVerifier, build_console_identity_headers

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_ENV = PROJECT_ROOT / ".task_tmp" / "v32" / "environment"
E2E_RUNTIME_SUBDIRECTORIES = ("plugin-installed", "synthetic-plugins", "console-runtime")
QUERY_STATS = {"connections": 0, "queries": Counter(), "sql_seconds": 0.0}
QUERY_LOCK = threading.Lock()


def e2e_fixture_lock(*, exclusive=False):
    """Hold shared use, or exclusive reset, of this dedicated E2E fixture."""
    import fcntl

    if (os.environ.get("AGENT_DB_NAME") != "v32_e2e_test"
            or os.environ.get("AGENT_DB_HOST") != "127.0.0.1"
            or os.environ.get("AGENT_DB_PORT") != "33326"
            or TASK_ENV.resolve() != TASK_ENV):
        raise RuntimeError("E2E runtime lock requires its exact owned environment")
    TASK_ENV.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(TASK_ENV / "e2e-fixture.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(descriptor, "a+b")
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("E2E fixture is in use; wait for all participants before preparing or resetting it") from exc
    except BaseException:
        handle.close()
        raise
    return handle


class MeasuredCursor(pymysql.cursors.DictCursor):
    def execute(self, query, args=None):
        started = time.monotonic()
        try:
            return super().execute(query, args)
        finally:
            # Query templates only. Bound values, row contents and auth are not logged.
            fingerprint = " ".join(str(query).split())
            with QUERY_LOCK:
                QUERY_STATS["queries"][fingerprint] += 1
                QUERY_STATS["sql_seconds"] += time.monotonic() - started


def connect():
    if os.environ["AGENT_DB_NAME"] != "v32_e2e_test":
        raise RuntimeError("exact v32_e2e_test required")
    with QUERY_LOCK:
        QUERY_STATS["connections"] += 1
    return pymysql.connect(
        host="127.0.0.1", port=int(os.environ["AGENT_DB_PORT"]),
        user=os.environ["AGENT_DB_USER"], password=os.environ["AGENT_DB_PASS"],
        database="v32_e2e_test", charset="utf8mb4", autocommit=False,
        cursorclass=MeasuredCursor,
    )


class UnavailablePort:
    def __getattr__(self, name):
        raise RuntimeError(f"catalog fixture does not implement execution/binding port {name}")


class EmptyIsolatedAccountDirectory:
    """Synthetic compute packages have no accounts; no live directory is read."""

    def list_accounts(self, **_filters):
        return []


class ManagementFixture:
    def __init__(self, *, connection_factory=None, runtime_root=None, account_manager=None,
                 broker_handlers=None, resource_provider=None, upload_signature_verifier=None,
                 enable_directory_faults=True, migration_account_bindings=None):
        self.startup_id = str(uuid4())
        if not os.environ.get("AGENT_DB_NAME", "").endswith("_test") or os.environ.get("AGENT_DB_HOST") != "127.0.0.1":
            raise RuntimeError("explicit isolated loopback test database required")
        self._e2e_lock = e2e_fixture_lock() if os.environ["AGENT_DB_NAME"] == "v32_e2e_test" else None
        self.task_env = Path(runtime_root) if runtime_root is not None else TASK_ENV
        self.task_env.mkdir(parents=True, exist_ok=True)
        self.account_manager = account_manager or EmptyIsolatedAccountDirectory()
        self.broker_handlers = dict(broker_handlers or {})
        handler_keys = tuple(self.broker_handlers)
        self.internal_token = secrets.token_urlsafe(32)
        self.signing_secret = secrets.token_urlsafe(32)
        self.requests = []
        self.resource_calls = 0
        self.external_delay_seconds = 30
        self.fault_phase = "measurement"
        self.fault_calls = []
        self.fault_http_paths = ("/internal/v1/admin/accounts", "/internal/v1/automation/workers")
        self.enable_directory_faults = bool(enable_directory_faults)
        self.repository = OrchestrationRepository(connection_factory or connect)
        self.packages = MySQLAutomationPluginRepositoryAdapter(
            self.repository, release_hold_provider=lambda: False,
            migration_account_bindings=migration_account_bindings,
        )
        self.catalog = PluginCatalog(
            MySQLAutomationPluginCatalogRepositoryAdapter(self.repository),
            MySQLAutomationProjectConfigurationReadAdapter(self.repository),
            allowed_execution_platforms=("server",),
            migration_pair_provider=self.packages.get_catalog_migration_pair,
        )
        self.management_repository = MySQLAutomationPluginManagementRepository(self.repository)
        self.core_catalog = ToolRegistry()
        self.runtime_repository = MySQLAutomationPluginRuntimeAdapter(self.repository)
        self.binding_resolver = ProductionProjectBindingResolver(
            account_manager=self.account_manager, resource_provider=resource_provider or (lambda _id: None),
            worker_repository=self.management_repository,
        )
        self.service_registry = ServiceRegistry()
        self.contribution_registry = ManagedContributionRegistry()
        self.driver = ProductionRuntimeEffectDriver(
            broker_handler_keys=handler_keys, service_registry=self.service_registry,
            contribution_registry=self.contribution_registry,
        )
        self.reconciler = AutomationRuntimeReconciler(
            repository=self.runtime_repository,
            coeffects=ProductionRuntimeCoeffectProvider(
                core_catalog=self.core_catalog, broker_handler_keys=handler_keys,
                account_manager=self.account_manager, binding_resolver=self.binding_resolver,
                service_registry=self.service_registry,
            ),
            planner=ProductionRuntimeEffectPlanner(), driver=self.driver,
        )
        self.targets = MySQLRuntimeTargetService(
            orchestration_repository=self.repository, catalog=self.catalog,
            core_catalog=self.core_catalog, runtime_repository=self.runtime_repository,
            reconciler=self.reconciler, catalog_repository=self.packages,
        )
        self.driver.restore_from_repository(self.runtime_repository)
        self.storage = FilesystemPluginStorage(self.task_env / "plugin-installed")
        self.lifecycle = AutomationPluginService(
            repository=self.packages, storage=self.storage,
            environments=LockedVirtualEnvironmentBuilder(),
            upload_signature_verifier=upload_signature_verifier or Ed25519TrustStore({"v32-test": ECC.generate(curve="Ed25519").public_key().export_key(format="raw")}),
            allowed_execution_platforms=("server",),
        )
        self.configuration = AutomationProjectConfigurationService(
            catalog=self.catalog, repository=self.management_repository,
            binding_resolver=self.binding_resolver,
        )
        self.management = AutomationPluginManagementService(
            catalog=self.catalog, lifecycle=self.lifecycle,
            configuration=self.configuration, worker_repository=self.management_repository,
            target_service=self.targets, package_repository=self.packages,
            storage=self.storage, release_hold_provider=lambda: False,
            resource_catalog_provider=self.delayed_external_resources,
            contribution_registry=self.contribution_registry,
        )
        self.policy = AutomationProjectPolicyService(
            self.repository, ToolRegistry(), self.catalog,
            dynamic_resolver=TrustedDynamicArgumentResolver(),
            release_hold_provider=lambda: False,
            command_gateway=CommandGateway(self.repository),
            contribution_registry=self.contribution_registry,
        )
        self.app = FastAPI()
        # Execute the exact production handler without importing main's startup
        # code, which loads deployment configuration. No error mapping is copied.
        main_path = PROJECT_ROOT / "agent" / "main.py"
        handler = next(node for node in ast.parse(main_path.read_text(encoding="utf-8")).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "orchestration_error_handler")
        handler.decorator_list = []
        namespace = {"Request": Request, "OrchestrationError": OrchestrationError,
            "JSONResponse": JSONResponse, "api_failure": api_failure, "redact_text": redact_text}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), str(main_path), "exec"), namespace)
        self.app.add_exception_handler(OrchestrationError, namespace[handler.name])
        verifier = ConsoleIdentityVerifier(self.signing_secret)

        @self.app.middleware("http")
        async def signed_identity(request: Request, call_next):
            self.requests.append({"path": request.url.path, "query": request.url.query, "phase": self.fault_phase})
            failure = authenticate_internal_request(
                path=request.url.path, expected_token=self.internal_token,
                provided_token=request.headers.get("X-Agent-Internal-Token", ""),
            )
            if failure:
                return JSONResponse(status_code=failure.status_code, content=api_failure("internal_auth_failed", failure.message))
            target = request.url.path + (("?" + request.url.query) if request.url.query else "")
            try:
                principal = verifier.verify(headers=request.headers, method=request.method,
                    request_target=target, body=await request.body())
            except ConsoleIdentityError as exc:
                return JSONResponse(status_code=401, content=api_failure(exc.code, str(exc)))
            if principal is None:
                return JSONResponse(status_code=403, content=api_failure("ACTION_FORBIDDEN", "signed administrator required"))
            request.state.console_principal = principal
            if self.enable_directory_faults and request.url.path in self.fault_http_paths:
                self.fault_calls.append({"boundary": request.url.path, "phase": self.fault_phase})
                await asyncio.sleep(self.external_delay_seconds)
                return JSONResponse(status_code=503, content=api_failure("V32_ISOLATED_DIRECTORY_FAILURE", "isolated directory deliberately unavailable"))
            return await call_next(request)

        def actor_provider(request):
            principal = request.state.console_principal
            return Actor(ActorType.CONSOLE_ADMIN, principal["actor_id"],
                roles=tuple(principal["roles"]), authenticated_by=principal["authenticated_by"])

        self.app.include_router(create_automation_plugin_management_router(
            service_provider=lambda: self.management, actor_provider=actor_provider,
            policy_service_provider=lambda: self.policy,
            include_worker_routes=False,
        ))
        self.app.include_router(create_automation_project_router(
            service_provider=lambda: self.policy, actor_provider=actor_provider,
        ))
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("isolated management HTTP server failed to start")
            threading.Event().wait(0.01)

    def delayed_external_resources(self):
        if self.fault_phase == "measurement":
            self.resource_calls += 1
        self.fault_calls.append({"boundary": "workflow_resource_directory", "phase": self.fault_phase})
        threading.Event().wait(self.external_delay_seconds)
        raise RuntimeError("explicit isolated external-directory failure")

    async def verify_fault_injection(self):
        """Exercise the exact installed faults before, outside timing samples."""
        self.fault_phase = "canary"
        principal = {"actor_type": "console_admin", "actor_id": "v32-fault-canary",
            "roles": ["super_admin"], "authenticated_by": "mysql_admin_session"}
        async def http_fault(path):
            headers = build_console_identity_headers(secret=self.signing_secret, method="GET",
                request_target=path, body=b"", principal=principal, nonce=uuid4().hex)
            headers["X-Agent-Internal-Token"] = self.internal_token
            started = time.monotonic()
            async with httpx.AsyncClient(timeout=40) as client:
                response = await client.get(self.url + path, headers=headers)
            elapsed = time.monotonic() - started
            return {"boundary": path, "status": "PASS" if response.status_code == 503 and elapsed >= 30 else "FAIL",
                "elapsed_ms": elapsed * 1000, "http_status": response.status_code}
        async def resource_fault():
            started = time.monotonic()
            try:
                await asyncio.to_thread(self.delayed_external_resources)
            except RuntimeError as exc:
                elapsed = time.monotonic() - started
                return {"boundary": "workflow_resource_directory", "status": "PASS" if elapsed >= 30 else "FAIL",
                    "elapsed_ms": elapsed * 1000, "failure": str(exc)}
            raise AssertionError("isolated resource fault did not fail")
        try:
            results = await asyncio.gather(*(http_fault(path) for path in self.fault_http_paths), resource_fault())
            if any(item["status"] != "PASS" for item in results):
                raise AssertionError(f"fault injection canary failed: {results}")
            return results
        finally:
            self.fault_phase = "measurement"

    def seed_instances(self, *, per_module=50, package_version="1.0.1"):
        fixture_actor = Actor(ActorType.CONSOLE_ADMIN, "v32-fixture-seed",
            roles=("super_admin",), authenticated_by="mysql_admin_session")
        source_parent = self.task_env / "synthetic-plugins"
        source_parent.mkdir(exist_ok=True)
        results = {}
        for module in ("automation", "finance", "customer_service"):
            plugin_id = "v32_list_" + module
            source = source_parent / (plugin_id + "-" + package_version)
            archive = source_parent / (plugin_id + "-" + package_version + ".zip")
            if not source.exists():
                init_service_v2_source(source, plugin_id=plugin_id, name="V3.2 合成 " + module, version=package_version)
                manifest_path = source / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["contributes"]["harness"] = []
                manifest["management"] = (
                    {"purpose": "action", "module": "automation", "dataset": "", "version": ""}
                    if module == "automation" else
                    {"purpose": "collector", "module": module, "dataset": MODULE_DATASETS[module], "version": "1"}
                )
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            if not archive.exists():
                build_service_v2_package(source, archive)
            package = archive.read_bytes()
            digest = hashlib.sha256(package).hexdigest()
            existing = self.management.catalog_projection(actor=fixture_actor, module=module, summary=True)["instances"]
            names = {item["instance_name"] for item in existing}
            for index in range(per_module):
                name = f"V3.2 合成 {module} {index + 1:02d}"
                if name in names:
                    continue
                self.management.install_service_v2(package,
                    request_id=str(uuid4()), transport_package_sha256=digest,
                    raw_intent=json.dumps({"instance_name": name, "permissions_confirmed": True}),
                    actor=fixture_actor)
            records = self.management.catalog_projection(actor=fixture_actor, module=module, summary=True)["instances"]
            for record in records:
                entry = self.catalog.require(record["automation_id"])
                if not entry.configured:
                    self.management.save_plugin_settings(
                        entry.automation_id, config={}, account_bindings={}, resource_bindings={},
                        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version,
                        actor=fixture_actor,
                    )
                self.targets.reconcile_project(record["automation_id"])
            results[module] = {"instances": len(records), "version": package_version, "artifact_sha256": digest}
        return results

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("isolated management server did not stop")
        if self._e2e_lock is not None:
            self._e2e_lock.close()
            self._e2e_lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


if __name__ == "__main__":
    with ManagementFixture() as fixture:
        results = fixture.seed_instances()
        results["resource_directory_calls"] = fixture.resource_calls
        (TASK_ENV / "synthetic-list-seed.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(results, indent=2))
