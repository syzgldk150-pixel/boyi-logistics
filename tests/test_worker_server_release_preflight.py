from __future__ import annotations

import stat
from pathlib import Path

import pytest
from Crypto.PublicKey import ECC

from agent.automation_plugins.errors import PluginPackageError
from agent.windows_worker.release_preflight import (
    verify_worker_server_identity,
)


def _write_identity(tmp_path: Path, *, cursor_secret: str = "c" * 32) -> Path:
    key_path = tmp_path / "worker-server.pem"
    key_path.write_text(
        ECC.generate(curve="Ed25519").export_key(format="PEM"),
        encoding="ascii",
    )
    key_path.chmod(0o600)
    environment = tmp_path / "agent.env"
    environment.write_text(
        "\n".join(
            (
                f"BOYI_AUTOMATION_PLUGIN_CURSOR_SECRET={cursor_secret}",
                f"BOYI_AUTOMATION_WORKER_SERVER_SIGNING_KEY_PATH={key_path}",
                "BOYI_AUTOMATION_WORKER_SERVER_SIGNING_KEY_ID=worker-server-v1",
                "",
            )
        ),
        encoding="utf-8",
    )
    environment.chmod(0o600)
    return environment


def test_worker_server_identity_preflight_accepts_closed_ed25519_configuration(
    tmp_path: Path,
) -> None:
    verify_worker_server_identity(_write_identity(tmp_path))


@pytest.mark.parametrize(
    "failure",
    ("short-secret", "broad-env", "broad-key", "environment-symlink"),
)
def test_worker_server_identity_preflight_fails_closed(
    tmp_path: Path,
    failure: str,
) -> None:
    environment = _write_identity(
        tmp_path,
        cursor_secret="short" if failure == "short-secret" else "c" * 32,
    )
    if failure == "broad-env":
        environment.chmod(0o644)
    elif failure == "broad-key":
        values = environment.read_text(encoding="utf-8").splitlines()
        key_path = Path(values[1].split("=", 1)[1])
        key_path.chmod(0o644)
    elif failure == "environment-symlink":
        linked_environment = tmp_path / "agent-linked.env"
        linked_environment.symlink_to(environment)
        environment = linked_environment

    with pytest.raises((ValueError, OSError, PluginPackageError)):
        verify_worker_server_identity(environment)

    if failure == "broad-env":
        assert stat.S_IMODE(environment.stat().st_mode) == 0o644
