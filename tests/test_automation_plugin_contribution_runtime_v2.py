from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.generation import AutomationRuntimeReconciler
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.host_capability_registry import governance_for_effect
from agent.automation_plugins.models import (
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeActivationPhase,
    RuntimeCoeffectKind,
    RuntimeCoeffectSnapshot,
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
from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
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


def _non_durable_event_contributions() -> dict[str, list[dict[str, object]]]:
    contributions = _contributions()
    contributions["events"][0]["durable"] = False
    return contributions


def _event_material(snapshot: RuntimeGenerationSnapshot) -> dict[str, object]:
    return next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "events"
    )


def _event_snapshot(
    *,
    generation: int = 1,
    contributions: dict[str, list[dict[str, object]]] | None = None,
    enabled_entrypoints: tuple[str, ...] = ("orders_changed",),
) -> RuntimeGenerationSnapshot:
    return _snapshot(
        generation=generation,
        schedule={"kind": "none", "times": [], "enabled": False},
        contributions=contributions,
        enabled_entrypoints=enabled_entrypoints,
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


class _GenerationRepository:
    def __init__(self, *records: RuntimeGenerationRecord) -> None:
        self._records: dict[str, list[RuntimeGenerationRecord]] = {}
        for record in records:
            self._records.setdefault(record.snapshot.automation_id, []).append(record)

    def list_project_runtime_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))

    def list_project_generations(
        self,
        automation_id: str,
    ) -> tuple[RuntimeGenerationRecord, ...]:
        return tuple(
            sorted(
                self._records[automation_id],
                key=lambda record: record.snapshot.generation,
            )
        )

    def list_project_runtimes(self) -> tuple[SimpleNamespace, ...]:
        return tuple(
            SimpleNamespace(automation_id=automation_id)
            for automation_id in sorted(self._records)
        )


