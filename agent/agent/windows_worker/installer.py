"""Auditable Windows Service/Tray registration without credential reads."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Mapping

from agent.automation_plugins.errors import WorkerProtocolError
from agent.windows_worker.configuration import (
    WindowsWorkerConfiguration,
    load_windows_worker_configuration,
)
from agent.windows_worker.state import read_worker_cleanup_safety_snapshot


_SCRIPT_TIMEOUT_SECONDS = 120


CommandRunner = Callable[[Sequence[str]], int]


def _default_command_runner(arguments: Sequence[str]) -> int:
    completed = subprocess.run(
        list(arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=_SCRIPT_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return int(completed.returncode)


def _absolute_regular_file(value: Path | str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be one absolute non-symlink file")
    return path.resolve()


def _closed_tray_user(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("Windows Tray user identity is invalid")
    return value


class WindowsWorkerInstaller:
    """Register/unregister only; never create keys, read secrets or call HTTPS."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner = _default_command_runner,
        platform_name: str = os.name,
        safety_reader: Callable[[Path], Mapping[str, int]] = (
            read_worker_cleanup_safety_snapshot
        ),
    ) -> None:
        self._run_command = command_runner
        self._platform_name = platform_name
        self._safety_reader = safety_reader
        self._script_path = _absolute_regular_file(
            Path(__file__).with_name("manage_installation.ps1"),
            "Windows Worker installation script",
        )
        self._host_entrypoint = _absolute_regular_file(
            Path(__file__).resolve().parents[2] / "windows_worker_host.py",
            "Windows Worker host entrypoint",
        )

    def _require_windows(self) -> None:
        if self._platform_name != "nt":
            raise WorkerProtocolError(
                "Windows Worker installation requires Windows",
                code="WORKER_WINDOWS_REQUIRED",
            )

    @staticmethod
    def _task_name(config: WindowsWorkerConfiguration) -> str:
        return f"BoyiAutomationTray-{config.device_id}"

    def _invoke_script(
        self,
        *,
        mode: str,
        config_path: Path,
        config: WindowsWorkerConfiguration,
        python_executable: Path,
        pythonw_executable: Path,
        tray_user: str,
    ) -> None:
        service_command = subprocess.list2cmdline(
            [
                str(python_executable),
                str(self._host_entrypoint),
                "--config",
                str(config_path),
                "service",
            ]
        )
        tray_arguments = subprocess.list2cmdline(
            [
                str(self._host_entrypoint),
                "--config",
                str(config_path),
                "tray",
            ]
        )
        arguments = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-File",
            str(self._script_path),
            "-Mode",
            mode,
            "-ServiceName",
            config.service_name,
            "-TaskName",
            self._task_name(config),
            "-ServiceCommand",
            service_command,
            "-TrayExecutable",
            str(pythonw_executable),
            "-TrayArguments",
            tray_arguments,
            "-TrayUser",
            tray_user,
            "-ConfigPath",
            str(config_path),
            "-StateRoot",
            str(config.state_root),
            "-PackageRoot",
            str(config.package_root),
            "-TrayPipeKeyPath",
            str(config.tray_pipe_key_path),
            "-DeviceSigningKeyPath",
            str(config.device_signing_key_path),
            "-TlsClientPrivateKeyPath",
            str(config.tls_client_private_key_path),
            "-TlsClientCertificatePath",
            str(config.tls_client_certificate_path),
            "-TlsCaPath",
            str(config.tls_ca_path),
            "-ServerTrustRoot",
            str(config.server_trust_root),
            "-PackageTrustRoot",
            str(config.package_trust_root),
        ]
        try:
            return_code = self._run_command(arguments)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkerProtocolError(
                "Windows Worker installation command failed",
                code="WORKER_INSTALLATION_COMMAND_FAILED",
            ) from exc
        if isinstance(return_code, bool) or not isinstance(return_code, int) or return_code != 0:
            raise WorkerProtocolError(
                "Windows Worker installation command failed",
                code="WORKER_INSTALLATION_COMMAND_FAILED",
            )

    @staticmethod
    def _python_hosts(python_executable: Path | str) -> tuple[Path, Path]:
        python = _absolute_regular_file(python_executable, "Windows Python executable")
        if python.name.casefold() != "python.exe":
            raise ValueError("Windows Worker installation requires python.exe")
        pythonw = _absolute_regular_file(
            python.with_name("pythonw.exe"),
            "Windows pythonw executable",
        )
        return python, pythonw

    def install(
        self,
        *,
        config_path: Path | str,
        python_executable: Path | str,
        tray_user: str,
    ) -> None:
        self._require_windows()
        config_file = _absolute_regular_file(config_path, "Windows Worker configuration")
        config = load_windows_worker_configuration(config_file)
        python, pythonw = self._python_hosts(python_executable)
        self._invoke_script(
            mode="Install",
            config_path=config_file,
            config=config,
            python_executable=python,
            pythonw_executable=pythonw,
            tray_user=_closed_tray_user(tray_user),
        )

    @staticmethod
    def _empty_or_absent_directory(path: Path) -> bool:
        if not path.exists():
            return True
        if path.is_symlink() or not path.is_dir():
            return False
        try:
            next(path.iterdir())
        except StopIteration:
            return True
        return False

    def _uninstall_safety_snapshot(
        self,
        config: WindowsWorkerConfiguration,
    ) -> dict[str, int]:
        database_path = config.state_root / "worker-state.sqlite3"
        if not database_path.exists():
            if self._empty_or_absent_directory(
                config.state_root
            ) and self._empty_or_absent_directory(config.package_root):
                return {"active_jobs": 0, "unknown_writes": 0}
            raise WorkerProtocolError(
                "Windows Worker state is unavailable for safe uninstall",
                code="WORKER_UNINSTALL_STATE_UNAVAILABLE",
            )
        if database_path.is_symlink() or not database_path.is_file():
            raise WorkerProtocolError(
                "Windows Worker state database is unsafe",
                code="WORKER_UNINSTALL_STATE_UNAVAILABLE",
            )
        try:
            snapshot = dict(self._safety_reader(database_path))
        except (OSError, ValueError) as exc:
            raise WorkerProtocolError(
                "Windows Worker state is unavailable for safe uninstall",
                code="WORKER_UNINSTALL_STATE_UNAVAILABLE",
            ) from exc
        if set(snapshot) != {"active_jobs", "unknown_writes"} or any(
            isinstance(snapshot[field], bool)
            or not isinstance(snapshot[field], int)
            or snapshot[field] < 0
            for field in snapshot
        ):
            raise WorkerProtocolError(
                "Windows Worker uninstall safety snapshot is invalid",
                code="WORKER_UNINSTALL_STATE_UNAVAILABLE",
            )
        return snapshot

    def uninstall(
        self,
        *,
        config_path: Path | str,
        python_executable: Path | str,
        tray_user: str,
    ) -> None:
        self._require_windows()
        config_file = _absolute_regular_file(config_path, "Windows Worker configuration")
        config = load_windows_worker_configuration(config_file)
        python, pythonw = self._python_hosts(python_executable)
        closed_user = _closed_tray_user(tray_user)
        # Graceful SCM stop comes first.  The script never force-kills an
        # in-flight action, and it stops the Tray task only after SCM reports
        # STOPPED.  No registration or local bytes are removed at this stage.
        self._invoke_script(
            mode="Stop",
            config_path=config_file,
            config=config,
            python_executable=python,
            pythonw_executable=pythonw,
            tray_user=closed_user,
        )
        snapshot = self._uninstall_safety_snapshot(config)
        if snapshot["active_jobs"] or snapshot["unknown_writes"]:
            raise WorkerProtocolError(
                "Active jobs or unknown writes block Windows Worker uninstall",
                code="WORKER_UNINSTALL_BLOCKED",
            )
        self._invoke_script(
            mode="Remove",
            config_path=config_file,
            config=config,
            python_executable=python,
            pythonw_executable=pythonw,
            tray_user=closed_user,
        )


def install_windows_worker(
    *,
    config_path: Path | str,
    python_executable: Path | str,
    tray_user: str,
) -> None:
    WindowsWorkerInstaller().install(
        config_path=config_path,
        python_executable=python_executable,
        tray_user=tray_user,
    )


def uninstall_windows_worker(
    *,
    config_path: Path | str,
    python_executable: Path | str,
    tray_user: str,
) -> None:
    WindowsWorkerInstaller().uninstall(
        config_path=config_path,
        python_executable=python_executable,
        tray_user=tray_user,
    )
