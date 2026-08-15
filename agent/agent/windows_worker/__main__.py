"""Production Windows Service entrypoint for the automation Worker."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from agent.windows_worker.configuration import (
    build_windows_worker_service,
    load_windows_worker_configuration,
)
from agent.windows_worker.installer import install_windows_worker, uninstall_windows_worker
from agent.windows_worker.tray_host import build_windows_tray_host
from agent.windows_worker.windows_service import run_windows_service


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boyi-windows-worker")
    parser.add_argument(
        "--config",
        required=True,
        help="Absolute path to the Worker JSON configuration",
    )
    parser.add_argument(
        "command",
        choices=("service", "tray", "validate", "install", "uninstall"),
        help="Validate, host, register or unregister the Windows Worker",
    )
    parser.add_argument(
        "--python-executable",
        help="Absolute python.exe path required only for install/uninstall",
    )
    parser.add_argument(
        "--tray-user",
        help="Exact Windows login principal required only for install/uninstall",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command in {"install", "uninstall"}:
        if not arguments.python_executable or not arguments.tray_user:
            parser.error("install/uninstall require --python-executable and --tray-user")
        operation = (
            install_windows_worker
            if arguments.command == "install"
            else uninstall_windows_worker
        )
        operation(
            config_path=arguments.config,
            python_executable=arguments.python_executable,
            tray_user=arguments.tray_user,
        )
        return 0
    configuration = load_windows_worker_configuration(arguments.config)
    if arguments.command == "tray":
        build_windows_tray_host(configuration).run_forever()
        return 0
    loop = build_windows_worker_service(configuration)
    if arguments.command == "validate":
        del loop
        return 0
    if arguments.command == "service":
        run_windows_service(configuration.service_name, loop)
        return 0
    raise AssertionError("unreachable Windows Worker command")


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
