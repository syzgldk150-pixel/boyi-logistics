from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.host_capability_registry import governance_for_effect
from agent.automation_plugins.models import (
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeCoeffectKind,
    RuntimeEffectRecord,
    RuntimeEffectState,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
)
from agent.automation_plugins.production import (
    ManagedContributionRegistry,
    ProductionRuntimeCoeffectProvider,
    ProductionRuntimeEffectDriver,
    ProductionRuntimeEffectPlanner,
    build_runtime_generation_snapshot,
)
from shared.automation_plugin_generation_repository import (
    _scheduler_contribution_binding,
)


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _contributions() -> dict[str, list[dict[str, object]]]:
    service = "plugin.example_plugin.runner@1"
    return {
        "console": [
            {
                "id": "run_now",
                "title": "Run now",
                "service": service,
                "operation": "run",
                "default_enabled": True,
            }
        ],
        "scheduler": [
            {
                "id": "daily_run",
                "title": "Daily run",
                "service": service,
                "operation": "run",
                "default_enabled": True,
                "schedule": {
                    "kind": "cron",
                    "expression": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                },
            }
        ],
        "webhook": [
            {
                "id": "receive_hook",
                "service": service,
                "operation": "run",
                "method": "POST",
                "route": "receive",
                "default_enabled": True,
            }
        ],
        "feishu": [
            {
                "id": "run_command",
                "service": service,
                "operation": "run",
                "commands": ["执行示例"],
                "default_enabled": True,
            }
        ],
        "events": [
            {
                "id": "orders_changed",
                "service": service,
                "operation": "run",
                "event": "orders.changed",
                "durable": True,
                "default_enabled": True,
            }
        ],
    }


def _snapshot(
    *,
    generation: int = 1,
    schedule: dict[str, object] | None = None,
    contributions: dict[str, list[dict[str, object]]] | None = None,
    enabled_entrypoints: tuple[str, ...] | None = None,
) -> RuntimeGenerationSnapshot:
    project_schedule = schedule or {
        "kind": "daily_times",
        "times": ["09:00"],
        "enabled": True,
    }
    declared = copy.deepcopy(contributions or _contributions())
    enabled = (
        enabled_entrypoints
        if enabled_entrypoints is not None
        else tuple(
            str(item["id"])
            for kind in ("console", "scheduler", "webhook", "feishu", "events")
            for item in declared[kind]
        )
    )
    service = "plugin.example_plugin.runner@1"
    contribution_kinds = {
        str(item["id"]): kind
        for kind in ("console", "scheduler", "webhook", "feishu", "events")
        for item in declared[kind]
    }
    metadata = {
        "project_config_version": 1,
        "project_config": {},
        "account_bindings": {},
        "resource_bindings": {},
        "device_binding": None,
        "schedule": project_schedule,
        "compiled_invocations": {
            contribution_id: {
                "arguments": {},
                "dynamic_resolvers": {},
                "target": {
                    "service": service,
                    "operation": "run",
                    "contribution_id": contribution_id,
                    "contribution_kind": contribution_kinds[contribution_id],
                },
                "governance": governance_for_effect("read").to_mapping(),
            }
            for contribution_id in enabled
        },
        "runtime_descriptor": {
            "install_metadata": {},
            "runtime": {"mode": "on_demand"},
            "runtime_permissions": {"broker_operations": []},
            "account_roles": [],
            "resource_roles": [],
        },
        "action_contract": {},
        "governance_anchor": {},
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "plugin_api": "2.0.0",
        "service_contracts": {
            "provides": [
                {
                    "service": service,
                    "operations": [{"name": "run", "effect": "read"}],
                }
            ],
            "requires": [],
        },
        "contributions": declared,
        "storage_contract": {"kv": False, "collections": []},
    }
    return RuntimeGenerationSnapshot(
        automation_id="example-project",
        generation=generation,
        plugin_id="example_plugin",
        plugin_version="1.0.0",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD,
        project_config_sha256=_sha({}),
        account_bindings_sha256=_sha({}),
        resource_bindings_sha256=_sha({}),
        device_binding_sha256=_sha(None),
        schedule_sha256=_sha(project_schedule),
        core_registry_sha256="3" * 64,
        tool_contract_sha256=_sha({}),
        invocation_contracts_sha256="4" * 64,
        compiled_invocations_sha256=_sha(metadata["compiled_invocations"]),
        runtime_descriptor_sha256=_sha(metadata["runtime_descriptor"]),
        governance_anchor_sha256=_sha({}),
        policy_contract_sha256="5" * 64,
        enabled_entrypoints=enabled,
        execution_metadata=metadata,
        created_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )


