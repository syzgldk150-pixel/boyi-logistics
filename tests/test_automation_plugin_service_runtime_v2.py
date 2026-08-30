from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import Sequence

import pytest

from agent.automation_plugins.catalog import PluginCatalog, PluginCatalogEntry
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.generation import AutomationRuntimeReconciler
from agent.automation_plugins.host_capability_registry import governance_for_effect
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    PluginProjectState,
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeActivationPhase,
    RuntimeCoeffectKind,
    RuntimeEffectKind,
    RuntimeEffectRecord,
    RuntimeEffectState,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
    RuntimeReconcileState,
)
from agent.automation_plugins.production import (
    ManagedContributionRegistry,
    ProductionRuntimeCoeffectProvider,
    ProductionRuntimeEffectDriver,
    ProductionRuntimeEffectPlanner,
)
from agent.automation_plugins.service_registry import (
    ServiceProviderAmbiguous,
    ServiceProviderConflict,
    ServiceRegistry,
    ServiceUnavailable,
    package_provider_registration_id,
)


class _CoreCatalog:
    @staticmethod
    def get_capability(_name: str):
        raise AssertionError("service-v2 coeffects must not consult the v1 registry")


class _Accounts:
    @staticmethod
    def list_accounts(*, include_status: bool, validate: bool):
        assert include_status is False
        assert validate is False
        return []


class _EmptyCatalogRepository:
    @staticmethod
    def list_instances():
        return []

    @staticmethod
    def get_instance(_automation_id: str):
        return None


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _snapshot(
    *,
    automation_id: str,
    plugin_id: str,
    package_sha256: str,
    manifest_sha256: str,
    generation: int = 1,
    version: str = "1.0.0",
    requires: tuple[str, ...] = (),
    with_scheduler: bool = False,
) -> RuntimeGenerationSnapshot:
    service = f"plugin.{plugin_id}.runner@1"
    scheduler = (
        [
            {
                "id": "daily_run",
                "service": service,
                "operation": "run",
                "schedule": {
                    "kind": "cron",
                    "expression": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                },
            }
        ]
        if with_scheduler
        else []
    )
    enabled_entrypoints = (
        ("run_now", "daily_run") if with_scheduler else ("run_now",)
    )
    project_schedule = (
        {
            "kind": "daily_times",
            "times": ["09:00"],
            "enabled": True,
        }
        if with_scheduler
        else {"kind": "none", "enabled": False}
    )
    execution_metadata = {
        "account_bindings": {},
        "resource_bindings": {},
        "device_binding": None,
        "schedule": project_schedule,
        "compiled_invocations": {
            entrypoint: {
                "arguments": {},
                "dynamic_resolvers": {},
                "target": {
                    "service": service,
                    "operation": "run",
                    "contribution_id": entrypoint,
                    "contribution_kind": (
                        "scheduler" if entrypoint == "daily_run" else "console"
                    ),
                },
                "governance": governance_for_effect("read").to_mapping(),
            }
            for entrypoint in enabled_entrypoints
        },
        "governance_anchor": {},
        "runtime_descriptor": {
            "install_metadata": {},
            "runtime": {"mode": "on_demand"},
            "runtime_permissions": {"broker_operations": []},
            "account_roles": [],
            "resource_roles": [],
        },
        "service_contracts": {
            "provides": [
                {
                    "service": service,
                    "operations": [{"name": "run", "effect": "read"}],
                }
            ],
            "requires": [{"service": item} for item in requires],
        },
        "contributions": {
            "console": [
                {
                    "id": "run_now",
                    "service": service,
                    "operation": "run",
                }
            ],
            "scheduler": scheduler,
            "webhook": [],
            "feishu": [],
            "events": [],
        },
        "storage_contract": {},
    }
    return RuntimeGenerationSnapshot(
        automation_id=automation_id,
        generation=generation,
        plugin_id=plugin_id,
        plugin_version=version,
        package_sha256=package_sha256,
        manifest_sha256=manifest_sha256,
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD,
        project_config_sha256="1" * 64,
        account_bindings_sha256="2" * 64,
        resource_bindings_sha256="3" * 64,
        device_binding_sha256="4" * 64,
        schedule_sha256=_sha(project_schedule),
        core_registry_sha256="6" * 64,
        tool_contract_sha256="7" * 64,
        invocation_contracts_sha256="8" * 64,
        compiled_invocations_sha256="9" * 64,
        runtime_descriptor_sha256="a" * 64,
        governance_anchor_sha256="b" * 64,
        policy_contract_sha256="c" * 64,
        enabled_entrypoints=enabled_entrypoints,
        execution_metadata=execution_metadata,
        created_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )


