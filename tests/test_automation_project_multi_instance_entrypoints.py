from __future__ import annotations

import asyncio
import copy
import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping
from unittest import TestCase

from pydantic import ValidationError

from agent.automation_plugins.binding_resolver import (
    ProductionProjectBindingResolver,
)
from agent.automation_plugins.catalog import CompositeToolRegistry, PluginCatalog
from agent.automation_plugins.manifest import (
    AutomationPluginManifest,
    canonical_json_bytes,
    governance_anchor_from_tool_contract,
)
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginInstanceRecord,
    PluginProjectState,
    PluginTrustSource,
    PluginVersionRecord,
    RuntimeCoeffectKind,
    RuntimeCoeffectSnapshot,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
    RuntimeReconcileState,
)
from agent.orchestration.automation_project_api import ProjectInvokeRequest
from agent.orchestration.automation_project_entrypoints import (
    AutomationProjectEntrypoints,
    CommittedAutomationProjectRouteResolver,
)
from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    CommandReceipt,
    OrchestrationError,
    RunStatus,
)
from shared.automation_project_authorization import AutomationEntrypoint


PLUGIN_ID = "synthetic_multi_instance_action"
PACKAGE_SHA256 = "1" * 64
ENTRYPOINTS = ("scheduler", "console", "feishu", "webhook")


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _manifest() -> AutomationPluginManifest:
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"marker": {"type": "string"}},
        "required": ["marker"],
    }
    tool_contract = {
        "name": PLUGIN_ID,
        "version": "1.0.0",
        "description": "Synthetic repeated-instance action",
        "operation_type": "read",
        "risk_level": "low",
        "approval": {"mode": "none"},
        "permissions": [],
        "idempotency": {"required": True},
        "retry": {"mode": "never"},
        "evidence": [],
        "postconditions": [],
        "project_full_auto_allowed": False,
        "executor": "payload/main.py",
        "input_schema": copy.deepcopy(config_schema),
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
    }
    invocation_contracts = {
        entrypoint: {
            "input_schema": copy.deepcopy(config_schema),
            "argument_template": {"marker": {"source": "project_config", "key": "marker"}},
            "dynamic_resolvers": {},
        }
        for entrypoint in ENTRYPOINTS
    }
    return AutomationPluginManifest.from_mapping(
        {
            "schema_version": 1,
            "plugin_id": PLUGIN_ID,
            "name": "Synthetic repeated-instance action",
            "version": "1.0.0",
            "description": "One signed package installed as two projects",
            "execution_platform": "server",
            "runtime": {
                "kind": "python_subprocess",
                "entrypoint": "payload/main.py",
            },
            "config_schema": config_schema,
            "account_roles": [
                {
                    "role": "business_account",
                    "allowed_systems": ["ronghui"],
                    "required": True,
                    "argument_field": None,
                    "collection": False,
                }
            ],
            "resource_roles": [
                {
                    "role": "feishu_route",
                    "allowed_kinds": ["feishu_route"],
                    "required": True,
                },
                {
                    "role": "webhook_route",
                    "allowed_kinds": ["webhook_route"],
                    "required": True,
                },
            ],
            "scheduling": {
                "supported": True,
                "allowed_kinds": ["daily_times"],
                "max_daily_times": 4,
            },
            "allowed_entrypoints": list(ENTRYPOINTS),
            "invocation_contracts": invocation_contracts,
            "governance_anchor": governance_anchor_from_tool_contract(tool_contract),
            "tool_contract": tool_contract,
            "worker_requirement": {
                "required": False,
                "interactive_session": False,
                "supported_os": ["linux"],
                "queue_deadline_seconds": 60,
            },
            "project_full_auto_allowed": False,
            "runtime_permissions": {
                "network": False,
                "browser": False,
                "office": False,
                "file_roles": [],
                "broker_operations": [],
                "max_broker_calls": 0,
            },
        }
    )


def _resource(
    *,
    resource_kind: str,
    route_key: str,
    configuration_version: int,
) -> dict[str, Any]:
    route_field = "path" if resource_kind == "webhook_route" else "route_key"
    return {
        "resource_kind": resource_kind,
        route_field: route_key,
        "_meta": {
            "source": "test_resource_pool",
            "configuration_version": configuration_version,
            "config_sha256": f"{configuration_version:x}"[-1] * 64,
            "updated_at": "2026-08-15 12:00:00",
        },
    }