def _managed_plans(snapshot: RuntimeGenerationSnapshot):
    return tuple(
        plan
        for plan in ProductionRuntimeEffectPlanner().plan(snapshot)
        if plan.payload.get("contract_version") == 1
    )


def _active_materials(
    snapshot: RuntimeGenerationSnapshot,
) -> tuple[dict[str, object], ...]:
    return tuple(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(snapshot)
        if plan.payload.get("contribution_kind") in {"console", "scheduler"}
    )


def _refresh_success() -> dict[str, object]:
    return {"initialized": True, "invalid_tasks": []}


def _effect(
    snapshot: RuntimeGenerationSnapshot,
    plan,
    sequence: int,
    *,
    state: RuntimeEffectState = RuntimeEffectState.PLANNED,
) -> RuntimeEffectRecord:
    return RuntimeEffectRecord(
        effect_id=f"effect-{snapshot.generation}-{sequence}",
        automation_id=snapshot.automation_id,
        generation=snapshot.generation,
        sequence=sequence,
        kind=plan.kind,
        state=state,
        reversible=True,
        effect_key=plan.effect_key,
        payload=plan.payload,
    )


def test_v2_contributions_are_registered_activated_and_reversibly_removed() -> None:
    snapshot = _snapshot(enabled_entrypoints=("run_now", "daily_run"))
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )
    applied = []
    plans = _managed_plans(snapshot)

    for sequence, plan in enumerate(plans, start=1):
        applied.append(
            driver.ensure_applied(
                snapshot=snapshot,
                plan=plan,
                effect=_effect(snapshot, plan, sequence),
            )
        )

    prepared = registry.snapshot()
    assert len(prepared) == 2
    assert all(item.phase == "PREPARED" for item in prepared)
    assert not any(item.dispatch_available for item in prepared)
    console = next(item for item in prepared if item.contribution_kind == "console")
    assert console.route_keys == ("console:example-project:run_now",)
    assert console.backend == "managed_console_router"
    assert console.backend_status == "READY"
    scheduler = next(
        item for item in prepared if item.contribution_kind == "scheduler"
    )
    assert scheduler.declaration["schedule"] == {
        "kind": "cron",
        "expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
    }
    assert scheduler.backend == "scheduled_tasks"
    assert scheduler.backend_status == "READY"

    driver.activate_committed(snapshot=snapshot, effects=applied)
    committed = registry.snapshot()
    assert all(item.phase == "COMMITTED" for item in committed)
    assert next(
        item for item in committed if item.contribution_kind == "scheduler"
    ).dispatch_available is True
    assert next(
        item for item in committed if item.contribution_kind == "console"
    ).dispatch_available is True

    for effect in reversed(applied):
        driver.dispose(effect)
    assert registry.snapshot() == ()


def test_registry_applies_console_and_scheduler_as_one_atomic_generation() -> None:
    snapshot = _snapshot(enabled_entrypoints=("run_now", "daily_run"))
    registry = ManagedContributionRegistry()
    registry.prepare_generation(_active_materials(snapshot))

    assert registry.active_generation(snapshot.automation_id) is None
    assert registry.active_snapshot() == ()
    assert {item.phase for item in registry.snapshot()} == {"PREPARED"}
    refresh_observations: list[tuple[int | None, tuple[dict[str, object], ...]]] = []

    def _refresh() -> dict[str, object]:
        refresh_observations.append(
            (
                registry.active_generation(snapshot.automation_id),
                registry.active_snapshot(automation_id=snapshot.automation_id),
            )
        )
        return _refresh_success()

    registry.apply_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=_refresh,
    )

    assert refresh_observations == [(None, ())]
    assert registry.active_generation(snapshot.automation_id) == snapshot.generation
    assert {
        item["contribution_kind"] for item in registry.active_snapshot()
    } == {"console", "scheduler"}
    assert set(registry.active_snapshot()[0]) == {
        "automation_id",
        "generation",
        "contribution_id",
        "contribution_kind",
        "service",
        "operation",
        "backend",
        "backend_status",
    }
    assert registry.resolve_active(
        snapshot.automation_id,
        snapshot.generation,
        "console",
        "run_now",
    ).route_keys == ("console:example-project:run_now",)
    assert registry.resolve_active(
        snapshot.automation_id,
        snapshot.generation,
        "scheduler",
        "daily_run",
    ).route_keys == ("scheduler:example-project:daily_run",)