class _PrepareRepository:
    def __init__(self, snapshot: RuntimeGenerationSnapshot) -> None:
        self.generation = RuntimeGenerationRecord(
            snapshot=snapshot,
            state=RuntimeGenerationState.TARGET,
        )

    def get_generation(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord | None:
        snapshot = self.generation.snapshot
        if (automation_id, generation) == (
            snapshot.automation_id,
            snapshot.generation,
        ):
            return self.generation
        return None

    def list_project_runtime_ids(self) -> tuple[str, ...]:
        return (self.generation.snapshot.automation_id,)

    def list_project_generations(
        self,
        _automation_id: str,
    ) -> tuple[RuntimeGenerationRecord, ...]:
        return (self.generation,)

    def list_project_runtimes(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(automation_id=self.generation.snapshot.automation_id),
        )

    def mark_generation_preparing(self, _automation_id: str, _generation: int) -> None:
        self.generation = replace(
            self.generation,
            state=RuntimeGenerationState.PREPARING,
        )

    def replace_generation_coeffects(
        self,
        _automation_id: str,
        _generation: int,
        coeffects,
    ) -> None:
        self.generation = replace(self.generation, coeffects=tuple(coeffects))

    def reserve_generation_effect(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        plan,
        sequence: int,
    ) -> RuntimeEffectRecord:
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
        self.generation = replace(
            self.generation,
            effects=(*self.generation.effects, effect),
        )
        return effect

    def mark_generation_effect_applied(
        self,
        applied: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        self.generation = replace(
            self.generation,
            effects=tuple(
                applied if effect.effect_id == applied.effect_id else effect
                for effect in self.generation.effects
            ),
        )
        return applied

    def mark_generation_prepared(self, _automation_id: str, _generation: int) -> None:
        self.generation = replace(
            self.generation,
            state=RuntimeGenerationState.PREPARED,
        )

    def fail_generation(
        self,
        _automation_id: str,
        _generation: int,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        assert error_code
        assert error_summary
        self.generation = replace(
            self.generation,
            state=RuntimeGenerationState.FAILED,
        )

    @staticmethod
    def get_project_runtime(_automation_id: str):
        return None


class _ReadyCoeffects:
    @staticmethod
    def observe(
        _snapshot: RuntimeGenerationSnapshot,
    ) -> tuple[RuntimeCoeffectSnapshot, ...]:
        return (
            RuntimeCoeffectSnapshot(
                kind=RuntimeCoeffectKind.CORE_ADAPTER,
                key="offline-ready",
                revision="offline-ready-1",
                ready=True,
            ),
        )


class _ManagedContributionPlanner:
    @staticmethod
    def plan(snapshot: RuntimeGenerationSnapshot):
        return _managed_plans(snapshot)


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


def test_feishu_command_is_ready_global_exact_and_resolves_only_active_generation() -> None:
    disabled_schedule = {"kind": "none", "times": [], "enabled": False}
    first = _snapshot(
        generation=1,
        schedule=disabled_schedule,
        enabled_entrypoints=("run_command",),
    )
    second = _snapshot(
        generation=2,
        schedule=disabled_schedule,
        enabled_entrypoints=("run_command",),
    )
    first_materials = tuple(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(first)
        if plan.payload["contribution_kind"] == "feishu"
    )
    second_materials = tuple(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(second)
        if plan.payload["contribution_kind"] == "feishu"
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation(first_materials)
    assert first_materials[0]["route_keys"] == [
        "feishu:command:"
        + hashlib.sha256("执行示例".encode("utf-8")).hexdigest()
    ]
    registry.apply_generation(
        first.automation_id,
        first.generation,
        refresh=_refresh_success,
        expected_registration_ids=(first_materials[0]["registration_id"],),
    )
    first_target = registry.resolve_active_feishu_command("执行示例")
    assert (
        first_target.automation_id,
        first_target.generation,
        first_target.contribution_id,
    ) == (first.automation_id, 1, "run_command")

    registry.prepare_generation(second_materials)
    registry.apply_generation(
        second.automation_id,
        second.generation,
        refresh=_refresh_success,
        expected_registration_ids=(second_materials[0]["registration_id"],),
    )
    assert registry.resolve_active_feishu_command("执行示例").generation == 2
    with pytest.raises(PluginConflictError) as wrong_case:
        registry.resolve_active_feishu_command("执行示例 ")
    assert wrong_case.value.code == "CAPABILITY_UNAVAILABLE"


def test_webhook_route_is_ready_global_exact_and_resolves_only_active_generation() -> None:
    disabled_schedule = {"kind": "none", "times": [], "enabled": False}
    first = _snapshot(
        generation=1,
        schedule=disabled_schedule,
        enabled_entrypoints=("receive_hook",),
    )
    second = _snapshot(
        generation=2,
        schedule=disabled_schedule,
        enabled_entrypoints=("receive_hook",),
    )
    first_material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(first)
        if plan.payload["contribution_kind"] == "webhook"
    )
    second_material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(second)
        if plan.payload["contribution_kind"] == "webhook"
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation((first_material,))

    assert first_material["route_keys"] == ["webhook:POST:receive"]
    assert first_material["backend"] == "managed_webhook_router"
    assert first_material["backend_status"] == "READY"
    assert registry.resolve_active_webhook_route(method="POST", route="receive") is None
    registry.apply_generation(
        first.automation_id,
        first.generation,
        refresh=_refresh_success,
        expected_registration_ids=(first_material["registration_id"],),
    )
    target = registry.resolve_active_webhook_route(method="POST", route="receive")
    assert target is not None
    assert (
        target.automation_id,
        target.generation,
        target.contribution_id,
    ) == (first.automation_id, 1, "receive_hook")
    assert registry.resolve_active_webhook_route(method="GET", route="receive") is None
    assert registry.resolve_active_webhook_route(method="POST", route="unknown") is None
    with pytest.raises(PluginConflictError) as invalid_route:
        registry.resolve_active_webhook_route(method="POST", route="receive ")
    assert invalid_route.value.code == "CONTRIBUTION_REGISTRATION_CONFLICT"

    registry.prepare_generation((second_material,))
    registry.apply_generation(
        second.automation_id,
        second.generation,
        refresh=_refresh_success,
        expected_registration_ids=(second_material["registration_id"],),
    )
    upgraded = registry.resolve_active_webhook_route(method="POST", route="receive")
    assert upgraded is not None
    assert upgraded.generation == 2


@pytest.mark.parametrize("existing_active", (False, True), ids=("prepared", "active"))
def test_webhook_route_conflict_is_global_and_leaves_no_partial_reservation(
    existing_active: bool,
) -> None:
    schedule = {"kind": "none", "times": [], "enabled": False}
    first = _snapshot(schedule=schedule, enabled_entrypoints=("receive_hook",))
    second = replace(first, automation_id="other-project")
    first_material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(first)
        if plan.payload["contribution_kind"] == "webhook"
    )
    second_material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(second)
        if plan.payload["contribution_kind"] == "webhook"
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation((first_material,))
    if existing_active:
        registry.apply_generation(
            first.automation_id,
            first.generation,
            refresh=_refresh_success,
            expected_registration_ids=(first_material["registration_id"],),
        )

    with pytest.raises(PluginConflictError) as conflict:
        registry.prepare_generation((second_material,))

    assert conflict.value.code == "CONTRIBUTION_ROUTE_CONFLICT"
    assert tuple(item.automation_id for item in registry.snapshot()) == (
        first.automation_id,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (("method", "POST "), ("method", "GET"), ("route", "receive ")),
)
def test_webhook_runtime_rejects_non_exact_declarations(
    field: str,
    value: str,
) -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("receive_hook",),
    )
    material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "webhook"
    )
    material["declaration"][field] = value
    registry = ManagedContributionRegistry()

    with pytest.raises(PluginConflictError) as invalid:
        registry.prepare_generation((material,))

    assert invalid.value.code == "CONTRIBUTION_REGISTRATION_CONFLICT"
    assert registry.snapshot() == ()


def test_committed_webhook_effect_restores_exact_active_route_after_restart() -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("receive_hook",),
    )
    plans = tuple(
        plan
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "webhook"
    )
    generation = RuntimeGenerationRecord(
        snapshot=snapshot,
        state=RuntimeGenerationState.COMMITTED,
        effects=tuple(
            _effect(
                snapshot,
                plan,
                sequence,
                state=RuntimeEffectState.APPLIED,
            )
            for sequence, plan in enumerate(plans, start=1)
        ),
    )
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )

    repository = _GenerationRepository(generation)
    driver.restore_from_repository(repository)
    driver.restore_from_repository(repository)

    target = registry.resolve_active_webhook_route(method="POST", route="receive")
    assert target is not None
    assert (
        target.automation_id,
        target.generation,
        target.contribution_id,
    ) == (snapshot.automation_id, snapshot.generation, "receive_hook")


