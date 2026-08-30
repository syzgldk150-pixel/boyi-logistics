"""Bounded filesystem storage and locked virtual environments per version."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
import venv
from pathlib import Path
from typing import Any

from agent.automation_plugins.errors import PluginConflictError, PluginPackageError
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2


_AUTOMATION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_HASHED_REQUIREMENT_RE = re.compile(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)")
_WINDOWS_REPARSE_POINT = 0x0400
MAX_VERIFIED_ARCHIVE_BYTES = 256 * 1024 * 1024
VERIFIED_ARCHIVE_RELATIVE = "package-archive.zip"
_RENAME_NOREPLACE = 1


def _assert_segment(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise PluginPackageError(f"invalid {label}")
    return value


def _is_reparse_point(path: Path, *, stat_result: Any | None = None) -> bool:
    """Detect Windows junctions and other reparse points without following them."""

    metadata = stat_result if stat_result is not None else path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PluginPackageError("plugin filesystem entry cannot be inspected") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PluginPackageError("plugin tree must not contain symbolic links")
    if _is_reparse_point(path, stat_result=metadata):
        raise PluginPackageError("plugin tree must not contain reparse points or junctions")
    return metadata


def validate_regular_plugin_file(path: Path | str) -> os.stat_result:
    """Require a private regular file so chmod/remove cannot affect another tree."""

    target = Path(path)
    metadata = _safe_lstat(target)
    if not stat.S_ISREG(metadata.st_mode):
        raise PluginPackageError("plugin tree contains a non-regular file")
    if metadata.st_nlink != 1:
        raise PluginPackageError("plugin tree must not contain hard-linked files")
    return metadata


def validate_plugin_tree(root: Path | str) -> Path:
    """Validate one materialized tree without following links or reparse points."""

    target = Path(root)
    root_metadata = _safe_lstat(target)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise PluginPackageError("plugin tree root is not a directory")
    for directory, child_directories, files in os.walk(
        target,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        current_metadata = _safe_lstat(current)
        if not stat.S_ISDIR(current_metadata.st_mode):
            raise PluginPackageError("plugin tree contains a non-directory path")
        for name in child_directories:
            child = current / name
            child_metadata = _safe_lstat(child)
            if not stat.S_ISDIR(child_metadata.st_mode):
                raise PluginPackageError("plugin tree contains a non-directory path")
        for name in files:
            validate_regular_plugin_file(current / name)
    return target


def _same_filesystem_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _linux_open_directory(
    path: Path | str,
    *,
    dir_fd: int | None = None,
) -> tuple[int, os.stat_result]:
    """Open one directory without following it and pin its inspected inode."""

    try:
        before = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise PluginPackageError("plugin directory cannot be inspected") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise PluginPackageError("plugin directory is unsafe")
    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_DIRECTORY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    flags |= int(getattr(os, "O_CLOEXEC", 0) or 0)
    try:
        descriptor = os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise PluginPackageError("plugin directory cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        after = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not _same_filesystem_entry(before, opened)
            or not _same_filesystem_entry(opened, after)
        ):
            raise PluginPackageError("plugin directory changed during inspection")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, opened


def _linux_directory_matches(
    path: Path | str,
    expected: os.stat_result,
    *,
    dir_fd: int | None = None,
) -> bool:
    try:
        current = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and _same_filesystem_entry(
        expected,
        current,
    )


def _linux_rename_no_replace(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PluginPackageError("atomic no-clobber plugin publish is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        source_dir_fd,
        os.fsencode(source_name),
        destination_dir_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise PluginConflictError(
            "immutable plugin version appeared during commit"
        )
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise PluginPackageError("atomic no-clobber plugin publish is unavailable")
    raise PluginPackageError(
        f"atomic plugin publish failed: {os.strerror(error)}"
    )


def _linux_publish_directory_no_replace(
    source: Path,
    destination: Path,
    *,
    storage_root: Path,
    staging_root: Path,
) -> None:
    if (
        source.parent != staging_root
        or staging_root.parent != storage_root
        or destination.parent.parent != storage_root
    ):
        raise PluginPackageError("plugin publish paths are outside storage")
    descriptors: list[int] = []
    try:
        root_fd, root_identity = _linux_open_directory(storage_root)
        descriptors.append(root_fd)
        staging_fd, staging_identity = _linux_open_directory(
            ".staging",
            dir_fd=root_fd,
        )
        descriptors.append(staging_fd)
        project_name = destination.parent.name
        project_fd, project_identity = _linux_open_directory(
            project_name,
            dir_fd=root_fd,
        )
        descriptors.append(project_fd)
        source_identity = _safe_lstat(source)
        try:
            pinned_source = os.stat(
                source.name,
                dir_fd=staging_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise PluginPackageError(
                "plugin staging root cannot be pinned"
            ) from exc
        if (
            not stat.S_ISDIR(pinned_source.st_mode)
            or not _same_filesystem_entry(source_identity, pinned_source)
        ):
            raise PluginPackageError("plugin staging root changed before publish")
        try:
            os.stat(
                destination.name,
                dir_fd=project_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PluginPackageError(
                "plugin publish target cannot be inspected"
            ) from exc
        else:
            raise PluginConflictError(
                "immutable plugin version appeared during commit"
            )
        if not (
            _linux_directory_matches(storage_root, root_identity)
            and _linux_directory_matches(
                ".staging",
                staging_identity,
                dir_fd=root_fd,
            )
            and _linux_directory_matches(
                project_name,
                project_identity,
                dir_fd=root_fd,
            )
        ):
            raise PluginPackageError("plugin parent directory changed before publish")
        _linux_rename_no_replace(
            staging_fd,
            source.name,
            project_fd,
            destination.name,
        )
        if not (
            _linux_directory_matches(storage_root, root_identity)
            and _linux_directory_matches(
                ".staging",
                staging_identity,
                dir_fd=root_fd,
            )
            and _linux_directory_matches(
                project_name,
                project_identity,
                dir_fd=root_fd,
            )
        ):
            try:
                _linux_rename_no_replace(
                    project_fd,
                    destination.name,
                    staging_fd,
                    source.name,
                )
            except Exception as restore_exc:
                raise PluginPackageError(
                    "plugin parent changed and staging could not be restored"
                ) from restore_exc
            raise PluginPackageError("plugin parent directory changed during publish")
    except OSError as exc:
        raise PluginPackageError("plugin directory changed during publish") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _publish_directory_no_replace(
    source: Path,
    destination: Path,
    *,
    storage_root: Path,
    staging_root: Path,
) -> None:
    """Atomically publish ``source`` without ever replacing ``destination``."""

    if sys.platform.startswith("linux"):
        _linux_publish_directory_no_replace(
            source,
            destination,
            storage_root=storage_root,
            staging_root=staging_root,
        )
        return
    if os.name == "nt":
        try:
            # Windows rename fails when the destination already exists.
            os.rename(source, destination)
            return
        except OSError as exc:
            try:
                destination.lstat()
            except FileNotFoundError:
                raise PluginPackageError("atomic plugin publish failed") from exc
            except OSError as inspect_exc:
                raise PluginPackageError(
                    "plugin publish target cannot be inspected"
                ) from inspect_exc
            raise PluginConflictError(
                "immutable plugin version appeared during commit"
            ) from exc
    raise PluginPackageError(
        "atomic no-clobber plugin publish is unsupported on this platform"
    )


class FilesystemPluginStorage:
    """Keep immutable versions below one explicitly configured root."""

    def __init__(self, root: Path | str) -> None:
        requested_root = Path(root)
        if requested_root.exists() or requested_root.is_symlink():
            metadata = _safe_lstat(requested_root)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("plugin storage root must be a directory")
        self._root = requested_root.resolve()
        if self._root == self._root.parent:
            raise ValueError("plugin storage root cannot be a filesystem root")
        self._root.mkdir(parents=True, exist_ok=True)
        self._staging = self._root / ".staging"
        self._staging.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _expected_version_root(
        self,
        *,
        plugin_id: str,
        version: str,
        manifest_sha256: str,
    ) -> Path:
        safe_plugin_id = _assert_segment(
            plugin_id,
            _AUTOMATION_ID_RE,
            "plugin_id",
        )
        safe_version = _assert_segment(version, _VERSION_RE, "plugin version")
        digest = str(manifest_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PluginPackageError("plugin manifest digest is invalid")
        root_metadata = _safe_lstat(self._root)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise PluginPackageError("plugin storage root is unsafe")
        project_root = self._root / safe_plugin_id
        try:
            project_metadata = project_root.lstat()
        except FileNotFoundError:
            project_metadata = None
        except OSError as exc:
            raise PluginPackageError(
                "plugin project root cannot be inspected"
            ) from exc
        if project_metadata is not None:
            _safe_lstat(project_root)
            if not stat.S_ISDIR(project_metadata.st_mode):
                raise PluginPackageError("plugin project root is unsafe")
        return project_root / f"{safe_version}-{digest[:12]}"

    def inspect_expected_version_root(
        self,
        *,
        plugin_id: str,
        version: str,
        manifest_sha256: str,
    ) -> tuple[Path, bool]:
        """Inspect the one deterministic target without creating or following it."""

        target = self._expected_version_root(
            plugin_id=plugin_id,
            version=version,
            manifest_sha256=manifest_sha256,
        )
        try:
            target_metadata = target.lstat()
        except FileNotFoundError:
            return target, False
        except OSError as exc:
            raise PluginPackageError(
                "plugin immutable version root cannot be inspected"
            ) from exc
        _safe_lstat(target)
        if not stat.S_ISDIR(target_metadata.st_mode):
            raise PluginPackageError("plugin immutable version root is unsafe")
        return target, True

    def create_staging_root(self, plugin_id: str, version: str) -> Path:
        plugin_id = _assert_segment(plugin_id, _AUTOMATION_ID_RE, "plugin_id")
        version = _assert_segment(version, _VERSION_RE, "plugin version")
        target = self._staging / f"{plugin_id}-{version}-{uuid.uuid4().hex}"
        target.mkdir(parents=False, exist_ok=False)
        return target

    def commit_staging_root(
        self,
        staging_root: Path,
        *,
        plugin_id: str,
        version: str,
        manifest_sha256: str,
    ) -> Path:
        plugin_id = _assert_segment(plugin_id, _AUTOMATION_ID_RE, "plugin_id")
        version = _assert_segment(version, _VERSION_RE, "plugin version")
        validate_plugin_tree(staging_root)
        stage = staging_root.resolve()
        if stage.parent != self._staging or not stage.is_dir():
            raise PluginPackageError("plugin staging directory is outside the configured root")
        destination, destination_exists = self.inspect_expected_version_root(
            plugin_id=plugin_id,
            version=version,
            manifest_sha256=manifest_sha256,
        )
        if destination_exists:
            raise PluginConflictError("immutable plugin version already exists on disk")
        project_root = destination.parent
        project_root.mkdir(parents=True, exist_ok=True)
        project_metadata = _safe_lstat(project_root)
        if not stat.S_ISDIR(project_metadata.st_mode):
            raise PluginPackageError("plugin project root is unsafe")
        confirmed_destination, destination_exists = self.inspect_expected_version_root(
            plugin_id=plugin_id,
            version=version,
            manifest_sha256=manifest_sha256,
        )
        if confirmed_destination != destination or destination_exists:
            raise PluginConflictError("immutable plugin version already exists on disk")
        _publish_directory_no_replace(
            stage,
            destination,
            storage_root=self._root,
            staging_root=self._staging,
        )
        validate_plugin_tree(destination)
        return destination.resolve()

    def persist_verified_archive(
        self,
        staging_root: Path,
        archive_bytes: bytes,
        *,
        expected_sha256: str,
    ) -> str:
        """Persist the exact verified ZIP before the staging tree is committed."""

        data = bytes(archive_bytes)
        digest = str(expected_sha256 or "").strip().lower()
        if (
            not data
            or len(data) > MAX_VERIFIED_ARCHIVE_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise PluginPackageError("verified plugin archive bytes or digest are invalid")
        stage = Path(staging_root).absolute()
        if stage.parent != self._staging or not stage.exists():
            raise PluginPackageError("plugin archive staging directory is unrecognized")
        stage_metadata = _safe_lstat(stage)
        if not stat.S_ISDIR(stage_metadata.st_mode):
            raise PluginPackageError("plugin archive staging root is not a directory")
        target = stage / VERIFIED_ARCHIVE_RELATIVE
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(0o600)
            metadata = validate_regular_plugin_file(target)
            if metadata.st_size != len(data):
                raise PluginPackageError("verified plugin archive was not persisted completely")
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            if target.exists() or target.is_symlink():
                metadata = _safe_lstat(target)
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    target.unlink()
            raise
        return VERIFIED_ARCHIVE_RELATIVE

    def read_verified_archive(
        self,
        install_root: Path,
        archive_relative: str,
        *,
        expected_sha256: str,
    ) -> bytes:
        """Read exact immutable ZIP bytes without following filesystem links."""

        if str(archive_relative or "") != VERIFIED_ARCHIVE_RELATIVE:
            raise PluginPackageError("plugin archive relative path is invalid")
        digest = str(expected_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PluginPackageError("plugin archive digest is invalid")
        root = self._checked_removal_target(Path(install_root), allow_project=False)
        if not root.is_dir():
            raise PluginPackageError("plugin install root is missing")
        target = root / VERIFIED_ARCHIVE_RELATIVE
        inspected = validate_regular_plugin_file(target)
        if not 0 < inspected.st_size <= MAX_VERIFIED_ARCHIVE_BYTES:
            raise PluginPackageError("plugin archive size is invalid")
        flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0) or 0)
        descriptor = -1
        try:
            descriptor = os.open(target, flags)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_dev != inspected.st_dev
                or opened.st_ino != inspected.st_ino
            ):
                raise PluginPackageError("plugin archive changed during inspection")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                data = stream.read(MAX_VERIFIED_ARCHIVE_BYTES + 1)
        except OSError as exc:
            raise PluginPackageError("plugin archive cannot be read safely") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            not data
            or len(data) > MAX_VERIFIED_ARCHIVE_BYTES
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise PluginPackageError("plugin archive bytes failed immutable digest verification")
        return data

    def _checked_removal_target(self, target: Path, *, allow_project: bool) -> Path:
        absolute = target.absolute()
        try:
            relative = absolute.relative_to(self._root)
        except ValueError as exc:
            raise PluginPackageError("refusing to delete outside plugin storage") from exc
        minimum_parts = 1 if allow_project else 2
        if len(relative.parts) < minimum_parts or relative.parts[0] == ".staging":
            raise PluginPackageError("refusing broad plugin storage deletion")
        current = self._root
        for part in relative.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                _safe_lstat(current)
        resolved = absolute.resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise PluginPackageError("refusing to delete outside plugin storage") from exc
        return resolved

    @staticmethod
    def _remove_tree(target: Path) -> None:
        """Make only this verified tree removable, without following links."""

        validate_plugin_tree(target)
        for directory, _, files in os.walk(target, topdown=False, followlinks=False):
            current = Path(directory)
            for name in files:
                child = current / name
                validate_regular_plugin_file(child)
                child.chmod(0o600)
            _safe_lstat(current)
            current.chmod(0o700)
        shutil.rmtree(target)

    def remove_version_root(self, install_root: Path) -> None:
        target = self._checked_removal_target(Path(install_root), allow_project=False)
        if target.exists():
            self._remove_tree(target)

    def remove_plugin_roots(self, plugin_id: str) -> None:
        plugin_id = _assert_segment(plugin_id, _AUTOMATION_ID_RE, "plugin_id")
        target = self._checked_removal_target(self._root / plugin_id, allow_project=True)
        if target.exists():
            self._remove_tree(target)

    def discard_staging_root(self, staging_root: Path) -> None:
        absolute = staging_root.absolute()
        if absolute.parent != self._staging:
            raise PluginPackageError("refusing to delete an unrecognized staging directory")
        _safe_lstat(self._staging)
        if absolute.exists() or absolute.is_symlink():
            _safe_lstat(absolute)
        target = absolute.resolve()
        if target.parent != self._staging:
            raise PluginPackageError("refusing to delete an unrecognized staging directory")
        if target.exists():
            self._remove_tree(target)


class LockedVirtualEnvironmentBuilder:
    """Create an isolated venv and install only hashed wheels from the ZIP."""

    def __init__(self, *, install_timeout_seconds: int = 300) -> None:
        self._install_timeout_seconds = max(30, int(install_timeout_seconds))

    @staticmethod
    def _python_path(venv_root: Path) -> Path:
        if os.name == "nt":
            return venv_root / "Scripts" / "python.exe"
        return venv_root / "bin" / "python"

    @staticmethod
    def _normalize_copied_venv(venv_root: Path) -> None:
        """Remove redundant CPython compatibility entries from a copied venv.

        Linux CPython creates ``lib64 -> lib`` plus two full interpreter aliases
        even when ``symlinks=False``.  Plugins execute only ``bin/python``; the
        aliases waste two interpreter copies per immutable plugin version and a
        half-created venv can otherwise leave a link that masks the real error
        during staging cleanup.
        """

        if os.name == "nt":
            return
        lib64 = venv_root / "lib64"
        if lib64.is_symlink():
            if os.readlink(lib64) not in {"lib", "./lib"}:
                raise PluginPackageError(
                    "plugin virtual environment contains an unsafe lib64 link"
                )
            lib64.unlink()
        bin_root = venv_root / "bin"
        for name in {
            f"python{sys.version_info.major}",
            f"python{sys.version_info.major}.{sys.version_info.minor}",
        }:
            alias = bin_root / name
            try:
                metadata = alias.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PluginPackageError(
                    "plugin virtual environment alias cannot be inspected"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                if os.readlink(alias) not in {"python", "./python"}:
                    raise PluginPackageError(
                        "plugin virtual environment contains an unsafe Python alias"
                    )
            elif not stat.S_ISREG(metadata.st_mode):
                raise PluginPackageError(
                    "plugin virtual environment contains an unsafe Python alias"
                )
            alias.unlink()

    @staticmethod
    def _validated_lock(lock_path: Path) -> None:
        try:
            text = lock_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PluginPackageError("plugin requirements lock must be UTF-8") from exc
        logical: list[str] = []
        pending = ""
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            pending = f"{pending} {line}".strip()
            if pending.endswith("\\"):
                pending = pending[:-1].strip()
                continue
            logical.append(pending)
            pending = ""
        if pending:
            logical.append(pending)
        if not logical:
            raise PluginPackageError("declared requirements lock is empty")
        for requirement in logical:
            lowered = requirement.lower()
            if (
                "==" not in requirement
                or "@" in requirement
                or "://" in lowered
                or lowered.startswith(("-e ", "--", "git+", "hg+", "svn+", "bzr+"))
                or not _HASHED_REQUIREMENT_RE.search(lowered + " ")
            ):
                raise PluginPackageError(
                    "plugin dependencies must be exact versions with SHA-256 hashes and no URLs/options"
                )

    @staticmethod
    def _minimal_install_environment() -> dict[str, str]:
        environment = {
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return environment

    def build(
        self,
        version_root: Path,
        manifest: AutomationPluginManifest | AutomationPluginManifestV2,
    ) -> Path:
        package_root = version_root / "package"
        if not package_root.is_dir():
            raise PluginPackageError("verified plugin package directory is missing")
        if isinstance(manifest, AutomationPluginManifestV2) and (
            not sys.platform.startswith("linux")
            or sys.version_info[:2] != (3, 10)
        ):
            raise PluginPackageError(
                "service-v2 plugins require the Linux Python 3.10 host runtime",
                code="PLUGIN_RUNTIME_UNAVAILABLE",
            )
        venv_root = version_root / "venv"
        lock_name = manifest.runtime.get("requirements_lock")
        try:
            venv.EnvBuilder(
                with_pip=bool(lock_name),
                clear=False,
                # A symlinked venv interpreter resolves to /usr/bin/python and
                # escapes both the immutable version root and the Bubblewrap bind.
                symlinks=False,
                system_site_packages=False,
            ).create(venv_root)
        finally:
            # Normalize partial environments too, so a creation failure remains
            # the reported cause and the staging tree can be removed safely.
            self._normalize_copied_venv(venv_root)
        python_path = self._python_path(venv_root)
        if not python_path.is_file():
            raise PluginPackageError("plugin virtual environment did not create Python")
        if lock_name:
            lock_path = package_root.joinpath(*str(lock_name).split("/"))
            self._validated_lock(lock_path)
            wheels = package_root / "payload" / (
                "wheelhouse"
                if isinstance(manifest, AutomationPluginManifestV2)
                else "wheels"
            )
            if not wheels.is_dir():
                raise PluginPackageError("hashed plugin dependencies require payload/wheels")
            wheel_files = tuple(wheels.iterdir())
            if not wheel_files or any(not item.is_file() or item.suffix != ".whl" for item in wheel_files):
                raise PluginPackageError("plugin dependency bundle may contain wheels only")
            if isinstance(manifest, AutomationPluginManifestV2):
                declared_wheels = {
                    package_root.joinpath(*str(item).split("/")).resolve()
                    for item in manifest.runtime["wheelhouse"]
                }
                observed_wheels = {item.resolve() for item in wheel_files}
                if not declared_wheels or observed_wheels != declared_wheels:
                    raise PluginPackageError(
                        "service-v2 wheelhouse differs from its manifest declaration"
                    )
            completed = subprocess.run(
                [
                    str(python_path),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--require-hashes",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--find-links",
                    str(wheels),
                    "-r",
                    str(lock_path),
                ],
                cwd=str(version_root),
                env=self._minimal_install_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._install_timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                # Do not include pip output: package metadata may contain
                # attacker-controlled or credential-like strings.
                raise PluginPackageError("isolated plugin dependency installation failed")
        for directory, child_directories, files in os.walk(venv_root, followlinks=False):
            current = Path(directory)
            if any((current / name).is_symlink() for name in [*child_directories, *files]):
                raise PluginPackageError("plugin virtual environment must not contain symbolic links")
        validate_plugin_tree(version_root)
        return python_path.resolve()