def test_registry_upgrade_switch_and_old_generation_withdraw_are_atomic() -> None:
    generation_one = _snapshot(
        generation=1,
        enabled_entrypoints=("run_now", "daily_run"),
    )
    generation_two = _snapshot(
        generation=2,
        enabled_entrypoints=("run_now", "daily_run"),
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation(_active_materials(generation_one))
    registry.apply_generation(
        generation_one.automation_id,
        generation_one.generation,
        refresh=_refresh_success,
    )
    registry.prepare_generation(_active_materials(generation_two))

    before_switch = registry.active_snapshot()
    assert {item["generation"] for item in before_switch} == {1}
    registry.apply_generation(
        generation_two.automation_id,
        generation_two.generation,
        refresh=_refresh_success,
    )
    after_switch = registry.snapshot()
    assert registry.active_generation(generation_two.automation_id) == 2
    assert {
        (item.generation, item.phase)
        for item in after_switch
    } == {(1, "DRAINING"), (2, "COMMITTED")}

    stable = registry.snapshot()
    registry.apply_generation(
        generation_two.automation_id,
        generation_two.generation,
        refresh=_refresh_success,
    )
    assert registry.snapshot() == stable

    registry.withdraw_generation(
        generation_one.automation_id,
        generation_one.generation,
        refresh=_refresh_success,
    )
    assert registry.active_generation(generation_two.automation_id) == 2
    assert {item.generation for item in registry.snapshot()} == {2}
    registry.withdraw_generation(
        generation_one.automation_id,
        generation_one.generation,
        refresh=lambda: pytest.fail("idempotent cleanup must not refresh"),
    )


@pytest.mark.parametrize(
    "refresh",
    (
        lambda: {},
        lambda: {"initialized": True},
        lambda: {"initialized": False, "invalid_tasks": []},
        lambda: {"initialized": True, "invalid_tasks": [{"task_id": "bad"}]},
    ),
)
def test_registry_failed_upgrade_refresh_preserves_the_complete_projection(
    refresh,
) -> None:
    generation_one = _snapshot(
        generation=1,
        enabled_entrypoints=("run_now", "daily_run"),
    )
    generation_two = _snapshot(
        generation=2,
        enabled_entrypoints=("run_now", "daily_run"),
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation(_active_materials(generation_one))
    registry.apply_generation(
        generation_one.automation_id,
        generation_one.generation,
        refresh=_refresh_success,
    )
    registry.prepare_generation(_active_materials(generation_two))
    before_registrations = registry.snapshot()
    before_routes = {
        key: frozenset(value) for key, value in registry._route_owners.items()
    }
    before_active = registry.active_generation(generation_one.automation_id)

    with pytest.raises(PluginConflictError) as exc_info:
        registry.apply_generation(
            generation_two.automation_id,
            generation_two.generation,
            refresh=refresh,
        )

    assert exc_info.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    assert registry.snapshot() == before_registrations
    assert {
        key: frozenset(value) for key, value in registry._route_owners.items()
    } == before_routes
    assert registry.active_generation(generation_one.automation_id) == before_active
    assert {item["generation"] for item in registry.active_snapshot()} == {1}


def test_registry_refresh_exception_and_failed_withdraw_preserve_live_routes() -> None:
    snapshot = _snapshot(enabled_entrypoints=("run_now", "daily_run"))
    registry = ManagedContributionRegistry()
    registry.prepare_generation(_active_materials(snapshot))
    registry.apply_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=_refresh_success,
    )
    before = registry.snapshot()

    def _raise_refresh() -> None:
        raise RuntimeError("offline injected refresh failure")

    with pytest.raises(PluginConflictError) as exc_info:
        registry.withdraw_generation(
            snapshot.automation_id,
            snapshot.generation,
            refresh=_raise_refresh,
        )
    assert exc_info.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    assert registry.snapshot() == before
    assert registry.active_generation(snapshot.automation_id) == snapshot.generation

    registry.withdraw_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=_refresh_success,
    )
    assert registry.snapshot() == ()
    assert registry.active_generation(snapshot.automation_id) is None
    registry.withdraw_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=lambda: pytest.fail("idempotent withdraw must not refresh"),
    )


