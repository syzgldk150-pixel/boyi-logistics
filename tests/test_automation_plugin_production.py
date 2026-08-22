from __future__ import annotations

import copy
import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from agent.automation_plugins import production as production_module
from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.invocation import compile_instance_arguments
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginInstanceRecord,
    PluginProjectState,
    PluginTrustSource,
    PluginVersionRecord,
    RuntimeCoeffectKind,
    RuntimeEffectKind,
    RuntimeEffectRecord,
    RuntimeEffectState,
)
from agent.automation_plugins.production import (
    CURSOR_SECRET_ENV,
    MySQLRuntimeTargetService,
    ProductionAutomationPluginRuntime,
    ProductionRuntimeCoeffectProvider,
    ProductionRuntimeEffectDriver,
    ProductionRuntimeEffectPlanner,
    build_runtime_generation_snapshot,
    production_cursor_secret,
)
from agent.automation_plugins.sandbox import SandboxCanaryResult
from agent.automation_plugins.runtime_repository import snapshot_to_row
from agent.automation_plugins.first_party import (
    FIRST_PARTY_PACKAGE_VERSION,
    resolve_first_party_manifests,
    resolve_release_first_party_manifests,
)
from agent.automation_plugins.manifest import AutomationPluginManifest, canonical_json_bytes
from agent.tool_registry import ToolRegistry
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.orchestration_repository_support import IdempotencyConflict, _json_hash


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class _CatalogRepository:
    def __init__(self, instance: PluginInstanceRecord) -> None:
        self.instance = instance
        self.list_calls = 0

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        return self.instance if automation_id == self.instance.automation_id else None

    def list_instances(self) -> list[PluginInstanceRecord]:
        self.list_calls += 1
        return [self.instance]


class _ConfigurationRepository:
    def __init__(self, config: AutomationProjectConfigRecord) -> None:
        self.config = config

    def get_project_config(
        self,
        automation_id: str,
    ) -> AutomationProjectConfigRecord | None:
        return self.config if automation_id == self.config.automation_id else None


class _AccountManager:
    def __init__(
        self,
        *,
        authenticated: bool = True,
        active: bool = True,
        system: str = "ronghui",
    ) -> None:
        self.authenticated = authenticated
        self.active = active
        self.system = system

    def list_accounts(self, **_: object) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "ronghui-a",
                "system": self.system,
                "is_active": self.active,
            }
        ]

    def require_authenticated_binding(self, account_id: str) -> Mapping[str, str]:
        if not self.authenticated:
            raise RuntimeError("not authenticated")
        assert account_id == "ronghui-a"
        return {
            "account_id": account_id,
            "system": self.system,
            "account_purpose": "general",
        }


class _ScriptedGenerationCursor:
    def __init__(self, actions: list[tuple[str, object, int]]) -> None:
        self._actions = list(actions)
        self._row: object = None
        self.rowcount = 0

    def execute(self, sql: object, _params: object = None) -> None:
        if not self._actions:
            raise AssertionError(f"unexpected SQL: {sql}")
        marker, self._row, self.rowcount = self._actions.pop(0)
        normalized = " ".join(str(sql).split())
        assert marker in normalized

    def fetchone(self) -> object:
        return self._row

    def close(self) -> None:
        return None


class _ScriptedGenerationConnection:
    def __init__(self, actions: list[tuple[str, object, int]]) -> None:
        self._cursor = _ScriptedGenerationCursor(actions)

    def cursor(self) -> _ScriptedGenerationCursor:
        return self._cursor


