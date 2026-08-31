"""Generation-aware in-memory registry for schema-v2 plugin services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
from threading import RLock
from typing import Any, Mapping

from agent.automation_plugins.connector_compatibility import (
    ConnectorRequirementContract,
    connector_requirement_for_service,
    connector_requirement_from_mapping,
    connector_requirements_from_manifest,
    evaluate_connector_requirement,
)
from agent.automation_plugins.connector_registry import (
    ConnectorRegistry,
    ConnectorRegistryError,
)
from agent.automation_plugins.errors import AutomationPluginError, PluginConflictError
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2


class ServiceRegistryError(AutomationPluginError):
    code = "SERVICE_REGISTRY_ERROR"


class ServiceProviderConflict(PluginConflictError):
    code = "SERVICE_PROVIDER_CONFLICT"


class StaleServiceGeneration(ServiceRegistryError):
    code = "SERVICE_GENERATION_STALE"


class ServiceUnavailable(ServiceRegistryError):
    code = "SERVICE_UNAVAILABLE"


class ServiceProviderAmbiguous(ServiceUnavailable):
    """A bare service name has more than one ready package Provider."""

    code = "SERVICE_PROVIDER_AMBIGUOUS"


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
    operation_effects: tuple[tuple[str, CapabilityEffect], ...]
    automation_id: str
    generation: int
    plugin_id: str
    plugin_version: str
    package_sha256: str
    manifest_sha256: str
    runtime_mode: str
    active: bool
    requested_operation: str | None = None

    @property
    def operations(self) -> tuple[str, ...]:
        """The immutable operation names, retained for safe read projections."""

        return tuple(name for name, _effect in self.operation_effects)

    @property
    def effect(self) -> CapabilityEffect:
        """The authoritative effect for a value returned by ``require_operation``."""

        if self.requested_operation is None:
            raise ServiceOperationUnavailable(
                "service Provider has no selected operation",
                code="SERVICE_OPERATION_UNDECLARED",
            )
        for name, effect in self.operation_effects:
            if name == self.requested_operation:
                return effect
        raise ServiceOperationUnavailable(
            "service Provider selected an undeclared operation",
            code="SERVICE_OPERATION_UNDECLARED",
        )

    def effect_for(self, operation: str) -> CapabilityEffect:
        """Return the immutable effect for one exact Provider operation."""

        normalized = _validate_operation_name(operation)
        for name, effect in self.operation_effects:
            if name == normalized:
                return effect
        raise ServiceOperationUnavailable(
            f"service operation is not declared: {self.service}/{normalized}",
            code="SERVICE_OPERATION_UNDECLARED",
        )


@dataclass(frozen=True)
class ServiceProviderReference:
    """One project generation that may execute an immutable package Provider."""

    provider_automation_id: str
    automation_id: str
    generation: int
    package_sha256: str
    manifest_sha256: str
    accepts_new_calls: bool = False


@dataclass(frozen=True)
class ResolvedServiceOperation:
    """The complete immutable identity required to execute one Provider call."""

    provider_registration_id: str
    provider_contract_generation: int
    project_automation_id: str
    project_generation: int
    plugin_id: str
    plugin_version: str
    package_sha256: str
    manifest_sha256: str
    runtime_mode: str
    service: str
    operation: str
    effect: CapabilityEffect

    def __post_init__(self) -> None:
        provider_registration_id = _validate_automation_id(
            self.provider_registration_id
        )
        _validate_generation(self.provider_contract_generation)
        _validate_automation_id(self.project_automation_id)
        _validate_generation(self.project_generation)
        package_sha256 = _validate_sha256(self.package_sha256, "package_sha256")
        _validate_sha256(self.manifest_sha256, "manifest_sha256")
        if (
            provider_registration_id
            != package_provider_registration_id(package_sha256)
        ):
            raise ServiceRegistryError(
                "resolved Provider registration identity does not match its package digest"
            )
        if (
            not isinstance(self.plugin_id, str)
            or not self.plugin_id.strip()
            or self.plugin_id != self.plugin_id.strip()
            or len(self.plugin_id) > 128
            or not isinstance(self.plugin_version, str)
            or not self.plugin_version.strip()
            or self.plugin_version != self.plugin_version.strip()
            or len(self.plugin_version) > 128
        ):
            raise ServiceRegistryError("resolved Provider identity is invalid")
        if self.runtime_mode not in {"on_demand", "resident"}:
            raise ServiceRegistryError("resolved Provider runtime mode is invalid")
        if (
            not isinstance(self.service, str)
            or not self.service.strip()
            or self.service != self.service.strip()
            or len(self.service) > 255
            or not self.service.startswith(f"plugin.{self.plugin_id}.")
            or "@" not in self.service
            or self.service.endswith("@")
        ):
            raise ServiceRegistryError("resolved service is invalid")
        _validate_operation_name(self.operation)
        if not isinstance(self.effect, CapabilityEffect):
            raise ServiceRegistryError("resolved service operation effect is invalid")


@dataclass(frozen=True)
class ServiceProjectRouteTransition:
    """Exact reference state transition that can be rolled back fail-closed."""

    before: tuple[ServiceProviderReference, ...]
    after: tuple[ServiceProviderReference, ...]


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
    connector_requirements: tuple[ConnectorRequirementContract, ...]
    state: ServiceRegistrationState
    blocking_reasons: tuple[DependencyBlockReason, ...] = ()

    @property
    def active(self) -> bool:
        return self.state is ServiceRegistrationState.ACTIVE


@dataclass(frozen=True)
class ServiceProviderReplacement:
    """Legacy exact package replacement token, committed under one lock.

    New Service v2 upgrades should instead register the new immutable package
    alongside the old package and resolve calls through the exact project
    reference.  This token remains for callers that explicitly need to remove
    an unshared Provider as one reversible operation.
    """

    replaced_provider: ServiceRegistration
    replaced_references: tuple[ServiceProviderReference, ...]
    replacement_provider: ServiceRegistration
    replacement_reference: ServiceProviderReference


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


def _validate_operation_name(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized or normalized != value or len(normalized) > 191:
        raise ServiceOperationUnavailable(
            "service operation is invalid",
            code="SERVICE_OPERATION_UNDECLARED",
        )
    return normalized


def _normalize_operation_effects(
    value: object,
) -> tuple[tuple[str, CapabilityEffect], ...]:
    """Validate the v2 operation object form without name-based governance.

    Service operation names are payload-owned and consequently cannot classify
    an operation as read or write.  Only the immutable manifest effect object
    is accepted here; persisted effect payloads use the same close-set.
    """

    if not isinstance(value, (list, tuple)) or not value:
        raise ServiceRegistryError("persisted service operations are invalid")
    normalized: list[tuple[str, CapabilityEffect]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"name", "effect"}:
            raise ServiceRegistryError("persisted service operation is invalid")
        name = _validate_operation_name(item.get("name"))
        if name in seen:
            raise ServiceRegistryError("persisted service operations are invalid")
        try:
            effect = CapabilityEffect(str(item.get("effect") or ""))
        except (TypeError, ValueError) as exc:
            raise ServiceRegistryError("persisted service operation effect is invalid") from exc
        seen.add(name)
        normalized.append((name, effect))
    return tuple(normalized)


def package_provider_registration_id(package_sha256: object) -> str:
    """Return the stable registry owner for one immutable ZIP package.

    Project instances deliberately do not own service names.  Multiple
    projects referencing the same package therefore converge on this single
    owner, while a byte-different package receives a distinct owner and must
    pass the registry's single-provider conflict check.
    """

    return f"package:{_validate_sha256(package_sha256, 'package_sha256')}"


class ServiceRegistry:
    """Own immutable package claims and expose only dependency-ready providers.

    Several packages can provide one service name during generation drain. A
    bare service lookup therefore succeeds only when exactly one ready package
    exists; execution paths must use the project generation's exact reference.
    Registering a new generation for the same package owner replaces that
    package's complete claim set in one lock-held commit.
    """

    def __init__(
        self,
        *,
        lock: Any | None = None,
        connector_registry: ConnectorRegistry | None = None,
    ) -> None:
        """Create the registry, optionally sharing a runtime projection lock.

        Production couples service ownership with contribution route/job
        projection.  Sharing its re-entrant lock prevents readers from seeing
        a half-applied cross-registry switch.  Existing callers keep the
        previous private-lock behavior.
        """

        if lock is not None and (
            not callable(getattr(lock, "acquire", None))
            or not callable(getattr(lock, "release", None))
        ):
            raise TypeError("service registry lock must support acquire/release")
        self._lock = lock if lock is not None else RLock()
        self._connectors = connector_registry or ConnectorRegistry()
        self._registrations: dict[str, ServiceRegistration] = {}
        # Several immutable packages may provide one service name.  Bare-name
        # resolution is valid only when exactly one ready Provider exists;
        # execution should use an exact project/package reference.
        self._service_owners: dict[str, tuple[tuple[str, int], ...]] = {}
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
        connector_requirements: tuple[
            ConnectorRequirementContract | Mapping[str, Any], ...
        ] = (),
    ) -> ServiceRegistration:
        """Restore a validated package claim from an immutable generation."""

        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        incoming = self._registration_from_contract(
            automation_id=automation_id,
            generation=generation,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            package_sha256=package_sha256,
            manifest_sha256=manifest_sha256,
            runtime_mode=runtime_mode,
            provides=provides,
            requires=requires,
            connector_requirements=connector_requirements,
        )
        return self._register_incoming(incoming)

    def replace_package_provider_for_upgrade(
        self,
        *,
        replaced_provider_automation_id: str,
        replacement_provider_automation_id: str,
        replacement_provider_generation: int,
        replacement_plugin_id: str,
        replacement_plugin_version: str,
        replacement_package_sha256: str,
        replacement_manifest_sha256: str,
        replacement_runtime_mode: str,
        replacement_provides: tuple[Mapping[str, Any], ...],
        replacement_requires: tuple[str, ...],
        replacement_connector_requirements: tuple[
            ConnectorRequirementContract | Mapping[str, Any], ...
        ] = (),
        automation_id: str,
        generation: int,
    ) -> ServiceProviderReplacement:
        """Legacy atomically replace one unshared package Provider/reference.

        New Service v2 upgrades should retain the old package until its leases
        drain, register the new package separately, and use
        ``require_operation_for_reference`` for deterministic routing.
        """

        replaced_provider_automation_id = _validate_automation_id(
            replaced_provider_automation_id
        )
        replacement_provider_automation_id = _validate_automation_id(
            replacement_provider_automation_id
        )
        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        replacement = self._registration_from_contract(
            automation_id=replacement_provider_automation_id,
            generation=_validate_generation(replacement_provider_generation),
            plugin_id=replacement_plugin_id,
            plugin_version=replacement_plugin_version,
            package_sha256=replacement_package_sha256,
            manifest_sha256=replacement_manifest_sha256,
            runtime_mode=replacement_runtime_mode,
            provides=replacement_provides,
            requires=replacement_requires,
            connector_requirements=replacement_connector_requirements,
        )
        if replaced_provider_automation_id == replacement_provider_automation_id:
            raise ServiceProviderConflict(
                "package Provider replacement must use a byte-different package"
            )
        if replacement_provider_automation_id != package_provider_registration_id(
            replacement.package_sha256
        ):
            raise ServiceProviderConflict(
                "replacement Provider identity does not match its package digest"
            )
        replacement_reference = ServiceProviderReference(
            provider_automation_id=replacement_provider_automation_id,
            automation_id=automation_id,
            generation=generation,
            package_sha256=replacement.package_sha256,
            manifest_sha256=replacement.manifest_sha256,
        )
        with self._lock:
            replaced = self._registrations.get(replaced_provider_automation_id)
            if replaced is None:
                raise ServiceUnavailable(
                    "package Provider is not registered",
                    code="SERVICE_PROVIDER_MISSING",
                )
            if replaced_provider_automation_id != package_provider_registration_id(
                replaced.package_sha256
            ):
                raise ServiceProviderConflict(
                    "replaced Provider identity does not match its package digest"
                )
            if replacement_provider_automation_id in self._registrations:
                raise ServiceProviderConflict(
                    "replacement package Provider is already registered"
                )
            replaced_references = tuple(
                self._provider_references.get(replaced_provider_automation_id, {})[key]
                for key in sorted(
                    self._provider_references.get(replaced_provider_automation_id, {})
                )
            )
            if not replaced_references:
                raise ServiceProviderConflict(
                    "package Provider has no project reference to replace"
                )
            if any(
                reference.automation_id != automation_id
                or reference.generation >= generation
                or reference.provider_automation_id != replaced_provider_automation_id
                or reference.package_sha256 != replaced.package_sha256
                or reference.manifest_sha256 != replaced.manifest_sha256
                for reference in replaced_references
            ):
                raise ServiceProviderConflict(
                    "package Provider remains referenced outside the upgrading project"
                )
            if not self._same_upgrade_contract(replaced, replacement):
                raise ServiceProviderConflict(
                    "package Provider upgrade changed its declared service contract"
                )

            candidate_registrations = dict(self._registrations)
            del candidate_registrations[replaced_provider_automation_id]
            candidate_registrations[replacement_provider_automation_id] = replacement
            candidate_owners = self._build_owners(candidate_registrations)
            candidate_references = {
                provider_id: dict(references)
                for provider_id, references in self._provider_references.items()
                if provider_id != replaced_provider_automation_id
            }
            candidate_references[replacement_provider_automation_id] = {
                (automation_id, generation): replacement_reference,
            }
            reconciled = self._reconcile(candidate_registrations, candidate_owners)

            self._registrations = reconciled
            self._service_owners = candidate_owners
            self._provider_references = candidate_references
            return ServiceProviderReplacement(
                replaced_provider=replaced,
                replaced_references=replaced_references,
                replacement_provider=self._registrations[
                    replacement_provider_automation_id
                ],
                replacement_reference=replacement_reference,
            )

    def rollback_package_provider_replacement(
        self,
        replacement: ServiceProviderReplacement,
    ) -> None:
        """Restore an exact replacement token without exposing an intermediate state."""

        if not isinstance(replacement, ServiceProviderReplacement):
            raise TypeError("replacement must be a ServiceProviderReplacement")
        old = replacement.replaced_provider
        new = replacement.replacement_provider
        with self._lock:
            current = self._registrations.get(new.automation_id)
            current_references = tuple(
                self._provider_references.get(new.automation_id, {})[key]
                for key in sorted(self._provider_references.get(new.automation_id, {}))
            )
            if (
                current is None
                or not self._same_contract(current, new)
                or current_references != (replacement.replacement_reference,)
                or old.automation_id in self._registrations
            ):
                raise ServiceProviderConflict(
                    "package Provider replacement can no longer be rolled back exactly"
                )
            candidate_registrations = dict(self._registrations)
            del candidate_registrations[new.automation_id]
            candidate_registrations[old.automation_id] = old
            candidate_owners = self._build_owners(candidate_registrations)
            candidate_references = {
                provider_id: dict(references)
                for provider_id, references in self._provider_references.items()
                if provider_id != new.automation_id
            }
            candidate_references[old.automation_id] = {
                (reference.automation_id, reference.generation): reference
                for reference in replacement.replaced_references
            }
            reconciled = self._reconcile(candidate_registrations, candidate_owners)
            self._registrations = reconciled
            self._service_owners = candidate_owners
            self._provider_references = candidate_references

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
    def _registration_from_contract(
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
        connector_requirements: tuple[
            ConnectorRequirementContract | Mapping[str, Any], ...
        ],
    ) -> ServiceRegistration:
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
        if not isinstance(connector_requirements, (list, tuple)):
            raise ServiceRegistryError("persisted Connector requirements are invalid")
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
            normalized_operations = _normalize_operation_effects(operations)
            seen.add(service)
            providers.append(
                ServiceProvider(
                    service=service,
                    operation_effects=normalized_operations,
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
        try:
            normalized_connector_requirements = tuple(
                item
                if isinstance(item, ConnectorRequirementContract)
                else connector_requirement_from_mapping(item)
                for item in connector_requirements
            )
        except (TypeError, ValueError, ConnectorRegistryError) as exc:
            raise ServiceRegistryError(
                "persisted Connector requirements are invalid"
            ) from exc
        connector_services = tuple(
            item.service for item in normalized_connector_requirements
        )
        if (
            len(connector_services) != len(set(connector_services))
            or any(service not in normalized_requires for service in connector_services)
        ):
            raise ServiceRegistryError("persisted Connector requirements are invalid")
        return ServiceRegistration(
            automation_id=automation_id,
            generation=generation,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            package_sha256=package_sha256,
            manifest_sha256=manifest_sha256,
            provided_services=tuple(providers),
            required_services=normalized_requires,
            connector_requirements=normalized_connector_requirements,
            state=ServiceRegistrationState.BLOCKED_DEPENDENCY,
        )

    @staticmethod
    def _same_contract(
        current: ServiceRegistration,
        incoming: ServiceRegistration,
    ) -> bool:
        def providers(
            registration: ServiceRegistration,
        ) -> tuple[tuple[str, tuple[tuple[str, CapabilityEffect], ...]], ...]:
            return tuple(
                (provider.service, provider.operation_effects)
                for provider in registration.provided_services
            )

        return (
            current.plugin_id == incoming.plugin_id
            and current.plugin_version == incoming.plugin_version
            and current.package_sha256 == incoming.package_sha256
            and current.manifest_sha256 == incoming.manifest_sha256
            and current.required_services == incoming.required_services
            and current.connector_requirements == incoming.connector_requirements
            and providers(current) == providers(incoming)
            and tuple(
                provider.runtime_mode for provider in current.provided_services
            )
            == tuple(
                provider.runtime_mode for provider in incoming.provided_services
            )
        )

    @staticmethod
    def _same_upgrade_contract(
        current: ServiceRegistration,
        incoming: ServiceRegistration,
    ) -> bool:
        """Accept package/version rotation only when the service API is exact."""

        return (
            current.plugin_id == incoming.plugin_id
            and current.required_services == incoming.required_services
            and current.connector_requirements == incoming.connector_requirements
            and tuple(
                (
                    provider.service,
                    provider.operation_effects,
                    provider.runtime_mode,
                )
                for provider in current.provided_services
            )
            == tuple(
                (
                    provider.service,
                    provider.operation_effects,
                    provider.runtime_mode,
                )
                for provider in incoming.provided_services
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
        providers = self.providers_for(service)
        return providers[0] if len(providers) == 1 else None

    def claimed_provider_for(self, service: str) -> ServiceProvider | None:
        """Return the owner even when dependencies prevent invocation."""

        providers = self.claimed_providers_for(service)
        return providers[0] if len(providers) == 1 else None

    def providers_for(self, service: str) -> tuple[ServiceProvider, ...]:
        """Return ready routes that currently accept new calls.

        A ready package alone is not routable.  Its exact project generation
        must be explicitly activated first; multiple active routes remain
        ambiguous to bare-name callers.
        """

        with self._lock:
            return tuple(
                provider
                for provider, _reference in self._ready_service_routes_locked(service)
            )

    def claimed_providers_for(self, service: str) -> tuple[ServiceProvider, ...]:
        """Return every package claim, including dependency-blocked Providers."""

        with self._lock:
            providers: list[ServiceProvider] = []
            for owner in self._service_owners.get(service, ()):
                registration = self._registrations.get(owner[0])
                if registration is None:
                    continue
                provider = self._provider_from_registration(registration, service)
                if provider is not None:
                    providers.append(provider)
            return tuple(providers)

    def require_provider(self, service: str) -> ServiceProvider:
        providers = self.providers_for(service)
        if len(providers) == 1:
            return providers[0]
        if len(providers) > 1:
            raise ServiceProviderAmbiguous(
                f"service has multiple ready package Providers: {service}"
            )
        claimed = self.claimed_providers_for(service)
        if not claimed:
            raise ServiceUnavailable(
                f"service has no registered provider: {service}",
                code="SERVICE_PROVIDER_MISSING",
            )
        reason_codes = ",".join(
            sorted(
                {
                    reason.code
                    for provider in claimed
                    for reason in (
                        self.registration(provider.automation_id).blocking_reasons
                        if self.registration(provider.automation_id) is not None
                        else ()
                    )
                }
            )
        ) or "UNKNOWN"
        raise ServiceUnavailable(
            f"service provider is blocked: {service} ({reason_codes})",
            code="SERVICE_PROVIDER_BLOCKED",
        )

    def require_operation(self, service: str, operation: str) -> ResolvedServiceOperation:
        """Resolve one newly-routable service operation without ambiguity."""

        normalized_operation = _validate_operation_name(operation)
        with self._lock:
            routes = self._ready_service_routes_locked(service)
            if len(routes) == 1:
                provider, reference = routes[0]
                return self._resolved_operation(
                    provider=provider,
                    reference=reference,
                    operation=normalized_operation,
                )
        # Retain the established missing/blocked/ambiguous error taxonomy.
        self.require_provider(service)
        raise AssertionError("unique ready Provider route was lost")

    def require_operation_for_reference(
        self,
        *,
        service: str,
        operation: str,
        automation_id: str,
        generation: int,
        provider_generation: int,
        package_sha256: str,
        manifest_sha256: str,
    ) -> ResolvedServiceOperation:
        """Resolve one operation through an exact project/package binding.

        There is intentionally no global service-name fallback: a stale lease
        or package identity drift cannot run a different package.
        """

        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        provider_generation = _validate_generation(provider_generation)
        package_sha256 = _validate_sha256(package_sha256, "package_sha256")
        manifest_sha256 = _validate_sha256(manifest_sha256, "manifest_sha256")
        normalized_operation = _validate_operation_name(operation)
        provider_automation_id = package_provider_registration_id(package_sha256)
        with self._lock:
            reference = self._provider_references.get(provider_automation_id, {}).get(
                (automation_id, generation)
            )
            if reference is None:
                raise ServiceUnavailable(
                    "project generation has no matching package Provider reference",
                    code="SERVICE_PROVIDER_REFERENCE_MISSING",
                )
            if (
                reference.provider_automation_id != provider_automation_id
                or reference.package_sha256 != package_sha256
                or reference.manifest_sha256 != manifest_sha256
            ):
                raise ServiceProviderConflict(
                    "project generation Provider reference changed package identity"
                )
            registration = self._registrations.get(provider_automation_id)
            if registration is None:
                raise ServiceUnavailable(
                    "referenced package Provider is not registered",
                    code="SERVICE_PROVIDER_MISSING",
                )
            if (
                registration.generation != provider_generation
                or registration.package_sha256 != package_sha256
                or registration.manifest_sha256 != manifest_sha256
            ):
                raise ServiceProviderConflict(
                    "referenced package Provider registration changed identity"
                )
            if not registration.active:
                raise ServiceUnavailable(
                    "referenced package Provider is dependency-blocked",
                    code="SERVICE_PROVIDER_BLOCKED",
                )
            provider = self._provider_from_registration(registration, service)
            if provider is None:
                raise ServiceUnavailable(
                    f"referenced package Provider does not provide service: {service}",
                    code="SERVICE_PROVIDER_MISSING",
                )
            return self._resolved_operation(
                provider=provider,
                reference=reference,
                operation=normalized_operation,
            )

    def _ready_service_routes_locked(
        self,
        service: str,
    ) -> tuple[tuple[ServiceProvider, ServiceProviderReference], ...]:
        routes: list[tuple[ServiceProvider, ServiceProviderReference]] = []
        for owner in self._service_owners.get(service, ()):
            registration = self._registrations.get(owner[0])
            if registration is None or not registration.active:
                continue
            provider = self._provider_from_registration(registration, service)
            if provider is None:
                continue
            active_references = tuple(
                reference
                for _key, reference in sorted(
                    self._provider_references.get(registration.automation_id, {}).items()
                )
                if reference.accepts_new_calls
            )
            if active_references:
                routes.extend((provider, reference) for reference in active_references)
                continue
            # ``register`` predates package Provider identities. Keep that
            # narrow in-memory API compatible while production package owners
            # always require an explicitly activated reference.
            if registration.automation_id != package_provider_registration_id(
                registration.package_sha256
            ):
                routes.append(
                    (
                        provider,
                        ServiceProviderReference(
                            provider_automation_id=registration.automation_id,
                            automation_id=registration.automation_id,
                            generation=registration.generation,
                            package_sha256=registration.package_sha256,
                            manifest_sha256=registration.manifest_sha256,
                            accepts_new_calls=True,
                        ),
                    )
                )
        return tuple(routes)

    @staticmethod
    def _resolved_operation(
        *,
        provider: ServiceProvider,
        reference: ServiceProviderReference,
        operation: str,
    ) -> ResolvedServiceOperation:
        effect = provider.effect_for(operation)
        return ResolvedServiceOperation(
            provider_registration_id=provider.automation_id,
            provider_contract_generation=provider.generation,
            project_automation_id=reference.automation_id,
            project_generation=reference.generation,
            plugin_id=provider.plugin_id,
            plugin_version=provider.plugin_version,
            package_sha256=provider.package_sha256,
            manifest_sha256=provider.manifest_sha256,
            runtime_mode=provider.runtime_mode,
            service=provider.service,
            operation=operation,
            effect=effect,
        )

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
            existing_reference = self._project_reference_locked(
                automation_id=automation_id,
                generation=generation,
            )
            if (
                existing_reference is not None
                and existing_reference.provider_automation_id != provider_automation_id
            ):
                raise ServiceProviderConflict(
                    "project generation reference changed its Provider identity"
                )
            references = self._provider_references.setdefault(
                provider_automation_id,
                {},
            )
            current = references.get(key)
            if current is not None:
                if replace(current, accepts_new_calls=False) != reference:
                    raise ServiceProviderConflict(
                        "project generation reference changed its Provider identity"
                    )
                return current
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

    def project_reference(
        self,
        *,
        automation_id: str,
        generation: int,
    ) -> ServiceProviderReference | None:
        """Return the sole Provider binding for one project generation."""

        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        with self._lock:
            return self._project_reference_locked(
                automation_id=automation_id,
                generation=generation,
            )

    def activate_project_reference(
        self,
        *,
        automation_id: str,
        generation: int,
    ) -> ServiceProjectRouteTransition:
        """Route new calls to one project generation and drain its old routes."""

        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        with self._lock:
            target = self._project_reference_locked(
                automation_id=automation_id,
                generation=generation,
            )
            if target is None:
                raise ServiceUnavailable(
                    "project generation has no package Provider reference",
                    code="SERVICE_PROVIDER_REFERENCE_MISSING",
                )
            registration = self._registrations.get(target.provider_automation_id)
            if registration is None or not registration.active:
                raise ServiceUnavailable(
                    "project generation Provider is not ready",
                    code="SERVICE_PROVIDER_BLOCKED",
                )
            before = self._project_references_for_automation_locked(automation_id)
            after = tuple(
                replace(
                    reference,
                    accepts_new_calls=(
                        reference.provider_automation_id
                        == target.provider_automation_id
                        and reference.generation == target.generation
                    ),
                )
                for reference in before
            )
            self._apply_reference_states_locked(after)
            return ServiceProjectRouteTransition(before=before, after=after)

    def deactivate_project_reference(
        self,
        *,
        automation_id: str,
        generation: int,
    ) -> ServiceProjectRouteTransition:
        """Stop assigning new calls to one project generation without removing it."""

        automation_id = _validate_automation_id(automation_id)
        generation = _validate_generation(generation)
        with self._lock:
            target = self._project_reference_locked(
                automation_id=automation_id,
                generation=generation,
            )
            if target is None:
                raise ServiceUnavailable(
                    "project generation has no package Provider reference",
                    code="SERVICE_PROVIDER_REFERENCE_MISSING",
                )
            before = (target,)
            after = (replace(target, accepts_new_calls=False),)
            self._apply_reference_states_locked(after)
            return ServiceProjectRouteTransition(before=before, after=after)

    def block_project_references(
        self,
        automation_id: str,
    ) -> ServiceProjectRouteTransition:
        """Fail closed every Provider route owned by one project.

        This is the emergency boundary used when a durable activation cannot
        be acknowledged or reversed.  Package registrations and immutable
        references remain available for diagnosis, but none can receive a new
        call until a controlled recovery rebuilds the process projection.
        """

        automation_id = _validate_automation_id(automation_id)
        with self._lock:
            before = self._project_references_for_automation_locked(automation_id)
            after = tuple(
                replace(reference, accepts_new_calls=False)
                for reference in before
            )
            self._apply_reference_states_locked(after)
            return ServiceProjectRouteTransition(before=before, after=after)

    def rollback_project_reference_transition(
        self,
        transition: ServiceProjectRouteTransition,
    ) -> None:
        """Restore a route transition only if every post-state still matches."""

        if not isinstance(transition, ServiceProjectRouteTransition):
            raise TypeError("transition must be a ServiceProjectRouteTransition")
        if len(transition.before) != len(transition.after):
            raise ServiceProviderConflict("project route transition is invalid")
        with self._lock:
            for reference in transition.after:
                current = self._provider_references.get(
                    reference.provider_automation_id,
                    {},
                ).get((reference.automation_id, reference.generation))
                if current != reference:
                    raise ServiceProviderConflict(
                        "project route transition can no longer be rolled back exactly"
                    )
            self._apply_reference_states_locked(transition.before)

    def _project_reference_locked(
        self,
        *,
        automation_id: str,
        generation: int,
    ) -> ServiceProviderReference | None:
        matches = tuple(
            references[(automation_id, generation)]
            for references in self._provider_references.values()
            if (automation_id, generation) in references
        )
        if len(matches) > 1:
            raise ServiceProviderConflict(
                "project generation has multiple Provider references"
            )
        return matches[0] if matches else None

    def _project_references_for_automation_locked(
        self,
        automation_id: str,
    ) -> tuple[ServiceProviderReference, ...]:
        return tuple(
            reference
            for provider_automation_id in sorted(self._provider_references)
            for _key, reference in sorted(
                self._provider_references[provider_automation_id].items()
            )
            if reference.automation_id == automation_id
        )

    def _apply_reference_states_locked(
        self,
        references: tuple[ServiceProviderReference, ...],
    ) -> None:
        for reference in references:
            provider_references = self._provider_references.get(
                reference.provider_automation_id
            )
            if provider_references is None:
                raise ServiceProviderConflict("project Provider reference disappeared")
            key = (reference.automation_id, reference.generation)
            if key not in provider_references:
                raise ServiceProviderConflict("project Provider reference disappeared")
        for reference in references:
            self._provider_references[reference.provider_automation_id][
                (reference.automation_id, reference.generation)
            ] = reference

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
                operation_effects=_normalize_operation_effects(item["operations"]),
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
            connector_requirements=connector_requirements_from_manifest(manifest),
            state=ServiceRegistrationState.BLOCKED_DEPENDENCY,
            blocking_reasons=(),
        )

    @staticmethod
    def _build_owners(
        registrations: Mapping[str, ServiceRegistration],
    ) -> dict[str, tuple[tuple[str, int], ...]]:
        owners: dict[str, list[tuple[str, int]]] = {}
        for automation_id in sorted(registrations):
            registration = registrations[automation_id]
            for provider in registration.provided_services:
                owners.setdefault(provider.service, []).append(
                    (automation_id, registration.generation)
                )
        return {
            service: tuple(sorted(claims))
            for service, claims in owners.items()
        }

    def _reconcile(
        self,
        registrations: Mapping[str, ServiceRegistration],
        owners: Mapping[str, tuple[tuple[str, int], ...]],
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
                    (
                        evaluate_connector_requirement(
                            self._connectors,
                            connector_requirement_for_service(
                                registration.connector_requirements,
                                service,
                            ),
                            service=service,
                        ).ready
                        if service.startswith("connector.")
                        else False
                    )
                    or (
                        not service.startswith("connector.")
                        and (claims := owners.get(service)) is not None
                        and any(owner[0] in active for owner in claims)
                    )
                    for service in registration.required_services
                ):
                    active.add(automation_id)
                    changed = True

        result: dict[str, ServiceRegistration] = {}
        for automation_id, registration in registrations.items():
            is_active = automation_id in active
            reasons = () if is_active else self._blocking_reasons_for(
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

    def _blocking_reasons_for(
        self,
        registration: ServiceRegistration,
        *,
        registrations: Mapping[str, ServiceRegistration],
        owners: Mapping[str, tuple[tuple[str, int], ...]],
        active: set[str],
    ) -> tuple[DependencyBlockReason, ...]:
        reasons: list[DependencyBlockReason] = []
        for service in registration.required_services:
            if service.startswith("connector."):
                compatibility = evaluate_connector_requirement(
                    self._connectors,
                    connector_requirement_for_service(
                        registration.connector_requirements,
                        service,
                    ),
                    service=service,
                )
                if not compatibility.ready:
                    reasons.append(
                        DependencyBlockReason(
                            code=compatibility.reason_code
                            or "CONNECTOR_REQUIREMENT_INCOMPATIBLE",
                            service=service,
                            message=compatibility.reason
                            or "Connector requirement is incompatible",
                        )
                    )
                continue
            if self._connectors.is_available(service):
                continue
            claims = owners.get(service)
            if claims is None:
                reasons.append(
                    DependencyBlockReason(
                        code=(
                            "MISSING_CONNECTOR"
                            if service.startswith("connector.")
                            else "MISSING_PROVIDER"
                        ),
                        service=service,
                        message=f"required service is unavailable: {service}",
                    )
                )
                continue
            if not any(provider_automation_id in active for provider_automation_id, _generation in claims):
                provider_automation_id, _provider_generation = claims[0]
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
    "ServiceProviderAmbiguous",
    "ServiceProviderReference",
    "ServiceProviderReplacement",
    "ServiceProjectRouteTransition",
    "ResolvedServiceOperation",
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
