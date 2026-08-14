from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


def _load_runner():
    project_root = Path(__file__).resolve().parents[1]
    script_path = project_root / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("test_mysql8_migration_runner", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ScheduledTaskPolicyMigrationContractTests(unittest.TestCase):
    def test_current_policy_cascades_but_immutable_events_do_not_reference_tasks(self):
        project_root = Path(__file__).resolve().parents[1]
        migration_sql = (
            project_root
            / "agent"
            / "migrations"
            / "015_scheduled_task_approval_policies.sql"
        ).read_text(encoding="utf-8")
        normalized = " ".join(migration_sql.split())

        policy_table_sql = migration_sql.split(
            "CREATE TABLE IF NOT EXISTS scheduled_task_approval_policies",
            maxsplit=1,
        )[1].split(
            ") ENGINE=InnoDB",
            maxsplit=1,
        )[0]
        self.assertNotIn("FOREIGN KEY", policy_table_sql)
        event_table_sql = migration_sql.split(
            "CREATE TABLE IF NOT EXISTS scheduled_task_approval_policy_events",
            maxsplit=1,
        )[1].split(
            ") ENGINE=InnoDB",
            maxsplit=1,
        )[0]
        self.assertNotIn("FOREIGN KEY", event_table_sql)
        self.assertIn(
            "ALTER TABLE scheduled_task_approval_policy_events DROP FOREIGN KEY "
            "fk_scheduled_task_policy_event_task",
            normalized,
        )
        self.assertIn(
            "ALTER TABLE scheduled_task_approval_policies DROP FOREIGN KEY "
            "fk_scheduled_task_policy_task",
            normalized,
        )
        self.assertLess(
            migration_sql.index("cp015_drop_policy_task_fk"),
            migration_sql.index("cp015_modify_policy_task_id_stmt"),
        )
        self.assertIn(
            "SELECT CHARACTER_SET_NAME FROM information_schema.COLUMNS",
            normalized,
        )
        self.assertIn(
            "SELECT COLLATION_NAME FROM information_schema.COLUMNS",
            normalized,
        )
        self.assertIn("cp015_invalid_parent_task_id_metadata", migration_sql)
        self.assertIn(
            "ADD CONSTRAINT fk_scheduled_task_policy_task FOREIGN KEY (task_id) "
            "REFERENCES scheduled_tasks (id) ON DELETE CASCADE",
            normalized,
        )


class _MigrationCursor:
    def __init__(self, version: str, *, migration_table_exists: bool = False) -> None:
        self.version = version
        self.migration_table_exists = migration_table_exists
        self.calls: list[tuple[str, object]] = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if normalized == "SELECT VERSION() AS version":
            self._row = {"version": self.version}
        elif "FROM information_schema.TABLES" in normalized:
            self._row = {"exists": 1} if self.migration_table_exists else None
        else:
            self._row = None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _MigrationConnection:
    def __init__(self, cursor: _MigrationCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class _RestoreCursor:
    def __init__(
        self,
        *,
        applied: bool = True,
        backup_exists: bool = True,
        fail_restore: bool = False,
    ) -> None:
        self.applied = applied
        self.backup_exists = backup_exists
        self.fail_restore = fail_restore
        self.calls: list[tuple[str, object]] = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        if normalized == "SELECT VERSION() AS version":
            self._row = {"version": "8.0.44"}
        elif "FROM information_schema.TABLES" in normalized:
            table_name = params[0] if params else ""
            exists = table_name != "control_plane_task_cutover_backup_014" or self.backup_exists
            self._row = {"exists": 1} if exists else None
        elif normalized.startswith("SELECT version FROM schema_migrations"):
            self._row = {"version": "014"} if self.applied else None
        elif normalized.startswith("SELECT 1 FROM schema_migrations"):
            self._row = {"exists": 1} if self.applied else None
        elif normalized.startswith("INSERT INTO scheduled_tasks") and self.fail_restore:
            raise RuntimeError("injected restore failure")
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _RestoreConnection:
    def __init__(self, cursor: _RestoreCursor) -> None:
        self._cursor = cursor
        self.begun = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def begin(self):
        self.begun = True

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _WindowCursor:
    def __init__(
        self,
        rows: list[dict],
        *,
        policy_exists: bool,
        candidate_rows: list[dict] | None = None,
    ) -> None:
        self.rows = rows
        self.candidate_rows = rows if candidate_rows is None else candidate_rows
        self.policy_exists = policy_exists
        self.calls: list[tuple[str, object]] = []
        self._row = None
        self._rows: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self._row = None
        self._rows = []
        if normalized == "SELECT VERSION() AS version":
            self._row = {"version": "8.0.44"}
        elif "FROM information_schema.TABLES" in normalized:
            table_name = params[0] if params else ""
            exists = table_name == "scheduled_tasks" or (
                table_name == "scheduled_task_approval_policies"
                and self.policy_exists
            )
            self._row = {"exists": 1} if exists else None
        elif normalized.startswith("SELECT policy.task_id"):
            self._rows = list(self.rows)
        elif normalized.startswith("SELECT id, tool_name, tool_params"):
            self._rows = list(self.candidate_rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _WindowConnection:
    def __init__(self, cursor: _WindowCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class MigrationRunnerMySQLVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()

    def _run(self, version: str, *, check_only: bool):
        cursor = _MigrationCursor(version)
        connection = _MigrationConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            patch.object(self.runner, "discover_migrations", return_value=[]),
        ):
            result = self.runner.run(check_only=check_only)
        return result, connection, cursor

    def test_apply_checks_mysql8_before_creating_migration_history(self):
        result, connection, cursor = self._run("8.0.43-0ubuntu0.24.04.1", check_only=False)

        self.assertEqual(0, result)
        self.assertTrue(connection.closed)
        self.assertEqual("SELECT VERSION() AS version", cursor.calls[0][0])
        self.assertTrue(cursor.calls[1][0].startswith("CREATE TABLE IF NOT EXISTS schema_migrations"))

    def test_check_mode_checks_mysql8_before_reading_migration_history(self):
        result, connection, cursor = self._run("8.4.6", check_only=True)

        self.assertEqual(0, result)
        self.assertTrue(connection.closed)
        self.assertEqual("SELECT VERSION() AS version", cursor.calls[0][0])
        self.assertIn("FROM information_schema.TABLES", cursor.calls[1][0])
        self.assertFalse(
            any(
                "CREATE TABLE" in sql or "INSERT INTO schema_migrations" in sql
                for sql, _ in cursor.calls
            )
        )

    def test_unsupported_servers_fail_before_any_history_access_or_ddl(self):
        for version in (
            "5.7.44-log",
            "8.0.15",
            "8.0.0",
            "8",
            "9.0.1",
            "10.11.8-MariaDB-0ubuntu0.24.04.1",
        ):
            for check_only in (False, True):
                with self.subTest(version=version, check_only=check_only):
                    cursor = _MigrationCursor(version)
                    connection = _MigrationConnection(cursor)
                    with (
                        patch.object(self.runner, "_connect", return_value=connection),
                        patch.object(self.runner, "discover_migrations", return_value=[]),
                        self.assertRaisesRegex(RuntimeError, "requires MySQL 8"),
                    ):
                        self.runner.run(check_only=check_only)

                    self.assertTrue(connection.closed)
                    self.assertEqual([("SELECT VERSION() AS version", None)], cursor.calls)

    def test_control_plane_task_restore_is_fixed_transactional_and_removes_014_history(self):
        cursor = _RestoreCursor()
        connection = _RestoreConnection(cursor)
        with patch.object(self.runner, "_connect", return_value=connection):
            result = self.runner.restore_control_plane_task_cutover()

        self.assertEqual(0, result)
        self.assertTrue(connection.begun)
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertTrue(connection.closed)
        restore_calls = [
            (sql, params)
            for sql, params in cursor.calls
            if sql.startswith("INSERT INTO scheduled_tasks")
        ]
        self.assertEqual(1, len(restore_calls))
        self.assertIsNone(restore_calls[0][1])
        self.assertIn("FROM control_plane_task_cutover_backup_014", restore_calls[0][0])
        self.assertIn("ON DUPLICATE KEY UPDATE", restore_calls[0][0])
        seed_cleanup_calls = [
            (sql, params)
            for sql, params in cursor.calls
            if sql.startswith("DELETE seeded FROM scheduled_tasks AS seeded")
        ]
        self.assertEqual(1, len(seed_cleanup_calls))
        cleanup_sql, cleanup_params = seed_cleanup_calls[0]
        self.assertIn(
            "INNER JOIN schema_migrations AS migration ON migration.version=%s",
            cleanup_sql,
        )
        self.assertIn(
            "LEFT JOIN control_plane_task_cutover_backup_014 AS backup "
            "ON backup.id=seeded.id",
            cleanup_sql,
        )
        self.assertIn("seeded.created_at >= migration.applied_at", cleanup_sql)
        self.assertIn("backup.id IS NULL", cleanup_sql)
        self.assertEqual("014", cleanup_params[0])
        self.assertEqual(
            set(self.runner._load_control_plane_seed_task_ids()),
            set(cleanup_params[1:]),
        )
        self.assertEqual(55, len(cleanup_params[1:]))
        marker_cleanup_calls = [
            (sql, params)
            for sql, params in cursor.calls
            if sql.startswith("DELETE created_task FROM scheduled_tasks AS created_task")
        ]
        self.assertEqual(1, len(marker_cleanup_calls))
        self.assertIn(
            "INNER JOIN control_plane_task_cutover_created_014 AS marker",
            marker_cleanup_calls[0][0],
        )
        self.assertIn("backup.id IS NULL", marker_cleanup_calls[0][0])
        self.assertIn(
            ("DELETE FROM schema_migrations WHERE version=%s", ("014",)),
            cursor.calls,
        )

    def test_control_plane_seed_cleanup_set_is_exact_and_code_owned(self):
        from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES

        seed_ids = set(self.runner._load_control_plane_seed_task_ids())
        reviewed_ids = set(
            self.runner._load_control_plane_reviewed_task_contracts()
        )

        self.assertEqual(51, len(reviewed_ids))
        self.assertEqual(
            {
                "customer_problems_shadow",
                "finance_bills_0010",
                "finance_startup_catchup",
                "yunda_dispatch_forecast_1700",
            },
            self.runner.CONTROL_PLANE_STATIC_SEED_TASK_IDS,
        )
        self.assertEqual(
            reviewed_ids | self.runner.CONTROL_PLANE_STATIC_SEED_TASK_IDS,
            seed_ids,
        )
        self.assertEqual(55, len(seed_ids))
        self.assertEqual(
            {str(task["id"]) for task in PHASE7_SCHEDULED_TASK_TEMPLATES},
            seed_ids,
        )
        self.assertNotIn("clockin_daxiang_1830", seed_ids)
        self.assertNotIn("clockin_daxiang_s_1833", seed_ids)

    def test_control_plane_task_restore_rolls_back_without_hiding_failure(self):
        cursor = _RestoreCursor(fail_restore=True)
        connection = _RestoreConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            self.assertRaisesRegex(RuntimeError, "injected restore failure"),
        ):
            self.runner.restore_control_plane_task_cutover()

        self.assertTrue(connection.begun)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_control_plane_task_restore_skips_if_backup_was_never_created(self):
        cursor = _RestoreCursor(backup_exists=False)
        connection = _RestoreConnection(cursor)
        with patch.object(self.runner, "_connect", return_value=connection):
            result = self.runner.restore_control_plane_task_cutover()

        self.assertEqual(0, result)
        self.assertTrue(connection.begun)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(
            any(sql.startswith("INSERT INTO scheduled_tasks") for sql, _ in cursor.calls)
        )
        self.assertTrue(connection.closed)

    def test_control_plane_task_restore_handles_partial_014_without_history_row(self):
        cursor = _RestoreCursor(applied=False)
        connection = _RestoreConnection(cursor)
        with patch.object(self.runner, "_connect", return_value=connection):
            result = self.runner.restore_control_plane_task_cutover()

        self.assertEqual(0, result)
        self.assertTrue(connection.begun)
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertTrue(
            any(sql.startswith("INSERT INTO scheduled_tasks") for sql, _ in cursor.calls)
        )
        self.assertIn(
            ("DELETE FROM schema_migrations WHERE version=%s", ("014",)),
            cursor.calls,
        )
        self.assertIn(
            ("DELETE FROM control_plane_task_cutover_backup_014", None),
            cursor.calls,
        )
        self.assertTrue(connection.closed)

    def test_control_plane_task_status_reports_only_fixed_state(self):
        for applied, expected in (
            (True, "control_plane_task_cutover_status=applied"),
            (False, "control_plane_task_cutover_status=pending_clean"),
        ):
            with self.subTest(applied=applied):
                cursor = _RestoreCursor(applied=applied)
                connection = _RestoreConnection(cursor)
                with (
                    patch.object(self.runner, "_connect", return_value=connection),
                    patch("builtins.print") as print_mock,
                ):
                    result = self.runner.report_control_plane_task_cutover_status()

                self.assertEqual(0, result)
                print_mock.assert_called_once_with(expected)
                self.assertTrue(connection.closed)

    def test_control_plane_task_status_detects_unrecovered_partial_attempt(self):
        cursor = _RestoreCursor(applied=False)
        connection = _RestoreConnection(cursor)

        original_execute = cursor.execute

        def execute(sql, params=None):
            original_execute(sql, params)
            if "FROM control_plane_task_cutover_backup_014 LIMIT 1" in " ".join(sql.split()):
                cursor._row = {"exists": 1}

        cursor.execute = execute
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            patch("builtins.print") as print_mock,
        ):
            result = self.runner.report_control_plane_task_cutover_status()

        self.assertEqual(0, result)
        print_mock.assert_called_once_with(
            "control_plane_task_cutover_status=pending_dirty"
        )

    def test_scheduled_write_window_uses_shanghai_daily_boundaries(self):
        zone = ZoneInfo("Asia/Shanghai")
        is_active = self.runner._is_within_daily_schedule_window

        self.assertTrue(
            is_active(
                datetime(2026, 8, 14, 17, 30, tzinfo=zone),
                "30 18 * * *",
                before_minutes=60,
                after_minutes=45,
            )
        )
        self.assertTrue(
            is_active(
                datetime(2026, 8, 14, 19, 15, tzinfo=zone),
                "30 18 * * *",
                before_minutes=60,
                after_minutes=45,
            )
        )
        self.assertFalse(
            is_active(
                datetime(2026, 8, 14, 19, 16, tzinfo=zone),
                "30 18 * * *",
                before_minutes=60,
                after_minutes=45,
            )
        )
        self.assertTrue(
            is_active(
                datetime(2026, 8, 14, 23, 30, tzinfo=zone),
                "15 0 * * *",
                before_minutes=60,
                after_minutes=45,
            )
        )

    def test_scheduled_write_snapshot_must_match_active_task_binding(self):
        snapshot = {
            "task_id": "external_write_1830",
            "tool_name": "exact_external_write",
            "operation_type": "external_write",
            "cron_expression": "30 18 * * *",
            "enabled": True,
        }
        row = {
            "task_id": "external_write_1830",
            "mode": "EXACT_SCHEDULE_EXEMPT",
            "contract_snapshot_json": json.dumps(snapshot),
            "tool_name": "exact_external_write",
            "cron_expression": "30 18 * * *",
            "enabled": 1,
        }

        self.assertEqual(
            "30 18 * * *",
            self.runner._scheduled_write_snapshot_cron(row),
        )
        for field, value in (
            ("tool_name", "changed_tool"),
            ("cron_expression", "31 18 * * *"),
        ):
            changed = dict(row)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(
                self.runner.ControlPlaneTaskCutoverPreflightError
            ) as error:
                self.runner._scheduled_write_snapshot_cron(changed)
            self.assertEqual("SCHEDULED_WRITE_BINDING_INVALID", error.exception.code)

        disabled = dict(row)
        disabled["enabled"] = 0
        self.assertIsNone(self.runner._scheduled_write_snapshot_cron(disabled))

    def test_scheduled_write_window_policy_path_is_read_only_and_redacted(self):
        task_id = "sensitive-task-id"
        row = {
            "task_id": task_id,
            "mode": "EXACT_SCHEDULE_EXEMPT",
            "contract_snapshot_json": json.dumps(
                {
                    "task_id": task_id,
                    "tool_name": "external_write_tool",
                    "operation_type": "external_write",
                    "cron_expression": "30 18 * * *",
                    "enabled": True,
                    "arguments": {"secret": "TASK_PARAM_SECRET_SENTINEL"},
                }
            ),
            "tool_name": "external_write_tool",
            "cron_expression": "30 18 * * *",
            "enabled": 1,
        }
        cursor = _WindowCursor([row], policy_exists=True)
        connection = _WindowConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            patch("builtins.print") as print_mock,
        ):
            result = self.runner.check_scheduled_write_window(
                before_minutes=60,
                after_minutes=45,
                now=datetime(
                    2026,
                    8,
                    14,
                    18,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

        self.assertEqual(1, result)
        self.assertTrue(connection.closed)
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))
        rendered = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertIn("SCHEDULED_WRITE_WINDOW_ACTIVE", rendered)
        self.assertNotIn(task_id, rendered)
        self.assertNotIn("TASK_PARAM_SECRET_SENTINEL", rendered)

    def test_scheduled_write_window_legacy_path_uses_exact_clock_and_r7_rows(self):
        internal = self.runner._load_control_plane_reviewed_task_contracts()
        clocks = self.runner._load_control_plane_clock_contracts()
        r7_contracts = self.runner._load_control_plane_r7_contracts()
        rows = [
            {
                "id": task_id,
                "tool_name": contract["tool_name"],
                "tool_params": dict(contract["canonical_arguments"]),
                "cron_expression": contract["cron_expression"],
                "enabled": 1,
            }
            for task_id, contract in sorted(internal.items())
        ]
        rows.extend(
            {
                "id": task_id,
                "tool_name": "tms_query",
                "tool_params": self.runner._legacy_clock_arguments(
                    task_id,
                    contract["canonical_arguments"],
                ),
                "cron_expression": contract["cron_expression"],
                "enabled": 1,
            }
            for task_id, contract in sorted(clocks.items())
        )
        rows.extend(
            {
                "id": task_id,
                "tool_name": contract["tool_name"],
                "tool_params": dict(contract["canonical_arguments"]),
                "cron_expression": contract["cron_expression"],
                "enabled": 1,
            }
            for task_id, contract in sorted(r7_contracts.items())
        )
        cursor = _WindowCursor(rows, policy_exists=False)
        connection = _WindowConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            patch("builtins.print") as print_mock,
        ):
            result = self.runner.check_scheduled_write_window(
                before_minutes=60,
                after_minutes=45,
                now=datetime(
                    2026,
                    8,
                    14,
                    3,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

        self.assertEqual(0, result)
        print_mock.assert_called_once_with(
            "scheduled_write_window=ok checked_schedules=15"
        )

    def test_policy_tables_do_not_hide_legacy_external_window_after_source_rollback(self):
        clocks = self.runner._load_control_plane_clock_contracts()
        r7_contracts = self.runner._load_control_plane_r7_contracts()
        clock_rows = [
            {
                "id": task_id,
                "tool_name": contract["tool_name"],
                "tool_params": dict(contract["canonical_arguments"]),
                "cron_expression": contract["cron_expression"],
                "enabled": 1,
            }
            for task_id, contract in sorted(clocks.items())
        ]
        r7_rows = [
            {
                "id": task_id,
                "tool_name": contract["tool_name"],
                "tool_params": dict(contract["canonical_arguments"]),
                "cron_expression": contract["cron_expression"],
                "enabled": 1,
            }
            for task_id, contract in sorted(r7_contracts.items())
        ]
        cursor = _WindowCursor(
            [],
            policy_exists=True,
            candidate_rows=clock_rows + r7_rows,
        )
        connection = _WindowConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            patch("builtins.print") as print_mock,
        ):
            result = self.runner.check_scheduled_write_window(
                before_minutes=60,
                after_minutes=45,
                now=datetime(
                    2026,
                    8,
                    14,
                    12,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

        self.assertEqual(1, result)
        rendered = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertIn("SCHEDULED_WRITE_WINDOW_ACTIVE", rendered)


if __name__ == "__main__":
    unittest.main()