def _entry_and_row(
    tmp_path: Path,
) -> tuple[ToolRegistry, object, dict[str, Any], dict[str, Any]]:
    core = ToolRegistry()
    manifest = resolve_first_party_manifests(core)["sync_customer_service_problems"]
    package_file = tmp_path / "plugin" / "package" / "payload" / "main.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_bytes(b"print('ok')\n")
    python_file = tmp_path / "plugin" / "venv" / "bin" / "python"
    python_file.parent.mkdir(parents=True)
    python_file.write_bytes(b"python")
    install_metadata = {
        "python_relative": "venv/bin/python",
        "package_files": [
            {
                "path": "payload/main.py",
                "sha256": hashlib.sha256(package_file.read_bytes()).hexdigest(),
                "size": package_file.stat().st_size,
            }
        ],
    }
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="1" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root=str(tmp_path / "plugin"),
        install_metadata=install_metadata,
    )
    instance = PluginInstanceRecord(
        automation_id="customer-a",
        display_name="Customer A",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.ENABLED,
        active_version=version,
        target_generation=1,
        committed_generation=None,
        committed_snapshot=None,
    )
    config = {"direction": "both"}
    accounts = {"customer_service_source": ("ronghui-a",)}
    transient = type(
        "TransientEntry",
        (),
        {
            "automation_id": "customer-a",
            "invocation_contracts": manifest.invocation_contracts,
            "allowed_entrypoints": manifest.allowed_entrypoints,
            "config_schema": manifest.config_schema,
            "account_roles": manifest.account_roles,
            "resource_roles": manifest.resource_roles,
            "tool_contract": manifest.tool_contract,
            "action_id": "automation.customer-a.run",
        },
    )()
    compiled = compile_instance_arguments(
        transient,
        config=config,
        account_bindings=accounts,
        resource_bindings={},
        entrypoint="console",
        resolve_dynamic=False,
    )
    compiled_invocations = {
        "console": {
            "arguments": dict(compiled.arguments),
            "dynamic_resolvers": dict(compiled.unresolved_dynamic_resolvers),
        }
    }
    config_record = AutomationProjectConfigRecord(
        automation_id="customer-a",
        config=config,
        account_bindings=accounts,
        resource_bindings={},
        schedule={"kind": "none", "times": [], "enabled": False},
        config_version=2,
        configured=True,
        config_sha256=_sha(config),
        account_bindings_sha256=_sha(accounts),
        resource_bindings_sha256=_sha({}),
        device_binding_sha256=_sha(None),
        enabled_entrypoints=("console",),
    )
    entry = PluginCatalog(
        _CatalogRepository(instance),
        _ConfigurationRepository(config_record),
    ).require("customer-a")
    row = {
        "automation_id": "customer-a",
        "config_json": config,
        "config_sha256": _sha(config),
        "account_bindings_json": accounts,
        "account_bindings_sha256": _sha(accounts),
        "resource_bindings_json": {},
        "resource_bindings_sha256": _sha({}),
        "enabled_entrypoints_json": ["console"],
        "enabled_entrypoints_sha256": _sha(["console"]),
        "desired_schedule_json": {"kind": "none", "times": [], "enabled": False},
        "desired_schedule_sha256": _sha(
            {"kind": "none", "times": [], "enabled": False}
        ),
        "compiled_invocations_json": compiled_invocations,
        "compiled_invocations_sha256": _sha(compiled_invocations),
        "device_binding_sha256": _sha(None),
        "device_id": None,
        "configured": True,
        "config_version": 2,
    }
    policy = {
        "automation_id": "customer-a",
        "project_generation": 1,
        "mode": "REQUIRE_EACH_RUN",
        "project_configuration_version": 2,
        "version": 3,
    }
    return core, entry, row, policy


def test_server_only_catalog_excludes_persisted_windows_instances() -> None:
    mapping = resolve_first_party_manifests(ToolRegistry())["sync_arrive_list"].to_mapping()
    mapping["execution_platform"] = "windows"
    mapping["worker_requirement"] = {
        "required": True,
        "interactive_session": True,
        "supported_os": ["windows"],
        "queue_deadline_seconds": 3600,
    }
    manifest = AutomationPluginManifest.from_mapping(mapping)
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="1" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_UPLOAD,
        install_root="/unused/windows-plugin",
    )
    instance = PluginInstanceRecord(
        automation_id="windows-worker-instance",
        display_name="Deferred Windows action",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.DISABLED,
        active_version=version,
    )
    repository = _CatalogRepository(instance)
    catalog = PluginCatalog(
        repository,
        allowed_execution_platforms=("server",),
    )

    assert catalog.list() == []
    assert catalog.get(instance.automation_id) is None
    assert catalog.excluded_persisted_automation_ids() == {
        instance.automation_id
    }
    calls_before_projection = repository.list_calls
    assert catalog.safe_projection()["hidden_automation_ids"] == [
        instance.automation_id
    ]
    assert repository.list_calls == calls_before_projection + 1


