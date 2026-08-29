from __future__ import annotations

from pathlib import Path
import unittest

from agent.orchestration.control_plane_retention import (
    CONTROL_PLANE_RETENTION_DAYS,
    ControlPlaneRetentionWorker,
)
from shared.orchestration_repository import ControlPlaneRetentionRepository


class _RetentionCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.rows: list[dict] = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self.rows = []
        self.rowcount = 0
        if normalized.startswith("SELECT domain_event.event_id"):
            self.rows = [{"event_id": "event-1"}]
        elif normalized.startswith("SELECT approval.approval_id"):
            self.rows = [{"approval_id": "approval-1"}]
        elif normalized.startswith("DELETE FROM event_consumptions"):
            self.rowcount = 2
        elif normalized.startswith("DELETE FROM outbox_events"):
            self.rowcount = 2
        elif normalized.startswith("DELETE FROM domain_events"):
            self.rowcount = 1
        elif normalized.startswith("DELETE FROM approval_decisions"):
            self.rowcount = 1
        elif normalized.startswith("DELETE FROM approval_requests"):
            self.rowcount = 1

    def fetchall(self):
        return list(self.rows)

    def close(self):
        return None


class _RetentionConnection:
    def __init__(self, cursor: _RetentionCursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class ControlPlaneRetentionRepositoryTests(unittest.TestCase):
    def test_purge_batch_deletes_children_before_locked_parents(self):
        cursor = _RetentionCursor()
        repository = ControlPlaneRetentionRepository(_RetentionConnection(cursor))

        counts = repository.purge_batch(retention_days=30, batch_size=500)

        self.assertEqual(
            {
                "domain_events": 1,
                "outbox_events": 2,
                "event_consumptions": 2,
                "approval_requests": 1,
                "approval_decisions": 1,
            },
            counts,
        )
        statements = [sql for sql, _params in cursor.calls]
        self.assertLess(
            statements.index(
                next(
                    sql
                    for sql in statements
                    if sql.startswith("DELETE FROM event_consumptions")
                )
            ),
            statements.index(
                next(
                    sql
                    for sql in statements
                    if sql.startswith("DELETE FROM domain_events")
                )
            ),
        )
        self.assertLess(
            statements.index(
                next(
                    sql
                    for sql in statements
                    if sql.startswith("DELETE FROM approval_decisions")
                )
            ),
            statements.index(
                next(
                    sql
                    for sql in statements
                    if sql.startswith("DELETE FROM approval_requests")
                )
            ),
        )

        event_query = next(
            sql
            for sql in statements
            if sql.startswith("SELECT domain_event.event_id")
        )
        self.assertIn("pending_outbox.status IN ('PENDING', 'PROCESSING')", event_query)
        self.assertIn("item.status IN ('RESOLVED', 'CANCELLED')", event_query)
        approval_query = next(
            sql for sql in statements if sql.startswith("SELECT approval.approval_id")
        )
        self.assertIn(
            "approval.status IN ('APPROVED', 'REJECTED', 'EXPIRED', 'INVALIDATED')",
            approval_query,
        )
        self.assertIn("delivery.notification_lease_token IS NOT NULL", approval_query)

    def test_retention_migration_adds_all_lookup_indexes_idempotently(self):
        migration = (
            Path(__file__).resolve().parents[1]
            / "agent"
            / "migrations"
            / "031_control_plane_retention.sql"
        ).read_text(encoding="utf-8")

        for index_name in (
            "idx_domain_events_retention",
            "idx_outbox_events_retention",
            "idx_approval_requests_retention",
        ):
            self.assertIn(index_name, migration)
        self.assertEqual(3, migration.count("FROM information_schema.statistics"))


class _WorkerRetentionRows:
    def __init__(self, batches: list[dict[str, int]]) -> None:
        self._batches = list(batches)
        self.calls: list[tuple[int, int]] = []

    def purge_batch(self, *, retention_days: int, batch_size: int):
        self.calls.append((retention_days, batch_size))
        return self._batches.pop(0)


class _WorkerUnitOfWork:
    def __init__(self, rows: _WorkerRetentionRows) -> None:
        self.retention = rows
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def commit(self):
        self.committed = True


class _WorkerRepository:
    def __init__(self, batches: list[dict[str, int]]) -> None:
        self.rows = _WorkerRetentionRows(batches)

    def unit_of_work(self):
        return _WorkerUnitOfWork(self.rows)


class ControlPlaneRetentionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_repeats_full_batches_and_uses_fixed_thirty_day_window(self):
        repository = _WorkerRepository(
            [
                {
                    "domain_events": 2,
                    "outbox_events": 3,
                    "event_consumptions": 4,
                    "approval_requests": 0,
                    "approval_decisions": 0,
                },
                {
                    "domain_events": 1,
                    "outbox_events": 1,
                    "event_consumptions": 1,
                    "approval_requests": 1,
                    "approval_decisions": 1,
                },
            ]
        )
        worker = ControlPlaneRetentionWorker(
            repository,
            batch_size=2,
            batch_pause_seconds=0,
        )

        totals = await worker.purge_available()

        self.assertEqual(3, totals["domain_events"])
        self.assertEqual(4, totals["outbox_events"])
        self.assertEqual(5, totals["event_consumptions"])
        self.assertEqual(1, totals["approval_requests"])
        self.assertEqual(
            [(CONTROL_PLANE_RETENTION_DAYS, 2), (CONTROL_PLANE_RETENTION_DAYS, 2)],
            repository.rows.calls,
        )


if __name__ == "__main__":
    unittest.main()
