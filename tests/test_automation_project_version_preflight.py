from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_preflight():
    script_path = (
        PROJECT_ROOT
        / "agent"
        / "scripts"
        / "automation_project_version_preflight.py"
    )
    spec = importlib.util.spec_from_file_location(
        "test_automation_project_version_preflight",
        script_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_runner():
    script_path = PROJECT_ROOT / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location(
        "test_mysql8_migration_runner",
        script_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _RollbackExactSeedCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, object]] = []
        self._rows: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self._rows = []
        if normalized.startswith("SELECT project.automation_id"):
            expected_ids = set(params or ())
            self._rows = [
                row for row in self.rows if row.get("automation_id") in expected_ids
            ]

    def fetchall(self):
        return self._rows


class _RollbackExactSeedConnection:
    def __init__(self, cursor: _RollbackExactSeedCursor) -> None:
        self._cursor = cursor
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class RollbackExactSeedCompatibilityPreflightTests(unittest.TestCase):
    @staticmethod
    def _seed(automation_id: str, plugin_id: str, version: str) -> dict:
        return {
            "automation_id": automation_id,
            "plugin_id": plugin_id,
            "version": version,
        }

    def _run(self, payload: object, rows: list[dict]):
        preflight = _load_preflight()
        cursor = _RollbackExactSeedCursor(rows)
        connection = _RollbackExactSeedConnection(cursor)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "exact-seeds.json"
            serialized = payload if isinstance(payload, str) else json.dumps(payload)
            manifest_path.write_text(serialized, encoding="utf-8")
            with redirect_stdout(output):
                status = preflight.check_rollback_exact_seed_compatibility(
                    lambda: connection,
                    manifest_path,
                )
        return status, output.getvalue(), cursor, connection

    def test_equal_and_older_rows_are_allowed_without_trust_source_filtering(self):
        payload = {
            "seeds": [
                self._seed("arrival_sync", "arrival_plugin", "1.2.3"),
                self._seed("arrival_retry", "arrival_plugin", "2.0.0"),
            ]
        }
        rows = [
            {
                "automation_id": "arrival_sync",
                "plugin_id": "arrival_plugin",
                "version": "1.2.3",
                "trust_source": "uploaded_untrusted",
            },
            {
                "automation_id": "arrival_retry",
                "plugin_id": "arrival_plugin",
                "version": "1.9.9",
                "trust_source": "uploaded_untrusted",
            },
        ]
        status, output, cursor, connection = self._run(payload, rows)

        self.assertEqual(0, status)
        self.assertEqual(
            "rollback_exact_seed_compatibility=ok checked_seeds=2\n",
            output,
        )
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertEqual("START TRANSACTION READ ONLY", cursor.calls[0][0])
        query, params = cursor.calls[1]
        self.assertIn("FROM automation_projects AS project", query)
        self.assertNotIn("trust_source", query)
        self.assertEqual(("arrival_retry", "arrival_sync"), params)

    def test_newer_installed_version_is_blocked(self):
        payload = {"seeds": [self._seed("arrival_sync", "arrival_plugin", "1.2.3")]}
        rows = [
            {
                "automation_id": "arrival_sync",
                "plugin_id": "arrival_plugin",
                "version": "1.2.4",
            }
        ]
        status, output, _cursor, connection = self._run(payload, rows)

        self.assertEqual(1, status)
        self.assertEqual(
            "rollback_exact_seed_compatibility=blocked "
            "code=DATABASE_VERSION_NEWER\n",
            output,
        )
        self.assertTrue(connection.rolled_back)

    def test_invalid_database_semantic_version_is_blocked(self):
        payload = {"seeds": [self._seed("arrival_sync", "arrival_plugin", "1.2.3")]}
        rows = [
            {
                "automation_id": "arrival_sync",
                "plugin_id": "arrival_plugin",
                "version": "1.02.3",
            }
        ]
        status, output, _cursor, _connection = self._run(payload, rows)

        self.assertEqual(1, status)
        self.assertEqual(
            "rollback_exact_seed_compatibility=blocked "
            "code=DATABASE_VERSION_INVALID\n",
            output,
        )

    def test_missing_exact_project_is_allowed(self):
        payload = {"seeds": [self._seed("arrival_sync", "arrival_plugin", "1.2.3")]}
        status, output, _cursor, connection = self._run(payload, [])

        self.assertEqual(0, status)
        self.assertEqual(
            "rollback_exact_seed_compatibility=ok checked_seeds=1\n",
            output,
        )
        self.assertTrue(connection.closed)

    def test_plugin_mismatch_and_shared_plugin_instances_are_checked_per_id(self):
        payload = {
            "seeds": [
                self._seed("arrival_sync", "arrival_plugin", "1.2.3"),
                self._seed("arrival_retry", "arrival_plugin", "1.2.3"),
            ]
        }
        rows = [
            {
                "automation_id": "arrival_sync",
                "plugin_id": "arrival_plugin",
                "version": "1.2.3",
            },
            {
                "automation_id": "arrival_retry",
                "plugin_id": "different_plugin",
                "version": "1.2.3",
            },
        ]
        status, output, _cursor, _connection = self._run(payload, rows)

        self.assertEqual(1, status)
        self.assertEqual(
            "rollback_exact_seed_compatibility=blocked "
            "code=DATABASE_PLUGIN_MISMATCH\n",
            output,
        )

    def test_query_is_exact_and_ignores_unrelated_projects(self):
        payload = {"seeds": [self._seed("arrival_sync", "arrival_plugin", "1.2.3")]}
        rows = [
            {
                "automation_id": "arrival_sync",
                "plugin_id": "arrival_plugin",
                "version": "1.2.3",
            },
            {
                "automation_id": "unrelated_project",
                "plugin_id": "unrelated_plugin",
                "version": "9.9.9",
            },
        ]
        status, output, cursor, _connection = self._run(payload, rows)

        self.assertEqual(0, status)
        self.assertEqual(
            "rollback_exact_seed_compatibility=ok checked_seeds=1\n",
            output,
        )
        self.assertEqual(("arrival_sync",), cursor.calls[1][1])

    def test_malformed_manifest_is_blocked_before_database_access(self):
        payloads = (
            {"seeds": []},
            '{"seeds":[],"seeds":[]}',
            {
                "seeds": [
                    self._seed("arrival_sync", "arrival_plugin", "01.2.3"),
                ]
            },
            {
                "seeds": [
                    self._seed("arrival_sync", "arrival_plugin", "1.2.3"),
                    self._seed("arrival_sync", "arrival_plugin", "1.2.3"),
                ]
            },
            {
                "seeds": [
                    {
                        **self._seed("arrival_sync", "arrival_plugin", "1.2.3"),
                        "unexpected": True,
                    }
                ]
            },
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                status, output, cursor, connection = self._run(payload, [])

                self.assertEqual(1, status)
                self.assertEqual(
                    "rollback_exact_seed_compatibility=blocked code=MANIFEST_INVALID\n",
                    output,
                )
                self.assertEqual([], cursor.calls)
                self.assertFalse(connection.rolled_back)
                self.assertFalse(connection.closed)

    def test_runner_cli_binds_manifest_only_to_rollback_mode(self):
        runner = _load_runner()
        with (
            patch.object(
                runner,
                "check_rollback_exact_seed_compatibility",
                return_value=7,
            ) as check,
            patch.object(
                sys,
                "argv",
                [
                    "run_migrations.py",
                    "--check-rollback-exact-seed-compatibility",
                    "--rollback-exact-seed-manifest",
                    "/safe/release/exact-seeds.json",
                ],
            ),
        ):
            self.assertEqual(7, runner.main())
        check.assert_called_once_with(runner._connect, "/safe/release/exact-seeds.json")

        with patch.object(
            sys,
            "argv",
            [
                "run_migrations.py",
                "--rollback-exact-seed-manifest",
                "/safe/release/exact-seeds.json",
            ],
        ):
            with self.assertRaises(SystemExit):
                runner.main()
