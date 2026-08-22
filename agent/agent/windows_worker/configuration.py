"""Strict file-path-only composition for the Windows background service."""

from __future__ import annotations

import json
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.automation_plugins.package import load_ed25519_trust_store
from agent.automation_plugins.storage import FilesystemPluginStorage
from agent.windows_worker.background_service import WindowsWorkerBackgroundService
from agent.windows_worker.dpapi import load_ed25519_device_signer, read_protected_secret
from agent.windows_worker.local_runtime import WindowsLocalPluginRuntime
from agent.windows_worker.protocol import RepositoryReplayGuard
from agent.windows_worker.service_loop import WindowsWorkerServiceLoop
from agent.windows_worker.state import WindowsWorkerStateStore
from agent.windows_worker.transport import HttpsWorkerTransport
from agent.windows_worker.tray_ipc import NamedPipeTrayClient


_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "service_name",
        "device_id",
        "worker_version",
        "server_url",
        "state_root",
        "package_root",
        "server_trust_root",
        "package_trust_root",
        "server_key_id",
        "device_key_id",
        "device_signing_key_path",
        "tray_pipe_key_path",
        "tls_ca_path",
        "tls_client_certificate_path",
        "tls_client_private_key_path",
        "heartbeat_seconds",
    }
)
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def _absolute_path(value: Any, label: str, *, directory: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError(f"{label} must be one absolute non-symlink path")
    if directory:
        if not path.is_dir():
            raise ValueError(f"{label} directory does not exist")
    elif not path.is_file():
        raise ValueError(f"{label} file does not exist")
    return path.resolve()


@dataclass(frozen=True)
class WindowsWorkerConfiguration:
    service_name: str
    device_id: str
    worker_version: str
    server_url: str
    state_root: Path
    package_root: Path
    server_trust_root: Path
    package_trust_root: Path
    server_key_id: str
    device_key_id: str
    device_signing_key_path: Path
    tray_pipe_key_path: Path
    tls_ca_path: Path
    tls_client_certificate_path: Path
    tls_client_private_key_path: Path
    heartbeat_seconds: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> WindowsWorkerConfiguration:
        raw = dict(value)
        if set(raw) != _CONFIG_FIELDS or raw["schema_version"] != 1:
            raise ValueError("Windows Worker configuration schema is invalid")
        text_fields = (
            "device_id",
            "server_url",
            "server_key_id",
            "device_key_id",
        )
        if any(
            not isinstance(raw[field], str) or not raw[field] or len(raw[field]) > 256
            for field in text_fields
        ):
            raise ValueError("Windows Worker configuration identity is invalid")
        if not isinstance(raw["service_name"], str) or not _SERVICE_NAME_RE.fullmatch(
            raw["service_name"]
        ):
            raise ValueError("Windows Worker service_name is invalid")
        if any(
            not _IDENTIFIER_RE.fullmatch(raw[field])
            for field in ("device_id", "server_key_id", "device_key_id")
        ):
            raise ValueError("Windows Worker identifier is invalid")
        if not isinstance(raw["worker_version"], str) or not _VERSION_RE.fullmatch(
            raw["worker_version"]
        ):
            raise ValueError("Windows Worker version is invalid")
        heartbeat = raw["heartbeat_seconds"]
        if isinstance(heartbeat, bool) or not isinstance(heartbeat, int) or not 10 <= heartbeat <= 300:
            raise ValueError("Windows Worker heartbeat_seconds must be from 10 to 300")
        state_root = Path(str(raw["state_root"]))
        package_root = Path(str(raw["package_root"]))
        for path, label in ((state_root, "state_root"), (package_root, "package_root")):
            if not path.is_absolute() or path.is_symlink() or path == path.parent:
                raise ValueError(f"Windows Worker {label} is unsafe")
        if state_root.resolve() == package_root.resolve():
            raise ValueError("Windows Worker state and package roots must be distinct")
        return cls(
            service_name=str(raw["service_name"]),
            device_id=str(raw["device_id"]),
            worker_version=str(raw["worker_version"]),
            server_url=str(raw["server_url"]),
            state_root=state_root.resolve(),
            package_root=package_root.resolve(),
            server_trust_root=_absolute_path(raw["server_trust_root"], "server_trust_root", directory=True),
            package_trust_root=_absolute_path(raw["package_trust_root"], "package_trust_root", directory=True),
            server_key_id=str(raw["server_key_id"]),
            device_key_id=str(raw["device_key_id"]),
            device_signing_key_path=_absolute_path(
                raw["device_signing_key_path"],
                "device_signing_key_path",
                directory=False,
            ),
            tray_pipe_key_path=_absolute_path(
                raw["tray_pipe_key_path"],
                "tray_pipe_key_path",
                directory=False,
            ),
            tls_ca_path=_absolute_path(raw["tls_ca_path"], "tls_ca_path", directory=False),
            tls_client_certificate_path=_absolute_path(
                raw["tls_client_certificate_path"],
                "tls_client_certificate_path",
                directory=False,
            ),
            tls_client_private_key_path=_absolute_path(
                raw["tls_client_private_key_path"],
                "tls_client_private_key_path",
                directory=False,
            ),
            heartbeat_seconds=heartbeat,
        )


def load_windows_worker_configuration(path: Path | str) -> WindowsWorkerConfiguration:
    target = Path(path)
    if not target.is_absolute() or target.is_symlink() or not target.is_file():
        raise ValueError("Windows Worker configuration path is missing or unsafe")
    if target.stat().st_size <= 0 or target.stat().st_size > 64 * 1024:
        raise ValueError("Windows Worker configuration size is invalid")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Windows Worker configuration is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("Windows Worker configuration must be an object")
    return WindowsWorkerConfiguration.from_mapping(raw)


def build_windows_worker_service(config: WindowsWorkerConfiguration) -> WindowsWorkerServiceLoop:
    """Compose production ports; missing files/keys fail before SCM starts."""

    # Validate every read-only trust/key dependency before creating the SQLite
    # state database or package directories.  A broken deployment must leave
    # no partially initialized Worker state behind.
    tls_context = ssl.create_default_context(cafile=str(config.tls_ca_path))
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(
        certfile=str(config.tls_client_certificate_path),
        keyfile=str(config.tls_client_private_key_path),
    )
    transport = HttpsWorkerTransport(
        base_url=config.server_url,
        device_id=config.device_id,
        ssl_context=tls_context,
    )
    package_trust = load_ed25519_trust_store(config.package_trust_root)
    server_trust = load_ed25519_trust_store(config.server_trust_root)
    entropy = f"boyi-worker:{config.device_id}".encode("utf-8")
    device_signer = load_ed25519_device_signer(
        config.device_signing_key_path,
        entropy=entropy + b":signing",
        key_id=config.device_key_id,
    )
    pipe_key = read_protected_secret(
        config.tray_pipe_key_path,
        entropy=entropy + b":tray-pipe",
    )
    state = WindowsWorkerStateStore(config.state_root)
    tray = NamedPipeTrayClient(device_id=config.device_id, auth_key=pipe_key)
    runtime = WindowsLocalPluginRuntime(
        state=state,
        package_storage=FilesystemPluginStorage(config.package_root),
        signature_verifier=package_trust,
        package_fetcher=transport,
    )
    background = WindowsWorkerBackgroundService(
        device_id=config.device_id,
        runtime=runtime,
        tray=tray,
    )

    def heartbeat_snapshot() -> dict[str, Any]:
        try:
            session_state = tray.session_state()
        except Exception:
            session_state = "LOGGED_OUT"
        return {
            "service_state": "ONLINE",
            "session_state": session_state,
            # Direct/local execution stays held. A single signed dispatch
            # authorization is required for every command.
            "release_hold": True,
            "active_jobs": state.count_active_jobs(),
        }

    return WindowsWorkerServiceLoop(
        device_id=config.device_id,
        worker_version=config.worker_version,
        service=background,
        transport=transport,
        state=state,
        server_verifier=server_trust,
        server_key_id=config.server_key_id,
        device_signer=device_signer,
        replay_guard=RepositoryReplayGuard(state),
        heartbeat_snapshot=heartbeat_snapshot,
        heartbeat_seconds=config.heartbeat_seconds,
    )
