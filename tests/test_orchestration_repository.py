from __future__ import annotations

from datetime import datetime
import unittest

from shared.orchestration_repository import (
    OrchestrationRepository,
    OrchestrationUnitOfWork,
)
from shared.orchestration_schema import REQUIRED_COLUMNS, REQUIRED_TABLES


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
    def __init__(self, *, approval_status="APPROVED", expire_pending=False):
        super().__init__()
        self.approval_status = approval_status
        self.expire_pending = expire_pending

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
        elif self.expire_pending and "SET status='EXPIRED'" in sql:
            self.approval_status = "EXPIRED"
        elif "SELECT ar.*" in sql and "FROM approval_requests ar" in sql:
            self.row = {
                "approval_id": "approval-1",
                "run_id": "run-1",
                "approval_round": 2,
                "plan_hash": "plan-hash",
                "status": self.approval_status,
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


class _ApprovalWakeCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "UPDATE agent_runs" in sql:
            self.rowcount = 1
        elif "SELECT * FROM agent_runs WHERE run_id=" in sql:
            self.row = {
                "run_id": "run-1",
                "status": "WAITING_APPROVAL",
                "worker_id": "polling-worker",
                "version": 4,
            }


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
        elif "SELECT status FROM work_items" in sql:
            self.row = {"status": "OPEN"}
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


class _SuspendedCancelCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "UPDATE agent_runs" in sql:
            self.rowcount = 1
        elif "SELECT * FROM agent_runs WHERE run_id=" in sql:
            self.row = {
                "run_id": "run-1",
                "status": "CANCELLED",
                "next_attempt_at": datetime(2026, 8, 13, 2, 30),
                "version": 2,
            }


class _OutboxClaimCursor(_Cursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "SELECT outbox_id" in sql:
            if "status='PENDING'" in sql and "attempt_count < max_attempts" in sql:
                self.rows = [{"outbox_id": 7}]
            else:
                self.rows = []
        elif "SELECT o.*" in sql:
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


class _OutboxTwoStageCursor(_Cursor):
    def __init__(self, *, status: str, attempts_operator: str, outbox_id: int):
        super().__init__()
        self.status = status
        self.attempts_operator = attempts_operator
        self.outbox_id = outbox_id

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "SELECT outbox_id" in sql:
            matches = (
                f"status='{self.status}'" in sql
                and f"attempt_count {self.attempts_operator} max_attempts" in sql
            )
            self.rows = [{"outbox_id": self.outbox_id}] if matches else []
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
        assert command_id == "old-command" and not for_update
        return {"work_item_id": "old-item"}


class _StubRuns:
    def get_first_for_work_item(self, work_item_id, *, for_update=False):
        assert work_item_id == "old-item" and not for_update
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
    def test_schema_requires_persisted_write_scope_and_legacy_quarantine_columns(self):
        for column in ("execution_resource_keys_json", "legacy_scope_quarantined_at"):
            with self.subTest(column=column):
                missing = ("automation_write_attempt_receipts", column)

                class SchemaCursor(_Cursor):
                    def execute(self, sql, params=None):
                        super().execute(sql, params)
                        if "information_schema.TABLES" in sql:
                            self.rows = [{"TABLE_NAME": name} for name in REQUIRED_TABLES]
                        else:
                            self.rows = [{"TABLE_NAME": table, "COLUMN_NAME": name}
                                         for table, name in REQUIRED_COLUMNS if (table, name) != missing]

                with OrchestrationUnitOfWork(lambda: _Connection(SchemaCursor())) as uow:
                    with self.assertRaisesRegex(RuntimeError, "automation_write_attempt_receipts[.]" + column):
                        uow.validate_schema()

    def test_schema_does_not_require_retired_control_plane_tables(self):
        retired = {
            "agent_commands", "work_items", "work_item_entities", "agent_runs",
            "agent_run_steps", "approval_requests", "approval_decisions",
            "evidence_records", "external_entity_links", "domain_events",
            "outbox_events", "event_consumptions", "feishu_approval_deliveries",
            "scheduled_task_approval_policies", "scheduled_task_approval_policy_events",
            "automation_project_approval_batches",
        }
        self.assertFalse(retired & REQUIRED_TABLES)
        self.assertFalse({table for table, _column in REQUIRED_COLUMNS} & retired)
        self.assertNotIn(("automation_write_attempt_receipts", "orchestration_run_id"), REQUIRED_COLUMNS)
        self.assertFalse({
            column
            for table, column in REQUIRED_COLUMNS
            if table == "automation_project_generation_transition_tasks" and column.startswith("policy_")
        })

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

if __name__ == "__main__":
    unittest.main()
