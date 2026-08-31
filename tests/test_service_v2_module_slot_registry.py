from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.host_capability_registry import governance_for_effect
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeEffectKind,
    RuntimeGenerationSnapshot,
)
from agent.automation_plugins.service_v2_projection import (
    ManagedContributionRegistry,
    _service_v2_contribution_effect_plans,
)


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _snapshot(
    *,
    automation_id: str = "waybill-project",
    generation: int = 1,
    title: str = "校验运单",
    provider_effect: str = "read",
) -> RuntimeGenerationSnapshot:
    service = "plugin.waybill_extension.validator@1"
    declaration = {
        "id": "validate_waybill",
        "slot": "waybill_entry.validators",
        "title": title,
        "service": service,
        "operation": "validate",
        "default_enabled": True,
    }
    contributions = {
        "console": [],
        "scheduler": [],
        "webhook": [],
        "feishu": [],
        "events": [],
        "module_slots": [declaration],
    }
    schedule = {"kind": "none", "times": [], "enabled": False}
    compiled_invocations = {
        "validate_waybill": {
            "arguments": {},
            "dynamic_resolvers": {"waybill": "verified_module_slots_waybill"},
            "target": {
                "service": service,
                "operation": "validate",
                "contribution_id": "validate_waybill",
                "contribution_kind": "module_slots",
            },
            "governance": governance_for_effect(provider_effect).to_mapping(),
        }
    }
    metadata: dict[str, Any] = {
        "project_config_version": 1,
        "project_config": {},
        "account_bindings": {},
        "resource_bindings": {},
        "device_binding": None,
        "schedule": schedule,
        "compiled_invocations": compiled_invocations,
        "runtime_descriptor": {
            "install_metadata": {},
            "runtime": {"mode": "on_demand"},
            "runtime_permissions": {
                "network": False,
                "browser": False,
                "office": False,
                "file_roles": [],
                "broker_operations": [],
                "max_broker_calls": 0,
            },
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
                    "operations": [
                        {"name": "validate", "effect": provider_effect}
                    ],
                }
            ],
            "requires": [],
        },
        "contributions": contributions,
        "storage_contract": {"kv": False, "collections": []},
    }
    return RuntimeGenerationSnapshot(
        automation_id=automation_id,
        generation=generation,
        plugin_id="waybill_extension",
        plugin_version="1.0.0",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD,
        project_config_sha256=_sha({}),
        account_bindings_sha256=_sha({}),
        resource_bindings_sha256=_sha({}),
        device_binding_sha256=_sha(None),
        schedule_sha256=_sha(schedule),
        core_registry_sha256="3" * 64,
        tool_contract_sha256=_sha({}),
        invocation_contracts_sha256="4" * 64,
        compiled_invocations_sha256=_sha(compiled_invocations),
        runtime_descriptor_sha256=_sha(metadata["runtime_descriptor"]),
        governance_anchor_sha256=_sha({}),
        policy_contract_sha256="5" * 64,
        enabled_entrypoints=("validate_waybill",),
        execution_metadata=metadata,
        created_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )


def _material(snapshot: RuntimeGenerationSnapshot) -> dict[str, Any]:
    plan = next(
        plan
        for plan in _service_v2_contribution_effect_plans(snapshot)
        if plan.payload.get("contribution_kind") == "module_slots"
    )
    assert plan.kind is RuntimeEffectKind.CONTRIBUTION_REGISTRATION
    assert plan.payload["backend"] == "managed_module_slot_host"
    return copy.deepcopy(dict(plan.payload))


def _activate(
    registry: ManagedContributionRegistry,
    snapshot: RuntimeGenerationSnapshot,
) -> dict[str, Any]:
    material = _material(snapshot)
    registry.prepare_generation((material,))
    registry.apply_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=lambda: {"initialized": True, "invalid_tasks": []},
        expected_registration_ids=(material["registration_id"],),
    )
    return material


def test_registry_projects_only_safe_fields_and_resolves_exact_internal_owner() -> None:
    registry = ManagedContributionRegistry()
    snapshot = _snapshot()
    _activate(registry, snapshot)

    public = registry.active_module_slot_snapshot()

    assert len(public) == 1
    assert set(public[0]) == {"slot", "handle", "title"}
    assert public[0]["slot"] == "waybill_entry.validators"
    assert len(public[0]["handle"]) == 64
    assert set(registry.active_snapshot(contribution_kind="module_slots")[0]) == {
        "slot",
        "handle",
        "title",
    }
    target = registry.resolve_active_module_slot(
        slot=public[0]["slot"],
        handle=public[0]["handle"],
    )
    assert (
        target.automation_id,
        target.generation,
        target.contribution_id,
        target.contribution_kind,
        target.service,
        target.operation,
    ) == (
        snapshot.automation_id,
        1,
        "validate_waybill",
        "module_slots",
        "plugin.waybill_extension.validator@1",
        "validate",
    )
    assert target.declaration["slot"] == "waybill_entry.validators"
    assert len(target.declaration_sha256) == 64
    with pytest.raises(TypeError):
        target.declaration["title"] = "tampered"


def test_generation_switch_and_withdraw_revoke_old_module_slot_handles() -> None:
    registry = ManagedContributionRegistry()
    first = _snapshot(generation=1, title="第一代校验")
    _activate(registry, first)
    old_handle = registry.active_module_slot_snapshot()[0]["handle"]

    second = _snapshot(generation=2, title="第二代校验")
    _activate(registry, second)
    current = registry.active_module_slot_snapshot()

    assert len(current) == 1
    assert current[0]["title"] == "第二代校验"
    assert current[0]["handle"] != old_handle
    with pytest.raises(PluginConflictError) as stale:
        registry.resolve_active_module_slot(
            slot="waybill_entry.validators",
            handle=old_handle,
        )
    assert stale.value.code == "CAPABILITY_UNAVAILABLE"

    registry.withdraw_generation(
        second.automation_id,
        second.generation,
        refresh=lambda: {"initialized": True, "invalid_tasks": []},
    )
    assert registry.active_module_slot_snapshot() == ()


def test_same_contribution_id_from_multiple_owners_coexists_with_distinct_handles() -> None:
    registry = ManagedContributionRegistry()
    _activate(registry, _snapshot(automation_id="owner-a"))
    _activate(registry, _snapshot(automation_id="owner-b"))

    public = registry.active_module_slot_snapshot()

    assert len(public) == 2
    assert len({item["handle"] for item in public}) == 2


def test_module_slot_effect_and_declaration_drift_fail_closed() -> None:
    with pytest.raises(PluginConflictError) as write_effect:
        _service_v2_contribution_effect_plans(
            _snapshot(provider_effect="external_write")
        )
    assert write_effect.value.code == "CAPABILITY_UNAVAILABLE"

    material = _material(_snapshot())
    material["declaration"]["title"] = "drifted"
    with pytest.raises(PluginConflictError):
        ManagedContributionRegistry().prepare_generation((material,))


@pytest.mark.parametrize(
    ("slot", "handle"),
    [
        ("waybill_entry.actions", "a" * 64),
        ("waybill_entry.validators", "A" * 64),
        ("module_slots", "a" * 64),
    ],
)
def test_module_slot_resolver_rejects_wrong_slot_or_handle(
    slot: str,
    handle: str,
) -> None:
    registry = ManagedContributionRegistry()
    _activate(registry, _snapshot())
    with pytest.raises(PluginConflictError):
        registry.resolve_active_module_slot(slot=slot, handle=handle)
