"""Pure projection of signed Host grants to a read-only invocation surface."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginConflictError, PluginExecutionError
from agent.automation_plugins.host_capability_registry import (
    HOST_CAPABILITY_API_VERSION,
    CapabilityEffect,
    default_host_capability_registry,
    governance_for_effect,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.service_v2_contract import SYSTEM_CAPABILITY_ROLE


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def project_read_runtime_permissions(value: object) -> dict[str, Any]:
    """Keep reviewed read/compute Host actions and effect-limited service calls.

    The package descriptor is the authority for requested capabilities. Host
    governance is the authority for their effects. Browser access here means
    named Host actions, never a browser object, raw network or credentials.
    """
    if not isinstance(value, Mapping) or set(value) != {
        "network", "browser", "office", "file_roles", "broker_operations", "max_broker_calls",
    }:
        raise PluginConflictError("harness runtime permissions are missing or not closed", code="PLUGIN_CONTRACT_INVALID")
    if any(type(value[name]) is not bool for name in ("network", "browser", "office")):
        raise PluginConflictError("harness runtime permission flags are invalid", code="PLUGIN_CONTRACT_INVALID")
    raw = _plain(value)
    operations, files, limit = raw["broker_operations"], raw["file_roles"], raw["max_broker_calls"]
    if (not isinstance(operations, list) or not isinstance(files, list)
            or any(not isinstance(name, str) or not name for name in files)
            or type(limit) is not int or not 0 <= limit <= 1000):
        raise PluginConflictError("harness runtime permissions are invalid", code="PLUGIN_CONTRACT_INVALID")

    def unavailable() -> None:
        raise PluginConflictError("harness runtime permissions expose an unsafe capability surface", code="CAPABILITY_UNAVAILABLE")

    registry = default_host_capability_registry()
    allowed = []
    seen = set()
    names = set()
    for operation in operations:
        fields = {"operation", "action", "roles", "effect", "broker_effect", "governance", "dynamic_effect"}
        if not isinstance(operation, Mapping) or set(operation) not in (fields, fields | {"per_action_limit"}):
            unavailable()
        name, action, roles = operation["operation"], operation["action"], operation["roles"]
        if (not isinstance(name, str) or not name or not isinstance(action, str) or not action
                or not isinstance(roles, list) or len(roles) != 1
                or not isinstance(roles[0], str) or not roles[0] or (name, action) in seen):
            unavailable()
        seen.add((name, action))
        names.add(name)
        if name == "service.invoke":
            expected = governance_for_effect(CapabilityEffect.EXTERNAL_WRITE)
            if operation["dynamic_effect"] is not True or roles != [SYSTEM_CAPABILITY_ROLE]:
                unavailable()
            if "per_action_limit" in operation and (
                type(operation["per_action_limit"]) is not int or not 1 <= operation["per_action_limit"] <= 1000
            ):
                unavailable()
            keep = True  # The exact caller effect ceiling is enforced at dispatch.
        else:
            if operation["dynamic_effect"] is not False or "per_action_limit" in operation:
                unavailable()
            try:
                descriptor = registry.resolve(api_version=HOST_CAPABILITY_API_VERSION, capability=name, action=action)
            except PluginExecutionError:
                unavailable()
            expected = descriptor.governance
            keep = expected.effect in {CapabilityEffect.READ, CapabilityEffect.COMPUTE}
        if (operation["effect"] != expected.effect.value or operation["broker_effect"] != expected.broker_effect
                or canonical_json_bytes(operation["governance"]) != canonical_json_bytes(expected.to_mapping())):
            unavailable()
        if keep:
            allowed.append(operation)
    if (raw["browser"] != ("browser.session" in names)
            or raw["network"] != ("http.request" in names) or raw["office"] is not False
            or files != [name for name in ("file.read", "file.write") if name in names]
            or operations and limit == 0):
        unavailable()
    read_names = {item["operation"] for item in allowed}
    return {
        "network": "http.request" in read_names,
        "browser": "browser.session" in read_names,
        "office": False,
        "file_roles": [name for name in ("file.read", "file.write") if name in read_names],
        "broker_operations": allowed,
        "max_broker_calls": limit if allowed else 0,
    }
