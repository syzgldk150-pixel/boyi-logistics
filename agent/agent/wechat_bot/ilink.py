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

from shared.wechat_bot import ITEM_TEXT, MESSAGE_STATE_FINISH, MESSAGE_TYPE_BOT

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

    async def notify(self, base_url: str, token: str, *, start: bool) -> None:
        endpoint = "ilink/bot/msg/notifystart" if start else "ilink/bot/msg/notifystop"
        try:
            await self._post(base_url, endpoint, {"base_info": _base_info()}, token=token,
                             timeout=LIGHT_TIMEOUT_SECONDS, label="通知连接状态")
        except (ILinkError, httpx.TimeoutException):
            logger.info("iLink %s ignored", "notifystart" if start else "notifystop")
