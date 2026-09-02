"""Validate and persist core-owned automation instance configuration."""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Mapping, Sequence

from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.code_owned_fields import (
    first_party_code_owned_config_fields,
    normalize_first_party_code_owned_config,
)
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.invocation import compile_instance_arguments
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    DeviceBinding,
    PluginRuntimeModel,
)
from agent.automation_plugins.ports import (
    AutomationProjectConfigurationPort,
    ProjectBindingResolverPort,
)
from agent.tool_registry import validate_schema_instance


def _closed_bindings(
    raw: Mapping[str, Any],
    declarations: Sequence[Mapping[str, Any]],
    *,
    kind: str,
) -> dict[str, Any]:
    declared = {str(item.get("role") or ""): item for item in declarations}
    if "" in declared:
        raise PluginConflictError(f"plugin {kind} role declaration is invalid")
    supplied = {str(key): value for key, value in raw.items()}
    unknown = set(supplied) - set(declared)
    missing = {
        role
        for role, declaration in declared.items()
        if declaration.get("required") is True and role not in supplied
    }
    if unknown or missing:
        raise PluginConflictError(
            f"{kind} bindings do not match signed roles; "
            f"unknown={sorted(unknown)}, missing={sorted(missing)}"
        )
    result: dict[str, Any] = {}
    argument_field_counts: dict[str, int] = {}
    if kind == "account":
        for declaration in declarations:
            field = str(declaration.get("argument_field") or "")
            argument_field_counts[field] = argument_field_counts.get(field, 0) + 1
    for role, value in supplied.items():
        declaration = declared[role]
        collection = (
            kind == "account"
            and declaration.get("collection") is True
            and (
                declaration.get("argument_field") is None
                or argument_field_counts.get(str(declaration.get("argument_field") or "")) == 1
            )
        )
        values = value if isinstance(value, (list, tuple)) else [value]
        if not collection and len(values) != 1:
            raise PluginConflictError(f"{kind} role accepts one identifier only")
        normalized_values = [str(item or "").strip() for item in values]
        if (
            not normalized_values
            or any(not item or len(item) > 128 for item in normalized_values)
            or len(normalized_values) != len(set(normalized_values))
        ):
            raise PluginConflictError(f"{kind} binding identifiers are invalid or duplicated")
        result[role] = tuple(normalized_values) if collection else normalized_values[0]
    return result


