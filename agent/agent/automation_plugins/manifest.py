"""Strict action-package manifest reusable by independent project instances."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginManifestError


_AUTOMATION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_ALLOWED_ENTRYPOINTS = frozenset({"scheduler", "console", "feishu", "webhook"})
_ALLOWED_PLATFORMS = frozenset({"server", "windows"})
_ALLOWED_RUNTIME_KINDS = frozenset({"python_subprocess"})
_ALLOWED_BROKER_OPERATIONS = frozenset(
    {
        "browser.invoke",
        "office.invoke",
        "file.read",
        "file.write",
        "network.request",
        "projection.invoke",
        "ledger.invoke",
    }
)
GOVERNANCE_ANCHOR_FIELDS = frozenset(
    {
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
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "plugin_id",
        "name",
        "version",
        "description",
        "execution_platform",
        "runtime",
        "config_schema",
        "account_roles",
        "resource_roles",
        "scheduling",
        "allowed_entrypoints",
        "invocation_contracts",
        "governance_anchor",
        "tool_contract",
        "worker_requirement",
        "project_full_auto_allowed",
        "runtime_permissions",
    }
)


def canonical_json_bytes(value: Mapping[str, Any] | list[Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginManifestError("manifest must contain canonical JSON values") from exc


def _mapping(value: Any, path: str, fields: frozenset[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PluginManifestError(f"{path} must be an object")
    if fields is not None:
        unknown = set(value) - fields
        missing = fields - set(value)
        if unknown:
            raise PluginManifestError(f"{path} has unsupported fields: {sorted(unknown)}")
        if missing:
            raise PluginManifestError(f"{path} is missing fields: {sorted(missing)}")
    return value


def _non_empty_text(value: Any, path: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PluginManifestError(f"{path} must be a non-empty string no longer than {maximum}")
    return value.strip()


def governance_anchor_from_tool_contract(
    tool_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the closed core-governance fields from an action contract."""

    contract = _mapping(copy.deepcopy(dict(tool_contract)), "tool_contract")
    missing = GOVERNANCE_ANCHOR_FIELDS - set(contract)
    if missing:
        raise PluginManifestError(
            "tool_contract is missing governance fields: "
            + ", ".join(sorted(missing))
        )
    anchor = {
        field: copy.deepcopy(contract[field])
        for field in sorted(GOVERNANCE_ANCHOR_FIELDS)
    }
    canonical_json_bytes(anchor)
    return anchor


