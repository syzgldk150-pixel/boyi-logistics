from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import re
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


def _as_service_v2_project_row(row: dict) -> dict:
    """Give a window test row the committed Service V2 shape its cron implies."""
    row = deepcopy(row)
    snapshot = row.get("snapshot_json")
    if not isinstance(snapshot, dict) or "runtime_model" in snapshot:
        return row
    match = re.fullmatch(r"(\d{1,2}) (\d{1,2}) \* \* \*", str(row.get("cron_expression") or ""))
    times = [f"{int(match.group(2)):02d}:{int(match.group(1)):02d}"] if match else []
    desired = {"kind": "daily_times" if times else "none", "times": times, "enabled": bool(times)}
    version = row.get("configuration_version", 1)
    metadata = snapshot.setdefault("execution_metadata", {})
    contribution_id = next(iter(metadata.get("compiled_invocations") or {"scheduler": None}))
    snapshot.update(runtime_model="SERVICE_V2", plugin_api="2.0.0", enabled_entrypoints=[contribution_id])
    metadata.update(
        project_config_version=version,
        schedule=deepcopy(desired),
        contributions={"scheduler": [{"id": contribution_id, "default_enabled": True,
                                      "schedule": {"timezone": "Asia/Shanghai"}}]},
    )
    row.update(
        configuration_version=version, config_version=version, desired_schedule_json=desired,
        desired_schedule_sha256=hashlib.sha256(json.dumps(
            desired, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
    )
    return row


class _WindowCursor:
    def __init__(
        self,
        rows: list[dict],
        *,
        policy_exists: bool,
        candidate_rows: list[dict] | None = None,
        cutover_backup_exists: bool = False,
        backup_rows: list[dict] | None = None,
        applied_014: bool = False,
        applied_017: bool = False,
        project_schema_exists: bool = False,
        project_rows: list[dict] | None = None,
    ) -> None:
        self.rows = rows
        self.candidate_rows = rows if candidate_rows is None else candidate_rows
        self.policy_exists = policy_exists
        self.cutover_backup_exists = cutover_backup_exists
        self.backup_rows = [] if backup_rows is None else backup_rows
        self.applied_014 = applied_014
        self.applied_017 = applied_017
        self.project_schema_exists = project_schema_exists
        self.project_rows = [_as_service_v2_project_row(row) for row in project_rows or []]
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
        elif "FROM information_schema.TABLES" in normalized or "FROM information_schema.tables" in normalized:
            table_name = params[0] if params else ""
            exists = table_name == "scheduled_tasks" or (
                table_name == "schema_migrations"
                and (self.applied_014 or self.applied_017)
            ) or (
                table_name == "scheduled_task_approval_policies"
                and self.policy_exists
            ) or (
                table_name == "control_plane_task_cutover_backup_014"
                and self.cutover_backup_exists
            ) or (
                table_name
                in {
                    "automation_projects",
                    "automation_project_policies",
                    "automation_project_generations",
                    "automation_project_generation_leases",
                }
                and self.project_schema_exists
            )
            self._row = {"exists": 1, "table_count": 1} if exists else {"table_count": 0}
        elif normalized.startswith("SELECT 1 FROM schema_migrations WHERE version="):
            version_match = re.search(r"version='([^']+)'", normalized)
            version = (
                params[0]
                if isinstance(params, tuple) and params
                else version_match.group(1)
                if version_match is not None
                else None
            )
            self._row = (
                {"exists": 1}
                if (
                    self.applied_014
                    and str(version) == "014"
                    or self.applied_017
                    and str(version) == "017"
                )
                else None
            )
        elif normalized.startswith("SELECT policy.task_id"):
            self._rows = list(self.rows)
        elif normalized.startswith("SELECT task.id AS task_id"):
            self._rows = list(self.project_rows)
        elif (
            "COUNT(*) AS matched_count" in normalized
            and "control_plane_task_cutover_backup_014" in normalized
        ):
            task_id = params[0] if params else ""
            expected_message_sha256 = params[1] if params else ""
            current = next(
                (
                    row
                    for row in self.candidate_rows
                    if row.get("id") == task_id
                ),
                None,
            )
            prior = next(
                (row for row in self.backup_rows if row.get("id") == task_id),
                None,
            )
            current_arguments = (
                current.get("tool_params") if isinstance(current, dict) else None
            )
            prior_arguments = (
                prior.get("tool_params") if isinstance(prior, dict) else None
            )
            current_message = (
                current.get("last_message") if isinstance(current, dict) else None
            )
            requires_configuration_version = (
                "task.configuration_version = 1" in normalized
            )
            matched = bool(
                isinstance(current, dict)
                and isinstance(prior, dict)
                and current.get("tool_name") == "sync_yunda_send_waybills"
                and current.get("cron_expression") == "55 23 * * *"
                and current.get("enabled") in {False, 0}
                and type(current.get("enabled")) in {bool, int}
                and current.get("last_status") == "disabled"
                and (
                    not requires_configuration_version
                    or (
                        type(current.get("configuration_version")) is int
                        and current.get("configuration_version") == 1
                    )
                )
                and type(current_message) is str
                and hashlib.sha256(current_message.encode("utf-8")).hexdigest()
                == expected_message_sha256
                and type(current_arguments) is dict
                and set(current_arguments) == {"account_id", "ensure_fields"}
                and type(current_arguments.get("account_id")) is str
                and current_arguments.get("account_id") == "yunda_default"
                and type(current_arguments.get("ensure_fields")) is bool
                and current_arguments.get("ensure_fields") is False
                and prior.get("tool_name") == "sync_yunda_send_waybills"
                and prior.get("cron_expression") == "55 23 * * *"
                and prior.get("enabled") in {True, 1}
                and type(prior.get("enabled")) in {bool, int}
                and type(prior_arguments) is dict
                and set(prior_arguments)
                == {
                    "account_id",
                    "session_profile",
                    "ensure_fields",
                    "target_date",
                }
                and type(prior_arguments.get("account_id")) is str
                and prior_arguments.get("account_id") == "yunda_default"
                and type(prior_arguments.get("session_profile")) is str
                and prior_arguments.get("session_profile") == "yunda"
                and type(prior_arguments.get("ensure_fields")) is bool
                and prior_arguments.get("ensure_fields") is False
                and type(prior_arguments.get("target_date")) is str
                and prior_arguments.get("target_date") == ""
            )
            self._row = {"matched_count": int(matched)}
        elif "FROM control_plane_task_cutover_backup_014" in normalized:
            self._rows = list(self.backup_rows)
            self._row = self.backup_rows[0] if self.backup_rows else None
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


class _ReleaseRestoreCursor:
    def __init__(
        self,
        *,
        tables: set[str] | None = None,
        running_count: int = 0,
        fail_restore: bool = False,
        applied_versions: set[str] | None = None,
        dirty_backups: set[str] | None = None,
        marker_present: bool = False,
        orphan_policy_count: int = 0,
        unsafe_created_count: int = 0,
        bootstrap_policy_rows: int = 0,
        bootstrap_event_rows: int = 0,
        bootstrap_domain_rows: int = 0,
        remaining_created_count: int = 0,
        columns: set[str] | None = None,
        lease_rows: int = 0,
    ) -> None:
        self.tables = tables or {
            "schema_migrations",
            "scheduled_tasks",
            "scheduled_task_contract_upgrade_backup_017",
            "scheduled_task_contract_upgrade_created_017",
            "daily_sign_single_tms_backup_016",
            "scheduled_task_approval_policies",
            "scheduled_task_approval_policy_events",
            "domain_events",
            "outbox_events",
            "event_consumptions",
            "agent_run_steps",
        }
        self.running_count = running_count
        self.fail_restore = fail_restore
        self.applied_versions = applied_versions or set()
        self.dirty_backups = dirty_backups or set()
        self.marker_present = marker_present
        self.orphan_policy_count = orphan_policy_count
        self.unsafe_created_count = unsafe_created_count
        self.bootstrap_policy_rows = bootstrap_policy_rows
        self.bootstrap_event_rows = bootstrap_event_rows
        self.bootstrap_domain_rows = bootstrap_domain_rows
        self.remaining_created_count = remaining_created_count
        self.columns = set(columns or ())
        self.lease_rows = lease_rows
        self.rowcount = 0
        self.calls: list[tuple[str, object]] = []
        self._row = None
        self._rows: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self.rowcount = 0
        self._row = None
        self._rows = []
        if normalized == "SELECT VERSION() AS version":
            self._row = {"version": "8.0.44"}
        elif "FROM information_schema.TABLES" in normalized:
            table_name = params[0] if params else ""
            self._row = {"exists": 1} if table_name in self.tables else None
        elif "FROM information_schema.COLUMNS" in normalized:
            column_name = params[1] if params and len(params) > 1 else ""
            self._row = {"exists": 1} if column_name in self.columns else None
        elif normalized.startswith("SELECT COUNT(*) AS running_count"):
            self._row = {"running_count": self.running_count}
        elif normalized.startswith("SELECT COUNT(*) AS orphan_count"):
            self._row = {"orphan_count": self.orphan_policy_count}
        elif normalized.startswith("SELECT COUNT(*) AS unsafe_count"):
            self._row = {"unsafe_count": self.unsafe_created_count}
        elif normalized.startswith("SELECT COUNT(*) AS remaining_created_count"):
            self._row = {"remaining_created_count": self.remaining_created_count}
        elif normalized.startswith("SELECT policy.task_id"):
            self._rows = [
                {"task_id": "finance_startup_catchup"}
                for _ in range(self.bootstrap_policy_rows)
            ]
        elif normalized.startswith("SELECT event.event_id"):
            self._rows = [
                {"event_id": index + 1}
                for index in range(self.bootstrap_event_rows)
            ]
        elif normalized.startswith("SELECT domain_event.event_id"):
            self._rows = [
                {"event_id": str(index + 1)}
                for index in range(self.bootstrap_domain_rows)
            ]
        elif normalized.startswith("SELECT 1 FROM schema_migrations WHERE version="):
            version = params[0] if params else ""
            self._row = {"exists": 1} if version in self.applied_versions else None
        elif normalized.startswith("SELECT 1 FROM scheduled_task_approval_policy_events"):
            self._row = {"exists": 1} if self.marker_present else None
        elif normalized.startswith("SELECT 1 FROM ") and normalized.endswith(" LIMIT 1"):
            table_name = normalized.split()[3]
            self._row = {"exists": 1} if table_name in self.dirty_backups else None
        elif normalized.startswith("UPDATE feishu_approval_deliveries"):
            self.rowcount = self.lease_rows
        elif normalized.startswith("INSERT INTO scheduled_tasks") and self.fail_restore:
            raise RuntimeError("injected 017 restore failure")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


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

    def _exact_policy(self, *, task, profile, registry, approval):
        capability = registry.get_capability(profile.tool_name)
        contract = approval.build_scheduled_task_contract(
            task,
            capability,
            dynamic_argument_rules=profile.dynamic_argument_rules,
            allowed_special_cron=profile.cron_expression,
        )
        return {
            "task_id": task["id"],
            "mode": "EXACT_SCHEDULE_EXEMPT",
            "contract_hash": contract.contract_hash,
            "contract_snapshot_json": contract.snapshot,
            "tool_contract_hash": contract.tool_contract_hash,
            "approved_by_actor_id": self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ID,
            "approved_by_actor_role": self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
            "version": 2,
            "has_explaining_event": 1,
            "latest_event_reason": "control_plane_v1_bootstrap",
            **{key: value for key, value in task.items() if key != "id"},
        }

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

    def test_running_protected_write_gate_is_read_only_and_redacted(self):
        for running_count, expected in ((0, 0), (2, 1)):
            with self.subTest(running_count=running_count):
                cursor = _ReleaseRestoreCursor(running_count=running_count)
                connection = _WindowConnection(cursor)
                with (
                    patch.object(self.runner, "_connect", return_value=connection),
                    patch("builtins.print") as print_mock,
                ):
                    result = self.runner.check_running_protected_writes()

                self.assertEqual(expected, result)
                self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))
                rendered = " ".join(str(call) for call in print_mock.call_args_list)
                self.assertNotIn("run_id", rendered)
                if running_count:
                    self.assertIn("PROTECTED_WRITE_RUNNING", rendered)
                protected_query = next(
                    sql
                    for sql, _ in cursor.calls
                    if sql.startswith("SELECT COUNT(*) AS running_count")
                )
                self.assertIn("status IN ('RUNNING', 'VERIFYING')", protected_query)
                self.assertIn("'DESTRUCTIVE'", protected_query)
                self.assertNotIn("INTERNAL_PROJECTION_WRITE", protected_query)

    def test_scheduled_write_window_uses_shanghai_daily_boundaries(self):
        zone = ZoneInfo("Asia/Shanghai")
        is_active = self.runner._SCHEDULED_WRITE_WINDOW.is_within_daily_schedule_window

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

    def test_scheduled_write_window_accepts_typed_project_write_contracts(self):
        typed_rows = [
            {
                "task_id": "clockin_daxiang_1830",
                "automation_id": "clockin_daxiang",
                "automation_generation": 3,
                "tool_name": "automation.clockin_daxiang.run",
                "cron_expression": "30 18 * * *",
                "enabled": 1,
                "committed_generation": 3,
                "project_enabled": 1,
                "project_state": "ENABLED",
                "policy_mode": "PROJECT_FULL_AUTO",
                # A release may be the operation that repairs a missing signed
                # runtime root.  Its committed generation remains authoritative
                # for write-window classification while reconciliation is blocked.
                "generation_state": "BLOCKED",
                "snapshot_json": {
                    "automation_id": "clockin_daxiang",
                    "generation": 3,
                    "execution_metadata": {
                        "compiled_invocations": {"scheduler": {"arguments": {}}},
                        "governance_anchor": {"operation_type": "external_write"},
                    },
                },
            }
        ]
        candidate_rows = [
            {
                "id": "clockin_daxiang_1830",
                "tool_name": "automation.clockin_daxiang.run",
                "tool_params": {},
                "cron_expression": "30 18 * * *",
                "enabled": 1,
            }
        ]
        cursor = _WindowCursor(
            [],
            policy_exists=True,
            candidate_rows=candidate_rows,
            project_schema_exists=True,
            project_rows=typed_rows,
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
                    18,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

        self.assertEqual(1, result)
        rendered = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertIn("SCHEDULED_WRITE_WINDOW_ACTIVE", rendered)
        self.assertNotIn("CLOCK_TASK_TOOL_NOT_REVIEWED", rendered)
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))
        typed_query = next(
            sql for sql, _ in cursor.calls if sql.startswith("SELECT task.id AS task_id")
        )
        self.assertIn(
            "ON BINARY project.automation_id=BINARY task.automation_id",
            typed_query,
        )
        self.assertIn(
            "ON BINARY policy.automation_id=BINARY project.automation_id",
            typed_query,
        )

    def test_scheduled_write_window_excludes_exact_unknown_write_quarantine(self):
        typed_row = {
            "task_id": "arrive_list_0500",
            "automation_id": "arrive_list",
            "automation_generation": 3,
            "tool_name": "automation.arrive_list.run",
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "committed_generation": 3,
            "target_generation": 4,
            "project_enabled": 1,
            "project_state": "UPGRADING",
            "reconcile_state": "READY_TO_COMMIT",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "BLOCKED",
            "generation_error_code": "WRITE_OUTCOME_UNKNOWN",
            "target_generation_state": "PREPARED",
            "target_base_generation": 3,
            "policy_project_generation": 4,
            "max_generation": 4,
            "non_disposed_other_count": 1,
            "unsafe_non_disposed_other_count": 0,
            "active_current_lease_count": 0,
            "unknown_write_count": 2,
            "snapshot_json": {
                "automation_id": "arrive_list",
                "generation": 3,
                "execution_metadata": {
                    "compiled_invocations": {"scheduler": {"arguments": {}}},
                    "governance_anchor": {"operation_type": "external_write"},
                },
            },
        }
        cursor = _WindowCursor(
            [],
            policy_exists=True,
            candidate_rows=[],
            project_schema_exists=True,
            project_rows=[typed_row],
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
                    5,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

        self.assertEqual(0, result)
        print_mock.assert_called_once_with(
            "scheduled_write_window=ok checked_schedules=0"
        )
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))
        typed_query = next(
            sql for sql, _ in cursor.calls if sql.startswith("SELECT task.id AS task_id")
        )
        self.assertIn("FROM automation_project_generation_leases AS lease", typed_query)
        self.assertIn("AS unsafe_non_disposed_other_count", typed_query)
        self.assertIn("lease.outcome IN ('RUNNING', 'VERIFYING')", typed_query)
        self.assertIn("lease.outcome='WRITE_OUTCOME_UNKNOWN'", typed_query)
        self.assertIn(
            "CAST( COALESCE(MAX(history.generation), 0) AS UNSIGNED)",
            typed_query,
        )

    def test_scheduled_write_window_excludes_unknown_write_with_missing_target(self):
        typed_row = {
            "task_id": "arrive_list_0500",
            "automation_id": "arrive_list",
            "automation_generation": 3,
            "tool_name": "automation.arrive_list.run",
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "committed_generation": 3,
            "target_generation": 4,
            "project_enabled": 1,
            "project_state": "ENABLED",
            "reconcile_state": "PREPARING",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "BLOCKED",
            "generation_error_code": "WRITE_OUTCOME_UNKNOWN",
            "target_generation_state": None,
            "target_base_generation": None,
            "policy_project_generation": 4,
            "max_generation": 3,
            "non_disposed_other_count": 0,
            "unsafe_non_disposed_other_count": 0,
            "active_current_lease_count": 0,
            "unknown_write_count": 1,
            "snapshot_json": {
                "automation_id": "arrive_list",
                "generation": 3,
                "execution_metadata": {
                    "compiled_invocations": {"scheduler": {"arguments": {}}},
                    "governance_anchor": {"operation_type": "external_write"},
                },
            },
        }
        cursor = _WindowCursor(
            [],
            policy_exists=True,
            candidate_rows=[],
            project_schema_exists=True,
            project_rows=[typed_row],
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
                    5,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

        self.assertEqual(0, result)
        print_mock.assert_called_once_with(
            "scheduled_write_window=ok checked_schedules=0"
        )

    def test_scheduled_write_window_keeps_runnable_config_save_runtime(self):
        typed_row = {
            "task_id": "arrive_list_0500",
            "automation_id": "arrive_list",
            "automation_generation": 3,
            "tool_name": "automation.arrive_list.run",
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "committed_generation": 3,
            "target_generation": 4,
            "project_enabled": 1,
            "project_state": "ENABLED",
            "reconcile_state": "PREPARING",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "COMMITTED",
            "generation_error_code": None,
            "target_generation_state": None,
            "target_base_generation": None,
            "policy_project_generation": 4,
            "max_generation": 3,
            "non_disposed_other_count": 0,
            "unsafe_non_disposed_other_count": 0,
            "active_current_lease_count": 0,
            "unknown_write_count": 0,
            "snapshot_json": {
                "automation_id": "arrive_list",
                "generation": 3,
                "execution_metadata": {
                    "compiled_invocations": {"scheduler": {"arguments": {}}},
                    "governance_anchor": {"operation_type": "external_write"},
                },
            },
        }
        checkpoints = (
            (None, None, "PREPARING", 3),
            ("TARGET", 3, "PREPARING", 4),
            ("PREPARING", 3, "PREPARING", 4),
            ("WAITING_COEFFECTS", 3, "WAITING_COEFFECTS", 4),
            ("PREPARED", 3, "READY_TO_COMMIT", 4),
        )
        for target_state, target_base, reconcile_state, maximum in checkpoints:
            row = {
                **typed_row,
                "target_generation_state": target_state,
                "target_base_generation": target_base,
                "reconcile_state": reconcile_state,
                "max_generation": maximum,
            }
            cursor = _WindowCursor(
                [], policy_exists=True, candidate_rows=[],
                project_schema_exists=True, project_rows=[row],
            )
            connection = _WindowConnection(cursor)
            with (
                patch.object(self.runner, "_connect", return_value=connection),
                patch("builtins.print") as print_mock,
            ):
                result = self.runner.check_scheduled_write_window(
                    before_minutes=60,
                    after_minutes=45,
                    now=datetime(2026, 8, 14, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                )
            with self.subTest(target_state=target_state):
                self.assertEqual(1, result)
                rendered = " ".join(str(call) for call in print_mock.call_args_list)
                self.assertIn("SCHEDULED_WRITE_WINDOW_ACTIVE", rendered)

        invalid_row = {**typed_row, "target_generation": 5}
        cursor = _WindowCursor(
            [], policy_exists=True, candidate_rows=[],
            project_schema_exists=True, project_rows=[invalid_row],
        )
        with (
            patch.object(self.runner, "_connect", return_value=_WindowConnection(cursor)),
            patch("builtins.print") as print_mock,
        ):
            result = self.runner.check_scheduled_write_window(
                before_minutes=60,
                after_minutes=45,
                now=datetime(2026, 8, 14, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
        self.assertEqual(1, result)
        rendered = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertIn("PROJECT_SCHEDULE_RUNTIME_INVALID", rendered)
        self.assertIn("CHECK_PROJECT_STATE_INVALID", rendered)

    def test_scheduled_write_window_missing_target_runtime_fails_closed(self):
        base = {
            "task_id": "arrive_list_0500",
            "automation_id": "arrive_list",
            "automation_generation": 3,
            "tool_name": "automation.arrive_list.run",
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "committed_generation": 3,
            "target_generation": 4,
            "project_enabled": 1,
            "project_state": "UPGRADING",
            "reconcile_state": "PREPARING",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "COMMITTED",
            "generation_error_code": None,
            "target_generation_state": None,
            "target_base_generation": None,
            "policy_project_generation": 4,
            "max_generation": 3,
            "non_disposed_other_count": 0,
            "unsafe_non_disposed_other_count": 0,
            "active_current_lease_count": 0,
            "unknown_write_count": 0,
            "snapshot_json": {
                "automation_id": "arrive_list",
                "generation": 3,
                "execution_metadata": {
                    "compiled_invocations": {"scheduler": {"arguments": {}}},
                    "governance_anchor": {"operation_type": "external_write"},
                },
            },
        }
        mutations = (
            {"reconcile_state": "READY_TO_COMMIT"},
            {"target_generation": 3},
            {"target_generation": 5},
            {"policy_project_generation": 3},
            {"max_generation": 4},
            {"unsafe_non_disposed_other_count": 1},
            {"generation_state": "BLOCKED"},
            {"generation_error_code": "RUNTIME_ROOT_MISSING"},
            {"target_generation_state": "TARGET"},
            {"target_base_generation": 3},
            {"unknown_write_count": 1},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                cursor = _WindowCursor(
                    [],
                    policy_exists=True,
                    candidate_rows=[],
                    project_schema_exists=True,
                    project_rows=[{**base, **mutation}],
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
                            3,
                            0,
                            tzinfo=ZoneInfo("Asia/Shanghai"),
                        ),
                    )

                self.assertEqual(1, result)
                rendered = " ".join(
                    str(call) for call in print_mock.call_args_list
                )
                self.assertIn("PROJECT_SCHEDULE_RUNTIME_INVALID", rendered)
                self.assertIn("CHECK_PROJECT_STATE_INVALID", rendered)
                self.assertNotIn("arrive_list", rendered)

    def test_missing_target_runtime_still_validates_signed_schedule_contract(self):
        row = {
            "task_id": "arrive_list_0500",
            "automation_id": "arrive_list",
            "automation_generation": 3,
            "tool_name": "automation.arrive_list.run",
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "committed_generation": 3,
            "target_generation": 4,
            "project_enabled": 1,
            "project_state": "ENABLED",
            "reconcile_state": "PREPARING",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "COMMITTED",
            "generation_error_code": None,
            "target_generation_state": None,
            "target_base_generation": None,
            "policy_project_generation": 4,
            "max_generation": 3,
            "non_disposed_other_count": 0,
            "unsafe_non_disposed_other_count": 0,
            "active_current_lease_count": 0,
            "unknown_write_count": 0,
            "snapshot_json": {
                "automation_id": "arrive_list",
                "generation": 3,
                "execution_metadata": {
                    "compiled_invocations": {"scheduler": {"arguments": {}}},
                    "governance_anchor": {"operation_type": "external_write"},
                },
            },
        }
        invalid_snapshot = deepcopy(row["snapshot_json"])
        invalid_snapshot["execution_metadata"]["compiled_invocations"] = {}
        mutations = (
            ({"snapshot_json": invalid_snapshot}, "PROJECT_SCHEDULE_CONTRACT_INVALID"),
            ({"cron_expression": ""}, "PROJECT_SCHEDULE_CRON_INVALID"),
        )
        for mutation, expected_code in mutations:
            with self.subTest(expected_code=expected_code):
                cursor = _WindowCursor(
                    [],
                    policy_exists=True,
                    candidate_rows=[],
                    project_schema_exists=True,
                    project_rows=[{**row, **mutation}],
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
                            3,
                            0,
                            tzinfo=ZoneInfo("Asia/Shanghai"),
                        ),
                    )

                self.assertEqual(1, result)
                rendered = " ".join(
                    str(call) for call in print_mock.call_args_list
                )
                self.assertIn(expected_code, rendered)
                self.assertNotIn("arrive_list", rendered)

    def test_scheduled_write_window_unknown_write_quarantine_fails_closed(self):
        base = {
            "task_id": "arrive_list_0500",
            "automation_id": "arrive_list",
            "automation_generation": 3,
            "tool_name": "automation.arrive_list.run",
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "committed_generation": 3,
            "target_generation": 4,
            "project_enabled": 1,
            "project_state": "UPGRADING",
            "reconcile_state": "READY_TO_COMMIT",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "BLOCKED",
            "generation_error_code": "WRITE_OUTCOME_UNKNOWN",
            "target_generation_state": "PREPARED",
            "target_base_generation": 3,
            "policy_project_generation": 4,
            "max_generation": 4,
            "non_disposed_other_count": 1,
            "unsafe_non_disposed_other_count": 0,
            "active_current_lease_count": 0,
            "unknown_write_count": 1,
            "snapshot_json": {
                "automation_id": "arrive_list",
                "generation": 3,
                "execution_metadata": {
                    "compiled_invocations": {"scheduler": {"arguments": {}}},
                    "governance_anchor": {"operation_type": "external_write"},
                },
            },
        }
        mutations = (
            (
                "wrong reconcile state",
                {"reconcile_state": "PREPARING"},
                "RECONCILE_STATE_PREPARING",
            ),
            (
                "missing lease",
                {"unknown_write_count": 0},
                "UNKNOWN_WRITE_LEASES_ZERO",
            ),
            (
                "wrong error",
                {"generation_error_code": "RUNTIME_ROOT_MISSING"},
                "COMMITTED_ERROR_OTHER",
            ),
            (
                "wrong generation state",
                {"generation_state": "COMMITTED"},
                "COMMITTED_STATE_COMMITTED",
            ),
            (
                "target not newer",
                {"target_generation": 3},
                "TARGET_RELATION_MATCH",
            ),
            (
                "policy not advanced",
                {"policy_project_generation": 3},
                "POLICY_TARGET_RELATION_BEHIND",
            ),
            (
                "unsafe open generation",
                {"unsafe_non_disposed_other_count": 1},
                "UNSAFE_NON_DISPOSED_OTHERS_ONE",
            ),
            (
                "active committed lease",
                {"active_current_lease_count": 1},
                "ACTIVE_CURRENT_LEASES_ONE",
            ),
            (
                "target not prepared",
                {"target_generation_state": "PREPARING"},
                "TARGET_STATE_PREPARING",
            ),
            (
                "target wrong base",
                {"target_base_generation": 2},
                "TARGET_BASE_RELATION_BEHIND",
            ),
            (
                "target state absent but base present",
                {"target_generation_state": None},
                "TARGET_STATE_ABSENT",
            ),
            (
                "target prepared but base absent",
                {"target_base_generation": None},
                "TARGET_BASE_RELATION_ABSENT",
            ),
        )
        for label, mutation, expected_diagnostic in mutations:
            with self.subTest(label=label):
                row = {**base, **mutation}
                cursor = _WindowCursor(
                    [],
                    policy_exists=True,
                    candidate_rows=[],
                    project_schema_exists=True,
                    project_rows=[row],
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
                            3,
                            0,
                            tzinfo=ZoneInfo("Asia/Shanghai"),
                        ),
                    )

                self.assertEqual(1, result)
                rendered = " ".join(str(call) for call in print_mock.call_args_list)
                self.assertIn("PROJECT_SCHEDULE_RUNTIME_INVALID", rendered)
                self.assertIn("CHECK_PROJECT_STATE_INVALID", rendered)
                self.assertIn("PROJECT_STATE_UPGRADING", rendered)
                self.assertIn(expected_diagnostic, rendered)
                self.assertNotIn("arrive_list", rendered)

    def test_scheduled_write_runtime_diagnostic_redacts_unexpected_values(self):
        row = {
            "task_id": "SENSITIVE_TASK_ID_SENTINEL",
            "automation_id": "SENSITIVE_PROJECT_ID_SENTINEL",
            "automation_generation": 3,
            "tool_name": "automation.SENSITIVE_PROJECT_ID_SENTINEL.run",
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "committed_generation": 3,
            "target_generation": "SENSITIVE_TARGET_GENERATION_SENTINEL",
            "project_enabled": 1,
            "project_state": "SENSITIVE_PROJECT_STATE_SENTINEL",
            "reconcile_state": "SENSITIVE_RECONCILE_STATE_SENTINEL",
            "policy_mode": "PROJECT_FULL_AUTO",
            "policy_project_generation": "SENSITIVE_POLICY_GENERATION_SENTINEL",
            "max_generation": "SENSITIVE_MAX_GENERATION_SENTINEL",
            "non_disposed_other_count": "SENSITIVE_OPEN_GENERATION_COUNT_SENTINEL",
            "unsafe_non_disposed_other_count": "SENSITIVE_UNSAFE_GENERATION_SENTINEL",
            "active_current_lease_count": "SENSITIVE_ACTIVE_LEASE_SENTINEL",
            "generation_state": "SENSITIVE_COMMITTED_STATE_SENTINEL",
            "generation_error_code": "SENSITIVE_ERROR_SENTINEL",
            "target_generation_state": "SENSITIVE_TARGET_STATE_SENTINEL",
            "target_base_generation": "SENSITIVE_TARGET_BASE_SENTINEL",
            "unknown_write_count": "SENSITIVE_LEASE_COUNT_SENTINEL",
            "snapshot_json": {
                "arguments": {"secret": "SENSITIVE_ARGUMENT_SENTINEL"},
                "actor": "SENSITIVE_ACTOR_SENTINEL",
                "contract_hash": "SENSITIVE_HASH_SENTINEL",
            },
        }
        cursor = _WindowCursor(
            [],
            policy_exists=True,
            candidate_rows=[],
            project_schema_exists=True,
            project_rows=[row],
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
                    3,
                    0,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

        self.assertEqual(1, result)
        rendered = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertIn("CHECK_PROJECT_STATE_INVALID", rendered)
        self.assertIn("PROJECT_STATE_OTHER", rendered)
        self.assertIn("RECONCILE_STATE_OTHER", rendered)
        self.assertIn("TARGET_RELATION_INVALID", rendered)
        self.assertIn("POLICY_TARGET_RELATION_INVALID", rendered)
        self.assertIn("MAX_COMMITTED_RELATION_INVALID", rendered)
        self.assertIn("TARGET_MAX_NEXT_INVALID", rendered)
        self.assertIn("NON_DISPOSED_OTHERS_INVALID", rendered)
        self.assertIn("UNSAFE_NON_DISPOSED_OTHERS_INVALID", rendered)
        self.assertIn("ACTIVE_CURRENT_LEASES_INVALID", rendered)
        self.assertIn("COMMITTED_STATE_OTHER", rendered)
        self.assertIn("COMMITTED_ERROR_OTHER", rendered)
        self.assertIn("TARGET_STATE_OTHER", rendered)
        self.assertIn("TARGET_BASE_RELATION_INVALID", rendered)
        self.assertIn("UNKNOWN_WRITE_LEASES_INVALID", rendered)
        for secret in (
            "SENSITIVE_TASK_ID_SENTINEL",
            "SENSITIVE_PROJECT_ID_SENTINEL",
            "SENSITIVE_TARGET_GENERATION_SENTINEL",
            "SENSITIVE_PROJECT_STATE_SENTINEL",
            "SENSITIVE_RECONCILE_STATE_SENTINEL",
            "SENSITIVE_POLICY_GENERATION_SENTINEL",
            "SENSITIVE_MAX_GENERATION_SENTINEL",
            "SENSITIVE_OPEN_GENERATION_COUNT_SENTINEL",
            "SENSITIVE_UNSAFE_GENERATION_SENTINEL",
            "SENSITIVE_ACTIVE_LEASE_SENTINEL",
            "SENSITIVE_COMMITTED_STATE_SENTINEL",
            "SENSITIVE_ERROR_SENTINEL",
            "SENSITIVE_TARGET_STATE_SENTINEL",
            "SENSITIVE_TARGET_BASE_SENTINEL",
            "SENSITIVE_LEASE_COUNT_SENTINEL",
            "SENSITIVE_ARGUMENT_SENTINEL",
            "SENSITIVE_ACTOR_SENTINEL",
            "SENSITIVE_HASH_SENTINEL",
        ):
            self.assertNotIn(secret, rendered)

    def test_unknown_write_quarantine_still_validates_signed_schedule_contract(self):
        row = {
            "task_id": "arrive_list_0500",
            "automation_id": "arrive_list",
            "automation_generation": 3,
            "tool_name": "automation.arrive_list.run",
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "committed_generation": 3,
            "target_generation": 4,
            "project_enabled": 1,
            "project_state": "UPGRADING",
            "reconcile_state": "READY_TO_COMMIT",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "BLOCKED",
            "generation_error_code": "WRITE_OUTCOME_UNKNOWN",
            "target_generation_state": "PREPARED",
            "target_base_generation": 3,
            "policy_project_generation": 4,
            "max_generation": 4,
            "non_disposed_other_count": 1,
            "unsafe_non_disposed_other_count": 0,
            "active_current_lease_count": 0,
            "unknown_write_count": 1,
            "snapshot_json": {
                "automation_id": "arrive_list",
                "generation": 3,
                "execution_metadata": {
                    "compiled_invocations": {"scheduler": {"arguments": {}}},
                    "governance_anchor": {"operation_type": "external_write"},
                },
            },
        }
        invalid_snapshot = deepcopy(row["snapshot_json"])
        invalid_snapshot["execution_metadata"]["compiled_invocations"] = {}
        mutations = (
            (
                "missing scheduler contract",
                {"snapshot_json": invalid_snapshot},
                "PROJECT_SCHEDULE_CONTRACT_INVALID",
            ),
            ("empty cron", {"cron_expression": ""}, "PROJECT_SCHEDULE_CRON_INVALID"),
        )
        for label, mutation, expected_code in mutations:
            with self.subTest(label=label):
                cursor = _WindowCursor(
                    [],
                    policy_exists=True,
                    candidate_rows=[],
                    project_schema_exists=True,
                    project_rows=[{**row, **mutation}],
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
                            3,
                            0,
                            tzinfo=ZoneInfo("Asia/Shanghai"),
                        ),
                    )

                self.assertEqual(1, result)
                rendered = " ".join(str(call) for call in print_mock.call_args_list)
                self.assertIn(expected_code, rendered)
                self.assertNotIn("arrive_list", rendered)

if __name__ == "__main__":
    unittest.main()
