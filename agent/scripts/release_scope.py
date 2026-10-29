"""Select the exact Service V2 plugin source, tests and quality files of a release.

The server release ships every current package under ``agent/service_v2_plugins``
listed in :data:`SERVICE_V2_PLUGIN_IDS` together with the shared builder files.
A staged payload must contain exactly these packages.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Sequence


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


# Packages whose ZIP is assembled from ``manifest.json + payload/main.py``.
STANDALONE_SERVICE_V2_PLUGIN_IDS = frozenset({
    "quote_prices_v2",
    "r7_vehicle_checkin_v2",
    "sync_ronghui_finance_statement_v2",
    "sync_ronghui_route_status_v2",
})
# Packages built by ``_shared/build_zip.py`` from ``payload/plugin.py`` and a settings page.
SHARED_BUILDER_SERVICE_V2_PLUGIN_IDS = frozenset({
    "clockin_daxiang_s_v2",
    "clockin_daxiang_v2",
    "self_pickup_problem_upload_v2",
    "split_pending_problem_upload_v2",
    "sync_arrival_stats_v2",
    "sync_arrive_list_v2",
    "sync_customer_service_problems_v2",
    "sync_daily_send_orders_v2",
    "sync_daily_should_sign_v2",
    "sync_delivery_status_v2",
    "sync_scan_codes_v2",
    "sync_self_pickup_should_sign_v2",
    "sync_site_send_list_v2",
    "sync_yunda_dispatch_forecast_v2",
    "sync_yunda_send_waybills_v2",
})
SERVICE_V2_PLUGIN_IDS = STANDALONE_SERVICE_V2_PLUGIN_IDS | SHARED_BUILDER_SERVICE_V2_PLUGIN_IDS
_PLUGIN_ROOT_FILES = frozenset({"_shared", "__init__.py", "README.md", "AGENTS.md", "CLAUDE.md"})


class ReleaseScopeError(ValueError):
    """Raised when the release boundary is ambiguous or incomplete."""


def _repository_root(value: str | None) -> Path:
    root = Path(value).resolve() if value else Path(__file__).resolve().parents[2]
    if not (root / "agent" / "service_v2_plugins" / "_shared").is_dir():
        raise ReleaseScopeError(f"repository root is invalid: {root}")
    return root


def _plugin_root(repository_root: Path) -> Path:
    return repository_root / "agent" / "service_v2_plugins"


def _python_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.py")
                  if path.is_file() and "__pycache__" not in path.parts)


def release_source_files(repository_root: Path) -> tuple[Path, ...]:
    root = _plugin_root(repository_root)
    paths = _python_files(root / "_shared")
    for plugin_id in sorted(SERVICE_V2_PLUGIN_IDS):
        required_files = (
            ("manifest.json", "payload/main.py")
            if plugin_id in STANDALONE_SERVICE_V2_PLUGIN_IDS
            else ("manifest.json", "payload/plugin.py", "settings/index.html")
        )
        for required in required_files:
            if not (root / plugin_id / required).is_file():
                raise ReleaseScopeError(f"release payload is incomplete: {plugin_id}/{required}")
        paths.extend(_python_files(root / plugin_id))
    return tuple(sorted(set(paths)))


def test_files(repository_root: Path, *, suite: str) -> tuple[Path, ...]:
    roots: list[Path] = []
    if suite in {"root", "all"}:
        roots.append(repository_root / "tests")
    if suite in {"agent", "all"}:
        roots.append(repository_root / "agent" / "tests")
    if not roots:
        raise ReleaseScopeError(f"unsupported test suite: {suite}")
    return tuple(sorted(path for root in roots for path in root.glob("test_*.py") if path.is_file()))


def quality_files(repository_root: Path) -> tuple[Path, ...]:
    plugin_root = _plugin_root(repository_root)
    release_sources = set(release_source_files(repository_root))
    paths: list[Path] = []
    for root in (repository_root / "agent", repository_root / "shared", repository_root / "tests"):
        for path in _python_files(root):
            if path.is_relative_to(plugin_root) and path not in release_sources:
                continue
            paths.append(path)
    return tuple(sorted(set(paths)))


def verify_staged_tree(repository_root: Path) -> None:
    """Fail if a staged server payload contains unknown or incomplete packages."""

    root = _plugin_root(repository_root)
    allowed = SERVICE_V2_PLUGIN_IDS | _PLUGIN_ROOT_FILES
    unexpected = sorted(p.name for p in root.iterdir() if p.is_symlink() or p.name not in allowed)
    if unexpected:
        raise ReleaseScopeError(f"staged plugin source contains unexpected entries: {unexpected}")
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
    sources.add_argument("--null", action="store_true")
    tests = subparsers.add_parser("test-files")
    tests.add_argument("--suite", choices=("root", "agent", "all"), default="all")
    tests.add_argument("--null", action="store_true")
    quality = subparsers.add_parser("quality-files")
    quality.add_argument("--null", action="store_true")
    subparsers.add_parser("verify-staged")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = _repository_root(args.repository_root)
    if args.command == "plugin-ids":
        _emit(sorted(SERVICE_V2_PLUGIN_IDS), repository_root=root, nul=args.null)
    elif args.command == "source-files":
        _emit(release_source_files(root), repository_root=root, nul=args.null)
    elif args.command == "test-files":
        _emit(test_files(root, suite=args.suite), repository_root=root, nul=args.null)
    elif args.command == "quality-files":
        _emit(quality_files(root), repository_root=root, nul=args.null)
    elif args.command == "verify-staged":
        verify_staged_tree(root)
        print("release_source_scope=ok")
    else:  # pragma: no cover - argparse enforces the closed command set.
        raise ReleaseScopeError(f"unsupported command: {args.command}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ReleaseScopeError, SyntaxError, UnicodeError) as exc:
        print(f"release_source_scope=blocked reason={exc}", file=sys.stderr)
        raise SystemExit(1) from None
