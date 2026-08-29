"""Rolling retention worker for terminal orchestration records."""

from __future__ import annotations

import asyncio
import logging
from typing import Any


logger = logging.getLogger("agent")

CONTROL_PLANE_RETENTION_DAYS = 30
CONTROL_PLANE_RETENTION_BATCH_SIZE = 500
CONTROL_PLANE_RETENTION_INTERVAL_SECONDS = 6 * 60 * 60
CONTROL_PLANE_RETENTION_BATCH_PAUSE_SECONDS = 0.05


class ControlPlaneRetentionWorker:
    """Periodically purge eligible rows without holding one large transaction."""

    def __init__(
        self,
        repository: Any,
        *,
        batch_size: int = CONTROL_PLANE_RETENTION_BATCH_SIZE,
        interval_seconds: float = CONTROL_PLANE_RETENTION_INTERVAL_SECONDS,
        batch_pause_seconds: float = CONTROL_PLANE_RETENTION_BATCH_PAUSE_SECONDS,
    ) -> None:
        self._repository = repository
        self._batch_size = max(1, min(int(batch_size), 1_000))
        self._interval_seconds = max(1.0, float(interval_seconds))
        self._batch_pause_seconds = max(0.0, float(batch_pause_seconds))
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="control-plane-retention",
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        await task
        self._task = None

    async def purge_available(self) -> dict[str, int]:
        totals = self._empty_counts()
        while not self._stop.is_set():
            batch = await asyncio.to_thread(self._purge_batch)
            for table, deleted in batch.items():
                totals[table] += int(deleted)
            if (
                int(batch.get("domain_events", 0)) < self._batch_size
                and int(batch.get("approval_requests", 0)) < self._batch_size
            ):
                break
            if self._batch_pause_seconds:
                await asyncio.sleep(self._batch_pause_seconds)
        return totals

    def _purge_batch(self) -> dict[str, int]:
        with self._repository.unit_of_work() as uow:
            result = uow.retention.purge_batch(
                retention_days=CONTROL_PLANE_RETENTION_DAYS,
                batch_size=self._batch_size,
            )
            uow.commit()
        return result

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                totals = await self.purge_available()
                if any(totals.values()):
                    logger.info(
                        "Control-plane retention completed days=%d deleted=%s",
                        CONTROL_PLANE_RETENTION_DAYS,
                        totals,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Control-plane retention cycle failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {
            "domain_events": 0,
            "outbox_events": 0,
            "event_consumptions": 0,
            "approval_requests": 0,
            "approval_decisions": 0,
        }
