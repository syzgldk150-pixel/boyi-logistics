"""QR binding and revocation of WeChat bot devices for the Console.

A super administrator picks the identity, label and function limits, the
Agent asks iLink for a QR code, and the person scans it with their own WeChat.
Only a confirmed scan creates a device row; the bot token goes to the private
token store. Login sessions live in this process only (a restart abandons
them; the administrator simply generates a new QR code).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent.wechat_bot.ilink import ILinkClient, ILinkError, safe_base_url, DEFAULT_BASE_URL
from agent.wechat_bot.tokens import WeChatTokenStore
from shared.identity_repository import IdentityRepository
from shared.wechat_bot import validate_device_permissions, validate_label
from shared.wechat_bot_repository import WeChatDeviceConflict, WeChatDeviceRepository

logger = logging.getLogger("agent.wechat_bot")

LOGIN_TTL_SECONDS = 8 * 60
MAX_ACTIVE_LOGINS = 5
MAX_STATUS_FAILURES = 3

PUBLIC_STATUS = {
    "wait": "请用要绑定的微信扫描二维码",
    "scaned": "已扫码，请在手机微信上确认",
    "need_verifycode": "请输入对方手机微信上显示的数字",
    "confirmed": "绑定成功",
    "failed": "绑定未完成",
}


class WeChatBotError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def qr_image(content: str) -> str:
    """A data URI the Console can show directly."""
    if content.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
        return content
    if not content.startswith("https://"):
        raise WeChatBotError("WECHAT_QR_UNSUPPORTED", "iLink 返回的二维码内容无法识别")
    import segno

    return segno.make(content, error="m").svg_data_uri(scale=6, border=2)


@dataclass
class LoginSession:
    login_id: str
    admin_id: int
    person_id: str
    label: str
    permissions: tuple[str, ...] | None
    qrcode: str
    qr_image: str
    qr_link: str
    started_at: float = field(default_factory=time.monotonic)
    base_url: str = DEFAULT_BASE_URL
    status: str = "wait"
    message: str = PUBLIC_STATUS["wait"]
    device_id: str | None = None
    verify_code: str | None = None
    task: asyncio.Task | None = None

    def public(self) -> dict[str, Any]:
        remaining = max(0, int(LOGIN_TTL_SECONDS - (time.monotonic() - self.started_at)))
        finished = self.status in {"confirmed", "failed"}
        return {
            "login_id": self.login_id, "status": self.status, "message": self.message,
            "device_id": self.device_id, "expires_in_seconds": 0 if finished else remaining,
            "qr_image": None if finished else self.qr_image, "qr_link": None if finished else self.qr_link,
            "label": self.label,
        }


class WeChatBotService:
    def __init__(self, *, devices: WeChatDeviceRepository, identities: IdentityRepository, client: ILinkClient,
                 tokens: WeChatTokenStore,
                 on_bound: Callable[[str], Awaitable[None]] | None = None,
                 on_revoked: Callable[[str], Awaitable[None]] | None = None):
        self._devices = devices
        self._identities = identities
        self._client = client
        self._tokens = tokens
        self._on_bound = on_bound
        self._on_revoked = on_revoked
        self._logins: dict[str, LoginSession] = {}

    def bind_channel(self, *, on_bound, on_revoked) -> None:
        self._on_bound, self._on_revoked = on_bound, on_revoked

    def _purge(self) -> None:
        for login_id, session in list(self._logins.items()):
            if time.monotonic() - session.started_at > LOGIN_TTL_SECONDS + 600:
                if session.task is not None and not session.task.done():
                    session.task.cancel()
                self._logins.pop(login_id, None)

    def _check_person(self, person_id) -> None:
        with self._identities.cursor() as cursor:
            IdentityRepository.check_person(cursor, person_id)

    async def start_login(self, admin_id: int, *, person_id: str | None = None,
                          label: str = "", permissions=None) -> dict[str, Any]:
        try:
            label = validate_label(label)
            allowed = validate_device_permissions(permissions)
        except ValueError as exc:
            raise WeChatBotError("INVALID_INPUT", str(exc)) from exc
        if not isinstance(person_id, str) or not person_id.strip():
            raise WeChatBotError("INVALID_INPUT", "请选择人员")
        person_id = person_id.strip()
        try:
            await asyncio.to_thread(self._check_person, person_id)
        except ValueError as exc:
            raise WeChatBotError("INVALID_INPUT", "所选人员不存在或已停用") from exc
        self._purge()
        if sum(1 for s in self._logins.values() if s.status not in {"confirmed", "failed"}) >= MAX_ACTIVE_LOGINS:
            raise WeChatBotError("WECHAT_LOGIN_BUSY", "同时进行中的扫码绑定过多，请稍后再试")
        active = await asyncio.to_thread(self._devices.active_devices)
        try:
            qr = await self._client.get_bot_qrcode(self._tokens.tokens([row["device_id"] for row in active]))
        except ILinkError as exc:
            raise WeChatBotError(exc.code, f"获取微信二维码失败：{exc}") from exc
        session = LoginSession(
            login_id=str(uuid.uuid4()), admin_id=int(admin_id), person_id=person_id,
            label=label, permissions=allowed, qrcode=qr.qrcode, qr_image=qr_image(qr.content),
            qr_link=qr.content if qr.content.startswith("https://") else "",
        )
        self._logins[session.login_id] = session
        session.task = asyncio.create_task(self._watch(session), name=f"wechat-login-{session.login_id}")
        return session.public()

    def _session(self, login_id: str, admin_id: int) -> LoginSession:
        session = self._logins.get(str(login_id))
        if session is None or session.admin_id != int(admin_id):
            raise WeChatBotError("WECHAT_LOGIN_NOT_FOUND", "扫码绑定已结束或不存在，请重新生成二维码")
        return session

    def login_status(self, login_id: str, admin_id: int) -> dict[str, Any]:
        return self._session(login_id, admin_id).public()

    def submit_verify_code(self, login_id: str, admin_id: int, code: str) -> dict[str, Any]:
        session = self._session(login_id, admin_id)
        if session.status != "need_verifycode":
            raise WeChatBotError("WECHAT_VERIFY_NOT_REQUIRED", "当前不需要输入数字")
        if not isinstance(code, str) or not code.strip().isdigit() or len(code.strip()) > 12:
            raise WeChatBotError("INVALID_INPUT", "请输入手机微信上显示的数字")
        session.verify_code = code.strip()
        session.message = "正在核对数字"
        return session.public()

    def cancel_login(self, login_id: str, admin_id: int) -> None:
        session = self._session(login_id, admin_id)
        if session.task is not None and not session.task.done():
            session.task.cancel()
        self._finish(session, "failed", "已取消扫码绑定")

    @staticmethod
    def _finish(session: LoginSession, status: str, message: str) -> None:
        session.status, session.message, session.verify_code = status, message, None

    async def _watch(self, session: LoginSession) -> None:
        failures = 0
        try:
            while time.monotonic() - session.started_at < LOGIN_TTL_SECONDS:
                code = session.verify_code
                try:
                    status = await self._client.get_qrcode_status(session.base_url, session.qrcode, code)
                    failures = 0
                except ILinkError as exc:
                    failures += 1
                    logger.warning("WeChat QR status failed code=%s attempt=%d", exc.code, failures)
                    if failures >= MAX_STATUS_FAILURES:
                        self._finish(session, "failed", "查询扫码状态连续失败，请重新生成二维码")
                        return
                    await asyncio.sleep(2)
                    continue
                if code and session.verify_code == code:
                    session.verify_code = None  # Each code is sent once; a wrong one is asked for again.
                kind = status.status
                if kind == "confirmed":
                    await self._bind(session, status)
                    return
                if kind == "expired":
                    self._finish(session, "failed", "二维码已过期，请重新生成")
                    return
                if kind == "verify_code_blocked":
                    self._finish(session, "failed", "数字输错次数过多，请稍后重新生成二维码")
                    return
                if kind == "binded_redirect":
                    self._finish(session, "failed", "该微信已连接过本系统，请先在设备列表解绑原设备再扫码")
                    return
                if kind == "scaned_but_redirect":
                    if status.redirect_host:
                        session.base_url = safe_base_url(status.redirect_host)
                    session.status, session.message = "scaned", PUBLIC_STATUS["scaned"]
                elif kind == "need_verifycode":
                    if session.status == "need_verifycode" and code:
                        session.message = "数字不匹配，请重新输入"
                    else:
                        session.message = PUBLIC_STATUS["need_verifycode"]
                    session.status = "need_verifycode"
                    while session.verify_code is None and time.monotonic() - session.started_at < LOGIN_TTL_SECONDS:
                        await asyncio.sleep(1)
                    continue
                elif kind in {"wait", "scaned"}:
                    session.status, session.message = kind, PUBLIC_STATUS[kind]
                else:
                    self._finish(session, "failed", "微信返回了未知的扫码状态，请重新生成二维码")
                    return
                await asyncio.sleep(1)
            self._finish(session, "failed", "二维码已过期，请重新生成")
        except asyncio.CancelledError:
            if session.status not in {"confirmed", "failed"}:
                self._finish(session, "failed", "扫码绑定已停止")
            raise
        except ILinkError as exc:
            logger.warning("WeChat QR login rejected code=%s", exc.code)
            self._finish(session, "failed", str(exc))
        except Exception:
            logger.exception("WeChat QR login failed")
            self._finish(session, "failed", "绑定失败，请重新生成二维码")

    async def _bind(self, session: LoginSession, status) -> None:
        if not status.ilink_bot_id or not status.bot_token or not status.ilink_user_id:
            self._finish(session, "failed", "微信确认了扫码，但没有返回完整的机器人信息，请重新生成二维码")
            return
        base_url = safe_base_url(status.base_url)
        device_id = str(uuid.uuid4())
        self._tokens.save(device_id, token=status.bot_token, base_url=base_url,
                          ilink_bot_id=status.ilink_bot_id, owner_user_id=status.ilink_user_id)
        try:
            await asyncio.to_thread(
                self._devices.create_device, device_id=device_id, ilink_bot_id=status.ilink_bot_id,
                owner_user_id=status.ilink_user_id, label=session.label, person_id=session.person_id,
                permissions=session.permissions, created_by=session.admin_id)
        except WeChatDeviceConflict as exc:
            self._tokens.delete(device_id)
            self._finish(session, "failed", str(exc))
            return
        except ValueError:
            self._tokens.delete(device_id)
            self._finish(session, "failed", "所选身份或后台账号已失效，请重新生成二维码")
            return
        except Exception:
            self._tokens.delete(device_id)
            raise
        session.device_id = device_id
        self._finish(session, "confirmed", PUBLIC_STATUS["confirmed"])
        logger.info("WeChat device bound device_id=%s", device_id)
        if self._on_bound is not None:
            await self._on_bound(device_id)

    async def revoke(self, device_id: str) -> bool:
        revoked = await asyncio.to_thread(self._devices.revoke, str(device_id))
        if self._on_revoked is not None:
            await self._on_revoked(str(device_id))
        else:
            self._tokens.delete(str(device_id))
        return revoked

    async def close(self) -> None:
        for session in self._logins.values():
            if session.task is not None and not session.task.done():
                session.task.cancel()
        self._logins.clear()
