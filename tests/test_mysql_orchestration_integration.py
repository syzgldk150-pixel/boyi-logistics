from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

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
FEISHU_QUEUE_SCENARIOS = _load_test_scenarios("mysql_feishu_queue_scenarios")


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

    def test_feishu_notification_lease_holds_the_database_binding_lane(self): FEISHU_QUEUE_SCENARIOS.run_test_feishu_notification_lease_holds_the_database_binding_lane(self)

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

    def test_scheduler_supersession_selector_is_exact_and_terminal_retry_observes_cancellation(self):
        AUTOMATION_PROJECT_SCENARIOS.run_test_scheduler_supersession_selector_is_exact_and_terminal_retry_observes_cancellation(
            self
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
