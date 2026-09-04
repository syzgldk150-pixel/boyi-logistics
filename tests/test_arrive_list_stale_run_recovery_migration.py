from __future__ import annotations

from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "agent" / "migrations" / "037_cancel_stale_arrive_list_runs.sql"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").split())


class ArriveListStaleRunRecoveryMigrationTests(TestCase):
    def test_targets_only_old_suspended_arrive_list_scheduler_runs(self) -> None:
        sql = _sql()

        self.assertIn("BINARY command.automation_id = BINARY 'arrive_list'", sql)
        self.assertIn("BINARY command.source = BINARY 'scheduler'", sql)
        self.assertIn("TIMESTAMP('2026-09-03 16:00:00.000000')", sql)
        self.assertIn(
            "run.status IN ('NEEDS_CLARIFICATION', 'BLOCKED_LOGIN', 'BLOCKED_DATA')",
            sql,
        )
        self.assertIn("run.cancel_requested_at = UTC_TIMESTAMP(6)", sql)
        self.assertIn("run.cancel_requested_by_type = 'system'", sql)
        self.assertIn("run.cancel_requested_by_id = 'migration-037'", sql)

    def test_fails_closed_for_executing_or_leased_runs(self) -> None:
        sql = _sql()

        self.assertIn("@cp037_executing_run_count = 0", sql)
        self.assertIn("cp037_arrive_list_run_still_executing", sql)
        self.assertIn("@cp037_live_lease_count = 0", sql)
        self.assertIn("cp037_arrive_list_live_lease", sql)
        self.assertIn("run.lease_expires_at > UTC_TIMESTAMP(6)", sql)
        self.assertIn("run.lease_expires_at <= UTC_TIMESTAMP(6)", sql)

    def test_uses_runner_cancellation_instead_of_direct_terminal_rewrite(self) -> None:
        sql = _sql()

        self.assertNotIn("SET run.status = 'CANCELLED'", sql)
        self.assertNotIn("DELETE FROM agent_runs", sql)
        self.assertIn("@cp037_unmarked_run_count = 0", sql)
        self.assertIn("cp037_arrive_list_cancel_request_failed", sql)