def test_authoritative_empty_webhook_generation_revokes_and_releases_route() -> None:
    schedule = {"kind": "none", "times": [], "enabled": False}
    first = _snapshot(schedule=schedule, enabled_entrypoints=("receive_hook",))
    first_material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(first)
        if plan.payload["contribution_kind"] == "webhook"
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation((first_material,))
    registry.apply_generation(
        first.automation_id,
        first.generation,
        refresh=_refresh_success,
        expected_registration_ids=(first_material["registration_id"],),
    )

    registry.apply_generation(
        first.automation_id,
        first.generation + 1,
        refresh=_refresh_success,
        expected_registration_ids=(),
    )

    assert registry.resolve_active_webhook_route(method="POST", route="receive") is None
    assert {item.phase for item in registry.snapshot()} == {"DRAINING"}
    reclaimer = replace(first, automation_id="other-project")
    reclaim_material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(reclaimer)
        if plan.payload["contribution_kind"] == "webhook"
    )
    registry.prepare_generation((reclaim_material,))
    registry.apply_generation(
        reclaimer.automation_id,
        reclaimer.generation,
        refresh=_refresh_success,
        expected_registration_ids=(reclaim_material["registration_id"],),
    )
    target = registry.resolve_active_webhook_route(method="POST", route="receive")
    assert target is not None
    assert target.automation_id == reclaimer.automation_id


def test_nondurable_event_is_ready_global_exact_and_switches_adjacent_generation() -> None:
    contributions = _non_durable_event_contributions()
    first = _event_snapshot(
        generation=1,
        contributions=contributions,
        enabled_entrypoints=("orders_changed",),
    )
    second = _event_snapshot(
        generation=2,
        contributions=contributions,
        enabled_entrypoints=("orders_changed",),
    )
    first_material = _event_material(first)
    second_material = _event_material(second)
    registry = ManagedContributionRegistry()

    assert first_material["route_keys"] == ["event:orders.changed"]
    assert first_material["backend"] == "managed_event_dispatcher"
    assert first_material["backend_status"] == "READY"
    assert registry.resolve_active_event(event_name="orders.changed") is None
    assert registry.resolve_active_event(event_name="orders.unknown") is None
    with pytest.raises(PluginConflictError) as invalid:
        registry.resolve_active_event(event_name="Orders.changed")
    assert invalid.value.code == "CONTRIBUTION_REGISTRATION_CONFLICT"

    registry.prepare_generation((first_material,))
    registry.apply_generation(
        first.automation_id,
        first.generation,
        refresh=_refresh_success,
        expected_registration_ids=(first_material["registration_id"],),
    )
    target = registry.resolve_active_event(event_name="orders.changed")
    assert target is not None
    assert (
        target.automation_id,
        target.generation,
        target.contribution_id,
    ) == (first.automation_id, 1, "orders_changed")

    registry.prepare_generation((second_material,))
    registry.apply_generation(
        second.automation_id,
        second.generation,
        refresh=_refresh_success,
        expected_registration_ids=(second_material["registration_id"],),
    )
    upgraded = registry.resolve_active_event(event_name="orders.changed")
    assert upgraded is not None
    assert upgraded.generation == 2
    assert {
        (record.generation, record.phase) for record in registry.snapshot()
    } == {(1, "DRAINING"), (2, "COMMITTED")}


def test_webhook_and_event_effective_routes_require_bound_process_ingress() -> None:
    availability = RuntimeContributionBackendAvailability()
    schedule = {"kind": "none", "times": [], "enabled": False}
    webhook_snapshot = _snapshot(
        schedule=schedule,
        enabled_entrypoints=("receive_hook",),
    )
    webhook_material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(webhook_snapshot)
        if plan.payload["contribution_kind"] == "webhook"
    )
    webhook_registry = ManagedContributionRegistry(
        backend_availability=availability
    )
    webhook_registry.prepare_generation((webhook_material,))
    webhook_registry.apply_generation(
        webhook_snapshot.automation_id,
        webhook_snapshot.generation,
        refresh=_refresh_success,
        expected_registration_ids=(webhook_material["registration_id"],),
    )

    assert webhook_registry.active_snapshot(contribution_kind="webhook") == ()
    assert webhook_registry.resolve_active_webhook_route(
        method="POST", route="unknown"
    ) is None
    with pytest.raises(PluginConflictError) as webhook_unavailable:
        webhook_registry.resolve_active_webhook_route(
            method="POST", route="receive"
        )
    assert webhook_unavailable.value.code == "CAPABILITY_UNAVAILABLE"
    availability.mark_available("webhook")
    assert webhook_registry.resolve_active_webhook_route(
        method="POST", route="receive"
    ) is not None

    event_snapshot = _event_snapshot(
        contributions=_non_durable_event_contributions(),
        enabled_entrypoints=("orders_changed",),
    )
    event_material = _event_material(event_snapshot)
    event_registry = ManagedContributionRegistry(
        backend_availability=availability
    )
    event_registry.prepare_generation((event_material,))
    event_registry.apply_generation(
        event_snapshot.automation_id,
        event_snapshot.generation,
        refresh=_refresh_success,
        expected_registration_ids=(event_material["registration_id"],),
    )
    with pytest.raises(PluginConflictError) as event_unavailable:
        event_registry.resolve_active_event(event_name="orders.changed")
    assert event_unavailable.value.code == "CAPABILITY_UNAVAILABLE"
    availability.mark_available("events")
    assert event_registry.resolve_active_event(event_name="orders.changed") is not None


