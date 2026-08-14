"""Governed tool catalog with fail-closed LLM exposure and input validation."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("agent")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "tools" / "registry.yaml"

_JSON_SCHEMA_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "array", "object", "null"}
)
_COMMON_SCHEMA_FIELDS = frozenset({"type", "description", "enum"})
_SCHEMA_FIELDS_BY_TYPE = {
    "string": _COMMON_SCHEMA_FIELDS | {"minLength", "maxLength"},
    "integer": _COMMON_SCHEMA_FIELDS | {"minimum", "maximum"},
    "number": _COMMON_SCHEMA_FIELDS | {"minimum", "maximum"},
    "boolean": _COMMON_SCHEMA_FIELDS,
    "array": _COMMON_SCHEMA_FIELDS | {"items", "minItems", "maxItems", "uniqueItems"},
    "object": _COMMON_SCHEMA_FIELDS | {"properties", "required", "additionalProperties"},
    "null": _COMMON_SCHEMA_FIELDS,
}
_OPERATION_TYPES = frozenset(
    {
        "read",
        "compute",
        "internal_projection_write",
        "external_write",
        "financial_write",
        "destructive",
    }
)
_RISK_LEVELS = frozenset({"low", "medium", "high", "extreme"})
_APPROVAL_MODES = frozenset({"none", "required", "schedule_allowlist", "disabled"})
_APPROVAL_ROLES = frozenset({"admin", "super_admin"})
_IDEMPOTENCY_MODES = frozenset({"none", "key", "parameters"})
_LLM_OPERATION_TYPES = frozenset({"read", "compute"})
_WRITE_OPERATION_TYPES = frozenset(
    {"internal_projection_write", "external_write", "financial_write"}
)
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

_REQUIRED_TOOL_FIELDS = frozenset(
    {
        "name",
        "version",
        "description",
        "operation_type",
        "risk_level",
        "llm_exposed",
        "approval",
        "permissions",
        "account_scope",
        "idempotency",
        "retry",
        "evidence",
        "postconditions",
        "input_schema",
        "output_schema",
        "executor",
        "timeout",
        "heavy",
    }
)
_OPTIONAL_TOOL_FIELDS = frozenset({"queue_timeout"})


def _validation_error(index: int, message: str) -> ValueError:
    return ValueError(f"Invalid tools registry entry #{index}: {message}")


def _expect_mapping(index: int, value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _validation_error(index, f"{path} must be a mapping")
    return value


def _expect_string_list(
    index: int,
    value: Any,
    path: str,
    *,
    allow_empty: bool,
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise _validation_error(index, f"{path} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise _validation_error(index, f"{path} must not be empty")
    if len(value) != len(set(value)):
        raise _validation_error(index, f"{path} must not contain duplicates")
    return value


def _normalize_postconditions(index: int, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise _validation_error(index, "postconditions must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if isinstance(item, str):
            name = item.strip()
            condition = {"name": name}
        elif isinstance(item, dict) and set(item) == {"name"}:
            name = str(item.get("name") or "").strip()
            condition = {"name": name}
        else:
            raise _validation_error(
                index,
                "postconditions items must be non-empty strings or objects containing only name",
            )
        if not name:
            raise _validation_error(index, "postconditions names must be non-empty strings")
        if name in names:
            raise _validation_error(index, "postconditions must not contain duplicates")
        names.add(name)
        normalized.append(condition)
    return normalized


def _validate_schema_definition(
    index: int,
    schema: Any,
    path: str,
    *,
    require_object: bool = False,
    require_closed_object: bool = False,
) -> None:
    schema = _expect_mapping(index, schema, path)
    schema_type = schema.get("type")
    one_of = schema.get("oneOf")

    if schema_type is None:
        if require_object:
            raise _validation_error(index, f"{path}.type must be object")
        if not isinstance(one_of, list) or not one_of:
            raise _validation_error(index, f"{path}.type or {path}.oneOf is required")
        unknown_fields = set(schema) - {"oneOf", "description"}
        if unknown_fields:
            raise _validation_error(index, f"{path} has unsupported fields: {sorted(unknown_fields)}")
        for candidate_index, candidate in enumerate(one_of):
            _validate_schema_definition(index, candidate, f"{path}.oneOf[{candidate_index}]")
        return

    if one_of is not None:
        raise _validation_error(index, f"{path} cannot define both type and oneOf")
    if schema_type not in _JSON_SCHEMA_TYPES:
        raise _validation_error(index, f"{path}.type is invalid: {schema_type!r}")
    if require_object and schema_type != "object":
        raise _validation_error(index, f"{path}.type must be object")
    unknown_fields = set(schema) - _SCHEMA_FIELDS_BY_TYPE[schema_type]
    if unknown_fields:
        raise _validation_error(index, f"{path} has unsupported fields: {sorted(unknown_fields)}")

    enum_values = schema.get("enum")
    if enum_values is not None and (not isinstance(enum_values, list) or not enum_values):
        raise _validation_error(index, f"{path}.enum must be a non-empty list")

    if schema_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise _validation_error(index, f"{path}.properties must be a mapping")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            raise _validation_error(index, f"{path}.required must be a string list")
        if len(required) != len(set(required)):
            raise _validation_error(index, f"{path}.required must not contain duplicates")
        unknown_required = set(required) - set(properties)
        if unknown_required:
            raise _validation_error(
                index,
                f"{path}.required has unknown properties: {sorted(unknown_required)}",
            )
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, (bool, dict)):
            raise _validation_error(index, f"{path}.additionalProperties must be boolean or a schema")
        if require_closed_object and additional is not False:
            raise _validation_error(index, f"{path}.additionalProperties must be false")
        if isinstance(additional, dict):
            _validate_schema_definition(index, additional, f"{path}.additionalProperties")
        for name, property_schema in properties.items():
            if not isinstance(name, str) or not name:
                raise _validation_error(index, f"{path}.properties keys must be non-empty strings")
            _validate_schema_definition(index, property_schema, f"{path}.properties.{name}")

    if schema_type == "array":
        if "items" not in schema:
            raise _validation_error(index, f"{path}.items is required")
        _validate_schema_definition(index, schema["items"], f"{path}.items")
        if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
            raise _validation_error(index, f"{path}.uniqueItems must be boolean")

    for bound_name in ("minLength", "maxLength", "minItems", "maxItems"):
        if bound_name in schema:
            bound = schema[bound_name]
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                raise _validation_error(index, f"{path}.{bound_name} must be a non-negative integer")
    for bound_name in ("minimum", "maximum"):
        if bound_name in schema:
            bound = schema[bound_name]
            if (
                isinstance(bound, bool)
                or not isinstance(bound, (int, float))
                or (isinstance(bound, float) and not math.isfinite(bound))
            ):
                raise _validation_error(index, f"{path}.{bound_name} must be a finite number")
    for lower_name, upper_name in (
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
        ("minimum", "maximum"),
    ):
        if lower_name in schema and upper_name in schema and schema[lower_name] > schema[upper_name]:
            raise _validation_error(index, f"{path}.{lower_name} cannot exceed {path}.{upper_name}")


def _validate_governance(index: int, tool: dict[str, Any]) -> None:
    version = tool["version"]
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        raise _validation_error(index, "version must use MAJOR.MINOR.PATCH")

    operation_type = tool["operation_type"]
    if operation_type not in _OPERATION_TYPES:
        raise _validation_error(index, f"operation_type must be one of {sorted(_OPERATION_TYPES)}")
    risk_level = tool["risk_level"]
    if risk_level not in _RISK_LEVELS:
        raise _validation_error(index, f"risk_level must be one of {sorted(_RISK_LEVELS)}")
    if not isinstance(tool["llm_exposed"], bool):
        raise _validation_error(index, "llm_exposed must be boolean")

    approval = _expect_mapping(index, tool["approval"], "approval")
    approval_mode = approval.get("mode")
    if approval_mode not in _APPROVAL_MODES:
        raise _validation_error(index, f"approval.mode must be one of {sorted(_APPROVAL_MODES)}")
    approval_fields = set(approval)
    if approval_mode in {"required", "schedule_allowlist"}:
        if approval_fields != {"mode", "required_role"}:
            raise _validation_error(
                index,
                f"approval mode {approval_mode} requires mode and required_role",
            )
        if approval["required_role"] not in _APPROVAL_ROLES:
            raise _validation_error(
                index,
                f"approval.required_role must be one of {sorted(_APPROVAL_ROLES)}",
            )
    elif approval_fields != {"mode"}:
        raise _validation_error(
            index,
            f"approval mode {approval_mode} must contain only mode",
        )

    permissions = _expect_mapping(index, tool["permissions"], "permissions")
    if set(permissions) != {"required_roles"}:
        raise _validation_error(index, "permissions must contain only required_roles")
    required_roles = _expect_string_list(
        index,
        permissions.get("required_roles"),
        "permissions.required_roles",
        allow_empty=False,
    )
    unknown_roles = set(required_roles) - _APPROVAL_ROLES
    if unknown_roles:
        raise _validation_error(
            index,
            f"permissions.required_roles has unsupported roles: {sorted(unknown_roles)}",
        )
    if approval_mode in {"required", "schedule_allowlist"} and approval["required_role"] not in required_roles:
        raise _validation_error(
            index,
            "approval.required_role must also be listed in permissions.required_roles",
        )

    account_scope = _expect_mapping(index, tool["account_scope"], "account_scope")
    if "mode" in account_scope:
        if set(account_scope) != {"mode", "allow_implicit_default"}:
            raise _validation_error(
                index,
                "mode account_scope must contain mode and allow_implicit_default",
            )
        if account_scope.get("mode") not in {
            "none",
            "optional",
            "single",
            "all_configured",
            "single_or_all_configured",
        }:
            raise _validation_error(index, "account_scope.mode is invalid")
        if not isinstance(account_scope["allow_implicit_default"], bool):
            raise _validation_error(index, "account_scope.allow_implicit_default must be boolean")
        if account_scope["allow_implicit_default"]:
            raise _validation_error(index, "mode account_scope cannot allow an implicit default")
    else:
        if set(account_scope) != {"required", "allow_implicit_default"}:
            raise _validation_error(
                index,
                "account_scope must contain required and allow_implicit_default",
            )
        if not isinstance(account_scope["required"], bool) or not isinstance(
            account_scope["allow_implicit_default"], bool
        ):
            raise _validation_error(index, "account_scope values must be boolean")
        if account_scope["required"] and account_scope["allow_implicit_default"]:
            raise _validation_error(index, "required account_scope cannot allow an implicit default")

    idempotency = _expect_mapping(index, tool["idempotency"], "idempotency")
    if set(idempotency) != {"mode", "key_fields"}:
        raise _validation_error(index, "idempotency must contain mode and key_fields")
    if idempotency.get("mode") not in _IDEMPOTENCY_MODES:
        raise _validation_error(index, f"idempotency.mode must be one of {sorted(_IDEMPOTENCY_MODES)}")
    key_fields = _expect_string_list(
        index,
        idempotency.get("key_fields"),
        "idempotency.key_fields",
        allow_empty=True,
    )
    input_properties = tool["input_schema"].get("properties", {})
    if idempotency["mode"] == "key":
        if not key_fields:
            raise _validation_error(index, "idempotency.key_fields is required for key mode")
        unknown_keys = set(key_fields) - set(input_properties)
        if unknown_keys:
            raise _validation_error(index, f"idempotency.key_fields has unknown inputs: {sorted(unknown_keys)}")
        optional_keys = set(key_fields) - set(tool["input_schema"].get("required", []))
        if optional_keys:
            raise _validation_error(
                index,
                f"idempotency.key_fields must be required inputs: {sorted(optional_keys)}",
            )
    elif key_fields:
        raise _validation_error(index, "idempotency.key_fields must be empty unless mode is key")

    retry = _expect_mapping(index, tool["retry"], "retry")
    if set(retry) != {"safe", "max_attempts"}:
        raise _validation_error(index, "retry must contain safe and max_attempts")
    if not isinstance(retry["safe"], bool):
        raise _validation_error(index, "retry.safe must be boolean")
    max_attempts = retry["max_attempts"]
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
        raise _validation_error(index, "retry.max_attempts must be an integer from 1 to 5")
    if not retry["safe"] and max_attempts != 1:
        raise _validation_error(index, "unsafe retry must use max_attempts=1")

    evidence = _expect_mapping(index, tool["evidence"], "evidence")
    if set(evidence) != {"required", "required_fields"}:
        raise _validation_error(index, "evidence must contain required and required_fields")
    if not isinstance(evidence["required"], bool):
        raise _validation_error(index, "evidence.required must be boolean")
    evidence_fields = _expect_string_list(
        index,
        evidence["required_fields"],
        "evidence.required_fields",
        allow_empty=True,
    )
    if evidence["required"] and not evidence_fields:
        raise _validation_error(index, "required evidence must declare required_fields")
    if not evidence["required"] and evidence_fields:
        raise _validation_error(index, "optional evidence cannot declare required_fields")

    tool["postconditions"] = _normalize_postconditions(index, tool["postconditions"])

    if tool["llm_exposed"]:
        if operation_type not in _LLM_OPERATION_TYPES:
            raise _validation_error(index, "llm_exposed tools must be read or compute operations")
        if risk_level not in {"low", "medium"}:
            raise _validation_error(index, "llm_exposed tools must have low or medium risk")
        if approval_mode != "none":
            raise _validation_error(index, "llm_exposed tools cannot require or disable approval")

    if operation_type == "read" and approval_mode != "none":
        raise _validation_error(index, "read tools must use approval.mode=none")

    if operation_type == "internal_projection_write":
        if risk_level not in {"medium", "high"}:
            raise _validation_error(index, "internal projection writes must have medium or high risk")
        if approval_mode not in {"required", "schedule_allowlist"}:
            raise _validation_error(
                index,
                "internal projection writes must require approval or use the schedule allowlist",
            )
        if risk_level == "high" and (
            approval.get("required_role") != "super_admin"
            or "super_admin" not in required_roles
        ):
            raise _validation_error(
                index,
                "high-risk internal projection writes must require super_admin",
            )

    if operation_type == "external_write":
        if risk_level != "high" or approval_mode not in {"required", "schedule_allowlist"}:
            raise _validation_error(
                index,
                "external writes must have high risk and require approval or explicitly permit an exact schedule exemption",
            )
        if approval.get("required_role") != "super_admin" or "super_admin" not in required_roles:
            raise _validation_error(index, "external writes must require super_admin")

    if operation_type == "financial_write":
        if risk_level != "high" or approval_mode not in {"required", "schedule_allowlist"}:
            raise _validation_error(
                index,
                "financial writes must have high risk and require approval or use the schedule allowlist",
            )
        if approval.get("required_role") != "super_admin" or "super_admin" not in required_roles:
            raise _validation_error(index, "financial writes must require super_admin")

    if operation_type in _WRITE_OPERATION_TYPES:
        if retry["safe"] or max_attempts != 1:
            raise _validation_error(index, "write tools cannot be retried automatically")

    if operation_type == "destructive" or risk_level == "extreme":
        if operation_type != "destructive" or risk_level != "extreme":
            raise _validation_error(index, "destructive and extreme classifications must be paired")
        if approval_mode != "disabled":
            raise _validation_error(index, "extreme destructive tools must be disabled")
        if tool["llm_exposed"]:
            raise _validation_error(index, "extreme destructive tools cannot be exposed to the LLM")
        if "super_admin" not in required_roles:
            raise _validation_error(index, "extreme destructive tools must require super_admin")

    if approval_mode == "disabled" and operation_type != "destructive":
        raise _validation_error(index, "only destructive tools may use approval.mode=disabled")


def validate_registry(data: Any, *, project_root: Path = PROJECT_ROOT) -> list[dict[str, Any]]:
    """Validate the complete governed manifest before exposing any executor."""

    if not isinstance(data, dict) or set(data) != {"tools"}:
        raise ValueError("Invalid tools registry: root must contain only tools")
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise ValueError("Invalid tools registry: tools must be a list")

    seen_names: set[str] = set()
    validated: list[dict[str, Any]] = []
    resolved_root = project_root.resolve()
    allowed_fields = _REQUIRED_TOOL_FIELDS | _OPTIONAL_TOOL_FIELDS
    for index, tool_value in enumerate(tools, start=1):
        tool = _expect_mapping(index, tool_value, "entry")
        missing_fields = sorted(_REQUIRED_TOOL_FIELDS - set(tool))
        if missing_fields:
            raise _validation_error(index, f"missing required fields: {missing_fields}")
        unknown_fields = sorted(set(tool) - allowed_fields)
        if unknown_fields:
            raise _validation_error(index, f"unknown fields: {unknown_fields}")

        name = tool["name"]
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise _validation_error(index, "name must be a valid function name with at most 64 characters")
        if name in seen_names:
            raise _validation_error(index, f"duplicate name: {name}")
        seen_names.add(name)
        if not isinstance(tool["description"], str) or not tool["description"].strip():
            raise _validation_error(index, "description must be a non-empty string")

        executor_value = tool["executor"]
        if not isinstance(executor_value, str) or not executor_value.strip():
            raise _validation_error(index, "executor must be a non-empty relative path")
        executor = (resolved_root / executor_value).resolve()
        if executor == resolved_root or resolved_root not in executor.parents:
            raise _validation_error(index, "executor must stay inside the Agent project root")
        if not executor.is_file():
            raise _validation_error(index, f"executor does not exist: {executor_value}")

        timeout = tool["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise _validation_error(index, "timeout must be a positive integer")
        if not isinstance(tool["heavy"], bool):
            raise _validation_error(index, "heavy must be boolean")
        if "queue_timeout" in tool:
            queue_timeout = tool["queue_timeout"]
            if isinstance(queue_timeout, bool) or not isinstance(queue_timeout, (int, float)) or queue_timeout < 0:
                raise _validation_error(index, "queue_timeout must be a non-negative number")

        _validate_schema_definition(
            index,
            tool["input_schema"],
            "input_schema",
            require_object=True,
            require_closed_object=True,
        )
        _validate_schema_definition(
            index,
            tool["output_schema"],
            "output_schema",
            require_object=True,
        )
        _validate_governance(index, tool)
        validated.append(copy.deepcopy(tool))
    return validated


def _input_error(tool_name: str, path: str, message: str) -> ValueError:
    return ValueError(f"Invalid input for tool {tool_name!r} at {path}: {message}")


def _validate_instance(tool_name: str, value: Any, schema: dict[str, Any], path: str) -> None:
    one_of = schema.get("oneOf")
    if one_of is not None:
        matches = 0
        for candidate in one_of:
            try:
                _validate_instance(tool_name, value, candidate, path)
            except ValueError:
                continue
            matches += 1
        if matches != 1:
            raise _input_error(tool_name, path, "must match exactly one allowed schema")
        return

    schema_type = schema["type"]
    if schema_type == "object":
        if not isinstance(value, Mapping):
            raise _input_error(tool_name, path, "must be an object")
        properties = schema.get("properties", {})
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            raise _input_error(tool_name, path, f"missing required properties: {sorted(missing)}")
        additional = schema.get("additionalProperties", True)
        unknown = sorted(str(name) for name in set(value) - set(properties))
        if unknown and additional is False:
            raise _input_error(tool_name, path, f"unknown properties: {unknown}")
        for name, item in value.items():
            if name in properties:
                _validate_instance(tool_name, item, properties[name], f"{path}.{name}")
            elif isinstance(additional, dict):
                _validate_instance(tool_name, item, additional, f"{path}.{name}")
        return

    if schema_type == "array":
        if not isinstance(value, list):
            raise _input_error(tool_name, path, "must be an array")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise _input_error(tool_name, path, f"must contain at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise _input_error(tool_name, path, f"must contain at most {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            for item_index, item in enumerate(value):
                if item in value[:item_index]:
                    raise _input_error(tool_name, path, "must not contain duplicate items")
        for item_index, item in enumerate(value):
            _validate_instance(tool_name, item, schema["items"], f"{path}[{item_index}]")
    elif schema_type == "string":
        if not isinstance(value, str):
            raise _input_error(tool_name, path, "must be a string")
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise _input_error(tool_name, path, f"must contain at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise _input_error(tool_name, path, f"must contain at most {schema['maxLength']} characters")
    elif schema_type == "boolean":
        if not isinstance(value, bool):
            raise _input_error(tool_name, path, "must be boolean")
    elif schema_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _input_error(tool_name, path, "must be an integer")
    elif schema_type == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise _input_error(tool_name, path, "must be a finite number")
    elif schema_type == "null" and value is not None:
        raise _input_error(tool_name, path, "must be null")

    if "enum" in schema and value not in schema["enum"]:
        raise _input_error(tool_name, path, f"must be one of {schema['enum']!r}")
    if schema_type in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise _input_error(tool_name, path, f"must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise _input_error(tool_name, path, f"must be <= {schema['maximum']}")


def validate_schema_instance(
    subject: str,
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str = "$",
) -> None:
    """Validate a JSON-compatible value against the catalog's supported schema subset."""

    _validate_instance(subject, value, dict(schema), path)


