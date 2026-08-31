"""Process-local projections for durable Service v2 generation effects."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Callable, Iterable, Mapping

from agent.automation_plugins.connector_compatibility import (
    connector_requirement_from_mapping,
    connector_requirements_from_contracts,
)
from agent.automation_plugins.connector_registry import ConnectorRegistryError
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    governance_for_effect,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    PluginRuntimeModel,
    RuntimeEffectKind,
    RuntimeGenerationSnapshot,
)
from agent.automation_plugins.ports import RuntimeEffectPlan
from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.automation_plugins.service_registry import (
    package_provider_registration_id,
)
from shared.waybill_entry_extensions import (
    normalize_waybill_entry_extension_handle,
    normalize_waybill_entry_slot,
)


_SERVICE_PROVIDER_GENERATION = 1
_CONTRIBUTION_EFFECT_CONTRACT_VERSION = 1
_LEGACY_SERVICE_V2_CONTRIBUTION_KINDS = (
    "console",
    "scheduler",
    "webhook",
    "feishu",
    "events",
)
_SERVICE_V2_CONTRIBUTION_KINDS = _LEGACY_SERVICE_V2_CONTRIBUTION_KINDS + (
    "harness",
    "module_slots",
)
_SERVICE_V2_CONTRIBUTION_KEYSETS = frozenset(
    frozenset((*_LEGACY_SERVICE_V2_CONTRIBUTION_KINDS, *optional))
    for optional in ((), ("harness",), ("module_slots",), ("harness", "module_slots"))
)
_MANAGED_CONTRIBUTION_KINDS = _SERVICE_V2_CONTRIBUTION_KINDS
_ACTIVE_CONTRIBUTION_KINDS = (
    "console",
    "scheduler",
    "webhook",
    "feishu",
    "events",
    "harness",
    "module_slots",
)
_WEBHOOK_ROUTE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$", re.ASCII)
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$", re.ASCII)
_PROJECT_DAILY_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$", re.ASCII)
_EMPTY_HARNESS_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
    "required": [],
}


class _FrozenDict(dict[str, Any]):
    """JSON-compatible immutable mapping for the active registry boundary."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("active contribution snapshot is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __ior__(self, other: object) -> "_FrozenDict":
        del other
        raise TypeError("active contribution snapshot is immutable")


