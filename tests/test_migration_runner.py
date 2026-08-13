from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
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
        self.assertFalse(any("CREATE TABLE" in sql or "INSERT INTO schema_migrations" in sql for sql, _ in cursor.calls))

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


if __name__ == "__main__":
    unittest.main()
