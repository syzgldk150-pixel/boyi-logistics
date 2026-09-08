"""Actual MySQL join: a verified sheet alone cannot claim statistics ownership."""
from __future__ import annotations

import json
import os
import asyncio
from copy import deepcopy
from pathlib import Path
import socket
from uuid import uuid4

import pytest

from plugin_core_adapters import arrival_report
from plugin_core_adapters import arrival, first_party
from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer, LocalCoreAutomationBroker
from agent.automation_plugins.core_adapter import AccountManagerSessionResolver, RegisteredCoreAutomationBrokerAdapter
from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.orchestration.execution_resources import EXECUTION_ACTION_SCOPES
from agent.tool_registry import ToolRegistry
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.execution_resource_journal import EXECUTION_RESOURCE_KEYS
from shared.orchestration_repository_support import _json_hash
from tests.test_arrival_report_ownership import DAY, ACCOUNT, _publication
from tests.first_party_action_payload_support import load_first_party_action
from tests.test_arrival_production_adapter import _record
from tests.test_first_party_action_payloads import _ExactResourceResolver
from tests.test_legacy_unknown_scope_migration_mysql import database as database


pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL")


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


def test_real_plugins_broker_and_mysql_list_statistics_list_keep_statistical_sheet(database, monkeypatch, tmp_path):
    """Only TMS/Feishu terminal protocols use fixtures; SQL and algorithms run."""
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
            "_meta": {"resource_key": resource_id},
        }
    resources["fixture-split-resource"]["range"] = "fixture-split-resource!A1:S100"
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

    class Manager:
        def require_active_binding_descriptor(self, account_id):
            assert account_id == ACCOUNT
            return {"account_id": ACCOUNT, "system": "ronghui", "account_purpose": "general", "session_profile": "fixture"}

    manager = Manager()
    root = Path(__file__).resolve().parents[1]
    manifests = resolve_first_party_manifests(ToolRegistry(root / "agent/tools/registry.yaml"))

    def record_write(receipt):
        with database.helper._connection() as connection:
            AutomationPluginRepository(connection).record_generation_write_attempt_row(receipt)
            connection.commit()

    issuer = LocalBrokerCapabilityIssuer(tmp_path / "arrival-report.sock", write_attempt_recorder=record_write)
    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers=first_party.build_production_first_party_core_handler_map(
            cursor_secret=b"fixture-report-publication-secret", account_manager=manager,
        ), account_resolver=AccountManagerSessionResolver(manager),
        resource_resolver=_ExactResourceResolver({key: "feishu_sheet" for key in resources}),
    )

    async def run_plugin(plugin_id, day):
        manifest = manifests[plugin_id]
        mapping = manifest.to_mapping()
        binding = ({"arrive_primary_sheet": "fixture-primary-resource", "arrive_secondary_sheet": "fixture-secondary-resource"}
                   if plugin_id == "sync_arrive_list" else {
                       "arrival_stats_primary_sheet": "fixture-primary-resource", "arrival_stats_secondary_sheet": "fixture-secondary-resource",
                       "arrival_stats_split_pending_sheet": "fixture-split-resource", "arrival_stats_archive_sheet": "fixture-split-resource",
                   })
        seeded = database.seed(run_status="RUNNING", step_status="RUNNING", lease_outcome="RUNNING",
                               receipt_outcome="NOT_APPLIED")
        metadata = {"account_bindings": {"account_id": [ACCOUNT]}, "resource_bindings": binding}
        with database.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE automation_project_generation_leases SET runtime_metadata_json=%s,runtime_metadata_sha256=%s WHERE lease_id=%s",
                           (json.dumps(metadata), _json_hash(metadata), seeded["lease_id"]))
            connection.commit()
        scopes = {}
        for role, key in binding.items():
            scopes[("network.request", "feishu.sheet.replace", role)] = (("physical-write", "feishu_sheet",
                arrival_report._sha("fixture-book"), arrival_report._sha(key)),)
        all_keys = tuple(sorted({key for group in scopes.values() for key in group}))
        token_keys = EXECUTION_RESOURCE_KEYS.set(all_keys)
        token_scopes = EXECUTION_ACTION_SCOPES.set(scopes)
        try:
            capability = issuer.issue(
                automation_id=database.project_id, plugin_version=manifest.version, tool_name=plugin_id,
                ttl_seconds=120, runtime_permissions=mapping["runtime_permissions"],
                account_roles=mapping["account_roles"], resource_roles=mapping["resource_roles"],
                account_bindings={"account_id": [ACCOUNT]}, resource_bindings=binding,
                write_attempt_context={"automation_id": database.project_id, "plugin_id": plugin_id, "generation": 1,
                    "lease_id": seeded["lease_id"], "orchestration_run_id": seeded["run_id"], "step_id": seeded["step_id"]},
            )
        finally:
            EXECUTION_RESOURCE_KEYS.reset(token_keys)
            EXECUTION_ACTION_SCOPES.reset(token_scopes)

        def call(operation, *, action, role, arguments):
            request = {"schema_version": 1, "capability": capability, "request_id": str(uuid4()), "operation": operation,
                       "action": action, "role": role, "arguments": arguments}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(issuer.broker_socket_path))
                client.sendall(json.dumps(request).encode() + b"\n")
                with client.makefile("rb") as stream:
                    response = json.loads(stream.readline())
            assert response["ok"] is True, response
            return response["data"]

        action = load_first_party_action(plugin_id)
        arguments = {"target_date": day}
        if plugin_id == "sync_arrival_stats":
            arguments.update(pending_sheet_disabled=True, archive_snapshot=False)
        result = await asyncio.to_thread(action.run_action, arguments, call)
        assert result["status"] == "SUCCESS"
        observations = issuer.broker_call_observations(capability)
        written = [row for row in observations if row["write_started"]]
        assert written and all(row["result"].get("committed") is True for row in written)
        # The workflow result is already verified at every production port;
        # publish its terminal fixture state to exercise the real reader join.
        with database.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE automation_write_attempt_receipts SET outcome='WRITE_VERIFIED' WHERE lease_id=%s AND outcome='STARTED'", (seeded["lease_id"],))
            cursor.execute("UPDATE automation_project_generation_leases SET outcome='WRITE_VERIFIED' WHERE lease_id=%s", (seeded["lease_id"],))
            cursor.execute("UPDATE agent_runs SET status='COMPLETED',finished_at=UTC_TIMESTAMP(6) WHERE run_id=%s", (seeded["run_id"],))
            cursor.execute("UPDATE agent_run_steps SET status='COMPLETED',postcondition_status='VERIFIED' WHERE step_id=%s", (seeded["step_id"],))
            connection.commit()
        issuer.revoke(capability)
        return result, written

    async def scenario():
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=adapter)
        await broker.start()
        try:
            first, _ = await run_plugin("sync_arrive_list", DAY)
            assert first["data"]["bill_codes"] == 1
            assert len(cells["fixture-primary-resource"][1]) == 18
            stats, _ = await run_plugin("sync_arrival_stats", DAY)
            assert stats["data"]["records"] == 1
            assert cells["fixture-primary-resource"][2][18] == 0
            before = deepcopy(cells)
            source.clear()
            writes_before = len(feishu_writes)
            last, writes = await run_plugin("sync_arrive_list", DAY)
            assert last["data"]["bill_codes"] == 0
            assert last["data"]["statistics_sheets_preserved"] == ["arrive_primary_sheet", "arrive_secondary_sheet"]
            assert cells == before and len(feishu_writes) == writes_before
            assert {row["action"] for row in writes} == {"waybill.snapshot.replace", "arrival.forecast_snapshot.replace"}
            from tools.daily_sign_store import snapshot_fingerprint
            forecast = arrival._read_forecast_runs(DAY)
            assert any(row["row_count"] == 0 and row["fingerprint"] == snapshot_fingerprint([]) for row in forecast)
            following, _ = await run_plugin("sync_arrive_list", "2026-09-09")
            assert following["data"]["statistics_sheets_preserved"] == []
            assert len(feishu_writes) > writes_before
        finally:
            await broker.stop()

    asyncio.run(scenario())
