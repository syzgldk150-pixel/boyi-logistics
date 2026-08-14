from __future__ import annotations

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


RUN_MYSQL = os.getenv("RUN_MYSQL_INTEGRATION") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_migration_runner():
    path = PROJECT_ROOT / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("mysql_integration_migrations", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
        cls.databases = (
            cls.database,
            cls.upgrade_database,
            cls.partial_database,
            cls.rollback_database,
            cls.collation_database,
        )
        cls.all_databases = (*cls.databases, cls.compat_database)
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
        cls._apply_through(cls.compat_database, "013")

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
                for version, path in migrations:
                    for statement in cls.runner.split_sql_statements(
                        path.read_text(encoding="utf-8")
                    ):
                        cursor.execute(statement)
                    cursor.execute(
                        "INSERT INTO schema_migrations (version, filename, checksum) "
                        "VALUES (%s, %s, %s)",
                        (version, path.name, cls.runner.migration_checksum(path)),
                    )

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
    def _repository(cls):
        from shared.orchestration_repository import OrchestrationRepository

        def connect():
            return cls.pymysql.connect(
                host=cls.host,
                port=cls.port,
                user=cls.user,
                password=cls.password,
                database=cls.database,
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
        for database in self.databases:
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
                "(id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES (%s, 'policy default', 'query_waybill', JSON_OBJECT(), "
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
                "(id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES (%s, 'policy exact', 'integration_external_write', JSON_OBJECT(), "
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
