from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import uuid
from unittest import TestCase
from unittest.mock import patch

from agent.automation_plugins.first_party import release_first_party_automation_ids
from agent.phase7_resource_import import BUILTIN_RESOURCES
from shared.automation_project_manifest import FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "agent" / "migrations" / "018_automation_project_authorization.sql"
RUNNER_PATH = ROOT / "agent" / "scripts" / "run_migrations.py"
RESOURCE_PREFLIGHT_PATH = (
    ROOT / "agent" / "scripts" / "automation_project_resource_preflight.py"
)
SCHEDULE_IDENTITY_PREFLIGHT_PATH = (
    ROOT
    / "agent"
    / "scripts"
    / "automation_project_schedule_identity_preflight.py"
)
MIGRATION_HELPER_PATH = ROOT / "agent" / "scripts" / "migration_018_authorization.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("migration_runner_018_test", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_resource_preflight():
    spec = importlib.util.spec_from_file_location(
        "automation_project_resource_preflight_test",
        RESOURCE_PREFLIGHT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_schedule_identity_preflight():
    spec = importlib.util.spec_from_file_location(
        "automation_project_schedule_identity_preflight_test",
        SCHEDULE_IDENTITY_PREFLIGHT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration_helper():
    spec = importlib.util.spec_from_file_location(
        "automation_project_authorization_migration_helper_test",
        MIGRATION_HELPER_PATH,
    )
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


class _ResourceDiagnosticCursor:
    def __init__(self, rows, *, fail_resource_key: str | None = None) -> None:
        self.rows = rows
        self.fail_resource_key = fail_resource_key
        self._row = None
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.calls.append((normalized, params))
        if normalized == "START TRANSACTION READ ONLY":
            self._row = None
            return
        if "FROM workflow_resources" not in normalized:
            raise AssertionError(f"unexpected diagnostic SQL: {normalized}")
        expected_kind, resource_key = params
        if resource_key == self.fail_resource_key:
            raise RuntimeError("simulated read failure")
        self._row = self.rows.get(resource_key)
        if self._row is not None:
            assert expected_kind in {"feishu_bitable", "feishu_sheet"}

    def fetchone(self):
        return self._row


class _ResourceDiagnosticConnection:
    def __init__(self, cursor: _ResourceDiagnosticCursor) -> None:
        self._cursor = cursor
        self.rollback_count = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


class _ScheduleIdentityCursor:
    def __init__(self, rows=(), *, applied=False, fail_query=False) -> None:
        self.rows = rows
        self.applied = applied
        self.fail_query = fail_query
        self._row = None
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.calls.append((normalized, params))
        if normalized == "START TRANSACTION READ ONLY":
            self._row = None
        elif normalized.startswith("SELECT 1 FROM schema_migrations"):
            self._row = {"applied": 1} if self.applied else None
        elif normalized.startswith("SELECT id, tool_name FROM scheduled_tasks"):
            if self.fail_query:
                raise RuntimeError("malicious\npassword=must-not-leak")
            self._row = None
        else:
            raise AssertionError(f"unexpected identity SQL: {normalized}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self.rows


class _ScheduleIdentityConnection:
    def __init__(self, cursor: _ScheduleIdentityCursor) -> None:
        self._cursor = cursor
        self.rollback_count = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


def _valid_diagnostic_row(resource_preflight, spec):
    row = {"resource_key": spec.resource_key}
    for field_name in (
        resource_preflight.AUTOMATION_PROJECT_REQUIRED_RESOURCE_FIELD_NAMES
    ):
        row[f"{field_name}_present"] = 0
        row[f"{field_name}_type"] = None
        row[f"{field_name}_nonempty"] = 0
    row["resource_kind_matches"] = 0
    selected_fields = [*spec.required_fields]
    selected_fields.extend(group[0] for group in spec.alternative_field_groups)
    for field_name in selected_fields:
        row[f"{field_name}_present"] = 1
        row[f"{field_name}_type"] = "STRING"
        row[f"{field_name}_nonempty"] = 1
    return row


class AutomationProjectAuthorizationMigrationTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = _load_runner()
        cls.resource_preflight = _load_resource_preflight()
        cls.schedule_identity_preflight = _load_schedule_identity_preflight()
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_sql_splits_and_reviewed_map_is_exact_and_complete(self):
        statements = self.runner.split_sql_statements(self.sql)
        self.assertGreater(len(statements), 1)
        values_section = self.sql.split(
            "INSERT INTO automation_project_reviewed_schedule_map_018", 1
        )[1].split("ON DUPLICATE KEY UPDATE", 1)[0]
        self.assertEqual(71, values_section.count("('"))
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
        self.assertIn(
            "('r7_departure_checkin', 'r7_departure_checkin', "
            "'r7_departure_checkin')",
            values_section,
        )
        sql_identities = {
            task_id: (tool_name, automation_id)
            for task_id, tool_name, automation_id in re.findall(
                r"\('([^']+)', '([^']+)', '([^']+)'\)",
                values_section,
            )
        }
        shared_identities = (
            self.schedule_identity_preflight.load_reviewed_schedule_identities()
        )
        self.assertEqual(71, len(shared_identities))
        self.assertEqual(sql_identities, shared_identities)
        self.assertEqual(
            ("r7_departure_checkin", "r7_departure_checkin"),
            shared_identities["r7_departure_checkin"],
        )
        self.assertIn(
            "WHEN BINARY id = BINARY 'r7_departure_checkin'",
            self.sql,
        )
        self.assertIn(
            "WHEN 'r7_departure_checkin' THEN NOT EXISTS",
            self.sql,
        )
        self.assertIn("updated_at = updated_at", self.sql)

    def test_schedule_identity_authority_restores_complete_shared_namespace(self):
        before = {
            name: module
            for name, module in sys.modules.items()
            if name == "shared" or name.startswith("shared.")
        }
        self.schedule_identity_preflight.load_reviewed_schedule_identities()
        after = {
            name: module
            for name, module in sys.modules.items()
            if name == "shared" or name.startswith("shared.")
        }
        self.assertEqual(set(before), set(after))
        self.assertTrue(all(after[name] is module for name, module in before.items()))

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
        materialized_configs = {
            resource_key: config
            for resource_key, config in BUILTIN_RESOURCES.items()
            if resource_key
            not in {
                "automation.feishu_route.r7_arrival_checkin",
                "automation.feishu_route.r7_departure_checkin",
            }
        }
        materialized = {
            resource_key: str(config["resource_kind"])
            for resource_key, config in materialized_configs.items()
        }
        required_existing = {
            "phase7.site_send_bitable": "feishu_bitable",
            "phase7.site_send_sheet": "feishu_sheet",
            "phase7.send_order_bitable": "feishu_bitable",
            "phase7.arrive_primary_sheet": "feishu_sheet",
            "phase7.arrive_secondary_sheet": "feishu_sheet",
            "phase7.stats_archive_sheet": "feishu_sheet",
            "phase7.daily_sign_bitable": "feishu_bitable",
            "phase7.daily_sign_sheet": "feishu_sheet",
        }
        reviewed_rows = {
            resource_key: (resource_kind, should_materialize == "TRUE")
            for resource_key, resource_kind, should_materialize in re.findall(
                r"\(\s*'((?:phase7|automation)\.[^']+)'\s*,\s*"
                r"'((?:feishu_(?:bitable|sheet|route)|webhook_route))'"
                r"\s*,\s*(TRUE|FALSE)\s*,",
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
        release_automation_ids = release_first_party_automation_ids()
        template_resource_keys: set[str] = set()
        for automation_id in release_automation_ids:
            template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[automation_id]
            for binding in template.resource_bindings.values():
                if isinstance(binding, tuple):
                    template_resource_keys.update(binding)
                else:
                    template_resource_keys.add(binding)
        self.assertEqual(len(release_automation_ids), 16)
        self.assertEqual(template_resource_keys, set(reviewed_rows))
        self.assertTrue(
            {
                "automation.feishu_route.r7_arrival_checkin",
                "automation.feishu_route.r7_departure_checkin",
            }.isdisjoint(template_resource_keys)
        )
        for resource_key, resource_kind in materialized.items():
            match = re.search(
                rf"\(\s*'{re.escape(resource_key)}'\s*,\s*"
                rf"'{resource_kind}'\s*,\s*TRUE\s*,\s*JSON_OBJECT\("
                rf"(?P<body>.*?)\)\s*\)",
                resource_map,
                re.DOTALL,
            )
            self.assertIsNotNone(match, resource_key)
            sql_config = dict(
                re.findall(
                    r"'([^']+)'\s*,\s*'([^']*)'",
                    match.group("body"),
                )
            )
            self.assertEqual(sql_config, materialized_configs[resource_key])
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
        self.assertEqual(
            set(materialized),
            set(
                self.resource_preflight.AUTOMATION_PROJECT_CODE_OWNED_RESOURCE_KEYS
            ),
        )
        self.assertEqual(len(materialized), 18)
        self.assertEqual(len(required_existing), 8)
        self.assertIn("@cp018_reviewed_resource_count = 26", self.sql)
        self.assertIn(
            "@cp018_resource_backup_count = 26 + "
            "@cp018_legacy_pending_backup_count",
            self.sql,
        )
        obsolete_delete = (
            "DELETE FROM automation_project_reviewed_resource_map_018\n"
            "WHERE BINARY resource_key = BINARY "
            "'phase7.pending_arrivals_sheet'"
        )
        self.assertIn(obsolete_delete, self.sql)
        self.assertLess(
            self.sql.index(obsolete_delete),
            self.sql.index("SET @cp018_reviewed_resource_count"),
        )
        self.assertIn(
            "@cp018_legacy_pending_backup_count IN (0, 1)",
            self.sql,
        )
        self.assertIn("cp018_legacy_pending_backup_guard_stmt", self.sql)
        self.assertIn("cp018_legacy_pending_partial_drift_count", self.sql)
        self.assertIn("cp018_resource_backup_missing_reviewed_count", self.sql)
        self.assertIn("cp018_resource_backup_unexpected_count", self.sql)
        self.assertIn(
            "cp018_resource_backup_hash_layout_guard_stmt",
            self.sql,
        )
        self.assertIn(
            "14 + @cp018_legacy_pending_backup_count",
            self.sql,
        )
        delivery_shape = post_materialization_guard.split(
            "BINARY 'phase7.delivery_status_bitable'",
            1,
        )[1].split(
            "BINARY 'phase7.yunda_dispatch_forecast_bitable'",
            1,
        )[0]
        self.assertIn("$.base_token", delivery_shape)
        self.assertIn("$.table_id", delivery_shape)
        self.assertIn("$.view_name", delivery_shape)
        self.assertIn("$.view_id", delivery_shape)
        self.assertIn(
            "TRIM(BOTH '/' FROM TRIM(JSON_UNQUOTE(JSON_EXTRACT(",
            post_materialization_guard,
        )
        webhook_shape = post_materialization_guard.split(
            "BINARY 'phase7.delivery_status_webhook'",
            1,
        )[1].split(
            "BINARY 'automation.feishu_route.arrive_list'",
            1,
        )[0]
        feishu_route_shape = post_materialization_guard.split(
            "BINARY 'automation.feishu_route.arrive_list'",
            1,
        )[1].split(
            "BINARY 'phase7.site_send_bitable'",
            1,
        )[0]
        self.assertIn("reviewed.default_config_json", webhook_shape)
        self.assertIn("reviewed.default_config_json", feishu_route_shape)
        self.assertGreaterEqual(
            post_materialization_guard.count("BETWEEN 1 AND 191"),
            2,
        )

    def test_resource_normalization_preserves_existing_updated_at(self):
        hash_backfill = self.sql.split(
            "UPDATE workflow_resources\nSET", 1
        )[1].split("WHERE config_sha256 IS NULL;", 1)[0]
        self.assertIn("updated_at = updated_at", hash_backfill)

        resource_kind_normalization = self.sql.split(
            "UPDATE workflow_resources AS resource", 1
        )[1].split(
            "WHERE JSON_EXTRACT(resource.config_json, '$.resource_kind') IS NULL;",
            1,
        )[0]
        self.assertIn(
            "resource.updated_at = resource.updated_at",
            resource_kind_normalization,
        )

    def test_required_resource_diagnostic_spec_matches_018_sql_guard(self):
        specs = (
            self.resource_preflight.AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS
        )
        self.assertEqual(8, len(specs))

        resource_map = self.sql.split(
            "INSERT INTO automation_project_reviewed_resource_map_018", 1
        )[1].split("ON DUPLICATE KEY UPDATE", 1)[0]
        sql_required_kinds = {
            resource_key: resource_kind
            for resource_key, resource_kind in re.findall(
                r"\(\s*'(phase7\.[^']+)'\s*,\s*"
                r"'(feishu_(?:bitable|sheet))'\s*,\s*FALSE\s*,\s*NULL\s*\)",
                resource_map,
                re.DOTALL,
            )
        }
        self.assertEqual(
            sql_required_kinds,
            {spec.resource_key: spec.expected_kind for spec in specs},
        )

        guard = self.sql.split(
            "SET @cp018_required_existing_resource_invalid_count", 1
        )[1].split("SET @cp018_required_existing_resource_guard_sql", 1)[0]
        shape_case = guard.split("OR NOT COALESCE(CASE", 1)[1].split(
            "ELSE FALSE", 1
        )[0]
        sql_shapes = {}
        for branch in re.finditer(
            r"\bWHEN\b(?P<condition>.*?)\bTHEN\b(?P<body>.*?)"
            r"(?=\n\s*WHEN\b|\Z)",
            shape_case,
            re.DOTALL,
        ):
            resource_keys = set(
                re.findall(r"'(phase7\.[^']+)'", branch.group("condition"))
            )
            field_names = set(
                re.findall(r"'\$\.([a-z0-9_]+)'", branch.group("body"))
            )
            for resource_key in resource_keys:
                self.assertNotIn(resource_key, sql_shapes)
                sql_shapes[resource_key] = (field_names, branch.group("body"))

        self.assertEqual({spec.resource_key for spec in specs}, set(sql_shapes))
        for spec in specs:
            actual_fields, branch_body = sql_shapes[spec.resource_key]
            expected_fields = set(spec.required_fields)
            for field_group in spec.alternative_field_groups:
                expected_fields.update(field_group)
                normalized_body = " ".join(branch_body.split())
                first = normalized_body.index(f"'$.{field_group[0]}'")
                second = normalized_body.index(f"'$.{field_group[1]}'")
                self.assertIn(" OR ", normalized_body[first:second])
            self.assertEqual(expected_fields, actual_fields, spec.resource_key)

    def test_required_resource_diagnostic_is_read_only_and_closed_on_success(self):
        specs = (
            self.resource_preflight.AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS
        )
        rows = {
            spec.resource_key: _valid_diagnostic_row(self.resource_preflight, spec)
            for spec in specs
        }
        cursor = _ResourceDiagnosticCursor(rows)
        connection = _ResourceDiagnosticConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            result = (
                self.resource_preflight.check_automation_project_required_resources(
                    lambda: connection
                )
            )

        self.assertEqual(0, result)
        self.assertEqual(
            "automation_project_required_resources=ok count=8\n",
            output.getvalue(),
        )
        self.assertEqual(1, connection.rollback_count)
        self.assertTrue(connection.closed)
        self.assertEqual(
            ["START TRANSACTION READ ONLY"],
            [sql for sql, _params in cursor.calls if sql.startswith("START")],
        )
        resource_queries = [
            call for call in cursor.calls if "workflow_resources" in call[0]
        ]
        self.assertEqual(8, len(resource_queries))
        self.assertEqual(
            [(spec.expected_kind, spec.resource_key) for spec in specs],
            [params for _sql, params in resource_queries],
        )
        allowed_aliases = {
            f"{field_name}_{suffix}"
            for field_name in (
                self.resource_preflight.AUTOMATION_PROJECT_REQUIRED_RESOURCE_FIELD_NAMES
            )
            for suffix in ("present", "type", "nonempty")
        }
        allowed_aliases.add("resource_kind_matches")
        for sql, _params in resource_queries:
            projection = sql.split(" FROM workflow_resources", 1)[0]
            self.assertEqual(
                allowed_aliases,
                set(re.findall(r"\bAS ([a-z0-9_]+)", projection)),
            )
            self.assertNotIn("SELECT resource_key, config_json", projection)

    def test_required_resource_diagnostic_reports_only_keys_reasons_and_fields(self):
        specs = (
            self.resource_preflight.AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS
        )
        rows = {
            spec.resource_key: _valid_diagnostic_row(self.resource_preflight, spec)
            for spec in specs
        }
        rows.pop("phase7.daily_sign_sheet")
        rows["phase7.site_send_bitable"].update(
            {
                "resource_kind_present": 1,
                "resource_kind_type": "STRING",
                "resource_kind_nonempty": 1,
                "resource_kind_matches": 0,
                "unexpected_secret": "must-not-be-printed",
            }
        )
        rows["phase7.arrive_primary_sheet"]["clear_range_present"] = 0
        rows["phase7.stats_archive_sheet"].update(
            {
                "default_write_range_present": 1,
                "default_write_range_type": "OBJECT",
                "default_write_range_nonempty": 0,
                "source_snapshot_range_present": 0,
            }
        )
        cursor = _ResourceDiagnosticCursor(rows)
        connection = _ResourceDiagnosticConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            result = (
                self.resource_preflight.check_automation_project_required_resources(
                    lambda: connection
                )
            )

        self.assertEqual(1, result)
        lines = output.getvalue().splitlines()
        self.assertEqual("automation_project_required_resources=blocked count=4", lines[0])
        self.assertIn(
            "automation_project_required_resource=phase7.site_send_bitable "
            "reason=INVALID_KIND field=resource_kind",
            lines,
        )
        self.assertIn(
            "automation_project_required_resource=phase7.arrive_primary_sheet "
            "reason=MISSING_FIELD field=clear_range",
            lines,
        )
        self.assertIn(
            "automation_project_required_resource=phase7.daily_sign_sheet "
            "reason=MISSING_ROW field=resource_key",
            lines,
        )
        self.assertIn(
            "automation_project_required_resource=phase7.stats_archive_sheet "
            "reason=INVALID_FIELD_TYPE "
            "field=default_write_range_or_source_snapshot_range",
            lines,
        )
        self.assertNotIn("must-not-be-printed", output.getvalue())
        self.assertEqual(1, connection.rollback_count)
        self.assertTrue(connection.closed)

    def test_required_resource_diagnostic_rolls_back_when_read_fails(self):
        specs = (
            self.resource_preflight.AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS
        )
        rows = {
            spec.resource_key: _valid_diagnostic_row(self.resource_preflight, spec)
            for spec in specs
        }
        cursor = _ResourceDiagnosticCursor(
            rows,
            fail_resource_key="phase7.send_order_bitable",
        )
        connection = _ResourceDiagnosticConnection(cursor)

        with self.assertRaisesRegex(RuntimeError, "simulated read failure"):
            self.resource_preflight.check_automation_project_required_resources(
                lambda: connection
            )

        self.assertEqual(1, connection.rollback_count)
        self.assertTrue(connection.closed)

    def test_schedule_identity_preflight_allows_missing_expected_rows(self):
        cursor = _ScheduleIdentityCursor(
            (
                {
                    "id": "send_order_2359",
                    "tool_name": "sync_daily_send_orders",
                },
                {
                    "id": "customer_problems_shadow",
                    "tool_name": "sync_customer_service_problems",
                },
                {
                    "id": "r7_departure_checkin",
                    "tool_name": "r7_departure_checkin",
                },
            )
        )
        connection = _ScheduleIdentityConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            result = self.schedule_identity_preflight.check_automation_project_scheduled_task_identities(
                lambda: connection
            )

        self.assertEqual(0, result)
        self.assertEqual(
            "automation_project_scheduled_task_identities=ok "
            "state=pending allowed_count=71\n",
            output.getvalue(),
        )
        self.assertEqual("START TRANSACTION READ ONLY", cursor.calls[0][0])
        self.assertEqual(("018",), cursor.calls[1][1])
        self.assertEqual(
            "SELECT id, tool_name FROM scheduled_tasks ORDER BY BINARY id",
            cursor.calls[2][0],
        )
        self.assertEqual(1, connection.rollback_count)
        self.assertTrue(connection.closed)

    def test_schedule_identity_preflight_skips_legacy_set_after_018(self):
        cursor = _ScheduleIdentityCursor(
            ({"id": "user_plugin_schedule", "tool_name": "user_tool"},),
            applied=True,
        )
        connection = _ScheduleIdentityConnection(cursor)
        output = io.StringIO()

        with (
            patch.object(
                self.schedule_identity_preflight,
                "load_reviewed_schedule_identities",
                side_effect=AssertionError("must not load legacy authority"),
            ),
            redirect_stdout(output),
        ):
            result = self.schedule_identity_preflight.check_automation_project_scheduled_task_identities(
                lambda: connection
            )

        self.assertEqual(0, result)
        self.assertEqual(
            "automation_project_scheduled_task_identities=ok "
            "state=applied allowed_count=71\n",
            output.getvalue(),
        )
        self.assertFalse(any("scheduled_tasks" in sql for sql, _ in cursor.calls))
        self.assertEqual(1, connection.rollback_count)
        self.assertTrue(connection.closed)

    def test_schedule_identity_preflight_hex_encodes_untrusted_database_text(self):
        unknown_id = "未知\npassword=must-not-leak"
        unknown_tool = "恶意\r\ntool"
        wrong_tool = "wrong\npassword=must-not-leak"
        cursor = _ScheduleIdentityCursor(
            (
                {"id": unknown_id, "tool_name": unknown_tool},
                {"id": "send_order_2359", "tool_name": wrong_tool},
                {"id": "x" * 513, "tool_name": "sync_daily_send_orders"},
                {"id": "send_order_2359", "tool_name": "y" * 513},
            )
        )
        connection = _ScheduleIdentityConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            result = self.schedule_identity_preflight.check_automation_project_scheduled_task_identities(
                lambda: connection
            )

        self.assertEqual(1, result)
        rendered = output.getvalue()
        self.assertNotIn("must-not-leak", rendered)
        self.assertNotIn("未知", rendered)
        lines = rendered.splitlines()
        self.assertEqual(
            "automation_project_scheduled_task_identities=blocked count=4",
            lines[0],
        )
        hex_lines = [line for line in lines[1:] if " task_id_hex=" in line]
        hash_lines = [line for line in lines[1:] if "_sha256=" in line]
        self.assertEqual(2, len(hex_lines))
        self.assertEqual(2, len(hash_lines))
        for line in hex_lines:
            match = re.fullmatch(
                r"automation_project_scheduled_task_identity "
                r"task_id_hex=([0-9a-f]{0,1024}) "
                r"tool_name_hex=([0-9a-f]{0,1024}) "
                r"reason=(UNKNOWN_TASK_ID|TOOL_NAME_MISMATCH) "
                r"field=(id|tool_name)",
                line,
            )
            self.assertIsNotNone(match)
        unknown_line = next(
            line for line in hex_lines if "reason=UNKNOWN_TASK_ID" in line
        )
        unknown_task_hex = re.search(
            r"task_id_hex=([0-9a-f]+)", unknown_line
        ).group(1)
        self.assertEqual(bytes.fromhex(unknown_task_hex).decode(), unknown_id)
        for line in hash_lines:
            self.assertRegex(
                line,
                r"^automation_project_scheduled_task_identity_sha256="
                r"[0-9a-f]{64} reason=INVALID_IDENTITY "
                r"field=(id|tool_name)$",
            )
        self.assertEqual(1, connection.rollback_count)
        self.assertTrue(connection.closed)

    def test_schedule_identity_preflight_suppresses_query_errors_and_rolls_back(self):
        cursor = _ScheduleIdentityCursor(fail_query=True)
        connection = _ScheduleIdentityConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            result = self.schedule_identity_preflight.check_automation_project_scheduled_task_identities(
                lambda: connection
            )

        self.assertEqual(1, result)
        self.assertEqual(
            "automation_project_scheduled_task_identities=blocked "
            "reason=AUTOMATION_PROJECT_IDENTITY_PREFLIGHT_RUNTIME_ERROR "
            "count=1\n",
            output.getvalue(),
        )
        self.assertEqual(1, connection.rollback_count)
        self.assertTrue(connection.closed)

    def test_runner_exposes_schedule_identity_preflight_cli(self):
        with (
            patch.object(sys, "argv", [
                "run_migrations.py",
                "--check-automation-project-scheduled-task-identities",
            ]),
            patch.object(
                self.runner,
                "check_automation_project_scheduled_task_identities",
                return_value=17,
            ) as check,
        ):
            result = self.runner.main()

        self.assertEqual(17, result)
        check.assert_called_once_with(self.runner._connect)

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

    def test_bootstrap_source_snapshot_schema_repairs_only_empty_old_partial(self):
        create_items = self.sql.split(
            "CREATE TABLE IF NOT EXISTS automation_project_bootstrap_items_018",
            1,
        )[1].split(
            "CREATE TABLE IF NOT EXISTS automation_project_bootstrap_marker_018",
            1,
        )[0]
        self.assertIn("source_snapshot_json JSON NOT NULL", create_items)
        self.assertIn(
            "chk_automation_project_bootstrap_source_snapshot",
            create_items,
        )
        guard = self.sql.index(
            "cp018_bootstrap_evidence_missing_guard_stmt"
        )
        add_column = self.sql.index(
            "cp018_add_bootstrap_source_snapshot_stmt"
        )
        require_column = self.sql.index(
            "cp018_require_bootstrap_source_snapshot_stmt"
        )
        self.assertLess(guard, add_column)
        self.assertLess(add_column, require_column)
        self.assertIn(
            "cp018_bootstrap_evidence_unrecoverable",
            self.sql,
        )
        self.assertIn(
            "ADD COLUMN source_snapshot_json JSON NULL",
            self.sql,
        )
        self.assertIn(
            "MODIFY COLUMN source_snapshot_json JSON NOT NULL",
            self.sql,
        )
        self.assertIn(
            "cp018_bootstrap_source_snapshot_final_check_count = 1",
            self.sql,
        )

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

    def test_restore_validation_accepts_only_four_exact_resource_layouts(self):
        class Cursor:
            def __init__(
                self,
                *,
                resource_keys,
                captured_keys=frozenset(),
                pending_existed=True,
                pending_changed_count=0,
            ) -> None:
                self.resource_keys = frozenset(resource_keys)
                self.captured_keys = frozenset(captured_keys)
                self.pending_existed = pending_existed
                self.pending_changed_count = pending_changed_count
                self._row = None
                self._rows = []

            def execute(self, sql, params=None):
                normalized = " ".join(str(sql).split())
                if (
                    "SELECT resource_key, existed_before," in normalized
                    and "ORDER BY BINARY resource_key" in normalized
                ):
                    self._rows = [
                        {
                            "resource_key": resource_key,
                            "existed_before": (
                                self.pending_existed
                                if resource_key == "phase7.pending_arrivals_sheet"
                                else True
                            ),
                            "captured": resource_key in self.captured_keys,
                        }
                        for resource_key in sorted(self.resource_keys)
                    ]
                    self._row = None
                elif (
                    "AS changed_count" in normalized
                    and "phase7.pending_arrivals_sheet" in normalized
                ):
                    self._row = {
                        "changed_count": self.pending_changed_count
                    }
                elif "AS changed_count" in normalized:
                    self._row = {"changed_count": 0}
                else:
                    self._row = None
                    self._rows = []

            def fetchone(self):
                return self._row

            def fetchall(self):
                return self._rows

        resource_backup_table = "automation_project_resource_backup_018"
        reviewed_map_table = "automation_project_reviewed_resource_map_018"
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
                reviewed_map_table
            ),
            "_table_exists": lambda _cursor, table: table in {
                resource_backup_table,
                reviewed_map_table,
            },
            "_column_exists": lambda _cursor, table, column: (
                table == "workflow_resources"
                and column in {"configuration_version", "config_sha256"}
            ),
        }

        helper = self.runner._MIGRATION_018_HELPER
        old = helper._OLD_REVIEWED_RESOURCE_KEYS
        current = helper._CURRENT_REVIEWED_RESOURCE_KEYS
        pending = {helper._LEGACY_PENDING_RESOURCE_KEY}
        valid_cases = (
            ("old14_empty", old, frozenset()),
            ("old14_captured", old, old),
            ("old_legacy15_empty", old | pending, frozenset()),
            ("old_legacy15_captured", old | pending, old | pending),
            ("current26_empty", current, frozenset()),
            ("current26_transition", current, old),
            ("current26_captured", current, current),
            ("current_legacy27_empty", current | pending, frozenset()),
            ("current_legacy27_transition", current | pending, old | pending),
            ("current_legacy27_captured", current | pending, current | pending),
        )
        for name, resource_keys, captured_keys in valid_cases:
            with self.subTest(name=name):
                self.assertFalse(
                    helper._validate_automation_project_authorization_restore(
                        runtime,
                        Cursor(
                            resource_keys=resource_keys,
                            captured_keys=captured_keys,
                        ),
                    )
                )

        same_count_fake = (current - {next(iter(current))}) | {
            "phase7.same_count_fake"
        }
        invalid_transition = (old - {next(iter(old))}) | {
            next(iter(helper._EXPANDED_CODE_OWNED_RESOURCE_KEYS))
        }
        invalid_cases = (
            ("same_count_fake", same_count_fake, frozenset(), True),
            ("legacy_pending_created", old | pending, frozenset(), False),
            ("wrong_hash_subset", current, invalid_transition, True),
        )
        for name, resource_keys, captured_keys, pending_existed in invalid_cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "backup is incomplete|capture is incomplete",
                ):
                    helper._validate_automation_project_authorization_restore(
                        runtime,
                        Cursor(
                            resource_keys=resource_keys,
                            captured_keys=captured_keys,
                            pending_existed=pending_existed,
                        ),
                    )
        with self.assertRaisesRegex(
            RuntimeError,
            "dirty legacy pending resource",
        ):
            helper._validate_automation_project_authorization_restore(
                runtime,
                Cursor(
                    resource_keys=old | pending,
                    pending_changed_count=1,
                ),
            )

    def test_restore_validation_empty_resource_backup_state_matrix(self):
        class Cursor:
            def __init__(self, capture_state: str | None) -> None:
                self.capture_state = capture_state
                self._row = None
                self._rows = []

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
                elif (
                    "SELECT resource_key, existed_before," in normalized
                    and "ORDER BY BINARY resource_key" in normalized
                ):
                    self._row = None
                    self._rows = []
                else:
                    self._row = None
                    self._rows = []

            def fetchone(self):
                return self._row

            def fetchall(self):
                return self._rows

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
        helper = self.runner._MIGRATION_018_HELPER

        class Cursor:
            def __init__(self) -> None:
                self._row = None
                self._rows = []

            def execute(self, sql, params=None):
                normalized = " ".join(str(sql).split())
                if (
                    "SELECT resource_key, existed_before," in normalized
                    and "ORDER BY BINARY resource_key" in normalized
                ):
                    self._rows = [
                        {
                            "resource_key": resource_key,
                            "existed_before": True,
                            "captured": False,
                        }
                        for resource_key in sorted(
                            helper._CURRENT_REVIEWED_RESOURCE_KEYS
                        )
                    ]
                    self._row = None
                elif "AS changed_count" in normalized:
                    self._row = {"changed_count": 0}
                else:
                    self._row = None
                    self._rows = []

            def fetchone(self):
                return self._row

            def fetchall(self):
                return self._rows

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

    def test_restore_accepts_only_exact_release_owned_project_policy_bootstrap(self):
        helper = _load_migration_helper()
        helper_source = MIGRATION_HELPER_PATH.read_text(encoding="utf-8")
        self.assertEqual(4, helper_source.count("AS UNSIGNED) AS source_"))
        runner_source = RUNNER_PATH.read_text(encoding="utf-8")
        reverse_start = runner_source.index(
            "AUTOMATION_PROJECT_AUTHORIZATION_TABLES_REVERSE = ("
        )
        reverse_end = runner_source.index("\n)", reverse_start)
        reverse_block = runner_source[reverse_start:reverse_end]
        self.assertLess(
            reverse_block.rindex('"automation_project_bootstrap_items_018"'),
            reverse_block.rindex('"automation_project_bootstrap_marker_018"'),
        )
        self.assertIn(
            "SET completed_by=%s",
            helper_source,
        )

        class BootstrapCursor:
            def __init__(self, *, marker, items, events):
                self.marker = marker
                self.items = items
                self.events = events
                self._rows = ()
                self._row = None

            def execute(self, sql, _params=None):
                normalized = " ".join(str(sql).split())
                self._row = None
                if "FROM automation_project_bootstrap_marker_018" in normalized:
                    self._rows = tuple(self.marker)
                elif (
                    "FROM automation_project_bootstrap_items_018" in normalized
                    and "LIMIT 1" in normalized
                ):
                    self._rows = ()
                    self._row = self.items[0] if self.items else None
                elif "FROM automation_project_bootstrap_items_018" in normalized:
                    self._rows = tuple(self.items)
                elif "FROM automation_project_policy_events AS event" in normalized:
                    self._rows = tuple(self.events)
                else:
                    raise AssertionError(f"unexpected bootstrap restore SQL: {normalized}")

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._row

        tables = {
            "automation_project_bootstrap_marker_018",
            "automation_project_bootstrap_items_018",
            "automation_project_policy_events",
        }
        runtime = {
            "_table_exists": lambda _cursor, table_name: table_name in tables,
            "hashlib": hashlib,
            "json": json,
            "uuid": uuid,
            "AUTOMATION_PROJECT_AUTHORIZATION_TABLES_REVERSE": (
                "automation_project_policy_events",
                "automation_project_bootstrap_items_018",
                "automation_project_bootstrap_marker_018",
            ),
        }
        release_sha = "d" * 40

        def sha(value):
            return hashlib.sha256(
                json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

        def marker_for(items, *, completed_by=None):
            return [
                {
                    "marker_id": 1,
                    "release_sha": release_sha,
                    "project_set_sha256": sha(
                        {
                            "schema_version": 1,
                            "release_sha": release_sha,
                            "projects": sorted(
                                [
                                    {
                                        "automation_id": item["automation_id"],
                                        "initial_mode": item["initial_mode"],
                                        "source_set_sha256": item[
                                            "source_set_sha256"
                                        ],
                                        "policy_version": item["policy_version"],
                                    }
                                    for item in items
                                ],
                                key=lambda value: value["automation_id"],
                            ),
                        }
                    ),
                    "completed_by": completed_by
                    or "system:automation-project-bootstrap-018",
                }
            ]

        def build_bootstrap(*, finance_enabled):
            items = []
            events = []
            disabled_task_ids = {"yunda_dispatch_forecast_1700"}
            if not finance_enabled:
                disabled_task_ids.add("finance_bills_0010")
            for event_id, automation_id in enumerate(
                sorted(helper._BOOTSTRAP_PROJECT_IDS_018),
                start=1,
            ):
                definition = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[
                    automation_id
                ]
                configuration_request_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "boyi:first-party-plugin-config:"
                        f"{release_sha}:{automation_id}",
                    )
                )
                source_tasks = []
                for task_id in sorted(definition.scheduled_task_ids):
                    enabled = task_id not in disabled_task_ids
                    has_legacy_grant = task_id != "customer_problems_shadow"
                    legacy_authorized = enabled and has_legacy_grant
                    source_tasks.append(
                        {
                            "task_id": task_id,
                            "tool_name": definition.tool_name,
                            "automation_generation": 1,
                            "configuration_version": 2,
                            "enabled": enabled,
                            "cron_expression_hash": sha(
                                {"cron": task_id}
                            ),
                            "arguments_hash": sha(
                                {"arguments": automation_id}
                            ),
                            "source_policy_mode": "REQUIRE_EACH_RUN",
                            "source_policy_version": 2,
                            "legacy_authorized": legacy_authorized,
                            "legacy_grant_request_id": (
                                str(
                                    uuid.uuid5(
                                        uuid.NAMESPACE_URL,
                                        f"boyi:control-plane-v1:{task_id}",
                                    )
                                )
                                if has_legacy_grant
                                else ""
                            ),
                            "legacy_grant_contract_hash": (
                                sha({"legacy": task_id})
                                if has_legacy_grant
                                else ""
                            ),
                            "legacy_grant_tool_contract_hash": (
                                sha({"tool": definition.tool_name})
                                if has_legacy_grant
                                else ""
                            ),
                            "retirement_kind": (
                                "CONFIGURATION_MIGRATION"
                                if has_legacy_grant
                                else "NONE"
                            ),
                            "retirement_request_id": (
                                configuration_request_id
                                if has_legacy_grant
                                else ""
                            ),
                        }
                    )
                mode = (
                    "LEGACY_SCHEDULE_ONLY"
                    if source_tasks
                    and all(
                        task["legacy_authorized"] is True
                        for task in source_tasks
                    )
                    else "REQUIRE_EACH_RUN"
                )
                contract_snapshot = {"automation_id": automation_id}
                source_contract_hash = sha(contract_snapshot)
                source_snapshot = {
                    "schema_version": 1,
                    "automation_id": automation_id,
                    "automation_generation": 1,
                    "project_configuration_version": 2,
                    "contract_hash": source_contract_hash,
                    "configuration_request_id": configuration_request_id,
                    "configuration_event_metadata_sha256": sha(
                        {"configuration": automation_id}
                    ),
                    "scheduled_tasks": source_tasks,
                }
                source_set_sha256 = sha(source_snapshot)
                items.append(
                    {
                        "automation_id": automation_id,
                        "initial_mode": mode,
                        "source_set_sha256": source_set_sha256,
                        "source_snapshot_json": source_snapshot,
                        "policy_version": 2 if mode == "LEGACY_SCHEDULE_ONLY" else 1,
                        "source_automation_id": automation_id,
                        "source_generation": 1,
                        "source_configuration_version": 2,
                    }
                )
                request_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "boyi:automation-project-bootstrap-018:"
                        f"{automation_id}",
                    )
                )
                legacy = mode == "LEGACY_SCHEDULE_ONLY"
                events.append(
                    {
                        "event_id": event_id,
                        "automation_id": automation_id,
                        "request_id": request_id,
                        "correlation_id": request_id,
                        "from_mode": "REQUIRE_EACH_RUN",
                        "to_mode": mode,
                        "contract_hash": (
                            source_contract_hash if legacy else None
                        ),
                        "contract_snapshot_json": (
                            contract_snapshot if legacy else None
                        ),
                        "tool_contract_hash": "b" * 64 if legacy else None,
                        "plugin_contract_hash": "c" * 64 if legacy else None,
                        "project_configuration_version": 2,
                        "project_generation": 1,
                        "actor_id": "system:automation-project-bootstrap-018",
                        "actor_role": "system",
                        "actor_display_name": "Automation project bootstrap 018",
                        "reason": "AUTOMATION_PROJECT_BOOTSTRAP_018",
                        "comment": "Release-held one-time policy bootstrap",
                        "initial_mode": mode,
                        "source_contract_hash": source_contract_hash,
                        "source_generation": 1,
                        "source_configuration_version": 2,
                    }
                )
            return items, events, marker_for(items)

        items, events, marker = build_bootstrap(finance_enabled=True)
        self.assertEqual(
            (10, 6, 55),
            (
                sum(
                    item["initial_mode"] == "LEGACY_SCHEDULE_ONLY"
                    for item in items
                ),
                sum(
                    item["initial_mode"] == "REQUIRE_EACH_RUN"
                    for item in items
                ),
                sum(
                    task["legacy_authorized"]
                    for item in items
                    for task in item["source_snapshot_json"]["scheduled_tasks"]
                ),
            ),
        )

        self.assertTrue(
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(marker=marker, items=items, events=events),
            )
        )

        # Incident regression: disabled finance and dispatch schedules retain
        # their exact grant/retirement evidence but safely force their projects
        # to REQUIRE_EACH_RUN.  The restore identity is not a fixed mode count.
        incident_items, incident_events, incident_marker = build_bootstrap(
            finance_enabled=False
        )
        self.assertEqual(
            (9, 7, 54),
            (
                sum(
                    item["initial_mode"] == "LEGACY_SCHEDULE_ONLY"
                    for item in incident_items
                ),
                sum(
                    item["initial_mode"] == "REQUIRE_EACH_RUN"
                    for item in incident_items
                ),
                sum(
                    task["legacy_authorized"]
                    for item in incident_items
                    for task in item["source_snapshot_json"]["scheduled_tasks"]
                ),
            ),
        )
        self.assertTrue(
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(
                    marker=incident_marker,
                    items=incident_items,
                    events=incident_events,
                ),
            )
        )

        tampered_events = [dict(event) for event in events]
        tampered_events[-1]["comment"] = "user supplied"
        with self.assertRaisesRegex(
            RuntimeError,
            "project policy bootstrap events are invalid",
        ):
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(
                    marker=marker,
                    items=items,
                    events=tampered_events,
                ),
            )

        tampered_items = [dict(item) for item in items]
        tampered_items[0]["source_snapshot_json"] = dict(
            tampered_items[0]["source_snapshot_json"]
        )
        tampered_items[0]["source_snapshot_json"]["automation_generation"] = 2
        with self.assertRaisesRegex(
            RuntimeError,
            "project policy bootstrap items are invalid",
        ):
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(
                    marker=marker,
                    items=tampered_items,
                    events=events,
                ),
            )

        forged_items = [dict(item) for item in items]
        forged_items[0] = dict(forged_items[0])
        forged_snapshot = dict(forged_items[0]["source_snapshot_json"])
        forged_snapshot["automation_id"] = "unknown-project"
        forged_snapshot["configuration_request_id"] = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "boyi:first-party-plugin-config:"
                f"{release_sha}:unknown-project",
            )
        )
        forged_items[0].update(
            {
                "automation_id": "unknown-project",
                "source_automation_id": "unknown-project",
                "source_snapshot_json": forged_snapshot,
                "source_set_sha256": sha(forged_snapshot),
            }
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "project policy bootstrap items are invalid",
        ):
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(
                    marker=marker_for(forged_items),
                    items=forged_items,
                    events=events,
                ),
            )

        forged_mode_items = [dict(item) for item in items]
        require_index = next(
            index
            for index, item in enumerate(forged_mode_items)
            if not item["source_snapshot_json"]["scheduled_tasks"]
        )
        forged_mode_items[require_index] = dict(forged_mode_items[require_index])
        forged_mode_items[require_index]["initial_mode"] = "LEGACY_SCHEDULE_ONLY"
        with self.assertRaisesRegex(
            RuntimeError,
            "project policy bootstrap items are invalid",
        ):
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(
                    marker=marker_for(forged_mode_items),
                    items=forged_mode_items,
                    events=events,
                ),
            )

        # A committed restore sentinel is valid both before the first DROP and
        # after any exact sequential prefix.  A hole in that prefix is rejected.
        restore_marker = marker_for(
            incident_items,
            completed_by=(
                "system:automation-project-bootstrap-018:restore-in-progress"
            ),
        )
        self.assertTrue(
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(
                    marker=restore_marker,
                    items=incident_items,
                    events=incident_events,
                ),
            )
        )
        tables.remove("automation_project_policy_events")
        self.assertTrue(
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(
                    marker=restore_marker,
                    items=incident_items,
                    events=(),
                ),
            )
        )
        tables.remove("automation_project_bootstrap_items_018")
        self.assertTrue(
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(marker=restore_marker, items=(), events=()),
            )
        )
        tables.add("automation_project_policy_events")
        with self.assertRaisesRegex(RuntimeError, "bootstrap is incomplete"):
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(marker=restore_marker, items=(), events=()),
            )
        tables.update(
            {
                "automation_project_bootstrap_items_018",
                "automation_project_policy_events",
            }
        )

        self.assertFalse(
            helper._validate_project_policy_bootstrap_restore(
                runtime,
                BootstrapCursor(marker=(), items=(), events=()),
            )
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
