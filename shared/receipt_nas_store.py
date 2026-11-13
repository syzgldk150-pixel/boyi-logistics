"""WebDAV archive of shared images on the NAS, reached over Tailscale.

Only the standard library is used so the Agent job (read-write account) and
the public share service (read-only account) can both import it. The caller
passes the address and account; this module reads no environment. Paths are
the relative ones made by ``shared.receipt_share.nas_image_path`` and are
re-checked here: nothing outside the configured folder can be addressed.
Files are written once and never overwritten.
"""
from __future__ import annotations

import base64
import re
import urllib.error
import urllib.request
from typing import Iterator
from urllib.parse import quote, urlsplit

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class NasStoreError(RuntimeError):
    """The NAS could not be read or written; ``code`` says why without any address or account."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):  # credentials never follow a redirect
        return None


class ReceiptNasStore:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 20.0) -> None:
        parts = urlsplit(str(base_url or ""))
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment:
            raise NasStoreError("NAS_CONFIG_INVALID", "NAS 地址未正确配置。")
        if not username or not password:
            raise NasStoreError("NAS_CONFIG_INVALID", "NAS 账号未配置。")
        self._base = str(base_url).rstrip("/")
        self._authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._timeout = float(timeout)
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _url(self, relative_path: str) -> str:
        segments = str(relative_path or "").split("/")
        if not segments or any(not _SEGMENT.fullmatch(part) or ".." in part for part in segments):
            raise NasStoreError("NAS_PATH_INVALID", "图片路径无效。")
        return self._base + "/" + "/".join(quote(part, safe="") for part in segments)

    def _request(self, method: str, url: str, *, data: bytes | None = None, headers: dict | None = None):
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", self._authorization)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            return self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in {401, 403}:
                raise NasStoreError("NAS_AUTH_FAILED", "NAS 拒绝了账号，请检查 WebDAV 账号与文件夹权限。") from None
            if exc.code == 404:
                raise NasStoreError("NAS_NOT_FOUND", "NAS 上没有这个文件。") from None
            if exc.code in {405, 409, 412}:
                raise NasStoreError(f"NAS_HTTP_{exc.code}", "NAS 未接受这次操作。") from None
            raise NasStoreError("NAS_UNAVAILABLE", f"NAS 返回了错误（{exc.code}）。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise NasStoreError("NAS_UNAVAILABLE", "暂时连不上 NAS。") from None

    def exists(self, relative_path: str) -> bool:
        try:
            self._request("HEAD", self._url(relative_path)).close()
            return True
        except NasStoreError as exc:
            if exc.code == "NAS_NOT_FOUND":
                return False
            raise

    def _ensure_folders(self, relative_path: str) -> None:
        segments = relative_path.split("/")[:-1]
        for depth in range(1, len(segments) + 1):
            try:
                self._request("MKCOL", self._url("/".join(segments[:depth]))).close()
            except NasStoreError as exc:
                if exc.code != "NAS_HTTP_405":  # 405: the folder already exists
                    raise

    def put_if_absent(self, relative_path: str, content: bytes) -> bool:
        """Store ``content`` unless the path exists. Returns whether it was written."""

        if not isinstance(content, (bytes, bytearray)) or not content or len(content) > MAX_IMAGE_BYTES:
            raise NasStoreError("NAS_CONTENT_INVALID", "图片为空或超过 20MB。")
        url = self._url(relative_path)
        if self.exists(relative_path):
            return False
        self._ensure_folders(relative_path)
        try:
            self._request("PUT", url, data=bytes(content),
                          headers={"If-None-Match": "*", "Content-Type": "application/octet-stream"}).close()
        except NasStoreError as exc:
            if exc.code == "NAS_HTTP_412":  # written by a concurrent refresh
                return False
            raise
        return True

    def read(self, relative_path: str) -> bytes:
        with self._request("GET", self._url(relative_path)) as response:
            content = response.read(MAX_IMAGE_BYTES + 1)
        if len(content) > MAX_IMAGE_BYTES:
            raise NasStoreError("NAS_CONTENT_INVALID", "图片超过 20MB。")
        return content

    def stream(self, relative_path: str, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        response = self._request("GET", self._url(relative_path))

        def chunks() -> Iterator[bytes]:
            try:
                while True:
                    block = response.read(chunk_size)
                    if not block:
                        return
                    yield block
            finally:
                response.close()

        return chunks()
