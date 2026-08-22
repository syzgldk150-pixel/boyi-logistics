"""Deterministic package building and Ed25519 verification.

The verifier never extracts before the archive digest, member limits, canonical
manifest, signed file table and Ed25519 signature have all been checked.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, runtime_checkable

from agent.automation_plugins.errors import PluginPackageError, PluginSignatureError
from agent.automation_plugins.manifest import AutomationPluginManifest, canonical_json_bytes
from agent.automation_plugins.sdk import PLUGIN_SDK_SOURCE
from agent.automation_plugins.storage import (
    MAX_VERIFIED_ARCHIVE_BYTES,
    validate_plugin_tree,
)


MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "signature.json"
MAX_ARCHIVE_BYTES = MAX_VERIFIED_ARCHIVE_BYTES
MAX_FILES = 512
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
_SIGNATURE_FIELDS = frozenset(
    {
        "schema_version",
        "algorithm",
        "key_id",
        "manifest_sha256",
        "statement_sha256",
        "signature",
    }
)
_FORBIDDEN_FRONTEND_PARTS = frozenset({"console", "static", "templates", "ui"})
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@runtime_checkable
class PackageSignatureVerifier(Protocol):
    def verify(self, *, key_id: str, message: bytes, signature: bytes) -> None: ...


@runtime_checkable
class PackageSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class Ed25519TrustStore:
    """Verify against explicitly configured Ed25519 public keys.

    Keys are injected as raw 32-byte public keys or PEM/OpenSSH public key
    text. This class never discovers keys from project files or environment
    variables.
    """

    def __init__(self, public_keys: Mapping[str, bytes | str]) -> None:
        if not public_keys:
            raise ValueError("at least one Ed25519 public key is required")
        normalized: dict[str, Any] = {}
        for key_id, material in public_keys.items():
            if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
                raise ValueError("trusted Ed25519 key_id is invalid")
            normalized[key_id] = self._import_public_key(material)
        self._public_keys = normalized

    @staticmethod
    def _import_public_key(value: bytes | str) -> Any:
        try:
            from Crypto.PublicKey import ECC
            from Crypto.Signature import eddsa
        except ImportError as exc:  # pragma: no cover - production dependency guard
            raise RuntimeError("pycryptodome with Ed25519 support is required") from exc
        try:
            if isinstance(value, bytes) and len(value) == 32:
                key = eddsa.import_public_key(value)
            else:
                key = ECC.import_key(value)
        except (ValueError, TypeError, IndexError) as exc:
            raise PluginSignatureError("configured Ed25519 public key is invalid") from exc
        if str(getattr(key, "curve", "")) != "Ed25519" or key.has_private():
            raise PluginSignatureError("trust store accepts Ed25519 public keys only")
        return key

    def verify(self, *, key_id: str, message: bytes, signature: bytes) -> None:
        key = self._public_keys.get(key_id)
        if key is None:
            raise PluginSignatureError("package signing key is not trusted")
        try:
            from Crypto.Signature import eddsa
        except ImportError as exc:  # pragma: no cover - production dependency guard
            raise RuntimeError("pycryptodome with Ed25519 support is required") from exc
        try:
            eddsa.new(key, "rfc8032").verify(message, signature)
        except ValueError as exc:
            raise PluginSignatureError("Ed25519 package signature verification failed") from exc


def load_ed25519_trust_store(directory: Path | str) -> Ed25519TrustStore:
    """Load an explicit directory of public ``<key_id>.pub`` files.

    No environment or repository fallback exists. Missing/empty/unsafe trust
    roots fail closed, and private keys are rejected by ``Ed25519TrustStore``.
    """

    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise PluginSignatureError("Ed25519 trust directory does not exist or is unsafe")
    entries = sorted(root.iterdir())
    if not entries or len(entries) > 64:
        raise PluginSignatureError("Ed25519 trust directory must contain from 1 to 64 keys")
    public_keys: dict[str, bytes] = {}
    for entry in entries:
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".pub":
            raise PluginSignatureError("Ed25519 trust directory contains an unsupported entry")
        key_id = entry.stem
        if not _KEY_ID_RE.fullmatch(key_id) or key_id in public_keys:
            raise PluginSignatureError("Ed25519 trust key_id is invalid or duplicated")
        if entry.stat().st_size <= 0 or entry.stat().st_size > 64 * 1024:
            raise PluginSignatureError("Ed25519 public key file size is invalid")
        public_keys[key_id] = entry.read_bytes()
    return Ed25519TrustStore(public_keys)


class Ed25519PackageSigner:
    """Build-time signer; callers inject an in-memory private key object."""

    def __init__(self, *, key_id: str, private_key: Any) -> None:
        if not key_id or len(key_id) > 128:
            raise ValueError("key_id must be a non-empty string no longer than 128")
        if not getattr(private_key, "has_private", lambda: False)():
            raise ValueError("an Ed25519 private key is required")
        if str(getattr(private_key, "curve", "")) != "Ed25519":
            raise ValueError("private key must use Ed25519")
        self._key_id = key_id
        self._private_key = private_key

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, message: bytes) -> bytes:
        try:
            from Crypto.Signature import eddsa
        except ImportError as exc:  # pragma: no cover - production dependency guard
            raise RuntimeError("pycryptodome with Ed25519 support is required") from exc
        return eddsa.new(self._private_key, "rfc8032").sign(message)


@dataclass(frozen=True)
class PackageFileDigest:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class VerifiedPluginPackage:
    manifest: AutomationPluginManifest
    package_sha256: str
    manifest_sha256: str
    signing_key_id: str
    files: tuple[PackageFileDigest, ...]
    archive_bytes: bytes


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_member_name(name: str) -> str:
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or "//" in name
    ):
        raise PluginPackageError("ZIP member path is unsafe")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PluginPackageError("ZIP member path traversal is forbidden")
    if any(":" in part or any(ord(char) < 32 for char in part) for part in pure.parts):
        raise PluginPackageError("ZIP member path contains unsupported characters")
    normalized = pure.as_posix()
    if len(normalized) > 240:
        raise PluginPackageError("ZIP member path is too long")
    if normalized not in {MANIFEST_NAME, SIGNATURE_NAME}:
        if not normalized.startswith("payload/"):
            raise PluginPackageError("package files must be below payload/")
        lowered_parts = {part.casefold() for part in pure.parts[1:-1]}
        if lowered_parts & _FORBIDDEN_FRONTEND_PARTS:
            raise PluginPackageError("plugins cannot provide Console HTML/JavaScript assets")
        if pure.suffix.casefold() in {".html", ".htm", ".js", ".mjs", ".cjs"}:
            raise PluginPackageError("plugins cannot provide Console HTML/JavaScript assets")
    return normalized


def _validate_zip_metadata(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    if not infos or len(infos) > MAX_FILES:
        raise PluginPackageError(f"package must contain from 1 to {MAX_FILES} files")
    seen: set[str] = set()
    total_size = 0
    for info in infos:
        name = _safe_member_name(info.filename)
        folded = name.casefold()
        if folded in seen:
            raise PluginPackageError("duplicate or case-colliding ZIP member")
        seen.add(folded)
        if info.is_dir():
            raise PluginPackageError("explicit directory entries are not allowed")
        if info.flag_bits & 0x1:
            raise PluginPackageError("encrypted ZIP members are not allowed")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise PluginPackageError("symbolic links are not allowed in plugin packages")
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
            raise PluginPackageError("plugin file exceeds the per-file size limit")
        total_size += info.file_size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise PluginPackageError("plugin package exceeds the uncompressed size limit")
        if info.file_size and info.compress_size == 0:
            raise PluginPackageError("plugin member has an invalid compressed size")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise PluginPackageError("plugin package compression ratio is unsafe")
    if MANIFEST_NAME.casefold() not in seen or SIGNATURE_NAME.casefold() not in seen:
        raise PluginPackageError("package requires canonical manifest.json and signature.json")
    return infos


def _file_statement(files: Mapping[str, bytes], manifest_sha256: str) -> tuple[bytes, tuple[PackageFileDigest, ...]]:
    digests = tuple(
        PackageFileDigest(path=path, sha256=_sha256(content), size=len(content))
        for path, content in sorted(files.items())
    )
    statement = {
        "schema_version": 1,
        "manifest_sha256": manifest_sha256,
        "files": [
            {"path": item.path, "sha256": item.sha256, "size": item.size}
            for item in digests
        ],
    }
    return canonical_json_bytes(statement), digests


def _zip_bytes(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o444) << 16
            info.flag_bits |= 0x800
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def build_signed_plugin_zip(
    manifest: AutomationPluginManifest,
    payload_files: Mapping[str, bytes],
    *,
    signer: PackageSigner,
) -> bytes:
    """Build a deterministic signed ZIP without reading signing material from disk."""

    files: dict[str, bytes] = {MANIFEST_NAME: canonical_json_bytes(manifest.to_mapping())}
    for raw_name, raw_content in payload_files.items():
        name = _safe_member_name(str(raw_name))
        if name in {MANIFEST_NAME, SIGNATURE_NAME}:
            raise PluginPackageError("payload cannot replace reserved package files")
        if not isinstance(raw_content, bytes):
            raise PluginPackageError("payload content must be bytes")
        if name in files:
            raise PluginPackageError("duplicate payload file")
        files[name] = raw_content
    runtime = manifest.runtime
    if runtime["kind"] == "python_subprocess":
        if runtime["entrypoint"] not in files:
            raise PluginPackageError("runtime entrypoint is missing from the package")
        if files.get("payload/boyi_plugin_sdk.py") != PLUGIN_SDK_SOURCE.encode("utf-8"):
            raise PluginPackageError("plugin package requires the exact platform broker SDK")
        lock = runtime.get("requirements_lock")
        if lock and lock not in files:
            raise PluginPackageError("runtime requirements lock is missing from the package")
    manifest_sha256 = _sha256(files[MANIFEST_NAME])
    statement, _ = _file_statement(files, manifest_sha256)
    signature = signer.sign(statement)
    signature_record = {
        "schema_version": 1,
        "algorithm": "Ed25519",
        "key_id": signer.key_id,
        "manifest_sha256": manifest_sha256,
        "statement_sha256": _sha256(statement),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    files[SIGNATURE_NAME] = canonical_json_bytes(signature_record)
    package = _zip_bytes(files)
    if len(package) > MAX_ARCHIVE_BYTES:
        raise PluginPackageError("plugin archive exceeds the compressed size limit")
    return package


def _read_archive_source(source: bytes | bytearray | Path | str) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        path = Path(source)
        if not path.is_file():
            raise PluginPackageError("plugin archive does not exist")
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise PluginPackageError("plugin archive exceeds the compressed size limit")
        data = path.read_bytes()
    if not data or len(data) > MAX_ARCHIVE_BYTES:
        raise PluginPackageError("plugin archive size is invalid")
    return data


def verify_signed_plugin_zip(
    source: bytes | bytearray | Path | str,
    *,
    verifier: PackageSignatureVerifier,
    expected_package_sha256: str | None = None,
) -> VerifiedPluginPackage:
    """Verify archive digest and signature before returning extractable bytes."""

    archive_bytes = _read_archive_source(source)
    package_sha256 = _sha256(archive_bytes)
    if (
        expected_package_sha256 is not None
        and package_sha256 != str(expected_package_sha256).strip().lower()
    ):
        raise PluginPackageError("plugin archive SHA-256 does not match the expected digest")
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise PluginPackageError("plugin archive is not a valid ZIP") from exc
    with archive:
        infos = _validate_zip_metadata(archive)
        file_bytes: dict[str, bytes] = {}
        for info in infos:
            name = _safe_member_name(info.filename)
            try:
                content = archive.read(info)
            except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
                raise PluginPackageError("plugin archive member cannot be read safely") from exc
            if len(content) != info.file_size:
                raise PluginPackageError("plugin archive member size changed during verification")
            file_bytes[name] = content
    try:
        manifest_mapping = json.loads(file_bytes[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginPackageError("manifest.json must be UTF-8 JSON") from exc
    if not isinstance(manifest_mapping, dict):
        raise PluginPackageError("manifest.json must contain an object")
    manifest = AutomationPluginManifest.from_mapping(manifest_mapping)
    # Verify the untouched signed schema-v1 mapping. The runtime projection
    # may conservatively add fields that older packages did not sign.
    canonical_manifest = canonical_json_bytes(manifest.to_signed_mapping())
    if file_bytes[MANIFEST_NAME] != canonical_manifest:
        raise PluginPackageError("manifest.json must use canonical JSON encoding")
    manifest_sha256 = _sha256(canonical_manifest)
    try:
        signature_record = json.loads(file_bytes[SIGNATURE_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginSignatureError("signature.json must be UTF-8 JSON") from exc
    if not isinstance(signature_record, dict) or set(signature_record) != _SIGNATURE_FIELDS:
        raise PluginSignatureError("signature.json has an invalid schema")
    if file_bytes[SIGNATURE_NAME] != canonical_json_bytes(signature_record):
        raise PluginSignatureError("signature.json must use canonical JSON encoding")
    if signature_record["schema_version"] != 1 or signature_record["algorithm"] != "Ed25519":
        raise PluginSignatureError("only Ed25519 signature schema v1 is accepted")
    key_id = signature_record["key_id"]
    if not isinstance(key_id, str) or not key_id or len(key_id) > 128:
        raise PluginSignatureError("signature key_id is invalid")
    if signature_record["manifest_sha256"] != manifest_sha256:
        raise PluginSignatureError("signed manifest digest does not match manifest.json")
    signed_files = {name: content for name, content in file_bytes.items() if name != SIGNATURE_NAME}
    statement, file_digests = _file_statement(signed_files, manifest_sha256)
    if signature_record["statement_sha256"] != _sha256(statement):
        raise PluginSignatureError("signed package file table digest does not match")
    try:
        signature = base64.b64decode(signature_record["signature"], validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise PluginSignatureError("signature is not valid base64") from exc
    if len(signature) != 64:
        raise PluginSignatureError("Ed25519 signature must be 64 bytes")
    verifier.verify(key_id=key_id, message=statement, signature=signature)
    runtime = manifest.runtime
    if runtime["kind"] == "python_subprocess":
        signed_names = {item.path for item in file_digests}
        if runtime["entrypoint"] not in signed_names:
            raise PluginPackageError("signed package omits its runtime entrypoint")
        lock = runtime.get("requirements_lock")
        if lock and lock not in signed_names:
            raise PluginPackageError("signed package omits its requirements lock")
        sdk_path = "payload/boyi_plugin_sdk.py"
        if file_bytes.get(sdk_path) != PLUGIN_SDK_SOURCE.encode("utf-8"):
            raise PluginPackageError("uploaded plugin must include the exact platform broker SDK")
    return VerifiedPluginPackage(
        manifest=manifest,
        package_sha256=package_sha256,
        manifest_sha256=manifest_sha256,
        signing_key_id=key_id,
        files=file_digests,
        archive_bytes=archive_bytes,
    )


def extract_verified_package(package: VerifiedPluginPackage, destination: Path | str) -> Path:
    """Safely materialize already-verified bytes into a new immutable directory."""

    if _sha256(package.archive_bytes) != package.package_sha256:
        raise PluginPackageError("verified package bytes changed before extraction")
    target = Path(destination)
    if target.exists():
        raise PluginPackageError("plugin extraction target already exists")
    target.mkdir(parents=True, exist_ok=False)
    expected = {item.path: item for item in package.files}
    try:
        with zipfile.ZipFile(io.BytesIO(package.archive_bytes), mode="r") as archive:
            infos = _validate_zip_metadata(archive)
            for info in infos:
                name = _safe_member_name(info.filename)
                content = archive.read(info)
                if name != SIGNATURE_NAME:
                    digest = expected.get(name)
                    if digest is None or digest.size != len(content) or digest.sha256 != _sha256(content):
                        raise PluginPackageError("archive content changed after signature verification")
                output = target.joinpath(*PurePosixPath(name).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
                try:
                    output.chmod(0o444)
                except OSError:
                    pass
        validate_plugin_tree(target)
        for directory in sorted((item for item in target.rglob("*") if item.is_dir()), reverse=True):
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
        # The caller owns cleanup of a failed staging directory. Avoid broad
        # recursive deletion here because target lifecycle policy is injected.
        raise
    return target


def package_sha256(package_bytes: bytes) -> str:
    return _sha256(package_bytes)
