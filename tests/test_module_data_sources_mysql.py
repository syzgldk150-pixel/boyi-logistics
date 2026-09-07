"""Real MySQL source identity, publication and local query regression evidence."""

from __future__ import annotations

import os
from pathlib import Path
import re
from uuid import uuid4

import pytest

from shared.customer_service_repository import CustomerServiceRepository
from shared.data_sources import DataSourceError, DataSourceRepository, SourceIdentity


@pytest.fixture(scope="module")
def database():
    if os.getenv("RUN_MYSQL_INTEGRATION") != "1":
        pytest.skip("requires the explicit isolated MySQL integration environment")
    import pymysql
    from tests import test_mysql_orchestration_integration as support

    requested_name = os.environ["AGENT_DB_NAME"]
    if not re.fullmatch(r"[a-z0-9_]+_test", requested_name):
        pytest.fail("source integration tests require an explicitly test-scoped database")
    # Own a fresh database just like the existing integration bootstrap. The
    # CI service's pre-created database can use a different default collation;
    # reusing it with IF NOT EXISTS makes migration order affect this fixture.
    name = f"v32_sources_{uuid4().hex}_test"
    fixture = type("SourceDatabase", (support.MySqlOrchestrationIntegrationTests,), {})
    fixture.pymysql, fixture.runner = pymysql, support._load_migration_runner()
    fixture.host, fixture.port = os.environ["AGENT_DB_HOST"], int(os.environ["AGENT_DB_PORT"])
    fixture.user, fixture.password = os.environ["AGENT_DB_USER"], os.environ["AGENT_DB_PASS"]
    with fixture._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    try:
        fixture._run_migrations(name)
        fixture._run_migrations(name, check_only=True)
        yield fixture, name
    finally:
        with fixture._server_connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{name}`")


def identity(organization: str) -> SourceIdentity:
    return SourceIdentity(module="customer_service", provider="ronghui", organization_key=organization,
        dataset="customer_service.problems", contract_version="1", dedup_contract="provider-external-id-direction-v1")


def test_source_identity_continuation_history_and_manual_fields(database):
    fixture, name = database
    organization = "synthetic-site-" + uuid4().hex
    with fixture._connection(name) as connection:
        sources = DataSourceRepository(connection)
        source = sources.register_source(identity(organization), display_name="合成客服来源",
            producer_instance_id="synthetic-old", producer_generation=1, request_id=uuid4().hex)
        source_id = source["source_id"]
        with pytest.raises(DataSourceError, match="SOURCE_PRODUCER_PROVENANCE_UNVERIFIED"):
            sources.producer_snapshot("synthetic-old", 1)
        sources.bind_account_alias(source_id, provider="ronghui", account_id="synthetic-a-" + organization)
        sources.bind_account_alias(source_id, provider="ronghui", account_id="synthetic-b-" + organization)
        assert len([row for row in sources.list_sources("customer_service") if row["organization_key"] == organization]) == 1
        with pytest.raises(DataSourceError, match="EXPLICIT_CONTINUATION"):
            sources.register_source(identity(organization), display_name="同一真实组织另一凭据",
                producer_instance_id="synthetic-new", producer_generation=1, request_id=uuid4().hex)
        other = sources.register_source(identity(organization + "-different-site"), display_name="合成客服来源",
            producer_instance_id="synthetic-other-site", producer_generation=1, request_id=uuid4().hex)
        assert other["display_name"] == source["display_name"] and other["source_id"] != source_id
        with pytest.raises(DataSourceError, match="SOURCE_IDENTITY_OR_CONTRACT_MISMATCH"):
            sources.switch_producer(source_id, expected_revision=1, expected_producer_instance_id="synthetic-old",
                producer_instance_id="synthetic-other-site", producer_generation=1,
                identity=identity(organization + "-different-site"), request_id=uuid4().hex)
        unchanged = sources.get(source_id)
        assert unchanged["revision"] == 1 and unchanged["producer_instance_id"] == "synthetic-old"
        repository = CustomerServiceRepository(connection)
        record = {"platform": "ronghui", "external_id": "synthetic-GUID", "source_direction": "received",
            "waybill_no": "SYNTHETIC-001", "status": "待处理", "problem_text": "隔离问题件",
            "updated_at": "2026-09-07 12:00:00", "resolved": False}
        proof = repository.publish(source_id=source_id, producer_instance_id="synthetic-old", producer_generation=1,
            source_revision=1, run_id="synthetic-run-1", records=[record], pagination_complete=True)
        assert proof["record_count"] == 1
        assert repository.publish(source_id=source_id, producer_instance_id="synthetic-old", producer_generation=1,
            source_revision=1, run_id="synthetic-run-1", records=[record], pagination_complete=True)["publication_id"] == proof["publication_id"]
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO customer_problem_manual_fields
                (source_id,external_id,source_direction,note,revision,updated_at)
                VALUES(%s,'synthetic-GUID','received','人工备注保留',1,UTC_TIMESTAMP(6))""", (source_id,))
        with pytest.raises(DataSourceError, match="INCOMPLETE"):
            repository.publish(source_id=source_id, producer_instance_id="synthetic-old", producer_generation=1,
                source_revision=1, run_id="synthetic-incomplete", records=[], pagination_complete=False)
        source = sources.switch_producer(source_id, expected_revision=1, expected_producer_instance_id="synthetic-old",
            producer_instance_id="synthetic-new", producer_generation=2, identity=identity(organization), request_id="synthetic-switch")
        with pytest.raises(DataSourceError, match="PRODUCER_STALE"):
            repository.publish(source_id=source_id, producer_instance_id="synthetic-old", producer_generation=1,
                source_revision=1, run_id="synthetic-late", records=[record], pagination_complete=True)
        repository.publish(source_id=source_id, producer_instance_id="synthetic-new", producer_generation=2,
            source_revision=source["revision"], run_id="synthetic-run-2", records=[{**record, "status": "已关闭", "resolved": True}], pagination_complete=True)
        sources.retire_producer("synthetic-new")
        result = repository.query(source_ids=[source_id])
        assert result["stats"] == {"row_count": 1, "open_count": 0, "resolved_count": 1}
        assert result["rows"][0]["note"] == "人工备注保留"
        assert result["rows"][0]["source_state"] == "history_only"
        assert sources.get(source_id)["latest_publication_id"]
        connection.rollback()


@pytest.mark.parametrize("migration_name", ["039_module_data_sources.sql", "040_source_producer_provenance.sql"])
def test_migration_reentry_is_safe(database, migration_name):
    fixture, name = database
    migration = Path(__file__).resolve().parents[1] / "agent" / "migrations" / migration_name
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM module_data_sources")
        before = cursor.fetchone()["n"]
        for statement in fixture.runner.split_sql_statements(migration.read_text(encoding="utf-8")):
            cursor.execute(statement)
        cursor.execute("SELECT COUNT(*) AS n FROM module_data_sources")
        assert cursor.fetchone()["n"] == before
        cursor.execute("""SELECT TABLE_NAME,IS_NULLABLE FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE() AND COLUMN_NAME='producer_snapshot_json'
            AND TABLE_NAME IN ('finance_source_run_bindings','customer_problem_publications')""")
        assert {row["TABLE_NAME"]: row["IS_NULLABLE"] for row in cursor.fetchall()} == {
            "finance_source_run_bindings": "YES", "customer_problem_publications": "YES"}


@pytest.mark.parametrize("run_status", [None, "RECEIVED", "PLANNED", "RUNNING", "VERIFYING",
    "WAITING_APPROVAL", "FAILED_RETRYABLE", "COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"])
def test_uninstall_uses_actual_run_state_after_command_acceptance(database, run_status):
    from shared.automation_plugin_repository import AutomationPluginRepository

    fixture, name = database
    producer, command_id, item_id, run_id = (str(uuid4()) for _ in range(4))
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        cursor.execute("""INSERT INTO agent_commands(command_id,command_type,source,actor_type,actor_roles_json,
            entity_refs_json,parameters_json,idempotency_key,correlation_id,requested_at,automation_id,status)
            VALUES(%s,'automation.run','console','console_admin','[]','[]','{}',%s,%s,UTC_TIMESTAMP(6),%s,'RECEIVED')""",
            (command_id, command_id, command_id, producer))
        if run_status:
            cursor.execute("""INSERT INTO work_items(work_item_id,command_id,type,title,source,dedupe_key)
                VALUES(%s,%s,'AUTOMATION','synthetic lifecycle boundary','console',%s)""", (item_id, command_id, item_id))
            cursor.execute("""INSERT INTO agent_runs(run_id,work_item_id,command_id,run_no,status,mode,planner_kind,correlation_id)
                VALUES(%s,%s,%s,1,%s,'AUTO','deterministic',%s)""", (run_id, item_id, command_id, run_status, command_id))
        blocks = AutomationPluginRepository(connection).list_execution_blocks(producer)
        if run_status in {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"}:
            assert blocks == []
        else:
            assert len(blocks) == 1
            assert blocks[0]["kind"] == ("VERIFYING" if run_status == "VERIFYING" else "RUNNING")
            assert blocks[0]["message"] == "control-plane run blocks plugin uninstall"
        connection.rollback()


def test_recent_failed_collection_keeps_published_history(database):
    import json
    fixture, name = database
    producer = "synthetic-status-" + uuid4().hex
    command_id, item_id, run_id = (str(uuid4()) for _ in range(3))
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        source = DataSourceRepository(connection).register_source(identity(producer), display_name="合成采集失败仍可查",
            producer_instance_id=producer, producer_generation=1, request_id=uuid4().hex)
        CustomerServiceRepository(connection).publish(source_id=source["source_id"], producer_instance_id=producer,
            producer_generation=1, source_revision=1, run_id="synthetic-published-before-failure",
            records=[{"platform": "ronghui", "source_direction": "received", "external_id": "synthetic-history",
                "problem_text": "上次已发布历史", "resolved": False}], pagination_complete=True)
        cursor.execute("""INSERT INTO agent_commands(command_id,command_type,source,actor_type,actor_roles_json,
            entity_refs_json,parameters_json,idempotency_key,correlation_id,requested_at,automation_id)
            VALUES(%s,'automation.run','console','console_admin','[]','[]','{}',%s,%s,UTC_TIMESTAMP(6),%s)""",
            (command_id, command_id, command_id, producer))
        cursor.execute("""INSERT INTO work_items(work_item_id,command_id,type,title,source,dedupe_key)
            VALUES(%s,%s,'AUTOMATION','synthetic failed collection','console',%s)""", (item_id, command_id, item_id))
        cursor.execute("""INSERT INTO agent_runs(run_id,work_item_id,command_id,run_no,status,mode,planner_kind,
            correlation_id,error_code,error_summary) VALUES(%s,%s,%s,1,'FAILED_TERMINAL','AUTO','deterministic',%s,
            'AUTH_REQUIRED','synthetic account needs authentication')""", (run_id, item_id, command_id, command_id))
        result = CustomerServiceRepository(connection).query(source_ids=[source["source_id"]])
        assert result["ok"] is True and len(result["rows"]) == 1
        assert result["rows"][0]["problem_text"] == "上次已发布历史"
        assert result["errors"][0]["error_code"] == "AUTH_REQUIRED"
        assert result["source_statuses"][0]["collection_status"] == "FAILED_TERMINAL"
        json.dumps(result)
        connection.rollback()


def test_legacy_finance_import_is_incremental_and_reports_missing_identity(database):
    from shared.data_source_migration import import_legacy_finance_sources

    fixture, name = database
    prefix = "synthetic-import-" + uuid4().hex
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        def control_plane():
            result = {}
            for table, identity_column in (("automation_projects", "automation_id"),
                    ("automation_project_configs", "automation_id"), ("scheduled_tasks", "id"),
                    ("agent_commands", "command_id"), ("agent_runs", "run_id")):
                cursor.execute(f"SELECT * FROM {table} ORDER BY {identity_column}")
                result[table] = cursor.fetchall()
            return result
        previous_control_plane = control_plane()
        cursor.execute("""INSERT INTO finance_sync_batches
            (trigger_type,requested_start_date,requested_end_date,rescan_days,status,frozen_at,started_at,created_at,finished_at)
            VALUES('manual','2026-09-07','2026-09-07',1,'success',UTC_TIMESTAMP(),UTC_TIMESTAMP(),UTC_TIMESTAMP(),UTC_TIMESTAMP())""")
        batch = cursor.lastrowid
        for suffix, site in (("first", prefix), ("same-site", prefix), ("missing", "")):
            cursor.execute("""INSERT INTO finance_sync_runs
                (batch_id,platform,account_id,target_date,source_site_code,source_site_name,attempt_no,status,started_at,created_at)
                VALUES(%s,'ronghui',%s,'2026-09-07',%s,'合成已验证站点',1,'success',UTC_TIMESTAMP(),UTC_TIMESTAMP())""",
                (batch, prefix + suffix, site))
        dry = import_legacy_finance_sources(connection)
        assert dry["amounts_unchanged"] and dry["rows_unchanged"]
        assert any(row["account_id"] == prefix + "missing" and row["reason"] == "SOURCE_IDENTITY_MISSING" for row in dry["unresolved"])
        first = import_legacy_finance_sources(connection, apply=True)
        second = import_legacy_finance_sources(connection, apply=True)
        selected = [row for row in first["mapped"] if row["account_id"].startswith(prefix)]
        assert len(selected) == 2 and len({row["source_id"] for row in selected}) == 1
        assert first == second
        # This incremental source migration must not re-create or rewrite the
        # existing instance IDs, bound roles, schedules, enabled state or runs.
        assert control_plane() == previous_control_plane
        source = DataSourceRepository(connection).get(selected[0]["source_id"])
        assert source["status"] == "legacy_unassigned" and source["producer_instance_id"] is None
        claimed = DataSourceRepository(connection).register_source(SourceIdentity(module="finance", provider="ronghui",
            organization_key=prefix, dataset="finance.transactions", contract_version="1", dedup_contract="provider-guid-per-business-date-v1"),
            display_name="合成已验证站点", producer_instance_id=prefix, producer_generation=1, request_id=prefix)
        assert claimed["source_id"] == source["source_id"] and claimed["status"] == "active"
        connection.rollback()


def test_finance_repository_rolls_back_with_autocommit_factory(database):
    from shared.finance.repository import FinanceRepository

    fixture, name = database
    source_identity = identity("synthetic-transaction-" + uuid4().hex)
    from contextlib import contextmanager

    @contextmanager
    def factory():
        with fixture._connection(name) as connection:
            connection.autocommit(True)
            yield connection
    repository = FinanceRepository(factory)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with repository._connection() as connection:
            DataSourceRepository(connection).register_source(source_identity, display_name="隔离回滚",
                producer_instance_id="synthetic-rollback", producer_generation=1, request_id=uuid4().hex)
            raise RuntimeError("synthetic rollback")
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM module_data_sources WHERE identity_fingerprint=%s", (source_identity.fingerprint,))
        assert cursor.fetchone()["n"] == 0


def test_finance_publication_hides_unfinished_batch_and_deduplicates_site(database):
    from shared.finance.publication import LATEST_PUBLISHED_RUNS

    fixture, name = database
    organization = "synthetic-finance-" + uuid4().hex
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        def add(status, account, finalized):
            cursor.execute("""INSERT INTO finance_sync_batches
                (trigger_type,requested_start_date,requested_end_date,rescan_days,status,frozen_at,started_at,created_at,finished_at)
                VALUES('manual','2026-09-07','2026-09-07',1,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP(),UTC_TIMESTAMP(),%s)""", (status, finalized))
            batch = cursor.lastrowid
            cursor.execute("""INSERT INTO finance_sync_runs
                (batch_id,platform,account_id,target_date,source_site_code,attempt_no,status,started_at,created_at)
                VALUES(%s,'ronghui',%s,'2026-09-07',%s,1,'success',UTC_TIMESTAMP(),UTC_TIMESTAMP())""", (batch, account, organization))
            return batch, cursor.lastrowid
        _old_batch, old_run = add("success", "synthetic-original-account", "2026-09-07 10:00:00")
        next_batch, new_run = add("running", "synthetic-equivalent-account", None)
        cursor.execute("SELECT latest.latest_run_id FROM (" + LATEST_PUBLISHED_RUNS + ") latest JOIN finance_sync_runs r ON r.id=latest.latest_run_id WHERE r.source_site_code=%s", (organization,))
        assert [row["latest_run_id"] for row in cursor.fetchall()] == [old_run]
        cursor.execute("UPDATE finance_sync_batches SET status='partial_failed',finished_at=UTC_TIMESTAMP() WHERE id=%s", (next_batch,))
        cursor.execute("SELECT latest.latest_run_id FROM (" + LATEST_PUBLISHED_RUNS + ") latest JOIN finance_sync_runs r ON r.id=latest.latest_run_id WHERE r.source_site_code=%s", (organization,))
        assert [row["latest_run_id"] for row in cursor.fetchall()] == [new_run]
        connection.rollback()


@pytest.mark.parametrize("direction,required_policy,publish_site,notify_site,expected_count", [
    ("received", False, "合成站点甲", "合成站点乙", 1),
    ("both", False, "合成站点甲", "合成站点乙", 2),
    ("received", True, "邵阳操作场", "邵阳操作场", 1),
    ("received", True, "邵阳操作场", "长沙操作场", 0),
])
def test_host_observed_customer_identity_parser_publication_and_local_query(database,
        direction, required_policy, publish_site, notify_site, expected_count):
    from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
    from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
    from agent.automation_plugins.models import GenerationVerificationContext
    from agent.orchestration.customer_source_projection import publish_customer_collection
    from tests.first_party_action_payload_support import load_first_party_action

    fixture, name = database
    organization = "synthetic-host-source-" + uuid4().hex
    account = "synthetic-credential-" + uuid4().hex
    producer = "synthetic-producer-" + uuid4().hex
    calls = []
    empty_source = [False]
    def read_protocol(arguments):
        if arguments["action"] == "detail":
            return {"ok": True, "details": {"REVERSION": "合成详情已回复", "REVERSION_STATUS": "已回复"}}
        assert arguments["raw_source"] is True
        assert arguments["account_id"] == account
        if empty_source[0]:
            return {"ok": True, "source_site_code": organization, "rows": [],
                "stats": {"total": 0, "returned": 0, "total_authoritative": True}}
        return {"ok": True, "source_site_code": organization,
            "rows": [{"platform": "ronghui", "source_direction": arguments["direction"], "external_id": "synthetic-raw-guid", "site_policy_required": required_policy,
                "raw_fields": {"GUID": "synthetic-raw-guid", "BILL_CODE": "SYNTHETIC-WAYBILL",
                    "REGISTER_SITE": publish_site, "SEND_SITE": notify_site,
                    "PROBLEM_CAUSE": "隔离原始字段进入插件", "REVERSION_STATUS": "未回复", "REVERSION": "",
                    "REVERSION_DATE": "2026-09-07 10:00:00"}}],
            "stats": {"total": 1, "returned": 1, "total_authoritative": True}}
    handlers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
        describe_account=lambda value: {"account_id": value, "system": "ronghui", "session_profile": "synthetic-session"},
        customer_action=read_protocol), cursor_secret=b"synthetic-source-test-secret-32-bytes")
    def broker(operation, *, action, role, arguments):
        context = CoreBrokerInvocationContext(automation_id=producer, plugin_version="1.0.0",
            generation=1, tool_name="sync_customer_service_problems", operation=operation,
            action=action, role=role, account_ids=(account,), account_bindings={role: (account,)})
        response = handlers[(operation, action)](context, arguments)
        calls.append({"action": action, "result": response})
        return response
    result = load_first_party_action("sync_customer_service_problems").run_action({"direction": direction}, broker)
    assert result["status"] == "SUCCESS"
    assert result["data"]["records"][0]["problem_text"] == "隔离原始字段进入插件"
    verification = GenerationVerificationContext(automation_id=producer, generation=1,
        lease_id=str(uuid4()), account_ids=(account,), account_bindings_sha256="a" * 64,
        requires_write_verification=False, host_call_observations=tuple(calls))
    with fixture._connection(name) as connection:
        # This protocol slice intentionally has no Runner generation; the full
        # installed flow requires and verifies durable producer provenance.
        publish_customer_collection(connection, verification=verification, run_id="synthetic-host-run",
            records=result["data"]["records"], require_runtime_provenance=False)
        sources = DataSourceRepository(connection)
        found = [row for row in sources.list_sources("customer_service") if row["organization_key"] == organization]
        assert len(found) == 1
        source_id = found[0]["source_id"]
        query = CustomerServiceRepository(connection).query(source_ids=[source_id])
        import json
        json.dumps(query)  # The actual HTTP response contract must be closed JSON.
        rows = query["rows"]
        assert len(rows) == expected_count
        assert all(row["problem_text"] == "隔离原始字段进入插件" and row["account_id"] == account for row in rows)
        if direction == "both":
            assert {row["source_direction"] for row in rows} == {"received", "registered"}
        if expected_count and direction == "received":
            from dataclasses import replace
            from agent.orchestration.pilot_projection import _opaque_problem_identity

            with connection.cursor() as cursor:
                cursor.execute("""INSERT INTO customer_problem_manual_fields(source_id,external_id,source_direction,note,revision,updated_at)
                    VALUES(%s,'synthetic-raw-guid','received','详情关闭保留人工备注',1,UTC_TIMESTAMP(6))""", (source_id,))
            empty_source[0] = True
            calls.clear()
            detail_result = load_first_party_action("sync_customer_service_problems").run_action(
                {"direction": direction, "recheck_items": result["data"]["records"]}, broker)
            assert detail_result["status"] == "SUCCESS" and detail_result["data"]["records"] == []
            detail_verification = replace(verification, host_call_observations=tuple(calls))
            checks = [{**check, "account_id": _opaque_problem_identity(check, detail_verification)["account_id"]}
                for check in detail_result["data"]["rechecks"]]
            assert checks[0]["status"] == "RESOLVED"
            publish_customer_collection(connection, verification=detail_verification, run_id="synthetic-detail-run",
                records=[], rechecks=checks, require_runtime_provenance=False)
            retained = CustomerServiceRepository(connection).query(source_ids=[source_id])["rows"]
            assert retained[0]["resolved"] is True and retained[0]["note"] == "详情关闭保留人工备注"
        upgraded = sources.register_source(identity(organization), display_name=organization,
            producer_instance_id=producer, producer_generation=2, request_id="synthetic-upgrade")
        assert upgraded["source_id"] == source_id and upgraded["revision"] == 2
        with pytest.raises(DataSourceError, match="STALE"):
            publish_customer_collection(connection, verification=verification, run_id="synthetic-late-run", records=result["data"]["records"])
        assert sources.verify_aliases_source(source_id, account_ids=[account]).organization_key == organization
        with pytest.raises(DataSourceError, match="UNVERIFIED"):
            sources.verify_aliases_source(source_id, account_ids=["synthetic-unknown"])
        connection.rollback()


@pytest.mark.parametrize("switch_round", range(20))
def test_management_adapter_continues_verified_source_atomically(database, switch_round):
    import hashlib
    import json

    from agent.automation_plugins.mysql_repository import MySQLAutomationPluginRepositoryAdapter
    from shared.automation_plugin_repository import AutomationPluginRepository

    fixture, name = database
    suffix = f"{switch_round:02d}" + uuid4().hex
    account, old, target = "synthetic-account-" + suffix, "synthetic-old-" + suffix, "synthetic-target-" + suffix
    plugin_id = "synthetic_collector_" + suffix
    metadata = {"management": {"purpose": "collector", "module": "customer_service",
        "dataset": "customer_service.problems", "version": "1"}}
    manifest = json.dumps(metadata, sort_keys=True)
    digest = hashlib.sha256(manifest.encode()).hexdigest()
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO automation_plugin_packages(plugin_id,display_name,description,latest_version) VALUES(%s,'合成接续采集器','isolated fixture','1.0.0')", (plugin_id,))
        hash_columns = ["package_sha256", "manifest_sha256", "tool_contract_sha256", "config_schema_sha256",
            "allowed_entrypoints_sha256", "invocation_contracts_sha256", "worker_requirement_sha256",
            "runtime_sha256", "scheduling_sha256", "install_root_metadata_sha256"]
        columns = ["plugin_id", "version", "manifest_json", "trust_source", "install_root_metadata_json", "installed_by_actor_id", *hash_columns]
        cursor.execute("INSERT INTO automation_plugin_versions(" + ",".join(columns) + ") VALUES(" + ",".join(["%s"] * len(columns)) + ")",
            (plugin_id, "1.0.0", manifest, "ed25519_first_party", "{}", "synthetic-admin", *([digest] * len(hash_columns))))
        cursor.execute("""INSERT INTO automation_projects(automation_id,plugin_id,plugin_version,display_name,
            enabled,state,install_request_id,install_payload_sha256,installed_by_actor_id,
            target_generation,committed_generation,reconcile_state)
            VALUES(%s,%s,'1.0.0','合成接续实例',0,'DISABLED',%s,%s,'synthetic-admin',2,2,'STABLE')""", (target, plugin_id, str(uuid4()), digest))
        AutomationPluginRepository(connection).initialize_project_config(target, enabled_entrypoints=[])
        bindings = json.dumps({"customer_service_source": [account]}, sort_keys=True)
        cursor.execute("UPDATE automation_project_configs SET account_bindings_json=%s,account_bindings_sha256=%s,configured=1 WHERE automation_id=%s", (bindings, hashlib.sha256(bindings.encode()).hexdigest(), target))
        sources = DataSourceRepository(connection)
        source = sources.register_source(identity("synthetic-organization-" + suffix), display_name="隔离来源",
            producer_instance_id=old, producer_generation=1, request_id=str(uuid4()))
        sources.bind_account_alias(source["source_id"], provider="ronghui", account_id=account)
        connection.commit()
    adapter = MySQLAutomationPluginRepositoryAdapter(fixture._repository(name))
    request = str(uuid4())
    switched = adapter.continue_data_source(source["source_id"], producer_instance_id=target,
        expected_revision=1, expected_producer_instance_id=old, request_id=request)
    assert switched["producer_instance_id"] == target and switched["producer_generation"] == 2
    assert adapter.continue_data_source(source["source_id"], producer_instance_id=target,
        expected_revision=1, expected_producer_instance_id=old, request_id=request)["revision"] == 2
    with fixture._connection(name) as connection:
        with pytest.raises(DataSourceError, match="STALE"):
            DataSourceRepository(connection).assert_producer(source["source_id"],
                producer_instance_id=old, producer_generation=1, revision=1)