def test_server_catalog_quarantines_historical_deferred_plugin_uuid() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["r7_arrival_checkin"]
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="2" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_UPLOAD,
        install_root="/unused/deferred-plugin",
    )
    instance = PluginInstanceRecord(
        automation_id="b9b157b8-03ca-4a6f-8b5d-a38c8b04e293",
        display_name="Historical deferred action",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.DISABLED,
        active_version=version,
    )
    catalog = PluginCatalog(
        _CatalogRepository(instance),
        excluded_plugin_ids=("r7_arrival_checkin",),
        allowed_execution_platforms=("server",),
    )

    assert catalog.list() == []
    assert catalog.get(instance.automation_id) is None
    assert catalog.excluded_persisted_automation_ids() == {instance.automation_id}


def test_typed_legacy_exclusion_does_not_hide_identity_collision() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_arrive_list"]
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="3" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_UPLOAD,
        install_root="/unused/server-plugin",
    )
    instance = PluginInstanceRecord(
        automation_id="r7_arrival_checkin",
        display_name="Identity collision",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.DISABLED,
        active_version=version,
    )
    catalog = PluginCatalog(
        _CatalogRepository(instance),
        excluded_automation_plugins={"r7_arrival_checkin": "r7_arrival_checkin"},
        allowed_execution_platforms=("server",),
    )

    assert [entry.automation_id for entry in catalog.list()] == [instance.automation_id]
    assert catalog.get(instance.automation_id) is not None
    assert catalog.excluded_persisted_automation_ids() == set()


def test_disabled_unconfigured_signed_upload_is_non_blocking_before_binding() -> None:
    manifest = resolve_release_first_party_manifests(ToolRegistry())["sync_arrive_list"]
    version = PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256="4" * 64,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_UPLOAD,
        install_root="/unused/unconfigured-plugin",
    )
    instance = PluginInstanceRecord(
        automation_id="27f7634d-d005-4932-a945-a4f3b32e31a2",
        display_name="Installed action awaiting setup",
        plugin_id=manifest.plugin_id,
        state=PluginProjectState.DISABLED,
        active_version=version,
    )
    catalog = PluginCatalog(_CatalogRepository(instance))
    runtime = type(
        "NoGenerationRuntime",
        (),
        {
            "get_project_runtime": staticmethod(lambda _automation_id: None),
            "list_project_generations": staticmethod(lambda _automation_id: ()),
        },
    )()
    target_service = object.__new__(MySQLRuntimeTargetService)
    target_service._catalog = catalog
    target_service._runtime = runtime

    assert target_service.reconcile_all() == ()
    health = catalog.production_health(())
    assert health["ok"] is True
    assert health["unstable_generations"] == []


def test_snapshot_binds_only_closed_desired_material(tmp_path: Path) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=row,
        policy_row=policy,
        generation=1,
        core_catalog=core,
    )
    assert snapshot.execution_metadata["account_bindings"] == {
        "customer_service_source": ("ronghui-a",)
    }
    assert snapshot.execution_metadata["schedule"]["kind"] == "none"
    assert snapshot.plugin_version == FIRST_PARTY_PACKAGE_VERSION
    assert snapshot.trust_source == PluginTrustSource.ED25519_FIRST_PARTY
    assert snapshot.policy_contract_sha256 == _sha(policy)

    tampered = dict(row)
    tampered["config_json"] = {"direction": "outbound"}
    with pytest.raises(Exception, match="digest changed"):
        build_runtime_generation_snapshot(
            entry,
            desired_config_row=tampered,
            policy_row=policy,
            generation=1,
            core_catalog=core,
        )


def test_snapshot_accepts_project_with_every_entrypoint_disabled(tmp_path: Path) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    row["enabled_entrypoints_json"] = []
    row["enabled_entrypoints_sha256"] = _sha([])
    row["compiled_invocations_json"] = {}
    row["compiled_invocations_sha256"] = _sha({})

    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=row,
        policy_row=policy,
        generation=1,
        core_catalog=core,
    )

    assert snapshot.enabled_entrypoints == ()
    assert snapshot.execution_metadata["compiled_invocations"] == {}


