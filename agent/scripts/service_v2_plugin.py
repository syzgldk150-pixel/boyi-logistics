"""Offline developer CLI for deterministic Service v2 plugin artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent.automation_plugins.developer_v2 import (
    ServiceV2DeveloperError,
    build_service_v2_package,
    init_service_v2_source,
    inspect_service_v2_artifact,
    load_local_json_object,
    load_verified_local_artifact,
    validate_service_v2_artifact,
)
from agent.automation_plugins.developer_reports_v2 import (
    diff_verified_packages,
    project_permission_report,
)
from agent.automation_plugins.developer_simulator_v2 import (
    run_service_v2_scenarios,
)
from agent.automation_plugins.errors import AutomationPluginError
from agent.automation_plugins.fixture_connectors import (
    invoke_fixture_tracking_query,
)


def _configure_windows_streams() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


_configure_windows_streams()


class _CliUsageError(RuntimeError):
    code = "CLI_USAGE_ERROR"


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliUsageError(message)


CommandHandler = Callable[[argparse.Namespace], Mapping[str, Any]]


def _handle_init(args: argparse.Namespace) -> Mapping[str, Any]:
    source = init_service_v2_source(
        args.destination,
        plugin_id=args.plugin_id,
        name=args.name,
        version=args.version,
    )
    return validate_service_v2_artifact(source)


def _handle_validate(args: argparse.Namespace) -> Mapping[str, Any]:
    return validate_service_v2_artifact(args.artifact)


def _handle_test(args: argparse.Namespace) -> Mapping[str, Any]:
    verified = load_verified_local_artifact(args.artifact)
    scenarios = load_local_json_object(args.scenarios)
    return run_service_v2_scenarios(
        verified,
        scenarios,
        timeout_seconds=args.timeout_seconds,
    )


def _handle_permissions(args: argparse.Namespace) -> Mapping[str, Any]:
    return project_permission_report(load_verified_local_artifact(args.artifact))


def _handle_package(args: argparse.Namespace) -> Mapping[str, Any]:
    output = build_service_v2_package(args.source, args.output)
    return validate_service_v2_artifact(output)


def _handle_inspect(args: argparse.Namespace) -> Mapping[str, Any]:
    return inspect_service_v2_artifact(args.artifact)


def _handle_diff(args: argparse.Namespace) -> Mapping[str, Any]:
    return diff_verified_packages(
        load_verified_local_artifact(args.before),
        load_verified_local_artifact(args.after),
    )


def _handle_connector_test(args: argparse.Namespace) -> Mapping[str, Any]:
    return asyncio.run(
        invoke_fixture_tracking_query(
            fixture_root=args.fixture_root,
            fixture_path=args.fixture,
            tracking_number=args.tracking_number,
        )
    )


def _register_core_commands(subparsers: Any) -> None:
    init_parser = subparsers.add_parser(
        "init",
        help="create a minimal compute-only Service v2 source tree",
    )
    init_parser.add_argument("destination", type=Path)
    init_parser.add_argument("--plugin-id", required=True)
    init_parser.add_argument("--name")
    init_parser.add_argument("--version", default="0.1.0")
    init_parser.set_defaults(_handler=_handle_init)

    validate_parser = subparsers.add_parser(
        "validate",
        help="verify a source tree or existing ZIP without side effects",
    )
    validate_parser.add_argument("artifact", type=Path)
    validate_parser.set_defaults(_handler=_handle_validate)

    test_parser = subparsers.add_parser(
        "test",
        help="run closed local fixtures in the fail-closed Linux sandbox",
    )
    test_parser.add_argument("artifact", type=Path)
    test_parser.add_argument("--scenarios", required=True, type=Path)
    test_parser.add_argument("--timeout-seconds", type=int, default=30)
    test_parser.set_defaults(_handler=_handle_test)

    permissions_parser = subparsers.add_parser(
        "permissions",
        help="project declared authority without creating grants",
    )
    permissions_parser.add_argument("artifact", type=Path)
    permissions_parser.set_defaults(_handler=_handle_permissions)

    package_parser = subparsers.add_parser(
        "package",
        help="build a deterministic verified ZIP at a new path",
    )
    package_parser.add_argument("source", type=Path)
    package_parser.add_argument("output", type=Path)
    package_parser.set_defaults(_handler=_handle_package)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="show canonical identities and safe contract summaries",
    )
    inspect_parser.add_argument("artifact", type=Path)
    inspect_parser.set_defaults(_handler=_handle_inspect)

    diff_parser = subparsers.add_parser(
        "diff",
        help="compare two verified packages without a compatibility claim",
    )
    diff_parser.add_argument("before", type=Path)
    diff_parser.add_argument("after", type=Path)
    diff_parser.set_defaults(_handler=_handle_diff)

    connector_parser = subparsers.add_parser(
        "connector-test",
        help="query the opt-in offline tracking fixture through ConnectorRegistry",
    )
    connector_parser.add_argument("--fixture-root", required=True, type=Path)
    connector_parser.add_argument("--fixture", required=True, type=Path)
    connector_parser.add_argument("--tracking-number", required=True)
    connector_parser.set_defaults(_handler=_handle_connector_test)


def build_parser() -> argparse.ArgumentParser:
    parser = _StableArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    _register_core_commands(subparsers)
    return parser


def _emit(stream: Any, value: Mapping[str, Any]) -> None:
    json.dump(
        dict(value),
        stream,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    stream.write("\n")


def _stable_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, _CliUsageError):
        code = exc.code
        message = str(exc)
    elif isinstance(exc, (ServiceV2DeveloperError, AutomationPluginError)):
        code = str(exc.code)
        message = str(exc)
    elif isinstance(exc, FileExistsError):
        code = "LOCAL_TARGET_EXISTS"
        message = "local target already exists"
    elif isinstance(exc, FileNotFoundError):
        code = "LOCAL_ARTIFACT_NOT_FOUND"
        message = "local artifact does not exist"
    elif isinstance(exc, PermissionError):
        code = "LOCAL_ACCESS_DENIED"
        message = "local filesystem access was denied"
    elif isinstance(exc, OSError):
        code = "LOCAL_IO_ERROR"
        message = "local filesystem operation failed"
    else:
        code = "LOCAL_ARTIFACT_INVALID"
        message = str(exc)
    return {"ok": False, "error": {"code": code, "message": message}}


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        handler = getattr(args, "_handler", None)
        if not callable(handler):
            raise _CliUsageError("a command is required")
        data = handler(args)
    except (
        _CliUsageError,
        AutomationPluginError,
        ServiceV2DeveloperError,
        OSError,
        ValueError,
    ) as exc:
        _emit(sys.stderr, _stable_error(exc))
        return 2
    _emit(sys.stdout, {"ok": True, "data": dict(data)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
