"""Migration 064 on a real MySQL holding retired plugin rows next to live ones.

The database is migrated to 063, then receives an ACTION_V1 project with its
old control-plane history, an old finance collector instance and the retired
R7 tool schedule, next to a current Service V2 project and its schedule. 064
must remove exactly the retired facts, export each removed row first, leave
every other row byte-identical and be safe to apply again.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from tests.test_legacy_unknown_scope_migration_mysql import _Database
from tests.test_service_v2_optional_console_upgrade_mysql import _compiled, _versions, _witness, _version

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8",
)
ACTOR = "isolated-console-admin"
DROPPED = {
    "automation_worker_job_messages", "automation_worker_cleanup_directives", "automation_worker_pairing_events",
    "automation_worker_jobs", "automation_worker_devices", "automation_plugin_migration_pair_events",
    "automation_plugin_migration_run_locks", "automation_plugin_migration_pairs",
    "automation_project_bootstrap_items_018", "automation_project_bootstrap_marker_018",
    "automation_project_migration_capture_018", "automation_project_resource_backup_018",
    "automation_project_reviewed_resource_map_018", "automation_project_reviewed_schedule_map_018",
    "scheduled_task_automation_identity_backup_018",
}


@pytest.fixture
def database(tmp_path):
    import pymysql
    from tests import test_mysql_orchestration_integration as support
    from tests.mysql_database_cleanup import drop_test_database

    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    assert re.fullmatch(r"[A-Za-z0-9_]+_test", os.environ["AGENT_DB_NAME"])
    helper = type("Migration064Database", (support.MySqlOrchestrationIntegrationTests,), {})
    helper.pymysql, helper.runner = pymysql, support._load_migration_runner()
    helper.host, helper.port = os.environ["AGENT_DB_HOST"], int(os.environ["AGENT_DB_PORT"])
    helper.user, helper.password = os.environ["AGENT_DB_USER"], os.environ["AGENT_DB_PASS"]
    helper.database = "migration_064_" + uuid4().hex + "_test"
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{helper.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    discover = helper.runner.discover_migrations
    try:
        with patch.object(helper.runner, "discover_migrations",
                          lambda: [item for item in discover() if item[0] < "064"]):
            helper._run_migrations(helper.database)
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("AGENT_DB_NAME", helper.database)
            environment.setenv("MIGRATION_BACKUP_DIRECTORY", str(tmp_path / "migration-backups"))
            yield _Database(helper)
    finally:
        with helper._server_connection() as connection, connection.cursor() as cursor:
            drop_test_database(cursor, helper.database)


def _install_v2(database, automation_id, plugin_id):
    """A committed Service V2 project of the given plugin, as an upgrade test installs it."""

    from agent.automation_plugins.mysql_repository import MySQLAutomationPluginRepositoryAdapter

    old, _ = _versions()
    payload = json.loads(json.dumps(old.manifest).replace(old.plugin_id, plugin_id))
    version = _version(payload, "4" if plugin_id == "sync_finance_bills_v2" else "5")
    adapter = MySQLAutomationPluginRepositoryAdapter(database.repository)
    sources = ["monthly_capture"]
    accounts = {role["role"]: "isolated-" + role["role"] for role in version.manifest["account_roles"]}
    with patch("tests.test_service_v2_optional_console_upgrade_mysql.PROJECT", automation_id):
        compiled = _compiled(version, {"page_size": 100}, accounts, sources)
    with database.repository.unit_of_work() as uow:
        adapter._register(uow.automation_plugins, version, actor_id=ACTOR)
        uow.automation_plugins.install_project_instance({
            "automation_id": automation_id, "plugin_id": plugin_id, "plugin_version": version.version,
            "display_name": automation_id, "install_request_id": str(uuid4()),
            "install_payload_sha256": "3" * 64, "installed_by_actor_id": ACTOR, "migration_authority": False,
        })
        uow.automation_plugins.initialize_project_config(automation_id, enabled_entrypoints=sources)
        uow.automation_projects.ensure_default(automation_id, mode="PROJECT_FULL_AUTO",
            project_generation=1, project_configuration_version=1)
        uow.automation_plugins.save_project_config(automation_id, config={"page_size": 100},
            account_bindings=accounts, resource_bindings={}, enabled_entrypoints=sources,
            schedule={"kind": "daily_times", "times": ["00:10"], "enabled": False},
            compiled_invocations=compiled, contract_witness=_witness(version),
            actor_id=ACTOR, actor_role="super_admin", request_id=str(uuid4()),
            expected_project_configuration_version=1)
        uow.commit()
    with database.helper._connection() as connection, connection.cursor() as cursor:
        hashes = ["package_sha256", "manifest_sha256", "project_config_sha256", "account_bindings_sha256",
            "resource_bindings_sha256", "device_binding_sha256", "schedule_sha256", "core_registry_sha256",
            "tool_contract_sha256", "invocation_contracts_sha256", "compiled_invocations_sha256",
            "runtime_descriptor_sha256", "governance_anchor_sha256", "policy_contract_sha256",
            "enabled_entrypoints_sha256", "snapshot_sha256"]
        cursor.execute("INSERT INTO automation_project_generations(automation_id,generation,request_id,state,"
            "plugin_id,plugin_version,trust_source,runtime_model,plugin_api,snapshot_json," + ",".join(hashes) + ") "
            "VALUES(%s,1,%s,'COMMITTED',%s,%s,'super_admin_upload','SERVICE_V2','2.0.0','{}'," +
            ",".join(["%s"] * len(hashes)) + ")", (automation_id, str(uuid4()), plugin_id, version.version,
                *(["a" * 64] * len(hashes))))
        cursor.execute("UPDATE automation_projects SET target_generation=1,committed_generation=1,"
            "reconcile_state='STABLE',state='DISABLED',enabled=FALSE WHERE automation_id=%s", (automation_id,))
        cursor.execute("INSERT INTO scheduled_tasks(id,automation_id,name,tool_name,cron_expression) "
            "VALUES(%s,%s,%s,%s,'10 0 * * *')", (automation_id + "_0010", automation_id, automation_id,
                f"automation.{automation_id}.run"))
        connection.commit()


def _retire_to_v1(database):
    """Give the fixture project the exact shape of a production ACTION_V1 project."""

    with database.helper._connection() as connection, connection.cursor() as cursor:
        for table in ("automation_plugin_versions", "automation_project_generations"):
            cursor.execute(f"UPDATE {table} SET runtime_model='ACTION_V1', plugin_api='1.0.0', "
                "trust_source='ed25519_first_party' WHERE plugin_id=%s", (database.plugin_id,))
        connection.commit()


def _seed(database):
    _retire_to_v1(database)
    run = database.seed()  # ACTION_V1 project with its Command/Run/Step, lease and unknown write.
    _install_v2(database, "old-finance-instance", "sync_finance_bills_v2")
    _install_v2(database, "live-statement-instance", "sync_ronghui_finance_statement_v2")
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO scheduled_tasks(id,automation_id,name,tool_name,cron_expression) VALUES"
            "('r7_arrival_checkin_0900','r7_arrival_checkin','R7','r7_arrival_checkin','0 9 * * *')")
        cursor.execute("INSERT INTO finance_sync_batches(id,trigger_type,requested_start_date,requested_end_date,"
            "status,frozen_at,started_at,created_at) VALUES(990001,'manual','2026-09-01','2026-09-30',"
            "'SUCCESS',NOW(),NOW(),NOW())")
        connection.commit()
    return run


def _tables(cursor):
    cursor.execute("SELECT TABLE_SCHEMA AS s, TABLE_NAME AS t FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA LIKE %s AND TABLE_TYPE='BASE TABLE' ORDER BY 1, 2",
        (cursor.connection.db.decode().removesuffix("_test") + "%",))
    return [(row["s"], row["t"]) for row in cursor.fetchall()]


def _snapshot(database):
    result = {}
    with database.helper._connection() as connection, connection.cursor() as cursor:
        for schema, table in _tables(cursor):
            cursor.execute(f"SELECT * FROM `{schema}`.`{table}`")
            result[(schema, table)] = sorted(json.dumps(row, sort_keys=True, default=str) for row in cursor.fetchall())
    return result


def _apply_064(database):
    with patch.dict(os.environ, database.helper._environment(database.helper.database), clear=False):
        database.helper.runner.run(check_only=False)


def _export_records(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_064_removes_only_retired_facts_exports_them_and_repeats(database, tmp_path):
    run = _seed(database)
    before = _snapshot(database)
    _apply_064(database)
    after = _snapshot(database)

    retired = {"isolated_legacy_scope", "old-finance-instance", "r7_arrival_checkin"}
    removed = {}
    for key, rows in before.items():
        kept = after.get(key)
        if key[1] in DROPPED:
            assert kept is None, key
            removed[key[1]] = len(rows)
            continue
        if key[1] == "schema_migrations":
            assert [json.loads(row)["version"] for row in set(kept) - set(rows)] == ["064"]
            continue
        assert set(kept) <= set(rows), key
        gone = [json.loads(row) for row in set(rows) - set(kept)]
        for row in gone:
            text = json.dumps(row, default=str)
            assert any(name in text for name in retired) or row.get("run_id") == run["run_id"] or \
                row.get("plugin_id") in {"isolated_legacy_scope_plugin", "sync_finance_bills_v2"} or \
                key[1] in {"domain_events", "outbox_events", "work_items", "work_item_entities"}, (key, row)
            assert "live-statement-instance" not in text, (key, row)
        if gone:
            removed[key[1]] = len(gone)
        if key[0].endswith(("_finance", "_waybill")):
            assert kept == rows, key
    for table in ("automation_projects", "automation_plugin_versions", "automation_plugin_packages",
                  "agent_commands", "agent_runs", "agent_run_steps", "automation_write_attempt_receipts",
                  "automation_project_generation_leases", "scheduled_tasks"):
        assert removed.get(table), table
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT automation_id FROM automation_projects")
        assert [row["automation_id"] for row in cursor.fetchall()] == ["live-statement-instance"]
        cursor.execute("SELECT id FROM scheduled_tasks")
        assert [row["id"] for row in cursor.fetchall()] == ["live-statement-instance_0010"]
        cursor.execute("SELECT plugin_id FROM automation_plugin_packages")
        assert [row["plugin_id"] for row in cursor.fetchall()] == ["sync_ronghui_finance_statement_v2"]

    exports = sorted((tmp_path / "migration-backups").glob("*-migration-064-removed-rows.jsonl"))
    assert len(exports) == 1 and oct(exports[0].stat().st_mode & 0o777) == "0o600"
    records = _export_records(exports[0])
    exported = {}
    for record in records:
        if "rows" in record:
            exported[record["table"]] = exported.get(record["table"], 0) + len(record["rows"])
    assert exported == {table: count for table, count in removed.items() if count}
    assert "committed_at" in records[-2] and records[-1]["removed_rows"] == exported

    # The retired models can no longer be written.
    with database.helper._connection() as connection, connection.cursor() as cursor:
        with pytest.raises(Exception, match="chk_automation_plugin_runtime_model|3819"):
            cursor.execute("UPDATE automation_plugin_versions SET runtime_model='ACTION_V1'")
        connection.rollback()
        with pytest.raises(Exception, match="chk_automation_plugin_trust_source|3819"):
            cursor.execute("UPDATE automation_plugin_versions SET trust_source='ed25519_first_party'")
        connection.rollback()

    # Applying the same file again changes nothing and removes no row.
    with database.helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
        path = next(path for version, path in database.helper.runner.discover_migrations() if version == "064")
        helper = database.helper.runner._load_script_helper("migration_064_retire_pre_v2.py")
        again = helper.apply(cursor, path, database.helper.runner.split_sql_statements, tmp_path / "again")
    assert _snapshot(database) == after
    assert _export_records(again)[-1]["removed_rows"] == {}


def test_064_refuses_while_a_retired_project_still_runs(database):
    _retire_to_v1(database)
    database.seed(live_sibling_lease=True)
    before = _snapshot(database)
    with pytest.raises(Exception, match="(?i)cp064_pre_v2_project_still_running"):
        _apply_064(database)
    assert _snapshot(database) == before
