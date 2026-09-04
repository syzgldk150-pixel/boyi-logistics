from __future__ import annotations

from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
REQUEST_MIGRATION = ROOT / "agent" / "migrations" / "037_cancel_stale_arrive_list_runs.sql"
FINALIZE_MIGRATION = ROOT / "agent" / "migrations" / "038_finalize_stale_arrive_list_cancellations.sql"


def _sql(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


class ArriveListStaleRunRecoveryMigrationTests(TestCase):
    def test_targets_only_old_suspended_arrive_list_scheduler_runs(self) -> None:
        sql = _sql(REQUEST_MIGRATION)

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
        sql = _sql(REQUEST_MIGRATION)

        self.assertIn("@cp037_executing_run_count = 0", sql)
        self.assertIn("cp037_arrive_list_run_still_executing", sql)
        self.assertIn("@cp037_live_lease_count = 0", sql)
        self.assertIn("cp037_arrive_list_live_lease", sql)
        self.assertIn("run.lease_expires_at > UTC_TIMESTAMP(6)", sql)
        self.assertIn("run.lease_expires_at <= UTC_TIMESTAMP(6)", sql)

    def test_uses_runner_cancellation_instead_of_direct_terminal_rewrite(self) -> None:
        sql = _sql(REQUEST_MIGRATION)

        self.assertNotIn("SET run.status = 'CANCELLED'", sql)
        self.assertNotIn("DELETE FROM agent_runs", sql)
        self.assertIn("@cp037_unmarked_run_count = 0", sql)
        self.assertIn("cp037_arrive_list_cancel_request_failed", sql)

    def test_finalizer_only_accepts_runs_marked_by_request_migration(self) -> None:
        sql = _sql(FINALIZE_MIGRATION)

        self.assertIn("BINARY command.automation_id = BINARY 'arrive_list'", sql)
        self.assertIn("BINARY command.source = BINARY 'scheduler'", sql)
        self.assertIn("TIMESTAMP('2026-09-03 16:00:00.000000')", sql)
        self.assertIn("run.cancel_requested_at IS NOT NULL", sql)
        self.assertIn("BINARY run.cancel_requested_by_type = BINARY 'system'", sql)
        self.assertIn("BINARY run.cancel_requested_by_id = BINARY 'migration-037'", sql)

    def test_finalizer_fails_closed_for_live_lease_or_state_mismatch(self) -> None:
        sql = _sql(FINALIZE_MIGRATION)

        self.assertIn("run.lease_expires_at > UTC_TIMESTAMP(6)", sql)
        self.assertIn("cp038_arrive_list_live_lease", sql)
        self.assertIn("BINARY previous_work_item_status <> BINARY previous_status", sql)
        self.assertIn("cp038_arrive_list_item_state_mismatch", sql)

    def test_finalizer_closes_run_and_item_with_audit_outbox(self) -> None:
        sql = _sql(FINALIZE_MIGRATION)

        self.assertIn("SET run.status = 'CANCELLED'", sql)
        self.assertIn("SET item.status = 'CANCELLED'", sql)
        self.assertIn("run.error_code = 'CANCELLED_BY_ACTOR'", sql)
        self.assertIn("item.current_reason_code = 'CANCELLED_BY_ACTOR'", sql)
        self.assertIn("INSERT INTO domain_events", sql)
        self.assertIn("CONCAT('migration-038:', candidate.run_id)", sql)
        self.assertIn("INSERT INTO outbox_events", sql)
        self.assertIn("'orchestration.audit'", sql)
        self.assertIn("@cp038_unclosed_count = 0", sql)
        self.assertNotIn("DELETE FROM agent_runs", sql)
        self.assertNotIn("DELETE FROM work_items", sql)
