"""HTTP client for the official WeChat iLink Bot API (ClawBot).

The wire format follows Tencent's published channel plugin
``@tencent-weixin/openclaw-weixin`` 2.4.10 (``src/api/api.ts``,
``src/api/types.ts``, ``src/auth/login-qr.ts``): JSON over HTTPS to
``https://ilinkai.weixin.qq.com``, ``AuthorizationType: ilink_bot_token`` with
``Authorization: Bearer <bot_token>``, a random ``X-WECHAT-UIN`` per request,
long-poll ``getupdates`` and ``sendmessage`` that echoes the user's
``context_token``. Tokens never appear in logs or exceptions.
"""
from __future__ import annotations

import base64
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import httpx

from shared.wechat_bot import ITEM_IMAGE, ITEM_TEXT, MESSAGE_STATE_FINISH, MESSAGE_TYPE_BOT

logger = logging.getLogger("agent.wechat_ilink")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
PROTOCOL_VERSION = "2.4.10"
ILINK_APP_ID = "bot"
BOT_AGENT = "BoyiLogistics/1.0"
LONG_POLL_TIMEOUT_SECONDS = 35.0
API_TIMEOUT_SECONDS = 15.0
LIGHT_TIMEOUT_SECONDS = 10.0
STALE_TOKEN_ERRCODE = -14
# Images (src/cdn/upload.ts, cdn-upload.ts, cdn-url.ts, messaging/send.ts of the same plugin):
# getuploadurl -> AES-128-ECB (PKCS7) upload to the CDN -> sendmessage with an image item.
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
UPLOAD_MEDIA_IMAGE = 1
MAX_IMAGE_BYTES = 10_000_000
CDN_UPLOAD_ATTEMPTS = 3


class ILinkError(RuntimeError):
    """A failed iLink call. The message never contains tokens or bodies."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _client_version(version: str) -> int:
    major, minor, patch = (int(part) for part in version.split("."))
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _common_headers() -> dict[str, str]:
    return {"iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": str(_client_version(PROTOCOL_VERSION))}


def _headers(token: str | None) -> dict[str, str]:
    uin = str(int.from_bytes(secrets.token_bytes(4), "big"))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode("utf-8")).decode("ascii"),
        **_common_headers(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _base_info() -> dict[str, str]:
    return {"channel_version": PROTOCOL_VERSION, "bot_agent": BOT_AGENT}


def safe_base_url(value: Any) -> str:
    """iLink may return a per-bot base URL; accept only HTTPS hosts under weixin.qq.com."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return DEFAULT_BASE_URL
    if not text.startswith("https://"):
        text = "https://" + text
    host = urlparse(text).hostname or ""
    if host != "weixin.qq.com" and not host.endswith(".weixin.qq.com"):
        raise ILinkError("ILINK_BASE_URL_REJECTED", "iLink 返回的服务地址不在微信域名内")
    return f"https://{host}"


@dataclass(frozen=True)
class QRCode:
    qrcode: str
    content: str


@dataclass(frozen=True)
class QRStatus:
    status: str
    bot_token: str = ""
    ilink_bot_id: str = ""
    ilink_user_id: str = ""
    base_url: str = ""
    redirect_host: str = ""


MAX_INBOUND_IMAGE_BYTES = 8_000_000


def _wechat_https(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "weixin.qq.com" or host.endswith(".weixin.qq.com"))


