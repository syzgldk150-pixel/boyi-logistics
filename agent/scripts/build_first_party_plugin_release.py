"""Atomically build the complete signed first-party automation release.

The command never discovers signing material and never prints private-key
bytes.  It refuses to merge into or overwrite an existing artifact directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from agent.automation_plugins.first_party import (
    first_party_payload_files,
    preflight_signed_first_party_release,
    resolve_release_first_party_manifests,
    verify_release_first_party_source_review,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.package import (
    Ed25519PackageSigner,
    Ed25519TrustStore,
    build_signed_plugin_zip,
    load_ed25519_trust_store,
    verify_signed_plugin_zip,
)
from agent.tool_registry import ToolRegistry
from scripts.sign_automation_plugin import load_ed25519_private_key

sys.stdout.reconfigure(encoding="utf-8")

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build all source-reviewed first-party automation plugin ZIPs"
    )
    parser.add_argument("--private-key", type=Path)
    parser.add_argument("--key-id")
    parser.add_argument("--reuse-artifact-root", type=Path)
    parser.add_argument("--trust-root", type=Path)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--digest-lock", type=Path)
    return parser


def _safe_output_target(path: Path) -> tuple[Path, Path]:
    target = path.resolve()
    parent = target.parent
    if path.is_symlink() or target.exists():
        raise ValueError("output-root already exists; refusing to merge or overwrite")
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("output-root parent must be an existing regular directory")
    return target, parent


def _private_key_public_bytes(private_key: object) -> bytes:
    try:
        public_key = private_key.public_key()  # type: ignore[attr-defined]
        value = public_key.export_key(format="raw")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("could not derive the Ed25519 public key") from exc
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError("derived Ed25519 public key has an invalid encoding")
    return value


def _load_release_index(root: Path) -> dict[str, object]:
    index_path = root / "release-index.json"
    try:
        raw = index_path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("reuse artifact release index is invalid") from exc
    canonical = canonical_json_bytes(value) if isinstance(value, dict) else b""
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "release_sha", "plugins"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("release_sha"), str)
        or not _RELEASE_SHA_RE.fullmatch(str(value["release_sha"]))
        or not isinstance(value.get("plugins"), dict)
        or raw not in {canonical, canonical + b"\n", canonical + b"\r\n"}
    ):
        raise ValueError("reuse artifact release index is invalid")
    return value


def _expected_file_rows(manifest: object) -> dict[str, tuple[str, int]]:
    manifest_bytes = canonical_json_bytes(manifest.to_mapping())  # type: ignore[attr-defined]
    files = {"manifest.json": manifest_bytes, **first_party_payload_files(manifest)}
    return {
        name: (hashlib.sha256(content).hexdigest(), len(content))
        for name, content in sorted(files.items())
    }


def _reuse_release(
    root: Path,
    *,
    release_sha: str,
    source_root: Path,
    trust_root: Path,
    core_catalog: ToolRegistry,
) -> None:
    source = source_root.resolve(strict=True)
    if source_root.is_symlink() or not source.is_dir() or source == root:
        raise ValueError("reuse-artifact-root must be a distinct regular directory")
    verify_release_first_party_source_review(core_catalog)
    verifier = load_ed25519_trust_store(trust_root)
    source_index = _load_release_index(source)
    preflight_signed_first_party_release(
        artifact_root=source,
        signature_verifier=verifier,
        core_catalog=core_catalog,
        release_sha=str(source_index["release_sha"]),
    )
    manifests = resolve_release_first_party_manifests(core_catalog)
    indexed = source_index["plugins"]
    if not isinstance(indexed, dict) or set(indexed) != set(manifests):
        raise ValueError("reuse artifact package set does not match the current release scope")
    output_plugins: dict[str, dict[str, str]] = {}
    for plugin_id, manifest in sorted(manifests.items()):
        row = indexed[plugin_id]
        if not isinstance(row, dict):
            raise ValueError(f"reuse artifact index row is invalid: {plugin_id}")
        archive_path = source / f"{plugin_id}-{manifest.version}.zip"
        verified = verify_signed_plugin_zip(
            archive_path,
            verifier=verifier,
            expected_package_sha256=str(row.get("package_sha256") or ""),
        )
        actual_files = {
            item.path: (item.sha256, item.size)
            for item in verified.files
        }
        if actual_files != _expected_file_rows(manifest):
            raise ValueError(
                f"reuse artifact payload does not match the current reviewed source: {plugin_id}"
            )
        output = root / archive_path.name
        output.write_bytes(verified.archive_bytes)
        if os.name != "nt":
            output.chmod(0o600)
        output_plugins[plugin_id] = {
            "version": manifest.version,
            "manifest_sha256": verified.manifest_sha256,
            "package_sha256": verified.package_sha256,
        }
    index_path = root / "release-index.json"
    index_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "release_sha": release_sha,
                "plugins": output_plugins,
            }
        )
    )
    if os.name != "nt":
        index_path.chmod(0o600)


def _write_release(
    root: Path,
    *,
    release_sha: str,
    signer: Ed25519PackageSigner,
    core_catalog: ToolRegistry,
) -> None:
    verify_release_first_party_source_review(core_catalog)
    manifests = resolve_release_first_party_manifests(core_catalog)
    index_plugins: dict[str, dict[str, str]] = {}
    for plugin_id, manifest in sorted(manifests.items()):
        package = build_signed_plugin_zip(
            manifest,
            first_party_payload_files(manifest),
            signer=signer,
        )
        package_sha256 = hashlib.sha256(package).hexdigest()
        package_path = root / f"{plugin_id}-{manifest.version}.zip"
        package_path.write_bytes(package)
        if os.name != "nt":
            package_path.chmod(0o600)
        index_plugins[plugin_id] = {
            "version": manifest.version,
            "manifest_sha256": manifest.manifest_sha256,
            "package_sha256": package_sha256,
        }
    index_path = root / "release-index.json"
    index_path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "release_sha": release_sha,
                "plugins": index_plugins,
            }
        )
    )
    if os.name != "nt":
        index_path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    release_sha = str(args.release_sha or "").strip().lower()
    if not _RELEASE_SHA_RE.fullmatch(release_sha):
        raise SystemExit("release-sha must be a lower-case Git SHA")
    signing_mode = args.private_key is not None or args.key_id is not None
    reuse_mode = args.reuse_artifact_root is not None or args.trust_root is not None
    if signing_mode == reuse_mode:
        raise SystemExit(
            "choose exactly one mode: --private-key/--key-id or "
            "--reuse-artifact-root/--trust-root"
        )
    if signing_mode and (args.private_key is None or not args.key_id):
        raise SystemExit("signing mode requires --private-key and --key-id")
    if reuse_mode and (args.reuse_artifact_root is None or args.trust_root is None):
        raise SystemExit("reuse mode requires --reuse-artifact-root and --trust-root")
    target, parent = _safe_output_target(args.output_root)
    signer = None
    trust_store = None
    if signing_mode:
        private_key = load_ed25519_private_key(args.private_key)
        signer = Ed25519PackageSigner(key_id=str(args.key_id), private_key=private_key)
        trust_store = Ed25519TrustStore(
            {signer.key_id: _private_key_public_bytes(private_key)}
        )
    else:
        trust_store = load_ed25519_trust_store(args.trust_root)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    if os.name != "nt":
        staging.chmod(0o700)
    try:
        catalog = ToolRegistry()
        if signing_mode:
            _write_release(
                staging,
                release_sha=release_sha,
                signer=signer,
                core_catalog=catalog,
            )
        else:
            _reuse_release(
                staging,
                release_sha=release_sha,
                source_root=args.reuse_artifact_root,
                trust_root=args.trust_root,
                core_catalog=catalog,
            )
        preflight_kwargs = {}
        if args.digest_lock is not None:
            preflight_kwargs["digest_lock_path"] = args.digest_lock
        result = preflight_signed_first_party_release(
            artifact_root=staging,
            signature_verifier=trust_store,
            core_catalog=catalog,
            release_sha=release_sha,
            **preflight_kwargs,
        )
        staging.replace(target)
    except BaseException:
        if staging.exists() and staging.parent == parent:
            shutil.rmtree(staging)
        raise
    print("status=ok")
    print(f"release_sha={result.release_sha}")
    print(f"package_count={result.package_count}")
    print(f"instance_count={result.instance_count}")
    print(f"contracts_sha256={result.contracts_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