def _catalog_entry(
    *,
    automation_id: str,
    plugin_id: str,
    requires: tuple[str, ...] = (),
    enabled: bool = True,
    configured: bool = True,
    account_id: str | None = None,
    runtime_mode: str = "on_demand",
) -> PluginCatalogEntry:
    package_sha = (plugin_id[0] if plugin_id else "a") * 64
    manifest_sha = (plugin_id[-1] if plugin_id else "b") * 64
    snapshot = _snapshot(
        automation_id=automation_id,
        plugin_id=plugin_id,
        package_sha256=package_sha,
        manifest_sha256=manifest_sha,
        requires=requires,
    )
    service = f"plugin.{plugin_id}.runner@1"
    account_roles = (
        (
            {
                "role": "operator",
                "allowed_systems": ["ronghui"],
                "required": True,
            },
        )
        if account_id is not None
        else ()
    )
    return PluginCatalogEntry(
        automation_id=automation_id,
        plugin_id=plugin_id,
        manifest_schema_version=2,
        display_name=plugin_id,
        name=plugin_id,
        state=(
            PluginProjectState.ENABLED.value
            if enabled
            else PluginProjectState.DISABLED.value
        ),
        record_version=1,
        installed_version="1.0.0",
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
        package_sha256=package_sha,
        manifest_sha256=manifest_sha,
        config_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        account_roles=account_roles,
        resource_roles=(),
        allowed_entrypoints=("run_now",),
        invocation_contracts={
            "run_now": {
                "service": service,
                "operation": "run",
                "contribution_kind": "console",
                "effect": "read",
                "governance": governance_for_effect("read").to_mapping(),
            }
        },
        governance_anchor={},
        governance_anchor_sha256="0" * 64,
        tool_contract={"description": plugin_id},
        worker_requirement={"required": False},
        execution_platform="server",
        runtime={"kind": "python_subprocess", "mode": runtime_mode},
        scheduling={"supported": False},
        project_full_auto_allowed=True,
        runtime_permissions={},
        signed_runtime_permissions={},
        enabled=enabled,
        configured=configured,
        project_config={},
        account_bindings=(
            {"operator": account_id} if account_id is not None else {}
        ),
        resource_bindings={},
        project_schedule={"kind": "none", "enabled": False},
        install_root=f"/plugins/{plugin_id}/1.0.0",
        device_binding=None,
        install_metadata={},
        project_config_version=1 if configured else 0,
        project_config_sha256=snapshot.project_config_sha256,
        account_bindings_sha256=snapshot.account_bindings_sha256,
        resource_bindings_sha256=snapshot.resource_bindings_sha256,
        device_binding_sha256=snapshot.device_binding_sha256,
        enabled_entrypoints=("run_now",),
        current_enabled_entrypoints=("run_now",),
        target_generation=1,
        committed_generation=1,
        reconcile_state=RuntimeReconcileState.STABLE,
        committed_snapshot=snapshot,
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        plugin_api="2.0.0",
        runtime_mode=runtime_mode,
        provided_services=(service,),
        required_services=requires,
        contributions=snapshot.execution_metadata["contributions"],
        declared_capabilities=(),
        storage_contract={},
        service_contracts={
            "provides": [
                {
                    "service": service,
                    "operations": [{"name": "run", "effect": "read"}],
                }
            ],
            "requires": [{"service": item} for item in requires],
        },
    )


def _service_plan(snapshot: RuntimeGenerationSnapshot):
    return next(
        plan
        for plan in ProductionRuntimeEffectPlanner().plan(snapshot)
        if plan.kind is RuntimeEffectKind.SERVICE_REGISTRATION
    )


def _effect(
    snapshot: RuntimeGenerationSnapshot,
    *,
    state: RuntimeEffectState = RuntimeEffectState.PLANNED,
) -> RuntimeEffectRecord:
    plan = _service_plan(snapshot)
    return RuntimeEffectRecord(
        effect_id=f"effect-{snapshot.automation_id}-{snapshot.generation}",
        automation_id=snapshot.automation_id,
        generation=snapshot.generation,
        sequence=5,
        kind=plan.kind,
        state=state,
        reversible=True,
        effect_key=plan.effect_key,
        payload=plan.payload,
    )


def _apply(
    driver: ProductionRuntimeEffectDriver,
    snapshot: RuntimeGenerationSnapshot,
) -> RuntimeEffectRecord:
    plan = _service_plan(snapshot)
    return driver.ensure_applied(
        snapshot=snapshot,
        plan=plan,
        effect=_effect(snapshot),
    )


def _commit_service(
    driver: ProductionRuntimeEffectDriver,
    snapshot: RuntimeGenerationSnapshot,
) -> RuntimeEffectRecord:
    effect = _apply(driver, snapshot)
    driver.activate_committed(snapshot=snapshot, effects=(effect,))
    return effect


def _committed_projection_effects(
    snapshot: RuntimeGenerationSnapshot,
) -> tuple[RuntimeEffectRecord, ...]:
    effects: list[RuntimeEffectRecord] = []
    for sequence, plan in enumerate(
        ProductionRuntimeEffectPlanner().plan(snapshot),
        start=1,
    ):
        if (
            plan.kind is not RuntimeEffectKind.SERVICE_REGISTRATION
            and plan.payload.get("contract_version") != 1
        ):
            continue
        effects.append(
            RuntimeEffectRecord(
                effect_id=(
                    f"projection-{snapshot.automation_id}-"
                    f"{snapshot.generation}-{sequence}"
                ),
                automation_id=snapshot.automation_id,
                generation=snapshot.generation,
                sequence=sequence,
                kind=plan.kind,
                state=RuntimeEffectState.APPLIED,
                reversible=True,
                effect_key=plan.effect_key,
                payload=plan.payload,
            )
        )
    return tuple(effects)


def test_prepared_service_effect_is_not_routable_before_commit() -> None:
    registry = ServiceRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        service_registry=registry,
    )
    snapshot = _snapshot(
        automation_id="prepared-project",
        plugin_id="prepared_plugin",
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )

    effect = _apply(driver, snapshot)

    assert effect.state is RuntimeEffectState.APPLIED
    assert registry.snapshot() == ()
    assert registry.provider_for("plugin.prepared_plugin.runner@1") is None

    driver.activate_committed(snapshot=snapshot, effects=(effect,))
    assert registry.require_provider("plugin.prepared_plugin.runner@1").active is True