def test_runtime_health_fails_closed_when_real_sandbox_canary_failed(monkeypatch) -> None:
    class _HealthCatalog:
        @staticmethod
        def production_health(_expected):
            return {"ok": True, "runnable": True, "runtime_status": "READY"}

        @staticmethod
        def excluded_persisted_automation_ids():
            return set()

    monkeypatch.setattr(
        production_module,
        "runtime_generation_health",
        lambda *_args, **_kwargs: SimpleNamespace(
            healthy=True,
            project_count=1,
            committed_count=1,
            active_lease_count=0,
            blocked_projects={},
        ),
    )
    runtime = object.__new__(ProductionAutomationPluginRuntime)
    runtime.catalog = _HealthCatalog()
    runtime.required_first_party_ids = frozenset({"project-a"})
    runtime.runtime_repository = object()
    runtime.release = SimpleNamespace(verified_release_sha="a" * 40)
    runtime._started = True
    runtime._sandbox_canary = SandboxCanaryResult(
        healthy=False,
        code="PLUGIN_SANDBOX_CANARY_FAILED",
        checked_at=datetime.now(timezone.utc),
    )

    health = runtime.health()

    assert health["ok"] is False
    assert health["runnable"] is False
    assert health["runtime_status"] == "UNAVAILABLE"
    assert health["sandbox"]["state"] == "unavailable"
    assert health["sandbox"]["code"] == "PLUGIN_SANDBOX_CANARY_FAILED"


def test_policy_must_target_exact_generation(tmp_path: Path) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    policy["project_generation"] = 2
    with pytest.raises(Exception, match="desired runtime generation"):
        build_runtime_generation_snapshot(
            entry,
            desired_config_row=row,
            policy_row=policy,
            generation=1,
            core_catalog=core,
        )


def test_coeffects_require_structural_account_and_all_typed_handlers(
    tmp_path: Path,
) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=row,
        policy_row=policy,
        generation=1,
        core_catalog=core,
    )
    required = [
        (item["operation"], item["action"])
        for item in entry.runtime_permissions["broker_operations"]
    ]
    ready = ProductionRuntimeCoeffectProvider(
        core_catalog=core,
        broker_handler_keys=required,
        account_manager=_AccountManager(),
    ).observe(snapshot)
    assert ready
    assert all(item.ready for item in ready)
    assert {item.kind for item in ready} == {
        RuntimeCoeffectKind.CORE_ADAPTER,
        RuntimeCoeffectKind.ACCOUNT,
    }

    blocked = ProductionRuntimeCoeffectProvider(
        core_catalog=core,
        broker_handler_keys=required[:-1],
        account_manager=_AccountManager(authenticated=False),
    ).observe(snapshot)
    reasons = {item.reason_code for item in blocked if not item.ready}
    assert reasons == {"CORE_ADAPTER_ACTION_UNAVAILABLE"}
    assert all(item.kind is not RuntimeCoeffectKind.SESSION for item in blocked)

    for manager in (
        _AccountManager(active=False),
        _AccountManager(system="r7"),
    ):
        account_blocked = ProductionRuntimeCoeffectProvider(
            core_catalog=core,
            broker_handler_keys=required,
            account_manager=manager,
        ).observe(snapshot)
        assert {
            item.reason_code for item in account_blocked if not item.ready
        } == {"BLOCKED_CONFIG"}


def test_resource_coeffect_binds_exact_managed_resource_revision(
    tmp_path: Path,
) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=row,
        policy_row=policy,
        generation=1,
        core_catalog=core,
    )
    resource = {
        "resource_kind": "webhook_route",
        "path": "webhook/phase7/test",
        "_meta": {
            "source": "test",
            "configuration_version": 2,
            "config_sha256": "a" * 64,
            "updated_at": "2026-08-15 12:00:00",
        },
    }
    metadata = copy.deepcopy(dict(snapshot.execution_metadata))
    metadata["resource_bindings"] = {"webhook_route": "route-one"}
    metadata["runtime_descriptor"]["resource_roles"] = [
        {
            "role": "webhook_route",
            "allowed_kinds": ["webhook_route"],
            "required": True,
        }
    ]
    snapshot = replace(snapshot, execution_metadata=metadata)

    class _Workers:
        @staticmethod
        def get_worker_device(_device_id):
            return None

    resolver = ProductionProjectBindingResolver(
        account_manager=_AccountManager(),
        resource_provider=lambda resource_id: (
            resource if resource_id == "route-one" else None
        ),
        worker_repository=_Workers(),
    )
    required = [
        (item["operation"], item["action"])
        for item in entry.runtime_permissions["broker_operations"]
    ]
    provider = ProductionRuntimeCoeffectProvider(
        core_catalog=core,
        broker_handler_keys=required,
        account_manager=_AccountManager(),
        binding_resolver=resolver,
    )

    first = provider.observe(snapshot)
    first_resource = next(
        item for item in first if item.kind is RuntimeCoeffectKind.RESOURCE
    )
    assert first_resource.ready is True

    resource["_meta"]["configuration_version"] = 3
    resource["_meta"]["config_sha256"] = "b" * 64
    second = provider.observe(snapshot)
    second_resource = next(
        item for item in second if item.kind is RuntimeCoeffectKind.RESOURCE
    )
    assert second_resource.ready is True
    assert second_resource.revision != first_resource.revision

    resource["_meta"].pop("config_sha256")
    blocked = provider.observe(snapshot)
    blocked_resource = next(
        item for item in blocked if item.kind is RuntimeCoeffectKind.RESOURCE
    )
    assert blocked_resource.ready is False
    assert blocked_resource.reason_code == "PLUGIN_RESOURCE_REVISION_INVALID"


