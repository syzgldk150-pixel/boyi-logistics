"""Deterministically materialize signed action templates for one instance."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from agent.automation_plugins.catalog import PluginCatalogEntry
from agent.automation_plugins.errors import PluginConflictError
from agent.tool_registry import validate_schema_instance


@runtime_checkable
class DynamicArgumentResolverPort(Protocol):
    def resolve(
        self,
        *,
        resolver_id: str,
        field: str,
        automation_id: str,
        entrypoint: str,
        context: Mapping[str, Any],
    ) -> Any:
        """Resolve one code-owned field or raise; never guess/default."""


@dataclass(frozen=True)
class CompiledInstanceArguments:
    arguments: Mapping[str, Any]
    account_bindings: Mapping[str, Any]
    resource_bindings: Mapping[str, str]
    unresolved_dynamic_resolvers: Mapping[str, str]


def _normalize_account_binding(value: Any, *, allow_many: bool) -> tuple[str, ...]:
    raw_values = value if isinstance(value, (list, tuple)) else (value,)
    if not allow_many and len(raw_values) != 1:
        raise PluginConflictError("account role requires exactly one account")
    normalized = tuple(str(item or "").strip() for item in raw_values)
    if (
        not normalized
        or any(not item or len(item) > 128 for item in normalized)
        or len(normalized) != len(set(normalized))
    ):
        raise PluginConflictError("account role binding is empty or duplicated")
    return normalized


def compile_instance_arguments(
    entry: PluginCatalogEntry,
    *,
    config: Mapping[str, Any],
    account_bindings: Mapping[str, Any],
    resource_bindings: Mapping[str, str],
    entrypoint: str,
    dynamic_resolver: DynamicArgumentResolverPort | None = None,
    dynamic_context: Mapping[str, Any] | None = None,
    resolve_dynamic: bool = True,
) -> CompiledInstanceArguments:
    """Compile action arguments from signed mappings and core-owned settings.

    ``resolve_dynamic=False`` returns the exact persisted schedule template and
    its resolver IDs. Occurrence execution calls again with a code-owned
    resolver and validates the complete core tool schema.
    """

    source = str(entrypoint or "").strip()
    contract = entry.invocation_contracts.get(source)
    if contract is None or source not in entry.allowed_entrypoints:
        raise PluginConflictError("entrypoint is not declared by the signed plugin")
    validate_schema_instance(
        f"automation.{entry.automation_id}.config",
        dict(config),
        entry.config_schema,
    )
    template = contract.get("argument_template")
    resolver_map = contract.get("dynamic_resolvers")
    if not isinstance(template, Mapping) or not isinstance(resolver_map, Mapping):
        raise PluginConflictError("signed invocation contract is invalid")
    arguments: dict[str, Any] = {}
    for field, raw_binding in template.items():
        if not isinstance(raw_binding, Mapping):
            raise PluginConflictError("signed argument binding is invalid")
        binding_source = raw_binding.get("source")
        if binding_source == "project_config":
            key = str(raw_binding.get("key") or "")
            if key in config:
                arguments[str(field)] = copy.deepcopy(config[key])
        elif binding_source == "literal":
            arguments[str(field)] = copy.deepcopy(raw_binding.get("value"))
        else:  # defensive: manifest parsing should already have rejected it
            raise PluginConflictError("signed argument binding source is unsupported")
    unresolved: dict[str, str] = {}
    for field, resolver_id in resolver_map.items():
        if not resolve_dynamic:
            unresolved[str(field)] = str(resolver_id)
            continue
        if dynamic_resolver is None:
            raise PluginConflictError("dynamic argument resolver is required")
        arguments[str(field)] = dynamic_resolver.resolve(
            resolver_id=str(resolver_id),
            field=str(field),
            automation_id=entry.automation_id,
            entrypoint=source,
            context=dict(dynamic_context or {}),
        )

    declared_roles = {str(role.get("role") or ""): role for role in entry.account_roles}
    if set(account_bindings) - set(declared_roles):
        raise PluginConflictError("account bindings contain an undeclared role")
    normalized_accounts: dict[str, Any] = {}
    for role in entry.account_roles:
        role_name = str(role.get("role") or "")
        if role_name not in account_bindings:
            if role.get("required") is True:
                raise PluginConflictError(f"required account role is unbound: {role_name}")
            continue
        if role.get("argument_field") is not None:
            raise PluginConflictError("plugin account roles must be broker-only")
        allow_many = role.get("collection") is True
        values = _normalize_account_binding(account_bindings[role_name], allow_many=allow_many)
        normalized_accounts[role_name] = list(values) if allow_many else values[0]

    declared_resources = {str(role.get("role") or ""): role for role in entry.resource_roles}
    if set(resource_bindings) - set(declared_resources):
        raise PluginConflictError("resource bindings contain an undeclared role")
    normalized_resources: dict[str, str] = {}
    for role_name, role in declared_resources.items():
        raw_value = resource_bindings.get(role_name)
        if raw_value is None:
            if role.get("required") is True:
                raise PluginConflictError(f"required resource role is unbound: {role_name}")
            continue
        resource_id = str(raw_value or "").strip()
        if not resource_id or len(resource_id) > 128:
            raise PluginConflictError("resource binding identifier is invalid")
        normalized_resources[role_name] = resource_id

    if resolve_dynamic:
        validate_schema_instance(
            str(entry.tool_contract.get("name") or entry.action_id),
            arguments,
            entry.tool_contract["input_schema"],
        )
    return CompiledInstanceArguments(
        arguments=arguments,
        account_bindings=normalized_accounts,
        resource_bindings=normalized_resources,
        unresolved_dynamic_resolvers=unresolved,
    )
