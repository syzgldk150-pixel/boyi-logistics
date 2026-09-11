"""Every shipped V1 instance has a closed, executable V2 migration target."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.automation_plugins.first_party import resolve_release_first_party_manifests
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.migration_binding_mapping import reviewed_migration_binding_mapping
from agent.automation_plugins.migration_entrypoint_ownership import migration_target_entrypoints_and_ownership
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.tool_registry import ToolRegistry
from shared.automation_project_manifest import FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES


SOURCES = resolve_release_first_party_manifests(ToolRegistry())
INSTANCES = {key: template for key, template in FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES.items() if template.tool_name in SOURCES}


@pytest.mark.parametrize("instance", sorted(INSTANCES))
def test_every_production_instance_retains_its_accounts_resources_and_entrypoints(instance):
    template = INSTANCES[instance]
    source = SOURCES[template.tool_name]
    plugin_id = instance + "_v2" if template.tool_name == "clock_in_dual" else template.tool_name + "_v2"
    manifest = AutomationPluginManifestV2.from_mapping(json.loads((Path(__file__).resolve().parents[1] /
        "agent/service_v2_plugins" / plugin_id / "manifest.json").read_text()))
    target = ServiceV2ProjectContract.from_manifest(manifest)
    mapping = reviewed_migration_binding_mapping(source_automation_id=instance,
        source_plugin_id=source.plugin_id, target_plugin_id=plugin_id)
    assert mapping is not None
    source_accounts = {role["role"]: (["isolated-one", "isolated-two"] if role.get("collection") else "isolated-one") for role in source.account_roles}
    bindings = dict(template.resource_bindings)
    for role in source.resource_roles:
        if role["role"] not in bindings and role.get("required"):
            bindings[role["role"]] = "isolated-" + role["role"]
    schedule = ({"kind": "daily_times", "times": ["23:55"], "enabled": True} if "scheduler" in template.allowed_entrypoints
        else {"kind": "none", "enabled": False, "times": []})
    enabled, ownership, consumed = migration_target_entrypoints_and_ownership(
        source=SimpleNamespace(automation_id=instance, plugin_id=source.plugin_id),
        target=SimpleNamespace(plugin_id=manifest.plugin_id, contributions=manifest.contributes),
        source_enabled_entrypoints=tuple(sorted(template.allowed_entrypoints)), source_schedule=schedule,
        source_resource_bindings=bindings)
    accounts = AutomationPluginManagementService._map_migration_bindings(source_bindings=source_accounts,
        source_roles=source.account_roles, target_roles=target.account_roles, source_to_target_roles=mapping.account_roles, kind="account")
    resources = AutomationPluginManagementService._map_migration_bindings(source_bindings=bindings,
        source_roles=source.resource_roles, target_roles=target.resource_roles, source_to_target_roles=mapping.resource_roles,
        explicitly_consumed_source_bindings=consumed, kind="resource")
    assert accounts == {target: source_accounts[source] for source, target in mapping.account_roles.items()}
    assert resources == {target: bindings[source] for source, target in mapping.resource_roles.items() if source in bindings}
    assert len(enabled) == len(set(enabled))
    for kind in template.allowed_entrypoints:
        assert ownership[kind]["source_enabled"] is True
        assert ownership["owners"]["CUTOVER"][kind] == "SERVICE_V2"
        assert ownership["owners"]["ROLLED_BACK"][kind] == "ACTION_V1"
