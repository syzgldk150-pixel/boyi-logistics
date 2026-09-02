"""Offline, deterministic developer tooling for Service v2 plugin artifacts.

This module deliberately stops at the local package boundary.  It never loads
deployment configuration, connects to a Host service, or installs a plugin.
Both source directories and existing ZIP files pass through the same
authoritative package verifier and project-contract projection.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import secrets
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginPackageError
from agent.automation_plugins.inspection_v2 import service_v2_wizard_projection
from agent.automation_plugins.package_v2 import (
    MAX_ARCHIVE_BYTES,
    MAX_FILES,
    MAX_FILE_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    VerifiedPluginPackageV2,
    verify_unsigned_plugin_zip_v2,
)
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract


_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_SDK_ARCHIVE_PATH = "payload/boyi_plugin_sdk.py"
_SDK_SOURCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "service_v2_plugins"
    / "_shared"
    / "boyi_plugin_sdk.py"
)
_SENSITIVE_EXACT_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        "accesstoken",
        "apikey",
        "auth",
        "authorization",
        "cert",
        "certificate",
        "cookie",
        "clientsecret",
        "credentials",
        "credential",
        "id_ed25519",
        "id_rsa",
        "key",
        "passwd",
        "password",
        "private_key",
        "privatekey",
        "refreshtoken",
        "secret",
        "secrets",
        "session",
        "sessiontoken",
        "sessions",
        "token",
        "tokens",
    }
)
_SENSITIVE_SUFFIXES = frozenset(
    {".cer", ".crt", ".der", ".key", ".p12", ".pem", ".pfx"}
)
_SENSITIVE_TOKEN_RE = re.compile(r"[a-z0-9]+")
DEFAULT_LOCAL_JSON_MAX_BYTES = 1024 * 1024
_NATIVE_DIRFD_OPERATIONS = frozenset(os.supports_dir_fd)
_NATIVE_SECURE_DIRFD_AVAILABLE = all(
    operation in _NATIVE_DIRFD_OPERATIONS
    for operation in (os.open, os.stat, os.mkdir, os.unlink, os.rmdir)
)
_NATIVE_LINK_DIRFD_AVAILABLE = os.link in _NATIVE_DIRFD_OPERATIONS


class ServiceV2DeveloperError(RuntimeError):
    """Stable local developer-tool failure without installation side effects."""

    code = "SERVICE_V2_DEVELOPER_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.code


@dataclass
class _ScannedRegularFile:
    path: Path
    archive_path: str
    stat_result: os.stat_result
    parent_descriptor: int
    entry_name: str
    parent_path: Path
    parent_stat_result: os.stat_result


@dataclass(frozen=True)
class _AnchoredDirectory:
    """A directory reached from ``/`` without resolving any symlink."""

    path: Path
    descriptor: int
    stat_result: os.stat_result


def _same_file_snapshot(actual: Any, expected: Any) -> bool:
    return all(
        getattr(actual, field) == getattr(expected, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _same_directory_identity(actual: Any, expected: Any) -> bool:
    return (
        getattr(actual, "st_dev") == getattr(expected, "st_dev")
        and getattr(actual, "st_ino") == getattr(expected, "st_ino")
    )


def _same_regular_file_identity(actual: Any, expected: Any) -> bool:
    return (
        stat.S_ISREG(getattr(actual, "st_mode"))
        and stat.S_ISREG(getattr(expected, "st_mode"))
        and getattr(actual, "st_dev") == getattr(expected, "st_dev")
        and getattr(actual, "st_ino") == getattr(expected, "st_ino")
    )


def _require_secure_dirfd_primitives(*, require_link: bool = False) -> None:
    """Refuse local filesystem work unless every path step can be anchored.

    The developer tool intentionally has no Windows/path-string fallback.  A
    final-leaf ``O_NOFOLLOW`` check does not protect against an ancestor swap,
    so every directory transition must be an ``openat`` transition.
    """

    if (
        os.name != "posix"
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not _NATIVE_SECURE_DIRFD_AVAILABLE
        or (require_link and not _NATIVE_LINK_DIRFD_AVAILABLE)
    ):
        raise ServiceV2DeveloperError(
            "secure local filesystem primitives are unavailable",
            code="SECURE_FILESYSTEM_UNAVAILABLE",
        )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | int(getattr(os, "O_DIRECTORY", 0) or 0)
        | int(getattr(os, "O_NOFOLLOW", 0) or 0)
        | int(getattr(os, "O_CLOEXEC", 0) or 0)
    )


def _absolute_without_following(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_anchored_directory(
    path: Path | str,
    *,
    missing_message: str,
    invalid_code: str = "LOCAL_ARTIFACT_UNAVAILABLE",
) -> _AnchoredDirectory:
    """Open an absolute directory one component at a time from ``/``."""

    _require_secure_dirfd_primitives()
    absolute = _absolute_without_following(path)
    if not absolute.is_absolute():  # pragma: no cover - ``abspath`` guarantees it.
        raise ServiceV2DeveloperError(
            "local artifact path must be absolute",
            code=invalid_code,
        )
    descriptor = -1
    transferred = False
    try:
        descriptor = os.open("/", _directory_open_flags())
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=descriptor,
            )
            previous_descriptor = descriptor
            descriptor = next_descriptor
            _close_descriptor(previous_descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ServiceV2DeveloperError(
                "local artifact directory is invalid",
                code=invalid_code,
            )
        anchored = _AnchoredDirectory(absolute, descriptor, metadata)
        transferred = True
        return anchored
    except FileNotFoundError as exc:
        raise ServiceV2DeveloperError(missing_message, code="LOCAL_ARTIFACT_NOT_FOUND") from exc
    except ServiceV2DeveloperError:
        raise
    except OSError as exc:
        raise ServiceV2DeveloperError(
            "local artifact directory cannot be opened safely",
            code=invalid_code,
        ) from exc
    finally:
        if descriptor >= 0 and not transferred:
            _close_descriptor(descriptor)


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is None or descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _entry_lstat(
    directory_descriptor: int,
    name: str,
    *,
    missing_message: str,
) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ServiceV2DeveloperError(
            missing_message,
            code="LOCAL_ARTIFACT_NOT_FOUND",
        ) from exc
    except OSError as exc:
        raise ServiceV2DeveloperError(
            "local artifact metadata is unavailable",
            code="LOCAL_ARTIFACT_UNAVAILABLE",
        ) from exc


def _entry_exists(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ServiceV2DeveloperError(
            "local output metadata is unavailable",
            code="LOCAL_ARTIFACT_UNAVAILABLE",
        ) from exc
    return True


def _assert_directory_path_matches(
    path: Path,
    expected: os.stat_result,
    *,
    code: str = "LOCAL_ARTIFACT_CHANGED",
) -> None:
    """Ensure a lexical path still resolves to the held directory identity."""

    try:
        reopened = _open_anchored_directory(
            path,
            missing_message="local artifact directory changed while in use",
            invalid_code=code,
        )
    except ServiceV2DeveloperError as exc:
        if exc.code == code:
            raise
        raise ServiceV2DeveloperError(
            "local artifact directory changed while in use",
            code=code,
        ) from exc
    try:
        if not _same_directory_identity(reopened.stat_result, expected):
            raise ServiceV2DeveloperError(
                "local artifact directory changed while in use",
                code=code,
            )
    finally:
        _close_descriptor(reopened.descriptor)


def _is_sensitive_entry_name(name: str) -> bool:
    """Classify a directory-entry name without opening the named object."""

    normalized = unicodedata.normalize("NFKC", str(name)).casefold()
    if normalized.startswith(".env"):
        return True
    suffix = PurePosixPath(normalized).suffix
    if suffix in _SENSITIVE_SUFFIXES:
        return True
    stem = PurePosixPath(normalized).stem
    tokens = set(_SENSITIVE_TOKEN_RE.findall(stem))
    return bool(tokens & _SENSITIVE_EXACT_NAMES) or stem in _SENSITIVE_EXACT_NAMES


def _read_scanned_regular_file(
    item: _ScannedRegularFile,
    *,
    maximum: int,
) -> bytes:
    """Read the exact regular file that was scanned, without following links."""

    descriptor = -1
    try:
        _assert_directory_path_matches(item.parent_path, item.parent_stat_result)
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
        flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
        flags |= int(getattr(os, "O_CLOEXEC", 0) or 0)
        try:
            descriptor = os.open(item.entry_name, flags, dir_fd=item.parent_descriptor)
        except OSError as exc:
            raise ServiceV2DeveloperError(
                "local artifact file cannot be opened safely",
                code="LOCAL_ARTIFACT_UNAVAILABLE",
            ) from exc
        current = os.fstat(descriptor)
        expected = item.stat_result
        if (
            not stat.S_ISREG(current.st_mode)
            or not _same_file_snapshot(current, expected)
        ):
            raise ServiceV2DeveloperError(
                "local artifact changed while it was being inspected",
                code="LOCAL_ARTIFACT_CHANGED",
            )
        if current.st_size < 0 or current.st_size > maximum:
            raise PluginPackageError("local artifact file exceeds its size limit")
        chunks: list[bytes] = []
        remaining = current.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ServiceV2DeveloperError(
                    "local artifact changed while it was being read",
                    code="LOCAL_ARTIFACT_CHANGED",
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ServiceV2DeveloperError(
                "local artifact changed while it was being read",
                code="LOCAL_ARTIFACT_CHANGED",
            )
        if not _same_file_snapshot(os.fstat(descriptor), expected):
            raise ServiceV2DeveloperError(
                "local artifact changed while it was being read",
                code="LOCAL_ARTIFACT_CHANGED",
            )
        return b"".join(chunks)
    finally:
        # This object owns the duplicate parent fd.  Mark it consumed before
        # the descriptor number can be reused, so a later aggregate cleanup
        # cannot accidentally close an unrelated newly opened fd.
        parent_descriptor = item.parent_descriptor
        item.parent_descriptor = -1
        _close_descriptor(descriptor)
        _close_descriptor(parent_descriptor)


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def load_local_json_object(
    path: Path | str,
    *,
    maximum_bytes: int = DEFAULT_LOCAL_JSON_MAX_BYTES,
) -> dict[str, Any]:
    """Read one explicit, non-sensitive regular file as a strict JSON object."""

    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes <= 0
    ):
        raise ValueError("maximum_bytes must be a positive integer")
    candidate = _absolute_without_following(path)
    scanned = _scan_regular_file(candidate, candidate.name)
    content = _read_scanned_regular_file(scanned, maximum=maximum_bytes)
    try:
        text = content.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ServiceV2DeveloperError(
            "local JSON must be one strict UTF-8 object",
            code="LOCAL_JSON_INVALID",
        ) from exc
    if not isinstance(value, dict):
        raise ServiceV2DeveloperError(
            "local JSON must contain one object",
            code="LOCAL_JSON_INVALID",
        )
    return value


def _scan_regular_file(path: Path, archive_path: str) -> _ScannedRegularFile:
    if _is_sensitive_entry_name(path.name):
        raise ServiceV2DeveloperError(
            "local artifact uses a forbidden sensitive-name candidate",
            code="SENSITIVE_SOURCE_NAME",
        )
    parent = _open_anchored_directory(
        path.parent,
        missing_message="local artifact parent directory does not exist",
    )
    try:
        metadata = _entry_lstat(
            parent.descriptor,
            path.name,
            missing_message="local artifact file does not exist",
        )
        if not stat.S_ISREG(metadata.st_mode):
            raise ServiceV2DeveloperError(
                "local artifact members must be regular files",
                code="UNSAFE_SOURCE_OBJECT",
            )
        return _ScannedRegularFile(
            path=path,
            archive_path=archive_path,
            stat_result=metadata,
            parent_descriptor=parent.descriptor,
            entry_name=path.name,
            parent_path=parent.path,
            parent_stat_result=parent.stat_result,
        )
    except Exception:
        _close_descriptor(parent.descriptor)
        raise


def _scan_payload_directory(
    payload_root: Path,
    current: Path,
    current_descriptor: int,
    current_metadata: os.stat_result,
    files: list[_ScannedRegularFile],
    *,
    archive_prefix: str = "payload",
) -> None:
    try:
        names = sorted(os.listdir(current_descriptor))
    except OSError as exc:
        raise ServiceV2DeveloperError(
            "source payload cannot be enumerated safely",
            code="LOCAL_ARTIFACT_UNAVAILABLE",
        ) from exc
    # Names are classified for sensitivity before any member content is read.
    if any(_is_sensitive_entry_name(name) for name in names):
        raise ServiceV2DeveloperError(
            "source tree contains a forbidden sensitive-name candidate",
            code="SENSITIVE_SOURCE_NAME",
        )
    directories: list[tuple[Path, str, os.stat_result]] = []
    for name in names:
        path = current / name
        try:
            metadata = os.stat(name, dir_fd=current_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ServiceV2DeveloperError(
                "source payload metadata is unavailable",
                code="LOCAL_ARTIFACT_UNAVAILABLE",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ServiceV2DeveloperError(
                "source tree cannot contain symbolic links",
                code="UNSAFE_SOURCE_OBJECT",
            )
        if stat.S_ISDIR(metadata.st_mode):
            directories.append((path, name, metadata))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ServiceV2DeveloperError(
                "source tree can contain only directories and regular files",
                code="UNSAFE_SOURCE_OBJECT",
            )
        relative = path.relative_to(payload_root).as_posix()
        archive_path = f"{archive_prefix}/{relative}"
        if archive_prefix == "payload" and archive_path.casefold() == _SDK_ARCHIVE_PATH.casefold():
            raise ServiceV2DeveloperError(
                "source must not provide the Host-owned Service v2 SDK",
                code="SDK_SOURCE_FORBIDDEN",
            )
        files.append(
            _ScannedRegularFile(
                path=path,
                archive_path=archive_path,
                stat_result=metadata,
                parent_descriptor=os.dup(current_descriptor),
                entry_name=name,
                parent_path=current,
                parent_stat_result=current_metadata,
            )
        )
    for directory, name, expected in directories:
        try:
            child_descriptor = os.open(
                name,
                _directory_open_flags(),
                dir_fd=current_descriptor,
            )
        except OSError as exc:
            raise ServiceV2DeveloperError(
                "source payload directory cannot be opened safely",
                code="LOCAL_ARTIFACT_CHANGED",
            ) from exc
        try:
            current = os.fstat(child_descriptor)
            if not _same_directory_identity(current, expected):
                raise ServiceV2DeveloperError(
                    "source payload directory changed while it was being inspected",
                    code="LOCAL_ARTIFACT_CHANGED",
                )
            _scan_payload_directory(
                payload_root,
                directory,
                child_descriptor,
                current,
                files,
                archive_prefix=archive_prefix,
            )
        finally:
            _close_descriptor(child_descriptor)


def _scan_source_from_anchor(
    root: Path,
    root_descriptor: int,
) -> tuple[_ScannedRegularFile, ...]:
    if _is_sensitive_entry_name(root.name):
        raise ServiceV2DeveloperError(
            "source directory uses a forbidden sensitive-name candidate",
            code="SENSITIVE_SOURCE_NAME",
        )
    try:
        root_names = sorted(os.listdir(root_descriptor))
    except OSError as exc:
        raise ServiceV2DeveloperError(
            "source directory cannot be enumerated safely",
            code="LOCAL_ARTIFACT_UNAVAILABLE",
        ) from exc
    if any(_is_sensitive_entry_name(name) for name in root_names):
        raise ServiceV2DeveloperError(
            "source tree contains a forbidden sensitive-name candidate",
            code="SENSITIVE_SOURCE_NAME",
        )
    if set(root_names) not in ({"manifest.json", "payload"}, {"manifest.json", "payload", "settings"}):
        raise ServiceV2DeveloperError(
            "source root must contain only manifest.json, payload/ and optional settings/",
            code="SOURCE_LAYOUT_INVALID",
        )
    try:
        manifest_metadata = os.stat(
            "manifest.json",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        payload_metadata = os.stat(
            "payload",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ServiceV2DeveloperError(
            "source root metadata is unavailable",
            code="LOCAL_ARTIFACT_UNAVAILABLE",
        ) from exc
    if (
        stat.S_ISLNK(manifest_metadata.st_mode)
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or stat.S_ISLNK(payload_metadata.st_mode)
        or not stat.S_ISDIR(payload_metadata.st_mode)
    ):
        raise ServiceV2DeveloperError(
            "manifest.json must be regular and payload/ must be a real directory",
            code="UNSAFE_SOURCE_OBJECT",
        )
    files = [
        _ScannedRegularFile(
            path=root / "manifest.json",
            archive_path="manifest.json",
            stat_result=manifest_metadata,
            parent_descriptor=os.dup(root_descriptor),
            entry_name="manifest.json",
            parent_path=root,
            parent_stat_result=os.fstat(root_descriptor),
        )
    ]
    try:
        payload_descriptor = os.open(
            "payload",
            _directory_open_flags(),
            dir_fd=root_descriptor,
        )
    except OSError as exc:
        _close_descriptor(files[0].parent_descriptor)
        raise ServiceV2DeveloperError(
            "source payload directory cannot be opened safely",
            code="LOCAL_ARTIFACT_CHANGED",
        ) from exc
    try:
        if not _same_directory_identity(os.fstat(payload_descriptor), payload_metadata):
            raise ServiceV2DeveloperError(
                "source payload directory changed while it was being inspected",
                code="LOCAL_ARTIFACT_CHANGED",
            )
        _scan_payload_directory(
            root / "payload",
            root / "payload",
            payload_descriptor,
            payload_metadata,
            files,
        )
        if "settings" in root_names:
            try:
                settings_metadata = os.stat(
                    "settings",
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                settings_descriptor = os.open(
                    "settings",
                    _directory_open_flags(),
                    dir_fd=root_descriptor,
                )
            except OSError as exc:
                raise ServiceV2DeveloperError(
                    "source settings directory cannot be opened safely",
                    code="LOCAL_ARTIFACT_CHANGED",
                ) from exc
            try:
                if not stat.S_ISDIR(settings_metadata.st_mode) or not _same_directory_identity(
                    os.fstat(settings_descriptor), settings_metadata
                ):
                    raise ServiceV2DeveloperError(
                        "source settings directory changed while it was being inspected",
                        code="LOCAL_ARTIFACT_CHANGED",
                    )
                _scan_payload_directory(
                    root / "settings",
                    root / "settings",
                    settings_descriptor,
                    settings_metadata,
                    files,
                    archive_prefix="settings",
                )
            finally:
                _close_descriptor(settings_descriptor)
        if len(files) + 1 > MAX_FILES:
            raise PluginPackageError("source tree exceeds the package file-count limit")
        if any(item.stat_result.st_size > MAX_FILE_BYTES for item in files):
            raise PluginPackageError("source tree contains a file above the package size limit")
        if sum(item.stat_result.st_size for item in files) > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise PluginPackageError("source tree exceeds the package size limit")
        return tuple(sorted(files, key=lambda item: item.archive_path))
    except Exception:
        _close_scanned_files(files)
        raise
    finally:
        _close_descriptor(payload_descriptor)


def _close_scanned_files(files: list[_ScannedRegularFile] | tuple[_ScannedRegularFile, ...]) -> None:
    for item in files:
        descriptor = item.parent_descriptor
        item.parent_descriptor = -1
        _close_descriptor(descriptor)


def _scan_source(source: Path | str) -> tuple[Path, os.stat_result, tuple[_ScannedRegularFile, ...]]:
    root = _absolute_without_following(source)
    if _is_sensitive_entry_name(root.name):
        raise ServiceV2DeveloperError(
            "source directory uses a forbidden sensitive-name candidate",
            code="SENSITIVE_SOURCE_NAME",
        )
    anchor = _open_anchored_directory(
        root,
        missing_message="source directory does not exist",
    )
    try:
        return root, anchor.stat_result, _scan_source_from_anchor(
            root,
            anchor.descriptor,
        )
    finally:
        _close_descriptor(anchor.descriptor)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o444) << 16
    return info


def _host_sdk_bytes() -> bytes:
    sdk_item = _scan_regular_file(_SDK_SOURCE_PATH, _SDK_ARCHIVE_PATH)
    return _read_scanned_regular_file(sdk_item, maximum=MAX_FILE_BYTES)


def _package_entries(entries: Mapping[str, bytes]) -> bytes:
    if len(entries) > MAX_FILES:
        raise PluginPackageError("source tree exceeds the package file-count limit")
    if sum(len(content) for content in entries.values()) > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise PluginPackageError("source tree exceeds the package size limit")
    stream = io.BytesIO()
    with zipfile.ZipFile(
        stream,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
    ) as archive:
        for name in sorted(entries):
            archive.writestr(_zip_info(name), entries[name])
    package_bytes = stream.getvalue()
    if len(package_bytes) > MAX_ARCHIVE_BYTES:
        raise PluginPackageError("built Service v2 package exceeds the archive size limit")
    return package_bytes


def _source_package_bytes(source: Path | str) -> bytes:
    root, root_metadata, scanned_files = _scan_source(source)
    try:
        return _source_package_bytes_from_scanned(root, root_metadata, scanned_files)
    finally:
        _close_scanned_files(scanned_files)


def _source_package_bytes_from_scanned(
    root: Path,
    root_metadata: os.stat_result,
    scanned_files: tuple[_ScannedRegularFile, ...],
) -> bytes:
    # Holding a directory fd prevents a swap from redirecting reads.  The
    # lexical identity check additionally makes a concurrent rename/symlink
    # replacement visible to the caller instead of silently accepting it.
    _assert_directory_path_matches(root, root_metadata)
    entries: dict[str, bytes] = {}
    try:
        for item in scanned_files:
            entries[item.archive_path] = _read_scanned_regular_file(
                item,
                maximum=MAX_FILE_BYTES,
            )
        _assert_directory_path_matches(root, root_metadata)
    finally:
        _close_scanned_files(scanned_files)
    entries[_SDK_ARCHIVE_PATH] = _host_sdk_bytes()
    return _package_entries(entries)


def _preflight_zip_sensitive_names(package_bytes: bytes) -> None:
    """Reject secret-looking ZIP paths from central-directory metadata only.

    ``ZipInfo`` comes from the central directory and does not open/decompress a
    member.  Invalid ZIP bytes intentionally fall through to the authoritative
    verifier, preserving its public ``PluginPackageError`` contract.
    """

    try:
        with zipfile.ZipFile(io.BytesIO(package_bytes), mode="r") as archive:
            names = tuple(info.filename for info in archive.infolist())
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return
    for name in names:
        for component in re.split(r"[\\\\/]+", name):
            if component and _is_sensitive_entry_name(component):
                raise ServiceV2DeveloperError(
                    "plugin ZIP contains a forbidden sensitive-name candidate",
                    code="SENSITIVE_SOURCE_NAME",
                )


def _verify_package_bytes(
    package_bytes: bytes,
) -> tuple[VerifiedPluginPackageV2, ServiceV2ProjectContract]:
    _preflight_zip_sensitive_names(package_bytes)
    package_sha256 = hashlib.sha256(package_bytes).hexdigest()
    verified = verify_unsigned_plugin_zip_v2(
        package_bytes,
        transport_sha256=package_sha256,
    )
    contract = ServiceV2ProjectContract.from_manifest(verified.manifest)
    return verified, contract


def _load_verified_and_contract(
    path: Path | str,
) -> tuple[VerifiedPluginPackageV2, ServiceV2ProjectContract]:
    candidate = _absolute_without_following(path)
    if _is_sensitive_entry_name(candidate.name):
        raise ServiceV2DeveloperError(
            "local artifact uses a forbidden sensitive-name candidate",
            code="SENSITIVE_SOURCE_NAME",
        )
    parent = _open_anchored_directory(
        candidate.parent,
        missing_message="local artifact parent directory does not exist",
    )
    try:
        metadata = _entry_lstat(
            parent.descriptor,
            candidate.name,
            missing_message="local artifact does not exist",
        )
        if stat.S_ISDIR(metadata.st_mode):
            try:
                source_descriptor = os.open(
                    candidate.name,
                    _directory_open_flags(),
                    dir_fd=parent.descriptor,
                )
            except OSError as exc:
                raise ServiceV2DeveloperError(
                    "local source directory cannot be opened safely",
                    code="LOCAL_ARTIFACT_CHANGED",
                ) from exc
            try:
                source_metadata = os.fstat(source_descriptor)
                if not _same_directory_identity(source_metadata, metadata):
                    raise ServiceV2DeveloperError(
                        "local source directory changed while it was being inspected",
                        code="LOCAL_ARTIFACT_CHANGED",
                    )
                scanned_files = _scan_source_from_anchor(
                    candidate,
                    source_descriptor,
                )
                try:
                    package_bytes = _source_package_bytes_from_scanned(
                        candidate,
                        source_metadata,
                        scanned_files,
                    )
                finally:
                    _close_scanned_files(scanned_files)
            finally:
                _close_descriptor(source_descriptor)
        elif stat.S_ISREG(metadata.st_mode):
            package_bytes = _read_scanned_regular_file(
                _ScannedRegularFile(
                    candidate,
                    candidate.name,
                    metadata,
                    os.dup(parent.descriptor),
                    candidate.name,
                    parent.path,
                    parent.stat_result,
                ),
                maximum=MAX_ARCHIVE_BYTES,
            )
        else:
            raise ServiceV2DeveloperError(
                "local artifact must be a regular source directory or ZIP file",
                code="LOCAL_ARTIFACT_TYPE_INVALID",
            )
    finally:
        if parent is not None:
            _close_descriptor(parent.descriptor)
    return _verify_package_bytes(package_bytes)


def load_verified_local_artifact(path: Path | str) -> VerifiedPluginPackageV2:
    """Load a source tree or ZIP through the authoritative v2 verification chain."""

    verified, _ = _load_verified_and_contract(path)
    return verified


def _identity_projection(
    verified: VerifiedPluginPackageV2,
    contract: ServiceV2ProjectContract,
) -> dict[str, Any]:
    return {
        "schema_version": verified.manifest.schema_version,
        "runtime_model": verified.manifest.runtime_model,
        "plugin_id": verified.manifest.plugin_id,
        "version": verified.manifest.version,
        "package_sha256": verified.package_sha256,
        "manifest_sha256": verified.manifest_sha256,
        "files_sha256": verified.files_sha256,
        "runtime_sha256": verified.runtime_sha256,
        "service_contracts_sha256": verified.service_contracts_sha256,
        "contributions_sha256": verified.contributions_sha256,
        "capabilities_sha256": verified.capabilities_sha256,
        "storage_sha256": verified.storage_sha256,
        "governance_anchor_sha256": contract.governance_anchor_sha256,
    }


def _contract_projection(contract: ServiceV2ProjectContract) -> dict[str, Any]:
    manifest = contract.manifest
    return {
        "provided_services": list(manifest.provided_services),
        "required_services": list(manifest.required_services),
        "allowed_entrypoints": list(contract.allowed_entrypoints),
        "default_entrypoints": list(contract.default_entrypoints),
        "contribution_kinds": {
            key: str(contract.contribution_kinds[key])
            for key in sorted(contract.contribution_kinds)
        },
        "runtime": {
            "kind": str(manifest.runtime["kind"]),
            "python": str(manifest.runtime["python"]),
            "mode": str(manifest.runtime["mode"]),
            "entrypoint": str(manifest.runtime["entrypoint"]),
        },
        "tool": {
            key: copy.deepcopy(contract.tool_contract[key])
            for key in (
                "name",
                "version",
                "effect",
                "risk_level",
                "broker_effect",
                "mutating",
            )
        },
        "scheduling": copy.deepcopy(dict(contract.scheduling)),
    }


def validate_service_v2_artifact(path: Path | str) -> dict[str, Any]:
    """Validate a local artifact and return a stable, content-derived receipt."""

    verified, contract = _load_verified_and_contract(path)
    return {
        "valid": True,
        "identity": _identity_projection(verified, contract),
        "contract": _contract_projection(contract),
    }


def inspect_service_v2_artifact(path: Path | str) -> dict[str, Any]:
    """Return safe package identities and summaries without payload contents."""

    verified, contract = _load_verified_and_contract(path)
    return {
        "identity": _identity_projection(verified, contract),
        "members": [item.to_mapping() for item in verified.files],
        "contract": _contract_projection(contract),
        "wizard": service_v2_wizard_projection(verified),
    }


def _open_output_parent(path: Path) -> _AnchoredDirectory:
    return _open_anchored_directory(
        path.parent,
        missing_message="output parent directory does not exist",
        invalid_code="OUTPUT_PARENT_INVALID",
    )


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("local filesystem short write")
        offset += written


def _create_temporary_output(parent_descriptor: int, output_name: str) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_NOFOLLOW", 0) or 0)
        | int(getattr(os, "O_CLOEXEC", 0) or 0)
    )
    for _ in range(32):
        temporary_name = f".{output_name}.{secrets.token_hex(16)}.tmp"
        try:
            return (
                os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor),
                temporary_name,
            )
        except FileExistsError:
            continue
    raise ServiceV2DeveloperError(
        "could not allocate a unique temporary output name",
        code="LOCAL_ARTIFACT_UNAVAILABLE",
    )


def _output_entry_metadata(
    parent_descriptor: int,
    output_name: str,
    *,
    missing_code: str,
) -> os.stat_result:
    try:
        return os.stat(
            output_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise ServiceV2DeveloperError(
            "published package output disappeared",
            code=missing_code,
        ) from exc
    except OSError as exc:
        raise ServiceV2DeveloperError(
            "published package output metadata is unavailable",
            code=missing_code,
        ) from exc


def _remove_published_output_if_owned(
    parent_descriptor: int,
    output_name: str,
    expected: os.stat_result,
) -> None:
    try:
        actual = os.stat(
            output_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if not _same_regular_file_identity(actual, expected):
        return
    os.unlink(output_name, dir_fd=parent_descriptor)


def build_service_v2_package(
    source: Path | str,
    output: Path | str,
) -> Path:
    """Build one verified deterministic ZIP at a caller-selected new path."""

    output_path = _absolute_without_following(output)
    if _is_sensitive_entry_name(output_path.name):
        raise ServiceV2DeveloperError(
            "output name is a forbidden sensitive-name candidate",
            code="SENSITIVE_OUTPUT_NAME",
        )
    _require_secure_dirfd_primitives(require_link=True)
    parent = _open_output_parent(output_path)
    temporary_descriptor = -1
    temporary_name: str | None = None
    published_metadata: os.stat_result | None = None
    published_snapshot: os.stat_result | None = None
    try:
        if _entry_exists(parent.descriptor, output_path.name):
            raise FileExistsError("output artifact already exists")
        package_bytes = _source_package_bytes(source)
        _verify_package_bytes(package_bytes)
        _assert_directory_path_matches(
            parent.path,
            parent.stat_result,
            code="OUTPUT_PARENT_CHANGED",
        )
        if _entry_exists(parent.descriptor, output_path.name):
            raise FileExistsError("output artifact already exists")
        temporary_descriptor, temporary_name = _create_temporary_output(
            parent.descriptor,
            output_path.name,
        )
        _write_all(temporary_descriptor, package_bytes)
        os.fsync(temporary_descriptor)
        _assert_directory_path_matches(
            parent.path,
            parent.stat_result,
            code="OUTPUT_PARENT_CHANGED",
        )
        try:
            os.link(
                temporary_name,
                output_path.name,
                src_dir_fd=parent.descriptor,
                dst_dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise FileExistsError("output artifact already exists") from exc
        temporary_metadata = os.fstat(temporary_descriptor)
        linked_metadata = _output_entry_metadata(
            parent.descriptor,
            output_path.name,
            missing_code="OUTPUT_ARTIFACT_CHANGED",
        )
        if not _same_regular_file_identity(linked_metadata, temporary_metadata):
            raise ServiceV2DeveloperError(
                "published package output changed during publication",
                code="OUTPUT_ARTIFACT_CHANGED",
            )
        published_metadata = linked_metadata
        os.unlink(temporary_name, dir_fd=parent.descriptor)
        temporary_name = None
        published_snapshot = _output_entry_metadata(
            parent.descriptor,
            output_path.name,
            missing_code="OUTPUT_ARTIFACT_CHANGED",
        )
        held_metadata = os.fstat(temporary_descriptor)
        if (
            not _same_regular_file_identity(published_snapshot, published_metadata)
            or not _same_file_snapshot(published_snapshot, held_metadata)
        ):
            raise ServiceV2DeveloperError(
                "published package output changed during publication",
                code="OUTPUT_ARTIFACT_CHANGED",
            )
        os.fsync(parent.descriptor)
        _assert_directory_path_matches(
            parent.path,
            parent.stat_result,
            code="OUTPUT_PARENT_CHANGED",
        )
        final_metadata = _output_entry_metadata(
            parent.descriptor,
            output_path.name,
            missing_code="OUTPUT_ARTIFACT_CHANGED",
        )
        if (
            not _same_regular_file_identity(final_metadata, published_metadata)
            or published_snapshot is None
            or not _same_file_snapshot(final_metadata, published_snapshot)
        ):
            raise ServiceV2DeveloperError(
                "published package output changed before completion",
                code="OUTPUT_ARTIFACT_CHANGED",
            )
    except Exception:
        cleanup_error: OSError | None = None
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent.descriptor)
            except FileNotFoundError:
                pass
            except OSError as temporary_error:
                cleanup_error = temporary_error
        if published_metadata is not None:
            try:
                _remove_published_output_if_owned(
                    parent.descriptor,
                    output_path.name,
                    published_metadata,
                )
            except FileNotFoundError:
                pass
            except (OSError, ServiceV2DeveloperError) as output_error:
                cleanup_error = cleanup_error or output_error
        if cleanup_error is not None:
            raise ServiceV2DeveloperError(
                "failed package output cleanup was incomplete",
                code="PACKAGE_CLEANUP_FAILED",
            ) from cleanup_error
        raise
    finally:
        _close_descriptor(temporary_descriptor)
        _close_descriptor(parent.descriptor)
    return Path(output)


_MINIMAL_MAIN_TEMPLATE = '''"""Minimal no-write Service v2 compute example."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone


PLUGIN_ID = __PLUGIN_ID__
PLUGIN_VERSION = __PLUGIN_VERSION__
SERVICE_NAME = __SERVICE_NAME__
_REQUEST_FIELDS = {
    "schema_version",
    "runtime_model",
    "automation_id",
    "plugin_id",
    "plugin_version",
    "entrypoint",
    "target",
    "governance",
    "arguments",
}
_EXPECTED_GOVERNANCE = {
    "effect": "compute",
    "operation_type": "compute",
    "risk_level": "low",
    "lock_class": "none",
    "evidence": {"required": False, "required_fields": []},
    "postconditions": [],
    "retry": {"safe": True, "max_attempts": 3},
    "harness_allowed": True,
    "broker_effect": "read",
    "approval": {"mode": "project_policy"},
    "idempotency": {"mode": "parameters", "key_fields": []},
    "project_full_auto_allowed": True,
}


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _meta(*, record_count: int) -> dict[str, object]:
    return {
        "source_system": "service_v2_example",
        "observed_at": _observed_at(),
        "record_count": record_count,
        "pagination_complete": True,
        "evidence_refs": [],
    }


def _read_request() -> dict[str, object]:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("service request schema is invalid")
    automation_id = os.environ.get("BOYI_AUTOMATION_ID", "")
    if (
        request.get("schema_version") != 2
        or request.get("runtime_model") != "SERVICE_V2"
        or not automation_id
        or request.get("automation_id") != automation_id
        or request.get("plugin_id") != PLUGIN_ID
        or request.get("plugin_id") != os.environ.get("BOYI_PLUGIN_ID", "")
        or request.get("plugin_version") != PLUGIN_VERSION
        or request.get("plugin_version") != os.environ.get("BOYI_PLUGIN_VERSION", "")
        or request.get("entrypoint") not in {"console", "harness"}
        or request.get("arguments") != {}
    ):
        raise ValueError("service request identity is invalid")
    contribution_id = "run" if request.get("entrypoint") == "console" else "assistant_run"
    expected_target = {
        "service": SERVICE_NAME,
        "operation": "run",
        "contribution_id": contribution_id,
        "contribution_kind": str(request.get("entrypoint")),
    }
    target = request.get("target")
    governance = request.get("governance")
    if not isinstance(target, Mapping) or dict(target) != expected_target:
        raise ValueError("service target is invalid")
    if not isinstance(governance, Mapping) or dict(governance) != _EXPECTED_GOVERNANCE:
        raise ValueError("service governance is invalid")
    return request


