"""Isolated real-MySQL fixtures shared by migration and plugin persistence tests.

Each fixture owns one throwaway database on the explicit loopback test server
and migrates it with the reviewed deployment runner.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
import re
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from shared.automation_plugin_repository import AutomationPluginRepository
from tests.mysql_database_cleanup import drop_test_database


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8",
)
# schema_migrations.applied_at is a database-local DATETIME without fractions.
RELEASE_CUTOFF = datetime(2026, 9, 7, 13, 7, 53)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def migration_helper(name: str, database: str):
    import pymysql
    from tests import test_mysql_orchestration_integration as support

    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    helper = type(name, (support.MySqlOrchestrationIntegrationTests,), {})
    helper.pymysql, helper.runner = pymysql, support._load_migration_runner()
    helper.host, helper.port = os.environ["AGENT_DB_HOST"], int(os.environ["AGENT_DB_PORT"])
    helper.user, helper.password = os.environ["AGENT_DB_USER"], os.environ["AGENT_DB_PASS"]
    helper.database = database
    return helper


class IsolatedProjectDatabase:
    """A migrated database holding one committed Service V2 project."""

    def __init__(self, helper):
        self.helper = helper
        self.repository = helper._repository()
        self.project_id = "isolated_legacy_scope"
        self.plugin_id = "isolated_legacy_scope_plugin"
        with helper._connection() as connection:
            repository = AutomationPluginRepository(connection)
            digest = _digest({"synthetic": "migration identity fixture"})
            hashes = {
                field: digest for field in (
                    "package_sha256", "manifest_sha256", "tool_contract_sha256", "config_schema_sha256",
                    "allowed_entrypoints_sha256", "invocation_contracts_sha256", "worker_requirement_sha256",
                    "runtime_sha256", "scheduling_sha256", "install_root_metadata_sha256",
                )
            }
            repository.register_package_version(
                package={"plugin_id": self.plugin_id, "display_name": "isolated", "description": "migration fixture"},
                version={**hashes, "version": "1.0.0", "manifest_json": {"runtime": {"kind": "python_subprocess"}},
                    "trust_source": "super_admin_upload", "install_root_metadata_json": {},
                    "runtime_model": "SERVICE_V2", "plugin_api": "2.0.0",
                    "installed_by_actor_id": "isolated-admin", "project_full_auto_allowed": False},
            )
            repository.install_project_instance({
                "automation_id": self.project_id, "plugin_id": self.plugin_id, "plugin_version": "1.0.0",
                "display_name": "isolated", "install_request_id": str(uuid4()), "install_payload_sha256": digest,
                "installed_by_actor_id": "isolated-admin", "migration_authority": False,
            })
            columns = ["package_sha256", "manifest_sha256", "project_config_sha256", "account_bindings_sha256",
                "resource_bindings_sha256", "device_binding_sha256", "schedule_sha256", "core_registry_sha256",
                "tool_contract_sha256", "invocation_contracts_sha256", "compiled_invocations_sha256",
                "runtime_descriptor_sha256", "governance_anchor_sha256", "policy_contract_sha256",
                "enabled_entrypoints_sha256", "snapshot_sha256"]
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO automation_project_generations(automation_id,generation,request_id,state,plugin_id,"
                    "plugin_version,trust_source,runtime_model,plugin_api,snapshot_json," + ",".join(columns) + ") "
                    "VALUES(%s,1,%s,'COMMITTED',%s,'1.0.0','super_admin_upload','SERVICE_V2','2.0.0','{}',"
                    + ",".join(["%s"] * len(columns)) + ")",
                    (self.project_id, str(uuid4()), self.plugin_id, *([digest] * len(columns))),
                )
                cursor.execute("UPDATE schema_migrations SET applied_at=%s WHERE version='041'", (RELEASE_CUTOFF,))
            connection.commit()

    def seed_legacy_run(self, *, run_status="CANCELLED", receipt_outcome="WRITE_OUTCOME_UNKNOWN",
                        lease_outcome="WRITE_OUTCOME_UNKNOWN", step_status="BLOCKED_DATA",
                        live_sibling_lease=False):
        """Insert one historical Command/Run/Step with its lease and write receipt.

        Only historical migration tests use this; current code never creates
        these rows.
        """

        command_id, work_item_id, run_id = (str(uuid4()) for _ in range(3))
        lease_id, step_id, request_id = (str(uuid4()) for _ in range(3))
        receipt_id = str(uuid5(NAMESPACE_URL, f"boyi:write-attempt:{lease_id}:{request_id}"))
        original_time = RELEASE_CUTOFF - timedelta(days=1)
        target = {"schema": 1, "automation_id": self.project_id, "operation": "projection.invoke",
            "action": "scan.snapshot.replace", "role_sha256": hashlib.sha256(b"account_id").hexdigest(),
            "binding_sha256": hashlib.sha256(b"isolated-account").hexdigest(),
            "request_sha256": hashlib.sha256(request_id.encode()).hexdigest(), "business_date_sha256": "",
            "batch_sha256": "", "run_sha256": "", "idempotency_key_sha256": "", "record_count": 0,
            "content_sha256": _digest({"records": []})}
        with self.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_commands (command_id, command_type, automation_id, automation_generation,
                       source, actor_type, actor_id, actor_roles_json, entity_refs_json, parameters_json,
                       automation_invocation_json, idempotency_key, correlation_id, status, requested_at)
                   VALUES (%s, 'automation.project.invoke', %s, 1, 'console', 'console_admin', 'isolated-admin',
                           '[]', '[]', '{}', '{}', %s, %s, 'ACCEPTED', %s)""",
                (command_id, self.project_id, f"legacy:{command_id}", str(uuid4()), original_time),
            )
            cursor.execute(
                """INSERT INTO work_items (work_item_id, command_id, type, title, status, priority, source, dedupe_key)
                   VALUES (%s, %s, 'automation_project', 'legacy', 'CANCELLED', 'NORMAL', 'console', %s)""",
                (work_item_id, command_id, f"legacy:{work_item_id}"),
            )
            cursor.execute(
                """INSERT INTO agent_runs (run_id, work_item_id, command_id, run_no, status, mode, planner_kind,
                       plan_schema_version, plan_json, plan_hash, correlation_id, next_attempt_at, created_at)
                   VALUES (%s, %s, %s, 1, %s, 'deterministic', 'deterministic', 1,
                           '{"schema_version":1,"steps":[]}', %s, %s, %s, %s)""",
                (run_id, work_item_id, command_id, run_status, "f" * 64, str(uuid4()), original_time, original_time),
            )
            cursor.execute("""INSERT INTO agent_run_steps(step_id,run_id,step_key,step_order,tool_name,tool_version,
                operation_type,risk_level,status,idempotency_key) VALUES(%s,%s,'original',1,'v32_local_workload',
                '1.0.0','INTERNAL_PROJECTION_WRITE','LOW',%s,%s)""", (step_id, run_id, step_status, step_id))
            cursor.execute("""INSERT INTO automation_project_generation_leases(lease_id,automation_id,generation,
                orchestration_run_id,lease_owner,runtime_metadata_json,runtime_metadata_sha256,outcome,acquired_at,expires_at)
                VALUES(%s,%s,1,%s,'isolated-fixture','{}',%s,%s,%s,%s)""",
                (lease_id, self.project_id, run_id, _digest({}), lease_outcome, original_time, original_time))
            if live_sibling_lease:
                cursor.execute("""INSERT INTO automation_project_generation_leases(lease_id,automation_id,generation,
                    orchestration_run_id,lease_owner,runtime_metadata_json,runtime_metadata_sha256,outcome,expires_at)
                    VALUES(%s,%s,1,%s,'isolated-active-sibling','{}',%s,'RUNNING',UTC_TIMESTAMP(6)+INTERVAL 1 DAY)""",
                    (str(uuid4()), self.project_id, run_id, _digest({})))
            cursor.execute("""INSERT INTO automation_write_attempt_receipts(receipt_id,automation_id,generation,
                lease_id,orchestration_run_id,step_id,request_id,operation,action,argument_sha256,target_ref_sha256,
                target_ref_json,outcome,evidence_sha256,created_at,updated_at)
                VALUES(%s,%s,1,%s,%s,%s,%s,'projection.invoke','scan.snapshot.replace',%s,%s,%s,%s,%s,%s,%s)""",
                (receipt_id, self.project_id, lease_id, run_id, step_id, request_id, target["content_sha256"],
                    _digest(target), json.dumps(target), receipt_outcome, _digest({"unresolved": "retained audit"}),
                    original_time, original_time))
            connection.commit()
        return {"receipt_id": receipt_id, "lease_id": lease_id, "run_id": run_id,
            "step_id": step_id, "command_id": command_id}