def test_same_immutable_package_has_one_provider_and_reference_counted_projects() -> None:
    registry = ServiceRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        service_registry=registry,
    )
    package_sha = "d" * 64
    manifest_sha = "e" * 64
    first = _snapshot(
        automation_id="first-project",
        plugin_id="shared_plugin",
        package_sha256=package_sha,
        manifest_sha256=manifest_sha,
    )
    second = _snapshot(
        automation_id="second-project",
        plugin_id="shared_plugin",
        package_sha256=package_sha,
        manifest_sha256=manifest_sha,
        generation=7,
    )

    first_effect = _commit_service(driver, first)
    second_effect = _commit_service(driver, second)

    assert len(registry.snapshot()) == 1
    with pytest.raises(ServiceProviderAmbiguous):
        registry.require_provider("plugin.shared_plugin.runner@1")
    assert driver.service_reference_count(package_sha) == 2

    driver.dispose(first_effect)
    driver.dispose(first_effect)
    assert registry.provider_for("plugin.shared_plugin.runner@1") is not None
    assert driver.service_reference_count(package_sha) == 1

    driver.dispose(second_effect)
    assert registry.provider_for("plugin.shared_plugin.runner@1") is None
    assert registry.snapshot() == ()


def test_byte_different_packages_coexist_but_bare_service_route_is_ambiguous() -> None:
    registry = ServiceRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        service_registry=registry,
    )
    current = _snapshot(
        automation_id="current-project",
        plugin_id="conflict_plugin",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
    )
    contender = _snapshot(
        automation_id="contender-project",
        plugin_id="conflict_plugin",
        package_sha256="3" * 64,
        manifest_sha256="4" * 64,
    )
    _commit_service(driver, current)
    contender_effect = _apply(driver, contender)
    driver.activate_committed(
        snapshot=contender,
        effects=(contender_effect,),
    )

    assert len(registry.snapshot()) == 2
    assert driver.service_reference_count(contender.package_sha256) == 1
    with pytest.raises(ServiceProviderAmbiguous):
        registry.require_provider("plugin.conflict_plugin.runner@1")


def test_required_service_coeffect_recovers_and_distinguishes_blocked_provider() -> None:
    registry = ServiceRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        service_registry=registry,
    )
    required_service = "plugin.base_plugin.runner@1"
    consumer = _snapshot(
        automation_id="consumer-project",
        plugin_id="consumer_plugin",
        package_sha256="5" * 64,
        manifest_sha256="6" * 64,
        requires=(required_service,),
    )
    provider = ProductionRuntimeCoeffectProvider(
        core_catalog=_CoreCatalog(),
        broker_handler_keys=(),
        account_manager=_Accounts(),
        service_registry=registry,
    )

    missing = next(
        item
        for item in provider.observe(consumer)
        if item.kind is RuntimeCoeffectKind.SERVICE
    )
    assert missing.ready is False
    assert missing.reason_code == "BLOCKED_DEPENDENCY"

    blocked_base = _snapshot(
        automation_id="base-project",
        plugin_id="base_plugin",
        package_sha256="7" * 64,
        manifest_sha256="8" * 64,
        requires=("plugin.missing_plugin.runner@1",),
    )
    blocked_effect = _apply(driver, blocked_base)
    driver._ensure_service_reference(blocked_effect.payload)
    blocked = next(
        item
        for item in provider.observe(consumer)
        if item.kind is RuntimeCoeffectKind.SERVICE
    )
    assert blocked.ready is False
    assert blocked.reason_code == "BLOCKED_DEPENDENCY"
    assert blocked.revision != missing.revision

    driver.dispose(blocked_effect)
    ready_base = _snapshot(
        automation_id="base-project",
        plugin_id="base_plugin",
        package_sha256="9" * 64,
        manifest_sha256="a" * 64,
    )
    _commit_service(driver, ready_base)
    ready = next(
        item
        for item in provider.observe(consumer)
        if item.kind is RuntimeCoeffectKind.SERVICE
    )
    assert ready.ready is True
    assert ready.reason_code is None
    assert ready.revision not in {missing.revision, blocked.revision}