def _catalog_sha256(tools: list[dict[str, Any]]) -> str:
    canonical_tools = sorted(tools, key=lambda item: item["name"])
    canonical = json.dumps(
        canonical_tools,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ToolRegistry:
    def __init__(self, registry_path: Path = REGISTRY_PATH, *, project_root: Path = PROJECT_ROOT):
        self._registry_path = Path(registry_path)
        self._project_root = Path(project_root)
        self._tools: dict[str, dict[str, Any]] = {}
        self._registry_mtime_ns: int | None = None
        self._catalog_hash = ""
        self.load()

    def load(self) -> None:
        """Load and fully validate the governed tool catalog."""

        if not self._registry_path.is_file():
            raise RuntimeError(f"tools registry does not exist: {self._registry_path}")
        with self._registry_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        tools = validate_registry(data, project_root=self._project_root)
        self._tools = {tool["name"]: tool for tool in tools}
        self._catalog_hash = _catalog_sha256(tools)
        self._registry_mtime_ns = self._registry_path.stat().st_mtime_ns
        logger.info(
            "已加载 %d 个工具定义 catalog_sha256=%s",
            len(self._tools),
            self._catalog_hash,
        )

    def reload_if_changed(self) -> None:
        """Reload atomically whenever the manifest content timestamp changes."""

        if not self._registry_path.is_file():
            raise RuntimeError(f"tools registry does not exist: {self._registry_path}")
        if self._registry_path.stat().st_mtime_ns != self._registry_mtime_ns:
            logger.info("registry.yaml 已更新，重新加载")
            self.load()

    def catalog_sha256(self) -> str:
        """Return the stable SHA-256 of the canonical, order-independent catalog."""

        self.reload_if_changed()
        return self._catalog_hash

    @property
    def catalog_hash(self) -> str:
        """Return the stable catalog digest used by orchestration snapshots."""

        return self.catalog_sha256()

    def list_llm_capabilities(self) -> list[dict[str, Any]]:
        """Return governed capabilities that are safe for LLM planning."""

        self.reload_if_changed()
        result: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if not tool["llm_exposed"] or tool["operation_type"] not in _LLM_OPERATION_TYPES:
                continue
            if tool["approval"]["mode"] == "disabled" or tool["risk_level"] == "extreme":
                continue
            result.append(copy.deepcopy(tool))
        return result

    def get_llm_tools(self) -> list[dict[str, Any]]:
        """Expose only explicitly allowed read/compute tools to function calling."""

        result: list[dict[str, Any]] = []
        for tool in self.list_llm_capabilities():
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": copy.deepcopy(tool["input_schema"]),
                    },
                }
            )
        return result

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """Compatibility alias that preserves the fail-closed LLM catalog."""

        return self.get_llm_tools()

    def get_tool(self, name: str, *, include_disabled: bool = False) -> dict[str, Any] | None:
        self.reload_if_changed()
        tool = self._tools.get(name)
        if tool is None:
            return None
        if not include_disabled and tool["approval"]["mode"] == "disabled":
            return None
        return copy.deepcopy(tool)

    def get_capability(self, tool_name: str) -> dict[str, Any] | None:
        """Return governance metadata, including disabled catalog entries."""

        return self.get_tool(tool_name, include_disabled=True)

    def validate_input(self, name: str, params: Any) -> dict[str, Any]:
        """Validate JSON-compatible tool parameters without coercion or defaults."""

        self.reload_if_changed()
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        _validate_instance(name, params, tool["input_schema"], "$")
        return dict(params)

    def validate_arguments(self, tool_name: str, arguments: Any) -> None:
        """Validate orchestration arguments and raise a precise error on failure."""

        self.validate_input(tool_name, arguments)

    def list_tools(self) -> list[str]:
        self.reload_if_changed()
        return list(self._tools)
