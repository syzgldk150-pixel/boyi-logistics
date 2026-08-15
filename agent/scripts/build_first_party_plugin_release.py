"""Atomically build the complete signed first-party automation release.

The command never discovers signing material and never prints private-key
bytes.  It refuses to merge into or overwrite an existing artifact directory.
"""

from __future__ import annotations

import argparse
import hashlib
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
)
from agent.tool_registry import ToolRegistry
from scripts.sign_automation_plugin import load_ed25519_private_key

sys.stdout.reconfigure(encoding="utf-8")

_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build all source-reviewed first-party automation plugin ZIPs"
    )
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
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
    target, parent = _safe_output_target(args.output_root)
    private_key = load_ed25519_private_key(args.private_key)
    signer = Ed25519PackageSigner(key_id=str(args.key_id), private_key=private_key)
    trust_store = Ed25519TrustStore(
        {signer.key_id: _private_key_public_bytes(private_key)}
    )
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    if os.name != "nt":
        staging.chmod(0o700)
    try:
        catalog = ToolRegistry()
        _write_release(
            staging,
            release_sha=release_sha,
            signer=signer,
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