def _resource_descriptor(resource_id: str, resource: Mapping[str, Any]) -> dict[str, Any]:
    metadata = resource["_meta"]
    return {
        "resource_id": resource_id,
        "resource_kind": resource["resource_kind"],
        "source": metadata["source"],
        "configuration_version": metadata["configuration_version"],
        "config_sha256": metadata["config_sha256"],
        "updated_at": metadata["updated_at"],
    }


def _snapshot(
    *,
    automation_id: str,
    generation: int,
    config_version: int,
    marker: str,
    account_id: str,
    resource_bindings: Mapping[str, str],
    manifest: AutomationPluginManifest,
) -> RuntimeGenerationSnapshot:
    project_config = {"marker": marker}
    account_bindings = {"business_account": account_id}
    schedule = {
        "kind": "daily_times",
        "times": ["07:00"],
        "enabled": True,
    }
    compiled_invocations = {
        entrypoint: {"arguments": dict(project_config), "dynamic_resolvers": {}} for entrypoint in ENTRYPOINTS
    }
    runtime_descriptor = {
        "install_metadata": {
            "install_root": "/srv/automation-plugins/synthetic/1.0.0",
            "python_relative": "venv/bin/python",
        },
        "runtime": copy.deepcopy(dict(manifest.runtime)),
        "runtime_permissions": copy.deepcopy(dict(manifest.runtime_permissions)),
        "account_roles": [copy.deepcopy(dict(item)) for item in manifest.account_roles],
        "resource_roles": [copy.deepcopy(dict(item)) for item in manifest.resource_roles],
    }
    action_contract = copy.deepcopy(dict(manifest.tool_contract))
    governance_anchor = copy.deepcopy(dict(manifest.governance_anchor))
    execution_metadata = {
        "project_config_version": config_version,
        "project_config": project_config,
        "account_bindings": account_bindings,
        "resource_bindings": dict(resource_bindings),
        "device_binding": None,
        "schedule": schedule,
        "compiled_invocations": compiled_invocations,
        "runtime_descriptor": runtime_descriptor,
        "action_contract": action_contract,
        "governance_anchor": governance_anchor,
    }
    return RuntimeGenerationSnapshot(
        automation_id=automation_id,
        generation=generation,
        plugin_id=manifest.plugin_id,
        plugin_version=manifest.version,
        package_sha256=PACKAGE_SHA256,
        manifest_sha256=manifest.manifest_sha256,
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        project_config_sha256=_sha(project_config),
        account_bindings_sha256=_sha(account_bindings),
        resource_bindings_sha256=_sha(resource_bindings),
        device_binding_sha256=_sha(None),
        schedule_sha256=_sha(schedule),
        core_registry_sha256=_sha({"core_registry": "test"}),
        tool_contract_sha256=_sha(action_contract),
        invocation_contracts_sha256=_sha(manifest.to_mapping()["invocation_contracts"]),
        compiled_invocations_sha256=_sha(compiled_invocations),
        runtime_descriptor_sha256=_sha(runtime_descriptor),
        governance_anchor_sha256=_sha(governance_anchor),
        policy_contract_sha256=_sha({"mode": "REQUIRE_EACH_RUN"}),
        enabled_entrypoints=ENTRYPOINTS,
        execution_metadata=execution_metadata,
        created_at=datetime.now(timezone.utc),
    )


class _CatalogRepository:
    def __init__(self, projects: list[PluginInstanceRecord]) -> None:
        self.projects = {project.automation_id: project for project in projects}

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        return self.projects.get(automation_id)

    def list_instances(self) -> list[PluginInstanceRecord]:
        return list(self.projects.values())


class _ConfigurationRepository:
    def __init__(self, records: list[AutomationProjectConfigRecord]) -> None:
        self.records = {record.automation_id: record for record in records}

    def get_project_config(self, automation_id: str) -> AutomationProjectConfigRecord | None:
        return self.records.get(automation_id)


