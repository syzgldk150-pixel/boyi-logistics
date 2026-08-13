from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_runner():
    project_root = Path(__file__).resolve().parents[1]
    script_path = project_root / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("test_mysql8_migration_runner", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
        for version in ("5.7.44-log", "9.0.1", "10.11.8-MariaDB-0ubuntu0.24.04.1"):
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
        self.assertIn(
            ("DELETE FROM schema_migrations WHERE version=%s", ("014",)),
            cursor.calls,
        )

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


if __name__ == "__main__":
    unittest.main()