def test_effect_plan_and_driver_are_reversible_and_integrity_bound(
    tmp_path: Path,
) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=row,
        policy_row=policy,
        generation=1,
        core_catalog=core,
    )
    required = [
        (item["operation"], item["action"])
        for item in entry.runtime_permissions["broker_operations"]
    ]
    plans = ProductionRuntimeEffectPlanner().plan(snapshot)
    assert [item.kind for item in plans] == [
        RuntimeEffectKind.PACKAGE_REFERENCE,
        RuntimeEffectKind.VENV_REFERENCE,
        RuntimeEffectKind.INSTANCE_RUNTIME,
        RuntimeEffectKind.BROKER_SCOPE,
    ]
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=required)
    for sequence, plan in enumerate(plans, start=1):
        planned = RuntimeEffectRecord(
            effect_id=f"effect-{sequence}",
            automation_id=snapshot.automation_id,
            generation=snapshot.generation,
            sequence=sequence,
            kind=plan.kind,
            state=RuntimeEffectState.PLANNED,
            reversible=True,
            effect_key=plan.effect_key,
            payload=plan.payload,
        )
        applied = driver.ensure_applied(snapshot=snapshot, plan=plan, effect=planned)
        assert applied.state == RuntimeEffectState.APPLIED
        assert applied.payload == plan.payload
        driver.dispose(applied)

    payload = tmp_path / "plugin" / "package" / "payload" / "main.py"
    payload.write_bytes(b"tampered")
    with pytest.raises(Exception, match="integrity"):
        driver.ensure_applied(
            snapshot=snapshot,
            plan=plans[0],
            effect=RuntimeEffectRecord(
                effect_id="tampered",
                automation_id=snapshot.automation_id,
                generation=1,
                sequence=1,
                kind=plans[0].kind,
                state=RuntimeEffectState.PLANNED,
                reversible=True,
                effect_key=plans[0].effect_key,
                payload=plans[0].payload,
            ),
        )


def test_disabled_scheduler_entrypoint_keeps_schedule_without_binding_effect(
    tmp_path: Path,
) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    schedule = {"kind": "daily", "times": ["08:30"], "enabled": True}
    row["desired_schedule_json"] = schedule
    row["desired_schedule_sha256"] = _sha(schedule)

    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=row,
        policy_row=policy,
        generation=1,
        core_catalog=core,
    )

    assert snapshot.execution_metadata["schedule"] == schedule
    assert RuntimeEffectKind.SCHEDULE_BINDING not in {
        item.kind for item in ProductionRuntimeEffectPlanner().plan(snapshot)
    }


