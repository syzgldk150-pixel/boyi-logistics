"""Long-poll every bound WeChat device and route its text into the chat pipeline.

Only one Agent process consumes WeChat messages: the runtime holds the MySQL
named lock ``logistics_agent_wechat_consumer`` on a dedicated connection and
polls nothing without it. Each device accepts messages only from the WeChat
account that scanned its QR code. Text and WeChat's own voice transcription
are delivered as ``wechat:<device_id>`` with the stable event id
``wechat:<device_id>:<message_id>``; the delivered handler re-checks the
device's live identity for every message, so a revoked device or a changed
identity takes effect at once.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from agent.wechat_bot.ilink import LONG_POLL_TIMEOUT_SECONDS, STALE_TOKEN_ERRCODE, ILinkClient, ILinkError
from agent.wechat_bot.tokens import WeChatTokenStore
from shared.wechat_bot import (
    MESSAGE_TYPE_USER,
    has_unsupported_media,
    inbound_text,
    parse_wechat_actor_id,
    split_text,
    wechat_actor_id,
)
from shared.wechat_bot_repository import WeChatDeviceRepository

logger = logging.getLogger("agent.wechat_bot")

LEASE_NAME = "logistics_agent_wechat_consumer"
RECONCILE_INTERVAL_SECONDS = 30.0
RETRY_DELAY_SECONDS = 2.0
BACKOFF_DELAY_SECONDS = 30.0
MAX_CONSECUTIVE_FAILURES = 3
STALE_PAUSE_SECONDS = 3600.0
ORPHAN_TOKEN_GRACE_SECONDS = 600.0
SEEN_MESSAGES_PER_DEVICE = 200
UNSUPPORTED_MEDIA_REPLY = "目前只支持文字和语音消息，请用文字描述要办理的事情。"

Deliver = Callable[[str, str, str], Awaitable[None]]  # (actor_id, text, event_id)


class WeChatConsumerLease:
    """MySQL named lock held on one raw (never pooled) connection."""

    def __init__(self, connect: Callable[[], Any]):
        self._connect = connect
        self._connection = None

    def acquire(self) -> bool:
        if self._connection is not None:
            return True
        try:
            connection = self._connect()
        except Exception:
            logger.warning("WeChat consumer lease: database unavailable")
            return False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (LEASE_NAME,))
                row = cursor.fetchone()
            acquired = (row.get("acquired") if isinstance(row, dict) else row[0]) == 1
        except Exception:
            acquired = False
        if not acquired:
            connection.close()
            return False
        self._connection = connection
        return True

    def alive(self) -> bool:
        if self._connection is None:
            return False
        try:
            self._connection.ping(reconnect=False)
            return True
        except Exception:
            self._drop()
            return False

    def _drop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def release(self) -> None:
        if self._connection is None:
            return
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (LEASE_NAME,))
        except Exception:
            pass
        self._drop()


class WeChatChannelRuntime:
    def __init__(self, *, devices: WeChatDeviceRepository, tokens: WeChatTokenStore, client: ILinkClient,
                 lease: WeChatConsumerLease, deliver: Deliver):
        self._devices = devices
        self._tokens = tokens
        self._client = client
        self._lease = lease
        self._deliver = deliver
        self._pollers: dict[str, asyncio.Task] = {}
        self._states: dict[str, str] = {}
        self._contexts: dict[str, tuple[str, str]] = {}  # device_id -> (to_user_id, context_token)
        self._seen: dict[str, OrderedDict[str, None]] = {}
        self._handlers: set[asyncio.Task] = set()
        self._reconcile_task: asyncio.Task | None = None
        self._stopping = False

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._stopping = False
        self._reconcile_task = asyncio.create_task(self._reconcile_loop(), name="wechat-bot-reconcile")

    async def stop(self) -> None:
        self._stopping = True
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            await asyncio.gather(self._reconcile_task, return_exceptions=True)
        for device_id in list(self._pollers):
            await self._stop_poller(device_id, notify=True)
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await asyncio.to_thread(self._lease.release)

    async def _reconcile_loop(self) -> None:
        while not self._stopping:
            try:
                await self.reconcile()
            except Exception:
                logger.exception("WeChat device reconcile failed")
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)

    async def reconcile(self) -> None:
        held = await asyncio.to_thread(self._lease.alive) or await asyncio.to_thread(self._lease.acquire)
        if not held:
            for device_id in list(self._pollers):
                await self._stop_poller(device_id, notify=False)
            return
        rows = await asyncio.to_thread(self._devices.active_devices)
        active = {row["device_id"]: row for row in rows}
        for device_id in list(self._pollers):
            if device_id not in active:
                await self.device_revoked(device_id)
        for device_id in self._tokens.device_ids(older_than_seconds=ORPHAN_TOKEN_GRACE_SECONDS):
            if device_id not in active:
                # Revoked elsewhere; the token must not linger. The grace period
                # covers a binding whose token is saved just before its row commits.
                self._tokens.delete(device_id)
        for device_id, row in active.items():
            if device_id not in self._pollers or self._pollers[device_id].done():
                self._pollers[device_id] = asyncio.create_task(self._poll(dict(row)), name=f"wechat-poll-{device_id}")

    async def device_bound(self, device_id: str) -> None:
        await self.reconcile()

    async def device_revoked(self, device_id: str) -> None:
        await self._stop_poller(device_id, notify=True)
        self._contexts.pop(device_id, None)
        self._seen.pop(device_id, None)
        self._states.pop(device_id, None)
        self._tokens.delete(device_id)

    async def _stop_poller(self, device_id: str, *, notify: bool) -> None:
        task = self._pollers.pop(device_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if notify:
            credentials = self._tokens.load(device_id)
            if credentials:
                await self._client.notify(credentials["base_url"], credentials["token"], start=False)

    async def _set_state(self, device_id: str, state: str, *, message_received: bool = False) -> None:
        if self._states.get(device_id) == state and not message_received:
            return
        self._states[device_id] = state
        try:
            await asyncio.to_thread(self._devices.record_connection, device_id, state, message_received=message_received)
        except Exception:
            logger.warning("WeChat device state not saved device_id=%s", device_id)

    # -- polling ---------------------------------------------------------
    async def _poll(self, row: dict) -> None:
        device_id = row["device_id"]
        credentials = self._tokens.load(device_id)
        if not credentials or credentials["ilink_bot_id"] != row["ilink_bot_id"]:
            logger.error("WeChat device has no matching bot token device_id=%s", device_id)
            await self._set_state(device_id, "session_expired")
            return
        base_url, token = credentials["base_url"], credentials["token"]
        await self._set_state(device_id, "connecting")
        await self._client.notify(base_url, token, start=True)
        buf = self._tokens.load_cursor(device_id)
        timeout = LONG_POLL_TIMEOUT_SECONDS
        failures = 0
        while not self._stopping:
            try:
                response = await self._client.get_updates(base_url, token, buf, timeout=timeout)
            except ILinkError as exc:
                failures += 1
                logger.warning("WeChat getupdates failed device_id=%s code=%s attempt=%d", device_id, exc.code, failures)
                await self._set_state(device_id, "error")
                await asyncio.sleep(BACKOFF_DELAY_SECONDS if failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                failures = 0 if failures >= MAX_CONSECUTIVE_FAILURES else failures
                continue
            suggested = response.get("longpolling_timeout_ms")
            if isinstance(suggested, int) and suggested > 0:
                timeout = min(max(suggested / 1000.0, 5.0), 120.0)
            ret, errcode = response.get("ret"), response.get("errcode")
            if ret not in (None, 0) or errcode not in (None, 0):
                if STALE_TOKEN_ERRCODE in (ret, errcode):
                    logger.error("WeChat bot session expired device_id=%s; pausing 60 minutes", device_id)
                    await self._set_state(device_id, "session_expired")
                    await asyncio.sleep(STALE_PAUSE_SECONDS)
                    continue
                failures += 1
                logger.warning("WeChat getupdates rejected device_id=%s ret=%s errcode=%s", device_id, ret, errcode)
                await self._set_state(device_id, "error")
                await asyncio.sleep(BACKOFF_DELAY_SECONDS if failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                failures = 0 if failures >= MAX_CONSECUTIVE_FAILURES else failures
                continue
            failures = 0
            await self._set_state(device_id, "online")
            new_buf = response.get("get_updates_buf")
            if isinstance(new_buf, str) and new_buf and new_buf != buf:
                buf = new_buf
                self._tokens.save_cursor(device_id, buf)
            for message in response.get("msgs") or ():
                if isinstance(message, dict):
                    await self._accept(row, message)

    async def _accept(self, row: dict, message: dict) -> None:
        device_id = row["device_id"]
        if message.get("message_type") != MESSAGE_TYPE_USER:
            return
        sender = message.get("from_user_id")
        if sender != row["owner_user_id"]:
            logger.warning("WeChat message from a non-owner ignored device_id=%s", device_id)
            return
        message_id = str(message.get("message_id") or "").strip()
        if not message_id or len(message_id) > 64:
            logger.warning("WeChat message without a stable id ignored device_id=%s", device_id)
            return
        seen = self._seen.setdefault(device_id, OrderedDict())
        if message_id in seen:
            return
        seen[message_id] = None
        while len(seen) > SEEN_MESSAGES_PER_DEVICE:
            seen.popitem(last=False)
        context_token = message.get("context_token")
        if isinstance(context_token, str) and context_token:
            self._contexts[device_id] = (sender, context_token)
        await self._set_state(device_id, "online", message_received=True)
        actor_id = wechat_actor_id(device_id)
        text = inbound_text(message)
        logger.info("wechat inbound | device=%s | kind=%s | chars=%d", device_id,
                    "text" if text else "media" if has_unsupported_media(message) else "other", len(text))
        if not text:
            if has_unsupported_media(message):
                await self.send(actor_id, UNSUPPORTED_MEDIA_REPLY)
            return
        task = asyncio.create_task(self._deliver(actor_id, text, f"{actor_id}:{message_id}"),
                                   name=f"wechat-message-{device_id}")
        self._handlers.add(task)
        task.add_done_callback(self._handler_done)

    def _handler_done(self, task: asyncio.Task) -> None:
        self._handlers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("WeChat message handling failed: %s", type(task.exception()).__name__)

    # -- replies ---------------------------------------------------------
    async def send(self, receive_id: str, text: str) -> None:
        device_id = parse_wechat_actor_id(receive_id)
        if device_id is None:
            logger.error("WeChat reply target is not a device")
            return
        credentials = self._tokens.load(device_id)
        context = self._contexts.get(device_id)
        if not credentials:
            logger.error("WeChat reply dropped: device has no token device_id=%s", device_id)
            return
        to_user = context[0] if context else credentials["owner_user_id"]
        context_token = context[1] if context else None
        for chunk in split_text(text):
            try:
                await self._client.send_text(credentials["base_url"], credentials["token"], to_user_id=to_user,
                                             text=chunk, context_token=context_token)
            except ILinkError as exc:
                logger.error("WeChat reply failed device_id=%s code=%s", device_id, exc.code)
                return
        logger.info("wechat outbound | device=%s | chars=%d", device_id, len(str(text or "")))
