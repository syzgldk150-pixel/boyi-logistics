from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import zipfile

import pytest

from agent.automation_plugins.developer_reports_v2 import project_permission_report
from agent.automation_plugins.errors import PluginConflictError, PluginManifestError
from agent.automation_plugins.host_capability_registry import governance_for_effect
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import (
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeEffectKind,
    RuntimeGenerationSnapshot,
)
from agent.automation_plugins.package_v2 import verify_unsigned_plugin_zip_v2
from agent.automation_plugins.production import ProductionRuntimeEffectDriver
from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.automation_plugins.service_v2_projection import (
    ManagedContributionRegistry,
    _service_v2_contribution_effect_plans,
)
from agent.automation_plugins.inspection_v2 import (
    service_v2_wizard_projection,
    validate_service_v2_install_contract,
)
from agent.harness.catalog import HarnessToolCatalog, ManagedToolHandle
from agent.harness.errors import HarnessError
from shared.automation_project_manifest import TRUSTED_AUTOMATION_ENTRYPOINTS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = (
    PROJECT_ROOT
    / "agent"
    / "extension_sdk"
    / "schemas"
    / "manifest-v2.schema.json"
)


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _manifest_mapping(
    *,
    provider_effect: str = "read",
    declared_effect: str = "read",
    include_harness: bool = True,
) -> dict[str, Any]:
    service = "plugin.harness_contract.runner@1"
    contributes: dict[str, Any] = {
        "console": [],
        "scheduler": [],
        "webhook": [],
        "feishu": [],
        "events": [],
    }
    if include_harness:
        contributes["harness"] = [
            {
                "id": "analyze_tool",
                "title": "Analyze problems",
                "description": "Reads an offline projection and computes a report.",
                "scenarios": ["分析当前问题件结果"],
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                    "required": [],
                },
                "service": service,
                "operation": "analyze",
                "effect": declared_effect,
                "confirmation_policy": "none",
                "preview_operation": None,
            }
        ]
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": "harness_contract",
        "name": "Harness contract fixture",
        "version": "1.0.0",
        "description": "Offline Harness contribution contract fixture.",
        "host_api": {"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
        "runtime": {
            "kind": "python_subprocess",
            "python": "3.10",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
            "requirements_lock": None,
            "wheelhouse": [],
        },
        "provides": [
            {
                "service": service,
                "operations": [{"name": "analyze", "effect": provider_effect}],
            }
        ],
        "requires": [],
        "capabilities": [],
        "account_roles": [],
        "resource_roles": [],
        "contributes": contributes,
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "storage": {"kv": False, "collections": []},
    }


def _verified(manifest: dict[str, Any]) -> Any:
    stream = BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        )
        archive.writestr("payload/main.py", b"def run():\n    return {}\n")
    package_bytes = stream.getvalue()
    return verify_unsigned_plugin_zip_v2(
        package_bytes,
        transport_sha256=hashlib.sha256(package_bytes).hexdigest(),
    )


def _runtime_snapshot(
    *,
    generation: int = 1,
    title: str = "Analyze problems",
    enabled: tuple[str, ...] = ("analyze_tool",),
) -> RuntimeGenerationSnapshot:
    service = "plugin.harness_contract.runner@1"
    declaration = {
        "id": "analyze_tool",
        "title": title,
        "description": "Reads an offline projection and computes a report.",
        "scenarios": ["分析当前问题件结果"],
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "service": service,
        "operation": "analyze",
        "effect": "read",
        "confirmation_policy": "none",
        "preview_operation": None,
    }
    contributions = {
        "console": [],
        "scheduler": [],
        "webhook": [],
        "feishu": [],
        "events": [],
        "harness": [declaration],
    }
    schedule = {"kind": "none", "times": [], "enabled": False}
    compiled_invocations = {
        "analyze_tool": {
            "arguments": {},
            "dynamic_resolvers": {},
            "target": {
                "service": service,
                "operation": "analyze",
                "contribution_id": "analyze_tool",
                "contribution_kind": "harness",
            },
            "governance": governance_for_effect("read").to_mapping(),
        }
    }
    metadata = {
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
                    "operations": [{"name": "analyze", "effect": "read"}],
                }
            ],
            "requires": [],
        },
        "contributions": contributions,
        "storage_contract": {"kv": False, "collections": []},
    }
    return RuntimeGenerationSnapshot(
        automation_id="harness-project",
        generation=generation,
        plugin_id="harness_contract",
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
        enabled_entrypoints=enabled,
        execution_metadata=metadata,
        created_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        runtime_model=PluginRuntimeModel.SERVICE_V2,
        plugin_api="2.0.0",
    )