def test_registry_resolve_is_exact_and_disabled_scheduler_fails_closed() -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("run_now", "daily_run"),
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation(_active_materials(snapshot))
    registry.apply_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=_refresh_success,
    )

    assert registry.resolve_active(
        snapshot.automation_id,
        snapshot.generation,
        "console",
        "run_now",
    ).backend == "managed_console_router"
    with pytest.raises(PluginConflictError) as stale:
        registry.resolve_active(
            snapshot.automation_id,
            snapshot.generation + 1,
            "console",
            "run_now",
        )
    assert stale.value.code == "RUNTIME_PROJECTION_STALE"
    with pytest.raises(PluginConflictError) as disabled:
        registry.resolve_active(
            snapshot.automation_id,
            snapshot.generation,
            "scheduler",
            "daily_run",
        )
    assert disabled.value.code == "CAPABILITY_UNAVAILABLE"
    with pytest.raises(PluginConflictError) as missing:
        registry.resolve_active(
            snapshot.automation_id,
            snapshot.generation,
            "console",
            "missing",
        )
    assert missing.value.code == "CAPABILITY_UNAVAILABLE"
    assert [
        item["contribution_kind"] for item in registry.active_snapshot()
    ] == ["console"]


def test_registry_rejects_an_unsupported_backend_without_partial_prepare() -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("receive_hook",),
    )
    unsupported = tuple(dict(plan.payload) for plan in _managed_plans(snapshot))
    registry = ManagedContributionRegistry()

    with pytest.raises(PluginConflictError) as exc_info:
        registry.prepare_generation(unsupported)

    assert exc_info.value.code == "CAPABILITY_UNAVAILABLE"
    assert registry.snapshot() == ()
    assert registry.active_generation(snapshot.automation_id) is None


@pytest.mark.parametrize(
    ("kind", "contribution_id"),
    (
        ("webhook", "receive_hook"),
        ("feishu", "run_command"),
        ("events", "orders_changed"),
    ),
)
def test_unavailable_managed_backend_blocks_before_effect_registration(
    kind: str,
    contribution_id: str,
) -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=(contribution_id,),
    )

    class _CoreCatalog:
        @staticmethod
        def get_capability(_name: str):
            raise AssertionError("service-v2 must not consult the v1 registry")

    class _Accounts:
        @staticmethod
        def list_accounts(*, include_status: bool, validate: bool):
            assert include_status is False
            assert validate is False
            return []

    coeffects = ProductionRuntimeCoeffectProvider(
        core_catalog=_CoreCatalog(),
        broker_handler_keys=(),
        account_manager=_Accounts(),
    ).observe(snapshot)
    blocked = next(
        item
        for item in coeffects
        if item.key == f"contribution:{kind}:{contribution_id}"
    )
    assert blocked.kind is RuntimeCoeffectKind.CORE_ADAPTER
    assert blocked.ready is False
    assert blocked.reason_code == "CAPABILITY_UNAVAILABLE"

    plan = next(
        item
        for item in _managed_plans(snapshot)
        if item.payload["contribution_kind"] == kind
    )
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())
    with pytest.raises(Exception) as exc_info:
        driver.ensure_applied(
            snapshot=snapshot,
            plan=plan,
            effect=_effect(snapshot, plan, 1),
        )
    assert getattr(exc_info.value, "code", None) == "CAPABILITY_UNAVAILABLE"
    assert driver.contribution_registry.snapshot() == ()


