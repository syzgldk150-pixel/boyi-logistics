from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.windows_worker import configuration as configuration_module
from agent.windows_worker import __main__ as worker_main_module
from agent.windows_worker.configuration import (
    WindowsWorkerConfiguration,
    build_windows_worker_service,
    load_windows_worker_configuration,
)


def _configuration_mapping(tmp_path: Path) -> dict[str, Any]:
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
        Path(values[field]).write_bytes(b"test-only-placeholder")
    return values


def test_configuration_is_closed_and_does_not_choose_unsafe_roots(tmp_path: Path) -> None:
    values = _configuration_mapping(tmp_path)
    config = WindowsWorkerConfiguration.from_mapping(values)
    assert config.device_id == "office_pc_one"
    assert config.heartbeat_seconds == 30

    with pytest.raises(ValueError, match="schema"):
        WindowsWorkerConfiguration.from_mapping({**values, "unexpected": True})
    with pytest.raises(ValueError, match="heartbeat_seconds"):
        WindowsWorkerConfiguration.from_mapping({**values, "heartbeat_seconds": True})
    with pytest.raises(ValueError, match="version"):
        WindowsWorkerConfiguration.from_mapping({**values, "worker_version": "latest"})
    with pytest.raises(ValueError, match="identifier"):
        WindowsWorkerConfiguration.from_mapping({**values, "device_id": "office pc"})
    with pytest.raises(ValueError, match="must be distinct"):
        WindowsWorkerConfiguration.from_mapping(
            {**values, "package_root": values["state_root"]}
        )


def test_configuration_file_is_bounded_utf8_json(tmp_path: Path) -> None:
    values = _configuration_mapping(tmp_path)
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    loaded = load_windows_worker_configuration(path)
    assert loaded.service_name == "BoyiAutomationWorker"

    path.write_bytes(b"{" + b"x" * (64 * 1024))
    with pytest.raises(ValueError, match="size"):
        load_windows_worker_configuration(path)


def test_composition_validates_trust_before_creating_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = WindowsWorkerConfiguration.from_mapping(_configuration_mapping(tmp_path))
    state_created = False

    class _TlsContext:
        minimum_version: object
        verify_mode = configuration_module.ssl.CERT_REQUIRED
        check_hostname = True

        def load_cert_chain(self, **_: Any) -> None:
            return None

    def _state_factory(_: Path) -> None:
        nonlocal state_created
        state_created = True
        raise AssertionError("state must not be created before trust validation")

    monkeypatch.setattr(
        configuration_module.ssl,
        "create_default_context",
        lambda **_: _TlsContext(),
    )
    monkeypatch.setattr(
        configuration_module,
        "load_ed25519_trust_store",
        lambda _path: (_ for _ in ()).throw(ValueError("invalid trust root")),
    )
    monkeypatch.setattr(configuration_module, "WindowsWorkerStateStore", _state_factory)

    with pytest.raises(ValueError, match="invalid trust root"):
        build_windows_worker_service(config)
    assert state_created is False


def test_service_entrypoint_has_no_implicit_foreground_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _configuration_mapping(tmp_path)
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    built = object()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(worker_main_module, "build_windows_worker_service", lambda _config: built)
    monkeypatch.setattr(
        worker_main_module,
        "run_windows_service",
        lambda service_name, loop: calls.append((service_name, loop)),
    )

    assert worker_main_module.main(("--config", str(path), "service")) == 0
    assert calls == [("BoyiAutomationWorker", built)]

    assert worker_main_module.main(("--config", str(path), "validate")) == 0
    assert calls == [("BoyiAutomationWorker", built)]

    with pytest.raises(SystemExit):
        worker_main_module.main(("--config", str(path), "console"))


def test_tray_and_registration_commands_use_only_their_explicit_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = _configuration_mapping(tmp_path)
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    tray_calls: list[str] = []
    registration_calls: list[tuple[str, dict[str, Any]]] = []

    class _TrayHost:
        def run_forever(self) -> None:
            tray_calls.append("run")

    monkeypatch.setattr(
        worker_main_module,
        "build_windows_tray_host",
        lambda config: (_TrayHost() if config.device_id == "office_pc_one" else None),
    )
    monkeypatch.setattr(
        worker_main_module,
        "build_windows_worker_service",
        lambda _config: (_ for _ in ()).throw(AssertionError("wrong host composed")),
    )
    monkeypatch.setattr(
        worker_main_module,
        "install_windows_worker",
        lambda **kwargs: registration_calls.append(("install", kwargs)),
    )
    monkeypatch.setattr(
        worker_main_module,
        "uninstall_windows_worker",
        lambda **kwargs: registration_calls.append(("uninstall", kwargs)),
    )

    assert worker_main_module.main(("--config", str(path), "tray")) == 0
    assert tray_calls == ["run"]
    common = (
        "--config",
        str(path),
        "--python-executable",
        r"C:\Python\python.exe",
        "--tray-user",
        r"BOYI\automation-user",
    )
    assert worker_main_module.main((*common, "install")) == 0
    assert worker_main_module.main((*common, "uninstall")) == 0
    assert [name for name, _kwargs in registration_calls] == ["install", "uninstall"]
    assert registration_calls[0][1]["config_path"] == str(path)

    with pytest.raises(SystemExit):
        worker_main_module.main(("--config", str(path), "install"))