def main() -> int:
    try:
        _read_request()
        result = {
            "status": "SUCCESS",
            "data": {"message": "Service v2 example is ready."},
            "meta": _meta(record_count=1),
            "warnings": [],
            "error": None,
        }
    except (ValueError, json.JSONDecodeError):
        result = {
            "status": "FAILED",
            "data": {},
            "meta": _meta(record_count=0),
            "warnings": [],
            "error": {"code": "INVALID_REQUEST", "message": "request rejected"},
        }
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _minimal_main_source(*, plugin_id: str, version: str) -> bytes:
    service = f"plugin.{plugin_id}.example@1"
    source = (
        _MINIMAL_MAIN_TEMPLATE.replace("__PLUGIN_ID__", repr(plugin_id))
        .replace("__PLUGIN_VERSION__", repr(version))
        .replace("__SERVICE_NAME__", repr(service))
    )
    return source.encode("utf-8")


def _minimal_manifest(
    *,
    plugin_id: str,
    name: str,
    version: str,
) -> dict[str, Any]:
    service = f"plugin.{plugin_id}.example@1"
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": plugin_id,
        "name": name,
        "version": version,
        "description": "Minimal no-write Service v2 compute example.",
        "host_api": {
            "minimum": "2.0.0",
            "maximum_exclusive": "3.0.0",
        },
        "runtime": {
            "kind": "python_subprocess",
            "python": "3.10",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
            "requirements_lock": None,
            "wheelhouse": [],
        },
        "provides": [
            {
                "service": service,
                "operations": [{"name": "run", "effect": "compute"}],
            }
        ],
        "requires": [],
        "capabilities": [],
        "account_roles": [],
        "resource_roles": [],
        "contributes": {
            "console": [
                {
                    "id": "run",
                    "title": "Run compute example",
                    "service": service,
                    "operation": "run",
                    "default_enabled": True,
                }
            ],
            "scheduler": [],
            "webhook": [],
            "feishu": [],
            "events": [],
            "harness": [
                {
                    "id": "assistant_run",
                    "title": "运行计算示例",
                    "description": "运行当前插件的无写入计算示例并返回结果。",
                    "scenarios": ["运行这个计算插件", "检查计算示例"],
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                        "required": [],
                    },
                    "service": service,
                    "operation": "run",
                    "effect": "compute",
                    "confirmation_policy": "none",
                    "preview_operation": None,
                }
            ],
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "storage": {"kv": False, "collections": []},
    }


