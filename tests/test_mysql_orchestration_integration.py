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
        cls.databases = (cls.database, cls.upgrade_database, cls.partial_database)
        cls.runner = _load_migration_runner()

        with cls._server_connection() as connection, connection.cursor() as cursor:
            for database in cls.databases:
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

    @classmethod
    def tearDownClass(cls) -> None:
        with cls._server_connection() as connection, connection.cursor() as cursor:
            for database in cls.databases:
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
        for label in ("outbox-claim-a", "outbox-claim-b"):
            rows = self._aggregate_rows(label)
            with repository.unit_of_work() as uow:
                uow.command_gateway_create(*rows)
                uow.commit()

        first = repository.unit_of_work()
        second = repository.unit_of_work()
        try:
            first.__enter__()
            first_claim = first.outbox.claim(
                "outbox-worker-a",
                consumer_name="integration-consumer",
                limit=1,
            )
            second.__enter__()
            second_claim = second.outbox.claim(
                "outbox-worker-b",
                consumer_name="integration-consumer",
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

    def test_task_cutover_migrates_closed_scheduler_families_fail_closed(self):
        migration = next(
            path
            for version, path in self.runner.discover_migrations()
            if version == "014"
        )
        statements = self.runner.split_sql_statements(migration.read_text(encoding="utf-8"))
        rows = [
            (
                "arrive_list_0830",
                "arrival no account",
                "sync_arrive_list",
                '{"target_date":"","login_site_code":"legacy-site"}',
            ),
            (
                "arrive_list_0930",
                "arrival explicit account",
                "sync_arrive_list",
                '{"account_id":"configured-account","keep_me":true}',
            ),
            (
                "arrival_stats_1000",
                "arrival conflicting account",
                "sync_arrival_stats",
                '{"account_id":"configured-account",'
                '"arrive_list_request_body":{"params":{"accountId":"other-account"}}}',
            ),
            (
                "daily_sign_1100",
                "daily sign custom time",
                "sync_daily_should_sign",
                '{"days":3,"keep_me":true}',
            ),
            (
                "delivery_status_1030",
                "delivery status custom time",
                "sync_delivery_status",
                '{}',
            ),
            (
                "send_order_2150",
                "send order custom time",
                "sync_daily_send_orders",
                '{}',
            ),
            (
                "site_send_1900",
                "site send custom time",
                "sync_site_send_list",
                '{}',
            ),
            (
                "yunda_dispatch_forecast_1700",
                "yunda forecast legacy profile",
                "sync_yunda_dispatch_forecast",
                '{"session_profile":"yunda","dest_brch":"56739382"}',
            ),
            (
                "yunda_send_waybills_2100",
                "yunda send legacy defaults",
                "sync_yunda_send_waybills",
                '{"session_profile":"yunda","target_date":"","ensure_fields":true}',
            ),
            (
                "finance_bills_0010",
                "finance canonical",
                "sync_finance_bills",
                '{"mode":"sync","rescan_days":7}',
            ),
            (
                "clockin_daxiang_s_1830",
                "Daxiang S clock",
                "tms_query",
                '{"endpoint":"/clock_in_dual","params":{"timeout_sec":600,'
                '"params":{"sitecode":"configured-site","sitefbcode":"configured-fb",'
                '"site_name":"configured name","site_fb_name":"configured fb name",'
                '"first_type":"交件到港","second_type":"接件离港",'
                '"delay_seconds":4,"administrator_note":"keep"}}}',
            ),
        ]
        with self._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM control_plane_task_cutover_backup_014")
            for task_id, name, tool_name, tool_params in rows:
                cursor.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, name, tool_name, tool_params, cron_expression, enabled) "
                    "VALUES (%s, %s, %s, CAST(%s AS JSON), '0 1 * * *', TRUE) "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name), tool_name=VALUES(tool_name), "
                    "tool_params=VALUES(tool_params), cron_expression=VALUES(cron_expression), enabled=TRUE, "
                    "last_status=NULL, last_message=NULL",
                    (task_id, name, tool_name, tool_params),
                )
            for statement in statements:
                cursor.execute(statement)
            for statement in statements:
                cursor.execute(statement)

            cursor.execute(
                "SELECT id, tool_name, tool_params, enabled, last_message "
                "FROM scheduled_tasks WHERE id IN "
                "('arrive_list_0830', 'arrive_list_0930', 'arrival_stats_1000', "
                "'daily_sign_1100', 'delivery_status_1030', 'send_order_2150', "
                "'site_send_1900', 'yunda_dispatch_forecast_1700', "
                "'yunda_send_waybills_2100', 'finance_bills_0010', "
                "'clockin_daxiang_s_1830')"
            )
            migrated = {row["id"]: row for row in cursor.fetchall()}
            for row in migrated.values():
                if isinstance(row["tool_params"], str):
                    row["tool_params"] = json.loads(row["tool_params"])

            self.assertEqual("ronghui_default", migrated["arrive_list_0830"]["tool_params"]["account_id"])
            self.assertNotIn("target_date", migrated["arrive_list_0830"]["tool_params"])
            self.assertNotIn("login_site_code", migrated["arrive_list_0830"]["tool_params"])
            self.assertTrue(migrated["arrive_list_0830"]["enabled"])
            self.assertEqual("configured-account", migrated["arrive_list_0930"]["tool_params"]["account_id"])
            self.assertTrue(migrated["arrive_list_0930"]["tool_params"]["keep_me"])
            self.assertFalse(migrated["arrive_list_0930"]["enabled"])
            self.assertFalse(migrated["arrival_stats_1000"]["enabled"])
            self.assertIn("参数不符合", migrated["arrival_stats_1000"]["last_message"])

            daily = migrated["daily_sign_1100"]["tool_params"]
            self.assertEqual(3, daily["days"])
            self.assertTrue(daily["keep_me"])
            self.assertEqual("r13_default", daily["r13_account_id"])
            self.assertFalse(migrated["daily_sign_1100"]["enabled"])

            for task_id in ("delivery_status_1030", "send_order_2150", "site_send_1900"):
                self.assertEqual(
                    {"account_id": "ronghui_default"},
                    migrated[task_id]["tool_params"],
                )
                self.assertTrue(migrated[task_id]["enabled"])

            self.assertEqual(
                {"account_id": "yunda_default", "dest_brch": "56739382"},
                migrated["yunda_dispatch_forecast_1700"]["tool_params"],
            )
            self.assertTrue(migrated["yunda_dispatch_forecast_1700"]["enabled"])
            self.assertEqual(
                {"account_id": "yunda_default", "ensure_fields": True},
                migrated["yunda_send_waybills_2100"]["tool_params"],
            )
            self.assertTrue(migrated["yunda_send_waybills_2100"]["enabled"])
            self.assertEqual(
                {"mode": "sync", "platform": "ronghui", "rescan_days": 7},
                migrated["finance_bills_0010"]["tool_params"],
            )
            self.assertTrue(migrated["finance_bills_0010"]["enabled"])

            clock = migrated["clockin_daxiang_s_1830"]
            self.assertEqual("clock_in_dual", clock["tool_name"])
            self.assertEqual("configured-site", clock["tool_params"]["sitecode"])
            self.assertEqual("configured name", clock["tool_params"]["sitename"])
            self.assertEqual("keep", clock["tool_params"]["administrator_note"])
            self.assertEqual(600, clock["tool_params"]["timeout_sec"])
            self.assertTrue(clock["enabled"])

            cursor.execute(
                "SELECT COUNT(*) AS count FROM control_plane_task_cutover_backup_014 "
                "WHERE id IN ('arrive_list_0830', 'arrive_list_0930', 'arrival_stats_1000', "
                "'daily_sign_1100', 'delivery_status_1030', 'send_order_2150', "
                "'site_send_1900', 'yunda_dispatch_forecast_1700', "
                "'yunda_send_waybills_2100', 'finance_bills_0010', "
                "'clockin_daxiang_s_1830')"
            )
            self.assertEqual(11, cursor.fetchone()["count"])