@pytest.fixture
def database():
    """A fresh, fully migrated database with one committed Service V2 project."""

    assert re.fullmatch(r"(?:test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)", os.environ["AGENT_DB_NAME"])
    helper = migration_helper("IsolatedProjectDatabase", "isolated_project_" + uuid4().hex + "_test")
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{helper.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    try:
        helper._run_migrations(helper.database)
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("AGENT_DB_NAME", helper.database)
            yield IsolatedProjectDatabase(helper)
    finally:
        with helper._server_connection() as connection, connection.cursor() as cursor:
            drop_test_database(cursor, helper.database)


@pytest.fixture(scope="module")
def repository():
    """A module-scoped migrated database exposed through the runtime repository."""

    helper = migration_helper("ReliabilityDatabase", "v32_reliability_test")
    with helper._server_connection() as connection, connection.cursor() as cursor:
        drop_test_database(cursor, helper.database)
        cursor.execute(f"CREATE DATABASE `{helper.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    helper._run_migrations(helper.database)
    result = helper._repository()
    result.validate_mysql8()
    # Runtime resource-store facades use the configured database directly.
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("AGENT_DB_NAME", helper.database)
        yield result
    with helper._server_connection() as connection, connection.cursor() as cursor:
        drop_test_database(cursor, helper.database)
