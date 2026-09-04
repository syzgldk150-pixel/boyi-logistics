"""Production composition helpers for signed automation-plugin runtimes.

This module deliberately keeps package verification, desired project state,
and committed execution generations separate.  A desired configuration is
never executable until :class:`AutomationRuntimeReconciler` commits the exact
immutable snapshot built here.
"""

from __future__ import annotations

import copy
import hashlib
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Callable, Mapping, Sequence

from agent.automation_plugins.broker import (
    LocalBrokerCapabilityIssuer,
    LocalCoreAutomationBroker,
)
from agent.automation_plugins.binding_resolver import (
    ProductionProjectBindingResolver,
)
from agent.automation_plugins.capability_proxy_v2 import (
    SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY,
    UNAVAILABLE_SERVICE_V2_HANDLER_KEYS,
    build_service_v2_capability_handler_map,
)
from agent.automation_plugins.catalog import (
    CompositeToolRegistry,
    PluginCatalog,
    PluginCatalogEntry,
    project_capability_from_snapshot,
)
from agent.automation_plugins.core_adapter import (
    AccountManagerSessionResolver,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.errors import (
    AutomationPluginError,
    PluginConflictError,
    PluginExecutionError,
    PluginPackageError,
)
from agent.automation_plugins.configuration import (
    AutomationProjectConfigurationService,
)
from agent.automation_plugins.connector_dependency_projection import (
    project_service_dependencies,
)
from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.execution import (
    FilesystemPluginIntegrityVerifier,
    PluginExecutionRouter,
)
from agent.automation_plugins.first_party import (
    SignedFirstPartyPackageProvider,
    bootstrap_first_party_plugins,
    deferred_first_party_automation_plugins,
    deferred_first_party_plugin_ids,
    release_first_party_automation_ids,
    release_first_party_broker_action_keys,
    resolve_release_first_party_manifests,
)
from agent.automation_plugins.generation import (
    AutomationRuntimeReconciler,
    RuntimeGenerationHealth,
    RuntimeReconcileResult,
    runtime_generation_health,
)
from agent.automation_plugins.manifest import (
    canonical_json_bytes,
    runtime_descriptor_matches_signed_installation,
)
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.management_repository import (
    MySQLAutomationPluginManagementRepository,
)
from agent.automation_plugins.migration import PluginMigrationRuntimeCoordinator
from agent.automation_plugins.migration_entrypoint_ownership import (
    MigrationEntrypointOwnershipResolver,
)
from agent.automation_plugins.models import (
    BootstrapResult,
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeActivationPhase,
    RuntimeCoeffectKind,
    RuntimeCoeffectSnapshot,
    RuntimeEffectKind,
    RuntimeEffectRecord,
    RuntimeEffectState,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
    RuntimeReconcileState,
)
from agent.automation_plugins.mysql_repository import (
    MySQLAutomationPluginRepositoryAdapter,
)
from agent.automation_plugins.package import load_ed25519_trust_store
from agent.automation_plugins.ports import RuntimeEffectPlan
from agent.automation_plugins.production_snapshot import (
    build_runtime_generation_snapshot,
    _required_policy_generation,
)
from agent.automation_plugins.production_projection_identity import ProjectionIdentityJournal, project_projection_identity
from agent.automation_plugins.release_config import (
    ProductionPluginReleaseConfig,
    load_production_plugin_release_config,
)
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter,
    MySQLAutomationPluginRuntimeAdapter,
    MySQLAutomationProjectConfigurationReadAdapter,
)
from agent.orchestration.scan_preview_binding import (
    SCAN_PREVIEW_CONTEXT_KEY,
    normalize_preview_run_id,
    scan_preview_recovery_projection,
)
from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.automation_plugins.service_registry import (
    ResolvedServiceOperation,
    ServiceRegistry,
    ServiceProjectRouteTransition,
    package_provider_registration_id,
)
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from shared.orchestration_repository_support import ConcurrentUpdateError
from agent.automation_plugins.service_v2_projection import (
    _CONTRIBUTION_EFFECT_CONTRACT_VERSION,
    _MANAGED_CONTRIBUTION_KINDS,
    _SERVICE_PROVIDER_GENERATION,
    ManagedContributionRegistration,
    ManagedContributionRegistry,
    _closed_service_v2_contributions,
    _contribution_backend,
    _service_registration_material,
    _service_v2_contribution_effect_plans,
    _validated_managed_contribution_effect_payload,
    _validated_service_registration_payload,
)
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox, SandboxCanaryResult
from agent.automation_plugins.storage import (
    FilesystemPluginStorage,
    LockedVirtualEnvironmentBuilder,
)
from shared.finance.sources import enabled_finance_account_ids
from shared.redaction import redact_text


