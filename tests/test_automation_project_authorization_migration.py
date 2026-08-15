from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import re
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "agent" / "migrations" / "018_automation_project_authorization.sql"
RUNNER_PATH = ROOT / "agent" / "scripts" / "run_migrations.py"
MIGRATION_HELPER_PATH = ROOT / "agent" / "scripts" / "migration_018_authorization.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("migration_runner_018_test", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __init__(self, *, applied: bool = False) -> None:
        self.applied = applied
        self._row = None
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.calls.append((normalized, params))
        if "FROM schema_migrations WHERE version=%s" in normalized:
            self._row = {"version": "018"} if self.applied else None
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class AutomationProjectAuthorizationMigrationTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_sql_splits_and_reviewed_map_is_exact_and_complete(self):
        statements = self.runner.split_sql_statements(self.sql)
        self.assertGreater(len(statements), 1)
        values_section = self.sql.split(
            "INSERT INTO automation_project_reviewed_schedule_map_018", 1
        )[1].split("ON DUPLICATE KEY UPDATE", 1)[0]
        self.assertEqual(70, values_section.count("('"))
        self.assertIn(
            "('customer_problems_shadow', 'sync_customer_service_problems', "
            "'customer_problems_shadow')",
            values_section,
        )
        self.assertIn(
            "('finance_bills_0010', 'sync_finance_bills', 'finance_bills')",
            values_section,
        )
        self.assertIn(
            "('finance_startup_catchup', 'sync_finance_bills', "
            "'finance_startup_catchup')",
            values_section,
        )

    def test_data_guards_and_capture_complete_before_core_alter(self):
        first_alter = self.sql.index("ALTER TABLE scheduled_tasks ADD COLUMN")
        self.assertLess(
            self.sql.index("cp018_preflight_identity_guard_stmt"),
            first_alter,
        )
        self.assertLess(
            self.sql.index("cp018_preflight_account_guard_stmt"),
            first_alter,
        )
        self.assertLess(
            self.sql.index("capture_state = 'CAPTURED'"),
            first_alter,
        )
        self.assertIn("BINARY JSON_UNQUOTE", self.sql)
        self.assertIn("cp018_scheduler_backup_incomplete", self.sql)
        self.assertIn("automation_id VARCHAR(128) NOT NULL", self.sql)

    def test_reviewed_resources_are_exactly_backed_up_and_hash_guarded(self):
        first_alter = self.sql.index("ALTER TABLE scheduled_tasks ADD COLUMN")
        resource_map = self.sql.split(
            "INSERT INTO automation_project_reviewed_resource_map_018", 1
        )[1].split("ON DUPLICATE KEY UPDATE", 1)[0]
        materialized = {
            "phase7.yunda_dispatch_forecast_bitable": "feishu_bitable",
            "phase7.yunda_send_waybills_bitable": "feishu_bitable",
            "phase7.yunda_send_waybills_sheet": "feishu_sheet",
            "phase7.self_pickup_source_sheet": "feishu_sheet",
            "phase7.split_pending_source_sheet": "feishu_sheet",
            "phase7.split_pending_target_sheet": "feishu_sheet",
        }
        required_existing = {
            "phase7.site_send_bitable": "feishu_bitable",
            "phase7.site_send_sheet": "feishu_sheet",
            "phase7.send_order_bitable": "feishu_bitable",
            "phase7.arrive_primary_sheet": "feishu_sheet",
            "phase7.arrive_secondary_sheet": "feishu_sheet",
            "phase7.pending_arrivals_sheet": "feishu_sheet",
            "phase7.stats_archive_sheet": "feishu_sheet",
            "phase7.daily_sign_bitable": "feishu_bitable",
            "phase7.daily_sign_sheet": "feishu_sheet",
        }
        reviewed_rows = {
            resource_key: (resource_kind, should_materialize == "TRUE")
            for resource_key, resource_kind, should_materialize in re.findall(
                r"\(\s*'(phase7\.[^']+)'\s*,\s*"
                r"'(feishu_(?:bitable|sheet))'\s*,\s*(TRUE|FALSE)\s*,",
                resource_map,
            )
        }
        self.assertEqual(
            reviewed_rows,
            {
                **{key: (kind, True) for key, kind in materialized.items()},
                **{key: (kind, False) for key, kind in required_existing.items()},
            },
        )
        for resource_key, resource_kind in materialized.items():
            self.assertRegex(
                resource_map,
                re.compile(
                    rf"\(\s*'{re.escape(resource_key)}'\s*,\s*"
                    rf"'{resource_kind}'\s*,\s*TRUE\s*,\s*JSON_OBJECT\(",
                    re.DOTALL,
                ),
            )
        for resource_key, resource_kind in required_existing.items():
            self.assertRegex(
                resource_map,
                re.compile(
                    rf"\(\s*'{re.escape(resource_key)}'\s*,\s*"
                    rf"'{resource_kind}'\s*,\s*FALSE\s*,\s*NULL\s*\)",
                    re.DOTALL,
                ),
            )
        pre_ddl_resource_guard = self.sql.split(
            "SET @cp018_required_existing_resource_invalid_count", 1
        )[1].split("SET @cp018_unreviewed_schedule_count", 1)[0]
        post_materialization_guard = self.sql.split(
            "SET @cp018_invalid_reviewed_resource_count", 1
        )[1].split("SET @cp018_reviewed_resource_shape_guard_sql", 1)[0]
        for resource_key in required_existing:
            self.assertIn(resource_key, pre_ddl_resource_guard)
            self.assertIn(resource_key, post_materialization_guard)
        for required_field in (
            "'base_token'",
            "'table_id'",
            "'spreadsheet_token'",
            "'sheet_id'",
            "'sheet_range'",
            "'clear_range'",
        ):
            self.assertIn(required_field, resource_map)
        for required_existing_field in (
            "$.snapshot_range",
            "$.default_write_range",
            "$.source_snapshot_range",
        ):
            self.assertIn(required_existing_field, self.sql)
        self.assertLess(
            self.sql.index("cp018_required_existing_resource_guard_stmt"),
            first_alter,
        )
        self.assertLess(
            self.sql.index("CREATE TABLE IF NOT EXISTS automation_project_resource_backup_018"),
            first_alter,
        )
        self.assertLess(
            self.sql.index("cp018_resource_backup_guard_stmt"),
            first_alter,
        )
        self.assertIn(
            "ON BINARY resource.resource_key = BINARY reviewed.resource_key",
            self.sql,
        )
        self.assertIn("migration_config_sha256", self.sql)
        self.assertIn("reviewed.materialize_missing = TRUE", self.sql)
        self.assertIn("cp018_resource_partial_drift_guard_stmt", self.sql)
        self.assertIn("cp018_reviewed_resource_shape_guard_stmt", self.sql)
        self.assertIn("cp018_resource_capture_guard_stmt", self.sql)
        self.assertIn("@cp018_reviewed_resource_count = 15", self.sql)
        self.assertIn("@cp018_resource_backup_count = 15", self.sql)

    def test_plugin_instance_schema_matches_closed_trust_and_worker_contract(self):
        for required in (
            "automation_plugin_packages",
            "automation_plugin_versions",
            "automation_projects",
            "automation_project_configs",
            "automation_project_policies",
            "automation_project_approval_batches",
            "automation_worker_devices",
            "automation_worker_pairing_events",
            "dispatch_sequence BIGINT UNSIGNED NOT NULL DEFAULT 0",
            "inbound_sequence BIGINT UNSIGNED NULL",
            "last_inbound_envelope_sha256 CHAR(64) NULL",
            "automation_worker_jobs",
            "automation_generation BIGINT UNSIGNED NOT NULL",
            "target_device_id VARCHAR(128) NOT NULL",
            "dispatch_message_id CHAR(36) NULL",
            "dispatch_authorization_id CHAR(36) NULL",
            "dispatch_envelope_json JSON NULL",
            "automation_worker_job_messages",
            "dispatch_message_id CHAR(36) NOT NULL",
            "dispatch_authorization_id CHAR(36) NOT NULL",
            "automation_worker_cleanup_directives",
            "automation_plugin_purge_journal",
            "deadline_at DATETIME(6) NOT NULL",
            "max_attempts INT UNSIGNED NOT NULL DEFAULT 1",
            "'OUTCOME_UNKNOWN'",
            "'BLOCKED_DATA'",
            "'ed25519_upload'",
            "'ed25519_first_party'",
            "'builtin_release'",
            "enabled = FALSE OR state IN ('ENABLED', 'UPGRADING')",
        ):
            self.assertIn(required, self.sql)

    def test_status_distinguishes_clean_dirty_and_applied_without_row_data(self):
        cases = (
            (False, set(), "pending_clean"),
            (False, {"automation_projects"}, "pending_dirty"),
            (True, {"automation_projects"}, "applied"),
        )
        for applied, artifacts, expected in cases:
            with self.subTest(expected=expected):
                cursor = _Cursor(applied=applied)
                connection = _Connection(cursor)
                with (
                    patch.object(self.runner, "_connect", return_value=connection),
                    patch.object(self.runner, "_require_mysql8"),
                    patch.object(self.runner, "_migration_table_exists", return_value=True),
                    patch.object(
                        self.runner,
                        "_automation_project_authorization_artifacts",
                        return_value=artifacts,
                    ),
                    patch("builtins.print") as print_mock,
                ):
                    result = self.runner.report_automation_project_authorization_status()
                self.assertEqual(0, result)
                print_mock.assert_called_once_with(
                    f"automation_project_authorization_status={expected}"
                )
                self.assertTrue(connection.closed)

    def test_resource_restore_is_restartable_and_drops_only_captured_rows(self):
        calls = []

        class Cursor:
            def execute(self, sql, params=None):
                calls.append((" ".join(str(sql).split()), params))

        runtime = {
            "AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE": (
                "automation_project_resource_backup_018"
            ),
            "_table_exists": lambda _cursor, table: (
                table == "automation_project_resource_backup_018"
            ),
            "_column_exists": lambda _cursor, table, column: (
                table == "workflow_resources"
                and column in {"configuration_version", "config_sha256"}
            ),
        }
        self.runner._MIGRATION_018_HELPER._restore_automation_project_resources(
            runtime,
            Cursor(),
        )
        self.assertEqual(len(calls), 3)
        self.assertIn(
            "ON BINARY backup.resource_key = BINARY resource.resource_key",
            calls[0][0],
        )
        self.assertIn("WHERE backup.existed_before = TRUE", calls[0][0])
        self.assertIn("WHERE backup.existed_before = FALSE", calls[1][0])
        self.assertIn("SET migration_config_sha256 = NULL", calls[2][0])

    def test_restore_validation_accepts_complete_fifteen_resource_capture(self):
        class Cursor:
            def __init__(self) -> None:
                self._row = None

            def execute(self, sql, params=None):
                normalized = " ".join(str(sql).split())
                if "SUM(migration_config_sha256 IS NOT NULL)" in normalized:
                    self._row = {"row_count": 15, "captured_count": 15}
                elif "AS changed_count" in normalized:
                    self._row = {"changed_count": 0}
                else:
                    self._row = None

            def fetchone(self):
                return self._row

        resource_backup_table = "automation_project_resource_backup_018"
        runtime = {
            "AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE": (
                "scheduled_tasks_backup_018"
            ),
            "AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE": (
                "scheduled_tasks_capture_018"
            ),
            "AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE": (
                resource_backup_table
            ),
            "AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_RESOURCE_MAP_TABLE": (
                "automation_project_reviewed_resource_map_018"
            ),
            "_table_exists": lambda _cursor, table: table == resource_backup_table,
            "_column_exists": lambda _cursor, table, column: (
                table == "workflow_resources"
                and column in {"configuration_version", "config_sha256"}
            ),
        }

        self.assertFalse(
            self.runner._MIGRATION_018_HELPER._validate_automation_project_authorization_restore(
                runtime,
                Cursor(),
            )
        )

    def test_restore_validation_empty_resource_backup_state_matrix(self):
        class Cursor:
            def __init__(self, capture_state: str | None) -> None:
                self.capture_state = capture_state
                self._row = None

            def execute(self, sql, params=None):
                normalized = " ".join(str(sql).split())
                if "FROM scheduled_tasks_capture_018" in normalized:
                    self._row = (
                        {
                            "capture_state": self.capture_state,
                            "source_row_count": 0,
                        }
                        if self.capture_state
                        else None
                    )
                elif "SUM(migration_config_sha256 IS NOT NULL)" in normalized:
                    self._row = {"row_count": 0, "captured_count": 0}
                else:
                    self._row = None

            def fetchone(self):
                return self._row

        cases = (
            ("not_started", None, set(), None),
            ("capture_started", "CAPTURING", set(), "empty after migration start"),
            (
                "schedule_column",
                None,
                {("scheduled_tasks", "automation_id")},
                "identity column exists without a complete capture marker",
            ),
            (
                "resource_version_column",
                None,
                {("workflow_resources", "configuration_version")},
                "empty after migration start",
            ),
            (
                "resource_hash_column",
                None,
                {("workflow_resources", "config_sha256")},
                "empty after migration start",
            ),
        )
        for name, capture_state, columns, expected_error in cases:
            with self.subTest(name=name):
                tables = {"automation_project_resource_backup_018"}
                if capture_state:
                    tables.add("scheduled_tasks_capture_018")
                runtime = {
                    "AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE": (
                        "scheduled_tasks_backup_018"
                    ),
                    "AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE": (
                        "scheduled_tasks_capture_018"
                    ),
                    "AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE": (
                        "automation_project_resource_backup_018"
                    ),
                    "AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_RESOURCE_MAP_TABLE": (
                        "automation_project_reviewed_resource_map_018"
                    ),
                    "_table_exists": lambda _cursor, table: table in tables,
                    "_column_exists": (
                        lambda _cursor, table, column: (table, column) in columns
                    ),
                }
                if expected_error is None:
                    self.assertFalse(
                        self.runner._MIGRATION_018_HELPER._validate_automation_project_authorization_restore(
                            runtime,
                            Cursor(capture_state),
                        )
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        self.runner._MIGRATION_018_HELPER._validate_automation_project_authorization_restore(
                            runtime,
                            Cursor(capture_state),
                        )

    def test_restore_accepts_post_restore_resource_backup_residue(self):
        class Cursor:
            def __init__(self) -> None:
                self._row = None

            def execute(self, sql, params=None):
                normalized = " ".join(str(sql).split())
                if "SUM(migration_config_sha256 IS NOT NULL)" in normalized:
                    self._row = {"row_count": 15, "captured_count": 0}
                elif "AS changed_count" in normalized:
                    self._row = {"changed_count": 0}
                else:
                    self._row = None

            def fetchone(self):
                return self._row

        resource_backup_table = "automation_project_resource_backup_018"
        runtime = {
            "AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE": (
                "scheduled_tasks_backup_018"
            ),
            "AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE": (
                "scheduled_tasks_capture_018"
            ),
            "AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE": (
                resource_backup_table
            ),
            "AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_RESOURCE_MAP_TABLE": (
                "automation_project_reviewed_resource_map_018"
            ),
            "_table_exists": lambda _cursor, table: table == resource_backup_table,
            "_column_exists": lambda _cursor, _table, _column: False,
        }
        self.assertFalse(
            self.runner._MIGRATION_018_HELPER._validate_automation_project_authorization_restore(
                runtime,
                Cursor(),
            )
        )

    def test_restore_clears_capture_marker_before_scheduler_backup(self):
        helper_source = MIGRATION_HELPER_PATH.read_text(encoding="utf-8")
        restore_body = helper_source.split(
            "def restore_automation_project_authorization", 1
        )[1]
        resource_restore = restore_body.index(
            "_restore_automation_project_resources(runtime, cursor)"
        )
        scheduler_restore = restore_body.index("INSERT INTO scheduled_tasks")
        final_cleanup = restore_body.index("for table_name in (")
        cleanup_body = restore_body[final_cleanup:]
        capture_drop = cleanup_body.index(
            'runtime["AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE"]'
        )
        scheduler_backup_drop = cleanup_body.index(
            'runtime["AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE"]'
        )
        self.assertLess(resource_restore, final_cleanup)
        self.assertLess(scheduler_restore, final_cleanup)
        self.assertLess(capture_drop, scheduler_backup_drop)

    def test_applied_migration_014_bytes_are_unchanged(self):
        digest = hashlib.sha256(
            (ROOT / "agent" / "migrations" / "014_control_plane_task_cutover.sql").read_bytes()
        ).hexdigest()
        self.assertEqual(
            "4b447a7c139980369c61eb9c2c5e250a974452b8c80036a1bce0f04a95a4fcdf",
            digest,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
