from __future__ import annotations

import copy
import hashlib
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.first_party import (
    deferred_first_party_automation_ids,
    deferred_first_party_plugin_ids,
    release_first_party_automation_ids,
    release_first_party_broker_action_keys,
    release_first_party_instance_seeds,
    resolve_release_first_party_manifests,
)
from agent.automation_plugins.generation import (
    AutomationRuntimeReconciler,
    runtime_generation_health,
)
from agent.automation_plugins.invocation import compile_instance_arguments
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    BootstrapResult,
    PluginInstanceRecord,
    PluginProjectState,
    PluginTrustSource,
    PluginVersionRecord,
    ProjectRuntimeRecord,
    RuntimeEffectRecord,
    RuntimeEffectState,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
    RuntimeReconcileState,
)
from agent.automation_plugins.ports import RuntimeEffectPlan
from agent.automation_plugins.production import (
    MySQLRuntimeTargetService,
    ProductionAutomationPluginRuntime,
    ProductionRuntimeCoeffectProvider,
    ProductionRuntimeEffectPlanner,
    build_runtime_generation_snapshot,
)
from agent.orchestration.automation_project_entrypoints import (
    CommittedAutomationProjectRouteResolver,
)
from agent.orchestration.models import OrchestrationError
from agent.phase7_resource_import import BUILTIN_RESOURCES
from agent.tool_registry import ToolRegistry, validate_schema_instance
from plugin_core_adapters import build_production_first_party_core_handler_map
from scripts.automation_project_resource_preflight import (
    AUTOMATION_PROJECT_CODE_OWNED_RESOURCE_KEYS,
    AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS,
)
from shared.automation_project_authorization import AutomationEntrypoint
from shared.automation_project_manifest import FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES


_DEFERRED_R7_AUTOMATION_IDS = frozenset(
    {"r7_arrival_checkin", "r7_departure_checkin"}
)
_DEFERRED_R7_RESOURCE_KEYS = frozenset(
    {
        "automation.feishu_route.r7_arrival_checkin",
        "automation.feishu_route.r7_departure_checkin",
    }
)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class _Accounts:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = tuple(copy.deepcopy(dict(row)) for row in rows)

    def list_accounts(self, **_kwargs: object) -> list[dict[str, Any]]:
        return [copy.deepcopy(dict(row)) for row in self._rows]


class _Workers:
    @staticmethod
    def get_worker_device(_device_id: str) -> None:
        return None


class _CatalogRepository:
    def __init__(self, projects: Mapping[str, PluginInstanceRecord]) -> None:
        self.projects = dict(projects)

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        return self.projects.get(automation_id)

    def list_instances(self) -> Sequence[PluginInstanceRecord]:
        return tuple(self.projects.values())


class _ConfigurationRepository:
    def __init__(
        self,
        configurations: Mapping[str, AutomationProjectConfigRecord],
    ) -> None:
        self.configurations = dict(configurations)

    def get_project_config(
        self,
        automation_id: str,
    ) -> AutomationProjectConfigRecord | None:
        return self.configurations.get(automation_id)