def test_provider_disable_and_uninstall_withdraw_consumer_schedule_until_restore() -> None:
    services = ServiceRegistry()
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        service_registry=services,
        contribution_registry=contributions,
    )
    provider_snapshot = _snapshot(
        automation_id="provider-project",
        plugin_id="base_plugin",
        package_sha256="b" * 64,
        manifest_sha256="c" * 64,
    )
    consumer_snapshot = _snapshot(
        automation_id="consumer-project",
        plugin_id="consumer_plugin",
        package_sha256="d" * 64,
        manifest_sha256="e" * 64,
        requires=("plugin.base_plugin.runner@1",),
        with_scheduler=True,
    )
    provider_effects = _committed_projection_effects(provider_snapshot)
    consumer_effects = _committed_projection_effects(consumer_snapshot)
    provider_generation = RuntimeGenerationRecord(
        snapshot=provider_snapshot,
        state=RuntimeGenerationState.COMMITTED,
        effects=provider_effects,
    )
    consumer_generation = RuntimeGenerationRecord(
        snapshot=consumer_snapshot,
        state=RuntimeGenerationState.COMMITTED,
        effects=consumer_effects,
    )

    class _SchedulerGate:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []
            self.physical_enabled = {consumer_snapshot.automation_id: True}

        def set_project_dependency_scheduler_gate(
            self,
            automation_id: str,
            *,
            dependency_ready: bool,
        ) -> dict[str, object]:
            self.calls.append((automation_id, dependency_ready))
            enabled = bool(
                dependency_ready
                and automation_id == consumer_snapshot.automation_id
            )
            self.physical_enabled[automation_id] = enabled
            return {
                "automation_id": automation_id,
                "scheduler_enabled": enabled,
            }

    gate = _SchedulerGate()
    coeffects = ProductionRuntimeCoeffectProvider(
        core_catalog=_CoreCatalog(),
        broker_handler_keys=(),
        account_manager=_Accounts(),
        service_registry=services,
    )
    reconciler = AutomationRuntimeReconciler(
        repository=gate,
        coeffects=coeffects,
        planner=ProductionRuntimeEffectPlanner(),
        driver=driver,
    )
    driver.activate_committed(
        snapshot=provider_snapshot,
        effects=provider_effects,
    )
    driver.activate_committed(
        snapshot=consumer_snapshot,
        effects=consumer_effects,
    )
    assert services.provider_for("plugin.consumer_plugin.runner@1") is not None
    assert {
        (
            item.automation_id,
            item.contribution_kind,
            item.contribution_id,
        )
        for item in contributions.snapshot()
    } == {
        (provider_snapshot.automation_id, "console", "run_now"),
        (consumer_snapshot.automation_id, "console", "run_now"),
        (consumer_snapshot.automation_id, "scheduler", "daily_run"),
    }
    assert all(item.dispatch_available for item in contributions.snapshot())

    provider_disabled = reconciler.reconcile_committed_projection(
        provider_generation,
        project_enabled=False,
    )
    consumer_blocked = reconciler.reconcile_committed_projection(
        consumer_generation,
        project_enabled=True,
    )

    assert provider_disabled.waiting_coeffects == ("PROJECT_DISABLED",)
    assert consumer_blocked.waiting_coeffects == ("BLOCKED_DEPENDENCY",)
    assert services.provider_for("plugin.base_plugin.runner@1") is None
    assert services.provider_for("plugin.consumer_plugin.runner@1") is None
    assert contributions.snapshot() == ()
    assert gate.physical_enabled[consumer_snapshot.automation_id] is False

    reconciler.reconcile_committed_projection(
        provider_generation,
        project_enabled=True,
    )
    consumer_restored = reconciler.reconcile_committed_projection(
        consumer_generation,
        project_enabled=True,
    )

    assert consumer_restored.waiting_coeffects == ()
    assert services.provider_for("plugin.consumer_plugin.runner@1") is not None
    assert {
        (
            item.automation_id,
            item.contribution_kind,
            item.contribution_id,
        )
        for item in contributions.snapshot()
    } == {
        (provider_snapshot.automation_id, "console", "run_now"),
        (consumer_snapshot.automation_id, "console", "run_now"),
        (consumer_snapshot.automation_id, "scheduler", "daily_run"),
    }
    assert all(item.dispatch_available for item in contributions.snapshot())
    assert gate.physical_enabled[consumer_snapshot.automation_id] is True

    provider_effect = next(
        effect
        for effect in provider_effects
        if effect.kind is RuntimeEffectKind.SERVICE_REGISTRATION
    )
    driver.dispose(provider_effect)
    consumer_uninstalled = reconciler.reconcile_committed_projection(
        consumer_generation,
        project_enabled=True,
    )

    assert consumer_uninstalled.waiting_coeffects == ("BLOCKED_DEPENDENCY",)
    remaining = contributions.snapshot()
    assert len(remaining) == 1
    assert remaining[0].automation_id == provider_snapshot.automation_id
    assert remaining[0].contribution_kind == "console"
    assert remaining[0].contribution_id == "run_now"
    assert remaining[0].dispatch_available is True
    assert gate.physical_enabled[consumer_snapshot.automation_id] is False


def test_committed_projection_switch_keeps_old_generation_until_refresh_retries() -> None:
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=contributions,
    )
    first = _snapshot(
        automation_id="atomic-project",
        plugin_id="atomic_plugin",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        generation=1,
        with_scheduler=True,
    )
    second = _snapshot(
        automation_id="atomic-project",
        plugin_id="atomic_plugin",
        package_sha256="3" * 64,
        manifest_sha256="4" * 64,
        generation=2,
        version="2.0.0",
        with_scheduler=True,
    )
    first_effects = _committed_projection_effects(first)
    second_effects = _committed_projection_effects(second)
    refresh_state = {"fail": False}

    def refresh_scheduler() -> dict[str, object]:
        if refresh_state["fail"]:
            raise RuntimeError("synthetic Scheduler refresh failure")
        return {"initialized": True, "invalid_tasks": []}

    driver.bind_scheduler_projection_refresher(refresh_scheduler)
    driver.activate_committed(snapshot=first, effects=first_effects)
    first_route = driver.service_registry.require_operation(
        "plugin.atomic_plugin.runner@1",
        "run",
    )
    assert first_route.project_generation == 1
    assert first_route.package_sha256 == "1" * 64
    assert contributions.active_generation(first.automation_id) == 1
    assert {
        (item["generation"], item["contribution_kind"], item["contribution_id"])
        for item in contributions.active_snapshot(automation_id=first.automation_id)
    } == {
        (1, "console", "run_now"),
        (1, "scheduler", "daily_run"),
    }

    refresh_state["fail"] = True
    with pytest.raises(PluginConflictError) as raised:
        driver.activate_committed(snapshot=second, effects=second_effects)

    assert raised.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    retained_route = driver.service_registry.require_operation(
        "plugin.atomic_plugin.runner@1",
        "run",
    )
    assert retained_route.project_generation == 1
    assert retained_route.package_sha256 == "1" * 64
    candidate_reference = driver.service_registry.project_reference(
        automation_id=second.automation_id,
        generation=second.generation,
    )
    assert candidate_reference is not None
    assert candidate_reference.accepts_new_calls is False
    assert contributions.active_generation(first.automation_id) == 1
    assert {
        item["generation"]
        for item in contributions.active_snapshot(automation_id=first.automation_id)
    } == {1}

    refresh_state["fail"] = False
    assert driver.refresh_contribution_projection() == {
        "initialized": True,
        "invalid_tasks": [],
    }
    promoted_route = driver.service_registry.require_operation(
        "plugin.atomic_plugin.runner@1",
        "run",
    )
    assert promoted_route.project_generation == 2
    assert promoted_route.package_sha256 == "3" * 64
    assert contributions.active_generation(first.automation_id) == 2
    assert {
        (item["generation"], item["contribution_kind"], item["contribution_id"])
        for item in contributions.active_snapshot(automation_id=first.automation_id)
    } == {
        (2, "console", "run_now"),
        (2, "scheduler", "daily_run"),
    }


