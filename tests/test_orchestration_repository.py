from __future__ import annotations

from datetime import datetime
from pathlib import Path
import unittest

from shared.orchestration_repository import (
    AgentRunRepository,
    ApprovalRepository,
    CommandRepository,
    OrchestrationRepository,
    OrchestrationUnitOfWork,
    OutboxRepository,
    WorkItemRepository,
)


class _Cursor:
    def __init__(self, *, rows=None, row=None):
        self.rows = list(rows or [])
        self.row = row
        self.rowcount = 0
        self.calls = []
        self.description = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        expected_params = normalized.count("%s")
        actual_params = 0 if params is None else len(params)
        if expected_params != actual_params:
            raise AssertionError(
                f"SQL placeholder mismatch: expected {expected_params}, received {actual_params}"
            )
        self.calls.append((normalized, params))
        self.rowcount = 0

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _ApprovedExecutionCursor(_Cursor):
    def __init__(self, *, approval_status="APPROVED", is_unexpired=1):
        super().__init__()
        self.approval_status = approval_status
        self.is_unexpired = is_unexpired

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "SELECT * FROM agent_runs WHERE run_id=" in sql:
            self.row = {
                "run_id": "run-1",
                "status": "WAITING_APPROVAL",
                "version": 4,
                "plan_hash": "plan-hash",
                "plan_json": '{"steps":[]}',
            }
        elif "SELECT ar.*, (ar.expires_at > NOW(6))" in sql:
            self.row = {
                "approval_id": "approval-1",
                "run_id": "run-1",
                "approval_round": 2,
                "plan_hash": "plan-hash",
                "status": self.approval_status,
                "is_unexpired": self.is_unexpired,
                "impact_json": '{}',
            }


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit_calls = []
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False

    def autocommit(self, value):
        self.autocommit_calls.append(value)

    def begin(self):
        self.begin_count += 1

    def cursor(self, _cursor_factory=None):
        return self._cursor

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


class _LinkedCommandWorkItemCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "JOIN agent_commands c" in sql:
            self.row = None
        elif "JOIN agent_runs r" in sql:
            self.row = {
                "work_item_id": "item-1",
                "command_id": "original-command",
                "resolution_json": '{"source":"retry"}',
            }


class _CommandCursor(_Cursor):
    def __init__(self, persisted):
        super().__init__()
        self.persisted = dict(persisted)

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "INSERT INTO agent_commands" in sql:
            self.rowcount = 0
            self.row = None
        elif "FROM agent_commands WHERE source=" in sql:
            self.row = dict(self.persisted)


class _ClaimCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "SELECT * FROM agent_runs" in sql:
            self.rows = [
                {
                    "run_id": "run-1",
                    "status": "RECEIVED",
                    "version": 1,
                    "execution_attempt_count": 0,
                    "plan_json": None,
                }
            ]
        elif "UPDATE agent_runs" in sql:
            self.rowcount = 1


class _RunCreateCursor(_Cursor):
    def __init__(self, persisted):
        super().__init__()
        self.persisted = dict(persisted)

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "INSERT INTO agent_runs" in sql:
            self.rowcount = 1
        elif "FROM agent_runs WHERE work_item_id=" in sql:
            self.row = dict(self.persisted)


