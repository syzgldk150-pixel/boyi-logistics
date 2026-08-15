"""Bounded same-origin HTTPS transport for outbound-only Windows Workers."""

from __future__ import annotations

import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginPackageError, WorkerProtocolError
from agent.automation_plugins.package import MAX_ARCHIVE_BYTES
from agent.windows_worker.ports import WorkerTransportPort
from agent.windows_worker.routes import (
    WORKER_MESSAGES_PATH,
    WORKER_PACKAGE_PREFIX,
    WORKER_POLL_PATH,
    parse_worker_package_path,
)


_MAX_ENVELOPE_BYTES = 1024 * 1024


def _read_bounded(response: Any, limit: int) -> bytes:
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            declared = int(length)
        except (TypeError, ValueError) as exc:
            raise WorkerProtocolError("Worker response Content-Length is invalid") from exc
        if declared < 0 or declared > limit:
            raise WorkerProtocolError("Worker response exceeds its size limit")
    value = response.read(limit + 1)
    if len(value) > limit:
        raise WorkerProtocolError("Worker response exceeds its size limit")
    return value


def _closed_json(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerProtocolError("Worker transport payload is not closed JSON") from exc
    if len(encoded) > _MAX_ENVELOPE_BYTES:
        raise WorkerProtocolError("Worker transport payload exceeds one MiB")
    return encoded


@dataclass(frozen=True)
class WorkerHttpsEndpoints:
    poll_path: str = WORKER_POLL_PATH
    result_path: str = WORKER_MESSAGES_PATH
    package_prefix: str = WORKER_PACKAGE_PREFIX

    def __post_init__(self) -> None:
        for value in (self.poll_path, self.result_path, self.package_prefix):
            if not value.startswith("/") or "//" in value or ".." in value:
                raise ValueError("Worker HTTPS endpoint path is unsafe")
        if self.package_prefix != WORKER_PACKAGE_PREFIX:
            raise ValueError("Worker package endpoint prefix is fixed")


class HttpsWorkerTransport(WorkerTransportPort):
    """Poll one server using an injected, certificate-validating TLS context.

    The context is expected to carry the device's client certificate.  This
    class deliberately has no bearer-token, plaintext HTTP or cross-origin
    redirect fallback.
    """

    def __init__(
        self,
        *,
        base_url: str,
        device_id: str,
        ssl_context: ssl.SSLContext,
        endpoints: WorkerHttpsEndpoints | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Windows Worker server must be one credential-free HTTPS origin")
        if ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname:
            raise ValueError("Windows Worker TLS context must verify certificates and hostnames")
        self._origin = f"https://{parsed.netloc}"
        base_path = parsed.path.rstrip("/")
        self._base_path = base_path
        self._device_id = str(device_id)
        if not self._device_id or len(self._device_id) > 128:
            raise ValueError("Windows Worker device_id is invalid")
        self._context = ssl_context
        self._endpoints = endpoints or WorkerHttpsEndpoints()
        self._timeout = max(5, min(int(timeout_seconds), 120))
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._context),
            _SameOriginRedirectHandler(self._origin),
        )

    def _url(self, path: str) -> str:
        return f"{self._origin}{self._base_path}{path}"

    def _request(
        self,
        *,
        path: str,
        method: str,
        body: bytes | None = None,
        accept: str = "application/json",
    ) -> Any:
        request = urllib.request.Request(
            self._url(path),
            data=body,
            method=method,
            headers={
                "Accept": accept,
                "Content-Type": "application/json",
                "X-Worker-Device-ID": self._device_id,
                "User-Agent": "boyi-windows-worker/1",
            },
        )
        try:
            return self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WorkerProtocolError("Windows Worker HTTPS request failed") from exc

    def poll(self) -> Mapping[str, Any] | None:
        path = f"{self._endpoints.poll_path}?device_id={urllib.parse.quote(self._device_id, safe='')}"
        try:
            response = self._request(path=path, method="GET")
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return None
            raise WorkerProtocolError(f"Windows Worker poll returned HTTP {exc.code}") from exc
        with response:
            if response.status == 204:
                return None
            if response.status != 200:
                raise WorkerProtocolError("Windows Worker poll returned an unexpected status")
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise WorkerProtocolError("Windows Worker poll response is not JSON")
            data = _read_bounded(response, _MAX_ENVELOPE_BYTES)
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerProtocolError("Windows Worker poll response is invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise WorkerProtocolError("Windows Worker poll envelope must be an object")
        return decoded

    def send(self, envelope: Mapping[str, Any]) -> None:
        payload = _closed_json(envelope)
        try:
            response = self._request(path=self._endpoints.result_path, method="POST", body=payload)
        except urllib.error.HTTPError as exc:
            # A duplicate response means the exact signed message reached the
            # server before the connection was lost.  The server must signal
            # that case explicitly; all other conflicts fail closed.
            if exc.code == 409 and exc.headers.get("X-Worker-Message-Status") == "already-accepted":
                return
            raise WorkerProtocolError(f"Windows Worker message returned HTTP {exc.code}") from exc
        with response:
            if response.status not in {200, 202, 204}:
                raise WorkerProtocolError("Windows Worker message returned an unexpected status")
            _read_bounded(response, 64 * 1024)

    def fetch_package(self, url: str, *, expected_sha256: str) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != url
        ):
            raise PluginPackageError("Worker package path must be a fixed same-origin relative path")
        if (
            len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
        ):
            raise PluginPackageError("Worker package SHA-256 is invalid")
        identity = parse_worker_package_path(parsed.path)
        if identity is None or identity[2] != expected_sha256:
            raise PluginPackageError("Worker package path identity is invalid")
        try:
            response = self._request(
                path=parsed.path,
                method="GET",
                accept="application/zip",
            )
        except urllib.error.HTTPError as exc:
            raise PluginPackageError(f"Worker package download returned HTTP {exc.code}") from exc
        with response:
            if response.status != 200:
                raise PluginPackageError("Worker package download returned an unexpected status")
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/zip", "application/octet-stream"}:
                raise PluginPackageError("Worker package response has an unsupported content type")
            try:
                payload = _read_bounded(response, MAX_ARCHIVE_BYTES)
            except WorkerProtocolError as exc:
                raise PluginPackageError("Worker package exceeds its size limit") from exc
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise PluginPackageError("Worker package digest does not match the signed command")
        return payload


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: str) -> None:
        super().__init__()
        self._origin = origin

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> urllib.request.Request | None:
        parsed = urllib.parse.urlsplit(new_url)
        if f"{parsed.scheme}://{parsed.netloc}" != self._origin:
            raise WorkerProtocolError("Windows Worker refused a cross-origin redirect")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)
