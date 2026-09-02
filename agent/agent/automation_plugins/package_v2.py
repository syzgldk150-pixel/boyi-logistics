"""Unsigned, content-addressed package verification for service-v2 plugins.

The v2 transport boundary accepts only the uploaded ZIP bytes and the
SHA-256 supplied by that transport.  It deliberately has no signature,
public-key, or trust-store integration: administrator authority is recorded
by the installer, while this module establishes the immutable technical
identity of the exact bytes that were uploaded.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginManifestError, PluginPackageError
from agent.automation_plugins.manifest_v2 import (
    AutomationPluginManifestV2,
    canonical_json_bytes,
    parse_manifest_v2,
)
from agent.automation_plugins.storage import (
    MAX_VERIFIED_ARCHIVE_BYTES,
    validate_plugin_tree,
)


MANIFEST_NAME = "manifest.json"
MAX_ARCHIVE_BYTES = MAX_VERIFIED_ARCHIVE_BYTES
MAX_FILES = 512
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".ini",
        ".json",
        ".lock",
        ".md",
        ".py",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_FORBIDDEN_FRONTEND_SUFFIXES = frozenset(
    {
        ".cjs",
        ".css",
        ".htm",
        ".html",
        ".js",
        ".jsx",
        ".mjs",
        ".ts",
        ".tsx",
        ".wasm",
    }
)
_FORBIDDEN_FRONTEND_PARTS = frozenset(
    {"console", "frontend", "static", "templates", "ui", "web"}
)
_FORBIDDEN_HOOK_PARTS = frozenset(
    {".hooks", "hooks", "install-hooks", "post-install", "post_install"}
)
_FORBIDDEN_HOOK_FILENAMES = frozenset(
    {
        "install.bat",
        "install.cmd",
        "install.ps1",
        "install.sh",
        "post-install.bat",
        "post-install.cmd",
        "post-install.py",
        "post-install.sh",
        "post_install.py",
        "post_install.sh",
        "postinstall.py",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
    }
)
_FORBIDDEN_HOOK_KEYS = frozenset(
    {
        "install_hook",
        "install_hooks",
        "post_install",
        "post_install_hook",
        "post_install_hooks",
        "postinstall",
    }
)
_SUPPORTED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


@dataclass(frozen=True)
class PackageFileDigestV2:
    """One immutable, regular package member."""

    path: str
    sha256: str
    size: int

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class VerifiedPluginPackageV2:
    """Closed verification result suitable for installer persistence."""

    manifest: AutomationPluginManifestV2
    package_bytes: bytes
    package_sha256: str
    manifest_json: bytes
    manifest_sha256: str
    files: tuple[PackageFileDigestV2, ...]
    files_sha256: str
    runtime_sha256: str
    config_schema_sha256: str
    service_contracts_sha256: str
    contributions_sha256: str
    capabilities_sha256: str
    storage_sha256: str

    @property
    def archive_bytes(self) -> bytes:
        """Compatibility alias for storage code that calls ZIP bytes an archive."""

        return self.package_bytes

    @property
    def manifest_mapping(self) -> dict[str, Any]:
        """Return a fresh JSON-safe copy of the canonical persisted manifest."""

        value = json.loads(self.manifest_json.decode("utf-8"))
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise PluginPackageError("verified v2 manifest record is not an object")
        return value

    def to_persistence_mapping(self) -> dict[str, Any]:
        """Project all immutable identity fields without transport-only bytes."""

        manifest_mapping = self.manifest_mapping
        return {
            "schema_version": manifest_mapping["schema_version"],
            "runtime_model": manifest_mapping["runtime_model"],
            "plugin_id": manifest_mapping["plugin_id"],
            "version": manifest_mapping["version"],
            "package_sha256": self.package_sha256,
            "manifest_sha256": self.manifest_sha256,
            "manifest_json": manifest_mapping,
            "files": [item.to_mapping() for item in self.files],
            "files_sha256": self.files_sha256,
            "runtime_sha256": self.runtime_sha256,
            "config_schema_sha256": self.config_schema_sha256,
            "service_contracts_sha256": self.service_contracts_sha256,
            "contributions_sha256": self.contributions_sha256,
            "capabilities_sha256": self.capabilities_sha256,
            "storage_sha256": self.storage_sha256,
        }


class _DuplicateJsonKey(ValueError):
    pass


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_digest(value: Mapping[str, Any] | list[Any]) -> str:
    return _sha256(canonical_json_bytes(value))


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _parse_manifest_json(content: bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PluginPackageError("manifest.json must be strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PluginPackageError("manifest.json must contain an object")
    return value


def _contains_post_install_hook(value: Any) -> bool:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            normalized = str(raw_key).casefold().replace("-", "_")
            if normalized in _FORBIDDEN_HOOK_KEYS:
                return True
            if _contains_post_install_hook(child):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_post_install_hook(item) for item in value)
    return False


def _safe_member_name(raw_name: str) -> str:
    if not isinstance(raw_name, str):
        raise PluginPackageError("ZIP member name must be text")
    try:
        raw_name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PluginPackageError("ZIP member name is not valid UTF-8") from exc
    if unicodedata.normalize("NFC", raw_name) != raw_name:
        raise PluginPackageError("ZIP member path must use NFC Unicode")
    if (
        not raw_name
        or "\\" in raw_name
        or "\x00" in raw_name
        or raw_name.startswith("/")
        or "//" in raw_name
    ):
        raise PluginPackageError("ZIP member path is unsafe")
    pure = PurePosixPath(raw_name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PluginPackageError("ZIP member path traversal is forbidden")
    if any(
        ":" in part
        or part.endswith((" ", "."))
        or any(ord(character) < 32 for character in part)
        for part in pure.parts
    ):
        raise PluginPackageError("ZIP member path contains unsupported characters")
    normalized = pure.as_posix()
    if normalized != raw_name or len(normalized) > 240:
        raise PluginPackageError("ZIP member path is not normalized or is too long")
    if normalized != MANIFEST_NAME:
        is_settings_asset = normalized.startswith("settings/")
        if not normalized.startswith("payload/") and not is_settings_asset:
            raise PluginPackageError(
                "v2 package files must be manifest.json or below payload/ or settings/"
            )
        if is_settings_asset:
            settings_suffixes = frozenset(
                {".html", ".css", ".js", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff2"}
            )
            if pure.suffix.casefold() not in settings_suffixes:
                raise PluginPackageError("settings assets use an unsupported file type")
            return normalized
        payload_parts = tuple(part.casefold() for part in pure.parts[1:])
        if set(payload_parts[:-1]) & _FORBIDDEN_FRONTEND_PARTS:
            raise PluginPackageError("plugins cannot provide custom frontend assets")
        if pure.suffix.casefold() in _FORBIDDEN_FRONTEND_SUFFIXES:
            raise PluginPackageError("plugins cannot provide custom frontend assets")
        if (
            set(payload_parts[:-1]) & _FORBIDDEN_HOOK_PARTS
            or payload_parts[-1] in _FORBIDDEN_HOOK_FILENAMES
        ):
            raise PluginPackageError("post-install hooks and build scripts are forbidden")
    return normalized


def _validate_zip_metadata(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    if not infos or len(infos) > MAX_FILES:
        raise PluginPackageError(f"v2 package must contain from 1 to {MAX_FILES} files")
    names: set[str] = set()
    total_size = 0
    for info in infos:
        name = _safe_member_name(info.filename)
        if any(ord(character) > 127 for character in name) and not info.flag_bits & 0x800:
            raise PluginPackageError("non-ASCII ZIP member names must use UTF-8 encoding")
        folded = name.casefold()
        if folded in names:
            raise PluginPackageError("duplicate or case-colliding ZIP member")
        names.add(folded)
        if any(
            PurePosixPath(*PurePosixPath(name).parts[:index]).as_posix().casefold()
            in names
            for index in range(1, len(PurePosixPath(name).parts))
        ):
            raise PluginPackageError("ZIP file path collides with another member")
        if info.is_dir():
            raise PluginPackageError("explicit ZIP directory entries are not allowed")
        if info.flag_bits & 0x1:
            raise PluginPackageError("encrypted ZIP members are not allowed")
        if info.compress_type not in _SUPPORTED_COMPRESSION:
            raise PluginPackageError("ZIP member uses an unsupported compression method")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type == stat.S_IFLNK:
            raise PluginPackageError("symbolic links are not allowed in plugin packages")
        if file_type not in {0, stat.S_IFREG}:
            raise PluginPackageError("plugin ZIP members must be regular files")
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
            raise PluginPackageError("plugin file exceeds the per-file size limit")
        total_size += info.file_size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise PluginPackageError("plugin package exceeds the uncompressed size limit")
        if info.file_size and info.compress_size <= 0:
            raise PluginPackageError("plugin member has an invalid compressed size")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise PluginPackageError("plugin package compression ratio is unsafe")
    if MANIFEST_NAME.casefold() not in names:
        raise PluginPackageError("v2 package requires manifest.json")
    sorted_names = sorted(names)
    for index, name in enumerate(sorted_names):
        prefix = f"{name}/"
        if any(candidate.startswith(prefix) for candidate in sorted_names[index + 1 :]):
            raise PluginPackageError("ZIP file path collides with another member")
    return infos


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    maximum = min(MAX_FILE_BYTES, info.file_size) + 1
    try:
        with archive.open(info, mode="r") as stream:
            content = stream.read(maximum)
            if len(content) <= info.file_size:
                trailing = stream.read(1)
            else:
                trailing = b""
    except (RuntimeError, OSError, EOFError, zipfile.BadZipFile) as exc:
        raise PluginPackageError("plugin archive member cannot be read safely") from exc
    if len(content) != info.file_size or trailing:
        raise PluginPackageError("plugin archive member size differs from ZIP metadata")
    if PurePosixPath(info.filename).suffix.casefold() in _TEXT_SUFFIXES:
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PluginPackageError("plugin text files must use valid UTF-8") from exc
    return content


def _read_archive(package_bytes: bytes) -> tuple[dict[str, bytes], tuple[PackageFileDigestV2, ...]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(package_bytes), mode="r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise PluginPackageError("plugin archive is not a valid ZIP") from exc
    with archive:
        infos = _validate_zip_metadata(archive)
        file_bytes: dict[str, bytes] = {}
        file_digests: list[PackageFileDigestV2] = []
        for info in infos:
            name = _safe_member_name(info.filename)
            content = _read_member(archive, info)
            file_bytes[name] = content
            file_digests.append(
                PackageFileDigestV2(
                    path=name,
                    sha256=_sha256(content),
                    size=len(content),
                )
            )
    return file_bytes, tuple(sorted(file_digests, key=lambda item: item.path))


def _require_runtime_files(
    manifest_mapping: Mapping[str, Any],
    file_bytes: Mapping[str, bytes],
) -> None:
    runtime = manifest_mapping["runtime"]
    entrypoint = str(runtime["entrypoint"])
    if entrypoint not in file_bytes:
        raise PluginPackageError("v2 package omits its runtime entrypoint")
    requirements_lock = runtime["requirements_lock"]
    if requirements_lock is not None and str(requirements_lock) not in file_bytes:
        raise PluginPackageError("v2 package omits its requirements lock")
    for wheel_path in runtime["wheelhouse"]:
        if str(wheel_path) not in file_bytes:
            raise PluginPackageError("v2 package omits a declared wheelhouse file")


def verify_unsigned_plugin_zip_v2(
    source: bytes | bytearray | memoryview,
    *,
    transport_sha256: str,
) -> VerifiedPluginPackageV2:
    """Verify one unsigned v2 ZIP and return its immutable install record.

    ``transport_sha256`` is mandatory.  It binds any multipart/upload staging
    layer to the exact bytes parsed here, but is not publisher authentication.
    """

    if not isinstance(source, (bytes, bytearray, memoryview)):
        raise PluginPackageError("v2 plugin package source must be raw ZIP bytes")
    package_bytes = bytes(source)
    if not package_bytes or len(package_bytes) > MAX_ARCHIVE_BYTES:
        raise PluginPackageError("plugin archive size is invalid")
    if not isinstance(transport_sha256, str) or not _SHA256_RE.fullmatch(
        transport_sha256
    ):
        raise PluginPackageError("transport SHA-256 must contain exactly 64 hexadecimal characters")
    package_sha256 = _sha256(package_bytes)
    if not hmac.compare_digest(package_sha256, transport_sha256.casefold()):
        raise PluginPackageError("plugin archive SHA-256 does not match the transport digest")

    file_bytes, file_digests = _read_archive(package_bytes)
    manifest_source = _parse_manifest_json(file_bytes[MANIFEST_NAME])
    if _contains_post_install_hook(manifest_source):
        raise PluginPackageError("post-install hooks are forbidden in v2 manifests")
    try:
        manifest = parse_manifest_v2(manifest_source)
    except PluginManifestError:
        raise
    except (TypeError, ValueError) as exc:
        raise PluginManifestError("v2 manifest parser rejected manifest.json") from exc
    manifest_mapping = manifest.to_mapping()
    manifest_json = canonical_json_bytes(manifest_mapping)
    manifest_sha256 = _sha256(manifest_json)
    if manifest.manifest_sha256 != manifest_sha256:
        raise PluginPackageError("v2 manifest digest differs from its canonical mapping")
    _require_runtime_files(manifest_mapping, file_bytes)

    files_statement = [item.to_mapping() for item in file_digests]
    return VerifiedPluginPackageV2(
        manifest=manifest,
        package_bytes=package_bytes,
        package_sha256=package_sha256,
        manifest_json=manifest_json,
        manifest_sha256=manifest_sha256,
        files=file_digests,
        files_sha256=_json_digest(files_statement),
        runtime_sha256=_json_digest(manifest_mapping["runtime"]),
        config_schema_sha256=_json_digest(manifest_mapping["config_schema"]),
        service_contracts_sha256=_json_digest(
            {
                "provides": manifest_mapping["provides"],
                "requires": manifest_mapping["requires"],
            }
        ),
        contributions_sha256=_json_digest(manifest_mapping["contributes"]),
        capabilities_sha256=_json_digest(manifest_mapping["capabilities"]),
        storage_sha256=_json_digest(manifest_mapping["storage"]),
    )


def extract_verified_plugin_package_v2(
    package: VerifiedPluginPackageV2,
    destination: Path | str,
) -> Path:
    """Materialize a verified v2 package as a new read-only regular-file tree."""

    if not isinstance(package, VerifiedPluginPackageV2):
        raise PluginPackageError("v2 extraction requires a verified package record")
    if _sha256(package.package_bytes) != package.package_sha256:
        raise PluginPackageError("verified v2 package bytes changed before extraction")
    file_bytes, file_digests = _read_archive(package.package_bytes)
    if file_digests != package.files:
        raise PluginPackageError("v2 package file table changed before extraction")
    if canonical_json_bytes(package.manifest_mapping) != package.manifest_json:
        raise PluginPackageError("verified v2 manifest record changed before extraction")

    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise PluginPackageError("plugin extraction target already exists")
    target.mkdir(parents=True, exist_ok=False)
    try:
        for name, content in sorted(file_bytes.items()):
            output = target.joinpath(*PurePosixPath(name).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
            descriptor = os.open(output, flags, 0o400)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(content)
                    stream.flush()
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            try:
                output.chmod(0o444)
            except OSError:
                pass
        validate_plugin_tree(target)
        for directory in sorted(
            (item for item in target.rglob("*") if item.is_dir()),
            reverse=True,
        ):
            try:
                directory.chmod(0o555)
            except OSError:
                pass
        try:
            target.chmod(0o555)
        except OSError:
            pass
        validate_plugin_tree(target)
    except Exception:
        # The installer owns cleanup of its exact staging directory.
        raise
    return target


def package_sha256_v2(package_bytes: bytes) -> str:
    """Return the transport digest for raw v2 package bytes."""

    if not isinstance(package_bytes, bytes):
        raise TypeError("package_bytes must be bytes")
    return _sha256(package_bytes)
