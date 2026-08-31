"""Pure, credential-free inspection projections for Service v2 packages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.automation_plugins.errors import PluginConflictError


def _thaw_json(value: Any) -> Any:
    """Copy a verified, recursively frozen JSON value into mutable containers."""

    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(child) for child in value]
    return value


def service_v2_wizard_projection(
    verified: Any,
) -> dict[str, Any]:
    """Return closed install-wizard material without raw package authority."""

    manifest = getattr(verified, "manifest", None)
    if manifest is None:
        raise PluginConflictError(
            "service-v2 inspection requires a verified package",
            code="PLUGIN_CONTRACT_INVALID",
        )
    contributions: list[dict[str, Any]] = []
    default_schedule: dict[str, Any] = {
        "kind": "none",
        "times": [],
        "enabled": False,
    }
    for kind in (
        "console",
        "scheduler",
        "webhook",
        "feishu",
        "events",
        "harness",
        "module_slots",
    ):
        raw_items = manifest.contributes.get(kind, ())
        if not isinstance(raw_items, (list, tuple)):
            raise PluginConflictError(
                "verified service-v2 contribution projection is invalid",
                code="PLUGIN_CONTRACT_INVALID",
            )
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise PluginConflictError(
                    "verified service-v2 contribution projection is invalid",
                    code="PLUGIN_CONTRACT_INVALID",
                )
            item = {
                "id": str(raw.get("id") or ""),
                "kind": kind,
                "title": str(raw.get("title") or ""),
            }
            if kind == "harness":
                # Keep the wizard projection safe: descriptive metadata and
                # the effect are useful to an administrator, while service /
                # operation remain server-owned invocation authority.
                item.update(
                    {
                        "description": str(raw.get("description") or ""),
                        "effect": str(raw.get("effect") or ""),
                    }
                )
            else:
                item["default_enabled"] = raw.get("default_enabled") is True
            # The manifest boundary already proved that an enabled default
            # Scheduler cron is representable by the Host daily-time model.
            if kind == "scheduler" and item["default_enabled"]:
                raw_schedule = raw.get("schedule")
                expression = (
                    raw_schedule.get("expression")
                    if isinstance(raw_schedule, Mapping)
                    else None
                )
                fields = expression.split() if isinstance(expression, str) else []
                if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
                    default_schedule = {
                        "kind": "daily_times",
                        "times": [f"{int(fields[1]):02d}:{int(fields[0]):02d}"],
                        "enabled": True,
                    }
            contributions.append(item)

    permissions: list[dict[str, Any]] = []
    for capability in manifest.capabilities:
        if not isinstance(capability, Mapping):
            raise PluginConflictError(
                "verified service-v2 permission projection is invalid",
                code="PLUGIN_CONTRACT_INVALID",
            )
        permission = {
            "name": str(capability.get("name") or ""),
            "operations": list(capability.get("operations") or ()),
            "account_role": capability.get("account_role"),
            "resource_role": capability.get("resource_role"),
        }
        action_call_limits = capability.get("action_call_limits")
        if isinstance(action_call_limits, Mapping):
            permission["action_call_limits"] = _thaw_json(action_call_limits)
        permissions.append(permission)
    return {
        "plugin_id": manifest.plugin_id,
        "name": manifest.name,
        "version": manifest.version,
        "host_api": {
            "minimum": str(manifest.host_api["minimum"]),
            "maximum_exclusive": str(manifest.host_api["maximum_exclusive"]),
        },
        "permissions": permissions,
        "account_roles": [_thaw_json(item) for item in manifest.account_roles],
        "resource_roles": [_thaw_json(item) for item in manifest.resource_roles],
        "config_schema": _thaw_json(manifest.config_schema),
        "contributions": sorted(
            contributions,
            key=lambda item: (item["kind"], item["id"]),
        ),
        "scheduling": {
            "supported": any(item["kind"] == "scheduler" for item in contributions),
            "default_schedule": default_schedule,
        },
    }


__all__ = ["service_v2_wizard_projection"]
