"""Read-only production identity preflight for the Worker command server."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path

from dotenv import dotenv_values

from agent.automation_plugins.production import production_cursor_secret
from agent.windows_worker.server_api import load_worker_server_signer


_KEY_PATH_ENV = "BOYI_AUTOMATION_WORKER_SERVER_SIGNING_KEY_PATH"
_KEY_ID_ENV = "BOYI_AUTOMATION_WORKER_SERVER_SIGNING_KEY_ID"
_MAX_ENVIRONMENT_BYTES = 1024 * 1024


def _read_closed_environment_file(environment_file: Path | str) -> dict[str, str]:
    requested = Path(environment_file)
    if not requested.is_absolute() or requested.is_symlink():
        raise ValueError("Worker server environment file must be an absolute regular file")
    target = requested.resolve(strict=True)
    if target != requested:
        raise ValueError("Worker server environment path cannot contain symbolic links")
    before = target.stat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > _MAX_ENVIRONMENT_BYTES
    ):
        raise ValueError("Worker server environment file must be a bounded regular file")
    if stat.S_IMODE(before.st_mode) & 0o077:
        raise ValueError("Worker server environment file permissions are too broad")

    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0) or 0)
    descriptor = os.open(target, flags)
    material = bytearray()
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) & 0o077
        ):
            raise ValueError("Worker server environment changed during inspection")
        while len(material) <= _MAX_ENVIRONMENT_BYTES:
            chunk = os.read(
                descriptor,
                min(8192, _MAX_ENVIRONMENT_BYTES + 1 - len(material)),
            )
            if not chunk:
                break
            material.extend(chunk)
        if not material or len(material) > _MAX_ENVIRONMENT_BYTES:
            raise ValueError("Worker server environment file is invalid")
        try:
            text = material.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Worker server environment file must be UTF-8") from exc
        parsed = dotenv_values(stream=io.StringIO(text))
        return {
            str(key): str(value or "")
            for key, value in parsed.items()
            if key is not None
        }
    finally:
        os.close(descriptor)
        for index in range(len(material)):
            material[index] = 0


def verify_worker_server_identity(environment_file: Path | str) -> None:
    values = _read_closed_environment_file(environment_file)
    # These are separate authorities; both are required and neither is logged.
    production_cursor_secret(values)
    load_worker_server_signer(
        private_key_path=values.get(_KEY_PATH_ENV, ""),
        key_id=values.get(_KEY_ID_ENV, ""),
    )


__all__ = ["verify_worker_server_identity"]