def test_durable_event_stays_unavailable_and_mixed_generation_is_atomic() -> None:
    snapshot = _event_snapshot(
        enabled_entrypoints=("run_now", "orders_changed")
    )
    materials = tuple(copy.deepcopy(dict(plan.payload)) for plan in _managed_plans(snapshot))
    durable_material = next(
        material
        for material in materials
        if material["contribution_kind"] == "events"
    )
    nondurable_snapshot = _event_snapshot(
        contributions=_non_durable_event_contributions(),
        enabled_entrypoints=("orders_changed",),
    )

    assert durable_material["route_keys"] == ["event:orders.changed"]
    assert _event_material(nondurable_snapshot)["route_keys"] == [
        "event:orders.changed"
    ]
    assert durable_material["backend"] == "managed_event_subscriptions"
    assert durable_material["backend_status"] == "CAPABILITY_UNAVAILABLE"
    assert durable_material["reason_code"] == "CAPABILITY_UNAVAILABLE"
    assert durable_material["reason_detail"] == "EVENTS_HOST_BACKEND_UNAVAILABLE"
    registry = ManagedContributionRegistry()

    with pytest.raises(PluginConflictError) as unavailable:
        registry.prepare_generation(materials)

    assert unavailable.value.code == "CAPABILITY_UNAVAILABLE"
    assert registry.snapshot() == ()


@pytest.mark.parametrize("existing_active", (False, True), ids=("prepared", "active"))
def test_event_route_conflict_is_global_and_leaves_no_partial_reservation(
    existing_active: bool,
) -> None:
    contributions = _non_durable_event_contributions()
    first = _event_snapshot(
        contributions=contributions,
        enabled_entrypoints=("orders_changed",),
    )
    second = replace(first, automation_id="other-project")
    first_material = _event_material(first)
    second_materials = tuple(
        copy.deepcopy(dict(plan.payload)) for plan in _managed_plans(second)
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation((first_material,))
    if existing_active:
        registry.apply_generation(
            first.automation_id,
            first.generation,
            refresh=_refresh_success,
            expected_registration_ids=(first_material["registration_id"],),
        )

    with pytest.raises(PluginConflictError) as conflict:
        registry.prepare_generation(second_materials)

    assert conflict.value.code == "CONTRIBUTION_ROUTE_CONFLICT"
    assert {record.automation_id for record in registry.snapshot()} == {
        first.automation_id
    }


def test_duplicate_event_name_in_one_generation_is_atomic() -> None:
    contributions = _non_durable_event_contributions()
    contributions["events"].append(
        {
            **copy.deepcopy(contributions["events"][0]),
            "id": "orders_changed_duplicate",
        }
    )
    snapshot = _event_snapshot(
        contributions=contributions,
        enabled_entrypoints=("orders_changed", "orders_changed_duplicate"),
    )
    materials = tuple(copy.deepcopy(dict(plan.payload)) for plan in _managed_plans(snapshot))
    registry = ManagedContributionRegistry()

    with pytest.raises(PluginConflictError) as conflict:
        registry.prepare_generation(materials)

    assert conflict.value.code == "CONTRIBUTION_ROUTE_CONFLICT"
    assert registry.snapshot() == ()


@pytest.mark.parametrize(
    ("mutation", "value"),
    (
        ("extra", "forbidden"),
        ("missing", None),
        ("event", "Orders.changed"),
        ("event", "orders.changed "),
        ("durable", 0),
        ("default_enabled", 1),
        ("id", 1),
    ),
)
def test_event_runtime_rejects_non_closed_or_non_exact_declarations(
    mutation: str,
    value: object,
) -> None:
    snapshot = _event_snapshot(
        contributions=_non_durable_event_contributions(),
        enabled_entrypoints=("orders_changed",),
    )
    material = _event_material(snapshot)
    declaration = material["declaration"]
    assert isinstance(declaration, dict)
    if mutation == "extra":
        declaration["unexpected"] = value
    elif mutation == "missing":
        declaration.pop("default_enabled")
    else:
        declaration[mutation] = value
    registry = ManagedContributionRegistry()

    with pytest.raises(PluginConflictError) as invalid:
        registry.prepare_generation((material,))

    assert invalid.value.code == "CONTRIBUTION_REGISTRATION_CONFLICT"
    assert registry.snapshot() == ()


def test_committed_event_effect_restore_is_a_fixed_point() -> None:
    snapshot = _event_snapshot(
        contributions=_non_durable_event_contributions(),
        enabled_entrypoints=("orders_changed",),
    )
    plans = tuple(
        plan
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "events"
    )
    generation = RuntimeGenerationRecord(
        snapshot=snapshot,
        state=RuntimeGenerationState.COMMITTED,
        effects=tuple(
            _effect(
                snapshot,
                plan,
                sequence,
                state=RuntimeEffectState.APPLIED,
            )
            for sequence, plan in enumerate(plans, start=1)
        ),
    )
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )
    repository = _GenerationRepository(generation)

    driver.restore_from_repository(repository)
    stable = registry.snapshot()
    driver.restore_from_repository(repository)

    assert registry.snapshot() == stable
    target = registry.resolve_active_event(event_name="orders.changed")
    assert target is not None
    assert (
        target.automation_id,
        target.generation,
        target.contribution_id,
    ) == (snapshot.automation_id, snapshot.generation, "orders_changed")


def test_empty_event_generation_releases_draining_route_for_reclaim() -> None:
    contributions = _non_durable_event_contributions()
    first = _event_snapshot(
        contributions=contributions,
        enabled_entrypoints=("orders_changed",),
    )
    first_material = _event_material(first)
    registry = ManagedContributionRegistry()
    registry.prepare_generation((first_material,))
    registry.apply_generation(
        first.automation_id,
        first.generation,
        refresh=_refresh_success,
        expected_registration_ids=(first_material["registration_id"],),
    )

    registry.apply_generation(
        first.automation_id,
        2,
        refresh=_refresh_success,
        expected_registration_ids=(),
    )

    assert registry.resolve_active_event(event_name="orders.changed") is None
    assert {record.phase for record in registry.snapshot()} == {"DRAINING"}
    reclaimer = replace(first, automation_id="other-project")
    reclaim_material = _event_material(reclaimer)
    registry.prepare_generation((reclaim_material,))
    registry.apply_generation(
        reclaimer.automation_id,
        reclaimer.generation,
        refresh=_refresh_success,
        expected_registration_ids=(reclaim_material["registration_id"],),
    )
    registry.withdraw_generation(
        first.automation_id,
        first.generation,
        refresh=_refresh_success,
    )

    target = registry.resolve_active_event(event_name="orders.changed")
    assert target is not None
    assert target.automation_id == reclaimer.automation_id
    assert {record.automation_id for record in registry.snapshot()} == {
        reclaimer.automation_id
    }