def test_durable_reverse_discards_candidate_and_rebuilds_exact_old_projection() -> None:
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=contributions,
    )
    first = _snapshot(
        automation_id="reverse-project",
        plugin_id="reverse_plugin",
        package_sha256="7" * 64,
        manifest_sha256="8" * 64,
        generation=1,
        with_scheduler=True,
    )
    second = _snapshot(
        automation_id="reverse-project",
        plugin_id="reverse_plugin",
        package_sha256="9" * 64,
        manifest_sha256="a" * 64,
        generation=2,
        version="2.0.0",
        with_scheduler=True,
    )
    first_effects = _committed_projection_effects(first)
    second_effects = _committed_projection_effects(second)
    fail_refresh = {"value": False}

    def refresh_scheduler() -> dict[str, object]:
        if fail_refresh["value"]:
            raise RuntimeError("synthetic candidate refresh failure")
        return {"initialized": True, "invalid_tasks": []}

    driver.bind_scheduler_projection_refresher(refresh_scheduler)
    driver.activate_committed(snapshot=first, effects=first_effects)
    fail_refresh["value"] = True
    with pytest.raises(PluginConflictError):
        driver.activate_committed(snapshot=second, effects=second_effects)

    fail_refresh["value"] = False
    driver.rollback_committed_activation(
        snapshot=second,
        effects=second_effects,
        restored_snapshot=first,
        restored_effects=first_effects,
    )

    route = driver.service_registry.require_operation(
        "plugin.reverse_plugin.runner@1",
        "run",
    )
    assert route.project_generation == 1
    assert route.package_sha256 == "7" * 64
    assert driver.service_reference_count(second.package_sha256) == 0
    assert contributions.active_generation(first.automation_id) == 1
    assert {
        item["generation"]
        for item in contributions.active_snapshot(automation_id=first.automation_id)
    } == {1}
    assert driver.projection_signature()[2] == ()


def test_activation_ack_failure_reverses_live_candidate_to_durable_predecessor() -> None:
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=contributions,
    )
    first = _snapshot(
        automation_id="ack-reverse-project",
        plugin_id="ack_reverse_plugin",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        generation=1,
        with_scheduler=True,
    )
    second = _snapshot(
        automation_id="ack-reverse-project",
        plugin_id="ack_reverse_plugin",
        package_sha256="3" * 64,
        manifest_sha256="4" * 64,
        generation=2,
        version="2.0.0",
        with_scheduler=True,
    )
    first_effects = _committed_projection_effects(first)
    second_effects = _committed_projection_effects(second)
    durable_generation: dict[str, int | None] = {"value": 1}
    refreshed_generations: list[int | None] = []

    def refresh_scheduler() -> dict[str, object]:
        refreshed_generations.append(durable_generation["value"])
        return {"initialized": True, "invalid_tasks": []}

    driver.bind_scheduler_projection_refresher(refresh_scheduler)
    driver.activate_committed(snapshot=first, effects=first_effects)
    durable_generation["value"] = 2
    driver.activate_committed(snapshot=second, effects=second_effects)
    assert driver.service_registry.require_operation(
        "plugin.ack_reverse_plugin.runner@1",
        "run",
    ).project_generation == 2
    assert contributions.active_generation(second.automation_id) == 2

    # The durable activation ACK failed, and reverse-CAS has restored gen 1.
    durable_generation["value"] = 1
    driver.rollback_committed_activation(
        snapshot=second,
        effects=second_effects,
        restored_snapshot=first,
        restored_effects=first_effects,
    )

    route = driver.service_registry.require_operation(
        "plugin.ack_reverse_plugin.runner@1",
        "run",
    )
    assert route.project_generation == 1
    assert route.package_sha256 == first.package_sha256
    assert contributions.active_generation(first.automation_id) == 1
    assert {
        item["generation"]
        for item in contributions.active_snapshot(
            automation_id=first.automation_id,
        )
    } == {1}
    assert driver.service_reference_count(second.package_sha256) == 0
    assert refreshed_generations == [1, 2, 1]


def test_first_install_ack_failure_reverses_live_candidate_to_empty_projection() -> None:
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=contributions,
    )
    candidate = _snapshot(
        automation_id="first-install-reverse",
        plugin_id="first_install_reverse_plugin",
        package_sha256="5" * 64,
        manifest_sha256="6" * 64,
        generation=1,
        with_scheduler=True,
    )
    effects = _committed_projection_effects(candidate)
    durable_generation: dict[str, int | None] = {"value": 1}
    refreshed_generations: list[int | None] = []

    def refresh_scheduler() -> dict[str, object]:
        refreshed_generations.append(durable_generation["value"])
        return {"initialized": True, "invalid_tasks": []}

    driver.bind_scheduler_projection_refresher(refresh_scheduler)
    driver.activate_committed(snapshot=candidate, effects=effects)
    assert contributions.active_generation(candidate.automation_id) == 1

    # Reverse-CAS of the first install restores an empty durable projection.
    durable_generation["value"] = None
    driver.rollback_committed_activation(
        snapshot=candidate,
        effects=effects,
    )

    assert contributions.active_generation(candidate.automation_id) is None
    assert contributions.active_snapshot(
        automation_id=candidate.automation_id,
    ) == ()
    assert driver.service_reference_count(candidate.package_sha256) == 0
    with pytest.raises(ServiceUnavailable):
        driver.service_registry.require_operation(
            "plugin.first_install_reverse_plugin.runner@1",
            "run",
        )
    assert refreshed_generations == [1, None]


