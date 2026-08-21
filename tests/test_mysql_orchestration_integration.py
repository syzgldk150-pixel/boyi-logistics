from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from shared.scheduled_task_contracts import _arguments_for_schema_validation


RUN_MYSQL = os.getenv("RUN_MYSQL_INTEGRATION") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_migration_runner():
    path = PROJECT_ROOT / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("mysql_integration_migrations", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_automation_project_scenarios():
    path = PROJECT_ROOT / "tests" / "mysql_automation_project_scenarios.py"
    spec = importlib.util.spec_from_file_location("mysql_automation_project_scenarios", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_daily_sign_scenarios():
    path = PROJECT_ROOT / "tests" / "mysql_daily_sign_scenarios.py"
    spec = importlib.util.spec_from_file_location("mysql_daily_sign_scenarios", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_feishu_queue_scenarios():
    path = PROJECT_ROOT / "tests" / "mysql_feishu_queue_scenarios.py"
    spec = importlib.util.spec_from_file_location("mysql_feishu_queue_scenarios", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


AUTOMATION_PROJECT_SCENARIOS = _load_automation_project_scenarios()
DAILY_SIGN_SCENARIOS = _load_daily_sign_scenarios()
FEISHU_QUEUE_SCENARIOS = _load_feishu_queue_scenarios()


@unittest.skipUnless(RUN_MYSQL, "set RUN_MYSQL_INTEGRATION=1 for real MySQL 8 tests")
class MySqlOrchestrationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import pymysql

        cls.pymysql = pymysql
        cls.host = os.getenv("AGENT_DB_HOST", "127.0.0.1")
        cls.port = int(os.getenv("AGENT_DB_PORT", "3306"))
        cls.user = os.getenv("AGENT_DB_USER", "root")
        cls.password = os.getenv("AGENT_DB_PASS", "")
        cls.database = os.getenv("AGENT_DB_NAME", "agent_control_plane_test")
        if not re.fullmatch(r"(?:test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)", cls.database):
            raise RuntimeError("real MySQL tests require an explicitly test-scoped database name")
        cls.upgrade_database = f"{cls.database[:-5]}_upgrade_test"
        cls.partial_database = f"{cls.database[:-5]}_partial_test"
        cls.rollback_database = f"{cls.database[:-5]}_rollback_test"
        cls.collation_database = f"{cls.database[:-5]}_collation_test"
        cls.compat_database = f"{cls.database[:-5]}_schedule_compat_test"
        cls.contract_chain_database = f"{cls.database[:-5]}_contract_chain_test"
        cls.startup_contract_database = f"{cls.database[:-5]}_startup_contract_test"
        cls.policy_restore_database = f"{cls.database[:-5]}_policy_restore_test"
        cls.release_manifest_database = f"{cls.database[:-5]}_release_manifest_test"
        cls.protected_write_database = f"{cls.database[:-5]}_protected_write_test"
        cls.project_authorization_database = (
            f"{cls.database[:-5]}_project_authorization_test"
        )
        cls.project_authorization_partial_database = (
            f"{cls.database[:-5]}_project_authorization_partial_test"
        )
        cls.project_authorization_collation_database = (
            f"{cls.database[:-5]}_project_authorization_collation_test"
        )
        cls.project_approval_atomic_database = (
            f"{cls.database[:-5]}_project_approval_atomic_test"
        )
        cls.worker_dispatch_database = (
            f"{cls.database[:-5]}_worker_dispatch_test"
        )
        cls.daily_sign_readback_database = (
            f"{cls.database[:-5]}_daily_sign_readback_test"
        )
        cls.feishu_queue_recovery_database = (
            f"{cls.database[:-5]}_feishu_queue_recovery_test"
        )
        cls.databases = (
            cls.database,
            cls.upgrade_database,
            cls.partial_database,
            cls.rollback_database,
            cls.collation_database,
        )
        cls.all_databases = (
            *cls.databases,
            cls.compat_database,
            cls.contract_chain_database,
            cls.startup_contract_database,
            cls.policy_restore_database,
            cls.release_manifest_database,
            cls.protected_write_database,
            cls.project_authorization_database,
            cls.project_authorization_partial_database,
            cls.project_authorization_collation_database,
            cls.project_approval_atomic_database,
            cls.worker_dispatch_database,
            cls.daily_sign_readback_database,
            cls.feishu_queue_recovery_database,
        )
        cls.runner = _load_migration_runner()

        with cls._server_connection() as connection, connection.cursor() as cursor:
            for database in cls.all_databases:
                cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
                cursor.execute(
                    f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )

        cls._run_migrations(cls.database)
        cls._run_migrations(cls.database)
        cls._run_migrations(cls.database, check_only=True)

        cls._apply_through(cls.upgrade_database, "010")
        cls._run_migrations(cls.upgrade_database)

        cls._apply_through(cls.partial_database, "010")
        migration_011 = next(
            path
            for version, path in cls.runner.discover_migrations()
            if version == "011"
        )
        first_statements = cls.runner.split_sql_statements(
            migration_011.read_text(encoding="utf-8")
        )[:2]
        with cls._connection(cls.partial_database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for statement in first_statements:
                    cursor.execute(statement)
        cls._run_migrations(cls.partial_database)
        cls._run_migrations(cls.rollback_database)
        cls._run_migrations(cls.policy_restore_database)
        # This dedicated database exercises the legacy manifest validator.
        # Keep it immediately before 018 so the dispatcher cannot silently
        # substitute the post-018 71-row project contract for that boundary.
        cls._apply_through(cls.release_manifest_database, "017")
        cls._run_migrations(cls.protected_write_database)
        cls._apply_through(cls.project_authorization_database, "017")
        cls._apply_through(cls.project_authorization_partial_database, "017")
        cls._apply_through(cls.project_authorization_collation_database, "017")
        cls._apply_through(cls.project_approval_atomic_database, "017")
        cls._apply_through(cls.worker_dispatch_database, "017")
        cls._apply_through(cls.daily_sign_readback_database, "013")
        cls._apply_through(cls.feishu_queue_recovery_database, "017")
        cls._seed_required_project_resources(cls.feishu_queue_recovery_database)
        cls._apply_through(cls.feishu_queue_recovery_database, "022")
        cls._apply_through(cls.compat_database, "013")
        cls._apply_through(cls.contract_chain_database, "013")
        cls._apply_through(cls.startup_contract_database, "015")
        cls._apply_one(cls.startup_contract_database, "016")

        # The shared runtime table can predate the control plane under a
        # different database/table collation. Run 015's SQL directly twice:
        # the first pass creates the current-policy FK, while the second
        # simulates an interrupted earlier inline-FK migration without a
        # schema_migrations record. That rerun must drop before MODIFY and
        # re-add the current-policy FK after alignment.
        cls._apply_through(cls.collation_database, "014")
        with cls._connection(cls.collation_database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE scheduled_tasks MODIFY id VARCHAR(64) "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL"
                )
                migration_015 = next(
                    path
                    for version, path in cls.runner.discover_migrations()
                    if version == "015"
                )
                statements_015 = cls.runner.split_sql_statements(
                    migration_015.read_text(encoding="utf-8")
                )
                for _ in range(2):
                    for statement in statements_015:
                        cursor.execute(statement)

    @classmethod
    def tearDownClass(cls) -> None:
        with cls._server_connection() as connection, connection.cursor() as cursor:
            for database in cls.all_databases:
                cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")

    @classmethod
    @contextmanager
    def _server_connection(cls):
        connection = cls.pymysql.connect(
            host=cls.host,
            port=cls.port,
            user=cls.user,
            password=cls.password,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=cls.pymysql.cursors.DictCursor,
        )
        try:
            yield connection
        finally:
            connection.close()

    @classmethod
    @contextmanager
    def _connection(cls, database: str | None = None, *, autocommit: bool = False):
        connection = cls.pymysql.connect(
            host=cls.host,
            port=cls.port,
            user=cls.user,
            password=cls.password,
            database=database or cls.database,
            charset="utf8mb4",
            autocommit=autocommit,
            cursorclass=cls.pymysql.cursors.DictCursor,
        )
        try:
            yield connection
        finally:
            connection.close()

    @classmethod
    def _environment(cls, database: str) -> dict[str, str]:
        return {
            "AGENT_DB_HOST": cls.host,
            "AGENT_DB_PORT": str(cls.port),
            "AGENT_DB_USER": cls.user,
            "AGENT_DB_PASS": cls.password,
            "AGENT_DB_NAME": database,
            # The integration job is fully configured through explicit
            # variables and must never consult the project .env file.
            "MIGRATION_ENV_FILE": os.devnull,
        }

    @classmethod
    def _run_migrations(cls, database: str, *, check_only: bool = False) -> None:
        if not check_only:
            # Migration 018 intentionally refuses to guess its eight reviewed
            # external destinations. Integration databases therefore model the
            # deployment prerequisite explicitly after 017, instead of weakening
            # the production migration with test defaults.
            cls._apply_through(database, "017")
            cls._seed_required_project_resources(database)
        with patch.dict(os.environ, cls._environment(database), clear=False):
            cls.runner.run(check_only=check_only)

    @classmethod
    def _apply_through(cls, database: str, final_version: str) -> None:
        migrations = [
            item
            for item in cls.runner.discover_migrations()
            if item[0] <= final_version
        ]
        with cls._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(cls.runner.SCHEMA_MIGRATIONS_SQL)
                cursor.execute("SELECT version FROM schema_migrations")
                applied_versions = {
                    str(row["version"]) for row in cursor.fetchall()
                }
                for version, path in migrations:
                    if version in applied_versions:
                        continue
                    for statement in cls.runner.split_sql_statements(
                        path.read_text(encoding="utf-8")
                    ):
                        cursor.execute(statement)
                    cursor.execute(
                        "INSERT INTO schema_migrations (version, filename, checksum) "
                        "VALUES (%s, %s, %s)",
                        (version, path.name, cls.runner.migration_checksum(path)),
                    )
                    applied_versions.add(version)

    @classmethod
    def _seed_required_project_resources(cls, database: str) -> None:
        AUTOMATION_PROJECT_SCENARIOS.seed_required_project_resources(cls, database)

    @classmethod
    def _apply_one(cls, database: str, version: str) -> None:
        migration = next(
            path
            for discovered_version, path in cls.runner.discover_migrations()
            if discovered_version == version
        )
        with cls._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for statement in cls.runner.split_sql_statements(
                    migration.read_text(encoding="utf-8")
                ):
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) "
                    "VALUES (%s, %s, %s)",
                    (version, migration.name, cls.runner.migration_checksum(migration)),
                )

    @classmethod
    def _repository(cls, database: str | None = None):
        from shared.orchestration_repository import OrchestrationRepository

        def connect():
            return cls.pymysql.connect(
                host=cls.host,
                port=cls.port,
                user=cls.user,
                password=cls.password,
                database=database or cls.database,
                charset="utf8mb4",
                autocommit=False,
                cursorclass=cls.pymysql.cursors.DictCursor,
            )

        return OrchestrationRepository(connect, cls.pymysql.cursors.DictCursor)

    @staticmethod
    def _aggregate_rows(label: str) -> tuple[dict, dict, dict, dict, list[dict]]:
        command_id = str(uuid4())
        work_item_id = str(uuid4())
        run_id = str(uuid4())
        correlation_id = str(uuid4())
        now = datetime.now()
        return (
            {
                "command_id": command_id,
                "command_type": "integration_probe",
                "source": "integration",
                "actor_type": "system",
                "actor_id": "mysql-test",
                "actor_roles": [],
                "entity_refs": [],
                "parameters": {"label": label},
                "idempotency_key": f"integration:{label}:{command_id}",
                "correlation_id": correlation_id,
                "requested_at": now,
            },
            {
                "work_item_id": work_item_id,
                "type": "integration_probe",
                "title": f"MySQL integration {label}",
                "status": "OPEN",
                "priority": "NORMAL",
                "source": "integration",
                "dedupe_key": f"integration:{label}:{work_item_id}",
            },
            {
                "run_id": run_id,
                "run_no": 1,
                "status": "RECEIVED",
                "mode": "deterministic",
                "planner_kind": "deterministic",
                "correlation_id": correlation_id,
                "next_attempt_at": now,
            },
            {
                "event_id": str(uuid4()),
                "event_type": "agent.command.received",
                "schema_version": 1,
                "source_system": "integration",
                "occurred_at": now,
                "observed_at": now,
                "correlation_id": correlation_id,
                "payload": {"label": label},
            },
            [
                {
                    "consumer_name": "integration-consumer",
                    "topic": "agent.command.received",
                    "partition_key": work_item_id,
                }
            ],
        )

    def _seed_release_manifest(self, database: str) -> dict[str, object]:
        registry = self.runner._load_control_plane_tool_registry()
        approval = self.runner._load_scheduled_task_approval_contract_module()
        profiles = self.runner._load_control_plane_scheduled_task_profiles()
        profile_by_task_id = {
            task_id: profile
            for profile in profiles.values()
            for task_id in profile.approved_task_ids
        }
        task_contracts: dict[str, dict] = {}
        for loader in (
            self.runner._load_control_plane_reviewed_task_contracts,
            self.runner._load_control_plane_optional_task_contracts,
            self.runner._load_control_plane_clock_contracts,
            self.runner._load_control_plane_r7_contracts,
        ):
            task_contracts.update(loader())
        expected_ids = self.runner._load_control_plane_reviewed_manifest_ids()
        self.assertEqual(expected_ids, set(task_contracts))

        exact_contracts: dict[str, object] = {}
        with self._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for task_id in sorted(expected_ids):
                    reviewed = task_contracts[task_id]
                    arguments = dict(reviewed["canonical_arguments"])
                    enabled = task_id not in self.runner.CONTROL_PLANE_REVIEWED_DISABLED_IDS
                    task = {
                        "id": task_id,
                        "tool_name": reviewed["tool_name"],
                        "tool_params": arguments,
                        "cron_expression": reviewed["cron_expression"],
                        "enabled": enabled,
                        "configuration_version": 1,
                    }
                    cursor.execute(
                        "INSERT INTO scheduled_tasks "
                        "(id, name, tool_name, tool_params, cron_expression, enabled, "
                        "configuration_version) VALUES (%s, %s, %s, "
                        "CAST(%s AS JSON), %s, %s, %s)",
                        (
                            task_id,
                            f"release manifest {task_id}",
                            task["tool_name"],
                            json.dumps(
                                arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            task["cron_expression"],
                            enabled,
                            task["configuration_version"],
                        ),
                    )
                    if not enabled:
                        cursor.execute(
                            "INSERT INTO scheduled_task_approval_policies (task_id) "
                            "VALUES (%s)",
                            (task_id,),
                        )
                        continue

                    profile = profile_by_task_id[task_id]
                    capability = registry.get_capability(task["tool_name"])
                    registry.validate_arguments(
                        task["tool_name"],
                        _arguments_for_schema_validation(
                            arguments,
                            profile.dynamic_argument_rules,
                        ),
                    )
                    exact = approval.build_scheduled_task_contract(
                        task,
                        capability,
                        dynamic_argument_rules=profile.dynamic_argument_rules,
                        allowed_special_cron=profile.cron_expression,
                    )
                    exact_contracts[task_id] = exact
                    snapshot = json.dumps(
                        exact.snapshot,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    cursor.execute(
                        "INSERT INTO scheduled_task_approval_policies "
                        "(task_id, mode, contract_hash, contract_snapshot_json, "
                        "tool_contract_hash, approved_by_actor_id, "
                        "approved_by_actor_role, approved_at, version) "
                        "VALUES (%s, 'EXACT_SCHEDULE_EXEMPT', %s, CAST(%s AS JSON), "
                        "%s, %s, %s, NOW(6), 2)",
                        (
                            task_id,
                            exact.contract_hash,
                            snapshot,
                            exact.tool_contract_hash,
                            self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ID,
                            self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                        ),
                    )
                    cursor.execute(
                        "INSERT INTO scheduled_task_approval_policy_events "
                        "(task_id, from_mode, to_mode, contract_hash, "
                        "contract_snapshot_json, tool_contract_hash, actor_id, "
                        "actor_role, reason, correlation_id, request_id) "
                        "VALUES (%s, 'REQUIRE_EACH_RUN', 'EXACT_SCHEDULE_EXEMPT', "
                        "%s, CAST(%s AS JSON), %s, %s, %s, "
                        "'control_plane_v1_bootstrap', %s, %s)",
                        (
                            task_id,
                            exact.contract_hash,
                            snapshot,
                            exact.tool_contract_hash,
                            self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ID,
                            self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                            str(uuid4()),
                            str(uuid4()),
                        ),
                    )
                cursor.execute(
                    "INSERT INTO scheduled_task_approval_policy_events "
                    "(task_id, to_mode, actor_id, actor_role, reason, "
                    "correlation_id, request_id) "
                    "VALUES (%s, 'REQUIRE_EACH_RUN', %s, %s, "
                    "'control_plane_v1_bootstrap_complete', %s, %s)",
                    (
                        self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID,
                        self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ID,
                        self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                        self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
                        self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
                    ),
                )
        self.assertEqual(
            self.runner.CONTROL_PLANE_REVIEWED_ENABLED_COUNT,
            len(exact_contracts),
        )
        return exact_contracts

    def _set_manifest_require_policy(
        self,
        database: str,
        task_id: str,
        *,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> None:
        with self._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_task_approval_policies SET "
                    "mode='REQUIRE_EACH_RUN', contract_hash=NULL, "
                    "contract_snapshot_json=NULL, tool_contract_hash=NULL, "
                    "approved_by_actor_id=%s, approved_by_actor_role=%s, "
                    "approved_at=NOW(6), version=version+1 WHERE task_id=%s",
                    (actor_id, actor_role, task_id),
                )
                self.assertEqual(1, cursor.rowcount)
                cursor.execute(
                    "INSERT INTO scheduled_task_approval_policy_events "
                    "(task_id, from_mode, to_mode, actor_id, actor_role, reason, "
                    "correlation_id, request_id) "
                    "VALUES (%s, 'EXACT_SCHEDULE_EXEMPT', 'REQUIRE_EACH_RUN', "
                    "%s, %s, %s, %s, %s)",
                    (task_id, actor_id, actor_role, reason, str(uuid4()), str(uuid4())),
                )

    def _restore_manifest_exact_policy(
        self,
        database: str,
        task_id: str,
        exact: object,
    ) -> None:
        snapshot = json.dumps(
            exact.snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_task_approval_policies SET "
                    "mode='EXACT_SCHEDULE_EXEMPT', contract_hash=%s, "
                    "contract_snapshot_json=CAST(%s AS JSON), tool_contract_hash=%s, "
                    "approved_by_actor_id=%s, approved_by_actor_role=%s, "
                    "approved_at=NOW(6), version=version+1 WHERE task_id=%s",
                    (
                        exact.contract_hash,
                        snapshot,
                        exact.tool_contract_hash,
                        self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ID,
                        self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                        task_id,
                    ),
                )
                self.assertEqual(1, cursor.rowcount)
                cursor.execute(
                    "INSERT INTO scheduled_task_approval_policy_events "
                    "(task_id, from_mode, to_mode, contract_hash, "
                    "contract_snapshot_json, tool_contract_hash, actor_id, "
                    "actor_role, reason, correlation_id, request_id) "
                    "VALUES (%s, 'REQUIRE_EACH_RUN', 'EXACT_SCHEDULE_EXEMPT', "
                    "%s, CAST(%s AS JSON), %s, %s, %s, "
                    "'control_plane_v1_bootstrap', %s, %s)",
                    (
                        task_id,
                        exact.contract_hash,
                        snapshot,
                        exact.tool_contract_hash,
                        self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ID,
                        self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                        str(uuid4()),
                        str(uuid4()),
                    ),
                )

    def test_empty_upgrade_and_partial_migrations_are_reentrant(self):
        fully_migrated_databases = (
            self.database,
            self.upgrade_database,
            self.partial_database,
            self.rollback_database,
        )
        for database in fully_migrated_databases:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
                versions = [str(row["version"]) for row in cursor.fetchall()]
            self.assertEqual(
                [version for version, _ in self.runner.discover_migrations()],
                versions,
            )

    def test_json_unique_foreign_key_and_transaction_rollback(self):
        repository = self._repository()
        repository.validate_mysql8()
        repository.validate_schema()
        command, item, run, event, outbox = self._aggregate_rows("rollback")
        event.pop("event_type")

        with self.assertRaisesRegex(ValueError, "event_type is required"):
            with repository.unit_of_work() as uow:
                uow.command_gateway_create(command, item, run, event, outbox)

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM agent_commands WHERE command_id=%s",
                (command["command_id"],),
            )
            self.assertEqual(0, cursor.fetchone()["count"])
            with self.assertRaises(self.pymysql.MySQLError):
                cursor.execute(
                    "INSERT INTO work_items "
                    "(work_item_id, command_id, type, title, status, priority, source, dedupe_key) "
                    "VALUES (%s, %s, 'probe', 'probe', 'OPEN', 'NORMAL', 'integration', %s)",
                    (str(uuid4()), str(uuid4()), str(uuid4())),
                )
            connection.rollback()

        command, item, run, event, outbox = self._aggregate_rows("json")
        with repository.unit_of_work() as uow:
            receipt = uow.command_gateway_create(command, item, run, event, outbox)
            uow.commit()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT JSON_UNQUOTE(JSON_EXTRACT(parameters_json, '$.label')) AS label "
                "FROM agent_commands WHERE command_id=%s",
                (receipt["command_id"],),
            )
            self.assertEqual("json", cursor.fetchone()["label"])
            with self.assertRaises(self.pymysql.MySQLError):
                cursor.execute(
                    "INSERT INTO agent_commands "
                    "(command_id, command_type, source, actor_type, actor_roles_json, "
                    "entity_refs_json, parameters_json, idempotency_key, correlation_id, status, requested_at) "
                    "VALUES (%s, 'probe', %s, 'system', '[]', '[]', '{}', %s, %s, 'RECEIVED', NOW(6))",
                    (
                        str(uuid4()),
                        command["source"],
                        command["idempotency_key"],
                        str(uuid4()),
                    ),
                )

    @unittest.skip("superseded by immutable-014 forward-chain restore coverage")
    def test_empty_cutover_seed_rollback_is_reentrant_and_preserves_prior_rows(self):
        database = self.rollback_database
        seed_task_ids = self.runner._load_control_plane_seed_task_ids()
        self.assertEqual(55, len(seed_task_ids))

        def applied_at() -> datetime:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT applied_at FROM schema_migrations WHERE version='014'"
                )
                row = cursor.fetchone()
            self.assertIsNotNone(row)
            return row["applied_at"]

        def seed_rows(*, created_at: datetime, excluded: set[str] | None = None) -> None:
            excluded_ids = excluded or set()
            with self._connection(
                database,
                autocommit=True,
            ) as connection, connection.cursor() as cursor:
                for task_id in seed_task_ids:
                    if task_id in excluded_ids:
                        continue
                    cursor.execute(
                        "INSERT INTO scheduled_tasks "
                        "(id, name, tool_name, tool_params, cron_expression, enabled, created_at) "
                        "VALUES (%s, 'release seed probe', 'seed_probe', JSON_OBJECT(), "
                        "'0 0 * * *', FALSE, %s)",
                        (task_id, created_at),
                    )

        first_applied_at = applied_at()
        seed_rows(created_at=first_applied_at + timedelta(seconds=1))
        with patch.dict(os.environ, self._environment(database), clear=False):
            self.assertEqual(0, self.runner.restore_control_plane_task_cutover())

        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM scheduled_tasks")
            self.assertEqual(0, cursor.fetchone()["count"])
            cursor.execute("SELECT COUNT(*) AS count FROM schema_migrations WHERE version='014'")
            self.assertEqual(0, cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count "
                "FROM control_plane_task_cutover_backup_014"
            )
            self.assertEqual(0, cursor.fetchone()["count"])

        # The emptied database must accept 014 again after rollback.
        self._run_migrations(database)
        second_applied_at = applied_at()

        # A code-owned ID that predates this 014 application, and an unrelated
        # row created afterwards, are both outside rollback deletion authority.
        preserved_seed_id = "finance_bills_0010"
        unrelated_id = "integration_unmanaged_release_probe"
        with self._connection(
            database,
            autocommit=True,
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled, created_at) "
                "VALUES (%s, 'preexisting seed', 'sync_finance_bills', JSON_OBJECT(), "
                "'10 0 * * *', FALSE, %s)",
                (preserved_seed_id, second_applied_at - timedelta(seconds=1)),
            )
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled, created_at) "
                "VALUES (%s, 'unmanaged row', 'unmanaged_probe', JSON_OBJECT(), "
                "'0 0 * * *', FALSE, %s)",
                (unrelated_id, second_applied_at + timedelta(seconds=1)),
            )
        seed_rows(
            created_at=second_applied_at + timedelta(seconds=1),
            excluded={preserved_seed_id},
        )

        with patch.dict(os.environ, self._environment(database), clear=False):
            self.assertEqual(0, self.runner.restore_control_plane_task_cutover())

        with self._connection(
            database,
            autocommit=True,
        ) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM scheduled_tasks ORDER BY id")
            self.assertEqual(
                [preserved_seed_id, unrelated_id],
                [row["id"] for row in cursor.fetchall()],
            )
            cursor.execute(
                "DELETE FROM scheduled_tasks WHERE id IN (%s, %s)",
                (preserved_seed_id, unrelated_id),
            )

        # Leave the dedicated database fully migrated for order-independent
        # integration execution and prove a second rollback remains reentrant.
        self._run_migrations(database)

    @unittest.skip("superseded by immutable-014 forward-chain restore coverage")
    def test_legacy_finance_yunda_startup_apply_restore_and_reapply_are_exact(self):
        database = self.compat_database
        contracts = self.runner._load_control_plane_reviewed_task_contracts()
        finance_id = "finance_bills_0010"
        yunda_id = "yunda_dispatch_forecast_1700"
        startup_id = "finance_startup_catchup"

        def load_rows() -> dict[str, dict]:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, name, tool_name, tool_params, cron_expression, enabled, "
                    "last_run, last_status, last_duration_ms, last_message, created_at "
                    "FROM scheduled_tasks ORDER BY id"
                )
                rows = cursor.fetchall()
            normalized: dict[str, dict] = {}
            for row in rows:
                item = dict(row)
                if isinstance(item["tool_params"], str):
                    item["tool_params"] = json.loads(item["tool_params"])
                normalized[str(item["id"])] = item
            return normalized

        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            for task_id, contract in sorted(contracts.items()):
                cursor.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, tool_name, tool_params, cron_expression, enabled, last_status) "
                    "VALUES (%s, %s, %s, CAST(%s AS JSON), %s, TRUE, 'legacy-active')",
                    (
                        task_id,
                        f"reviewed {task_id}",
                        contract["tool_name"],
                        json.dumps(contract["canonical_arguments"], separators=(",", ":")),
                        contract["cron_expression"],
                    ),
                )
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled, "
                "last_status, last_duration_ms, last_message) "
                "VALUES (%s, 'legacy finance', 'sync_finance_bills', CAST(%s AS JSON), "
                "'10 0 * * *', FALSE, 'legacy-disabled', 123, 'preserve-finance')",
                (finance_id, json.dumps({"mode": "sync", "rescan_days": 7})),
            )
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled, "
                "last_status, last_duration_ms, last_message) "
                "VALUES (%s, 'legacy yunda', 'sync_yunda_dispatch_forecast', "
                "CAST(%s AS JSON), '0 17 * * *', FALSE, 'legacy-disabled', 456, "
                "'preserve-yunda')",
                (
                    yunda_id,
                    json.dumps(
                        {"session_profile": "yunda", "dest_brch": "56739382"}
                    ),
                ),
            )

        original = load_rows()
        self.assertNotIn(startup_id, original)
        self._apply_one(database, "014")

        applied = load_rows()
        self.assertEqual(
            {"mode": "sync", "platform": "ronghui", "rescan_days": 7},
            applied[finance_id]["tool_params"],
        )
        self.assertFalse(applied[finance_id]["enabled"])
        self.assertEqual("legacy-disabled", applied[finance_id]["last_status"])
        self.assertEqual(
            {"account_id": "yunda_default", "dest_brch": "56739382"},
            applied[yunda_id]["tool_params"],
        )
        self.assertFalse(applied[yunda_id]["enabled"])
        self.assertEqual(
            {
                "mode": "sync",
                "platform": "ronghui",
                "rescan_days": 7,
                "_startup_catchup": True,
            },
            applied[startup_id]["tool_params"],
        )
        self.assertTrue(applied[startup_id]["enabled"])
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM control_plane_task_cutover_created_014 "
                "WHERE task_id=%s",
                (startup_id,),
            )
            self.assertEqual(1, cursor.fetchone()["count"])

        with patch.dict(os.environ, self._environment(database), clear=False):
            self.assertEqual(0, self.runner.restore_control_plane_task_cutover())

        self.assertEqual(original, load_rows())
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM control_plane_task_cutover_created_014 "
                "WHERE task_id=%s",
                (startup_id,),
            )
            self.assertEqual(0, cursor.fetchone()["count"])
            cursor.execute("SELECT COUNT(*) AS count FROM schema_migrations WHERE version='014'")
            self.assertEqual(0, cursor.fetchone()["count"])

        self._apply_one(database, "014")
        reapplied = load_rows()
        self.assertEqual(set(applied), set(reapplied))
        for task_id in set(applied) - {startup_id}:
            self.assertEqual(applied[task_id], reapplied[task_id])
        applied_startup = {
            key: value
            for key, value in applied[startup_id].items()
            if key != "created_at"
        }
        reapplied_startup = {
            key: value
            for key, value in reapplied[startup_id].items()
            if key != "created_at"
        }
        self.assertEqual(applied_startup, reapplied_startup)

    def test_immutable_014_forward_chain_017_restore_partial_and_reapply(self):
        database = self.contract_chain_database
        internal = self.runner._load_control_plane_reviewed_task_contracts()
        optional = self.runner._load_control_plane_optional_task_contracts()
        clocks = self.runner._load_control_plane_clock_contracts()
        r7 = self.runner._load_control_plane_r7_contracts()
        expected_ids = self.runner._load_control_plane_reviewed_manifest_ids()
        self.assertEqual(69, len(expected_ids))

        # Production already owns immutable 014 history. The reviewed rows
        # below model its exact post-cutover state before forward upgrades.
        self._apply_one(database, "014")
        self._apply_one(database, "015")

        daily_transition = {
            "account_id": "r13_default",
            "r13_account_id": "r13_default",
            "problem_account_id": "ronghui_daxiang_s",
            "sign_account_id": "ronghui_daxiang_s",
            "detail_account_id": "ronghui_default",
            "days": 7,
        }
        reviewed_arrive_site = "mysql-integration-reviewed-arrive-site"
        arrive_transition = {
            "account_id": "ronghui_default",
            "site_code": reviewed_arrive_site,
        }
        arrive_ids = tuple(
            sorted(
                task_id
                for task_id, contract in internal.items()
                if contract["group_id"] == "arrive_list"
            )
        )
        self.assertEqual(
            (
                "arrive_list_0830",
                "arrive_list_0900",
                "arrive_list_0930",
            ),
            arrive_ids,
        )
        yunda_send_id = "yunda_send_waybills_2355"
        yunda_send_pre_014 = {
            "account_id": "yunda_default",
            "session_profile": "yunda",
            "ensure_fields": False,
            "target_date": "",
        }
        yunda_disabled_message = "mysql-integration-014-disabled-message"
        rows: dict[str, tuple[str, dict, str, bool]] = {}
        for task_id, contract in internal.items():
            arguments = (
                daily_transition
                if contract["group_id"] == "daily_sign"
                else arrive_transition
                if contract["group_id"] == "arrive_list"
                else dict(contract["canonical_arguments"])
            )
            rows[task_id] = (
                contract["tool_name"],
                arguments,
                contract["cron_expression"],
                task_id != yunda_send_id,
            )
        for task_id, contract in r7.items():
            rows[task_id] = (
                contract["tool_name"],
                dict(contract["canonical_arguments"]),
                contract["cron_expression"],
                True,
            )
        for task_id, contract in clocks.items():
            rows[task_id] = (
                "clock_in_dual" if task_id == "clockin_daxiang_1830" else "tms_query",
                self.runner._applied_014_clock_arguments(
                    task_id,
                    contract["canonical_arguments"],
                ),
                contract["cron_expression"],
                True,
            )
        rows["finance_bills_0010"] = (
            optional["finance_bills_0010"]["tool_name"],
            {
                "account_id": "ronghui_default",
                "mode": "sync",
                "platform": "ronghui",
                "rescan_days": 7,
            },
            optional["finance_bills_0010"]["cron_expression"],
            False,
        )
        rows["finance_startup_catchup"] = (
            optional["finance_startup_catchup"]["tool_name"],
            dict(optional["finance_startup_catchup"]["canonical_arguments"]),
            optional["finance_startup_catchup"]["cron_expression"],
            True,
        )
        rows["yunda_dispatch_forecast_1700"] = (
            optional["yunda_dispatch_forecast_1700"]["tool_name"],
            dict(optional["yunda_dispatch_forecast_1700"]["canonical_arguments"]),
            optional["yunda_dispatch_forecast_1700"]["cron_expression"],
            False,
        )
        self.assertEqual(expected_ids, set(rows))

        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            for task_id, (tool_name, arguments, cron_expression, enabled) in sorted(rows.items()):
                last_status = "disabled" if task_id == yunda_send_id else "pre-017"
                last_message = (
                    yunda_disabled_message
                    if task_id == yunda_send_id
                    else "preserve exact row"
                )
                task_name = (
                    "财务启动缺口扫描"
                    if task_id == "finance_startup_catchup"
                    else f"reviewed {task_id}"
                )
                cursor.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, tool_name, tool_params, cron_expression, enabled, "
                    "last_status, last_duration_ms, last_message) "
                    "VALUES (%s, %s, %s, CAST(%s AS JSON), %s, %s, %s, 17, %s)",
                    (
                        task_id,
                        task_name,
                        tool_name,
                        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                        cron_expression,
                        enabled,
                        last_status,
                        last_message,
                    ),
                )
            cursor.execute(
                "INSERT INTO control_plane_task_cutover_backup_014 "
                "(id, name, tool_name, tool_params, cron_expression, enabled, "
                "last_status, last_duration_ms, last_message) "
                "VALUES (%s, %s, 'sync_yunda_send_waybills', CAST(%s AS JSON), "
                "'55 23 * * *', TRUE, 'legacy-active', 23, 'pre-014-yunda')",
                (
                    yunda_send_id,
                    "pre-014 reviewed yunda send",
                    json.dumps(
                        yunda_send_pre_014,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )

        self._apply_one(database, "016")

        def load_daily_arguments() -> dict:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT tool_params FROM scheduled_tasks WHERE id='daily_sign_0500'"
                )
                value = cursor.fetchone()["tool_params"]
            return json.loads(value) if isinstance(value, str) else dict(value)

        self.assertEqual(
            dict(internal["daily_sign_0500"]["canonical_arguments"]),
            load_daily_arguments(),
        )
        with patch.dict(os.environ, self._environment(database), clear=False):
            self.assertEqual(0, self.runner.restore_daily_sign_single_tms_account())
            self.assertEqual(0, self.runner.restore_daily_sign_single_tms_account())
        self.assertEqual(daily_transition, load_daily_arguments())
        self._apply_one(database, "016")
        self.assertEqual(
            dict(internal["daily_sign_0500"]["canonical_arguments"]),
            load_daily_arguments(),
        )

        migration_017 = next(
            path
            for version, path in self.runner.discover_migrations()
            if version == "017"
        )
        migration_017_sql = migration_017.read_text(encoding="utf-8")
        production_arrive_sha256 = (
            self.runner.CONTROL_PLANE_REVIEWED_ARRIVE_SITE_SHA256
        )
        integration_arrive_sha256 = hashlib.sha256(
            reviewed_arrive_site.encode("utf-8")
        ).hexdigest()
        production_disabled_message_sha256 = (
            self.runner.CONTROL_PLANE_APPLIED_014_YUNDA_DISABLED_MESSAGE_SHA256
        )
        integration_disabled_message_sha256 = hashlib.sha256(
            yunda_disabled_message.encode("utf-8")
        ).hexdigest()
        self.assertEqual(1, migration_017_sql.count(production_arrive_sha256))
        self.assertEqual(
            1,
            migration_017_sql.count(production_disabled_message_sha256),
        )
        integration_017_sql = migration_017_sql.replace(
            production_arrive_sha256,
            integration_arrive_sha256,
        ).replace(
            production_disabled_message_sha256,
            integration_disabled_message_sha256,
        )
        statements_017 = self.runner.split_sql_statements(integration_017_sql)

        def set_arrive_arguments(task_id: str, arguments: dict) -> None:
            with self._connection(
                database,
                autocommit=True,
            ) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_tasks SET tool_params=CAST(%s AS JSON) "
                    "WHERE id=%s",
                    (
                        json.dumps(
                            arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        task_id,
                    ),
                )

        def load_arrive_arguments(task_id: str) -> dict:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT tool_params FROM scheduled_tasks WHERE id=%s",
                    (task_id,),
                )
                value = cursor.fetchone()["tool_params"]
            return json.loads(value) if isinstance(value, str) else dict(value)

        def load_yunda_proof_state() -> tuple[dict | None, dict | None]:
            result: list[dict | None] = []
            with self._connection(database) as connection, connection.cursor() as cursor:
                for table in (
                    "scheduled_tasks",
                    "control_plane_task_cutover_backup_014",
                ):
                    version_column = (
                        ", configuration_version"
                        if table == "scheduled_tasks"
                        else ""
                    )
                    cursor.execute(
                        "SELECT id, tool_name, tool_params, cron_expression, enabled, "
                        f"last_status, last_message{version_column} "
                        f"FROM {table} WHERE id=%s",
                        (yunda_send_id,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        row = dict(row)
                        if isinstance(row["tool_params"], str):
                            row["tool_params"] = json.loads(row["tool_params"])
                    result.append(row)
            return result[0], result[1]

        def restore_yunda_proof_state() -> None:
            with self._connection(
                database,
                autocommit=True,
            ) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_tasks SET "
                    "tool_name='sync_yunda_send_waybills', "
                    "tool_params=CAST(%s AS JSON), cron_expression='55 23 * * *', "
                    "enabled=FALSE, last_status='disabled', last_message=%s, "
                    "configuration_version=1 "
                    "WHERE id=%s",
                    (
                        json.dumps(
                            internal[yunda_send_id]["canonical_arguments"],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        yunda_disabled_message,
                        yunda_send_id,
                    ),
                )
                cursor.execute(
                    "INSERT INTO control_plane_task_cutover_backup_014 "
                    "(id, name, tool_name, tool_params, cron_expression, enabled, "
                    "last_status, last_duration_ms, last_message) "
                    "VALUES (%s, %s, 'sync_yunda_send_waybills', CAST(%s AS JSON), "
                    "'55 23 * * *', TRUE, 'legacy-active', 23, 'pre-014-yunda') "
                    "ON DUPLICATE KEY UPDATE "
                    "tool_name=VALUES(tool_name), tool_params=VALUES(tool_params), "
                    "cron_expression=VALUES(cron_expression), enabled=VALUES(enabled), "
                    "last_status=VALUES(last_status), last_duration_ms=VALUES(last_duration_ms), "
                    "last_message=VALUES(last_message)",
                    (
                        yunda_send_id,
                        "pre-014 reviewed yunda send",
                        json.dumps(
                            yunda_send_pre_014,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                )

        def apply_017() -> None:
            with self._connection(
                database,
                autocommit=True,
            ) as connection, connection.cursor() as cursor:
                for statement in statements_017:
                    cursor.execute(statement)
                cursor.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) "
                    "VALUES (%s, %s, %s)",
                    (
                        "017",
                        migration_017.name,
                        self.runner.migration_checksum(migration_017),
                    ),
                )

        for invalid_arguments in (
            {
                "account_id": "ronghui_default",
                "site_code": "wrong-reviewed-site",
            },
            {
                "account_id": "ronghui_default",
                "site_code": reviewed_arrive_site,
                "unexpected": True,
            },
            {
                "account_id": "ronghui_default",
                "site_code": 123,
            },
        ):
            set_arrive_arguments(arrive_ids[0], invalid_arguments)
            with self.assertRaises(self.pymysql.MySQLError):
                with self._connection(
                    database,
                    autocommit=True,
                ) as connection, connection.cursor() as cursor:
                    for statement in statements_017:
                        cursor.execute(statement)
            self.assertEqual(
                invalid_arguments,
                load_arrive_arguments(arrive_ids[0]),
            )
        set_arrive_arguments(arrive_ids[0], arrive_transition)

        yunda_invalid_mutations = (
            (
                "missing backup",
                "DELETE FROM control_plane_task_cutover_backup_014 WHERE id=%s",
                (yunda_send_id,),
            ),
            (
                "backup disabled",
                "UPDATE control_plane_task_cutover_backup_014 SET enabled=FALSE "
                "WHERE id=%s",
                (yunda_send_id,),
            ),
            (
                "backup extra argument",
                "UPDATE control_plane_task_cutover_backup_014 SET "
                "tool_params=JSON_SET(tool_params, '$.unexpected', TRUE) WHERE id=%s",
                (yunda_send_id,),
            ),
            (
                "current extra argument",
                "UPDATE scheduled_tasks SET "
                "tool_params=JSON_SET(tool_params, '$.unexpected', TRUE) WHERE id=%s",
                (yunda_send_id,),
            ),
            (
                "current wrong status",
                "UPDATE scheduled_tasks SET last_status='success' WHERE id=%s",
                (yunda_send_id,),
            ),
            (
                "current wrong message",
                "UPDATE scheduled_tasks SET last_message='changed-message' WHERE id=%s",
                (yunda_send_id,),
            ),
            (
                "current changed version",
                "UPDATE scheduled_tasks SET configuration_version=2 WHERE id=%s",
                (yunda_send_id,),
            ),
        )
        for label, mutation_sql, mutation_params in yunda_invalid_mutations:
            with self.subTest(yunda_proof=label):
                with self._connection(
                    database,
                    autocommit=True,
                ) as connection, connection.cursor() as cursor:
                    cursor.execute(mutation_sql, mutation_params)
                invalid_state = load_yunda_proof_state()
                with self.assertRaises(self.pymysql.MySQLError):
                    with self._connection(
                        database,
                        autocommit=True,
                    ) as connection, connection.cursor() as cursor:
                        for statement in statements_017:
                            cursor.execute(statement)
                self.assertEqual(invalid_state, load_yunda_proof_state())
                restore_yunda_proof_state()

        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s "
                "AND TABLE_NAME='scheduled_task_contract_upgrade_backup_017'",
                (database,),
            )
            self.assertEqual(0, cursor.fetchone()["count"])

        restored_ids = tuple(
            sorted(
                {
                    "clockin_daxiang_1830",
                    "clockin_daxiang_s_1833",
                    "finance_bills_0010",
                    "finance_startup_catchup",
                    yunda_send_id,
                    *arrive_ids,
                }
            )
        )
        placeholders = ",".join("%s" for _ in restored_ids)

        def load_restore_rows() -> dict[str, dict]:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, name, tool_name, tool_params, cron_expression, enabled, "
                    "last_run, last_status, last_duration_ms, last_message, created_at, "
                    "configuration_version, updated_at FROM scheduled_tasks "
                    f"WHERE id IN ({placeholders}) ORDER BY id",
                    restored_ids,
                )
                result = {str(row["id"]): dict(row) for row in cursor.fetchall()}
            for row in result.values():
                if isinstance(row["tool_params"], str):
                    row["tool_params"] = json.loads(row["tool_params"])
            return result

        pre_017 = load_restore_rows()
        for task_id in arrive_ids:
            self.assertEqual(arrive_transition, pre_017[task_id]["tool_params"])
        self.assertFalse(pre_017[yunda_send_id]["enabled"])
        self.assertEqual(
            dict(internal[yunda_send_id]["canonical_arguments"]),
            pre_017[yunda_send_id]["tool_params"],
        )
        self.assertEqual("disabled", pre_017[yunda_send_id]["last_status"])
        self.assertEqual(
            yunda_disabled_message,
            pre_017[yunda_send_id]["last_message"],
        )
        apply_017()
        canonical = load_restore_rows()
        for task_id, contract in clocks.items():
            self.assertEqual("clock_in_dual", canonical[task_id]["tool_name"])
            self.assertEqual(
                dict(contract["canonical_arguments"]),
                canonical[task_id]["tool_params"],
            )
        self.assertEqual(
            dict(optional["finance_bills_0010"]["canonical_arguments"]),
            canonical["finance_bills_0010"]["tool_params"],
        )
        for task_id in arrive_ids:
            self.assertEqual(
                dict(internal[task_id]["canonical_arguments"]),
                canonical[task_id]["tool_params"],
            )
            self.assertNotIn("site_code", canonical[task_id]["tool_params"])
        self.assertTrue(canonical[yunda_send_id]["enabled"])
        self.assertEqual(
            dict(internal[yunda_send_id]["canonical_arguments"]),
            canonical[yunda_send_id]["tool_params"],
        )
        self.assertEqual("legacy-active", canonical[yunda_send_id]["last_status"])
        self.assertEqual("pre-014-yunda", canonical[yunda_send_id]["last_message"])
        self.assertEqual(
            pre_017[yunda_send_id]["configuration_version"] + 1,
            canonical[yunda_send_id]["configuration_version"],
        )

        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            for statement in statements_017:
                cursor.execute(statement)
        self.assertEqual(canonical, load_restore_rows())

        with patch.dict(os.environ, self._environment(database), clear=False):
            self.assertEqual(0, self.runner.restore_scheduled_task_contract_upgrade())
        self.assertEqual(pre_017, load_restore_rows())

        # Simulate an interrupted 017 whose SQL committed but whose history
        # insert did not. Restore must still use the capture table.
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            for statement in statements_017:
                cursor.execute(statement)
            cursor.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations WHERE version='017'"
            )
            self.assertEqual(0, cursor.fetchone()["count"])
        with patch.dict(os.environ, self._environment(database), clear=False):
            self.assertEqual(0, self.runner.restore_scheduled_task_contract_upgrade())
            self.assertEqual(0, self.runner.restore_scheduled_task_contract_upgrade())
        self.assertEqual(pre_017, load_restore_rows())

        apply_017()
        def stable_contract_rows(source: dict[str, dict]) -> dict[str, dict]:
            return {
                task_id: {
                    key: value
                    for key, value in row.items()
                    if key != "updated_at"
                }
                for task_id, row in source.items()
            }

        self.assertEqual(
            stable_contract_rows(canonical),
            stable_contract_rows(load_restore_rows()),
        )
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            self.assertEqual(
                [
                    version
                    for version, _ in self.runner.discover_migrations()
                    if version <= "017"
                ],
                [str(row["version"]) for row in cursor.fetchall()],
            )

    def test_017_finance_startup_create_seed_restore_and_reapply_are_exact(self):
        database = self.startup_contract_database
        startup_id = "finance_startup_catchup"
        startup_arguments = {
            "mode": "sync",
            "platform": "ronghui",
            "rescan_days": 7,
            "_startup_catchup": True,
        }
        migration_017 = next(
            path
            for version, path in self.runner.discover_migrations()
            if version == "017"
        )
        statements_017 = self.runner.split_sql_statements(
            migration_017.read_text(encoding="utf-8")
        )

        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES ('finance_bills_0010', 'finance daily', "
                "'sync_finance_bills', CAST(%s AS JSON), '10 0 * * *', FALSE)",
                (json.dumps({"mode": "sync", "platform": "ronghui", "rescan_days": 7}),),
            )
            cursor.execute(
                "SELECT filename, checksum, applied_at "
                "FROM schema_migrations WHERE BINARY version=BINARY '015'"
            )
            migration_015 = cursor.fetchone()
            applied_015_at = migration_015["applied_at"]
            migration_015_filename = str(migration_015["filename"])
            migration_015_checksum = str(migration_015["checksum"])

        def apply_017() -> None:
            self._apply_one(database, "017")

        def restore_017() -> None:
            with patch.dict(os.environ, self._environment(database), clear=False):
                self.assertEqual(
                    0,
                    self.runner.restore_scheduled_task_contract_upgrade(),
                )

        def load_startup() -> dict | None:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, name, tool_name, tool_params, cron_expression, enabled, "
                    "last_run, last_status, last_duration_ms, last_message, created_at, "
                    "configuration_version, updated_at FROM scheduled_tasks WHERE id=%s",
                    (startup_id,),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            if isinstance(result["tool_params"], str):
                result["tool_params"] = json.loads(result["tool_params"])
            return result

        def capture_count(table: str) -> int:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE task_id=%s"
                    if table.endswith("created_017")
                    else f"SELECT COUNT(*) AS count FROM {table} WHERE id=%s",
                    (startup_id,),
                )
                return int(cursor.fetchone()["count"])

        def migration_017_count() -> int:
            with self._connection(database) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM schema_migrations "
                    "WHERE BINARY version=BINARY '017'"
                )
                return int(cursor.fetchone()["count"])

        # Absent rows are created enabled and get the exact deletion marker.
        apply_017()
        created = load_startup()
        self.assertIsNotNone(created)
        self.assertEqual("财务启动缺口扫描", created["name"])
        self.assertEqual("sync_finance_bills", created["tool_name"])
        self.assertEqual(startup_arguments, created["tool_params"])
        self.assertEqual("@startup", created["cron_expression"])
        self.assertTrue(created["enabled"])
        self.assertEqual(1, created["configuration_version"])
        self.assertEqual(
            1,
            capture_count("scheduled_task_contract_upgrade_created_017"),
        )
        self.assertEqual(
            0,
            capture_count("scheduled_task_contract_upgrade_backup_017"),
        )
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            for statement in statements_017:
                cursor.execute(statement)
        self.assertEqual(created, load_startup())

        # Restore must run only after bootstrap cleanup and must preserve all
        # recovery artifacts when it refuses the ordering.
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_task_approval_policies (task_id, mode) "
                "VALUES (%s, 'REQUIRE_EACH_RUN')",
                (startup_id,),
            )
        with (
            patch.dict(os.environ, self._environment(database), clear=False),
            self.assertRaisesRegex(RuntimeError, "bootstrap cleanup first"),
        ):
            self.runner.restore_scheduled_task_contract_upgrade()
        self.assertEqual(created, load_startup())
        self.assertEqual(1, migration_017_count())
        self.assertEqual(
            1,
            capture_count("scheduled_task_contract_upgrade_created_017"),
        )
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM scheduled_task_approval_policies "
                "WHERE BINARY task_id=BINARY %s",
                (startup_id,),
            )
            cursor.execute(
                "INSERT INTO scheduled_task_approval_policy_events "
                "(task_id, to_mode, actor_id, actor_role, reason, "
                "correlation_id, request_id) "
                "VALUES (%s, 'REQUIRE_EACH_RUN', %s, %s, "
                "'control_plane_v1_bootstrap_complete', %s, %s)",
                (
                    self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID,
                    self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ID,
                    self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ROLE,
                    self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
                    self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
                ),
            )
        with (
            patch.dict(os.environ, self._environment(database), clear=False),
            self.assertRaisesRegex(RuntimeError, "completion marker remains"),
        ):
            self.runner.restore_scheduled_task_contract_upgrade()
        self.assertEqual(created, load_startup())
        self.assertEqual(1, migration_017_count())
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM scheduled_task_approval_policy_events "
                "WHERE BINARY task_id=BINARY %s",
                (self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID,),
            )
            domain_event_id = str(uuid4())
            correlation_id = str(uuid4())
            source_event_id = f"{startup_id}:{uuid4()}"
            cursor.execute(
                "INSERT INTO domain_events "
                "(event_id, event_type, schema_version, source_system, "
                "source_event_id, entity_type, entity_id, occurred_at, "
                "observed_at, correlation_id, payload_json, payload_sha256) "
                "VALUES (%s, 'scheduled_task.approval_policy_changed', 1, "
                "'agent', %s, 'scheduled_task', %s, NOW(6), NOW(6), %s, "
                "CAST('{}' AS JSON), SHA2('{}', 256))",
                (domain_event_id, source_event_id, startup_id, correlation_id),
            )
            cursor.execute(
                "INSERT INTO outbox_events "
                "(event_id, consumer_name, topic, partition_key) "
                "VALUES (%s, 'test-consumer', "
                "'scheduled_task.approval_policy_changed', %s)",
                (domain_event_id, startup_id),
            )
        with (
            patch.dict(os.environ, self._environment(database), clear=False),
            self.assertRaisesRegex(RuntimeError, "domain or outbox state remains"),
        ):
            self.runner.restore_scheduled_task_contract_upgrade()
        self.assertEqual(created, load_startup())
        self.assertEqual(1, migration_017_count())
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM outbox_events WHERE BINARY event_id=BINARY %s",
                (domain_event_id,),
            )
            cursor.execute(
                "DELETE FROM domain_events WHERE BINARY event_id=BINARY %s",
                (domain_event_id,),
            )

        # Case-only drift must not pass a case-insensitive production collation.
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE scheduled_tasks "
                "SET tool_name='Sync_finance_bills' WHERE BINARY id=BINARY %s",
                (startup_id,),
            )
        with (
            patch.dict(os.environ, self._environment(database), clear=False),
            self.assertRaisesRegex(RuntimeError, "no longer matches its marker"),
        ):
            self.runner.restore_scheduled_task_contract_upgrade()
        self.assertEqual(1, migration_017_count())
        self.assertEqual(
            1,
            capture_count("scheduled_task_contract_upgrade_created_017"),
        )
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE scheduled_tasks "
                "SET tool_name=%s, updated_at=%s WHERE BINARY id=BINARY %s",
                (created["tool_name"], created["updated_at"], startup_id),
            )
        restore_017()
        self.assertIsNone(load_startup())
        apply_017()
        restore_017()
        self.assertIsNone(load_startup())

        # The one proven failed-release seed is captured and enabled, never marked created.
        seed_created_at = applied_015_at + timedelta(seconds=10)
        seed_updated_at = seed_created_at + timedelta(microseconds=500_000)
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled, "
                "created_at, configuration_version, updated_at) "
                "VALUES (%s, '财务启动缺口扫描', 'sync_finance_bills', CAST(%s AS JSON), "
                "'@startup', FALSE, %s, 1, %s)",
                (
                    startup_id,
                    json.dumps(startup_arguments, separators=(",", ":")),
                    seed_created_at,
                    seed_updated_at,
                ),
            )
        seeded = load_startup()
        for column, invalid_value, original_value in (
            ("filename", migration_015_filename.swapcase(), migration_015_filename),
            ("checksum", migration_015_checksum.upper(), migration_015_checksum),
        ):
            with self.subTest(provenance_column=column):
                with self._connection(
                    database,
                    autocommit=True,
                ) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE schema_migrations SET {column}=%s "
                        "WHERE BINARY version=BINARY '015'",
                        (invalid_value,),
                    )
                with self.assertRaises(self.pymysql.MySQLError):
                    with self._connection(
                        database,
                        autocommit=True,
                    ) as connection, connection.cursor() as cursor:
                        for statement in statements_017:
                            cursor.execute(statement)
                self.assertEqual(seeded, load_startup())
                self.assertEqual(
                    0,
                    capture_count("scheduled_task_contract_upgrade_backup_017"),
                )
                with self._connection(
                    database,
                    autocommit=True,
                ) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE schema_migrations SET {column}=%s "
                        "WHERE BINARY version=BINARY '015'",
                        (original_value,),
                    )
        apply_017()
        enabled_seed = load_startup()
        self.assertTrue(enabled_seed["enabled"])
        self.assertEqual(2, enabled_seed["configuration_version"])
        self.assertEqual(
            0,
            capture_count("scheduled_task_contract_upgrade_created_017"),
        )
        self.assertEqual(
            1,
            capture_count("scheduled_task_contract_upgrade_backup_017"),
        )
        restore_017()
        self.assertEqual(seeded, load_startup())
        apply_017()
        restore_017()
        self.assertEqual(seeded, load_startup())

        # A same-shape administrator row outside the release window is untouched.
        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            outside = applied_015_at + timedelta(seconds=31)
            cursor.execute(
                "UPDATE scheduled_tasks SET created_at=%s, updated_at=%s WHERE id=%s",
                (outside, outside + timedelta(microseconds=500_000), startup_id),
            )
        rejected = load_startup()
        with self.assertRaises(self.pymysql.MySQLError):
            with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
                for statement in statements_017:
                    cursor.execute(statement)
        self.assertEqual(rejected, load_startup())
        self.assertEqual(
            0,
            capture_count("scheduled_task_contract_upgrade_backup_017"),
        )

        with self._connection(database, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM scheduled_tasks WHERE id=%s", (startup_id,))
        apply_017()

    def test_two_workers_skip_locked_without_duplicate_claim(self):
        repository = self._repository()
        for label in ("claim-a", "claim-b"):
            rows = self._aggregate_rows(label)
            with repository.unit_of_work() as uow:
                uow.command_gateway_create(*rows)
                uow.commit()

        first = repository.unit_of_work()
        second = repository.unit_of_work()
        try:
            first.__enter__()
            first_claim = first.runs.claim("worker-a", ("RECEIVED",), limit=1)
            second.__enter__()
            second_claim = second.runs.claim("worker-b", ("RECEIVED",), limit=1)
            self.assertEqual(1, len(first_claim))
            self.assertEqual(1, len(second_claim))
            self.assertNotEqual(first_claim[0]["run_id"], second_claim[0]["run_id"])
        finally:
            second.rollback()
            second.__exit__(None, None, None)
            first.rollback()
            first.__exit__(None, None, None)

    def test_two_outbox_workers_skip_locked_without_duplicate_claim(self):
        repository = self._repository()
        consumer_name = f"pending-claim-{uuid4()}"
        for label in ("outbox-claim-a", "outbox-claim-b"):
            rows = self._aggregate_rows(label)
            rows[4][0]["consumer_name"] = consumer_name
            with repository.unit_of_work() as uow:
                uow.command_gateway_create(*rows)
                uow.commit()

        first = repository.unit_of_work()
        second = repository.unit_of_work()
        try:
            first.__enter__()
            with first.connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout=2")
            first_claim = first.outbox.claim(
                "outbox-worker-a",
                consumer_name=consumer_name,
                limit=1,
            )
            second.__enter__()
            with second.connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout=2")
            second_claim = second.outbox.claim(
                "outbox-worker-b",
                consumer_name=consumer_name,
                limit=1,
            )
            self.assertEqual(1, len(first_claim))
            self.assertEqual(1, len(second_claim))
            self.assertNotEqual(
                first_claim[0]["outbox_id"],
                second_claim[0]["outbox_id"],
            )
        finally:
            second.rollback()
            second.__exit__(None, None, None)
            first.rollback()
            first.__exit__(None, None, None)

    def test_two_outbox_workers_recover_distinct_expired_leases(self):
        repository = self._repository()
        consumer_name = f"expired-lease-{uuid4()}"
        for label in ("expired-outbox-a", "expired-outbox-b"):
            rows = self._aggregate_rows(label)
            rows[4][0]["consumer_name"] = consumer_name
            with repository.unit_of_work() as uow:
                uow.command_gateway_create(*rows)
                uow.commit()
        with self._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE outbox_events SET status='PROCESSING', locked_by='expired', "
                "locked_until=DATE_SUB(NOW(6), INTERVAL 1 SECOND) "
                "WHERE consumer_name=%s",
                (consumer_name,),
            )

        first = repository.unit_of_work()
        second = repository.unit_of_work()
        try:
            first.__enter__()
            with first.connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout=2")
            first_claim = first.outbox.claim(
                "lease-worker-a", consumer_name=consumer_name, limit=1
            )
            second.__enter__()
            with second.connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout=2")
            second_claim = second.outbox.claim(
                "lease-worker-b", consumer_name=consumer_name, limit=1
            )
            self.assertEqual(1, len(first_claim))
            self.assertEqual(1, len(second_claim))
            self.assertNotEqual(
                first_claim[0]["outbox_id"], second_claim[0]["outbox_id"]
            )
        finally:
            second.rollback()
            second.__exit__(None, None, None)
            first.rollback()
            first.__exit__(None, None, None)

    def test_concurrent_approval_decisions_accept_only_the_first_commit(self):
        from shared.orchestration_repository import InvalidStateError

        repository = self._repository()
        command, item, run, event, outbox = self._aggregate_rows("approval-race")
        with repository.unit_of_work() as uow:
            receipt = uow.command_gateway_create(command, item, run, event, outbox)
            uow.commit()

        plan_hash = "a" * 64
        approval_id = str(uuid4())
        with self._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_runs SET status='WAITING_APPROVAL', plan_hash=%s WHERE run_id=%s",
                (plan_hash, receipt["run_id"]),
            )
        with repository.unit_of_work() as uow:
            uow.approvals.create_or_get(
                {
                    "approval_id": approval_id,
                    "work_item_id": receipt["work_item_id"],
                    "run_id": receipt["run_id"],
                    "approval_round": 1,
                    "plan_hash": plan_hash,
                    "impact": {"entities": []},
                    "risk_level": "HIGH",
                    "required_role": "super_admin",
                    "required_approvals": 1,
                    "status": "PENDING",
                    "requested_by_type": "console_admin",
                    "requested_by_id": "admin-1",
                    "expires_at": datetime.now() + timedelta(minutes=5),
                }
            )
            uow.commit()

        first_has_lock = threading.Event()
        second_started = threading.Event()
        outcomes: list[str] = []
        unexpected: list[BaseException] = []

        def decide_first() -> None:
            try:
                with repository.unit_of_work() as uow:
                    uow.approvals.record_decision(
                        {
                            "decision_id": str(uuid4()),
                            "approval_id": approval_id,
                            "actor_type": "console_admin",
                            "actor_id": "super-1",
                            "actor_roles": ["super_admin"],
                            "decision": "APPROVED",
                            "decided_at": datetime.now(),
                        },
                        expected_plan_hash=plan_hash,
                    )
                    first_has_lock.set()
                    if not second_started.wait(5):
                        raise TimeoutError("second approval transaction did not start")
                    uow.commit()
                outcomes.append("first-approved")
            except BaseException as exc:  # pragma: no cover - surfaced below
                unexpected.append(exc)

        def decide_second() -> None:
            try:
                if not first_has_lock.wait(5):
                    raise TimeoutError("first approval transaction did not acquire its lock")
                with repository.unit_of_work() as uow:
                    second_started.set()
                    uow.approvals.record_decision(
                        {
                            "decision_id": str(uuid4()),
                            "approval_id": approval_id,
                            "actor_type": "console_admin",
                            "actor_id": "super-2",
                            "actor_roles": ["super_admin"],
                            "decision": "APPROVED",
                            "decided_at": datetime.now(),
                        },
                        expected_plan_hash=plan_hash,
                    )
                    uow.commit()
                outcomes.append("second-approved")
            except InvalidStateError:
                outcomes.append("second-rejected")
            except BaseException as exc:  # pragma: no cover - surfaced below
                unexpected.append(exc)

        first_thread = threading.Thread(target=decide_first, daemon=True)
        second_thread = threading.Thread(target=decide_second, daemon=True)
        first_thread.start()
        second_thread.start()
        first_thread.join(10)
        second_thread.join(10)

        self.assertFalse(first_thread.is_alive() or second_thread.is_alive())
        self.assertEqual([], unexpected)
        self.assertCountEqual(["first-approved", "second-rejected"], outcomes)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM approval_requests WHERE approval_id=%s",
                (approval_id,),
            )
            self.assertEqual("APPROVED", cursor.fetchone()["status"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM approval_decisions WHERE approval_id=%s",
                (approval_id,),
            )
            self.assertEqual(1, cursor.fetchone()["count"])

    def test_feishu_queue_has_a_database_single_active_binding_constraint(self):
        """Migration 023 must enforce serialization even if a caller races."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COLUMN_NAME, EXTRA, GENERATION_EXPRESSION
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME='feishu_approval_deliveries'
                  AND COLUMN_NAME='active_binding_id'
                """
            )
            column = cursor.fetchone()
            self.assertIsNotNone(column)
            self.assertIn("VIRTUAL GENERATED", str(column["EXTRA"]).upper())
            self.assertIn("ACTIVE", str(column["GENERATION_EXPRESSION"]).upper())

            cursor.execute(
                """
                SELECT NON_UNIQUE
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME='feishu_approval_deliveries'
                  AND INDEX_NAME='uq_feishu_approval_delivery_active_binding'
                  AND COLUMN_NAME='active_binding_id'
                """
            )
            index = cursor.fetchone()
            self.assertIsNotNone(index)
            self.assertEqual(0, int(index["NON_UNIQUE"]))

    def test_feishu_queue_migration_requeues_ambiguous_active_rows_and_resends(self):
        FEISHU_QUEUE_SCENARIOS.run_test_feishu_queue_migration_requeues_ambiguous_active_rows_and_resends(
            self
        )

    def test_linked_terminal_retry_reuses_the_first_child_run(self):
        repository = self._repository()
        command, item, run, event, outbox = self._aggregate_rows("retry-replay")
        with repository.unit_of_work() as uow:
            receipt = uow.command_gateway_create(command, item, run, event, outbox)
            uow.commit()
        with self._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_runs SET status='FAILED_TERMINAL' WHERE run_id=%s",
                (receipt["run_id"],),
            )

        first = repository.create_linked_retry_run(
            receipt["run_id"],
            new_run_id=str(uuid4()),
            new_command_id=str(uuid4()),
        )
        replay = repository.create_linked_retry_run(
            receipt["run_id"],
            new_run_id=str(uuid4()),
            new_command_id=str(uuid4()),
        )

        self.assertEqual(first["run_id"], replay["run_id"])
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM agent_runs WHERE retry_of_run_id=%s",
                (receipt["run_id"],),
            )
            self.assertEqual(1, cursor.fetchone()["count"])

    @unittest.skip("014 is production-applied and byte-immutable; upgrades live in 016/017")
    def test_task_cutover_exact_production_set_is_reentrant_and_fail_closed(self):
        migration = next(
            path
            for version, path in self.runner.discover_migrations()
            if version == "014"
        )
        statements = self.runner.split_sql_statements(migration.read_text(encoding="utf-8"))
        contracts = self.runner._load_control_plane_reviewed_task_contracts()
        clock_contracts = self.runner._load_control_plane_clock_contracts()
        self.assertEqual(51, len(contracts))
        self.assertEqual(2, len(clock_contracts))

        reviewed_login_site_sha256 = (
            self.runner.CONTROL_PLANE_REVIEWED_ARRIVE_LOGIN_SITE_SHA256
        )
        reviewed_login_site_code = None
        for number in range(100_000):
            candidate = f"{number:05d}"
            if (
                hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                == reviewed_login_site_sha256
            ):
                reviewed_login_site_code = candidate
                break
        self.assertIsNotNone(reviewed_login_site_code)
        legacy_arguments = {
            "arrive_list": {
                "account_id": "ronghui_default",
                "login_site_code": reviewed_login_site_code,
                "site_code": "reviewed-unconsumed-legacy-filter",
                "target_date": "",
            },
            "daily_sign": {
                "account_id": "r13_default",
                "detail_account_id": "ronghui_default",
                "r13_account_id": "r13_default",
            },
            "delivery_status": {},
            "send_order": {"account_id": "price_default", "target_date": ""},
            "yunda_send_waybills": {
                "account_id": "yunda_default",
                "ensure_fields": False,
                "session_profile": "yunda",
                "target_date": "",
            },
        }

        def execute_migration(cursor):
            for statement in statements:
                cursor.execute(statement)

        def seed_reviewed_tasks(cursor, task_ids=None):
            for task_id in sorted(task_ids or contracts):
                contract = contracts[task_id]
                group_id = contract["group_id"]
                arguments = legacy_arguments.get(
                    group_id,
                    dict(contract["canonical_arguments"]),
                )
                cursor.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, tool_name, tool_params, cron_expression, enabled) "
                    "VALUES (%s, %s, %s, CAST(%s AS JSON), %s, TRUE) "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name), tool_name=VALUES(tool_name), "
                    "tool_params=VALUES(tool_params), cron_expression=VALUES(cron_expression), "
                    "enabled=TRUE, last_status=NULL, last_message=NULL",
                    (
                        task_id,
                        f"reviewed {group_id}",
                        contract["tool_name"],
                        json.dumps(arguments, separators=(",", ":")),
                        contract["cron_expression"],
                    ),
                )

        managed_ids = tuple(sorted(contracts))
        managed_placeholders = ",".join("%s" for _ in managed_ids)
        with self._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM scheduled_tasks WHERE id IN ({managed_placeholders})",
                managed_ids,
            )
            cursor.execute(
                "DELETE FROM scheduled_tasks WHERE id IN "
                "('send_order_1200', 'clockin_unknown_1200', "
                "'clockin_daxiang_1830', 'clockin_daxiang_s_1833', "
                "'integration_unrelated_tms')"
            )
            cursor.execute("DELETE FROM control_plane_task_cutover_backup_014")

            # Empty bootstrap state remains valid and reentrant.
            execute_migration(cursor)
            execute_migration(cursor)

            # One candidate turns the full reviewed set into a requirement.
            seed_reviewed_tasks(cursor, {managed_ids[0]})
            with self.assertRaises(self.pymysql.MySQLError):
                execute_migration(cursor)
            cursor.execute(
                "SELECT COUNT(*) AS count FROM control_plane_task_cutover_backup_014"
            )
            self.assertEqual(0, cursor.fetchone()["count"])

            # Exact production IDs and reviewed legacy semantics become the
            # same code-owned canonical arguments. The reviewed login identity
            # is recovered only in-memory from its code-owned fingerprint and
            # is never embedded in test source or output.
            seed_reviewed_tasks(cursor)
            execute_migration(cursor)
            execute_migration(cursor)
            cursor.execute(
                f"SELECT id, tool_name, tool_params, cron_expression, enabled "
                f"FROM scheduled_tasks WHERE id IN ({managed_placeholders})",
                managed_ids,
            )
            migrated = {row["id"]: row for row in cursor.fetchall()}
            self.assertEqual(set(contracts), set(migrated))
            for task_id, contract in contracts.items():
                row = migrated[task_id]
                arguments = row["tool_params"]
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                self.assertEqual(dict(contract["canonical_arguments"]), arguments)
                self.assertEqual(contract["tool_name"], row["tool_name"])
                self.assertEqual(contract["cron_expression"], row["cron_expression"])
                self.assertTrue(row["enabled"])

            cursor.execute(
                f"SELECT COUNT(*) AS count FROM control_plane_task_cutover_backup_014 "
                f"WHERE id IN ({managed_placeholders})",
                managed_ids,
            )
            self.assertEqual(51, cursor.fetchone()["count"])
            cursor.execute(
                "SELECT tool_params FROM control_plane_task_cutover_backup_014 "
                "WHERE id='daily_sign_0500'"
            )
            backup_arguments = cursor.fetchone()["tool_params"]
            if isinstance(backup_arguments, str):
                backup_arguments = json.loads(backup_arguments)
            self.assertEqual(legacy_arguments["daily_sign"], backup_arguments)

            # Extra fields fail before mutation.
            changed_task_id = "daily_sign_0500"
            changed_arguments = dict(contracts[changed_task_id]["canonical_arguments"])
            changed_arguments["unexpected"] = True
            cursor.execute(
                "UPDATE scheduled_tasks SET tool_params=CAST(%s AS JSON) WHERE id=%s",
                (json.dumps(changed_arguments), changed_task_id),
            )
            with self.assertRaises(self.pymysql.MySQLError):
                execute_migration(cursor)
            cursor.execute(
                "SELECT tool_params, enabled FROM scheduled_tasks WHERE id=%s",
                (changed_task_id,),
            )
            unchanged = cursor.fetchone()
            unchanged_arguments = unchanged["tool_params"]
            if isinstance(unchanged_arguments, str):
                unchanged_arguments = json.loads(unchanged_arguments)
            self.assertTrue(unchanged_arguments["unexpected"])
            self.assertTrue(unchanged["enabled"])
            cursor.execute(
                "UPDATE scheduled_tasks SET tool_params=CAST(%s AS JSON) WHERE id=%s",
                (
                    json.dumps(contracts[changed_task_id]["canonical_arguments"]),
                    changed_task_id,
                ),
            )

            # An unreviewed ID is rejected even when disabled.
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES ('send_order_1200', 'unreviewed', 'sync_daily_send_orders', "
                "CAST(%s AS JSON), '0 12 * * *', FALSE)",
                (json.dumps({"account_id": "price_default"}),),
            )
            with self.assertRaises(self.pymysql.MySQLError):
                execute_migration(cursor)
            cursor.execute("DELETE FROM scheduled_tasks WHERE id='send_order_1200'")

            # The exact optional pair is backed up and normalized without
            # changing IDs, cron expressions, enabled flags, or audit fields.
            seed_reviewed_tasks(cursor)
            cursor.execute("DELETE FROM control_plane_task_cutover_backup_014")
            for task_id, contract in sorted(clock_contracts.items()):
                legacy_clock = self.runner._legacy_clock_arguments(
                    task_id,
                    contract["canonical_arguments"],
                )
                cursor.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, tool_name, tool_params, cron_expression, enabled, last_status) "
                    "VALUES (%s, %s, 'tms_query', CAST(%s AS JSON), %s, TRUE, 'legacy')",
                    (
                        task_id,
                        f"legacy {task_id}",
                        json.dumps(legacy_clock, separators=(",", ":")),
                        contract["cron_expression"],
                    ),
                )
            execute_migration(cursor)
            cursor.execute(
                "SELECT id, name, tool_name, tool_params, cron_expression, enabled, last_status "
                "FROM scheduled_tasks WHERE id IN "
                "('clockin_daxiang_1830', 'clockin_daxiang_s_1833')"
            )
            migrated_clocks = {row["id"]: row for row in cursor.fetchall()}
            self.assertEqual(set(clock_contracts), set(migrated_clocks))
            for task_id, contract in clock_contracts.items():
                clock = migrated_clocks[task_id]
                arguments = clock["tool_params"]
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                self.assertEqual("clock_in_dual", clock["tool_name"])
                self.assertEqual(dict(contract["canonical_arguments"]), arguments)
                self.assertEqual(contract["cron_expression"], clock["cron_expression"])
                self.assertTrue(clock["enabled"])
                self.assertEqual(f"legacy {task_id}", clock["name"])
                self.assertEqual("legacy", clock["last_status"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM control_plane_task_cutover_backup_014"
            )
            self.assertEqual(104, cursor.fetchone()["count"])

            # A partial or disabled pair fails before mutation.
            cursor.execute(
                "DELETE FROM scheduled_tasks WHERE id='clockin_daxiang_s_1833'"
            )
            with self.assertRaises(self.pymysql.MySQLError):
                execute_migration(cursor)
            remaining_contract = clock_contracts["clockin_daxiang_1830"]
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES ('clockin_daxiang_s_1833', 'restored pair', 'clock_in_dual', "
                "CAST(%s AS JSON), '33 18 * * *', FALSE)",
                (
                    json.dumps(
                        clock_contracts["clockin_daxiang_s_1833"]["canonical_arguments"],
                        separators=(",", ":"),
                    ),
                ),
            )
            with self.assertRaises(self.pymysql.MySQLError):
                execute_migration(cursor)
            cursor.execute(
                "UPDATE scheduled_tasks SET enabled=TRUE "
                "WHERE id='clockin_daxiang_s_1833'"
            )

            # Wrong values and unknown clock IDs fail closed, while an
            # unrelated TMS read does not enter the clock candidate set.
            changed_clock_arguments = dict(remaining_contract["canonical_arguments"])
            changed_clock_arguments["delay_seconds"] = 3
            cursor.execute(
                "UPDATE scheduled_tasks SET tool_params=CAST(%s AS JSON) "
                "WHERE id='clockin_daxiang_1830'",
                (json.dumps(changed_clock_arguments),),
            )
            with self.assertRaises(self.pymysql.MySQLError):
                execute_migration(cursor)
            cursor.execute(
                "UPDATE scheduled_tasks SET tool_params=CAST(%s AS JSON) "
                "WHERE id='clockin_daxiang_1830'",
                (json.dumps(remaining_contract["canonical_arguments"]),),
            )
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES ('clockin_unknown_1200', 'unknown clock', 'tms_query', "
                "CAST(%s AS JSON), '0 12 * * *', FALSE)",
                (json.dumps({"endpoint": "/clock_in_dual", "params": {}}),),
            )
            with self.assertRaises(self.pymysql.MySQLError):
                execute_migration(cursor)
            cursor.execute(
                "DELETE FROM scheduled_tasks WHERE id IN "
                "('clockin_unknown_1200', 'clockin_daxiang_1830', 'clockin_daxiang_s_1833')"
            )
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES ('integration_unrelated_tms', 'unrelated read', 'tms_query', "
                "CAST(%s AS JSON), '0 4 * * *', TRUE)",
                (json.dumps({"endpoint": "/query", "params": {}}),),
            )
            execute_migration(cursor)
            cursor.execute(
                "DELETE FROM scheduled_tasks WHERE id='integration_unrelated_tms'"
            )

    def test_scheduled_task_approval_policy_schema_defaults_and_constraints(self):
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT DATA_TYPE, COLUMN_DEFAULT FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scheduled_tasks' "
                "AND COLUMN_NAME='configuration_version'"
            )
            configuration_version = cursor.fetchone()
            self.assertEqual("bigint", configuration_version["DATA_TYPE"])
            self.assertEqual("1", str(configuration_version["COLUMN_DEFAULT"]))
            cursor.execute(
                "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scheduled_tasks' "
                "AND COLUMN_NAME='updated_at'"
            )
            self.assertEqual("datetime", cursor.fetchone()["DATA_TYPE"])

        task_id = "integration_policy_default"
        with self._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, automation_id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES (%s, 'integration_policy_default_automation', "
                "'policy default', 'query_waybill', JSON_OBJECT(), "
                "'0 3 * * *', TRUE)",
                (task_id,),
            )
            cursor.execute(
                "INSERT INTO scheduled_task_approval_policies (task_id) VALUES (%s)",
                (task_id,),
            )
            cursor.execute(
                "SELECT mode, contract_hash, approved_by_actor_id, version "
                "FROM scheduled_task_approval_policies WHERE task_id=%s",
                (task_id,),
            )
            policy = cursor.fetchone()
            self.assertEqual("REQUIRE_EACH_RUN", policy["mode"])
            self.assertIsNone(policy["contract_hash"])
            self.assertIsNone(policy["approved_by_actor_id"])
            self.assertEqual(1, policy["version"])

            with self.assertRaises(self.pymysql.MySQLError):
                cursor.execute(
                    "UPDATE scheduled_task_approval_policies "
                    "SET mode='EXACT_SCHEDULE_EXEMPT' WHERE task_id=%s",
                    (task_id,),
                )
            cursor.execute(
                "DELETE FROM scheduled_task_approval_policies WHERE task_id=%s",
                (task_id,),
            )
            cursor.execute("DELETE FROM scheduled_tasks WHERE id=%s", (task_id,))

    def test_policy_fk_aligns_to_preexisting_parent_collation_and_retains_events(self):
        task_id = "integration_collation_policy"
        with self._connection(self.collation_database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT TABLE_NAME, CHARACTER_SET_NAME, COLLATION_NAME "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() "
                    "AND ((TABLE_NAME='scheduled_tasks' AND COLUMN_NAME='id') "
                    "OR (TABLE_NAME IN ('scheduled_task_approval_policies', "
                    "'scheduled_task_approval_policy_events') AND COLUMN_NAME='task_id')) "
                    "ORDER BY TABLE_NAME"
                )
                columns = cursor.fetchall()
                self.assertEqual(3, len(columns))
                self.assertEqual(
                    {"utf8mb4"},
                    {str(row["CHARACTER_SET_NAME"]) for row in columns},
                )
                self.assertEqual(
                    {"utf8mb4_general_ci"},
                    {str(row["COLLATION_NAME"]) for row in columns},
                )
                cursor.execute(
                    "SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS "
                    "WHERE CONSTRAINT_SCHEMA=DATABASE() "
                    "AND CONSTRAINT_NAME='fk_scheduled_task_policy_task'"
                )
                self.assertEqual("CASCADE", cursor.fetchone()["DELETE_RULE"])
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM information_schema.REFERENTIAL_CONSTRAINTS "
                    "WHERE CONSTRAINT_SCHEMA=DATABASE() "
                    "AND TABLE_NAME='scheduled_task_approval_policy_events'"
                )
                self.assertEqual(0, cursor.fetchone()["count"])

                cursor.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, tool_name, tool_params, cron_expression, enabled) "
                    "VALUES (%s, 'collation policy', 'query_waybill', JSON_OBJECT(), "
                    "'0 3 * * *', TRUE)",
                    (task_id,),
                )
                cursor.execute(
                    "INSERT INTO scheduled_task_approval_policies (task_id) VALUES (%s)",
                    (task_id,),
                )
                cursor.execute(
                    "INSERT INTO scheduled_task_approval_policy_events "
                    "(task_id, to_mode, actor_id, actor_role, reason, correlation_id, request_id) "
                    "VALUES (%s, 'REQUIRE_EACH_RUN', 'integration', 'super_admin', "
                    "'collation_test', %s, %s)",
                    (task_id, str(uuid4()), str(uuid4())),
                )
                cursor.execute("DELETE FROM scheduled_tasks WHERE id=%s", (task_id,))
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM scheduled_task_approval_policies "
                    "WHERE task_id=%s",
                    (task_id,),
                )
                self.assertEqual(0, cursor.fetchone()["count"])
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM scheduled_task_approval_policy_events "
                    "WHERE task_id=%s",
                    (task_id,),
                )
                self.assertEqual(1, cursor.fetchone()["count"])
                cursor.execute(
                    "DELETE FROM scheduled_task_approval_policy_events WHERE task_id=%s",
                    (task_id,),
                )

    def test_scheduled_task_policy_event_request_is_idempotent_and_exact_is_audited(self):
        task_id = "integration_policy_exact"
        snapshot = json.dumps(
            {
                "task_id": task_id,
                "tool_name": "integration_external_write",
                "operation_type": "external_write",
                "cron_expression": "30 18 * * *",
                "enabled": True,
            },
            separators=(",", ":"),
        )
        digest = "a" * 64
        tool_digest = "b" * 64
        request_id = str(uuid4())
        correlation_id = str(uuid4())
        with self._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM information_schema.REFERENTIAL_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=DATABASE() "
                "AND TABLE_NAME='scheduled_task_approval_policy_events'"
            )
            self.assertEqual(0, cursor.fetchone()["count"])
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, automation_id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES (%s, 'integration_policy_exact_automation', "
                "'policy exact', 'integration_external_write', JSON_OBJECT(), "
                "'30 18 * * *', TRUE)",
                (task_id,),
            )
            cursor.execute(
                "INSERT INTO scheduled_task_approval_policies "
                "(task_id, mode, contract_hash, contract_snapshot_json, tool_contract_hash, "
                "approved_by_actor_id, approved_by_actor_role, approved_at) "
                "VALUES (%s, 'EXACT_SCHEDULE_EXEMPT', %s, CAST(%s AS JSON), %s, "
                "'system:migration:control-plane-v1', 'migration_authority', NOW(6))",
                (task_id, digest, snapshot, tool_digest),
            )
            event_values = (
                task_id,
                digest,
                snapshot,
                tool_digest,
                correlation_id,
                request_id,
            )
            event_sql = (
                "INSERT INTO scheduled_task_approval_policy_events "
                "(task_id, from_mode, to_mode, contract_hash, contract_snapshot_json, "
                "tool_contract_hash, actor_id, actor_role, reason, correlation_id, request_id) "
                "VALUES (%s, 'REQUIRE_EACH_RUN', 'EXACT_SCHEDULE_EXEMPT', %s, "
                "CAST(%s AS JSON), %s, 'system:migration:control-plane-v1', "
                "'migration_authority', 'bootstrap', %s, %s)"
            )
            cursor.execute(event_sql, event_values)
            with self.assertRaises(self.pymysql.IntegrityError):
                cursor.execute(event_sql, event_values)

            # Time-slot edits replace persisted scheduled-task rows. Current
            # policy state follows the task, while the immutable audit event
            # must survive after that task ID has been removed.
            cursor.execute("DELETE FROM scheduled_tasks WHERE id=%s", (task_id,))
            cursor.execute(
                "SELECT COUNT(*) AS count FROM scheduled_task_approval_policies "
                "WHERE task_id=%s",
                (task_id,),
            )
            self.assertEqual(0, cursor.fetchone()["count"])
            cursor.execute(
                "SELECT to_mode, request_id FROM scheduled_task_approval_policy_events "
                "WHERE task_id=%s",
                (task_id,),
            )
            retained_event = cursor.fetchone()
            self.assertEqual("EXACT_SCHEDULE_EXEMPT", retained_event["to_mode"])
            self.assertEqual(request_id, retained_event["request_id"])
            cursor.execute(
                "DELETE FROM scheduled_task_approval_policy_events WHERE task_id=%s",
                (task_id,),
            )

    def test_policy_bootstrap_restore_is_exact_reentrant_and_transactional(self):
        database = self.policy_restore_database
        migration_actor = self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ID
        migration_role = self.runner.CONTROL_PLANE_MIGRATION_ACTOR_ROLE
        migration_task = "integration_restore_migration"
        admin_task = "integration_restore_admin"
        unrelated_task = "integration_restore_unrelated"

        def insert_exact_policy(
            cursor,
            *,
            task_id: str,
            actor_id: str,
            actor_role: str,
            reason: str | None,
        ) -> str | None:
            contract_hash = hashlib.sha256(f"contract:{task_id}".encode()).hexdigest()
            tool_hash = hashlib.sha256(f"tool:{task_id}".encode()).hexdigest()
            snapshot = json.dumps(
                {"task_id": task_id, "enabled": True},
                separators=(",", ":"),
            )
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, automation_id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES (%s, %s, %s, 'query_waybill', JSON_OBJECT(), "
                "'0 3 * * *', TRUE)",
                (task_id, f"{task_id}_automation", task_id),
            )
            cursor.execute(
                "INSERT INTO scheduled_task_approval_policies "
                "(task_id, mode, contract_hash, contract_snapshot_json, "
                "tool_contract_hash, approved_by_actor_id, approved_by_actor_role, "
                "approved_at, version) VALUES (%s, 'EXACT_SCHEDULE_EXEMPT', %s, "
                "CAST(%s AS JSON), %s, %s, %s, NOW(6), 2)",
                (
                    task_id,
                    contract_hash,
                    snapshot,
                    tool_hash,
                    actor_id,
                    actor_role,
                ),
            )
            if reason is None:
                return None
            request_id = str(uuid4())
            cursor.execute(
                "INSERT INTO scheduled_task_approval_policy_events "
                "(task_id, from_mode, to_mode, contract_hash, "
                "contract_snapshot_json, tool_contract_hash, actor_id, actor_role, "
                "reason, correlation_id, request_id) VALUES (%s, "
                "'REQUIRE_EACH_RUN', 'EXACT_SCHEDULE_EXEMPT', %s, "
                "CAST(%s AS JSON), %s, %s, %s, %s, %s, %s)",
                (
                    task_id,
                    contract_hash,
                    snapshot,
                    tool_hash,
                    actor_id,
                    actor_role,
                    reason,
                    str(uuid4()),
                    request_id,
                ),
            )
            return request_id

        def insert_domain_chain(cursor, *, task_id: str, request_id: str) -> str:
            event_id = str(uuid4())
            correlation_id = str(uuid4())
            payload = json.dumps({"task_id": task_id}, separators=(",", ":"))
            payload_hash = hashlib.sha256(payload.encode()).hexdigest()
            cursor.execute(
                "INSERT INTO domain_events "
                "(event_id, event_type, schema_version, source_system, source_event_id, "
                "entity_type, entity_id, occurred_at, observed_at, correlation_id, "
                "payload_json, payload_sha256) VALUES (%s, "
                "'scheduled_task.approval_policy_changed', 1, 'agent', %s, "
                "'scheduled_task', %s, NOW(6), NOW(6), %s, CAST(%s AS JSON), %s)",
                (
                    event_id,
                    f"{task_id}:{request_id}",
                    task_id,
                    correlation_id,
                    payload,
                    payload_hash,
                ),
            )
            cursor.execute(
                "INSERT INTO outbox_events "
                "(event_id, consumer_name, topic, partition_key) "
                "VALUES (%s, 'integration-release-gate', "
                "'scheduled_task.approval_policy_changed', %s)",
                (event_id, task_id),
            )
            cursor.execute(
                "INSERT INTO event_consumptions (consumer_name, event_id, processed_at) "
                "VALUES ('integration-release-gate', %s, NOW(6))",
                (event_id,),
            )
            return event_id

        with self._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                migration_request = insert_exact_policy(
                    cursor,
                    task_id=migration_task,
                    actor_id=migration_actor,
                    actor_role=migration_role,
                    reason="control_plane_v1_bootstrap",
                )
                admin_request = insert_exact_policy(
                    cursor,
                    task_id=admin_task,
                    actor_id="integration-admin",
                    actor_role="super_admin",
                    reason="console_policy_change",
                )
                self.assertIsNotNone(migration_request)
                self.assertIsNotNone(admin_request)
                migration_domain_event = insert_domain_chain(
                    cursor,
                    task_id=migration_task,
                    request_id=str(migration_request),
                )
                admin_domain_event = insert_domain_chain(
                    cursor,
                    task_id=admin_task,
                    request_id=str(admin_request),
                )
                unrelated_request = str(uuid4())
                cursor.execute(
                    "INSERT INTO scheduled_task_approval_policy_events "
                    "(task_id, to_mode, actor_id, actor_role, reason, "
                    "correlation_id, request_id) VALUES (%s, 'REQUIRE_EACH_RUN', "
                    "'integration-system', 'system', 'unrelated_audit', %s, %s)",
                    (unrelated_task, str(uuid4()), unrelated_request),
                )
                unrelated_domain_event = insert_domain_chain(
                    cursor,
                    task_id=unrelated_task,
                    request_id=unrelated_request,
                )
                cursor.execute(
                    "INSERT INTO scheduled_task_approval_policy_events "
                    "(task_id, to_mode, actor_id, actor_role, reason, "
                    "correlation_id, request_id) VALUES (%s, 'REQUIRE_EACH_RUN', "
                    "%s, %s, 'control_plane_v1_bootstrap_complete', %s, %s)",
                    (
                        self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID,
                        migration_actor,
                        migration_role,
                        self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
                        self.runner.CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID,
                    ),
                )

        with patch.dict(os.environ, self._environment(database), clear=False):
            self.assertEqual(0, self.runner.restore_control_plane_policy_bootstrap())

        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT task_id FROM scheduled_task_approval_policies ORDER BY task_id"
            )
            self.assertEqual([admin_task], [row["task_id"] for row in cursor.fetchall()])
            cursor.execute(
                "SELECT task_id, reason FROM scheduled_task_approval_policy_events "
                "ORDER BY task_id, event_id"
            )
            self.assertEqual(
                [(admin_task, "console_policy_change"), (unrelated_task, "unrelated_audit")],
                [(row["task_id"], row["reason"]) for row in cursor.fetchall()],
            )
            for event_id, expected in (
                (migration_domain_event, 0),
                (admin_domain_event, 1),
                (unrelated_domain_event, 1),
            ):
                for table in ("domain_events", "outbox_events", "event_consumptions"):
                    cursor.execute(
                        f"SELECT COUNT(*) AS count FROM {table} WHERE event_id=%s",
                        (event_id,),
                    )
                    self.assertEqual(expected, cursor.fetchone()["count"])

        tracked_tables = (
            "scheduled_task_approval_policies",
            "scheduled_task_approval_policy_events",
            "domain_events",
            "outbox_events",
            "event_consumptions",
        )

        def table_counts() -> dict[str, int]:
            with self._connection(database) as connection:
                with connection.cursor() as cursor:
                    counts = {}
                    for table in tracked_tables:
                        cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
                        counts[table] = int(cursor.fetchone()["count"])
            return counts

        before_reentrant = table_counts()
        with patch.dict(os.environ, self._environment(database), clear=False):
            self.assertEqual(0, self.runner.restore_control_plane_policy_bootstrap())
        self.assertEqual(before_reentrant, table_counts())

        valid_task = "integration_restore_valid_second"
        orphan_task = "integration_restore_orphan"
        with self._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                valid_request = insert_exact_policy(
                    cursor,
                    task_id=valid_task,
                    actor_id=migration_actor,
                    actor_role=migration_role,
                    reason="control_plane_v1_bootstrap",
                )
                self.assertIsNotNone(valid_request)
                insert_domain_chain(
                    cursor,
                    task_id=valid_task,
                    request_id=str(valid_request),
                )
                self.assertIsNone(
                    insert_exact_policy(
                        cursor,
                        task_id=orphan_task,
                        actor_id=migration_actor,
                        actor_role=migration_role,
                        reason=None,
                    )
                )

        before_failed_restore = table_counts()
        with (
            patch.dict(os.environ, self._environment(database), clear=False),
            self.assertRaisesRegex(
                RuntimeError,
                "MIGRATION_EXACT_POLICY_BOOTSTRAP_EVENT_MISSING",
            ),
        ):
            self.runner.restore_control_plane_policy_bootstrap()
        self.assertEqual(before_failed_restore, table_counts())

    def test_database_backed_pre_018_release_manifest_initial_and_later_policy_gates(
        self,
    ):
        database = self.release_manifest_database
        exact_contracts = self._seed_release_manifest(database)

        def check(*, initial: bool = False) -> int:
            with patch.dict(os.environ, self._environment(database), clear=False):
                return self.runner.check_control_plane_release_manifest(
                    expect_initial_production_manifest=initial
                )

        self.assertEqual(0, check(initial=True))
        self.assertEqual(0, check())
        task_ids = sorted(exact_contracts)
        admin_task, credential_task, default_task, stale_task, missing_task = task_ids[:5]

        self._set_manifest_require_policy(
            database,
            admin_task,
            actor_id="integration-admin",
            actor_role="super_admin",
            reason="console_policy_change",
        )
        self.assertEqual(1, check(initial=True))
        self.assertEqual(0, check())
        self._restore_manifest_exact_policy(
            database,
            admin_task,
            exact_contracts[admin_task],
        )

        approval = self.runner._load_scheduled_task_approval_contract_module()
        self._set_manifest_require_policy(
            database,
            credential_task,
            actor_id=approval.ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
            actor_role="system",
            reason=approval.ACCOUNT_CREDENTIAL_CHANGE_REASON,
        )
        self.assertEqual(1, check(initial=True))
        self.assertEqual(0, check())
        self._restore_manifest_exact_policy(
            database,
            credential_task,
            exact_contracts[credential_task],
        )

        with self._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_task_approval_policies SET "
                    "mode='REQUIRE_EACH_RUN', contract_hash=NULL, "
                    "contract_snapshot_json=NULL, tool_contract_hash=NULL, "
                    "approved_by_actor_id=NULL, approved_by_actor_role=NULL, "
                    "approved_at=NULL, version=1 WHERE task_id=%s",
                    (default_task,),
                )
                self.assertEqual(1, cursor.rowcount)
        self.assertEqual(1, check())
        self._restore_manifest_exact_policy(
            database,
            default_task,
            exact_contracts[default_task],
        )

        with self._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_tasks SET configuration_version=2 WHERE id=%s",
                    (stale_task,),
                )
                self.assertEqual(1, cursor.rowcount)
        self.assertEqual(1, check())
        with self._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE scheduled_tasks SET configuration_version=1 WHERE id=%s",
                    (stale_task,),
                )
                cursor.execute("DELETE FROM scheduled_tasks WHERE id=%s", (missing_task,))
                self.assertEqual(1, cursor.rowcount)
        self.assertEqual(1, check())

    def test_protected_write_gate_uses_real_mysql_step_state(self):
        database = self.protected_write_database
        repository = self._repository(database)
        aggregate = self._aggregate_rows("protected-write-gate")
        with repository.unit_of_work() as uow:
            receipt = uow.command_gateway_create(*aggregate)
            uow.commit()
        run_id = receipt["run_id"]

        def insert_step(*, status: str, operation_type: str, order: int) -> str:
            step_id = str(uuid4())
            with self._connection(database, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO agent_run_steps "
                        "(step_id, run_id, step_key, step_order, tool_name, tool_version, "
                        "operation_type, risk_level, status, requires_approval, retry_safe, "
                        "idempotency_key) VALUES (%s, %s, %s, %s, %s, '1.0.0', %s, "
                        "'HIGH', %s, TRUE, FALSE, %s)",
                        (
                            step_id,
                            run_id,
                            f"gate-{order}",
                            order,
                            f"integration_gate_{order}",
                            operation_type,
                            status,
                            f"integration-gate:{step_id}",
                        ),
                    )
            return step_id

        def check() -> int:
            with patch.dict(os.environ, self._environment(database), clear=False):
                return self.runner.check_running_protected_writes()

        order = 0
        for operation_type in ("EXTERNAL_WRITE", "FINANCIAL_WRITE", "DESTRUCTIVE"):
            for status in ("RUNNING", "VERIFYING"):
                order += 1
                step_id = insert_step(
                    status=status,
                    operation_type=operation_type,
                    order=order,
                )
                with self.subTest(operation_type=operation_type, status=status):
                    self.assertEqual(1, check())
                with self._connection(database, autocommit=True) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM agent_run_steps WHERE step_id=%s",
                            (step_id,),
                        )

        safe_rows = (
            ("RUNNING", "INTERNAL_PROJECTION_WRITE"),
            ("VERIFYING", "INTERNAL_PROJECTION_WRITE"),
            ("COMPLETED", "EXTERNAL_WRITE"),
            ("FAILED_TERMINAL", "FINANCIAL_WRITE"),
            ("CANCELLED", "DESTRUCTIVE"),
        )
        for status, operation_type in safe_rows:
            order += 1
            insert_step(status=status, operation_type=operation_type, order=order)
        self.assertEqual(0, check())

    def test_automation_project_018_forward_restore_reapply_and_atomic_config(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_automation_project_018_forward_restore_reapply_and_atomic_config(
            self
        )

    def test_automation_project_018_partial_rerun_is_safe(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_automation_project_018_partial_rerun_is_safe(
            self
        )

    @unittest.skip("Windows Worker is deferred from the current server-only release")
    def test_automation_worker_dispatch_is_durable_exact_device_and_replay_safe(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_automation_worker_dispatch_is_durable_exact_device_and_replay_safe(
            self
        )

    def test_automation_project_018_case_drift_fails_before_ddl(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_automation_project_018_case_drift_fails_before_ddl(
            self
        )

    def test_automation_project_grouped_approval_rolls_back_real_mysql_transaction(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_grouped_approval_second_cas_failure_is_atomic(
            self
        )

    def test_daily_sign_fresh_readback_rejects_event_and_publication_tamper(self):
        DAILY_SIGN_SCENARIOS.run_test_daily_sign_fresh_readback_rejects_mysql_tamper(
            self
        )