def test_event_resolver_fails_closed_for_corrupt_route_owner() -> None:
    snapshot = _event_snapshot(
        contributions=_non_durable_event_contributions(),
        enabled_entrypoints=("orders_changed",),
    )
    material = _event_material(snapshot)
    registry = ManagedContributionRegistry()
    registry.prepare_generation((material,))
    registry.apply_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=_refresh_success,
        expected_registration_ids=(material["registration_id"],),
    )
    registry._route_owners["event:orders.changed"].add("missing-registration")

    with pytest.raises(PluginConflictError) as corrupt:
        registry.resolve_active_event(event_name="orders.changed")

    assert corrupt.value.code == "RUNTIME_PROJECTION_AMBIGUOUS"


def test_reserved_feishu_command_rejects_the_whole_prepare_batch() -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("run_now", "run_command"),
    )
    materials = tuple(copy.deepcopy(dict(plan.payload)) for plan in _managed_plans(snapshot))
    registry = ManagedContributionRegistry(
        reserved_feishu_command=lambda command: command == "执行示例"
    )

    with pytest.raises(PluginConflictError) as conflict:
        registry.prepare_generation(materials)

    assert conflict.value.code == "CONTRIBUTION_ROUTE_CONFLICT"
    assert registry.snapshot() == ()


def test_authoritative_empty_generation_clears_live_routes_and_refresh_failure_rolls_back() -> None:
    first = _snapshot(
        generation=1,
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("run_now",),
    )
    materials = _active_materials(first)
    registry = ManagedContributionRegistry()
    registry.prepare_generation(materials)
    registry.apply_generation(
        first.automation_id,
        first.generation,
        refresh=_refresh_success,
        expected_registration_ids=tuple(item["registration_id"] for item in materials),
    )
    before = registry.snapshot()

    with pytest.raises(PluginConflictError) as failed:
        registry.apply_generation(
            first.automation_id,
            2,
            refresh=lambda: {"initialized": False, "invalid_tasks": []},
            expected_registration_ids=(),
        )
    assert failed.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    assert registry.snapshot() == before
    assert registry.active_generation(first.automation_id) == 1

    registry.apply_generation(
        first.automation_id,
        2,
        refresh=_refresh_success,
        expected_registration_ids=(),
    )
    assert registry.active_generation(first.automation_id) is None
    assert registry.active_snapshot(automation_id=first.automation_id) == ()
    assert {item.phase for item in registry.snapshot()} == {"DRAINING"}



def test_draining_feishu_generation_releases_its_global_command_for_another_project() -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("run_command",),
    )
    material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "feishu"
    )
    registry = ManagedContributionRegistry()
    registry.prepare_generation((material,))
    registry.apply_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=_refresh_success,
        expected_registration_ids=(material["registration_id"],),
    )
    registry.apply_generation(
        snapshot.automation_id,
        2,
        refresh=_refresh_success,
        expected_registration_ids=(),
    )
    other_project_material = copy.deepcopy(material)
    other_project_material["automation_id"] = "other-project"
    other_project_material["registration_id"] = "other-project:1:run_command"
    registry.prepare_generation((other_project_material,))
    assert {
        item.automation_id for item in registry.snapshot()
    } == {"example-project", "other-project"}


def test_reconciler_atomic_prepare_leaves_no_route_for_duplicate_feishu_command() -> None:
    contributions = _contributions()
    contributions["feishu"].append(
        {
            **copy.deepcopy(contributions["feishu"][0]),
            "id": "run_command_duplicate",
        }
    )
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        contributions=contributions,
        enabled_entrypoints=("run_command", "run_command_duplicate"),
    )
    repository = _PrepareRepository(snapshot)
    registry = ManagedContributionRegistry()
    reconciler = AutomationRuntimeReconciler(
        repository=repository,
        coeffects=_ReadyCoeffects(),
        planner=_ManagedContributionPlanner(),
        driver=ProductionRuntimeEffectDriver(
            broker_handler_keys=(),
            contribution_registry=registry,
        ),
    )

    with pytest.raises(PluginConflictError) as conflict:
        reconciler.prepare_target(repository.generation)

    assert conflict.value.code == "CONTRIBUTION_ROUTE_CONFLICT"
    assert repository.generation.state is RuntimeGenerationState.FAILED
    assert registry.snapshot() == ()