def normalize_project_schedule(
    raw: Mapping[str, Any],
    scheduling_capability: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate system-owned schedule settings; never read cron from a plugin."""

    value = dict(raw)
    if set(value) != {"kind", "times", "enabled"}:
        raise PluginConflictError("schedule fields must be kind, times and enabled")
    kind = value.get("kind")
    times = value.get("times")
    enabled = value.get("enabled")
    if kind not in {"none", "daily_times", "startup"} or not isinstance(enabled, bool):
        raise PluginConflictError("schedule kind/enabled is invalid")
    if not isinstance(times, list) or any(not isinstance(item, str) for item in times):
        raise PluginConflictError("schedule times must be an array of local HH:MM values")
    if kind == "none":
        if times or enabled:
            raise PluginConflictError("none schedule must be disabled with no times")
        return {"kind": "none", "times": [], "enabled": False}
    if scheduling_capability.get("supported") is not True:
        raise PluginConflictError("plugin action does not support schedules")
    allowed = scheduling_capability.get("allowed_kinds")
    if not isinstance(allowed, list) or kind not in allowed:
        raise PluginConflictError("schedule kind exceeds the signed plugin capability")
    if kind == "startup":
        if times:
            raise PluginConflictError("startup schedule cannot contain daily times")
        return {"kind": "startup", "times": [], "enabled": enabled}
    normalized = [str(item) for item in times]
    if (
        any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item) for item in normalized)
        or len(normalized) != len(set(normalized))
        or len(normalized) > int(scheduling_capability.get("max_daily_times") or 0)
        or (enabled and not normalized)
    ):
        raise PluginConflictError("daily schedule times are invalid, duplicated or exceed the limit")
    return {"kind": "daily_times", "times": sorted(normalized), "enabled": enabled}


class AutomationProjectConfigurationService:
    """Save one instance's config without exposing credentials to plugins."""

    def __init__(
        self,
        *,
        catalog: PluginCatalog,
        repository: AutomationProjectConfigurationPort,
        binding_resolver: ProjectBindingResolverPort,
    ) -> None:
        self._catalog = catalog
        self._repository = repository
        self._bindings = binding_resolver

    def read(self, automation_id: str) -> AutomationProjectConfigRecord:
        """Return the closed, credential-free configuration for migration only."""

        reader = getattr(self._repository, "get_project_config", None)
        if not callable(reader):
            raise PluginConflictError(
                "project configuration cannot be read for migration",
                code="PLUGIN_MIGRATION_CONFIGURATION_UNAVAILABLE",
            )
        record = reader(automation_id)
        if not isinstance(record, AutomationProjectConfigRecord) or not record.configured:
            raise PluginConflictError(
                "source project configuration is unavailable for migration",
                code="PLUGIN_MIGRATION_CONFIGURATION_UNAVAILABLE",
            )
        return record

    def save(
        self,
        automation_id: str,
        *,
        config: Mapping[str, Any],
        account_bindings: Mapping[str, Any],
        resource_bindings: Mapping[str, Any],
        enabled_entrypoints: Sequence[str],
        schedule: Mapping[str, Any],
        device_id: str | None,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_project_configuration_version: int,
    ) -> AutomationProjectConfigRecord:
        if not str(actor_id or "").strip():
            raise PluginConflictError("authenticated actor is required")
        if not str(actor_role or "").strip():
            raise PluginConflictError("authenticated actor role is required")
        try:
            uuid.UUID(str(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginConflictError("request_id must be UUID") from exc
        if (
            isinstance(expected_project_configuration_version, bool)
            or not isinstance(expected_project_configuration_version, int)
            or expected_project_configuration_version < 0
        ):
            raise PluginConflictError("expected_config_version must be a non-negative integer")
        if not isinstance(config, Mapping):
            raise PluginConflictError("project config must be an object")
        entry = self._catalog.require(automation_id)
        submitted_config = copy.deepcopy(dict(config))
        code_owned_fields = set(
            first_party_code_owned_config_fields(
                automation_id=entry.automation_id,
                plugin_id=entry.plugin_id,
                trust_source=entry.trust_source,
            )
        )
        submitted_code_owned_fields = code_owned_fields & set(submitted_config)
        if submitted_code_owned_fields:
            raise PluginConflictError(
                "project config contains code-owned fields: "
                + ", ".join(sorted(submitted_code_owned_fields)),
                code="PROJECT_CONFIG_CODE_OWNED_FIELD",
            )
        normalized_config = normalize_first_party_code_owned_config(
            automation_id=entry.automation_id,
            plugin_id=entry.plugin_id,
            trust_source=entry.trust_source,
            config=submitted_config,
        )
        validate_schema_instance(
            f"automation.{automation_id}.config",
            normalized_config,
            entry.config_schema,
        )
        accounts = _closed_bindings(
            account_bindings,
            entry.account_roles,
            kind="account",
        )
        resources = _closed_bindings(
            resource_bindings,
            entry.resource_roles,
            kind="resource",
        )
        for role_name, binding in accounts.items():
            role = next(item for item in entry.account_roles if item["role"] == role_name)
            account_ids = binding if isinstance(binding, tuple) else (binding,)
            for account_id in account_ids:
                self._bindings.validate_account_binding(
                    automation_id=automation_id,
                    role=role,
                    account_id=account_id,
                )
        for role_name, resource_id in resources.items():
            role = next(item for item in entry.resource_roles if item["role"] == role_name)
            self._bindings.validate_resource_binding(
                automation_id=automation_id,
                role=role,
                resource_id=resource_id,
            )
        requested_sources = tuple(
            str(item or "").strip() for item in enabled_entrypoints
        )
        mandatory_harness = tuple(
            source
            for source, contract in entry.invocation_contracts.items()
            if str(contract.get("contribution_kind") or "") == "harness"
        )
        sources = tuple(dict.fromkeys((*requested_sources, *mandatory_harness)))
        if any(not item for item in requested_sources) or len(requested_sources) != len(set(requested_sources)):
            raise PluginConflictError("enabled_entrypoints must be a unique list")
        if not set(sources) <= set(entry.allowed_entrypoints):
            raise PluginConflictError("enabled_entrypoints exceed the signed plugin contract")
        normalized_device_id = str(device_id or "").strip()
        device_binding: DeviceBinding | None = None
        worker_required = entry.worker_requirement.get("required") is True
        if worker_required:
            if not normalized_device_id:
                raise PluginConflictError("a named Worker device binding is required")
            device_binding = self._bindings.resolve_device_binding(
                automation_id=automation_id,
                device_id=normalized_device_id,
                worker_requirement=entry.worker_requirement,
            )
        elif normalized_device_id:
            raise PluginConflictError("server plugin instances cannot bind a desktop Worker")
        normalized_schedule = normalize_project_schedule(schedule, entry.scheduling)
        contract_witness = {
            "runtime_model": entry.runtime_model,
            "allowed_entrypoints": list(entry.allowed_entrypoints),
            "invocation_contracts": {
                source: copy.deepcopy(dict(contract))
                for source, contract in entry.invocation_contracts.items()
            },
            "scheduling": copy.deepcopy(dict(entry.scheduling)),
        }
        compiled_invocations: dict[str, dict[str, Any]] = {}
        for source in sources:
            compiled = compile_instance_arguments(
                entry,
                config=normalized_config,
                account_bindings=accounts,
                resource_bindings=resources,
                entrypoint=source,
                resolve_dynamic=False,
            )
            compiled_invocations[source] = {
                "arguments": copy.deepcopy(dict(compiled.arguments)),
                "dynamic_resolvers": copy.deepcopy(
                    dict(compiled.unresolved_dynamic_resolvers)
                ),
            }
            if entry.runtime_model == PluginRuntimeModel.SERVICE_V2.value:
                invocation_contract = entry.invocation_contracts[source]
                governance = invocation_contract.get("governance")
                if not isinstance(governance, Mapping):
                    raise PluginConflictError(
                        "service-v2 invocation governance is unavailable"
                    )
                compiled_invocations[source]["governance"] = copy.deepcopy(
                    dict(governance)
                )
                compiled_invocations[source]["target"] = {
                    "service": str(invocation_contract.get("service") or ""),
                    "operation": str(invocation_contract.get("operation") or ""),
                    "contribution_id": source,
                    "contribution_kind": str(
                        invocation_contract.get("contribution_kind") or ""
                    ),
                }
        return self._repository.save_project_config(
            automation_id,
            config=normalized_config,
            account_bindings=accounts,
            resource_bindings=resources,
            enabled_entrypoints=sources,
            schedule=normalized_schedule,
            compiled_invocations=compiled_invocations,
            contract_witness=contract_witness,
            device_binding=device_binding,
            actor_id=str(actor_id).strip(),
            actor_role=str(actor_role).strip(),
            request_id=str(request_id),
            expected_project_configuration_version=expected_project_configuration_version,
        )