CURSOR_SECRET_ENV = "BOYI_AUTOMATION_PLUGIN_CURSOR_SECRET"


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _required_sha(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PluginConflictError(f"persisted {field} is not a SHA-256 digest")
    return text


class ProductionRuntimeCoeffectProvider:
    """Observe structural account, resource and closed-adapter revisions.

    Authenticated sessions are deliberately not generation coeffects.  They
    are transient execution dependencies and the core Broker revalidates the
    exact bound account immediately before every account-backed invocation.
    """

    def __init__(
        self,
        *,
        core_catalog: Any,
        broker_handler_keys: Sequence[tuple[str, str]],
        account_manager: Any,
        binding_resolver: ProductionProjectBindingResolver | None = None,
        service_registry: ServiceRegistry | None = None,
        connector_registry: ConnectorRegistry | None = None,
    ) -> None:
        self._core_catalog = core_catalog
        self._handler_keys = frozenset((str(a), str(b)) for a, b in broker_handler_keys)
        self._account_manager = account_manager
        self._bindings = binding_resolver
        self._connectors = connector_registry or ConnectorRegistry()
        self._services = service_registry or ServiceRegistry(
            connector_registry=self._connectors
        )

    @staticmethod
    def _record(
        kind: RuntimeCoeffectKind,
        key: str,
        value: Any,
        *,
        ready: bool,
        reason_code: str | None = None,
    ) -> RuntimeCoeffectSnapshot:
        return RuntimeCoeffectSnapshot(
            kind=kind,
            key=key,
            revision=_digest(value),
            ready=ready,
            reason_code=reason_code,
        )

    def _service_coeffects(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> tuple[RuntimeCoeffectSnapshot, ...]:
        material = _service_registration_material(snapshot)
        dependencies = project_service_dependencies(
            material["requires"],
            connector_requirements=material.get("connector_requirements", ()),
            connector_registry=self._connectors,
            service_registry=self._services,
        )
        return tuple(
            self._record(
                RuntimeCoeffectKind.SERVICE,
                service,
                revision,
                ready=ready,
                reason_code=None if ready else "BLOCKED_DEPENDENCY",
            )
            for service, ready, revision in dependencies
        )

    def _contribution_coeffects(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> tuple[RuntimeCoeffectSnapshot, ...]:
        """Observe the host backend for every enabled managed contribution."""

        contributions = _closed_service_v2_contributions(snapshot)
        enabled = set(snapshot.enabled_entrypoints)
        project_schedule = snapshot.execution_metadata.get("schedule")
        if not isinstance(project_schedule, Mapping):
            raise PluginConflictError("generation project schedule is invalid")
        results: list[RuntimeCoeffectSnapshot] = []
        for kind in _MANAGED_CONTRIBUTION_KINDS:
            for declaration in contributions[kind]:
                contribution_id = str(declaration.get("id") or "")
                if contribution_id not in enabled:
                    continue
                backend, status, reason_code, reason_detail = _contribution_backend(
                    contribution_kind=kind,
                    declaration=declaration,
                    project_schedule=project_schedule,
                )
                # DISABLED is emitted only for an intentionally closed project
                # schedule.  It is audited but is not a missing host capability.
                ready = status in {"READY", "DISABLED"}
                results.append(
                    self._record(
                        RuntimeCoeffectKind.CORE_ADAPTER,
                        f"contribution:{kind}:{contribution_id}",
                        {
                            "backend": backend,
                            "backend_status": status,
                            "reason_code": reason_code,
                            "reason_detail": reason_detail,
                        },
                        ready=ready,
                        reason_code=None if ready else "CAPABILITY_UNAVAILABLE",
                    )
                )
        return tuple(results)

    def observe(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> Sequence[RuntimeCoeffectSnapshot]:
        metadata = snapshot.execution_metadata
        descriptor = metadata.get("runtime_descriptor")
        if not isinstance(descriptor, Mapping):
            raise PluginConflictError("runtime descriptor is absent from generation")
        permissions = descriptor.get("runtime_permissions")
        account_roles = descriptor.get("account_roles")
        bindings = metadata.get("account_bindings")
        anchor = metadata.get("governance_anchor")
        if (
            not isinstance(permissions, Mapping)
            or not isinstance(account_roles, list)
            or not isinstance(bindings, Mapping)
            or not isinstance(anchor, Mapping)
        ):
            raise PluginConflictError("runtime coeffect material is invalid")
        operations = permissions.get("broker_operations")
        if not isinstance(operations, list):
            raise PluginConflictError("signed broker operation contract is invalid")
        required_pairs: set[tuple[str, str]] = set()
        for operation in operations:
            if not isinstance(operation, Mapping):
                raise PluginConflictError("signed broker operation contract is invalid")
            required_pairs.add((str(operation.get("operation") or ""), str(operation.get("action") or "")))
        is_v2 = snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2
        runtime_contract = descriptor.get("runtime")
        service_runtime_ready = (
            not is_v2 or isinstance(runtime_contract, Mapping) and runtime_contract.get("mode") == "on_demand"
        )
        core_capability = None if is_v2 else self._core_catalog.get_capability(str(anchor.get("name") or ""))
        core_ready = (is_v2 and service_runtime_ready) or (
            not is_v2
            and isinstance(core_capability, Mapping)
            and all(
                key in core_capability and canonical_json_bytes(core_capability[key]) == canonical_json_bytes(value)
                for key, value in anchor.items()
            )
        )
        adapters_ready = (
            all(pair in self._handler_keys or (pair[0], "*") in self._handler_keys for pair in required_pairs)
            if is_v2
            else bool(required_pairs) and required_pairs <= self._handler_keys
        )
        results: list[RuntimeCoeffectSnapshot] = [
            self._record(
                RuntimeCoeffectKind.CORE_ADAPTER,
                "governance-and-broker",
                {
                    "governance_anchor_sha256": snapshot.governance_anchor_sha256,
                    "required_broker_operations": sorted(required_pairs),
                    "registered_broker_operations": sorted(
                        pair
                        for pair in required_pairs
                        if pair in self._handler_keys or (pair[0], "*") in self._handler_keys
                    ),
                },
                ready=core_ready and adapters_ready,
                reason_code=(
                    None
                    if core_ready and adapters_ready
                    else (
                        "CORE_REGISTRY_MISMATCH"
                        if not core_ready and service_runtime_ready
                        else "RESIDENT_RUNTIME_UNAVAILABLE"
                        if not service_runtime_ready
                        else "CORE_ADAPTER_ACTION_UNAVAILABLE"
                    )
                ),
            )
        ]
        if is_v2:
            results.extend(self._service_coeffects(snapshot))
            results.extend(self._contribution_coeffects(snapshot))
        public_accounts = {
            str(item.get("account_id") or ""): item
            for item in self._account_manager.list_accounts(
                include_status=False,
                validate=False,
            )
            if isinstance(item, Mapping) and str(item.get("account_id") or "")
        }
        declared = {str(item.get("role") or ""): item for item in account_roles}
        if "" in declared or set(bindings) != {
            role for role, value in declared.items() if value.get("required") is True
        } | (set(bindings) - {""}):
            # The exact set is validated below; this guard primarily rejects
            # malformed duplicate/empty declarations before any session read.
            if "" in declared or not set(bindings) <= set(declared):
                raise PluginConflictError("generation account role contract is invalid")
        for role_name, declaration in declared.items():
            raw_binding = bindings.get(role_name)
            if raw_binding is None and declaration.get("required") is not True:
                results.append(
                    self._record(
                        RuntimeCoeffectKind.ACCOUNT,
                        role_name,
                        None,
                        ready=True,
                    )
                )
                continue
            values = raw_binding if isinstance(raw_binding, (list, tuple)) else (raw_binding,)
            account_ids = tuple(str(value or "").strip() for value in values)
            allowed_systems = declaration.get("allowed_systems")
            if (
                not account_ids
                or any(not account_id for account_id in account_ids)
                or not isinstance(allowed_systems, list)
            ):
                raise PluginConflictError("generation account binding is invalid")
            descriptors = [public_accounts.get(account_id) for account_id in account_ids]
            accounts_ready = all(
                descriptor is not None
                and descriptor.get("is_active") is True
                and str(descriptor.get("system") or "") in set(allowed_systems)
                for descriptor in descriptors
            )
            account_revision = [
                {
                    "binding_sha256": _digest(account_id),
                    "system": str((descriptor or {}).get("system") or ""),
                    "active": bool((descriptor or {}).get("is_active") is True),
                }
                for account_id, descriptor in zip(account_ids, descriptors, strict=True)
            ]
            results.append(
                self._record(
                    RuntimeCoeffectKind.ACCOUNT,
                    role_name,
                    account_revision,
                    ready=accounts_ready,
                    reason_code=None if accounts_ready else "BLOCKED_CONFIG",
                )
            )
        resource_roles = descriptor.get("resource_roles")
        resource_bindings = metadata.get("resource_bindings")
        if not isinstance(resource_roles, list) or not isinstance(
            resource_bindings,
            Mapping,
        ):
            raise PluginConflictError("generation resource role contract is invalid")
        declared_resource_roles = {
            str(item.get("role") or ""): item for item in resource_roles if isinstance(item, Mapping)
        }
        if (
            len(declared_resource_roles) != len(resource_roles)
            or "" in declared_resource_roles
            or not set(resource_bindings) <= set(declared_resource_roles)
        ):
            raise PluginConflictError("generation resource role contract is invalid")
        for raw_role in resource_roles:
            if not isinstance(raw_role, Mapping) or not str(raw_role.get("role") or ""):
                raise PluginConflictError("generation resource role contract is invalid")
            role_name = str(raw_role["role"])
            resource_id = resource_bindings.get(role_name)
            if resource_id is None:
                if raw_role.get("required") is True:
                    raise PluginConflictError("generation resource binding is missing")
                results.append(
                    self._record(
                        RuntimeCoeffectKind.RESOURCE,
                        role_name,
                        None,
                        ready=True,
                    )
                )
                continue
            descriptor_value: Mapping[str, Any] | None = None
            reason_code: str | None = None
            if self._bindings is None:
                reason_code = "RESOURCE_BINDING_UNAVAILABLE"
            else:
                try:
                    descriptor_value = self._bindings.describe_resource_binding(
                        automation_id=snapshot.automation_id,
                        role=raw_role,
                        resource_id=str(resource_id),
                    )
                except PluginConflictError as exc:
                    reason_code = exc.code
            results.append(
                self._record(
                    RuntimeCoeffectKind.RESOURCE,
                    role_name,
                    descriptor_value or {"binding_sha256": _digest(resource_id)},
                    ready=descriptor_value is not None,
                    reason_code=reason_code,
                )
            )
        if metadata.get("device_binding") is not None:
            results.append(
                self._record(
                    RuntimeCoeffectKind.DEVICE,
                    "named-worker",
                    {"binding": "unresolved"},
                    ready=False,
                    reason_code="WORKER_DEVICE_UNAVAILABLE",
                )
            )
        return tuple(results)


class ProductionRuntimeEffectPlanner:
    """Return deterministic reversible platform ownership for one generation."""

    def plan(self, snapshot: RuntimeGenerationSnapshot) -> Sequence[RuntimeEffectPlan]:
        metadata = snapshot.execution_metadata
        schedule = metadata.get("schedule")
        entrypoints = set(snapshot.enabled_entrypoints)

        base = f"{snapshot.automation_id}:{snapshot.generation}"
        plans = [
            RuntimeEffectPlan(
                RuntimeEffectKind.PACKAGE_REFERENCE,
                f"package:{snapshot.plugin_id}:{snapshot.plugin_version}",
                {
                    "package_sha256": snapshot.package_sha256,
                    "manifest_sha256": snapshot.manifest_sha256,
                },
            ),
            RuntimeEffectPlan(
                RuntimeEffectKind.VENV_REFERENCE,
                f"venv:{snapshot.plugin_id}:{snapshot.plugin_version}",
                {"runtime_descriptor_sha256": snapshot.runtime_descriptor_sha256},
            ),
            RuntimeEffectPlan(
                RuntimeEffectKind.INSTANCE_RUNTIME,
                f"instance:{base}",
                {"compiled_invocations_sha256": snapshot.compiled_invocations_sha256},
            ),
            RuntimeEffectPlan(
                RuntimeEffectKind.BROKER_SCOPE,
                f"broker:{base}",
                {"governance_anchor_sha256": snapshot.governance_anchor_sha256},
            ),
        ]
        if snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2:
            service_registration = _service_registration_material(snapshot)
            plans.append(
                RuntimeEffectPlan(
                    RuntimeEffectKind.SERVICE_REGISTRATION,
                    f"services:{base}",
                    service_registration,
                )
            )
            plans.extend(_service_v2_contribution_effect_plans(snapshot))
            return tuple(plans)
        if "scheduler" in entrypoints and isinstance(schedule, Mapping) and schedule.get("kind") != "none":
            plans.append(
                RuntimeEffectPlan(
                    RuntimeEffectKind.SCHEDULE_BINDING,
                    f"schedule:{base}",
                    {"schedule_sha256": snapshot.schedule_sha256},
                )
            )
        if "webhook" in entrypoints:
            plans.append(
                RuntimeEffectPlan(
                    RuntimeEffectKind.WEBHOOK_BINDING,
                    f"webhook:{base}",
                    {"manifest_sha256": snapshot.manifest_sha256},
                )
            )
        return tuple(plans)


class ProductionRuntimeEffectDriver:
    """Idempotently validate prepared ownership; route switching stays in CAS."""

    def __init__(
        self,
        *,
        broker_handler_keys: Sequence[tuple[str, str]],
        service_registry: ServiceRegistry | None = None,
        contribution_registry: ManagedContributionRegistry | None = None,
        projection_lock: Any | None = None,
    ) -> None:
        shared_projection_lock = projection_lock or RLock()
        self._integrity = FilesystemPluginIntegrityVerifier()
        self._handler_keys = frozenset((str(a), str(b)) for a, b in broker_handler_keys)
        self._services = service_registry or ServiceRegistry()
        self._contributions = contribution_registry or ManagedContributionRegistry()
        self._service_lock = shared_projection_lock
        self._service_references: dict[str, set[str]] = {}
        self._reference_packages: dict[str, str] = {}
        self._service_registry_restored = False
        self._projection_lock = shared_projection_lock
        self._scheduler_projection_refresher: (
            Callable[[], Mapping[str, Any]] | None
        ) = None
        self._scheduler_project_emergency_withdrawer: (
            Callable[[str], Mapping[str, Any]] | None
        ) = None
        self._emergency_blocked_projects: dict[str, int] = {}
        # Retry state is authoritative.  In particular an empty expected set
        # means "clear the active contribution generation", not "derive the
        # set from whatever process registrations happen to remain".
        self._pending_projection_transitions: dict[
            tuple[str, int], tuple[str, tuple[str, ...]]
        ] = {}
        self._projection_identities = ProjectionIdentityJournal()

    @property
    def service_registry(self) -> ServiceRegistry:
        return self._services

    @property
    def contribution_registry(self) -> ManagedContributionRegistry:
        return self._contributions

    def projection_signature(self) -> tuple[object, object, object]:
        """Return a stable process projection used for fixed-point retries."""

        with self._projection_lock:
            pending = tuple(
                sorted(
                    (
                        automation_id,
                        generation,
                        operation,
                    )
                    for (automation_id, generation), (operation, _) in (
                        self._pending_projection_transitions.items()
                    )
                )
            )
        return self._services.snapshot(), self._contributions.snapshot(), pending

    def _project_projection_identity(self, automation_id: str) -> str:
        return project_projection_identity(
            services=self._services, contributions=self._contributions, automation_id=automation_id
        )

    def bind_scheduler_projection_refresher(
        self,
        refresher: Callable[[], Mapping[str, Any]],
    ) -> None:
        """Bind the live Scheduler only after its process runtime exists."""

        if not callable(refresher):
            raise TypeError("scheduler projection refresher must be callable")
        with self._projection_lock:
            self._scheduler_projection_refresher = refresher

    def bind_scheduler_project_emergency_withdrawer(
        self,
        withdrawer: Callable[[str], Mapping[str, Any]],
    ) -> None:
        """Bind the DB-independent last-resort Scheduler withdrawal."""

        if not callable(withdrawer):
            raise TypeError("scheduler project emergency withdrawer must be callable")
        with self._projection_lock:
            self._scheduler_project_emergency_withdrawer = withdrawer
            for automation_id in sorted(self._emergency_blocked_projects):
                self._validated_emergency_scheduler_withdrawal(
                    automation_id,
                    withdrawer=withdrawer,
                )

    @staticmethod
    def _validate_emergency_scheduler_evidence(
        automation_id: str,
        evidence: object,
    ) -> Mapping[str, Any]:
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("automation_id") != automation_id
            or evidence.get("tombstoned") is not True
            or evidence.get("complete") is not True
            or not isinstance(evidence.get("removed_job_ids"), (list, tuple))
            or evidence.get("remaining_target_job_ids") not in ([], ())
            or evidence.get("remaining_malformed_marker_job_ids") not in ([], ())
        ):
            raise PluginConflictError(
                "emergency Scheduler withdrawal returned incomplete evidence",
                code="RUNTIME_PROJECTION_EMERGENCY_WITHDRAW_FAILED",
            )
        return dict(evidence)

    def _validated_emergency_scheduler_withdrawal(
        self,
        automation_id: str,
        *,
        withdrawer: Callable[[str], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        try:
            evidence = withdrawer(automation_id)
        except Exception as exc:
            raise PluginConflictError(
                "emergency Scheduler project withdrawal failed",
                code="RUNTIME_PROJECTION_EMERGENCY_WITHDRAW_FAILED",
            ) from exc
        return self._validate_emergency_scheduler_evidence(
            automation_id,
            evidence,
        )

    def _refresh_scheduler_projection(self) -> Mapping[str, Any] | None:
        refresher = self._scheduler_projection_refresher
        if refresher is None:
            # Standalone tests and non-Agent embeddings may not own a live
            # Scheduler. Agent startup binds a stopped Scheduler before durable
            # reconciliation, so production activation never takes this path.
            return None
        try:
            evidence = refresher()
        except Exception as exc:
            raise PluginConflictError(
                "live Scheduler projection refresh failed",
                code="RUNTIME_PROJECTION_REFRESH_FAILED",
            ) from exc
        invalid_tasks = (
            evidence.get("invalid_tasks")
            if isinstance(evidence, Mapping)
            else None
        )
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("initialized") is not True
            or invalid_tasks not in (None, (), [])
        ):
            raise PluginConflictError(
                "live Scheduler projection refresh returned incomplete evidence",
                code="RUNTIME_PROJECTION_REFRESH_FAILED",
            )
        return dict(evidence)

    def _apply_projection_transition(
        self,
        *,
        operation: str,
        automation_id: str,
        generation: int,
        expected_registration_ids: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        key = (automation_id, generation)
        expected_ids = tuple(sorted(str(value) for value in expected_registration_ids))
        if len(set(expected_ids)) != len(expected_ids):
            raise PluginConflictError(
                "managed contribution expected registration identities are duplicated",
                code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
            )
        evidence: Mapping[str, Any] | None = None
        service_transition: ServiceProjectRouteTransition | None = None

        def switch_service_route() -> None:
            nonlocal service_transition
            reference = self._services.project_reference(
                automation_id=automation_id,
                generation=generation,
            )
            if reference is None:
                return
            if operation == "apply":
                service_transition = self._services.activate_project_reference(
                    automation_id=automation_id,
                    generation=generation,
                )
            elif operation == "withdraw":
                service_transition = self._services.deactivate_project_reference(
                    automation_id=automation_id,
                    generation=generation,
                )

        def refresh() -> Mapping[str, Any] | None:
            nonlocal evidence, service_transition
            switch_service_route()
            try:
                evidence = self._refresh_scheduler_projection()
                return evidence
            except Exception:
                if service_transition is not None:
                    self._services.rollback_project_reference_transition(
                        service_transition,
                    )
                    service_transition = None
                raise

        with self._projection_lock:
            current_transition = self._pending_projection_transitions.get(key)
            if current_transition is None:
                self._pending_projection_transitions[key] = (
                    operation,
                    expected_ids,
                )
                self._projection_identities.begin(
                    key,
                    self._project_projection_identity(automation_id),
                )
            elif current_transition == (operation, expected_ids):
                self._projection_identities.require_baseline(
                    key,
                    self._project_projection_identity(automation_id),
                )
            else:
                raise PluginConflictError(
                    "runtime projection transition changed while pending",
                    code="RUNTIME_PROJECTION_STALE",
                )
            if operation == "apply":
                self._contributions.apply_generation(
                    automation_id,
                    generation,
                    refresh=refresh,
                    expected_registration_ids=expected_ids,
                )
            elif operation == "withdraw":
                has_contributions = any(
                    record.automation_id == automation_id
                    and record.generation == generation
                    for record in self._contributions.snapshot()
                )
                if has_contributions:
                    self._contributions.withdraw_generation(
                        automation_id,
                        generation,
                        refresh=refresh,
                    )
                else:
                    refresh()
            elif operation in {"apply", "withdraw"}:
                refresh()
            else:
                raise PluginConflictError(
                    "runtime projection transition is invalid",
                    code="RUNTIME_PROJECTION_TRANSITION_INVALID",
                )
            self._pending_projection_transitions.pop(key, None)
            self._projection_identities.clear_baseline(key)
            self._projection_identities.record(
                automation_id=automation_id,
                generation=generation if operation == "apply" else None,
                identity_sha256=self._project_projection_identity(automation_id),
            )
        return (
            dict(evidence)
            if evidence is not None
            else {
                "initialized": False,
                "deferred_until_scheduler_start": True,
                "invalid_tasks": [],
            }
        )

    def refresh_contribution_projection(self) -> Mapping[str, Any]:
        """Retry any committed projection switch, then verify live jobs."""

        with self._projection_lock:
            pending = tuple(
                sorted(
                    (
                        automation_id,
                        generation,
                        operation,
                        expected_ids,
                    )
                    for (automation_id, generation), (operation, expected_ids) in (
                        self._pending_projection_transitions.items()
                    )
                    if operation != "blocked"
                )
            )
            if not pending:
                evidence = self._refresh_scheduler_projection()
                return (
                    dict(evidence)
                    if evidence is not None
                    else {
                        "initialized": False,
                        "deferred_until_scheduler_start": True,
                        "invalid_tasks": [],
                    }
                )
            evidence: Mapping[str, Any] = {
                "initialized": False,
                "invalid_tasks": [],
            }
            for automation_id, generation, operation, expected_ids in pending:
                evidence = self._apply_projection_transition(
                    operation=operation,
                    automation_id=automation_id,
                    generation=generation,
                    expected_registration_ids=expected_ids,
                )
            return evidence

    def fail_closed_project_projection(
        self,
        *,
        automation_id: str,
        generation: int,
    ) -> Mapping[str, Any]:
        """Withdraw all process routes after an unrecoverable activation CAS.

        This path must not read the generation repository: it is specifically
        available when durable recovery evidence is conflicting or the
        database is unavailable.  The Scheduler callback owns a process-local
        tombstone so a later ordinary reload cannot resurrect the project.
        """

        automation_key = str(automation_id)
        generation_number = int(generation)
        if (
            not automation_key
            or automation_key != automation_key.strip()
            or generation_number < 1
        ):
            raise PluginConflictError(
                "runtime projection emergency identity is invalid",
                code="RUNTIME_PROJECTION_EMERGENCY_WITHDRAW_FAILED",
            )
        with self._projection_lock:
            self._services.block_project_references(automation_key)
            self._contributions.block_project(automation_key)
            for key in tuple(self._pending_projection_transitions):
                if key[0] == automation_key:
                    self._pending_projection_transitions.pop(key, None)
            self._pending_projection_transitions[
                (automation_key, generation_number)
            ] = ("blocked", ())
            self._emergency_blocked_projects[automation_key] = generation_number
            self._projection_identities.fail_closed(automation_key)
            withdrawer = self._scheduler_project_emergency_withdrawer
            if withdrawer is None:
                return {
                    "initialized": False,
                    "automation_id": automation_key,
                    "tombstoned": True,
                    "complete": True,
                    "removed_job_ids": [],
                    "remaining_target_job_ids": [],
                    "remaining_malformed_marker_job_ids": [],
                    "deferred_until_scheduler_start": True,
                }
            return self._validated_emergency_scheduler_withdrawal(
                automation_key,
                withdrawer=withdrawer,
            )

    @staticmethod
    def _validated_service_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        return _validated_service_registration_payload(payload)

    @staticmethod
    def _validated_contribution_payload(
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _validated_managed_contribution_effect_payload(payload)

    def _ensure_service_reference(self, payload: Mapping[str, Any]) -> None:
        material = self._validated_service_payload(payload)
        package_id = material["provider_registration_id"]
        reference_id = material["reference_id"]
        with self._service_lock:
            existing_package = self._reference_packages.get(reference_id)
            if existing_package is not None and existing_package != package_id:
                raise PluginConflictError(
                    "one generation cannot reference two service packages",
                    code="SERVICE_REFERENCE_CONFLICT",
                )
            self._services.register_contract(
                automation_id=package_id,
                generation=material["provider_generation"],
                plugin_id=material["plugin_id"],
                plugin_version=material["plugin_version"],
                package_sha256=material["package_sha256"],
                manifest_sha256=material["manifest_sha256"],
                runtime_mode=material["runtime_mode"],
                provides=tuple(material["provides"]),
                requires=tuple(material["requires"]),
                connector_requirements=tuple(
                    material.get("connector_requirements", ())
                ),
            )
            automation_id, raw_generation = reference_id.rsplit(":", 1)
            try:
                project_generation = int(raw_generation)
            except ValueError as exc:
                raise PluginConflictError(
                    "service project reference generation is invalid",
                    code="SERVICE_REFERENCE_INVALID",
                ) from exc
            self._services.bind_project_reference(
                provider_automation_id=package_id,
                automation_id=automation_id,
                generation=project_generation,
                package_sha256=material["package_sha256"],
                manifest_sha256=material["manifest_sha256"],
            )
            self._service_references.setdefault(package_id, set()).add(reference_id)
            self._reference_packages[reference_id] = package_id

    def _remove_service_reference(self, material: Mapping[str, Any]) -> None:
        package_id = str(material["provider_registration_id"])
        reference_id = str(material["reference_id"])
        automation_id, raw_generation = reference_id.rsplit(":", 1)
        try:
            generation = int(raw_generation)
        except ValueError as exc:
            raise PluginConflictError(
                "service project reference generation is invalid",
                code="SERVICE_REFERENCE_INVALID",
            ) from exc
        with self._service_lock:
            references = self._service_references.get(package_id)
            if references is None or reference_id not in references:
                return
            references.remove(reference_id)
            self._reference_packages.pop(reference_id, None)
            self._services.unbind_project_reference(
                provider_automation_id=package_id,
                automation_id=automation_id,
                generation=generation,
            )
            if references:
                return
            self._service_references.pop(package_id, None)
            self._services.unregister(
                package_id,
                generation=int(material["provider_generation"]),
            )

    @staticmethod
    def _is_managed_contribution_effect(effect: RuntimeEffectRecord) -> bool:
        return (
            effect.kind
            in {
                RuntimeEffectKind.SCHEDULE_BINDING,
                RuntimeEffectKind.WEBHOOK_BINDING,
                RuntimeEffectKind.CONTRIBUTION_REGISTRATION,
            }
            and effect.payload.get("contract_version") == _CONTRIBUTION_EFFECT_CONTRACT_VERSION
        )

    @staticmethod
    def _expected_contribution_plan(
        snapshot: RuntimeGenerationSnapshot,
        *,
        kind: RuntimeEffectKind,
        effect_key: str,
    ) -> RuntimeEffectPlan:
        matches = [
            plan
            for plan in _service_v2_contribution_effect_plans(snapshot)
            if plan.kind is kind and plan.effect_key == effect_key
        ]
        if len(matches) != 1:
            raise PluginConflictError(
                "managed contribution effect is absent from its generation",
                code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
            )
        return matches[0]

    def service_reference_count(self, package_sha256: str) -> int:
        package_id = package_provider_registration_id(package_sha256)
        with self._service_lock:
            return len(self._service_references.get(package_id, ()))

    def restore_from_repository(self, repository: Any) -> None:
        """Rebuild service and contribution registries from durable effects once."""

        with self._service_lock:
            if self._service_registry_restored:
                return
        runtime_id_reader = getattr(repository, "list_project_runtime_ids", None)
        if callable(runtime_id_reader):
            automation_ids = tuple(str(value or "").strip() for value in runtime_id_reader())
        else:
            automation_ids = tuple(
                str(runtime.automation_id or "").strip() for runtime in repository.list_project_runtimes()
            )
        if any(not value for value in automation_ids) or len(set(automation_ids)) != len(automation_ids):
            raise PluginConflictError(
                "runtime service restoration identities are missing or duplicated",
                code="PLUGIN_IDENTITY_CONFLICT",
            )
        restored_services: list[tuple[RuntimeGenerationSnapshot, RuntimeEffectRecord]] = []
        restored_contributions: list[tuple[RuntimeGenerationState, RuntimeGenerationSnapshot, RuntimeEffectRecord]] = []
        committed_by_project: dict[str, int] = {}
        committed_contribution_effects: dict[tuple[str, int], tuple[Any, list[RuntimeEffectRecord]]] = {}
        contribution_groups: dict[tuple[str, int], tuple[Any, Any, list[RuntimeEffectRecord]]] = {}
        activation_phases: dict[tuple[str, int], RuntimeActivationPhase | None] = {}
        eligible_states = {
            RuntimeGenerationState.TARGET,
            RuntimeGenerationState.PREPARING,
            RuntimeGenerationState.WAITING_COEFFECTS,
            RuntimeGenerationState.PREPARED,
            RuntimeGenerationState.COMMITTED,
            RuntimeGenerationState.DRAINING,
            RuntimeGenerationState.DISPOSING,
        }
        for automation_id in sorted(automation_ids):
            for generation in repository.list_project_generations(automation_id):
                if generation.state not in eligible_states:
                    continue
                snapshot = generation.snapshot
                generation_key = (snapshot.automation_id, snapshot.generation)
                activation_phases[generation_key] = generation.activation_phase
                activation_ready = generation.activation_phase in {
                    None,
                    RuntimeActivationPhase.ACTIVE,
                }
                if (
                    generation.state is RuntimeGenerationState.COMMITTED
                    and activation_ready
                ):
                    prior = committed_by_project.setdefault(
                        snapshot.automation_id,
                        snapshot.generation,
                    )
                    if prior != snapshot.generation:
                        raise PluginConflictError(
                            "multiple committed contribution generations were restored",
                            code="CONTRIBUTION_REGISTRATION_CONFLICT",
                        )
                    # Generations committed before managed contributions were
                    # introduced have neither this contract nor contribution
                    # effects.  Restore their durable service registration,
                    # but do not invent an empty contribution contract for
                    # them.  A present (even malformed) contract, or any
                    # persisted contribution effect, still takes the strict
                    # validation path below.
                    if "contributions" in snapshot.execution_metadata:
                        committed_contribution_effects[generation_key] = (
                            snapshot,
                            [],
                        )
                if (
                    "contributions" in snapshot.execution_metadata
                    and (
                        generation.state
                        in {
                            RuntimeGenerationState.DRAINING,
                            RuntimeGenerationState.DISPOSING,
                        }
                        or generation.activation_phase
                        in {
                            RuntimeActivationPhase.BLOCKED,
                            RuntimeActivationPhase.ROLLED_BACK,
                        }
                    )
                ):
                    contribution_groups.setdefault(
                        generation_key, (generation.state, snapshot, [])
                    )
                for effect in generation.effects:
                    if (
                        effect.kind is RuntimeEffectKind.SERVICE_REGISTRATION
                        and generation.state
                        in {
                            RuntimeGenerationState.COMMITTED,
                            RuntimeGenerationState.DRAINING,
                        }
                        and effect.state is RuntimeEffectState.APPLIED
                    ):
                        restored_services.append((snapshot, effect))
                    elif self._is_managed_contribution_effect(effect) and effect.state in {
                        RuntimeEffectState.APPLIED,
                        RuntimeEffectState.DISPOSING,
                        RuntimeEffectState.DISPOSED,
                    }:
                        restored_contributions.append((generation.state, snapshot, effect))
                        committed_effects = committed_contribution_effects.get(generation_key)
                        if committed_effects is not None:
                            committed_effects[1].append(effect)
        for snapshot, effect in sorted(
            restored_services,
            key=lambda item: (
                item[0].package_sha256,
                item[0].automation_id,
                item[0].generation,
            ),
        ):
            expected = _service_registration_material(snapshot)
            if canonical_json_bytes(dict(effect.payload)) != canonical_json_bytes(expected):
                raise PluginConflictError(
                    "persisted service registration effect does not match its generation",
                    code="SERVICE_REGISTRATION_EFFECT_MISMATCH",
                )
            self._ensure_service_reference(expected)
        for generation_state, snapshot, effect in restored_contributions:
            group_key = (snapshot.automation_id, snapshot.generation)
            existing = contribution_groups.get(group_key)
            if existing is None:
                contribution_groups[group_key] = (generation_state, snapshot, [effect])
                continue
            if existing[0] is not generation_state or existing[1] != snapshot:
                raise PluginConflictError(
                    "persisted contribution generation state is ambiguous",
                    code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
                )
            existing[2].append(effect)
        for group_key, (generation_state, snapshot, effects) in sorted(
            contribution_groups.items()
        ):
            if (
                generation_state is RuntimeGenerationState.COMMITTED
                and (snapshot.automation_id, snapshot.generation)
                in committed_contribution_effects
            ):
                continue
            retiring = generation_state in {
                RuntimeGenerationState.DRAINING,
                RuntimeGenerationState.DISPOSING,
            }
            partial = generation_state in {
                RuntimeGenerationState.TARGET,
                RuntimeGenerationState.PREPARING,
                RuntimeGenerationState.WAITING_COEFFECTS,
            }
            activation_phase = activation_phases[group_key]
            inactive = retiring or activation_phase is RuntimeActivationPhase.BLOCKED
            allowed_states = {RuntimeEffectState.APPLIED}
            if retiring:
                allowed_states.update({RuntimeEffectState.DISPOSING, RuntimeEffectState.DISPOSED})
            materials = self._validated_generation_contribution_materials(
                snapshot=snapshot,
                effects=effects,
                allowed_states=frozenset(allowed_states),
                require_exact=not partial,
            )
            if partial:
                materials = tuple(
                    self._validated_contribution_payload(plan.payload)
                    for plan in _service_v2_contribution_effect_plans(snapshot)
                )
            if retiring:
                remaining = {
                    (effect.kind, effect.effect_key)
                    for effect in effects
                    if effect.state is not RuntimeEffectState.DISPOSED
                }
                materials = tuple(
                    material
                    for plan, material in zip(
                        _service_v2_contribution_effect_plans(snapshot), materials
                    )
                    if (plan.kind, plan.effect_key) in remaining
                )
            if materials and activation_phase is not RuntimeActivationPhase.ROLLED_BACK:
                self._contributions.prepare_generation(
                    materials, committed=(generation_state is RuntimeGenerationState.COMMITTED),
                    restored_inactive=inactive,
                )
        for snapshot, effects in committed_contribution_effects.values():
            materials = self._validated_generation_contribution_materials(
                snapshot=snapshot,
                effects=effects,
                allowed_states=frozenset({RuntimeEffectState.APPLIED}),
            )
            if materials:
                self._contributions.prepare_generation(materials, committed=True)
        for automation_id, generation in sorted(committed_by_project.items()):
            if self._services.project_reference(
                automation_id=automation_id,
                generation=generation,
            ) is not None:
                self._services.activate_project_reference(
                    automation_id=automation_id,
                    generation=generation,
                )
            self._contributions.activate(automation_id, generation)
        with self._service_lock:
            self._service_registry_restored = True

    @staticmethod
    def _contained_regular_file(
        root: Path,
        relative: object,
        *,
        prefix: tuple[str, ...] = (),
    ) -> Path:
        pure = PurePosixPath(str(relative or ""))
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise PluginConflictError("plugin executable path is unsafe")
        unresolved = root.joinpath(*prefix, *pure.parts)
        if unresolved.is_symlink():
            raise PluginConflictError("plugin executable cannot be a symlink")
        resolved = unresolved.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PluginConflictError("plugin executable escaped its install root") from exc
        if not resolved.is_file():
            raise PluginConflictError("plugin executable is missing")
        return resolved

    @staticmethod
    def _runtime_metadata(snapshot: RuntimeGenerationSnapshot) -> dict[str, Any]:
        descriptor = snapshot.execution_metadata.get("runtime_descriptor")
        if not isinstance(descriptor, Mapping):
            raise PluginConflictError("generation runtime descriptor is missing")
        install = descriptor.get("install_metadata")
        if not isinstance(install, Mapping):
            raise PluginConflictError("generation install descriptor is missing")
        return {
            "install_root": str(install.get("install_root") or ""),
            "install_metadata": copy.deepcopy(dict(install)),
        }

    def ensure_applied(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        plan: RuntimeEffectPlan,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        runtime = self._runtime_metadata(snapshot)
        if plan.kind in {
            RuntimeEffectKind.PACKAGE_REFERENCE,
            RuntimeEffectKind.VENV_REFERENCE,
            RuntimeEffectKind.INSTANCE_RUNTIME,
        }:
            self._integrity.verify_install_root(runtime)
        if plan.kind == RuntimeEffectKind.INSTANCE_RUNTIME:
            descriptor = snapshot.execution_metadata["runtime_descriptor"]
            install = descriptor["install_metadata"]
            root = Path(str(install["install_root"])).resolve()
            self._contained_regular_file(
                root,
                install.get("python_relative"),
            )
            self._contained_regular_file(
                root,
                descriptor["runtime"].get("entrypoint"),
                prefix=("package",),
            )
        if plan.kind == RuntimeEffectKind.BROKER_SCOPE:
            permissions = snapshot.execution_metadata["runtime_descriptor"]["runtime_permissions"]
            operations = permissions.get("broker_operations")
            required = (
                {
                    (str(item.get("operation") or ""), str(item.get("action") or ""))
                    for item in operations
                    if isinstance(item, Mapping)
                }
                if isinstance(operations, list)
                else set()
            )
            handlers_ready = (
                all(pair in self._handler_keys or (pair[0], "*") in self._handler_keys for pair in required)
                if snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2
                else bool(required) and required <= self._handler_keys
            )
            if not handlers_ready:
                raise PluginConflictError(
                    "generation broker scope is unavailable",
                    code="CORE_ADAPTER_ACTION_UNAVAILABLE",
                )
        if plan.kind is RuntimeEffectKind.SERVICE_REGISTRATION:
            expected = _service_registration_material(snapshot)
            if (
                effect.automation_id != snapshot.automation_id
                or effect.generation != snapshot.generation
                or effect.kind is not RuntimeEffectKind.SERVICE_REGISTRATION
                or expected["reference_id"] != f"{effect.automation_id}:{effect.generation}"
                or canonical_json_bytes(dict(plan.payload)) != canonical_json_bytes(expected)
            ):
                raise PluginConflictError(
                    "service registration plan does not match its generation",
                    code="SERVICE_REGISTRATION_EFFECT_MISMATCH",
                )
            # A prepared generation owns only a durable effect journal.  The
            # service must not become routable until the generation CAS commits.
        if snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2 and plan.kind in {
            RuntimeEffectKind.SCHEDULE_BINDING,
            RuntimeEffectKind.WEBHOOK_BINDING,
            RuntimeEffectKind.CONTRIBUTION_REGISTRATION,
        }:
            expected = self._expected_contribution_plan(
                snapshot,
                kind=plan.kind,
                effect_key=plan.effect_key,
            )
            if (
                effect.automation_id != snapshot.automation_id
                or effect.generation != snapshot.generation
                or effect.kind is not plan.kind
                or canonical_json_bytes(dict(plan.payload)) != canonical_json_bytes(dict(expected.payload))
            ):
                raise PluginConflictError(
                    "contribution registration plan does not match its generation",
                    code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
                )
            material = self._validated_contribution_payload(expected.payload)
            if material["backend_status"] not in {"READY", "DISABLED"}:
                raise PluginConflictError(
                    "enabled contribution has no compatible host backend",
                    code="CAPABILITY_UNAVAILABLE",
                )
            generation_materials = tuple(
                self._validated_contribution_payload(item.payload)
                for item in _service_v2_contribution_effect_plans(snapshot)
            )
            self._contributions.prepare_generation(generation_materials, committed=False)
        return replace(
            effect,
            state=RuntimeEffectState.APPLIED,
            payload=dict(plan.payload),
        )

    def _validated_generation_contribution_materials(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        effects: Sequence[RuntimeEffectRecord],
        allowed_states: frozenset[RuntimeEffectState],
        require_exact: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        plans = _service_v2_contribution_effect_plans(snapshot)
        expected_plans = {
            (plan.kind, plan.effect_key): plan
            for plan in plans
        }
        observed: dict[tuple[RuntimeEffectKind, str], dict[str, Any]] = {}
        for effect in effects:
            if not self._is_managed_contribution_effect(effect):
                continue
            if (
                effect.automation_id != snapshot.automation_id
                or effect.generation != snapshot.generation
                or effect.state not in allowed_states
            ):
                raise PluginConflictError(
                    "contribution effect identity is invalid",
                    code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
                )
            key = (effect.kind, effect.effect_key)
            expected = expected_plans.get(key)
            if expected is None or key in observed:
                raise PluginConflictError(
                    "contribution effect is absent from or duplicated in its generation",
                    code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
                )
            if canonical_json_bytes(dict(effect.payload)) != canonical_json_bytes(
                dict(expected.payload)
            ):
                raise PluginConflictError(
                    "contribution effect payload is invalid",
                    code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
                )
            observed[key] = self._validated_contribution_payload(expected.payload)
        if require_exact and set(observed) != set(expected_plans):
            raise PluginConflictError(
                "contribution effects do not exactly match their generation",
                code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
            )
        return tuple(
            observed[(plan.kind, plan.effect_key)]
            for plan in plans
            if (plan.kind, plan.effect_key) in observed
        )

    def _validated_generation_service_materials(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        effects: Sequence[RuntimeEffectRecord],
        allowed_states: frozenset[RuntimeEffectState],
    ) -> tuple[dict[str, Any], ...]:
        expected = _service_registration_material(snapshot)
        materials: list[dict[str, Any]] = []
        for effect in effects:
            if effect.kind is not RuntimeEffectKind.SERVICE_REGISTRATION:
                continue
            if (
                effect.automation_id != snapshot.automation_id
                or effect.generation != snapshot.generation
                or effect.state not in allowed_states
                or expected["reference_id"]
                != f"{effect.automation_id}:{effect.generation}"
                or canonical_json_bytes(dict(effect.payload))
                != canonical_json_bytes(expected)
            ):
                raise PluginConflictError(
                    "service registration effect payload is invalid",
                    code="SERVICE_REGISTRATION_EFFECT_MISMATCH",
                )
            materials.append(self._validated_service_payload(expected))
        return tuple(materials)

    def activate_committed(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        effects: Sequence[RuntimeEffectRecord],
    ) -> None:
        """Activate exactly the contribution effects behind the committed CAS."""

        if snapshot.runtime_model is not PluginRuntimeModel.SERVICE_V2:
            return
        allowed_states = frozenset({RuntimeEffectState.APPLIED})
        services = self._validated_generation_service_materials(
            snapshot=snapshot,
            effects=effects,
            allowed_states=allowed_states,
        )
        contributions = self._validated_generation_contribution_materials(
            snapshot=snapshot,
            effects=effects,
            allowed_states=allowed_states,
        )
        for material in services:
            self._ensure_service_reference(material)
        if contributions:
            self._contributions.prepare_generation(
                contributions,
                committed=False,
            )
        self._apply_projection_transition(
            operation="apply",
            automation_id=snapshot.automation_id,
            generation=snapshot.generation,
            expected_registration_ids=tuple(
                material["registration_id"] for material in contributions
            ),
        )

    def rollback_committed_activation(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        effects: Sequence[RuntimeEffectRecord],
        restored_snapshot: RuntimeGenerationSnapshot | None = None,
        restored_effects: Sequence[RuntimeEffectRecord] = (),
    ) -> None:
        """Restore the durable predecessor after an exact reverse-CAS.

        The pending baseline or successful activation receipt is the compare
        side; only that unchanged candidate process projection may be replaced.
        """

        if snapshot.runtime_model is not PluginRuntimeModel.SERVICE_V2:
            return
        services = self._validated_generation_service_materials(
            snapshot=snapshot,
            effects=effects,
            allowed_states=frozenset({RuntimeEffectState.APPLIED}),
        )
        contributions = self._validated_generation_contribution_materials(
            snapshot=snapshot,
            effects=effects,
            allowed_states=frozenset({RuntimeEffectState.APPLIED}),
        )
        restored_services: tuple[dict[str, Any], ...] = ()
        restored_contributions: tuple[dict[str, Any], ...] = ()
        if restored_snapshot is not None:
            if (
                restored_snapshot.automation_id != snapshot.automation_id
                or restored_snapshot.generation >= snapshot.generation
            ):
                raise PluginConflictError(
                    "durable predecessor identity is invalid",
                    code="RUNTIME_PROJECTION_ROLLBACK_FAILED",
                )
            if restored_snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2:
                restored_services = self._validated_generation_service_materials(
                    snapshot=restored_snapshot,
                    effects=restored_effects,
                    allowed_states=frozenset({RuntimeEffectState.APPLIED}),
                )
                restored_contributions = (
                    self._validated_generation_contribution_materials(
                        snapshot=restored_snapshot,
                        effects=restored_effects,
                        allowed_states=frozenset({RuntimeEffectState.APPLIED}),
                    )
                )
        with self._projection_lock:
            key = (snapshot.automation_id, snapshot.generation)
            pending = self._pending_projection_transitions.get(key)
            if pending is None:
                expected_projection = self._projection_identities.expected_rollback(
                    key,
                    pending=False,
                )
            elif pending[0] == "apply":
                expected_projection = self._projection_identities.expected_rollback(
                    key,
                    pending=True,
                )
            else:
                raise PluginConflictError(
                    "failed activation has a conflicting projection transition",
                    code="RUNTIME_PROJECTION_ROLLBACK_FAILED",
                )
            self._projection_identities.require_exact(
                snapshot.automation_id,
                expected_projection,
                self._project_projection_identity(snapshot.automation_id),
            )
            self._pending_projection_transitions.pop(key, None)
            self._projection_identities.clear_baseline(key)
            if (
                restored_snapshot is not None
                and restored_snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2
            ):
                for material in restored_services:
                    self._ensure_service_reference(material)
                if restored_contributions:
                    self._contributions.prepare_generation(
                        restored_contributions,
                        committed=False,
                    )
                self._apply_projection_transition(
                    operation="apply",
                    automation_id=restored_snapshot.automation_id,
                    generation=restored_snapshot.generation,
                    expected_registration_ids=tuple(
                        material["registration_id"]
                        for material in restored_contributions
                    ),
                )
            else:
                self._apply_projection_transition(
                    operation="withdraw",
                    automation_id=snapshot.automation_id,
                    generation=snapshot.generation,
                    expected_registration_ids=(),
                )
            for material in contributions:
                self._contributions.unregister(material["registration_id"])
            for material in reversed(services):
                self._remove_service_reference(material)
            self._projection_identities.record(
                automation_id=snapshot.automation_id,
                generation=(
                    restored_snapshot.generation
                    if restored_snapshot is not None
                    and restored_snapshot.runtime_model
                    is PluginRuntimeModel.SERVICE_V2
                    else None
                ),
                identity_sha256=self._project_projection_identity(
                    snapshot.automation_id,
                ),
            )

    def deactivate_committed(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        effects: Sequence[RuntimeEffectRecord],
    ) -> None:
        """Withdraw process routes while retaining the committed durable journal."""

        if snapshot.runtime_model is not PluginRuntimeModel.SERVICE_V2:
            return
        allowed_states = frozenset({RuntimeEffectState.APPLIED})
        self._validated_generation_contribution_materials(
            snapshot=snapshot,
            effects=effects,
            allowed_states=allowed_states,
        )
        self.deactivate_generation(snapshot=snapshot, effects=effects)
        services = self._validated_generation_service_materials(
            snapshot=snapshot,
            effects=effects,
            allowed_states=allowed_states,
        )
        for material in reversed(services):
            self._remove_service_reference(material)

    def deactivate_generation(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        effects: Sequence[RuntimeEffectRecord],
    ) -> None:
        """Atomically withdraw one generation before row-wise compensation."""

        if snapshot.runtime_model is not PluginRuntimeModel.SERVICE_V2:
            return
        self._validated_generation_service_materials(
            snapshot=snapshot,
            effects=effects,
            allowed_states=frozenset(
                {
                    RuntimeEffectState.APPLIED,
                    RuntimeEffectState.DISPOSING,
                    RuntimeEffectState.DISPOSED,
                }
            ),
        )
        self._validated_generation_contribution_materials(
            snapshot=snapshot,
            effects=effects,
            allowed_states=frozenset(
                {
                    RuntimeEffectState.APPLIED,
                    RuntimeEffectState.DISPOSING,
                    RuntimeEffectState.DISPOSED,
                }
            ),
        )
        self._apply_projection_transition(
            operation="withdraw",
            automation_id=snapshot.automation_id,
            generation=snapshot.generation,
        )

    def dispose(self, effect: RuntimeEffectRecord) -> None:
        if effect.reversible is not True:
            raise PluginConflictError(
                "non-reversible generation effect cannot be disposed",
                code="RUNTIME_EFFECT_NOT_REVERSIBLE",
            )
        if self._is_managed_contribution_effect(effect):
            material = self._validated_contribution_payload(effect.payload)
            if material["automation_id"] != effect.automation_id or material["generation"] != effect.generation:
                raise PluginConflictError(
                    "contribution registration does not match its generation",
                    code="CONTRIBUTION_REGISTRATION_EFFECT_MISMATCH",
                )
            self._contributions.unregister(material["registration_id"])
            return
        if effect.kind is not RuntimeEffectKind.SERVICE_REGISTRATION:
            return
        material = self._validated_service_payload(effect.payload)
        expected_reference = f"{effect.automation_id}:{effect.generation}"
        if material["reference_id"] != expected_reference:
            raise PluginConflictError(
                "service registration reference does not match its generation",
                code="SERVICE_REGISTRATION_EFFECT_MISMATCH",
            )
        self._remove_service_reference(material)


class MySQLRuntimeTargetService:
    """Compile desired rows and drive crash-safe generation reconciliation."""

    def __init__(
        self,
        *,
        orchestration_repository: Any,
        catalog: PluginCatalog,
        core_catalog: Any,
        runtime_repository: MySQLAutomationPluginRuntimeAdapter,
        reconciler: AutomationRuntimeReconciler,
        wake_runner: Callable[[str], None] | None = None,
        catalog_repository: Any | None = None,
    ) -> None:
        self._orchestration = orchestration_repository
        self._catalog = catalog
        self._core_catalog = core_catalog
        self._runtime = runtime_repository
        self._reconciler = reconciler
        self._wake_runner = wake_runner
        self._catalog_repository = catalog_repository
        self._last_reconcile_failures: dict[str, dict[str, str]] = {}

    def set_wake_runner(self, wake_runner: Callable[[str], None] | None) -> None:
        """Bind the process-local wake hook after the Runner is constructed."""

        self._wake_runner = wake_runner

    def _desired_rows(
        self,
        automation_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._orchestration.unit_of_work() as uow:
            config = uow.automation_plugins.get_project_config(automation_id)
            policy = uow.automation_projects.get_policy(automation_id)
        if not isinstance(config, Mapping) or not isinstance(policy, Mapping):
            raise PluginConflictError("project desired state is incomplete")
        return dict(config), dict(policy)

    @staticmethod
    def _same_material(
        current: RuntimeGenerationSnapshot,
        desired: RuntimeGenerationSnapshot,
        *,
        manifest_schema_version: int,
    ) -> bool:
        fields = (
            "plugin_id",
            "plugin_version",
            "package_sha256",
            "manifest_sha256",
            "trust_source",
            "project_config_sha256",
            "account_bindings_sha256",
            "resource_bindings_sha256",
            "device_binding_sha256",
            "schedule_sha256",
            "core_registry_sha256",
            "tool_contract_sha256",
            "invocation_contracts_sha256",
            "compiled_invocations_sha256",
            "governance_anchor_sha256",
            "enabled_entrypoints",
        )
        if not all(getattr(current, field) == getattr(desired, field) for field in fields):
            return False
        current_metadata = current.execution_metadata
        desired_metadata = desired.execution_metadata
        if not isinstance(current_metadata, Mapping) or not isinstance(
            desired_metadata,
            Mapping,
        ):
            return False
        current_descriptor = current_metadata.get("runtime_descriptor")
        desired_descriptor = desired_metadata.get("runtime_descriptor")
        if not isinstance(current_descriptor, Mapping) or not isinstance(
            desired_descriptor,
            Mapping,
        ):
            return False
        if (
            _digest(current_descriptor) != current.runtime_descriptor_sha256
            or _digest(desired_descriptor) != desired.runtime_descriptor_sha256
        ):
            return False
        current_without_descriptor = copy.deepcopy(dict(current_metadata))
        desired_without_descriptor = copy.deepcopy(dict(desired_metadata))
        current_without_descriptor.pop("runtime_descriptor", None)
        desired_without_descriptor.pop("runtime_descriptor", None)
        if canonical_json_bytes(current_without_descriptor) != canonical_json_bytes(desired_without_descriptor):
            return False
        return runtime_descriptor_matches_signed_installation(
            current_descriptor,
            desired_descriptor,
            schema_version=manifest_schema_version,
        )

    def _reconcile_uninstalling_project(
        self,
        automation_id: str,
        runtime: Any,
        generations: Sequence[Any],
    ) -> RuntimeReconcileResult | None:
        """Drain and dispose all effects after uninstall revocation.

        Uninstall preparation intentionally marks the currently committed
        generation as ``DRAINING`` too: there is no successor route once the
        project is revoked.  The ordinary generation reconciler protects the
        current route from disposal, so this explicit uninstall path is the
        only place allowed to dispose it.  The repository still rechecks
        leases and unknown writes for every generation.
        """

        disposed: list[int] = []
        draining: list[int] = []
        for generation in sorted(
            generations,
            key=lambda item: item.snapshot.generation,
        ):
            number = generation.snapshot.generation
            if generation.state is RuntimeGenerationState.DISPOSED:
                continue
            draining.append(number)
            if generation.state is RuntimeGenerationState.COMMITTED:
                self._runtime.mark_generation_draining(automation_id, number)
                generation = self._runtime.get_generation(automation_id, number) or generation
            if generation.state not in {
                RuntimeGenerationState.DRAINING,
                RuntimeGenerationState.DISPOSING,
                RuntimeGenerationState.FAILED,
            }:
                raise PluginConflictError(
                    "uninstall runtime generation is not drainable",
                    code="PLUGIN_UNINSTALL_RUNTIME_STATE_INVALID",
                )
            if self._reconciler.dispose_generation(generation):
                disposed.append(number)
        refreshed = self._runtime.get_project_runtime(automation_id) or runtime
        return RuntimeReconcileResult(
            automation_id=automation_id,
            target_generation=int(refreshed.target_generation),
            committed_generation=refreshed.committed_generation,
            draining_generations=tuple(draining),
            disposed_generations=tuple(disposed),
        )

    def _affected_v2_consumer_ids(
        self,
        provider_automation_id: str,
        provider_services: Sequence[str],
    ) -> tuple[str, ...]:
        """Resolve direct and transitive consumers before Provider withdrawal."""

        pending_services = {str(service).strip() for service in provider_services if str(service).strip()}
        affected: set[str] = set()
        entries = tuple(self._catalog.list())
        changed = True
        while changed:
            changed = False
            for entry in sorted(entries, key=lambda item: item.automation_id):
                automation_id = str(entry.automation_id)
                if automation_id == provider_automation_id or automation_id in affected:
                    continue
                if entry.runtime_model != PluginRuntimeModel.SERVICE_V2.value:
                    continue
                if not pending_services.intersection(entry.required_services):
                    continue
                affected.add(automation_id)
                pending_services.update(entry.provided_services)
                changed = True
        return tuple(sorted(affected))

    def suspend_provider_consumers(
        self,
        provider_automation_id: str,
        *,
        provider_services: Sequence[str],
    ) -> tuple[str, ...]:
        """Persistently close every affected consumer before Provider mutation."""

        consumers = self._affected_v2_consumer_ids(
            provider_automation_id,
            provider_services,
        )
        for automation_id in consumers:
            self._runtime.set_project_dependency_scheduler_gate(
                automation_id,
                dependency_ready=False,
            )
        return consumers

    def restore_provider_dependency_tree(
        self,
        provider_automation_id: str,
        *,
        provider_services: Sequence[str],
        consumer_automation_ids: Sequence[str] | None = None,
    ) -> object:
        """Restore exact projections first, then open strict scheduler gates."""

        consumers = tuple(
            consumer_automation_ids
            if consumer_automation_ids is not None
            else self._affected_v2_consumer_ids(
                provider_automation_id,
                provider_services,
            )
        )
        projects = (provider_automation_id, *consumers)
        for automation_id in projects:
            self._runtime.set_project_dependency_scheduler_gate(
                automation_id,
                dependency_ready=False,
            )
        result = self.reconcile_project(
            provider_automation_id,
            defer_scheduler_enable=True,
        )
        projections: list[tuple[str, object]] = [(provider_automation_id, result)]
        for automation_id in consumers:
            projections.append(
                (
                    automation_id,
                    self.reconcile_project(
                        automation_id,
                        defer_scheduler_enable=True,
                    ),
                )
            )
        for automation_id, projection in projections:
            waiting = getattr(projection, "waiting_coeffects", None)
            if not isinstance(waiting, tuple) or waiting:
                continue
            self._runtime.set_project_dependency_scheduler_gate(
                automation_id,
                dependency_ready=True,
            )
        return result

    def reconcile_provider_dependency_tree(
        self,
        provider_automation_id: str,
        *,
        provider_services: Sequence[str],
        enabled: bool,
        consumer_automation_ids: Sequence[str] | None = None,
    ) -> object:
        consumers = tuple(
            consumer_automation_ids
            if consumer_automation_ids is not None
            else self._affected_v2_consumer_ids(
                provider_automation_id,
                provider_services,
            )
        )
        if enabled:
            return self.restore_provider_dependency_tree(
                provider_automation_id,
                provider_services=provider_services,
                consumer_automation_ids=consumers,
            )
        result = self.reconcile_project(provider_automation_id)
        for automation_id in consumers:
            self.reconcile_project(automation_id)
        return result

    def reconcile_project(
        self,
        automation_id: str,
        *,
        defer_scheduler_enable: bool = False,
    ) -> object:
        entry = self._catalog.require(automation_id)
        runtime = self._runtime.get_project_runtime(automation_id)
        generations = tuple(self._runtime.list_project_generations(automation_id))
        if not entry.enabled and not entry.configured and runtime is None and not generations:
            # A signed action may be installed before the administrator binds
            # accounts/configuration.  It has no executable generation and is
            # therefore a safe, non-blocking catalog state.
            return None
        by_number = {item.snapshot.generation: item for item in generations}
        if runtime is not None and entry.state == "UNINSTALLING":
            return self._reconcile_uninstalling_project(
                automation_id,
                runtime,
                generations,
            )
        blocked_unknown_committed = False
        if runtime is not None:
            committed_generation = runtime.committed_generation
            committed = by_number.get(committed_generation) if committed_generation is not None else None
            if (
                committed_generation is not None
                and committed is not None
                and committed.state is RuntimeGenerationState.BLOCKED
            ):
                if self._runtime.has_unknown_generation_write(
                    automation_id,
                    committed_generation,
                ):
                    # Preserve the unknown-write generation as an archival
                    # safety fence, but allow a separately prepared successor
                    # to atomically take the current route.  The repository CAS
                    # locks and verifies this exact state and lease before it
                    # permits the exception.
                    blocked_unknown_committed = True
                else:
                    raise PluginConflictError(
                        "committed runtime generation is blocked without unknown-write evidence",
                        code="RUNTIME_COMMIT_INCONSISTENT",
                    )
            target = by_number.get(runtime.target_generation)
            if target is not None and target.state in {
                RuntimeGenerationState.TARGET,
                RuntimeGenerationState.PREPARING,
                RuntimeGenerationState.WAITING_COEFFECTS,
                RuntimeGenerationState.PREPARED,
            }:
                config, policy = self._desired_rows(automation_id)
                try:
                    target_candidate = build_runtime_generation_snapshot(
                        entry,
                        desired_config_row=config,
                        policy_row=policy,
                        generation=target.snapshot.generation,
                        core_catalog=self._core_catalog,
                    )
                except PluginConflictError as exc:
                    raise PluginConflictError(
                        "runtime target no longer matches current signed desired state",
                        code="RUNTIME_TARGET_MATERIAL_MISMATCH",
                    ) from exc
                if not self._same_material(
                    target.snapshot,
                    target_candidate,
                    manifest_schema_version=entry.manifest_schema_version,
                ):
                    raise PluginConflictError(
                        "runtime target no longer matches current signed desired state",
                        code="RUNTIME_TARGET_MATERIAL_MISMATCH",
                    )
                return self._reconciler.resume_project(automation_id)
            if runtime.reconcile_state in {
                RuntimeReconcileState.DRAINING,
                RuntimeReconcileState.DISPOSING,
            }:
                return self._reconciler.resume_project(automation_id)
            if runtime.reconcile_state in {
                RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
                RuntimeReconcileState.ERROR,
            }:
                # These states require evidence or an explicit administrator
                # repair.  Keep the affected project unavailable without
                # preventing the rest of the Agent from starting.
                if not (
                    runtime.reconcile_state is RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE and blocked_unknown_committed
                ):
                    return None

        config, policy = self._desired_rows(automation_id)
        next_generation = max(by_number, default=0) + 1
        if not generations and runtime is not None:
            next_generation = max(1, runtime.target_generation)
        if (
            runtime is not None
            and runtime.reconcile_state is RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE
            and blocked_unknown_committed
        ):
            policy_generation = _required_policy_generation(policy)
            if policy_generation == runtime.committed_generation:
                # Unknown-write evidence freezes the current route. Only an
                # explicit, policy-bound release stage may create a successor.
                return None
            if policy_generation != next_generation:
                raise PluginConflictError(
                    "project policy is not bound to the desired runtime generation",
                    code="PLUGIN_POLICY_GENERATION_MISMATCH",
                )
        if (
            runtime is not None
            and runtime.committed_generation is not None
            and runtime.target_generation == runtime.committed_generation
            and runtime.reconcile_state == RuntimeReconcileState.STABLE
        ):
            committed = by_number.get(runtime.committed_generation)
            if committed is None or committed.state is not RuntimeGenerationState.COMMITTED:
                raise PluginConflictError(
                    "stable project does not point to a committed generation record",
                    code="RUNTIME_COMMIT_INCONSISTENT",
                )
            policy_generation = _required_policy_generation(policy)
            if policy_generation == runtime.committed_generation:
                # A restart observes policy bound to the already-committed
                # generation. Compare that exact closed material before ever
                # asking the policy row to authorize a new generation.
                committed_candidate = build_runtime_generation_snapshot(
                    entry,
                    desired_config_row=config,
                    policy_row=policy,
                    generation=runtime.committed_generation,
                    core_catalog=self._core_catalog,
                )
                if self._same_material(
                    committed.snapshot,
                    committed_candidate,
                    manifest_schema_version=entry.manifest_schema_version,
                ):
                    if entry.runtime_model == PluginRuntimeModel.SERVICE_V2.value:
                        return self._reconciler.reconcile_committed_projection(
                            committed,
                            project_enabled=entry.enabled,
                            defer_scheduler_enable=defer_scheduler_enable,
                        )
                    return None
            elif policy_generation != next_generation:
                raise PluginConflictError(
                    "project policy is not bound to the desired runtime generation",
                    code="PLUGIN_POLICY_GENERATION_MISMATCH",
                )

        desired = build_runtime_generation_snapshot(
            entry,
            desired_config_row=config,
            policy_row=policy,
            generation=next_generation,
            core_catalog=self._core_catalog,
        )
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "boyi:plugin-generation:"
                f"{automation_id}:{next_generation}:{desired.manifest_sha256}:"
                f"{desired.project_config_sha256}:{desired.policy_contract_sha256}",
            )
        )
        return self._reconciler.reconcile(
            desired,
            expected_committed_generation=(runtime.committed_generation if runtime is not None else None),
            request_id=request_id,
        )

    def resolve_unknown_write_not_applied(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        evidence_sha256: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """Use the shared recovery transaction for a verified empty readback."""

        return self._runtime.resolve_unknown_write_not_applied(
            automation_id=automation_id,
            generation=generation,
            lease_id=lease_id,
            evidence_sha256=evidence_sha256,
            request_id=request_id,
            actor_id=actor_id,
            actor_role=actor_role,
        )

    def recover_unknown_write(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
        authoritative_applied_proof: Mapping[str, object] | None = None,
        authoritative_not_applied_proof: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Resolve only from server-owned durable receipt evidence."""

        result = self._runtime.resolve_unknown_write_recovery(
            automation_id=automation_id,
            generation=generation,
            lease_id=lease_id,
            request_id=request_id,
            actor_id=actor_id,
            actor_role=actor_role,
            authoritative_applied_proof=authoritative_applied_proof,
            authoritative_not_applied_proof=authoritative_not_applied_proof,
        )
        run_id = str(result.get("run_id") or "")
        if result.get("transitioned") is True and run_id and self._wake_runner:
            self._wake_runner(run_id)
        return result

    def recover_current_unknown_write(
        self,
        *,
        automation_id: str,
        generation: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        authoritative_applied_proof: Mapping[str, object] | None = None,
        authoritative_not_applied_proof: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Resolve the sole current unknown lease from server-owned evidence."""

        result = self._runtime.resolve_current_unknown_write_recovery(
            automation_id=automation_id,
            generation=generation,
            request_id=request_id,
            actor_id=actor_id,
            actor_role=actor_role,
            authoritative_applied_proof=authoritative_applied_proof,
            authoritative_not_applied_proof=authoritative_not_applied_proof,
        )
        run_id = str(result.get("run_id") or "")
        if result.get("transitioned") is True and run_id and self._wake_runner:
            self._wake_runner(run_id)
        return result

    def recover_unknown_writes_not_applied(
        self,
        *,
        automation_id: str,
        generation: int,
        recoveries: Sequence[Mapping[str, object]],
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """Resolve one bounded sibling set only from exact empty readback."""

        result = self._runtime.resolve_unknown_writes_not_applied(
            automation_id=automation_id,
            generation=generation,
            recoveries=recoveries,
            actor_id=actor_id,
            actor_role=actor_role,
        )
        if self._wake_runner:
            for recovered in result.get("results", ()):
                if not isinstance(recovered, Mapping):
                    continue
                run_id = str(recovered.get("run_id") or "")
                if recovered.get("transitioned") is True and run_id:
                    self._wake_runner(run_id)
        return result

    def inspect_current_unknown_write(
        self, *, automation_id: str, generation: int,
    ) -> dict[str, Any]:
        """Return the sole current receipt locator for server readback."""
        return self._runtime.inspect_current_unknown_write_recovery(
            automation_id=automation_id,
            generation=generation,
        )

    def inspect_current_unknown_writes(
        self, *, automation_id: str, generation: int,
    ) -> dict[str, Any]:
        """Return a bounded current sibling set for exact server readback."""
        return self._runtime.inspect_current_unknown_write_recoveries(
            automation_id=automation_id,
            generation=generation,
        )

    def inspect_scan_unknown_write_candidates(
        self,
        *,
        automation_id: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Inspect unresolved scan leases across committed and archived generations."""

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("scan recovery candidate limit is invalid")
        candidates: list[dict[str, Any]] = []
        generations = sorted(
            self._runtime.list_project_generations(automation_id),
            key=lambda item: item.snapshot.generation,
        )
        for item in generations:
            generation = int(item.snapshot.generation)
            batch = self._runtime.inspect_current_unknown_write_recoveries(
                automation_id=automation_id,
                generation=generation,
            )
            state = str(batch.get("state") or "")
            if state == "RECOVERY_LEASE_LIMIT_EXCEEDED":
                return {
                    "state": "RECOVERY_LEASE_LIMIT_EXCEEDED",
                    "candidate_count": int(batch.get("lease_count") or 0),
                    "candidates": [],
                }
            if state == "RECOVERY_LEASE_MISSING":
                continue
            snapshots = batch.get("snapshots")
            if (
                state != "RECOVERY_LEASES_IDENTIFIED"
                or not isinstance(snapshots, list)
                or any(not isinstance(snapshot, Mapping) for snapshot in snapshots)
            ):
                return {
                    "state": "RECOVERY_CANDIDATES_INVALID",
                    "candidate_count": 0,
                    "candidates": [],
                }
            for snapshot in snapshots:
                candidates.append({"generation": generation, "snapshot": dict(snapshot)})
                if len(candidates) > limit:
                    return {
                        "state": "RECOVERY_LEASE_LIMIT_EXCEEDED",
                        "candidate_count": len(candidates),
                        "candidates": [],
                    }
        return {
            "state": (
                "RECOVERY_CANDIDATES_IDENTIFIED"
                if candidates
                else "RECOVERY_LEASE_MISSING"
            ),
            "candidate_count": len(candidates),
            "candidates": candidates,
        }

    def inspect_scan_unknown_write_context(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
    ) -> dict[str, Any]:
        """Return the server-owned scan selection and attempt timestamps.

        The lease selects the formal Run.  That Run's persisted Command then
        selects the compact preview binding, and the binding is independently
        matched to the completed preview Step before any business identities
        are returned to the recovery reader.
        """

        with self._orchestration.unit_of_work() as uow:
            runtime_context = (
                uow.automation_plugins.lock_unknown_write_recovery_context_row(
                    automation_id=automation_id,
                    generation=generation,
                    lease_id=lease_id,
                )
            )
            lease = runtime_context.get("lease")
            if not isinstance(lease, Mapping):
                raise ValueError("scan recovery lease context is invalid")
            run_id = str(lease.get("orchestration_run_id") or "").strip()
            run = uow.runs.get(run_id, for_update=False) if run_id else None
            if not isinstance(run, Mapping):
                raise ValueError("scan recovery run is unavailable")
            command_id = str(run.get("command_id") or "").strip()
            command = uow.commands.get(command_id, for_update=False) if command_id else None
            if not isinstance(command, Mapping):
                raise ValueError("scan recovery command is unavailable")
            parameters = command.get("parameters_json", command.get("parameters"))
            if not isinstance(parameters, Mapping):
                raise ValueError("scan recovery command parameters are invalid")
            execution_context = parameters.get("execution_context")
            preview_context = (
                execution_context.get(SCAN_PREVIEW_CONTEXT_KEY)
                if isinstance(execution_context, Mapping)
                else None
            )
            if not isinstance(preview_context, Mapping):
                raise ValueError("scan recovery preview binding is unavailable")
            preview_run_id = normalize_preview_run_id(
                preview_context.get("preview_run_id")
            )
            preview = scan_preview_recovery_projection(
                uow,
                preview_run_id=preview_run_id,
                trusted_context=preview_context,
                now=datetime.now(timezone.utc),
            )
            steps = uow.steps.list_for_run(run_id)
            if len(steps) != 1 or not isinstance(steps[0], Mapping):
                raise ValueError("scan recovery step identity is invalid")
            step = steps[0]

        started_at = next(
            (
                value
                for value in (
                    step.get("started_at"),
                    step.get("created_at"),
                    run.get("started_at"),
                    run.get("created_at"),
                    command.get("requested_at"),
                )
                if isinstance(value, datetime)
            ),
            None,
        )
        finished_at = next(
            (
                value
                for value in (
                    step.get("completed_at"),
                    step.get("updated_at"),
                    run.get("completed_at"),
                    run.get("updated_at"),
                )
                if isinstance(value, datetime)
            ),
            started_at,
        )
        if not isinstance(started_at, datetime) or not isinstance(finished_at, datetime):
            raise ValueError("scan recovery attempt timestamps are unavailable")
        return {
            "state": "SCAN_RECOVERY_CONTEXT_IDENTIFIED",
            "run_id": run_id,
            "target_date": str(preview["target_date"]),
            "items": [dict(item) for item in preview["items"]],
            "attempt_started_at": started_at,
            "attempt_finished_at": finished_at,
        }

    def _reconciliation_automation_ids(self) -> tuple[str, ...]:
        """Discover project identities without compiling every catalog entry.

        The production repository is the identity authority. Reading the list
        is a platform operation and therefore remains fail-fast. Individual
        manifest/configuration compilation happens later inside the project
        boundary, so one invalid candidate cannot prevent another project from
        being reconciled.
        """

        persisted_id_reader = getattr(
            self._catalog,
            "persisted_automation_ids",
            None,
        )
        if callable(persisted_id_reader):
            return tuple(persisted_id_reader())

        repository = getattr(self, "_catalog_repository", None)
        if repository is None:
            repository = getattr(self._catalog, "_repository", None)
        if repository is None:
            # Small adapters used outside production may expose only the public
            # catalog API. Discovery errors here remain global because no
            # authoritative project identity is available to quarantine.
            raw_ids = [str(entry.automation_id or "").strip() for entry in self._catalog.list()]
        else:
            raw_id_reader = getattr(repository, "list_instance_ids", None)
            if callable(raw_id_reader):
                raw_ids = [str(automation_id or "").strip() for automation_id in raw_id_reader()]
            else:
                projects = tuple(repository.list_instances())
                raw_ids = [str(getattr(project, "automation_id", "") or "").strip() for project in projects]
            if any(not automation_id for automation_id in raw_ids) or len(set(raw_ids)) != len(raw_ids):
                raise PluginConflictError(
                    "automation project identities are missing or duplicated",
                    code="PLUGIN_IDENTITY_CONFLICT",
                )
            hidden_ids = frozenset(self._catalog.excluded_persisted_automation_ids())
            raw_ids = [automation_id for automation_id in raw_ids if automation_id not in hidden_ids]
        if any(not automation_id for automation_id in raw_ids) or len(set(raw_ids)) != len(raw_ids):
            raise PluginConflictError(
                "automation project identities are missing or duplicated",
                code="PLUGIN_IDENTITY_CONFLICT",
            )
        return tuple(sorted(raw_ids))

    def reconcile_all(self) -> tuple[object, ...]:
        automation_ids = self._reconciliation_automation_ids()
        results: dict[str, object] = {}
        failures: dict[str, dict[str, str]] = {}
        signature_reader = getattr(
            getattr(self, "_reconciler", None),
            "projection_signature",
            None,
        )
        # Provider activation may make a previously visited consumer ready.
        # Iterate to a stable process registry instead of depending on project
        # name ordering; the bounded pass count covers every acyclic chain.
        for _pass in range(max(1, len(automation_ids) + 1)):
            before = signature_reader() if callable(signature_reader) else None
            for automation_id in automation_ids:
                try:
                    result = self.reconcile_project(automation_id)
                except AutomationPluginError as exc:
                    if exc.code == "PLUGIN_IDENTITY_CONFLICT":
                        raise
                    failures[automation_id] = {
                        "code": str(getattr(exc, "code", "PLUGIN_RUNTIME_RECONCILE_FAILED")),
                        "error_summary": redact_text(exc)[:300],
                    }
                    continue
                except ValueError as exc:
                    failures[automation_id] = {
                        "code": "PLUGIN_PROJECT_DATA_INVALID",
                        "error_summary": redact_text(exc)[:300],
                    }
                    continue
                except ConcurrentUpdateError as exc:
                    # A stale prepared generation belongs to one automation
                    # project.  Quarantine it for an explicit retry instead of
                    # preventing every healthy project and core tool from
                    # starting.
                    failures[automation_id] = {
                        "code": "PLUGIN_PROJECT_CONCURRENT_UPDATE",
                        "error_summary": redact_text(exc)[:300],
                    }
                    continue
                failures.pop(automation_id, None)
                if result is not None:
                    results[automation_id] = result
            after = signature_reader() if callable(signature_reader) else None
            if before is None or after == before:
                break
        else:
            raise PluginConflictError(
                "service dependency projection did not converge",
                code="SERVICE_DEPENDENCY_RECONCILE_DID_NOT_CONVERGE",
            )
        self._last_reconcile_failures = failures
        return tuple(results[key] for key in sorted(results))

    def reconciliation_failures(self) -> dict[str, dict[str, str]]:
        """Return the latest per-project reconciliation warnings.

        Catalog discovery itself still fails as one unit because duplicate global
        identities cannot be routed safely.  Once identities are known, however,
        a malformed candidate belongs to that project only and must not stop the
        remaining committed projects or core tools from starting.
        """

        return copy.deepcopy(getattr(self, "_last_reconcile_failures", {}))


class ProductionServiceV2ProviderExecutor:
    """Execute the exact project generation selected by ServiceRegistry."""

    def __init__(
        self,
        *,
        service_registry: ServiceRegistry,
        generation_repository: Any,
    ) -> None:
        self._services = service_registry
        if not callable(getattr(generation_repository, "get_generation", None)):
            raise TypeError("service Provider executor requires a generation repository")
        self._generations = generation_repository
        self._router: PluginExecutionRouter | None = None

    def bind_router(self, router: PluginExecutionRouter) -> None:
        if self._router is not None and self._router is not router:
            raise RuntimeError("service Provider executor is already bound")
        self._router = router

    def _routable_capability(
        self,
        provider: ResolvedServiceOperation,
    ) -> Mapping[str, Any]:
        if provider.runtime_mode != "on_demand":
            raise PluginExecutionError(
                "resident Service v2 Providers are not supported by this runtime",
                code="RESIDENT_RUNTIME_UNAVAILABLE",
            )
        automation_id = provider.project_automation_id
        generation_number = provider.project_generation
        reference = self._services.project_reference(
            automation_id=automation_id,
            generation=generation_number,
        )
        if reference is None or not reference.accepts_new_calls:
            raise PluginExecutionError(
                "service Provider project generation no longer accepts new calls",
                code="SERVICE_PROVIDER_BLOCKED",
            )
        if (
            reference.provider_automation_id != provider.provider_registration_id
            or reference.package_sha256 != provider.package_sha256
            or reference.manifest_sha256 != provider.manifest_sha256
        ):
            raise PluginExecutionError(
                "service Provider route changed package identity",
                code="SERVICE_PROVIDER_ROUTE_INVALID",
            )
        generation = self._generations.get_generation(
            automation_id,
            generation_number,
        )
        if (
            generation is None
            or generation.state is not RuntimeGenerationState.COMMITTED
            or generation.activation_phase
            in {
                RuntimeActivationPhase.PENDING_PROJECTION,
                RuntimeActivationPhase.BLOCKED,
                RuntimeActivationPhase.ROLLED_BACK,
            }
        ):
            raise PluginExecutionError(
                "service Provider project generation is not activation-ready",
                code="SERVICE_PROVIDER_BLOCKED",
            )
        snapshot = generation.snapshot
        if (
            snapshot.automation_id != automation_id
            or snapshot.generation != generation_number
            or snapshot.runtime_model is not PluginRuntimeModel.SERVICE_V2
            or snapshot.plugin_id != provider.plugin_id
            or snapshot.plugin_version != provider.plugin_version
            or snapshot.package_sha256 != provider.package_sha256
            or snapshot.manifest_sha256 != provider.manifest_sha256
        ):
            raise PluginExecutionError(
                "service Provider generation changed package identity",
                code="SERVICE_PROVIDER_ROUTE_INVALID",
            )
        capability = project_capability_from_snapshot(snapshot)
        metadata = capability.get("_plugin_runtime")
        if not isinstance(metadata, Mapping):
            raise PluginExecutionError(
                "service Provider project route has no runtime metadata",
                code="SERVICE_PROVIDER_ROUTE_INVALID",
            )
        return capability

    async def __call__(
        self,
        *,
        provider: ResolvedServiceOperation,
        caller_automation_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        call_chain: tuple[str, ...],
    ) -> Mapping[str, Any]:
        del caller_automation_id
        router = self._router
        if router is None:
            raise PluginExecutionError(
                "service Provider runtime is not bound",
                code="SERVICE_PROVIDER_BLOCKED",
            )
        if operation != provider.operation:
            raise PluginExecutionError(
                "resolved service operation changed before execution",
                code="SERVICE_PROVIDER_ROUTE_INVALID",
            )
        capability = self._routable_capability(provider)
        return await router.execute_service_operation(
            capability,
            arguments,
            service=provider.service,
            operation=operation,
            effect=provider.effect,
            call_chain=call_chain,
        )


@dataclass
class ProductionAutomationPluginRuntime:
    release: ProductionPluginReleaseConfig
    bootstrap: BootstrapResult
    repository: MySQLAutomationPluginRepositoryAdapter
    runtime_repository: MySQLAutomationPluginRuntimeAdapter
    catalog: PluginCatalog
    composite_catalog: CompositeToolRegistry
    issuer: LocalBrokerCapabilityIssuer
    broker: LocalCoreAutomationBroker
    execution_router: PluginExecutionRouter
    migration_runtime: PluginMigrationRuntimeCoordinator
    migration_entrypoint_ownership: MigrationEntrypointOwnershipResolver
    target_service: MySQLRuntimeTargetService
    storage: FilesystemPluginStorage
    environments: LockedVirtualEnvironmentBuilder
    upload_signature_verifier: Any
    management_repository: MySQLAutomationPluginManagementRepository
    binding_resolver: ProductionProjectBindingResolver
    service_registry: ServiceRegistry
    connector_registry: ConnectorRegistry
    contribution_registry: ManagedContributionRegistry
    contribution_backend_availability: RuntimeContributionBackendAvailability
    service_effect_driver: ProductionRuntimeEffectDriver
    lifecycle_service: AutomationPluginService
    configuration_service: AutomationProjectConfigurationService
    management: AutomationPluginManagementService
    required_first_party_ids: frozenset[str]
    _bootstrap_first_party: Callable[[], BootstrapResult]
    _sandbox_canary: SandboxCanaryResult | None = None
    _health_snapshot: dict[str, Any] | None = None
    _started: bool = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        await self.broker.start()
        self._started = True
        self._sandbox_canary = await self.execution_router.startup_sandbox_canary()

    async def stop(self) -> None:
        if not self._started:
            return
        await self.broker.stop()
        self._started = False

    def reconcile(self) -> tuple[object, ...]:
        if not self.bootstrap.ok:
            raise PluginConflictError("first-party plugin bootstrap was rejected")
        service_driver = getattr(self, "service_effect_driver", None)
        if isinstance(service_driver, ProductionRuntimeEffectDriver):
            service_driver.restore_from_repository(self.runtime_repository)
        first_pass = self.target_service.reconcile_all()

        # A release bootstrap deliberately does not replace an in-flight
        # generation.  Finish that exact signed generation first, then let the
        # same release bootstrap observe the newly stable lineage and stage
        # any newer signed package before a second reconciliation pass.  This
        # closes the normal recovery-and-upgrade path in one Agent startup
        # without inventing a generation or relying on an operator restart.
        # A target still waiting on a real coeffect remains unavailable and is
        # retried by the next explicit reconciliation after that dependency is
        # restored.
        refreshed = self._bootstrap_first_party()
        if not refreshed.ok:
            raise PluginConflictError("first-party plugin bootstrap was rejected")
        self.bootstrap = refreshed
        second_pass = self.target_service.reconcile_all()
        return first_pass + second_pass

    def reconcile_after_dependency_change(
        self,
        *,
        account_id: str | None = None,
        service: str | None = None,
    ) -> tuple[object, ...]:
        """Retry every affected v2 target after a dependency becomes ready.

        Account-session restoration and ServiceRegistry provider activation are
        process-local events, while the waiting generation is durable.  The
        target service is the authority that re-observes all coeffects and
        retries only projects whose target still needs work; passing the
        optional event identity is intentionally informational and never used
        to derive a binding or choose a project.
        """

        del account_id, service
        if not self.bootstrap.ok:
            raise PluginConflictError("first-party plugin bootstrap was rejected")
        return tuple(self.target_service.reconcile_all())

    def health(self) -> dict[str, Any]:
        catalog = self.catalog.production_health(tuple(self.required_first_party_ids))
        ignored_automation_ids = self.catalog.excluded_persisted_automation_ids()
        generations: RuntimeGenerationHealth = runtime_generation_health(
            self.runtime_repository,
            expected_automation_ids=self.required_first_party_ids,
            ignored_automation_ids=ignored_automation_ids,
        )
        sandbox = self._sandbox_canary
        sandbox_ready = bool(sandbox is not None and sandbox.healthy)
        target_service = getattr(self, "target_service", None)
        failure_reader = getattr(target_service, "reconciliation_failures", None)
        reconciliation_errors = failure_reader() if callable(failure_reader) else {}
        runnable = bool(catalog.get("runnable") is True and self._started and sandbox_ready)
        payload = {
            "ok": bool(catalog.get("ok") is True and self._started and sandbox_ready),
            "runnable": runnable,
            "runtime_status": "READY" if runnable else "UNAVAILABLE",
            "release_sha": self.release.verified_release_sha,
            "broker": {"state": "running" if self._started else "stopped"},
            "sandbox": {
                "state": "ready" if sandbox_ready else "unavailable",
                "code": sandbox.code if sandbox is not None else "NOT_CHECKED",
                "checked_at": (sandbox.checked_at.isoformat() if sandbox is not None else None),
            },
            "catalog": catalog,
            "generations": {
                "healthy": generations.healthy,
                "project_count": generations.project_count,
                "committed_count": generations.committed_count,
                "active_lease_count": generations.active_lease_count,
                "archival_unknown_generation_count": (getattr(generations, "archival_unknown_generation_count", 0)),
                "blocked_projects": {key: list(value) for key, value in sorted(generations.blocked_projects.items())},
            },
            "reconciliation_errors": reconciliation_errors,
        }
        self._health_snapshot = copy.deepcopy(payload)
        return payload

    def health_snapshot(self) -> dict[str, Any]:
        """Return the last complete live health projection without database I/O."""

        snapshot = copy.deepcopy(self._health_snapshot)
        if not isinstance(snapshot, dict):
            return {
                "ok": False,
                "runnable": False,
                "runtime_status": "UNAVAILABLE",
                "release_sha": self.release.verified_release_sha,
                "broker": {"state": "running" if self._started else "stopped"},
                "sandbox": {"state": "unavailable", "code": "NOT_CHECKED", "checked_at": None},
                "catalog": {"ok": False, "runnable": False, "runtime_status": "UNAVAILABLE"},
                "generations": {"healthy": False, "blocked_projects": {}},
                "reconciliation_errors": {},
                "error_code": "AUTOMATION_PLUGIN_HEALTH_NOT_CHECKED",
            }
        if not self._started:
            snapshot["ok"] = False
            snapshot["runnable"] = False
            snapshot["runtime_status"] = "UNAVAILABLE"
            snapshot["broker"] = {"state": "stopped"}
        return snapshot

    def assert_release_ready(self) -> dict[str, Any]:
        health = self.health()
        if health["runnable"] is not True:
            raise PluginConflictError(
                "automation plugin runtime is not release-ready",
                code="AUTOMATION_PLUGIN_RUNTIME_NOT_READY",
            )
        return health

    def package_bytes(
        self,
        plugin_id: str,
        version: str,
        *,
        expected_sha256: str,
    ) -> bytes:
        """Return exact signed archive bytes for an authenticated Worker route."""

        return self.management.package_bytes(
            plugin_id,
            version,
            expected_sha256=expected_sha256,
        )


def _cursor_secret(environ: Mapping[str, str]) -> bytes:
    value = str(environ.get(CURSOR_SECRET_ENV) or "")
    encoded = value.encode("utf-8")
    if len(encoded) < 32 or len(encoded) > 4096:
        raise PluginPackageError(f"{CURSOR_SECRET_ENV} must contain from 32 to 4096 UTF-8 bytes")
    return encoded


def _migration_account_bindings(
    *,
    core_catalog: Any,
    account_manager: Any,
) -> dict[str, Mapping[str, Any]]:
    manifests = resolve_release_first_party_manifests(core_catalog)
    result: dict[str, Mapping[str, Any]] = {}
    finance = manifests.get("sync_finance_bills")
    if finance is not None:
        if len(finance.account_roles) != len(enabled_finance_account_ids()):
            raise PluginPackageError("finance first-party role contract is incomplete")
        finance_bindings = {
            str(role["role"]): account_id
            for role, account_id in zip(
                finance.account_roles,
                enabled_finance_account_ids(),
                strict=True,
            )
        }
        result["finance_bills"] = finance_bindings
        result["finance_startup_catchup"] = finance_bindings

    customer = manifests.get("sync_customer_service_problems")
    if customer is not None:
        if len(customer.account_roles) != 1:
            raise PluginPackageError("customer first-party role contract is incomplete")
        role = customer.account_roles[0]
        allowed = set(str(item) for item in role.get("allowed_systems", ()))
        accounts = tuple(
            sorted(
                str(item.get("account_id") or "")
                for item in account_manager.list_accounts(
                    include_status=False,
                    validate=False,
                )
                if isinstance(item, Mapping)
                and item.get("is_active") is True
                and str(item.get("system") or "") in allowed
                and str(item.get("account_id") or "")
            )
        )
        if not accounts:
            raise PluginPackageError("customer first-party migration has no active account binding")
        result["customer_problems_shadow"] = {str(role["role"]): accounts}
    return result


def build_production_automation_plugin_runtime(
    *,
    orchestration_repository: Any,
    core_catalog: Any,
    core_executor: Any,
    account_manager: Any,
    broker_handlers: Mapping[tuple[str, str], Any],
    runtime_release_sha: str,
    environ: Mapping[str, str] | None = None,
    release_hold_provider: Callable[[], bool] | None = None,
    bubblewrap_path: Path | str = Path("/usr/bin/bwrap"),
    resource_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
    reserved_feishu_command: Callable[[str], bool] | None = None,
) -> ProductionAutomationPluginRuntime:
    """Build the production graph without starting its local broker socket."""

    environment = os.environ if environ is None else environ
    release = load_production_plugin_release_config(
        environment,
        runtime_release_sha=runtime_release_sha,
    )
    if release.artifact_root.parent.name != "releases":
        raise PluginPackageError("plugin artifact root must be inside the releases directory")
    base = release.artifact_root.parent.parent.resolve()
    storage_root = (base / "installed").resolve()
    runtime_root = (base / "runtime").resolve()
    if storage_root in release.artifact_root.parents or release.artifact_root in storage_root.parents:
        raise PluginPackageError("plugin storage and release artifacts must be independent")
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    storage = FilesystemPluginStorage(storage_root)
    environments = LockedVirtualEnvironmentBuilder()
    trust = load_ed25519_trust_store(release.trust_root)
    provider = SignedFirstPartyPackageProvider(
        artifact_root=release.artifact_root,
        signature_verifier=trust,
        storage=storage,
        environments=environments,
    )
    repository = MySQLAutomationPluginRepositoryAdapter(
        orchestration_repository,
        migration_account_bindings=_migration_account_bindings(
            core_catalog=core_catalog,
            account_manager=account_manager,
        ),
        release_hold_provider=release_hold_provider,
    )

    def _bootstrap_first_party() -> BootstrapResult:
        return bootstrap_first_party_plugins(
            repository,
            core_catalog=core_catalog,
            current_release_sha=runtime_release_sha,
            expected_release_sha=release.verified_release_sha,
            package_provider=provider,
            package_materializer=provider,
        )

    bootstrap = _bootstrap_first_party()
    if not bootstrap.ok:
        raise PluginPackageError(
            "signed first-party bootstrap failed: " + ", ".join(sorted(bootstrap.rejected.values()))
        )
    catalog_repository = MySQLAutomationPluginCatalogRepositoryAdapter(orchestration_repository)
    config_repository = MySQLAutomationProjectConfigurationReadAdapter(orchestration_repository)
    catalog_account_resolver = AccountManagerSessionResolver(account_manager)
    connector_registry = ConnectorRegistry()
    contribution_backend_availability = RuntimeContributionBackendAvailability()

    def _effective_contribution_backend(
        *,
        contribution_kind: str,
        declaration: Mapping[str, Any],
        project_schedule: Mapping[str, Any],
    ) -> tuple[str, str, str | None, str | None]:
        structural_status = _contribution_backend(
            contribution_kind=contribution_kind,
            declaration=declaration,
            project_schedule=project_schedule,
        )
        return contribution_backend_availability.effective_status(
            contribution_kind=contribution_kind,
            structural_status=structural_status,
        )

    catalog = PluginCatalog(
        catalog_repository,
        config_repository,
        excluded_automation_plugins=deferred_first_party_automation_plugins(),
        excluded_plugin_ids=deferred_first_party_plugin_ids(),
        allowed_execution_platforms=("server",),
        migration_pair_provider=(repository.get_active_plugin_migration_pair_for_automation),
        account_binding_ready=lambda account_id, allowed_systems: bool(
            catalog_account_resolver.require_active_binding_descriptor(
                account_id=account_id,
                allowed_systems=allowed_systems,
            )
        ),
        contribution_backend_status=_effective_contribution_backend,
        connector_registry=connector_registry,
    )
    runtime_repository = MySQLAutomationPluginRuntimeAdapter(orchestration_repository)
    management_repository = MySQLAutomationPluginManagementRepository(orchestration_repository)
    if resource_provider is None:
        from agent.workflow_resource_store import get_workflow_resource

        resource_provider = get_workflow_resource
    from agent.workflow_resource_store import list_workflow_resource_descriptors

    binding_resolver = ProductionProjectBindingResolver(
        account_manager=account_manager,
        resource_provider=resource_provider,
        worker_repository=management_repository,
    )
    release_handler_keys = release_first_party_broker_action_keys(core_catalog)
    missing_handler_keys = release_handler_keys - set(broker_handlers)
    if missing_handler_keys:
        raise PluginPackageError(
            "release-scoped first-party Broker handlers are incomplete: "
            + ", ".join(f"{operation}/{action}" for operation, action in sorted(missing_handler_keys))
        )
    scoped_broker_handlers = {key: handler for key, handler in broker_handlers.items() if key in release_handler_keys}
    runtime_projection_lock = RLock()
    migration_entrypoint_ownership = MigrationEntrypointOwnershipResolver(
        repository
    )
    service_registry = ServiceRegistry(
        lock=runtime_projection_lock,
        connector_registry=connector_registry,
    )
    contribution_registry = ManagedContributionRegistry(
        lock=runtime_projection_lock,
        reserved_feishu_command=reserved_feishu_command,
        migration_reserved_feishu_target=(
            migration_entrypoint_ownership.allow_reserved_feishu_target
        ),
        backend_availability=contribution_backend_availability,
    )
    service_executor = ProductionServiceV2ProviderExecutor(
        service_registry=service_registry,
        generation_repository=runtime_repository,
    )
    platform_v2_handlers = build_service_v2_capability_handler_map(
        orchestration_repository,
        reviewed_handlers=scoped_broker_handlers,
        service_registry=service_registry,
        service_executor=service_executor,
        connector_registry=connector_registry,
    )
    conflicting_platform_handlers = set(scoped_broker_handlers) & set(platform_v2_handlers)
    if conflicting_platform_handlers:
        raise PluginPackageError(
            "service-v2 platform Broker handlers conflict with release handlers: "
            + ", ".join(f"{operation}/{action}" for operation, action in sorted(conflicting_platform_handlers))
        )
    scoped_broker_handlers.update(platform_v2_handlers)
    unavailable_handler_keys = set(UNAVAILABLE_SERVICE_V2_HANDLER_KEYS)
    unavailable_handler_keys.discard(SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY)
    handler_keys = tuple(sorted(set(scoped_broker_handlers) - unavailable_handler_keys))
    coeffects = ProductionRuntimeCoeffectProvider(
        core_catalog=core_catalog,
        broker_handler_keys=handler_keys,
        account_manager=account_manager,
        binding_resolver=binding_resolver,
        service_registry=service_registry,
        connector_registry=connector_registry,
    )
    planner = ProductionRuntimeEffectPlanner()
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=handler_keys,
        service_registry=service_registry,
        contribution_registry=contribution_registry,
        projection_lock=runtime_projection_lock,
    )
    reconciler = AutomationRuntimeReconciler(
        repository=runtime_repository,
        coeffects=coeffects,
        planner=planner,
        driver=driver,
    )
    target_service = MySQLRuntimeTargetService(
        orchestration_repository=orchestration_repository,
        catalog=catalog,
        core_catalog=core_catalog,
        runtime_repository=runtime_repository,
        reconciler=reconciler,
        catalog_repository=repository,
    )
    lifecycle_service = AutomationPluginService(
        repository=repository,
        storage=storage,
        environments=environments,
        upload_signature_verifier=trust,
        allowed_execution_platforms=("server",),
        blocked_plugin_ids=deferred_first_party_plugin_ids(),
    )
    configuration_service = AutomationProjectConfigurationService(
        catalog=catalog,
        repository=management_repository,
        binding_resolver=binding_resolver,
    )
    management = AutomationPluginManagementService(
        catalog=catalog,
        lifecycle=lifecycle_service,
        configuration=configuration_service,
        worker_repository=management_repository,
        target_service=target_service,
        package_repository=repository,
        storage=storage,
        release_hold_provider=release_hold_provider,
        resource_catalog_provider=list_workflow_resource_descriptors,
        contribution_registry=contribution_registry,
    )
    socket_path = runtime_root / f"agent-{os.getpid()}.sock"
    issuer = LocalBrokerCapabilityIssuer(
        socket_path,
        write_attempt_recorder=runtime_repository.record_write_attempt,
    )
    core_adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers=scoped_broker_handlers,
        account_resolver=catalog_account_resolver,
        resource_resolver=binding_resolver,
        connector_registry=connector_registry,
    )
    broker = LocalCoreAutomationBroker(issuer=issuer, adapter=core_adapter)
    migration_runtime = PluginMigrationRuntimeCoordinator(repository)
    execution_router = PluginExecutionRouter(
        core_executor=core_executor,
        capability_issuer=issuer,
        sandbox_launcher=BubblewrapPluginSandbox(bubblewrap_path),
        generation_leases=runtime_repository,
        migration_runtime=migration_runtime,
        release_hold_provider=release_hold_provider,
    )
    service_executor.bind_router(execution_router)
    # Read the secret during graph construction so a missing stable identity
    # fails startup before the broker accepts any action.  Handler factories
    # receive the same value from the composition root.
    _cursor_secret(environment)
    return ProductionAutomationPluginRuntime(
        release=release,
        bootstrap=bootstrap,
        repository=repository,
        runtime_repository=runtime_repository,
        catalog=catalog,
        composite_catalog=CompositeToolRegistry(
            core_catalog,
            catalog,
            blocked_core_tool_names=deferred_first_party_plugin_ids(),
        ),
        issuer=issuer,
        broker=broker,
        execution_router=execution_router,
        migration_runtime=migration_runtime,
        migration_entrypoint_ownership=migration_entrypoint_ownership,
        target_service=target_service,
        storage=storage,
        environments=environments,
        upload_signature_verifier=trust,
        management_repository=management_repository,
        binding_resolver=binding_resolver,
        service_registry=service_registry,
        connector_registry=connector_registry,
        contribution_registry=contribution_registry,
        contribution_backend_availability=contribution_backend_availability,
        service_effect_driver=driver,
        lifecycle_service=lifecycle_service,
        configuration_service=configuration_service,
        management=management,
        required_first_party_ids=release_first_party_automation_ids(),
        _bootstrap_first_party=_bootstrap_first_party,
    )


def production_cursor_secret(environ: Mapping[str, str] | None = None) -> bytes:
    """Return the stable cursor key without ever logging or persisting it."""

    return _cursor_secret(os.environ if environ is None else environ)


__all__ = [
    "CURSOR_SECRET_ENV",
    "ManagedContributionRegistration",
    "ManagedContributionRegistry",
    "MySQLRuntimeTargetService",
    "ProductionAutomationPluginRuntime",
    "ProductionServiceV2ProviderExecutor",
    "ProductionRuntimeCoeffectProvider",
    "ProductionRuntimeEffectDriver",
    "ProductionRuntimeEffectPlanner",
    "build_production_automation_plugin_runtime",
    "build_runtime_generation_snapshot",
    "production_cursor_secret",
]
