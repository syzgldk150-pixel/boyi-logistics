"""Sign-for and receipt photo thumbnails of 寄件运单查询 rows.

The photo list is read live from the platform through Agent's ``waybill-photos``
reader. Images are fetched only through the receipt module's allow-listed
sources (Ronghui public downloads, Yunda sign-for images, Yunda receipt images
via Agent) and cached on disk by address, since a stored image never changes.
"""

from __future__ import annotations

import threading
import time

from console.app_support import *  # noqa: F403
from console.services.business_calls import call_business

WAYBILL_PHOTO_PLATFORMS = {"ronghui", "yunda"}
WAYBILL_PHOTO_LIST_TTL_SECONDS = 600
WAYBILL_PHOTO_NO = re.compile(r"[A-Za-z0-9-]{4,40}")
# Yunda receipt images need the platform login and are read by Agent, which
# accepts only its INMS host; nothing else is ever sent there.
YUNDA_RECEIPT_IMAGE_HOST = "kyinms.yunda56.com"
_photo_lists: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_photo_lists_lock = threading.Lock()


def _cached_photo_list(key: tuple[str, str]) -> dict[str, Any] | None:
    with _photo_lists_lock:
        entry = _photo_lists.get(key)
        if entry and time.monotonic() - entry[0] < WAYBILL_PHOTO_LIST_TTL_SECONDS:
            return entry[1]
        _photo_lists.pop(key, None)
        return None


def _remember_photo_list(key: tuple[str, str], data: dict[str, Any]) -> None:
    with _photo_lists_lock:
        if len(_photo_lists) > 2000:
            _photo_lists.clear()
        _photo_lists[key] = (time.monotonic(), data)


def _image_link(platform: str, source_url: str) -> str:
    return "/waybills/photo-image?" + urlencode({"platform": platform, "url": source_url})


class WaybillPhotosServiceMixin:
    def _waybill_photo_params(self, query: dict[str, list[str]]) -> tuple[str, str] | None:
        platform = str((query or {}).get("platform", [""])[0] or "").strip()
        waybill_no = str((query or {}).get("waybill_no", [""])[0] or "").strip()
        if platform not in WAYBILL_PHOTO_PLATFORMS or not WAYBILL_PHOTO_NO.fullmatch(waybill_no):
            return None
        return platform, waybill_no

    def _handle_waybill_photos(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        params = self._waybill_photo_params(query)
        if params is None:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "平台或运单号无效。"})
            return
        cached = _cached_photo_list(params)
        if cached is not None:
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": cached})
            return
        context = self._receipt_attachment_context()
        if context is None:
            self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "message": "需要真实管理员会话。"})
            return
        platform, waybill_no = params
        result = call_business(self, "waybill-photos", {"platform": platform, "waybill_no": waybill_no},
                               trusted_context=context, timeout_sec=60)
        data = result.get("data") if result.get("ok") else None
        if not isinstance(data, dict) or data.get("waybill_no") != waybill_no:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {
                "ok": False, "message": str(result.get("error") or "签收图片读取失败，请稍后重试。")})
            return
        payload = {
            "sign": [_image_link(platform, url) for url in data.get("sign_photos") or [] if isinstance(url, str)],
            "receipt": [_image_link(platform, url) for url in data.get("receipt_photos") or [] if isinstance(url, str)],
            "receipt_error": str(data.get("receipt_error") or ""),
        }
        if not payload["receipt_error"]:
            _remember_photo_list(params, payload)
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": payload})

    def _handle_waybill_photo_image(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        platform = str((query or {}).get("platform", [""])[0] or "").strip()
        source_url = str((query or {}).get("url", [""])[0] or "").strip()
        parsed = urlparse(source_url)
        yunda_host = parsed.scheme == "https" and parsed.hostname in {YUNDA_SIGN_PHOTO_HOST, YUNDA_RECEIPT_IMAGE_HOST}
        if (platform not in WAYBILL_PHOTO_PLATFORMS or not source_url or len(source_url) > 2048
                or (platform == "yunda" and not yunda_host)):
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "图片地址无效。"})
            return
        cache_dir = (self.settings.runtime_dir / "waybill_photos").resolve()
        target = cache_dir / hashlib.sha256(f"{platform}\n{source_url}".encode("utf-8")).hexdigest()
        payload = b""
        if target.is_file():
            payload = target.read_bytes()
        content_type = self._receipt_image_mime_type(payload)
        if not content_type:
            fetched = self._fetch_receipt_attachment_source(
                {"platform": platform}, source_url, trusted_context=self._receipt_attachment_context())
            payload = fetched[0] if fetched else b""
            content_type = self._receipt_image_mime_type(payload)
            if not content_type:
                self._send_json(handler, HTTPStatus.NOT_FOUND, {"ok": False, "message": "图片读取失败。"})
                return
            cache_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        self._send_bytes(handler, HTTPStatus.OK, payload, content_type, cache_control="private, max-age=86400",
                         extra_headers={"X-Content-Type-Options": "nosniff",
                                        "Content-Security-Policy": "sandbox; default-src 'none'"})