def test_manifest_webhook_mount_does_not_require_a_legacy_route_resource() -> None:
    service = "plugin.example_plugin.runner@1"
    contributions = {
        "console": [],
        "scheduler": [],
        "webhook": [
            {
                "id": "receive_hook",
                "service": service,
                "operation": "run",
                "method": "POST",
                "route": "receive",
                "default_enabled": True,
            }
        ],
        "feishu": [],
        "events": [],
    }
    compiled = {
        "receive_hook": {
            "arguments": {},
            "dynamic_resolvers": {},
            "target": {
                "service": service,
                "operation": "run",
                "contribution_id": "receive_hook",
                "contribution_kind": "webhook",
            },
            "governance": governance_for_effect("read").to_mapping(),
        }
    }
    schedule = {"kind": "none", "times": [], "enabled": False}
    entry = SimpleNamespace(
        automation_id="example-project",
        plugin_id="example_plugin",
        installed_version="1.0.0",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
        runtime_model=PluginRuntimeModel.SERVICE_V2.value,
        plugin_api="2.0.0",
        runtime={
            "kind": "python_subprocess",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
        },
        allowed_entrypoints=("receive_hook",),
        invocation_contracts={
            "receive_hook": {
                "service": service,
                "operation": "run",
                "contribution_kind": "webhook",
                "effect": "read",
                "governance": governance_for_effect("read").to_mapping(),
            }
        },
        governance_anchor={},
        governance_anchor_sha256=_sha({}),
        resource_roles=(),
        account_roles=(),
        tool_contract={},
        signed_runtime_permissions={"broker_operations": []},
        install_metadata={"python_relative": "venv/bin/python"},
        install_root="/plugins/example_plugin/1.0.0",
        service_contracts={
            "provides": [
                {
                    "service": service,
                    "operations": [{"name": "run", "effect": "read"}],
                }
            ],
            "requires": [],
        },
        contributions=contributions,
        storage_contract={"kv": False, "collections": []},
    )
    desired = {
        "automation_id": entry.automation_id,
        "config_json": {},
        "config_sha256": _sha({}),
        "account_bindings_json": {},
        "account_bindings_sha256": _sha({}),
        "resource_bindings_json": {},
        "resource_bindings_sha256": _sha({}),
        "enabled_entrypoints_json": ["receive_hook"],
        "enabled_entrypoints_sha256": _sha(["receive_hook"]),
        "desired_schedule_json": schedule,
        "desired_schedule_sha256": _sha(schedule),
        "compiled_invocations_json": compiled,
        "compiled_invocations_sha256": _sha(compiled),
        "device_binding_sha256": _sha(None),
        "device_id": None,
        "configured": True,
        "config_version": 1,
    }
    policy = {
        "automation_id": entry.automation_id,
        "project_generation": 1,
        "mode": "PROJECT_FULL_AUTO",
        "project_configuration_version": 1,
        "version": 1,
    }

    snapshot = build_runtime_generation_snapshot(
        entry,
        desired_config_row=desired,
        policy_row=policy,
        generation=1,
        core_catalog=SimpleNamespace(
            get_capability=lambda _name: pytest.fail(
                "service-v2 must not use the legacy core route registry"
            )
        ),
    )

    plan = next(
        item
        for item in _managed_plans(snapshot)
        if item.payload["contribution_kind"] == "webhook"
    )
    assert snapshot.execution_metadata["resource_bindings"] == {}
    assert plan.payload["backend_status"] == "CAPABILITY_UNAVAILABLE"
    assert plan.payload["reason_code"] == "CAPABILITY_UNAVAILABLE"
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())
    with pytest.raises(Exception) as exc_info:
        driver.ensure_applied(
            snapshot=snapshot,
            plan=plan,
            effect=_effect(snapshot, plan, 1),
        )
    assert getattr(exc_info.value, "code", None) == "CAPABILITY_UNAVAILABLE"
    assert driver.contribution_registry.snapshot() == ()


def test_disabled_project_schedule_keeps_manifest_default_as_audited_declaration() -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False}
    )
    schedule_plan = next(
        plan
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "scheduler"
    )

    assert schedule_plan.payload["declaration"]["schedule"]["expression"] == (
        "0 9 * * *"
    )
    assert schedule_plan.payload["declaration"]["schedule"]["timezone"] == (
        "Asia/Shanghai"
    )
    assert schedule_plan.payload["backend_status"] == "DISABLED"
    assert schedule_plan.payload["reason_detail"] == "PROJECT_SCHEDULE_DISABLED"


