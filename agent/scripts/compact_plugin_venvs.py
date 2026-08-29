"""Compact historical plugin venvs without sharing mutable runtime state."""

from __future__ import annotations

import argparse
import filecmp
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION_ROOT_RE = re.compile(r"^\d+\.\d+\.\d+-[0-9a-f]{12}$")
_REDUNDANT_ALIASES = ("python3", "python3.10")


@dataclass(frozen=True)
class CompactionCandidate:
    path: Path
    size: int


def _private_regular_file(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError(f"unsafe plugin interpreter file: {path}")
    return metadata


def scan_install_root(install_root: Path) -> tuple[CompactionCandidate, ...]:
    """Find private aliases that are byte-identical to their venv's Python."""

    root = install_root.absolute()
    root_metadata = root.lstat()
    if (
        root == Path("/")
        or root.is_symlink()
        or not stat.S_ISDIR(root_metadata.st_mode)
    ):
        raise RuntimeError("plugin install root is unsafe")

    candidates: list[CompactionCandidate] = []
    for plugin_root in sorted(root.iterdir()):
        if plugin_root.name == ".staging":
            continue
        if not plugin_root.is_dir() or not _PLUGIN_ID_RE.fullmatch(plugin_root.name):
            raise RuntimeError(f"unexpected plugin root: {plugin_root}")
        for version_root in sorted(plugin_root.iterdir()):
            if (
                not version_root.is_dir()
                or not _VERSION_ROOT_RE.fullmatch(version_root.name)
            ):
                raise RuntimeError(f"unexpected plugin version root: {version_root}")
            bin_root = version_root / "venv" / "bin"
            primary = bin_root / "python"
            _private_regular_file(primary)
            for alias_name in _REDUNDANT_ALIASES:
                alias = bin_root / alias_name
                if not alias.exists() and not alias.is_symlink():
                    continue
                metadata = _private_regular_file(alias)
                if not filecmp.cmp(primary, alias, shallow=False):
                    raise RuntimeError(f"plugin interpreter alias differs: {alias}")
                candidates.append(
                    CompactionCandidate(path=alias, size=metadata.st_size)
                )
    return tuple(candidates)


def compact_install_root(install_root: Path) -> tuple[int, int]:
    """Delete only aliases accepted by one complete read-only preflight."""

    candidates = scan_install_root(install_root)
    for candidate in candidates:
        metadata = _private_regular_file(candidate.path)
        if metadata.st_size != candidate.size:
            raise RuntimeError("plugin interpreter alias changed after preflight")
        candidate.path.unlink()
    if any(candidate.path.exists() for candidate in candidates):
        raise RuntimeError("plugin venv compaction did not finish")
    return len(candidates), sum(candidate.size for candidate in candidates)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run by default. --apply removes only byte-identical copied "
            "Python aliases and preserves every plugin version's private venv."
        )
    )
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    candidates = scan_install_root(args.install_root)
    candidate_bytes = sum(candidate.size for candidate in candidates)
    print(
        f"plugin_venv_compaction=dry_run candidate_files={len(candidates)} "
        f"candidate_bytes={candidate_bytes}"
    )
    if not args.apply:
        return 0
    removed_files, released_bytes = compact_install_root(args.install_root)
    print(
        f"plugin_venv_compaction=applied removed_files={removed_files} "
        f"released_bytes={released_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
