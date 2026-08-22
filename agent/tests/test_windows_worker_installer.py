from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest

from agent.automation_plugins.errors import WorkerProtocolError
from agent.windows_worker.installer import WindowsWorkerInstaller
from agent.windows_worker.models import WorkerJob, WorkerJobStatus, WorkerJobType
from agent.windows_worker.state import WindowsWorkerStateStore


def _configuration(tmp_path: Path) -> Path:
    server_trust = tmp_path / "server-trust"
    package_trust = tmp_path / "package-trust"
    server_trust.mkdir()
    package_trust.mkdir()
    values: dict[str, Any] = {
        "schema_version": 1,
        "service_name": "BoyiAutomationWorker",
        "device_id": "office_pc_one",
        "worker_version": "1.0.0",
        "server_url": "https://worker.test/control",
        "state_root": str(tmp_path / "state"),
        "package_root": str(tmp_path / "packages"),
        "server_trust_root": str(server_trust),
        "package_trust_root": str(package_trust),
        "server_key_id": "server-prod-1",
        "device_key_id": "device-office-pc-one",
        "device_signing_key_path": str(tmp_path / "device-key.dpapi"),
        "tray_pipe_key_path": str(tmp_path / "tray-key.dpapi"),
        "tls_ca_path": str(tmp_path / "ca.pem"),
        "tls_client_certificate_path": str(tmp_path / "client.pem"),
        "tls_client_private_key_path": str(tmp_path / "client-key.pem"),
        "heartbeat_seconds": 30,
    }
    for field in (
        "device_signing_key_path",
        "tray_pipe_key_path",
        "tls_ca_path",
        "tls_client_certificate_path",
        "tls_client_private_key_path",
    ):
        Path(values[field]).write_bytes(b"TEST_MATERIAL_MUST_NOT_BE_READ")
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def _python_hosts(tmp_path: Path) -> Path:
    python = tmp_path / "python.exe"
    python.write_bytes(b"test executable")
    python.with_name("pythonw.exe").write_bytes(b"test windowless executable")
    return python


class _Commands:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> int:
        self.calls.append(list(arguments))
        return 0


def _mode(arguments: Sequence[str]) -> str:
    index = list(arguments).index("-Mode")
    return str(arguments[index + 1])


def _active_job() -> WorkerJob:
    now = datetime.now(timezone.utc)
    return WorkerJob(
        job_id=str(uuid.uuid4()),
        automation_id="arrive_instance_one",
        plugin_id="sync_arrive_list",
        plugin_version="1.0.0",
        job_type=WorkerJobType.INVOKE,
        status=WorkerJobStatus.CLAIMED,
        payload={},
        target_device_id="office_pc_one",
        available_at=now,
        deadline_at=now + timedelta(minutes=5),
        requires_interactive_session=True,
        operation_type="external_write",
    )


def test_install_registers_service_and_login_task_without_reading_material(
    tmp_path: Path,
) -> None:
    config = _configuration(tmp_path)
    python = _python_hosts(tmp_path)
    commands = _Commands()
    installer = WindowsWorkerInstaller(command_runner=commands, platform_name="nt")
    installer.install(
        config_path=config,
        python_executable=python,
        tray_user=r"BOYI\automation-user",
    )
    assert len(commands.calls) == 1
    arguments = commands.calls[0]
    assert _mode(arguments) == "Install"
    assert "-NonInteractive" in arguments
    assert "RemoteSigned" in arguments
    assert str(python.with_name("pythonw.exe")) in arguments
    service_command = arguments[arguments.index("-ServiceCommand") + 1]
    tray_arguments = arguments[arguments.index("-TrayArguments") + 1]
    assert "windows_worker_host.py" in service_command
    assert service_command.endswith(" service")
    assert "windows_worker_host.py" in tray_arguments
    assert tray_arguments.endswith(" tray")
    assert all("TEST_MATERIAL_MUST_NOT_BE_READ" not in value for value in arguments)


def test_uninstall_stops_then_checks_durable_state_before_removing(tmp_path: Path) -> None:
    config = _configuration(tmp_path)
    python = _python_hosts(tmp_path)
    commands = _Commands()
    installer = WindowsWorkerInstaller(command_runner=commands, platform_name="nt")
    installer.uninstall(
        config_path=config,
        python_executable=python,
        tray_user=r"BOYI\automation-user",
    )
    assert [_mode(call) for call in commands.calls] == ["Stop", "Remove"]


@pytest.mark.parametrize("make_unknown", (False, True))
def test_active_or_unknown_write_blocks_host_removal(
    tmp_path: Path,
    make_unknown: bool,
) -> None:
    config = _configuration(tmp_path)
    python = _python_hosts(tmp_path)
    state = WindowsWorkerStateStore(tmp_path / "state")
    job = _active_job()
    assert state.begin_once(job)
    if make_unknown:
        assert state.prior_result(job.job_id)["status"] == "OUTCOME_UNKNOWN"
    commands = _Commands()
    installer = WindowsWorkerInstaller(command_runner=commands, platform_name="nt")
    with pytest.raises(WorkerProtocolError) as blocked:
        installer.uninstall(
            config_path=config,
            python_executable=python,
            tray_user=r"BOYI\automation-user",
        )
    assert blocked.value.code == "WORKER_UNINSTALL_BLOCKED"
    assert [_mode(call) for call in commands.calls] == ["Stop"]


def test_uninstall_does_not_assume_nonempty_state_without_database_is_safe(
    tmp_path: Path,
) -> None:
    config = _configuration(tmp_path)
    python = _python_hosts(tmp_path)
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "unverified-runtime").mkdir()
    commands = _Commands()
    installer = WindowsWorkerInstaller(command_runner=commands, platform_name="nt")
    with pytest.raises(WorkerProtocolError) as blocked:
        installer.uninstall(
            config_path=config,
            python_executable=python,
            tray_user=r"BOYI\automation-user",
        )
    assert blocked.value.code == "WORKER_UNINSTALL_STATE_UNAVAILABLE"
    assert [_mode(call) for call in commands.calls] == ["Stop"]


def test_installation_script_is_registration_only_and_has_no_secret_fallback() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "agent"
        / "windows_worker"
        / "manage_installation.ps1"
    ).read_text(encoding="utf-8")
    assert "New-Service" in script
    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "-LogonType Interactive" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "Start-Service" not in script
    assert "Start-ScheduledTask" not in script
    assert "Invoke-WebRequest" not in script
    assert "Remove-Item" not in script
    assert "Get-Credential" not in script
    assert "ConvertFrom-SecureString" not in script


def test_installer_refuses_non_windows_without_running_commands(tmp_path: Path) -> None:
    commands = _Commands()
    installer = WindowsWorkerInstaller(command_runner=commands, platform_name="posix")
    with pytest.raises(WorkerProtocolError) as wrong_platform:
        installer.install(
            config_path=_configuration(tmp_path),
            python_executable=_python_hosts(tmp_path),
            tray_user=r"BOYI\automation-user",
        )
    assert wrong_platform.value.code == "WORKER_WINDOWS_REQUIRED"
    assert commands.calls == []
