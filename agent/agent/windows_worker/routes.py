"""Closed route identities shared by the Worker server and local client."""

from __future__ import annotations

import re
import uuid
from typing import Any


WORKER_POLL_PATH = "/internal/v1/automation/worker/commands/poll"
WORKER_MESSAGES_PATH = "/internal/v1/automation/worker/messages"
WORKER_PACKAGE_PREFIX = "/internal/v1/automation/worker/packages/"

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_uuid(value: Any) -> str | None:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None
    canonical = str(parsed)
    return canonical if canonical == value else None


def build_worker_package_path(
    *,
    plugin_id: str,
    version: str,
    package_sha256: str,
    dispatch_authorization_id: str,
) -> str:
    """Build the sole relative path accepted for one signed package claim."""

    if (
        not _PLUGIN_ID_RE.fullmatch(str(plugin_id or ""))
        or not _VERSION_RE.fullmatch(str(version or ""))
        or not _SHA256_RE.fullmatch(str(package_sha256 or ""))
        or _canonical_uuid(dispatch_authorization_id) is None
    ):
        raise ValueError("Worker package route identity is invalid")
    return (
        f"{WORKER_PACKAGE_PREFIX}{plugin_id}/{version}/"
        f"{package_sha256}/{dispatch_authorization_id}"
    )


def parse_worker_package_path(path: str) -> tuple[str, str, str, str] | None:
    value = str(path or "")
    if not value.startswith(WORKER_PACKAGE_PREFIX):
        return None
    parts = value[len(WORKER_PACKAGE_PREFIX) :].split("/")
    if len(parts) != 4:
        return None
    plugin_id, version, package_sha256, authorization_id = parts
    try:
        canonical = build_worker_package_path(
            plugin_id=plugin_id,
            version=version,
            package_sha256=package_sha256,
            dispatch_authorization_id=authorization_id,
        )
    except ValueError:
        return None
    if canonical != value:
        return None
    return plugin_id, version, package_sha256, authorization_id


def is_worker_transport_path(path: str) -> bool:
    """Return true only for the three exact Worker transport route shapes."""

    value = str(path or "")
    return value in {WORKER_POLL_PATH, WORKER_MESSAGES_PATH} or (
        parse_worker_package_path(value) is not None
    )


__all__ = [
    "WORKER_MESSAGES_PATH",
    "WORKER_PACKAGE_PREFIX",
    "WORKER_POLL_PATH",
    "build_worker_package_path",
    "is_worker_transport_path",
    "parse_worker_package_path",
]