@pytest.mark.parametrize("existing_active", (False, True), ids=("prepared", "active"))
def test_reconciler_atomic_prepare_leaves_no_current_route_on_late_cross_project_conflict(
    existing_active: bool,
) -> None:
    schedule = {"kind": "none", "times": [], "enabled": False}
    existing = replace(
        _snapshot(schedule=schedule, enabled_entrypoints=("run_command",)),
        automation_id="existing-project",
    )
    existing_material = next(
        copy.deepcopy(dict(plan.payload))
        for plan in _managed_plans(existing)
        if plan.payload["contribution_kind"] == "feishu"
    )
    contributions = _contributions()
    contributions["feishu"] = [
        {
            **copy.deepcopy(contributions["feishu"][0]),
            "id": "first_command",
            "commands": ["执行唯一"],
        },
        {
            **copy.deepcopy(contributions["feishu"][0]),
            "id": "second_command",
        },
    ]
    snapshot = _snapshot(
        schedule=schedule,
        contributions=contributions,
        enabled_entrypoints=("first_command", "second_command"),
    )
    repository = _PrepareRepository(snapshot)
    registry = ManagedContributionRegistry()
    registry.prepare_generation((existing_material,))
    if existing_active:
        registry.apply_generation(
            existing.automation_id,
            existing.generation,
            refresh=_refresh_success,
            expected_registration_ids=(existing_material["registration_id"],),
        )
    reconciler = AutomationRuntimeReconciler(
        repository=repository,
        coeffects=_ReadyCoeffects(),
        planner=_ManagedContributionPlanner(),
        driver=ProductionRuntimeEffectDriver(
            broker_handler_keys=(),
            contribution_registry=registry,
        ),
    )

    with pytest.raises(PluginConflictError) as conflict:
        reconciler.prepare_target(repository.generation)

    assert conflict.value.code == "CONTRIBUTION_ROUTE_CONFLICT"
    assert repository.generation.state is RuntimeGenerationState.FAILED
    assert {
        (item.automation_id, item.contribution_id, item.phase)
        for item in registry.snapshot()
    } == {
        (
            existing.automation_id,
            "run_command",
            "COMMITTED" if existing_active else "PREPARED",
        )
    }


def test_restart_restores_full_atomic_batch_from_partial_preparing_journal_and_reconciles() -> None:
    contributions = _contributions()
    contributions["feishu"] = [
        {
            **copy.deepcopy(contributions["feishu"][0]),
            "id": "first_command",
            "commands": ["执行一号"],
        },
        {
            **copy.deepcopy(contributions["feishu"][0]),
            "id": "second_command",
            "commands": ["执行二号"],
        },
    ]
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        contributions=contributions,
        enabled_entrypoints=("first_command", "second_command"),
    )
    plans = _managed_plans(snapshot)
    repository = _PrepareRepository(snapshot)
    repository.generation = RuntimeGenerationRecord(
        snapshot=snapshot,
        state=RuntimeGenerationState.PREPARING,
        effects=(
            _effect(
                snapshot,
                plans[0],
                1,
                state=RuntimeEffectState.APPLIED,
            ),
        ),
    )
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )

    driver.restore_from_repository(repository)

    assert {
        (item.contribution_id, item.phase)
        for item in registry.snapshot()
    } == {
        ("first_command", "PREPARED"),
        ("second_command", "PREPARED"),
    }
    reconciler = AutomationRuntimeReconciler(
        repository=repository,
        coeffects=_ReadyCoeffects(),
        planner=_ManagedContributionPlanner(),
        driver=driver,
    )
    assert reconciler.prepare_target(repository.generation) == ()
    assert repository.generation.state is RuntimeGenerationState.PREPARED
    assert len(repository.generation.effects) == 2
    assert all(
        effect.state is RuntimeEffectState.APPLIED
        for effect in repository.generation.effects
    )


def test_empty_generation_retry_preserves_authoritative_empty_expected_ids() -> None:
    first = _snapshot(
        generation=1,
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("run_now",),
    )
    second = _snapshot(
        generation=2,
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=(),
    )
    first_effects = tuple(
        _effect(
            first,
            plan,
            sequence,
            state=RuntimeEffectState.APPLIED,
        )
        for sequence, plan in enumerate(_managed_plans(first), start=1)
    )
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )
    driver.bind_scheduler_projection_refresher(_refresh_success)
    driver.activate_committed(snapshot=first, effects=first_effects)
    driver.bind_scheduler_projection_refresher(
        lambda: {"initialized": False, "invalid_tasks": []}
    )

    with pytest.raises(PluginConflictError) as failed:
        driver.activate_committed(snapshot=second, effects=())

    assert failed.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    assert registry.active_generation(first.automation_id) == 1
    assert driver._pending_projection_transitions[(first.automation_id, 2)] == (
        "apply",
        (),
    )
    driver.bind_scheduler_projection_refresher(_refresh_success)
    driver.refresh_contribution_projection()
    assert registry.active_generation(first.automation_id) is None
    assert registry.active_snapshot(automation_id=first.automation_id) == ()


def test_activation_rejects_missing_expected_contribution_effects() -> None:
    snapshot = _snapshot(enabled_entrypoints=("run_now", "daily_run"))
    console_plan = next(
        plan
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "console"
    )
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())

    with pytest.raises(PluginConflictError) as mismatch:
        driver.activate_committed(
            snapshot=snapshot,
            effects=(
                _effect(
                    snapshot,
                    console_plan,
                    1,
                    state=RuntimeEffectState.APPLIED,
                ),
            ),
        )

    assert mismatch.value.code == "CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH"


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
        enabled_entrypoints=("orders_changed",),
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
    (("events", "orders_changed"),),
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
    assert plan.payload["backend_status"] == "READY"
    assert plan.payload["reason_code"] is None
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())
    applied = driver.ensure_applied(
        snapshot=snapshot,
        plan=plan,
        effect=_effect(snapshot, plan, 1),
    )
    assert applied.state is RuntimeEffectState.APPLIED
    assert driver.contribution_registry.snapshot()[0].phase == "PREPARED"


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


def test_default_disabled_scheduler_can_omit_a_static_manifest_clock() -> None:
    contributions = copy.deepcopy(_contributions())
    contributions["scheduler"][0].pop("schedule")
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        contributions=contributions,
        enabled_entrypoints=("daily_run",),
    )

    schedule_plan = next(
        plan
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "scheduler"
    )

    assert "schedule" not in schedule_plan.payload["declaration"]
    assert schedule_plan.payload["project_schedule"] == {
        "kind": "none",
        "times": [],
        "enabled": False,
    }
    assert schedule_plan.payload["backend_status"] == "DISABLED"


