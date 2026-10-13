"""Historical ownership joins and current V2 replacement through actual MySQL."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import time
from uuid import uuid4

import pytest

from plugin_core_adapters import arrival_report
from plugin_core_adapters import arrival, first_party
from agent.automation_plugins.arrival_connectors_v2 import build_arrival_connectors
from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.list_connectors_v2 import build_list_connectors
from agent.automation_plugins.service_v2_projection import _contribution_backend
from service_v2_plugins._shared.build_zip import build_plugin_zip
from shared.orchestration_repository_support import _json_hash
from tests.test_arrival_report_ownership import DAY, _publication
from tests.direct_invocation_fixture import DirectFixture
from tests.test_arrival_production_adapter import _record
from tests.test_legacy_unknown_scope_migration_mysql import database as database
from tests.test_v2_maintenance_mysql import ACTOR, database as v2_database  # noqa: F401
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.problem_fixture import ACCOUNTS, ProblemAccounts


pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL")


def test_direct_invocation_mysql_publication_needs_no_run_and_incomplete_write_is_visible(database):
    seeded = database.seed(run_status="CANCELLED", receipt_outcome="WRITE_VERIFIED", lease_outcome="WRITE_VERIFIED")
    proof = _publication()
    target = proof["target_ref_json"]
    target["automation_id"] = database.project_id
    call_id = str(uuid4())
    digest = "c" * 64
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("""INSERT INTO automation_plugin_invocations(invocation_id,request_key_sha256,
            request_sha256,request_id,automation_id,plugin_id,plugin_version,generation,operation,
            source,actor_id,owner_id,status,invocation_json,arguments_json,result_json,started_at,
            finished_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,'1.0.0',1,'execute','console','fixture',%s,
            'COMPLETED','{}','{}',%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
            (call_id, digest, digest, call_id, database.project_id, database.plugin_id, str(uuid4()),
             json.dumps({"status": "SUCCESS", "data": {}, "meta": {}, "error": None})))
        cursor.execute("""UPDATE automation_project_generation_leases SET invocation_id=%s,
            orchestration_run_id=NULL,runtime_metadata_json=%s,runtime_metadata_sha256=%s WHERE lease_id=%s""",
            (call_id, json.dumps(proof["runtime_metadata_json"]), proof["runtime_metadata_sha256"], seeded["lease_id"]))
        cursor.execute("""UPDATE automation_write_attempt_receipts SET invocation_id=%s,
            orchestration_run_id=NULL,step_id=NULL,operation='network.request',action='feishu.sheet.replace',
            target_ref_json=%s,target_ref_sha256=%s,execution_resource_keys_json=%s WHERE receipt_id=%s""",
            (call_id, json.dumps(target), _json_hash(target), json.dumps(proof["execution_resource_keys_json"]), seeded["receipt_id"]))
        connection.commit()
    pending = arrival_report._read_publications(DAY)
    assert len(pending) == 1 and pending[0]["invocation_id"] == call_id
    assert pending[0]["publication_verified"] == 0
    final_target = {**target, "operation": "projection.invoke", "action": "arrival.snapshot.replace"}
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("""INSERT INTO automation_write_attempt_receipts(receipt_id,automation_id,generation,
            lease_id,invocation_id,request_id,operation,action,argument_sha256,target_ref_sha256,target_ref_json,
            outcome,evidence_sha256,created_at,updated_at) VALUES(%s,%s,1,%s,%s,%s,'projection.invoke',
            'arrival.snapshot.replace',%s,%s,%s,'WRITE_VERIFIED',%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
            (str(uuid4()), database.project_id, seeded["lease_id"], call_id, str(uuid4()),
             final_target["content_sha256"], _json_hash(final_target), json.dumps(final_target), "e" * 64))
        connection.commit()
    closed = arrival_report._read_publications(DAY)
    assert len(closed) == 1 and closed[0]["publication_verified"] == 1
    assert "orchestration_run_id" not in closed[0]
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE automation_plugin_invocations SET status='WRITE_OUTCOME_UNKNOWN' WHERE invocation_id=%s", (call_id,))
        connection.commit()
    unconfirmed = arrival_report._read_publications(DAY)
    assert len(unconfirmed) == 1 and unconfirmed[0]["publication_verified"] == 0


def test_mysql_requires_completed_run_verified_step_and_final_same_day_snapshot(database):
    seeded = database.seed(run_status="COMPLETED", step_status="COMPLETED",
                           receipt_outcome="WRITE_VERIFIED", lease_outcome="WRITE_VERIFIED")
    proof = _publication()
    target = proof["target_ref_json"]
    target["automation_id"] = database.project_id
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE agent_runs SET finished_at=UTC_TIMESTAMP(6) WHERE run_id=%s", (seeded["run_id"],))
        cursor.execute("UPDATE agent_run_steps SET postcondition_status='VERIFIED' WHERE step_id=%s", (seeded["step_id"],))
        cursor.execute("UPDATE automation_project_generation_leases SET runtime_metadata_json=%s,runtime_metadata_sha256=%s WHERE lease_id=%s",
                       (json.dumps(proof["runtime_metadata_json"]), proof["runtime_metadata_sha256"], seeded["lease_id"]))
        cursor.execute("""UPDATE automation_write_attempt_receipts SET operation='network.request',
                       action='feishu.sheet.replace',target_ref_json=%s,target_ref_sha256=%s,
                       execution_resource_keys_json=%s WHERE receipt_id=%s""",
                       (json.dumps(target), _json_hash(target), json.dumps(proof["execution_resource_keys_json"]), seeded["receipt_id"]))
        connection.commit()
    assert arrival_report._read_publications(DAY) == []
    final_target = {**target, "operation": "projection.invoke", "action": "arrival.snapshot.replace"}
    final_id = str(uuid4())
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("""INSERT INTO automation_write_attempt_receipts(receipt_id,automation_id,generation,
                       lease_id,orchestration_run_id,step_id,request_id,operation,action,argument_sha256,
                       target_ref_sha256,target_ref_json,outcome,evidence_sha256,created_at,updated_at)
                       VALUES(%s,%s,1,%s,%s,%s,%s,'projection.invoke','arrival.snapshot.replace',%s,%s,%s,
                              'WRITE_VERIFIED',%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                       (final_id, database.project_id, seeded["lease_id"], seeded["run_id"], seeded["step_id"],
                        str(uuid4()), final_target["content_sha256"], _json_hash(final_target), json.dumps(final_target), "e" * 64))
        connection.commit()
    before = database.snapshot()
    rows = arrival_report._read_publications(DAY)
    assert len(rows) == 1 and rows[0]["orchestration_run_id"] == seeded["run_id"]
    assert arrival_report._read_publications("2026-09-09") == []
    assert database.snapshot() == before
    for table, key, key_value, field, bad, restore in (
        ("agent_runs", "run_id", seeded["run_id"], "status", "FAILED_TERMINAL", "COMPLETED"),
        ("agent_run_steps", "step_id", seeded["step_id"], "postcondition_status", "FAILED", "VERIFIED"),
        ("automation_write_attempt_receipts", "receipt_id", final_id, "outcome", "WRITE_OUTCOME_UNKNOWN", "WRITE_VERIFIED"),
    ):
        with database.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET {field}=%s WHERE {key}=%s", (bad, key_value))
            connection.commit()
        assert arrival_report._read_publications(DAY) == []
        with database.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET {field}=%s WHERE {key}=%s", (restore, key_value))
            connection.commit()


def test_real_v2_plugins_broker_and_mysql_latest_list_replaces_shared_sheet(v2_database, monkeypatch, tmp_path):
    """Only TMS/Feishu terminal protocols use fixtures; SQL and algorithms run."""
    fixture, name = v2_database
    monkeypatch.setenv("AGENT_DB_NAME", name)

    def connect():
        return fixture.pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, database=name, charset="utf8mb4", autocommit=False,
            cursorclass=fixture.pymysql.cursors.DictCursor)

    source = [{**_record("R12345678901"), "recipient_address": "complete fixture delivery address"}]
    monkeypatch.setattr(first_party, "_arrive_list_read_page", lambda *_args: {
        "items": deepcopy(source), "returned": len(source), "total": len(source), "total_authoritative": True,
    })
    monkeypatch.setattr(first_party, "_scan_read_page", lambda *_args: {
        "items": [], "returned": 0, "total": 0, "total_authoritative": True,
    })
    resources = {}
    for resource_id in ("fixture-primary-resource", "fixture-secondary-resource", "fixture-split-resource"):
        resources[resource_id] = {
            "resource_kind": "feishu_sheet", "spreadsheet_token": "fixture-book", "sheet_id": resource_id,
            "range": f"{resource_id}!A2:R100", "snapshot_range": f"{resource_id}!A2:S100",
            "clear_range": f"{resource_id}!A2:S100", "title_range": f"{resource_id}!A1:S1",
        }
    resources["fixture-split-resource"]["range"] = "fixture-split-resource!A1:S100"
    for resource_id, resource in resources.items():
        resource["_meta"] = {"resource_key": resource_id, "configuration_version": 1,
            "source": "explicit isolated resource fixture",
            "config_sha256": sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()}
    monkeypatch.setattr(arrival, "_load_resource", lambda key: deepcopy(resources[key]))
    from agent import workflow_resource_store
    monkeypatch.setattr(workflow_resource_store, "get_saved_workflow_resource", lambda key: deepcopy(resources[key]))
    cells = {key: {} for key in resources}
    feishu_writes = []

    def feishu(action, params):
        shape = arrival._range_shape(params["range"], label="fixture request")
        sheet = cells[shape["sheet"]]
        if action == "write_sheet":
            feishu_writes.append(shape["sheet"])
            for offset, row in enumerate(params["values"]):
                sheet[shape["start_row"] + offset] = list(row)
            return {"ok": True}
        if action == "clear_sheet":
            feishu_writes.append(shape["sheet"])
            for row in range(shape["start_row"], shape["end_row"] + 1):
                sheet.pop(row, None)
            return {"ok": True}
        assert action == "read_sheet"
        return {"values": [sheet.get(row, []) for row in range(shape["start_row"], shape["end_row"] + 1)]}

    monkeypatch.setattr(arrival, "_invoke_feishu", feishu)

    accounts = ProblemAccounts()
    reviewed = first_party.build_production_first_party_core_handler_map(
        cursor_secret=b"fixture-report-publication-secret", account_manager=accounts)
    registry = ConnectorRegistry((*build_arrival_connectors(reviewed), *build_list_connectors(reviewed)))
    root = Path(__file__).resolve().parents[1]
    with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "host",
            account_manager=accounts, resource_provider=resources.get,
            enable_directory_faults=False, connector_registry=registry,
            contribution_backend_status=_contribution_backend) as host:
        projects = {}
        for plugin_id, account_role, bindings, config in (
            ("sync_arrive_list", "arrive_list_ronghui", {
                "arrive_primary_sheet": "fixture-primary-resource",
                "arrive_secondary_sheet": "fixture-secondary-resource"}, {"target_date": DAY}),
            ("sync_arrival_stats", "arrival_stats_tms", {
                "arrival_stats_primary_sheet": "fixture-primary-resource",
                "arrival_stats_secondary_sheet": "fixture-secondary-resource",
                "arrival_stats_split_pending_sheet": "fixture-split-resource",
                "arrival_stats_archive_sheet": "fixture-split-resource"}, {
                    "target_date": DAY, "pending_sheet_disabled": True, "archive_snapshot": False}),
        ):
            archive = build_plugin_zip(root / "agent/service_v2_plugins" / (plugin_id + "_v2"),
                tmp_path / (plugin_id + ".zip")).read_bytes()
            installed = host.management.install_service_v2(archive, request_id=str(uuid4()),
                transport_package_sha256=sha256(archive).hexdigest(), actor=ACTOR,
                raw_intent=json.dumps({"instance_name": plugin_id, "permissions_confirmed": True}))
            project = projects[plugin_id] = installed["automation_id"]
            entry = host.catalog.require(project)
            host.configuration.save(project, config=config,
                account_bindings={account_role: [ACCOUNTS["account_id"]]}, resource_bindings=bindings,
                enabled_entrypoints=tuple(key for key, value in entry.invocation_contracts.items()
                    if value.get("contribution_kind") == "console"),
                schedule={"kind": "none", "times": [], "enabled": False}, device_id=None,
                actor_id=ACTOR.actor_id, actor_role="super_admin", request_id=str(uuid4()),
                expected_project_configuration_version=entry.project_config_version)
            host.targets.reconcile_project(project)
            entry = host.catalog.require(project)
            host.management.set_enabled(project, enabled=True, expected_record_version=entry.record_version,
                request_id=str(uuid4()), actor=ACTOR)
            host.targets.reconcile_project(project)

        with DirectFixture(host, directory=tmp_path / "ipc", saved_resource_provider=resources.get) as runtime:
            def run_plugin(plugin_id):
                receipt = host.policy.invoke_console(projects[plugin_id], request_id=str(uuid4()), actor=ACTOR)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    result = runtime.service.get(receipt["invocation_id"])
                    if result["status"] not in {"STARTING", "ACCEPTED", "RUNNING"}:
                        break
                    time.sleep(.02)
                assert result["status"] == "COMPLETED", (result, runtime.broker_errors)
                with connect() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT operation,action,outcome FROM automation_write_attempt_receipts WHERE invocation_id=%s",
                        (receipt["invocation_id"],))
                    writes = cursor.fetchall()
                assert writes and all(row["outcome"] == "WRITE_VERIFIED" for row in writes)
                return result["result"], writes

            first, _ = run_plugin("sync_arrive_list")
            assert first["data"]["bill_codes"] == 1
            assert len(cells["fixture-primary-resource"][1]) == 18
            stats, _ = run_plugin("sync_arrival_stats")
            assert stats["data"]["records"] == 1
            assert cells["fixture-primary-resource"][2][18] == 0
            split_before = deepcopy(cells["fixture-split-resource"])
            source[0]["goods_name"] = "latest fixture goods"
            updated, _ = run_plugin("sync_arrive_list")
            assert updated["data"]["bill_codes"] == 1
            assert updated["data"]["statistics_sheets_preserved"] == []
            assert len(cells["fixture-primary-resource"][1]) == 18
            assert len(cells["fixture-primary-resource"][2]) == 18
            assert cells["fixture-primary-resource"][2][1] == "latest fixture goods"
            assert cells["fixture-split-resource"] == split_before
            # An authoritative empty source is also the latest valid result.
            source.clear()
            writes_before = len(feishu_writes)
            last, writes = run_plugin("sync_arrive_list")
            assert last["data"]["bill_codes"] == 0
            assert last["data"]["statistics_sheets_preserved"] == []
            for resource_id in ("fixture-primary-resource", "fixture-secondary-resource"):
                assert len(cells[resource_id][1]) == 18
                assert all(not any(value not in ("", None) for value in row)
                    for number, row in cells[resource_id].items() if number > 1)
            assert cells["fixture-split-resource"] == split_before
            assert set(feishu_writes[writes_before:]) == {"fixture-primary-resource", "fixture-secondary-resource"}
            assert {row["operation"] for row in writes} == {"service.invoke"}
            assert sorted(row["action"] for row in writes) == ["forecast_replace", "replace", "replace", "waybill_replace"]
            from tools.daily_sign_store import snapshot_fingerprint
            forecast = arrival._read_forecast_runs(DAY)
            assert any(row["row_count"] == 0 and row["fingerprint"] == snapshot_fingerprint([]) for row in forecast)
            project = projects["sync_arrive_list"]
            entry = host.catalog.require(project)
            host.management.save_plugin_settings(project, config={"target_date": "2026-09-09"},
                account_bindings={"arrive_list_ronghui": [ACCOUNTS["account_id"]]},
                resource_bindings={"arrive_primary_sheet": "fixture-primary-resource",
                    "arrive_secondary_sheet": "fixture-secondary-resource"}, request_id=str(uuid4()),
                expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
            host.targets.reconcile_project(project)
            following, _ = run_plugin("sync_arrive_list")
            assert following["data"]["statistics_sheets_preserved"] == []
            assert len(feishu_writes) > writes_before
            assert cells["fixture-split-resource"] == split_before
            assert arrival._read_forecast_runs(DAY) == forecast
