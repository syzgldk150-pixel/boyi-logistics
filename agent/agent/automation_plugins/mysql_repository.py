"""Domain adapter over the transactional automation-plugin MySQL repository.

The shared repository deliberately exposes row-oriented operations.  This
adapter is the composition boundary used by signed-package lifecycle and
first-party bootstrap code; it never performs DDL or filesystem operations.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from agent.automation_plugins.errors import (
    PluginConflictError,
    PluginPackageError,
)
from agent.automation_plugins.code_owned_fields import (
    normalize_first_party_code_owned_config,
)
from agent.automation_plugins.invocation import compile_instance_arguments
from agent.automation_plugins.configuration import (
    _closed_bindings,
    normalize_project_schedule,
)
from agent.automation_plugins.manifest import (
    AutomationPluginManifest,
    canonical_json_bytes,
)
from agent.automation_plugins.models import (
    ExecutionBlock,
    ExecutionBlockKind,
    FirstPartyInstanceSeed,
    PluginInstanceRecord,
    PluginVersionRecord,
    WorkerCleanupRequest,
)
from agent.automation_plugins.ports import (
    AutomationPluginRepositoryPort,
    BootstrapPersistenceResult,
    HardUninstallPreparation,
)
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter,
    MySQLAutomationProjectConfigurationReadAdapter,
)
from agent.automation_plugins.storage import VERIFIED_ARCHIVE_RELATIVE
from agent.tool_registry import validate_schema_instance
from shared.automation_project_manifest import (
    FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES,
)


_MIGRATION_ACTOR_ID = "system:migration:automation-plugin-v1"
_MIGRATION_ACTOR_ROLE = "migration_authority"
_FIRST_PARTY_RELEASE_ACTOR_ID = "system:release:first-party-upgrade"
_FIRST_PARTY_RELEASE_ACTOR_ROLE = "super_admin"
_REQUIRE_EACH_RUN = "REQUIRE_EACH_RUN"
_PROJECT_FULL_AUTO = "PROJECT_FULL_AUTO"


def _digest(value: Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _semantic_version_key(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in str(value or "").split("."))
    except ValueError as exc:
        raise PluginPackageError("first-party package version is not semantic") from exc
    if len(parts) != 3 or any(part < 0 for part in parts):
        raise PluginPackageError("first-party package version is not semantic")
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
    manifest = AutomationPluginManifest.from_mapping(version.manifest)
    if (
        manifest.plugin_id != version.plugin_id
        or manifest.version != version.version
        or manifest.manifest_sha256 != version.manifest_sha256
    ):
        raise PluginPackageError("plugin version identity differs from its signed manifest")
    metadata = _installed_metadata(version)
    manifest_mapping = manifest.to_mapping()
    package = {
        "plugin_id": manifest.plugin_id,
        "display_name": manifest.name,
        "description": manifest.description,
    }
    persisted_version = {
        "version": manifest.version,
        "package_sha256": version.package_sha256,
        "manifest_sha256": version.manifest_sha256,
        "manifest_json": manifest_mapping,
        "tool_contract_sha256": _digest(manifest_mapping["tool_contract"]),
        "config_schema_sha256": _digest(manifest_mapping["config_schema"]),
        "allowed_entrypoints_sha256": _digest(
            manifest_mapping["allowed_entrypoints"]
        ),
        "invocation_contracts_sha256": _digest(
            manifest_mapping["invocation_contracts"]
        ),
        "worker_requirement_sha256": _digest(
            manifest_mapping["worker_requirement"]
        ),
        "runtime_sha256": _digest(
            {
                "runtime": manifest_mapping["runtime"],
                "runtime_permissions": manifest_mapping["runtime_permissions"],
            }
        ),
        "scheduling_sha256": _digest(manifest_mapping["scheduling"]),
        "project_full_auto_allowed": manifest.project_full_auto_allowed,
        "trust_source": version.trust_source.value,
        "install_root_metadata_json": metadata,
        "install_root_metadata_sha256": _digest(metadata),
        "installed_by_actor_id": str(actor_id),
    }
    return package, persisted_version


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


def _transient_entry(
    automation_id: str,
    manifest: AutomationPluginManifest,
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


def _legacy_project_config(
    template: Any,
    manifest: AutomationPluginManifest,
) -> dict[str, Any]:
    properties = manifest.config_schema.get("properties", {})
    config = {
        str(field): copy.deepcopy(value)
        for field, value in template.legacy_arguments.items()
        if str(field) in properties
    }
    validate_schema_instance(
        f"automation.{template.automation_id}.migration_config",
        config,
        manifest.config_schema,
    )
    return config


class MySQLAutomationPluginRepositoryAdapter(AutomationPluginRepositoryPort):
    """Translate domain records to one explicit orchestration Unit of Work."""

    def __init__(
        self,
        orchestration_repository: Any,
        *,
        migration_account_bindings: Mapping[str, Mapping[str, Any]] | None = None,
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
        self._migration_account_bindings = {
            str(project): copy.deepcopy(dict(bindings))
            for project, bindings in dict(migration_account_bindings or {}).items()
        }
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

    def list_instances(self) -> Sequence[PluginInstanceRecord]:
        return self._catalog.list_instances()

    @staticmethod
    def _register(
        low_level: Any,
        version: PluginVersionRecord,
        *,
        actor_id: str,
    ) -> None:
        package, persisted_version = _registration_rows(version, actor_id=actor_id)
        low_level.register_package_version(
            package=package,
            version=persisted_version,
        )

    def install_instance(
        self,
        version: PluginVersionRecord,
        *,
        instance_name: str,
        actor_id: str,
        actor_role: str,
        request_id: str,
    ) -> PluginInstanceRecord:
        manifest = AutomationPluginManifest.from_mapping(version.manifest)
        automation_id = str(uuid.uuid4())
        with self._orchestration.unit_of_work() as uow:
            self._register(uow.automation_plugins, version, actor_id=actor_id)
            row = uow.automation_plugins.install_project_instance(
                {
                    "automation_id": automation_id,
                    "plugin_id": version.plugin_id,
                    "plugin_version": version.version,
                    "display_name": instance_name,
                    "install_request_id": request_id,
                    "install_payload_sha256": _installation_payload_sha256(
                        package_sha256=version.package_sha256,
                        instance_name=instance_name,
                    ),
                    "installed_by_actor_id": actor_id,
                    "migration_authority": False,
                }
            )
            automation_id = str(row["automation_id"])
            config = uow.automation_plugins.initialize_project_config(
                automation_id,
                enabled_entrypoints=manifest.allowed_entrypoints,
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
    ) -> PluginInstanceRecord:
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
        if len(current_key) != 3 or len(target_key) != 3 or target_key < current_key:
            raise PluginConflictError(
                "plugin upgrade target cannot be older than the expected version",
                code="PLUGIN_UPGRADE_VERSION_INVALID",
            )

        manifest = AutomationPluginManifest.from_mapping(version.manifest)
        if manifest.plugin_id != version.plugin_id or manifest.version != version.version:
            raise PluginPackageError("upgrade package identity differs from its signed manifest")
        with self._orchestration.unit_of_work() as uow:
            self._register(uow.automation_plugins, version, actor_id=actor_id)
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
                    manifest.config_schema,
                )
                accounts = _closed_bindings(
                    account_bindings,
                    manifest.account_roles,
                    kind="account",
                )
                resources = _closed_bindings(
                    resource_bindings,
                    manifest.resource_roles,
                    kind="resource",
                )
                normalized_schedule = normalize_project_schedule(
                    schedule,
                    manifest.scheduling,
                )
                sources = tuple(str(item or "").strip() for item in enabled_entrypoints)
                if (
                    any(not source for source in sources)
                    or len(sources) != len(set(sources))
                    or not set(sources) <= set(manifest.allowed_entrypoints)
                ):
                    raise PluginConflictError(
                        "enabled entrypoints differ from the upgrade contract"
                    )
                worker_required = manifest.worker_requirement.get("required") is True
                has_device = config.get("device_id") not in (None, "")
                if worker_required != has_device:
                    raise PluginConflictError(
                        "Worker binding differs from the upgrade contract"
                    )
                transient = _transient_entry(automation_id, manifest)
                compiled_after: dict[str, dict[str, Any]] = {}
                for source in sources:
                    compiled = compile_instance_arguments(
                        transient,
                        config=dict(config_json),
                        account_bindings=accounts,
                        resource_bindings=resources,
                        entrypoint=source,
                        resolve_dynamic=False,
                    )
                    compiled_after[source] = {
                        "arguments": copy.deepcopy(dict(compiled.arguments)),
                        "dynamic_resolvers": copy.deepcopy(
                            dict(compiled.unresolved_dynamic_resolvers)
                        ),
                    }
                if (
                    canonical_json_bytes(normalized_schedule)
                    != canonical_json_bytes(schedule)
                    or canonical_json_bytes(compiled_after)
                    != canonical_json_bytes(compiled_before)
                ):
                    raise PluginConflictError(
                        "saved schedule or invocation templates need reconfiguration"
                    )
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

            staged = uow.automation_plugins.stage_project_upgrade(
                automation_id,
                plugin_id=version.plugin_id,
                from_version=expected_current_version,
                to_version=version.version,
                package_sha256=version.package_sha256,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
                expected_record_version=expected_record_version,
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
                        "reason": "PLUGIN_VERSION_CHANGED",
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
            uow.commit()
        persisted = self.get_instance(automation_id)
        if persisted is None:
            raise PluginConflictError(
                "automation project disappeared after plugin upgrade staging",
                code="PLUGIN_INSTANCE_NOT_FOUND",
            )
        return persisted

    def _prepare_first_party_upgrade_configuration(
        self,
        *,
        seed: FirstPartyInstanceSeed,
        version: PluginVersionRecord,
        release_sha: str,
        expected_current_version: str,
    ) -> tuple[str, int]:
        """Recompile preserved settings against a signed first-party target.

        A release may move interactive or planner-owned values out of durable
        project configuration.  The signed target schema remains authoritative;
        the narrow first-party normalizer only removes or injects fields owned
        by core code for a reserved instance identity.  Saving first gives the
        generic upgrade path a closed target configuration and leaves a fully
        recoverable generation if the process stops between the two commits.
        """

        manifest = AutomationPluginManifest.from_mapping(version.manifest)
        if manifest.plugin_id != seed.plugin_id or manifest.version != seed.version:
            raise PluginPackageError("first-party upgrade target identity is invalid")
        with self._orchestration.unit_of_work() as uow:
            project = uow.automation_plugins.get_project(
                seed.automation_id,
                for_update=True,
            )
            config = uow.automation_plugins.get_project_config(
                seed.automation_id,
                for_update=True,
            )
            if project is None or config is None:
                raise PluginConflictError(
                    "first-party automation project configuration is missing",
                    code="PLUGIN_INSTANCE_NOT_FOUND",
                )
            if (
                str(project.get("plugin_id") or "") != seed.plugin_id
                or str(project.get("plugin_version") or "")
                != expected_current_version
            ):
                raise PluginConflictError(
                    "first-party automation project changed before release upgrade",
                    code="PLUGIN_INSTANCE_VERSION_CONFLICT",
                )
            raw_config = config.get("config_json")
            account_bindings = config.get("account_bindings_json")
            resource_bindings = config.get("resource_bindings_json")
            enabled_entrypoints = config.get("enabled_entrypoints_json")
            schedule = config.get("desired_schedule_json")
            compiled_before = config.get("compiled_invocations_json")
            if (
                not isinstance(raw_config, Mapping)
                or not isinstance(account_bindings, Mapping)
                or not isinstance(resource_bindings, Mapping)
                or not isinstance(enabled_entrypoints, list)
                or not isinstance(schedule, Mapping)
                or not isinstance(compiled_before, Mapping)
            ):
                raise PluginConflictError(
                    "first-party automation project configuration is not closed",
                    code="PLUGIN_UPGRADE_CONFIGURATION_INCOMPATIBLE",
                )
            normalized_config = normalize_first_party_code_owned_config(
                automation_id=seed.automation_id,
                plugin_id=seed.plugin_id,
                trust_source=version.trust_source.value,
                config=raw_config,
            )
            try:
                validate_schema_instance(
                    f"automation.{seed.automation_id}.first_party_upgrade_config",
                    normalized_config,
                    manifest.config_schema,
                )
                accounts = _closed_bindings(
                    account_bindings,
                    manifest.account_roles,
                    kind="account",
                )
                resources = _closed_bindings(
                    resource_bindings,
                    manifest.resource_roles,
                    kind="resource",
                )
                normalized_schedule = normalize_project_schedule(
                    schedule,
                    manifest.scheduling,
                )
                sources = tuple(
                    str(item or "").strip() for item in enabled_entrypoints
                )
                if (
                    any(not source for source in sources)
                    or len(sources) != len(set(sources))
                    or not set(sources) <= set(manifest.allowed_entrypoints)
                ):
                    raise PluginConflictError(
                        "enabled entrypoints differ from the release contract"
                    )
                worker_required = manifest.worker_requirement.get("required") is True
                has_device = config.get("device_id") not in (None, "")
                if worker_required != has_device:
                    raise PluginConflictError(
                        "Worker binding differs from the release contract"
                    )
                transient = _transient_entry(seed.automation_id, manifest)
                compiled_after: dict[str, dict[str, Any]] = {}
                for source in sources:
                    compiled = compile_instance_arguments(
                        transient,
                        config=normalized_config,
                        account_bindings=accounts,
                        resource_bindings=resources,
                        entrypoint=source,
                        resolve_dynamic=False,
                    )
                    compiled_after[source] = {
                        "arguments": copy.deepcopy(dict(compiled.arguments)),
                        "dynamic_resolvers": copy.deepcopy(
                            dict(compiled.unresolved_dynamic_resolvers)
                        ),
                    }
            except PluginConflictError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise PluginConflictError(
                    "first-party release is incompatible with saved project settings",
                    code="PLUGIN_UPGRADE_CONFIGURATION_INCOMPATIBLE",
                ) from exc

            needs_save = any(
                canonical_json_bytes(left) != canonical_json_bytes(right)
                for left, right in (
                    (normalized_config, raw_config),
                    (normalized_schedule, schedule),
                    (compiled_after, compiled_before),
                )
            )
            if needs_save:
                save_request_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "boyi:first-party-plugin-upgrade-config:"
                        f"{release_sha}:{seed.automation_id}:"
                        f"{expected_current_version}:{version.version}",
                    )
                )
                device_id = config.get("device_id")
                uow.automation_plugins.save_project_config(
                    seed.automation_id,
                    config=normalized_config,
                    account_bindings=accounts,
                    resource_bindings=resources,
                    enabled_entrypoints=sources,
                    schedule=normalized_schedule,
                    compiled_invocations=compiled_after,
                    device_binding=(
                        {"device_id": str(device_id)}
                        if device_id not in (None, "")
                        else None
                    ),
                    actor_id=_FIRST_PARTY_RELEASE_ACTOR_ID,
                    actor_role=_FIRST_PARTY_RELEASE_ACTOR_ROLE,
                    request_id=save_request_id,
                    expected_project_configuration_version=int(
                        config.get("config_version") or 0
                    ),
                )
                uow.automation_projects.invalidate_pending_approvals_and_wake_runs(
                    seed.automation_id,
                    event_repository=uow.events,
                )
                project = uow.automation_plugins.get_project(
                    seed.automation_id,
                    for_update=True,
                )
                if project is None:
                    raise PluginConflictError(
                        "first-party project disappeared during release upgrade",
                        code="PLUGIN_INSTANCE_NOT_FOUND",
                    )
            uow.commit()
        return (
            str(project.get("plugin_version") or ""),
            int(project.get("record_version") or 0),
        )

    def bootstrap_missing(
        self,
        versions: Sequence[PluginVersionRecord],
        instances: Sequence[FirstPartyInstanceSeed],
        *,
        release_sha: str,
    ) -> BootstrapPersistenceResult:
        by_version = {(item.plugin_id, item.version): item for item in versions}
        if len(by_version) != len(tuple(versions)):
            raise PluginPackageError("first-party bootstrap contains duplicate versions")
        created: list[str] = []
        existing: list[str] = []
        upgrades: list[
            tuple[FirstPartyInstanceSeed, PluginVersionRecord, str]
        ] = []
        with self._orchestration.unit_of_work() as uow:
            for version in versions:
                self._register(
                    uow.automation_plugins,
                    version,
                    actor_id=_MIGRATION_ACTOR_ID,
                )
            for seed in sorted(instances, key=lambda item: item.automation_id):
                version = by_version.get((seed.plugin_id, seed.version))
                if version is None:
                    raise PluginPackageError(
                        f"first-party instance references an absent package: {seed.automation_id}"
                    )
                current = uow.automation_plugins.get_project(
                    seed.automation_id,
                    for_update=True,
                )
                if current is not None:
                    if str(current.get("plugin_id") or "") != seed.plugin_id:
                        raise PluginConflictError(
                            f"first-party instance belongs to another plugin: {seed.automation_id}"
                        )
                    current_version = str(current.get("plugin_version") or "")
                    current_key = _semantic_version_key(current_version)
                    target_key = _semantic_version_key(seed.version)
                    if current_key > target_key:
                        raise PluginConflictError(
                            "first-party release cannot downgrade an installed instance",
                            code="PLUGIN_UPGRADE_VERSION_INVALID",
                        )
                    if current_key < target_key:
                        upgrades.append((seed, version, current_version))
                    existing.append(seed.automation_id)
                    continue
                manifest = AutomationPluginManifest.from_mapping(version.manifest)
                template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES.get(
                    seed.automation_id
                )
                if template is None or template.tool_name != seed.plugin_id:
                    raise PluginPackageError(
                        f"first-party migration template is absent: {seed.automation_id}"
                    )
                install_request_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"boyi:first-party-plugin:{release_sha}:{seed.automation_id}",
                    )
                )
                uow.automation_plugins.install_project_instance(
                    {
                        "automation_id": seed.automation_id,
                        "plugin_id": seed.plugin_id,
                        "plugin_version": seed.version,
                        "display_name": seed.display_name,
                        "install_request_id": install_request_id,
                        "install_payload_sha256": _installation_payload_sha256(
                            package_sha256=version.package_sha256,
                            instance_name=seed.display_name,
                        ),
                        "installed_by_actor_id": _MIGRATION_ACTOR_ID,
                        "migration_authority": True,
                    }
                )
                initial = uow.automation_plugins.initialize_project_config(
                    seed.automation_id,
                    enabled_entrypoints=seed.allowed_entrypoints,
                )
                config = _legacy_project_config(template, manifest)
                supplied_accounts = dict(template.legacy_account_bindings)
                supplied_accounts.update(
                    self._migration_account_bindings.get(seed.automation_id, {})
                )
                declared_roles = {
                    str(role.get("role") or ""): role
                    for role in manifest.account_roles
                }
                unknown_roles = set(supplied_accounts) - set(declared_roles)
                required_roles = {
                    name
                    for name, role in declared_roles.items()
                    if role.get("required") is True
                }
                if unknown_roles or not required_roles <= set(supplied_accounts):
                    raise PluginPackageError(
                        f"first-party account bindings are incomplete: {seed.automation_id}"
                    )
                transient = _transient_entry(seed.automation_id, manifest)
                compiled: dict[str, dict[str, Any]] = {}
                normalized_accounts: Mapping[str, Any] | None = None
                normalized_resources: Mapping[str, str] | None = None
                for entrypoint in seed.allowed_entrypoints:
                    item = compile_instance_arguments(
                        transient,
                        config=config,
                        account_bindings=supplied_accounts,
                        resource_bindings=dict(template.resource_bindings),
                        entrypoint=entrypoint,
                        resolve_dynamic=False,
                    )
                    if normalized_accounts is None:
                        normalized_accounts = item.account_bindings
                    elif dict(normalized_accounts) != dict(item.account_bindings):
                        raise PluginPackageError(
                            "first-party account compilation changed by entrypoint"
                        )
                    if normalized_resources is None:
                        normalized_resources = item.resource_bindings
                    elif dict(normalized_resources) != dict(item.resource_bindings):
                        raise PluginPackageError(
                            "first-party resource compilation changed by entrypoint"
                        )
                    compiled[entrypoint] = {
                        "arguments": copy.deepcopy(dict(item.arguments)),
                        "dynamic_resolvers": copy.deepcopy(
                            dict(item.unresolved_dynamic_resolvers)
                        ),
                    }
                current_config = uow.automation_plugins.get_project_config(
                    seed.automation_id,
                    for_update=True,
                )
                if current_config is None:
                    raise PluginConflictError("first-party config disappeared")
                schedule = dict(
                    current_config.get("committed_schedule")
                    or {"kind": "none", "times": [], "enabled": False}
                )
                save_request_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"boyi:first-party-plugin-config:{release_sha}:{seed.automation_id}",
                    )
                )
                uow.automation_plugins.save_project_config(
                    seed.automation_id,
                    config=config,
                    account_bindings=dict(normalized_accounts or {}),
                    resource_bindings=dict(normalized_resources or {}),
                    enabled_entrypoints=seed.allowed_entrypoints,
                    schedule=schedule,
                    compiled_invocations=compiled,
                    device_binding=None,
                    actor_id=_MIGRATION_ACTOR_ID,
                    actor_role=_MIGRATION_ACTOR_ROLE,
                    request_id=save_request_id,
                    expected_project_configuration_version=int(
                        initial["config_version"]
                    ),
                )
                project = uow.automation_plugins.get_project(
                    seed.automation_id,
                    for_update=True,
                )
                if project is None:
                    raise PluginConflictError("first-party project disappeared")
                uow.automation_plugins.set_project_enabled(
                    seed.automation_id,
                    enabled=True,
                    expected_record_version=int(project["record_version"]),
                )
                created.append(seed.automation_id)
            uow.commit()

        # Package registration and missing-instance creation are committed
        # before upgrades so each upgrade can retain its own idempotent audit
        # boundary.  A crash after configuration preparation is safe: the old
        # signed package remains active, and the same release request resumes
        # staging on the next startup.
        for seed, version, current_version in upgrades:
            prepared_version, record_version = (
                self._prepare_first_party_upgrade_configuration(
                    seed=seed,
                    version=version,
                    release_sha=release_sha,
                    expected_current_version=current_version,
                )
            )
            if prepared_version != current_version or record_version <= 0:
                raise PluginConflictError(
                    "first-party project changed during release preparation",
                    code="PLUGIN_INSTANCE_VERSION_CONFLICT",
                )
            upgrade_request_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "boyi:first-party-plugin-upgrade:"
                    f"{release_sha}:{seed.automation_id}:"
                    f"{current_version}:{version.version}",
                )
            )
            self.upgrade_instance(
                seed.automation_id,
                version,
                actor_id=_FIRST_PARTY_RELEASE_ACTOR_ID,
                actor_role=_FIRST_PARTY_RELEASE_ACTOR_ROLE,
                request_id=upgrade_request_id,
                expected_current_version=current_version,
                expected_record_version=record_version,
            )
        return BootstrapPersistenceResult(
            created=tuple(sorted(created)),
            existing=tuple(sorted(existing)),
        )

    def set_enabled(
        self,
        automation_id: str,
        *,
        enabled: bool,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_record_version: int,
    ) -> PluginInstanceRecord:
        del actor_id, actor_role, request_id
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.set_project_enabled(
                automation_id,
                enabled=enabled,
                expected_record_version=expected_record_version,
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
        config = self._configuration.get_project_config(automation_id)
        devices = (
            [config.device_binding.device_id]
            if config is not None and config.device_binding is not None
            else []
        )
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
                cleanup_devices=devices,
            )
            uow.commit()
        return self._preparation_from_journal(journal)

    def _preparation_from_journal(
        self,
        journal: Mapping[str, Any],
    ) -> HardUninstallPreparation:
        automation_id = str(journal.get("automation_id") or "")
        instance = self.get_instance(automation_id)
        if instance is None:
            snapshot = journal.get("instance_snapshot_json")
            if not isinstance(snapshot, Mapping):
                raise PluginConflictError("purge journal has no instance snapshot")
            version = self.get_package_version(
                str(journal.get("plugin_id") or ""),
                str(journal.get("plugin_version") or ""),
            )
            if version is None:
                raise PluginConflictError("purge journal plugin version disappeared")
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
                record_version=int(snapshot.get("record_version") or 1),
                target_generation=int(snapshot.get("target_generation") or 1),
                committed_generation=snapshot.get("committed_generation"),
                reconcile_state=RuntimeReconcileState.DRAINING,
            )
        prepared_at = journal.get("created_at")
        if not isinstance(prepared_at, datetime):
            prepared_at = datetime.now(timezone.utc)
        elif prepared_at.tzinfo is None:
            prepared_at = prepared_at.replace(tzinfo=timezone.utc)
        cleanup_requests = tuple(
            WorkerCleanupRequest(
                command_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"boyi:purge:{journal.get('purge_id')}:{device_id}",
                    )
                ),
                automation_id=automation_id,
                version=str(journal.get("plugin_version") or ""),
                device_id=str(device_id),
                requested_at=prepared_at,
                package_sha256=str(journal.get("package_sha256") or ""),
                cleanup_scope=str(journal.get("cleanup_scope") or "INSTANCE"),
            )
            for device_id in sorted(journal.get("cleanup_devices_json") or [])
        )
        return HardUninstallPreparation(
            purge_id=str(journal.get("purge_id") or ""),
            instance=instance,
            cleanup_requests=cleanup_requests,
            prepared_at=prepared_at,
            delete_shared_package=bool(journal.get("delete_shared_package")),
        )

    def persist_cleanup_requests(
        self,
        preparation: HardUninstallPreparation,
    ) -> None:
        deadline_seconds = int(
            preparation.instance.active_version.manifest.get(
                "worker_requirement", {}
            ).get("queue_deadline_seconds", 86400)
        )
        directives = [
            {
                "command_id": item.command_id,
                "device_id": item.device_id,
                "deadline_at": item.requested_at + timedelta(seconds=deadline_seconds),
            }
            for item in preparation.cleanup_requests
        ]
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.persist_cleanup_directives(
                preparation.purge_id,
                directives,
                release_hold=bool(self._release_hold_provider()),
            )
            uow.commit()

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

    def all_cleanup_acknowledged(
        self,
        preparation: HardUninstallPreparation,
    ) -> bool:
        with self._orchestration.unit_of_work() as uow:
            return bool(
                uow.automation_plugins.all_cleanup_directives_acknowledged(
                    preparation.purge_id
                )
            )

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
