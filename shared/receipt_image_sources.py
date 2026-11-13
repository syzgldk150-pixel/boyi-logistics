"""The login-free platform image addresses that may be fetched, and the one fetcher.

Only these sources are ever requested without a platform session: Ronghui's
object storage and its ``/unauth/download/`` links, and Yunda sign-for photos.
The Console receipt pages and the Agent share refresh both use this module, so
the allow-list exists once.
"""
from __future__ import annotations

import ssl
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

RONGHUI_DIRECT_ATTACHMENT_HOSTS = {"rhk13.obs.cn-east-3.myhuaweicloud.com"}
# Since 2026-11 Ronghui returns receipt images as login-free download links on
# its TMS host; only this path prefix is public, so only it is fetched.
RONGHUI_PUBLIC_DOWNLOAD_HOST = "tms.ronghuiwl.com"
RONGHUI_PUBLIC_DOWNLOAD_PREFIX = "/unauth/download/"
# Yunda sign-for photos (tracking `monitoring.signedPic`) are login-free under this path.
YUNDA_SIGN_PHOTO_HOST = "kyscan.yunda56.com"
YUNDA_SIGN_PHOTO_PREFIX = "/ws/"
RECEIPT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024
RECEIPT_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


def image_mime_type(payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG"):
        return "image/png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return ""


def fetch_public_receipt_image(source_url: str, *, opener: Callable = urlopen,
                               timeout: float = 180) -> tuple[bytes, str] | None:
    """Fetch an allow-listed login-free image; ``None`` for any other address or a non-image answer."""

    parsed = urlparse(source_url)
    host = str(parsed.hostname or "").strip().lower()
    public_download = host == RONGHUI_PUBLIC_DOWNLOAD_HOST and (parsed.path or "").startswith(
        RONGHUI_PUBLIC_DOWNLOAD_PREFIX)
    yunda_sign_photo = host == YUNDA_SIGN_PHOTO_HOST and (parsed.path or "").startswith(YUNDA_SIGN_PHOTO_PREFIX)
    if parsed.scheme.lower() != "https" or not (
            host in RONGHUI_DIRECT_ATTACHMENT_HOSTS or public_download or yunda_sign_photo):
        return None
    suffix = Path(parsed.path or "").suffix.lower()
    if suffix not in RECEIPT_IMAGE_SUFFIXES:
        return None
    request = Request(source_url, headers={"Accept": "image/*,*/*", "User-Agent": "Mozilla/5.0"})
    context = None
    if public_download:
        # The Ronghui TMS host only negotiates legacy ciphers (as in the
        # Agent's Ronghui session); the certificate is still verified.
        context = ssl.create_default_context()
        context.set_ciphers("DEFAULT:@SECLEVEL=1")
    try:
        with opener(request, timeout=timeout, context=context) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            if status_code < 200 or status_code >= 300:
                return None
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type and not content_type.startswith("image/"):
                return None
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > RECEIPT_ATTACHMENT_MAX_BYTES:
                    return None
                chunks.append(chunk)
    except (HTTPError, URLError, OSError, TimeoutError, ValueError):
        return None
    payload = b"".join(chunks)
    mime_type = image_mime_type(payload)
    return (payload, mime_type) if mime_type else None
