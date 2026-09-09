"""Real business SQL and original plugin/Host primitives; external data is synthetic."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from agent.automation_plugins.models import GenerationVerificationContext
from agent.customer_collection_business import prepare_customer_rechecks, publish_verified_customer_collection
from agent.orchestration.models import ToolResult
from shared.customer_service_repository import CustomerServiceRepository
from shared.data_sources import DataSourceError, DataSourceRepository
from tests.first_party_action_payload_support import load_first_party_action
from tests.test_module_data_sources_mysql import database  # noqa: F401


def _seed_generation(connection, producer):
    plugin_id = "synthetic_" + producer
    fields = ["package", "manifest", "project_config", "account_bindings", "resource_bindings",
        "device_binding", "schedule", "core_registry", "tool_contract", "invocation_contracts",
        "compiled_invocations", "runtime_descriptor", "governance_anchor", "policy_contract",
        "enabled_entrypoints", "snapshot"]
    columns = ["automation_id", "generation", "request_id", "plugin_id", "plugin_version", "trust_source", "snapshot_json"]
    values = [producer, 1, str(uuid4()), plugin_id, "1.0.0", "builtin_release", "{}"]
    columns += [name + "_sha256" for name in fields]
    values += ["a" * 64] * len(fields)
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO automation_plugin_packages(plugin_id,display_name,description,latest_version) VALUES(%s,'合成来源校验','isolated fixture','1.0.0')", (plugin_id,))
        hashes = ["package", "manifest", "tool_contract", "config_schema", "allowed_entrypoints",
            "invocation_contracts", "worker_requirement", "runtime", "scheduling", "install_root_metadata"]
        version_columns = ["plugin_id", "version", "manifest_json", "trust_source", "install_root_metadata_json", "installed_by_actor_id"] + [field + "_sha256" for field in hashes]
        cursor.execute("INSERT INTO automation_plugin_versions(" + ",".join(version_columns) + ") VALUES(" + ",".join(["%s"] * len(version_columns)) + ")",
            (plugin_id, "1.0.0", "{}", "builtin_release", "{}", "synthetic-admin", *(["a" * 64] * len(hashes))))
        cursor.execute("INSERT INTO automation_projects(automation_id,plugin_id,plugin_version,display_name,install_request_id,install_payload_sha256,installed_by_actor_id) VALUES(%s,%s,'1.0.0','合成业务实例',%s,%s,'synthetic-admin')",
            (producer, plugin_id, str(uuid4()), "a" * 64))
        cursor.execute("INSERT INTO automation_project_generations (" + ",".join(columns) + ") VALUES (" + ",".join(["%s"] * len(values)) + ")", values)


def _collector(producer, account, organization):
    state = {"empty": False, "detail": "reply"}
    calls = []
    def protocol(arguments):
        if arguments["action"] == "detail":
            if state["detail"] == "auth":
                return {"ok": False, "error_code": "AUTH_REQUIRED"}
            return {"ok": True, "details": {"REVERSION": "已回复" if state["detail"] == "reply" else "",
                "REVERSION_STATUS": "已回复" if state["detail"] == "reply" else "未回复"}}
        rows = [] if state["empty"] else [{"platform": "ronghui", "source_direction": arguments["direction"],
            "external_id": "synthetic-problem", "site_policy_required": False,
            "raw_fields": {"GUID": "synthetic-problem", "BILL_CODE": "SYNTHETIC-WAYBILL",
                "REGISTER_SITE": "合成网点", "SEND_SITE": "合成网点", "PROBLEM_CAUSE": "隔离问题",
                "REVERSION_STATUS": "未回复", "REVERSION": "", "REVERSION_DATE": "2026-09-09 10:00:00"}}]
        return {"ok": True, "source_site_code": organization, "rows": rows,
            "stats": {"total": len(rows), "returned": len(rows), "total_authoritative": True}}
    handlers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
        describe_account=lambda value: {"account_id": value, "system": "ronghui", "session_profile": "synthetic"},
        customer_action=protocol), cursor_secret=b"synthetic-business-protocol-secret")
    def collect(refs):
        calls.clear()
        def broker(operation, *, action, role, arguments):
            context = CoreBrokerInvocationContext(automation_id=producer, plugin_version="1.0.0",
                generation=1, tool_name="sync_customer_service_problems", operation=operation,
                action=action, role=role, account_ids=(account,), account_bindings={role: (account,)})
            response = handlers[(operation, action)](context, arguments)
            calls.append({"action": action, "result": response})
            return response
        raw = load_first_party_action("sync_customer_service_problems").run_action(
            {"direction": "received", "recheck_items": refs}, broker)
        assert raw["status"] == "SUCCESS"
        call_id = str(uuid4())
        raw["meta"]["account_id"] = "binding-set:" + "a" * 64
        result = ToolResult(status=raw["status"], data=raw["data"], meta=raw["meta"],
            warnings=tuple(raw.get("warnings", [])), error=raw.get("error"))
        verification = GenerationVerificationContext(automation_id=producer, generation=1,
            lease_id=str(uuid4()), invocation_id=call_id, account_ids=(account,),
            account_bindings_sha256="a" * 64, requires_write_verification=False,
            host_call_observations=tuple(calls))
        return call_id, SimpleNamespace(accepted=True, result=result, generation_verification=verification)
    return state, collect


@pytest.mark.parametrize("detail_state", ["reply", "unproven", "auth"])
def test_direct_publish_recheck_uses_business_rows_preserves_manual_fields_and_uncertain_state(database, detail_state):
    fixture, name = database
    producer, account, organization = ("synthetic-" + uuid4().hex for _ in range(3))
    metadata = {"account_bindings": {"customer_service_source": [account]}}
    state, collect = _collector(producer, account, organization)
    with fixture._connection(name) as connection:
        _seed_generation(connection, producer)
        refs = prepare_customer_rechecks(connection, automation_id=producer, metadata=metadata)
        assert refs == []
        call_id, outcome = collect(refs)
        publish_verified_customer_collection(connection, invocation_id=call_id, automation_id=producer,
            metadata=metadata, outcome=outcome, recheck_items=refs)
        source = next(row for row in DataSourceRepository(connection).list_sources("customer_service") if row["organization_key"] == organization)
        source_id = source["source_id"]
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO customer_problem_manual_fields(source_id,external_id,source_direction,note,revision,updated_at) VALUES(%s,'synthetic-problem','received','保留人工备注',1,UTC_TIMESTAMP(6))", (source_id,))
        refs = prepare_customer_rechecks(connection, automation_id=producer, metadata=metadata)
        assert len(refs) == 1 and "account_id" not in refs[0]
        assert prepare_customer_rechecks(connection, automation_id="other-instance", metadata=metadata) == []
        assert prepare_customer_rechecks(connection, automation_id=producer,
            metadata={"account_bindings": {"customer_service_source": ["other-account"]}}) == []
        state.update(empty=True, detail=detail_state)
        call_id, outcome = collect(refs)
        publish_verified_customer_collection(connection, invocation_id=call_id, automation_id=producer,
            metadata=metadata, outcome=outcome, recheck_items=refs)
        rows = CustomerServiceRepository(connection).query(source_ids=[source_id])["rows"]
        assert len(rows) == 1 and rows[0]["note"] == "保留人工备注"
        assert rows[0]["resolved"] is (detail_state == "reply")
        with connection.cursor() as cursor:
            for table in ("agent_commands", "agent_runs", "agent_run_steps", "work_items"):
                cursor.execute("SELECT COUNT(*) AS n FROM " + table)
                assert cursor.fetchone()["n"] == 0
        connection.rollback()


def test_changed_invocation_or_forged_detail_proof_cannot_publish(database):
    fixture, name = database
    producer, account, organization = ("synthetic-" + uuid4().hex for _ in range(3))
    metadata = {"account_bindings": {"customer_service_source": [account]}}
    state, collect = _collector(producer, account, organization)
    with fixture._connection(name) as connection:
        _seed_generation(connection, producer)
        call_id, outcome = collect([])
        with pytest.raises(DataSourceError, match="INVOCATION_PROOF"):
            publish_verified_customer_collection(connection, invocation_id=str(uuid4()), automation_id=producer,
                metadata=metadata, outcome=outcome, recheck_items=[])
        publish_verified_customer_collection(connection, invocation_id=call_id, automation_id=producer,
            metadata=metadata, outcome=outcome, recheck_items=[])
        refs = prepare_customer_rechecks(connection, automation_id=producer, metadata=metadata)
        state["empty"] = True
        call_id, outcome = collect(refs)
        original = outcome.generation_verification
        outcome.generation_verification = replace(original,
            host_call_observations=tuple(row for row in original.host_call_observations if row["action"] != "customer_problem.detail"))
        with pytest.raises(DataSourceError, match="DETAIL_EVIDENCE"):
            publish_verified_customer_collection(connection, invocation_id=call_id, automation_id=producer,
                metadata=metadata, outcome=outcome, recheck_items=refs)
        outcome.generation_verification = original
        with pytest.raises(DataSourceError, match="UNEXPECTED_PROBLEM_DETAIL"):
            publish_verified_customer_collection(connection, invocation_id=call_id, automation_id=producer,
                metadata=metadata, outcome=outcome, recheck_items=[])
        connection.rollback()
