"""Project a strict service-v2 manifest onto shared runtime primitives.

The projection is intentionally mechanical.  Business behaviour stays in the
ZIP payload; this module only translates declarative services, contributions
and capabilities into the existing generation, broker and Console contracts.
"""

from __future__ import annotations

import copy
import re
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
from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_DRAFT_FIELDS,
    WAYBILL_ENTRY_DRAFT_MAX_LENGTHS,
    WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD,
    WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID,
    normalize_waybill_entry_slot,
)


HOST_API_VERSION = "2.0.0"
SYSTEM_CAPABILITY_ROLE = "__system__"
SERVICE_INVOKE_PER_CALL_LIMIT = 64
SELECTION_ARGUMENT_FIELDS = frozenset(
    {"dry_run", "selected_bill_codes", "preview_fingerprint"}
)
SELECTION_MAX_SELECTED_BILL_CODES = 250
SERVICE_V2_CONTRIBUTION_KINDS = (
    "console",
    "scheduler",
    "webhook",
    "feishu",
    "events",
    "harness",
    "module_slots",
)
_HARNESS_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
    "required": [],
}
_SELECTION_INPUT_PROPERTIES = {
    "dry_run": {"type": "boolean"},
    "selected_bill_codes": {
        "type": "array",
        "maxItems": SELECTION_MAX_SELECTED_BILL_CODES,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": 64},
    },
    "preview_fingerprint": {"type": "string", "maxLength": 64},
}
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")

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


def _waybill_entry_draft_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            field: {
                "type": "string",
                "maxLength": WAYBILL_ENTRY_DRAFT_MAX_LENGTHS[field],
            }
            for field in WAYBILL_ENTRY_DRAFT_FIELDS
        },
        "required": list(WAYBILL_ENTRY_DRAFT_FIELDS),
    }


def _selection_input_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(schema))
    properties = result.get("properties")
    if not isinstance(properties, dict):
        raise PluginManifestError("service-v2 selection input schema is invalid")
    if set(properties) & SELECTION_ARGUMENT_FIELDS:
        raise PluginManifestError(
            "service-v2 config collides with Host-owned selection arguments"
        )
    properties.update(copy.deepcopy(_SELECTION_INPUT_PROPERTIES))
    return result


def resolve_service_v2_selection_phase(arguments: Mapping[str, Any]) -> str:
    """Resolve the exact Host-owned selection phase from invocation arguments."""

    if not isinstance(arguments, Mapping) or not SELECTION_ARGUMENT_FIELDS <= set(
        arguments
    ):
        raise ValueError("selection arguments are incomplete")
    dry_run = arguments.get("dry_run")
    selected = arguments.get("selected_bill_codes")
    fingerprint = arguments.get("preview_fingerprint")
    if dry_run is True:
        if selected not in (None, []) or fingerprint not in (None, ""):
            raise ValueError("selection preview arguments are invalid")
        return "PREVIEW"
    if dry_run is False:
        if (
            not isinstance(selected, list)
            or not 1 <= len(selected) <= SELECTION_MAX_SELECTED_BILL_CODES
            or len(selected) != len(set(selected))
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                or len(item) > 64
                for item in selected
            )
            or not isinstance(fingerprint, str)
            or _HEX_SHA256.fullmatch(fingerprint) is None
        ):
            raise ValueError("selection execute arguments are invalid")
        return "FORMAL"
    raise ValueError("selection phase is invalid")