def test_effect_ack_round_trips_exact_reserved_payload_and_rejects_drift(
    tmp_path: Path,
) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=row,
        policy_row=policy,
        generation=1,
        core_catalog=core,
    )
    plan = ProductionRuntimeEffectPlanner().plan(snapshot)[0]
    payload = dict(plan.payload)
    effect_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "boyi:automation-effect:"
            f"{snapshot.automation_id}:{snapshot.generation}:1:{plan.effect_key}",
        )
    )
    planned_row = {
        "effect_id": effect_id,
        "automation_id": snapshot.automation_id,
        "generation": snapshot.generation,
        "effect_sequence": 1,
        "effect_kind": plan.kind.value,
        "effect_key": plan.effect_key,
        "reversible": True,
        "state": RuntimeEffectState.PLANNED.value,
        "evidence_json": payload,
        "evidence_sha256": _json_hash(payload),
    }
    applied_row = {**planned_row, "state": RuntimeEffectState.APPLIED.value}
    repository = AutomationPluginRepository(
        _ScriptedGenerationConnection(
            [
                ("SELECT state FROM automation_project_generations", {"state": "PREPARING"}, 0),
                ("SELECT COUNT(*) AS unavailable_count", {"unavailable_count": 0}, 0),
                ("INSERT INTO automation_project_generation_effects", None, 1),
                ("SELECT * FROM automation_project_generation_effects", planned_row, 0),
                ("SELECT * FROM automation_project_generation_effects", planned_row, 0),
                ("UPDATE automation_project_generation_effects", None, 1),
                ("SELECT * FROM automation_project_generation_effects", applied_row, 0),
            ]
        )
    )
    reserved = repository.reserve_generation_effect_row(
        snapshot_to_row(snapshot),
        plan={
            "kind": plan.kind.value,
            "effect_key": plan.effect_key,
            "payload": payload,
            "reversible": plan.reversible,
        },
        sequence=1,
    )
    planned = RuntimeEffectRecord(
        effect_id=str(reserved["effect_id"]),
        automation_id=str(reserved["automation_id"]),
        generation=int(reserved["generation"]),
        sequence=int(reserved["effect_sequence"]),
        kind=RuntimeEffectKind(str(reserved["effect_kind"])),
        state=RuntimeEffectState(str(reserved["state"])),
        reversible=bool(reserved["reversible"]),
        effect_key=str(reserved["effect_key"]),
        payload=dict(reserved["evidence_json"]),
    )
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=[
            (item["operation"], item["action"])
            for item in entry.runtime_permissions["broker_operations"]
        ]
    )
    applied = driver.ensure_applied(snapshot=snapshot, plan=plan, effect=planned)
    ack = {
        "effect_id": applied.effect_id,
        "automation_id": applied.automation_id,
        "generation": applied.generation,
        "sequence": applied.sequence,
        "kind": applied.kind.value,
        "reversible": applied.reversible,
        "effect_key": applied.effect_key,
        "payload": dict(applied.payload),
    }

    persisted = repository.mark_generation_effect_applied_row(ack)

    assert persisted["state"] == RuntimeEffectState.APPLIED.value
    assert applied.payload == plan.payload == persisted["evidence_json"]
    drifted_payloads = (
        {**payload, "effect_contract_sha256": "f" * 64},
        {**payload, next(iter(payload)): "0" * 64},
        {key: value for key, value in payload.items() if key != next(iter(payload))},
    )
    for drifted_payload in drifted_payloads:
        drift_repository = AutomationPluginRepository(
            _ScriptedGenerationConnection(
                [("SELECT * FROM automation_project_generation_effects", applied_row, 0)]
            )
        )
        with pytest.raises(IdempotencyConflict, match="does not match"):
            drift_repository.mark_generation_effect_applied_row(
                {**ack, "payload": drifted_payload}
            )


def test_generation_prepare_rejects_interpreter_symlink_escape(tmp_path: Path) -> None:
    core, entry, row, policy = _entry_and_row(tmp_path)
    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=row,
        policy_row=policy,
        generation=1,
        core_catalog=core,
    )
    python_file = tmp_path / "plugin" / "venv" / "bin" / "python"
    python_file.unlink()
    python_file.symlink_to(Path("/usr/bin/python3"))
    plans = ProductionRuntimeEffectPlanner().plan(snapshot)
    plan = next(item for item in plans if item.kind == RuntimeEffectKind.INSTANCE_RUNTIME)
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=[
            (item["operation"], item["action"])
            for item in entry.runtime_permissions["broker_operations"]
        ]
    )
    effect = RuntimeEffectRecord(
        effect_id="escaped-python",
        automation_id=snapshot.automation_id,
        generation=1,
        sequence=3,
        kind=plan.kind,
        state=RuntimeEffectState.PLANNED,
        reversible=True,
        effect_key=plan.effect_key,
        payload=plan.payload,
    )

    with pytest.raises(Exception, match="unsafe filesystem entry"):
        driver.ensure_applied(snapshot=snapshot, plan=plan, effect=effect)


def test_cursor_secret_is_stable_and_fail_closed() -> None:
    secret = "s" * 48
    assert production_cursor_secret({CURSOR_SECRET_ENV: secret}) == secret.encode()
    with pytest.raises(Exception, match=CURSOR_SECRET_ENV):
        production_cursor_secret({})
