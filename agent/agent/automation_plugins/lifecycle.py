"""Install reusable action packages and manage independent instances."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.automation_plugins.errors import PluginConflictError, PluginPackageError, PluginUninstallBlocked
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import (
    ExecutionBlockKind,
    PluginInstanceRecord,
    PluginRuntimeModel,
    PluginTrustSource,
    PluginUninstallResult,
    PluginUninstallStatus,
    PluginVersionRecord,
)
from agent.automation_plugins.package import (
    PackageSignatureVerifier,
    VerifiedPluginPackage,
    extract_verified_package,
    verify_signed_plugin_zip,
)
from agent.automation_plugins.package_v2 import (
    VerifiedPluginPackageV2,
    extract_verified_plugin_package_v2,
    verify_unsigned_plugin_zip_v2,
)
from agent.automation_plugins.ports import (
    AutomationPluginRepositoryPort,
    PluginEnvironmentBuilderPort,
    PluginStoragePort,
)
from agent.tool_registry import validate_registry
from agent.automation_plugins.service_v2_contract import (
    HOST_API_VERSION,
    ServiceV2ProjectContract,
)
from shared.redaction import redact_text


@dataclass(frozen=True)
class _PreparedVersion:
    record: PluginVersionRecord
    newly_materialized: bool


_VerifiedPackage = VerifiedPluginPackage | VerifiedPluginPackageV2


def _package_bytes(source: bytes | bytearray | Path | str) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    try:
        target = Path(source)
        if target.name.casefold() == ".env" or target.suffix.casefold() in {
            ".key",
            ".pem",
        }:
            raise PluginPackageError("plugin source path is forbidden")
        return target.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise PluginPackageError("plugin package source cannot be read") from exc


def _package_runtime_model(source: bytes | bytearray | Path | str) -> PluginRuntimeModel:
    """Read only the discriminator; validation never falls back across schemas."""

    raw = _package_bytes(source)
    try:
        with zipfile.ZipFile(io.BytesIO(raw), mode="r") as archive:
            matches = [
                item
                for item in archive.infolist()
                if item.filename.casefold() == "manifest.json"
            ]
            if len(matches) != 1 or matches[0].file_size > 1024 * 1024:
                raise PluginPackageError("plugin ZIP must contain one bounded manifest.json")
            manifest = json.loads(archive.read(matches[0]).decode("utf-8"))
    except PluginPackageError:
        raise
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise PluginPackageError("plugin ZIP manifest discriminator is invalid") from exc
    if not isinstance(manifest, dict):
        raise PluginPackageError("plugin ZIP manifest must be an object")
    schema_version = manifest.get("schema_version")
    runtime_model = manifest.get("runtime_model")
    if schema_version == 1 and runtime_model in (None, "action_v1"):
        return PluginRuntimeModel.ACTION_V1
    if schema_version == 2 and runtime_model == "service_v2":
        return PluginRuntimeModel.SERVICE_V2
    raise PluginPackageError(
        "plugin ZIP must declare schema_version=1 action_v1 or schema_version=2 service_v2",
        code="PLUGIN_RUNTIME_MODEL_INVALID",
    )


class AutomationPluginService:
    """Lifecycle service; signed API adapters enforce super-admin identity."""

    def __init__(
        self,
        *,
        repository: AutomationPluginRepositoryPort,
        storage: PluginStoragePort,
        environments: PluginEnvironmentBuilderPort,
        upload_signature_verifier: PackageSignatureVerifier,
        allowed_execution_platforms: Sequence[str] | None = None,
        blocked_plugin_ids: Sequence[str] = (),
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._environments = environments
        self._signature_verifier = upload_signature_verifier
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
        self._blocked_plugin_ids = frozenset(
            str(item or "").strip()
            for item in blocked_plugin_ids
            if str(item or "").strip()
        )

    def _validate_release_scope(self, manifest: AutomationPluginManifest) -> None:
        if manifest.plugin_id in self._blocked_plugin_ids:
            raise PluginPackageError(
                "plugin action is deferred from the current release",
                code="PLUGIN_ACTION_DEFERRED",
            )
        if (
            self._allowed_execution_platforms is not None
            and manifest.execution_platform not in self._allowed_execution_platforms
        ):
            raise PluginPackageError(
                "plugin execution platform is disabled in the current release",
                code="PLUGIN_EXECUTION_PLATFORM_DISABLED",
            )

    @staticmethod
    def _validate_super_admin(actor_id: str, actor_role: str, request_id: str) -> None:
        if not str(actor_id or "").strip() or str(actor_role or "").strip() != "super_admin":
            raise PluginConflictError("plugin lifecycle requires an authenticated super administrator")
        try:
            uuid.UUID(str(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginConflictError("plugin lifecycle request_id must be UUID") from exc

    def _materialize_upload(self, verified: _VerifiedPackage) -> _PreparedVersion:
        if isinstance(verified, VerifiedPluginPackageV2):
            return self._materialize_upload_v2(verified)
        manifest = verified.manifest
        if manifest.runtime["kind"] != "python_subprocess":
            raise PluginPackageError("uploaded plugins cannot reference a governed core executor")
        existing = self._repository.get_package_version(manifest.plugin_id, manifest.version)
        if existing is not None:
            if (
                existing.package_sha256 != verified.package_sha256
                or existing.manifest_sha256 != verified.manifest_sha256
            ):
                raise PluginConflictError("plugin_id/version already belongs to different immutable bytes")
            return _PreparedVersion(existing, False)
        staging = self._storage.create_staging_root(manifest.plugin_id, manifest.version)
        committed: Path | None = None
        try:
            package_root = staging / "package"
            extract_verified_package(verified, package_root)
            archive_relative = self._storage.persist_verified_archive(
                staging,
                verified.archive_bytes,
                expected_sha256=verified.package_sha256,
            )
            tools = validate_registry(
                {"tools": [manifest.to_mapping()["tool_contract"]]},
                project_root=package_root,
            )
            if len(tools) != 1 or tools[0]["name"] != manifest.tool_contract["name"]:
                raise PluginPackageError("plugin tool governance validation failed")
            python_path = self._environments.build(staging, manifest)
            python_relative = str(python_path.relative_to(staging)).replace("\\", "/")
            committed = self._storage.commit_staging_root(
                staging,
                plugin_id=manifest.plugin_id,
                version=manifest.version,
                manifest_sha256=verified.manifest_sha256,
            )
            return _PreparedVersion(
                PluginVersionRecord(
                    plugin_id=manifest.plugin_id,
                    version=manifest.version,
                    package_sha256=verified.package_sha256,
                    manifest_sha256=verified.manifest_sha256,
                    manifest=manifest.to_signed_mapping(),
                    trust_source=PluginTrustSource.ED25519_UPLOAD,
                    install_root=str(committed),
                    install_metadata={
                        "python_relative": python_relative,
                        "archive_relative": archive_relative,
                        "archive_sha256": verified.package_sha256,
                        "package_files": [
                            {"path": item.path, "sha256": item.sha256, "size": item.size}
                            for item in verified.files
                        ],
                    },
                    runtime_model=PluginRuntimeModel.ACTION_V1,
                    plugin_api="1.0.0",
                ),
                True,
            )
        except Exception:
            if committed is not None:
                self._storage.remove_version_root(committed)
            else:
                self._storage.discard_staging_root(staging)
            raise

    def _materialize_upload_v2(
        self,
        verified: VerifiedPluginPackageV2,
    ) -> _PreparedVersion:
        manifest = verified.manifest
        ServiceV2ProjectContract.from_manifest(manifest)
        existing = self._repository.get_package_version(
            manifest.plugin_id,
            manifest.version,
        )
        if existing is not None:
            if existing.runtime_model is not PluginRuntimeModel.SERVICE_V2:
                raise PluginConflictError(
                    "plugin_id/version already belongs to another runtime model"
                )
            if (
                existing.package_sha256 != verified.package_sha256
                or existing.manifest_sha256 != verified.manifest_sha256
            ):
                raise PluginConflictError(
                    "plugin_id/version already belongs to different immutable bytes"
                )
            return _PreparedVersion(existing, False)

        staging = self._storage.create_staging_root(
            manifest.plugin_id,
            manifest.version,
        )
        committed: Path | None = None
        try:
            package_root = staging / "package"
            extract_verified_plugin_package_v2(verified, package_root)
            archive_relative = self._storage.persist_verified_archive(
                staging,
                verified.package_bytes,
                expected_sha256=verified.package_sha256,
            )
            python_path = self._environments.build(staging, manifest)  # type: ignore[arg-type]
            python_relative = str(python_path.relative_to(staging)).replace("\\", "/")
            committed = self._storage.commit_staging_root(
                staging,
                plugin_id=manifest.plugin_id,
                version=manifest.version,
                manifest_sha256=verified.manifest_sha256,
            )
            return _PreparedVersion(
                PluginVersionRecord(
                    plugin_id=manifest.plugin_id,
                    version=manifest.version,
                    package_sha256=verified.package_sha256,
                    manifest_sha256=verified.manifest_sha256,
                    manifest=manifest.to_mapping(),
                    trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD,
                    install_root=str(committed),
                    install_metadata={
                        "python_relative": python_relative,
                        "archive_relative": archive_relative,
                        "archive_sha256": verified.package_sha256,
                        "package_files": [
                            {
                                "path": item.path,
                                "sha256": item.sha256,
                                "size": item.size,
                            }
                            for item in verified.files
                        ],
                        "files_sha256": verified.files_sha256,
                        "runtime_sha256": verified.runtime_sha256,
                        "config_schema_sha256": verified.config_schema_sha256,
                        "service_contracts_sha256": verified.service_contracts_sha256,
                        "contributions_sha256": verified.contributions_sha256,
                        "capabilities_sha256": verified.capabilities_sha256,
                        "storage_sha256": verified.storage_sha256,
                    },
                    runtime_model=PluginRuntimeModel.SERVICE_V2,
                    plugin_api=str(manifest.host_api["minimum"]),
                ),
                True,
            )
        except Exception:
            if committed is not None:
                self._storage.remove_version_root(committed)
            else:
                self._storage.discard_staging_root(staging)
            raise

    def _verified_upload(
        self,
        package_source: bytes | bytearray | Path | str,
        transport_package_sha256: str | None = None,
    ) -> _VerifiedPackage:
        runtime_model = _package_runtime_model(package_source)
        if runtime_model is PluginRuntimeModel.SERVICE_V2:
            if transport_package_sha256 is None:
                raise PluginPackageError("v2 upload requires its transport SHA-256")
            verified_v2 = verify_unsigned_plugin_zip_v2(
                _package_bytes(package_source),
                transport_sha256=transport_package_sha256,
            )
            if not verified_v2.manifest.supports_host_api(HOST_API_VERSION):
                raise PluginPackageError(
                    f"plugin does not support Host API {HOST_API_VERSION}",
                    code="PLUGIN_HOST_API_INCOMPATIBLE",
                )
            ServiceV2ProjectContract.from_manifest(verified_v2.manifest)
            return verified_v2
        verified_v1 = verify_signed_plugin_zip(
            package_source,
            verifier=self._signature_verifier,
            expected_package_sha256=transport_package_sha256,
        )
        self._validate_release_scope(verified_v1.manifest)
        return verified_v1

    def install_upload(
        self,
        package_source: bytes | bytearray | Path | str,
        *,
        instance_name: str,
        actor_id: str,
        actor_role: str,
        request_id: str,
        transport_package_sha256: str | None = None,
    ) -> PluginInstanceRecord:
        """Install a new disabled instance; automation_id is server-generated."""

        self._validate_super_admin(actor_id, actor_role, request_id)

        verified = self._verified_upload(package_source, transport_package_sha256)
        normalized_name = str(instance_name or "").strip() or verified.manifest.name
        if len(normalized_name) > 120:
            raise PluginPackageError("instance_name must be no longer than 120 characters")
        prepared = self._materialize_upload(verified)
        try:
            instance = self._repository.install_instance(
                prepared.record,
                instance_name=normalized_name,
                actor_id=actor_id,
                actor_role=actor_role,
                request_id=request_id,
            )
            return instance
        except Exception:
            # Keep bytes if another idempotent/concurrent transaction now owns
            # the shared version; otherwise remove our unreferenced staging.
            persisted = self._repository.get_package_version(
                prepared.record.plugin_id,
                prepared.record.version,
            )
            if prepared.newly_materialized and persisted is None and prepared.record.install_root:
                self._storage.remove_version_root(Path(prepared.record.install_root))
            raise

    def upgrade_upload(
        self,
        automation_id: str,
        package_source: bytes | bytearray | Path | str,
        *,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_current_version: str,
        expected_record_version: int,
        transport_package_sha256: str | None = None,
    ) -> PluginInstanceRecord:
        self._validate_super_admin(actor_id, actor_role, request_id)
        current = self._repository.get_instance(automation_id)
        if current is None:
            raise PluginConflictError(f"automation instance does not exist: {automation_id}")
        prepared = self._materialize_upload(
            self._verified_upload(package_source, transport_package_sha256)
        )
        if prepared.record.runtime_model is not current.active_version.runtime_model:
            raise PluginConflictError(
                "runtime model changes require a new parallel automation project",
                code="PLUGIN_RUNTIME_MODEL_MIGRATION_REQUIRED",
            )
        if prepared.record.plugin_id != current.plugin_id:
            raise PluginConflictError("an upgrade cannot change the instance plugin_id")
        try:
            upgraded = self._repository.upgrade_instance(
                automation_id,
                prepared.record,
                actor_id=actor_id,
                actor_role=actor_role,
                request_id=request_id,
                expected_current_version=expected_current_version,
                expected_record_version=expected_record_version,
            )
            return upgraded
        except Exception:
            persisted = self._repository.get_package_version(
                prepared.record.plugin_id,
                prepared.record.version,
            )
            if prepared.newly_materialized and persisted is None and prepared.record.install_root:
                self._storage.remove_version_root(Path(prepared.record.install_root))
            raise

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
        self._validate_super_admin(actor_id, actor_role, request_id)
        if enabled:
            current = self._repository.get_instance(automation_id)
            if current is not None:
                if current.active_version.runtime_model is PluginRuntimeModel.ACTION_V1:
                    self._validate_release_scope(
                        AutomationPluginManifest.from_mapping(
                            current.active_version.manifest
                        )
                    )
                else:
                    manifest = AutomationPluginManifestV2.from_mapping(
                        current.active_version.manifest
                    )
                    if not manifest.supports_host_api(HOST_API_VERSION):
                        raise PluginPackageError(
                            "service-v2 Host API range no longer includes this host",
                            code="PLUGIN_HOST_API_INCOMPATIBLE",
                        )
        return self._repository.set_enabled(
            automation_id,
            enabled=enabled,
            actor_id=actor_id,
            actor_role=actor_role,
            request_id=request_id,
            expected_record_version=expected_record_version,
        )

    def hard_uninstall(
        self,
        automation_id: str,
        *,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_current_version: str,
        expected_record_version: int,
        before_finalize: Callable[[str], object] | None = None,
    ) -> PluginUninstallResult:
        """Revoke immediately, drain runtime effects, then await cleanup ACKs.

        ``prepare_hard_uninstall`` revokes the project route and marks all
        generations as draining in one transaction.  The production caller
        supplies ``before_finalize`` so those generations are actually
        reconciled/disposed before the purge journal is finalized.  The
        callback is deliberately optional to preserve the lower-level
        lifecycle API used by Worker-only adapters; such callers must invoke
        their runtime reconciler before calling ``finalize_hard_uninstall``.
        """

        self._validate_super_admin(actor_id, actor_role, request_id)

        blocks = tuple(self._repository.list_execution_blocks(automation_id))
        if blocks:
            raise PluginUninstallBlocked(
                "plugin instance has protected execution state: "
                + ", ".join(sorted({block.kind.value for block in blocks}))
            )
        preparation = self._repository.prepare_hard_uninstall(
            automation_id,
            actor_id=actor_id,
            actor_role=actor_role,
            request_id=request_id,
            expected_current_version=expected_current_version,
            expected_record_version=expected_record_version,
        )
        try:
            self._repository.persist_cleanup_requests(preparation)
        except Exception as exc:
            self._repository.mark_purge_failed(
                preparation,
                error_code=type(exc).__name__.upper()[:64],
                error_summary=redact_text(exc)[:500],
            )
            raise
        if before_finalize is not None:
            try:
                before_finalize(automation_id)
            except Exception as exc:
                # The project has already been revoked.  Preserve the purge
                # journal for a retry and never delete application state while
                # runtime generation disposal is uncertain.
                self._repository.mark_purge_failed(
                    preparation,
                    error_code=getattr(exc, "code", type(exc).__name__.upper())[:64],
                    error_summary=redact_text(exc)[:500],
                )
                raise
        if preparation.cleanup_requests:
            return PluginUninstallResult(
                automation_id=automation_id,
                purge_id=preparation.purge_id,
                status=PluginUninstallStatus.PENDING,
                pending_cleanup_commands=tuple(
                    request.command_id for request in preparation.cleanup_requests
                ),
            )
        return self.finalize_hard_uninstall(
            automation_id,
            purge_id=preparation.purge_id,
        )

    def finalize_hard_uninstall(
        self,
        automation_id: str,
        *,
        purge_id: str,
    ) -> PluginUninstallResult:
        """Finalize only after ACKs; crash-safe purge journal remains until done."""

        preparation = self._repository.get_hard_uninstall_preparation(
            automation_id=automation_id,
            purge_id=purge_id,
        )
        if preparation is None:
            raise PluginConflictError("plugin uninstall purge journal does not exist")
        if not self._repository.all_cleanup_acknowledged(preparation):
            return PluginUninstallResult(
                automation_id=automation_id,
                purge_id=purge_id,
                status=PluginUninstallStatus.PENDING,
                pending_cleanup_commands=tuple(
                    request.command_id for request in preparation.cleanup_requests
                ),
            )
        try:
            reserved = self._repository.reserve_hard_uninstall_finalize(preparation)
            atomic_blocks = tuple(self._repository.list_execution_blocks(automation_id))
            if any(
                block.kind
                in {
                    ExecutionBlockKind.RUNNING,
                    ExecutionBlockKind.VERIFYING,
                    ExecutionBlockKind.WRITE_OUTCOME_UNKNOWN,
                }
                for block in atomic_blocks
            ):
                raise PluginUninstallBlocked("protected execution appeared before purge finalize")
            # DB application state is removed first while the purge journal and
            # immutable version metadata retain the exact filesystem target.
            # A filesystem failure is therefore retryable and cannot resurrect
            # any execution entrypoint.
            self._repository.hard_delete_application_state(reserved)
            if reserved.delete_shared_package and reserved.instance.active_version.install_root:
                self._storage.remove_version_root(
                    Path(reserved.instance.active_version.install_root)
                )
            self._repository.complete_hard_uninstall(reserved)
            return PluginUninstallResult(
                automation_id=automation_id,
                purge_id=purge_id,
                status=PluginUninstallStatus.COMPLETED,
            )
        except Exception as exc:
            self._repository.mark_purge_failed(
                preparation,
                error_code=type(exc).__name__.upper()[:64],
                error_summary=redact_text(exc)[:500],
            )
            raise