class _LinkedRetryCursor(_Cursor):
    def __init__(self):
        super().__init__()
        self.source = {
            "run_id": "source-run",
            "work_item_id": "item-1",
            "command_id": "command-1",
            "run_no": 2,
            "status": "FAILED_TERMINAL",
            "mode": "COMMAND",
            "planner_kind": "DETERMINISTIC",
            "planner_provider": None,
            "planner_model": None,
            "correlation_id": "correlation-1",
            "plan_json": '{"steps":[]}',
        }
        self.source_command = {
            "command_id": "command-1",
            "command_type": "tool.execute",
            "source": "console",
            "actor_type": "console_admin",
            "actor_id": "17",
            "actor_roles_json": '["super_admin"]',
            "entity_refs_json": "[]",
            "parameters_json": '{"tool_name":"receipts_audit","arguments":{}}',
        }

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "WHERE retry_of_run_id=" in sql:
            self.row = None
        elif "SELECT * FROM agent_runs WHERE run_id=" in sql:
            if params and params[0] == "source-run":
                self.row = dict(self.source)
            else:
                self.row = {
                    **self.source,
                    "run_id": "retry-run",
                    "run_no": 3,
                    "status": "RECEIVED",
                    "retry_of_run_id": "source-run",
                    "plan_json": None,
                }
        elif "SELECT * FROM agent_commands WHERE command_id=" in sql:
            self.row = dict(self.source_command)
        elif "SELECT work_item_id FROM work_items" in sql:
            self.row = {"work_item_id": "item-1"}
        elif "SELECT COALESCE(MAX(run_no)" in sql:
            self.row = {"max_run_no": 2}
        elif "INSERT INTO agent_runs" in sql:
            self.rowcount = 1
        elif "INSERT INTO agent_commands" in sql:
            self.rowcount = 1


class _BlockedLoginCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "SELECT COUNT(*) AS total" in sql:
            self.row = {"total": 3}
        elif "SELECT r.*" in sql:
            self.rows = [
                {
                    "run_id": "blocked-1",
                    "status": "BLOCKED_LOGIN",
                    "plan_json": '{"steps":[{"account_id":"account-1"}]}',
                },
                {
                    "run_id": "blocked-2",
                    "status": "BLOCKED_LOGIN",
                    "plan_json": None,
                },
            ]


class _CancelCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "SELECT status FROM agent_runs" in sql:
            self.row = {"status": "PARTIAL"}


class _OutboxClaimCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "SELECT o.*" in sql:
            self.rows = [
                {
                    "outbox_id": 7,
                    "event_id": "event-1",
                    "status": "PENDING",
                    "attempt_count": 0,
                    "payload_json": '{"ok":true}',
                    "headers_json": None,
                }
            ]
        elif "UPDATE outbox_events" in sql:
            self.rowcount = 1


class _AssignCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "UPDATE work_items" in sql:
            self.rowcount = 1
        elif "SELECT * FROM work_items" in sql:
            self.row = {
                "work_item_id": "item-1",
                "owner_type": "console_admin",
                "owner_id": "admin-1",
                "version": 2,
                "resolution_json": None,
            }


class _StubCommands:
    def create_or_get(self, _row):
        return {
            "command_id": "old-command",
            "correlation_id": "old-correlation",
            "_created": False,
        }


class _StubWorkItems:
    def get_by_command(self, command_id, *, for_update=False):
        assert command_id == "old-command" and for_update
        return {"work_item_id": "old-item"}


class _StubRuns:
    def get_first_for_work_item(self, work_item_id, *, for_update=False):
        assert work_item_id == "old-item" and for_update
        return {"run_id": "old-run"}


class _StubEvents:
    def __init__(self):
        self.appended = False

    def get_first_for_entity(self, entity_type, entity_id):
        assert (entity_type, entity_id) == ("agent_command", "old-command")
        return {"event_id": "old-event"}

    def append_with_outbox(self, *_args):
        self.appended = True
        raise AssertionError("duplicate gateway request must not append an event")


class _StubOutbox:
    def list_for_event(self, event_id):
        assert event_id == "old-event"
        return [{"outbox_id": 1, "event_id": event_id}]


