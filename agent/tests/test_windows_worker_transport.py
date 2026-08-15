from __future__ import annotations

import hashlib
import io
import ssl
from email.message import Message
from typing import Any

import pytest

from agent.automation_plugins.errors import PluginPackageError, WorkerProtocolError
from agent.windows_worker.transport import HttpsWorkerTransport


class _Response:
    def __init__(self, *, status: int, body: bytes, content_type: str) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _Opener:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: int) -> _Response:
        self.requests.append((request, timeout))
        return self.responses.pop(0)


def _transport() -> HttpsWorkerTransport:
    context = ssl.create_default_context()
    return HttpsWorkerTransport(
        base_url="https://worker.test/control",
        device_id="office_pc_one",
        ssl_context=context,
    )


def test_transport_requires_verified_https_and_closed_json() -> None:
    context = ssl.create_default_context()
    with pytest.raises(ValueError, match="HTTPS origin"):
        HttpsWorkerTransport(
            base_url="http://worker.test",
            device_id="office_pc_one",
            ssl_context=context,
        )
    context.check_hostname = False
    with pytest.raises(ValueError, match="verify certificates"):
        HttpsWorkerTransport(
            base_url="https://worker.test",
            device_id="office_pc_one",
            ssl_context=context,
        )

    transport = _transport()
    transport._opener = _Opener(  # type: ignore[attr-defined]
        _Response(status=200, body=b'{"schema_version":1}', content_type="application/json")
    )
    assert transport.poll() == {"schema_version": 1}
    request, _ = transport._opener.requests[0]  # type: ignore[attr-defined]
    assert request.full_url.startswith(
        "https://worker.test/control/internal/v1/automation/worker/commands/poll?device_id="
    )
    assert request.headers["X-worker-device-id"] == "office_pc_one"

    transport._opener = _Opener(  # type: ignore[attr-defined]
        _Response(status=200, body=b"[]", content_type="application/json")
    )
    with pytest.raises(WorkerProtocolError, match="must be an object"):
        transport.poll()


def test_package_download_is_same_origin_bounded_and_digest_bound() -> None:
    package = b"signed-package"
    digest = hashlib.sha256(package).hexdigest()
    authorization_id = "f6d9dc71-b197-4800-bad3-4efe484406df"
    transport = _transport()
    transport._opener = _Opener(  # type: ignore[attr-defined]
        _Response(status=200, body=package, content_type="application/zip")
    )
    url = (
        "/internal/v1/automation/worker/packages/plugin/1.0.0/"
        f"{digest}/{authorization_id}"
    )
    assert transport.fetch_package(url, expected_sha256=digest) == package
    request, _ = transport._opener.requests[0]  # type: ignore[attr-defined]
    assert request.full_url == f"https://worker.test/control{url}"

    with pytest.raises(PluginPackageError, match="relative"):
        transport.fetch_package(
            f"https://worker.test/control{url}",
            expected_sha256=digest,
        )
    with pytest.raises(PluginPackageError, match="relative"):
        transport.fetch_package(
            f"https://attacker.test/control{url}",
            expected_sha256=digest,
        )
    transport._opener = _Opener(  # type: ignore[attr-defined]
        _Response(status=200, body=package, content_type="application/zip")
    )
    with pytest.raises(PluginPackageError, match="digest"):
        transport.fetch_package(
            (
                "/internal/v1/automation/worker/packages/plugin/1.0.0/"
                f"{'0' * 64}/{authorization_id}"
            ),
            expected_sha256="0" * 64,
        )
