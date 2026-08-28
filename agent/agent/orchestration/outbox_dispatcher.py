"""Leased, at-least-once MySQL outbox dispatcher."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from shared.redaction import redact_text


logger = logging.getLogger("agent")
OutboxHandler = Callable[[Mapping[str, Any], Any], Mapping[str, Any] | None]


class OutboxRetryAfter(RuntimeError):
    """Ask the dispatcher to retry after a known external lease expires."""

    def __init__(self, message: str, *, delay_seconds: int) -> None:
        super().__init__(message)
        self.delay_seconds = max(1, int(delay_seconds))


class OutboxDispatcher:
    def __init__(
        self,
        repository: Any,
        *,
        worker_id: str,
        consumer_name: str | None = None,
        handlers: Mapping[str, OutboxHandler] | None = None,
        handler_uses_uow: bool = True,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = 60,
        batch_size: int = 50,
    ) -> None:
        self._repository = repository
        self._worker_id = worker_id
        self._consumer_name = str(consumer_name or "").strip() or None
        self._handlers = dict(handlers or {})
        self._handler_uses_uow = bool(handler_uses_uow)
        self._poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._lease_seconds = max(1, int(lease_seconds))
        self._batch_size = max(1, int(batch_size))
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def register_handler(self, consumer_name: str, handler: OutboxHandler) -> None:
        if self._task is not None:
            raise RuntimeError("outbox handlers cannot be changed while the dispatcher is running")
        self._handlers[str(consumer_name)] = handler

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"outbox:{self._worker_id}")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                claimed = await asyncio.to_thread(self._claim)
                for delivery in claimed:
                    if self._stop.is_set():
                        break
                    await asyncio.to_thread(self._deliver, delivery)
                if claimed:
                    continue
            except Exception:
                logger.exception("Outbox dispatcher iteration failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    def _claim(self) -> list[dict[str, Any]]:
        with self._repository.unit_of_work() as uow:
            rows = uow.outbox.claim(
                self._worker_id,
                limit=self._batch_size,
                lease_seconds=self._lease_seconds,
                consumer_name=self._consumer_name,
            )
            uow.commit()
        return rows

    def _deliver(self, delivery: Mapping[str, Any]) -> None:
        outbox_id = int(delivery["outbox_id"])
        consumer_name = str(delivery.get("consumer_name") or "")
        event_id = str(delivery.get("event_id") or "")
        handler = self._handlers.get(consumer_name)
        if handler is None:
            self._reschedule(
                outbox_id,
                error_code="OUTBOX_HANDLER_MISSING",
                error_summary=f"No handler is registered for {consumer_name}",
            )
            return
        try:
            if not self._handler_uses_uow:
                if self._mark_published_if_consumed(
                    outbox_id=outbox_id,
                    consumer_name=consumer_name,
                    event_id=event_id,
                ):
                    return
                result = handler(delivery, None)
                if inspect.isawaitable(result):
                    raise RuntimeError("Outbox handlers must be synchronous")
                self._record_delivery(
                    outbox_id=outbox_id,
                    consumer_name=consumer_name,
                    event_id=event_id,
                    result=result,
                )
                return
            with self._repository.unit_of_work() as uow:
                if uow.outbox.was_consumed(consumer_name=consumer_name, event_id=event_id):
                    uow.outbox.mark_published(outbox_id, worker_id=self._worker_id)
                    uow.commit()
                    return
                result = handler(delivery, uow)
                if inspect.isawaitable(result):
                    raise RuntimeError("Outbox handlers must be synchronous to preserve transaction boundaries")
                uow.outbox.record_consumption(
                    consumer_name=consumer_name,
                    event_id=event_id,
                    result_summary=dict(result or {}),
                )
                uow.outbox.mark_published(outbox_id, worker_id=self._worker_id)
                uow.commit()
        except OutboxRetryAfter as exc:
            self._reschedule(
                outbox_id,
                error_code="OUTBOX_DEPENDENCY_LEASED",
                error_summary=redact_text(exc)[:500],
                delay_seconds=exc.delay_seconds,
            )
        except Exception as exc:
            self._reschedule(
                outbox_id,
                error_code=type(exc).__name__.upper()[:64],
                error_summary=redact_text(exc)[:500],
            )

    def _mark_published_if_consumed(
        self,
        *,
        outbox_id: int,
        consumer_name: str,
        event_id: str,
    ) -> bool:
        with self._repository.unit_of_work() as uow:
            if not uow.outbox.was_consumed(consumer_name=consumer_name, event_id=event_id):
                return False
            uow.outbox.mark_published(outbox_id, worker_id=self._worker_id)
            uow.commit()
        return True

    def _record_delivery(
        self,
        *,
        outbox_id: int,
        consumer_name: str,
        event_id: str,
        result: Mapping[str, Any] | None,
    ) -> None:
        with self._repository.unit_of_work() as uow:
            if not uow.outbox.was_consumed(consumer_name=consumer_name, event_id=event_id):
                uow.outbox.record_consumption(
                    consumer_name=consumer_name,
                    event_id=event_id,
                    result_summary=dict(result or {}),
                )
            uow.outbox.mark_published(outbox_id, worker_id=self._worker_id)
            uow.commit()

    def _reschedule(
        self,
        outbox_id: int,
        *,
        error_code: str,
        error_summary: str,
        delay_seconds: int = 5,
    ) -> None:
        with self._repository.unit_of_work() as uow:
            status = uow.outbox.reschedule(
                outbox_id,
                worker_id=self._worker_id,
                delay_seconds=delay_seconds,
                error_code=error_code,
                error_summary=error_summary,
            )
            uow.commit()
        if status == "DEAD_LETTER":
            logger.error("Outbox delivery moved to dead letter outbox_id=%s code=%s", outbox_id, error_code)


class OutboxDispatcherGroup:
    """Start, stop, and wake independent consumer-specific dispatcher lanes."""

    def __init__(self, dispatchers: Sequence[OutboxDispatcher]) -> None:
        self._dispatchers = tuple(dispatchers)

    async def start(self) -> None:
        await asyncio.gather(*(dispatcher.start() for dispatcher in self._dispatchers))

    async def stop(self) -> None:
        await asyncio.gather(*(dispatcher.stop() for dispatcher in self._dispatchers))

    def wake(self) -> None:
        for dispatcher in self._dispatchers:
            dispatcher.wake()