class OrchestrationRepositoryTests(unittest.TestCase):
    def test_unit_of_work_disables_autocommit_and_requires_explicit_commit(self):
        first = _Connection(_Cursor())
        with OrchestrationUnitOfWork(lambda: first):
            pass

        self.assertEqual([False], first.autocommit_calls)
        self.assertEqual(1, first.begin_count)
        self.assertEqual(0, first.commit_count)
        self.assertEqual(1, first.rollback_count)
        self.assertTrue(first.closed)

        second = _Connection(_Cursor())
        with OrchestrationUnitOfWork(lambda: second) as uow:
            uow.commit()
        self.assertEqual(1, second.commit_count)
        self.assertEqual(0, second.rollback_count)

    def test_command_retry_uses_first_correlation_id_and_redacted_payload(self):
        persisted = {
            "command_id": "command-1",
            "command_type": "sync",
            "source": "console",
            "actor_type": "admin",
            "actor_id": "admin-1",
            "actor_roles_json": '["operations"]',
            "entity_refs_json": "[]",
            "parameters_json": '{"password":"[REDACTED]"}',
            "idempotency_key": "same-key",
            "correlation_id": "first-correlation",
            "status": "RECEIVED",
        }
        cursor = _CommandCursor(persisted)
        repository = CommandRepository(_Connection(cursor))

        result = repository.create_or_get(
            {
                "command_id": "new-random-id",
                "command_type": "sync",
                "source": "console",
                "actor_type": "admin",
                "actor_id": "admin-1",
                "actor_roles": ["operations"],
                "entity_refs": [],
                "parameters": {"password": "secret-value"},
                "idempotency_key": "same-key",
                "correlation_id": "new-correlation",
                "requested_at": datetime(2026, 8, 13, 1, 0),
            }
        )

        self.assertFalse(result["_created"])
        self.assertEqual("first-correlation", result["correlation_id"])
        insert_params = next(params for sql, params in cursor.calls if "INSERT INTO agent_commands" in sql)
        self.assertIn("[REDACTED]", insert_params[7])
        self.assertNotIn("secret-value", insert_params[7])

    def test_duplicate_gateway_returns_original_aggregate_without_new_event(self):
        connection = _Connection(_Cursor())
        uow = OrchestrationUnitOfWork(lambda: connection)
        uow.connection = connection
        uow._entered = True
        uow.commands = _StubCommands()
        uow.work_items = _StubWorkItems()
        uow.runs = _StubRuns()
        uow.events = _StubEvents()
        uow.outbox = _StubOutbox()

        receipt = uow.command_gateway_create({}, {}, {}, {}, [])

        self.assertEqual("old-item", receipt["work_item_id"])
        self.assertEqual("old-run", receipt["run_id"])
        self.assertEqual("old-event", receipt["event_id"])
        self.assertFalse(any(receipt["created"].values()))
        self.assertFalse(uow.events.appended)

    def test_run_claim_uses_mysql8_skip_locked_and_lease_recovery(self):
        cursor = _ClaimCursor()
        repository = AgentRunRepository(_Connection(cursor))

        rows = repository.claim(
            "worker-1",
            ["RECEIVED", "FAILED_RETRYABLE"],
            limit=5,
            lease_seconds=30,
            now=datetime(2026, 8, 13, 2, 0),
        )

        select_sql = next(sql for sql, _ in cursor.calls if "SELECT * FROM agent_runs" in sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", select_sql)
        self.assertIn("lease_expires_at <=", select_sql)
        self.assertEqual("worker-1", rows[0]["worker_id"])
        self.assertEqual(0, rows[0]["execution_attempt_count"])
        claim_update = next(sql for sql, _ in cursor.calls if "UPDATE agent_runs" in sql)
        self.assertNotIn("execution_attempt_count", claim_update)

    def test_cancellation_claim_excludes_every_terminal_status(self):
        cursor = _ClaimCursor()
        repository = AgentRunRepository(_Connection(cursor))

        repository.claim_cancel_requested("worker-1", limit=5, lease_seconds=30)

        select_sql = next(sql for sql, _ in cursor.calls if "SELECT * FROM agent_runs" in sql)
        self.assertIn("'COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED'", select_sql)

    def test_partial_run_rejects_a_new_cancellation_request(self):
        cursor = _CancelCursor()
        repository = AgentRunRepository(_Connection(cursor))

        with self.assertRaisesRegex(Exception, "terminal run"):
            repository.request_cancel("run-1", requested_by_type="admin")

        self.assertFalse(any("UPDATE agent_runs" in sql for sql, _ in cursor.calls))

    def test_run_create_accepts_an_explicit_initial_schedule(self):
        scheduled_at = datetime(2026, 8, 13, 2, 30)
        cursor = _RunCreateCursor(
            {
                "run_id": "run-1",
                "work_item_id": "item-1",
                "command_id": "command-1",
                "run_no": 1,
                "status": "RECEIVED",
                "plan_json": None,
            }
        )
        repository = AgentRunRepository(_Connection(cursor))

        created = repository.create_or_get(
            {
                "run_id": "run-1",
                "work_item_id": "item-1",
                "command_id": "command-1",
                "run_no": 1,
                "status": "RECEIVED",
                "mode": "command",
                "correlation_id": "correlation-1",
                "next_attempt_at": scheduled_at,
            }
        )

        insert_sql, insert_params = next(
            (sql, params) for sql, params in cursor.calls if "INSERT INTO agent_runs" in sql
        )
        self.assertIn("COALESCE(%s, NOW(6))", insert_sql)
        self.assertIs(scheduled_at, insert_params[20])
        self.assertTrue(created["_created"])

    def test_generic_transition_rejects_a_same_state_plan_replacement(self):
        cursor = _Cursor()
        repository = AgentRunRepository(_Connection(cursor))

        with self.assertRaisesRegex(Exception, "refresh_waiting_plan"):
            repository.transition(
                "run-1",
                expected_version=1,
                expected_statuses=("WAITING_APPROVAL",),
                status="WAITING_APPROVAL",
                plan={"steps": []},
                plan_hash="new-plan-hash",
            )

        self.assertEqual([], cursor.calls)

    def test_linked_retry_locks_source_and_allocates_a_fresh_run_number(self):
        retry_at = datetime(2026, 8, 13, 3, 0)
        cursor = _LinkedRetryCursor()
        repository = AgentRunRepository(_Connection(cursor))

        created = repository.create_linked_retry(
            "source-run",
            new_run_id="retry-run",
            new_command_id="retry-command",
            now=retry_at,
        )

        source_sql = next(
            sql for sql, params in cursor.calls if params and params[0] == "source-run"
        )
        self.assertIn("FOR UPDATE", source_sql)
        self.assertIn("status IN", source_sql)
        self.assertTrue(any("FROM work_items" in sql and "FOR UPDATE" in sql for sql, _ in cursor.calls))
        command_sql, command_params = next(
            (sql, params) for sql, params in cursor.calls if "INSERT INTO agent_commands" in sql
        )
        self.assertIn("'RECEIVED'", command_sql)
        self.assertEqual("retry-command", command_params[0])
        self.assertEqual("retry:source-run:retry-run", command_params[8])
        insert_sql, insert_params = next(
            (sql, params) for sql, params in cursor.calls if "INSERT INTO agent_runs" in sql
        )
        self.assertIn("'RECEIVED'", insert_sql)
        self.assertNotIn("plan_json", insert_sql)
        self.assertEqual(3, insert_params[3])
        self.assertEqual("retry-command", insert_params[2])
        self.assertEqual("source-run", insert_params[9])
        self.assertIs(retry_at, insert_params[10])
        self.assertEqual("retry-run", created["run_id"])
        self.assertEqual("source-run", created["retry_of_run_id"])

    def test_linked_retry_rejects_non_terminal_source_statuses_before_sql(self):
        cursor = _Cursor()
        repository = AgentRunRepository(_Connection(cursor))

        with self.assertRaisesRegex(Exception, "terminal retry states"):
            repository.create_linked_retry(
                "source-run",
                new_run_id="retry-run",
                new_command_id="retry-command",
                expected_statuses=("RUNNING",),
            )
        self.assertEqual([], cursor.calls)

    def test_blocked_login_account_query_is_explicit_and_page_reports_completion(self):
        cursor = _BlockedLoginCursor()
        repository = AgentRunRepository(_Connection(cursor))

        page = repository.page_blocked_login_for_account("account-1", limit=2, offset=0)

        list_sql, list_params = next(
            (sql, params) for sql, params in cursor.calls if "SELECT r.*" in sql
        )
        self.assertIn("r.status='BLOCKED_LOGIN'", list_sql)
        self.assertIn("r.cancel_requested_at IS NULL", list_sql)
        self.assertIn("BINARY s.account_id=BINARY %s", list_sql)
        self.assertIn("JSON_EXTRACT(s.result_summary_json, '$.meta.account_id')", list_sql)
        self.assertIn("JSON_OBJECT('account_id', %s)", list_sql)
        self.assertIn("'$.steps'", list_sql)
        self.assertNotIn("JSON_SEARCH", list_sql)
        self.assertEqual(("account-1", "account-1", "account-1", 2, 0), list_params)
        self.assertEqual(3, page["total"])
        self.assertEqual(2, page["next_offset"])
        self.assertFalse(page["is_complete"])
        self.assertEqual({"steps": [{"account_id": "account-1"}]}, page["items"][0]["plan_json"])

    def test_outbox_claim_uses_skip_locked_and_decodes_event_payload(self):
        cursor = _OutboxClaimCursor()
        repository = OutboxRepository(_Connection(cursor))

        rows = repository.claim("dispatcher-1", limit=2, lease_seconds=20)

        select_sql = next(sql for sql, _ in cursor.calls if "SELECT o.*" in sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", select_sql)
        self.assertEqual({"ok": True}, rows[0]["payload_json"])
        self.assertEqual("PROCESSING", rows[0]["status"])

    def test_work_item_assignment_rejects_empty_owner(self):
        cursor = _Cursor()
        repository = WorkItemRepository(_Connection(cursor))

        with self.assertRaisesRegex(ValueError, "owner_id is required"):
            repository.assign("item-1", 1, "console_admin", "")
        self.assertEqual([], cursor.calls)

    def test_work_item_lookup_follows_linked_retry_command_through_run(self):
        cursor = _LinkedCommandWorkItemCursor()
        repository = WorkItemRepository(_Connection(cursor))

        item = repository.get_by_command("retry-command")

        self.assertEqual("item-1", item["work_item_id"])
        self.assertEqual({"source": "retry"}, item["resolution_json"])
        self.assertEqual(2, len(cursor.calls))
        self.assertIn("JOIN agent_runs r", cursor.calls[1][0])

    def test_work_item_list_applies_exact_filters_search_and_sla_window(self):
        cursor = _Cursor(rows=[])
        repository = WorkItemRepository(_Connection(cursor))
        start = datetime(2026, 8, 13, 0, 0, 0)
        end = datetime(2026, 8, 14, 0, 0, 0)

        repository.list(
            status="OPEN",
            item_type="daily_sign",
            priority="HIGH",
            source="scheduler",
            query="YD_100%",
            owner_id="17",
            sla_from=start,
            sla_before=end,
            sla_missing=False,
            limit=25,
            offset=50,
        )

        sql, params = cursor.calls[0]
        self.assertIn("EXISTS (SELECT 1 FROM work_item_entities", sql)
        self.assertIn("sla_deadline >= %s", sql)
        self.assertIn("sla_deadline < %s", sql)
        self.assertIn("sla_deadline IS NOT NULL", sql)
        self.assertIn("%YD\\_100\\%%", params)
        self.assertEqual((25, 50), tuple(params[-2:]))

    def test_work_item_assignment_is_cas_and_facade_commits(self):
        cursor = _AssignCursor()
        connection = _Connection(cursor)
        repository = OrchestrationRepository(lambda: connection)

        assigned = repository.assign_work_item(
            "item-1",
            expected_version=1,
            owner_type="console_admin",
            owner_id="admin-1",
        )

        update_sql, update_params = next(
            (sql, params) for sql, params in cursor.calls if "UPDATE work_items" in sql
        )
        self.assertIn("WHERE work_item_id=%s AND version=%s", update_sql)
        self.assertEqual(("console_admin", "admin-1", "item-1", 1), update_params)
        self.assertEqual(2, assigned["version"])
        self.assertEqual(1, connection.commit_count)
        self.assertEqual(0, connection.rollback_count)

    def test_mysql_version_preflight_rejects_mariadb_and_old_mysql(self):
        for version in ("10.11.6-MariaDB", "5.7.44"):
            with self.subTest(version=version):
                connection = _Connection(_Cursor(row={"version": version}))
                repository = OrchestrationRepository(lambda: connection)
                with self.assertRaisesRegex(RuntimeError, "requires MySQL 8"):
                    repository.validate_mysql8()

        connection = _Connection(_Cursor(row={"version": "8.0.43"}))
        repository = OrchestrationRepository(lambda: connection)
        self.assertEqual("8.0.43", repository.validate_mysql8())

    def test_approved_execution_uses_db_clock_and_locks_run_and_latest_approval(self):
        cursor = _ApprovedExecutionCursor()
        repository = ApprovalRepository(_Connection(cursor))

        prepared = repository.prepare_approved_execution(
            "run-1",
            expected_plan_hash="plan-hash",
        )

        self.assertEqual("APPROVED", prepared["outcome"])
        sql = [statement for statement, _params in cursor.calls]
        self.assertIn("SELECT * FROM agent_runs WHERE run_id=%s FOR UPDATE", sql[0])
        self.assertTrue(any("status IN ('PENDING', 'APPROVED')" in item for item in sql))
        self.assertTrue(any("expires_at <= NOW(6)" in item for item in sql))
        self.assertTrue(any("LIMIT 1 FOR UPDATE" in item for item in sql))

    def test_approved_execution_cannot_consume_expired_approval(self):
        cursor = _ApprovedExecutionCursor(approval_status="EXPIRED", is_unexpired=0)
        repository = ApprovalRepository(_Connection(cursor))

        prepared = repository.prepare_approved_execution(
            "run-1",
            expected_plan_hash="plan-hash",
        )

        self.assertEqual("EXPIRED", prepared["outcome"])

    def test_migrations_define_required_control_plane_guards(self):
        root = Path(__file__).resolve().parents[1]
        core_sql = (root / "agent" / "migrations" / "011_agent_orchestration_core.sql").read_text(
            encoding="utf-8"
        )
        outbox_sql = (root / "agent" / "migrations" / "012_domain_event_outbox.sql").read_text(
            encoding="utf-8"
        )

        for table in (
            "agent_commands",
            "work_items",
            "agent_runs",
            "agent_run_steps",
            "approval_requests",
            "evidence_records",
            "external_entity_links",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", core_sql)
        self.assertIn("required_role VARCHAR(64) NOT NULL", core_sql)
        self.assertIn("INTERNAL_PROJECTION_WRITE", core_sql)
        self.assertIn("EXTREME", core_sql)
        self.assertIn(
            "control_plane_role ENUM(''admin'', ''super_admin'') NOT NULL DEFAULT ''admin''",
            core_sql,
        )
        self.assertIn("WHERE is_active = 1", core_sql)
        self.assertIn("ORDER BY created_at, id", core_sql)
        self.assertIn("WHERE control_plane_role = 'super_admin'", core_sql)
        for table in ("domain_events", "outbox_events", "event_consumptions"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", outbox_sql)
        self.assertIn("UNIQUE KEY uq_outbox_event_consumer", outbox_sql)


if __name__ == "__main__":
    unittest.main()