def _string_tuple(value: Any, path: str, *, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PluginManifestError(f"{path} must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise PluginManifestError(f"{path} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise PluginManifestError(f"{path} must not contain duplicates")
    if allowed is not None and not set(value) <= allowed:
        raise PluginManifestError(f"{path} contains unsupported values: {sorted(set(value) - allowed)}")
    return tuple(value)


def _validate_config_schema(value: Any) -> dict[str, Any]:
    schema = _mapping(value, "config_schema")
    if schema.get("type") != "object":
        raise PluginManifestError("config_schema.type must be object")
    if schema.get("additionalProperties") is not False:
        raise PluginManifestError("config_schema.additionalProperties must be false")
    if not isinstance(schema.get("properties", {}), dict):
        raise PluginManifestError("config_schema.properties must be an object")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise PluginManifestError("config_schema.required must be a string array")
    if not set(required) <= set(schema.get("properties", {})):
        raise PluginManifestError("config_schema.required contains an unknown property")
    return schema


def _validate_runtime(value: Any) -> dict[str, Any]:
    runtime = _mapping(value, "runtime")
    kind = runtime.get("kind")
    if kind not in _ALLOWED_RUNTIME_KINDS:
        raise PluginManifestError(f"runtime.kind must be one of {sorted(_ALLOWED_RUNTIME_KINDS)}")
    allowed = {"kind", "entrypoint", "requirements_lock"}
    if set(runtime) - allowed or "entrypoint" not in runtime:
        raise PluginManifestError("python_subprocess runtime has invalid fields")
    entrypoint = _non_empty_text(runtime["entrypoint"], "runtime.entrypoint", maximum=240)
    if not entrypoint.startswith("payload/") or not entrypoint.endswith(".py"):
        raise PluginManifestError("runtime.entrypoint must be a Python file below payload/")
    lock = runtime.get("requirements_lock")
    if lock is not None:
        lock = _non_empty_text(lock, "runtime.requirements_lock", maximum=240)
        if not lock.startswith("payload/") or not lock.endswith(".lock"):
            raise PluginManifestError("runtime.requirements_lock must be a .lock file below payload/")
    return runtime


def _validate_named_items(value: Any, path: str, *, allowed_fields: frozenset[str]) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise PluginManifestError(f"{path} must be an array")
    result: list[Mapping[str, Any]] = []
    identities: set[str] = set()
    identity_field = "role" if path == "account_roles" else "key"
    for index, raw in enumerate(value):
        item = _mapping(raw, f"{path}[{index}]")
        if set(item) != allowed_fields:
            raise PluginManifestError(f"{path}[{index}] must contain exactly {sorted(allowed_fields)}")
        identity = _non_empty_text(item.get(identity_field), f"{path}[{index}].{identity_field}", maximum=128)
        if identity in identities:
            raise PluginManifestError(f"{path} contains duplicate {identity_field}: {identity}")
        identities.add(identity)
        if not isinstance(item.get("required"), bool):
            raise PluginManifestError(f"{path}[{index}].required must be boolean")
        if "system" in item:
            _non_empty_text(item["system"], f"{path}[{index}].system", maximum=64)
        result.append(MappingProxyType(copy.deepcopy(item)))
    return tuple(result)


@dataclass(frozen=True)
class AutomationPluginManifest:
    schema_version: int
    plugin_id: str
    name: str
    version: str
    description: str
    execution_platform: str
    runtime: Mapping[str, Any]
    config_schema: Mapping[str, Any]
    account_roles: tuple[Mapping[str, Any], ...]
    resource_roles: tuple[Mapping[str, Any], ...]
    scheduling: Mapping[str, Any]
    allowed_entrypoints: tuple[str, ...]
    invocation_contracts: Mapping[str, Mapping[str, Any]]
    governance_anchor: Mapping[str, Any]
    tool_contract: Mapping[str, Any]
    worker_requirement: Mapping[str, Any]
    project_full_auto_allowed: bool
    runtime_permissions: Mapping[str, Any]
    _legacy_missing_effect_operations: frozenset[tuple[str, str]] = field(
        repr=False,
        compare=False,
    )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AutomationPluginManifest":
        data = _mapping(copy.deepcopy(dict(raw)), "manifest", _TOP_LEVEL_FIELDS)
        if data["schema_version"] != 1:
            raise PluginManifestError("schema_version must be 1")
        plugin_id = _non_empty_text(data["plugin_id"], "plugin_id", maximum=64)
        if not _AUTOMATION_ID_RE.fullmatch(plugin_id):
            raise PluginManifestError("plugin_id must be stable lower_snake_case")
        version = _non_empty_text(data["version"], "version", maximum=32)
        if not _SEMVER_RE.fullmatch(version):
            raise PluginManifestError("version must use MAJOR.MINOR.PATCH")
        platform = data["execution_platform"]
        if platform not in _ALLOWED_PLATFORMS:
            raise PluginManifestError(f"execution_platform must be one of {sorted(_ALLOWED_PLATFORMS)}")
        runtime = _validate_runtime(data["runtime"])
        config_schema = _validate_config_schema(data["config_schema"])
        governance_anchor = _mapping(
            data["governance_anchor"],
            "governance_anchor",
            GOVERNANCE_ANCHOR_FIELDS,
        )
        tool_contract = _mapping(data["tool_contract"], "tool_contract")
        tool_name = _non_empty_text(tool_contract.get("name"), "tool_contract.name", maximum=64)
        tool_version = _non_empty_text(tool_contract.get("version"), "tool_contract.version", maximum=32)
        if not _SEMVER_RE.fullmatch(tool_version):
            raise PluginManifestError("tool_contract.version must use MAJOR.MINOR.PATCH")
        if tool_contract.get("executor") != runtime["entrypoint"]:
            raise PluginManifestError("tool_contract.executor must match runtime.entrypoint")
        full_auto = data["project_full_auto_allowed"]
        if not isinstance(full_auto, bool):
            raise PluginManifestError("project_full_auto_allowed must be boolean")
        tool_full_auto = tool_contract.get("project_full_auto_allowed")
        if tool_full_auto is not True:
            tool_full_auto = False
        if full_auto != tool_full_auto:
            raise PluginManifestError(
                "manifest and signed tool contract must agree on project_full_auto_allowed"
            )
        anchor_full_auto = governance_anchor.get("project_full_auto_allowed")
        if not isinstance(anchor_full_auto, bool) or anchor_full_auto != full_auto:
            raise PluginManifestError(
                "manifest and governance anchor must agree on project_full_auto_allowed"
            )
        signed_governance = governance_anchor_from_tool_contract(tool_contract)
        if canonical_json_bytes(governance_anchor) != canonical_json_bytes(signed_governance):
            raise PluginManifestError(
                "signed action contract must match the closed governance anchor"
            )
        runtime_permissions = _mapping(data["runtime_permissions"], "runtime_permissions")
        if set(runtime_permissions) != {
            "network",
            "browser",
            "office",
            "file_roles",
            "broker_operations",
            "max_broker_calls",
        }:
            raise PluginManifestError("runtime_permissions fields are invalid")
        if any(not isinstance(runtime_permissions[name], bool) for name in ("network", "browser", "office")):
            raise PluginManifestError("runtime permission flags must be boolean")
        file_roles = runtime_permissions["file_roles"]
        if not isinstance(file_roles, list) or any(not isinstance(item, str) or not item for item in file_roles):
            raise PluginManifestError("runtime_permissions.file_roles must be a string array")
        if len(file_roles) != len(set(file_roles)):
            raise PluginManifestError("runtime_permissions.file_roles must not contain duplicates")
        broker_operations = runtime_permissions["broker_operations"]
        if not isinstance(broker_operations, list):
            raise PluginManifestError("runtime_permissions.broker_operations must be an array")
        max_broker_calls = runtime_permissions["max_broker_calls"]
        if (
            isinstance(max_broker_calls, bool)
            or not isinstance(max_broker_calls, int)
            or not 0 <= max_broker_calls <= 1000
        ):
            raise PluginManifestError("runtime_permissions.max_broker_calls must be from 0 to 1000")
        if runtime["kind"] == "python_subprocess":
            input_properties = tool_contract.get("input_schema", {}).get("properties", {})
            if any(
                str(name) == "account_id"
                or str(name) == "account_ids"
                or str(name).endswith(("_account_id", "_account_ids"))
                or any(token in str(name).lower() for token in ("password", "cookie", "credential", "secret", "token"))
                for name in input_properties
            ):
                raise PluginManifestError(
                    "plugin subprocess inputs cannot receive account IDs or credential material"
                )
        worker = _mapping(data["worker_requirement"], "worker_requirement")
        if set(worker) != {"required", "interactive_session", "supported_os", "queue_deadline_seconds"}:
            raise PluginManifestError("worker_requirement fields are invalid")
        if not isinstance(worker["required"], bool) or not isinstance(worker["interactive_session"], bool):
            raise PluginManifestError("worker requirement flags must be boolean")
        supported_os = _string_tuple(worker["supported_os"], "worker_requirement.supported_os")
        deadline = worker["queue_deadline_seconds"]
        if isinstance(deadline, bool) or not isinstance(deadline, int) or not 60 <= deadline <= 604800:
            raise PluginManifestError("worker queue_deadline_seconds must be from 60 to 604800")
        if platform == "windows" and (not worker["required"] or "windows" not in supported_os):
            raise PluginManifestError("windows plugins must require a Windows worker")
        if platform == "server" and worker["required"]:
            raise PluginManifestError("server plugins cannot require a desktop worker")
        account_roles_raw = data["account_roles"]
        if not isinstance(account_roles_raw, list):
            raise PluginManifestError("account_roles must be an array")
        account_roles: list[Mapping[str, Any]] = []
        seen_roles: set[str] = set()
        allowed_account_systems = frozenset({"ronghui", "yunda", "r7", "r13"})
        for index, raw_role in enumerate(account_roles_raw):
            role = _mapping(raw_role, f"account_roles[{index}]")
            if set(role) != {
                "role",
                "allowed_systems",
                "required",
                "argument_field",
                "collection",
            }:
                raise PluginManifestError("account role fields are invalid")
            role_name = _non_empty_text(role["role"], f"account_roles[{index}].role", maximum=128)
            if role_name in seen_roles:
                raise PluginManifestError(f"duplicate account role: {role_name}")
            seen_roles.add(role_name)
            systems = _string_tuple(
                role["allowed_systems"],
                f"account_roles[{index}].allowed_systems",
                allowed=allowed_account_systems,
            )
            if not isinstance(role["required"], bool):
                raise PluginManifestError("account role required must be boolean")
            if not isinstance(role["collection"], bool):
                raise PluginManifestError("account role collection must be boolean")
            if role["argument_field"] is not None:
                raise PluginManifestError("subprocess account roles must be broker-only")
            argument_field = None
            account_roles.append(
                MappingProxyType(
                    {
                        "role": role_name,
                        "allowed_systems": list(systems),
                        "required": role["required"],
                        "argument_field": argument_field,
                        "collection": role["collection"],
                    }
                )
            )
        account_fields = {
            str(name)
            for name in tool_contract.get("input_schema", {}).get("properties", {})
            if str(name) in {"account_id", "account_ids"}
            or str(name).endswith(("_account_id", "_account_ids"))
        }
        if account_fields:
            raise PluginManifestError("subprocess tool inputs cannot contain account arguments")
        resource_roles_raw = data["resource_roles"]
        if not isinstance(resource_roles_raw, list):
            raise PluginManifestError("resource_roles must be an array")
        resource_roles: list[Mapping[str, Any]] = []
        seen_resource_roles: set[str] = set()
        allowed_resource_kinds = frozenset(
            {
                "browser_session",
                "file",
                "office",
                "webhook_route",
                "feishu_route",
                "feishu_bitable",
                "feishu_sheet",
                "feishu_webhook",
                "feishu_resource",
            }
        )
        for index, raw_role in enumerate(resource_roles_raw):
            role = _mapping(raw_role, f"resource_roles[{index}]")
            if set(role) != {"role", "allowed_kinds", "required"}:
                raise PluginManifestError("resource role fields are invalid")
            role_name = _non_empty_text(role["role"], f"resource_roles[{index}].role", maximum=128)
            if role_name in seen_resource_roles:
                raise PluginManifestError(f"duplicate resource role: {role_name}")
            seen_resource_roles.add(role_name)
            kinds = _string_tuple(
                role["allowed_kinds"],
                f"resource_roles[{index}].allowed_kinds",
                allowed=allowed_resource_kinds,
            )
            if not isinstance(role["required"], bool):
                raise PluginManifestError("resource role required must be boolean")
            resource_roles.append(
                MappingProxyType(
                    {"role": role_name, "allowed_kinds": list(kinds), "required": role["required"]}
                )
            )
        if seen_roles & seen_resource_roles:
            raise PluginManifestError("account and resource role names must be globally unique")
        declared_broker_roles = seen_roles | seen_resource_roles
        normalized_broker_operations: list[dict[str, Any]] = []
        legacy_missing_effect_operations: set[tuple[str, str]] = set()
        seen_broker_operations: set[tuple[str, str]] = set()
        for index, raw_operation in enumerate(broker_operations):
            operation = _mapping(
                raw_operation,
                f"runtime_permissions.broker_operations[{index}]",
            )
            # Schema v1 packages existed before broker effects were explicit.
            # Their signed bytes stay untouched; only the in-memory runtime
            # projection conservatively treats a missing effect as a write.
            if set(operation) not in (
                {"operation", "action", "roles"},
                {"operation", "action", "roles", "effect"},
            ):
                raise PluginManifestError("broker operation fields are invalid")
            operation_name = _non_empty_text(
                operation["operation"],
                f"runtime_permissions.broker_operations[{index}].operation",
                maximum=64,
            )
            if operation_name not in _ALLOWED_BROKER_OPERATIONS:
                raise PluginManifestError("broker operation is unsupported")
            action = _non_empty_text(
                operation["action"],
                f"runtime_permissions.broker_operations[{index}].action",
                maximum=128,
            )
            if not re.fullmatch(r"^[a-z][a-z0-9_.-]{0,127}$", action):
                raise PluginManifestError("broker action must be a stable code-owned identifier")
            effect = operation.get("effect", "write")
            if effect not in {"read", "write"}:
                raise PluginManifestError("broker operation effect must be read or write")
            roles = _string_tuple(
                operation["roles"],
                f"runtime_permissions.broker_operations[{index}].roles",
            )
            if not set(roles) <= declared_broker_roles:
                raise PluginManifestError("broker operation references an undeclared role")
            identity = (operation_name, action)
            if identity in seen_broker_operations:
                raise PluginManifestError("duplicate broker operation/action contract")
            seen_broker_operations.add(identity)
            if "effect" not in operation:
                legacy_missing_effect_operations.add(identity)
            if operation_name.startswith("browser.") and runtime_permissions["browser"] is not True:
                raise PluginManifestError("browser broker operation requires browser permission")
            if operation_name.startswith("office.") and runtime_permissions["office"] is not True:
                raise PluginManifestError("Office broker operation requires office permission")
            if operation_name.startswith("network.") and runtime_permissions["network"] is not True:
                raise PluginManifestError("network broker operation requires network permission")
            if operation_name.startswith("file.") and not set(roles) <= set(file_roles):
                raise PluginManifestError("file broker operation roles must be signed file roles")
            normalized_broker_operations.append(
                {
                    "operation": operation_name,
                    "action": action,
                    "roles": list(roles),
                    "effect": effect,
                }
            )
        if bool(normalized_broker_operations) != (max_broker_calls > 0):
            raise PluginManifestError("broker operations and max_broker_calls must be enabled together")
        if set(file_roles) - seen_resource_roles:
            raise PluginManifestError("runtime file roles must reference declared resource roles")
        runtime_permissions["broker_operations"] = normalized_broker_operations
        allowed_entrypoints = _string_tuple(
            data["allowed_entrypoints"],
            "allowed_entrypoints",
            allowed=_ALLOWED_ENTRYPOINTS,
        )
        scheduling = _mapping(data["scheduling"], "scheduling")
        if set(scheduling) != {"supported", "allowed_kinds", "max_daily_times"}:
            raise PluginManifestError("scheduling fields are invalid")
        if not isinstance(scheduling["supported"], bool):
            raise PluginManifestError("scheduling.supported must be boolean")
        if scheduling["supported"] != ("scheduler" in allowed_entrypoints):
            raise PluginManifestError("scheduling.supported must match the scheduler entrypoint capability")
        allowed_kinds = scheduling["allowed_kinds"]
        if (
            not isinstance(allowed_kinds, list)
            or any(kind not in {"daily_times", "startup"} for kind in allowed_kinds)
            or len(allowed_kinds) != len(set(allowed_kinds))
        ):
            raise PluginManifestError("scheduling.allowed_kinds is invalid")
        max_daily_times = scheduling["max_daily_times"]
        if isinstance(max_daily_times, bool) or not isinstance(max_daily_times, int):
            raise PluginManifestError("scheduling.max_daily_times must be an integer")
        if scheduling["supported"]:
            if not allowed_kinds or not 1 <= max_daily_times <= 96:
                raise PluginManifestError("supported scheduling requires kinds and a bounded daily limit")
        elif allowed_kinds or max_daily_times != 0:
            raise PluginManifestError("unsupported scheduling cannot declare kinds or daily capacity")
        raw_invocation_contracts = _mapping(data["invocation_contracts"], "invocation_contracts")
        if set(raw_invocation_contracts) != set(allowed_entrypoints):
            raise PluginManifestError("invocation_contracts must cover allowed_entrypoints exactly")
        invocation_contracts: dict[str, Mapping[str, Any]] = {}
        for source in sorted(raw_invocation_contracts):
            contract = _mapping(raw_invocation_contracts[source], f"invocation_contracts.{source}")
            if set(contract) != {"input_schema", "argument_template", "dynamic_resolvers"}:
                raise PluginManifestError("invocation contract fields are invalid")
            input_schema = _validate_config_schema(contract["input_schema"])
            template = _mapping(contract["argument_template"], f"invocation_contracts.{source}.argument_template")
            unknown_template = set(template) - set(input_schema.get("properties", {}))
            if unknown_template:
                raise PluginManifestError("entrypoint template contains unknown input fields")
            if any("account_id" in str(key).lower() or "credential" in str(key).lower() for key in template):
                raise PluginManifestError("entrypoint templates cannot bind accounts or credentials")
            config_properties = config_schema.get("properties", {})
            input_properties = input_schema.get("properties", {})
            for field, field_schema in config_properties.items():
                if input_properties.get(field) != field_schema:
                    raise PluginManifestError("entrypoint schema drifted from the signed config schema")
            for field, raw_binding in template.items():
                binding = _mapping(
                    raw_binding,
                    f"invocation_contracts.{source}.argument_template.{field}",
                )
                binding_source = binding.get("source")
                if binding_source == "project_config":
                    if set(binding) != {"source", "key"} or binding.get("key") != field:
                        raise PluginManifestError("project_config template binding must reference its own field")
                    if field not in config_properties:
                        raise PluginManifestError("project_config template field is absent from config_schema")
                elif binding_source == "literal":
                    if set(binding) != {"source", "value"}:
                        raise PluginManifestError("literal template binding fields are invalid")
                    canonical_json_bytes({"value": binding.get("value")})
                else:
                    raise PluginManifestError("argument template source is unsupported")
            dynamic_resolvers = _mapping(
                contract["dynamic_resolvers"],
                f"invocation_contracts.{source}.dynamic_resolvers",
            )
            if set(dynamic_resolvers) - set(input_schema.get("properties", {})):
                raise PluginManifestError("dynamic resolver targets an unknown input field")
            if any(
                not isinstance(value, str) or not re.fullmatch(r"^[a-z][a-z0-9_.-]{0,127}$", value)
                for value in dynamic_resolvers.values()
            ):
                raise PluginManifestError("dynamic resolver names must be stable code-owned identifiers")
            if set(template) & set(dynamic_resolvers):
                raise PluginManifestError("an invocation field cannot be both templated and dynamic")
            if set(template) | set(dynamic_resolvers) != set(input_properties):
                raise PluginManifestError("every invocation field needs one closed value source")
            for field in config_properties:
                if field in dynamic_resolvers:
                    continue
                binding = template.get(field)
                if not isinstance(binding, dict) or binding.get("source") != "project_config":
                    raise PluginManifestError("every project config field must be bound explicitly")
            invocation_contracts[source] = MappingProxyType(
                {
                    "input_schema": copy.deepcopy(input_schema),
                    "argument_template": copy.deepcopy(template),
                    "dynamic_resolvers": copy.deepcopy(dynamic_resolvers),
                }
            )
        return cls(
            schema_version=1,
            plugin_id=plugin_id,
            name=_non_empty_text(data["name"], "name", maximum=120),
            version=version,
            description=_non_empty_text(data["description"], "description", maximum=1000),
            execution_platform=platform,
            runtime=MappingProxyType(copy.deepcopy(runtime)),
            config_schema=MappingProxyType(copy.deepcopy(config_schema)),
            account_roles=tuple(account_roles),
            resource_roles=tuple(resource_roles),
            scheduling=MappingProxyType(copy.deepcopy(scheduling)),
            allowed_entrypoints=allowed_entrypoints,
            invocation_contracts=MappingProxyType(invocation_contracts),
            governance_anchor=MappingProxyType(copy.deepcopy(governance_anchor)),
            tool_contract=MappingProxyType(copy.deepcopy(tool_contract)),
            worker_requirement=MappingProxyType(copy.deepcopy(worker)),
            project_full_auto_allowed=full_auto,
            runtime_permissions=MappingProxyType(copy.deepcopy(runtime_permissions)),
            _legacy_missing_effect_operations=frozenset(
                legacy_missing_effect_operations
            ),
        )

    def to_signed_mapping(self) -> dict[str, Any]:
        """Return the validated mapping whose canonical bytes were signed."""

        signed = self.to_mapping()
        for operation in signed["runtime_permissions"]["broker_operations"]:
            identity = (operation["operation"], operation["action"])
            if identity in self._legacy_missing_effect_operations:
                operation.pop("effect")
        return signed

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plugin_id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "execution_platform": self.execution_platform,
            "runtime": copy.deepcopy(dict(self.runtime)),
            "config_schema": copy.deepcopy(dict(self.config_schema)),
            "account_roles": [copy.deepcopy(dict(item)) for item in self.account_roles],
            "resource_roles": [copy.deepcopy(dict(item)) for item in self.resource_roles],
            "scheduling": copy.deepcopy(dict(self.scheduling)),
            "allowed_entrypoints": list(self.allowed_entrypoints),
            "invocation_contracts": {
                key: copy.deepcopy(dict(value))
                for key, value in sorted(self.invocation_contracts.items())
            },
            "governance_anchor": copy.deepcopy(dict(self.governance_anchor)),
            "tool_contract": copy.deepcopy(dict(self.tool_contract)),
            "worker_requirement": copy.deepcopy(dict(self.worker_requirement)),
            "project_full_auto_allowed": self.project_full_auto_allowed,
            "runtime_permissions": copy.deepcopy(dict(self.runtime_permissions)),
        }

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_signed_mapping())).hexdigest()

    @property
    def governance_anchor_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(dict(self.governance_anchor))).hexdigest()
