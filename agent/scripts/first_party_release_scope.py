"""Select the exact first-party source and tests admitted to a server release.

The production allowlist lives in ``agent.automation_plugins.release_scope``.
This helper deliberately parses that module with :mod:`ast` instead of
importing it, so discovering the release boundary never imports a deferred
payload (or any Agent runtime module).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


_ALLOWLIST_NAME = "RUNNABLE_SERVER_FIRST_PARTY_PLUGIN_IDS"
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FIRST_PARTY_TEST_MARKERS = (
    "first_party_automation_plugins",
    "FirstPartyCoreHandler",
    "first_party_handlers",
    "first_party_payload_files",
    "build_first_party_plugin_release",
)
_RELEASE_CONTROL_TESTS = frozenset(
    {
        "test_automation_plugin_platform.py",
        "test_automation_plugin_main_release_gate.py",
        "test_automation_plugin_release_scope.py",
        "test_first_party_action_payloads.py",
        "test_first_party_plugin_release_builder.py",
        "test_first_party_release_source_scope.py",
        "test_release_boundaries.py",
    }
)


class ReleaseScopeError(ValueError):
    """Raised when the executable release boundary is ambiguous or incomplete."""


def _repository_root(value: str | None) -> Path:
    root = Path(value).resolve() if value else Path(__file__).resolve().parents[2]
    if not (root / "agent" / "agent" / "automation_plugins" / "release_scope.py").is_file():
        raise ReleaseScopeError(f"repository root is invalid: {root}")
    return root


def _literal_string_set(node: ast.AST) -> frozenset[str]:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))
    ):
        raise ReleaseScopeError(
            f"{_ALLOWLIST_NAME} must be one literal frozenset of plugin IDs"
        )
    values: list[str] = []
    for item in node.args[0].elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            raise ReleaseScopeError(f"{_ALLOWLIST_NAME} contains a non-literal value")
        values.append(item.value)
    if not values or len(values) != len(set(values)):
        raise ReleaseScopeError(f"{_ALLOWLIST_NAME} must be non-empty and unique")
    invalid = sorted(value for value in values if not _PLUGIN_ID_RE.fullmatch(value))
    if invalid:
        raise ReleaseScopeError(f"invalid plugin IDs in {_ALLOWLIST_NAME}: {invalid}")
    return frozenset(values)


def release_plugin_ids(repository_root: Path) -> frozenset[str]:
    """Read the executable allowlist without importing Agent code."""

    path = repository_root / "agent" / "agent" / "automation_plugins" / "release_scope.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == _ALLOWLIST_NAME
            for target in node.targets
        ):
            assignments.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == _ALLOWLIST_NAME
            and node.value is not None
        ):
            assignments.append(node.value)
    if len(assignments) != 1:
        raise ReleaseScopeError(f"expected exactly one {_ALLOWLIST_NAME} assignment")
    return _literal_string_set(assignments[0])


def _first_party_root(repository_root: Path) -> Path:
    return repository_root / "agent" / "first_party_automation_plugins"


def known_plugin_ids(repository_root: Path) -> frozenset[str]:
    root = _first_party_root(repository_root)
    values: set[str] = set()
    for path in root.iterdir():
        if not path.is_dir() or path.name == "_runtime":
            continue
        if not _PLUGIN_ID_RE.fullmatch(path.name):
            raise ReleaseScopeError(f"invalid first-party package directory: {path.name}")
        values.add(path.name)
    selected = release_plugin_ids(repository_root)
    unknown = selected - values
    if unknown:
        raise ReleaseScopeError(f"release allowlist references missing source: {sorted(unknown)}")
    return frozenset(values)


def _python_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def release_source_files(repository_root: Path) -> tuple[Path, ...]:
    root = _first_party_root(repository_root)
    selected = release_plugin_ids(repository_root)
    known_plugin_ids(repository_root)
    paths = _python_files(root / "_runtime")
    for plugin_id in sorted(selected):
        action = root / plugin_id / "payload" / "action.py"
        if not action.is_file():
            raise ReleaseScopeError(f"release payload is incomplete: {action}")
        paths.extend(_python_files(root / plugin_id))
    return tuple(sorted(set(paths)))


def deferred_source_files(repository_root: Path) -> tuple[Path, ...]:
    root = _first_party_root(repository_root)
    deferred = known_plugin_ids(repository_root) - release_plugin_ids(repository_root)
    paths: list[Path] = []
    for plugin_id in sorted(deferred):
        paths.extend(_python_files(root / plugin_id))
    return tuple(sorted(set(paths)))


def _is_windows_deferred(path: Path, repository_root: Path) -> bool:
    relative = path.relative_to(repository_root).as_posix()
    name = path.name
    return (
        relative == "agent/windows_worker_host.py"
        or relative.startswith("agent/agent/windows_worker/")
        or name.startswith("test_windows_worker")
    )


def _first_party_test_is_deferred(
    path: Path,
    *,
    repository_root: Path,
) -> bool:
    if path.name in _RELEASE_CONTROL_TESTS:
        return False
    text = path.read_text(encoding="utf-8")
    if not any(marker in text for marker in _FIRST_PARTY_TEST_MARKERS):
        return False
    all_ids = known_plugin_ids(repository_root)
    mentioned = {
        plugin_id
        for plugin_id in all_ids
        if re.search(rf"(?<![a-z0-9_]){re.escape(plugin_id)}(?![a-z0-9_])", text)
    }
    deferred = all_ids - release_plugin_ids(repository_root)
    # Aggregate tests that do not bind themselves to a reviewed action are
    # intentionally audited outside the release gate.  A newly promoted action
    # moves its dedicated tests into the gate automatically.
    return not mentioned or bool(mentioned & deferred)


def test_files(
    repository_root: Path,
    *,
    scope: str,
    suite: str,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    if suite in {"root", "all"}:
        roots.append(repository_root / "tests")
    if suite in {"agent", "all"}:
        roots.append(repository_root / "agent" / "tests")
    candidates = sorted(
        path
        for root in roots
        for path in root.glob("test_*.py")
        if path.is_file() and not _is_windows_deferred(path, repository_root)
    )
    deferred = {
        path
        for path in candidates
        if path.is_relative_to(repository_root / "tests")
        and _first_party_test_is_deferred(path, repository_root=repository_root)
    }
    if scope == "gate":
        return tuple(path for path in candidates if path not in deferred)
    if scope == "deferred":
        return tuple(path for path in candidates if path in deferred)
    raise ReleaseScopeError(f"unsupported test scope: {scope}")


def quality_files(repository_root: Path, *, scope: str) -> tuple[Path, ...]:
    if scope == "deferred":
        return tuple(
            sorted(
                set(deferred_source_files(repository_root))
                | set(test_files(repository_root, scope="deferred", suite="all"))
            )
        )
    if scope != "gate":
        raise ReleaseScopeError(f"unsupported quality scope: {scope}")

    first_party_root = _first_party_root(repository_root)
    release_sources = set(release_source_files(repository_root))
    deferred_tests = set(test_files(repository_root, scope="deferred", suite="all"))
    paths: list[Path] = []
    for root in (
        repository_root / "agent",
        repository_root / "shared",
        repository_root / "tests",
    ):
        for path in _python_files(root):
            if _is_windows_deferred(path, repository_root) or path in deferred_tests:
                continue
            if path.is_relative_to(first_party_root) and path not in release_sources:
                continue
            paths.append(path)
    return tuple(sorted(set(paths)))


def verify_staged_tree(repository_root: Path) -> None:
    """Fail if a staged server payload contains any non-allowlisted package."""

    root = _first_party_root(repository_root)
    selected = release_plugin_ids(repository_root)
    allowed_entries = selected | {
        "_runtime",
        "README.md",
        "MIGRATION_MATRIX.md",
        "digests.json",
    }
    unexpected_entries = sorted(
        path.name
        for path in root.iterdir()
        if path.is_symlink() or path.name not in allowed_entries
    )
    if unexpected_entries:
        raise ReleaseScopeError(
            f"staged first-party source contains unexpected entries: {unexpected_entries}"
        )
    actual = known_plugin_ids(repository_root)
    unexpected = actual - selected
    missing = selected - actual
    if missing or unexpected:
        raise ReleaseScopeError(
            "staged first-party package set differs from release allowlist: "
            f"missing={sorted(missing)} unexpected={sorted(unexpected)}"
        )
    release_source_files(repository_root)


def _emit(paths_or_values: Iterable[Path | str], *, repository_root: Path, nul: bool) -> None:
    values = [
        item.relative_to(repository_root).as_posix() if isinstance(item, Path) else item
        for item in paths_or_values
    ]
    separator = "\0" if nul else "\n"
    if values:
        sys.stdout.write(separator.join(values) + separator)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ids = subparsers.add_parser("plugin-ids")
    ids.add_argument("--null", action="store_true")

    sources = subparsers.add_parser("source-files")
    sources.add_argument("--scope", choices=("gate", "deferred"), required=True)
    sources.add_argument("--null", action="store_true")

    tests = subparsers.add_parser("test-files")
    tests.add_argument("--scope", choices=("gate", "deferred"), required=True)
    tests.add_argument("--suite", choices=("root", "agent", "all"), default="all")
    tests.add_argument("--null", action="store_true")

    quality = subparsers.add_parser("quality-files")
    quality.add_argument("--scope", choices=("gate", "deferred"), required=True)
    quality.add_argument("--null", action="store_true")

    subparsers.add_parser("verify-staged")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = _repository_root(args.repository_root)
    if args.command == "plugin-ids":
        _emit(sorted(release_plugin_ids(root)), repository_root=root, nul=args.null)
    elif args.command == "source-files":
        files = (
            release_source_files(root)
            if args.scope == "gate"
            else deferred_source_files(root)
        )
        _emit(files, repository_root=root, nul=args.null)
    elif args.command == "test-files":
        _emit(
            test_files(root, scope=args.scope, suite=args.suite),
            repository_root=root,
            nul=args.null,
        )
    elif args.command == "quality-files":
        _emit(
            quality_files(root, scope=args.scope),
            repository_root=root,
            nul=args.null,
        )
    elif args.command == "verify-staged":
        verify_staged_tree(root)
        print("first_party_release_source_scope=ok")
    else:  # pragma: no cover - argparse enforces the closed command set.
        raise ReleaseScopeError(f"unsupported command: {args.command}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ReleaseScopeError, SyntaxError, UnicodeError) as exc:
        print(f"first_party_release_source_scope=blocked reason={exc}", file=sys.stderr)
        raise SystemExit(1) from None