def _minimal_source_material(
    *,
    plugin_id: str,
    name: str,
    version: str,
) -> tuple[bytes, bytes]:
    manifest = _minimal_manifest(
        plugin_id=plugin_id,
        name=name,
        version=version,
    )
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    main_bytes = _minimal_main_source(plugin_id=plugin_id, version=version)
    package_bytes = _package_entries(
        {
            "manifest.json": manifest_bytes,
            "payload/main.py": main_bytes,
            _SDK_ARCHIVE_PATH: _host_sdk_bytes(),
        }
    )
    _verify_package_bytes(package_bytes)
    return manifest_bytes, main_bytes


def _write_new_file_at(
    parent_descriptor: int,
    name: str,
    content: bytes,
) -> os.stat_result:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    flags |= int(getattr(os, "O_CLOEXEC", 0) or 0)
    descriptor = os.open(name, flags, 0o644, dir_fd=parent_descriptor)
    created_metadata = os.fstat(descriptor)
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
        written_metadata = os.fstat(descriptor)
        try:
            linked_metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ServiceV2DeveloperError(
                "initial source file changed while it was being written",
                code="INIT_TARGET_CHANGED",
            ) from exc
        if not _same_file_snapshot(linked_metadata, written_metadata):
            raise ServiceV2DeveloperError(
                "initial source file changed while it was being written",
                code="INIT_TARGET_CHANGED",
            )
        return written_metadata
    except Exception:
        try:
            actual = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            actual = None
        except OSError as cleanup_exc:
            raise ServiceV2DeveloperError(
                "initial source file cleanup failed",
                code="INIT_CLEANUP_FAILED",
            ) from cleanup_exc
        if actual is not None:
            if not _same_regular_file_identity(actual, created_metadata):
                raise ServiceV2DeveloperError(
                    "initial source file was replaced before cleanup",
                    code="INIT_CLEANUP_FAILED",
                )
            try:
                os.unlink(name, dir_fd=parent_descriptor)
            except OSError as cleanup_exc:
                raise ServiceV2DeveloperError(
                    "initial source file cleanup failed",
                    code="INIT_CLEANUP_FAILED",
                ) from cleanup_exc
        raise
    finally:
        os.close(descriptor)


