from __future__ import annotations

import asyncio
import threading
import unittest

from agent.orchestration.outbox_dispatcher import (
    OutboxDispatcher,
    OutboxDispatcherGroup,
    OutboxRetryAfter,
)


class _OutboxRows:
    def __init__(self, deliveries):
        self._deliveries = {
            str(delivery["consumer_name"]): dict(delivery)
            for delivery in deliveries
        }
        self._consumed: set[tuple[str, str]] = set()
        self.claims: list[tuple[str, str | None, int]] = []
        self.published: list[tuple[int, str]] = []
        self.rescheduled: list[tuple[tuple, dict]] = []
        self._lock = threading.Lock()

    def claim(self, worker_id, *, limit, lease_seconds, consumer_name=None):
        del limit
        with self._lock:
            self.claims.append((worker_id, consumer_name, lease_seconds))
            delivery = self._deliveries.pop(str(consumer_name), None)
        return [delivery] if delivery is not None else []

    def was_consumed(self, *, consumer_name, event_id):
        with self._lock:
            return (consumer_name, event_id) in self._consumed

    def record_consumption(self, *, consumer_name, event_id, result_summary):
        del result_summary
        with self._lock:
            self._consumed.add((consumer_name, event_id))

    def mark_published(self, outbox_id, *, worker_id):
        with self._lock:
            self.published.append((outbox_id, worker_id))

    def reschedule(self, *args, **kwargs):
        self.rescheduled.append((args, dict(kwargs)))
        return "PENDING"


class _UnitOfWork:
    def __init__(self, repository):
        self._repository = repository
        self.outbox = repository.outbox

    def __enter__(self):
        with self._repository._lock:
            self._repository.active_transactions += 1
            thread_id = threading.get_ident()
            self._repository.transaction_depth_by_thread[thread_id] = (
                self._repository.transaction_depth_by_thread.get(thread_id, 0) + 1
            )
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        with self._repository._lock:
            self._repository.active_transactions -= 1
            thread_id = threading.get_ident()
            depth = self._repository.transaction_depth_by_thread[thread_id] - 1
            if depth:
                self._repository.transaction_depth_by_thread[thread_id] = depth
            else:
                self._repository.transaction_depth_by_thread.pop(thread_id)
        return False

    def commit(self):
        return None


class _Repository:
    def __init__(self, deliveries):
        self.outbox = _OutboxRows(deliveries)
        self.active_transactions = 0
        self.transaction_depth_by_thread: dict[int, int] = {}
        self._lock = threading.Lock()

    def unit_of_work(self):
        return _UnitOfWork(self)


def _delivery(outbox_id: int, consumer_name: str) -> dict:
    return {
        "outbox_id": outbox_id,
        "consumer_name": consumer_name,
        "event_id": f"event-{outbox_id}",
        "payload_json": {},
    }


class OutboxDispatcherLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_outside_uow_handler_keeps_consumption_idempotency(self):
        repository = _Repository(())
        calls = []

        def handler(delivery, uow):
            with repository._lock:
                transaction_depth = repository.transaction_depth_by_thread.get(
                    threading.get_ident(),
                    0,
                )
            calls.append((delivery["event_id"], uow, transaction_depth))
            return {"processed": True}

        dispatcher = OutboxDispatcher(
            repository,
            worker_id="worker:finance",
            consumer_name="finance.brain",
            handlers={"finance.brain": handler},
            handler_uses_uow=False,
        )
        delivery = _delivery(7, "finance.brain")

        dispatcher._deliver(delivery)
        dispatcher._deliver(delivery)

        self.assertEqual([("event-7", None, 0)], calls)
        self.assertEqual(
            {("finance.brain", "event-7")},
            repository.outbox._consumed,
        )

    async def test_slow_finance_lane_does_not_block_approval_lane(self):
        repository = _Repository(
            (
                _delivery(1, "finance.brain"),
                _delivery(2, "feishu.approval"),
            )
        )
        finance_started = threading.Event()
        release_finance = threading.Event()
        approval_delivered = threading.Event()
        finance_transaction_state: list[int] = []

        def slow_finance(_delivery_row, _uow):
            with repository._lock:
                finance_transaction_state.append(
                    repository.transaction_depth_by_thread.get(threading.get_ident(), 0)
                )
            finance_started.set()
            if not release_finance.wait(timeout=2):
                raise TimeoutError("test did not release finance lane")
            return {"processed": True}

        def deliver_approval(_delivery_row, _uow):
            approval_delivered.set()
            return {"sent": 1}

        group = OutboxDispatcherGroup(
            (
                OutboxDispatcher(
                    repository,
                    worker_id="worker:finance",
                    consumer_name="finance.brain",
                    handlers={"finance.brain": slow_finance},
                    handler_uses_uow=False,
                    poll_interval_seconds=0.1,
                    lease_seconds=3600,
                ),
                OutboxDispatcher(
                    repository,
                    worker_id="worker:approval",
                    consumer_name="feishu.approval",
                    handlers={"feishu.approval": deliver_approval},
                    poll_interval_seconds=0.1,
                    lease_seconds=60,
                ),
            )
        )

        await group.start()
        try:
            self.assertTrue(await asyncio.to_thread(finance_started.wait, 1))
            self.assertTrue(await asyncio.to_thread(approval_delivered.wait, 1))
        finally:
            release_finance.set()
            await group.stop()

        claimed_consumers = {consumer for _worker, consumer, _lease in repository.outbox.claims}
        self.assertEqual({"finance.brain", "feishu.approval"}, claimed_consumers)
        leases = {
            consumer: lease
            for _worker, consumer, lease in repository.outbox.claims
            if consumer is not None
        }
        self.assertEqual(3600, leases["finance.brain"])
        self.assertEqual(60, leases["feishu.approval"])
        self.assertEqual([0], finance_transaction_state)
        self.assertEqual(2, len(repository.outbox.published))

    async def test_dependency_lease_uses_its_expiry_delay_for_one_retry_attempt(self):
        repository = _Repository(())

        def handler(_delivery_row, _uow):
            raise OutboxRetryAfter("notification lane busy", delay_seconds=87)

        dispatcher = OutboxDispatcher(
            repository,
            worker_id="worker:approval",
            consumer_name="feishu.approval",
            handlers={"feishu.approval": handler},
            handler_uses_uow=False,
        )

        dispatcher._deliver(_delivery(9, "feishu.approval"))

        self.assertEqual(1, len(repository.outbox.rescheduled))
        _args, kwargs = repository.outbox.rescheduled[0]
        self.assertEqual(87, kwargs["delay_seconds"])
        self.assertEqual("OUTBOX_DEPENDENCY_LEASED", kwargs["error_code"])


if __name__ == "__main__":
    unittest.main()