class _DesiredConfigRows:
    def __init__(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        self._rows = rows

    def get_project_config(self, automation_id: str) -> dict[str, Any] | None:
        row = self._rows.get(automation_id)
        return copy.deepcopy(dict(row)) if row is not None else None


class _DesiredPolicyRows:
    def __init__(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        self._rows = rows

    def get_policy(self, automation_id: str) -> dict[str, Any] | None:
        row = self._rows.get(automation_id)
        return copy.deepcopy(dict(row)) if row is not None else None


class _DesiredStateUnitOfWork:
    def __init__(
        self,
        configs: Mapping[str, Mapping[str, Any]],
        policies: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.automation_plugins = _DesiredConfigRows(configs)
        self.automation_projects = _DesiredPolicyRows(policies)

    def __enter__(self) -> _DesiredStateUnitOfWork:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _DesiredStateRepository:
    def __init__(
        self,
        configs: Mapping[str, Mapping[str, Any]],
        policies: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._configs = configs
        self._policies = policies

    def unit_of_work(self) -> _DesiredStateUnitOfWork:
        return _DesiredStateUnitOfWork(self._configs, self._policies)


class _RuntimeRepository:
    """Small journal adapter that exercises the real generation reconciler."""

    def __init__(self) -> None:
        self.runtimes: dict[str, ProjectRuntimeRecord] = {}
        self.generations: dict[tuple[str, int], RuntimeGenerationRecord] = {}
        self.unknown_writes: set[tuple[str, int]] = set()

    def get_project_runtime(self, automation_id: str) -> ProjectRuntimeRecord | None:
        return self.runtimes.get(automation_id)

    def list_project_runtimes(self) -> Sequence[ProjectRuntimeRecord]:
        return tuple(self.runtimes.values())

    def get_generation(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord | None:
        return self.generations.get((automation_id, generation))

    def list_project_generations(
        self,
        automation_id: str,
    ) -> Sequence[RuntimeGenerationRecord]:
        return tuple(
            generation
            for (project_id, _number), generation in self.generations.items()
            if project_id == automation_id
        )

    def allocate_target_generation(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        expected_committed_generation: int | None,
        request_id: str,
    ) -> RuntimeGenerationRecord:
        uuid.UUID(request_id)
        key = (snapshot.automation_id, snapshot.generation)
        existing = self.generations.get(key)
        if existing is not None:
            assert existing.snapshot == snapshot
            return existing
        current = self.runtimes.get(snapshot.automation_id)
        assert (current.committed_generation if current else None) == (
            expected_committed_generation
        )
        target = RuntimeGenerationRecord(
            snapshot=snapshot,
            state=RuntimeGenerationState.TARGET,
        )
        self.generations[key] = target
        self.runtimes[snapshot.automation_id] = ProjectRuntimeRecord(
            automation_id=snapshot.automation_id,
            target_generation=snapshot.generation,
            committed_generation=expected_committed_generation,
            reconcile_state=RuntimeReconcileState.PREPARING,
            record_version=(current.record_version + 1) if current else 1,
        )
        return target

    def _generation_state(
        self,
        automation_id: str,
        generation: int,
        state: RuntimeGenerationState,
    ) -> None:
        key = (automation_id, generation)
        self.generations[key] = replace(self.generations[key], state=state)

    def mark_generation_preparing(self, automation_id: str, generation: int) -> None:
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.PREPARING,
        )

    def replace_generation_coeffects(
        self,
        automation_id: str,
        generation: int,
        coeffects: Sequence[Any],
    ) -> None:
        key = (automation_id, generation)
        self.generations[key] = replace(
            self.generations[key],
            coeffects=tuple(coeffects),
        )

    def mark_generation_waiting_coeffects(
        self,
        automation_id: str,
        generation: int,
        *,
        reason_codes: Sequence[str],
    ) -> None:
        assert reason_codes
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.WAITING_COEFFECTS,
        )
        self.runtimes[automation_id] = replace(
            self.runtimes[automation_id],
            reconcile_state=RuntimeReconcileState.WAITING_COEFFECTS,
        )

    def reserve_generation_effect(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        plan: RuntimeEffectPlan,
        sequence: int,
    ) -> RuntimeEffectRecord:
        key = (snapshot.automation_id, snapshot.generation)
        current = self.generations[key]
        for effect in current.effects:
            if effect.sequence == sequence:
                return effect
        effect = RuntimeEffectRecord(
            effect_id=f"{snapshot.automation_id}:{snapshot.generation}:{sequence}",
            automation_id=snapshot.automation_id,
            generation=snapshot.generation,
            sequence=sequence,
            kind=plan.kind,
            state=RuntimeEffectState.PLANNED,
            reversible=plan.reversible,
            effect_key=plan.effect_key,
            payload=dict(plan.payload),
        )
        self.generations[key] = replace(
            current,
            effects=(*current.effects, effect),
        )
        return effect

    def mark_generation_effect_applied(
        self,
        applied: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        key = (applied.automation_id, applied.generation)
        current = self.generations[key]
        self.generations[key] = replace(
            current,
            effects=tuple(
                applied if effect.effect_id == applied.effect_id else effect
                for effect in current.effects
            ),
        )
        return applied

    def mark_generation_prepared(self, automation_id: str, generation: int) -> None:
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.PREPARED,
        )

    def commit_generation_cas(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_committed_generation: int | None,
    ) -> ProjectRuntimeRecord:
        current = self.runtimes[automation_id]
        assert current.committed_generation == expected_committed_generation
        archival_unknown_predecessor = False
        if expected_committed_generation is not None:
            predecessor_key = (automation_id, expected_committed_generation)
            predecessor = self.generations[predecessor_key]
            if predecessor.state is RuntimeGenerationState.BLOCKED:
                if predecessor_key not in self.unknown_writes:
                    raise PluginConflictError(
                        "blocked predecessor has no unknown-write lease",
                        code="RUNTIME_ARCHIVAL_INCONSISTENT",
                    )
                archival_unknown_predecessor = True
            elif predecessor.state is RuntimeGenerationState.COMMITTED:
                self._generation_state(
                    automation_id,
                    expected_committed_generation,
                    RuntimeGenerationState.DRAINING,
                )
            else:
                raise PluginConflictError(
                    "previous committed runtime generation is not switchable",
                    code="RUNTIME_ARCHIVAL_INCONSISTENT",
                )
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.COMMITTED,
        )
        committed = replace(
            current,
            committed_generation=generation,
            reconcile_state=(
                RuntimeReconcileState.STABLE
                if archival_unknown_predecessor or expected_committed_generation is None
                else RuntimeReconcileState.DRAINING
            ),
            record_version=current.record_version + 1,
        )
        self.runtimes[automation_id] = committed
        return committed

    def list_active_generation_leases(
        self,
        _automation_id: str,
        _generation: int,
    ) -> Sequence[Any]:
        return ()

    def has_unknown_generation_write(
        self,
        automation_id: str,
        generation: int,
    ) -> bool:
        return (automation_id, generation) in self.unknown_writes

    def mark_generation_draining(self, automation_id: str, generation: int) -> None:
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.DRAINING,
        )

    def reserve_generation_dispose(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord:
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.DISPOSING,
        )
        return self.generations[(automation_id, generation)]

    def mark_generation_effect_disposing(self, effect_id: str) -> None:
        self._replace_effect_state(effect_id, RuntimeEffectState.DISPOSING)

    def mark_generation_effect_disposed(self, effect_id: str) -> None:
        self._replace_effect_state(effect_id, RuntimeEffectState.DISPOSED)

    def _replace_effect_state(
        self,
        effect_id: str,
        state: RuntimeEffectState,
    ) -> None:
        for key, current in tuple(self.generations.items()):
            effects = tuple(
                replace(effect, state=state)
                if effect.effect_id == effect_id
                else effect
                for effect in current.effects
            )
            if effects != current.effects:
                self.generations[key] = replace(current, effects=effects)
                return

    def complete_generation_dispose(self, automation_id: str, generation: int) -> None:
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.DISPOSED,
        )
        runtime = self.runtimes[automation_id]
        if all(
            item.snapshot.generation == runtime.committed_generation
            or item.state is RuntimeGenerationState.DISPOSED
            or (
                item.state is RuntimeGenerationState.BLOCKED
                and (automation_id, item.snapshot.generation) in self.unknown_writes
            )
            for item in self.list_project_generations(automation_id)
        ):
            self.runtimes[automation_id] = replace(
                runtime,
                reconcile_state=RuntimeReconcileState.STABLE,
            )

    def fail_generation(
        self,
        automation_id: str,
        generation: int,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        del error_code, error_summary
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.FAILED,
        )
        self.runtimes[automation_id] = replace(
            self.runtimes[automation_id],
            reconcile_state=RuntimeReconcileState.ERROR,
        )

    def block_generation_unknown_write(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        self._generation_state(
            automation_id,
            generation,
            RuntimeGenerationState.BLOCKED,
        )
        self.runtimes[automation_id] = replace(
            self.runtimes[automation_id],
            reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
        )

    def stabilize_project_after_archival_unknown(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        runtime = self.runtimes[automation_id]
        if (
            runtime.target_generation == runtime.committed_generation
            and runtime.committed_generation != generation
            and all(
                item.snapshot.generation == runtime.committed_generation
                or item.state is RuntimeGenerationState.DISPOSED
                or (
                    item.state is RuntimeGenerationState.BLOCKED
                    and (automation_id, item.snapshot.generation)
                    in self.unknown_writes
                )
                for item in self.list_project_generations(automation_id)
            )
        ):
            self.runtimes[automation_id] = replace(
                runtime,
                reconcile_state=RuntimeReconcileState.STABLE,
            )


class _EffectDriver:
    @staticmethod
    def ensure_applied(
        *,
        snapshot: RuntimeGenerationSnapshot,
        plan: RuntimeEffectPlan,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        assert effect.automation_id == snapshot.automation_id
        return replace(
            effect,
            state=RuntimeEffectState.APPLIED,
            payload=dict(plan.payload),
        )

    @staticmethod
    def dispose(_effect: RuntimeEffectRecord) -> None:
        return None


def _account_system(account_id: str) -> str:
    if account_id.startswith("yunda"):
        return "yunda"
    if account_id.startswith("r13"):
        return "r13"
    if account_id.startswith("r7"):
        return "r7"
    return "ronghui"


def _active_accounts() -> _Accounts:
    account_ids = {
        str(account_id)
        for automation_id in release_first_party_automation_ids()
        for account_id in FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[
            automation_id
        ].legacy_account_bindings.values()
    }
    return _Accounts(
        tuple(
            {
                "account_id": account_id,
                "system": _account_system(account_id),
                "is_active": True,
                "updated_at": "2026-08-15 12:00:00",
            }
            for account_id in sorted(account_ids)
        )
    )


def _resource_record(config: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    value = copy.deepcopy(dict(config))
    value["_meta"] = {
        "source": source,
        "configuration_version": 1,
        "config_sha256": _sha(config),
        "updated_at": "2026-08-15 12:00:00",
    }
    return value


def _release_resources() -> dict[str, dict[str, Any]]:
    code_owned = {
        key: config
        for key, config in BUILTIN_RESOURCES.items()
        if key not in _DEFERRED_R7_RESOURCE_KEYS
    }
    assert set(code_owned) == set(AUTOMATION_PROJECT_CODE_OWNED_RESOURCE_KEYS)
    assert len(code_owned) == 18
    assert len(AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS) == 8
    required_existing: dict[str, dict[str, Any]] = {}
    for spec in AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS:
        config = {"resource_kind": spec.expected_kind}
        for field_name in spec.required_fields:
            config[field_name] = f"test-{field_name}"
        for field_names in spec.alternative_field_groups:
            config[field_names[0]] = f"test-{field_names[0]}"
        required_existing[spec.resource_key] = config
    assert not (set(code_owned) & set(required_existing))
    resources = {
        key: _resource_record(config, source="migration-018-reviewed-builtin")
        for key, config in code_owned.items()
    }
    resources.update(
        {
            key: _resource_record(config, source="preexisting-test-resource")
            for key, config in required_existing.items()
        }
    )
    assert len(resources) == 26
    return resources


def _migration_config(template: Any, manifest: Any) -> dict[str, Any]:
    properties = manifest.config_schema.get("properties", {})
    config = {
        str(field): copy.deepcopy(value)
        for field, value in template.legacy_arguments.items()
        if str(field) in properties
    }
    validate_schema_instance(
        f"automation.{template.automation_id}.release_generation_test",
        config,
        manifest.config_schema,
    )
    return config


def _compile_configuration(
    *,
    automation_id: str,
    manifest: Any,
    accounts: _Accounts,
) -> tuple[AutomationProjectConfigRecord, dict[str, Any]]:
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[automation_id]
    config = _migration_config(template, manifest)
    account_bindings: dict[str, Any] = dict(template.legacy_account_bindings)
    public_accounts = accounts.list_accounts(include_status=False, validate=False)
    for role in manifest.account_roles:
        role_name = str(role["role"])
        if role.get("required") is not True or role_name in account_bindings:
            continue
        assert role.get("collection") is True
        allowed_systems = set(role["allowed_systems"])
        account_bindings[role_name] = tuple(
            sorted(
                str(account["account_id"])
                for account in public_accounts
                if account["is_active"] is True
                and str(account["system"]) in allowed_systems
            )
        )
    transient = SimpleNamespace(
        automation_id=automation_id,
        invocation_contracts=manifest.invocation_contracts,
        allowed_entrypoints=manifest.allowed_entrypoints,
        config_schema=manifest.config_schema,
        account_roles=manifest.account_roles,
        resource_roles=manifest.resource_roles,
        tool_contract=manifest.tool_contract,
        action_id=f"automation.{automation_id}.run",
    )
    compiled_invocations: dict[str, dict[str, Any]] = {}
    normalized_accounts: Mapping[str, Any] | None = None
    normalized_resources: Mapping[str, str] | None = None
    for entrypoint in sorted(template.allowed_entrypoints):
        compiled = compile_instance_arguments(
            transient,
            config=config,
            account_bindings=account_bindings,
            resource_bindings=dict(template.resource_bindings),
            entrypoint=entrypoint,
            resolve_dynamic=False,
        )
        if normalized_accounts is None:
            normalized_accounts = compiled.account_bindings
            normalized_resources = compiled.resource_bindings
        else:
            assert dict(normalized_accounts) == dict(compiled.account_bindings)
            assert dict(normalized_resources or {}) == dict(
                compiled.resource_bindings
            )
        compiled_invocations[entrypoint] = {
            "arguments": copy.deepcopy(dict(compiled.arguments)),
            "dynamic_resolvers": copy.deepcopy(
                dict(compiled.unresolved_dynamic_resolvers)
            ),
        }
    normalized_account_map = dict(normalized_accounts or {})
    normalized_resource_map = dict(normalized_resources or {})
    schedule = {"kind": "none", "times": [], "enabled": False}
    entrypoints = tuple(sorted(template.allowed_entrypoints))
    configuration = AutomationProjectConfigRecord(
        automation_id=automation_id,
        config=config,
        account_bindings=normalized_account_map,
        resource_bindings=normalized_resource_map,
        schedule=schedule,
        config_version=2,
        configured=True,
        config_sha256=_sha(config),
        account_bindings_sha256=_sha(normalized_account_map),
        resource_bindings_sha256=_sha(normalized_resource_map),
        device_binding_sha256=_sha(None),
        enabled_entrypoints=entrypoints,
    )
    desired_row = {
        "automation_id": automation_id,
        "config_json": config,
        "config_sha256": _sha(config),
        "account_bindings_json": normalized_account_map,
        "account_bindings_sha256": _sha(normalized_account_map),
        "resource_bindings_json": normalized_resource_map,
        "resource_bindings_sha256": _sha(normalized_resource_map),
        "enabled_entrypoints_json": list(entrypoints),
        "enabled_entrypoints_sha256": _sha(list(entrypoints)),
        "desired_schedule_json": schedule,
        "desired_schedule_sha256": _sha(schedule),
        "compiled_invocations_json": compiled_invocations,
        "compiled_invocations_sha256": _sha(compiled_invocations),
        "device_binding_sha256": _sha(None),
        "device_id": None,
        "configured": True,
        "config_version": 2,
    }
    return configuration, desired_row


def _build_release_world() -> SimpleNamespace:
    core = ToolRegistry()
    manifests = resolve_release_first_party_manifests(core)
    seeds = release_first_party_instance_seeds()
    expected_automation_ids = release_first_party_automation_ids()
    assert len(manifests) == 14
    assert len(seeds) == 16
    assert {seed.automation_id for seed in seeds} == expected_automation_ids
    assert not (set(manifests) & set(deferred_first_party_plugin_ids()))
    assert not (expected_automation_ids & deferred_first_party_automation_ids())

    accounts = _active_accounts()
    resources = _release_resources()
    handler_keys = release_first_party_broker_action_keys(core)
    handlers = build_production_first_party_core_handler_map(
        cursor_secret=bytes(range(32)),
        account_manager=accounts,
        allowed_action_keys=handler_keys,
    )
    assert set(handlers) == set(handler_keys)

    projects: dict[str, PluginInstanceRecord] = {}
    configurations: dict[str, AutomationProjectConfigRecord] = {}
    desired_rows: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        manifest = manifests[seed.plugin_id]
        version = PluginVersionRecord(
            plugin_id=seed.plugin_id,
            version=manifest.version,
            package_sha256=_sha({"plugin_id": seed.plugin_id, "release": 1}),
            manifest_sha256=manifest.manifest_sha256,
            manifest=manifest.to_mapping(),
            trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
            install_root=f"/immutable/test/{seed.plugin_id}/{manifest.version}",
            install_metadata={"python_relative": "venv/bin/python"},
        )
        projects[seed.automation_id] = PluginInstanceRecord(
            automation_id=seed.automation_id,
            display_name=seed.display_name,
            plugin_id=seed.plugin_id,
            state=PluginProjectState.ENABLED,
            active_version=version,
            enabled=True,
            target_generation=1,
            committed_generation=None,
            reconcile_state=RuntimeReconcileState.PREPARING,
            committed_snapshot=None,
        )
        configuration, desired_row = _compile_configuration(
            automation_id=seed.automation_id,
            manifest=manifest,
            accounts=accounts,
        )
        configurations[seed.automation_id] = configuration
        desired_rows[seed.automation_id] = desired_row

    project_repository = _CatalogRepository(projects)
    catalog = PluginCatalog(
        project_repository,
        _ConfigurationRepository(configurations),
        excluded_plugin_ids=deferred_first_party_plugin_ids(),
        allowed_execution_platforms=("server",),
    )
    policy_rows: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, RuntimeGenerationSnapshot] = {}
    for automation_id in sorted(expected_automation_ids):
        policy_rows[automation_id] = {
            "automation_id": automation_id,
            "project_generation": 1,
            "mode": "REQUIRE_EACH_RUN",
            "project_configuration_version": 2,
            "version": 1,
        }
        snapshots[automation_id] = build_runtime_generation_snapshot(
            catalog.require(automation_id),
            desired_config_row=desired_rows[automation_id],
            policy_row=policy_rows[automation_id],
            generation=1,
            core_catalog=core,
        )

    runtime = _RuntimeRepository()
    binding_resolver = ProductionProjectBindingResolver(
        account_manager=accounts,
        resource_provider=resources.get,
        worker_repository=_Workers(),
    )
    reconciler = AutomationRuntimeReconciler(
        repository=runtime,
        coeffects=ProductionRuntimeCoeffectProvider(
            core_catalog=core,
            broker_handler_keys=tuple(sorted(handlers)),
            account_manager=accounts,
            binding_resolver=binding_resolver,
        ),
        planner=ProductionRuntimeEffectPlanner(),
        driver=_EffectDriver(),
    )
    return SimpleNamespace(
        core=core,
        manifests=manifests,
        expected_automation_ids=expected_automation_ids,
        accounts=accounts,
        resources=resources,
        project_repository=project_repository,
        configurations=configurations,
        catalog=catalog,
        desired_rows=desired_rows,
        policy_rows=policy_rows,
        orchestration=_DesiredStateRepository(desired_rows, policy_rows),
        snapshots=snapshots,
        runtime=runtime,
        binding_resolver=binding_resolver,
        reconciler=reconciler,
    )


def _reconcile_world(world: SimpleNamespace) -> dict[str, Any]:
    results = {}
    for automation_id, snapshot in sorted(world.snapshots.items()):
        results[automation_id] = world.reconciler.reconcile(
            snapshot,
            expected_committed_generation=None,
            request_id=str(uuid.uuid5(uuid.NAMESPACE_URL, automation_id)),
        )
    for automation_id in sorted(world.expected_automation_ids):
        project_runtime = world.runtime.get_project_runtime(automation_id)
        assert project_runtime is not None
        committed = (
            world.runtime.get_generation(
                automation_id,
                project_runtime.committed_generation,
            )
            if project_runtime.committed_generation is not None
            else None
        )
        current = world.project_repository.projects[automation_id]
        world.project_repository.projects[automation_id] = replace(
            current,
            record_version=project_runtime.record_version,
            target_generation=project_runtime.target_generation,
            committed_generation=project_runtime.committed_generation,
            reconcile_state=project_runtime.reconcile_state,
            committed_snapshot=committed.snapshot if committed is not None else None,
        )
    return results


def _target_service(world: SimpleNamespace) -> MySQLRuntimeTargetService:
    return MySQLRuntimeTargetService(
        orchestration_repository=world.orchestration,
        catalog=world.catalog,
        core_catalog=world.core,
        runtime_repository=world.runtime,
        reconciler=world.reconciler,
    )


def _route_specs(world: SimpleNamespace) -> list[tuple[AutomationEntrypoint, str, str, str]]:
    specs: list[tuple[AutomationEntrypoint, str, str, str]] = []
    for automation_id, snapshot in sorted(world.snapshots.items()):
        bindings = snapshot.execution_metadata["resource_bindings"]
        for entrypoint in (AutomationEntrypoint.FEISHU, AutomationEntrypoint.WEBHOOK):
            role = f"{entrypoint.value}_route"
            resource_id = bindings.get(role)
            if resource_id is None:
                continue
            resource = world.resources[str(resource_id)]
            route_key = resource[
                "route_key" if entrypoint is AutomationEntrypoint.FEISHU else "path"
            ]
            specs.append((entrypoint, str(route_key), automation_id, str(resource_id)))
    return specs


def test_release_generation_commits_all_instances_and_resolves_all_trusted_routes() -> None:
    world = _build_release_world()
    results = _reconcile_world(world)

    assert len(world.manifests) == 14
    assert len(results) == 16
    assert all(result.waiting_coeffects == () for result in results.values())
    assert all(result.committed_generation == 1 for result in results.values())
    assert all(
        runtime.committed_generation == 1
        and runtime.target_generation == 1
        and runtime.reconcile_state is RuntimeReconcileState.STABLE
        for runtime in world.runtime.list_project_runtimes()
    )
    assert all(
        world.runtime.get_generation(automation_id, 1).state
        is RuntimeGenerationState.COMMITTED
        for automation_id in world.expected_automation_ids
    )
    generation_health = runtime_generation_health(
        world.runtime,
        expected_automation_ids=world.expected_automation_ids,
    )
    assert generation_health.healthy is True
    assert generation_health.project_count == 16
    assert generation_health.committed_count == 16

    catalog_health = world.catalog.production_health(
        tuple(sorted(world.expected_automation_ids))
    )
    assert catalog_health["ok"] is True
    assert catalog_health["signed_packages"] == 14
    assert catalog_health["instances"] == 16
    assert catalog_health["unstable_generations"] == []

    route_resolver = CommittedAutomationProjectRouteResolver(
        catalog=world.catalog,
        runtime_repository=world.runtime,
        binding_resolver=world.binding_resolver,
        resource_provider=world.resources.get,
    )
    route_specs = _route_specs(world)
    assert len(route_specs) == 11
    for entrypoint, route_key, automation_id, resource_id in route_specs:
        route = route_resolver.resolve_committed_route(
            entrypoint=entrypoint,
            route_key=route_key,
        )
        assert route is not None
        assert route.automation_id == automation_id
        assert route.route_id == resource_id
        assert route.automation_generation == 1
        assert route.route_revision == 1

    assert not (_DEFERRED_R7_AUTOMATION_IDS & set(world.expected_automation_ids))
    assert not (_DEFERRED_R7_RESOURCE_KEYS & set(world.resources))
    assert sum("r7" in plugin_id for plugin_id in world.manifests) == 0


def test_second_release_reconcile_reuses_identical_committed_generations() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    generations_before = copy.deepcopy(world.runtime.generations)
    runtimes_before = copy.deepcopy(world.runtime.runtimes)
    install_roots_before = {
        automation_id: record.snapshot.execution_metadata["runtime_descriptor"][
            "install_metadata"
        ]["install_root"]
        for (automation_id, generation), record in world.runtime.generations.items()
        if generation == 1
    }
    assert all(
        policy["project_generation"] == 1
        for policy in world.policy_rows.values()
    )

    bootstrap_calls = 0

    def _idempotent_release_bootstrap() -> BootstrapResult:
        nonlocal bootstrap_calls
        bootstrap_calls += 1
        assert world.runtime.generations == generations_before
        assert world.runtime.runtimes == runtimes_before
        return BootstrapResult(
            created=(),
            existing=tuple(sorted(world.expected_automation_ids)),
            rejected={},
        )

    production_runtime = object.__new__(ProductionAutomationPluginRuntime)
    production_runtime.bootstrap = BootstrapResult(
        created=(),
        existing=tuple(sorted(world.expected_automation_ids)),
        rejected={},
    )
    production_runtime.target_service = _target_service(world)
    production_runtime._bootstrap_first_party = _idempotent_release_bootstrap

    assert production_runtime.reconcile() == ()
    assert bootstrap_calls == 1

    assert world.runtime.generations == generations_before
    assert world.runtime.runtimes == runtimes_before
    assert {
        automation_id: record.snapshot.execution_metadata["runtime_descriptor"][
            "install_metadata"
        ]["install_root"]
        for (automation_id, generation), record in world.runtime.generations.items()
        if generation == 1
    } == install_roots_before
    assert {
        generation
        for automation_id, generation in world.runtime.generations
        if automation_id in world.expected_automation_ids
    } == {1}


def test_staged_target_over_blocked_committed_generation_commits_and_archives() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    blocked_id, following_id = sorted(world.expected_automation_ids)[:2]

    for automation_id in (blocked_id, following_id):
        world.policy_rows[automation_id] = {
            **world.policy_rows[automation_id],
            "project_generation": 2,
        }
        target_snapshot = build_runtime_generation_snapshot(
            world.catalog.require(automation_id),
            desired_config_row=world.desired_rows[automation_id],
            policy_row=world.policy_rows[automation_id],
            generation=2,
            core_catalog=world.core,
        )
        target = world.runtime.allocate_target_generation(
            target_snapshot,
            expected_committed_generation=1,
            request_id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"staged-target:{automation_id}:2",
                )
            ),
        )
        assert world.reconciler.prepare_target(target) == ()
        assert world.runtime.get_generation(automation_id, 2).state is (
            RuntimeGenerationState.PREPARED
        )

    blocked_generation = world.runtime.get_generation(blocked_id, 1)
    assert blocked_generation is not None
    world.runtime.generations[(blocked_id, 1)] = replace(
        blocked_generation,
        state=RuntimeGenerationState.BLOCKED,
    )
    world.runtime.unknown_writes.add((blocked_id, 1))
    service = _target_service(world)
    committed = service.reconcile_project(blocked_id)

    assert committed.committed_generation == 2
    assert committed.draining_generations == ()
    assert committed.disposed_generations == ()
    results = service.reconcile_all()

    assert world.runtime.get_generation(blocked_id, 1).state is (
        RuntimeGenerationState.BLOCKED
    )
    assert world.runtime.get_generation(blocked_id, 2).state is (
        RuntimeGenerationState.COMMITTED
    )
    assert world.runtime.get_project_runtime(blocked_id).committed_generation == 2
    assert world.runtime.get_project_runtime(blocked_id).reconcile_state is (
        RuntimeReconcileState.STABLE
    )
    assert any(result.automation_id == following_id for result in results)
    assert world.runtime.get_project_runtime(following_id).committed_generation == 2
    assert world.runtime.get_generation(following_id, 2).state is (
        RuntimeGenerationState.COMMITTED
    )


def test_blocked_committed_generation_without_unknown_lease_fails_closed() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "arrival_stats"
    current = world.runtime.get_generation(automation_id, 1)
    assert current is not None
    world.runtime.generations[(automation_id, 1)] = replace(
        current,
        state=RuntimeGenerationState.BLOCKED,
    )
    target = world.runtime.allocate_target_generation(
        replace(world.snapshots[automation_id], generation=2),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )
    assert world.reconciler.prepare_target(target) == ()

    with pytest.raises(PluginConflictError, match="without unknown-write evidence"):
        _target_service(world).reconcile_project(automation_id)

    assert world.runtime.get_project_runtime(automation_id).committed_generation == 1
    assert world.runtime.get_generation(automation_id, 2).state is (
        RuntimeGenerationState.PREPARED
    )


def test_blocked_current_without_target_prepares_and_commits_successor() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "finance_startup_catchup"
    current = world.runtime.get_generation(automation_id, 1)
    assert current is not None
    world.runtime.generations[(automation_id, 1)] = replace(
        current,
        state=RuntimeGenerationState.BLOCKED,
    )
    world.runtime.unknown_writes.add((automation_id, 1))
    runtime = world.runtime.runtimes[automation_id]
    world.runtime.runtimes[automation_id] = replace(
        runtime,
        reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
    )
    project = world.project_repository.projects[automation_id]
    world.project_repository.projects[automation_id] = replace(
        project,
        reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
    )
    world.policy_rows[automation_id] = {
        **world.policy_rows[automation_id],
        "project_generation": 2,
        "version": 2,
    }

    result = _target_service(world).reconcile_project(automation_id)

    assert result.committed_generation == 2
    assert world.runtime.get_generation(automation_id, 1).state is (
        RuntimeGenerationState.BLOCKED
    )
    assert world.runtime.get_generation(automation_id, 2).state is (
        RuntimeGenerationState.COMMITTED
    )


def test_blocked_current_without_release_marker_remains_quarantined() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "finance_startup_catchup"
    current = world.runtime.get_generation(automation_id, 1)
    assert current is not None
    world.runtime.generations[(automation_id, 1)] = replace(
        current,
        state=RuntimeGenerationState.BLOCKED,
    )
    world.runtime.unknown_writes.add((automation_id, 1))
    runtime = world.runtime.runtimes[automation_id]
    world.runtime.runtimes[automation_id] = replace(
        runtime,
        reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
    )
    project = world.project_repository.projects[automation_id]
    world.project_repository.projects[automation_id] = replace(
        project,
        reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
    )

    assert _target_service(world).reconcile_project(automation_id) is None
    assert world.runtime.get_generation(automation_id, 2) is None
    assert world.runtime.get_generation(automation_id, 1).state is (
        RuntimeGenerationState.BLOCKED
    )
    assert world.runtime.get_project_runtime(automation_id).committed_generation == 1


def test_blocked_current_rejects_non_successor_policy_marker() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "finance_startup_catchup"
    current = world.runtime.get_generation(automation_id, 1)
    assert current is not None
    world.runtime.generations[(automation_id, 1)] = replace(
        current,
        state=RuntimeGenerationState.BLOCKED,
    )
    world.runtime.unknown_writes.add((automation_id, 1))
    runtime = world.runtime.runtimes[automation_id]
    world.runtime.runtimes[automation_id] = replace(
        runtime,
        reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
    )
    world.policy_rows[automation_id] = {
        **world.policy_rows[automation_id],
        "project_generation": 3,
        "version": 2,
    }

    with pytest.raises(PluginConflictError) as raised:
        _target_service(world).reconcile_project(automation_id)

    assert raised.value.code == "PLUGIN_POLICY_GENERATION_MISMATCH"
    assert world.runtime.get_generation(automation_id, 2) is None


def test_stable_policy_version_drift_reuses_committed_generation() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "scan_codes"
    world.policy_rows[automation_id] = {
        **world.policy_rows[automation_id],
        "version": 3,
    }

    assert _target_service(world).reconcile_all() == ()
    assert world.runtime.get_project_runtime(automation_id).committed_generation == 1
    assert world.runtime.get_generation(automation_id, 2) is None


@pytest.mark.parametrize(
    ("target_generation", "reconcile_state"),
    (
        (1, RuntimeReconcileState.STABLE),
        (2, RuntimeReconcileState.PREPARING),
    ),
    ids=("stable-forward-policy", "staged-target"),
)
def test_forward_policy_reconciles_exact_next_generation(
    target_generation: int,
    reconcile_state: RuntimeReconcileState,
) -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "arrive_list"
    world.policy_rows[automation_id] = {
        **world.policy_rows[automation_id],
        "project_generation": 2,
        "version": 2,
    }
    runtime = world.runtime.runtimes[automation_id]
    world.runtime.runtimes[automation_id] = replace(
        runtime,
        target_generation=target_generation,
        reconcile_state=reconcile_state,
        record_version=runtime.record_version + 1,
    )
    project = world.project_repository.projects[automation_id]
    world.project_repository.projects[automation_id] = replace(
        project,
        target_generation=target_generation,
        reconcile_state=reconcile_state,
        record_version=project.record_version + 1,
    )

    result = _target_service(world).reconcile_project(automation_id)

    assert result.target_generation == 2
    assert result.committed_generation == 2
    assert world.runtime.get_generation(automation_id, 2).state is (
        RuntimeGenerationState.COMMITTED
    )
    assert world.runtime.get_project_runtime(automation_id).reconcile_state is (
        RuntimeReconcileState.STABLE
    )


def test_saved_configuration_material_reconciles_and_commits_next_generation() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "arrive_list"
    config = {"dry_run": True}
    compiled = copy.deepcopy(
        world.desired_rows[automation_id]["compiled_invocations_json"]
    )
    for invocation in compiled.values():
        invocation["arguments"] = {"dry_run": True}
    row = world.desired_rows[automation_id]
    row.update(
        {
            "config_json": config,
            "config_sha256": _sha(config),
            "compiled_invocations_json": compiled,
            "compiled_invocations_sha256": _sha(compiled),
            "config_version": 3,
        }
    )
    current_config = world.configurations[automation_id]
    world.configurations[automation_id] = replace(
        current_config,
        config=config,
        config_version=3,
        config_sha256=_sha(config),
    )
    world.policy_rows[automation_id] = {
        **world.policy_rows[automation_id],
        "project_generation": 2,
        "project_configuration_version": 3,
        "version": 2,
    }
    runtime = world.runtime.runtimes[automation_id]
    world.runtime.runtimes[automation_id] = replace(
        runtime,
        target_generation=2,
        reconcile_state=RuntimeReconcileState.PREPARING,
        record_version=runtime.record_version + 1,
    )
    project = world.project_repository.projects[automation_id]
    world.project_repository.projects[automation_id] = replace(
        project,
        target_generation=2,
        reconcile_state=RuntimeReconcileState.PREPARING,
        record_version=project.record_version + 1,
    )

    result = _target_service(world).reconcile_project(automation_id)

    assert result.target_generation == 2
    assert result.committed_generation == 2
    committed = world.runtime.get_generation(automation_id, 2)
    assert committed is not None
    assert committed.state is RuntimeGenerationState.COMMITTED
    assert committed.snapshot.project_config_sha256 == _sha(config)
    assert committed.snapshot.execution_metadata["project_config_version"] == 3
    assert world.runtime.get_generation(automation_id, 1).state is (
        RuntimeGenerationState.DISPOSED
    )


def test_stable_material_drift_requires_next_generation_policy_binding() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "arrive_list"
    row = world.desired_rows[automation_id]
    row["config_json"] = {**row["config_json"], "uncommitted_drift": True}
    row["config_sha256"] = _sha(row["config_json"])

    with pytest.raises(PluginConflictError) as raised:
        _target_service(world).reconcile_project(automation_id)

    assert raised.value.code == "PLUGIN_POLICY_GENERATION_MISMATCH"
    assert world.runtime.get_generation(automation_id, 2) is None
    runtime = world.runtime.get_project_runtime(automation_id)
    assert runtime is not None
    assert runtime.committed_generation == 1
    assert runtime.target_generation == 1
    assert runtime.reconcile_state is RuntimeReconcileState.STABLE


def test_stable_pointer_requires_a_committed_generation_record() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "arrive_list"
    committed = world.runtime.get_generation(automation_id, 1)
    assert committed is not None
    world.runtime.generations[(automation_id, 1)] = replace(
        committed,
        state=RuntimeGenerationState.DISPOSED,
    )

    with pytest.raises(PluginConflictError) as raised:
        _target_service(world).reconcile_project(automation_id)

    assert raised.value.code == "RUNTIME_COMMIT_INCONSISTENT"
    assert world.runtime.get_generation(automation_id, 2) is None


def test_stable_policy_bound_beyond_next_generation_fails_closed() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    automation_id = "arrive_list"
    world.policy_rows[automation_id]["project_generation"] = 3

    with pytest.raises(PluginConflictError) as raised:
        _target_service(world).reconcile_project(automation_id)

    assert raised.value.code == "PLUGIN_POLICY_GENERATION_MISMATCH"
    assert world.runtime.get_generation(automation_id, 2) is None


def test_missing_required_delivery_resource_waits_and_catalog_fails_closed() -> None:
    world = _build_release_world()
    world.resources.pop("phase7.delivery_status_bitable")

    results = _reconcile_world(world)

    assert results["delivery_status"].committed_generation is None
    assert results["delivery_status"].waiting_coeffects == (
        "PLUGIN_RESOURCE_BINDING_NOT_FOUND",
    )
    assert all(
        result.committed_generation == 1
        for automation_id, result in results.items()
        if automation_id != "delivery_status"
    )
    assert world.runtime.get_project_runtime("delivery_status").reconcile_state is (
        RuntimeReconcileState.WAITING_COEFFECTS
    )
    health = world.catalog.production_health(
        tuple(sorted(world.expected_automation_ids))
    )
    assert health["ok"] is True
    assert health["runnable"] is False
    assert health["runtime_status"] == "UNAVAILABLE"
    assert health["unstable_generations"] == ["delivery_status"]


def test_committed_route_value_drift_never_retargets_transport() -> None:
    world = _build_release_world()
    _reconcile_world(world)
    resolver = CommittedAutomationProjectRouteResolver(
        catalog=world.catalog,
        runtime_repository=world.runtime,
        binding_resolver=world.binding_resolver,
        resource_provider=world.resources.get,
    )
    resource_id = "automation.feishu_route.scan_codes"
    original_key = str(world.resources[resource_id]["route_key"])
    drifted_key = f"{original_key}.drifted"
    world.resources[resource_id]["route_key"] = drifted_key
    world.resources[resource_id]["_meta"] = {
        **world.resources[resource_id]["_meta"],
        "configuration_version": 2,
        "config_sha256": _sha(
            {
                key: value
                for key, value in world.resources[resource_id].items()
                if key != "_meta"
            }
        ),
    }

    assert resolver.resolve_committed_route(
        entrypoint=AutomationEntrypoint.FEISHU,
        route_key=original_key,
    ) is None
    with pytest.raises(OrchestrationError) as raised:
        resolver.resolve_committed_route(
            entrypoint=AutomationEntrypoint.FEISHU,
            route_key=drifted_key,
        )
    assert raised.value.code == "PROJECT_ROUTE_STALE"
