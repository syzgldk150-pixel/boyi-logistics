"""Fail when a runtime environment differs from an exact requirements lock."""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path


_LOCKED_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def read_lock(path: Path) -> dict[str, tuple[str, str]]:
    locked: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCKED_REQUIREMENT.fullmatch(line)
        if not match:
            raise ValueError(f"{path}:{line_number}: requirement must use an exact name==version pin")
        display_name, version = match.groups()
        name = _canonical_name(display_name)
        if name in locked:
            raise ValueError(f"{path}:{line_number}: duplicate package {display_name}")
        locked[name] = (display_name, version)
    if not locked:
        raise ValueError(f"{path}: lock file is empty")
    return locked


def verify(lock_path: Path, python_version: str) -> list[str]:
    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    problems: list[str] = []
    if actual_python != python_version:
        problems.append(f"python: expected {python_version}, installed {actual_python}")

    installed = {
        _canonical_name(distribution.metadata["Name"]): distribution.version
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }
    for name, (display_name, expected_version) in read_lock(lock_path).items():
        actual_version = installed.get(name)
        if actual_version is None:
            problems.append(f"{display_name}: missing (expected {expected_version})")
        elif actual_version != expected_version:
            problems.append(f"{display_name}: expected {expected_version}, installed {actual_version}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lock", type=Path)
    parser.add_argument("--python-version", default="3.10")
    args = parser.parse_args()

    try:
        problems = verify(args.lock, args.python_version)
    except (OSError, ValueError) as exc:
        print(f"locked_environment=invalid: {exc}", file=sys.stderr)
        return 2

    if problems:
        print("locked_environment=mismatch", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    pip_check = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False,
        text=True,
        capture_output=True,
    )
    if pip_check.returncode:
        print("locked_environment=pip-check-failed", file=sys.stderr)
        print((pip_check.stdout or pip_check.stderr).strip(), file=sys.stderr)
        return 1

    print(f"locked_environment=ok python={args.python_version} lock={args.lock.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