def _harness_material(snapshot: RuntimeGenerationSnapshot) -> dict[str, Any]:
    plan = next(
        item
        for item in _service_v2_contribution_effect_plans(snapshot)
        if item.payload["contribution_kind"] == "harness"
    )
    return copy.deepcopy(dict(plan.payload))


def test_harness_is_required_for_service_v2_manifests_and_closed_when_present() -> None:
    legacy_source = _manifest_mapping(include_harness=False)
    legacy_manifest = AutomationPluginManifestV2.from_mapping(legacy_source)
    with pytest.raises(PluginConflictError, match="AI assistant capability"):
        validate_service_v2_install_contract(
            SimpleNamespace(manifest=legacy_manifest)
        )

    source = _manifest_mapping()
    manifest = AutomationPluginManifestV2.from_mapping(source)
    assert manifest.to_mapping() == source
    with pytest.raises(PluginManifestError, match="unsupported fields"):
        invalid = copy.deepcopy(source)
        invalid["contributes"]["harness"][0]["default_enabled"] = False
        AutomationPluginManifestV2.from_mapping(invalid)


@pytest.mark.parametrize(
    ("provider_effect", "declared_effect", "message"),
    (
        ("external_write", "external_write", "confirmation_policy"),
        ("read", "compute", "match the provided operation effect"),
        ("external_write", "read", "match the provided operation effect"),
    ),
)
def test_harness_effect_is_redundant_and_must_match_safe_provider(
    provider_effect: str,
    declared_effect: str,
    message: str,
) -> None:
    with pytest.raises(PluginManifestError, match=message):
        AutomationPluginManifestV2.from_mapping(
            _manifest_mapping(
                provider_effect=provider_effect,
                declared_effect=declared_effect,
            )
        )


@pytest.mark.parametrize("effect", ("read", "compute"))
def test_harness_compiled_invocation_governance_and_safe_projections(
    effect: str,
) -> None:
    source = _manifest_mapping(provider_effect=effect, declared_effect=effect)
    manifest = AutomationPluginManifestV2.from_mapping(source)
    contract = ServiceV2ProjectContract.from_manifest(manifest)
    invocation = contract.invocation_contracts["analyze_tool"]
    assert invocation["contribution_kind"] == "harness"
    assert invocation["service"] == "plugin.harness_contract.runner@1"
    assert invocation["operation"] == "analyze"
    assert invocation["effect"] == effect
    assert invocation["argument_template"] == {}
    assert invocation["dynamic_resolvers"] == {}
    assert invocation["input_schema"] == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }
    assert invocation["governance"]["harness_allowed"] is True
    assert invocation["governance"]["broker_effect"] == "read"
    assert invocation["governance"]["operation_type"] == effect

    wizard = service_v2_wizard_projection(SimpleNamespace(manifest=manifest))
    assert wizard["contributions"] == [
        {
            "id": "analyze_tool",
            "kind": "harness",
            "title": "Analyze problems",
            "description": "Reads an offline projection and computes a report.",
            "effect": effect,
        }
    ]

    report = project_permission_report(_verified(source))
    row = next(item for item in report["contributions"] if item["kind"] == "harness")
    assert row["effect"] == effect
    assert row["governance"]["harness_allowed"] is True
    assert row["governance"]["broker_effect"] == "read"
    assert row["declaration"] == source["contributes"]["harness"][0]


def test_harness_schema_is_closed_and_keeps_legacy_required_kinds() -> None:
    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    contributes = schema["properties"]["contributes"]
    assert contributes["required"] == [
        "console",
        "scheduler",
        "webhook",
        "feishu",
        "events",
        "harness",
    ]
    harness = contributes["properties"]["harness"]
    assert harness["items"] == {"$ref": "#/$defs/harness_contribution"}
    harness_definition = schema["$defs"]["harness_contribution"]
    assert harness_definition["additionalProperties"] is False
    assert harness_definition["required"] == [
        "id",
        "title",
        "description",
        "scenarios",
        "input_schema",
        "service",
        "operation",
        "effect",
        "confirmation_policy",
        "preview_operation",
    ]
    assert harness_definition["properties"]["effect"]["enum"] == [
        "read",
        "compute",
        "internal_write",
        "external_write",
    ]


