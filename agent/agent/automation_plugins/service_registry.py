"""Generation-aware in-memory registry for schema-v2 plugin services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
from threading import RLock
from typing import Any, Mapping

from agent.automation_plugins.errors import AutomationPluginError, PluginConflictError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2


class ServiceRegistryError(AutomationPluginError):
    code = "SERVICE_REGISTRY_ERROR"


class ServiceProviderConflict(PluginConflictError):
    code = "SERVICE_PROVIDER_CONFLICT"


class StaleServiceGeneration(ServiceRegistryError):
    code = "SERVICE_GENERATION_STALE"


class ServiceUnavailable(ServiceRegistryError):
    code = "SERVICE_UNAVAILABLE"


class ServiceOperationUnavailable(ServiceRegistryError):
    code = "SERVICE_OPERATION_UNDECLARED"


class ServiceRegistrationState(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"


@dataclass(frozen=True)
class DependencyBlockReason:
    code: str
    service: str
    message: str
    provider_automation_id: str | None = None
    provider_generation: int | None = None


@dataclass(frozen=True)
class ServiceProvider:
    service: str
    operations: tuple[str, ...]
    automation_id: str
    generation: int
    plugin_id: str
    plugin_version: str
    package_sha256: str
    manifest_sha256: str
    runtime_mode: str
    active: bool


@dataclass(frozen=True)
class ServiceProviderReference:
    """One project generation that may execute an immutable package Provider."""

    provider_automation_id: str
    automation_id: str
    generation: int
    package_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class ServiceRegistration:
    automation_id: str
    generation: int
    plugin_id: str
    plugin_version: str
    package_sha256: str
    manifest_sha256: str
    provided_services: tuple[ServiceProvider, ...]
    required_services: tuple[str, ...]
    state: ServiceRegistrationState
    blocking_reasons: tuple[DependencyBlockReason, ...] = ()

    @property
    def active(self) -> bool:
        return self.state is ServiceRegistrationState.ACTIVE


def _validate_automation_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 128
    ):
        raise ServiceRegistryError(
            "automation_id must be a non-empty string no longer than 128"
        )
    return value


def _validate_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ServiceRegistryError("generation must be a positive integer")
    return value


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_sha256(value: object, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ServiceRegistryError(f"{field} must be a lowercase SHA-256 digest")
    return normalized


def package_provider_registration_id(package_sha256: object) -> str:
    """Return the stable registry owner for one immutable ZIP package.

    Project instances deliberately do not own service names.  Multiple
    projects referencing the same package therefore converge on this single
    owner, while a byte-different package receives a distinct owner and must
    pass the registry's single-provider conflict check.
    """

    return f"package:{_validate_sha256(package_sha256, 'package_sha256')}"


class ServiceRegistry:
    """Own service claims and expose only dependency-ready providers.

    A registration claims all of its provided service names even while it is
    blocked. This prevents a second package from taking a name that could
    become active later. Registering a new generation for the same automation
    replaces its complete claim set in one lock-held commit.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._registrations: dict[str, ServiceRegistration] = {}
        self._service_owners: dict[str, tuple[str, int]] = {}
        self._provider_references: dict[
            str,
            dict[tuple[str, int], ServiceProviderReference],
        ] = {}

    def register(
        self,
        *,
        automation_id: str,
        generation: int,
        manifest: AutomationPluginManifestV2 | Mapping[str, Any],
    ) -> ServiceRegistration:
        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        parsed = (
            manifest
            if isinstance(manifest, AutomationPluginManifestV2)
            else AutomationPluginManifestV2.from_mapping(manifest)
        )
        incoming = self._registration_from_manifest(
            automation_id=automation_id,
            generation=generation,
            manifest=parsed,
        )
        return self._register_incoming(incoming)

    def register_contract(
        self,
        *,
        automation_id: str,
        generation: int,
        plugin_id: str,
        plugin_version: str,
        package_sha256: str,
        manifest_sha256: str,
        runtime_mode: str,
        provides: tuple[Mapping[str, Any], ...],
        requires: tuple[str, ...],
    ) -> ServiceRegistration:
        """Restore a validated package claim from an immutable generation."""

        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        if not plugin_id or not plugin_version:
            raise ServiceRegistryError("persisted service identity is invalid")
        package_sha256 = _validate_sha256(package_sha256, "package_sha256")
        manifest_sha256 = _validate_sha256(manifest_sha256, "manifest_sha256")
        if runtime_mode not in {"on_demand", "resident"}:
            raise ServiceRegistryError("persisted service runtime mode is invalid")
        if not isinstance(provides, (list, tuple)) or not provides:
            raise ServiceRegistryError("persisted provided services are invalid")
        if not isinstance(requires, (list, tuple)):
            raise ServiceRegistryError("persisted required services are invalid")
        providers: list[ServiceProvider] = []
        seen: set[str] = set()
        for item in provides:
            service = str(item.get("service") or "")
            operations = item.get("operations")
            if (
                not service.startswith(f"plugin.{plugin_id}.")
                or "@" not in service
                or service in seen
                or not isinstance(operations, (list, tuple))
                or not operations
            ):
                raise ServiceRegistryError("persisted provided service is invalid")
            normalized_operations = tuple(str(value or "") for value in operations)
            if any(not value for value in normalized_operations) or len(
                set(normalized_operations)
            ) != len(normalized_operations):
                raise ServiceRegistryError("persisted service operations are invalid")
            seen.add(service)
            providers.append(
                ServiceProvider(
                    service=service,
                    operations=normalized_operations,
                    automation_id=automation_id,
                    generation=generation,
                    plugin_id=plugin_id,
                    plugin_version=plugin_version,
                    package_sha256=package_sha256,
                    manifest_sha256=manifest_sha256,
                    runtime_mode=runtime_mode,
                    active=False,
                )
            )
        normalized_requires = tuple(str(value or "") for value in requires)
        if any(not value for value in normalized_requires) or len(
            set(normalized_requires)
        ) != len(normalized_requires):
            raise ServiceRegistryError("persisted required services are invalid")
        incoming = ServiceRegistration(
            automation_id=automation_id,
            generation=generation,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            package_sha256=package_sha256,
            manifest_sha256=manifest_sha256,
            provided_services=tuple(providers),
            required_services=normalized_requires,
            state=ServiceRegistrationState.BLOCKED_DEPENDENCY,
        )
        return self._register_incoming(incoming)

    def _register_incoming(
        self,
        incoming: ServiceRegistration,
    ) -> ServiceRegistration:
        automation_id = incoming.automation_id
        generation = incoming.generation
        with self._lock:
            current = self._registrations.get(automation_id)
            if current is not None:
                if generation < current.generation:
                    raise StaleServiceGeneration(
                        f"generation {generation} is older than current generation "
                        f"{current.generation} for {automation_id}"
                    )
                if generation == current.generation:
                    if (
                        current.package_sha256 != incoming.package_sha256
                        or current.manifest_sha256 != incoming.manifest_sha256
                        or not self._same_contract(current, incoming)
                    ):
                        raise ServiceProviderConflict(
                            "the same automation generation cannot change its manifest"
                        )
                    return current

            candidate_registrations = dict(self._registrations)
            candidate_registrations[automation_id] = incoming
            candidate_owners = self._build_owners(candidate_registrations)
            reconciled = self._reconcile(candidate_registrations, candidate_owners)
            self._registrations = reconciled
            self._service_owners = candidate_owners
            return self._registrations[automation_id]

    @staticmethod
    def _same_contract(
        current: ServiceRegistration,
        incoming: ServiceRegistration,
    ) -> bool:
        def providers(
            registration: ServiceRegistration,
        ) -> tuple[tuple[str, tuple[str, ...]], ...]:
            return tuple(
                (provider.service, provider.operations)
                for provider in registration.provided_services
            )

        return (
            current.plugin_id == incoming.plugin_id
            and current.plugin_version == incoming.plugin_version
            and current.package_sha256 == incoming.package_sha256
            and current.manifest_sha256 == incoming.manifest_sha256
            and current.required_services == incoming.required_services
            and providers(current) == providers(incoming)
            and tuple(
                provider.runtime_mode for provider in current.provided_services
            )
            == tuple(
                provider.runtime_mode for provider in incoming.provided_services
            )
        )

    def unregister(
        self,
        automation_id: str,
        *,
        generation: int | None = None,
    ) -> bool:
        automation_id = _validate_automation_id(automation_id)
        if generation is not None:
            generation = _validate_generation(generation)
        with self._lock:
            current = self._registrations.get(automation_id)
            if current is None:
                return False
            if generation is not None and current.generation != generation:
                return False
            candidate_registrations = dict(self._registrations)
            del candidate_registrations[automation_id]
            candidate_owners = self._build_owners(candidate_registrations)
            self._registrations = self._reconcile(
                candidate_registrations,
                candidate_owners,
            )
            self._service_owners = candidate_owners
            self._provider_references.pop(automation_id, None)
            return True

    def registration(self, automation_id: str) -> ServiceRegistration | None:
        automation_id = _validate_automation_id(automation_id)
        with self._lock:
            return self._registrations.get(automation_id)

    def blocking_reasons(
        self,
        automation_id: str,
    ) -> tuple[DependencyBlockReason, ...]:
        registration = self.registration(automation_id)
        if registration is None:
            return ()
        return registration.blocking_reasons

    def provider_for(self, service: str) -> ServiceProvider | None:
        with self._lock:
            owner = self._service_owners.get(service)
            if owner is None:
                return None
            registration = self._registrations.get(owner[0])
            if registration is None or not registration.active:
                return None
            return self._provider_from_registration(registration, service)

    def claimed_provider_for(self, service: str) -> ServiceProvider | None:
        """Return the owner even when dependencies prevent invocation."""

        with self._lock:
            owner = self._service_owners.get(service)
            if owner is None:
                return None
            registration = self._registrations.get(owner[0])
            if registration is None:
                return None
            return self._provider_from_registration(registration, service)

    def require_provider(self, service: str) -> ServiceProvider:
        provider = self.provider_for(service)
        if provider is not None:
            return provider
        claimed = self.claimed_provider_for(service)
        if claimed is None:
            raise ServiceUnavailable(
                f"service has no registered provider: {service}",
                code="SERVICE_PROVIDER_MISSING",
            )
        registration = self.registration(claimed.automation_id)
        reasons = () if registration is None else registration.blocking_reasons
        reason_codes = ",".join(reason.code for reason in reasons) or "UNKNOWN"
        raise ServiceUnavailable(
            f"service provider is blocked: {service} ({reason_codes})",
            code="SERVICE_PROVIDER_BLOCKED",
        )

    def require_operation(self, service: str, operation: str) -> ServiceProvider:
        """Return the active Provider only when it declared the exact operation."""

        normalized_operation = str(operation or "").strip()
        if not normalized_operation or normalized_operation != operation:
            raise ServiceOperationUnavailable(
                "service operation is invalid",
                code="SERVICE_OPERATION_UNDECLARED",
            )
        provider = self.require_provider(service)
        if normalized_operation not in provider.operations:
            raise ServiceOperationUnavailable(
                f"service operation is not declared: {service}/{normalized_operation}",
                code="SERVICE_OPERATION_UNDECLARED",
            )
        return provider

    def bind_project_reference(
        self,
        *,
        provider_automation_id: str,
        automation_id: str,
        generation: int,
        package_sha256: str,
        manifest_sha256: str,
    ) -> ServiceProviderReference:
        """Attach one prepared project generation to its immutable Provider."""

        provider_automation_id = _validate_automation_id(provider_automation_id)
        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        package_sha256 = _validate_sha256(package_sha256, "package_sha256")
        manifest_sha256 = _validate_sha256(manifest_sha256, "manifest_sha256")
        with self._lock:
            registration = self._registrations.get(provider_automation_id)
            if registration is None:
                raise ServiceUnavailable(
                    "service package Provider is not registered",
                    code="SERVICE_PROVIDER_MISSING",
                )
            if (
                registration.package_sha256 != package_sha256
                or registration.manifest_sha256 != manifest_sha256
            ):
                raise ServiceProviderConflict(
                    "project reference does not match its immutable Provider"
                )
            reference = ServiceProviderReference(
                provider_automation_id=provider_automation_id,
                automation_id=automation_id,
                generation=generation,
                package_sha256=package_sha256,
                manifest_sha256=manifest_sha256,
            )
            key = (automation_id, generation)
            references = self._provider_references.setdefault(
                provider_automation_id,
                {},
            )
            current = references.get(key)
            if current is not None and current != reference:
                raise ServiceProviderConflict(
                    "project generation reference changed its Provider identity"
                )
            references[key] = reference
            return reference

    def unbind_project_reference(
        self,
        *,
        provider_automation_id: str,
        automation_id: str,
        generation: int,
    ) -> bool:
        provider_automation_id = _validate_automation_id(provider_automation_id)
        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        with self._lock:
            references = self._provider_references.get(provider_automation_id)
            if references is None or (automation_id, generation) not in references:
                return False
            del references[(automation_id, generation)]
            if not references:
                self._provider_references.pop(provider_automation_id, None)
            return True

    def project_references(
        self,
        provider_automation_id: str,
    ) -> tuple[ServiceProviderReference, ...]:
        provider_automation_id = _validate_automation_id(provider_automation_id)
        with self._lock:
            references = self._provider_references.get(provider_automation_id, {})
            return tuple(references[key] for key in sorted(references))

    def snapshot(self) -> tuple[ServiceRegistration, ...]:
        with self._lock:
            return tuple(
                self._registrations[key]
                for key in sorted(self._registrations)
            )

    @staticmethod
    def _registration_from_manifest(
        *,
        automation_id: str,
        generation: int,
        manifest: AutomationPluginManifestV2,
    ) -> ServiceRegistration:
        providers = tuple(
            ServiceProvider(
                service=str(item["service"]),
                operations=tuple(str(operation) for operation in item["operations"]),
                automation_id=automation_id,
                generation=generation,
                plugin_id=manifest.plugin_id,
                plugin_version=manifest.version,
                package_sha256=manifest.manifest_sha256,
                manifest_sha256=manifest.manifest_sha256,
                runtime_mode=str(manifest.runtime["mode"]),
                active=False,
            )
            for item in manifest.provides
        )
        return ServiceRegistration(
            automation_id=automation_id,
            generation=generation,
            plugin_id=manifest.plugin_id,
            plugin_version=manifest.version,
            package_sha256=manifest.manifest_sha256,
            manifest_sha256=manifest.manifest_sha256,
            provided_services=providers,
            required_services=manifest.required_services,
            state=ServiceRegistrationState.BLOCKED_DEPENDENCY,
            blocking_reasons=(),
        )

    @staticmethod
    def _build_owners(
        registrations: Mapping[str, ServiceRegistration],
    ) -> dict[str, tuple[str, int]]:
        owners: dict[str, tuple[str, int]] = {}
        for automation_id in sorted(registrations):
            registration = registrations[automation_id]
            for provider in registration.provided_services:
                existing = owners.get(provider.service)
                if existing is not None and existing[0] != automation_id:
                    raise ServiceProviderConflict(
                        f"service {provider.service} is already claimed by "
                        f"{existing[0]} generation {existing[1]}"
                    )
                owners[provider.service] = (automation_id, registration.generation)
        return owners

    @classmethod
    def _reconcile(
        cls,
        registrations: Mapping[str, ServiceRegistration],
        owners: Mapping[str, tuple[str, int]],
    ) -> dict[str, ServiceRegistration]:
        active: set[str] = set()
        changed = True
        while changed:
            changed = False
            for automation_id in sorted(registrations):
                if automation_id in active:
                    continue
                registration = registrations[automation_id]
                if all(
                    (owner := owners.get(service)) is not None and owner[0] in active
                    for service in registration.required_services
                ):
                    active.add(automation_id)
                    changed = True

        result: dict[str, ServiceRegistration] = {}
        for automation_id, registration in registrations.items():
            is_active = automation_id in active
            reasons = () if is_active else cls._blocking_reasons_for(
                registration,
                registrations=registrations,
                owners=owners,
                active=active,
            )
            providers = tuple(
                replace(provider, active=is_active)
                for provider in registration.provided_services
            )
            result[automation_id] = replace(
                registration,
                provided_services=providers,
                state=(
                    ServiceRegistrationState.ACTIVE
                    if is_active
                    else ServiceRegistrationState.BLOCKED_DEPENDENCY
                ),
                blocking_reasons=reasons,
            )
        return result

    @staticmethod
    def _blocking_reasons_for(
        registration: ServiceRegistration,
        *,
        registrations: Mapping[str, ServiceRegistration],
        owners: Mapping[str, tuple[str, int]],
        active: set[str],
    ) -> tuple[DependencyBlockReason, ...]:
        reasons: list[DependencyBlockReason] = []
        for service in registration.required_services:
            owner = owners.get(service)
            if owner is None:
                reasons.append(
                    DependencyBlockReason(
                        code="MISSING_PROVIDER",
                        service=service,
                        message=f"required service has no provider: {service}",
                    )
                )
                continue
            provider_automation_id, provider_generation = owner
            if provider_automation_id not in active:
                provider = registrations[provider_automation_id]
                reasons.append(
                    DependencyBlockReason(
                        code="PROVIDER_BLOCKED",
                        service=service,
                        message=(
                            f"required service provider {provider_automation_id} "
                            "is blocked by its own dependencies"
                        ),
                        provider_automation_id=provider_automation_id,
                        provider_generation=provider.generation,
                    )
                )
        if not reasons:
            reasons.append(
                DependencyBlockReason(
                    code="DEPENDENCY_CYCLE",
                    service="",
                    message="service dependency graph contains a cycle",
                )
            )
        return tuple(reasons)

    @staticmethod
    def _provider_from_registration(
        registration: ServiceRegistration,
        service: str,
    ) -> ServiceProvider | None:
        for provider in registration.provided_services:
            if provider.service == service:
                return provider
        return None


__all__ = [
    "DependencyBlockReason",
    "ServiceProvider",
    "ServiceProviderReference",
    "ServiceProviderConflict",
    "ServiceOperationUnavailable",
    "ServiceRegistration",
    "ServiceRegistrationState",
    "ServiceRegistry",
    "ServiceRegistryError",
    "ServiceUnavailable",
    "StaleServiceGeneration",
    "package_provider_registration_id",
]
