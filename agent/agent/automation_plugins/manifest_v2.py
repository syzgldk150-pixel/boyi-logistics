"""Closed manifest contract for dynamically installed service plugins.

Schema v2 is deliberately independent from the signed action-package schema.
It describes one Python 3.10 server plugin, the services it provides and
consumes, and the reversible host contributions that are mounted for an
automation project generation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_EXTENSION_SLOTS,
    normalize_waybill_entry_slot,
)


_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SERVICE_RE = re.compile(r"^plugin\.([a-z][a-z0-9_]{1,63})\.([a-z][a-z0-9_.-]{0,127})@(0|[1-9][0-9]*)$")
_CONNECTOR_SERVICE_RE = re.compile(
    r"^connector\.([a-z][a-z0-9_]{1,63})\.([a-z][a-z0-9_.-]{0,127})@(0|[1-9][0-9]*)$"
)
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ROUTE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "runtime_model",
        "plugin_id",
        "name",
        "version",
        "description",
        "host_api",
        "runtime",
        "provides",
        "requires",
        "capabilities",
        "account_roles",
        "resource_roles",
        "contributes",
        "config_schema",
        "storage",
    }
)
_HOST_API_FIELDS = frozenset({"minimum", "maximum_exclusive"})
_RUNTIME_FIELDS = frozenset(
    {
        "kind",
        "python",
        "mode",
        "entrypoint",
        "requirements_lock",
        "wheelhouse",
    }
)
_PROVIDE_FIELDS = frozenset({"service", "operations"})
_PROVIDE_OPERATION_FIELDS = frozenset({"name", "effect"})
_PLUGIN_REQUIRE_FIELDS = frozenset({"service"})
_CONNECTOR_REQUIRE_FIELDS = frozenset({"service", "account_role"})
_CAPABILITY_FIELDS = frozenset({"name", "operations", "account_role", "resource_role"})
_ACCOUNT_ROLE_FIELDS = frozenset({"role", "allowed_systems", "required"})
_RESOURCE_ROLE_FIELDS = frozenset({"role", "allowed_kinds", "required"})
_CONTRIBUTION_REQUIRED_FIELDS = frozenset(
    {"console", "scheduler", "webhook", "feishu", "events"}
)
_CONTRIBUTION_FIELDS = _CONTRIBUTION_REQUIRED_FIELDS | {"harness", "module_slots"}
_CONSOLE_FIELDS = frozenset({"id", "title", "service", "operation", "default_enabled"})
_MODULE_SLOT_FIELDS = frozenset(
    {"id", "slot", "title", "service", "operation", "default_enabled"}
)
_SCHEDULER_FIELDS = frozenset(
    {
        "id",
        "title",
        "service",
        "operation",
        "default_enabled",
        "schedule",
    }
)
_SCHEDULE_FIELDS = frozenset({"kind", "expression", "timezone"})
_WEBHOOK_FIELDS = frozenset({"id", "service", "operation", "method", "route", "default_enabled"})
_FEISHU_FIELDS = frozenset({"id", "service", "operation", "commands", "default_enabled"})
_EVENT_FIELDS = frozenset(
    {
        "id",
        "service",
        "operation",
        "event",
        "durable",
        "default_enabled",
    }
)
_HARNESS_FIELDS = frozenset(
    {"id", "title", "description", "service", "operation", "effect"}
)
_CONFIG_SCHEMA_FIELDS = frozenset({"type", "additionalProperties", "properties", "required"})
_STORAGE_FIELDS = frozenset({"kv", "collections"})
_COLLECTION_FIELDS = frozenset({"name", "fields", "indexes", "unique_constraints"})
_COLLECTION_FIELD_FIELDS = frozenset({"name", "type", "required"})
_INDEX_FIELDS = frozenset({"name", "fields"})
_COLLECTION_VALUE_TYPES = frozenset({"string", "integer", "number", "boolean", "datetime", "json"})
_SENSITIVE_CONFIG_TOKENS = (
    "password",
    "cookie",
    "credential",
    "secret",
    "token",
)
_CRON_FIELD_BOUNDS = (
    (0, 59, "minute"),
    (0, 23, "hour"),
    (1, 31, "day-of-month"),
    (1, 12, "month"),
    (0, 7, "day-of-week"),
)


def canonical_json_bytes(value: Mapping[str, Any] | list[Any]) -> bytes:
    """Return deterministic UTF-8 JSON bytes or fail on non-JSON values."""

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


def _mapping(
    value: Any,
    path: str,
    fields: frozenset[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PluginManifestError(f"{path} must be an object")
    result = copy.deepcopy(dict(value))
    if fields is not None:
        unknown = set(result) - fields
        missing = fields - set(result)
        if unknown:
            raise PluginManifestError(f"{path} has unsupported fields: {sorted(unknown)}")
        if missing:
            raise PluginManifestError(f"{path} is missing fields: {sorted(missing)}")
    return result


def _array(value: Any, path: str, *, non_empty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise PluginManifestError(f"{path} must be an array")
    if non_empty and not value:
        raise PluginManifestError(f"{path} must be a non-empty array")
    return copy.deepcopy(value)


def _text(value: Any, path: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PluginManifestError(f"{path} must be a non-empty string no longer than {maximum}")
    if value != value.strip():
        raise PluginManifestError(f"{path} must not contain surrounding whitespace")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise PluginManifestError(f"{path} must be boolean")
    return value


def _semver(value: Any, path: str) -> str:
    text = _text(value, path, maximum=32)
    if not _SEMVER_RE.fullmatch(text):
        raise PluginManifestError(f"{path} must use MAJOR.MINOR.PATCH")
    return text


def _semver_tuple(value: str) -> tuple[int, int, int]:
    match = _SEMVER_RE.fullmatch(value)
    if match is None:  # pragma: no cover - all public callers validate first
        raise ValueError(value)
    return tuple(int(part) for part in match.groups())


def _identifier(value: Any, path: str) -> str:
    text = _text(value, path, maximum=128)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise PluginManifestError(f"{path} must be a stable lower-case identifier")
    return text


def _validate_cron_atom(
    atom: str,
    *,
    minimum: int,
    maximum: int,
    path: str,
) -> None:
    source, separator, step_text = atom.partition("/")
    if separator:
        if not step_text.isdigit() or int(step_text) <= 0:
            raise PluginManifestError(f"{path} contains an invalid cron step")
    if source == "*":
        return
    bounds = source.split("-")
    if len(bounds) not in {1, 2} or any(not item.isdigit() for item in bounds):
        raise PluginManifestError(f"{path} contains an invalid cron field")
    values = [int(item) for item in bounds]
    if any(value < minimum or value > maximum for value in values):
        raise PluginManifestError(f"{path} contains an out-of-range cron value")
    if len(values) == 2 and values[0] > values[1]:
        raise PluginManifestError(f"{path} contains a reversed cron range")


def _validate_cron_expression(value: Any, path: str) -> str:
    expression = _text(value, path, maximum=120)
    fields = expression.split()
    if len(fields) != len(_CRON_FIELD_BOUNDS):
        raise PluginManifestError(f"{path} must contain five cron fields")
    for field, (minimum, maximum, name) in zip(
        fields,
        _CRON_FIELD_BOUNDS,
        strict=True,
    ):
        atoms = field.split(",")
        if any(not atom for atom in atoms):
            raise PluginManifestError(f"{path} contains an empty {name} value")
        for atom in atoms:
            _validate_cron_atom(
                atom,
                minimum=minimum,
                maximum=maximum,
                path=path,
            )
    return expression


def _validate_timezone(value: Any, path: str) -> str:
    timezone = _text(value, path, maximum=64)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PluginManifestError(f"{path} must be a valid IANA timezone") from exc
    return timezone


def _role(value: Any, path: str) -> str:
    text = _text(value, path, maximum=64)
    if not _ROLE_RE.fullmatch(text):
        raise PluginManifestError(f"{path} must be stable lower_snake_case")
    return text


def _string_array(
    value: Any,
    path: str,
    *,
    non_empty: bool = False,
    validator: Any | None = None,
) -> list[str]:
    raw = _array(value, path, non_empty=non_empty)
    result: list[str] = []
    for index, item in enumerate(raw):
        if validator is None:
            result.append(_text(item, f"{path}[{index}]", maximum=128))
        else:
            result.append(validator(item, f"{path}[{index}]"))
    if len(result) != len(set(result)):
        raise PluginManifestError(f"{path} must not contain duplicates")
    return result


def _service_name(value: Any, path: str) -> tuple[str, str, int]:
    service = _text(value, path, maximum=220)
    match = _SERVICE_RE.fullmatch(service)
    if match is None:
        raise PluginManifestError(f"{path} must use plugin.<plugin_id>.<service>@<major>")
    return service, match.group(1), int(match.group(3))


def _connector_service_name(value: Any, path: str) -> tuple[str, str, int]:
    service = _text(value, path, maximum=220)
    match = _CONNECTOR_SERVICE_RE.fullmatch(service)
    if match is None:
        raise PluginManifestError(f"{path} must use connector.<owner>.<service>@<major>")
    return service, match.group(1), int(match.group(3))


def _payload_path(value: Any, path: str, *, suffix: str) -> str:
    text = _text(value, path, maximum=240)
    if "\\" in text:
        raise PluginManifestError(f"{path} must use POSIX separators")
    candidate = PurePosixPath(text)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or candidate.parts[0] != "payload"
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.suffix != suffix
    ):
        raise PluginManifestError(f"{path} must be a {suffix} file below payload/")
    return candidate.as_posix()


def _wheel_path(value: Any, path: str) -> str:
    wheel = _payload_path(value, path, suffix=".whl")
    parts = PurePosixPath(wheel).parts
    if len(parts) < 3 or parts[:2] != ("payload", "wheelhouse"):
        raise PluginManifestError(f"{path} must be a wheel file below payload/wheelhouse/")
    return wheel


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return copy.deepcopy(value)


def _validate_config_property_names(schema: Mapping[str, Any], path: str) -> None:
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            lowered = str(name).lower()
            if (
                lowered in {"account_id", "account_ids"}
                or lowered.endswith(("_account_id", "_account_ids"))
                or any(token in lowered for token in _SENSITIVE_CONFIG_TOKENS)
            ):
                raise PluginManifestError(f"{path}.properties cannot declare account IDs or credential material")
            if isinstance(child, Mapping):
                _validate_config_property_names(child, f"{path}.properties.{name}")
    items = schema.get("items")
    if isinstance(items, Mapping):
        _validate_config_property_names(items, f"{path}.items")


def _validate_config_schema(value: Any) -> dict[str, Any]:
    schema = _mapping(value, "config_schema", _CONFIG_SCHEMA_FIELDS)
    if schema["type"] != "object":
        raise PluginManifestError("config_schema.type must be object")
    if schema["additionalProperties"] is not False:
        raise PluginManifestError("config_schema.additionalProperties must be false")
    if not isinstance(schema["properties"], Mapping):
        raise PluginManifestError("config_schema.properties must be an object")
    properties = copy.deepcopy(dict(schema["properties"]))
    if any(not isinstance(name, str) or not name for name in properties):
        raise PluginManifestError("config_schema property names must be non-empty strings")
    required = _string_array(schema["required"], "config_schema.required")
    if not set(required) <= set(properties):
        raise PluginManifestError("config_schema.required contains an unknown property")
    normalized = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }
    canonical_json_bytes(normalized)
    _validate_config_property_names(normalized, "config_schema")
    return normalized


def _validate_host_api(value: Any) -> dict[str, str]:
    raw = _mapping(value, "host_api", _HOST_API_FIELDS)
    minimum = _semver(raw["minimum"], "host_api.minimum")
    maximum = _semver(raw["maximum_exclusive"], "host_api.maximum_exclusive")
    if _semver_tuple(minimum) >= _semver_tuple(maximum):
        raise PluginManifestError("host_api.minimum must be lower than host_api.maximum_exclusive")
    return {"minimum": minimum, "maximum_exclusive": maximum}


def _validate_runtime(value: Any) -> dict[str, Any]:
    raw = _mapping(value, "runtime", _RUNTIME_FIELDS)
    if raw["kind"] != "python_subprocess":
        raise PluginManifestError("runtime.kind must be python_subprocess")
    if raw["python"] != "3.10":
        raise PluginManifestError("runtime.python must be 3.10")
    if raw["mode"] not in {"on_demand", "resident"}:
        raise PluginManifestError("runtime.mode must be on_demand or resident")
    entrypoint = _payload_path(
        raw["entrypoint"],
        "runtime.entrypoint",
        suffix=".py",
    )
    requirements_lock: str | None
    if raw["requirements_lock"] is None:
        requirements_lock = None
    else:
        requirements_lock = _payload_path(
            raw["requirements_lock"],
            "runtime.requirements_lock",
            suffix=".lock",
        )
    wheelhouse = _string_array(
        raw["wheelhouse"],
        "runtime.wheelhouse",
        validator=_wheel_path,
    )
    if wheelhouse and requirements_lock is None:
        raise PluginManifestError("runtime.wheelhouse requires runtime.requirements_lock")
    return {
        "kind": "python_subprocess",
        "python": "3.10",
        "mode": raw["mode"],
        "entrypoint": entrypoint,
        "requirements_lock": requirements_lock,
        "wheelhouse": wheelhouse,
    }


def _validate_provides(
    value: Any,
    *,
    plugin_id: str,
) -> tuple[list[dict[str, Any]], dict[str, frozenset[str]]]:
    raw_items = _array(value, "provides", non_empty=True)
    result: list[dict[str, Any]] = []
    operations_by_service: dict[str, frozenset[str]] = {}
    for index, raw_item in enumerate(raw_items):
        item = _mapping(raw_item, f"provides[{index}]", _PROVIDE_FIELDS)
        service, owner, _ = _service_name(
            item["service"],
            f"provides[{index}].service",
        )
        if owner != plugin_id:
            raise PluginManifestError("provided services must use the declaring plugin_id namespace")
        if service in operations_by_service:
            raise PluginManifestError(f"duplicate provided service: {service}")
        raw_operations = _array(
            item["operations"],
            f"provides[{index}].operations",
            non_empty=True,
        )
        operations: list[dict[str, str]] = []
        operation_names: set[str] = set()
        for operation_index, raw_operation in enumerate(raw_operations):
            operation = _mapping(
                raw_operation,
                f"provides[{index}].operations[{operation_index}]",
                _PROVIDE_OPERATION_FIELDS,
            )
            name = _identifier(
                operation["name"],
                f"provides[{index}].operations[{operation_index}].name",
            )
            if name in operation_names:
                raise PluginManifestError(f"provides[{index}].operations must not contain duplicate names")
            try:
                effect = CapabilityEffect(operation["effect"])
            except (TypeError, ValueError) as exc:
                raise PluginManifestError(f"provides[{index}].operations[{operation_index}].effect is invalid") from exc
            operation_names.add(name)
            operations.append({"name": name, "effect": effect.value})
        operations_by_service[service] = frozenset(operation_names)
        result.append({"service": service, "operations": operations})
    return result, operations_by_service


def _validate_requires(
    value: Any,
    *,
    provided_services: frozenset[str],
    account_roles: set[str],
    required_account_roles: frozenset[str],
) -> list[dict[str, str]]:
    raw_items = _array(value, "requires")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        path = f"requires[{index}]"
        item = _mapping(raw_item, path)
        service_value = item.get("service")
        service = _text(service_value, f"{path}.service", maximum=220)
        if _SERVICE_RE.fullmatch(service):
            if set(item) != _PLUGIN_REQUIRE_FIELDS:
                raise PluginManifestError(
                    f"{path} plugin service requirement must contain exactly service"
                )
            service, _, _ = _service_name(service, f"{path}.service")
            normalized = {"service": service}
            if service in provided_services:
                raise PluginManifestError("a plugin cannot require a service it provides")
        elif _CONNECTOR_SERVICE_RE.fullmatch(service):
            if set(item) != _CONNECTOR_REQUIRE_FIELDS:
                raise PluginManifestError(
                    f"{path} connector service requirement must contain exactly service and account_role"
                )
            service, _, _ = _connector_service_name(service, f"{path}.service")
            account_role = _role(item["account_role"], f"{path}.account_role")
            if account_role not in account_roles:
                raise PluginManifestError(
                    f"{path}.account_role references an undeclared account role"
                )
            if account_role not in required_account_roles:
                raise PluginManifestError(
                    f"{path}.account_role must reference an account role with required=true"
                )
            normalized = {"service": service, "account_role": account_role}
        else:
            raise PluginManifestError(
                f"{path}.service must use plugin.<plugin_id>.<service>@<major> "
                "or connector.<owner>.<service>@<major>"
            )
        if service in seen:
            raise PluginManifestError(f"duplicate required service: {service}")
        seen.add(service)
        result.append(normalized)
    return result


def _validate_account_roles(value: Any) -> tuple[list[dict[str, Any]], set[str]]:
    raw_items = _array(value, "account_roles")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = _mapping(
            raw_item,
            f"account_roles[{index}]",
            _ACCOUNT_ROLE_FIELDS,
        )
        role = _role(item["role"], f"account_roles[{index}].role")
        if role in seen:
            raise PluginManifestError(f"duplicate account role: {role}")
        seen.add(role)
        systems = _string_array(
            item["allowed_systems"],
            f"account_roles[{index}].allowed_systems",
            non_empty=True,
            validator=_identifier,
        )
        result.append(
            {
                "role": role,
                "allowed_systems": systems,
                "required": _boolean(
                    item["required"],
                    f"account_roles[{index}].required",
                ),
            }
        )
    return result, seen


def _validate_resource_roles(value: Any) -> tuple[list[dict[str, Any]], set[str]]:
    raw_items = _array(value, "resource_roles")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = _mapping(
            raw_item,
            f"resource_roles[{index}]",
            _RESOURCE_ROLE_FIELDS,
        )
        role = _role(item["role"], f"resource_roles[{index}].role")
        if role in seen:
            raise PluginManifestError(f"duplicate resource role: {role}")
        seen.add(role)
        kinds = _string_array(
            item["allowed_kinds"],
            f"resource_roles[{index}].allowed_kinds",
            non_empty=True,
            validator=_identifier,
        )
        result.append(
            {
                "role": role,
                "allowed_kinds": kinds,
                "required": _boolean(
                    item["required"],
                    f"resource_roles[{index}].required",
                ),
            }
        )
    return result, seen


def _validate_capabilities(
    value: Any,
    *,
    account_roles: set[str],
    resource_roles: set[str],
    storage: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    raw_items = _array(value, "capabilities")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for index, raw_item in enumerate(raw_items):
        item = _mapping(
            raw_item,
            f"capabilities[{index}]",
            _CAPABILITY_FIELDS,
        )
        name = _identifier(item["name"], f"capabilities[{index}].name")
        operations = _string_array(
            item["operations"],
            f"capabilities[{index}].operations",
            non_empty=True,
            validator=_identifier,
        )
        account_role = item["account_role"]
        if account_role is not None:
            account_role = _role(
                account_role,
                f"capabilities[{index}].account_role",
            )
            if account_role not in account_roles:
                raise PluginManifestError("capability references an undeclared account role")
        resource_role = item["resource_role"]
        if resource_role is not None:
            resource_role = _role(
                resource_role,
                f"capabilities[{index}].resource_role",
            )
            if resource_role not in resource_roles:
                raise PluginManifestError("capability references an undeclared resource role")
        identity = (name, account_role, resource_role)
        if identity in seen:
            raise PluginManifestError("duplicate capability binding")
        seen.add(identity)
        result.append(
            {
                "name": name,
                "operations": operations,
                "account_role": account_role,
                "resource_role": resource_role,
            }
        )
    if storage is not None:
        names = {item["name"] for item in result}
        if "storage.kv" in names and storage.get("kv") is not True:
            raise PluginManifestError("storage.kv capability requires storage.kv=true")
        if "storage.collection" in names and not storage.get("collections"):
            raise PluginManifestError("storage.collection capability requires declared collections")
    return result


def _contribution_target(
    item: Mapping[str, Any],
    *,
    path: str,
    operations_by_service: Mapping[str, frozenset[str]],
) -> tuple[str, str]:
    service, _, _ = _service_name(item["service"], f"{path}.service")
    operation = _identifier(item["operation"], f"{path}.operation")
    if service not in operations_by_service:
        raise PluginManifestError("contributions must target a provided service")
    if operation not in operations_by_service[service]:
        raise PluginManifestError("contribution operation is absent from the provided service")
    return service, operation


def _contribution_id(
    value: Any,
    path: str,
    *,
    seen: set[str],
) -> str:
    contribution_id = _role(value, path)
    if contribution_id in seen:
        raise PluginManifestError(f"duplicate contribution id: {contribution_id}")
    seen.add(contribution_id)
    return contribution_id


def _validate_contributes(
    value: Any,
    *,
    operations_by_service: Mapping[str, frozenset[str]],
    provided_operation_effects: Mapping[tuple[str, str], CapabilityEffect],
) -> dict[str, Any]:
    raw = _mapping(value, "contributes")
    unknown = set(raw) - _CONTRIBUTION_FIELDS
    missing = _CONTRIBUTION_REQUIRED_FIELDS - set(raw)
    if unknown:
        raise PluginManifestError(
            f"contributes has unsupported fields: {sorted(unknown)}"
        )
    if missing:
        raise PluginManifestError(
            f"contributes is missing fields: {sorted(missing)}"
        )
    seen_ids: set[str] = set()
    result: dict[str, list[dict[str, Any]]] = {
        "console": [],
        "scheduler": [],
        "webhook": [],
        "feishu": [],
        "events": [],
    }
    if "harness" in raw:
        result["harness"] = []
    if "module_slots" in raw:
        result["module_slots"] = []

    for index, raw_item in enumerate(_array(raw["console"], "contributes.console")):
        path = f"contributes.console[{index}]"
        item = _mapping(raw_item, path, _CONSOLE_FIELDS)
        service, operation = _contribution_target(
            item,
            path=path,
            operations_by_service=operations_by_service,
        )
        result["console"].append(
            {
                "id": _contribution_id(item["id"], f"{path}.id", seen=seen_ids),
                "title": _text(item["title"], f"{path}.title", maximum=120),
                "service": service,
                "operation": operation,
                "default_enabled": _boolean(
                    item["default_enabled"],
                    f"{path}.default_enabled",
                ),
            }
        )

    for index, raw_item in enumerate(_array(raw["scheduler"], "contributes.scheduler")):
        path = f"contributes.scheduler[{index}]"
        item = _mapping(raw_item, path, _SCHEDULER_FIELDS)
        service, operation = _contribution_target(
            item,
            path=path,
            operations_by_service=operations_by_service,
        )
        schedule = _mapping(item["schedule"], f"{path}.schedule", _SCHEDULE_FIELDS)
        if schedule["kind"] != "cron":
            raise PluginManifestError(f"{path}.schedule.kind must be cron")
        expression = _validate_cron_expression(
            schedule["expression"],
            f"{path}.schedule.expression",
        )
        timezone = _validate_timezone(
            schedule["timezone"],
            f"{path}.schedule.timezone",
        )
        result["scheduler"].append(
            {
                "id": _contribution_id(item["id"], f"{path}.id", seen=seen_ids),
                "title": _text(item["title"], f"{path}.title", maximum=120),
                "service": service,
                "operation": operation,
                "default_enabled": _boolean(
                    item["default_enabled"],
                    f"{path}.default_enabled",
                ),
                "schedule": {
                    "kind": "cron",
                    "expression": expression,
                    "timezone": timezone,
                },
            }
        )

    for index, raw_item in enumerate(_array(raw["webhook"], "contributes.webhook")):
        path = f"contributes.webhook[{index}]"
        item = _mapping(raw_item, path, _WEBHOOK_FIELDS)
        service, operation = _contribution_target(
            item,
            path=path,
            operations_by_service=operations_by_service,
        )
        if item["method"] != "POST":
            raise PluginManifestError(f"{path}.method must be POST")
        route = _text(item["route"], f"{path}.route", maximum=64)
        if not _ROUTE_RE.fullmatch(route):
            raise PluginManifestError(f"{path}.route must be a stable route segment")
        result["webhook"].append(
            {
                "id": _contribution_id(item["id"], f"{path}.id", seen=seen_ids),
                "service": service,
                "operation": operation,
                "method": "POST",
                "route": route,
                "default_enabled": _boolean(
                    item["default_enabled"],
                    f"{path}.default_enabled",
                ),
            }
        )

    for index, raw_item in enumerate(_array(raw["feishu"], "contributes.feishu")):
        path = f"contributes.feishu[{index}]"
        item = _mapping(raw_item, path, _FEISHU_FIELDS)
        service, operation = _contribution_target(
            item,
            path=path,
            operations_by_service=operations_by_service,
        )
        commands = _string_array(
            item["commands"],
            f"{path}.commands",
            non_empty=True,
        )
        result["feishu"].append(
            {
                "id": _contribution_id(item["id"], f"{path}.id", seen=seen_ids),
                "service": service,
                "operation": operation,
                "commands": commands,
                "default_enabled": _boolean(
                    item["default_enabled"],
                    f"{path}.default_enabled",
                ),
            }
        )

    for index, raw_item in enumerate(_array(raw["events"], "contributes.events")):
        path = f"contributes.events[{index}]"
        item = _mapping(raw_item, path, _EVENT_FIELDS)
        service, operation = _contribution_target(
            item,
            path=path,
            operations_by_service=operations_by_service,
        )
        event = _text(item["event"], f"{path}.event", maximum=128)
        if not _EVENT_RE.fullmatch(event):
            raise PluginManifestError(f"{path}.event must be a stable event name")
        result["events"].append(
            {
                "id": _contribution_id(item["id"], f"{path}.id", seen=seen_ids),
                "service": service,
                "operation": operation,
                "event": event,
                "durable": _boolean(item["durable"], f"{path}.durable"),
                "default_enabled": _boolean(
                    item["default_enabled"],
                    f"{path}.default_enabled",
                ),
            }
        )

    for index, raw_item in enumerate(
        _array(raw.get("harness", []), "contributes.harness")
    ):
        path = f"contributes.harness[{index}]"
        item = _mapping(raw_item, path, _HARNESS_FIELDS)
        service, operation = _contribution_target(
            item,
            path=path,
            operations_by_service=operations_by_service,
        )
        try:
            declared_effect = CapabilityEffect(item["effect"])
        except (TypeError, ValueError) as exc:
            raise PluginManifestError(
                f"{path}.effect must be read or compute"
            ) from exc
        if declared_effect not in {
            CapabilityEffect.READ,
            CapabilityEffect.COMPUTE,
        }:
            raise PluginManifestError(f"{path}.effect must be read or compute")
        authoritative_effect = provided_operation_effects.get((service, operation))
        if authoritative_effect is None:
            # The target check above should make this unreachable, but keep the
            # failure explicit if a future parser changes that lookup.
            raise PluginManifestError(
                f"{path} targets an operation without an authoritative effect"
            )
        if declared_effect is not authoritative_effect:
            raise PluginManifestError(
                f"{path}.effect must match the provided operation effect"
            )
        result["harness"].append(
            {
                "id": _contribution_id(item["id"], f"{path}.id", seen=seen_ids),
                "title": _text(item["title"], f"{path}.title", maximum=120),
                "description": _text(
                    item["description"],
                    f"{path}.description",
                    maximum=500,
                ),
                "service": service,
                "operation": operation,
                # This is a redundant declaration.  ServiceV2ProjectContract
                # derives governance from the Provider effect, never here.
                "effect": declared_effect.value,
            }
        )

    for index, raw_item in enumerate(
        _array(raw.get("module_slots", []), "contributes.module_slots")
    ):
        path = f"contributes.module_slots[{index}]"
        item = _mapping(raw_item, path, _MODULE_SLOT_FIELDS)
        service, operation = _contribution_target(
            item,
            path=path,
            operations_by_service=operations_by_service,
        )
        try:
            slot = normalize_waybill_entry_slot(item["slot"])
        except ValueError as exc:
            allowed = ", ".join(WAYBILL_ENTRY_EXTENSION_SLOTS)
            raise PluginManifestError(
                f"{path}.slot must be one of: {allowed}"
            ) from exc
        authoritative_effect = provided_operation_effects.get((service, operation))
        if authoritative_effect not in {
            CapabilityEffect.READ,
            CapabilityEffect.COMPUTE,
        }:
            raise PluginManifestError(
                f"{path} must target a read or compute Provider operation"
            )
        result["module_slots"].append(
            {
                "id": _contribution_id(item["id"], f"{path}.id", seen=seen_ids),
                "slot": slot,
                "title": _text(item["title"], f"{path}.title", maximum=120),
                "service": service,
                "operation": operation,
                "default_enabled": _boolean(
                    item["default_enabled"],
                    f"{path}.default_enabled",
                ),
            }
        )
    return result


def _validate_default_scheduler_host_compatibility(
    contributes: Mapping[str, Any],
) -> None:
    """Reject default schedules the current daily-time host cannot install.

    This belongs to manifest technical validation, before lifecycle persistence:
    accepting a valid-but-unmappable default cron would otherwise create a
    project that the installation service can only fail to configure later.
    Non-default scheduler contributions remain declarative; an administrator
    may configure a supported host schedule explicitly.
    """

    schedulers = contributes.get("scheduler")
    if not isinstance(schedulers, list):
        raise PluginManifestError("contributes.scheduler must be an array")
    defaults = [item for item in schedulers if item.get("default_enabled") is True]
    if len(defaults) > 1:
        raise PluginManifestError("at most one default scheduler is supported by this host")
    if not defaults:
        return
    schedule = defaults[0].get("schedule")
    if not isinstance(schedule, Mapping):
        raise PluginManifestError("default scheduler schedule is invalid")
    expression = schedule.get("expression")
    fields = expression.split() if isinstance(expression, str) else []
    if (
        schedule.get("timezone") != "Asia/Shanghai"
        or len(fields) != 5
        or fields[2:] != ["*", "*", "*"]
        or not fields[0].isdigit()
        or not fields[1].isdigit()
    ):
        raise PluginManifestError("default scheduler must be Asia/Shanghai fixed minute hour * * *")
    minute, hour = int(fields[0]), int(fields[1])
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise PluginManifestError("default scheduler minute/hour is out of range")


def _validate_field_references(
    value: Any,
    *,
    path: str,
    declared_fields: set[str],
) -> list[str]:
    fields = _string_array(value, path, non_empty=True, validator=_role)
    if not set(fields) <= declared_fields:
        raise PluginManifestError(f"{path} references an undeclared collection field")
    return fields


def _validate_storage(value: Any) -> dict[str, Any]:
    raw = _mapping(value, "storage", _STORAGE_FIELDS)
    kv = _boolean(raw["kv"], "storage.kv")
    raw_collections = _array(raw["collections"], "storage.collections")
    collections: list[dict[str, Any]] = []
    seen_collections: set[str] = set()
    for collection_index, raw_collection in enumerate(raw_collections):
        path = f"storage.collections[{collection_index}]"
        collection = _mapping(raw_collection, path, _COLLECTION_FIELDS)
        name = _role(collection["name"], f"{path}.name")
        if name in seen_collections:
            raise PluginManifestError(f"duplicate storage collection: {name}")
        seen_collections.add(name)

        fields: list[dict[str, Any]] = []
        declared_fields: set[str] = set()
        for field_index, raw_field in enumerate(_array(collection["fields"], f"{path}.fields", non_empty=True)):
            field_path = f"{path}.fields[{field_index}]"
            field = _mapping(raw_field, field_path, _COLLECTION_FIELD_FIELDS)
            field_name = _role(field["name"], f"{field_path}.name")
            if field_name in declared_fields:
                raise PluginManifestError(f"duplicate field in storage collection {name}: {field_name}")
            declared_fields.add(field_name)
            field_type = field["type"]
            if field_type not in _COLLECTION_VALUE_TYPES:
                raise PluginManifestError(f"{field_path}.type must be one of {sorted(_COLLECTION_VALUE_TYPES)}")
            fields.append(
                {
                    "name": field_name,
                    "type": field_type,
                    "required": _boolean(field["required"], f"{field_path}.required"),
                }
            )

        indexes: list[dict[str, Any]] = []
        constraints: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for group_name, destination in (
            ("indexes", indexes),
            ("unique_constraints", constraints),
        ):
            for item_index, raw_index in enumerate(_array(collection[group_name], f"{path}.{group_name}")):
                index_path = f"{path}.{group_name}[{item_index}]"
                index = _mapping(raw_index, index_path, _INDEX_FIELDS)
                index_name = _role(index["name"], f"{index_path}.name")
                if index_name in seen_names:
                    raise PluginManifestError(f"duplicate index or constraint name in collection {name}: {index_name}")
                seen_names.add(index_name)
                destination.append(
                    {
                        "name": index_name,
                        "fields": _validate_field_references(
                            index["fields"],
                            path=f"{index_path}.fields",
                            declared_fields=declared_fields,
                        ),
                    }
                )
        collections.append(
            {
                "name": name,
                "fields": fields,
                "indexes": indexes,
                "unique_constraints": constraints,
            }
        )
    return {"kv": kv, "collections": collections}


@dataclass(frozen=True)
class AutomationPluginManifestV2:
    """Validated, immutable projection of one schema-v2 plugin manifest."""

    schema_version: int
    runtime_model: str
    plugin_id: str
    name: str
    version: str
    description: str
    host_api: Mapping[str, Any]
    runtime: Mapping[str, Any]
    provides: tuple[Mapping[str, Any], ...]
    requires: tuple[Mapping[str, Any], ...]
    capabilities: tuple[Mapping[str, Any], ...]
    account_roles: tuple[Mapping[str, Any], ...]
    resource_roles: tuple[Mapping[str, Any], ...]
    contributes: Mapping[str, Any]
    config_schema: Mapping[str, Any]
    storage: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AutomationPluginManifestV2":
        if not isinstance(raw, Mapping):
            raise PluginManifestError("manifest must be an object")
        data = _mapping(raw, "manifest", _TOP_LEVEL_FIELDS)
        if isinstance(data["schema_version"], bool) or data["schema_version"] != 2:
            raise PluginManifestError("schema_version must be 2")
        if data["runtime_model"] != "service_v2":
            raise PluginManifestError("runtime_model must be service_v2")
        plugin_id = _text(data["plugin_id"], "plugin_id", maximum=64)
        if not _PLUGIN_ID_RE.fullmatch(plugin_id):
            raise PluginManifestError("plugin_id must be stable lower_snake_case")
        version = _semver(data["version"], "version")
        host_api = _validate_host_api(data["host_api"])
        runtime = _validate_runtime(data["runtime"])
        provides, operations_by_service = _validate_provides(
            data["provides"],
            plugin_id=plugin_id,
        )
        provided_operation_effects = {
            (str(provided["service"]), str(operation["name"])): CapabilityEffect(
                str(operation["effect"])
            )
            for provided in provides
            for operation in provided["operations"]
        }
        account_roles, account_role_names = _validate_account_roles(data["account_roles"])
        resource_roles, resource_role_names = _validate_resource_roles(data["resource_roles"])
        if account_role_names & resource_role_names:
            raise PluginManifestError("account and resource role names must be globally unique")
        requires = _validate_requires(
            data["requires"],
            provided_services=frozenset(operations_by_service),
            account_roles=account_role_names,
            required_account_roles=frozenset(
                str(item["role"])
                for item in account_roles
                if item["required"] is True
            ),
        )
        storage = _validate_storage(data["storage"])
        capabilities = _validate_capabilities(
            data["capabilities"],
            account_roles=account_role_names,
            resource_roles=resource_role_names,
            storage=storage,
        )
        contributes = _validate_contributes(
            data["contributes"],
            operations_by_service=operations_by_service,
            provided_operation_effects=provided_operation_effects,
        )
        _validate_default_scheduler_host_compatibility(contributes)
        config_schema = _validate_config_schema(data["config_schema"])
        normalized = cls(
            schema_version=2,
            runtime_model="service_v2",
            plugin_id=plugin_id,
            name=_text(data["name"], "name", maximum=120),
            version=version,
            description=_text(data["description"], "description", maximum=1000),
            host_api=_deep_freeze(host_api),
            runtime=_deep_freeze(runtime),
            provides=tuple(_deep_freeze(item) for item in provides),
            requires=tuple(_deep_freeze(item) for item in requires),
            capabilities=tuple(_deep_freeze(item) for item in capabilities),
            account_roles=tuple(_deep_freeze(item) for item in account_roles),
            resource_roles=tuple(_deep_freeze(item) for item in resource_roles),
            contributes=_deep_freeze(contributes),
            config_schema=_deep_freeze(config_schema),
            storage=_deep_freeze(storage),
        )
        canonical_json_bytes(normalized.to_mapping())
        return normalized

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_model": self.runtime_model,
            "plugin_id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "host_api": _deep_thaw(self.host_api),
            "runtime": _deep_thaw(self.runtime),
            "provides": [_deep_thaw(item) for item in self.provides],
            "requires": [_deep_thaw(item) for item in self.requires],
            "capabilities": [_deep_thaw(item) for item in self.capabilities],
            "account_roles": [_deep_thaw(item) for item in self.account_roles],
            "resource_roles": [_deep_thaw(item) for item in self.resource_roles],
            "contributes": _deep_thaw(self.contributes),
            "config_schema": _deep_thaw(self.config_schema),
            "storage": _deep_thaw(self.storage),
        }

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_mapping())).hexdigest()

    @property
    def provided_services(self) -> tuple[str, ...]:
        return tuple(str(item["service"]) for item in self.provides)

    @property
    def required_services(self) -> tuple[str, ...]:
        return tuple(str(item["service"]) for item in self.requires)

    @property
    def connector_requirements(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            item
            for item in self.requires
            if _CONNECTOR_SERVICE_RE.fullmatch(str(item["service"]))
        )

    def connector_account_role_for(self, service: str) -> str | None:
        for requirement in self.connector_requirements:
            if requirement["service"] == service:
                return str(requirement["account_role"])
        return None

    @property
    def runtime_entrypoint(self) -> str:
        return str(self.runtime["entrypoint"])

    def supports_host_api(self, version: str) -> bool:
        candidate = _semver(version, "host_api_version")
        return (
            _semver_tuple(str(self.host_api["minimum"]))
            <= _semver_tuple(candidate)
            < _semver_tuple(str(self.host_api["maximum_exclusive"]))
        )


def parse_manifest_v2(raw: Mapping[str, Any]) -> AutomationPluginManifestV2:
    """Parse only schema v2; callers must not fall back to schema v1 on error."""

    return AutomationPluginManifestV2.from_mapping(raw)


__all__ = [
    "AutomationPluginManifestV2",
    "canonical_json_bytes",
    "parse_manifest_v2",
]