def _cleanup_initialized_source(
    *,
    parent_descriptor: int,
    target_name: str,
    target_descriptor: int | None,
    target_metadata: os.stat_result | None,
    target_created: bool,
    payload_descriptor: int | None,
    payload_metadata: os.stat_result | None,
    payload_created: bool,
    manifest_metadata: os.stat_result | None,
    main_metadata: os.stat_result | None,
) -> None:
    """Remove only the exact objects created by this invocation.

    Every removal is relative to a held descriptor and is preceded by an inode
    comparison, so a concurrent swap cannot turn rollback into deletion of a
    caller's replacement tree.
    """

    def remove_file(
        descriptor: int,
        name: str,
        expected: os.stat_result | None,
    ) -> None:
        if expected is None:
            return
        actual = _entry_lstat(
            descriptor,
            name,
            missing_message="initial source cleanup target disappeared",
        )
        if not _same_file_snapshot(actual, expected):
            raise ServiceV2DeveloperError(
                "initial source cleanup target changed",
                code="INIT_CLEANUP_FAILED",
            )
        os.unlink(name, dir_fd=descriptor)

    def remove_directory(
        descriptor: int,
        name: str,
        expected: os.stat_result | None,
    ) -> None:
        if expected is None:
            return
        actual = _entry_lstat(
            descriptor,
            name,
            missing_message="initial source cleanup target disappeared",
        )
        if not _same_directory_identity(actual, expected):
            raise ServiceV2DeveloperError(
                "initial source cleanup target changed",
                code="INIT_CLEANUP_FAILED",
            )
        os.rmdir(name, dir_fd=descriptor)

    try:
        if target_created and target_metadata is None:
            raise ServiceV2DeveloperError(
                "initial source target identity was not established",
                code="INIT_CLEANUP_FAILED",
            )
        if payload_created and payload_metadata is None:
            raise ServiceV2DeveloperError(
                "initial source payload identity was not established",
                code="INIT_CLEANUP_FAILED",
            )
        if payload_descriptor is not None:
            remove_file(payload_descriptor, "main.py", main_metadata)
        if target_descriptor is not None:
            remove_file(target_descriptor, "manifest.json", manifest_metadata)
            remove_directory(target_descriptor, "payload", payload_metadata)
        remove_directory(parent_descriptor, target_name, target_metadata)
    except (OSError, ServiceV2DeveloperError) as exc:
        raise ServiceV2DeveloperError(
            "initial source cleanup failed",
            code="INIT_CLEANUP_FAILED",
        ) from exc


