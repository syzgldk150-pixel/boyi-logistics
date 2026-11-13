"""Application-root wiring of the customer share refresh.

The refresh itself (``agent.receipt_share_refresh``) has no outside effects of
its own; this module supplies them: the NAS account from the deployment's
share settings file, the platform photo reader and image fetcher that reuse
the ordinary read lifecycle, and the daily schedule. When the settings file is
absent the feature stays off and says so.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from agent.receipt_share_refresh import ReceiptShareRefresher
from agent.tms_runtime.receipts_query import fetch_receipt_image_async
from agent.tms_runtime.waybill_photos import query_waybill_photos_async
from shared.receipt_image_sources import fetch_public_receipt_image
from shared.receipt_nas_store import ReceiptNasStore
from shared.receipt_share_settings import SETTINGS_FILE_ENV, load_share_settings  # noqa: F401

logger = logging.getLogger("agent.receipt_share")

NAS_FOLDER = "/团队文件-物流共享资料/回单归档"
CHINA = ZoneInfo("Asia/Shanghai")
DAILY_RUNS = ((12, 0), (18, 0), (23, 0))
# While waybills are still waiting to be read, continue this soon instead of at the next daily run.
BACKLOG_INTERVAL_SECONDS = 600
PLATFORM_READ_TIMEOUT_SECONDS = 60


def nas_store_from_settings(settings: Mapping[str, str], *, role: str) -> ReceiptNasStore:
    from urllib.parse import quote

    base = str(settings.get("RECEIPT_NAS_URL") or "").rstrip("/")
    folder = str(settings.get("RECEIPT_NAS_FOLDER") or NAS_FOLDER)
    return ReceiptNasStore(
        base + "/".join(quote(part, safe="") for part in folder.split("/")),
        str(settings.get(f"RECEIPT_NAS_{role}_USER") or ""), str(settings.get(f"RECEIPT_NAS_{role}_PASS") or ""))


def next_daily_run(now: datetime) -> datetime:
    local = now.astimezone(CHINA)
    candidates = [local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day)
                  for day in (0, 1) for hour, minute in DAILY_RUNS]
    return min(candidate for candidate in candidates if candidate > local)


class ReceiptShareRuntime:
    """Runs refreshes one at a time: on the daily schedule and when the Console asks."""

    def __init__(self, *, connect: Callable[[], Any], settings: Mapping[str, str], read_lifecycle: Any,
                 loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lifecycle = read_lifecycle
        self._lock = asyncio.Lock()
        self._refresher = ReceiptShareRefresher(
            connect=connect, nas=nas_store_from_settings(settings, role="RW"),
            read_photos=self._read_photos, fetch_image=self._fetch_image)
        self._task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    # Called from the refresh worker thread; platform reads run on the event loop's read lifecycle.
    def _read_photos(self, platform: str, waybill_no: str) -> Mapping[str, Any]:
        future = asyncio.run_coroutine_threadsafe(
            query_waybill_photos_async({"platform": platform, "waybill_no": waybill_no},
                                       PLATFORM_READ_TIMEOUT_SECONDS, read_lifecycle=self._lifecycle), self._loop)
        return future.result(timeout=PLATFORM_READ_TIMEOUT_SECONDS + 30)

    def _fetch_image(self, platform: str, kind: str, url: str) -> bytes:
        fetched = fetch_public_receipt_image(url, timeout=60)
        if fetched is not None:
            return fetched[0]
        if platform == "yunda" and kind == "receipt":  # needs the Yunda login; only its INMS host is accepted there
            future = asyncio.run_coroutine_threadsafe(
                fetch_receipt_image_async({"source_url": url}, PLATFORM_READ_TIMEOUT_SECONDS,
                                          read_lifecycle=self._lifecycle), self._loop)
            return base64.b64decode(future.result(timeout=PLATFORM_READ_TIMEOUT_SECONDS + 30)["content_base64"])
        return b""  # not an allowed source: rejected as "not a picture" by the refresher

    async def refresh(self, share_id: int | None = None) -> list[Any]:
        async with self._lock:
            outcomes = await asyncio.to_thread(self._refresher.refresh, share_id=share_id)
        for outcome in outcomes:
            logger.info("Receipt share refreshed share=%s state=%s waybills=%s new_images=%s items=%s backlog=%s",
                        outcome.share_id, outcome.state, outcome.waybills, outcome.new_images, outcome.items,
                        outcome.backlog)
        return outcomes

    def request_refresh(self, share_id: int) -> None:
        """Start one share's refresh in the background (after the Console created or changed it)."""

        task = self._loop.create_task(self._guarded(share_id), name=f"receipt-share-refresh-{int(share_id)}")
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _guarded(self, share_id: int | None) -> list[Any]:
        try:
            return await self.refresh(share_id)
        except Exception as exc:  # noqa: BLE001 - reported per share by the refresher; this guards the loop
            logger.warning("Receipt share refresh run failed error=%s", type(exc).__name__)
            return []

    async def _schedule(self) -> None:
        while True:
            wait = (next_daily_run(datetime.now(CHINA)) - datetime.now(CHINA)).total_seconds()
            await asyncio.sleep(max(1.0, wait))
            outcomes = await self._guarded(None)
            continuations = 0
            # Catch up a backlog (a new link's history) without waiting for the next daily run.
            while any(outcome.backlog and outcome.state != "nas_unavailable" for outcome in outcomes) \
                    and continuations < 30:
                continuations += 1
                await asyncio.sleep(BACKLOG_INTERVAL_SECONDS)
                outcomes = await self._guarded(None)

    def start(self) -> None:
        self._task = self._loop.create_task(self._schedule(), name="receipt-share-schedule")

    async def stop(self) -> None:
        tasks = [task for task in (self._task, *self._pending) if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass


def build_receipt_share_runtime(*, connect: Callable[[], Any], read_lifecycle: Any,
                                loop: asyncio.AbstractEventLoop) -> ReceiptShareRuntime | None:
    settings = load_share_settings()
    if settings is None:
        logger.info("Receipt share refresh is off: no share settings file.")
        return None
    return ReceiptShareRuntime(connect=connect, settings=settings, read_lifecycle=read_lifecycle, loop=loop)
