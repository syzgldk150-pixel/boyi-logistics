"""Code-owned Service v2 Host API capability governance.

Service packages only declare that they need a Host API operation.  They do
not get to classify its effect or its governance.  This module is the single
authority for the exact ``(api_version, capability, action)`` identity.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from agent.automation_plugins.errors import PluginExecutionError


HOST_CAPABILITY_API_VERSION = "2.0.0"


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


class CapabilityEffect(str, Enum):
    """The complete, ordered effect vocabulary for a Host API action."""

    READ = "read"
    COMPUTE = "compute"
    INTERNAL_WRITE = "internal_write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"


_EFFECT_RANK = {
    CapabilityEffect.READ: 0,
    CapabilityEffect.COMPUTE: 1,
    CapabilityEffect.INTERNAL_WRITE: 2,
    CapabilityEffect.EXTERNAL_WRITE: 3,
    CapabilityEffect.DESTRUCTIVE: 4,
}


@dataclass(frozen=True)
class EffectGovernance:
    """Immutable governance assigned by the Host to one exact effect."""

    effect: CapabilityEffect
    operation_type: str
    risk_level: str
    lock_class: str
    evidence: Mapping[str, object]
    postconditions: tuple[Mapping[str, object], ...]
    retry: Mapping[str, object]
    harness_allowed: bool
    broker_effect: str
    approval: Mapping[str, object]
    idempotency: Mapping[str, object]
    project_full_auto_allowed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze(self.evidence))
        object.__setattr__(self, "postconditions", _freeze(self.postconditions))
        object.__setattr__(self, "retry", _freeze(self.retry))
        object.__setattr__(self, "approval", _freeze(self.approval))
        object.__setattr__(self, "idempotency", _freeze(self.idempotency))

    def to_mapping(self) -> dict[str, object]:
        return {
            "effect": self.effect.value,
            "operation_type": self.operation_type,
            "risk_level": self.risk_level,
            "lock_class": self.lock_class,
            "evidence": _thaw(self.evidence),
            "postconditions": _thaw(self.postconditions),
            "retry": _thaw(self.retry),
            "harness_allowed": self.harness_allowed,
            "broker_effect": self.broker_effect,
            "approval": _thaw(self.approval),
            "idempotency": _thaw(self.idempotency),
            "project_full_auto_allowed": self.project_full_auto_allowed,
        }


def effect_rank(effect: CapabilityEffect | str) -> int:
    """Return the conservative ordering rank for a validated effect."""

    try:
        return _EFFECT_RANK[CapabilityEffect(effect)]
    except (KeyError, ValueError) as exc:
        raise ValueError("capability effect is invalid") from exc


def governance_for_effect(effect: CapabilityEffect | str) -> EffectGovernance:
    """Return the unique code-owned governance mapping for ``effect``."""

    normalized = CapabilityEffect(effect)
    if normalized is CapabilityEffect.READ:
        return EffectGovernance(
            effect=normalized,
            operation_type="read",
            risk_level="low",
            lock_class="none",
            evidence={"required": False, "required_fields": []},
            postconditions=(),
            retry={"safe": True, "max_attempts": 3},
            harness_allowed=True,
            broker_effect="read",
            approval={"mode": "project_policy"},
            idempotency={"mode": "parameters", "key_fields": []},
            project_full_auto_allowed=True,
        )
    if normalized is CapabilityEffect.COMPUTE:
        return EffectGovernance(
            effect=normalized,
            operation_type="compute",
            risk_level="low",
            lock_class="none",
            evidence={"required": False, "required_fields": []},
            postconditions=(),
            retry={"safe": True, "max_attempts": 3},
            harness_allowed=True,
            broker_effect="read",
            approval={"mode": "project_policy"},
            idempotency={"mode": "parameters", "key_fields": []},
            project_full_auto_allowed=True,
        )
    if normalized is CapabilityEffect.INTERNAL_WRITE:
        return EffectGovernance(
            effect=normalized,
            operation_type="internal_projection_write",
            risk_level="medium",
            lock_class="project",
            evidence={"required": True, "required_fields": ["outcome"]},
            postconditions=({"name": "plugin_result_contract_valid"},),
            retry={"safe": False, "max_attempts": 1},
            harness_allowed=True,
            broker_effect="write",
            approval={"mode": "project_policy"},
            idempotency={"mode": "parameters", "key_fields": []},
            project_full_auto_allowed=True,
        )
    if normalized is CapabilityEffect.EXTERNAL_WRITE:
        return EffectGovernance(
            effect=normalized,
            operation_type="external_write",
            risk_level="high",
            lock_class="external_target",
            evidence={"required": True, "required_fields": ["service", "operation", "outcome"]},
            postconditions=({"name": "plugin_result_contract_valid"},),
            retry={"safe": False, "max_attempts": 1},
            harness_allowed=False,
            broker_effect="write",
            approval={"mode": "project_policy"},
            idempotency={"mode": "parameters", "key_fields": []},
            project_full_auto_allowed=True,
        )
    return EffectGovernance(
        effect=normalized,
        operation_type="destructive",
        risk_level="extreme",
        lock_class="destructive_target",
        evidence={"required": True, "required_fields": ["service", "operation", "outcome"]},
        postconditions=({"name": "plugin_result_contract_valid"},),
        retry={"safe": False, "max_attempts": 1},
        harness_allowed=False,
        broker_effect="write",
        approval={"mode": "project_policy"},
        idempotency={"mode": "parameters", "key_fields": []},
        project_full_auto_allowed=False,
    )


@dataclass(frozen=True)
class HostCapabilityDescriptor:
    """One code-owned Host action baseline, addressed by its full key."""

    api_version: str
    capability: str
    action: str
    governance: EffectGovernance
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    handler_key: str
    requires_account_role: bool
    requires_resource_role: bool
    scheduler_allowed: bool
    per_call_limit: int
    timeout_seconds: int
    enabled: bool = True

    def __post_init__(self) -> None:
        if (
            not self.api_version
            or not self.capability
            or not self.action
            or not self.handler_key
            or not isinstance(self.input_schema, Mapping)
            or not isinstance(self.output_schema, Mapping)
            or not isinstance(self.requires_account_role, bool)
            or not isinstance(self.requires_resource_role, bool)
            or not isinstance(self.scheduler_allowed, bool)
            or not isinstance(self.enabled, bool)
            or isinstance(self.per_call_limit, bool)
            or not isinstance(self.per_call_limit, int)
            or self.per_call_limit < 1
            or isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
        ):
            raise ValueError("Host capability descriptor is invalid")
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.api_version, self.capability, self.action)

    def to_mapping(self) -> dict[str, object]:
        return {
            "api_version": self.api_version,
            "capability": self.capability,
            "action": self.action,
            "handler_key": self.handler_key,
            "availability": "enabled" if self.enabled else "disabled",
            "enabled": self.enabled,
            "input_schema": _thaw(self.input_schema),
            "output_schema": _thaw(self.output_schema),
            "requires_account_role": self.requires_account_role,
            "requires_resource_role": self.requires_resource_role,
            "scheduler_allowed": self.scheduler_allowed,
            "per_call_limit": self.per_call_limit,
            "timeout_seconds": self.timeout_seconds,
            **self.governance.to_mapping(),
        }


DynamicDescriptorQuery = Callable[[str, str, str], HostCapabilityDescriptor | None]


class HostCapabilityRegistry:
    """Fail-closed exact registry with optional code-owned dynamic lookup."""

    def __init__(
        self,
        descriptors: Iterable[HostCapabilityDescriptor] = (),
        *,
        dynamic_query: DynamicDescriptorQuery | None = None,
    ) -> None:
        self._descriptors: dict[tuple[str, str, str], HostCapabilityDescriptor] = {}
        self._duplicate_keys: set[tuple[str, str, str]] = set()
        self._dynamic_query = dynamic_query
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: HostCapabilityDescriptor) -> None:
        if not isinstance(descriptor, HostCapabilityDescriptor):
            raise TypeError("host capability descriptor is invalid")
        key = descriptor.key
        existing = self._descriptors.get(key)
        if existing is not None:
            self._duplicate_keys.add(key)
            return
        self._descriptors[key] = descriptor

    def resolve(
        self,
        *,
        api_version: str,
        capability: str,
        action: str,
    ) -> HostCapabilityDescriptor:
        key = (str(api_version), str(capability), str(action))
        if key in self._duplicate_keys:
            self._unavailable("duplicate Host capability descriptor")
        static = self._descriptors.get(key)
        try:
            dynamic = self._dynamic_query(*key) if self._dynamic_query is not None else None
        except Exception as exc:
            raise PluginExecutionError(
                "dynamic Host capability lookup is unavailable",
                code="CAPABILITY_UNAVAILABLE",
            ) from exc
        if dynamic is not None:
            if not isinstance(dynamic, HostCapabilityDescriptor) or dynamic.key != key:
                self._unavailable("dynamic Host capability descriptor drifted")
            if static is not None and dynamic != static:
                self._unavailable("dynamic Host capability descriptor drifted")
            static = dynamic
        if static is None or static.enabled is not True:
            self._unavailable("Host capability is unavailable")
        return static

    def snapshot(self) -> tuple[HostCapabilityDescriptor, ...]:
        """Return a deterministic immutable registry view for diagnostics/tests."""

        return tuple(self._descriptors[key] for key in sorted(self._descriptors))

    @staticmethod
    def _unavailable(message: str) -> None:
        raise PluginExecutionError(message, code="CAPABILITY_UNAVAILABLE")


def _descriptor(
    capability: str,
    action: str,
    effect: CapabilityEffect,
    *,
    input_schema: Mapping[str, object],
    output_schema: Mapping[str, object],
    handler_key: str,
    enabled: bool = True,
) -> HostCapabilityDescriptor:
    return HostCapabilityDescriptor(
        api_version=HOST_CAPABILITY_API_VERSION,
        capability=capability,
        action=action,
        governance=governance_for_effect(effect),
        input_schema=input_schema,
        output_schema=output_schema,
        handler_key=handler_key,
        requires_account_role=capability == "browser.session",
        requires_resource_role=False,
        scheduler_allowed=True,
        per_call_limit=64,
        timeout_seconds=30,
        enabled=enabled,
    )


_STRING = {"type": "string", "minLength": 1, "maxLength": 191}
def _json_value_schema(array_depth: int = 16) -> dict[str, object]:
    candidates: list[dict[str, object]] = [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "object", "additionalProperties": True},
        {"type": "null"},
    ]
    if array_depth > 0:
        candidates.append(
            {
                "type": "array",
                "items": _json_value_schema(array_depth - 1),
            }
        )
    return {"oneOf": candidates}


_JSON_VALUE = _json_value_schema()
_SITE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sitecode": {"type": "string", "minLength": 1, "maxLength": 64},
        "sitefbcode": {"type": "string", "minLength": 1, "maxLength": 64},
        "sitename": {"type": "string", "minLength": 1, "maxLength": 100},
        "sitefbname": {"type": "string", "minLength": 1, "maxLength": 100},
    },
    "required": ["sitecode", "sitefbcode", "sitename", "sitefbname"],
}


def _object_schema(
    properties: Mapping[str, object],
    required: tuple[str, ...],
    *,
    additional_properties: bool = False,
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": additional_properties,
        "properties": copy.deepcopy(dict(properties)),
        "required": list(required),
    }


_KV_GET_INPUT = _object_schema({"key": _STRING}, ("key",))
_KV_PUT_INPUT = _object_schema(
    {
        "key": _STRING,
        "value": _JSON_VALUE,
        "expected_version": {"type": "integer", "minimum": 0},
    },
    ("key", "value", "expected_version"),
)
_KV_GET_OUTPUT = _object_schema(
    {
        "found": {"type": "boolean"},
        "value": _JSON_VALUE,
        "version": {"type": "integer", "minimum": 0},
    },
    ("found", "value", "version"),
)
_STORED_OUTPUT = _object_schema(
    {
        "stored": {"type": "boolean"},
        "version": {"type": "integer", "minimum": 1},
        "content_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
    },
    ("stored", "version", "content_sha256"),
)
_COLLECTION_GET_INPUT = _object_schema(
    {"collection": _STRING, "document_key": _STRING},
    ("collection", "document_key"),
)
_COLLECTION_GET_OUTPUT = _object_schema(
    {
        "found": {"type": "boolean"},
        "document": {"oneOf": [{"type": "object"}, {"type": "null"}]},
        "version": {"type": "integer", "minimum": 0},
    },
    ("found", "document", "version"),
)
_COLLECTION_QUERY_INPUT = _object_schema(
    {
        "collection": _STRING,
        "index_name": _STRING,
        "values": {"type": "object", "additionalProperties": True},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    ("collection", "index_name", "values", "limit"),
)
_COLLECTION_QUERY_OUTPUT = _object_schema(
    {
        "documents": {"type": "array"},
        "count": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    ("documents", "count", "limit"),
)
_COLLECTION_WRITE_INPUT = _object_schema(
    {
        "collection": _STRING,
        "document_key": _STRING,
        "document": {"type": "object", "additionalProperties": True},
        "expected_version": {"type": "integer", "minimum": 0},
    },
    ("collection", "document_key", "document", "expected_version"),
)
_CLOCK_PRECHECK_INPUT = _object_schema(
    {
        "site": _SITE,
        "clock_types": {"type": "array", "minItems": 1, "items": _STRING},
    },
    ("site", "clock_types"),
)
_CLOCK_SUBMIT_INPUT = _object_schema(
    {"site": _SITE, "clock_type": _STRING},
    ("site", "clock_type"),
)
_CLOCK_VERIFY_INPUT = _object_schema(
    {"site": _SITE, "clock_type": _STRING, "operation_id": _STRING},
    ("site", "clock_type", "operation_id"),
)
_CLOCK_READ_OUTPUT = _object_schema(
    {"evidence_ref": _STRING},
    ("evidence_ref",),
    additional_properties=True,
)
_CLOCK_WRITE_OUTPUT = _object_schema(
    {"accepted": {"type": "boolean"}, "operation_id": _STRING, "evidence_ref": _STRING},
    ("accepted", "operation_id", "evidence_ref"),
    additional_properties=True,
)


_BASELINE_DESCRIPTORS = (
    _descriptor(
        "storage.kv",
        "get",
        CapabilityEffect.READ,
        input_schema=_KV_GET_INPUT,
        output_schema=_KV_GET_OUTPUT,
        handler_key="storage.kv:*",
    ),
    _descriptor(
        "storage.kv",
        "put",
        CapabilityEffect.INTERNAL_WRITE,
        input_schema=_KV_PUT_INPUT,
        output_schema=_STORED_OUTPUT,
        handler_key="storage.kv:*",
    ),
    _descriptor(
        "storage.collection",
        "get",
        CapabilityEffect.READ,
        input_schema=_COLLECTION_GET_INPUT,
        output_schema=_COLLECTION_GET_OUTPUT,
        handler_key="storage.collection:*",
    ),
    _descriptor(
        "storage.collection",
        "query",
        CapabilityEffect.READ,
        input_schema=_COLLECTION_QUERY_INPUT,
        output_schema=_COLLECTION_QUERY_OUTPUT,
        handler_key="storage.collection:*",
    ),
    _descriptor(
        "storage.collection",
        "put",
        CapabilityEffect.INTERNAL_WRITE,
        input_schema=_COLLECTION_WRITE_INPUT,
        output_schema=_STORED_OUTPUT,
        handler_key="storage.collection:*",
    ),
    _descriptor(
        "storage.collection",
        "upsert",
        CapabilityEffect.INTERNAL_WRITE,
        input_schema=_COLLECTION_WRITE_INPUT,
        output_schema=_STORED_OUTPUT,
        handler_key="storage.collection:*",
    ),
    _descriptor(
        "browser.session",
        "ronghui.clock.precheck",
        CapabilityEffect.READ,
        input_schema=_CLOCK_PRECHECK_INPUT,
        output_schema=_CLOCK_READ_OUTPUT,
        handler_key="browser.session:ronghui.clock.precheck",
    ),
    _descriptor(
        "browser.session",
        "ronghui.clock.submit",
        CapabilityEffect.EXTERNAL_WRITE,
        input_schema=_CLOCK_SUBMIT_INPUT,
        output_schema=_CLOCK_WRITE_OUTPUT,
        handler_key="browser.session:ronghui.clock.submit",
    ),
    _descriptor(
        "browser.session",
        "ronghui.clock.verify",
        CapabilityEffect.READ,
        input_schema=_CLOCK_VERIFY_INPUT,
        output_schema=_CLOCK_READ_OUTPUT,
        handler_key="browser.session:ronghui.clock.verify",
    ),
)


def default_host_capability_registry() -> HostCapabilityRegistry:
    """Build a fresh registry so callers cannot alter the global baseline."""

    return HostCapabilityRegistry(_BASELINE_DESCRIPTORS)


__all__ = [
    "CapabilityEffect",
    "EffectGovernance",
    "HOST_CAPABILITY_API_VERSION",
    "HostCapabilityDescriptor",
    "HostCapabilityRegistry",
    "default_host_capability_registry",
    "effect_rank",
    "governance_for_effect",
]
