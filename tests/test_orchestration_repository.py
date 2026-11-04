from __future__ import annotations

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
