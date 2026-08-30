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
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2


HOST_API_VERSION = "2.0.0"
SYSTEM_CAPABILITY_ROLE = "__system__"

_READ_OPERATION_PREFIXES = (
    "describe",
    "find",
    "get",
    "inspect",
    "list",
    "precheck",
    "query",
    "read",
    "resolve",
    "verify",
)
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


def _operation_effect(operation: str) -> str:
    normalized = str(operation or "").strip().lower()
    terminal_verb = normalized.rsplit(".", 1)[-1]
    return (
        "read"
        if normalized.startswith(_READ_OPERATION_PREFIXES)
        or terminal_verb.startswith(_READ_OPERATION_PREFIXES)
        else "write"
    )


def _capability_operation_effect(capability: str, operation: str) -> str:
    """Classify Host API calls without trusting service operation names.

    A Provider owns its service operation names, so names such as ``get_*``
    cannot establish that the Provider is read-only.  Until the immutable
    service contract carries an explicit effect, every cross-plugin call uses
    the protected-write path.
    """

    if capability == "service.invoke":
        return "write"
    return _operation_effect(operation)


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
            raise PluginManifestError(
                f"service-v2 plugin does not support Host API {HOST_API_VERSION}"
            )
        capability_names = {
            str(item["name"])
            for item in manifest.capabilities
        }
        unsupported = capability_names - _SUPPORTED_CAPABILITIES
        if unsupported:
            raise PluginManifestError(
                f"unsupported service-v2 capabilities: {sorted(unsupported)}"
            )

        entrypoints: list[str] = []
        defaults: list[str] = []
        invocations: dict[str, Mapping[str, Any]] = {}
        kinds: dict[str, str] = {}
        config_properties = manifest.config_schema.get("properties")
        if not isinstance(config_properties, Mapping):
            raise PluginManifestError("service-v2 config properties are invalid")
        template = {
            str(field): {"source": "project_config", "key": str(field)}
            for field in config_properties
        }
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
                raise PluginManifestError(
                    f"service-v2 contribution list is invalid: {contribution_kind}"
                )
            for raw_item in raw_items:
                item = _thaw(raw_item)
                entrypoint_id = str(item["id"])
                entrypoints.append(entrypoint_id)
                kinds[entrypoint_id] = contribution_kind
                if item.get("default_enabled") is True:
                    defaults.append(entrypoint_id)
                invocations[entrypoint_id] = {
                    "input_schema": copy.deepcopy(input_schema),
                    "service": str(item["service"]),
                    "operation": str(item["operation"]),
                    "contribution_kind": contribution_kind,
                    "argument_template": copy.deepcopy(template),
                    "dynamic_resolvers": {},
                }

        if not entrypoints:
            raise PluginManifestError(
                "service-v2 plugin must contribute at least one entrypoint"
            )

        account_roles = tuple(
            {
                **_thaw(item),
                "argument_field": None,
                "collection": False,
            }
            for item in manifest.account_roles
        )
        resource_roles = tuple(
            _thaw(item) for item in manifest.resource_roles
        )
        broker_operations: list[dict[str, Any]] = []
        for capability in manifest.capabilities:
            name = str(capability["name"])
            bound_role = (
                capability.get("account_role")
                or capability.get("resource_role")
                or SYSTEM_CAPABILITY_ROLE
            )
            for operation in capability["operations"]:
                broker_operations.append(
                    {
                        "operation": name,
                        "action": str(operation),
                        "roles": [str(bound_role)],
                        "effect": _capability_operation_effect(
                            name,
                            str(operation),
                        ),
                    }
                )
        runtime_permissions = {
            "network": "http.request" in capability_names,
            "browser": "browser.session" in capability_names,
            "office": False,
            "file_roles": [
                name for name in ("file.read", "file.write") if name in capability_names
            ],
            "broker_operations": broker_operations,
            "max_broker_calls": (
                min(1000, len(broker_operations) * 64) if broker_operations else 0
            ),
        }
        primary = copy.deepcopy(dict(next(iter(invocations.values()))))
        mutating = any(item["effect"] == "write" for item in broker_operations)
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
            "operation_type": "external_write" if mutating else "read",
            "risk_level": "high",
            "approval": {"mode": "project_policy"},
            "permissions": sorted(capability_names),
            "idempotency": {"required": True, "scope": "project_run"},
            "retry": {"max_attempts": 1},
            "evidence": {
                "required": True,
                "required_fields": ["service", "operation", "outcome"],
            },
            "postconditions": [{"name": "plugin_result_contract_valid"}],
            "project_full_auto_allowed": True,
            "service": primary["service"],
            "operation": primary["operation"],
        }
        governance_fields = (
            "name",
            "version",
            "operation_type",
            "risk_level",
            "approval",
            "permissions",
            "idempotency",
            "retry",
            "evidence",
            "postconditions",
            "project_full_auto_allowed",
        )
        governance_anchor = {
            field: copy.deepcopy(tool_contract[field]) for field in governance_fields
        }
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

        return hashlib.sha256(
            canonical_json_bytes(dict(self.governance_anchor))
        ).hexdigest()


__all__ = [
    "HOST_API_VERSION",
    "SYSTEM_CAPABILITY_ROLE",
    "ServiceV2ProjectContract",
]
