"""Offline Ed25519 packager for reusable automation action plugins.

The command never discovers signing material and never prints private-key
bytes.  The caller must pass one explicitly protected key file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from agent.automation_plugins.manifest import AutomationPluginManifest, canonical_json_bytes
from agent.automation_plugins.package import (
    MAX_FILES,
    MAX_FILE_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    Ed25519PackageSigner,
    build_signed_plugin_zip,
)
from agent.automation_plugins.sdk import PLUGIN_SDK_SOURCE

sys.stdout.reconfigure(encoding="utf-8")


def load_ed25519_private_key(path: Path):
    """Load one explicitly supplied, access-restricted Ed25519 private key."""

    target = path.resolve(strict=True)
    if path.is_symlink() or not target.is_file():
        raise ValueError("private-key path must be a regular non-symlink file")
    size = target.stat().st_size
    if size <= 0 or size > 64 * 1024:
        raise ValueError("private-key file size is invalid")
    if os.name != "nt" and stat.S_IMODE(target.stat().st_mode) & 0o077:
        raise ValueError("private-key file must not be accessible by group or other users")
    try:
        from Crypto.PublicKey import ECC
    except ImportError as exc:  # pragma: no cover - locked production dependency
        raise RuntimeError("pycryptodome with Ed25519 support is required") from exc
    try:
        key = ECC.import_key(target.read_bytes())
    except (ValueError, TypeError, IndexError) as exc:
        raise ValueError("private-key file is not a valid Ed25519 key") from exc
    if not key.has_private() or str(getattr(key, "curve", "")) != "Ed25519":
        raise ValueError("private-key file must contain an Ed25519 private key")
    return key


def _load_manifest(path: Path) -> AutomationPluginManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest must be readable UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("manifest must contain one JSON object")
    return AutomationPluginManifest.from_mapping(raw)


def _payload_files(root: Path) -> dict[str, bytes]:
    source = root.resolve(strict=True)
    if not source.is_dir() or root.is_symlink():
        raise ValueError("payload-root must be a regular directory")
    payload: dict[str, bytes] = {}
    total = 0
    for item in sorted(source.rglob("*")):
        if item.is_symlink():
            raise ValueError("payload must not contain symbolic links")
        if item.is_dir():
            continue
        if not item.is_file():
            raise ValueError("payload contains an unsupported filesystem object")
        relative = item.relative_to(source).as_posix()
        content = item.read_bytes()
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("payload file exceeds the per-file package limit")
        total += len(content)
        if len(payload) + 2 > MAX_FILES or total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError("payload exceeds package file or size limits")
        payload[f"payload/{relative}"] = content
    if not payload:
        raise ValueError("payload-root must contain at least one file")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build an Ed25519-signed automation plugin ZIP")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--payload-root", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-index", type=Path)
    parser.add_argument("--release-sha")
    return parser


def _release_index_bytes(
    path: Path,
    *,
    release_sha: str,
    manifest: AutomationPluginManifest,
    package_sha256: str,
) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{7,64}", release_sha):
        raise ValueError("release-sha must be a lower-case Git SHA")
    target = path.resolve()
    if target.exists():
        raw = target.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("release index is invalid UTF-8 JSON") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "release_sha", "plugins"}
            or value.get("schema_version") != 1
            or value.get("release_sha") != release_sha
            or not isinstance(value.get("plugins"), dict)
            or raw != canonical_json_bytes(value)
        ):
            raise ValueError("release index is invalid or belongs to another release")
    else:
        value = {"schema_version": 1, "release_sha": release_sha, "plugins": {}}
    row = {
        "version": manifest.version,
        "manifest_sha256": manifest.manifest_sha256,
        "package_sha256": package_sha256,
    }
    current = value["plugins"].get(manifest.plugin_id)
    if current is not None and current != row:
        raise ValueError("release index already binds this plugin_id to different bytes")
    value["plugins"][manifest.plugin_id] = row
    return canonical_json_bytes(value)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("output already exists; refusing to overwrite")
    if bool(args.release_index) != bool(args.release_sha):
        raise SystemExit("--release-index and --release-sha must be supplied together")
    if args.release_index and args.release_index.resolve() == output:
        raise SystemExit("release index and package output must be different files")
    manifest = _load_manifest(args.manifest)
    signer = Ed25519PackageSigner(
        key_id=str(args.key_id),
        private_key=load_ed25519_private_key(args.private_key),
    )
    payload = _payload_files(args.payload_root)
    sdk_path = "payload/boyi_plugin_sdk.py"
    if sdk_path in payload:
        raise SystemExit("payload must not replace the platform broker SDK")
    if manifest.runtime["kind"] == "python_subprocess":
        payload[sdk_path] = PLUGIN_SDK_SOURCE.encode("utf-8")
    package = build_signed_plugin_zip(manifest, payload, signer=signer)
    package_digest = hashlib.sha256(package).hexdigest()
    index_bytes = None
    if args.release_index:
        index_bytes = _release_index_bytes(
            args.release_index,
            release_sha=str(args.release_sha),
            manifest=manifest,
            package_sha256=package_digest,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(package)
    if args.release_index and index_bytes is not None:
        index_target = args.release_index.resolve()
        index_target.parent.mkdir(parents=True, exist_ok=True)
        index_target.write_bytes(index_bytes)
    print(f"package_sha256={package_digest}")
    print(f"manifest_sha256={manifest.manifest_sha256}")
    print(f"key_id={signer.key_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