def test_harness_registry_snapshot_is_immutable_and_catalog_is_atomic() -> None:
    first = _runtime_snapshot(generation=1, title="Analyze problems")
    registry = ManagedContributionRegistry()
    first_material = _harness_material(first)
    assert first_material["runtime_permissions"] == first.execution_metadata[
        "runtime_descriptor"
    ]["runtime_permissions"]
    assert first_material["harness_contract"]["effect"] == "read"
    registry.prepare_generation((first_material,))
    assert registry.active_snapshot() == ()

    refresh_observations: list[tuple[int | None, tuple[Any, ...]]] = []

    def refresh_first() -> dict[str, Any]:
        refresh_observations.append(
            (
                registry.active_generation(first.automation_id),
                registry.active_snapshot(),
            )
        )
        return {"initialized": True, "invalid_tasks": []}

    registry.apply_generation(
        first.automation_id,
        first.generation,
        refresh=refresh_first,
    )
    assert refresh_observations == [(None, ())]
    active = registry.active_snapshot(contribution_kind="harness")
    assert len(active) == 1
    assert set(active[0]) == {
        "automation_id",
        "generation",
        "contribution_id",
        "contribution_kind",
        "runtime_model",
        "runtime_permissions",
        "harness_contract",
    }
    assert "package_sha256" not in active[0]
    assert "manifest_sha256" not in active[0]
    assert "source" not in active[0]
    assert active[0]["runtime_model"] == "SERVICE_V2"
    assert active[0]["runtime_permissions"]["network"] is False
    assert active[0]["runtime_permissions"]["file_roles"] == ()
    assert active[0]["harness_contract"]["input_schema"]["required"] == ()
    with pytest.raises(TypeError):
        active[0]["generation"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        active[0]["harness_contract"]["effect"] = "compute"  # type: ignore[index]

    class InvocationPort:
        def __init__(self) -> None:
            self.calls: list[tuple[object, dict[str, Any]]] = []

        def invoke(self, *, handle: object, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((handle, dict(arguments)))
            return {"status": "SUCCESS"}

    port = InvocationPort()
    catalog = HarnessToolCatalog(invocation_port=port, snapshot_provider=registry)
    first_tool_id = catalog.public_tools()[0]["tool_id"]
    assert "harness-project" not in first_tool_id
    assert "analyze_tool" not in first_tool_id
    assert catalog.invoke(tool_id=first_tool_id, arguments={}) == {"status": "SUCCESS"}
    assert port.calls == [
        (ManagedToolHandle("harness-project", 1, "analyze_tool"), {})
    ]

    second = _runtime_snapshot(generation=2, title="Analyze problems v2")
    registry.prepare_generation((_harness_material(second),))
    before_failed_switch = registry.active_snapshot()

    def fail_refresh() -> None:
        raise RuntimeError("offline refresh failure")

    with pytest.raises(PluginConflictError) as failed:
        registry.apply_generation(
            second.automation_id,
            second.generation,
            refresh=fail_refresh,
        )
    assert failed.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    assert registry.active_snapshot() == before_failed_switch
    assert registry.active_generation(first.automation_id) == 1

    registry.apply_generation(
        second.automation_id,
        second.generation,
        refresh=lambda: {"initialized": True, "invalid_tasks": []},
    )
    catalog.refresh()
    second_tool_id = catalog.public_tools()[0]["tool_id"]
    assert second_tool_id != first_tool_id
    assert {item["generation"] for item in registry.active_snapshot()} == {2}
    with pytest.raises(HarnessError) as old_tool:
        catalog.invoke(tool_id=first_tool_id, arguments={})
    assert old_tool.value.code == "HARNESS_TOOL_NOT_FOUND"

    before_failed_withdraw = registry.active_snapshot()
    with pytest.raises(PluginConflictError) as failed_withdraw:
        registry.withdraw_generation(
            second.automation_id,
            second.generation,
            refresh=fail_refresh,
        )
    assert failed_withdraw.value.code == "RUNTIME_PROJECTION_REFRESH_FAILED"
    assert registry.active_snapshot() == before_failed_withdraw

    registry.withdraw_generation(
        second.automation_id,
        second.generation,
        refresh=lambda: {"initialized": True, "invalid_tasks": []},
    )
    assert registry.active_snapshot() == ()
    catalog.refresh()
    assert catalog.public_tools() == ()


def test_harness_registry_effective_snapshot_requires_process_canary() -> None:
    availability = RuntimeContributionBackendAvailability()
    snapshot = _runtime_snapshot()
    material = _harness_material(snapshot)
    registry = ManagedContributionRegistry(backend_availability=availability)
    registry.prepare_generation((material,))
    registry.apply_generation(
        snapshot.automation_id,
        snapshot.generation,
        refresh=lambda: {"initialized": True, "invalid_tasks": []},
    )

    assert registry.active_snapshot(contribution_kind="harness") == ()
    with pytest.raises(PluginConflictError) as unavailable:
        registry.resolve_active(
            snapshot.automation_id,
            snapshot.generation,
            "harness",
            "analyze_tool",
        )
    assert unavailable.value.code == "CAPABILITY_UNAVAILABLE"

    availability.mark_available("harness")
    assert len(registry.active_snapshot(contribution_kind="harness")) == 1
    resolved = registry.resolve_active(
        snapshot.automation_id,
        snapshot.generation,
        "harness",
        "analyze_tool",
    )
    assert resolved.backend_status == "READY"
    assert resolved.schedule_sha256 == material["schedule_sha256"]

    availability.mark_unavailable("harness")
    assert registry.active_snapshot(contribution_kind="harness") == ()


def test_unsafe_harness_permissions_and_legacy_action_entrypoint_fail_closed() -> None:
    snapshot = _runtime_snapshot()
    material = _harness_material(snapshot)
    material["runtime_permissions"] = {
        "network": True,
        "browser": False,
        "office": False,
        "file_roles": [],
        "broker_operations": [],
        "max_broker_calls": 0,
    }
    registry = ManagedContributionRegistry()
    with pytest.raises(PluginConflictError) as unsafe:
        registry.prepare_generation((material,))
    assert unsafe.value.code == "CAPABILITY_UNAVAILABLE"
    assert registry.snapshot() == ()

    # Harness is a Service V2-only contribution kind; the legacy trusted
    # Action V1 entrypoint set remains deliberately unchanged.
    assert "harness" not in TRUSTED_AUTOMATION_ENTRYPOINTS


@pytest.mark.parametrize("missing_field", ("runtime_permissions", "harness_contract"))
def test_harness_material_requires_signed_runtime_surface_and_contract(
    missing_field: str,
) -> None:
    snapshot = _runtime_snapshot()
    material = _harness_material(snapshot)
    material.pop(missing_field)
    registry = ManagedContributionRegistry()
    with pytest.raises(PluginConflictError) as missing:
        registry.prepare_generation((material,))
    assert missing.value.code == "PLUGIN_CONTRACT_INVALID"
    assert registry.snapshot() == ()


def test_harness_material_requires_a_signed_runtime_descriptor() -> None:
    snapshot = _runtime_snapshot()
    metadata = copy.deepcopy(dict(snapshot.execution_metadata))
    metadata.pop("runtime_descriptor")
    invalid_snapshot = replace(snapshot, execution_metadata=metadata)
    with pytest.raises(PluginConflictError) as missing:
        _service_v2_contribution_effect_plans(invalid_snapshot)
    assert missing.value.code == "PLUGIN_CONTRACT_INVALID"


def test_production_effect_journal_preserves_closed_harness_material() -> None:
    material = _harness_material(_runtime_snapshot())
    validated = ProductionRuntimeEffectDriver._validated_contribution_payload(
        material
    )

    assert validated == material
    assert validated["runtime_model"] == "SERVICE_V2"
    assert validated["runtime_permissions"]["broker_operations"] == []
    assert validated["harness_contract"]["input_schema"][
        "additionalProperties"
    ] is False


@pytest.mark.parametrize(
    "change",
    (
        {"network": True},
        {"browser": True},
        {"office": True},
        {"file_roles": ["file"]},
        {"broker_operations": [{"operation": "storage.kv"}]},
        {"max_broker_calls": 1},
    ),
)
def test_harness_material_rejects_every_nonempty_runtime_permission_surface(
    change: dict[str, Any],
) -> None:
    material = _harness_material(_runtime_snapshot())
    material["runtime_permissions"].update(change)
    registry = ManagedContributionRegistry()
    with pytest.raises(PluginConflictError) as unsafe:
        registry.prepare_generation((material,))
    assert unsafe.value.code == "CAPABILITY_UNAVAILABLE"
    assert registry.snapshot() == ()
