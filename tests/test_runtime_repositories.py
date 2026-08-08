from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import unittest

from shared.runtime_repositories import ScheduledTaskRepository, WorkflowResourceRepository


class _Cursor:
    def __init__(self, rows=None, row=None):
        self.rows = list(rows or [])
        self.row = row
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self, _cursor_factory=None):
        return self._cursor

    def close(self):
        self.closed = True


class RuntimeRepositoryTests(unittest.TestCase):
    def test_scheduled_task_list_decodes_json_and_formats_timestamps(self):
        cursor = _Cursor(
            rows=[
                {
                    "id": "daily",
                    "name": "每日任务",
                    "tool_name": "sync",
                    "tool_params": '{"scope":"today"}',
                    "cron_expression": "0 1 * * *",
                    "enabled": 1,
                    "last_run": datetime(2026, 8, 8, 1, 0, 0),
                    "last_status": "success",
                    "last_duration_ms": 12,
                    "last_message": None,
                    "created_at": datetime(2026, 8, 1, 1, 0, 0),
                }
            ]
        )
        connection = _Connection(cursor)
        repository = ScheduledTaskRepository(lambda: connection)

        rows = repository.list_tasks(enabled_only=True)

        self.assertEqual({"scope": "today"}, rows[0]["tool_params"])
        self.assertEqual("2026-08-08 01:00:00", rows[0]["last_run"])
        self.assertIn("WHERE enabled=TRUE", cursor.calls[0][0])
        self.assertTrue(connection.closed)

    def test_workflow_resource_record_decodes_config(self):
        cursor = _Cursor(
            row={
                "resource_key": "phase7.source",
                "config_json": '{"table_id":"tbl"}',
                "source": "manual",
                "updated_at": datetime(2026, 8, 8, 1, 0, 0),
                "created_at": datetime(2026, 8, 1, 1, 0, 0),
            }
        )
        repository = WorkflowResourceRepository(lambda: _Connection(cursor))

        row = repository.get_record("phase7.source")

        self.assertEqual({"table_id": "tbl"}, row["config"])
        self.assertEqual("2026-08-08 01:00:00", row["updated_at"])
        self.assertEqual(("phase7.source",), cursor.calls[0][1])

    def test_migration_runner_discovers_ordered_sql_without_loading_configuration(self):
        project_root = Path(__file__).resolve().parents[1]
        script_path = project_root / "agent" / "scripts" / "run_migrations.py"
        spec = importlib.util.spec_from_file_location("test_run_migrations", script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        migrations = module.discover_migrations(project_root / "agent" / "migrations")

        self.assertEqual(["001", "002"], [version for version, _ in migrations])
        for _, path in migrations:
            self.assertTrue(module.split_sql_statements(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
