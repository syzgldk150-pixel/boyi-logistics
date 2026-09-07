"""Closed product ownership metadata shared by package and Console boundaries."""

from collections.abc import Mapping
from typing import Any


MODULE_DATASETS = {
    "finance": "finance.transactions",
    "customer_service": "customer_service.problems",
}
LEGACY_COLLECTOR_MODULES = {
    "sync_finance_bills": "finance",
    "sync_customer_service_problems": "customer_service",
}
MODULE_PATHS = {
    "automation": "/automations",
    "finance": "/modules/finance/data-sources",
    "customer_service": "/modules/customer-service/data-sources",
}


def validate_management(value: Any) -> dict[str, str]:
    """Validate a declared contract without guessing ownership from a title."""
    if not isinstance(value, Mapping) or set(value) != {"purpose", "module", "dataset", "version"}:
        raise ValueError("management requires purpose, module, dataset and version")
    result = dict(value)
    if any(not isinstance(item, str) for item in result.values()):
        raise ValueError("management fields must be strings")
    if result == {"purpose": "action", "module": "automation", "dataset": "", "version": ""}:
        return result
    if (
        result.get("purpose") != "collector"
        or result.get("module") not in MODULE_DATASETS
        or result.get("dataset") != MODULE_DATASETS[result["module"]]
        or result.get("version") != "1"
    ):
        raise ValueError("collector module or dataset contract is unsupported; core update required")
    return result


def management_for(plugin_id: str, declared: Any = None) -> dict[str, str]:
    """Explicit migration for the two existing collectors; other legacy packages are actions."""
    if declared is not None:
        return validate_management(declared)
    module = LEGACY_COLLECTOR_MODULES.get(plugin_id)
    if module is not None:
        return {"purpose": "collector", "module": module, "dataset": MODULE_DATASETS[module], "version": "1"}
    return {"purpose": "action", "module": "automation", "dataset": "", "version": ""}


def settings_mode(*, settings_ui: Any, account_roles: Any, resource_roles: Any, config_schema: Any,
                  resource_bindings: Mapping[str, Any] | None = None) -> str:
    if settings_ui is not None:
        return "custom"
    simple = simple_settings_schema(config_schema)
    # An existing instance can maintain scalar settings while preserving its
    # already-bound resources. Installation still needs a resource-capable UI
    # when required bindings are missing. Every save revalidates actual refs.
    missing_resource = any(role.get("required") is True and not (
        isinstance(resource_bindings, Mapping)
        and isinstance(resource_bindings.get(role.get("role")), str)
        and resource_bindings[role["role"]].strip()
    ) for role in resource_roles)
    if (config_schema.get("required") and not simple) or missing_resource:
        return "unavailable"
    return "accounts" if account_roles or (simple and config_schema.get("properties")) else "none"


def simple_settings_schema(schema: Any) -> bool:
    """Existing flat scalar settings fit native controls; nested workflows do not."""
    if not isinstance(schema, Mapping) or not isinstance(schema.get("properties"), Mapping):
        return False
    allowed = {"type", "title", "description", "enum", "format", "minimum", "maximum", "minLength", "maxLength"}
    return all(isinstance(field, Mapping) and not set(field) - allowed
        and field.get("type") in {"string", "integer", "number", "boolean"}
        and field.get("format") != "password" for field in schema["properties"].values())
