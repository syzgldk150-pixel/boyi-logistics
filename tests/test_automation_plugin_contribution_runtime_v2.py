from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.automation_plugins.manifest import canonical_json_bytes
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
    metadata = {
        "project_config_version": 1,
        "project_config": {},
        "account_bindings": {},
        "resource_bindings": {},
        "device_binding": None,
        "schedule": project_schedule,
        "compiled_invocations": {
            contribution_id: {"arguments": {}, "dynamic_resolvers": {}}
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
            "provides": [{"service": service, "operations": ["run"]}],
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
    assert len(prepared) == 1
    assert all(item.phase == "PREPARED" for item in prepared)
    assert not any(item.dispatch_available for item in prepared)
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

    for effect in reversed(applied):
        driver.dispose(effect)
    assert registry.snapshot() == ()


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
    compiled = {"receive_hook": {"arguments": {}, "dynamic_resolvers": {}}}
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
            "receive_hook": {"contribution_kind": "webhook"}
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
            "provides": [{"service": service, "operations": ["run"]}],
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
    assert len(restored) == 1
    assert all(item.phase == "COMMITTED" for item in restored)
    assert sum(item.dispatch_available for item in restored) == 1


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