def test_activation_reverse_refuses_to_overwrite_newer_process_projection() -> None:
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=contributions,
    )
    snapshots = tuple(
        _snapshot(
            automation_id="concurrent-reverse-project",
            plugin_id="concurrent_reverse_plugin",
            package_sha256=character * 64,
            manifest_sha256=character.upper().lower() * 64,
            generation=generation,
            version=f"{generation}.0.0",
            with_scheduler=True,
        )
        for generation, character in ((1, "7"), (2, "8"), (3, "9"))
    )
    effects = tuple(_committed_projection_effects(item) for item in snapshots)
    driver.bind_scheduler_projection_refresher(
        lambda: {"initialized": True, "invalid_tasks": []},
    )
    for snapshot, generation_effects in zip(snapshots, effects, strict=True):
        driver.activate_committed(
            snapshot=snapshot,
            effects=generation_effects,
        )

    with pytest.raises(PluginConflictError) as raised:
        driver.rollback_committed_activation(
            snapshot=snapshots[1],
            effects=effects[1],
            restored_snapshot=snapshots[0],
            restored_effects=effects[0],
        )

    assert raised.value.code == "RUNTIME_PROJECTION_ROLLBACK_FAILED"
    assert driver.service_registry.require_operation(
        "plugin.concurrent_reverse_plugin.runner@1",
        "run",
    ).project_generation == 3
    assert contributions.active_generation(snapshots[0].automation_id) == 3


def test_reverse_refresh_failure_retains_candidate_until_fail_closed() -> None:
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=contributions,
    )
    first = _snapshot(
        automation_id="reverse-refresh-project",
        plugin_id="reverse_refresh_plugin",
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
        generation=1,
        with_scheduler=True,
    )
    second = _snapshot(
        automation_id="reverse-refresh-project",
        plugin_id="reverse_refresh_plugin",
        package_sha256="c" * 64,
        manifest_sha256="d" * 64,
        generation=2,
        version="2.0.0",
        with_scheduler=True,
    )
    first_effects = _committed_projection_effects(first)
    second_effects = _committed_projection_effects(second)
    fail_refresh = {"value": False}

    def refresh_scheduler() -> dict[str, object]:
        if fail_refresh["value"]:
            raise RuntimeError("synthetic reverse refresh failure")
        return {"initialized": True, "invalid_tasks": []}

    driver.bind_scheduler_projection_refresher(refresh_scheduler)
    driver.activate_committed(snapshot=first, effects=first_effects)
    driver.activate_committed(snapshot=second, effects=second_effects)
    fail_refresh["value"] = True

    with pytest.raises(PluginConflictError) as raised:
        driver.rollback_committed_activation(
            snapshot=second,
            effects=second_effects,
            restored_snapshot=first,
            restored_effects=first_effects,
        )

    assert raised.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    assert driver.service_registry.require_operation(
        "plugin.reverse_refresh_plugin.runner@1",
        "run",
    ).project_generation == 2
    assert contributions.active_generation(second.automation_id) == 2

    evidence = driver.fail_closed_project_projection(
        automation_id=second.automation_id,
        generation=second.generation,
    )
    assert evidence["tombstoned"] is True
    assert contributions.active_generation(second.automation_id) is None
    with pytest.raises(ServiceUnavailable):
        driver.service_registry.require_operation(
            "plugin.reverse_refresh_plugin.runner@1",
            "run",
        )


def test_committed_projection_withdraw_failure_keeps_old_generation_active() -> None:
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=contributions,
    )
    snapshot = _snapshot(
        automation_id="withdraw-project",
        plugin_id="withdraw_plugin",
        package_sha256="3" * 64,
        manifest_sha256="4" * 64,
        with_scheduler=True,
    )
    effects = _committed_projection_effects(snapshot)
    refresh_state = {"fail": False}

    def refresh_scheduler() -> dict[str, object]:
        if refresh_state["fail"]:
            raise RuntimeError("synthetic Scheduler refresh failure")
        return {"initialized": True, "invalid_tasks": []}

    driver.bind_scheduler_projection_refresher(refresh_scheduler)
    driver.activate_committed(snapshot=snapshot, effects=effects)
    before = contributions.active_snapshot(automation_id=snapshot.automation_id)

    refresh_state["fail"] = True
    with pytest.raises(PluginConflictError) as raised:
        driver.deactivate_committed(snapshot=snapshot, effects=effects)

    assert raised.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    assert contributions.active_generation(snapshot.automation_id) == 1
    assert contributions.active_snapshot(automation_id=snapshot.automation_id) == before

    refresh_state["fail"] = False
    assert driver.refresh_contribution_projection() == {
        "initialized": True,
        "invalid_tasks": [],
    }
    assert contributions.active_generation(snapshot.automation_id) is None
    assert contributions.active_snapshot(automation_id=snapshot.automation_id) == ()