class _CoreCatalog:
    def __init__(self, manifest: AutomationPluginManifest) -> None:
        self.capability = copy.deepcopy(dict(manifest.tool_contract))
        self.catalog_hash = _sha({"core": PLUGIN_ID})

    def get_capability(self, tool_name: str) -> Mapping[str, Any] | None:
        return self.capability if tool_name == PLUGIN_ID else None

    def list_llm_capabilities(self) -> list[dict[str, Any]]:
        return []

    def list_tools(self) -> list[str]:
        return [PLUGIN_ID]

    def load(self) -> None:
        return None


class _AutomationProjects:
    def __init__(self, state: "_ServiceState") -> None:
        self.state = state

    def get_policy(self, automation_id: str, **_kwargs: Any) -> Mapping[str, Any] | None:
        return self.state.policies.get(automation_id)

    def list_configuration_rows(self, automation_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        row = self.state.schedules.get(automation_id)
        return [copy.deepcopy(row)] if row is not None else []


class _AutomationPlugins:
    def __init__(self, state: "_ServiceState") -> None:
        self.state = state

    def get_project(self, automation_id: str, **_kwargs: Any) -> Mapping[str, Any] | None:
        return self.state.projects.get(automation_id)

    def get_generation_row(
        self,
        automation_id: str,
        generation: int,
        **_kwargs: Any,
    ) -> Mapping[str, Any] | None:
        return self.state.generations.get((automation_id, generation))

    def get_project_config(self, automation_id: str, **_kwargs: Any) -> Mapping[str, Any] | None:
        return self.state.configs.get(automation_id)


class _UnitOfWork:
    def __init__(self, state: "_ServiceState") -> None:
        self.automation_projects = _AutomationProjects(state)
        self.automation_plugins = _AutomationPlugins(state)

    def __enter__(self) -> "_UnitOfWork":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def commit(self) -> None:
        return None


class _ServiceState:
    def __init__(
        self,
        projects: list[PluginInstanceRecord],
        configs: list[AutomationProjectConfigRecord],
    ) -> None:
        self.projects = {
            project.automation_id: {
                "automation_id": project.automation_id,
                "enabled": True,
                "state": "ENABLED",
                "target_generation": project.target_generation,
                "committed_generation": project.committed_generation,
                "reconcile_state": "STABLE",
            }
            for project in projects
        }
        self.configs = {
            config.automation_id: {
                "automation_id": config.automation_id,
                "config_version": config.config_version,
            }
            for config in configs
        }
        self.generations = {
            (project.automation_id, project.committed_generation): {
                "state": "COMMITTED",
                "manifest_sha256": project.committed_snapshot.manifest_sha256,
            }
            for project in projects
            if project.committed_generation is not None and project.committed_snapshot is not None
        }
        self.policies = {
            project.automation_id: {
                "automation_id": project.automation_id,
                "mode": "REQUIRE_EACH_RUN",
                "version": 1,
                "project_generation": project.committed_generation,
                "project_configuration_version": next(
                    config.config_version for config in configs if config.automation_id == project.automation_id
                ),
            }
            for project in projects
        }
        self.schedules = {
            project.automation_id: {
                "id": f"{project.automation_id}-daily",
                "automation_id": project.automation_id,
                "tool_name": f"automation.{project.automation_id}.run",
                "automation_generation": project.committed_generation,
                "tool_params": copy.deepcopy(dict(project.committed_snapshot.execution_metadata["project_config"])),
                "configuration_version": next(
                    config.config_version for config in configs if config.automation_id == project.automation_id
                ),
                "enabled": True,
                "cron_expression": "0 7 * * *",
            }
            for project in projects
            if project.committed_snapshot is not None
        }


class _ServiceRepository:
    def __init__(self, state: _ServiceState) -> None:
        self.state = state

    def unit_of_work(self) -> _UnitOfWork:
        return _UnitOfWork(self.state)


class _Gateway:
    def __init__(self, repository: _ServiceRepository) -> None:
        self.repository = repository
        self.commands: list[Command] = []

    def submit(self, command: Command, *, uow_guard: Any = None) -> CommandReceipt:
        with self.repository.unit_of_work() as uow:
            if uow_guard is not None:
                uow_guard(uow)
            uow.commit()
        self.commands.append(command)
        sequence = len(self.commands)
        return CommandReceipt(
            command_id=command.command_id,
            work_item_id=f"work-{sequence}",
            run_id=f"run-{sequence}",
            status=RunStatus.RECEIVED,
            reused=False,
        )

    async def wait_for_run(self, run_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        del timeout_seconds
        sequence = int(run_id.removeprefix("run-"))
        command = self.commands[sequence - 1]
        return {
            "run_id": run_id,
            "command_id": command.command_id,
            "work_item_id": f"work-{sequence}",
            "status": "COMPLETED",
            "correlation_id": command.correlation_id,
        }


class _RuntimeRepository:
    def __init__(self, generations: Mapping[tuple[str, int], RuntimeGenerationRecord]) -> None:
        self.generations = dict(generations)

    def get_generation(self, automation_id: str, generation: int) -> RuntimeGenerationRecord | None:
        return self.generations.get((automation_id, generation))


class _Accounts:
    def __init__(self, account_ids: tuple[str, ...]) -> None:
        self.account_ids = account_ids

    def list_accounts(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "account_id": account_id,
                "system": "ronghui",
                "is_active": True,
                "updated_at": "2026-08-15 12:00:00",
            }
            for account_id in self.account_ids
        ]


class _Workers:
    @staticmethod
    def get_worker_device(_device_id: str) -> None:
        return None


class AutomationProjectMultiInstanceEntrypointTests(TestCase):
    def setUp(self) -> None:
        self.manifest = _manifest()
        self.resources = {
            "feishu-route-one": _resource(
                resource_kind="feishu_route",
                route_key="instance.one",
                configuration_version=3,
            ),
            "webhook-route-one": _resource(
                resource_kind="webhook_route",
                route_key="hooks/instance-one",
                configuration_version=4,
            ),
            "feishu-route-two": _resource(
                resource_kind="feishu_route",
                route_key="instance.two",
                configuration_version=7,
            ),
            "webhook-route-two": _resource(
                resource_kind="webhook_route",
                route_key="hooks/instance-two",
                configuration_version=8,
            ),
        }
        version = PluginVersionRecord(
            plugin_id=PLUGIN_ID,
            version="1.0.0",
            package_sha256=PACKAGE_SHA256,
            manifest_sha256=self.manifest.manifest_sha256,
            manifest=self.manifest.to_mapping(),
            trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
            install_root="/srv/automation-plugins/synthetic/1.0.0",
            install_metadata={
                "install_root": "/srv/automation-plugins/synthetic/1.0.0",
                "python_relative": "venv/bin/python",
            },
        )
        specifications = (
            (
                "instance-one",
                3,
                11,
                "one",
                "business-account-one",
                {
                    "feishu_route": "feishu-route-one",
                    "webhook_route": "webhook-route-one",
                },
            ),
            (
                "instance-two",
                7,
                22,
                "two",
                "business-account-two",
                {
                    "feishu_route": "feishu-route-two",
                    "webhook_route": "webhook-route-two",
                },
            ),
        )
        self.projects: list[PluginInstanceRecord] = []
        self.configs: list[AutomationProjectConfigRecord] = []
        runtime_generations: dict[tuple[str, int], RuntimeGenerationRecord] = {}
        for (
            automation_id,
            generation,
            config_version,
            marker,
            account_id,
            resource_bindings,
        ) in specifications:
            snapshot = _snapshot(
                automation_id=automation_id,
                generation=generation,
                config_version=config_version,
                marker=marker,
                account_id=account_id,
                resource_bindings=resource_bindings,
                manifest=self.manifest,
            )
            project = PluginInstanceRecord(
                automation_id=automation_id,
                display_name=f"Instance {marker}",
                plugin_id=PLUGIN_ID,
                state=PluginProjectState.ENABLED,
                active_version=version,
                enabled=True,
                target_generation=generation,
                committed_generation=generation,
                reconcile_state=RuntimeReconcileState.STABLE,
                committed_snapshot=snapshot,
            )
            config = AutomationProjectConfigRecord(
                automation_id=automation_id,
                config={"marker": marker},
                account_bindings={"business_account": account_id},
                resource_bindings=resource_bindings,
                schedule={
                    "kind": "daily_times",
                    "times": ["07:00"],
                    "enabled": True,
                },
                config_version=config_version,
                configured=True,
                config_sha256=snapshot.project_config_sha256,
                account_bindings_sha256=snapshot.account_bindings_sha256,
                resource_bindings_sha256=snapshot.resource_bindings_sha256,
                device_binding_sha256=snapshot.device_binding_sha256,
                enabled_entrypoints=ENTRYPOINTS,
            )
            coeffects = tuple(
                RuntimeCoeffectSnapshot(
                    kind=RuntimeCoeffectKind.RESOURCE,
                    key=role,
                    revision=_sha(_resource_descriptor(resource_id, self.resources[resource_id])),
                    ready=True,
                )
                for role, resource_id in resource_bindings.items()
            )
            runtime_generations[(automation_id, generation)] = RuntimeGenerationRecord(
                snapshot=snapshot,
                state=RuntimeGenerationState.COMMITTED,
                coeffects=coeffects,
            )
            self.projects.append(project)
            self.configs.append(config)

        self.catalog = PluginCatalog(
            _CatalogRepository(self.projects),
            _ConfigurationRepository(self.configs),
        )
        self.core_catalog = _CoreCatalog(self.manifest)
        self.state = _ServiceState(self.projects, self.configs)
        self.repository = _ServiceRepository(self.state)
        self.gateway = _Gateway(self.repository)
        self.policy = AutomationProjectPolicyService(
            self.repository,
            self.core_catalog,
            self.catalog,
            command_gateway=self.gateway,
        )
        resource_provider = self.resources.get
        self.binding_resolver = ProductionProjectBindingResolver(
            account_manager=_Accounts(("business-account-one", "business-account-two")),
            resource_provider=resource_provider,
            worker_repository=_Workers(),
        )
        self.routes = CommittedAutomationProjectRouteResolver(
            catalog=self.catalog,
            runtime_repository=_RuntimeRepository(runtime_generations),
            binding_resolver=self.binding_resolver,
            resource_provider=resource_provider,
        )
        self.entrypoints = AutomationProjectEntrypoints(
            self.policy,
            route_resolver=self.routes,
        )

    def test_same_signed_package_runs_two_exact_instances_through_all_entrypoints(self):
        projection = self.catalog.safe_projection()
        self.assertEqual(1, len(projection["plugins"]))
        self.assertEqual(
            {"instance-one", "instance-two"},
            {item["automation_id"] for item in projection["instances"]},
        )
        entries = self.catalog.list()
        self.assertEqual({PLUGIN_ID}, {entry.plugin_id for entry in entries})
        self.assertEqual({PACKAGE_SHA256}, {entry.package_sha256 for entry in entries})
        self.assertEqual(
            {self.manifest.manifest_sha256},
            {entry.manifest_sha256 for entry in entries},
        )
        for entry in entries:
            capability = self.catalog.get_project_capability(entry.automation_id)
            runtime = capability["_plugin_runtime"]
            self.assertEqual(
                entry.committed_generation,
                runtime["generation"],
            )
            self.assertEqual(entry.account_bindings, runtime["account_bindings"])
            self.assertEqual(entry.resource_bindings, runtime["resource_bindings"])
            account_id = entry.account_bindings["business_account"]
            descriptor = self.binding_resolver.describe_account_binding(
                automation_id=entry.automation_id,
                role=entry.account_roles[0],
                account_id=account_id,
            )
            self.assertEqual(account_id, descriptor["account_id"])

        admin = Actor(
            ActorType.CONSOLE_ADMIN,
            "admin-one",
            roles=("admin",),
            authenticated_by="mysql_admin_session",
        )
        for automation_id in ("instance-one", "instance-two"):
            self.policy.invoke_console(
                automation_id,
                request_id=f"console-{automation_id}",
                actor=admin,
            )
            task_id = f"{automation_id}-daily"
            project = self.state.projects[automation_id]
            config = self.state.configs[automation_id]
            self.policy.invoke_trusted(
                automation_id,
                entrypoint=AutomationEntrypoint.SCHEDULER,
                request_id=f"scheduler-{automation_id}",
                actor=Actor(
                    ActorType.SCHEDULER,
                    task_id,
                    roles=("system",),
                    authenticated_by="apscheduler",
                ),
                trusted_context={
                    "task_id": task_id,
                    "scheduled_for": "2026-08-15T07:00:00+08:00",
                    "cron_expression": "0 7 * * *",
                    "configuration_version": config["config_version"],
                },
                expected_automation_generation=project["committed_generation"],
                expected_project_configuration_version=config["config_version"],
            )

        asyncio.run(
            self.entrypoints.invoke_feishu(
                route_key="instance.one",
                event_id="feishu-one",
                sender_id="sender-one",
                chat_id="chat-one",
            )
        )
        asyncio.run(
            self.entrypoints.invoke_feishu(
                route_key="instance.two",
                event_id="feishu-two",
                sender_id="sender-two",
                chat_id="chat-two",
            )
        )
        asyncio.run(
            self.entrypoints.invoke_webhook(
                route_key="hooks/instance-one",
                source_event_id="webhook-one",
                webhook_path="hooks/instance-one",
            )
        )
        asyncio.run(
            self.entrypoints.invoke_webhook(
                route_key="hooks/instance-two",
                source_event_id="webhook-two",
                webhook_path="hooks/instance-two",
            )
        )

        self.assertEqual(8, len(self.gateway.commands))
        expected = {
            "instance-one": {"generation": 3, "marker": "one"},
            "instance-two": {"generation": 7, "marker": "two"},
        }
        seen = set()
        for command in self.gateway.commands:
            invocation = command.automation_invocation
            self.assertIsNotNone(invocation)
            instance = expected[invocation.automation_id]
            seen.add((invocation.automation_id, invocation.entrypoint.value))
            self.assertEqual(instance["generation"], invocation.automation_generation)
            self.assertEqual(
                {"marker": instance["marker"]},
                command.parameters["arguments"],
            )
            self.assertNotIn("account_id", command.parameters["arguments"])
            self.assertNotIn("resource_bindings", command.parameters["arguments"])
            expected_contract = (
                f"scheduler:{invocation.automation_id}-daily"
                if invocation.entrypoint is AutomationEntrypoint.SCHEDULER
                else invocation.entrypoint.value
            )
            self.assertEqual(expected_contract, invocation.contract_id)
        self.assertEqual(
            {(automation_id, entrypoint) for automation_id in expected for entrypoint in ENTRYPOINTS},
            seen,
        )

    def test_generic_llm_and_binding_overrides_are_rejected(self):
        composite = CompositeToolRegistry(self.core_catalog, self.catalog)
        self.assertEqual([], self.catalog.list_llm_capabilities())
        self.assertEqual([], composite.list_llm_capabilities())

        for extra in (
            {"account_id": "business-account-two"},
            {"resource_bindings": {"webhook_route": "webhook-route-two"}},
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ValidationError):
                    ProjectInvokeRequest(
                        request_id="generic-api-forgery",
                        **extra,
                    )

        with self.assertRaises(OrchestrationError) as raised:
            Command(
                command_type="tool.invoke",
                source="legacy_api",
                actor=Actor(ActorType.LEGACY_API, "generic-client"),
                parameters={
                    "tool_name": "automation.instance-one.run",
                    "arguments": {},
                },
                idempotency_key="generic-project-call",
            )
        self.assertEqual("RESERVED_AUTOMATION_CONTEXT", raised.exception.code)

        for envelope, code in (
            (
                {"body": {"account_id": "business-account-two"}, "query": {}},
                "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN",
            ),
            (
                {
                    "body": {"resource_bindings": {"webhook_route": "webhook-route-two"}},
                    "query": {},
                },
                "PROJECT_RESOURCE_OVERRIDE_FORBIDDEN",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(OrchestrationError) as raised:
                    asyncio.run(
                        self.entrypoints.invoke_webhook(
                            route_key="hooks/instance-one",
                            source_event_id=f"forged-{code}",
                            webhook_path="hooks/instance-one",
                            envelope=envelope,
                        )
                    )
                self.assertEqual(code, raised.exception.code)
        self.assertEqual([], self.gateway.commands)


if __name__ == "__main__":
    import unittest

    unittest.main()
