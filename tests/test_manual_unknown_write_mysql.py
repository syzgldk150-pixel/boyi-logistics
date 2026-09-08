"""Exact historical verification against the real migration schema, never production."""

import os
import json
from uuid import uuid4

import pytest

from shared.automation_plugin_repository import AutomationPluginRepository
from shared.orchestration_repository import OrchestrationRepository
from shared.orchestration_repository_support import ConcurrentUpdateError, _json_hash, _json_param
from tests import test_mysql_orchestration_integration as mysql_helpers


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8",
)


@pytest.fixture(scope="module")
def database():
    import pymysql

    # Reuse the established real migration/explicit resource fixture, but only
    # create this test's fresh UUID database rather than the whole suite matrix.
    case = type("ManualRecoveryDatabase", (mysql_helpers.MySqlOrchestrationIntegrationTests,), {})
    case.pymysql = pymysql
    case.host = os.environ["AGENT_DB_HOST"]
    case.port = int(os.environ["AGENT_DB_PORT"])
    case.user = os.environ["AGENT_DB_USER"]
    case.password = os.environ["AGENT_DB_PASS"]
    case.database = f"test_manual_recovery_{uuid4().hex}"
    case.runner = mysql_helpers._load_migration_runner()
    with case._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{case.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    try:
        case._run_migrations(case.database)
        yield case
    finally:
        with case._server_connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{case.database}`")


def _connection(case):
    return case.pymysql.connect(
        host=case.host, port=case.port, user=case.user, password=case.password,
        database=case.database, charset="utf8mb4", cursorclass=case.pymysql.cursors.DictCursor,
    )


def _seed(case, *, current=2, generation_error="WRITE_OUTCOME_UNKNOWN", outcome="WRITE_VERIFIED", runtime_metadata=None):
    identity = {name: str(uuid4()) for name in ("automation_id", "plugin_id", "command_id", "work_item_id", "run_id", "step_id", "lease_id")}
    with case._connection() as connection:
        plugins = AutomationPluginRepository(connection, cursor_factory=case.pymysql.cursors.DictCursor)
        plugins.register_package_version(
            package={"plugin_id": identity["plugin_id"], "display_name": "manual verification", "description": "synthetic integration"},
            version={
                "version": "1.0.0", "package_sha256": "1" * 64, "manifest_sha256": "2" * 64,
                "manifest_json": {"runtime": {"kind": "python_subprocess"}},
                **{name: "3" * 64 for name in (
                    "tool_contract_sha256", "config_schema_sha256", "allowed_entrypoints_sha256",
                    "invocation_contracts_sha256", "worker_requirement_sha256", "runtime_sha256", "scheduling_sha256",
                )},
                "project_full_auto_allowed": False, "trust_source": "ed25519_first_party",
                "install_root_metadata_json": {}, "install_root_metadata_sha256": "4" * 64,
                "installed_by_actor_id": "integration-admin",
            },
        )
        plugins.install_project_instance({
            "automation_id": identity["automation_id"], "plugin_id": identity["plugin_id"], "plugin_version": "1.0.0",
            "display_name": "manual verification", "install_request_id": str(uuid4()),
            "install_payload_sha256": "5" * 64, "installed_by_actor_id": "integration-admin", "migration_authority": False,
        })
        with connection.cursor() as cursor:
            for generation in range(1, current + 1):
                cursor.execute(
                    """INSERT INTO automation_project_generations (
                        automation_id, generation, request_id, state, plugin_id, plugin_version,
                        package_sha256, manifest_sha256, trust_source, project_config_sha256,
                        account_bindings_sha256, resource_bindings_sha256, device_binding_sha256,
                        schedule_sha256, core_registry_sha256, tool_contract_sha256,
                        invocation_contracts_sha256, compiled_invocations_sha256,
                        runtime_descriptor_sha256, governance_anchor_sha256, policy_contract_sha256,
                        enabled_entrypoints_sha256, snapshot_json, snapshot_sha256, error_code
                    ) VALUES (%s, %s, %s, %s, %s, '1.0.0', %s, %s, 'ed25519_first_party',
                              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}', %s, %s)""",
                    (identity["automation_id"], generation, str(uuid4()), "BLOCKED" if generation == 1 else "COMMITTED",
                     identity["plugin_id"], "1" * 64, "2" * 64, *("6" * 64 for _ in range(14)),
                     generation_error if generation == 1 else None),
                )
            cursor.execute(
                "UPDATE automation_projects SET enabled=TRUE, state='ENABLED', target_generation=%s, committed_generation=%s, reconcile_state=%s WHERE automation_id=%s",
                (current, current, "STABLE" if current > 1 else "BLOCKED_UNKNOWN_WRITE", identity["automation_id"]),
            )
            cursor.execute(
                """INSERT INTO agent_commands (command_id, command_type, automation_id, automation_generation,
                    source, actor_type, actor_id, actor_roles_json, entity_refs_json, parameters_json,
                    automation_invocation_json, idempotency_key, correlation_id, status, requested_at)
                    VALUES (%s, 'automation.project.invoke', %s, 1, 'console', 'console_admin', 'integration-admin',
                            '[]', '[]', '{}', '{}', %s, %s, 'ACCEPTED', NOW(6))""",
                (identity["command_id"], identity["automation_id"], str(uuid4()), str(uuid4())),
            )
            cursor.execute(
                "INSERT INTO work_items (work_item_id, command_id, type, title, status, priority, source, dedupe_key) VALUES (%s, %s, 'automation_project', 'manual verification', 'CANCELLED', 'NORMAL', 'console', %s)",
                (identity["work_item_id"], identity["command_id"], str(uuid4())),
            )
            cursor.execute(
                """INSERT INTO agent_runs (run_id, work_item_id, command_id, run_no, status, mode, planner_kind,
                    plan_schema_version, plan_json, plan_hash, correlation_id, next_attempt_at)
                    VALUES (%s, %s, %s, 1, 'CANCELLED', 'deterministic', 'deterministic', 1,
                            '{"schema_version":1,"steps":[]}', %s, %s, NOW(6))""",
                (identity["run_id"], identity["work_item_id"], identity["command_id"], "7" * 64, str(uuid4())),
            )
            cursor.execute(
                """INSERT INTO agent_run_steps (step_id, run_id, step_key, step_order, tool_name, tool_version,
                    operation_type, risk_level, status, retry_safe, idempotency_key)
                    VALUES (%s, %s, 'write', 1, 'integration.write', '1.0.0', 'EXTERNAL_WRITE', 'HIGH', 'CANCELLED', FALSE, %s)""",
                (identity["step_id"], identity["run_id"], str(uuid4())),
            )
            cursor.execute(
                """INSERT INTO automation_project_generation_leases (lease_id, automation_id, generation,
                    orchestration_run_id, lease_owner, runtime_metadata_json, runtime_metadata_sha256, outcome, expires_at)
                    VALUES (%s, %s, 1, %s, 'integration', %s, %s, 'WRITE_OUTCOME_UNKNOWN', DATE_SUB(NOW(6), INTERVAL 1 DAY))""",
                (identity["lease_id"], identity["automation_id"], identity["run_id"],
                 _json_param(runtime_metadata, {}), _json_hash(runtime_metadata) if runtime_metadata is not None else "8" * 64),
            )
            cursor.execute(
                """INSERT INTO automation_write_attempt_receipts (receipt_id, automation_id, generation, lease_id,
                    orchestration_run_id, step_id, request_id, operation, action, argument_sha256,
                    target_ref_sha256, target_ref_json, outcome, evidence_sha256, legacy_scope_quarantined_at, created_at, updated_at)
                    VALUES (%s, %s, 1, %s, %s, %s, %s, 'write', 'sync', %s, %s, '{}', %s, %s, NOW(6), NOW(6), NOW(6))""",
                (str(uuid4()), identity["automation_id"], identity["lease_id"], identity["run_id"], identity["step_id"],
                 str(uuid4()), "9" * 64, "a" * 64, outcome, "b" * 64 if outcome != "WRITE_OUTCOME_UNKNOWN" else None),
            )
        connection.commit()
    return identity


def _state(case, identity):
    with case._connection() as connection, connection.cursor() as cursor:
        result = {}
        for key, table, field in (
            ("project", "automation_projects", "automation_id"), ("run", "agent_runs", "run_id"),
            ("step", "agent_run_steps", "step_id"), ("item", "work_items", "work_item_id"),
            ("lease", "automation_project_generation_leases", "lease_id"),
        ):
            cursor.execute(f"SELECT * FROM {table} WHERE {field}=%s", (identity[field],))
            result[key] = cursor.fetchone()
        cursor.execute("SELECT state, error_code FROM automation_project_generations WHERE automation_id=%s AND generation=1", (identity["automation_id"],))
        result["generation"] = cursor.fetchone()
        cursor.execute("SELECT consumer_name FROM outbox_events WHERE partition_key=%s", (identity["work_item_id"],))
        result["consumers"] = [row["consumer_name"] for row in cursor.fetchall()]
    return result


@pytest.mark.parametrize("outcome", ["WRITE_VERIFIED", "NOT_APPLIED", "WRITE_OUTCOME_UNKNOWN"])
def test_cancelled_history_is_visible_and_only_authoritative_selected_lease_is_settled(database, outcome):
    identity = _seed(database, outcome=outcome)
    repository = OrchestrationRepository(lambda: _connection(database), database.pymysql.cursors.DictCursor)
    before = _state(database, identity)
    rows = repository.list_work_item_unknown_writes(identity["work_item_id"])
    assert len(rows) == 1 and rows[0]["identity_valid"] and rows[0]["legacy_scope_unavailable"]
    assert rows[0]["lease_id"] == identity["lease_id"] and rows[0]["generation"] == 1
    assert repository.list_work_item_unknown_writes(str(uuid4())) == []
    request = dict(
            automation_id=identity["automation_id"], generation=1, lease_id=identity["lease_id"],
            request_id=str(uuid4()), actor_id="integration-admin", actor_role="super_admin", resume_run=False,
            expected_run_id=identity["run_id"], expected_work_item_id=identity["work_item_id"],
    )
    with repository.unit_of_work() as uow:
        result = uow.recover_unknown_automation_write(**request)
        uow.commit()
    after = _state(database, identity)
    assert all(after[key] == before[key] for key in ("project", "run", "step", "item"))
    if outcome == "WRITE_OUTCOME_UNKNOWN":
        assert result["recovery_status"] == "UNKNOWN" and after == before
    else:
        assert result["recovery_status"] == ("APPLIED" if outcome == "WRITE_VERIFIED" else "NOT_APPLIED")
        assert after["generation"] == {"state": "DRAINING", "error_code": None}
        assert after["consumers"] == ["orchestration.audit"]
        assert repository.list_work_item_unknown_writes(identity["work_item_id"]) == []
        with repository.unit_of_work() as uow:
            assert uow.recover_unknown_automation_write(**request)["idempotent"] is True
            uow.commit()
        assert _state(database, identity) == after


def test_historical_settlement_preserves_unrelated_generation_error(database):
    identity = _seed(database, generation_error="DISPOSAL_FAILED")
    with database._connection() as connection:
        plugins = AutomationPluginRepository(connection, cursor_factory=database.pymysql.cursors.DictCursor)
        result = plugins.settle_unknown_write_recovery_row(
            automation_id=identity["automation_id"], generation=1, lease_id=identity["lease_id"],
            recovery_status="APPLIED", evidence_sha256="b" * 64, allow_historical=True,
        )
        connection.commit()
    assert result["transitioned"]
    assert _state(database, identity)["generation"] == {"state": "BLOCKED", "error_code": "DISPOSAL_FAILED"}


def test_multiple_historical_leases_are_selected_separately_before_archive_disposal(database):
    identity = _seed(database)
    second_lease = str(uuid4())
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO automation_project_generation_leases (lease_id, automation_id, generation,
                orchestration_run_id, lease_owner, runtime_metadata_json, runtime_metadata_sha256, outcome, expires_at)
                SELECT %s, automation_id, generation, orchestration_run_id, lease_owner,
                       runtime_metadata_json, runtime_metadata_sha256, outcome, expires_at
                FROM automation_project_generation_leases WHERE lease_id=%s""",
            (second_lease, identity["lease_id"]),
        )
        cursor.execute(
            """INSERT INTO automation_write_attempt_receipts (receipt_id, automation_id, generation, lease_id,
                orchestration_run_id, step_id, request_id, operation, action, argument_sha256,
                target_ref_sha256, target_ref_json, outcome, evidence_sha256, legacy_scope_quarantined_at, created_at, updated_at)
                SELECT %s, automation_id, generation, %s, orchestration_run_id, step_id, %s,
                       operation, action, argument_sha256, target_ref_sha256, target_ref_json,
                       outcome, evidence_sha256, legacy_scope_quarantined_at, created_at, updated_at
                FROM automation_write_attempt_receipts WHERE lease_id=%s""",
            (str(uuid4()), second_lease, str(uuid4()), identity["lease_id"]),
        )
        connection.commit()
    repository = OrchestrationRepository(lambda: _connection(database), database.pymysql.cursors.DictCursor)
    before = _state(database, identity)
    assert {row["lease_id"] for row in repository.list_work_item_unknown_writes(identity["work_item_id"])} == {identity["lease_id"], second_lease}
    for lease_id, expected_state in ((identity["lease_id"], "BLOCKED"), (second_lease, "DRAINING")):
        with repository.unit_of_work() as uow:
            assert uow.recover_unknown_automation_write(
                automation_id=identity["automation_id"], generation=1, lease_id=lease_id,
                request_id=str(uuid4()), actor_id="integration-admin", actor_role="super_admin", resume_run=False,
                expected_run_id=identity["run_id"], expected_work_item_id=identity["work_item_id"],
            )["recovery_status"] == "APPLIED"
            uow.commit()
        after = _state(database, identity)
        assert after["generation"]["state"] == expected_state
        assert all(after[key] == before[key] for key in ("project", "run", "step", "item"))
        rows = repository.list_work_item_unknown_writes(identity["work_item_id"])
        assert [row["lease_id"] for row in rows] == ([second_lease] if expected_state == "BLOCKED" else [])


def test_default_settlement_rejects_history_and_still_recovers_current_generation(database):
    old = _seed(database)
    with database._connection() as connection:
        plugins = AutomationPluginRepository(connection, cursor_factory=database.pymysql.cursors.DictCursor)
        with pytest.raises(ConcurrentUpdateError, match="current committed target"):
            plugins.settle_unknown_write_recovery_row(
                automation_id=old["automation_id"], generation=1, lease_id=old["lease_id"],
                recovery_status="APPLIED", evidence_sha256="b" * 64,
            )
    current = _seed(database, current=1)
    with database._connection() as connection:
        plugins = AutomationPluginRepository(connection, cursor_factory=database.pymysql.cursors.DictCursor)
        assert plugins.settle_unknown_write_recovery_row(
            automation_id=current["automation_id"], generation=1, lease_id=current["lease_id"],
            recovery_status="APPLIED", evidence_sha256="b" * 64,
        )["transitioned"]
        connection.commit()
    after = _state(database, current)
    assert after["generation"] == {"state": "COMMITTED", "error_code": None}
    assert after["project"]["reconcile_state"] == "STABLE"


def test_public_receipt_diagnostics_use_real_columns_and_exact_lease_scope_without_writes(database):
    identity = _seed(database, outcome="WRITE_OUTCOME_UNKNOWN")
    unrelated = _seed(database, outcome="WRITE_OUTCOME_UNKNOWN")
    second_lease = str(uuid4())
    second_receipt = str(uuid4())
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO automation_project_generation_leases (lease_id, automation_id, generation,
                orchestration_run_id, lease_owner, runtime_metadata_json, runtime_metadata_sha256, outcome, expires_at)
                SELECT %s, automation_id, generation, orchestration_run_id, lease_owner,
                       runtime_metadata_json, runtime_metadata_sha256, outcome, expires_at
                FROM automation_project_generation_leases WHERE lease_id=%s""",
            (second_lease, identity["lease_id"]),
        )
        cursor.execute(
            """INSERT INTO automation_write_attempt_receipts (receipt_id, automation_id, generation, lease_id,
                orchestration_run_id, step_id, request_id, operation, action, argument_sha256,
                target_ref_sha256, target_ref_json, outcome, execution_resource_keys_json,
                legacy_scope_quarantined_at, created_at, updated_at)
                SELECT %s, automation_id, generation, %s, orchestration_run_id, step_id, %s,
                       operation, action, argument_sha256, target_ref_sha256, '{}', outcome, '[]',
                       legacy_scope_quarantined_at, created_at, updated_at
                FROM automation_write_attempt_receipts WHERE lease_id=%s""",
            (second_receipt, second_lease, str(uuid4()), identity["lease_id"]),
        )
        cursor.execute(
            """UPDATE automation_write_attempt_receipts
               SET target_ref_json=%s, execution_resource_keys_json=%s, legacy_scope_quarantined_at=NULL,
                   operation='network.request', action='feishu.sheet.replace_rows'
               WHERE lease_id=%s""",
            (json.dumps({"record_count": 17, "locator": "private-fixture-locator"}),
             json.dumps([["account-write", "private-fixture-account"], ["physical-write", "feishu_sheet", "private-parent", "private-child"]]),
             identity["lease_id"]),
        )
        connection.commit()

    def stored_receipts():
        with database._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_write_attempt_receipts WHERE lease_id IN (%s,%s,%s) ORDER BY receipt_id",
                (identity["lease_id"], second_lease, unrelated["lease_id"]),
            )
            return cursor.fetchall()

    before = stored_receipts()
    before_run = _state(database, identity)
    repository = OrchestrationRepository(lambda: _connection(database), database.pymysql.cursors.DictCursor)
    rows = repository.list_work_item_unknown_writes(identity["work_item_id"])
    assert {row["lease_id"] for row in rows} == {identity["lease_id"], second_lease}
    attempts = {row["lease_id"]: row["write_attempts"] for row in rows}
    first = attempts[identity["lease_id"]][0]
    second = attempts[second_lease][0]
    assert len(attempts[identity["lease_id"]]) == len(attempts[second_lease]) == 1
    assert first["operation"] == "network.request" and first["action"] == "feishu.sheet.replace_rows"
    assert first["record_count"] == 17 and first["outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert first["original_scope_key_kinds"] == ["account-write", "physical-write"]
    assert not first["scope_missing"] and not first["scope_malformed"] and not first["scope_quarantined"]
    assert second["receipt_id"] == second_receipt and second["record_count"] is None
    assert second["scope_missing"] and not second["scope_malformed"] and second["scope_quarantined"]
    raw = next(row for row in before if row["receipt_id"] == first["receipt_id"])
    assert first["created_at"] == raw["created_at"].isoformat()
    assert first["updated_at"] == raw["updated_at"].isoformat()
    assert not first["created_at"].endswith("Z") and "verified_at" not in first and "effect" not in first
    serialized = json.dumps(rows)
    assert "private" not in serialized and unrelated["lease_id"] not in serialized
    assert "target_ref_json" not in serialized and "execution_resource_keys_json" not in serialized
    assert stored_receipts() == before and _state(database, identity) == before_run


def test_real_production_scope_metadata_with_ingress_route_is_readable_without_mutation(database):
    from tests.test_historical_receipt_execution_scopes import _production_case

    row, keys, _physical = _production_case()
    metadata = json.loads(row["runtime_metadata_json"])
    identity = _seed(database, outcome="WRITE_OUTCOME_UNKNOWN", runtime_metadata=metadata)
    target = json.loads(row["target_ref_json"])
    target["automation_id"] = identity["automation_id"]
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE automation_write_attempt_receipts SET operation=%s,action=%s,argument_sha256=%s,
                   target_ref_json=%s,target_ref_sha256=%s,execution_resource_keys_json=%s,
                   legacy_scope_quarantined_at=NULL WHERE lease_id=%s""",
            (row["operation"], row["action"], target["content_sha256"], _json_param(target, {}), _json_hash(target),
             _json_param(keys, []), identity["lease_id"]),
        )
        connection.commit()

    def read_receipt():
        with database._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM automation_write_attempt_receipts WHERE lease_id=%s", (identity["lease_id"],))
            return cursor.fetchone()

    before, receipt_before = _state(database, identity), read_receipt()
    repository = OrchestrationRepository(lambda: _connection(database), database.pymysql.cursors.DictCursor)
    item = repository.list_work_item_unknown_writes(identity["work_item_id"])[0]["write_attempts"][0]
    assert item["scope_derivation_reason"] == "ORIGINAL_RESOURCE_SCOPE_DERIVED"
    assert item["derived_scope_key_kinds"] == ["account-resource", "physical-write"]
    assert item["outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert all(field not in json.dumps(item) for field in (
        "runtime_metadata_json", "target_ref_json", "webhook_route", "delivery_status_bitable"))
    assert _state(database, identity) == before and read_receipt() == receipt_before
