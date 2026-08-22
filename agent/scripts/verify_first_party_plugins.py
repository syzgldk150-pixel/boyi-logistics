"""Read-only release gate for the complete signed first-party plugin set."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent.automation_plugins.first_party import preflight_signed_first_party_release
from agent.automation_plugins.package import load_ed25519_trust_store
from agent.tool_registry import ToolRegistry

sys.stdout.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify all offline-signed first-party automation plugin artifacts"
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--trust-root", type=Path, required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--digest-lock", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kwargs = {}
    if args.digest_lock is not None:
        kwargs["digest_lock_path"] = args.digest_lock
    result = preflight_signed_first_party_release(
        artifact_root=args.artifact_root,
        signature_verifier=load_ed25519_trust_store(args.trust_root),
        core_catalog=ToolRegistry(),
        release_sha=str(args.release_sha),
        **kwargs,
    )
    print("status=ok")
    print(f"release_sha={result.release_sha}")
    print(f"package_count={result.package_count}")
    print(f"instance_count={result.instance_count}")
    print(f"contracts_sha256={result.contracts_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