def test_unrecoverable_activation_blocks_every_process_route_and_scheduler_job() -> None:
    contributions = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=contributions,
    )
    snapshot = _snapshot(
        automation_id="blocked-project",
        plugin_id="blocked_plugin",
        package_sha256="5" * 64,
        manifest_sha256="6" * 64,
        with_scheduler=True,
    )
    effects = _committed_projection_effects(snapshot)
    withdrawn: list[str] = []
    driver.bind_scheduler_projection_refresher(
        lambda: {"initialized": True, "invalid_tasks": []},
    )
    driver.bind_scheduler_project_emergency_withdrawer(
        lambda automation_id: {
            "initialized": True,
            "automation_id": withdrawn.append(automation_id) or automation_id,
            "tombstoned": True,
            "complete": True,
            "removed_job_ids": ["dynamic-job"],
            "remaining_target_job_ids": [],
            "remaining_malformed_marker_job_ids": [],
        }
    )
    driver.activate_committed(snapshot=snapshot, effects=effects)

    evidence = driver.fail_closed_project_projection(
        automation_id=snapshot.automation_id,
        generation=snapshot.generation,
    )

    assert evidence["removed_job_ids"] == ["dynamic-job"]
    assert withdrawn == [snapshot.automation_id]
    assert contributions.active_generation(snapshot.automation_id) is None
    assert contributions.active_snapshot(automation_id=snapshot.automation_id) == ()
    reference = driver.service_registry.project_reference(
        automation_id=snapshot.automation_id,
        generation=snapshot.generation,
    )
    assert reference is not None
    assert reference.accepts_new_calls is False
    with pytest.raises(ServiceUnavailable) as raised:
        driver.service_registry.require_operation(
            "plugin.blocked_plugin.runner@1",
            "run",
        )
    assert raised.value.code == "SERVICE_PROVIDER_BLOCKED"
    assert driver.projection_signature()[2] == (
        (snapshot.automation_id, snapshot.generation, "blocked"),
    )


def test_scheduler_tombstone_is_applied_when_emergency_callback_binds_late() -> None:
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())

    deferred = driver.fail_closed_project_projection(
        automation_id="startup-blocked-project",
        generation=3,
    )

    assert deferred["deferred_until_scheduler_start"] is True
    tombstoned: list[str] = []
    driver.bind_scheduler_project_emergency_withdrawer(
        lambda automation_id: {
            "automation_id": tombstoned.append(automation_id) or automation_id,
            "initialized": True,
            "tombstoned": True,
            "complete": True,
            "removed_job_ids": ["startup-job"],
            "remaining_target_job_ids": [],
            "remaining_malformed_marker_job_ids": [],
        }
    )

    assert tombstoned == ["startup-blocked-project"]


def test_contribution_projection_refreshes_are_process_serialized() -> None:
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())
    first_entered = Event()
    release_first = Event()
    second_attempted = Event()
    counter_lock = Lock()
    errors: list[BaseException] = []
    calls = 0
    active = 0
    maximum_active = 0

    def refresh_scheduler() -> dict[str, object]:
        nonlocal calls, active, maximum_active
        with counter_lock:
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
            first = calls == 1
        try:
            if first:
                first_entered.set()
                assert release_first.wait(timeout=2)
            return {"initialized": True, "invalid_tasks": []}
        finally:
            with counter_lock:
                active -= 1

    def invoke(*, attempted: Event | None = None) -> None:
        if attempted is not None:
            attempted.set()
        try:
            driver.refresh_contribution_projection()
        except BaseException as exc:  # noqa: BLE001 - surfaced in the main test thread
            errors.append(exc)

    driver.bind_scheduler_projection_refresher(refresh_scheduler)
    first_thread = Thread(target=invoke)
    second_thread = Thread(target=invoke, kwargs={"attempted": second_attempted})
    first_thread.start()
    assert first_entered.wait(timeout=2)
    second_thread.start()
    assert second_attempted.wait(timeout=2)
    try:
        with counter_lock:
            assert calls == 1
            assert maximum_active == 1
    finally:
        release_first.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert calls == 2
    assert maximum_active == 1


def test_catalog_does_not_report_a_disabled_provider_as_a_ready_dependency() -> None:
    provider = _catalog_entry(
        automation_id="base-project",
        plugin_id="base_plugin",
        enabled=False,
    )
    consumer = _catalog_entry(
        automation_id="consumer-project",
        plugin_id="consumer_plugin",
        requires=("plugin.base_plugin.runner@1",),
    )
    catalog = PluginCatalog(_EmptyCatalogRepository())

    statuses = catalog._v2_dependency_statuses((provider, consumer))

    assert statuses[consumer.automation_id] == (
        "BLOCKED_DEPENDENCY",
        [
            {
                "code": "PROVIDER_BLOCKED",
                "service": "plugin.base_plugin.runner@1",
                "message": "依赖服务自身尚未就绪",
            }
        ],
    )


def test_catalog_blocks_login_when_the_bound_account_is_not_runtime_ready() -> None:
    entry = _catalog_entry(
        automation_id="account-project",
        plugin_id="account_plugin",
        account_id="opaque-account-reference",
    )
    checked: list[tuple[str, tuple[str, ...]]] = []

    def account_ready(account_id: str, systems: Sequence[str]) -> bool:
        checked.append((account_id, tuple(systems)))
        return False

    catalog = PluginCatalog(
        _EmptyCatalogRepository(),
        account_binding_ready=account_ready,
    )
    dependency = catalog._v2_dependency_statuses((entry,))[entry.automation_id]

    assert catalog._readiness(entry, dependency) == (
        "BLOCKED_LOGIN",
        [
            {
                "code": "ACCOUNT_BINDING_MISSING",
                "service": "",
                "message": "必需的后台登录账号尚未绑定或登录已失效",
            }
        ],
    )
    assert checked == [
        ("opaque-account-reference", ("ronghui",)),
        ("opaque-account-reference", ("ronghui",)),
    ]


def test_catalog_does_not_report_unsupported_resident_runtime_as_ready() -> None:
    entry = _catalog_entry(
        automation_id="resident-project",
        plugin_id="resident_plugin",
        runtime_mode="resident",
    )
    catalog = PluginCatalog(_EmptyCatalogRepository())

    assert catalog._readiness(entry, ("READY", [])) == (
        "BLOCKED_DEPENDENCY",
        [
            {
                "code": "RESIDENT_RUNTIME_UNAVAILABLE",
                "service": "",
                "message": "当前主机尚未提供常驻进程运行器",
            }
        ],
    )


