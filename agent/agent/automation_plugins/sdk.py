"""Source of the credential-free SDK embedded in uploaded plugin packages."""

from __future__ import annotations


PLUGIN_SDK_SOURCE = '''"""Credential-free client for the Boyi core automation broker."""
from __future__ import annotations

import json
import os
import socket
import uuid

_MAX_RESPONSE = 10 * 1024 * 1024
_MAX_REQUEST = 10 * 1024 * 1024


def _broker_timeout():
    raw = os.environ.get("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "")
    if not raw.isdigit():
        raise RuntimeError("core broker timeout is unavailable")
    value = int(raw)
    if not 1 <= value <= 3600:
        raise RuntimeError("core broker timeout is invalid")
    return value


def broker_call(operation, *, action, role, arguments):
    endpoint = os.environ.get("BOYI_PLUGIN_BROKER_ENDPOINT", "")
    capability = os.environ.get("BOYI_PLUGIN_EXECUTION_CAPABILITY", "")
    if not endpoint.startswith("unix://") or not capability:
        raise RuntimeError("core broker capability is unavailable")
    request = {
        "schema_version": 1,
        "request_id": str(uuid.uuid4()),
        "capability": capability,
        "operation": str(operation),
        "action": str(action),
        "role": str(role),
        "arguments": dict(arguments),
    }
    payload = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\\n"
    if len(payload) > _MAX_REQUEST:
        raise RuntimeError("core broker request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_broker_timeout())
        client.connect(endpoint[len("unix://"):])
        client.sendall(payload)
        chunks = []
        size = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_RESPONSE:
                raise RuntimeError("core broker response is too large")
            chunks.append(chunk)
    response = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise RuntimeError(str(response.get("error_code") if isinstance(response, dict) else "BROKER_RESPONSE_INVALID"))
    return response.get("data")
'''


__all__ = ["PLUGIN_SDK_SOURCE"]
