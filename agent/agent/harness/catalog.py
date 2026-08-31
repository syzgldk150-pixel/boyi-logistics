"""Safe, read-only Harness tool catalog and trusted invocation boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping, Sequence

from agent.harness.errors import HarnessError
from agent.harness.models import strict_json
from agent.harness.ports import ManagedContributionSnapshotProvider, TrustedHarnessInvocationPort


_TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_FORBIDDEN_SUBMISSION_KEYS = frozenset(
    {"automation_id", "service", "operation", "account_id", "resource_id", "provider_id"}
)
_SAFE_EFFECTS = frozenset({"read", "compute"})
_RUNTIME_PERMISSION_FIELDS = frozenset(
    {
        "network",
        "browser",
        "office",
        "file_roles",
        "broker_operations",
        "max_broker_calls",
    }
)
_MANAGED_CONTRACT_FIELDS = frozenset(
    {
        "id",
        "title",
        "description",
        "service",
        "operation",
        "effect",
        "operation_type",
        "harness_allowed",
        "broker_effect",
        "input_schema",
    }
)


def _safe_text(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > max_length:
        raise HarnessError(f"{field_name} is invalid", code="HARNESS_TOOL_INVALID")
    return value


def _contains_forbidden_identity(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            not isinstance(key, str)
            or key in _FORBIDDEN_SUBMISSION_KEYS
            or _contains_forbidden_identity(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_identity(item) for item in value)
    return False


def _closed_value_schema(value: object) -> None:
    if not isinstance(value, dict) or set(value) - {"type", "enum", "maxLength", "maximum", "minimum", "items"}:
        raise HarnessError("tool input property schema is unsupported", code="HARNESS_TOOL_INVALID")
    kind = value.get("type")
    if kind not in {"string", "integer", "number", "boolean", "array"}:
        raise HarnessError("tool input property type is unsupported", code="HARNESS_TOOL_INVALID")
    if kind == "array":
        _closed_value_schema(value.get("items"))
    elif "items" in value:
        raise HarnessError("tool input property schema is unsupported", code="HARNESS_TOOL_INVALID")


def _closed_object_schema(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("type") != "object":
        raise HarnessError("tool input schema must be an object schema", code="HARNESS_TOOL_INVALID")
    schema = strict_json(dict(value), field_name="tool input schema")
    if not isinstance(schema, dict) or set(schema) - {"type", "properties", "required", "additionalProperties"}:
        raise HarnessError("tool input schema is not closed", code="HARNESS_TOOL_INVALID")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise HarnessError("tool input schema is invalid", code="HARNESS_TOOL_INVALID")
    if schema.get("additionalProperties") is not False:
        raise HarnessError("tool input schema must reject extra fields", code="HARNESS_TOOL_INVALID")
    if not all(isinstance(key, str) and key and key not in _FORBIDDEN_SUBMISSION_KEYS for key in properties):
        raise HarnessError("tool input field is unsafe", code="HARNESS_TOOL_INVALID")
    if not all(isinstance(key, str) and key in properties for key in required):
        raise HarnessError("tool input required fields are invalid", code="HARNESS_TOOL_INVALID")
    for field_schema in properties.values():
        _closed_value_schema(field_schema)
    return schema


def _validate_value(value: object, schema: Mapping[str, Any]) -> Any:
    kind = schema["type"]
    if kind == "string":
        if not isinstance(value, str) or ("maxLength" in schema and len(value) > schema["maxLength"]):
            raise HarnessError("tool argument is invalid", code="HARNESS_ARGUMENT_INVALID")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise HarnessError("tool argument is invalid", code="HARNESS_ARGUMENT_INVALID")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HarnessError("tool argument is invalid", code="HARNESS_ARGUMENT_INVALID")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise HarnessError("tool argument is invalid", code="HARNESS_ARGUMENT_INVALID")
    elif kind == "array":
        if not isinstance(value, list):
            raise HarnessError("tool argument is invalid", code="HARNESS_ARGUMENT_INVALID")
        value = [_validate_value(item, schema["items"]) for item in value]
    if "enum" in schema and value not in schema["enum"]:
        raise HarnessError("tool argument is invalid", code="HARNESS_ARGUMENT_INVALID")
    if "minimum" in schema and value < schema["minimum"]:
        raise HarnessError("tool argument is invalid", code="HARNESS_ARGUMENT_INVALID")
    if "maximum" in schema and value > schema["maximum"]:
        raise HarnessError("tool argument is invalid", code="HARNESS_ARGUMENT_INVALID")
    return strict_json(value, field_name="tool argument")


def _sanitize_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise HarnessError("tool arguments must be an object", code="HARNESS_ARGUMENT_INVALID")
    if set(arguments) - set(schema["properties"]):
        raise HarnessError("tool arguments contain an unsupported field", code="HARNESS_ARGUMENT_INVALID")
    if _contains_forbidden_identity(arguments):
        raise HarnessError("tool arguments contain an identity field", code="HARNESS_ARGUMENT_INVALID")
    if any(key not in arguments for key in schema["required"]):
        raise HarnessError("tool arguments omit a required field", code="HARNESS_ARGUMENT_INVALID")
    return {
        key: _validate_value(value, schema["properties"][key])
        for key, value in arguments.items()
    }


@dataclass(frozen=True)
class ToolDescriptor:
    """The only tool shape made visible to a browser or offline model."""

    tool_id: str
    title: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not _TOOL_ID_RE.fullmatch(self.tool_id):
            raise HarnessError("tool identifier is invalid", code="HARNESS_TOOL_INVALID")
        object.__setattr__(self, "title", _safe_text(self.title, field_name="tool title", max_length=120))
        object.__setattr__(
            self,
            "description",
            _safe_text(self.description, field_name="tool description", max_length=500),
        )
        object.__setattr__(self, "input_schema", _closed_object_schema(self.input_schema))

    def public_mapping(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "title": self.title,
            "description": self.description,
            "input_schema": copy.deepcopy(dict(self.input_schema)),
        }


@dataclass(frozen=True)
class FixedHarnessTool:
    """A Host-injected tool whose read-only governance is explicit."""

    descriptor: ToolDescriptor
    opaque_handle: object
    effect: str = "read"
    harness_allowed: bool = True
    broker_effect: str = "read"

    def __post_init__(self) -> None:
        if (
            self.effect not in _SAFE_EFFECTS
            or self.harness_allowed is not True
            or self.broker_effect != "read"
        ):
            raise HarnessError("fixed tool governance is unsafe", code="HARNESS_TOOL_INVALID")


@dataclass(frozen=True)
class ManagedToolHandle:
    """Catalog-private dynamic identity; it is never emitted to a caller."""

    automation_id: str
    generation: int
    contribution_id: str


@dataclass(frozen=True)
class _CatalogEntry:
    descriptor: ToolDescriptor
    handle: object
    dynamic: bool


def _mapping(record: object) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    return vars(record)


def _package_is_safe(record: Mapping[str, Any], contract: Mapping[str, Any]) -> bool:
    del contract
    if record.get("runtime_model") != "SERVICE_V2":
        return False
    if record.get("mutating") is True:
        return False
    permissions = record.get("runtime_permissions")
    if not isinstance(permissions, Mapping) or set(permissions) != _RUNTIME_PERMISSION_FIELDS:
        return False
    if any(
        type(permissions.get(field_name)) is not bool
        or permissions.get(field_name) is not False
        for field_name in ("network", "browser", "office")
    ):
        return False
    file_roles = permissions.get("file_roles")
    broker_operations = permissions.get("broker_operations")
    max_broker_calls = permissions.get("max_broker_calls")
    if not isinstance(file_roles, (list, tuple)) or file_roles:
        return False
    if not isinstance(broker_operations, (list, tuple)) or broker_operations:
        return False
    return type(max_broker_calls) is int and max_broker_calls == 0


def _managed_entry(record: Mapping[str, Any]) -> _CatalogEntry:
    if str(record.get("contribution_kind") or "") != "harness":
        raise HarnessError("dynamic contribution kind is invalid", code="HARNESS_TOOL_INVALID")
    automation_id = _safe_text(record.get("automation_id"), field_name="automation identity", max_length=160)
    generation = record.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise HarnessError("dynamic generation is invalid", code="HARNESS_TOOL_INVALID")
    contribution_id = _safe_text(record.get("contribution_id"), field_name="contribution identity", max_length=160)
    contract = record.get("harness_contract")
    if not isinstance(contract, Mapping) or set(contract) != _MANAGED_CONTRACT_FIELDS:
        raise HarnessError("dynamic Harness contract is missing", code="HARNESS_TOOL_INVALID")
    if not _package_is_safe(record, contract):
        raise HarnessError("dynamic package has a forbidden surface", code="HARNESS_TOOL_INVALID")
    effect = str(contract.get("effect") or "")
    if (
        str(contract.get("id") or "") != contribution_id
        or effect not in _SAFE_EFFECTS
        or contract.get("operation_type") != effect
        or contract.get("harness_allowed") is not True
        or contract.get("broker_effect") != "read"
    ):
        raise HarnessError("dynamic tool governance is unsafe", code="HARNESS_TOOL_INVALID")
    _safe_text(contract.get("service"), field_name="dynamic service", max_length=191)
    _safe_text(contract.get("operation"), field_name="dynamic operation", max_length=128)
    input_schema = _closed_object_schema(contract.get("input_schema"))
    if input_schema["properties"] != {} or input_schema["required"] != []:
        raise HarnessError("dynamic tool input schema is not empty", code="HARNESS_TOOL_INVALID")
    opaque_identity = json.dumps(
        [automation_id, generation, contribution_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    derived_tool_id = f"managed.{hashlib.sha256(opaque_identity).hexdigest()}"
    descriptor = ToolDescriptor(
        tool_id=derived_tool_id,
        title=_safe_text(contract.get("title"), field_name="dynamic tool title", max_length=120),
        description=_safe_text(contract.get("description"), field_name="dynamic tool description", max_length=500),
        input_schema=input_schema,
    )
    return _CatalogEntry(
        descriptor=descriptor,
        handle=ManagedToolHandle(automation_id, generation, contribution_id),
        dynamic=True,
    )


class HarnessToolCatalog:
    """Merge fixed Host tools with safe dynamic Harness contribution snapshots."""

    def __init__(
        self,
        *,
        invocation_port: TrustedHarnessInvocationPort,
        fixed_tools: Sequence[FixedHarnessTool] = (),
        snapshot_provider: ManagedContributionSnapshotProvider | None = None,
    ) -> None:
        self._invocation_port = invocation_port
        self._fixed_tools = tuple(fixed_tools)
        self._snapshot_provider = snapshot_provider
        self._lock = RLock()
        self._entries: dict[str, _CatalogEntry] = {}
        self.refresh()

    def refresh(self) -> tuple[ToolDescriptor, ...]:
        candidates: dict[str, _CatalogEntry] = {}
        for tool in self._fixed_tools:
            if not isinstance(tool, FixedHarnessTool):
                raise HarnessError("fixed Harness tool is invalid", code="HARNESS_TOOL_INVALID")
            descriptor = ToolDescriptor(
                tool_id=tool.descriptor.tool_id,
                title=tool.descriptor.title,
                description=tool.descriptor.description,
                input_schema=tool.descriptor.input_schema,
            )
            if descriptor.tool_id in candidates:
                raise HarnessError("Harness tool collision", code="HARNESS_TOOL_COLLISION")
            candidates[descriptor.tool_id] = _CatalogEntry(
                descriptor=descriptor,
                handle=tool.opaque_handle,
                dynamic=False,
            )
        if self._snapshot_provider is not None:
            try:
                records = self._snapshot_provider.active_snapshot()
            except Exception as exc:
                raise HarnessError("managed contribution snapshot is unavailable", code="HARNESS_CATALOG_UNAVAILABLE") from exc
            if not isinstance(records, tuple):
                raise HarnessError("managed contribution snapshot is invalid", code="HARNESS_CATALOG_UNAVAILABLE")
            for raw in records:
                record = _mapping(raw)
                if record.get("contribution_kind") != "harness":
                    continue
                entry = _managed_entry(record)
                if entry.descriptor.tool_id in candidates:
                    raise HarnessError("Harness tool collision", code="HARNESS_TOOL_COLLISION")
                candidates[entry.descriptor.tool_id] = entry
        with self._lock:
            self._entries = candidates
            return self.descriptors()

    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        with self._lock:
            return tuple(
                copy.deepcopy(entry.descriptor)
                for _, entry in sorted(self._entries.items())
            )

    def public_tools(self) -> tuple[dict[str, Any], ...]:
        return tuple(item.public_mapping() for item in self.descriptors())

    def invoke(self, *, tool_id: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            raise HarnessError("Harness tool is unavailable", code="HARNESS_TOOL_NOT_FOUND")
        sanitized = _sanitize_arguments(entry.descriptor.input_schema, arguments)
        handle = entry.handle
        if entry.dynamic:
            if self._snapshot_provider is None or not isinstance(handle, ManagedToolHandle):
                raise HarnessError("dynamic Harness tool is stale", code="HARNESS_TOOL_STALE")
            try:
                fresh = _mapping(
                    self._snapshot_provider.resolve_active(
                        handle.automation_id,
                        handle.generation,
                        "harness",
                        handle.contribution_id,
                    )
                )
            except Exception as exc:
                raise HarnessError("dynamic Harness tool is stale", code="HARNESS_TOOL_STALE") from exc
            fresh_entry = _managed_entry(fresh)
            if fresh_entry.handle != handle or fresh_entry.descriptor != entry.descriptor:
                raise HarnessError("dynamic Harness tool is stale", code="HARNESS_TOOL_STALE")
            handle = fresh_entry.handle
        try:
            result = self._invocation_port.invoke(handle=handle, arguments=sanitized)
        except HarnessError:
            raise
        except Exception as exc:
            raise HarnessError("trusted Harness invocation failed", code="HARNESS_GATEWAY_FAILED") from exc
        normalized = strict_json(result, field_name="trusted Harness result")
        if not isinstance(normalized, dict):
            raise HarnessError("trusted Harness result must be an object", code="HARNESS_GATEWAY_FAILED")
        return normalized


__all__ = [
    "FixedHarnessTool",
    "HarnessToolCatalog",
    "ManagedToolHandle",
    "ToolDescriptor",
]
