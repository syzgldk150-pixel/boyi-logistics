"""Run selected plugin tests and build one reviewed, versioned artifact.

This offline entrypoint does not install plugins, touch production settings,
start services, read credential files, or invoke the host deployer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_TESTS = {
    "sync_scan_codes": (
        "tests/test_scan_codes_action_payload.py",
        "tests/test_scan_codes_core_handlers.py",
        "tests/test_first_party_action_payloads.py::test_scan_codes_runs_router_to_fresh_server_verifier",
    ),
    "sync_arrival_stats": (
        "tests/test_arrival_production_adapter.py",
        "tests/test_first_party_action_payloads.py::test_arrival_stats_runs_closed_production_primitives_through_write_verifier",
    ),
    "self_pickup_problem_upload": (
        "tests/test_self_pickup_problem_upload_plugin_action.py",
        "tests/test_problem_plugin_production_adapter.py",
    ),
    "split_pending_problem_upload": (
        "tests/test_split_pending_problem_upload_plugin_action.py",
        "tests/test_problem_plugin_production_adapter.py",
    ),
    "sync_finance_bills": (
        "tests/test_sync_finance_bills_action_payload.py",
        "tests/test_finance_core_adapter.py",
        "tests/test_finance_plugin_router.py",
        "tests/test_finance_raw_plugin_protocol.py",
    ),
    "sync_customer_service_problems": (
        "tests/test_first_party_action_payloads.py::test_customer_problem_payload_owns_pagination_dedupe_and_recheck",
        "tests/test_first_party_action_payloads.py::test_customer_problem_payload_rejects_repeated_cursor",
        "tests/test_customer_source_plugin_runtime.py",
    ),
    "sync_scan_codes_v2": (
        "tests/test_sync_scan_codes_service_v2_package.py",
        "tests/test_sync_scan_codes_v1_v2_parity.py",
    ),
    "sync_arrival_stats_v2": (
        "tests/test_sync_arrival_stats_service_v2_package.py",
        "tests/test_sync_arrival_stats_v1_v2_parity.py",
    ),
    "self_pickup_problem_upload_v2": (
        "tests/test_self_pickup_problem_service_v2_package.py",
        "tests/test_self_pickup_problem_v1_v2_parity.py",
    ),
    "split_pending_problem_upload_v2": (
        "tests/test_split_pending_problem_service_v2_package.py",
        "tests/test_split_pending_problem_v1_v2_parity.py",
    ),
}


def selected_tests(plugin_id: str) -> tuple[str, ...]:
    try:
        nodes = PLUGIN_TESTS[plugin_id]
    except KeyError as exc:
        raise ValueError("plugin has no reviewed local test scope") from exc
    for node in nodes:
        if not (PROJECT_ROOT / node.split("::", 1)[0]).is_file():
            raise ValueError("reviewed local test is missing: " + node)
    return nodes


def maintenance_scope(plugin_id: str, paths: list[str]) -> dict[str, Any]:
    selected_tests(plugin_id)
    local_root = "agent/" + ("service_v2_plugins/" if plugin_id.endswith("_v2") else "first_party_automation_plugins/") + plugin_id + "/"
    local_tests = {node.split("::", 1)[0] for node in PLUGIN_TESTS[plugin_id]}
    supporting = [path for path in paths if path not in local_tests and (
        path.endswith('.md') or path.startswith(('tests/', 'agent/tests/', 'console/tests/')))]
    outside = [path for path in paths if not (path.startswith(local_root) or path in local_tests or path in supporting)]
    other_plugins = [path for path in outside if path.startswith(('agent/first_party_automation_plugins/', 'agent/service_v2_plugins/'))]
    core = [path for path in outside if path not in other_plugins]
    return {
        "scope": "CORE_UPDATE_REQUIRED" if core else "MULTI_PLUGIN_REVIEW_REQUIRED" if other_plugins else "PLUGIN_ONLY_CANDIDATE",
        "plugin_id": plugin_id, "plugin_source": local_root,
        "changed_paths": paths, "core_paths": core,
        "other_plugin_paths": other_plugins, "supporting_paths": supporting,
        "tests": list(PLUGIN_TESTS[plugin_id]),
        "meaning": "Source scope only; activation still verifies the signed Host/data/capability contract.",
    }


def _changed_paths(base_ref: str | None) -> list[str]:
    if base_ref is None:
        return []
    completed = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "--"], cwd=PROJECT_ROOT,
        check=True, capture_output=True, text=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=PROJECT_ROOT,
        check=True, capture_output=True, text=True,
    )
    return sorted(set(completed.stdout.splitlines() + untracked.stdout.splitlines()))


def run_local_tests(plugin_id: str) -> dict[str, Any]:
    if sys.version_info[:2] != (3, 10):
        raise RuntimeError("production plugin tests require Python 3.10")
    nodes = selected_tests(plugin_id)
    environment = dict(os.environ)
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    environment["MIGRATION_ENV_FILE"] = os.devnull
    environment["PYTHONPATH"] = os.pathsep.join((str(PROJECT_ROOT / "agent"), str(PROJECT_ROOT)))
    command = [sys.executable, "-m", "pytest", "-q", *nodes]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, env=environment, check=False)
    return {"plugin_id": plugin_id, "command": ["python", "-m", "pytest", "-q", *nodes],
        "exit_code": completed.returncode, "status": "PASS" if completed.returncode == 0 else "FAIL"}


def _write_new(path: Path, content: bytes) -> None:
    if path.is_symlink() or path.exists():
        raise FileExistsError("output already exists; refusing to overwrite")
    if not path.parent.is_dir():
        raise ValueError("output parent must already exist")
    with path.open("xb") as stream:
        stream.write(content)


def package_plugin(plugin_id: str, output: Path, *, version: str | None,
                   test_signing: bool, signing_key_env: str | None,
                   key_id: str | None) -> dict[str, Any]:
    selected_tests(plugin_id)
    if output.suffix.lower() != ".zip" or output.is_symlink() or output.exists():
        raise ValueError("output must be a new ZIP path")
    if plugin_id.endswith("_v2"):
        if test_signing or signing_key_env or key_id or version:
            raise ValueError("Service v2 version belongs in its manifest; signing options are ACTION_V1 only")
        from service_v2_plugins._shared.build_zip import build_plugin_zip
        from agent.automation_plugins.package_v2 import verify_unsigned_plugin_zip_v2

        source = PROJECT_ROOT / "agent" / "service_v2_plugins" / plugin_id
        with tempfile.TemporaryDirectory(prefix="plugin-maintenance-", dir=output.parent) as temporary:
            candidate = Path(temporary) / "candidate.zip"
            build_plugin_zip(source, candidate)
            package = candidate.read_bytes()
            digest = hashlib.sha256(package).hexdigest()
            verified = verify_unsigned_plugin_zip_v2(package, transport_sha256=digest)
            _write_new(output, package)
        return {"plugin_id": plugin_id, "version": verified.manifest.version,
            "runtime_model": "SERVICE_V2", "trust": "UNSIGNED_SUPER_ADMIN_UPLOAD",
            "package_sha256": hashlib.sha256(package).hexdigest(), "bytes": len(package)}
    if version is None or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError("ACTION_V1 packaging requires an explicit new semantic --version")
    if test_signing == bool(signing_key_env):
        raise ValueError("choose --test-signing or one injected --signing-key-env")
    from Crypto.PublicKey import ECC
    from agent.automation_plugins.first_party import first_party_payload_files, resolve_first_party_manifests
    from agent.automation_plugins.manifest import AutomationPluginManifest
    from agent.automation_plugins.package import Ed25519PackageSigner, Ed25519TrustStore, build_signed_plugin_zip, verify_signed_plugin_zip
    from agent.tool_registry import ToolRegistry

    source_manifest = resolve_first_party_manifests(ToolRegistry(), _plugin_ids=frozenset({plugin_id}))[plugin_id]
    mapping = source_manifest.to_mapping()
    mapping["version"] = version
    manifest = AutomationPluginManifest.from_mapping(mapping)
    if test_signing:
        private_key = ECC.generate(curve="Ed25519")
        signer_id = "isolated-test-only"
    else:
        if not key_id or not signing_key_env or re.fullmatch(r"[A-Z][A-Z0-9_]+", signing_key_env) is None:
            raise ValueError("injected signing needs --key-id and a valid uppercase environment name")
        material = os.environ.get(signing_key_env)
        if not material:
            raise ValueError("requested signing environment is not injected")
        try:
            private_key = ECC.import_key(material)
        except (ValueError, TypeError, IndexError):
            raise ValueError("injected signing material is invalid") from None
        if not private_key.has_private() or private_key.curve != "Ed25519":
            raise ValueError("injected signing material must be Ed25519")
        signer_id = key_id
    signer = Ed25519PackageSigner(key_id=signer_id, private_key=private_key)
    public_key = private_key.public_key().export_key(format="raw")
    package = build_signed_plugin_zip(manifest, first_party_payload_files(manifest), signer=signer)
    verified = verify_signed_plugin_zip(package, verifier=Ed25519TrustStore({signer_id: public_key}))
    _write_new(output, package)
    return {"plugin_id": plugin_id, "version": manifest.version,
        "runtime_model": "ACTION_V1", "trust": "TEST_ONLY" if test_signing else "INJECTED_SIGNER",
        "key_id": signer_id, "public_key_hex": public_key.hex(),
        "manifest_sha256": verified.manifest_sha256,
        "package_sha256": hashlib.sha256(package).hexdigest(), "bytes": len(package)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("describe", "test", "package"))
    parser.add_argument("plugin_id", choices=tuple(PLUGIN_TESTS))
    parser.add_argument("--base-ref", help="frozen host commit; changes outside this plugin require core update")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--version")
    signer = parser.add_mutually_exclusive_group()
    signer.add_argument("--test-signing", action="store_true")
    signer.add_argument("--signing-key-env")
    parser.add_argument("--key-id")
    args = parser.parse_args(argv)
    scope = maintenance_scope(args.plugin_id, _changed_paths(args.base_ref))
    scope["base_ref"] = args.base_ref
    if args.base_ref is None:
        scope["scope"] = "NOT_COMPARED"
    if args.action == "describe":
        print(json.dumps(scope, ensure_ascii=False, indent=2))
        return 0
    if args.base_ref and scope["scope"] != "PLUGIN_ONLY_CANDIDATE":
        print(json.dumps(scope, ensure_ascii=False, indent=2))
        return 2
    if args.report and (args.report.exists() or args.report.is_symlink()):
        parser.error("report already exists; choose a new report path")
    if args.action == "package" and args.output is None:
        parser.error("package requires --output")
    tests = run_local_tests(args.plugin_id)
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True).strip())
    result: dict[str, Any] = {"scope": scope, "tests": tests,
        "source_git_sha": git_sha, "worktree_dirty": dirty}
    code = tests["exit_code"]
    if code == 0 and args.action == "package":
        result["artifact"] = package_plugin(args.plugin_id, args.output,
            version=args.version, test_signing=args.test_signing,
            signing_key_env=args.signing_key_env, key_id=args.key_id)
    if args.report:
        _write_new(args.report, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
