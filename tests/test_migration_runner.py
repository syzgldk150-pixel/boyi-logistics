from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import re
import sys
import types
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


class _PluginOwnershipCursor:
    def __init__(self, rows: list[dict], *, table_exists: bool = True) -> None:
        self.rows = rows
        self.table_exists = table_exists
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
        if "FROM information_schema.TABLES" in normalized:
            self._row = {"exists": 1} if self.table_exists else None
        elif normalized.startswith("SELECT project.automation_id"):
            self._rows = list(self.rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _PluginOwnershipConnection:
    def __init__(self, cursor: _PluginOwnershipCursor) -> None:
        self._cursor = cursor
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class AutomationPluginInstallOwnershipPreflightTests(unittest.TestCase):
    @staticmethod
    def _row(
        *,
        root: str = "/srv/boyi/plugins/installed",
        automation_id: str = "legacy_action",
        plugin_id: str = "legacy_action",
        version: str = "1.2.3",
        state: str = "INSTALLED",
    ) -> dict:
        package_sha256 = "a" * 64
        manifest = {"plugin_id": plugin_id, "version": version}
        manifest_sha256 = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        metadata = {
            "signing_key_id": "release-key",
            "python_relative": "venv/bin/python",
            "archive_relative": "package-archive.zip",
            "archive_sha256": package_sha256,
            "package_files": [],
            "install_root": f"{root}/{plugin_id}/{version}-{manifest_sha256[:12]}",
        }
        metadata_sha256 = hashlib.sha256(
            json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "automation_id": automation_id,
            "plugin_id": plugin_id,
            "version": version,
            "package_sha256": package_sha256,
            "manifest_sha256": manifest_sha256,
            "manifest_json": json.dumps(manifest),
            "trust_source": "ed25519_first_party",
            "install_root_metadata_json": json.dumps(metadata),
            "install_root_metadata_sha256": metadata_sha256,
            "state": state,
        }

    def test_safe_identity_output_is_read_only_and_omits_metadata(self):
        runner = _load_runner()
        cursor = _PluginOwnershipCursor([self._row()])
        connection = _PluginOwnershipConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            status = runner.check_automation_plugin_install_ownership(
                lambda: connection,
                "/srv/boyi/plugins/installed",
            )

        self.assertEqual(0, status)
        lines = output.getvalue().splitlines()
        self.assertEqual("automation_plugin_install_ownership=ok count=1", lines[0])
        self.assertRegex(
            lines[1],
            r"^automation_plugin_install_owner plugin_id=legacy_action "
            r"version=1\.2\.3 package_sha256=[0-9a-f]{64} "
            r"manifest_sha256=[0-9a-f]{64}$",
        )
        self.assertNotIn("/srv/", output.getvalue())
        self.assertNotIn("signing_key_id", output.getvalue())
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        self.assertEqual("START TRANSACTION READ ONLY", cursor.calls[0][0])

    def test_absent_schema_has_an_exact_empty_contract(self):
        runner = _load_runner()
        cursor = _PluginOwnershipCursor([], table_exists=False)
        connection = _PluginOwnershipConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            status = runner.check_automation_plugin_install_ownership(
                lambda: connection,
                "/srv/boyi/plugins/installed",
            )

        self.assertEqual(0, status)
        self.assertEqual(
            "automation_plugin_install_ownership=ok count=0\n",
            output.getvalue(),
        )

    def test_self_rehashed_wrong_root_is_rejected_without_value_disclosure(self):
        runner = _load_runner()
        row = self._row(root="/wrong/root")
        cursor = _PluginOwnershipCursor([row])
        connection = _PluginOwnershipConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            status = runner.check_automation_plugin_install_ownership(
                lambda: connection,
                "/srv/boyi/plugins/installed",
            )

        self.assertEqual(1, status)
        self.assertEqual(
            "automation_plugin_install_ownership=blocked "
            "reason=INVALID_DATABASE_STATE count=1\n",
            output.getvalue(),
        )
        self.assertNotIn("wrong", output.getvalue())

    def test_project_references_are_deduplicated_and_retired_orphans_are_not_queried(self):
        runner = _load_runner()
        rows = [
            self._row(
                automation_id=f"project_{index:02d}",
                plugin_id=f"plugin_{min(index, 14):02d}",
                version=f"1.0.{min(index, 14)}",
            )
            for index in range(1, 17)
        ]
        cursor = _PluginOwnershipCursor(rows)
        connection = _PluginOwnershipConnection(cursor)
        output = io.StringIO()

        with redirect_stdout(output):
            status = runner.check_automation_plugin_install_ownership(
                lambda: connection,
                "/srv/boyi/plugins/installed",
            )

        self.assertEqual(0, status)
        self.assertEqual(
            "automation_plugin_install_ownership=ok count=14",
            output.getvalue().splitlines()[0],
        )
        query = next(
            sql
            for sql, _params in cursor.calls
            if sql.startswith("SELECT project.automation_id")
        )
        self.assertIn("FROM automation_projects AS project", query)
        self.assertIn("INNER JOIN automation_plugin_versions AS version", query)
        self.assertNotIn("RETIRED", query)

        retired = self._row(state="RETIRED")
        retired_cursor = _PluginOwnershipCursor([retired])
        retired_connection = _PluginOwnershipConnection(retired_cursor)
        retired_output = io.StringIO()
        with redirect_stdout(retired_output):
            retired_status = runner.check_automation_plugin_install_ownership(
                lambda: retired_connection,
                "/srv/boyi/plugins/installed",
            )
        self.assertEqual(1, retired_status)
        self.assertIn("reason=INVALID_DATABASE_STATE", retired_output.getvalue())

    def test_runner_cli_binds_root_only_to_the_ownership_mode(self):
        runner = _load_runner()
        with (
            patch.object(
                runner,
                "check_automation_plugin_install_ownership",
                return_value=7,
            ) as check,
            patch.object(
                sys,
                "argv",
                [
                    "run_migrations.py",
                    "--check-automation-plugin-install-ownership",
                    "--automation-plugin-install-root",
                    "/srv/boyi/plugins/installed",
                ],
            ),
        ):
            self.assertEqual(7, runner.main())
        check.assert_called_once_with(
            runner._connect,
            "/srv/boyi/plugins/installed",
        )

    def test_exact_path_loader_restores_helper_and_shared_namespaces(self):
        runner = _load_runner()
        helper_name = (
            "boyi_agent_scripts_automation_plugin_install_ownership_preflight"
        )
        helper_sentinel = types.ModuleType(helper_name)
        shared_sentinel = types.ModuleType("shared")
        shared_child_sentinel = types.ModuleType("shared.sentinel")
        with patch.dict(
            sys.modules,
            {
                helper_name: helper_sentinel,
                "shared": shared_sentinel,
                "shared.sentinel": shared_child_sentinel,
            },
        ):
            loaded = runner._load_script_helper(
                "automation_plugin_install_ownership_preflight.py"
            )
            self.assertTrue(
                callable(loaded.check_automation_plugin_install_ownership)
            )
            self.assertIs(sys.modules[helper_name], helper_sentinel)
            self.assertIs(sys.modules["shared"], shared_sentinel)
            self.assertIs(sys.modules["shared.sentinel"], shared_child_sentinel)


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
        self.project_rows = [] if project_rows is None else project_rows
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
            self._row = {"exists": 1} if exists else None
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
        self._row = None
        self._rows = []
        if normalized == "SELECT VERSION() AS version":
            self._row = {"version": "8.0.44"}
        elif "FROM information_schema.TABLES" in normalized:
            table_name = params[0] if params else ""
            self._row = {"exists": 1} if table_name in self.tables else None
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

    def test_017_restore_replaces_complete_rows_and_makes_upgrade_reapplicable(self):
        cursor = _ReleaseRestoreCursor()
        connection = _RestoreConnection(cursor)
        with patch.object(self.runner, "_connect", return_value=connection):
            result = self.runner.restore_scheduled_task_contract_upgrade()

        self.assertEqual(0, result)
        self.assertTrue(connection.begun)
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        restore = next(
            sql for sql, _ in cursor.calls if sql.startswith("INSERT INTO scheduled_tasks")
        )
        self.assertIn("FROM scheduled_task_contract_upgrade_backup_017", restore)
        self.assertIn("configuration_version = VALUES(configuration_version)", restore)
        self.assertIn("updated_at = VALUES(updated_at)", restore)
        lock_statements = [
            sql
            for sql, _ in cursor.calls
            if sql.endswith("FOR UPDATE")
        ]
        self.assertTrue(
            any(
                sql.startswith("SELECT marker.task_id")
                and "scheduled_task_contract_upgrade_created_017" in sql
                for sql in lock_statements
            )
        )
        self.assertTrue(
            any(
                sql.startswith("SELECT backup.id")
                and "scheduled_task_contract_upgrade_backup_017" in sql
                for sql in lock_statements
            )
        )
        self.assertTrue(
            any(
                sql.startswith("SELECT task.id")
                and "FROM scheduled_tasks AS task" in sql
                for sql in lock_statements
            )
        )
        unsafe_index = next(
            index
            for index, (sql, _) in enumerate(cursor.calls)
            if sql.startswith("SELECT COUNT(*) AS unsafe_count")
        )
        for prefix in ("SELECT marker.task_id", "SELECT backup.id", "SELECT task.id"):
            lock_index = next(
                index
                for index, (sql, _) in enumerate(cursor.calls)
                if sql.startswith(prefix) and sql.endswith("FOR UPDATE")
            )
            self.assertLess(lock_index, unsafe_index)
        marker_delete = next(
            sql
            for sql, _ in cursor.calls
            if sql.startswith("DELETE task FROM scheduled_tasks AS task")
        )
        self.assertIn(
            "INNER JOIN scheduled_task_contract_upgrade_created_017 AS marker",
            marker_delete,
        )
        self.assertIn("backup.id IS NULL", marker_delete)
        self.assertIn("BINARY task.name = BINARY '财务启动缺口扫描'", marker_delete)
        self.assertIn(
            "BINARY task.tool_name = BINARY 'sync_finance_bills'",
            marker_delete,
        )
        self.assertIn(
            "BINARY task.cron_expression = BINARY '@startup'",
            marker_delete,
        )
        self.assertIn("task.created_at = marker.task_created_at", marker_delete)
        self.assertIn("task.updated_at = marker.task_updated_at", marker_delete)
        self.assertIn("task.configuration_version =", marker_delete)
        self.assertIn("'_startup_catchup', CAST('true' AS JSON)", marker_delete)
        self.assertIn(
            ("DELETE FROM schema_migrations WHERE BINARY version=BINARY %s", ("017",)),
            cursor.calls,
        )
        self.assertIn(
            ("DELETE FROM scheduled_task_contract_upgrade_backup_017", None),
            cursor.calls,
        )
        self.assertIn(
            ("DELETE FROM scheduled_task_contract_upgrade_created_017", None),
            cursor.calls,
        )
        self.assertTrue(connection.closed)

    def test_017_restore_refuses_wrong_bootstrap_cleanup_order(self):
        cases = (
            ({"bootstrap_policy_rows": 1}, "finance startup policy remains"),
            ({"bootstrap_event_rows": 1}, "audit or completion marker remains"),
            ({"bootstrap_domain_rows": 1}, "domain or outbox state remains"),
        )
        for cursor_options, message in cases:
            with self.subTest(message=message):
                cursor = _ReleaseRestoreCursor(**cursor_options)
                connection = _RestoreConnection(cursor)
                with (
                    patch.object(self.runner, "_connect", return_value=connection),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    self.runner.restore_scheduled_task_contract_upgrade()

                self.assertTrue(connection.begun)
                self.assertFalse(connection.committed)
                self.assertTrue(connection.rolled_back)
                self.assertFalse(
                    any(
                        sql.startswith("DELETE FROM schema_migrations")
                        or sql.startswith(
                            "DELETE FROM scheduled_task_contract_upgrade_backup_017"
                        )
                        or sql.startswith(
                            "DELETE FROM scheduled_task_contract_upgrade_created_017"
                        )
                        for sql, _ in cursor.calls
                    )
                )

    def test_017_restore_refuses_concurrent_equivalent_residual_after_delete(self):
        # A surviving row is the observable state a SELECT -> DELETE race would
        # have produced before the marker/task/backup locking contract.
        cursor = _ReleaseRestoreCursor(remaining_created_count=1)
        connection = _RestoreConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            self.assertRaisesRegex(RuntimeError, "remained after delete"),
        ):
            self.runner.restore_scheduled_task_contract_upgrade()

        self.assertTrue(connection.begun)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(
            any(
                sql.startswith("SELECT COUNT(*) AS remaining_created_count")
                for sql, _ in cursor.calls
            )
        )
        self.assertFalse(
            any(
                sql.startswith("DELETE FROM schema_migrations")
                or sql.startswith(
                    "DELETE FROM scheduled_task_contract_upgrade_backup_017"
                )
                or sql.startswith(
                    "DELETE FROM scheduled_task_contract_upgrade_created_017"
                )
                for sql, _ in cursor.calls
            )
        )

    def test_017_restore_refuses_to_delete_a_changed_marker_owned_row(self):
        cursor = _ReleaseRestoreCursor(unsafe_created_count=1)
        connection = _RestoreConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            self.assertRaisesRegex(
                RuntimeError,
                "created finance startup row no longer matches",
            ),
        ):
            self.runner.restore_scheduled_task_contract_upgrade()

        self.assertTrue(connection.begun)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(
            any(
                sql.startswith("DELETE task FROM scheduled_tasks AS task")
                for sql, _ in cursor.calls
            )
        )
        self.assertTrue(connection.closed)

    def test_017_restore_is_transactional_on_failure(self):
        cursor = _ReleaseRestoreCursor(fail_restore=True)
        connection = _RestoreConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            self.assertRaisesRegex(RuntimeError, "injected 017 restore failure"),
        ):
            self.runner.restore_scheduled_task_contract_upgrade()

        self.assertTrue(connection.begun)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_016_restore_uses_the_same_complete_row_capture_contract(self):
        cursor = _ReleaseRestoreCursor()
        connection = _RestoreConnection(cursor)
        with patch.object(self.runner, "_connect", return_value=connection):
            result = self.runner.restore_daily_sign_single_tms_account()

        self.assertEqual(0, result)
        restore = next(
            sql for sql, _ in cursor.calls if sql.startswith("INSERT INTO scheduled_tasks")
        )
        self.assertIn("FROM daily_sign_single_tms_backup_016", restore)
        self.assertIn(
            ("DELETE FROM schema_migrations WHERE BINARY version=BINARY %s", ("016",)),
            cursor.calls,
        )
        self.assertIn(
            ("DELETE FROM daily_sign_single_tms_backup_016", None),
            cursor.calls,
        )

    def test_policy_bootstrap_restore_is_migration_owned_and_admin_safe(self):
        cursor = _ReleaseRestoreCursor()
        connection = _RestoreConnection(cursor)
        with patch.object(self.runner, "_connect", return_value=connection):
            result = self.runner.restore_control_plane_policy_bootstrap()

        self.assertEqual(0, result)
        self.assertTrue(connection.committed)
        statements = [sql for sql, _ in cursor.calls]
        for prefix in (
            "DELETE consumption FROM event_consumptions",
            "DELETE outbox FROM outbox_events",
            "DELETE domain_event FROM domain_events",
        ):
            statement = next(sql for sql in statements if sql.startswith(prefix))
            self.assertIn("policy_event.actor_id = %s", statement)
            self.assertIn("policy_event.reason = 'control_plane_v1_bootstrap'", statement)
            self.assertIn("domain_event.source_system = 'agent'", statement)
            self.assertIn(
                "BINARY domain_event.source_event_id = BINARY CONCAT(",
                statement,
            )
            self.assertIn(
                "BINARY domain_event.entity_id = BINARY policy_event.task_id",
                statement,
            )
        consumption_delete = next(
            sql
            for sql in statements
            if sql.startswith("DELETE consumption FROM event_consumptions")
        )
        self.assertIn(
            "BINARY domain_event.event_id = BINARY consumption.event_id",
            consumption_delete,
        )
        outbox_delete = next(
            sql
            for sql in statements
            if sql.startswith("DELETE outbox FROM outbox_events")
        )
        self.assertIn(
            "BINARY domain_event.event_id = BINARY outbox.event_id",
            outbox_delete,
        )
        policy_delete = next(
            sql
            for sql in statements
            if sql.startswith("DELETE policy FROM scheduled_task_approval_policies")
        )
        self.assertIn("policy.mode = 'EXACT_SCHEDULE_EXEMPT'", policy_delete)
        self.assertIn("policy.approved_by_actor_id = %s", policy_delete)
        self.assertIn("event.reason = 'control_plane_v1_bootstrap'", policy_delete)
        event_delete = next(
            sql
            for sql in statements
            if sql.startswith("DELETE FROM scheduled_task_approval_policy_events")
        )
        self.assertIn("control_plane_v1_bootstrap_complete", event_delete)
        self.assertFalse(any("approved_by_actor_role = 'super_admin'" in sql for sql in statements))

    def test_policy_bootstrap_restore_rejects_migration_exact_without_event(self):
        cursor = _ReleaseRestoreCursor(orphan_policy_count=1)
        connection = _RestoreConnection(cursor)
        with (
            patch.object(self.runner, "_connect", return_value=connection),
            self.assertRaisesRegex(
                RuntimeError,
                "MIGRATION_EXACT_POLICY_BOOTSTRAP_EVENT_MISSING",
            ),
        ):
            self.runner.restore_control_plane_policy_bootstrap()

        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertFalse(
            any(
                sql.startswith("DELETE FROM scheduled_task_approval_policy_events")
                for sql, _ in cursor.calls
            )
        )

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

    def test_release_status_commands_distinguish_clean_dirty_applied_and_marker(self):
        cases = (
            (set(), set(), "pending_clean"),
            (set(), {"scheduled_task_contract_upgrade_backup_017"}, "pending_dirty"),
            (set(), {"scheduled_task_contract_upgrade_created_017"}, "pending_dirty"),
            ({"017"}, set(), "applied"),
        )
        for applied, dirty, expected in cases:
            with self.subTest(expected=expected):
                cursor = _ReleaseRestoreCursor(
                    applied_versions=applied,
                    dirty_backups=dirty,
                )
                connection = _WindowConnection(cursor)
                with (
                    patch.object(self.runner, "_connect", return_value=connection),
                    patch("builtins.print") as print_mock,
                ):
                    result = self.runner.report_scheduled_task_contract_upgrade_status()
                self.assertEqual(0, result)
                print_mock.assert_called_once_with(
                    f"scheduled_task_contract_upgrade_status={expected}"
                )

        for marker_present, expected in ((False, "absent"), (True, "present")):
            cursor = _ReleaseRestoreCursor(marker_present=marker_present)
            connection = _WindowConnection(cursor)
            with (
                patch.object(self.runner, "_connect", return_value=connection),
                patch("builtins.print") as print_mock,
            ):
                result = self.runner.report_control_plane_policy_bootstrap_marker_status()
            self.assertEqual(0, result)
            print_mock.assert_called_once_with(
                f"control_plane_policy_bootstrap_marker_status={expected}"
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

    def test_release_manifest_is_exactly_69_with_two_expected_disabled_rows(self):
        manifest_ids = self.runner._load_control_plane_reviewed_manifest_ids()

        self.assertEqual(69, len(manifest_ids))
        self.assertEqual(
            {"finance_bills_0010", "yunda_dispatch_forecast_1700"},
            self.runner.CONTROL_PLANE_REVIEWED_DISABLED_IDS,
        )
        self.assertIn("finance_startup_catchup", manifest_ids)
        self.assertEqual(67, self.runner.CONTROL_PLANE_REVIEWED_ENABLED_COUNT)

    def test_manifest_policy_validation_supplies_dynamic_required_schema_field_only(self):
        registry = self.runner._load_control_plane_tool_registry()
        approval = self.runner._load_scheduled_task_approval_contract_module()
        scheduled_task_contracts = (
            self.runner._load_control_plane_scheduled_task_contract_module()
        )
        profile = scheduled_task_contracts.APPROVED_SCHEDULED_TASK_PROFILES[
            "site_send"
        ]
        task_id = "site_send_0500"
        static_arguments = dict(profile.approved_arguments)
        task = {
            "id": task_id,
            "tool_name": profile.tool_name,
            "tool_params": static_arguments,
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "configuration_version": 1,
        }
        exact = self._exact_policy(
            task=task,
            profile=profile,
            registry=registry,
            approval=approval,
        )
        validation_calls = []

        class RecordingRegistry:
            def get_capability(self, tool_name):
                return registry.get_capability(tool_name)

            def validate_arguments(self, tool_name, arguments):
                validation_calls.append((tool_name, dict(arguments)))
                return registry.validate_arguments(tool_name, arguments)

        self.runner._validate_control_plane_policy_states(
            [exact],
            enabled_ids={task_id},
            registry=RecordingRegistry(),
            approval_contracts=approval,
            profile_by_task_id={task_id: profile},
            arguments_for_schema_validation=(
                scheduled_task_contracts._arguments_for_schema_validation
            ),
        )

        self.assertEqual(1, len(validation_calls))
        self.assertIn("target_date", validation_calls[0][1])
        self.assertEqual(profile.approved_arguments, exact["tool_params"])
        self.assertNotIn("target_date", exact["tool_params"])
        self.assertNotIn("target_date", static_arguments)

    def test_manifest_policy_validation_rejects_unsupported_dynamic_resolver(self):
        registry = self.runner._load_control_plane_tool_registry()
        approval = self.runner._load_scheduled_task_approval_contract_module()
        scheduled_task_contracts = (
            self.runner._load_control_plane_scheduled_task_contract_module()
        )
        approved_profile = (
            scheduled_task_contracts.APPROVED_SCHEDULED_TASK_PROFILES["site_send"]
        )
        profile = replace(
            approved_profile,
            dynamic_argument_rules={"target_date": "unsupported_resolver"},
        )
        task_id = "site_send_0500"
        task = {
            "id": task_id,
            "tool_name": profile.tool_name,
            "tool_params": dict(profile.approved_arguments),
            "cron_expression": "0 5 * * *",
            "enabled": 1,
            "configuration_version": 1,
        }
        exact = self._exact_policy(
            task=task,
            profile=profile,
            registry=registry,
            approval=approval,
        )

        with self.assertRaises(
            self.runner.ControlPlaneTaskCutoverPreflightError
        ) as error:
            self.runner._validate_control_plane_policy_states(
                [exact],
                enabled_ids={task_id},
                registry=registry,
                approval_contracts=approval,
                profile_by_task_id={task_id: profile},
                arguments_for_schema_validation=(
                    scheduled_task_contracts._arguments_for_schema_validation
                ),
            )

        self.assertEqual("CONTROL_PLANE_POLICY_NOT_ACTIVE", error.exception.code)

    def test_enabled_default_or_stale_exact_policy_cannot_pass_manifest_health(self):
        registry = self.runner._load_control_plane_tool_registry()
        approval = self.runner._load_scheduled_task_approval_contract_module()
        scheduled_task_contracts = (
            self.runner._load_control_plane_scheduled_task_contract_module()
        )
        profiles = scheduled_task_contracts.APPROVED_SCHEDULED_TASK_PROFILES
        task_id = "send_order_2359"
        profile = profiles["send_order"]
        capability = registry.get_capability(profile.tool_name)
        task = {
            "id": task_id,
            "tool_name": profile.tool_name,
            "tool_params": dict(profile.approved_arguments),
            "cron_expression": "59 23 * * *",
            "enabled": 1,
            "configuration_version": 1,
        }
        contract = approval.build_scheduled_task_contract(
            task,
            capability,
            dynamic_argument_rules=profile.dynamic_argument_rules,
        )
        exact = {
            "task_id": task_id,
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
        kwargs = {
            "enabled_ids": {task_id},
            "registry": registry,
            "approval_contracts": approval,
            "profile_by_task_id": {task_id: profile},
            "arguments_for_schema_validation": (
                scheduled_task_contracts._arguments_for_schema_validation
            ),
        }

        self.runner._validate_control_plane_policy_states([exact], **kwargs)
        self.runner._validate_control_plane_policy_states(
            [exact],
            **{**kwargs, "require_enabled_exact": True},
        )

        stale = {**exact, "configuration_version": 2}
        with self.assertRaises(
            self.runner.ControlPlaneTaskCutoverPreflightError
        ) as stale_error:
            self.runner._validate_control_plane_policy_states([stale], **kwargs)
        self.assertEqual("CONTROL_PLANE_POLICY_NOT_ACTIVE", stale_error.exception.code)

        default = {
            "task_id": task_id,
            "mode": "REQUIRE_EACH_RUN",
            "contract_hash": None,
            "contract_snapshot_json": None,
            "tool_contract_hash": None,
            "approved_by_actor_id": None,
            "approved_by_actor_role": None,
            "version": 1,
            "has_explaining_event": 0,
            "latest_event_reason": None,
        }
        with self.assertRaises(
            self.runner.ControlPlaneTaskCutoverPreflightError
        ) as default_error:
            self.runner._validate_control_plane_policy_states([default], **kwargs)
        self.assertEqual(
            "ENABLED_TASK_DEFAULT_POLICY_NOT_ALLOWED",
            default_error.exception.code,
        )

        admin_require = {
            **default,
            "approved_by_actor_id": "admin-1",
            "approved_by_actor_role": "super_admin",
            "version": 2,
            "has_explaining_event": 1,
            "latest_event_reason": "console_policy_change",
        }
        self.runner._validate_control_plane_policy_states([admin_require], **kwargs)
        credential_require = {
            **admin_require,
            "approved_by_actor_id": approval.ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
            "approved_by_actor_role": "system",
            "latest_event_reason": approval.ACCOUNT_CREDENTIAL_CHANGE_REASON,
        }
        self.runner._validate_control_plane_policy_states(
            [credential_require],
            **kwargs,
        )
        for require_policy in (admin_require, credential_require):
            with self.assertRaises(
                self.runner.ControlPlaneTaskCutoverPreflightError
            ) as initial_error:
                self.runner._validate_control_plane_policy_states(
                    [require_policy],
                    **{**kwargs, "require_enabled_exact": True},
                )
            self.assertEqual(
                "INITIAL_ENABLED_TASK_EXACT_POLICY_REQUIRED",
                initial_error.exception.code,
            )
        unreviewed_system = {
            **credential_require,
            "approved_by_actor_id": "system:other",
        }
        with self.assertRaises(
            self.runner.ControlPlaneTaskCutoverPreflightError
        ) as system_error:
            self.runner._validate_control_plane_policy_states(
                [unreviewed_system],
                **kwargs,
            )
        self.assertEqual(
            "ENABLED_TASK_DEFAULT_POLICY_NOT_ALLOWED",
            system_error.exception.code,
        )
        self.runner._validate_control_plane_policy_states(
            [default],
            **{**kwargs, "enabled_ids": set()},
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
            "reconcile_state": "PREPARING",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "BLOCKED",
            "generation_error_code": "WRITE_OUTCOME_UNKNOWN",
            "target_generation_state": "PREPARED",
            "target_base_generation": 3,
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
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))
        typed_query = next(
            sql for sql, _ in cursor.calls if sql.startswith("SELECT task.id AS task_id")
        )
        self.assertIn("FROM automation_project_generation_leases AS lease", typed_query)
        self.assertIn("lease.outcome='WRITE_OUTCOME_UNKNOWN'", typed_query)
        self.assertIn(
            "CAST( COALESCE(MAX(history.generation), 0) AS UNSIGNED )",
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
            "project_state": "UPGRADING",
            "reconcile_state": "PREPARING",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "BLOCKED",
            "generation_error_code": "WRITE_OUTCOME_UNKNOWN",
            "target_generation_state": None,
            "target_base_generation": None,
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

    def test_scheduled_write_window_excludes_staged_missing_target_runtime(self):
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
            "reconcile_state": "PREPARING",
            "policy_mode": "PROJECT_FULL_AUTO",
            "generation_state": "COMMITTED",
            "generation_error_code": None,
            "target_generation_state": None,
            "target_base_generation": None,
            "policy_project_generation": 4,
            "max_generation": 3,
            "non_disposed_other_count": 0,
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
            {"non_disposed_other_count": 1},
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
                "missing lease",
                {"unknown_write_count": 0},
                "UNKNOWN_WRITE_LEASES_ZERO",
            ),
            (
                "duplicate lease",
                {"unknown_write_count": 2},
                "UNKNOWN_WRITE_LEASES_MULTIPLE",
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

    def _applied_014_yunda_window_rows(
        self,
        *,
        disabled_message: str,
    ) -> tuple[list[dict], list[dict]]:
        internal = self.runner._load_control_plane_reviewed_task_contracts()
        clocks = self.runner._load_control_plane_clock_contracts()
        r7_contracts = self.runner._load_control_plane_r7_contracts()
        task_id = "yunda_send_waybills_2355"
        rows = [
            {
                "id": current_task_id,
                "tool_name": contract["tool_name"],
                "tool_params": dict(contract["canonical_arguments"]),
                "cron_expression": contract["cron_expression"],
                "enabled": 0 if current_task_id == task_id else 1,
                "last_status": "disabled" if current_task_id == task_id else None,
                "last_message": disabled_message if current_task_id == task_id else None,
                "configuration_version": 1 if current_task_id == task_id else None,
            }
            for current_task_id, contract in sorted(internal.items())
        ]
        rows.extend(
            {
                "id": current_task_id,
                "tool_name": "tms_query",
                "tool_params": self.runner._legacy_clock_arguments(
                    current_task_id,
                    contract["canonical_arguments"],
                ),
                "cron_expression": contract["cron_expression"],
                "enabled": 1,
            }
            for current_task_id, contract in sorted(clocks.items())
        )
        rows.extend(
            {
                "id": current_task_id,
                "tool_name": contract["tool_name"],
                "tool_params": dict(contract["canonical_arguments"]),
                "cron_expression": contract["cron_expression"],
                "enabled": 1,
            }
            for current_task_id, contract in sorted(r7_contracts.items())
        )
        backup_rows = [
            {
                "id": task_id,
                "tool_name": "sync_yunda_send_waybills",
                "tool_params": {
                    "account_id": "yunda_default",
                    "session_profile": "yunda",
                    "ensure_fields": False,
                    "target_date": "",
                },
                "cron_expression": internal[task_id]["cron_expression"],
                "enabled": 1,
            }
        ]
        return rows, backup_rows

    def test_applied_014_yunda_disable_proof_keeps_legacy_write_window(self):
        message = "reviewed-014-disabled-message"
        message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
        self.assertEqual(
            "19129e9c68d5e20050a7d8c8e8489f4f1313f9fb6188adc55229aaeacad9c0e3",
            self.runner.CONTROL_PLANE_APPLIED_014_YUNDA_DISABLED_MESSAGE_SHA256,
        )
        candidate_rows, backup_rows = self._applied_014_yunda_window_rows(
            disabled_message=message,
        )
        for policy_exists in (False, True):
            with self.subTest(policy_exists=policy_exists):
                cursor = _WindowCursor(
                    [],
                    policy_exists=policy_exists,
                    candidate_rows=deepcopy(candidate_rows),
                    cutover_backup_exists=True,
                    backup_rows=deepcopy(backup_rows),
                    applied_014=True,
                )
                connection = _WindowConnection(cursor)
                with (
                    patch.object(self.runner, "_connect", return_value=connection),
                    patch.object(
                        self.runner,
                        "CONTROL_PLANE_APPLIED_014_YUNDA_DISABLED_MESSAGE_SHA256",
                        message_sha256,
                    ),
                    patch("builtins.print") as print_mock,
                ):
                    result = self.runner.check_scheduled_write_window(
                        before_minutes=60,
                        after_minutes=45,
                        now=datetime(
                            2026,
                            8,
                            14,
                            23,
                            30,
                            tzinfo=ZoneInfo("Asia/Shanghai"),
                        ),
                    )

                self.assertEqual(1, result)
                self.assertTrue(connection.closed)
                self.assertTrue(
                    all(sql.startswith("SELECT") for sql, _ in cursor.calls)
                )
                rendered = " ".join(
                    str(call) for call in print_mock.call_args_list
                )
                self.assertIn("SCHEDULED_WRITE_WINDOW_ACTIVE", rendered)
                self.assertNotIn("yunda_send_waybills_2355", rendered)
                self.assertNotIn(message, rendered)

    def test_applied_014_yunda_disable_exception_fails_closed_without_exact_proof(self):
        message = "reviewed-014-disabled-message"
        message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
        mutations = (
            ("missing backup", lambda current, backup: backup.clear()),
            (
                "backup disabled",
                lambda current, backup: backup[0].update(enabled=0),
            ),
            (
                "backup wrong tool",
                lambda current, backup: backup[0].update(tool_name="changed_tool"),
            ),
            (
                "backup wrong cron",
                lambda current, backup: backup[0].update(
                    cron_expression="54 23 * * *"
                ),
            ),
            (
                "backup extra argument",
                lambda current, backup: backup[0]["tool_params"].update(
                    unexpected=True
                ),
            ),
            (
                "current extra argument",
                lambda current, backup: current["tool_params"].update(
                    unexpected=True
                ),
            ),
            (
                "current wrong status",
                lambda current, backup: current.update(last_status="success"),
            ),
            (
                "current wrong message",
                lambda current, backup: current.update(last_message="changed-message"),
            ),
            (
                "current enabled type",
                lambda current, backup: current.update(enabled="0"),
            ),
            (
                "current changed version",
                lambda current, backup: current.update(configuration_version=2),
            ),
        )

        for policy_exists in (False, True):
            for label, mutation in mutations:
                if label == "current changed version" and not policy_exists:
                    continue
                with self.subTest(policy_exists=policy_exists, label=label):
                    candidate_rows, backup_rows = self._applied_014_yunda_window_rows(
                        disabled_message=message,
                    )
                    current = next(
                        row
                        for row in candidate_rows
                        if row["id"] == "yunda_send_waybills_2355"
                    )
                    mutation(current, backup_rows)
                    cursor = _WindowCursor(
                        [],
                        policy_exists=policy_exists,
                        candidate_rows=deepcopy(candidate_rows),
                        cutover_backup_exists=True,
                        backup_rows=deepcopy(backup_rows),
                        applied_014=True,
                    )
                    connection = _WindowConnection(cursor)
                    with (
                        patch.object(self.runner, "_connect", return_value=connection),
                        patch.object(
                            self.runner,
                            "CONTROL_PLANE_APPLIED_014_YUNDA_DISABLED_MESSAGE_SHA256",
                            message_sha256,
                        ),
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
                    self.assertTrue(connection.closed)
                    self.assertTrue(
                        all(sql.startswith("SELECT") for sql, _ in cursor.calls)
                    )
                    rendered = " ".join(
                        str(call) for call in print_mock.call_args_list
                    )
                    self.assertIn("scheduled_write_window=blocked", rendered)
                    self.assertNotIn("yunda_send_waybills_2355", rendered)
                    self.assertNotIn(message, rendered)

    def test_applied_017_admin_disabled_yunda_does_not_reenter_014_exception(self):
        message = "administrator-disabled-after-cutover"
        candidate_rows, _backup_rows = self._applied_014_yunda_window_rows(
            disabled_message=message,
        )
        current = next(
            row
            for row in candidate_rows
            if row["id"] == "yunda_send_waybills_2355"
        )
        current.update(
            configuration_version=2,
            last_status="disabled",
            last_message=message,
        )
        cursor = _WindowCursor(
            [],
            policy_exists=True,
            candidate_rows=candidate_rows,
            cutover_backup_exists=True,
            backup_rows=[],
            applied_014=True,
            applied_017=True,
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
                    23,
                    30,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

        self.assertEqual(0, result)
        self.assertTrue(connection.closed)
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in cursor.calls))
        print_mock.assert_called_once_with(
            "scheduled_write_window=ok checked_schedules=15"
        )

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
            "scheduled_write_window=ok checked_schedules=16"
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
