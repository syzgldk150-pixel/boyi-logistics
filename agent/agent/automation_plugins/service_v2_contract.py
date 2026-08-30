"""Project a strict service-v2 manifest onto shared runtime primitives.

The projection is intentionally mechanical.  Business behaviour stays in the
ZIP payload; this module only translates declarative services, contributions
and capabilities into the existing generation, broker and Console contracts.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    HOST_CAPABILITY_API_VERSION,
    default_host_capability_registry,
    effect_rank,
    governance_for_effect,
)
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2


HOST_API_VERSION = "2.0.0"
SYSTEM_CAPABILITY_ROLE = "__system__"

_SUPPORTED_CAPABILITIES = frozenset(
    {
        "browser.session",
        "event.publish",
        "file.read",
        "file.write",
        "http.request",
        "service.invoke",
        "storage.collection",
        "storage.kv",
    }
)


def _thaw(value: Any) -> Any:
    """Detach nested frozen Manifest values without copying mapping proxies."""

    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _result_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string"},
            "data": {"type": "object", "additionalProperties": True},
            "meta": {"type": "object", "additionalProperties": True},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "error": {
                "oneOf": [
                    {"type": "object", "additionalProperties": True},
                    {"type": "null"},
                ]
            },
        },
        "required": ["status", "data", "meta", "warnings", "error"],
    }


@dataclass(frozen=True)
class ServiceV2ProjectContract:
    """Closed common contract used by lifecycle, Catalog and generations."""

    manifest: AutomationPluginManifestV2
    allowed_entrypoints: tuple[str, ...]
    default_entrypoints: tuple[str, ...]
    invocation_contracts: Mapping[str, Mapping[str, Any]]
    contribution_kinds: Mapping[str, str]
    account_roles: tuple[Mapping[str, Any], ...]
    resource_roles: tuple[Mapping[str, Any], ...]
    runtime_permissions: Mapping[str, Any]
    tool_contract: Mapping[str, Any]
    governance_anchor: Mapping[str, Any]
    scheduling: Mapping[str, Any]

    @classmethod
    def from_manifest(
        cls,
        manifest: AutomationPluginManifestV2,
    ) -> "ServiceV2ProjectContract":
        if not manifest.supports_host_api(HOST_API_VERSION):
            raise PluginManifestError(f"service-v2 plugin does not support Host API {HOST_API_VERSION}")
        capability_names = {str(item["name"]) for item in manifest.capabilities}
        unsupported = capability_names - _SUPPORTED_CAPABILITIES
        if unsupported:
            raise PluginManifestError(f"unsupported service-v2 capabilities: {sorted(unsupported)}")

        registry = default_host_capability_registry()
        provided_operation_effects: dict[tuple[str, str], CapabilityEffect] = {}
        for provided in manifest.provides:
            service = str(provided["service"])
            operations = provided.get("operations")
            if not isinstance(operations, tuple):
                raise PluginManifestError("provided service operations are invalid")
            for operation in operations:
                if not isinstance(operation, Mapping):
                    raise PluginManifestError("provided service operation is invalid")
                name = str(operation.get("name") or "")
                try:
                    effect = CapabilityEffect(str(operation.get("effect") or ""))
                except ValueError as exc:
                    raise PluginManifestError("provided service operation effect is invalid") from exc
                key = (service, name)
                if not name or key in provided_operation_effects:
                    raise PluginManifestError("provided service operation is ambiguous")
                provided_operation_effects[key] = effect

        entrypoints: list[str] = []
        defaults: list[str] = []
        invocations: dict[str, Mapping[str, Any]] = {}
        kinds: dict[str, str] = {}
        config_properties = manifest.config_schema.get("properties")
        if not isinstance(config_properties, Mapping):
            raise PluginManifestError("service-v2 config properties are invalid")
        template = {str(field): {"source": "project_config", "key": str(field)} for field in config_properties}
        input_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": _thaw(config_properties),
            "required": list(manifest.config_schema.get("required") or []),
        }
        for contribution_kind in (
            "console",
            "scheduler",
            "webhook",
            "feishu",
            "events",
        ):
            raw_items = manifest.contributes.get(contribution_kind)
            if not isinstance(raw_items, tuple):
                raise PluginManifestError(f"service-v2 contribution list is invalid: {contribution_kind}")
            for raw_item in raw_items:
                item = _thaw(raw_item)
                entrypoint_id = str(item["id"])
                entrypoints.append(entrypoint_id)
                kinds[entrypoint_id] = contribution_kind
                if item.get("default_enabled") is True:
                    defaults.append(entrypoint_id)
                try:
                    effect = provided_operation_effects[(str(item["service"]), str(item["operation"]))]
                except KeyError as exc:
                    raise PluginManifestError(
                        "contribution operation is absent from its provider effect contract"
                    ) from exc
                governance = governance_for_effect(effect).to_mapping()
                invocations[entrypoint_id] = {
                    "input_schema": copy.deepcopy(input_schema),
                    "service": str(item["service"]),
                    "operation": str(item["operation"]),
                    "contribution_kind": contribution_kind,
                    "argument_template": copy.deepcopy(template),
                    "dynamic_resolvers": {},
                    "effect": effect.value,
                    "governance": governance,
                }

        if not entrypoints:
            raise PluginManifestError("service-v2 plugin must contribute at least one entrypoint")

        account_roles = tuple(
            {
                **_thaw(item),
                "argument_field": None,
                "collection": False,
            }
            for item in manifest.account_roles
        )
        resource_roles = tuple(_thaw(item) for item in manifest.resource_roles)
        broker_operations: list[dict[str, Any]] = []
        seen_broker_operations: set[tuple[str, str]] = set()
        max_broker_calls = 0
        scheduler_contributed = bool(manifest.contributes.get("scheduler"))
        for capability in manifest.capabilities:
            name = str(capability["name"])
            account_role = capability.get("account_role")
            resource_role = capability.get("resource_role")
            if name == "service.invoke" and (
                account_role is not None or resource_role is not None
            ):
                raise PluginManifestError(
                    "service.invoke must use the Host-owned system role"
                )
            for operation in capability["operations"]:
                action = str(operation)
                identity = (name, action)
                if identity in seen_broker_operations:
                    raise PluginManifestError("duplicate Host capability operation")
                seen_broker_operations.add(identity)
                if name == "service.invoke":
                    # The target Provider owns the immutable operation effect.
                    # This static admission ceiling is deliberately protective;
                    # capability_proxy_v2 must resolve and enforce the exact
                    # Provider effect immediately before dispatch.
                    governance = governance_for_effect(CapabilityEffect.EXTERNAL_WRITE).to_mapping()
                    dynamic_effect = True
                    per_call_limit = 64
                else:
                    try:
                        descriptor = registry.resolve(
                            api_version=HOST_CAPABILITY_API_VERSION,
                            capability=name,
                            action=action,
                        )
                    except Exception as exc:
                        if getattr(exc, "code", None) == "CAPABILITY_UNAVAILABLE":
                            raise PluginManifestError("Host capability is unavailable") from exc
                        raise
                    governance = descriptor.governance.to_mapping()
                    dynamic_effect = False
                    if descriptor.requires_account_role:
                        if not account_role or resource_role is not None:
                            raise PluginManifestError(
                                "Host capability requires exactly one account role"
                            )
                    elif descriptor.requires_resource_role:
                        if not resource_role or account_role is not None:
                            raise PluginManifestError(
                                "Host capability requires exactly one resource role"
                            )
                    elif account_role is not None or resource_role is not None:
                        raise PluginManifestError(
                            "Host capability does not accept a bound role"
                        )
                    if scheduler_contributed and not descriptor.scheduler_allowed:
                        raise PluginManifestError(
                            "Host capability is unavailable to scheduler contributions"
                        )
                    per_call_limit = descriptor.per_call_limit
                bound_role = account_role or resource_role or SYSTEM_CAPABILITY_ROLE
                max_broker_calls += per_call_limit
                broker_operations.append(
                    {
                        "operation": name,
                        "action": action,
                        "roles": [str(bound_role)],
                        "effect": str(governance["effect"]),
                        "broker_effect": str(governance["broker_effect"]),
                        "governance": governance,
                        "dynamic_effect": dynamic_effect,
                    }
                )
        runtime_permissions = {
            "network": "http.request" in capability_names,
            "browser": "browser.session" in capability_names,
            "office": False,
            "file_roles": [name for name in ("file.read", "file.write") if name in capability_names],
            "broker_operations": broker_operations,
            "max_broker_calls": min(1000, max_broker_calls),
        }
        primary = copy.deepcopy(dict(next(iter(invocations.values()))))
        strictest = max(
            invocations.values(),
            key=lambda item: effect_rank(str(item["effect"])),
        )
        summary_governance = copy.deepcopy(dict(strictest["governance"]))
        mutating = str(summary_governance["broker_effect"]) == "write"
        tool_contract = {
            "name": f"service.{manifest.plugin_id}",
            "version": manifest.version,
            "description": manifest.description,
            "executor": manifest.runtime_entrypoint,
            "input_schema": input_schema,
            "output_schema": _result_schema(),
            "timeout": 3600,
            "heavy": True,
            "mutating": mutating,
            **summary_governance,
            "permissions": sorted(capability_names),
            "service": primary["service"],
            "operation": primary["operation"],
        }
        governance_fields = (
            "name",
            "version",
            "effect",
            "operation_type",
            "risk_level",
            "lock_class",
            "approval",
            "permissions",
            "idempotency",
            "retry",
            "evidence",
            "postconditions",
            "project_full_auto_allowed",
            "harness_allowed",
            "broker_effect",
        )
        governance_anchor = {field: copy.deepcopy(tool_contract[field]) for field in governance_fields}
        scheduler_items = manifest.contributes.get("scheduler")
        scheduling = {
            "supported": bool(scheduler_items),
            "allowed_kinds": ["daily_times", "startup"] if scheduler_items else [],
            "max_daily_times": 96 if scheduler_items else 0,
        }
        return cls(
            manifest=manifest,
            allowed_entrypoints=tuple(entrypoints),
            default_entrypoints=tuple(defaults),
            invocation_contracts=invocations,
            contribution_kinds=kinds,
            account_roles=account_roles,
            resource_roles=resource_roles,
            runtime_permissions=runtime_permissions,
            tool_contract=tool_contract,
            governance_anchor=governance_anchor,
            scheduling=scheduling,
        )

    @property
    def governance_anchor_sha256(self) -> str:
        from agent.automation_plugins.manifest_v2 import canonical_json_bytes

        import hashlib

        return hashlib.sha256(canonical_json_bytes(dict(self.governance_anchor))).hexdigest()


__all__ = [
    "HOST_API_VERSION",
    "SYSTEM_CAPABILITY_ROLE",
    "ServiceV2ProjectContract",
]
