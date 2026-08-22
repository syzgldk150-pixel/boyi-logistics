"""Durable signed-package runtime used by the Windows background service."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from agent.automation_plugins.errors import PluginPackageError, WorkerProtocolError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.package import (
    PackageSignatureVerifier,
    VerifiedPluginPackage,
    extract_verified_package,
    verify_signed_plugin_zip,
)
from agent.automation_plugins.storage import FilesystemPluginStorage, LockedVirtualEnvironmentBuilder
from agent.windows_worker.models import WorkerJob, WorkerJobType
from agent.windows_worker.ports import LocalWorkerRuntimePort
from agent.windows_worker.routes import parse_worker_package_path
from agent.windows_worker.state import WindowsWorkerStateStore


_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTALL_FIELDS = frozenset(
    {
        "package_url",
        "package_sha256",
    }
)
_CLEANUP_FIELDS = frozenset({"purge_id", "generation"})


@runtime_checkable
class WorkerPackageFetcherPort(Protocol):
    def fetch_package(self, url: str, *, expected_sha256: str) -> bytes: ...


def _positive_generation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WorkerProtocolError("Worker generation must be a positive integer")
    return value


def _safe_root(root: Path, path: str, label: str) -> Path:
    value = Path(path)
    if not value.is_absolute() or value.is_symlink():
        raise WorkerProtocolError(f"Worker {label} path is invalid")
    resolved = value.resolve()
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise WorkerProtocolError(f"Worker {label} path escaped its configured root") from exc
    if not relative.parts:
        raise WorkerProtocolError(f"Worker {label} path is too broad")
    return resolved


class WindowsLocalPluginRuntime(LocalWorkerRuntimePort):
    """Install immutable versions and retain old generations until drained."""

    def __init__(
        self,
        *,
        state: WindowsWorkerStateStore,
        package_storage: FilesystemPluginStorage,
        signature_verifier: PackageSignatureVerifier,
        package_fetcher: WorkerPackageFetcherPort,
        environment_builder: LockedVirtualEnvironmentBuilder | None = None,
    ) -> None:
        self._state = state
        self._packages = package_storage
        self._verifier = signature_verifier
        self._fetcher = package_fetcher
        self._environment_builder = environment_builder or LockedVirtualEnvironmentBuilder()
        self._instances = FilesystemPluginStorage(self._state.root / "instances")

    def begin_once(self, job: WorkerJob) -> bool:
        return self._state.begin_once(job)

    def prior_result(self, job_id: str) -> Mapping[str, Any] | None:
        return self._state.prior_result(job_id)

    def save_result(self, job_id: str, result: Mapping[str, Any]) -> None:
        self._state.save_result(job_id, result)

    def has_unknown_write(self, automation_id: str) -> bool:
        return self._state.has_unknown_write(automation_id)

    def has_cleanup_blocker(
        self,
        automation_id: str,
        *,
        excluding_job_id: str,
    ) -> bool:
        return self._state.has_cleanup_blocker(
            automation_id,
            excluding_job_id=excluding_job_id,
        )

    def count_active_jobs(self) -> int:
        return self._state.count_active_jobs()

    @staticmethod
    def _validate_identity(job: WorkerJob) -> None:
        if not _SEGMENT_RE.fullmatch(job.automation_id):
            raise WorkerProtocolError("Worker automation_id is invalid")
        if not _SEGMENT_RE.fullmatch(job.plugin_id):
            raise WorkerProtocolError("Worker plugin_id is invalid")
        if not _VERSION_RE.fullmatch(job.plugin_version):
            raise WorkerProtocolError("Worker plugin version is invalid")

    def _verify_package(
        self,
        package_bytes: bytes,
        *,
        job: WorkerJob,
        package_sha256: str,
    ) -> VerifiedPluginPackage:
        package = verify_signed_plugin_zip(
            package_bytes,
            verifier=self._verifier,
            expected_package_sha256=package_sha256,
        )
        if (
            package.manifest.plugin_id != job.plugin_id
            or package.manifest.version != job.plugin_version
            or package.manifest.execution_platform != "windows"
        ):
            raise PluginPackageError("signed Worker package identity/platform does not match the job")
        return package

    @staticmethod
    def _assert_materialized(version_root: Path, package: VerifiedPluginPackage) -> None:
        package_root = version_root / "package"
        archive_path = version_root / "package.zip"
        if version_root.is_symlink() or package_root.is_symlink() or archive_path.is_symlink():
            raise PluginPackageError("materialized Worker package contains an unsafe symlink")
        if not package_root.is_dir() or not archive_path.is_file():
            raise PluginPackageError("materialized Worker package is incomplete")
        for directory, child_directories, files in os.walk(
            version_root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(directory)
            for name in [*child_directories, *files]:
                child = current / name
                if child.is_symlink() or (not child.is_file() and not child.is_dir()):
                    raise PluginPackageError("materialized Worker package tree is unsafe")
        python_path = (
            version_root / "venv" / "Scripts" / "python.exe"
            if os.name == "nt"
            else version_root / "venv" / "bin" / "python"
        )
        if not python_path.is_file() or python_path.is_symlink():
            raise PluginPackageError("materialized Worker virtual environment is incomplete")
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != package.package_sha256:
            raise PluginPackageError("materialized Worker package archive changed")
        for item in package.files:
            target = package_root.joinpath(*item.path.split("/"))
            if target.is_symlink() or not target.is_file():
                raise PluginPackageError("materialized Worker package file is missing or unsafe")
            content = target.read_bytes()
            if len(content) != item.size or hashlib.sha256(content).hexdigest() != item.sha256:
                raise PluginPackageError("materialized Worker package file changed")

    def _existing_package(
        self,
        job: WorkerJob,
        *,
        package_sha256: str,
    ) -> tuple[Path, str] | None:
        record = self._state.get_package(job.plugin_id, job.plugin_version)
        if record is None:
            return None
        if str(record["package_sha256"]) != package_sha256:
            raise PluginPackageError("immutable Worker package version digest changed")
        manifest_sha256 = str(record["manifest_sha256"])
        if not _SHA256_RE.fullmatch(manifest_sha256):
            raise PluginPackageError("stored Worker manifest digest is invalid")
        root = _safe_root(self._packages.root, str(record["install_root"]), "package")
        archive = root / "package.zip"
        if not archive.is_file() or archive.is_symlink():
            raise PluginPackageError("installed Worker package archive is missing")
        package = self._verify_package(
            archive.read_bytes(),
            job=job,
            package_sha256=package_sha256,
        )
        if package.manifest_sha256 != manifest_sha256:
            raise PluginPackageError("stored Worker manifest digest changed")
        self._assert_materialized(root, package)
        return root, manifest_sha256

    def _materialize_package(
        self,
        job: WorkerJob,
        *,
        package_url: str,
        package_sha256: str,
    ) -> tuple[Path, bool, str]:
        existing = self._existing_package(
            job,
            package_sha256=package_sha256,
        )
        if existing is not None:
            root, manifest_sha256 = existing
            return root, False, manifest_sha256
        package_bytes = self._fetcher.fetch_package(
            package_url,
            expected_sha256=package_sha256,
        )
        package = self._verify_package(
            package_bytes,
            job=job,
            package_sha256=package_sha256,
        )
        manifest_sha256 = package.manifest_sha256
        recovered_root = (
            self._packages.root
            / job.plugin_id
            / f"{job.plugin_version}-{manifest_sha256[:12]}"
        )
        if recovered_root.exists():
            recovered = _safe_root(self._packages.root, str(recovered_root), "package")
            archive = recovered / "package.zip"
            if not archive.is_file() or archive.is_symlink():
                raise PluginPackageError("orphaned Worker package is incomplete")
            package = self._verify_package(
                archive.read_bytes(),
                job=job,
                package_sha256=package_sha256,
            )
            if package.manifest_sha256 != manifest_sha256:
                raise PluginPackageError("recovered Worker manifest digest changed")
            self._assert_materialized(recovered, package)
            return recovered, False, manifest_sha256
        stage = self._packages.create_staging_root(job.plugin_id, job.plugin_version)
        try:
            archive = stage / "package.zip"
            archive.write_bytes(package.archive_bytes)
            try:
                archive.chmod(0o400)
            except OSError:
                pass
            extract_verified_package(package, stage / "package")
            self._environment_builder.build(stage, package.manifest)
            version_root = self._packages.commit_staging_root(
                stage,
                plugin_id=job.plugin_id,
                version=job.plugin_version,
                manifest_sha256=manifest_sha256,
            )
            self._assert_materialized(version_root, package)
            return version_root, True, manifest_sha256
        except Exception:
            if stage.exists():
                self._packages.discard_staging_root(stage)
            raise

    def install_or_upgrade(self, job: WorkerJob) -> Mapping[str, Any]:
        if job.job_type not in {WorkerJobType.INSTALL, WorkerJobType.UPGRADE}:
            raise WorkerProtocolError("Worker install runtime received another job type")
        self._validate_identity(job)
        payload = dict(job.payload)
        if set(payload) != _INSTALL_FIELDS:
            raise WorkerProtocolError("Worker install payload schema is invalid")
        generation = _positive_generation(job.automation_generation)
        package_url = payload["package_url"]
        package_sha256 = payload["package_sha256"]
        package_identity = (
            parse_worker_package_path(package_url)
            if isinstance(package_url, str)
            else None
        )
        if (
            not isinstance(package_sha256, str)
            or not _SHA256_RE.fullmatch(package_sha256)
            or package_identity is None
            or package_identity[0:3]
            != (job.plugin_id, job.plugin_version, package_sha256)
        ):
            raise WorkerProtocolError("Worker install package fields are invalid")
        existing_deployment = self._state.get_deployment(job.automation_id, generation)
        if existing_deployment is not None:
            package_record = self._state.get_package(job.plugin_id, job.plugin_version)
            if (
                str(existing_deployment["plugin_id"]) != job.plugin_id
                or str(existing_deployment["plugin_version"]) != job.plugin_version
                or not isinstance(package_record, Mapping)
                or str(package_record.get("package_sha256") or "") != package_sha256
            ):
                raise WorkerProtocolError("Worker generation already resolves to another package")
            return {
                "automation_id": job.automation_id,
                "generation": generation,
                "plugin_id": job.plugin_id,
                "plugin_version": job.plugin_version,
                "package_sha256": str(package_sha256),
                "installed": True,
            }
        version_root, created_package, manifest_sha256 = self._materialize_package(
            job,
            package_url=package_url,
            package_sha256=str(package_sha256),
        )
        instance_root = self._instances.root / job.automation_id
        runtime_root = instance_root / str(generation)
        if runtime_root.exists() or runtime_root.is_symlink():
            if created_package:
                self._packages.remove_version_root(version_root)
            raise WorkerProtocolError("Worker generation runtime root already exists")
        runtime_root.mkdir(parents=True, exist_ok=False)
        descriptor = {
            "automation_id": job.automation_id,
            "generation": generation,
            "plugin_id": job.plugin_id,
            "plugin_version": job.plugin_version,
            "package_sha256": package_sha256,
            "manifest_sha256": manifest_sha256,
        }
        descriptor_path = runtime_root / "generation.json"
        descriptor_path.write_bytes(canonical_json_bytes(descriptor))
        try:
            descriptor_path.chmod(0o400)
            runtime_root.chmod(0o700)
        except OSError:
            pass
        try:
            self._state.bind_deployment(
                automation_id=job.automation_id,
                generation=generation,
                plugin_id=job.plugin_id,
                plugin_version=job.plugin_version,
                package_sha256=str(package_sha256),
                manifest_sha256=str(manifest_sha256),
                install_root=str(version_root),
                instance_root=str(instance_root.resolve()),
                runtime_root=str(runtime_root.resolve()),
            )
        except Exception:
            self._instances.remove_version_root(runtime_root)
            if created_package:
                self._packages.remove_version_root(version_root)
            raise
        return {
            "automation_id": job.automation_id,
            "generation": generation,
            "plugin_id": job.plugin_id,
            "plugin_version": job.plugin_version,
            "package_sha256": str(package_sha256),
            "installed": True,
        }

    def cleanup_instance(self, job: WorkerJob) -> Mapping[str, Any]:
        if job.job_type not in {WorkerJobType.UNINSTALL, WorkerJobType.CLEANUP}:
            raise WorkerProtocolError("Worker cleanup runtime received another job type")
        self._validate_identity(job)
        payload = dict(job.payload)
        if set(payload) != _CLEANUP_FIELDS or not isinstance(payload["purge_id"], str):
            raise WorkerProtocolError("Worker cleanup payload schema is invalid")
        if self.has_cleanup_blocker(
            job.automation_id,
            excluding_job_id=job.job_id,
        ):
            raise WorkerProtocolError("active job or unknown write blocks Worker cleanup")
        generation = _positive_generation(payload["generation"])
        if job.cleanup_scope == "GENERATION":
            deployment = self._state.get_deployment(job.automation_id, generation)
            if deployment is None:
                return {
                    "automation_id": job.automation_id,
                    "generation": generation,
                    "disposed": True,
                }
            runtime_root = _safe_root(
                self._instances.root,
                str(deployment["runtime_root"]),
                "generation runtime",
            )
            self._instances.remove_version_root(runtime_root)
            disposed = self._state.dispose_deployment(
                automation_id=job.automation_id,
                generation=generation,
                excluding_job_id=job.job_id,
            )
            if disposed and bool(disposed["remove_package"]):
                package_root = _safe_root(
                    self._packages.root,
                    str(disposed["package_root"]),
                    "package",
                )
                self._packages.remove_version_root(package_root)
                self._state.remove_unreferenced_package(
                    plugin_id=str(disposed["plugin_id"]),
                    plugin_version=str(disposed["plugin_version"]),
                    package_sha256=str(disposed["package_sha256"]),
                    install_root=str(package_root),
                )
            return {
                "automation_id": job.automation_id,
                "generation": generation,
                "disposed": True,
            }
        if job.cleanup_scope != "INSTANCE":
            raise WorkerProtocolError("Worker cleanup scope is invalid")
        journal = self._state.prepare_purge(
            purge_id=str(payload["purge_id"]),
            automation_id=job.automation_id,
            excluding_job_id=job.job_id,
        )
        instance_root = _safe_root(
            self._instances.root,
            str(journal["instance_root"]),
            "instance",
        )
        self._instances.remove_plugin_roots(job.automation_id)
        package_root_value = journal["package_root"]
        if package_root_value is not None:
            package_root = _safe_root(
                self._packages.root,
                str(package_root_value),
                "package",
            )
            self._packages.remove_version_root(package_root)
        self._state.finalize_purge(
            purge_id=str(payload["purge_id"]),
            excluding_job_id=job.job_id,
        )
        return {
            "automation_id": job.automation_id,
            "generation": generation,
            "purged": True,
        }