def test_enabled_scheduler_uses_only_the_real_project_schedule_without_static_cron() -> None:
    contributions = copy.deepcopy(_contributions())
    contributions["scheduler"][0].pop("schedule")
    source_schedule = {
        "kind": "daily_times",
        "times": ["18:30"],
        "enabled": True,
    }
    snapshot = _snapshot(
        schedule=source_schedule,
        contributions=contributions,
        enabled_entrypoints=("daily_run",),
    )

    schedule_plan = next(
        plan
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "scheduler"
    )

    assert "schedule" not in schedule_plan.payload["declaration"]
    assert schedule_plan.payload["project_schedule"] == source_schedule
    assert schedule_plan.payload["backend_status"] == "READY"


@pytest.mark.parametrize(
    "source_schedule",
    (
        {"kind": "daily_times", "times": [], "enabled": True},
        {"kind": "daily_times", "times": ["25:00"], "enabled": True},
        {"kind": "daily_times", "times": ["18:30", "18:30"], "enabled": True},
        {"kind": "startup", "times": ["18:30"], "enabled": True},
    ),
)
def test_enabled_scheduler_rejects_an_invalid_real_project_schedule(
    source_schedule: dict[str, object],
) -> None:
    contributions = copy.deepcopy(_contributions())
    contributions["scheduler"][0].pop("schedule")
    snapshot = _snapshot(
        schedule=source_schedule,
        contributions=contributions,
        enabled_entrypoints=("daily_run",),
    )

    with pytest.raises(PluginConflictError, match="project scheduler is invalid"):
        _managed_plans(snapshot)


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


def test_restart_restores_empty_successor_and_cross_project_feishu_reclaim_with_draining_predecessor() -> None:
    schedule = {"kind": "none", "times": [], "enabled": False}
    predecessor = _snapshot(
        generation=1,
        schedule=schedule,
        enabled_entrypoints=("run_command",),
    )
    empty_successor = _snapshot(
        generation=2,
        schedule=schedule,
        enabled_entrypoints=(),
    )
    reclaimer = replace(
        _snapshot(schedule=schedule, enabled_entrypoints=("run_command",)),
        automation_id="other-project",
    )
    predecessor_effects = tuple(
        _effect(
            predecessor,
            plan,
            sequence,
            state=RuntimeEffectState.APPLIED,
        )
        for sequence, plan in enumerate(_managed_plans(predecessor), start=1)
    )
    reclaimer_effects = tuple(
        _effect(
            reclaimer,
            plan,
            sequence,
            state=RuntimeEffectState.APPLIED,
        )
        for sequence, plan in enumerate(_managed_plans(reclaimer), start=1)
    )
    repository = _GenerationRepository(
        RuntimeGenerationRecord(
            snapshot=predecessor,
            state=RuntimeGenerationState.DRAINING,
            effects=predecessor_effects,
        ),
        RuntimeGenerationRecord(
            snapshot=empty_successor,
            state=RuntimeGenerationState.COMMITTED,
            effects=(),
        ),
        RuntimeGenerationRecord(
            snapshot=reclaimer,
            state=RuntimeGenerationState.COMMITTED,
            effects=reclaimer_effects,
        ),
    )
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )

    driver.restore_from_repository(repository)

    assert {
        (item.automation_id, item.generation, item.phase)
        for item in registry.snapshot()
    } == {
        (predecessor.automation_id, 1, "DRAINING"),
        (reclaimer.automation_id, 1, "COMMITTED"),
    }
    assert registry.active_generation(predecessor.automation_id) is None
    assert registry.active_generation(reclaimer.automation_id) == 1
    assert registry.resolve_active_feishu_command("执行示例").automation_id == (
        reclaimer.automation_id
    )


def test_restart_rejects_committed_generation_missing_all_expected_contribution_effects() -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("run_command",),
    )
    repository = _GenerationRepository(
        RuntimeGenerationRecord(
            snapshot=snapshot,
            state=RuntimeGenerationState.COMMITTED,
            effects=(),
        )
    )
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )

    with pytest.raises(PluginConflictError) as mismatch:
        driver.restore_from_repository(repository)

    assert mismatch.value.code == "CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH"
    assert registry.snapshot() == ()


def test_restart_rejects_disposing_generation_with_missing_disposed_journal_entry() -> None:
    snapshot = _snapshot(
        schedule={"kind": "none", "times": [], "enabled": False},
        enabled_entrypoints=("run_now", "run_command"),
    )
    feishu_plan = next(
        plan
        for plan in _managed_plans(snapshot)
        if plan.payload["contribution_kind"] == "feishu"
    )
    repository = _GenerationRepository(
        RuntimeGenerationRecord(
            snapshot=snapshot,
            state=RuntimeGenerationState.DISPOSING,
            effects=(
                _effect(
                    snapshot,
                    feishu_plan,
                    2,
                    state=RuntimeEffectState.DISPOSING,
                ),
            ),
        )
    )
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())

    with pytest.raises(PluginConflictError) as mismatch:
        driver.restore_from_repository(repository)

    assert mismatch.value.code == "CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH"
    assert driver.contribution_registry.snapshot() == ()


