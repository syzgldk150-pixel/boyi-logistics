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
from typing import Any, Callable, Mapping, Sequence

from agent.automation_plugins.broker import (
    LocalBrokerCapabilityIssuer,
    LocalCoreAutomationBroker,
)
from agent.automation_plugins.binding_resolver import (
    ProductionProjectBindingResolver,
)
from agent.automation_plugins.catalog import (
    CompositeToolRegistry,
    PluginCatalog,
    PluginCatalogEntry,
)
from agent.automation_plugins.core_adapter import (
    AccountManagerSessionResolver,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.errors import PluginConflictError, PluginPackageError
from agent.automation_plugins.configuration import (
    AutomationProjectConfigurationService,
)
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
    runtime_generation_health,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.management_repository import (
    MySQLAutomationPluginManagementRepository,
)
from agent.automation_plugins.models import (
    BootstrapResult,
    PluginTrustSource,
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
from agent.automation_plugins.release_config import (
    ProductionPluginReleaseConfig,
    load_production_plugin_release_config,
)
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter,
    MySQLAutomationPluginRuntimeAdapter,
    MySQLAutomationProjectConfigurationReadAdapter,
)
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox
from agent.automation_plugins.storage import (
    FilesystemPluginStorage,
    LockedVirtualEnvironmentBuilder,
)
from shared.finance.sources import enabled_finance_account_ids
from shared.redaction import redact_text


CURSOR_SECRET_ENV = "BOYI_AUTOMATION_PLUGIN_CURSOR_SECRET"
_POLICY_PROJECTION_FIELDS = (
    "automation_id",
    "project_generation",
    "mode",
    "project_configuration_version",
    "version",
)
_CONFIG_JSON_FIELDS = (
    "config_json",
    "account_bindings_json",
    "resource_bindings_json",
    "enabled_entrypoints_json",
    "desired_schedule_json",
    "compiled_invocations_json",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _required_sha(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PluginConflictError(f"persisted {field} is not a SHA-256 digest")
    return text


def _required_policy_generation(policy: Mapping[str, Any]) -> int:
    value = policy.get("project_generation")
    if type(value) is not int or value <= 0:
        raise PluginConflictError(
            "project policy is not bound to the desired runtime generation",
            code="PLUGIN_POLICY_GENERATION_MISMATCH",
        )
    return value


def _closed_policy_projection(
    policy: Mapping[str, Any],
    *,
    automation_id: str,
    generation: int,
    project_configuration_version: int,
) -> dict[str, Any]:
    projection = {field: policy.get(field) for field in _POLICY_PROJECTION_FIELDS}
    policy_generation = _required_policy_generation(policy)
    if (
        str(projection["automation_id"] or "") != automation_id
        or policy_generation != generation
        or not str(projection["mode"] or "")
        or type(projection["project_configuration_version"]) is not int
        or int(projection["project_configuration_version"])
        != project_configuration_version
        or type(projection["version"]) is not int
        or int(projection["version"]) <= 0
    ):
        raise PluginConflictError(
            "project policy is not bound to the desired runtime generation",
            code="PLUGIN_POLICY_GENERATION_MISMATCH",
        )
    return projection


def _closed_config_row(
    row: Mapping[str, Any],
    *,
    automation_id: str,
) -> dict[str, Any]:
    if str(row.get("automation_id") or "") != automation_id:
        raise PluginConflictError("project configuration identity changed")
    result: dict[str, Any] = {}
    for field in _CONFIG_JSON_FIELDS:
        value = row.get(field)
        expected_type = list if field == "enabled_entrypoints_json" else Mapping
        if not isinstance(value, expected_type):
            raise PluginConflictError(f"project configuration field is invalid: {field}")
        result[field] = copy.deepcopy(list(value) if isinstance(value, list) else dict(value))
    version = row.get("config_version")
    if type(version) is not int or version <= 0 or row.get("configured") not in {True, 1}:
        raise PluginConflictError(
            "project configuration is incomplete",
            code="PLUGIN_PROJECT_NOT_CONFIGURED",
        )
    result["config_version"] = version
    result["device_id"] = str(row.get("device_id") or "").strip() or None
    hash_pairs = (
        ("config_json", "config_sha256"),
        ("account_bindings_json", "account_bindings_sha256"),
        ("resource_bindings_json", "resource_bindings_sha256"),
        ("enabled_entrypoints_json", "enabled_entrypoints_sha256"),
        ("desired_schedule_json", "desired_schedule_sha256"),
        ("compiled_invocations_json", "compiled_invocations_sha256"),
    )
    for value_field, hash_field in hash_pairs:
        persisted = _required_sha(row.get(hash_field), hash_field)
        if _digest(result[value_field]) != persisted:
            raise PluginConflictError(
                f"project configuration digest changed: {value_field}"
            )
        result[hash_field] = persisted
    result["device_binding_sha256"] = _required_sha(
        row.get("device_binding_sha256"),
        "device_binding_sha256",
    )
    return result


def build_runtime_generation_snapshot(
    entry: PluginCatalogEntry,
    *,
    desired_config_row: Mapping[str, Any],
    policy_row: Mapping[str, Any],
    generation: int,
    core_catalog: Any,
    created_at: datetime | None = None,
) -> RuntimeGenerationSnapshot:
    """Compile one closed non-secret generation from desired core-owned state."""

    if type(generation) is not int or generation <= 0:
        raise PluginConflictError("runtime generation must be a positive integer")
    if entry.trust_source not in {
        PluginTrustSource.ED25519_FIRST_PARTY.value,
        PluginTrustSource.ED25519_UPLOAD.value,
    }:
        raise PluginConflictError(
            "production generations require an Ed25519 plugin package",
            code="PLUGIN_TRUST_SOURCE_INVALID",
        )
    if entry.runtime.get("kind") != "python_subprocess":
        raise PluginConflictError(
            "production generations require a subprocess action payload",
            code="PLUGIN_RUNTIME_FORBIDDEN",
        )
    desired = _closed_config_row(
        desired_config_row,
        automation_id=entry.automation_id,
    )
    entrypoints = tuple(str(item) for item in desired["enabled_entrypoints_json"])
    if (
        not entrypoints
        or len(entrypoints) != len(set(entrypoints))
        or not set(entrypoints) <= set(entry.allowed_entrypoints)
        or set(desired["compiled_invocations_json"]) != set(entrypoints)
    ):
        raise PluginConflictError("desired entrypoint route is not closed")
    if desired["device_id"] is not None:
        # Worker snapshots require the exact paired key fingerprint and
        # capability revision.  A bare mutable device_id is not enough.
        raise PluginConflictError(
            "worker generation needs a closed immutable device descriptor",
            code="PLUGIN_DEVICE_SNAPSHOT_UNAVAILABLE",
        )
    if desired["device_binding_sha256"] != _digest(None):
        raise PluginConflictError("server plugin device binding digest is invalid")

    anchor = copy.deepcopy(dict(entry.governance_anchor))
    core_capability = core_catalog.get_capability(str(anchor.get("name") or ""))
    if not isinstance(core_capability, Mapping):
        raise PluginConflictError("governed core capability disappeared")
    if any(
        key not in core_capability
        or canonical_json_bytes(core_capability[key]) != canonical_json_bytes(value)
        for key, value in anchor.items()
    ):
        raise PluginConflictError(
            "governed core capability changed beneath the signed action",
            code="PLUGIN_GOVERNANCE_ANCHOR_MISMATCH",
        )
    if _digest(anchor) != entry.governance_anchor_sha256:
        raise PluginConflictError("signed governance anchor digest is invalid")

    project_config = desired["config_json"]
    account_bindings = desired["account_bindings_json"]
    resource_bindings = desired["resource_bindings_json"]
    schedule = desired["desired_schedule_json"]
    compiled_invocations = desired["compiled_invocations_json"]
    declared_resource_roles = {
        str(item.get("role") or ""): item
        for item in entry.resource_roles
        if isinstance(item, Mapping)
    }
    if (
        "" in declared_resource_roles
        or not set(resource_bindings) <= set(declared_resource_roles)
        or any(
            role.get("required") is True and role_name not in resource_bindings
            for role_name, role in declared_resource_roles.items()
        )
    ):
        raise PluginConflictError(
            "desired managed-resource bindings are incomplete",
            code="PLUGIN_RESOURCE_BINDING_INVALID",
        )
    if "webhook" in entrypoints and (
        "webhook_route" not in declared_resource_roles
        or "webhook_route" not in resource_bindings
    ):
        raise PluginConflictError(
            "an enabled Webhook entrypoint requires an explicit route resource",
            code="PLUGIN_WEBHOOK_ROUTE_REQUIRED",
        )
    action_contract = copy.deepcopy(dict(entry.tool_contract))
    runtime_descriptor = {
        "install_metadata": {
            **copy.deepcopy(dict(entry.install_metadata)),
            "install_root": entry.install_root,
        },
        "runtime": copy.deepcopy(dict(entry.runtime)),
        "runtime_permissions": copy.deepcopy(dict(entry.runtime_permissions)),
        "account_roles": [copy.deepcopy(dict(item)) for item in entry.account_roles],
        "resource_roles": [copy.deepcopy(dict(item)) for item in entry.resource_roles],
    }
    if not entry.install_root or not runtime_descriptor["install_metadata"].get(
        "python_relative"
    ):
        raise PluginConflictError(
            "plugin version is not materialized",
            code="PLUGIN_NOT_MATERIALIZED",
        )
    policy = _closed_policy_projection(
        policy_row,
        automation_id=entry.automation_id,
        generation=generation,
        project_configuration_version=int(desired["config_version"]),
    )
    execution_metadata = {
        "project_config_version": int(desired["config_version"]),
        "project_config": project_config,
        "account_bindings": account_bindings,
        "resource_bindings": resource_bindings,
        "device_binding": None,
        "schedule": schedule,
        "compiled_invocations": compiled_invocations,
        "runtime_descriptor": runtime_descriptor,
        "action_contract": action_contract,
        "governance_anchor": anchor,
    }
    observed_at = created_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise PluginConflictError("runtime generation timestamp must be timezone-aware")
    return RuntimeGenerationSnapshot(
        automation_id=entry.automation_id,
        generation=generation,
        plugin_id=entry.plugin_id,
        plugin_version=entry.installed_version,
        package_sha256=_required_sha(entry.package_sha256, "package_sha256"),
        manifest_sha256=_required_sha(entry.manifest_sha256, "manifest_sha256"),
        trust_source=PluginTrustSource(entry.trust_source),
        project_config_sha256=_digest(project_config),
        account_bindings_sha256=_digest(account_bindings),
        resource_bindings_sha256=_digest(resource_bindings),
        device_binding_sha256=_digest(None),
        schedule_sha256=_digest(schedule),
        # The signed governance projection is the exact core registry slice
        # used by this action; unrelated tools do not invalidate a generation.
        core_registry_sha256=_digest(anchor),
        tool_contract_sha256=_digest(action_contract),
        invocation_contracts_sha256=_digest(dict(entry.invocation_contracts)),
        compiled_invocations_sha256=_digest(compiled_invocations),
        runtime_descriptor_sha256=_digest(runtime_descriptor),
        governance_anchor_sha256=_digest(anchor),
        policy_contract_sha256=_digest(policy),
        enabled_entrypoints=entrypoints,
        execution_metadata=execution_metadata,
        created_at=observed_at.astimezone(timezone.utc),
    )


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
    ) -> None:
        self._core_catalog = core_catalog
        self._handler_keys = frozenset((str(a), str(b)) for a, b in broker_handler_keys)
        self._account_manager = account_manager
        self._bindings = binding_resolver

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
            required_pairs.add(
                (str(operation.get("operation") or ""), str(operation.get("action") or ""))
            )
        core_capability = self._core_catalog.get_capability(str(anchor.get("name") or ""))
        core_ready = isinstance(core_capability, Mapping) and all(
            key in core_capability
            and canonical_json_bytes(core_capability[key]) == canonical_json_bytes(value)
            for key, value in anchor.items()
        )
        adapters_ready = bool(required_pairs) and required_pairs <= self._handler_keys
        results: list[RuntimeCoeffectSnapshot] = [
            self._record(
                RuntimeCoeffectKind.CORE_ADAPTER,
                "governance-and-broker",
                {
                    "governance_anchor_sha256": snapshot.governance_anchor_sha256,
                    "required_broker_operations": sorted(required_pairs),
                    "registered_broker_operations": sorted(
                        required_pairs & self._handler_keys
                    ),
                },
                ready=core_ready and adapters_ready,
                reason_code=(
                    None
                    if core_ready and adapters_ready
                    else (
                        "CORE_REGISTRY_MISMATCH"
                        if not core_ready
                        else "CORE_ADAPTER_ACTION_UNAVAILABLE"
                    )
                ),
            )
        ]
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
            str(item.get("role") or ""): item
            for item in resource_roles
            if isinstance(item, Mapping)
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
        if (
            "scheduler" in entrypoints
            and isinstance(schedule, Mapping)
            and schedule.get("kind") != "none"
        ):
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

    def __init__(self, *, broker_handler_keys: Sequence[tuple[str, str]]) -> None:
        self._integrity = FilesystemPluginIntegrityVerifier()
        self._handler_keys = frozenset((str(a), str(b)) for a, b in broker_handler_keys)

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
            permissions = snapshot.execution_metadata["runtime_descriptor"][
                "runtime_permissions"
            ]
            operations = permissions.get("broker_operations")
            required = {
                (str(item.get("operation") or ""), str(item.get("action") or ""))
                for item in operations
                if isinstance(item, Mapping)
            } if isinstance(operations, list) else set()
            if not required or not required <= self._handler_keys:
                raise PluginConflictError(
                    "generation broker scope is unavailable",
                    code="CORE_ADAPTER_ACTION_UNAVAILABLE",
                )
        return replace(
            effect,
            state=RuntimeEffectState.APPLIED,
            payload=dict(plan.payload),
        )

    def dispose(self, effect: RuntimeEffectRecord) -> None:
        if effect.reversible is not True:
            raise PluginConflictError(
                "non-reversible generation effect cannot be disposed",
                code="RUNTIME_EFFECT_NOT_REVERSIBLE",
            )


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
    ) -> None:
        self._orchestration = orchestration_repository
        self._catalog = catalog
        self._core_catalog = core_catalog
        self._runtime = runtime_repository
        self._reconciler = reconciler

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
            "runtime_descriptor_sha256",
            "governance_anchor_sha256",
            "enabled_entrypoints",
            "execution_metadata",
        )
        return all(getattr(current, field) == getattr(desired, field) for field in fields)

    def reconcile_project(self, automation_id: str) -> object:
        entry = self._catalog.require(automation_id)
        runtime = self._runtime.get_project_runtime(automation_id)
        generations = tuple(self._runtime.list_project_generations(automation_id))
        if (
            not entry.enabled
            and not entry.configured
            and runtime is None
            and not generations
        ):
            # A signed action may be installed before the administrator binds
            # accounts/configuration.  It has no executable generation and is
            # therefore a safe, non-blocking catalog state.
            return None
        by_number = {item.snapshot.generation: item for item in generations}
        if runtime is not None:
            target = by_number.get(runtime.target_generation)
            if target is not None and target.state in {
                RuntimeGenerationState.TARGET,
                RuntimeGenerationState.PREPARING,
                RuntimeGenerationState.WAITING_COEFFECTS,
                RuntimeGenerationState.PREPARED,
            }:
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
                return None

        config, policy = self._desired_rows(automation_id)
        next_generation = max(by_number, default=0) + 1
        if not generations and runtime is not None:
            next_generation = max(1, runtime.target_generation)
        if (
            runtime is not None
            and runtime.committed_generation is not None
            and runtime.target_generation == runtime.committed_generation
            and runtime.reconcile_state == RuntimeReconcileState.STABLE
        ):
            committed = by_number.get(runtime.committed_generation)
            if (
                committed is None
                or committed.state is not RuntimeGenerationState.COMMITTED
            ):
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
                if self._same_material(committed.snapshot, committed_candidate):
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
            expected_committed_generation=(
                runtime.committed_generation if runtime is not None else None
            ),
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

    def reconcile_all(self) -> tuple[object, ...]:
        results: list[object] = []
        for entry in sorted(self._catalog.list(), key=lambda item: item.automation_id):
            try:
                result = self.reconcile_project(entry.automation_id)
            except PluginConflictError:
                raise
            except Exception as exc:
                raise PluginConflictError(
                    f"runtime generation reconciliation failed: {redact_text(exc)[:300]}",
                    code="PLUGIN_RUNTIME_RECONCILE_FAILED",
                ) from exc
            if result is not None:
                results.append(result)
        return tuple(results)


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
    target_service: MySQLRuntimeTargetService
    storage: FilesystemPluginStorage
    environments: LockedVirtualEnvironmentBuilder
    upload_signature_verifier: Any
    management_repository: MySQLAutomationPluginManagementRepository
    binding_resolver: ProductionProjectBindingResolver
    lifecycle_service: AutomationPluginService
    configuration_service: AutomationProjectConfigurationService
    management: AutomationPluginManagementService
    required_first_party_ids: frozenset[str]
    _started: bool = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        await self.broker.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self.broker.stop()
        self._started = False

    def reconcile(self) -> tuple[object, ...]:
        if not self.bootstrap.ok:
            raise PluginConflictError("first-party plugin bootstrap was rejected")
        return self.target_service.reconcile_all()

    def health(self) -> dict[str, Any]:
        catalog = self.catalog.production_health(tuple(self.required_first_party_ids))
        ignored_automation_ids = self.catalog.excluded_persisted_automation_ids()
        generations: RuntimeGenerationHealth = runtime_generation_health(
            self.runtime_repository,
            expected_automation_ids=self.required_first_party_ids,
            ignored_automation_ids=ignored_automation_ids,
        )
        runnable = bool(
            catalog.get("runnable") is True and generations.healthy and self._started
        )
        return {
            "ok": bool(catalog.get("ok") is True and self._started),
            "runnable": runnable,
            "runtime_status": "READY" if runnable else "UNAVAILABLE",
            "release_sha": self.release.verified_release_sha,
            "broker": {"state": "running" if self._started else "stopped"},
            "catalog": catalog,
            "generations": {
                "healthy": generations.healthy,
                "project_count": generations.project_count,
                "committed_count": generations.committed_count,
                "active_lease_count": generations.active_lease_count,
                "blocked_projects": {
                    key: list(value)
                    for key, value in sorted(generations.blocked_projects.items())
                },
            },
        }

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
        raise PluginPackageError(
            f"{CURSOR_SECRET_ENV} must contain from 32 to 4096 UTF-8 bytes"
        )
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
    bootstrap = bootstrap_first_party_plugins(
        repository,
        core_catalog=core_catalog,
        current_release_sha=runtime_release_sha,
        expected_release_sha=release.verified_release_sha,
        package_provider=provider,
        package_materializer=provider,
    )
    if not bootstrap.ok:
        raise PluginPackageError(
            "signed first-party bootstrap failed: "
            + ", ".join(sorted(bootstrap.rejected.values()))
        )
    catalog_repository = MySQLAutomationPluginCatalogRepositoryAdapter(
        orchestration_repository
    )
    config_repository = MySQLAutomationProjectConfigurationReadAdapter(
        orchestration_repository
    )
    catalog = PluginCatalog(
        catalog_repository,
        config_repository,
        excluded_automation_plugins=deferred_first_party_automation_plugins(),
        excluded_plugin_ids=deferred_first_party_plugin_ids(),
        allowed_execution_platforms=("server",),
    )
    runtime_repository = MySQLAutomationPluginRuntimeAdapter(
        orchestration_repository
    )
    management_repository = MySQLAutomationPluginManagementRepository(
        orchestration_repository
    )
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
            + ", ".join(
                f"{operation}/{action}"
                for operation, action in sorted(missing_handler_keys)
            )
        )
    scoped_broker_handlers = {
        key: handler
        for key, handler in broker_handlers.items()
        if key in release_handler_keys
    }
    handler_keys = tuple(sorted(scoped_broker_handlers))
    coeffects = ProductionRuntimeCoeffectProvider(
        core_catalog=core_catalog,
        broker_handler_keys=handler_keys,
        account_manager=account_manager,
        binding_resolver=binding_resolver,
    )
    planner = ProductionRuntimeEffectPlanner()
    driver = ProductionRuntimeEffectDriver(broker_handler_keys=handler_keys)
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
    )
    socket_path = runtime_root / f"agent-{os.getpid()}.sock"
    issuer = LocalBrokerCapabilityIssuer(socket_path)
    core_adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers=scoped_broker_handlers,
        account_resolver=AccountManagerSessionResolver(account_manager),
        resource_resolver=binding_resolver,
    )
    broker = LocalCoreAutomationBroker(issuer=issuer, adapter=core_adapter)
    execution_router = PluginExecutionRouter(
        core_executor=core_executor,
        capability_issuer=issuer,
        sandbox_launcher=BubblewrapPluginSandbox(bubblewrap_path),
        generation_leases=runtime_repository,
        release_hold_provider=release_hold_provider,
    )
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
        target_service=target_service,
        storage=storage,
        environments=environments,
        upload_signature_verifier=trust,
        management_repository=management_repository,
        binding_resolver=binding_resolver,
        lifecycle_service=lifecycle_service,
        configuration_service=configuration_service,
        management=management,
        required_first_party_ids=release_first_party_automation_ids(),
    )


def production_cursor_secret(environ: Mapping[str, str] | None = None) -> bytes:
    """Return the stable cursor key without ever logging or persisting it."""

    return _cursor_secret(os.environ if environ is None else environ)


__all__ = [
    "CURSOR_SECRET_ENV",
    "MySQLRuntimeTargetService",
    "ProductionAutomationPluginRuntime",
    "ProductionRuntimeCoeffectProvider",
    "ProductionRuntimeEffectDriver",
    "ProductionRuntimeEffectPlanner",
    "build_production_automation_plugin_runtime",
    "build_runtime_generation_snapshot",
    "production_cursor_secret",
]
