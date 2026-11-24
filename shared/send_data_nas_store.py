"""WebDAV folder on the NAS that holds the daily send-data workbooks.

The NAS share mapped as ``Z:`` on office PCs is reached from the ECS over
Tailscale through the NAS WebDAV service. The caller passes the folder URL and
account; this module reads no environment. Only ``<子文件夹>/<文件名>`` paths of
plain names are addressable, and a workbook replaces the previous one.
"""
from __future__ import annotations

import base64
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

MAX_WORKBOOK_BYTES = 30 * 1024 * 1024
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class SendDataNasError(RuntimeError):
    """``code`` says why; the message is safe to show and holds no address or account."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):  # credentials never follow a redirect
        return None


def _segment(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128 or value != value.strip()
            or value.startswith(".") or _FORBIDDEN.search(value)):
        raise SendDataNasError("NAS_PATH_INVALID", "NAS 文件路径无效。")
    return value


class SendDataNasStore:
    def __init__(self, folder_url: str, username: str, password: str, *, timeout: float = 30.0) -> None:
        parts = urlsplit(str(folder_url or ""))
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment:
            raise SendDataNasError("NAS_CONFIG_INVALID", "寄件数据的 NAS 地址未正确配置。")
        if not username or not password:
            raise SendDataNasError("NAS_CONFIG_INVALID", "寄件数据的 NAS 账号未配置。")
        # Percent-encode the folder path once (already-encoded "%XX" stays as is).
        self._base = parts._replace(path=quote(parts.path, safe="/%")).geturl().rstrip("/")
        self._authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._timeout = float(timeout)
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _url(self, *segments: str) -> str:
        return self._base + "/" + "/".join(quote(_segment(part), safe="") for part in segments)

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
                raise SendDataNasError("NAS_AUTH_FAILED", "NAS 拒绝了寄件数据账号，请检查 WebDAV 账号和“寄件数据”文件夹的读写权限。") from None
            if exc.code == 404:
                raise SendDataNasError("NAS_NOT_FOUND", "NAS 上没有这个文件或文件夹。") from None
            if exc.code == 423:
                raise SendDataNasError("NAS_FILE_LOCKED", "NAS 上的表格正被占用（可能有人用 Excel 打开着），请关闭后重新拉取。") from None
            if exc.code == 507:
                raise SendDataNasError("NAS_FULL", "NAS 空间不足。") from None
            if exc.code in {405, 409, 412}:
                raise SendDataNasError(f"NAS_HTTP_{exc.code}", "NAS 未接受这次操作。") from None
            raise SendDataNasError("NAS_UNAVAILABLE", f"NAS 返回了错误（{exc.code}）。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SendDataNasError("NAS_UNAVAILABLE", "暂时连不上 NAS。") from None

    def read_optional(self, folder: str, name: str) -> bytes | None:
        """The file's bytes, or ``None`` when it does not exist yet."""

        try:
            response = self._request("GET", self._url(folder, name))
        except SendDataNasError as exc:
            if exc.code == "NAS_NOT_FOUND":
                return None
            raise
        with response:
            content = response.read(MAX_WORKBOOK_BYTES + 1)
        if len(content) > MAX_WORKBOOK_BYTES:
            raise SendDataNasError("NAS_CONTENT_INVALID", "NAS 上的表格超过 30MB。")
        return content

    def write(self, folder: str, name: str, content: bytes) -> None:
        if not isinstance(content, (bytes, bytearray)) or not content or len(content) > MAX_WORKBOOK_BYTES:
            raise SendDataNasError("NAS_CONTENT_INVALID", "表格为空或超过 30MB。")
        try:
            self._request("MKCOL", self._url(folder)).close()
        except SendDataNasError as exc:
            if exc.code != "NAS_HTTP_405":  # 405: the folder already exists
                raise
        self._request("PUT", self._url(folder, name), data=bytes(content), headers={
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}).close()


__all__ = ["SendDataNasError", "SendDataNasStore"]