def test_applied_service_effects_restore_once_after_process_restart() -> None:
    package_sha = "b" * 64
    manifest_sha = "c" * 64
    first = _snapshot(
        automation_id="restore-one",
        plugin_id="restore_plugin",
        package_sha256=package_sha,
        manifest_sha256=manifest_sha,
    )
    second = _snapshot(
        automation_id="restore-two",
        plugin_id="restore_plugin",
        package_sha256=package_sha,
        manifest_sha256=manifest_sha,
    )
    records = {
        first.automation_id: RuntimeGenerationRecord(
            snapshot=first,
            state=RuntimeGenerationState.COMMITTED,
            effects=(_effect(first, state=RuntimeEffectState.APPLIED),),
        ),
        second.automation_id: RuntimeGenerationRecord(
            snapshot=second,
            state=RuntimeGenerationState.COMMITTED,
            effects=(_effect(second, state=RuntimeEffectState.APPLIED),),
        ),
    }

    class _Repository:
        @staticmethod
        def list_project_runtimes():
            return tuple(
                SimpleNamespace(automation_id=automation_id)
                for automation_id in sorted(records)
            )

        @staticmethod
        def list_project_generations(automation_id: str):
            return (records[automation_id],)

    registry = ServiceRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        service_registry=registry,
    )

    driver.restore_from_repository(_Repository())
    driver.restore_from_repository(_Repository())

    assert len(registry.snapshot()) == 1
    assert driver.service_reference_count(package_sha) == 2
    assert registry.claimed_provider_for("plugin.restore_plugin.runner@1").active is True
    with pytest.raises(ServiceProviderAmbiguous):
        registry.require_provider("plugin.restore_plugin.runner@1")


@pytest.mark.parametrize(
    "activation_phase",
    [RuntimeActivationPhase.PENDING_PROJECTION, RuntimeActivationPhase.BLOCKED],
)
def test_restart_stages_but_does_not_route_unacknowledged_generation(
    activation_phase: RuntimeActivationPhase,
) -> None:
    first = _snapshot(
        automation_id="restore-transition",
        plugin_id="restore_transition_plugin",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        generation=1,
    )
    second = _snapshot(
        automation_id="restore-transition",
        plugin_id="restore_transition_plugin",
        package_sha256="3" * 64,
        manifest_sha256="4" * 64,
        generation=2,
        version="2.0.0",
    )
    records = (
        RuntimeGenerationRecord(
            snapshot=first,
            state=RuntimeGenerationState.DRAINING,
            effects=(_effect(first, state=RuntimeEffectState.APPLIED),),
            activation_transition_token="00000000-0000-4000-8000-000000000001",
            activation_phase=RuntimeActivationPhase.ACTIVE,
        ),
        RuntimeGenerationRecord(
            snapshot=second,
            state=RuntimeGenerationState.COMMITTED,
            effects=(_effect(second, state=RuntimeEffectState.APPLIED),),
            base_committed_generation=1,
            activation_transition_token="00000000-0000-4000-8000-000000000002",
            activation_phase=activation_phase,
        ),
    )

    class _Repository:
        @staticmethod
        def list_project_runtimes():
            return (SimpleNamespace(automation_id=second.automation_id),)

        @staticmethod
        def list_project_generations(_automation_id: str):
            return records

    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())

    driver.restore_from_repository(_Repository())

    assert driver.service_reference_count(first.package_sha256) == 1
    assert driver.service_reference_count(second.package_sha256) == 1
    assert driver.service_registry.project_reference(
        automation_id=first.automation_id,
        generation=1,
    ).accepts_new_calls is False
    assert driver.service_registry.project_reference(
        automation_id=second.automation_id,
        generation=2,
    ).accepts_new_calls is False
    with pytest.raises(ServiceUnavailable) as raised:
        driver.service_registry.require_operation(
            "plugin.restore_transition_plugin.runner@1",
            "run",
        )
    assert raised.value.code == "SERVICE_PROVIDER_BLOCKED"


def test_service_operation_effect_is_exact_and_cannot_drift_on_replay() -> None:
    registry = ServiceRegistry()
    package_sha = "f" * 64
    common = {
        "automation_id": package_provider_registration_id(package_sha),
        "generation": 1,
        "plugin_id": "effect_plugin",
        "plugin_version": "1.0.0",
        "package_sha256": package_sha,
        "manifest_sha256": "a" * 64,
        "runtime_mode": "on_demand",
        "requires": (),
    }
    provides = (
        {
            "service": "plugin.effect_plugin.runner@1",
            "operations": [
                {"name": "inspect", "effect": "read"},
                {"name": "dispatch", "effect": "external_write"},
            ],
        },
    )

    provider = registry.register_contract(**common, provides=provides)
    registry.bind_project_reference(
        provider_automation_id=provider.automation_id,
        automation_id="effect-project",
        generation=1,
        package_sha256=package_sha,
        manifest_sha256="a" * 64,
    )
    registry.activate_project_reference(
        automation_id="effect-project",
        generation=1,
    )
    assert (
        registry.require_operation(
            "plugin.effect_plugin.runner@1", "inspect"
        ).effect.value
        == "read"
    )
    assert (
        registry.require_operation(
            "plugin.effect_plugin.runner@1", "dispatch"
        ).effect.value
        == "external_write"
    )

    with pytest.raises(ServiceProviderConflict):
        registry.register_contract(
            **common,
            provides=(
                {
                    "service": "plugin.effect_plugin.runner@1",
                    "operations": [
                        {"name": "inspect", "effect": "external_write"},
                        {"name": "dispatch", "effect": "external_write"},
                    ],
                },
            ),
        )
