"""Migration 066 on a real MySQL that still holds old control-plane rows.

The database is migrated to 065 and receives a historical Command/Run/Step
with its lease and write receipt plus a per-task scheduled approval policy.
066 must export every removed row and Run binding first, keep the leases,
receipts and scheduled tasks, leave a schema the runtime accepts, and be safe
to apply again.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from tests.mysql_isolated_fixtures import IsolatedProjectDatabase, migration_helper

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8",
)
RETIRED = {
    "agent_commands", "work_items", "work_item_entities", "agent_runs", "agent_run_steps",
    "approval_requests", "approval_decisions", "evidence_records", "external_entity_links",
    "domain_events", "outbox_events", "event_consumptions", "feishu_approval_deliveries",
    "scheduled_task_approval_policies", "scheduled_task_approval_policy_events",
    "automation_project_approval_batches", "control_plane_task_cutover_backup_014",
    "daily_sign_single_tms_backup_016", "scheduled_task_contract_upgrade_backup_017",
    "scheduled_task_contract_upgrade_created_017", "pending_run_timebase_snapshot_043",
}


@pytest.fixture
def database(tmp_path):
    from tests.mysql_database_cleanup import drop_test_database

    assert re.fullmatch(r"[A-Za-z0-9_]+_test", os.environ["AGENT_DB_NAME"])
    helper = migration_helper("Migration066Database", "migration_066_" + uuid4().hex + "_test")
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{helper.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    discover = helper.runner.discover_migrations
    try:
        with patch.object(helper.runner, "discover_migrations",
                          lambda: [item for item in discover() if item[0] < "066"]):
            helper._run_migrations(helper.database)
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("AGENT_DB_NAME", helper.database)
            environment.setenv("MIGRATION_BACKUP_DIRECTORY", str(tmp_path / "migration-backups"))
            yield IsolatedProjectDatabase(helper)
    finally:
        with helper._server_connection() as connection, connection.cursor() as cursor:
            drop_test_database(cursor, helper.database)


def _tables(database):
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
        return {row["TABLE_NAME"] for row in cursor.fetchall()}


def _columns(database, table):
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                       (table,))
        return {row["COLUMN_NAME"] for row in cursor.fetchall()}


def _apply(database):
    discover = database.helper.runner.discover_migrations
    with patch.dict(os.environ, database.helper._environment(database.helper.database), clear=False), \
            patch.object(database.helper.runner, "discover_migrations",
                         lambda: [item for item in discover() if item[0] <= "066"]):
        database.helper.runner.run(check_only=False)


def test_066_exports_and_retires_the_control_plane_and_repeats(database, tmp_path):
    seeded = database.seed_legacy_run(run_status="COMPLETED", receipt_outcome="WRITE_VERIFIED",
                                      lease_outcome="WRITE_VERIFIED", step_status="COMPLETED")
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("""INSERT INTO scheduled_tasks(id,automation_id,name,tool_name,cron_expression,enabled)
            VALUES('legacy-task',%s,'legacy','automation.legacy.run','0 9 * * *',FALSE)""", (database.project_id,))
        cursor.execute("INSERT INTO scheduled_task_approval_policies(task_id,mode) VALUES('legacy-task','REQUIRE_EACH_RUN')")
        connection.commit()
    assert RETIRED <= _tables(database)

    _apply(database)

    assert not RETIRED & _tables(database)
    assert "orchestration_run_id" not in _columns(database, "automation_project_generation_leases")
    assert "orchestration_run_id" not in _columns(database, "automation_write_attempt_receipts")
    assert not {c for c in _columns(database, "automation_project_generation_transition_tasks") if c.startswith("policy_")}
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM automation_project_generation_leases WHERE lease_id=%s", (seeded["lease_id"],))
        assert cursor.fetchone()["n"] == 1
        cursor.execute("SELECT COUNT(*) AS n FROM automation_write_attempt_receipts WHERE receipt_id=%s", (seeded["receipt_id"],))
        assert cursor.fetchone()["n"] == 1
        cursor.execute("SELECT COUNT(*) AS n FROM scheduled_tasks WHERE id='legacy-task'")
        assert cursor.fetchone()["n"] == 1
        cursor.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
        assert cursor.fetchone()["version"] == "066"
    database.repository.validate_schema()

    exports = sorted((tmp_path / "migration-backups").glob("*-migration-066-removed-rows.jsonl"))
    assert len(exports) == 1 and oct(exports[0].stat().st_mode & 0o777) == "0o600"
    records = [json.loads(line) for line in exports[0].read_text(encoding="utf-8").splitlines()]
    exported = {}
    for record in records:
        if "rows" in record:
            exported.setdefault(record["table"], []).extend(record["rows"])
    assert [row["run_id"] for row in exported["agent_runs"]] == [seeded["run_id"]]
    assert [row["command_id"] for row in exported["agent_commands"]] == [seeded["command_id"]]
    assert [row["task_id"] for row in exported["scheduled_task_approval_policies"]] == ["legacy-task"]
    assert [row["orchestration_run_id"] for row in exported["automation_project_generation_leases"]] == [seeded["run_id"]]
    assert [row["orchestration_run_id"] for row in exported["automation_write_attempt_receipts"]] == [seeded["run_id"]]
    assert records[-1]["removed_rows"]["agent_runs"] == 1

    # Applying the same file again changes nothing and removes no row.
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        path = next(path for version, path in database.helper.runner.discover_migrations() if version == "066")
        helper = database.helper.runner._load_script_helper("migration_row_export.py")
        again = helper.apply(cursor, path, database.helper.runner.split_sql_statements, tmp_path / "again")
    assert json.loads(Path(again).read_text(encoding="utf-8").splitlines()[-1])["removed_rows"] == {}