def init_service_v2_source(
    destination: Path | str,
    *,
    plugin_id: str,
    name: str | None = None,
    version: str = "0.1.0",
) -> Path:
    """Create a new minimal compute-only Service v2 source directory."""

    target = _absolute_without_following(destination)
    if _is_sensitive_entry_name(target.name):
        raise ServiceV2DeveloperError(
            "source destination uses a forbidden sensitive-name candidate",
            code="SENSITIVE_OUTPUT_NAME",
        )
    _require_secure_dirfd_primitives()
    parent = _open_output_parent(target)
    target_descriptor: int | None = None
    payload_descriptor: int | None = None
    target_metadata: os.stat_result | None = None
    target_created = False
    payload_metadata: os.stat_result | None = None
    payload_created = False
    manifest_metadata: os.stat_result | None = None
    main_metadata: os.stat_result | None = None
    try:
        normalized_plugin_id = str(plugin_id)
        normalized_name = str(plugin_id if name is None else name)
        normalized_version = str(version)
        # Validate the exact generated bytes through the same package + project
        # contract authority before creating the caller-selected directory.
        manifest_bytes, main_bytes = _minimal_source_material(
            plugin_id=normalized_plugin_id,
            name=normalized_name,
            version=normalized_version,
        )
        if _entry_exists(parent.descriptor, target.name):
            raise FileExistsError("source destination already exists")
        _assert_directory_path_matches(
            parent.path,
            parent.stat_result,
            code="OUTPUT_PARENT_CHANGED",
        )
        os.mkdir(target.name, mode=0o755, dir_fd=parent.descriptor)
        target_created = True
        target_descriptor = os.open(
            target.name,
            _directory_open_flags(),
            dir_fd=parent.descriptor,
        )
        target_metadata = os.fstat(target_descriptor)
        if not stat.S_ISDIR(target_metadata.st_mode):
            raise ServiceV2DeveloperError(
                "initial source target is not a directory",
                code="INIT_TARGET_CHANGED",
            )
        _assert_directory_path_matches(
            parent.path,
            parent.stat_result,
            code="OUTPUT_PARENT_CHANGED",
        )
        _assert_directory_path_matches(
            target,
            target_metadata,
            code="INIT_TARGET_CHANGED",
        )
        os.mkdir("payload", mode=0o755, dir_fd=target_descriptor)
        payload_created = True
        payload_descriptor = os.open(
            "payload",
            _directory_open_flags(),
            dir_fd=target_descriptor,
        )
        payload_metadata = os.fstat(payload_descriptor)
        manifest_metadata = _write_new_file_at(
            target_descriptor,
            "manifest.json",
            manifest_bytes,
        )
        main_metadata = _write_new_file_at(
            payload_descriptor,
            "main.py",
            main_bytes,
        )
        scanned_files = _scan_source_from_anchor(
            target,
            target_descriptor,
        )
        try:
            package_bytes = _source_package_bytes_from_scanned(
                target,
                target_metadata,
                scanned_files,
            )
        finally:
            _close_scanned_files(scanned_files)
        _verify_package_bytes(package_bytes)
        _assert_directory_path_matches(
            parent.path,
            parent.stat_result,
            code="OUTPUT_PARENT_CHANGED",
        )
        _assert_directory_path_matches(
            target,
            target_metadata,
            code="INIT_TARGET_CHANGED",
        )
    except Exception:
        _cleanup_initialized_source(
            parent_descriptor=parent.descriptor,
            target_name=target.name,
            target_descriptor=target_descriptor,
            target_metadata=target_metadata,
            target_created=target_created,
            payload_descriptor=payload_descriptor,
            payload_metadata=payload_metadata,
            payload_created=payload_created,
            manifest_metadata=manifest_metadata,
            main_metadata=main_metadata,
        )
        raise
    finally:
        _close_descriptor(payload_descriptor)
        _close_descriptor(target_descriptor)
        _close_descriptor(parent.descriptor)
    return Path(destination)


__all__ = [
    "ServiceV2DeveloperError",
    "build_service_v2_package",
    "init_service_v2_source",
    "inspect_service_v2_artifact",
    "load_local_json_object",
    "load_verified_local_artifact",
    "validate_service_v2_artifact",
]
