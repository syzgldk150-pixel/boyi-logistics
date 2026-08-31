"""Installed plugin catalog and compatibility wrapper for governed tools."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from agent.automation_plugins.code_owned_fields import (
    first_party_code_owned_config_fields,
    first_party_code_owned_plan_fields,
)
from agent.automation_plugins.connector_compatibility import (
    ConnectorRequirementContract,
    connector_requirement_for_service,
    connector_requirements_from_contracts,
    evaluate_connector_requirement,
)
from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.errors import (
    AutomationPluginError,
    PluginConflictError,
    PluginNotFoundError,
)
from agent.automation_plugins.manifest import (
    AutomationPluginManifest,
    canonical_json_bytes,
    runtime_descriptor_matches_signed_installation,
)
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import (
    PluginInstanceRecord,
    PluginProjectState,
    PluginRuntimeModel,
    PluginTrustSource,
    RuntimeReconcileState,
    RuntimeGenerationSnapshot,
)
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.automation_plugins.ports import AutomationPluginRepositoryPort, AutomationProjectConfigurationPort
from agent.tool_registry import validate_schema_instance


@dataclass(frozen=True)
class PluginCatalogEntry:
    automation_id: str
    plugin_id: str
    manifest_schema_version: int
    display_name: str
    name: str
    state: str
    record_version: int
    installed_version: str
    trust_source: str
    package_sha256: str
    manifest_sha256: str
    config_schema: Mapping[str, Any]
    account_roles: tuple[Mapping[str, Any], ...]
    resource_roles: tuple[Mapping[str, Any], ...]
    allowed_entrypoints: tuple[str, ...]
    invocation_contracts: Mapping[str, Mapping[str, Any]]
    governance_anchor: Mapping[str, Any]
    governance_anchor_sha256: str
    tool_contract: Mapping[str, Any]
    worker_requirement: Mapping[str, Any]
    execution_platform: str
    runtime: Mapping[str, Any]
    scheduling: Mapping[str, Any]
    project_full_auto_allowed: bool
    runtime_permissions: Mapping[str, Any]
    signed_runtime_permissions: Mapping[str, Any]
    enabled: bool
    configured: bool
    project_config: Mapping[str, Any]
    account_bindings: Mapping[str, Any]
    resource_bindings: Mapping[str, str]
    project_schedule: Mapping[str, Any]
    install_root: str | None
    device_binding: Mapping[str, str] | None
    install_metadata: Mapping[str, Any]
    project_config_version: int
    project_config_sha256: str
    account_bindings_sha256: str
    resource_bindings_sha256: str
    device_binding_sha256: str
    enabled_entrypoints: tuple[str, ...]
    current_enabled_entrypoints: tuple[str, ...]
    target_generation: int
    committed_generation: int | None
    reconcile_state: RuntimeReconcileState
    committed_snapshot: RuntimeGenerationSnapshot | None
    runtime_model: str = PluginRuntimeModel.ACTION_V1.value
    plugin_api: str = "1.0.0"
    runtime_mode: str = "on_demand"
    provided_services: tuple[str, ...] = ()
    required_services: tuple[str, ...] = ()
    contributions: Mapping[str, Any] = field(default_factory=dict)
    declared_capabilities: tuple[Mapping[str, Any], ...] = ()
    storage_contract: Mapping[str, Any] = field(default_factory=dict)
    service_contracts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def action_id(self) -> str:
        return f"automation.{self.automation_id}.run"

    @property
    def active_runtime_model(self) -> str | None:
        return (
            self.committed_snapshot.runtime_model.value
            if self.committed_snapshot is not None
            else None
        )

    @property
    def active_version(self) -> str | None:
        return (
            self.committed_snapshot.plugin_version
            if self.committed_snapshot is not None
            else None
        )


_EXECUTION_METADATA_FIELDS = frozenset(
    {
        "project_config_version",
        "project_config",
        "account_bindings",
        "resource_bindings",
        "device_binding",
        "schedule",
        "compiled_invocations",
        "runtime_descriptor",
        "action_contract",
        "governance_anchor",
    }
)
_EXECUTION_METADATA_FIELDS_V2 = _EXECUTION_METADATA_FIELDS | {
    "runtime_model",
    "plugin_api",
    "service_contracts",
    "contributions",
    "storage_contract",
}
_RUNTIME_DESCRIPTOR_FIELDS = frozenset(
    {
        "install_metadata",
        "runtime",
        "runtime_permissions",
        "account_roles",
        "resource_roles",
    }
)


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _snapshot_execution_metadata(
    snapshot: RuntimeGenerationSnapshot,
) -> dict[str, Any]:
    metadata = copy.deepcopy(dict(snapshot.execution_metadata))
    expected_fields = (
        _EXECUTION_METADATA_FIELDS_V2
        if snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2
        else _EXECUTION_METADATA_FIELDS
    )
    if set(metadata) != expected_fields:
        raise PluginConflictError("committed generation execution metadata is not closed")
    if snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2:
        if (
            metadata.get("runtime_model") != PluginRuntimeModel.SERVICE_V2.value
            or metadata.get("plugin_api") != snapshot.plugin_api
            or any(
                not isinstance(metadata.get(field), (Mapping, list))
                for field in (
                    "service_contracts",
                    "contributions",
                    "storage_contract",
                )
            )
        ):
            raise PluginConflictError("committed service-v2 metadata is invalid")
    if (
        isinstance(metadata.get("project_config_version"), bool)
        or not isinstance(metadata.get("project_config_version"), int)
        or int(metadata["project_config_version"]) <= 0
    ):
        raise PluginConflictError("committed project config version is invalid")
    runtime_descriptor = metadata.get("runtime_descriptor")
    if not isinstance(runtime_descriptor, Mapping) or set(runtime_descriptor) != _RUNTIME_DESCRIPTOR_FIELDS:
        raise PluginConflictError("committed generation runtime descriptor is not closed")
    if any(
        not isinstance(runtime_descriptor.get(field), Mapping)
        for field in ("install_metadata", "runtime", "runtime_permissions")
    ) or any(
        not isinstance(runtime_descriptor.get(field), list)
        for field in ("account_roles", "resource_roles")
    ):
        raise PluginConflictError("committed generation runtime descriptor is invalid")
    install_root = str(runtime_descriptor["install_metadata"].get("install_root") or "")
    if not install_root or runtime_descriptor["runtime"].get("kind") != "python_subprocess":
        raise PluginConflictError("committed generation subprocess installation is invalid")
    for field in (
        "project_config",
        "account_bindings",
        "resource_bindings",
        "schedule",
        "compiled_invocations",
        "action_contract",
        "governance_anchor",
    ):
        if not isinstance(metadata.get(field), Mapping):
            raise PluginConflictError(f"committed generation {field} is invalid")
    if metadata.get("device_binding") is not None and not isinstance(
        metadata.get("device_binding"), Mapping
    ):
        raise PluginConflictError("committed generation device binding is invalid")
    digest_pairs = (
        ("project_config", snapshot.project_config_sha256),
        ("account_bindings", snapshot.account_bindings_sha256),
        ("resource_bindings", snapshot.resource_bindings_sha256),
        ("device_binding", snapshot.device_binding_sha256),
        ("schedule", snapshot.schedule_sha256),
        ("action_contract", snapshot.tool_contract_sha256),
        ("compiled_invocations", snapshot.compiled_invocations_sha256),
        ("runtime_descriptor", snapshot.runtime_descriptor_sha256),
        ("governance_anchor", snapshot.governance_anchor_sha256),
    )
    if any(_canonical_digest(metadata[field]) != expected for field, expected in digest_pairs):
        raise PluginConflictError("committed generation execution metadata hash is invalid")
    return metadata


def _committed_execution_metadata(entry: PluginCatalogEntry) -> dict[str, Any]:
    snapshot = entry.committed_snapshot
    if snapshot is None or snapshot.generation != entry.committed_generation:
        raise PluginConflictError(
            f"automation plugin has no immutable committed generation: {entry.automation_id}"
        )
    metadata = _snapshot_execution_metadata(snapshot)
    if set(metadata["compiled_invocations"]) != set(snapshot.enabled_entrypoints):
        raise PluginConflictError(
            "committed invocations do not match the generation entrypoint route"
        )
    if snapshot.plugin_version != entry.installed_version:
        if (
            entry.state != PluginProjectState.UPGRADING.value
            or entry.reconcile_state
            not in {
                RuntimeReconcileState.PREPARING,
                RuntimeReconcileState.WAITING_COEFFECTS,
                RuntimeReconcileState.READY_TO_COMMIT,
                RuntimeReconcileState.DISPOSING,
                RuntimeReconcileState.ERROR,
            }
            or entry.target_generation <= snapshot.generation
        ):
            raise PluginConflictError(
                "committed generation differs from the desired plugin outside an upgrade"
            )
        # The committed snapshot was validated against its own immutable
        # package by the repository adapter.  During prepare it must remain
        # executable even though the catalog's desired manifest points at the
        # staged target version.
        return metadata
    if canonical_json_bytes(metadata["action_contract"]) != canonical_json_bytes(
        entry.tool_contract
    ):
        raise PluginConflictError("committed action contract differs from its signed manifest")
    if canonical_json_bytes(metadata["governance_anchor"]) != canonical_json_bytes(
        entry.governance_anchor
    ):
        raise PluginConflictError("committed governance anchor differs from its signed manifest")
    expected_runtime_descriptor = {
        "runtime": copy.deepcopy(dict(entry.runtime)),
        # The installed package's signed bytes are the comparison authority.
        # Schema-v1 packages omitted the now-explicit broker ``effect`` field;
        # manifest parsing projects that omission to a conservative write only
        # for execution, never for immutable-descriptor equality.
        "runtime_permissions": copy.deepcopy(
            dict(entry.signed_runtime_permissions)
        ),
        "account_roles": [copy.deepcopy(dict(item)) for item in entry.account_roles],
        "resource_roles": [copy.deepcopy(dict(item)) for item in entry.resource_roles],
        "install_metadata": {
            **copy.deepcopy(dict(entry.install_metadata)),
            "install_root": entry.install_root,
        },
    }
    if not runtime_descriptor_matches_signed_installation(
        metadata["runtime_descriptor"],
        expected_runtime_descriptor,
        schema_version=entry.manifest_schema_version,
    ):
        raise PluginConflictError(
            "committed runtime descriptor differs from its signed installation"
        )
    return metadata


def project_capability_from_snapshot(
    snapshot: RuntimeGenerationSnapshot,
) -> dict[str, Any]:
    """Build the only executable capability from an immutable committed snapshot."""

    committed = _snapshot_execution_metadata(snapshot)
    descriptor = copy.deepcopy(dict(committed["runtime_descriptor"]))
    runtime_permissions = copy.deepcopy(dict(descriptor["runtime_permissions"]))
    broker_operations = runtime_permissions.get("broker_operations")
    if isinstance(broker_operations, list):
        normalized_operations: list[object] = []
        for operation in broker_operations:
            normalized = copy.deepcopy(operation)
            if isinstance(normalized, dict) and set(normalized) == {
                "operation",
                "action",
                "roles",
            }:
                # Schema-v1 signed packages did not declare broker effects.
                # Their immutable descriptor was verified above; execution
                # alone conservatively projects the missing effect as write.
                normalized["effect"] = "write"
            normalized_operations.append(normalized)
        runtime_permissions["broker_operations"] = normalized_operations
    capability = copy.deepcopy(dict(committed["action_contract"]))
    capability["name"] = f"automation.{snapshot.automation_id}.run"
    capability["_plugin_runtime"] = {
        "automation_id": snapshot.automation_id,
        "plugin_id": snapshot.plugin_id,
        "version": snapshot.plugin_version,
        "generation": snapshot.generation,
        "package_sha256": snapshot.package_sha256,
        "manifest_sha256": snapshot.manifest_sha256,
        "trust_source": snapshot.trust_source.value,
        "runtime_model": snapshot.runtime_model.value,
        "plugin_api": snapshot.plugin_api,
        "install_root": str(descriptor["install_metadata"].get("install_root") or ""),
        "runtime": copy.deepcopy(descriptor["runtime"]),
        "core_tool_name": str(committed["governance_anchor"].get("name") or ""),
        "install_metadata": copy.deepcopy(descriptor["install_metadata"]),
        "runtime_permissions": runtime_permissions,
        "governance_anchor": copy.deepcopy(committed["governance_anchor"]),
        "governance_anchor_sha256": _canonical_digest(committed["governance_anchor"]),
        "account_roles": copy.deepcopy(descriptor["account_roles"]),
        "resource_roles": copy.deepcopy(descriptor["resource_roles"]),
        "account_bindings": copy.deepcopy(committed["account_bindings"]),
        "resource_bindings": copy.deepcopy(committed["resource_bindings"]),
    }
    if snapshot.runtime_model is PluginRuntimeModel.SERVICE_V2:
        capability["_plugin_runtime"].update(
            {
                "service_contracts": copy.deepcopy(committed["service_contracts"]),
                "contributions": copy.deepcopy(committed["contributions"]),
                "storage_contract": copy.deepcopy(committed["storage_contract"]),
                "compiled_invocations": copy.deepcopy(
                    committed["compiled_invocations"]
                ),
            }
        )
    return capability


def _entry_from_project(
    project: PluginInstanceRecord,
    project_configuration: AutomationProjectConfigurationPort | None = None,
) -> PluginCatalogEntry:
    version = project.active_version
    if version.runtime_model is PluginRuntimeModel.SERVICE_V2:
        manifest = AutomationPluginManifestV2.from_mapping(version.manifest)
        service_contract = ServiceV2ProjectContract.from_manifest(manifest)
        account_roles = service_contract.account_roles
        resource_roles = service_contract.resource_roles
        allowed_entrypoints = service_contract.allowed_entrypoints
        invocation_contracts = service_contract.invocation_contracts
        governance_anchor = service_contract.governance_anchor
        governance_anchor_sha256 = service_contract.governance_anchor_sha256
        tool_contract = service_contract.tool_contract
        worker_requirement = {
            "required": False,
            "interactive_session": False,
            "supported_os": [],
            "queue_deadline_seconds": 60,
        }
        execution_platform = "server"
        scheduling = service_contract.scheduling
        runtime_permissions = service_contract.runtime_permissions
        signed_runtime_permissions = service_contract.runtime_permissions
        project_full_auto_allowed = True
        provided_services = manifest.provided_services
        required_services = manifest.required_services
        service_contracts = {
            "provides": [copy.deepcopy(dict(item)) for item in manifest.provides],
            "requires": [copy.deepcopy(dict(item)) for item in manifest.requires],
        }
        contributions = manifest.to_mapping()["contributes"]
        declared_capabilities = manifest.capabilities
        storage_contract = manifest.storage
    else:
        manifest = AutomationPluginManifest.from_mapping(version.manifest)
        signed_manifest = manifest.to_signed_mapping()
        account_roles = manifest.account_roles
        resource_roles = manifest.resource_roles
        allowed_entrypoints = manifest.allowed_entrypoints
        invocation_contracts = manifest.invocation_contracts
        governance_anchor = manifest.governance_anchor
        governance_anchor_sha256 = manifest.governance_anchor_sha256
        tool_contract = manifest.tool_contract
        worker_requirement = manifest.worker_requirement
        execution_platform = manifest.execution_platform
        scheduling = manifest.scheduling
        runtime_permissions = manifest.runtime_permissions
        signed_runtime_permissions = signed_manifest["runtime_permissions"]
        project_full_auto_allowed = manifest.project_full_auto_allowed
        provided_services = ()
        required_services = ()
        contributions = {}
        declared_capabilities = ()
        storage_contract = {}
        service_contracts = {}
    if manifest.plugin_id != project.plugin_id or version.plugin_id != project.plugin_id:
        raise PluginConflictError("persisted plugin_id does not match its manifest")
    if manifest.version != version.version:
        raise PluginConflictError("persisted plugin version does not match its manifest")
    if manifest.manifest_sha256 != version.manifest_sha256:
        raise PluginConflictError("persisted plugin manifest digest is invalid")
    if not isinstance(version.trust_source, PluginTrustSource):
        raise PluginConflictError("persisted plugin trust source is invalid")
    committed = project.committed_snapshot
    if committed is not None:
        committed_matches_desired = (
            committed.plugin_version == version.version
            and committed.package_sha256 == version.package_sha256
            and committed.manifest_sha256 == version.manifest_sha256
            and committed.trust_source == version.trust_source
            and committed.runtime_model == version.runtime_model
            and committed.plugin_api == version.plugin_api
        )
        upgrade_in_progress = (
            project.state == PluginProjectState.UPGRADING
            and project.reconcile_state
            in {
                RuntimeReconcileState.PREPARING,
                RuntimeReconcileState.WAITING_COEFFECTS,
                RuntimeReconcileState.READY_TO_COMMIT,
                RuntimeReconcileState.DISPOSING,
                RuntimeReconcileState.ERROR,
            }
            and project.target_generation > committed.generation
        )
        if (
            project.committed_generation != committed.generation
            or committed.automation_id != project.automation_id
            or committed.plugin_id != project.plugin_id
            or (not committed_matches_desired and not upgrade_in_progress)
        ):
            raise PluginConflictError(
                "committed generation does not match its immutable plugin version"
            )
    binding = None
    config = project_configuration.get_project_config(project.automation_id) if project_configuration else None
    if config is not None and config.device_binding is not None:
        binding = {
            "device_id": config.device_binding.device_id,
            "device_name": config.device_binding.device_name,
        }
    return PluginCatalogEntry(
        automation_id=project.automation_id,
        plugin_id=project.plugin_id,
        manifest_schema_version=manifest.schema_version,
        display_name=project.display_name,
        name=manifest.name,
        state=project.state.value,
        record_version=project.record_version,
        installed_version=version.version,
        trust_source=version.trust_source.value,
        package_sha256=version.package_sha256,
        manifest_sha256=version.manifest_sha256,
        config_schema=copy.deepcopy(dict(manifest.config_schema)),
        account_roles=tuple(copy.deepcopy(dict(item)) for item in account_roles),
        resource_roles=tuple(copy.deepcopy(dict(item)) for item in resource_roles),
        allowed_entrypoints=tuple(allowed_entrypoints),
        invocation_contracts={
            key: copy.deepcopy(dict(value))
            for key, value in invocation_contracts.items()
        },
        governance_anchor=copy.deepcopy(dict(governance_anchor)),
        governance_anchor_sha256=governance_anchor_sha256,
        tool_contract=copy.deepcopy(dict(tool_contract)),
        worker_requirement=copy.deepcopy(dict(worker_requirement)),
        execution_platform=execution_platform,
        runtime=copy.deepcopy(dict(manifest.runtime)),
        scheduling=copy.deepcopy(dict(scheduling)),
        project_full_auto_allowed=project_full_auto_allowed,
        runtime_permissions=copy.deepcopy(dict(runtime_permissions)),
        signed_runtime_permissions=copy.deepcopy(dict(signed_runtime_permissions)),
        enabled=(
            project.enabled
            if project.enabled is not None
            else project.state == PluginProjectState.ENABLED
        ),
        configured=config.configured if config is not None else False,
        project_config=copy.deepcopy(dict(config.config)) if config is not None else {},
        account_bindings=(
            copy.deepcopy(dict(config.account_bindings)) if config is not None else {}
        ),
        resource_bindings=(
            copy.deepcopy(dict(config.resource_bindings)) if config is not None else {}
        ),
        project_schedule=(
            copy.deepcopy(dict(config.schedule))
            if config is not None
            else {"kind": "none", "times": [], "enabled": False}
        ),
        install_root=version.install_root,
        device_binding=binding,
        install_metadata=copy.deepcopy(dict(version.install_metadata)),
        project_config_version=config.config_version if config is not None else 0,
        project_config_sha256=config.config_sha256 if config is not None else "",
        account_bindings_sha256=config.account_bindings_sha256 if config is not None else "",
        resource_bindings_sha256=config.resource_bindings_sha256 if config is not None else "",
        device_binding_sha256=config.device_binding_sha256 if config is not None else "",
        enabled_entrypoints=(
            tuple(committed.enabled_entrypoints) if committed is not None else ()
        ),
        current_enabled_entrypoints=(
            tuple(config.enabled_entrypoints) if config is not None else ()
        ),
        target_generation=project.target_generation,
        committed_generation=project.committed_generation,
        reconcile_state=project.reconcile_state,
        committed_snapshot=committed,
        runtime_model=version.runtime_model.value,
        plugin_api=version.plugin_api,
        runtime_mode=str(manifest.runtime.get("mode") or "on_demand"),
        provided_services=tuple(provided_services),
        required_services=tuple(required_services),
        contributions=copy.deepcopy(dict(contributions)),
        declared_capabilities=tuple(
            copy.deepcopy(dict(item)) for item in declared_capabilities
        ),
        storage_contract=copy.deepcopy(dict(storage_contract)),
        service_contracts=copy.deepcopy(dict(service_contracts)),
    )


def project_contract_fragment(entry: PluginCatalogEntry) -> dict[str, Any]:
    """Return the stable plugin fragment bound into project authorization."""

    snapshot = entry.committed_snapshot
    committed_metadata = (
        _committed_execution_metadata(entry) if snapshot is not None else None
    )
    return {
        "automation_id": entry.automation_id,
        "plugin_id": entry.plugin_id,
        "action_id": entry.action_id,
        "plugin_version": entry.installed_version,
        "runtime_model": entry.runtime_model,
        "plugin_api": entry.plugin_api,
        "active_runtime_model": entry.active_runtime_model,
        "active_version": entry.active_version,
        "trust_source": entry.trust_source,
        "package_sha256": entry.package_sha256,
        "manifest_sha256": entry.manifest_sha256,
        "config_schema": copy.deepcopy(dict(entry.config_schema)),
        "account_roles": [copy.deepcopy(dict(item)) for item in entry.account_roles],
        "resource_roles": [copy.deepcopy(dict(item)) for item in entry.resource_roles],
        "allowed_entrypoints": list(entry.allowed_entrypoints),
        "entrypoint_kinds": {
            key: str(value.get("contribution_kind") or key)
            for key, value in sorted(entry.invocation_contracts.items())
        },
        "invocation_contracts": {
            key: copy.deepcopy(dict(value))
            for key, value in sorted(entry.invocation_contracts.items())
        },
        "governance_anchor": copy.deepcopy(dict(entry.governance_anchor)),
        "governance_anchor_sha256": entry.governance_anchor_sha256,
        "tool_contract": copy.deepcopy(dict(entry.tool_contract)),
        "worker_requirement": copy.deepcopy(dict(entry.worker_requirement)),
        "execution_platform": entry.execution_platform,
        "runtime_kind": str(entry.runtime.get("kind") or ""),
        "runtime_mode": entry.runtime_mode,
        "provided_services": list(entry.provided_services),
        "required_services": list(entry.required_services),
        "contributions": copy.deepcopy(dict(entry.contributions)),
        "scheduling": copy.deepcopy(dict(entry.scheduling)),
        "project_full_auto_allowed": entry.project_full_auto_allowed,
        "runtime_permissions": copy.deepcopy(dict(entry.runtime_permissions)),
        "code_owned_plan_fields": list(
            first_party_code_owned_plan_fields(
                automation_id=entry.automation_id,
                plugin_id=entry.plugin_id,
                trust_source=entry.trust_source,
            )
        ),
        "device_binding": (
            copy.deepcopy(committed_metadata["device_binding"])
            if committed_metadata is not None
            else None
        ),
        "enabled": entry.enabled,
        "project_config_version": (
            int(committed_metadata["project_config_version"])
            if committed_metadata is not None
            else 0
        ),
        "project_config_sha256": snapshot.project_config_sha256 if snapshot else "",
        "account_bindings_sha256": snapshot.account_bindings_sha256 if snapshot else "",
        "resource_bindings_sha256": snapshot.resource_bindings_sha256 if snapshot else "",
        "device_binding_sha256": snapshot.device_binding_sha256 if snapshot else "",
        "enabled_entrypoints": list(entry.enabled_entrypoints),
        "target_generation": entry.target_generation,
        "committed_generation": entry.committed_generation,
        "reconcile_state": entry.reconcile_state.value,
    }


class PluginCatalog:
    """Read-through catalog backed by an injected persistence adapter."""

    def __init__(
        self,
        repository: AutomationPluginRepositoryPort,
        project_configuration: AutomationProjectConfigurationPort | None = None,
        *,
        excluded_automation_ids: Sequence[str] = (),
        excluded_automation_plugins: Mapping[str, str] | None = None,
        excluded_plugin_ids: Sequence[str] = (),
        allowed_execution_platforms: Sequence[str] | None = None,
        migration_pair_provider: (
            Callable[[str], Mapping[str, Any] | None] | None
        ) = None,
        account_binding_ready: (
            Callable[[str, Sequence[str]], bool] | None
        ) = None,
        contribution_backend_status: (
            Callable[..., tuple[str, str, str | None, str | None]] | None
        ) = None,
        connector_registry: ConnectorRegistry | None = None,
    ) -> None:
        self._repository = repository
        self._project_configuration = project_configuration
        self._excluded_automation_ids = frozenset(
            str(item or "").strip()
            for item in excluded_automation_ids
            if str(item or "").strip()
        )
        self._excluded_automation_plugins = {
            str(automation_id or "").strip(): str(plugin_id or "").strip()
            for automation_id, plugin_id in (excluded_automation_plugins or {}).items()
            if str(automation_id or "").strip() and str(plugin_id or "").strip()
        }
        overlap = self._excluded_automation_ids & self._excluded_automation_plugins.keys()
        if overlap:
            raise ValueError(
                "excluded automation identities cannot use both untyped and typed exclusions"
            )
        self._excluded_plugin_ids = frozenset(
            str(item or "").strip()
            for item in excluded_plugin_ids
            if str(item or "").strip()
        )
        self._allowed_execution_platforms = (
            None
            if allowed_execution_platforms is None
            else frozenset(
                str(item or "").strip().lower()
                for item in allowed_execution_platforms
                if str(item or "").strip()
            )
        )
        if self._allowed_execution_platforms == frozenset():
            raise ValueError("allowed execution platforms cannot be empty")
        self._migration_pair_provider = migration_pair_provider
        self._account_binding_ready = account_binding_ready
        self._contribution_backend_status = contribution_backend_status
        self._connectors = connector_registry or ConnectorRegistry()

    def _project_is_excluded(self, project: PluginInstanceRecord) -> bool:
        if project.automation_id in self._excluded_automation_ids:
            return True
        raw_manifest = project.active_version.manifest
        try:
            manifest = (
                AutomationPluginManifestV2.from_mapping(raw_manifest)
                if project.active_version.runtime_model
                is PluginRuntimeModel.SERVICE_V2
                else AutomationPluginManifest.from_mapping(raw_manifest)
            )
        except Exception:
            # Missing or corrupt platform data remains visible and therefore
            # fails closed in _entry_from_project instead of being hidden.
            return False
        version = project.active_version
        if (
            manifest.plugin_id != project.plugin_id
            or version.plugin_id != project.plugin_id
            or manifest.version != version.version
            or manifest.manifest_sha256 != version.manifest_sha256
        ):
            return False
        expected_plugin_id = self._excluded_automation_plugins.get(
            project.automation_id
        )
        if expected_plugin_id is not None:
            # A legacy identity collision remains visible and fails closed; it
            # is never hidden merely because its automation_id is reserved.
            return manifest.plugin_id == expected_plugin_id
        if manifest.plugin_id in self._excluded_plugin_ids:
            return True
        return bool(
            self._allowed_execution_platforms is not None
            and (
                "server"
                if project.active_version.runtime_model
                is PluginRuntimeModel.SERVICE_V2
                else manifest.execution_platform
            )
            not in self._allowed_execution_platforms
        )

    def excluded_persisted_automation_ids(self) -> frozenset[str]:
        """Return exact persisted identities omitted by the release scope."""

        # Only persisted project identities are safe to expose as hidden IDs.
        # Static/reserved identifiers must not cause a fallback card to vanish;
        # a real same-ID project remains visible and fails closed instead.
        _, hidden_automation_ids, _ = self._partition_projects()
        return hidden_automation_ids

    @staticmethod
    def _project_data_failure(
        exc: AutomationPluginError | ValueError | None = None,
    ) -> dict[str, str]:
        if isinstance(exc, AutomationPluginError):
            if exc.code == "PLUGIN_IDENTITY_CONFLICT":
                raise exc
            code = str(exc.code)
        else:
            code = "PLUGIN_PROJECT_DATA_INVALID"
        return {
            "runtime_status": "UNAVAILABLE",
            "error_code": code,
        }

    @staticmethod
    def _validate_project_identities(raw_ids: Sequence[object]) -> tuple[str, ...]:
        automation_ids = tuple(str(item or "").strip() for item in raw_ids)
        if any(not automation_id for automation_id in automation_ids) or len(
            set(automation_ids)
        ) != len(automation_ids):
            raise PluginConflictError(
                "automation project identities are missing or duplicated",
                code="PLUGIN_IDENTITY_CONFLICT",
            )
        return automation_ids

    def _partition_projects(
        self,
    ) -> tuple[
        list[PluginInstanceRecord],
        frozenset[str],
        dict[str, dict[str, str]],
    ]:
        raw_id_reader = getattr(self._repository, "list_instance_ids", None)
        loaded_projects: dict[str, PluginInstanceRecord] | None = None
        if callable(raw_id_reader):
            automation_ids = self._validate_project_identities(tuple(raw_id_reader()))
        else:
            # Compatibility for lightweight/in-memory adapters. Production
            # repositories expose list_instance_ids so one bad row is never
            # parsed as part of global identity discovery.
            projects = tuple(self._repository.list_instances())
            automation_ids = self._validate_project_identities(
                tuple(getattr(project, "automation_id", "") for project in projects)
            )
            loaded_projects = dict(zip(automation_ids, projects, strict=True))
        visible: list[PluginInstanceRecord] = []
        hidden: set[str] = set()
        failures: dict[str, dict[str, str]] = {}
        for automation_id in automation_ids:
            if automation_id in self._excluded_automation_ids:
                hidden.add(automation_id)
                continue
            if loaded_projects is None:
                try:
                    project = self._repository.get_instance(automation_id)
                except (AutomationPluginError, ValueError) as exc:
                    failures[automation_id] = self._project_data_failure(exc)
                    continue
                if project is None:
                    failures[automation_id] = self._project_data_failure()
                    continue
            else:
                project = loaded_projects[automation_id]
            if str(getattr(project, "automation_id", "") or "").strip() != automation_id:
                failures[automation_id] = self._project_data_failure()
                continue
            if self._project_is_excluded(project):
                hidden.add(automation_id)
            else:
                visible.append(project)
        return visible, frozenset(hidden), dict(sorted(failures.items()))

    def persisted_automation_ids(self) -> tuple[str, ...]:
        """Return visible project identities without compiling project manifests."""

        projects, _, unavailable_projects = self._partition_projects()
        return tuple(
            sorted(
                {
                    *(project.automation_id for project in projects),
                    *unavailable_projects,
                }
            )
        )

    def _entries_with_failures(
        self,
    ) -> tuple[
        list[PluginCatalogEntry],
        frozenset[str],
        dict[str, dict[str, str]],
    ]:
        projects, hidden_automation_ids, failures = self._partition_projects()
        entries: list[PluginCatalogEntry] = []
        for project in projects:
            try:
                entries.append(
                    _entry_from_project(project, self._project_configuration)
                )
            except (AutomationPluginError, ValueError) as exc:
                failures[project.automation_id] = self._project_data_failure(exc)
        return (
            sorted(entries, key=lambda item: item.automation_id),
            hidden_automation_ids,
            dict(sorted(failures.items())),
        )

    def list(self, *, include_disabled: bool = True) -> list[PluginCatalogEntry]:
        entries, _, _ = self._entries_with_failures()
        if not include_disabled:
            entries = [entry for entry in entries if entry.enabled]
        return entries

    @staticmethod
    def _resource_summary(entry: PluginCatalogEntry) -> str:
        permissions = entry.runtime_permissions
        labels: list[str] = []
        for field in ("network", "browser", "office"):
            if permissions.get(field) is True:
                labels.append(field)
        file_roles = permissions.get("file_roles")
        if isinstance(file_roles, list):
            labels.extend(f"file:{role}" for role in file_roles if isinstance(role, str) and role)
        for resource in entry.resource_roles:
            role = str(resource.get("role") or "")
            kinds = resource.get("allowed_kinds")
            if role and isinstance(kinds, list):
                labels.append(f"{role}({'/'.join(str(kind) for kind in kinds)})")
        if entry.worker_requirement.get("required") is True:
            labels.append("named-windows-worker")
        return ", ".join(labels) if labels else "core-managed resources only"

    @staticmethod
    def _safe_account_roles(entry: PluginCatalogEntry) -> list[dict[str, Any]]:
        field_counts: dict[str, int] = {}
        for role in entry.account_roles:
            field = str(role.get("argument_field") or "")
            field_counts[field] = field_counts.get(field, 0) + 1
        result: list[dict[str, Any]] = []
        for role in entry.account_roles:
            projected = copy.deepcopy(dict(role))
            field = str(role.get("argument_field") or "")
            projected["binding_cardinality"] = (
                "many"
                if role.get("collection") is True
                and (role.get("argument_field") is None or field_counts.get(field) == 1)
                else "one"
            )
            result.append(projected)
        return result

    @staticmethod
    def _code_owned_config_fields(entry: PluginCatalogEntry) -> tuple[str, ...]:
        return first_party_code_owned_config_fields(
            automation_id=entry.automation_id,
            plugin_id=entry.plugin_id,
            trust_source=entry.trust_source,
        )

    @classmethod
    def _safe_instance_config_schema(
        cls,
        entry: PluginCatalogEntry,
    ) -> dict[str, Any]:
        schema = copy.deepcopy(dict(entry.config_schema))
        code_owned_fields = set(cls._code_owned_config_fields(entry))
        properties = schema.get("properties")
        if isinstance(properties, dict):
            schema["properties"] = {
                str(field_name): value
                for field_name, value in properties.items()
                if str(field_name) not in code_owned_fields
            }
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [
                str(field_name)
                for field_name in required
                if str(field_name) not in code_owned_fields
            ]
        return schema

    @classmethod
    def _safe_instance_config(cls, entry: PluginCatalogEntry) -> dict[str, Any]:
        code_owned_fields = set(cls._code_owned_config_fields(entry))
        return {
            str(field_name): copy.deepcopy(value)
            for field_name, value in entry.project_config.items()
            if str(field_name) not in code_owned_fields
        }

    def _v2_dependency_statuses(
        self,
        entries: Sequence[PluginCatalogEntry],
    ) -> dict[str, tuple[str, list[dict[str, str]]]]:
        """Resolve package-level service providers without rejecting instances.

        Multiple projects of the same immutable package share one provider
        claim, so each project may keep independent account/config bindings.
        Different immutable packages claiming the same service are blocked and
        remain installed for an administrator to resolve.
        """

        v2_entries = [
            entry
            for entry in entries
            if entry.runtime_model == PluginRuntimeModel.SERVICE_V2.value
        ]
        candidates: dict[str, list[PluginCatalogEntry]] = {}
        for entry in v2_entries:
            for service in entry.provided_services:
                candidates.setdefault(service, []).append(entry)

        conflicts: set[str] = set()
        providers: dict[str, tuple[str, str, str]] = {}
        for service, items in candidates.items():
            identities = {
                (item.plugin_id, item.installed_version, item.manifest_sha256)
                for item in items
            }
            if len(identities) != 1:
                conflicts.add(service)
            else:
                providers[service] = next(iter(identities))

        package_requirements: dict[tuple[str, str, str], tuple[str, ...]] = {}
        package_connector_requirements: dict[
            tuple[str, str, str],
            tuple[ConnectorRequirementContract, ...],
        ] = {}
        package_runtime_ready: dict[tuple[str, str, str], bool] = {}
        for entry in v2_entries:
            identity = (
                entry.plugin_id,
                entry.installed_version,
                entry.manifest_sha256,
            )
            package_requirements.setdefault(identity, entry.required_services)
            package_connector_requirements.setdefault(
                identity,
                self._entry_connector_requirements(entry),
            )
            package_runtime_ready[identity] = bool(
                package_runtime_ready.get(identity)
                or (
                    entry.enabled
                    and entry.configured
                    and entry.state == PluginProjectState.ENABLED.value
                    and entry.committed_snapshot is not None
                    and entry.committed_generation is not None
                    and entry.committed_snapshot.generation
                    == entry.committed_generation
                    and entry.committed_snapshot.plugin_version
                    == entry.installed_version
                    and entry.target_generation == entry.committed_generation
                    and entry.reconcile_state == RuntimeReconcileState.STABLE
                    and not self._missing_requirements(entry)
                    and not self._required_account_unavailable(entry)
                )
            )

        active_packages: set[tuple[str, str, str]] = set()
        changed = True
        while changed:
            changed = False
            for identity, requirements in package_requirements.items():
                if identity in active_packages or not package_runtime_ready.get(identity):
                    continue
                if all(
                    (
                        evaluate_connector_requirement(
                            self._connectors,
                            connector_requirement_for_service(
                                package_connector_requirements[identity],
                                service,
                            ),
                            service=service,
                        ).ready
                        if service.startswith("connector.")
                        else False
                    )
                    or (
                        not service.startswith("connector.")
                        and
                        service not in conflicts
                        and (provider := providers.get(service)) is not None
                        and provider in active_packages
                    )
                    for service in requirements
                ):
                    active_packages.add(identity)
                    changed = True

        result: dict[str, tuple[str, list[dict[str, str]]]] = {}
        for entry in v2_entries:
            reasons: list[dict[str, str]] = []
            for service in entry.required_services:
                if service.startswith("connector."):
                    compatibility = evaluate_connector_requirement(
                        self._connectors,
                        connector_requirement_for_service(
                            self._entry_connector_requirements(entry),
                            service,
                        ),
                        service=service,
                    )
                    if not compatibility.ready:
                        reasons.append(
                            {
                                "code": compatibility.reason_code
                                or "CONNECTOR_REQUIREMENT_INCOMPATIBLE",
                                "service": service,
                                "message": compatibility.reason
                                or "Connector requirement is incompatible",
                            }
                        )
                elif service in conflicts:
                    reasons.append(
                        {
                            "code": "PROVIDER_CONFLICT",
                            "service": service,
                            "message": "多个不同版本声明了同一服务",
                        }
                    )
                elif service not in providers:
                    reasons.append(
                        {
                            "code": "MISSING_PROVIDER",
                            "service": service,
                            "message": "依赖服务尚未安装",
                        }
                    )
                elif providers[service] not in active_packages:
                    reasons.append(
                        {
                            "code": "PROVIDER_BLOCKED",
                            "service": service,
                            "message": "依赖服务自身尚未就绪",
                        }
                    )
            for service in entry.provided_services:
                if service in conflicts:
                    reasons.append(
                        {
                            "code": "PROVIDER_CONFLICT",
                            "service": service,
                            "message": "该服务存在不同内容的 Provider 冲突",
                        }
                    )
            result[entry.automation_id] = (
                "READY" if not reasons else "BLOCKED_DEPENDENCY",
                reasons,
            )
        return result

    @staticmethod
    def _entry_connector_requirements(
        entry: PluginCatalogEntry,
    ) -> tuple[ConnectorRequirementContract, ...]:
        service_contracts = getattr(entry, "service_contracts", {})
        account_roles = getattr(entry, "account_roles", ())
        resource_roles = getattr(entry, "resource_roles", ())
        if not isinstance(service_contracts, Mapping):
            return ()
        raw_requirements = service_contracts.get("requires")
        if not isinstance(raw_requirements, (list, tuple)) or not isinstance(
            account_roles,
            (list, tuple),
        ) or not isinstance(resource_roles, (list, tuple)):
            return ()
        try:
            return connector_requirements_from_contracts(
                requirements=(
                    item for item in raw_requirements if isinstance(item, Mapping)
                ),
                account_roles=(
                    item for item in account_roles if isinstance(item, Mapping)
                ),
                resource_roles=(
                    item for item in resource_roles if isinstance(item, Mapping)
                ),
            )
        except (AutomationPluginError, TypeError, ValueError):
            return ()

    def _required_account_unavailable(self, entry: PluginCatalogEntry) -> bool:
        checker = self._account_binding_ready
        if checker is None:
            return False
        for role in entry.account_roles:
            if role.get("required") is not True:
                continue
            name = str(role.get("role") or "")
            raw_binding = entry.account_bindings.get(name)
            values = (
                raw_binding
                if isinstance(raw_binding, (list, tuple))
                else (raw_binding,)
            )
            allowed = role.get("allowed_systems")
            if not isinstance(allowed, list):
                return True
            for account_id in values:
                normalized = str(account_id or "").strip()
                if not normalized:
                    return True
                try:
                    ready = checker(normalized, [str(item) for item in allowed])
                except Exception:
                    return True
                if ready is not True:
                    return True
        return False

    def _contribution_backend_reasons(
        self,
        entry: PluginCatalogEntry,
    ) -> list[dict[str, str]]:
        """Fail closed when an enabled managed contribution has no host backend."""

        if entry.runtime_model != PluginRuntimeModel.SERVICE_V2.value:
            return []
        enabled = set(entry.current_enabled_entrypoints)
        reasons: list[dict[str, str]] = []
        resolver = self._contribution_backend_status
        for kind in ("scheduler", "webhook", "feishu", "events"):
            raw_items = entry.contributions.get(kind)
            if not isinstance(raw_items, (list, tuple)):
                return [
                    {
                        "code": "CAPABILITY_UNAVAILABLE",
                        "service": "",
                        "message": "插件贡献点声明不可用",
                    }
                ]
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    return [
                        {
                            "code": "CAPABILITY_UNAVAILABLE",
                            "service": "",
                            "message": "插件贡献点声明不可用",
                        }
                    ]
                contribution_id = str(raw_item.get("id") or "")
                if contribution_id not in enabled:
                    continue
                try:
                    if resolver is None:
                        raise RuntimeError("contribution backend resolver is unavailable")
                    _backend, status, reason_code, reason_detail = resolver(
                        contribution_kind=kind,
                        declaration=raw_item,
                        project_schedule=entry.project_schedule,
                    )
                except Exception:  # noqa: BLE001 - Catalog readiness must fail closed
                    status = "CAPABILITY_UNAVAILABLE"
                    reason_code = "CAPABILITY_UNAVAILABLE"
                    reason_detail = "CONTRIBUTION_HOST_BACKEND_UNAVAILABLE"
                # The injected host resolver is the sole backend authority.  It
                # may return DISABLED only for an intentionally closed project
                # schedule/entrypoint; every other non-READY state blocks.
                if status in {"READY", "DISABLED"}:
                    continue
                reasons.append(
                    {
                        "code": str(reason_code or "CAPABILITY_UNAVAILABLE"),
                        "service": str(raw_item.get("service") or ""),
                        "message": (
                            "贡献点宿主能力不可用："
                            f"{kind}/{contribution_id} ({reason_detail or status})"
                        ),
                    }
                )
        return reasons

    def _readiness(
        self,
        entry: PluginCatalogEntry,
        dependency: tuple[str, list[dict[str, str]]] | None,
    ) -> tuple[str, list[dict[str, str]]]:
        dependency_state, dependency_reasons = dependency or ("READY", [])
        if dependency_state != "READY":
            return dependency_state, dependency_reasons
        if entry.runtime_mode == "resident":
            return (
                "BLOCKED_DEPENDENCY",
                [
                    {
                        "code": "RESIDENT_RUNTIME_UNAVAILABLE",
                        "service": "",
                        "message": "当前主机尚未提供常驻进程运行器",
                    }
                ],
            )
        contribution_reasons = self._contribution_backend_reasons(entry)
        if contribution_reasons:
            return "BLOCKED_DEPENDENCY", contribution_reasons
        missing = self._missing_requirements(entry)
        if "account_binding" in missing or self._required_account_unavailable(entry):
            return (
                "BLOCKED_LOGIN",
                [
                    {
                        "code": "ACCOUNT_BINDING_MISSING",
                        "service": "",
                        "message": "必需的后台登录账号尚未绑定或登录已失效",
                    }
                ],
            )
        if missing:
            return (
                "NEEDS_CONFIGURATION",
                [
                    {
                        "code": "CONFIGURATION_INCOMPLETE",
                        "service": "",
                        "message": "项目配置或资源绑定尚未完成",
                    }
                ],
            )
        return "READY", []

    def safe_projection(self) -> dict[str, Any]:
        """Return the closed Console projection without integrity or filesystem data.

        The projection intentionally excludes raw manifests, integrity digests,
        installation roots, sessions and credentials. It includes only the
        declarative schemas and core-owned binding identifiers required by the
        uniform Console configuration form.
        """

        entries, hidden_automation_ids, unavailable_projects = (
            self._entries_with_failures()
        )
        dependency_statuses = self._v2_dependency_statuses(entries)
        newest: dict[str, PluginCatalogEntry] = {}
        for entry in entries:
            current = newest.get(entry.plugin_id)
            version_key = tuple(int(part) for part in entry.installed_version.split("."))
            current_key = (
                tuple(int(part) for part in current.installed_version.split("."))
                if current is not None
                else ()
            )
            if current is None or version_key > current_key:
                newest[entry.plugin_id] = entry
        plugins = [
            {
                "plugin_id": entry.plugin_id,
                "name": entry.name,
                "version": entry.installed_version,
                "runtime_model": entry.runtime_model,
                "plugin_api": entry.plugin_api,
                "runtime_mode": entry.runtime_mode,
                "provided_services": list(entry.provided_services),
                "required_services": list(entry.required_services),
                "execution_platform": entry.execution_platform,
                "can_schedule": entry.scheduling.get("supported") is True,
                "worker_required": entry.worker_requirement.get("required") is True,
                "action_summary": str(entry.tool_contract.get("description") or entry.name),
                "resource_summary": self._resource_summary(entry),
                "account_roles": self._safe_account_roles(entry),
                "resource_roles": [copy.deepcopy(dict(role)) for role in entry.resource_roles],
                "config_schema": copy.deepcopy(dict(entry.config_schema)),
                "scheduling": copy.deepcopy(dict(entry.scheduling)),
                "entrypoints": list(entry.allowed_entrypoints),
                "entrypoint_kinds": {
                    key: str(value.get("contribution_kind") or key)
                    for key, value in sorted(entry.invocation_contracts.items())
                },
            }
            for entry in sorted(newest.values(), key=lambda item: item.plugin_id)
        ]
        instances = [
            {
                "automation_id": entry.automation_id,
                "plugin_id": entry.plugin_id,
                "instance_name": entry.display_name,
                "version": entry.installed_version,
                "target_version": entry.installed_version,
                "active_version": entry.active_version,
                "runtime_model": entry.runtime_model,
                "target_runtime_model": entry.runtime_model,
                "active_runtime_model": entry.active_runtime_model,
                "plugin_api": entry.plugin_api,
                "runtime_mode": entry.runtime_mode,
                "provided_services": list(entry.provided_services),
                "required_services": list(entry.required_services),
                "dependency_state": self._readiness(
                    entry,
                    dependency_statuses.get(entry.automation_id),
                )[0],
                "blocking_reasons": self._readiness(
                    entry,
                    dependency_statuses.get(entry.automation_id),
                )[1],
                "migration": self._migration_projection(entry.automation_id),
                "enabled": entry.enabled,
                "configured": entry.configured,
                "state": entry.state,
                "record_version": entry.record_version,
                "target_generation": entry.target_generation,
                "committed_generation": entry.committed_generation,
                "reconcile_state": entry.reconcile_state.value,
                "project_configuration_version": entry.project_config_version,
                "execution_platform": entry.execution_platform,
                "can_schedule": entry.scheduling.get("supported") is True,
                "worker_required": entry.worker_requirement.get("required") is True,
                "action_summary": str(entry.tool_contract.get("description") or entry.name),
                "resource_summary": self._resource_summary(entry),
                "account_roles": self._safe_account_roles(entry),
                "resource_roles": [copy.deepcopy(dict(role)) for role in entry.resource_roles],
                "config_schema": self._safe_instance_config_schema(entry),
                "scheduling": copy.deepcopy(dict(entry.scheduling)),
                "entrypoints": list(entry.allowed_entrypoints),
                "entrypoint_kinds": {
                    key: str(value.get("contribution_kind") or key)
                    for key, value in sorted(entry.invocation_contracts.items())
                },
                "enabled_entrypoints": list(entry.current_enabled_entrypoints),
                "code_owned_config_fields": list(
                    self._code_owned_config_fields(entry)
                ),
                "config": self._safe_instance_config(entry),
                "account_bindings": copy.deepcopy(dict(entry.account_bindings)),
                "resource_bindings": copy.deepcopy(dict(entry.resource_bindings)),
                "schedule": copy.deepcopy(dict(entry.project_schedule)),
                "device": (
                    {
                        "id": str(entry.device_binding.get("device_id") or ""),
                        "name": str(entry.device_binding.get("device_name") or ""),
                    }
                    if entry.device_binding
                    else None
                ),
                "missing_requirements": self._missing_requirements(entry),
            }
            for entry in entries
        ]
        return {
            "connectors": [
                copy.deepcopy(item) for item in self._connectors.safe_projection()
            ],
            "plugins": plugins,
            "instances": instances,
            "unsupported_automation_ids": [],
            "hidden_automation_ids": sorted(hidden_automation_ids),
            "unavailable_projects": unavailable_projects,
        }

    def _migration_projection(self, automation_id: str) -> dict[str, Any] | None:
        provider = self._migration_pair_provider
        if provider is None:
            return None
        pair = provider(automation_id)
        if pair is None:
            return None
        pair_id = str(pair.get("migration_pair_id") or "").strip()
        source = str(pair.get("source_automation_id") or "").strip()
        target = str(pair.get("target_automation_id") or "").strip()
        state = str(pair.get("state") or "").strip()
        record_version = pair.get("record_version")
        if (
            not pair_id
            or automation_id not in {source, target}
            or not source
            or not target
            or not state
            or isinstance(record_version, bool)
            or not isinstance(record_version, int)
            or record_version <= 0
        ):
            raise PluginConflictError(
                "plugin migration catalog projection is invalid",
                code="PLUGIN_MIGRATION_DATA_INVALID",
            )
        is_source = automation_id == source
        return {
            "migration_pair_id": pair_id,
            "state": state,
            "record_version": record_version,
            "role": "source" if is_source else "target",
            "counterpart_automation_id": target if is_source else source,
        }

    @staticmethod
    def _missing_requirements(entry: PluginCatalogEntry) -> list[str]:
        missing: list[str] = []
        if not entry.configured:
            missing.append("project_config")
        required_accounts = {
            str(role.get("role") or "")
            for role in entry.account_roles
            if role.get("required") is True
        }
        if not required_accounts <= set(entry.account_bindings):
            missing.append("account_binding")
        required_resources = {
            str(role.get("role") or "")
            for role in entry.resource_roles
            if role.get("required") is True
        }
        if not required_resources <= set(entry.resource_bindings):
            missing.append("resource_binding")
        if entry.worker_requirement.get("required") is True and entry.device_binding is None:
            missing.append("device_binding")
        return missing

    def get(self, automation_id: str) -> PluginCatalogEntry | None:
        safe_automation_id = str(automation_id or "").strip()
        if safe_automation_id in self._excluded_automation_ids:
            return None
        project = self._repository.get_instance(safe_automation_id)
        if project is not None and self._project_is_excluded(project):
            return None
        return (
            _entry_from_project(project, self._project_configuration)
            if project is not None
            else None
        )

    def require(self, automation_id: str) -> PluginCatalogEntry:
        entry = self.get(automation_id)
        if entry is None:
            raise PluginNotFoundError(f"automation plugin is not installed: {automation_id}")
        return entry

    def resolve(self, automation_id: str) -> PluginCatalogEntry:
        """Resolve only by instance identity; plugin_id is never executable."""

        return self.require(automation_id)

    def resolve_invocation(self, automation_id: str, entrypoint: str) -> dict[str, Any]:
        entry = self.require(automation_id)
        source = str(entrypoint or "").strip()
        if entry.runtime_model == PluginRuntimeModel.SERVICE_V2.value:
            statuses = self._v2_dependency_statuses(self.list())
            readiness, _reasons = self._readiness(
                entry,
                statuses.get(entry.automation_id),
            )
            if readiness != "READY":
                raise PluginConflictError(
                    f"service-v2 project is not ready: {readiness}",
                    code=readiness,
                )
        if not entry.enabled:
            raise PluginConflictError(f"automation instance is disabled: {automation_id}")
        if source not in entry.enabled_entrypoints:
            raise PluginConflictError(
                f"entrypoint is not enabled for automation instance {automation_id}: {source}"
            )
        contract = entry.invocation_contracts.get(source)
        if contract is None:
            raise PluginConflictError(f"plugin does not support entrypoint: {source}")
        return {
            "entry": entry,
            "capability": self.get_project_capability(automation_id),
            "invocation_contract": copy.deepcopy(dict(contract)),
        }

    def resolve_unique_plugin_instance(self, plugin_id: str) -> PluginCatalogEntry:
        """Fail closed for Feishu/other callers that did not bind an instance."""

        matches = [entry for entry in self.list(include_disabled=False) if entry.plugin_id == plugin_id]
        if len(matches) != 1:
            raise PluginConflictError(
                f"AMBIGUOUS_PROJECT: plugin_id {plugin_id} resolves to {len(matches)} enabled instances"
            )
        return matches[0]

    def assert_projects_installed(self, automation_ids: Sequence[str]) -> None:
        """Fail release/runtime health when a displayed project has no plugin."""

        requested = {str(item or "").strip() for item in automation_ids if str(item or "").strip()}
        entries, _, unavailable_projects = self._entries_with_failures()
        installed = {
            *(entry.automation_id for entry in entries),
            *unavailable_projects,
        }
        unsupported = sorted(requested - installed)
        if unsupported:
            raise PluginNotFoundError(
                "persisted automation projects require an installed plugin: " + ", ".join(unsupported)
            )

    def production_health(self, automation_ids: Sequence[str]) -> dict[str, Any]:
        """Return a credential-free release projection and reject dev trust."""

        entries, _, unavailable_projects = self._entries_with_failures()
        expected = {str(item or "").strip() for item in automation_ids if str(item or "").strip()}
        installed = {
            *(entry.automation_id for entry in entries),
            *unavailable_projects,
        }
        unsupported = sorted(expected - installed)
        enabled_builtin = sorted(
            entry.automation_id
            for entry in entries
            if entry.enabled and entry.trust_source == PluginTrustSource.BUILTIN_RELEASE.value
        )
        invalid_trust = sorted(
            entry.automation_id
            for entry in entries
            if entry.enabled
            and entry.trust_source
            not in {
                PluginTrustSource.ED25519_UPLOAD.value,
                PluginTrustSource.ED25519_FIRST_PARTY.value,
                PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
                PluginTrustSource.BUILTIN_BUNDLE.value,
            }
        )
        unstable = sorted(
            entry.automation_id
            for entry in entries
            if not (
                not entry.enabled
                and not entry.configured
                and entry.committed_snapshot is None
                and entry.target_generation == entry.committed_generation
                and entry.reconcile_state == RuntimeReconcileState.STABLE
            )
            and (
                entry.committed_snapshot is None
                or entry.committed_generation is None
                or entry.target_generation != entry.committed_generation
                or entry.reconcile_state != RuntimeReconcileState.STABLE
            )
        )
        invalid_runtime = sorted(
            entry.automation_id
            for entry in entries
            if entry.enabled and entry.runtime.get("kind") != "python_subprocess"
        )
        package_keys = {
            (entry.plugin_id, entry.installed_version)
            for entry in entries
            if entry.trust_source
            in {
                PluginTrustSource.ED25519_UPLOAD.value,
                PluginTrustSource.ED25519_FIRST_PARTY.value,
                PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
                PluginTrustSource.BUILTIN_BUNDLE.value,
            }
        }
        trust_counts: dict[str, int] = {}
        for entry in entries:
            trust_counts[entry.trust_source] = trust_counts.get(entry.trust_source, 0) + 1
        integrity_ok = bool(
            not unsupported
            and not enabled_builtin
            and not invalid_trust
            and not invalid_runtime
        )
        runnable = bool(integrity_ok and not unstable)
        return {
            "ok": integrity_ok,
            "runnable": runnable,
            "runtime_status": "READY" if runnable else "UNAVAILABLE",
            "signed_packages": len(package_keys),
            "instances": len(entries),
            "trust_sources": dict(sorted(trust_counts.items())),
            "unsupported_automation_ids": unsupported,
            "enabled_builtin_release": enabled_builtin,
            "invalid_enabled_trust": invalid_trust,
            "unstable_generations": unstable,
            "invalid_enabled_runtime": invalid_runtime,
            "unavailable_projects": unavailable_projects,
        }

    def assert_production_ready(self, automation_ids: Sequence[str]) -> dict[str, Any]:
        health = self.production_health(automation_ids)
        if health["runnable"] is not True:
            raise PluginConflictError("automation plugin catalog is not production ready")
        return health

    @property
    def catalog_hash(self) -> str:
        fragments = [project_contract_fragment(entry) for entry in self.list()]
        return hashlib.sha256(canonical_json_bytes(fragments)).hexdigest()

    def get_capability(self, tool_name: str) -> dict[str, Any] | None:
        prefix = "automation."
        suffix = ".run"
        if not tool_name.startswith(prefix) or not tool_name.endswith(suffix):
            return None
        automation_id = tool_name[len(prefix) : -len(suffix)]
        project = self._repository.get_instance(automation_id)
        if project is None or self._project_is_excluded(project):
            return None
        enabled = (
            project.enabled
            if project.enabled is not None
            else project.state == PluginProjectState.ENABLED
        )
        if not enabled:
            return None
        return self.get_project_capability(automation_id)

    def find_by_core_tool(self, tool_name: str) -> PluginCatalogEntry | None:
        matches = [
            entry for entry in self.list(include_disabled=False)
            if entry.governance_anchor.get("name") == tool_name
        ]
        if len(matches) > 1:
            raise PluginConflictError(
                f"AMBIGUOUS_PLUGIN_ACTION: core tool {tool_name} belongs to multiple automation projects"
            )
        return matches[0] if matches else None

    def get_project_capability(self, automation_id: str) -> dict[str, Any]:
        safe_automation_id = str(automation_id or "").strip()
        project = self._repository.get_instance(safe_automation_id)
        if project is None or self._project_is_excluded(project):
            raise PluginNotFoundError(
                f"automation plugin is not installed: {automation_id}"
            )
        try:
            entry = _entry_from_project(project, self._project_configuration)
        except AutomationPluginError:
            snapshot = project.committed_snapshot
            enabled = (
                project.enabled
                if project.enabled is not None
                else project.state == PluginProjectState.ENABLED
            )
            failed_candidate_with_lkg = bool(
                enabled
                and snapshot is not None
                and project.committed_generation == snapshot.generation
                and snapshot.automation_id == project.automation_id
                and snapshot.plugin_id == project.plugin_id
                and project.state == PluginProjectState.UPGRADING
                and project.target_generation > snapshot.generation
                and project.reconcile_state
                in {
                    RuntimeReconcileState.PREPARING,
                    RuntimeReconcileState.WAITING_COEFFECTS,
                    RuntimeReconcileState.READY_TO_COMMIT,
                    RuntimeReconcileState.DISPOSING,
                    RuntimeReconcileState.ERROR,
                }
            )
            if not failed_candidate_with_lkg:
                raise
            return project_capability_from_snapshot(snapshot)
        if not entry.enabled:
            raise PluginConflictError(f"automation plugin is disabled: {automation_id}")
        if entry.committed_generation is None or entry.committed_snapshot is None:
            raise PluginConflictError(
                f"automation plugin has no committed generation: {automation_id}"
            )
        failed_uncommitted_target = (
            entry.state == PluginProjectState.UPGRADING.value
            and entry.target_generation > entry.committed_generation
            and entry.reconcile_state
            in {
                RuntimeReconcileState.DISPOSING,
                RuntimeReconcileState.ERROR,
            }
        )
        if (
            entry.reconcile_state == RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE
            or (
                entry.reconcile_state
                in {RuntimeReconcileState.DISPOSING, RuntimeReconcileState.ERROR}
                and not failed_uncommitted_target
            )
        ):
            raise PluginConflictError(
                f"automation plugin runtime is blocked: {automation_id}"
            )
        _committed_execution_metadata(entry)
        return project_capability_from_snapshot(entry.committed_snapshot)

    def validate_arguments(self, tool_name: str, arguments: Mapping[str, Any]) -> None:
        capability = self.get_capability(tool_name)
        if capability is None:
            raise KeyError(f"Unknown plugin tool: {tool_name}")
        validate_schema_instance(tool_name, arguments, capability["input_schema"])

    def list_llm_capabilities(self) -> list[dict[str, Any]]:
        # Project actions are reachable only through a trusted typed project
        # invocation (Scheduler/Console/Feishu/Webhook). A generic LLM/tool API
        # may never synthesize or enumerate automation.<id>.run capabilities.
        return []


class CompositeToolRegistry:
    """Expose core and installed plugin contracts through ToolCatalogPort."""

    def __init__(
        self,
        core_catalog: Any,
        plugin_catalog: PluginCatalog,
        *,
        blocked_core_tool_names: Sequence[str] = (),
    ) -> None:
        self._core = core_catalog
        self._plugins = plugin_catalog
        self._blocked_core_tool_names = frozenset(
            str(item or "").strip()
            for item in blocked_core_tool_names
            if str(item or "").strip()
        )

    @property
    def catalog_hash(self) -> str:
        value = {
            "core": str(self._core.catalog_hash),
            "plugins": self._plugins.catalog_hash,
            "blocked_core_tools": sorted(self._blocked_core_tool_names),
        }
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    def get_capability(self, tool_name: str) -> Mapping[str, Any] | None:
        if str(tool_name or "").strip() in self._blocked_core_tool_names:
            return None
        core = self._core.get_capability(tool_name)
        plugin = self._plugins.get_capability(tool_name)
        if core is None:
            return plugin
        if plugin is None:
            return core
        clean_plugin = {key: value for key, value in plugin.items() if key != "_plugin_projects"}
        if canonical_json_bytes(dict(core)) != canonical_json_bytes(clean_plugin):
            raise PluginConflictError(f"plugin attempts to replace governed core tool: {tool_name}")
        return copy.deepcopy(dict(core))

    def get_project_capability(self, automation_id: str) -> Mapping[str, Any]:
        return self._plugins.get_project_capability(automation_id)

    def validate_arguments(self, tool_name: str, arguments: Mapping[str, Any]) -> None:
        capability = self.get_capability(tool_name)
        if capability is None:
            raise KeyError(f"Unknown tool: {tool_name}")
        validate_schema_instance(tool_name, arguments, capability["input_schema"])

    def list_llm_capabilities(self) -> Sequence[Mapping[str, Any]]:
        merged: dict[str, Mapping[str, Any]] = {}
        for capability in [*self._core.list_llm_capabilities(), *self._plugins.list_llm_capabilities()]:
            name = str(capability.get("name") or "")
            if name in self._blocked_core_tool_names:
                continue
            current = merged.get(name)
            if current is not None and canonical_json_bytes(dict(current)) != canonical_json_bytes(dict(capability)):
                raise PluginConflictError(f"conflicting LLM capability: {name}")
            merged[name] = copy.deepcopy(dict(capability))
        return [merged[name] for name in sorted(merged)]

    def get_llm_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": str(capability["name"]),
                    "description": str(capability["description"]),
                    "parameters": copy.deepcopy(capability["input_schema"]),
                },
            }
            for capability in self.list_llm_capabilities()
        ]

    def get_openai_tools(self) -> list[dict[str, Any]]:
        return self.get_llm_tools()

    def list_tools(self) -> list[str]:
        core_names = {
            str(name)
            for name in self._core.list_tools()
            if str(name) not in self._blocked_core_tool_names
        }
        plugin_names = {entry.action_id for entry in self._plugins.list()}
        return sorted(core_names | plugin_names)

    def load(self) -> None:
        """Reload only the source-owned core registry; plugins are DB-backed."""

        self._core.load()
