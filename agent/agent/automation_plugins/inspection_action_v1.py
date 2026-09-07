"""Display-only wizard projection for an already verified ACTION_V1 package."""
from __future__ import annotations

from typing import Any

from agent.automation_plugins.inspection_v2 import _thaw_json
from agent.automation_plugins.code_owned_fields import first_party_code_owned_config_fields
from agent.automation_plugins.models import PluginTrustSource
from shared.plugin_management import management_for, settings_mode


def action_v1_wizard_projection(verified: Any) -> dict[str, Any]:
    manifest = verified.manifest
    config_schema = _thaw_json(manifest.config_schema)
    internal_fields = set(first_party_code_owned_config_fields(automation_id="", plugin_id=manifest.plugin_id,
        trust_source=PluginTrustSource.ED25519_UPLOAD.value))
    config_schema["properties"] = {name: value for name, value in config_schema.get("properties", {}).items()
        if name not in internal_fields}
    config_schema["required"] = [name for name in config_schema.get("required", []) if name not in internal_fields]
    account_roles = [{key: _thaw_json(role[key]) for key in ("role", "allowed_systems", "required")}
        for role in manifest.account_roles]
    resource_roles = [{key: _thaw_json(role[key]) for key in ("role", "allowed_kinds", "required")}
        for role in manifest.resource_roles]
    account_names = {role["role"] for role in account_roles}
    resource_names = {role["role"] for role in resource_roles}
    permissions = [{"name": item["operation"], "operations": [item["action"]],
        "account_role": role if role in account_names else None,
        "resource_role": role if role in resource_names else None}
        for item in manifest.runtime_permissions["broker_operations"]
        for role in item["roles"]]
    return {"plugin_id": manifest.plugin_id, "name": manifest.name, "version": manifest.version,
        "runtime_model": "ACTION_V1", "host_api": None, "permissions": permissions,
        "account_roles": account_roles, "resource_roles": resource_roles,
        "config_schema": config_schema,
        "contributions": [{"id": "action." + entrypoint, "kind": entrypoint,
            "title": manifest.name, "default_enabled": True} for entrypoint in manifest.allowed_entrypoints],
        "scheduling": {"supported": manifest.scheduling["supported"],
            "default_schedule": {"kind": "none", "times": [], "enabled": False}},
        "settings_ui": None,
        "management": management_for(manifest.plugin_id, manifest.management),
        "settings_mode": settings_mode(settings_ui=None,
            account_roles=manifest.account_roles, resource_roles=manifest.resource_roles,
            config_schema=config_schema)}
