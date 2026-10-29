"""Domain adapter over the transactional automation-plugin MySQL repository.

The shared repository deliberately exposes row-oriented operations.  This
adapter is the composition boundary used by the package lifecycle; it never
performs DDL or filesystem operations.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from agent.automation_plugins.errors import (
    PluginConflictError,
    PluginPackageError,
)
from agent.automation_plugins.invocation import compile_instance_arguments
from agent.automation_plugins.historical_version import require_committed_history
from agent.automation_plugins.configuration import (
    _closed_bindings,
    normalize_project_schedule,
)
from agent.automation_plugins.manifest import (
    canonical_json_bytes,
)
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import (
    ExecutionBlock,
    ExecutionBlockKind,
    PluginInstanceRecord,
    PluginRuntimeModel,
    PluginVersionRecord,
)
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.automation_plugins.ports import (
    AutomationPluginRepositoryPort,
    HardUninstallPreparation,
)
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter,
    MySQLAutomationProjectConfigurationReadAdapter,
    _version_from_row,
)
from agent.automation_plugins.storage import VERIFIED_ARCHIVE_RELATIVE
from agent.tool_registry import validate_schema_instance
from shared.automation_plugin_configuration_contract import (
    optional_console_input_additions,
    validate_optional_console_upgrade_configuration,
)


_PROJECT_FULL_AUTO = "PROJECT_FULL_AUTO"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _json_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_plain(item) for item in value]
    return copy.deepcopy(value)


def _digest(value: Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(_json_plain(value))).hexdigest()


def _semantic_version_key(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in str(value or "").split("."))
    except ValueError as exc:
        raise PluginPackageError("package version is not semantic") from exc
    if len(parts) != 3 or any(part < 0 for part in parts):
        raise PluginPackageError("package version is not semantic")
    return parts


def _installed_metadata(version: PluginVersionRecord) -> dict[str, Any]:
    root = str(version.install_root or "").strip()
    if not root:
        raise PluginPackageError("plugin version has no immutable install root")
    metadata = copy.deepcopy(dict(version.install_metadata))
    if "install_root" in metadata and str(metadata["install_root"]) != root:
        raise PluginPackageError("plugin install metadata changed its immutable root")
    metadata["install_root"] = root
    python_relative = str(metadata.get("python_relative") or "").strip()
    if not python_relative:
        raise PluginPackageError("plugin version has no isolated Python interpreter")
    if (
        metadata.get("archive_relative") != VERIFIED_ARCHIVE_RELATIVE
        or metadata.get("archive_sha256") != version.package_sha256
    ):
        raise PluginPackageError("plugin version has no immutable signed archive")
    return metadata


def _registration_rows(
    version: PluginVersionRecord,
    *,
    actor_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest, contract = _version_contract(version)
    if (
        manifest.plugin_id != version.plugin_id
        or manifest.version != version.version
        or manifest.manifest_sha256 != version.manifest_sha256
    ):
        raise PluginPackageError("plugin version identity differs from its manifest")
    metadata = _installed_metadata(version)
    manifest_mapping = manifest.to_mapping()
    package = {
        "plugin_id": manifest.plugin_id,
        "display_name": manifest.name,
        "description": manifest.description,
    }
    persisted_version = {
        "version": manifest.version,
        "runtime_model": version.runtime_model.value,
        "plugin_api": version.plugin_api,
        "package_sha256": version.package_sha256,
        "manifest_sha256": version.manifest_sha256,
        "manifest_json": manifest_mapping,
        "tool_contract_sha256": _digest(dict(contract.tool_contract)),
        "config_schema_sha256": _digest(manifest_mapping["config_schema"]),
        "allowed_entrypoints_sha256": _digest(
            list(contract.allowed_entrypoints)
        ),
        "invocation_contracts_sha256": _digest(
            dict(contract.invocation_contracts)
        ),
        "worker_requirement_sha256": _digest(
            dict(contract.worker_requirement)
        ),
        "runtime_sha256": _digest(
            {
                "runtime": manifest_mapping["runtime"],
                "runtime_permissions": dict(contract.runtime_permissions),
            }
        ),
        "scheduling_sha256": _digest(dict(contract.scheduling)),
        "project_full_auto_allowed": bool(contract.project_full_auto_allowed),
        "trust_source": version.trust_source.value,
        "install_root_metadata_json": metadata,
        "install_root_metadata_sha256": _digest(metadata),
        "installed_by_actor_id": str(actor_id),
    }
    return package, persisted_version


def _version_contract(
    version: PluginVersionRecord,
) -> tuple[AutomationPluginManifestV2, SimpleNamespace]:
    if version.runtime_model is PluginRuntimeModel.SERVICE_V2:
        manifest_v2 = AutomationPluginManifestV2.from_mapping(version.manifest)
        projected = ServiceV2ProjectContract.from_manifest(manifest_v2)
        return manifest_v2, SimpleNamespace(
            allowed_entrypoints=projected.allowed_entrypoints,
            default_entrypoints=projected.default_entrypoints,
            invocation_contracts=projected.invocation_contracts,
            config_schema=manifest_v2.config_schema,
            account_roles=projected.account_roles,
            resource_roles=projected.resource_roles,
            tool_contract=projected.tool_contract,
            worker_requirement={
                "required": False,
                "interactive_session": False,
                "supported_os": [],
                "queue_deadline_seconds": 60,
            },
            runtime=manifest_v2.runtime,
            runtime_permissions=projected.runtime_permissions,
            scheduling=projected.scheduling,
            project_full_auto_allowed=True,
        )
    raise PluginPackageError(
        "only service-v2 plugin versions are supported",
        code="PLUGIN_RUNTIME_MODEL_INVALID",
    )


def _installation_payload_sha256(
    *,
    package_sha256: str,
    instance_name: str,
) -> str:
    return _digest(
        {
            "instance_name": str(instance_name),
            "package_sha256": str(package_sha256),
        }
    )


def _configuration_contract_witness(
    contract: Any,
    *,
    runtime_model: PluginRuntimeModel,
) -> dict[str, Any]:
    """Forward the already-verified package contract to persistence.

    The shared repository compares hashes of this narrow witness with the
    immutable installed-version row.  It must never reparse a browser- or
    database-supplied manifest while saving configuration.
    """

    return {
        "runtime_model": runtime_model.value,
        "allowed_entrypoints": list(contract.allowed_entrypoints),
        "invocation_contracts": _json_plain(dict(contract.invocation_contracts)),
        "scheduling": _json_plain(dict(contract.scheduling)),
    }


def _transient_entry(
    automation_id: str,
    manifest: Any,
) -> SimpleNamespace:
    return SimpleNamespace(
        automation_id=automation_id,
        invocation_contracts=manifest.invocation_contracts,
        allowed_entrypoints=manifest.allowed_entrypoints,
        config_schema=manifest.config_schema,
        account_roles=manifest.account_roles,
        resource_roles=manifest.resource_roles,
        tool_contract=manifest.tool_contract,
        action_id=f"automation.{automation_id}.run",
    )


def _compile_upgrade_invocations(
    automation_id: str, contract: Any, config: Mapping[str, Any],
    accounts: Mapping[str, Any], resources: Mapping[str, Any], sources: Sequence[str],
    *, runtime_model: PluginRuntimeModel,
) -> dict[str, dict[str, Any]]:
    transient = _transient_entry(automation_id, contract)
    result = {}
    for source in sources:
        compiled = compile_instance_arguments(
            transient, config=dict(config), account_bindings=accounts,
            resource_bindings=resources, entrypoint=source, resolve_dynamic=False,
        )
        result[source] = {
            "arguments": copy.deepcopy(dict(compiled.arguments)),
            "dynamic_resolvers": copy.deepcopy(dict(compiled.unresolved_dynamic_resolvers)),
        }
        if runtime_model is PluginRuntimeModel.SERVICE_V2:
            invocation = contract.invocation_contracts[source]
            governance = invocation.get("governance")
            if not isinstance(governance, Mapping):
                raise PluginConflictError("service-v2 invocation governance is unavailable")
            result[source]["governance"] = copy.deepcopy(dict(governance))
            result[source]["target"] = {
                "service": str(invocation.get("service") or ""),
                "operation": str(invocation.get("operation") or ""),
                "contribution_id": source,
                "contribution_kind": str(invocation.get("contribution_kind") or ""),
            }
    return result


class MySQLAutomationPluginRepositoryAdapter(AutomationPluginRepositoryPort):
    """Translate domain records to one explicit orchestration Unit of Work."""

    def __init__(
        self,
        orchestration_repository: Any,
        *,
        release_hold_provider: Any | None = None,
    ) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise TypeError("orchestration_repository must expose unit_of_work()")
        self._orchestration = orchestration_repository
        self._catalog = MySQLAutomationPluginCatalogRepositoryAdapter(
            orchestration_repository
        )
        self._configuration = MySQLAutomationProjectConfigurationReadAdapter(
            orchestration_repository
        )
        # A missing provider is deliberately held.  Lifecycle callers must
        # explicitly inject the release marker authority.
        self._release_hold_provider = release_hold_provider or (lambda: True)

    def get_package_version(
        self,
        plugin_id: str,
        version: str,
    ) -> PluginVersionRecord | None:
        return self._catalog.get_package_version(plugin_id, version)

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        return self._catalog.get_instance(automation_id)

    def list_instance_ids(self) -> Sequence[str]:
        return self._catalog.list_instance_ids()

    def list_instance_ids_for_module(self, module: str) -> Sequence[str]:
        return self._catalog.list_instance_ids_for_module(module)

    def prefetch_catalog_instances(self, identities: Sequence[str]) -> None:
        self._catalog.prefetch_catalog_instances(identities)

    def catalog_read_scope(self):
        return self._catalog.catalog_read_scope()

    def list_instances(self) -> Sequence[PluginInstanceRecord]:
        return self._catalog.list_instances()

    def plugin_settings_history(self, automation_id: str) -> Sequence[Mapping[str, Any]]:
        with self._orchestration.unit_of_work() as uow:
            rows = uow.automation_plugins.list_generation_rows(automation_id)
            uow.commit()
        return rows

    def plugin_settings_snapshot(self, automation_id: str, generation: int) -> Mapping[str, Any]:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.get_generation_row(automation_id, generation)
            uow.commit()
        if row is None:
            raise PluginConflictError("settings generation does not exist", code="PLUGIN_SETTINGS_HISTORY_MISSING")
        return row

    def continue_data_source(self, source_id: str, *, producer_instance_id: str,
                             expected_revision: int, expected_producer_instance_id: str | None,
                             request_id: str) -> Mapping[str, Any]:
        from shared.data_sources import DataSourceRepository, DataSourceError
        from shared.plugin_management import management_for

        with self._orchestration.unit_of_work() as uow:
            sources = DataSourceRepository(uow.connection)
            source = sources.get(source_id, for_update=True)
            target = uow.automation_plugins.get_project(producer_instance_id, for_update=True)
            if target is None:
                raise DataSourceError("SOURCE_PRODUCER_NOT_FOUND")
            if expected_producer_instance_id:
                previous = uow.automation_plugins.get_project(expected_producer_instance_id, for_update=True)
                if previous and previous.get("state") not in {"DISABLED", "INSTALLED", "UNINSTALLING"}:
                    raise DataSourceError("SOURCE_PREVIOUS_PRODUCER_MUST_BE_DISABLED")
            version = uow.automation_plugins.get_version(target["plugin_id"], target["plugin_version"])
            if version is None:
                raise DataSourceError("SOURCE_PRODUCER_PACKAGE_UNAVAILABLE")
            metadata = management_for(version["manifest_json"].get("management"))
            if metadata["module"] != source["module"] or metadata["dataset"] != source["dataset"] or metadata["version"] != source["contract_version"]:
                raise DataSourceError("SOURCE_DATASET_INCOMPATIBLE")
            config = uow.automation_plugins.get_project_config(producer_instance_id, for_update=True)
            bindings = config.get("account_bindings_json") if config else None
            if not isinstance(bindings, Mapping):
                raise DataSourceError("SOURCE_ACCOUNT_BINDINGS_MISSING")
            accounts = tuple(item for value in bindings.values() for item in (value if isinstance(value, list) else [value]))
            identity = sources.verify_aliases_source(source_id, account_ids=accounts)
            generation = target.get("committed_generation")
            if not generation or generation != target["target_generation"]:
                raise DataSourceError("SOURCE_PRODUCER_GENERATION_NOT_READY")
            result = sources.switch_producer(
                source_id, expected_revision=expected_revision,
                expected_producer_instance_id=expected_producer_instance_id,
                producer_instance_id=producer_instance_id, producer_generation=int(generation),
                identity=identity, request_id=request_id,
            )
            uow.commit()
        return result

    def get_state_change_witness(
        self,
        automation_id: str,
        *,
        request_id: str,
    ) -> Mapping[str, object] | None:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.get_project_state_change_witness(
                automation_id,
                request_id=request_id,
            )
            uow.commit()
        if row is None:
            return None
        metadata = row.get("metadata_json")
        state_change_context = (
            metadata.get("state_change_context")
            if isinstance(metadata, Mapping)
            else None
        )
        request_payload = (
            {
                "enabled": metadata.get("enabled"),
                "expected_record_version": metadata.get("from_record_version"),
                "state_change_context": dict(state_change_context),
            }
            if isinstance(metadata, Mapping)
            and isinstance(state_change_context, Mapping)
            else None
        )
        request_payload_sha256 = (
            hashlib.sha256(canonical_json_bytes(request_payload)).hexdigest()
            if request_payload is not None
            else None
        )
        if (
            row.get("event_type") != "PLUGIN_STATE_CHANGED"
            or not isinstance(metadata, Mapping)
            or type(metadata.get("enabled")) is not bool
            or type(metadata.get("from_record_version")) is not int
            or type(metadata.get("to_record_version")) is not int
            or metadata["to_record_version"] != metadata["from_record_version"] + 1
            or not isinstance(state_change_context, Mapping)
            or metadata.get("request_payload_sha256") != request_payload_sha256
            or not str(row.get("actor_id") or "").strip()
            or not str(row.get("actor_role") or "").strip()
        ):
            raise PluginConflictError(
                "plugin state-change audit witness is invalid",
                code="PLUGIN_STATE_AUDIT_INVALID",
            )
        return {
            "enabled": metadata["enabled"],
            "expected_record_version": metadata["from_record_version"],
            "actor_id": str(row["actor_id"]),
            "actor_role": str(row["actor_role"]),
            "state_change_context": copy.deepcopy(dict(state_change_context)),
        }

    def claim_service_v2_install_enable_base(
        self,
        automation_id: str,
        *,
        root_request_id: str,
        install_payload_sha256: str,
        configuration_request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> int:
        with self._orchestration.unit_of_work() as uow:
            base_record_version = (
                uow.automation_plugins.claim_service_v2_install_enable_base(
                    automation_id,
                    root_request_id=root_request_id,
                    install_payload_sha256=install_payload_sha256,
                    configuration_request_id=configuration_request_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                )
            )
            uow.commit()
        if (
            isinstance(base_record_version, bool)
            or not isinstance(base_record_version, int)
            or base_record_version < 1
        ):
            raise PluginConflictError(
                "service-v2 install enable claim returned an invalid version",
                code="PLUGIN_INSTALL_PROGRESS_CONFLICT",
            )
        return base_record_version

    def permanently_clear_plugin_documents(
        self,
        automation_id: str,
        *,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> Mapping[str, Any]:
        with self._orchestration.unit_of_work() as uow:
            result = uow.automation_plugins.permanently_clear_plugin_documents(
                automation_id,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason,
            )
            uow.commit()
        return result

    @staticmethod
    def _register(
        low_level: Any,
        version: PluginVersionRecord,
        *,
        actor_id: str,
        actor_role: str | None = None,
        request_id: str | None = None,
    ) -> None:
        package, persisted_version = _registration_rows(version, actor_id=actor_id)
        register = low_level.register_package_version
        if "request_id" not in inspect.signature(register).parameters:
            # Narrow in-memory compatibility doubles predate package audit
            # rows. Production's shared repository always accepts the audit
            # context; do not make a test-only port look like a false failure.
            register(package=package, version=persisted_version)
            return
        register(
            package=package,
            version=persisted_version,
            request_id=request_id,
            actor_id=actor_id if request_id is not None else None,
            actor_role=actor_role,
        )

    def install_instance(
        self,
        version: PluginVersionRecord,
        *,
        instance_name: str,
        actor_id: str,
        actor_role: str,
        request_id: str,
        install_payload_sha256: str | None = None,
    ) -> PluginInstanceRecord:
        manifest, contract = _version_contract(version)
        automation_id = str(uuid.uuid4())
        payload_sha256 = (
            str(install_payload_sha256 or "").strip().lower()
            or _installation_payload_sha256(
                package_sha256=version.package_sha256,
                instance_name=instance_name,
            )
        )
        if not _SHA256_RE.fullmatch(payload_sha256):
            raise PluginConflictError("plugin installation payload digest is invalid")
        with self._orchestration.unit_of_work() as uow:
            self._register(
                uow.automation_plugins,
                version,
                actor_id=actor_id,
                actor_role=actor_role,
                request_id=request_id,
            )
            row = uow.automation_plugins.install_project_instance(
                {
                    "automation_id": automation_id,
                    "plugin_id": version.plugin_id,
                    "plugin_version": version.version,
                    "display_name": instance_name,
                    "install_request_id": request_id,
                    "install_payload_sha256": payload_sha256,
                    "installed_by_actor_id": actor_id,
                    "migration_authority": False,
                }
            )
            automation_id = str(row["automation_id"])
            # The shared repository marks whether this exact request created
            # the row.  Response-loss replay may re-read package identity, but
            # first config/policy/audit writes must never replay.
            created_marker = row.get("_install_created")
            if not isinstance(created_marker, bool):
                raise PluginConflictError(
                    "plugin installation result omitted creation authority"
                )
            created = created_marker
            if created:
                config = uow.automation_plugins.initialize_project_config(
                    automation_id,
                    enabled_entrypoints=(
                        contract.default_entrypoints
                        if version.runtime_model is PluginRuntimeModel.SERVICE_V2
                        else contract.allowed_entrypoints
                    ),
                )
                policy = uow.automation_projects.ensure_default(
                    automation_id,
                    mode=_PROJECT_FULL_AUTO,
                    project_generation=1,
                    project_configuration_version=int(config["config_version"]),
                )
                uow.automation_projects.append_event(
                    {
                        "automation_id": automation_id,
                        "request_id": request_id,
                        "from_mode": None,
                        "to_mode": _PROJECT_FULL_AUTO,
                        "contract_hash": None,
                        "contract_snapshot_json": None,
                        "tool_contract_hash": None,
                        "plugin_contract_hash": None,
                        "project_configuration_version": int(config["config_version"]),
                        "project_generation": 1,
                        "actor_id": actor_id,
                        "actor_role": actor_role,
                        "actor_display_name": None,
                        "reason": "PLUGIN_INSTANCE_INSTALLED",
                        "comment": None,
                        "correlation_id": request_id,
                    }
                )
                record_install_event = getattr(
                    uow.automation_plugins,
                    "record_project_install_lifecycle_event",
                    None,
                )
                if callable(record_install_event):
                    record_install_event(
                        automation_id,
                        request_id=request_id,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        enabled_entrypoints=tuple(config.get("enabled_entrypoints") or ()),
                        project_configuration_version=int(config["config_version"]),
                        policy_mode=_PROJECT_FULL_AUTO,
                    )
                if str(policy.get("mode") or "") != _PROJECT_FULL_AUTO:
                    raise PluginConflictError("new plugin instance policy did not default to full auto")
            uow.commit()
        persisted = self.get_instance(automation_id)
        if persisted is None:
            raise PluginConflictError("installed plugin instance disappeared")
        return persisted

    def upgrade_instance(
        self,
        automation_id: str,
        version: PluginVersionRecord,
        *,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_current_version: str,
        expected_record_version: int,
        prepared_configuration_request_id: str | None = None,
    ) -> PluginInstanceRecord:
        with self._orchestration.unit_of_work() as uow:
            self._stage_instance_upgrade(
                uow,
                automation_id, version, actor_id=actor_id, actor_role=actor_role,
                request_id=request_id, expected_current_version=expected_current_version,
                expected_record_version=expected_record_version,
                prepared_configuration_request_id=prepared_configuration_request_id,
            )
            uow.commit()
        persisted = self.get_instance(automation_id)
        if persisted is None:
            raise PluginConflictError(
                "automation project disappeared after plugin upgrade staging",
                code="PLUGIN_INSTANCE_NOT_FOUND",
            )
        return persisted

    def _stage_instance_upgrade(
        self,
        uow: Any,
        automation_id: str,
        version: PluginVersionRecord,
        *,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_current_version: str,
        expected_record_version: int,
        prepared_configuration_request_id: str | None = None,
    ) -> None:
        automation_id = str(automation_id or "").strip()
        actor_id = str(actor_id or "").strip()
        actor_role = str(actor_role or "").strip()
        request_id = str(request_id or "").strip()
        expected_current_version = str(expected_current_version or "").strip()
        if not automation_id or not actor_id or actor_role != "super_admin":
            raise PluginConflictError(
                "plugin upgrade requires an authenticated super administrator",
                code="PLUGIN_UPGRADE_FORBIDDEN",
            )
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginConflictError(
                "plugin upgrade request_id must be UUID",
                code="PLUGIN_UPGRADE_REQUEST_INVALID",
            ) from exc
        if (
            isinstance(expected_record_version, bool)
            or not isinstance(expected_record_version, int)
            or expected_record_version <= 0
        ):
            raise PluginConflictError(
                "plugin upgrade expected_record_version is invalid",
                code="PLUGIN_INSTANCE_VERSION_CONFLICT",
            )
        try:
            current_key = tuple(int(part) for part in expected_current_version.split("."))
            target_key = tuple(int(part) for part in version.version.split("."))
        except ValueError as exc:
            raise PluginConflictError(
                "plugin upgrade versions must be semantic versions",
                code="PLUGIN_UPGRADE_VERSION_INVALID",
            ) from exc
        if len(current_key) != 3 or len(target_key) != 3:
            raise PluginConflictError(
                "plugin upgrade versions must have three components",
                code="PLUGIN_UPGRADE_VERSION_INVALID",
            )
        restoring_history = target_key < current_key

        manifest, contract = _version_contract(version)
        if manifest.plugin_id != version.plugin_id or manifest.version != version.version:
            raise PluginPackageError("upgrade package identity differs from its manifest")
        self._register(
            uow.automation_plugins,
            version,
            actor_id=actor_id,
            actor_role=actor_role,
            request_id=request_id,
        )
        project = uow.automation_plugins.get_project(
            automation_id,
            for_update=True,
        )
        config = uow.automation_plugins.get_project_config(
            automation_id,
            for_update=True,
        )
        if project is None or config is None:
            raise PluginConflictError(
                "automation project or configuration does not exist",
                code="PLUGIN_INSTANCE_NOT_FOUND",
            )
        if str(project.get("plugin_id") or "") != version.plugin_id:
            raise PluginConflictError(
                "an upgrade cannot change the instance plugin_id",
                code="PLUGIN_UPGRADE_PLUGIN_ID_CONFLICT",
            )
        if restoring_history:
            require_committed_history(
                uow.automation_plugins, automation_id, project, version,
            )

        config_json = config.get("config_json")
        account_bindings = config.get("account_bindings_json")
        resource_bindings = config.get("resource_bindings_json")
        enabled_entrypoints = config.get("enabled_entrypoints_json")
        schedule = config.get("desired_schedule_json")
        compiled_before = config.get("compiled_invocations_json")
        if (
            not isinstance(config_json, Mapping)
            or not isinstance(account_bindings, Mapping)
            or not isinstance(resource_bindings, Mapping)
            or not isinstance(enabled_entrypoints, list)
            or not isinstance(schedule, Mapping)
            or not isinstance(compiled_before, Mapping)
        ):
            raise PluginConflictError(
                "automation project configuration is not closed",
                code="PLUGIN_UPGRADE_CONFIGURATION_INCOMPATIBLE",
            )
        try:
            validate_schema_instance(
                f"automation.{automation_id}.upgrade_config",
                dict(config_json),
                contract.config_schema,
            )
            accounts = _closed_bindings(
                account_bindings,
                contract.account_roles,
                kind="account",
            )
            resources = _closed_bindings(
                resource_bindings,
                contract.resource_roles,
                kind="resource",
            )
            normalized_schedule = normalize_project_schedule(
                schedule,
                contract.scheduling,
            )
            sources = tuple(str(item or "").strip() for item in enabled_entrypoints)
            if (
                any(not source for source in sources)
                or len(sources) != len(set(sources))
                or not set(sources) <= set(contract.allowed_entrypoints)
            ):
                raise PluginConflictError(
                    "enabled entrypoints differ from the upgrade contract"
                )
            compiled_after = _compile_upgrade_invocations(
                automation_id, contract, config_json, accounts, resources, sources,
                runtime_model=version.runtime_model,
            )
            if canonical_json_bytes(normalized_schedule) != canonical_json_bytes(schedule):
                raise PluginConflictError("saved schedule needs reconfiguration")
            if (version.runtime_model is PluginRuntimeModel.SERVICE_V2
                    and prepared_configuration_request_id is None
                    and (compiled_after != compiled_before
                         or project.get("plugin_version") == version.version)):
                prepared = self._prepare_optional_console_upgrade(
                    uow, automation_id, version, contract, project, config,
                    compiled_after=compiled_after, actor_id=actor_id, actor_role=actor_role,
                    request_id=request_id, expected_current_version=expected_current_version,
                    expected_record_version=expected_record_version,
                )
                if prepared is not None:
                    prepared_configuration_request_id, expected_record_version = prepared
                    config = uow.automation_plugins.get_project_config(automation_id, for_update=True)
                    compiled_before = config["compiled_invocations_json"]
            if canonical_json_bytes(compiled_after) != canonical_json_bytes(compiled_before):
                raise PluginConflictError("saved invocation templates need reconfiguration")
        except PluginConflictError as exc:
            raise PluginConflictError(
                "plugin upgrade is incompatible with the saved project configuration",
                code="PLUGIN_UPGRADE_CONFIGURATION_INCOMPATIBLE",
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise PluginConflictError(
                "plugin upgrade is incompatible with the saved project configuration",
                code="PLUGIN_UPGRADE_CONFIGURATION_INCOMPATIBLE",
            ) from exc

        stage_arguments: dict[str, Any] = {
            "plugin_id": version.plugin_id,
            "from_version": expected_current_version,
            "to_version": version.version,
            "package_sha256": version.package_sha256,
            "request_id": request_id,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "expected_record_version": expected_record_version,
        }
        stage = uow.automation_plugins.stage_project_upgrade
        if "audit_version_identities" in inspect.signature(stage).parameters:
            stage_arguments["audit_version_identities"] = True
        if prepared_configuration_request_id is not None:
            stage_arguments["prepared_configuration_request_id"] = (
                prepared_configuration_request_id
            )
        staged = stage(
            automation_id,
            **stage_arguments,
        )
        if staged.pop("_upgrade_staged_created", False):
            policy = uow.automation_projects.get_policy(
                automation_id,
                for_update=True,
            )
            if policy is None:
                raise PluginConflictError(
                    "automation project policy does not exist",
                    code="PLUGIN_POLICY_NOT_FOUND",
                )
            generation = int(staged["target_generation"])
            config_version = int(config.get("config_version") or 0)
            durable_mode = str(policy.get("mode") or "")
            uow.automation_projects.update_policy(
                automation_id,
                expected_version=int(policy.get("version") or 0),
                mode=durable_mode,
                contract_hash=None,
                contract_snapshot=None,
                tool_contract_hash=None,
                plugin_contract_hash=None,
                project_generation=generation,
                project_configuration_version=config_version,
                actor_id=actor_id,
                actor_role=actor_role,
                actor_display_name=None,
                comment=None,
            )
            uow.automation_projects.append_event(
                {
                    "automation_id": automation_id,
                    "request_id": request_id,
                    "from_mode": durable_mode,
                    "to_mode": durable_mode,
                    "contract_hash": None,
                    "contract_snapshot_json": None,
                    "tool_contract_hash": None,
                    "plugin_contract_hash": None,
                    "project_configuration_version": config_version,
                    "project_generation": generation,
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                    "actor_display_name": None,
                    "reason": (
                        "PLUGIN_HISTORICAL_VERSION_RESTORED"
                        if restoring_history else "PLUGIN_VERSION_CHANGED"
                    ),
                    "comment": None,
                    "correlation_id": request_id,
                }
            )
            uow.automation_projects.invalidate_pending_approvals_and_wake_runs(
                automation_id,
                event_repository=uow.events,
            )
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"boyi:automation-plugin-upgrade:{request_id}",
                )
            )
            uow.events.append_with_outbox(
                {
                    "event_id": event_id,
                    "event_type": "automation_plugin.upgrade_staged",
                    "schema_version": 1,
                    "source_system": "agent",
                    "source_event_id": f"plugin-upgrade:{request_id}",
                    "entity_type": "automation_project",
                    "entity_id": automation_id,
                    "correlation_id": request_id,
                    "payload": {
                        "automation_id": automation_id,
                        "plugin_id": version.plugin_id,
                        "from_version": expected_current_version,
                        "to_version": version.version,
                        "target_generation": generation,
                        "operation": "rollback" if restoring_history else "upgrade",
                    },
                },
                (
                    {
                        "consumer_name": "orchestration.audit",
                        "topic": "automation_plugin.upgrade_staged",
                        "partition_key": automation_id,
                        "max_attempts": 10,
                    },
                ),
            )

    def _prepare_optional_console_upgrade(
        self, uow: Any, automation_id: str, version: PluginVersionRecord,
        contract: Any, project: Mapping[str, Any], config: Mapping[str, Any], *,
        compiled_after: Mapping[str, Any], actor_id: str, actor_role: str,
        request_id: str, expected_current_version: str, expected_record_version: int,
    ) -> tuple[str, int] | None:
        """Prepare only additive optional Console inputs in the upgrade UoW."""
        previous_row = uow.automation_plugins.get_version(version.plugin_id, expected_current_version)
        if previous_row is None:
            raise PluginConflictError("installed upgrade source is unavailable")
        previous_version = _version_from_row(previous_row)
        if previous_version.runtime_model is not PluginRuntimeModel.SERVICE_V2:
            raise PluginConflictError("optional Console upgrade requires service-v2 contracts")
        previous_manifest, previous_contract = _version_contract(previous_version)
        if previous_manifest.manifest_sha256 != previous_version.manifest_sha256:
            raise PluginConflictError("installed upgrade source manifest changed")
        previous_witness = _configuration_contract_witness(previous_contract, runtime_model=version.runtime_model)
        target_witness = _configuration_contract_witness(contract, runtime_model=version.runtime_model)
        # A completed ordinary upgrade has no migration to replay.
        try:
            additions = optional_console_input_additions(previous_witness, target_witness)
        except ValueError:
            if compiled_after == config["compiled_invocations_json"]:
                return None
            raise
        sources = config["enabled_entrypoints_json"]
        if not set(sources).intersection(additions):
            return None
        old_compiled = _compile_upgrade_invocations(
            automation_id, previous_contract, config["config_json"],
            config["account_bindings_json"], config["resource_bindings_json"], sources,
            runtime_model=previous_version.runtime_model,
        )
        request = {
            "config": config["config_json"], "account_bindings": config["account_bindings_json"],
            "resource_bindings": config["resource_bindings_json"], "enabled_entrypoints": sources,
            "schedule": config["desired_schedule_json"], "compiled_invocations": compiled_after,
        }
        validate_optional_console_upgrade_configuration(
            previous_witness, target_witness, {**config, "compiled_invocations_json": old_compiled}, request,
        )
        prepare_request = str(uuid.uuid5(uuid.UUID(request_id), "optional-console-upgrade-configuration"))
        # The normal stage idempotency check still verifies the original UUID,
        # actor, package and CAS payload after a lost response.
        if project.get("plugin_version") == version.version:
            return prepare_request, expected_record_version + 1
        if (project.get("plugin_version") != expected_current_version
                or project.get("record_version") != expected_record_version):
            raise PluginConflictError("automation project changed before upgrade configuration")
        if canonical_json_bytes(config["compiled_invocations_json"]) != canonical_json_bytes(old_compiled):
            raise PluginConflictError("saved invocation template differs from its installed contract")
        uow.automation_plugins.save_project_config(
            automation_id, **request,
            contract_witness=target_witness,
            actor_id=actor_id, actor_role=actor_role, request_id=prepare_request,
            expected_project_configuration_version=int(config["config_version"]),
            optional_console_upgrade={
                "target": {"from_version": expected_current_version, "to_version": version.version,
                           "package_sha256": version.package_sha256},
                "previous_contract_witness": previous_witness,
            },
        )
        return prepare_request, expected_record_version + 1

    def set_enabled(
        self,
        automation_id: str,
        *,
        enabled: bool,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_record_version: int,
        state_change_context: Mapping[str, object] | None = None,
    ) -> PluginInstanceRecord:
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.set_project_enabled_with_audit(
                automation_id,
                enabled=enabled,
                expected_record_version=expected_record_version,
                actor_id=actor_id,
                actor_role=actor_role,
                request_id=request_id,
                state_change_context=state_change_context,
            )
            uow.commit()
        persisted = self.get_instance(automation_id)
        if persisted is None:
            raise PluginConflictError("plugin instance disappeared after state change")
        return persisted

    def prepare_hard_uninstall(
        self,
        automation_id: str,
        *,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_current_version: str,
        expected_record_version: int,
    ) -> HardUninstallPreparation:
        purge_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"boyi:purge:{request_id}"))
        with self._orchestration.unit_of_work() as uow:
            project = uow.automation_plugins.get_project(automation_id, for_update=True)
            persisted_record_version = (
                project.get("record_version") if isinstance(project, Mapping) else None
            )
            if (
                not isinstance(project, Mapping)
                or str(project.get("plugin_version") or "") != expected_current_version
                or isinstance(expected_record_version, bool)
                or not isinstance(expected_record_version, int)
                or isinstance(persisted_record_version, bool)
                or not isinstance(persisted_record_version, int)
                or persisted_record_version != expected_record_version
            ):
                raise PluginConflictError(
                    "plugin instance changed before uninstall",
                    code="PLUGIN_INSTANCE_VERSION_CONFLICT",
                )
            journal = uow.automation_plugins.prepare_instance_purge(
                automation_id,
                purge_id=purge_id,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
                cleanup_scope="PACKAGE",
            )
            retain_documents = getattr(
                uow.automation_plugins,
                "retain_plugin_documents_for_uninstall",
                None,
            )
            if callable(retain_documents):
                retain_documents(
                    automation_id,
                    request_id=request_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                )
            uow.commit()
        return self._preparation_from_journal(journal)

    def _preparation_from_journal(
        self,
        journal: Mapping[str, Any],
    ) -> HardUninstallPreparation:
        automation_id = str(journal.get("automation_id") or "")
        # Preparation has already revoked the instance and drained its
        # generation. Runtime catalog validation must stay strict; cleanup uses
        # the exact snapshot persisted by that transaction instead.
        snapshot = journal.get("instance_snapshot_json")
        if not isinstance(snapshot, Mapping):
            raise PluginConflictError("purge journal has no instance snapshot")
        version = self.get_package_version(
            str(journal.get("plugin_id") or ""),
            str(journal.get("plugin_version") or ""),
        )
        if version is None or version.package_sha256 != str(journal.get("package_sha256") or ""):
            raise PluginConflictError("purge journal plugin version disappeared or changed")
        from agent.automation_plugins.models import (
            PluginProjectState,
            RuntimeReconcileState,
        )

        instance = PluginInstanceRecord(
            automation_id=automation_id,
            display_name=str(snapshot.get("display_name") or ""),
            plugin_id=str(journal.get("plugin_id") or ""),
            state=PluginProjectState.UNINSTALLING,
            active_version=version,
            record_version=int(snapshot["record_version"]),
            target_generation=int(snapshot["target_generation"]),
            committed_generation=snapshot.get("committed_generation"),
            reconcile_state=RuntimeReconcileState.DRAINING,
        )
        prepared_at = journal.get("created_at")
        if not isinstance(prepared_at, datetime):
            prepared_at = datetime.now(timezone.utc)
        elif prepared_at.tzinfo is None:
            prepared_at = prepared_at.replace(tzinfo=timezone.utc)
        return HardUninstallPreparation(
            purge_id=str(journal.get("purge_id") or ""),
            instance=instance,
            prepared_at=prepared_at,
            delete_shared_package=bool(journal.get("delete_shared_package")),
        )

    def get_hard_uninstall_preparation(
        self,
        *,
        automation_id: str,
        purge_id: str,
    ) -> HardUninstallPreparation | None:
        with self._orchestration.unit_of_work() as uow:
            journal = uow.automation_plugins.get_purge_journal(
                automation_id,
                purge_id,
            )
        return self._preparation_from_journal(journal) if journal is not None else None

    def reserve_hard_uninstall_finalize(
        self,
        preparation: HardUninstallPreparation,
    ) -> HardUninstallPreparation:
        with self._orchestration.unit_of_work() as uow:
            journal = uow.automation_plugins.reserve_purge_finalize(
                preparation.purge_id
            )
            uow.commit()
        return self._preparation_from_journal(journal)

    def hard_delete_application_state(
        self,
        preparation: HardUninstallPreparation,
    ) -> None:
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.hard_delete_project_application_state(
                preparation.purge_id
            )
            uow.commit()

    def complete_hard_uninstall(
        self,
        preparation: HardUninstallPreparation,
    ) -> None:
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.complete_purge(preparation.purge_id)
            uow.commit()

    def mark_purge_failed(
        self,
        preparation: HardUninstallPreparation,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.mark_purge_failed(
                preparation.purge_id,
                error_code=error_code,
                error_summary=error_summary,
            )
            uow.commit()

    def list_execution_blocks(self, automation_id: str) -> Sequence[ExecutionBlock]:
        with self._orchestration.unit_of_work() as uow:
            rows = uow.automation_plugins.list_execution_blocks(automation_id)
        return tuple(
            ExecutionBlock(
                kind=ExecutionBlockKind(str(row.get("kind") or "")),
                run_id=str(row.get("run_id") or ""),
                message=str(row.get("message") or ""),
            )
            for row in rows
        )


__all__ = ["MySQLAutomationPluginRepositoryAdapter"]
