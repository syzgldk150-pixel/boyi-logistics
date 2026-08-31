"""Host-owned, immutable registry for credential-free Connector services.

Connectors are code-owned adapters, not installable Service v2 Providers.  A
plugin may address an exact Connector service through ``service.invoke``, but
the Host alone constructs this registry and supplies the opaque account
binding used by the adapter.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from agent.automation_plugins.errors import AutomationPluginError
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from agent.tool_registry import validate_schema_instance
from shared.redaction import is_sensitive_key, redact_text


_SERVICE_RE = re.compile(
    r"^connector\.[a-z][a-z0-9_]{1,63}\.[a-z][a-z0-9_.-]{0,127}@(0|[1-9][0-9]*)$"
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,190}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SYSTEM_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_JSON_BYTES = 1024 * 1024
_MIN_OPERATION_JSON_BYTES = 1
_MAX_CONNECTOR_INPUT_BYTES = 64 * 1024 * 1024
_MAX_CONNECTOR_OUTPUT_BYTES = 10 * 1024 * 1024
_SENSITIVE_EXACT_FIELDS = frozenset(
    {
        "account_id",
        "account_ids",
        "resource_id",
        "resource_ids",
        "database",
        "db_connection",
        "endpoint",
        "endpoints",
        "file_path",
        "path",
        "url",
    }
)
_SENSITIVE_FIELD_MARKERS = (
    "authorization",
    "authentication",
    "cookie",
    "credential",
    "password",
    "passwd",
    "private_key",
    "secret",
    "session",
    "token",
)
_URI_SCHEME_RE = re.compile(
    r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}:(?://|[^\s])"
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/(?:$|(?=[^/\s]))")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])[a-zA-Z]:[\\/]")
_UNC_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:\\\\|//)[^\\/\s]+(?:[\\/]|$)"
)
_IPV4_ENDPOINT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]{1,5}(?:/|\b)"
)
_IPV6_ENDPOINT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])\[[0-9A-Fa-f:]+\]:[0-9]{1,5}(?:/|\b)"
)
_SCHEMA_FIELDS = {
    "string": frozenset({"type", "description", "minLength", "maxLength", "pattern"}),
    "integer": frozenset({"type", "description", "minimum", "maximum"}),
    "number": frozenset({"type", "description", "minimum", "maximum"}),
    "boolean": frozenset({"type", "description"}),
    "array": frozenset(
        {"type", "description", "items", "minItems", "maxItems", "uniqueItems"}
    ),
    "object": frozenset(
        {"type", "description", "properties", "required", "additionalProperties"}
    ),
    "null": frozenset({"type", "description"}),
}


class ConnectorRegistryError(AutomationPluginError):
    code = "CONNECTOR_REGISTRY_ERROR"


class ConnectorContractInvalid(ConnectorRegistryError):
    code = "CONNECTOR_CONTRACT_INVALID"


class ConnectorConflict(ConnectorRegistryError):
    code = "CONNECTOR_CONFLICT"


class ConnectorUnavailable(ConnectorRegistryError):
    code = "CONNECTOR_UNAVAILABLE"


class ConnectorOperationUnavailable(ConnectorRegistryError):
    code = "CONNECTOR_OPERATION_UNDECLARED"


class ConnectorBindingInvalid(ConnectorRegistryError):
    code = "CONNECTOR_BINDING_INVALID"


class ConnectorInvocationError(ConnectorRegistryError):
    code = "CONNECTOR_INVOCATION_FAILED"


class ConnectorSensitiveDataDenied(ConnectorRegistryError):
    code = "CONNECTOR_SENSITIVE_DATA_DENIED"


class ConnectorBindingKind(str, Enum):
    """The only Host-owned identities a Connector operation may receive."""

    ACCOUNT = "account"
    RESOURCE = "resource"
    HOST_INTERNAL = "host_internal"


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _identifier(value: object, *, field: str, pattern: re.Pattern[str]) -> str:
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or (pattern is _SERVICE_RE and len(value) > 220)
    ):
        raise ConnectorContractInvalid(f"Connector {field} is invalid")
    return value


def validate_connector_service_name(
    value: object,
    *,
    field: str = "service",
) -> str:
    """Validate one exact code-owned Connector service identifier."""

    return _identifier(value, field=field, pattern=_SERVICE_RE)


def validate_connector_account_role(
    value: object,
    *,
    field: str = "account_role",
) -> str:
    """Validate one exact Connector account-role identifier."""

    return _identifier(value, field=field, pattern=_ROLE_RE)


def validate_connector_system(
    value: object,
    *,
    field: str = "system",
) -> str:
    """Validate one exact Connector system identifier."""

    return _identifier(value, field=field, pattern=_SYSTEM_RE)


def validate_connector_resource_role(
    value: object,
    *,
    field: str = "resource_role",
) -> str:
    """Validate one exact Connector resource-role identifier."""

    return _identifier(value, field=field, pattern=_ROLE_RE)


def validate_connector_resource_kind(
    value: object,
    *,
    field: str = "resource_kind",
) -> str:
    """Validate one exact Connector resource kind identifier."""

    return _identifier(value, field=field, pattern=_SYSTEM_RE)


def _binding_identifier(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 191
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ConnectorBindingInvalid(f"Connector binding {field} is invalid")
    return value


def _normalized_field(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _is_sensitive_field(value: object) -> bool:
    field = _normalized_field(value)
    return (
        field in _SENSITIVE_EXACT_FIELDS
        or field.endswith(
            (
                "_account_id",
                "_account_ids",
                "_resource_id",
                "_resource_ids",
                "_endpoint",
                "_file_path",
            )
        )
        or any(marker in field for marker in _SENSITIVE_FIELD_MARKERS)
        or is_sensitive_key(value)
    )


def validate_connector_public_text(value: object, *, subject: str) -> str:
    """Reject secrets, URI targets and absolute local paths in public text."""

    if not isinstance(value, str):
        raise ConnectorContractInvalid(f"Connector {subject} must be text")
    if (
        redact_text(value) != value
        or _URI_SCHEME_RE.search(value) is not None
        or _POSIX_ABSOLUTE_PATH_RE.search(value) is not None
        or _WINDOWS_ABSOLUTE_PATH_RE.search(value) is not None
        or _UNC_ABSOLUTE_PATH_RE.search(value) is not None
        or _IPV4_ENDPOINT_RE.search(value) is not None
        or _IPV6_ENDPOINT_RE.search(value) is not None
    ):
        raise ConnectorSensitiveDataDenied(f"Connector {subject} contains sensitive data")
    return value


def _validate_closed_schema(schema: object, *, path: str, root: bool = False) -> None:
    if not isinstance(schema, Mapping):
        raise ConnectorContractInvalid(f"Connector {path} must be a JSON Schema object")
    schema_type = schema.get("type")
    if schema_type not in _SCHEMA_FIELDS:
        raise ConnectorContractInvalid(f"Connector {path}.type is invalid")
    if root and schema_type != "object":
        raise ConnectorContractInvalid(f"Connector {path} must describe an object")
    unknown = set(schema) - _SCHEMA_FIELDS[str(schema_type)]
    if unknown:
        raise ConnectorContractInvalid(f"Connector {path} contains unsupported fields")
    description = schema.get("description")
    if description is not None:
        validate_connector_public_text(description, subject=f"{path} schema metadata")
    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            raise ConnectorContractInvalid(
                f"Connector {path}.additionalProperties must be false"
            )
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
            raise ConnectorContractInvalid(f"Connector {path} object contract is invalid")
        if any(not isinstance(item, str) for item in required) or len(required) != len(
            set(required)
        ):
            raise ConnectorContractInvalid(f"Connector {path}.required is invalid")
        if not set(required).issubset(properties):
            raise ConnectorContractInvalid(f"Connector {path}.required is invalid")
        for field, nested in properties.items():
            if not isinstance(field, str) or not field or _is_sensitive_field(field):
                raise ConnectorSensitiveDataDenied(
                    f"Connector {path} contains a sensitive field name"
                )
            _validate_closed_schema(nested, path=f"{path}.{field}")
    elif schema_type == "array":
        if "items" not in schema:
            raise ConnectorContractInvalid(f"Connector {path}.items is required")
        _validate_closed_schema(schema["items"], path=f"{path}[]")
        if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
            raise ConnectorContractInvalid(f"Connector {path}.uniqueItems is invalid")
    elif schema_type == "string" and "pattern" in schema:
        pattern = schema.get("pattern")
        try:
            if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
                raise re.error
            validate_connector_public_text(pattern, subject=f"{path} pattern")
            re.compile(pattern)
        except ConnectorRegistryError:
            raise
        except re.error as exc:
            raise ConnectorContractInvalid(f"Connector {path}.pattern is invalid") from exc
    for bound_name in ("minLength", "maxLength", "minItems", "maxItems"):
        if bound_name in schema:
            bound = schema[bound_name]
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                raise ConnectorContractInvalid(f"Connector {path}.{bound_name} is invalid")
    for bound_name in ("minimum", "maximum"):
        if bound_name in schema:
            bound = schema[bound_name]
            if (
                isinstance(bound, bool)
                or not isinstance(bound, (int, float))
                or (isinstance(bound, float) and not math.isfinite(bound))
            ):
                raise ConnectorContractInvalid(f"Connector {path}.{bound_name} is invalid")
    for lower_name, upper_name in (
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
        ("minimum", "maximum"),
    ):
        if (
            lower_name in schema
            and upper_name in schema
            and schema[lower_name] > schema[upper_name]
        ):
            raise ConnectorContractInvalid(f"Connector {path} bounds are invalid")


def _validate_schema_value(
    value: object,
    schema: Mapping[str, object],
    *,
    subject: str,
) -> None:
    try:
        validate_schema_instance(subject, value, schema)
    except (TypeError, ValueError):
        raise ConnectorInvocationError(
            f"Connector {subject} does not match its closed schema"
        ) from None


def _reject_sensitive_result(
    value: object,
    *,
    sensitive_identifiers: tuple[str, ...],
    reject_wrapped_identifiers: bool,
) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or _is_sensitive_field(key):
                raise ConnectorSensitiveDataDenied(
                    "Connector result contains sensitive data"
                )
            _reject_sensitive_result(
                nested,
                sensitive_identifiers=sensitive_identifiers,
                reject_wrapped_identifiers=reject_wrapped_identifiers,
            )
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive_result(
                nested,
                sensitive_identifiers=sensitive_identifiers,
                reject_wrapped_identifiers=reject_wrapped_identifiers,
            )
        return
    if isinstance(value, str):
        if any(
            identifier in value
            if reject_wrapped_identifiers
            else value == identifier
            or re.search(
                rf"(?<![A-Za-z0-9_.-]){re.escape(identifier)}(?![A-Za-z0-9_.-])",
                value,
            )
            for identifier in sensitive_identifiers
        ):
            raise ConnectorSensitiveDataDenied(
                "Connector result contains sensitive data"
            )
        validate_connector_public_text(value, subject="result")
        return
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        try:
            numeric_value = Decimal(str(value))
            matches_account = any(
                (numeric_identifier := Decimal(identifier)).is_finite()
                and numeric_value.is_finite()
                and numeric_value == numeric_identifier
                for identifier in sensitive_identifiers
            )
        except (InvalidOperation, ValueError):
            matches_account = False
        if matches_account:
            raise ConnectorSensitiveDataDenied(
                "Connector result contains sensitive data"
            )


def _json_copy(
    value: object,
    *,
    subject: str,
    maximum_bytes: int = _MAX_JSON_BYTES,
) -> object:
    try:
        material = dict(value) if isinstance(value, Mapping) else value
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > maximum_bytes:
            raise ValueError
        decoded = json.loads(encoded.decode("utf-8"))
    except Exception:
        raise ConnectorInvocationError(f"Connector {subject} is not bounded JSON") from None
    return decoded


ConnectorHandler = Callable[
    ["ConnectorBinding", Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


@dataclass(frozen=True)
class ConnectorBindingRef:
    """Opaque Host-private account binding passed only to a Connector adapter."""

    service: str
    account_role: str
    account_id: str
    system: str

    def __post_init__(self) -> None:
        validate_connector_service_name(self.service, field="binding service")
        validate_connector_account_role(self.account_role, field="binding account_role")
        _binding_identifier(self.account_id, field="account_id")
        validate_connector_system(self.system, field="binding system")


@dataclass(frozen=True)
class ConnectorResourceBindingRef:
    """Opaque Host-private resource binding passed only to a Connector adapter."""

    service: str
    resource_role: str
    resource_id: str
    kind: str

    def __post_init__(self) -> None:
        validate_connector_service_name(self.service, field="binding service")
        validate_connector_resource_role(
            self.resource_role,
            field="binding resource_role",
        )
        _binding_identifier(self.resource_id, field="resource_id")
        validate_connector_resource_kind(self.kind, field="binding resource kind")


@dataclass(frozen=True)
class ConnectorHostInternalBindingRef:
    """Opaque proof that the Host, rather than a package, owns this call."""

    service: str

    def __post_init__(self) -> None:
        validate_connector_service_name(self.service, field="binding service")


ConnectorBinding = (
    ConnectorBindingRef | ConnectorResourceBindingRef | ConnectorHostInternalBindingRef
)


@dataclass(frozen=True)
class ConnectorOperation:
    """One immutable code-owned Connector operation."""

    name: str
    effect: CapabilityEffect
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    handler: ConnectorHandler
    max_input_bytes: int = _MAX_JSON_BYTES
    max_output_bytes: int = _MAX_JSON_BYTES

    def __post_init__(self) -> None:
        _identifier(self.name, field="operation name", pattern=_NAME_RE)
        if self.effect not in {
            CapabilityEffect.READ,
            CapabilityEffect.INTERNAL_WRITE,
            CapabilityEffect.EXTERNAL_WRITE,
        }:
            raise ConnectorContractInvalid(
                "Connector operation effect must be read, internal_write or external_write"
            )
        if not callable(self.handler):
            raise ConnectorContractInvalid("Connector operation handler is invalid")
        _validate_closed_schema(self.input_schema, path="input_schema", root=True)
        _validate_closed_schema(self.output_schema, path="output_schema", root=True)
        for field, value, maximum in (
            ("max_input_bytes", self.max_input_bytes, _MAX_CONNECTOR_INPUT_BYTES),
            ("max_output_bytes", self.max_output_bytes, _MAX_CONNECTOR_OUTPUT_BYTES),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not _MIN_OPERATION_JSON_BYTES <= value <= maximum
            ):
                raise ConnectorContractInvalid(f"Connector {field} is invalid")
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))


@dataclass(frozen=True)
class ConnectorDescriptor:
    """One Host-owned Connector service and its closed operation set."""

    service: str
    title: str
    account_role: str | None
    allowed_systems: tuple[str, ...]
    operations: tuple[ConnectorOperation, ...]
    binding_kind: ConnectorBindingKind | str = ConnectorBindingKind.ACCOUNT
    resource_role: str | None = None
    allowed_resource_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_connector_service_name(self.service)
        try:
            binding_kind = ConnectorBindingKind(self.binding_kind)
        except (TypeError, ValueError) as exc:
            raise ConnectorContractInvalid("Connector binding_kind is invalid") from exc
        if (
            not isinstance(self.title, str)
            or not self.title
            or self.title != self.title.strip()
            or len(self.title) > 191
            or any(ord(character) < 32 or ord(character) == 127 for character in self.title)
        ):
            raise ConnectorContractInvalid("Connector title is invalid")
        validate_connector_public_text(self.title, subject="title")
        if not isinstance(self.allowed_systems, tuple):
            raise ConnectorContractInvalid("Connector allowed_systems is invalid")
        if not isinstance(self.allowed_resource_kinds, tuple):
            raise ConnectorContractInvalid("Connector allowed_resource_kinds is invalid")
        systems = tuple(
            validate_connector_system(item, field="allowed system")
            for item in self.allowed_systems
        )
        resource_kinds = tuple(
            validate_connector_resource_kind(item, field="allowed resource kind")
            for item in self.allowed_resource_kinds
        )
        if len(systems) != len(set(systems)):
            raise ConnectorContractInvalid("Connector allowed_systems contains duplicates")
        if len(resource_kinds) != len(set(resource_kinds)):
            raise ConnectorContractInvalid(
                "Connector allowed_resource_kinds contains duplicates"
            )
        if binding_kind is ConnectorBindingKind.ACCOUNT:
            if self.account_role is None:
                raise ConnectorContractInvalid("Connector account_role is invalid")
            validate_connector_account_role(self.account_role)
            if _is_sensitive_field(self.account_role):
                raise ConnectorSensitiveDataDenied(
                    "Connector account_role contains a sensitive field name"
                )
            if not systems or self.resource_role is not None or resource_kinds:
                raise ConnectorContractInvalid("Connector account binding contract is invalid")
        elif binding_kind is ConnectorBindingKind.RESOURCE:
            if (
                self.account_role is not None
                or systems
                or self.resource_role is None
                or not resource_kinds
            ):
                raise ConnectorContractInvalid("Connector resource binding contract is invalid")
            validate_connector_resource_role(self.resource_role)
        else:
            if (
                self.account_role is not None
                or systems
                or self.resource_role is not None
                or resource_kinds
            ):
                raise ConnectorContractInvalid("Connector host_internal binding contract is invalid")
        if not isinstance(self.operations, tuple) or not self.operations:
            raise ConnectorContractInvalid("Connector operations are invalid")
        if any(not isinstance(item, ConnectorOperation) for item in self.operations):
            raise ConnectorContractInvalid("Connector operations are invalid")
        operation_names = tuple(item.name for item in self.operations)
        if len(operation_names) != len(set(operation_names)):
            raise ConnectorConflict("Connector operation is duplicated")
        object.__setattr__(self, "allowed_systems", systems)
        object.__setattr__(self, "allowed_resource_kinds", resource_kinds)
        object.__setattr__(self, "binding_kind", binding_kind)


@dataclass(frozen=True)
class ResolvedConnectorOperation:
    """Complete immutable identity for one code-owned Connector call."""

    service: str
    title: str
    account_role: str | None
    allowed_systems: tuple[str, ...]
    operation: str
    effect: CapabilityEffect
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    handler: ConnectorHandler
    binding_kind: ConnectorBindingKind = ConnectorBindingKind.ACCOUNT
    resource_role: str | None = None
    allowed_resource_kinds: tuple[str, ...] = ()
    max_input_bytes: int = _MAX_JSON_BYTES
    max_output_bytes: int = _MAX_JSON_BYTES


class ConnectorRegistry:
    """Construct-once Connector registry with no package-controlled lifecycle."""

    def __init__(self, connectors: Iterable[ConnectorDescriptor] = ()) -> None:
        descriptors: dict[str, ConnectorDescriptor] = {}
        for descriptor in connectors:
            if not isinstance(descriptor, ConnectorDescriptor):
                raise TypeError("Connector descriptor is invalid")
            if descriptor.service in descriptors:
                raise ConnectorConflict("Connector service is duplicated")
            descriptors[descriptor.service] = descriptor
        self._descriptors = MappingProxyType(descriptors)

    def resolve(self, service: str) -> ConnectorDescriptor:
        if not isinstance(service, str) or _SERVICE_RE.fullmatch(service) is None:
            raise ConnectorUnavailable("Connector service is unavailable")
        descriptor = self._descriptors.get(service)
        if descriptor is None:
            raise ConnectorUnavailable("Connector service is unavailable")
        return descriptor

    def require_operation(self, service: str, operation: str) -> ResolvedConnectorOperation:
        descriptor = self.resolve(service)
        if not isinstance(operation, str) or _NAME_RE.fullmatch(operation) is None:
            raise ConnectorOperationUnavailable("Connector operation is unavailable")
        matches = tuple(item for item in descriptor.operations if item.name == operation)
        if len(matches) != 1:
            raise ConnectorOperationUnavailable("Connector operation is unavailable")
        selected = matches[0]
        return ResolvedConnectorOperation(
            service=descriptor.service,
            title=descriptor.title,
            binding_kind=descriptor.binding_kind,
            account_role=descriptor.account_role,
            allowed_systems=descriptor.allowed_systems,
            resource_role=descriptor.resource_role,
            allowed_resource_kinds=descriptor.allowed_resource_kinds,
            operation=selected.name,
            effect=selected.effect,
            input_schema=selected.input_schema,
            output_schema=selected.output_schema,
            max_input_bytes=selected.max_input_bytes,
            max_output_bytes=selected.max_output_bytes,
            handler=selected.handler,
        )

    def is_available(self, service: str) -> bool:
        return isinstance(service, str) and service in self._descriptors

    def snapshot(self) -> tuple[ConnectorDescriptor, ...]:
        return tuple(self._descriptors[key] for key in sorted(self._descriptors))

    def contract_sha256(self, service: str) -> str:
        """Return an internal revision for the complete closed service contract."""

        descriptor = self.resolve(service)
        material = self._contract_material(descriptor)
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _contract_material(descriptor: ConnectorDescriptor) -> dict[str, object]:
        """Preserve the EXT011 account/read/default-cap canonical revision."""

        if (
            descriptor.binding_kind is ConnectorBindingKind.ACCOUNT
            and all(
                operation.effect is CapabilityEffect.READ
                and operation.max_input_bytes == _MAX_JSON_BYTES
                and operation.max_output_bytes == _MAX_JSON_BYTES
                for operation in descriptor.operations
            )
        ):
            return {
                "service": descriptor.service,
                "title": descriptor.title,
                "account_role": descriptor.account_role,
                "allowed_systems": list(descriptor.allowed_systems),
                "operations": [
                    {
                        "name": operation.name,
                        "effect": operation.effect.value,
                        "input_schema": _thaw(operation.input_schema),
                        "output_schema": _thaw(operation.output_schema),
                    }
                    for operation in descriptor.operations
                ],
            }
        return {
            "service": descriptor.service,
            "title": descriptor.title,
            "binding_kind": descriptor.binding_kind.value,
            "account_role": descriptor.account_role,
            "allowed_systems": list(descriptor.allowed_systems),
            "resource_role": descriptor.resource_role,
            "allowed_resource_kinds": list(descriptor.allowed_resource_kinds),
            "operations": [
                {
                    "name": operation.name,
                    "effect": operation.effect.value,
                    "input_schema": _thaw(operation.input_schema),
                    "output_schema": _thaw(operation.output_schema),
                    "max_input_bytes": operation.max_input_bytes,
                    "max_output_bytes": operation.max_output_bytes,
                }
                for operation in descriptor.operations
            ],
        }
    def safe_projection(self) -> tuple[dict[str, object], ...]:
        return tuple(
            self._safe_descriptor_projection(descriptor)
            for descriptor in self.snapshot()
        )

    @staticmethod
    def _safe_descriptor_projection(descriptor: ConnectorDescriptor) -> dict[str, object]:
        projection: dict[str, object] = {
                "service": descriptor.service,
                "title": descriptor.title,
                "operations": [
                    {"name": operation.name, "effect": operation.effect.value}
                    for operation in descriptor.operations
                ],
        }
        if descriptor.binding_kind is ConnectorBindingKind.ACCOUNT:
            projection["account_role"] = descriptor.account_role
            projection["allowed_systems"] = list(descriptor.allowed_systems)
        elif descriptor.binding_kind is ConnectorBindingKind.RESOURCE:
            projection["binding_kind"] = descriptor.binding_kind.value
            projection["resource_role"] = descriptor.resource_role
            projection["allowed_resource_kinds"] = list(
                descriptor.allowed_resource_kinds
            )
        else:
            projection["binding_kind"] = descriptor.binding_kind.value
        return projection

    async def invoke(
        self,
        *,
        resolved: ResolvedConnectorOperation,
        binding: ConnectorBinding,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        detached_arguments = self.prepare_invocation(
            resolved=resolved,
            binding=binding,
            arguments=arguments,
        )
        current = self.require_operation(resolved.service, resolved.operation)
        try:
            result = current.handler(binding, MappingProxyType(detached_arguments))
            if inspect.isawaitable(result):
                result = await result
        except ConnectorRegistryError:
            raise
        except Exception:
            raise ConnectorInvocationError("Connector handler failed") from None
        if not isinstance(result, Mapping):
            raise ConnectorInvocationError("Connector result must be an object")
        detached_result = _json_copy(
            result,
            subject="result",
            maximum_bytes=current.max_output_bytes,
        )
        if not isinstance(detached_result, dict):
            raise ConnectorInvocationError("Connector result must be an object")
        _reject_sensitive_result(
            detached_result,
            sensitive_identifiers=self._binding_identifiers(binding),
            reject_wrapped_identifiers=isinstance(binding, ConnectorResourceBindingRef),
        )
        _validate_schema_value(detached_result, current.output_schema, subject="result")
        if any(
            isinstance(value, float) and not math.isfinite(value)
            for value in detached_result.values()
        ):
            raise ConnectorInvocationError("Connector result is not bounded JSON")
        return detached_result

    def prepare_invocation(
        self,
        *,
        resolved: ResolvedConnectorOperation,
        binding: ConnectorBinding,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate Connector input before a caller crosses a write boundary."""

        if not isinstance(resolved, ResolvedConnectorOperation):
            raise ConnectorOperationUnavailable("Connector operation is unavailable")
        current = self.require_operation(resolved.service, resolved.operation)
        if current != resolved:
            raise ConnectorOperationUnavailable("Connector operation contract drifted")
        self._validate_binding(current, binding)
        if not isinstance(arguments, Mapping):
            raise ConnectorInvocationError("Connector arguments must be an object")
        detached_arguments = _json_copy(
            arguments,
            subject="arguments",
            maximum_bytes=current.max_input_bytes,
        )
        if not isinstance(detached_arguments, dict):
            raise ConnectorInvocationError("Connector arguments must be an object")
        _validate_schema_value(
            detached_arguments,
            current.input_schema,
            subject="arguments",
        )
        return detached_arguments

    @staticmethod
    def _binding_identifiers(binding: ConnectorBinding) -> tuple[str, ...]:
        if isinstance(binding, ConnectorBindingRef):
            return (binding.account_id,)
        if isinstance(binding, ConnectorResourceBindingRef):
            return (binding.resource_id,)
        return ()

    @staticmethod
    def _validate_binding(
        current: ResolvedConnectorOperation,
        binding: object,
    ) -> None:
        if current.binding_kind is ConnectorBindingKind.ACCOUNT:
            if not isinstance(binding, ConnectorBindingRef) or (
                binding.service != current.service
                or binding.account_role != current.account_role
                or binding.system not in current.allowed_systems
            ):
                raise ConnectorBindingInvalid(
                    "Connector binding does not match the service contract"
                )
            return
        if current.binding_kind is ConnectorBindingKind.RESOURCE:
            if not isinstance(binding, ConnectorResourceBindingRef) or (
                binding.service != current.service
                or binding.resource_role != current.resource_role
                or binding.kind not in current.allowed_resource_kinds
            ):
                raise ConnectorBindingInvalid(
                    "Connector binding does not match the service contract"
                )
            return
        if not isinstance(binding, ConnectorHostInternalBindingRef) or (
            binding.service != current.service
        ):
            raise ConnectorBindingInvalid(
                "Connector binding does not match the service contract"
            )


__all__ = [
    "ConnectorBinding",
    "ConnectorBindingInvalid",
    "ConnectorBindingKind",
    "ConnectorBindingRef",
    "ConnectorConflict",
    "ConnectorContractInvalid",
    "ConnectorDescriptor",
    "ConnectorHostInternalBindingRef",
    "ConnectorInvocationError",
    "ConnectorOperation",
    "ConnectorOperationUnavailable",
    "ConnectorRegistry",
    "ConnectorRegistryError",
    "ConnectorResourceBindingRef",
    "ConnectorSensitiveDataDenied",
    "ConnectorUnavailable",
    "ResolvedConnectorOperation",
    "validate_connector_account_role",
    "validate_connector_public_text",
    "validate_connector_resource_kind",
    "validate_connector_resource_role",
    "validate_connector_service_name",
    "validate_connector_system",
]
