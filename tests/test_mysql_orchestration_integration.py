from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from tests.mysql_database_cleanup import drop_test_database


RUN_MYSQL = os.getenv("RUN_MYSQL_INTEGRATION") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_migration_runner():
    path = PROJECT_ROOT / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("mysql_integration_migrations", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_test_scenarios(module_name: str):
    path = PROJECT_ROOT / "tests" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


AUTOMATION_PROJECT_SCENARIOS = _load_test_scenarios("mysql_automation_project_scenarios")
DAILY_SIGN_SCENARIOS = _load_test_scenarios("mysql_daily_sign_scenarios")


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
        cls.project_invocation_database = (
            f"{cls.database[:-5]}_project_invocation_test"
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
        cls.legacy_plugin_full_auto_database = (
            f"{cls.database[:-5]}_legacy_plugin_full_auto_test"
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
            cls.project_invocation_database,
            cls.worker_dispatch_database,
            cls.daily_sign_readback_database,
            cls.feishu_queue_recovery_database,
            cls.legacy_plugin_full_auto_database,
        )
        cls.runner = _load_migration_runner()

        with cls._server_connection() as connection, connection.cursor() as cursor:
            for database in cls.all_databases:
                drop_test_database(cursor, database)
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
        cls._apply_through(cls.legacy_plugin_full_auto_database, "017")
        cls._seed_required_project_resources(cls.legacy_plugin_full_auto_database)
        cls._apply_through(cls.legacy_plugin_full_auto_database, "023")
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
                drop_test_database(cursor, database)

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
            "MIGRATION_BACKUP_DIRECTORY": os.getenv("MIGRATION_BACKUP_DIRECTORY", str(PROJECT_ROOT / ".task_tmp" / "mysql-migration-backups")),
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

    def test_automation_project_invocation_serializes_and_replays_on_real_mysql(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_project_invocation_serializes_and_replays_on_real_mysql(
            self
        )

    def test_automation_project_024_restores_only_original_plugin_writer(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_automation_project_024_original_plugin_full_auto(
            self
        )

    def test_generation_write_lock_order_races(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_generation_write_lock_order_races(self)

    def test_daily_sign_fresh_readback_rejects_event_and_publication_tamper(self):
        DAILY_SIGN_SCENARIOS.run_test_daily_sign_fresh_readback_rejects_mysql_tamper(
            self
        )
