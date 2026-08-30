"""Process-local projections for durable Service v2 generation effects."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Callable, Iterable, Mapping

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    PluginRuntimeModel,
    RuntimeEffectKind,
    RuntimeGenerationSnapshot,
)
from agent.automation_plugins.ports import RuntimeEffectPlan
from agent.automation_plugins.service_registry import (
    package_provider_registration_id,
)


_SERVICE_PROVIDER_GENERATION = 1
_CONTRIBUTION_EFFECT_CONTRACT_VERSION = 1
_SERVICE_V2_CONTRIBUTION_KINDS = (
    "console",
    "scheduler",
    "webhook",
    "feishu",
    "events",
)
_MANAGED_CONTRIBUTION_KINDS = _SERVICE_V2_CONTRIBUTION_KINDS
_ACTIVE_CONTRIBUTION_KINDS = ("console", "scheduler")
_SCHEDULE_BACKEND_TIMEZONE = "Asia/Shanghai"


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


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

    @property
    def dispatch_available(self) -> bool:
        return self.phase == "COMMITTED" and self.backend_status == "READY"


class ManagedContributionRegistry:
    """Recoverable registry for host-owned v2 contribution declarations.

    Durable ownership lives in generation effect rows. This registry is an
    indexed process projection rebuilt from those rows at startup; it never
    pretends an unavailable transport backend is runnable.
    """

    def __init__(self, *, lock: Any | None = None) -> None:
        self._lock = lock or RLock()
        self._registrations: dict[str, ManagedContributionRegistration] = {}
        self._route_owners: dict[str, set[str]] = {}
        self._active_generations: dict[str, int] = {}

    @staticmethod
    def _from_material(
        material: Mapping[str, Any],
        *,
        phase: str,
    ) -> ManagedContributionRegistration:
        return ManagedContributionRegistration(
            registration_id=str(material["registration_id"]),
            automation_id=str(material["automation_id"]),
            generation=int(material["generation"]),
            plugin_id=str(material["plugin_id"]),
            plugin_version=str(material["plugin_version"]),
            package_sha256=str(material["package_sha256"]),
            manifest_sha256=str(material["manifest_sha256"]),
            contribution_id=str(material["contribution_id"]),
            contribution_kind=str(material["contribution_kind"]),
            service=str(material["service"]),
            operation=str(material["operation"]),
            declaration=copy.deepcopy(dict(material["declaration"])),
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
        expected_routes = _contribution_route_keys(
            automation_id=candidate.automation_id,
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
    ) -> tuple[ManagedContributionRegistration, ...]:
        try:
            candidates = tuple(
                cls._from_material(material, phase="PREPARED")
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
    ) -> None:
        """Stage one complete generation without changing live dispatch.

        ``committed`` is retained as a restore-path compatibility hint. A
        generation becomes live only through the atomic ``apply_generation``
        (or compatibility ``activate``) operation.
        """

        del committed
        candidates = self._candidate_batch(materials)
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
            if not candidates:
                raise PluginConflictError(
                    "managed contribution generation is not prepared",
                    code="RUNTIME_PROJECTION_STALE",
                )
            self._candidate_batch(self._material(record) for record in candidates)
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
            active_generations[automation_key] = generation_number
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
            if not any(
                record.automation_id == str(automation_id)
                and record.generation == int(generation)
                for record in self._registrations.values()
            ):
                # A Service-v2 package may provide services without any enabled
                # Console/Scheduler contribution. It needs no registry marker.
                return
        self.apply_generation(
            automation_id,
            generation,
            refresh=lambda: {"initialized": True, "invalid_tasks": []},
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
            if record.backend_status != "READY":
                raise PluginConflictError(
                    "requested contribution is unavailable",
                    code="CAPABILITY_UNAVAILABLE",
                )
            return self._clone(record)

    def active_snapshot(
        self,
        *,
        automation_id: str | None = None,
        contribution_kind: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return the non-sensitive, dispatchable committed projection."""

        with self._lock:
            records = tuple(
                record
                for record in self._registrations.values()
                if record.dispatch_available
                and self._active_generations.get(record.automation_id)
                == record.generation
                and (automation_id is None or record.automation_id == automation_id)
                and (
                    contribution_kind is None
                    or record.contribution_kind == contribution_kind
                )
            )
            return tuple(
                {
                    "automation_id": record.automation_id,
                    "generation": record.generation,
                    "contribution_id": record.contribution_id,
                    "contribution_kind": record.contribution_kind,
                    "service": record.service,
                    "operation": record.operation,
                    "backend": record.backend,
                    "backend_status": record.backend_status,
                }
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
    if set(contributions) != set(_SERVICE_V2_CONTRIBUTION_KINDS):
        if set(contributions) - set(_SERVICE_V2_CONTRIBUTION_KINDS):
            raise PluginConflictError(
                "plugin-provided frontend or unknown contribution is forbidden",
                code="PLUGIN_CUSTOM_FRONTEND_FORBIDDEN",
            )
        raise PluginConflictError("v2 contribution contract is incomplete")
    normalized: dict[str, list[dict[str, Any]]] = {}
    identities: set[str] = set()
    for kind in _SERVICE_V2_CONTRIBUTION_KINDS:
        raw_items = contributions.get(kind)
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
            identities.add(contribution_id)
            items.append(item)
        normalized[kind] = items
    enabled = set(snapshot.enabled_entrypoints)
    if not enabled <= identities:
        raise PluginConflictError("enabled v2 contribution is undeclared")
    return normalized


def _contribution_route_keys(
    *,
    automation_id: str,
    contribution_kind: str,
    declaration: Mapping[str, Any],
) -> tuple[str, ...]:
    contribution_id = str(declaration.get("id") or "")
    if contribution_kind == "console":
        return (f"console:{automation_id}:{contribution_id}",)
    if contribution_kind == "scheduler":
        return (f"scheduler:{automation_id}:{contribution_id}",)
    if contribution_kind == "webhook":
        return (f"webhook:{automation_id}:{declaration.get('route')}",)
    if contribution_kind == "feishu":
        commands = declaration.get("commands")
        if not isinstance(commands, (list, tuple)):
            raise PluginConflictError("v2 Feishu commands are invalid")
        return tuple(
            f"feishu:{automation_id}:{_digest(str(command))}"
            for command in commands
        )
    if contribution_kind == "events":
        return (
            f"event:{declaration.get('event')}:{automation_id}:{contribution_id}",
        )
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
        schedule = declaration.get("schedule")
        if not isinstance(schedule, Mapping):
            raise PluginConflictError("v2 scheduler default is invalid")
        timezone_name = str(schedule.get("timezone") or "")
        if project_schedule.get("kind") == "none" or project_schedule.get(
            "enabled"
        ) is not True:
            return (
                "scheduled_tasks",
                "DISABLED",
                None,
                "PROJECT_SCHEDULE_DISABLED",
            )
        if timezone_name != _SCHEDULE_BACKEND_TIMEZONE:
            return (
                "scheduled_tasks",
                "CAPABILITY_UNAVAILABLE",
                "CAPABILITY_UNAVAILABLE",
                "SCHEDULER_TIMEZONE_UNAVAILABLE",
            )
        return "scheduled_tasks", "READY", None, None
    backend = {
        "webhook": "managed_webhook_router",
        "feishu": "managed_feishu_router",
        "events": "managed_event_subscriptions",
    }[contribution_kind]
    return (
        backend,
        "CAPABILITY_UNAVAILABLE",
        "CAPABILITY_UNAVAILABLE",
        f"{contribution_kind.upper()}_HOST_BACKEND_UNAVAILABLE",
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
    backend, backend_status, reason_code, reason_detail = _contribution_backend(
        contribution_kind=contribution_kind,
        declaration=declaration,
        project_schedule=project_schedule,
    )
    contribution_id = str(declaration.get("id") or "")
    return {
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
    if any(not item["service"] for item in provides) or any(
        not service for service in requires
    ):
        raise PluginConflictError("v2 service names cannot be empty")
    package_sha256 = _required_sha(snapshot.package_sha256, "package_sha256")
    manifest_sha256 = _required_sha(snapshot.manifest_sha256, "manifest_sha256")
    return {
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


__all__ = [
    "ManagedContributionRegistration",
    "ManagedContributionRegistry",
]