def test_restart_resumes_partially_disposed_contribution_generation_without_reclaiming_routes() -> None:
    schedule = {"kind": "none", "times": [], "enabled": False}
    disposing = _snapshot(
        schedule=schedule,
        enabled_entrypoints=("run_now", "run_command"),
    )
    reclaimer = replace(
        _snapshot(schedule=schedule, enabled_entrypoints=("run_command",)),
        automation_id="other-project",
    )
    disposing_effects = tuple(
        _effect(
            disposing,
            plan,
            sequence,
            state=(
                RuntimeEffectState.DISPOSING
                if plan.payload["contribution_kind"] == "feishu"
                else RuntimeEffectState.DISPOSED
            ),
        )
        for sequence, plan in enumerate(_managed_plans(disposing), start=1)
    )
    reclaimer_effects = tuple(
        _effect(
            reclaimer,
            plan,
            sequence,
            state=RuntimeEffectState.APPLIED,
        )
        for sequence, plan in enumerate(_managed_plans(reclaimer), start=1)
    )
    repository = _GenerationRepository(
        RuntimeGenerationRecord(
            snapshot=disposing,
            state=RuntimeGenerationState.DISPOSING,
            effects=disposing_effects,
        ),
        RuntimeGenerationRecord(
            snapshot=reclaimer,
            state=RuntimeGenerationState.COMMITTED,
            effects=reclaimer_effects,
        ),
    )
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )

    driver.restore_from_repository(repository)

    restored = registry.snapshot()
    assert {
        (item.automation_id, item.contribution_id, item.phase)
        for item in restored
    } == {
        (disposing.automation_id, "run_command", "DRAINING"),
        (reclaimer.automation_id, "run_command", "COMMITTED"),
    }
    assert registry.active_generation(disposing.automation_id) is None
    assert registry.resolve_active_feishu_command("执行示例").automation_id == (
        reclaimer.automation_id
    )


@pytest.mark.parametrize(
    ("generation_state", "activation_phase", "retains_diagnostic"),
    (
        (RuntimeGenerationState.COMMITTED, RuntimeActivationPhase.BLOCKED, True),
        (RuntimeGenerationState.PREPARED, RuntimeActivationPhase.ROLLED_BACK, False),
    ),
)
def test_restart_keeps_blocked_and_rolled_back_generations_off_feishu_routes(
    generation_state: RuntimeGenerationState,
    activation_phase: RuntimeActivationPhase,
    retains_diagnostic: bool,
) -> None:
    schedule = {"kind": "none", "times": [], "enabled": False}
    interrupted = _snapshot(
        schedule=schedule,
        enabled_entrypoints=("run_command",),
    )
    reclaimer = replace(
        _snapshot(schedule=schedule, enabled_entrypoints=("run_command",)),
        automation_id="other-project",
    )
    interrupted_effects = tuple(
        _effect(
            interrupted,
            plan,
            sequence,
            state=RuntimeEffectState.APPLIED,
        )
        for sequence, plan in enumerate(_managed_plans(interrupted), start=1)
    )
    reclaimer_effects = tuple(
        _effect(
            reclaimer,
            plan,
            sequence,
            state=RuntimeEffectState.APPLIED,
        )
        for sequence, plan in enumerate(_managed_plans(reclaimer), start=1)
    )
    repository = _GenerationRepository(
        RuntimeGenerationRecord(
            snapshot=interrupted,
            state=generation_state,
            effects=interrupted_effects,
            activation_transition_token="00000000-0000-4000-8000-000000000001",
            activation_phase=activation_phase,
        ),
        RuntimeGenerationRecord(
            snapshot=reclaimer,
            state=RuntimeGenerationState.COMMITTED,
            effects=reclaimer_effects,
        ),
    )
    registry = ManagedContributionRegistry()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        contribution_registry=registry,
    )

    driver.restore_from_repository(repository)

    interrupted_records = tuple(
        item
        for item in registry.snapshot()
        if item.automation_id == interrupted.automation_id
    )
    assert bool(interrupted_records) is retains_diagnostic
    assert all(item.phase == "DRAINING" for item in interrupted_records)
    assert registry.resolve_active_feishu_command("执行示例").automation_id == (
        reclaimer.automation_id
    )


def test_duplicate_webhook_routes_fail_before_a_generation_can_commit() -> None:
    contributions = _contributions()
    contributions["webhook"].append(
        {
            **copy.deepcopy(contributions["webhook"][0]),
            "id": "receive_hook_duplicate",
        }
    )
    snapshot = _snapshot(
        contributions=contributions,
        enabled_entrypoints=("receive_hook", "receive_hook_duplicate"),
        schedule={"kind": "none", "times": [], "enabled": False},
    )
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
    assert getattr(exc_info.value, "code", None) == "CONTRIBUTION_ROUTE_CONFLICT"
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


def test_generation_commit_accepts_disabled_scheduleless_declaration_only_with_real_schedule_on_enable() -> None:
    contributions = _contributions()
    scheduler = contributions["scheduler"][0]
    scheduler["default_enabled"] = False
    scheduler.pop("schedule")
    snapshot = {"runtime_model": "SERVICE_V2", "plugin_api": "2.0.0"}

    assert _scheduler_contribution_binding(
        snapshot=snapshot,
        execution_metadata={"contributions": contributions},
        enabled_entrypoints=("run_now",),
        schedule_expressions=(),
    ) == ("daily_run", False)
    with pytest.raises(Exception, match="explicit project schedule"):
        _scheduler_contribution_binding(
            snapshot=snapshot,
            execution_metadata={"contributions": contributions},
            enabled_entrypoints=("daily_run",),
            schedule_expressions=(),
        )
    assert _scheduler_contribution_binding(
        snapshot=snapshot,
        execution_metadata={"contributions": contributions},
        enabled_entrypoints=("daily_run",),
        schedule_expressions=("0 9 * * *",),
    ) == ("daily_run", True)