def _freeze_json(value: Any) -> Any:
    """Recursively freeze the detached JSON values exposed to Harness."""

    if isinstance(value, Mapping):
        return _FrozenDict(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return copy.deepcopy(value)


def _harness_runtime_permissions(value: object | None = None) -> dict[str, Any]:
    """Return the closed, non-sensitive permission surface for Harness.

    Harness material must carry the exact signed generation descriptor.  There
    is intentionally no default: an omitted permission surface must not be
    mistaken for a package that was admitted without Host capabilities.
    """

    if value is None:
        raise PluginConflictError(
            "harness runtime permissions are missing",
            code="PLUGIN_CONTRACT_INVALID",
        )
    if not isinstance(value, Mapping):
        raise PluginConflictError(
            "harness runtime permissions are invalid",
            code="PLUGIN_CONTRACT_INVALID",
        )
    expected_fields = {
        "network",
        "browser",
        "office",
        "file_roles",
        "broker_operations",
        "max_broker_calls",
    }
    if set(value) != expected_fields:
        raise PluginConflictError(
            "harness runtime permissions are not closed",
            code="PLUGIN_CONTRACT_INVALID",
        )
    if any(
        type(value.get(field_name)) is not bool
        for field_name in ("network", "browser", "office")
    ):
        raise PluginConflictError(
            "harness runtime permission flags are invalid",
            code="PLUGIN_CONTRACT_INVALID",
        )
    file_roles = value.get("file_roles")
    broker_operations = value.get("broker_operations")
    max_broker_calls = value.get("max_broker_calls")
    if (
        not isinstance(file_roles, (list, tuple))
        or any(not isinstance(item, str) or not item for item in file_roles)
        or not isinstance(broker_operations, (list, tuple))
        or any(not isinstance(item, Mapping) for item in broker_operations)
        or isinstance(max_broker_calls, bool)
        or not isinstance(max_broker_calls, int)
        or max_broker_calls < 0
    ):
        raise PluginConflictError(
            "harness runtime permissions are invalid",
            code="PLUGIN_CONTRACT_INVALID",
        )
    if (
        value.get("network") is not False
        or value.get("browser") is not False
        or value.get("office") is not False
        or file_roles
        or broker_operations
        or max_broker_calls
    ):
        raise PluginConflictError(
            "harness runtime permissions expose an unsafe capability surface",
            code="CAPABILITY_UNAVAILABLE",
        )
    return copy.deepcopy(dict(value))


def _harness_contract_from_declaration(
    declaration: Mapping[str, Any],
    *,
    authoritative_effect: CapabilityEffect | str | None = None,
) -> dict[str, Any]:
    expected_fields = {
        "id",
        "title",
        "description",
        "service",
        "operation",
        "effect",
    }
    if set(declaration) != expected_fields:
        raise PluginConflictError(
            "v2 harness contribution declaration is not closed",
            code="PLUGIN_CONTRACT_INVALID",
        )
    for field_name, maximum in (
        ("id", 160),
        ("title", 120),
        ("description", 500),
        ("service", 191),
        ("operation", 128),
    ):
        value = declaration.get(field_name)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > maximum
        ):
            raise PluginConflictError(
                f"v2 harness contribution {field_name} is invalid",
                code="PLUGIN_CONTRACT_INVALID",
            )
    if not isinstance(declaration.get("effect"), str):
        raise PluginConflictError(
            "v2 harness contribution effect is invalid",
            code="PLUGIN_CONTRACT_INVALID",
        )
    declared_effect = declaration["effect"]
    try:
        declared = CapabilityEffect(declared_effect)
    except (TypeError, ValueError) as exc:
        raise PluginConflictError(
            "v2 harness contribution effect is invalid",
            code="PLUGIN_CONTRACT_INVALID",
        ) from exc
    if authoritative_effect is None:
        effect = declared
    else:
        try:
            effect = CapabilityEffect(authoritative_effect)
        except (TypeError, ValueError) as exc:
            raise PluginConflictError(
                "v2 harness Provider operation effect is invalid",
                code="PLUGIN_CONTRACT_INVALID",
            ) from exc
        if declared is not effect:
            raise PluginConflictError(
                "v2 harness contribution effect does not match its Provider operation",
                code="PLUGIN_CONTRACT_INVALID",
            )
    if effect not in {CapabilityEffect.READ, CapabilityEffect.COMPUTE}:
        raise PluginConflictError(
            "v2 harness contributions must be read or compute",
            code="CAPABILITY_UNAVAILABLE",
        )
    governance = governance_for_effect(effect).to_mapping()
    return {
        "id": declaration["id"],
        "title": declaration["title"],
        "description": declaration["description"],
        "service": declaration["service"],
        "operation": declaration["operation"],
        "effect": effect.value,
        "operation_type": str(governance["operation_type"]),
        "harness_allowed": governance["harness_allowed"],
        "broker_effect": governance["broker_effect"],
        "input_schema": copy.deepcopy(_EMPTY_HARNESS_INPUT_SCHEMA),
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _module_slot_handle(
    *,
    automation_id: str,
    generation: int,
    declaration: Mapping[str, Any],
) -> str:
    """Derive the opaque public identity from the exact signed generation."""

    slot = normalize_waybill_entry_slot(declaration.get("slot"))
    contribution_id = str(declaration.get("id") or "")
    declaration_sha256 = _digest(dict(declaration))
    return _digest(
        {
            "automation_id": automation_id,
            "generation": generation,
            "slot": slot,
            "contribution_id": contribution_id,
            "declaration_sha256": declaration_sha256,
        }
    )


def _feishu_command_digest(command: str) -> str:
    """Hash the exact command bytes without JSON normalization or folding."""

    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _required_sha(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise PluginConflictError(f"persisted {field} is not a SHA-256 digest")
    return text


def _service_operation_material(value: object) -> list[dict[str, str]]:
    """Copy the immutable exact operation/effect records into an effect plan."""

    if not isinstance(value, (list, tuple)) or not value:
        raise PluginConflictError("v2 provided service operations are invalid")
    result: list[dict[str, str]] = []
    names: set[str] = set()
    for operation in value:
        if not isinstance(operation, Mapping) or set(operation) != {"name", "effect"}:
            raise PluginConflictError("v2 provided service operation is invalid")
        name = str(operation.get("name") or "")
        if not name or name != name.strip() or len(name) > 191 or name in names:
            raise PluginConflictError("v2 provided service operation is invalid")
        try:
            effect = CapabilityEffect(str(operation.get("effect") or ""))
        except (TypeError, ValueError) as exc:
            raise PluginConflictError(
                "v2 provided service operation effect is invalid"
            ) from exc
        names.add(name)
        result.append({"name": name, "effect": effect.value})
    return result


@dataclass(frozen=True)
class ManagedContributionRegistration:
    """Process-local projection of one durable generation effect journal."""

    registration_id: str
    automation_id: str
    generation: int
    plugin_id: str
    plugin_version: str
    package_sha256: str
    manifest_sha256: str
    contribution_id: str
    contribution_kind: str
    service: str
    operation: str
    declaration: Mapping[str, Any]
    route_keys: tuple[str, ...]
    backend: str
    backend_status: str
    reason_code: str | None
    reason_detail: str | None
    project_schedule: Mapping[str, Any]
    schedule_sha256: str
    phase: str
    runtime_model: str = "SERVICE_V2"
    runtime_permissions: Mapping[str, Any] = field(default_factory=dict)
    harness_contract: Mapping[str, Any] = field(default_factory=dict)

    @property
    def dispatch_available(self) -> bool:
        return self.phase == "COMMITTED" and self.backend_status == "READY"


@dataclass(frozen=True)
class ManagedFeishuDispatchTarget:
    """Closed, process-local target returned by exact managed command lookup."""

    automation_id: str
    generation: int
    contribution_id: str


@dataclass(frozen=True)
class ManagedWebhookDispatchTarget:
    """Closed, process-local target returned by exact managed route lookup."""

    automation_id: str
    generation: int
    contribution_id: str


@dataclass(frozen=True)
class ManagedEventDispatchTarget:
    """Closed, process-local target returned by exact managed event lookup."""

    automation_id: str
    generation: int
    contribution_id: str


@dataclass(frozen=True)
class ManagedModuleSlotDispatchTarget:
    """Exact internal owner resolved from one public generation-bound handle."""

    automation_id: str
    generation: int
    contribution_id: str
    contribution_kind: str
    slot: str
    handle: str
    declaration_sha256: str
    service: str
    operation: str
    declaration: Mapping[str, Any]


class ManagedContributionRegistry:
    """Recoverable registry for host-owned v2 contribution declarations.

    Durable ownership lives in generation effect rows. This registry is an
    indexed process projection rebuilt from those rows at startup; it never
    pretends an unavailable transport backend is runnable.
    """

    def __init__(
        self,
        *,
        lock: Any | None = None,
        reserved_feishu_command: Callable[[str], bool] | None = None,
        migration_reserved_feishu_target: (
            Callable[[str, int, str, str], bool] | None
        ) = None,
        backend_availability: RuntimeContributionBackendAvailability | None = None,
    ) -> None:
        if reserved_feishu_command is not None and not callable(
            reserved_feishu_command
        ):
            raise TypeError("reserved_feishu_command must be callable")
        if migration_reserved_feishu_target is not None and not callable(
            migration_reserved_feishu_target
        ):
            raise TypeError("migration_reserved_feishu_target must be callable")
        self._lock = lock or RLock()
        self._reserved_feishu_command = reserved_feishu_command
        self._migration_reserved_feishu_target = (
            migration_reserved_feishu_target
        )
        if (
            backend_availability is not None
            and not isinstance(
                backend_availability,
                RuntimeContributionBackendAvailability,
            )
        ):
            raise TypeError(
                "backend_availability must be RuntimeContributionBackendAvailability"
            )
        self._backend_availability = backend_availability
        self._registrations: dict[str, ManagedContributionRegistration] = {}
        self._route_owners: dict[str, set[str]] = {}
        self._active_generations: dict[str, int] = {}

    def _process_backend_available(self, contribution_kind: str) -> bool:
        availability = self._backend_availability
        return (
            availability is None
            or availability.is_available(contribution_kind)
        )

    @staticmethod
    def _from_material(
        material: Mapping[str, Any],
        *,
        phase: str,
    ) -> ManagedContributionRegistration:
        contribution_kind = str(material["contribution_kind"])
        declaration = copy.deepcopy(dict(material["declaration"]))
        raw_harness_contract = material.get("harness_contract")
        if contribution_kind == "harness":
            if material.get("runtime_model") != "SERVICE_V2":
                raise PluginConflictError(
                    "harness contribution runtime model is missing or invalid",
                    code="PLUGIN_CONTRACT_INVALID",
                )
            if not isinstance(material.get("runtime_permissions"), Mapping):
                raise PluginConflictError(
                    "harness runtime permissions are missing",
                    code="PLUGIN_CONTRACT_INVALID",
                )
            if not isinstance(raw_harness_contract, Mapping):
                raise PluginConflictError(
                    "harness contribution contract is missing",
                    code="PLUGIN_CONTRACT_INVALID",
                )
        return ManagedContributionRegistration(
            registration_id=str(material["registration_id"]),
            automation_id=str(material["automation_id"]),
            generation=int(material["generation"]),
            plugin_id=str(material["plugin_id"]),
            plugin_version=str(material["plugin_version"]),
            package_sha256=str(material["package_sha256"]),
            manifest_sha256=str(material["manifest_sha256"]),
            contribution_id=str(material["contribution_id"]),
            contribution_kind=contribution_kind,
            service=str(material["service"]),
            operation=str(material["operation"]),
            declaration=declaration,
            route_keys=tuple(str(item) for item in material["route_keys"]),
            backend=str(material["backend"]),
            backend_status=str(material["backend_status"]),
            reason_code=(
                str(material["reason_code"])
                if material.get("reason_code") is not None
                else None
            ),
            reason_detail=(
                str(material["reason_detail"])
                if material.get("reason_detail") is not None
                else None
            ),
            project_schedule=copy.deepcopy(dict(material["project_schedule"])),
            schedule_sha256=str(material["schedule_sha256"]),
            phase=phase,
            runtime_model=str(material.get("runtime_model") or "SERVICE_V2"),
            runtime_permissions=(
                _harness_runtime_permissions(material.get("runtime_permissions"))
                if contribution_kind == "harness"
                else {}
            ),
            harness_contract=copy.deepcopy(
                dict(raw_harness_contract or {})
            ),
        )

    @staticmethod
    def _material(record: ManagedContributionRegistration) -> dict[str, Any]:
        return {
            "registration_id": record.registration_id,
            "automation_id": record.automation_id,
            "generation": record.generation,
            "plugin_id": record.plugin_id,
            "plugin_version": record.plugin_version,
            "package_sha256": record.package_sha256,
            "manifest_sha256": record.manifest_sha256,
            "contribution_id": record.contribution_id,
            "contribution_kind": record.contribution_kind,
            "service": record.service,
            "operation": record.operation,
            "declaration": copy.deepcopy(dict(record.declaration)),
            "route_keys": list(record.route_keys),
            "backend": record.backend,
            "backend_status": record.backend_status,
            "reason_code": record.reason_code,
            "reason_detail": record.reason_detail,
            "project_schedule": copy.deepcopy(dict(record.project_schedule)),
            "schedule_sha256": record.schedule_sha256,
            "runtime_model": record.runtime_model,
            "runtime_permissions": copy.deepcopy(dict(record.runtime_permissions)),
            "harness_contract": copy.deepcopy(dict(record.harness_contract)),
        }

    @staticmethod
    def _clone(
        record: ManagedContributionRegistration,
    ) -> ManagedContributionRegistration:
        return ManagedContributionRegistry._from_material(
            ManagedContributionRegistry._material(record),
            phase=record.phase,
        )

    @staticmethod
    def _validate_candidate(candidate: ManagedContributionRegistration) -> None:
        if candidate.contribution_kind not in _ACTIVE_CONTRIBUTION_KINDS:
            raise PluginConflictError(
                "managed contribution has no compatible host backend",
                code="CAPABILITY_UNAVAILABLE",
            )
        if candidate.backend_status not in {"READY", "DISABLED"}:
            raise PluginConflictError(
                "managed contribution host backend is unavailable",
                code="CAPABILITY_UNAVAILABLE",
            )
        if (
            not candidate.automation_id
            or candidate.automation_id != candidate.automation_id.strip()
            or candidate.generation < 1
            or not candidate.plugin_id
            or not candidate.plugin_version
            or not candidate.contribution_id
            or candidate.contribution_id != candidate.contribution_id.strip()
            or not candidate.service
            or not candidate.operation
        ):
            raise PluginConflictError(
                "managed contribution identity is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        if candidate.registration_id != (
            f"{candidate.automation_id}:{candidate.generation}:"
            f"{candidate.contribution_id}"
        ):
            raise PluginConflictError(
                "managed contribution registration identity is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        declaration = candidate.declaration
        if (
            str(declaration.get("id") or "") != candidate.contribution_id
            or str(declaration.get("service") or "") != candidate.service
            or str(declaration.get("operation") or "") != candidate.operation
        ):
            raise PluginConflictError(
                "managed contribution declaration identity is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        if candidate.contribution_kind == "harness":
            expected_harness_contract = _harness_contract_from_declaration(
                declaration
            )
            if candidate.runtime_model != "SERVICE_V2":
                raise PluginConflictError(
                    "harness contribution runtime model is invalid",
                    code="CONTRIBUTION_REGISTRATION_CONFLICT",
                )
            if canonical_json_bytes(
                dict(candidate.harness_contract)
            ) != canonical_json_bytes(expected_harness_contract):
                raise PluginConflictError(
                    "harness contribution contract is invalid",
                    code="CONTRIBUTION_REGISTRATION_CONFLICT",
                )
            _harness_runtime_permissions(candidate.runtime_permissions)
        if candidate.contribution_kind == "module_slots":
            _validate_module_slot_declaration(declaration)
        if candidate.contribution_kind == "feishu":
            commands = declaration.get("commands")
            if (
                not isinstance(commands, (list, tuple))
                or not commands
                or any(
                    not isinstance(command, str)
                    or not command
                    or command != command.strip()
                    or len(command) > 128
                    for command in commands
                )
                or len(set(commands)) != len(commands)
            ):
                raise PluginConflictError(
                    "managed Feishu commands are invalid",
                    code="CONTRIBUTION_REGISTRATION_CONFLICT",
                )
        if candidate.contribution_kind == "webhook":
            _validated_webhook_declaration(declaration)
        if candidate.contribution_kind == "events":
            _validated_event_declaration(declaration)
        expected_routes = _contribution_route_keys(
            automation_id=candidate.automation_id,
            generation=candidate.generation,
            contribution_kind=candidate.contribution_kind,
            declaration=declaration,
        )
        expected_backend = _contribution_backend(
            contribution_kind=candidate.contribution_kind,
            declaration=declaration,
            project_schedule=candidate.project_schedule,
        )
        observed_backend = (
            candidate.backend,
            candidate.backend_status,
            candidate.reason_code,
            candidate.reason_detail,
        )
        if candidate.route_keys != expected_routes or observed_backend != expected_backend:
            raise PluginConflictError(
                "managed contribution route or backend identity is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        _required_sha(candidate.package_sha256, "package_sha256")
        _required_sha(candidate.manifest_sha256, "manifest_sha256")
        if _required_sha(candidate.schedule_sha256, "schedule_sha256") != _digest(
            dict(candidate.project_schedule)
        ):
            raise PluginConflictError(
                "managed contribution schedule digest is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )

    @classmethod
    def _candidate_batch(
        cls,
        materials: Iterable[Mapping[str, Any]],
        *,
        phase: str = "PREPARED",
    ) -> tuple[ManagedContributionRegistration, ...]:
        try:
            candidates = tuple(
                cls._from_material(material, phase=phase)
                for material in materials
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PluginConflictError(
                "managed contribution material is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            ) from exc
        if not candidates:
            raise PluginConflictError(
                "managed contribution generation is empty",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        automation_id = candidates[0].automation_id
        generation = candidates[0].generation
        registration_ids: set[str] = set()
        contribution_ids: set[tuple[str, str]] = set()
        for candidate in candidates:
            cls._validate_candidate(candidate)
            if (
                candidate.automation_id != automation_id
                or candidate.generation != generation
            ):
                raise PluginConflictError(
                    "managed contribution batch spans multiple generations",
                    code="CONTRIBUTION_REGISTRATION_CONFLICT",
                )
            contribution_identity = (
                candidate.contribution_kind,
                candidate.contribution_id,
            )
            if (
                candidate.registration_id in registration_ids
                or contribution_identity in contribution_ids
            ):
                raise PluginConflictError(
                    "managed contribution batch contains duplicate identities",
                    code="CONTRIBUTION_REGISTRATION_CONFLICT",
                )
            registration_ids.add(candidate.registration_id)
            contribution_ids.add(contribution_identity)
        return candidates

    @staticmethod
    def _route_index(
        registrations: Mapping[str, ManagedContributionRegistration],
    ) -> dict[str, set[str]]:
        route_owners: dict[str, set[str]] = {}
        for registration_id, candidate in registrations.items():
            # A draining predecessor is diagnostic/lease-retention state only;
            # it must neither receive traffic nor reserve a global command.
            if candidate.phase == "DRAINING":
                continue
            for route_key in candidate.route_keys:
                for owner_id in route_owners.get(route_key, ()):
                    owner = registrations[owner_id]
                    if (
                        owner.automation_id != candidate.automation_id
                        or owner.generation == candidate.generation
                    ):
                        raise PluginConflictError(
                            "managed contribution route is already registered",
                            code="CONTRIBUTION_ROUTE_CONFLICT",
                        )
                route_owners.setdefault(route_key, set()).add(registration_id)
        return route_owners

    @staticmethod
    def _strict_refresh(refresh: Callable[[], object]) -> None:
        try:
            evidence = refresh()
        except Exception as exc:
            raise PluginConflictError(
                "strict scheduler refresh failed",
                code="RUNTIME_PROJECTION_REFRESH_FAILED",
            ) from exc
        if evidence is None:
            return
        invalid_tasks = evidence.get("invalid_tasks") if isinstance(evidence, Mapping) else None
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("initialized") is not True
            or not isinstance(invalid_tasks, (list, tuple))
            or invalid_tasks
        ):
            raise PluginConflictError(
                "strict scheduler refresh did not return complete success evidence",
                code="RUNTIME_PROJECTION_REFRESH_FAILED",
            )

    def prepare_generation(
        self,
        materials: Iterable[Mapping[str, Any]],
        *,
        committed: bool = False,
        restored_inactive: bool = False,
    ) -> None:
        """Stage one complete generation without changing live dispatch.

        ``committed`` is retained as a restore-path compatibility hint. A
        generation becomes live only through the atomic ``apply_generation``
        (or compatibility ``activate``) operation.

        ``restored_inactive`` retains immutable crash-recovery evidence while
        excluding the generation from every route and command reservation.
        """

        del committed
        candidates = self._candidate_batch(
            materials,
            phase="DRAINING" if restored_inactive else "PREPARED",
        )
        reserved = None if restored_inactive else self._reserved_feishu_command
        if reserved is not None:
            for candidate in candidates:
                if candidate.contribution_kind != "feishu":
                    continue
                for command in candidate.declaration["commands"]:
                    try:
                        is_reserved = reserved(command)
                    except Exception as exc:
                        raise PluginConflictError(
                            "reserved Feishu command check failed",
                            code="CONTRIBUTION_ROUTE_CONFLICT",
                        ) from exc
                    if not isinstance(is_reserved, bool):
                        raise PluginConflictError(
                            "reserved Feishu command check returned an invalid result",
                            code="CONTRIBUTION_ROUTE_CONFLICT",
                        )
                    migration_allowed = False
                    if is_reserved and self._migration_reserved_feishu_target is not None:
                        try:
                            migration_allowed = self._migration_reserved_feishu_target(
                                candidate.automation_id,
                                candidate.generation,
                                candidate.contribution_id,
                                command,
                            )
                        except Exception as exc:
                            raise PluginConflictError(
                                "migration Feishu ownership check failed",
                                code="CONTRIBUTION_ROUTE_CONFLICT",
                            ) from exc
                        if not isinstance(migration_allowed, bool):
                            raise PluginConflictError(
                                "migration Feishu ownership check returned an invalid result",
                                code="CONTRIBUTION_ROUTE_CONFLICT",
                            )
                    if is_reserved and not migration_allowed:
                        raise PluginConflictError(
                            "managed Feishu command conflicts with an Action V1 command",
                            code="CONTRIBUTION_ROUTE_CONFLICT",
                        )
        with self._lock:
            registrations = dict(self._registrations)
            automation_id = candidates[0].automation_id
            generation = candidates[0].generation
            if self._active_generations.get(automation_id) == generation:
                existing_ids = {
                    record.registration_id
                    for record in registrations.values()
                    if record.automation_id == automation_id
                    and record.generation == generation
                }
                if any(
                    candidate.registration_id not in existing_ids
                    for candidate in candidates
                ):
                    raise PluginConflictError(
                        "active contribution generation is immutable",
                        code="CONTRIBUTION_REGISTRATION_CONFLICT",
                    )
            for candidate in candidates:
                existing = registrations.get(candidate.registration_id)
                if existing is not None:
                    if canonical_json_bytes(
                        self._material(existing)
                    ) != canonical_json_bytes(self._material(candidate)):
                        raise PluginConflictError(
                            "managed contribution registration identity was reused",
                            code="CONTRIBUTION_REGISTRATION_CONFLICT",
                        )
                    continue
                registrations[candidate.registration_id] = candidate
            route_owners = self._route_index(registrations)
            self._registrations, self._route_owners = registrations, route_owners

    def register(self, material: Mapping[str, Any], *, committed: bool) -> None:
        self.prepare_generation((material,), committed=committed)

    def apply_generation(
        self,
        automation_id: str,
        generation: int,
        *,
        refresh: Callable[[], object],
        expected_registration_ids: Iterable[str] | None = None,
    ) -> None:
        """Refresh physical Jobs, then atomically expose one prepared generation."""

        automation_key = str(automation_id)
        generation_number = int(generation)
        with self._lock:
            candidates = tuple(
                record
                for record in self._registrations.values()
                if record.automation_id == automation_key
                and record.generation == generation_number
            )
            if expected_registration_ids is None:
                expected_ids = tuple(
                    sorted(record.registration_id for record in candidates)
                )
            else:
                expected_ids = tuple(str(value) for value in expected_registration_ids)
                if (
                    len(set(expected_ids)) != len(expected_ids)
                    or any(
                        registration_id
                        != (
                            f"{automation_key}:{generation_number}:"
                            f"{registration_id.rsplit(':', 1)[-1]}"
                        )
                        or not registration_id.rsplit(":", 1)[-1]
                        for registration_id in expected_ids
                    )
                ):
                    raise PluginConflictError(
                        "managed contribution expected registration identities are invalid",
                        code="CONTRIBUTION_REGISTRATION_CONFLICT",
                    )
            candidate_ids = {record.registration_id for record in candidates}
            if candidate_ids != set(expected_ids):
                raise PluginConflictError(
                    "managed contribution prepared set does not match its generation",
                    code="RUNTIME_PROJECTION_STALE",
                )
            if candidates:
                self._candidate_batch(
                    self._material(record) for record in candidates
                )
            registrations = {
                registration_id: (
                    replace(
                        record,
                        phase=(
                            "COMMITTED"
                            if record.generation == generation_number
                            else "DRAINING"
                        ),
                    )
                    if record.automation_id == automation_key
                    else record
                )
                for registration_id, record in self._registrations.items()
            }
            route_owners = self._route_index(registrations)
            active_generations = dict(self._active_generations)
            if expected_ids:
                active_generations[automation_key] = generation_number
            else:
                active_generations.pop(automation_key, None)
            original = (
                dict(self._registrations),
                {key: set(value) for key, value in self._route_owners.items()},
                dict(self._active_generations),
            )
            try:
                self._strict_refresh(refresh)
            except Exception:
                (
                    self._registrations,
                    self._route_owners,
                    self._active_generations,
                ) = original
                raise
            (
                self._registrations,
                self._route_owners,
                self._active_generations,
            ) = registrations, route_owners, active_generations

    def activate(self, automation_id: str, generation: int) -> None:
        with self._lock:
            expected_registration_ids = tuple(
                sorted(
                    record.registration_id
                    for record in self._registrations.values()
                    if record.automation_id == str(automation_id)
                    and record.generation == int(generation)
                )
            )
        self.apply_generation(
            automation_id,
            generation,
            refresh=lambda: {"initialized": True, "invalid_tasks": []},
            expected_registration_ids=expected_registration_ids,
        )

    def withdraw_generation(
        self,
        automation_id: str,
        generation: int,
        *,
        refresh: Callable[[], object],
    ) -> None:
        """Refresh physical Jobs, then atomically withdraw the exact live generation."""

        automation_key = str(automation_id)
        generation_number = int(generation)
        with self._lock:
            target_exists = any(
                record.automation_id == automation_key
                and record.generation == generation_number
                for record in self._registrations.values()
            )
            if not target_exists:
                return
            registrations = {
                registration_id: record
                for registration_id, record in self._registrations.items()
                if not (
                    record.automation_id == automation_key
                    and record.generation == generation_number
                )
            }
            route_owners = self._route_index(registrations)
            active_generations = dict(self._active_generations)
            if active_generations.get(automation_key) == generation_number:
                active_generations.pop(automation_key, None)
            original = (
                dict(self._registrations),
                {key: set(value) for key, value in self._route_owners.items()},
                dict(self._active_generations),
            )
            try:
                self._strict_refresh(refresh)
            except Exception:
                (
                    self._registrations,
                    self._route_owners,
                    self._active_generations,
                ) = original
                raise
            (
                self._registrations,
                self._route_owners,
                self._active_generations,
            ) = registrations, route_owners, active_generations

    def block_project(self, automation_id: str) -> None:
        """Withdraw every live contribution route for one failed transition.

        The immutable registrations are retained so diagnostics can identify
        the exact generations involved.  This method deliberately performs no
        transport refresh; the caller must separately invoke its DB-independent
        emergency Scheduler withdrawal while holding the shared projection
        transaction lock.
        """

        automation_key = str(automation_id)
        if not automation_key or automation_key != automation_key.strip():
            raise PluginConflictError(
                "managed contribution project identity is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        with self._lock:
            registrations = {
                registration_id: (
                    replace(record, phase="DRAINING")
                    if record.automation_id == automation_key
                    else record
                )
                for registration_id, record in self._registrations.items()
            }
            self._registrations = registrations
            self._route_owners = self._route_index(registrations)
            self._active_generations.pop(automation_key, None)

    def unregister(self, registration_id: str) -> None:
        with self._lock:
            key = str(registration_id)
            record = self._registrations.get(key)
            if record is None:
                return
            registrations = dict(self._registrations)
            registrations.pop(key)
            route_owners = self._route_index(registrations)
            active_generations = dict(self._active_generations)
            if not any(
                item.automation_id == record.automation_id
                and item.generation == record.generation
                and item.phase == "COMMITTED"
                for item in registrations.values()
            ):
                if active_generations.get(record.automation_id) == record.generation:
                    active_generations.pop(record.automation_id, None)
            (
                self._registrations,
                self._route_owners,
                self._active_generations,
            ) = registrations, route_owners, active_generations

    def active_generation(self, automation_id: str) -> int | None:
        with self._lock:
            return self._active_generations.get(str(automation_id))

    def resolve_active(
        self,
        automation_id: str,
        generation: int,
        contribution_kind: str,
        contribution_id: str,
    ) -> ManagedContributionRegistration:
        automation_key = str(automation_id)
        generation_number = int(generation)
        kind = str(contribution_kind)
        identity = str(contribution_id)
        with self._lock:
            if self._active_generations.get(automation_key) != generation_number:
                raise PluginConflictError(
                    "requested contribution generation is stale",
                    code="RUNTIME_PROJECTION_STALE",
                )
            matches = tuple(
                record
                for record in self._registrations.values()
                if record.automation_id == automation_key
                and record.generation == generation_number
                and record.contribution_kind == kind
                and record.contribution_id == identity
            )
            if not matches:
                raise PluginConflictError(
                    "requested contribution is unavailable",
                    code="CAPABILITY_UNAVAILABLE",
                )
            if len(matches) != 1:
                raise PluginConflictError(
                    "requested contribution projection is ambiguous",
                    code="RUNTIME_PROJECTION_AMBIGUOUS",
                )
            record = matches[0]
            if record.phase != "COMMITTED":
                raise PluginConflictError(
                    "requested contribution generation is stale",
                    code="RUNTIME_PROJECTION_STALE",
                )
            if (
                record.backend_status != "READY"
                or not self._process_backend_available(record.contribution_kind)
            ):
                raise PluginConflictError(
                    "requested contribution is unavailable",
                    code="CAPABILITY_UNAVAILABLE",
                )
            return self._clone(record)

    def resolve_active_module_slot(
        self,
        *,
        slot: str,
        handle: str,
    ) -> ManagedModuleSlotDispatchTarget:
        """Resolve one safe public handle back to its exact active owner."""

        try:
            normalized_slot = normalize_waybill_entry_slot(slot)
            normalized_handle = normalize_waybill_entry_extension_handle(handle)
        except ValueError as exc:
            raise PluginConflictError(
                "requested module-slot contribution is unavailable",
                code="CAPABILITY_UNAVAILABLE",
            ) from exc
        with self._lock:
            matches = tuple(
                record
                for record in self._registrations.values()
                if record.contribution_kind == "module_slots"
                and record.dispatch_available
                and self._active_generations.get(record.automation_id)
                == record.generation
                and record.declaration.get("slot") == normalized_slot
                and _module_slot_handle(
                    automation_id=record.automation_id,
                    generation=record.generation,
                    declaration=record.declaration,
                )
                == normalized_handle
            )
            if not matches:
                raise PluginConflictError(
                    "requested module-slot contribution is unavailable",
                    code="CAPABILITY_UNAVAILABLE",
                )
            if len(matches) != 1:
                raise PluginConflictError(
                    "requested module-slot contribution is ambiguous",
                    code="RUNTIME_PROJECTION_AMBIGUOUS",
                )
            record = matches[0]
            declaration = copy.deepcopy(dict(record.declaration))
            declaration_sha256 = _digest(declaration)
            return ManagedModuleSlotDispatchTarget(
                automation_id=record.automation_id,
                generation=record.generation,
                contribution_id=record.contribution_id,
                contribution_kind=record.contribution_kind,
                slot=normalized_slot,
                handle=normalized_handle,
                declaration_sha256=declaration_sha256,
                service=record.service,
                operation=record.operation,
                declaration=_freeze_json(declaration),
            )

    def resolve_active_feishu_command(
        self,
        command: str,
    ) -> ManagedFeishuDispatchTarget:
        """Resolve one exact case-sensitive Feishu command without identifiers.

        The registration store may retain a draining predecessor for lease
        diagnostics, but the route index omits it.  Only the one record
        selected by the active generation map is dispatchable; any ambiguity
        in that active surface fails closed.
        """

        if (
            not isinstance(command, str)
            or not command
            or command != command.strip()
            or len(command) > 128
        ):
            raise PluginConflictError(
                "managed Feishu command is unavailable",
                code="CAPABILITY_UNAVAILABLE",
            )
        route_key = f"feishu:command:{_feishu_command_digest(command)}"
        with self._lock:
            owner_ids = tuple(self._route_owners.get(route_key, ()))
            if not owner_ids:
                raise PluginConflictError(
                    "managed Feishu command is unavailable",
                    code="CAPABILITY_UNAVAILABLE",
                )
            records: list[ManagedContributionRegistration] = []
            for registration_id in owner_ids:
                record = self._registrations.get(registration_id)
                if (
                    record is None
                    or record.contribution_kind != "feishu"
                    or route_key not in record.route_keys
                ):
                    raise PluginConflictError(
                        "managed Feishu route projection is invalid",
                        code="RUNTIME_PROJECTION_AMBIGUOUS",
                    )
                if (
                    record.dispatch_available
                    and self._active_generations.get(record.automation_id)
                    == record.generation
                ):
                    records.append(record)
            if not records:
                raise PluginConflictError(
                    "managed Feishu command is unavailable",
                    code="CAPABILITY_UNAVAILABLE",
                )
            if len(records) != 1:
                raise PluginConflictError(
                    "managed Feishu command route is ambiguous",
                    code="RUNTIME_PROJECTION_AMBIGUOUS",
                )
            record = records[0]
            if command not in record.declaration.get("commands", ()):
                raise PluginConflictError(
                    "managed Feishu command route is invalid",
                    code="RUNTIME_PROJECTION_AMBIGUOUS",
                )
            return ManagedFeishuDispatchTarget(
                automation_id=record.automation_id,
                generation=record.generation,
                contribution_id=record.contribution_id,
            )

    def resolve_active_webhook_route(
        self,
        *,
        method: str,
        route: str,
    ) -> ManagedWebhookDispatchTarget | None:
        """Resolve one global exact POST route without caller-supplied ownership."""

        if (
            not isinstance(method, str)
            or not method
            or method != method.strip()
        ):
            raise PluginConflictError(
                "managed Webhook method is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        if method != "POST":
            return None
        if not isinstance(route, str) or not _WEBHOOK_ROUTE_RE.fullmatch(route):
            raise PluginConflictError(
                "managed Webhook route is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        route_key = f"webhook:POST:{route}"
        with self._lock:
            owner_ids = tuple(self._route_owners.get(route_key, ()))
            if not owner_ids:
                return None
            records: list[ManagedContributionRegistration] = []
            for registration_id in owner_ids:
                record = self._registrations.get(registration_id)
                if (
                    record is None
                    or record.contribution_kind != "webhook"
                    or record.route_keys != (route_key,)
                    or record.backend != "managed_webhook_router"
                    or record.backend_status != "READY"
                    or record.reason_code is not None
                    or record.reason_detail is not None
                ):
                    raise PluginConflictError(
                        "managed Webhook route projection is invalid",
                        code="RUNTIME_PROJECTION_AMBIGUOUS",
                    )
                try:
                    declaration_method, declaration_route = (
                        _validated_webhook_declaration(record.declaration)
                    )
                except PluginConflictError as exc:
                    raise PluginConflictError(
                        "managed Webhook route projection is invalid",
                        code="RUNTIME_PROJECTION_AMBIGUOUS",
                    ) from exc
                if declaration_method != method or declaration_route != route:
                    raise PluginConflictError(
                        "managed Webhook route projection is invalid",
                        code="RUNTIME_PROJECTION_AMBIGUOUS",
                    )
                if (
                    record.dispatch_available
                    and self._active_generations.get(record.automation_id)
                    == record.generation
                ):
                    records.append(record)
            if not records:
                return None
            if not self._process_backend_available("webhook"):
                raise PluginConflictError(
                    "managed Webhook backend is unavailable",
                    code="CAPABILITY_UNAVAILABLE",
                )
            if len(records) != 1:
                raise PluginConflictError(
                    "managed Webhook route is ambiguous",
                    code="RUNTIME_PROJECTION_AMBIGUOUS",
                )
            record = records[0]
            return ManagedWebhookDispatchTarget(
                automation_id=record.automation_id,
                generation=record.generation,
                contribution_id=record.contribution_id,
            )

    def resolve_active_event(
        self,
        *,
        event_name: str,
    ) -> ManagedEventDispatchTarget | None:
        """Resolve one global exact non-durable event dispatch target."""

        if not isinstance(event_name, str) or not _EVENT_NAME_RE.fullmatch(event_name):
            raise PluginConflictError(
                "managed event name is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
        route_key = f"event:{event_name}"
        with self._lock:
            owner_ids = tuple(self._route_owners.get(route_key, ()))
            if not owner_ids:
                return None
            records: list[ManagedContributionRegistration] = []
            for registration_id in owner_ids:
                record = self._registrations.get(registration_id)
                if (
                    record is None
                    or record.contribution_kind != "events"
                    or record.route_keys != (route_key,)
                    or record.backend != "managed_event_dispatcher"
                    or record.backend_status != "READY"
                    or record.reason_code is not None
                    or record.reason_detail is not None
                ):
                    raise PluginConflictError(
                        "managed event projection is invalid",
                        code="RUNTIME_PROJECTION_AMBIGUOUS",
                    )
                try:
                    declaration_event, durable = _validated_event_declaration(
                        record.declaration
                    )
                except PluginConflictError as exc:
                    raise PluginConflictError(
                        "managed event projection is invalid",
                        code="RUNTIME_PROJECTION_AMBIGUOUS",
                    ) from exc
                if declaration_event != event_name or durable is not False:
                    raise PluginConflictError(
                        "managed event projection is invalid",
                        code="RUNTIME_PROJECTION_AMBIGUOUS",
                    )
                if (
                    record.dispatch_available
                    and self._active_generations.get(record.automation_id)
                    == record.generation
                ):
                    records.append(record)
            if not records:
                return None
            if not self._process_backend_available("events"):
                raise PluginConflictError(
                    "managed event backend is unavailable",
                    code="CAPABILITY_UNAVAILABLE",
                )
            if len(records) != 1:
                raise PluginConflictError(
                    "managed event route is ambiguous",
                    code="RUNTIME_PROJECTION_AMBIGUOUS",
                )
            record = records[0]
            return ManagedEventDispatchTarget(
                automation_id=record.automation_id,
                generation=record.generation,
                contribution_id=record.contribution_id,
            )

    def active_module_slot_snapshot(
        self,
        *,
        automation_id: str | None = None,
        slot: str | None = None,
    ) -> tuple[Mapping[str, str], ...]:
        """Return only the three host-renderable module-slot fields."""

        normalized_automation_id: str | None = None
        if automation_id is not None:
            normalized_automation_id = str(automation_id)
            if (
                not normalized_automation_id
                or normalized_automation_id != normalized_automation_id.strip()
            ):
                raise ValueError("automation_id is invalid")
        try:
            normalized_slot = (
                normalize_waybill_entry_slot(slot) if slot is not None else None
            )
        except ValueError as exc:
            raise ValueError("module slot is invalid") from exc
        with self._lock:
            projected = []
            for record in self._registrations.values():
                if (
                    record.contribution_kind != "module_slots"
                    or not record.dispatch_available
                    or self._active_generations.get(record.automation_id)
                    != record.generation
                    or (
                        normalized_automation_id is not None
                        and record.automation_id != normalized_automation_id
                    )
                    or (
                        normalized_slot is not None
                        and record.declaration.get("slot") != normalized_slot
                    )
                ):
                    continue
                item = {
                    "slot": normalize_waybill_entry_slot(
                        record.declaration.get("slot")
                    ),
                    "handle": _module_slot_handle(
                        automation_id=record.automation_id,
                        generation=record.generation,
                        declaration=record.declaration,
                    ),
                    "title": str(record.declaration.get("title") or ""),
                }
                projected.append(item)
            return tuple(
                _freeze_json(item)
                for item in sorted(
                    projected,
                    key=lambda value: (
                        value["slot"],
                        value["title"],
                        value["handle"],
                    ),
                )
            )

    def active_snapshot(
        self,
        *,
        automation_id: str | None = None,
        contribution_kind: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return an immutable non-sensitive active projection.

        Harness records intentionally expose only the closed runtime and
        invocation contract required by a future ``HarnessToolCatalog``.  No
        package bytes, source paths, or raw installation metadata cross this
        boundary.  Existing Console/Scheduler callers retain their compact
        projection shape.
        """

        with self._lock:
            records = tuple(
                record
                for record in self._registrations.values()
                if record.dispatch_available
                and self._process_backend_available(record.contribution_kind)
                and self._active_generations.get(record.automation_id)
                == record.generation
                and (automation_id is None or record.automation_id == automation_id)
                and (
                    contribution_kind is None
                    or record.contribution_kind == contribution_kind
                )
            )
            projected: list[Mapping[str, Any]] = []
            for record in sorted(
                records,
                key=lambda item: (
                    item.automation_id,
                    item.generation,
                    item.contribution_kind,
                    item.contribution_id,
                ),
            ):
                if record.contribution_kind == "harness":
                    # Harness receives an identity-only active record.  The
                    # closed contract carries invocation metadata; package
                    # and transport material stay inside the registry.
                    item = {
                        "automation_id": record.automation_id,
                        "generation": record.generation,
                        "contribution_id": record.contribution_id,
                        "contribution_kind": record.contribution_kind,
                    }
                    item.update(
                        {
                            "runtime_model": record.runtime_model,
                            "runtime_permissions": copy.deepcopy(
                                dict(record.runtime_permissions)
                            ),
                            "harness_contract": copy.deepcopy(
                                dict(record.harness_contract)
                            ),
                        }
                    )
                elif record.contribution_kind == "module_slots":
                    item = {
                        "slot": normalize_waybill_entry_slot(
                            record.declaration.get("slot")
                        ),
                        "handle": _module_slot_handle(
                            automation_id=record.automation_id,
                            generation=record.generation,
                            declaration=record.declaration,
                        ),
                        "title": str(record.declaration.get("title") or ""),
                    }
                else:
                    item = {
                        "automation_id": record.automation_id,
                        "generation": record.generation,
                        "contribution_id": record.contribution_id,
                        "contribution_kind": record.contribution_kind,
                        "service": record.service,
                        "operation": record.operation,
                        "backend": record.backend,
                        "backend_status": record.backend_status,
                    }
                projected.append(_freeze_json(item))
            return tuple(projected)

    def active_record_snapshot(
        self,
        *,
        automation_id: str | None = None,
        contribution_kind: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Alias with an explicit name for future catalog providers."""

        return self.active_snapshot(
            automation_id=automation_id,
            contribution_kind=contribution_kind,
        )

    def active_records_snapshot(
        self,
        *,
        automation_id: str | None = None,
        contribution_kind: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Plural compatibility alias for catalog integrations."""

        return self.active_snapshot(
            automation_id=automation_id,
            contribution_kind=contribution_kind,
        )

    def snapshot(
        self,
        *,
        contribution_kind: str | None = None,
    ) -> tuple[ManagedContributionRegistration, ...]:
        with self._lock:
            records = tuple(
                record
                for record in self._registrations.values()
                if contribution_kind is None
                or record.contribution_kind == contribution_kind
            )
            return tuple(
                self._from_material(
                    self._material(record),
                    phase=record.phase,
                )
                for record in sorted(
                    records,
                    key=lambda item: (
                        item.automation_id,
                        item.generation,
                        item.contribution_kind,
                        item.contribution_id,
                    ),
                )
            )


def _closed_service_v2_contributions(
    snapshot: RuntimeGenerationSnapshot,
) -> dict[str, list[dict[str, Any]]]:
    contributions = snapshot.execution_metadata.get("contributions")
    if not isinstance(contributions, Mapping):
        raise PluginConflictError("v2 contribution contract is missing")
    contribution_keys = set(contributions)
    if frozenset(contribution_keys) not in _SERVICE_V2_CONTRIBUTION_KEYSETS:
        if contribution_keys - set(_SERVICE_V2_CONTRIBUTION_KINDS):
            raise PluginConflictError(
                "plugin-provided frontend or unknown contribution is forbidden",
                code="PLUGIN_CUSTOM_FRONTEND_FORBIDDEN",
            )
        raise PluginConflictError("v2 contribution contract is incomplete")
    normalized: dict[str, list[dict[str, Any]]] = {}
    identities: set[str] = set()
    for kind in _SERVICE_V2_CONTRIBUTION_KINDS:
        raw_items = contributions.get(kind, ())
        if not isinstance(raw_items, (list, tuple)):
            raise PluginConflictError("v2 contribution list is invalid")
        items: list[dict[str, Any]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise PluginConflictError("v2 contribution declaration is invalid")
            item = copy.deepcopy(dict(raw_item))
            contribution_id = str(item.get("id") or "")
            service = str(item.get("service") or "")
            operation = str(item.get("operation") or "")
            if (
                not contribution_id
                or contribution_id in identities
                or not service
                or not operation
            ):
                raise PluginConflictError("v2 contribution identity is invalid")
            if kind == "harness":
                _validate_harness_declaration(
                    item,
                    snapshot=snapshot,
                )
            if kind == "module_slots":
                _validate_module_slot_declaration(
                    item,
                    snapshot=snapshot,
                )
            identities.add(contribution_id)
            items.append(item)
        normalized[kind] = items
    enabled = set(snapshot.enabled_entrypoints)
    if not enabled <= identities:
        raise PluginConflictError("enabled v2 contribution is undeclared")
    return normalized


def _provider_operation_effects(
    snapshot: RuntimeGenerationSnapshot,
) -> dict[tuple[str, str], CapabilityEffect]:
    contracts = snapshot.execution_metadata.get("service_contracts")
    if not isinstance(contracts, Mapping):
        raise PluginConflictError("v2 service contracts are missing")
    raw_provides = contracts.get("provides")
    if not isinstance(raw_provides, (list, tuple)):
        raise PluginConflictError("v2 provided service contracts are invalid")
    result: dict[tuple[str, str], CapabilityEffect] = {}
    for raw_provided in raw_provides:
        if not isinstance(raw_provided, Mapping):
            raise PluginConflictError("v2 provided service contract is invalid")
        service = str(raw_provided.get("service") or "")
        operations = raw_provided.get("operations")
        if not service or not isinstance(operations, (list, tuple)):
            raise PluginConflictError("v2 provided service contract is invalid")
        for raw_operation in operations:
            if not isinstance(raw_operation, Mapping):
                raise PluginConflictError("v2 provided service operation is invalid")
            name = str(raw_operation.get("name") or "")
            try:
                effect = CapabilityEffect(str(raw_operation.get("effect") or ""))
            except (TypeError, ValueError) as exc:
                raise PluginConflictError(
                    "v2 provided service operation effect is invalid"
                ) from exc
            key = (service, name)
            if not name or key in result:
                raise PluginConflictError("v2 provided service operation is ambiguous")
            result[key] = effect
    return result


def _validate_harness_declaration(
    declaration: Mapping[str, Any],
    *,
    snapshot: RuntimeGenerationSnapshot,
) -> None:
    service = str(declaration.get("service") or "")
    operation = str(declaration.get("operation") or "")
    expected_effect = _provider_operation_effects(snapshot).get(
        (service, operation)
    )
    if expected_effect is None:
        raise PluginConflictError(
            "v2 harness contribution effect does not match its Provider operation",
            code="PLUGIN_CONTRACT_INVALID",
        )
    contract = _harness_contract_from_declaration(
        declaration,
        authoritative_effect=expected_effect,
    )
    if (
        contract["harness_allowed"] is not True
        or contract["broker_effect"] != "read"
        or contract["operation_type"] not in {"read", "compute"}
    ):
        raise PluginConflictError(
            "v2 harness contribution governance is not read-only",
            code="CAPABILITY_UNAVAILABLE",
        )


def _validate_module_slot_declaration(
    declaration: Mapping[str, Any],
    *,
    snapshot: RuntimeGenerationSnapshot | None = None,
) -> None:
    expected_fields = {
        "id",
        "slot",
        "title",
        "service",
        "operation",
        "default_enabled",
    }
    if set(declaration) != expected_fields:
        raise PluginConflictError(
            "v2 module-slot contribution declaration is not closed",
            code="PLUGIN_CONTRACT_INVALID",
        )
    for field_name, maximum in (
        ("id", 64),
        ("title", 120),
        ("service", 191),
        ("operation", 128),
    ):
        value = declaration.get(field_name)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > maximum
        ):
            raise PluginConflictError(
                f"v2 module-slot contribution {field_name} is invalid",
                code="PLUGIN_CONTRACT_INVALID",
            )
    try:
        normalize_waybill_entry_slot(declaration.get("slot"))
    except ValueError as exc:
        raise PluginConflictError(
            "v2 module-slot contribution slot is invalid",
            code="PLUGIN_CONTRACT_INVALID",
        ) from exc
    if type(declaration.get("default_enabled")) is not bool:
        raise PluginConflictError(
            "v2 module-slot contribution default_enabled is invalid",
            code="PLUGIN_CONTRACT_INVALID",
        )
    if snapshot is None:
        return
    effect = _provider_operation_effects(snapshot).get(
        (str(declaration["service"]), str(declaration["operation"]))
    )
    if effect not in {CapabilityEffect.READ, CapabilityEffect.COMPUTE}:
        raise PluginConflictError(
            "v2 module-slot contribution Provider effect is not read-only",
            code="CAPABILITY_UNAVAILABLE",
        )
    governance = governance_for_effect(effect).to_mapping()
    if (
        governance["broker_effect"] != "read"
        or governance["operation_type"] not in {"read", "compute"}
    ):
        raise PluginConflictError(
            "v2 module-slot contribution governance is not read-only",
            code="CAPABILITY_UNAVAILABLE",
        )


def _validated_webhook_declaration(
    declaration: Mapping[str, Any],
) -> tuple[str, str]:
    method = declaration.get("method")
    route = declaration.get("route")
    if (
        not isinstance(method, str)
        or method != "POST"
        or not isinstance(route, str)
        or not _WEBHOOK_ROUTE_RE.fullmatch(route)
    ):
        raise PluginConflictError(
            "v2 Webhook declaration is invalid",
            code="CONTRIBUTION_REGISTRATION_CONFLICT",
        )
    return method, route


def _validated_event_declaration(
    declaration: Mapping[str, Any],
) -> tuple[str, bool]:
    expected_fields = {
        "id",
        "service",
        "operation",
        "event",
        "durable",
        "default_enabled",
    }
    if set(declaration) != expected_fields:
        raise PluginConflictError(
            "v2 Event declaration is not closed",
            code="CONTRIBUTION_REGISTRATION_CONFLICT",
        )
    for field_name in ("id", "service", "operation"):
        value = declaration.get(field_name)
        if not isinstance(value, str) or not value or value != value.strip():
            raise PluginConflictError(
                "v2 Event declaration identity is invalid",
                code="CONTRIBUTION_REGISTRATION_CONFLICT",
            )
    event_name = declaration.get("event")
    durable = declaration.get("durable")
    default_enabled = declaration.get("default_enabled")
    if (
        not isinstance(event_name, str)
        or not _EVENT_NAME_RE.fullmatch(event_name)
        or type(durable) is not bool
        or type(default_enabled) is not bool
    ):
        raise PluginConflictError(
            "v2 Event declaration is invalid",
            code="CONTRIBUTION_REGISTRATION_CONFLICT",
        )
    return event_name, durable


def _contribution_route_keys(
    *,
    automation_id: str,
    generation: int,
    contribution_kind: str,
    declaration: Mapping[str, Any],
) -> tuple[str, ...]:
    contribution_id = str(declaration.get("id") or "")
    if contribution_kind == "console":
        return (f"console:{automation_id}:{contribution_id}",)
    if contribution_kind == "scheduler":
        return (f"scheduler:{automation_id}:{contribution_id}",)
    if contribution_kind == "harness":
        return (f"harness:{automation_id}:{contribution_id}",)
    if contribution_kind == "module_slots":
        _validate_module_slot_declaration(declaration)
        slot = normalize_waybill_entry_slot(declaration.get("slot"))
        handle = _module_slot_handle(
            automation_id=automation_id,
            generation=generation,
            declaration=declaration,
        )
        return (f"module-slot:{slot}:{handle}",)
    if contribution_kind == "webhook":
        method, route = _validated_webhook_declaration(declaration)
        return (f"webhook:{method}:{route}",)
    if contribution_kind == "feishu":
        commands = declaration.get("commands")
        if not isinstance(commands, (list, tuple)):
            raise PluginConflictError("v2 Feishu commands are invalid")
        return tuple(
            f"feishu:command:{_feishu_command_digest(command)}"
            for command in commands
        )
    if contribution_kind == "events":
        event_name, _durable = _validated_event_declaration(declaration)
        return (f"event:{event_name}",)
    raise PluginConflictError("unsupported managed contribution kind")


def _contribution_backend(
    *,
    contribution_kind: str,
    declaration: Mapping[str, Any],
    project_schedule: Mapping[str, Any],
) -> tuple[str, str, str | None, str | None]:
    if contribution_kind == "console":
        return "managed_console_router", "READY", None, None
    if contribution_kind == "scheduler":
        if project_schedule.get("kind") == "none" or project_schedule.get(
            "enabled"
        ) is not True:
            return (
                "scheduled_tasks",
                "DISABLED",
                None,
                "PROJECT_SCHEDULE_DISABLED",
            )
        # The core-owned project schedule is the sole runnable clock.  A
        # declaration may intentionally omit a static/default schedule; in a
        # migration the enabled project schedule is copied exactly from the
        # v1 source.  Never manufacture a cron or substitute a manifest
        # default here.
        if (
            set(project_schedule) != {"kind", "times", "enabled"}
            or project_schedule.get("kind") not in {"daily_times", "startup"}
            or not isinstance(project_schedule.get("times"), (list, tuple))
        ):
            raise PluginConflictError("v2 project scheduler is invalid")
        schedule_kind = str(project_schedule["kind"])
        schedule_times = tuple(project_schedule["times"])
        if (
            (schedule_kind == "startup" and schedule_times)
            or (
                schedule_kind == "daily_times"
                and (
                    not schedule_times
                    or any(
                        not isinstance(item, str)
                        or _PROJECT_DAILY_TIME_RE.fullmatch(item) is None
                        for item in schedule_times
                    )
                    or len(set(schedule_times)) != len(schedule_times)
                )
            )
        ):
            raise PluginConflictError("v2 project scheduler is invalid")
        return "scheduled_tasks", "READY", None, None
    if contribution_kind == "harness":
        # This is a catalog projection only.  It does not start a Harness
        # runtime or expose a transport; a later HarnessToolCatalog consumes
        # the immutable active record through the registry snapshot.
        return "harness_tool_catalog", "READY", None, None
    if contribution_kind == "module_slots":
        _validate_module_slot_declaration(declaration)
        return "managed_module_slot_host", "READY", None, None
    if contribution_kind == "feishu":
        return "managed_feishu_router", "READY", None, None
    if contribution_kind == "webhook":
        _validated_webhook_declaration(declaration)
        return "managed_webhook_router", "READY", None, None
    if contribution_kind == "events":
        _event_name, durable = _validated_event_declaration(declaration)
        if durable is False:
            return "managed_event_dispatcher", "READY", None, None
        return (
            "managed_event_subscriptions",
            "CAPABILITY_UNAVAILABLE",
            "CAPABILITY_UNAVAILABLE",
            "EVENTS_HOST_BACKEND_UNAVAILABLE",
        )
    raise PluginConflictError(
        "unsupported managed contribution kind",
        code="CONTRIBUTION_REGISTRATION_CONFLICT",
    )


def _contribution_registration_material(
    snapshot: RuntimeGenerationSnapshot,
    *,
    contribution_kind: str,
    declaration: Mapping[str, Any],
) -> dict[str, Any]:
    project_schedule = snapshot.execution_metadata.get("schedule")
    if not isinstance(project_schedule, Mapping):
        raise PluginConflictError("generation project schedule is invalid")
    if contribution_kind == "module_slots":
        _validate_module_slot_declaration(declaration, snapshot=snapshot)
    backend, backend_status, reason_code, reason_detail = _contribution_backend(
        contribution_kind=contribution_kind,
        declaration=declaration,
        project_schedule=project_schedule,
    )
    contribution_id = str(declaration.get("id") or "")
    material = {
        "contract_version": _CONTRIBUTION_EFFECT_CONTRACT_VERSION,
        "registration_id": (
            f"{snapshot.automation_id}:{snapshot.generation}:{contribution_id}"
        ),
        "automation_id": snapshot.automation_id,
        "generation": snapshot.generation,
        "plugin_id": snapshot.plugin_id,
        "plugin_version": snapshot.plugin_version,
        "package_sha256": _required_sha(snapshot.package_sha256, "package_sha256"),
        "manifest_sha256": _required_sha(snapshot.manifest_sha256, "manifest_sha256"),
        "contribution_id": contribution_id,
        "contribution_kind": contribution_kind,
        "service": str(declaration.get("service") or ""),
        "operation": str(declaration.get("operation") or ""),
        "declaration": copy.deepcopy(dict(declaration)),
        "declaration_sha256": _digest(dict(declaration)),
        "route_keys": list(
            _contribution_route_keys(
                automation_id=snapshot.automation_id,
                generation=snapshot.generation,
                contribution_kind=contribution_kind,
                declaration=declaration,
            )
        ),
        "backend": backend,
        "backend_status": backend_status,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "project_schedule": copy.deepcopy(dict(project_schedule)),
        "schedule_sha256": _required_sha(snapshot.schedule_sha256, "schedule_sha256"),
    }
    if contribution_kind == "harness":
        runtime_descriptor = snapshot.execution_metadata.get("runtime_descriptor")
        if not isinstance(runtime_descriptor, Mapping):
            raise PluginConflictError(
                "harness runtime descriptor is missing",
                code="PLUGIN_CONTRACT_INVALID",
            )
        runtime_permissions = runtime_descriptor.get("runtime_permissions")
        expected_effect = _provider_operation_effects(snapshot).get(
            (str(declaration.get("service") or ""), str(declaration.get("operation") or ""))
        )
        if expected_effect is None:
            raise PluginConflictError(
                "harness contribution targets an unknown Provider operation",
                code="PLUGIN_CONTRACT_INVALID",
            )
        material.update(
            {
                "runtime_model": "SERVICE_V2",
                "runtime_permissions": _harness_runtime_permissions(
                    runtime_permissions
                ),
                "harness_contract": _harness_contract_from_declaration(
                    declaration,
                    authoritative_effect=expected_effect,
                ),
            }
        )
    return material


def _validated_managed_contribution_effect_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild one closed durable contribution payload before projection."""

    base_fields = {
        "contract_version",
        "registration_id",
        "automation_id",
        "generation",
        "plugin_id",
        "plugin_version",
        "package_sha256",
        "manifest_sha256",
        "contribution_id",
        "contribution_kind",
        "service",
        "operation",
        "declaration",
        "declaration_sha256",
        "route_keys",
        "backend",
        "backend_status",
        "reason_code",
        "reason_detail",
        "project_schedule",
        "schedule_sha256",
    }
    is_harness = payload.get("contribution_kind") == "harness"
    required_fields = (
        base_fields | {"runtime_model", "runtime_permissions", "harness_contract"}
        if is_harness
        else base_fields
    )
    if set(payload) != required_fields:
        raise PluginConflictError("managed contribution effect payload is invalid")
    if payload.get("contract_version") != _CONTRIBUTION_EFFECT_CONTRACT_VERSION:
        raise PluginConflictError(
            "managed contribution effect contract version is invalid"
        )
    generation = payload.get("generation")
    if type(generation) is not int or generation <= 0:
        raise PluginConflictError("managed contribution generation is invalid")
    strings = {
        field: str(payload.get(field) or "")
        for field in (
            "registration_id",
            "automation_id",
            "plugin_id",
            "plugin_version",
            "contribution_id",
            "contribution_kind",
            "service",
            "operation",
            "backend",
            "backend_status",
        )
    }
    if (
        not all(strings.values())
        or strings["contribution_kind"] not in _MANAGED_CONTRIBUTION_KINDS
        or strings["backend_status"]
        not in {"READY", "DISABLED", "CAPABILITY_UNAVAILABLE"}
        or strings["registration_id"]
        != f"{strings['automation_id']}:{generation}:{strings['contribution_id']}"
    ):
        raise PluginConflictError("managed contribution effect identity is invalid")
    declaration = payload.get("declaration")
    project_schedule = payload.get("project_schedule")
    route_keys = payload.get("route_keys")
    if (
        not isinstance(declaration, Mapping)
        or not isinstance(project_schedule, Mapping)
        or not isinstance(route_keys, list)
        or not route_keys
        or any(not isinstance(item, str) or not item for item in route_keys)
        or len(route_keys) != len(set(route_keys))
    ):
        raise PluginConflictError(
            "managed contribution effect declaration is invalid"
        )
    normalized_declaration = copy.deepcopy(dict(declaration))
    normalized_schedule = copy.deepcopy(dict(project_schedule))
    if (
        str(normalized_declaration.get("id") or "") != strings["contribution_id"]
        or str(normalized_declaration.get("service") or "") != strings["service"]
        or str(normalized_declaration.get("operation") or "") != strings["operation"]
        or payload.get("declaration_sha256") != _digest(normalized_declaration)
        or _required_sha(payload.get("schedule_sha256"), "schedule_sha256")
        != _digest(normalized_schedule)
    ):
        raise PluginConflictError("managed contribution effect digest is invalid")
    expected_routes = _contribution_route_keys(
        automation_id=strings["automation_id"],
        generation=generation,
        contribution_kind=strings["contribution_kind"],
        declaration=normalized_declaration,
    )
    expected_backend = _contribution_backend(
        contribution_kind=strings["contribution_kind"],
        declaration=normalized_declaration,
        project_schedule=normalized_schedule,
    )
    observed_backend = (
        strings["backend"],
        strings["backend_status"],
        payload.get("reason_code"),
        payload.get("reason_detail"),
    )
    if tuple(route_keys) != expected_routes or observed_backend != expected_backend:
        raise PluginConflictError(
            "managed contribution backend declaration is invalid"
        )
    material = {
        "contract_version": _CONTRIBUTION_EFFECT_CONTRACT_VERSION,
        **strings,
        "generation": generation,
        "package_sha256": _required_sha(
            payload.get("package_sha256"), "package_sha256"
        ),
        "manifest_sha256": _required_sha(
            payload.get("manifest_sha256"), "manifest_sha256"
        ),
        "declaration": normalized_declaration,
        "declaration_sha256": _digest(normalized_declaration),
        "route_keys": list(expected_routes),
        "reason_code": payload.get("reason_code"),
        "reason_detail": payload.get("reason_detail"),
        "project_schedule": normalized_schedule,
        "schedule_sha256": _digest(normalized_schedule),
    }
    if not is_harness:
        return material
    if payload.get("runtime_model") != PluginRuntimeModel.SERVICE_V2.value:
        raise PluginConflictError(
            "managed Harness runtime model is invalid",
            code="PLUGIN_CONTRACT_INVALID",
        )
    raw_harness_contract = payload.get("harness_contract")
    if not isinstance(raw_harness_contract, Mapping):
        raise PluginConflictError(
            "managed Harness contract is invalid",
            code="PLUGIN_CONTRACT_INVALID",
        )
    expected_harness_contract = _harness_contract_from_declaration(
        normalized_declaration
    )
    if canonical_json_bytes(dict(raw_harness_contract)) != canonical_json_bytes(
        expected_harness_contract
    ):
        raise PluginConflictError(
            "managed Harness contract is invalid",
            code="PLUGIN_CONTRACT_INVALID",
        )
    material.update(
        {
            "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
            "runtime_permissions": _harness_runtime_permissions(
                payload.get("runtime_permissions")
            ),
            "harness_contract": expected_harness_contract,
        }
    )
    return material


def _service_v2_contribution_effect_plans(
    snapshot: RuntimeGenerationSnapshot,
) -> tuple[RuntimeEffectPlan, ...]:
    contributions = _closed_service_v2_contributions(snapshot)
    enabled = set(snapshot.enabled_entrypoints)
    enabled_schedulers = [
        item
        for item in contributions["scheduler"]
        if str(item.get("id") or "") in enabled
    ]
    project_schedule = snapshot.execution_metadata.get("schedule")
    if (
        isinstance(project_schedule, Mapping)
        and project_schedule.get("kind") != "none"
        and project_schedule.get("enabled") is True
        and len(enabled_schedulers) != 1
    ):
        raise PluginConflictError(
            "an active project schedule requires exactly one scheduler contribution",
            code="PLUGIN_SCHEDULE_CONTRIBUTION_AMBIGUOUS",
        )
    effect_kinds = {
        "console": RuntimeEffectKind.CONTRIBUTION_REGISTRATION,
        "scheduler": RuntimeEffectKind.SCHEDULE_BINDING,
        "webhook": RuntimeEffectKind.WEBHOOK_BINDING,
        "feishu": RuntimeEffectKind.CONTRIBUTION_REGISTRATION,
        "events": RuntimeEffectKind.CONTRIBUTION_REGISTRATION,
        "harness": RuntimeEffectKind.CONTRIBUTION_REGISTRATION,
        "module_slots": RuntimeEffectKind.CONTRIBUTION_REGISTRATION,
    }
    plans: list[RuntimeEffectPlan] = []
    for kind in _MANAGED_CONTRIBUTION_KINDS:
        for declaration in contributions[kind]:
            contribution_id = str(declaration.get("id") or "")
            if contribution_id not in enabled:
                continue
            material = _contribution_registration_material(
                snapshot,
                contribution_kind=kind,
                declaration=declaration,
            )
            plans.append(
                RuntimeEffectPlan(
                    effect_kinds[kind],
                    (
                        f"contribution:{kind}:{snapshot.automation_id}:"
                        f"{snapshot.generation}:{contribution_id}"
                    ),
                    material,
                )
            )
    return tuple(plans)


def _service_registration_material(
    snapshot: RuntimeGenerationSnapshot,
) -> dict[str, Any]:
    """Return the closed package-level service claim for a v2 generation."""

    if snapshot.runtime_model is not PluginRuntimeModel.SERVICE_V2:
        raise PluginConflictError("service registration requires a v2 generation")
    contracts = snapshot.execution_metadata.get("service_contracts")
    descriptor = snapshot.execution_metadata.get("runtime_descriptor")
    if not isinstance(contracts, Mapping) or not isinstance(descriptor, Mapping):
        raise PluginConflictError("v2 service generation contract is missing")
    raw_provides = contracts.get("provides")
    raw_requires = contracts.get("requires")
    raw_account_roles = descriptor.get("account_roles")
    raw_resource_roles = descriptor.get("resource_roles")
    runtime = descriptor.get("runtime")
    if (
        not isinstance(raw_provides, (list, tuple))
        or not isinstance(raw_requires, (list, tuple))
        or not isinstance(runtime, Mapping)
    ):
        raise PluginConflictError("v2 service generation contract is invalid")
    provides: list[dict[str, Any]] = []
    for item in raw_provides:
        if not isinstance(item, Mapping):
            raise PluginConflictError("v2 provided service contract is invalid")
        provides.append(
            {
                "service": str(item.get("service") or ""),
                "operations": _service_operation_material(item.get("operations")),
            }
        )
    requires: list[str] = []
    for item in raw_requires:
        if not isinstance(item, Mapping):
            raise PluginConflictError("v2 required service contract is invalid")
        requires.append(str(item.get("service") or ""))
    try:
        connector_requirements = connector_requirements_from_contracts(
            requirements=(
                item for item in raw_requires if isinstance(item, Mapping)
            ),
            account_roles=(
                (item for item in raw_account_roles if isinstance(item, Mapping))
                if isinstance(raw_account_roles, (list, tuple))
                else ()
            ),
            resource_roles=(
                (item for item in raw_resource_roles if isinstance(item, Mapping))
                if isinstance(raw_resource_roles, (list, tuple))
                else ()
            ),
        )
    except (ConnectorRegistryError, TypeError, ValueError) as exc:
        raise PluginConflictError(
            "v2 Connector requirement contract is invalid"
        ) from exc
    if any(not item["service"] for item in provides) or any(
        not service for service in requires
    ):
        raise PluginConflictError("v2 service names cannot be empty")
    package_sha256 = _required_sha(snapshot.package_sha256, "package_sha256")
    manifest_sha256 = _required_sha(snapshot.manifest_sha256, "manifest_sha256")
    material = {
        "provider_registration_id": package_provider_registration_id(package_sha256),
        "provider_generation": _SERVICE_PROVIDER_GENERATION,
        "reference_id": f"{snapshot.automation_id}:{snapshot.generation}",
        "plugin_id": snapshot.plugin_id,
        "plugin_version": snapshot.plugin_version,
        "package_sha256": package_sha256,
        "manifest_sha256": manifest_sha256,
        "runtime_mode": str(runtime.get("mode") or ""),
        "provides": provides,
        "requires": requires,
        "service_contracts_sha256": _digest(
            {"provides": provides, "requires": [{"service": item} for item in requires]}
        ),
    }
    if connector_requirements:
        material["connector_requirements"] = [
            item.to_mapping() for item in connector_requirements
        ]
    return material


def _validated_service_registration_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one durable service effect, including exact Connector roles."""

    base_fields = {
        "provider_registration_id",
        "provider_generation",
        "reference_id",
        "plugin_id",
        "plugin_version",
        "package_sha256",
        "manifest_sha256",
        "runtime_mode",
        "provides",
        "requires",
        "service_contracts_sha256",
    }
    raw_requires = payload.get("requires")
    if not isinstance(raw_requires, list) or not all(
        isinstance(item, str) and item for item in raw_requires
    ):
        raise PluginConflictError("service registration effect contract is invalid")
    requires = [str(item) for item in raw_requires]
    connector_services = tuple(
        service for service in requires if service.startswith("connector.")
    )
    expected_fields = (
        base_fields | {"connector_requirements"}
        if connector_services
        else base_fields
    )
    if set(payload) != expected_fields:
        raise PluginConflictError("service registration effect payload is invalid")

    package_sha256 = _required_sha(payload.get("package_sha256"), "package_sha256")
    manifest_sha256 = _required_sha(
        payload.get("manifest_sha256"),
        "manifest_sha256",
    )
    registration_id = str(payload.get("provider_registration_id") or "")
    if registration_id != package_provider_registration_id(package_sha256):
        raise PluginConflictError("service package registration identity is invalid")
    if payload.get("provider_generation") != _SERVICE_PROVIDER_GENERATION:
        raise PluginConflictError("service provider generation is invalid")
    reference_id = str(payload.get("reference_id") or "")
    plugin_id = str(payload.get("plugin_id") or "")
    plugin_version = str(payload.get("plugin_version") or "")
    runtime_mode = str(payload.get("runtime_mode") or "")
    raw_provides = payload.get("provides")
    if (
        not reference_id
        or not plugin_id
        or not plugin_version
        or runtime_mode not in {"on_demand", "resident"}
        or not isinstance(raw_provides, list)
        or not all(isinstance(item, Mapping) for item in raw_provides)
    ):
        raise PluginConflictError("service registration effect contract is invalid")
    provides = [copy.deepcopy(dict(item)) for item in raw_provides]
    seen_services: set[str] = set()
    for provided in provides:
        if set(provided) != {"service", "operations"}:
            raise PluginConflictError("service registration effect contract is invalid")
        service = str(provided.get("service") or "")
        operations = provided.get("operations")
        if (
            not service
            or service in seen_services
            or not isinstance(operations, list)
            or not operations
        ):
            raise PluginConflictError("service registration effect contract is invalid")
        seen_services.add(service)
        operation_names: set[str] = set()
        for operation in operations:
            if not isinstance(operation, Mapping) or set(operation) != {
                "name",
                "effect",
            }:
                raise PluginConflictError(
                    "service registration effect contract is invalid"
                )
            name = str(operation.get("name") or "")
            try:
                CapabilityEffect(str(operation.get("effect") or ""))
            except (TypeError, ValueError) as exc:
                raise PluginConflictError(
                    "service registration effect contract is invalid"
                ) from exc
            if not name or name != name.strip() or name in operation_names:
                raise PluginConflictError(
                    "service registration effect contract is invalid"
                )
            operation_names.add(name)

    connector_requirements: list[dict[str, object]] = []
    if connector_services:
        raw_connector_requirements = payload.get("connector_requirements")
        if not isinstance(raw_connector_requirements, list) or not all(
            isinstance(item, Mapping) for item in raw_connector_requirements
        ):
            raise PluginConflictError("Connector registration contract is invalid")
        try:
            parsed_connector_requirements = tuple(
                connector_requirement_from_mapping(item)
                for item in raw_connector_requirements
            )
        except (ConnectorRegistryError, TypeError, ValueError) as exc:
            raise PluginConflictError(
                "Connector registration contract is invalid"
            ) from exc
        if tuple(item.service for item in parsed_connector_requirements) != (
            connector_services
        ):
            raise PluginConflictError("Connector registration contract is invalid")
        connector_requirements = [
            item.to_mapping() for item in parsed_connector_requirements
        ]

    expected_contract_sha = _digest(
        {"provides": provides, "requires": [{"service": item} for item in requires]}
    )
    if payload.get("service_contracts_sha256") != expected_contract_sha:
        raise PluginConflictError("service registration effect digest is invalid")
    material = {
        "provider_registration_id": registration_id,
        "provider_generation": _SERVICE_PROVIDER_GENERATION,
        "reference_id": reference_id,
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "package_sha256": package_sha256,
        "manifest_sha256": manifest_sha256,
        "runtime_mode": runtime_mode,
        "provides": provides,
        "requires": requires,
        "service_contracts_sha256": expected_contract_sha,
    }
    if connector_requirements:
        material["connector_requirements"] = connector_requirements
    return material


__all__ = [
    "ManagedContributionRegistration",
    "ManagedContributionRegistry",
    "ManagedEventDispatchTarget",
    "ManagedModuleSlotDispatchTarget",
    "ManagedWebhookDispatchTarget",
]