def image_aes_key(image_item: Mapping[str, Any]) -> bytes | None:
    """The 16-byte key of an inbound picture (src/media/media-download.ts, src/cdn/pic-decrypt.ts)."""
    hex_key = str(image_item.get("aeskey") or "")
    if hex_key:
        try:
            key = bytes.fromhex(hex_key)
        except ValueError:
            key = b""
        if len(key) != 16:
            raise ILinkError("ILINK_IMAGE_KEY_INVALID", "图片密钥格式无效")
        return key
    media = image_item.get("media") if isinstance(image_item.get("media"), Mapping) else {}
    encoded = str(media.get("aes_key") or "")
    if not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError:
        decoded = b""
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and all(chr(byte) in "0123456789abcdefABCDEF" for byte in decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    raise ILinkError("ILINK_IMAGE_KEY_INVALID", "图片密钥格式无效")


def image_download_url(image_item: Mapping[str, Any]) -> str:
    media = image_item.get("media") if isinstance(image_item.get("media"), Mapping) else {}
    full = str(media.get("full_url") or "").strip()
    if full:
        if not _wechat_https(full):
            raise ILinkError("ILINK_IMAGE_URL_INVALID", "图片地址不是微信的 HTTPS 地址")
        return full
    param = str(media.get("encrypt_query_param") or "")
    if not param:
        raise ILinkError("ILINK_IMAGE_URL_MISSING", "消息里没有图片地址")
    return f"{CDN_BASE_URL}/download?encrypted_query_param={quote(param, safe='')}"


def cdn_upload_url(ticket: Mapping[str, Any], filekey: str) -> str:
    """The CDN address from ``getuploadurl``: its full URL, or the plugin's documented fallback."""
    full = str(ticket.get("upload_full_url") or "").strip()
    if full:
        if not _wechat_https(full):
            raise ILinkError("ILINK_UPLOAD_URL_INVALID", "图片上传地址不是微信的 HTTPS 地址")
        return full
    param = str(ticket.get("upload_param") or "")
    if not param:
        raise ILinkError("ILINK_UPLOAD_URL_MISSING", "微信没有返回图片上传地址")
    return f"{CDN_BASE_URL}/upload?encrypted_query_param={quote(param, safe='')}&filekey={quote(filekey, safe='')}"


class ILinkClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, base_url: str, endpoint: str, body: Mapping[str, Any], *, token: str | None,
                    timeout: float, label: str) -> dict[str, Any]:
        try:
            response = await self._client.post(f"{base_url}/{endpoint}", json=dict(body),
                                               headers=_headers(token), timeout=timeout)
        except httpx.TimeoutException:
            raise
        except httpx.HTTPError as exc:
            raise ILinkError("ILINK_NETWORK_ERROR", f"{label} 网络请求失败（{type(exc).__name__}）") from None
        return self._decode(response, label)

    @staticmethod
    def _decode(response: httpx.Response, label: str) -> dict[str, Any]:
        if response.status_code != 200:
            raise ILinkError("ILINK_HTTP_ERROR", f"{label} 返回 HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise ILinkError("ILINK_BAD_RESPONSE", f"{label} 返回的内容不是 JSON") from None
        if not isinstance(data, dict):
            raise ILinkError("ILINK_BAD_RESPONSE", f"{label} 返回的内容格式无效")
        return data

    async def get_bot_qrcode(self, local_tokens: list[str]) -> QRCode:
        data = await self._post(DEFAULT_BASE_URL, f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
                                {"local_token_list": list(local_tokens)[:10]}, token=None,
                                timeout=API_TIMEOUT_SECONDS, label="获取二维码")
        qrcode, content = data.get("qrcode"), data.get("qrcode_img_content")
        if not isinstance(qrcode, str) or not qrcode or not isinstance(content, str) or not content:
            raise ILinkError("ILINK_BAD_RESPONSE", "获取二维码的返回缺少二维码内容")
        return QRCode(qrcode=qrcode, content=content)

    async def get_qrcode_status(self, base_url: str, qrcode: str, verify_code: str | None = None) -> QRStatus:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        try:
            response = await self._client.get(f"{base_url}/{endpoint}", headers=_common_headers(),
                                              timeout=LONG_POLL_TIMEOUT_SECONDS)
        except httpx.TimeoutException:
            return QRStatus(status="wait")  # Long-poll ended without news; ask again.
        except httpx.HTTPError as exc:
            raise ILinkError("ILINK_NETWORK_ERROR", f"查询扫码状态网络失败（{type(exc).__name__}）") from None
        data = self._decode(response, "查询扫码状态")
        status = data.get("status")
        if not isinstance(status, str) or not status:
            raise ILinkError("ILINK_BAD_RESPONSE", "扫码状态返回缺少 status")
        return QRStatus(
            status=status,
            bot_token=str(data.get("bot_token") or ""),
            ilink_bot_id=str(data.get("ilink_bot_id") or ""),
            ilink_user_id=str(data.get("ilink_user_id") or ""),
            base_url=str(data.get("baseurl") or ""),
            redirect_host=str(data.get("redirect_host") or ""),
        )

    async def get_updates(self, base_url: str, token: str, buf: str, *, timeout: float) -> dict[str, Any]:
        try:
            return await self._post(base_url, "ilink/bot/getupdates",
                                    {"get_updates_buf": buf, "base_info": _base_info()},
                                    token=token, timeout=timeout, label="接收消息")
        except httpx.TimeoutException:
            return {"ret": 0, "msgs": [], "get_updates_buf": buf}  # Normal long-poll end.

    async def send_text(self, base_url: str, token: str, *, to_user_id: str, text: str,
                        context_token: str | None) -> None:
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"boyi-{uuid.uuid4().hex}",
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        try:
            data = await self._post(base_url, "ilink/bot/sendmessage", {"msg": message, "base_info": _base_info()},
                                    token=token, timeout=API_TIMEOUT_SECONDS, label="发送消息")
        except httpx.TimeoutException:
            raise ILinkError("ILINK_SEND_TIMEOUT", "发送消息超时，消息可能未送达") from None
        ret = data.get("ret")
        if ret not in (None, 0):
            raise ILinkError("ILINK_SEND_FAILED", f"发送消息失败 ret={ret}")

    async def send_image(self, base_url: str, token: str, *, to_user_id: str, image: bytes,
                         context_token: str | None) -> None:
        """Upload one picture encrypted to the WeChat CDN, then send it as an image message."""
        import hashlib

        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad

        if not isinstance(image, bytes) or not 0 < len(image) <= MAX_IMAGE_BYTES:
            raise ILinkError("ILINK_IMAGE_INVALID", "图片为空或超过发送上限")
        filekey, aeskey = secrets.token_hex(16), secrets.token_bytes(16)
        ciphertext = AES.new(aeskey, AES.MODE_ECB).encrypt(pad(image, 16))
        try:
            ticket = await self._post(base_url, "ilink/bot/getuploadurl", {
                "filekey": filekey, "media_type": UPLOAD_MEDIA_IMAGE, "to_user_id": to_user_id,
                "rawsize": len(image), "rawfilemd5": hashlib.md5(image).hexdigest(),  # noqa: S324 - protocol field
                "filesize": len(ciphertext), "no_need_thumb": True, "aeskey": aeskey.hex(),
                "base_info": _base_info()}, token=token, timeout=API_TIMEOUT_SECONDS, label="申请图片上传")
        except httpx.TimeoutException:
            raise ILinkError("ILINK_UPLOAD_TIMEOUT", "申请图片上传超时") from None
        upload_url = cdn_upload_url(ticket, filekey)
        download_param = await self._upload_to_cdn(upload_url, ciphertext)
        message: dict[str, Any] = {
            "from_user_id": "", "to_user_id": to_user_id, "client_id": f"boyi-{uuid.uuid4().hex}",
            "message_type": MESSAGE_TYPE_BOT, "message_state": MESSAGE_STATE_FINISH,
            # The plugin base64-encodes the hex text of the key, not the raw key bytes.
            "item_list": [{"type": ITEM_IMAGE, "image_item": {
                "media": {"encrypt_query_param": download_param,
                          "aes_key": base64.b64encode(aeskey.hex().encode("ascii")).decode("ascii"), "encrypt_type": 1},
                "mid_size": len(ciphertext)}}],
        }
        if context_token:
            message["context_token"] = context_token
        try:
            data = await self._post(base_url, "ilink/bot/sendmessage", {"msg": message, "base_info": _base_info()},
                                    token=token, timeout=API_TIMEOUT_SECONDS, label="发送图片")
        except httpx.TimeoutException:
            raise ILinkError("ILINK_SEND_TIMEOUT", "发送图片超时，图片可能未送达") from None
        if data.get("ret") not in (None, 0):
            raise ILinkError("ILINK_SEND_FAILED", f"发送图片失败 ret={data.get('ret')}")

    async def download_image(self, image_item: Mapping[str, Any]) -> bytes:
        """Download one inbound picture from the CDN and decrypt it (AES-128-ECB) when it carries a key."""
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        url, key = image_download_url(image_item), image_aes_key(image_item)
        try:
            response = await self._client.get(url, timeout=API_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            raise ILinkError("ILINK_IMAGE_DOWNLOAD_FAILED", f"图片下载网络失败（{type(exc).__name__}）") from None
        if response.status_code != 200 or not response.content or len(response.content) > MAX_INBOUND_IMAGE_BYTES + 16:
            raise ILinkError("ILINK_IMAGE_DOWNLOAD_FAILED", f"图片下载失败 HTTP {response.status_code}")
        if key is None:
            return response.content
        try:
            return unpad(AES.new(key, AES.MODE_ECB).decrypt(response.content), 16)
        except ValueError:
            raise ILinkError("ILINK_IMAGE_DECRYPT_FAILED", "图片解密失败") from None

    async def _upload_to_cdn(self, url: str, ciphertext: bytes) -> str:
        for attempt in range(1, CDN_UPLOAD_ATTEMPTS + 1):
            try:
                response = await self._client.post(url, content=ciphertext, timeout=API_TIMEOUT_SECONDS,
                                                   headers={"Content-Type": "application/octet-stream"})
            except httpx.HTTPError as exc:
                if attempt == CDN_UPLOAD_ATTEMPTS:
                    raise ILinkError("ILINK_UPLOAD_FAILED", f"图片上传网络失败（{type(exc).__name__}）") from None
                continue
            if 400 <= response.status_code < 500:
                raise ILinkError("ILINK_UPLOAD_REJECTED", f"图片上传被拒绝 HTTP {response.status_code}")
            if response.status_code == 200:
                param = response.headers.get("x-encrypted-param")
                if not param:
                    raise ILinkError("ILINK_UPLOAD_FAILED", "图片上传后没有返回下载参数")
                return param
            if attempt == CDN_UPLOAD_ATTEMPTS:
                raise ILinkError("ILINK_UPLOAD_FAILED", f"图片上传失败 HTTP {response.status_code}")
        raise ILinkError("ILINK_UPLOAD_FAILED", "图片上传失败")

    async def notify(self, base_url: str, token: str, *, start: bool) -> None:
        endpoint = "ilink/bot/msg/notifystart" if start else "ilink/bot/msg/notifystop"
        try:
            await self._post(base_url, endpoint, {"base_info": _base_info()}, token=token,
                             timeout=LIGHT_TIMEOUT_SECONDS, label="通知连接状态")
        except (ILinkError, httpx.TimeoutException):
            logger.info("iLink %s ignored", "notifystart" if start else "notifystop")
