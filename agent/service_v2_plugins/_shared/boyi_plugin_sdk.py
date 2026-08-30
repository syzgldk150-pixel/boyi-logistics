"""Credential-free client for the Boyi Host API broker."""

from __future__ import annotations

import json
import os
import socket
import uuid
import zlib


_MAX_RESPONSE = 10 * 1024 * 1024
_MAX_REQUEST = 64 * 1024 * 1024
_MAX_COMPRESSED_REQUEST = 16 * 1024 * 1024
_FRAME_PREFIX = b"BOYI-BROKER-V2 "


def _broker_timeout() -> int:
    raw = os.environ.get("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "")
    if not raw.isdigit():
        raise RuntimeError("BROKER_TIMEOUT_UNAVAILABLE")
    value = int(raw)
    if not 1 <= value <= 3600:
        raise RuntimeError("BROKER_TIMEOUT_INVALID")
    return value


def broker_call(
    operation: str,
    *,
    action: str,
    role: str,
    arguments: dict[str, object],
) -> object:
    """Invoke one manifest-declared capability without receiving credentials."""

    endpoint = os.environ.get("BOYI_PLUGIN_BROKER_ENDPOINT", "")
    capability = os.environ.get("BOYI_PLUGIN_EXECUTION_CAPABILITY", "")
    if not endpoint.startswith("unix://") or not capability:
        raise RuntimeError("BROKER_CAPABILITY_UNAVAILABLE")
    request = {
        "schema_version": 1,
        "request_id": str(uuid.uuid4()),
        "capability": capability,
        "operation": str(operation),
        "action": str(action),
        "role": str(role),
        "arguments": dict(arguments),
    }
    payload = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_REQUEST:
        raise RuntimeError("BROKER_REQUEST_TOO_LARGE")
    compressed = zlib.compress(payload)
    if len(compressed) > _MAX_COMPRESSED_REQUEST:
        raise RuntimeError("BROKER_REQUEST_TOO_LARGE")
    frame = _FRAME_PREFIX + str(len(compressed)).encode("ascii") + b"\n" + compressed
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_broker_timeout())
        client.connect(endpoint.removeprefix("unix://"))
        client.sendall(frame)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_RESPONSE:
                raise RuntimeError("BROKER_RESPONSE_TOO_LARGE")
            chunks.append(chunk)
    response = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(response, dict) or response.get("ok") is not True:
        code = response.get("error_code") if isinstance(response, dict) else None
        raise RuntimeError(str(code or "BROKER_RESPONSE_INVALID"))
    return response.get("data")
