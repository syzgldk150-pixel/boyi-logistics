"""Freeze Host code and detect tracked or new runtime files during maintenance.

This evidence helper never reads environment, account or runtime configuration
files. Candidate payloads and their tests are deliberately outside Host scope.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CODE_SUFFIXES = {".py", ".js", ".css", ".html", ".sql", ".sh"}
_EXCLUDED_PARTS = {
    "tests", "docs", "examples", "config", "first_party_automation_plugins",
    "service_v2_plugins", "__pycache__",
}
_DEPENDENCIES = {
    "agent/requirements.txt", "agent/requirements.lock",
    "agent/windows_worker_requirements.lock", "console/requirements.txt",
    "console/requirements.lock", "agent/tools/registry.yaml",
    "agent/extension_sdk/schemas/manifest-v2.schema.json",
}


def _core_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if any(part.startswith(".") or part.lower().startswith(("credentials", "secrets")) for part in path.parts):
        return False
    return name in _DEPENDENCIES or (
        path.parts[0] in {"agent", "console", "shared"}
        and not set(path.parts) & _EXCLUDED_PARTS and path.suffix in _CODE_SUFFIXES
    )


def core_files(root: Path = PROJECT_ROOT) -> dict[str, str]:
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
    ).decode().split("\0")
    result = {}
    for name in sorted(set(filter(None, names))):
        if not _core_path(name):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Host source is missing or is a symlink: " + name)
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result:
        raise ValueError("Host source inventory is empty")
    return result


def freeze_host(output: Path, root: Path = PROJECT_ROOT) -> dict:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    files = core_files(root)
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root,
    ).decode().split("\0")
    dirty = subprocess.check_output(["git", "diff", "HEAD", "--name-only"], cwd=root, text=True).splitlines()
    if any(name in files for name in [*dirty, *untracked]):
        raise ValueError("Commit Host source changes before freezing")
    result = {"schema_version": 1, "host_base_sha": sha,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "runtime source including newly added files, SQL migrations, dependency locks and public registry/schema; excludes payloads, tests, docs and runtime configuration",
        "core_files": files}
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result


def verify_host(freeze_path: Path, root: Path = PROJECT_ROOT) -> dict:
    expected = json.loads(freeze_path.read_text(encoding="utf-8"))
    if expected.get("schema_version") != 1 or not isinstance(expected.get("core_files"), dict):
        raise ValueError("Host freeze evidence has an unsupported shape")
    actual = core_files(root)
    if expected["core_files"] != actual:
        changed = sorted(name for name in set(expected["core_files"]) | set(actual)
            if expected["core_files"].get(name) != actual.get(name))
        raise ValueError("Host changed during plugin maintenance: " + ", ".join(changed))
    return {"status": "PASS", "host_base_sha": expected["host_base_sha"],
        "core_file_count": len(actual), "core_files_sha256": hashlib.sha256(
            json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def process_identity() -> dict:
    pid = os.getpid()
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return {"pid": pid, "linux_start_ticks": int(fields[19]),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("freeze", "verify"))
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    result = freeze_host(args.file) if args.operation == "freeze" else verify_host(args.file)
    print(json.dumps({key: value for key, value in result.items() if key != "core_files"}, indent=2))