def test_applied_contribution_effects_restore_committed_state_after_restart() -> None:
    snapshot = _snapshot(enabled_entrypoints=("run_now", "daily_run"))
    effects = tuple(
        _effect(
            snapshot,
            plan,
            sequence,
            state=RuntimeEffectState.APPLIED,
        )
        for sequence, plan in enumerate(_managed_plans(snapshot), start=1)
    )
    generation = RuntimeGenerationRecord(
        snapshot=snapshot,
        state=RuntimeGenerationState.COMMITTED,
        effects=effects,
    )

    class _Repository:
        @staticmethod
        def list_project_runtime_ids():
            return (snapshot.automation_id,)

        @staticmethod
        def list_project_generations(automation_id: str):
            assert automation_id == snapshot.automation_id
            return (generation,)

        @staticmethod
        def list_project_runtimes():
            return (SimpleNamespace(automation_id=snapshot.automation_id),)

    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )

    driver.restore_from_repository(_Repository())
    driver.restore_from_repository(_Repository())

    restored = registry.snapshot()
    assert len(restored) == 2
    assert all(item.phase == "COMMITTED" for item in restored)
    assert sum(item.dispatch_available for item in restored) == 2


def test_unavailable_routes_fail_before_a_generation_can_commit() -> None:
    contributions = _contributions()
    contributions["webhook"].append(
        {
            **copy.deepcopy(contributions["webhook"][0]),
            "id": "receive_hook_duplicate",
        }
    )
    snapshot = _snapshot(contributions=contributions)
    plans = [
        plan
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "webhook"
    ]
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())

    with pytest.raises(Exception) as exc_info:
        driver.ensure_applied(
            snapshot=snapshot,
            plan=plans[0],
            effect=_effect(snapshot, plans[0], 1),
        )
    assert getattr(exc_info.value, "code", None) == "CAPABILITY_UNAVAILABLE"
    assert driver.contribution_registry.snapshot() == ()


def test_runtime_rejects_plugin_provided_frontend_contributions() -> None:
    contributions = _contributions()
    contributions["frontend"] = []
    snapshot = _snapshot(contributions=contributions)

    with pytest.raises(Exception) as exc_info:
        ProductionRuntimeEffectPlanner().plan(snapshot)
    assert getattr(exc_info.value, "code", None) == "PLUGIN_CUSTOM_FRONTEND_FORBIDDEN"


def test_generation_commit_resolves_the_declared_v2_scheduler_id() -> None:
    metadata = {"contributions": _contributions()}
    snapshot = {"runtime_model": "SERVICE_V2", "plugin_api": "2.0.0"}

    assert _scheduler_contribution_binding(
        snapshot=snapshot,
        execution_metadata=metadata,
        enabled_entrypoints=("daily_run",),
        schedule_expressions=("0 9 * * *",),
    ) == ("daily_run", True)
    assert _scheduler_contribution_binding(
        snapshot=snapshot,
        execution_metadata=metadata,
        enabled_entrypoints=("run_now",),
        schedule_expressions=("0 9 * * *",),
    ) == ("daily_run", False)


def test_generation_commit_rejects_an_unavailable_scheduler_timezone() -> None:
    contributions = _contributions()
    contributions["scheduler"][0]["schedule"]["timezone"] = "UTC"

    with pytest.raises(Exception, match="timezone is unavailable"):
        _scheduler_contribution_binding(
            snapshot={"runtime_model": "SERVICE_V2", "plugin_api": "2.0.0"},
            execution_metadata={"contributions": contributions},
            enabled_entrypoints=("daily_run",),
            schedule_expressions=("0 1 * * *",),
        )

    snapshot = _snapshot(contributions=contributions)
    plan = next(
        item
        for item in _managed_plans(snapshot)
        if item.payload["contribution_kind"] == "scheduler"
    )
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())
    with pytest.raises(Exception) as exc_info:
        driver.ensure_applied(
            snapshot=snapshot,
            plan=plan,
            effect=_effect(snapshot, plan, 1),
        )
    assert getattr(exc_info.value, "code", None) == "CAPABILITY_UNAVAILABLE"
    assert driver.contribution_registry.snapshot() == ()