def resolve_service_v2_selection_target(
    runtime: Mapping[str, Any],
    *,
    contribution_id: str,
    contribution_kind: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, Any], str] | None:
    """Resolve a signed preview/execute target from committed runtime material."""

    contributions = runtime.get("contributions")
    declarations = (
        contributions.get(contribution_kind)
        if isinstance(contributions, Mapping)
        else None
    )
    matches = [
        item
        for item in declarations or ()
        if isinstance(item, Mapping)
        and str(item.get("id") or "") == contribution_id
    ]
    if len(matches) != 1:
        return None
    declaration = matches[0]
    preview_operation = declaration.get("selection_preview_operation")
    if preview_operation is None:
        return None
    if contribution_kind not in {"console", "feishu"}:
        raise ValueError("selection contribution kind is invalid")
    phase = resolve_service_v2_selection_phase(arguments)

    compiled_invocations = runtime.get("compiled_invocations")
    compiled = (
        compiled_invocations.get(contribution_id)
        if isinstance(compiled_invocations, Mapping)
        else None
    )
    execute_target = compiled.get("target") if isinstance(compiled, Mapping) else None
    service = str(declaration.get("service") or "")
    execute_operation = str(declaration.get("operation") or "")
    if (
        not isinstance(execute_target, Mapping)
        or execute_target.get("service") != service
        or execute_target.get("operation") != execute_operation
        or execute_target.get("contribution_id") != contribution_id
        or execute_target.get("contribution_kind") != contribution_kind
    ):
        raise ValueError("selection execute target drifted")

    contracts = runtime.get("service_contracts")
    provided = contracts.get("provides") if isinstance(contracts, Mapping) else None
    service_matches = [
        item
        for item in provided or ()
        if isinstance(item, Mapping) and item.get("service") == service
    ]
    operations = service_matches[0].get("operations") if len(service_matches) == 1 else None
    effects = {
        str(item.get("name") or ""): str(item.get("effect") or "")
        for item in operations or ()
        if isinstance(item, Mapping) and set(item) == {"name", "effect"}
    }
    if (
        not service
        or not isinstance(preview_operation, str)
        or not preview_operation
        or effects.get(preview_operation) != CapabilityEffect.READ.value
        or effects.get(execute_operation) != CapabilityEffect.EXTERNAL_WRITE.value
    ):
        raise ValueError("selection operation effects drifted")
    operation = preview_operation if phase == "PREVIEW" else execute_operation
    effect = (
        CapabilityEffect.READ
        if phase == "PREVIEW"
        else CapabilityEffect.EXTERNAL_WRITE
    )
    return (
        {
            "service": service,
            "operation": operation,
            "contribution_id": contribution_id,
            "contribution_kind": contribution_kind,
        },
        governance_for_effect(effect).to_mapping(),
        phase,
    )


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
        module_slots_contributed = bool(manifest.contributes.get("module_slots"))
        selection_contributed = any(
            isinstance(item, Mapping) and "selection_preview_operation" in item
            for kind in ("console", "feishu")
            for item in manifest.contributes.get(kind, ())
        )
        if module_slots_contributed and WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD in config_properties:
            raise PluginManifestError(
                "service-v2 config field collides with the Host-owned waybill argument"
            )
        tool_input_schema = copy.deepcopy(input_schema)
        if selection_contributed:
            tool_input_schema = _selection_input_schema(tool_input_schema)
        if module_slots_contributed:
            # The package-level tool schema is shared by mixed contribution
            # kinds, so the Host-owned occurrence value is optional here.  The
            # per-entrypoint module-slot schema below makes it mandatory and
            # binds it to the only code-owned resolver.
            tool_input_schema["properties"][WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD] = (
                _waybill_entry_draft_schema()
            )
        for contribution_kind in SERVICE_V2_CONTRIBUTION_KINDS:
            raw_items = manifest.contributes.get(contribution_kind, ())
            if not isinstance(raw_items, tuple):
                raise PluginManifestError(f"service-v2 contribution list is invalid: {contribution_kind}")
            for raw_item in raw_items:
                item = _thaw(raw_item)
                entrypoint_id = str(item["id"])
                entrypoints.append(entrypoint_id)
                kinds[entrypoint_id] = contribution_kind
                if item.get("default_enabled") is True:
                    defaults.append(entrypoint_id)
                elif contribution_kind == "harness":
                    # AI projection follows the instance lifecycle.  It has no
                    # separate user-facing entrypoint switch.
                    defaults.append(entrypoint_id)
                try:
                    effect = provided_operation_effects[(str(item["service"]), str(item["operation"]))]
                except KeyError as exc:
                    raise PluginManifestError(
                        "contribution operation is absent from its provider effect contract"
                    ) from exc
                invocation_input_schema = input_schema
                argument_template = template
                dynamic_resolvers: dict[str, str] = {}
                if "selection_preview_operation" in item:
                    invocation_input_schema = _selection_input_schema(input_schema)
                if contribution_kind == "harness":
                    declared_effect = item.get("effect")
                    if declared_effect != effect.value:
                        raise PluginManifestError(
                            "harness contribution effect must match the provided operation effect"
                        )
                    if effect not in {
                        CapabilityEffect.READ,
                        CapabilityEffect.COMPUTE,
                    }:
                        raise PluginManifestError(
                            "harness contribution only accepts read or compute effects"
                        )
                    harness_governance = governance_for_effect(effect).to_mapping()
                    if (
                        harness_governance["harness_allowed"] is not True
                        or harness_governance["broker_effect"] != "read"
                        or harness_governance["operation_type"] not in {"read", "compute"}
                    ):
                        raise PluginManifestError(
                            "harness contribution governance is not read-only"
                        )
                    # Model-supplied fields are described by the Harness
                    # declaration, while the subprocess still receives only
                    # the instance's server-owned configuration.  Harness
                    # arguments never replace project configuration.
                    invocation_input_schema = input_schema
                    argument_template = template
                if contribution_kind == "module_slots":
                    try:
                        normalize_waybill_entry_slot(item.get("slot"))
                    except ValueError as exc:
                        raise PluginManifestError(
                            "module-slot contribution slot is invalid"
                        ) from exc
                    if effect not in {
                        CapabilityEffect.READ,
                        CapabilityEffect.COMPUTE,
                    }:
                        raise PluginManifestError(
                            "module-slot contributions only accept read or compute effects"
                        )
                    module_governance = governance_for_effect(effect).to_mapping()
                    if (
                        module_governance["broker_effect"] != "read"
                        or module_governance["operation_type"] not in {"read", "compute"}
                    ):
                        raise PluginManifestError(
                            "module-slot contribution governance is not read-only"
                        )
                    invocation_input_schema = copy.deepcopy(input_schema)
                    invocation_input_schema["properties"][
                        WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD
                    ] = _waybill_entry_draft_schema()
                    invocation_input_schema["required"] = [
                        *invocation_input_schema["required"],
                        WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD,
                    ]
                    dynamic_resolvers = {
                        WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD: (
                            WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID
                        )
                    }
                governance = governance_for_effect(effect).to_mapping()
                invocation_contract = {
                    "input_schema": copy.deepcopy(invocation_input_schema),
                    "service": str(item["service"]),
                    "operation": str(item["operation"]),
                    "contribution_kind": contribution_kind,
                    "argument_template": copy.deepcopy(argument_template),
                    "dynamic_resolvers": dynamic_resolvers,
                    "effect": effect.value,
                    "governance": governance,
                }
                invocations[entrypoint_id] = invocation_contract

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
            action_call_limits = capability.get("action_call_limits")
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
                    per_call_limit = (
                        int(action_call_limits[action])
                        if isinstance(action_call_limits, Mapping)
                        else SERVICE_INVOKE_PER_CALL_LIMIT
                    )
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
                broker_operation = {
                    "operation": name,
                    "action": action,
                    "roles": [str(bound_role)],
                    "effect": str(governance["effect"]),
                    "broker_effect": str(governance["broker_effect"]),
                    "governance": governance,
                    "dynamic_effect": dynamic_effect,
                }
                if name == "service.invoke" and isinstance(
                    action_call_limits, Mapping
                ):
                    broker_operation["per_action_limit"] = per_call_limit
                broker_operations.append(broker_operation)
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
            "input_schema": tool_input_schema,
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
    "SELECTION_ARGUMENT_FIELDS",
    "SELECTION_MAX_SELECTED_BILL_CODES",
    "SERVICE_INVOKE_PER_CALL_LIMIT",
    "SERVICE_V2_CONTRIBUTION_KINDS",
    "SYSTEM_CAPABILITY_ROLE",
    "ServiceV2ProjectContract",
    "resolve_service_v2_selection_phase",
    "resolve_service_v2_selection_target",
]
