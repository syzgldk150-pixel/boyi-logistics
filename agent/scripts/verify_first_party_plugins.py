"""Read-only release gate for the complete signed first-party plugin set."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent.automation_plugins.first_party import preflight_signed_first_party_release
from agent.automation_plugins.package import load_ed25519_trust_store
from agent.tool_registry import ToolRegistry
from agent.automation_plugins.first_party_retirement import read_retired_release, require_completed_migrations

sys.stdout.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify all offline-signed first-party automation plugin artifacts"
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--trust-root", type=Path, required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--digest-lock", type=Path)
    parser.add_argument("--service-v2-only", action="store_true")
    parser.add_argument("--require-completed-migrations", action="store_true")
    parser.add_argument("--runtime-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    retired = read_retired_release(args.artifact_root, str(args.release_sha))
    if retired is not None:
        if args.require_completed_migrations:
            if args.runtime_root is None:
                raise SystemExit("completed-migration verification requires --runtime-root")
            from agent import runtime_config
            runtime_config.PROJECT_ROOT = args.runtime_root.resolve(strict=True)
            runtime_config.load_agent_environment()
            from main import _orchestration_connection
            from shared.orchestration_repository import OrchestrationRepository
            require_completed_migrations(OrchestrationRepository(_orchestration_connection))
        print("status=ok")
        print("first_party_retired=true")
        print(f"release_sha={retired.release_sha}")
        print("package_count=0")
        print(f"instance_count={retired.instance_count}")
        print(f"contracts_sha256={retired.contracts_sha256}")
        return 0
    if args.service_v2_only:
        raise SystemExit("current release requires a verified V1 retirement index")
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
