"""Actual V2 process/Broker results published to isolated MySQL across an upgrade."""
from dataclasses import replace
from uuid import uuid4

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.customer_connectors_v2 import build_customer_connectors
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from agent.customer_collection_business import prepare_customer_rechecks, publish_verified_customer_collection
from shared.customer_service_repository import CustomerServiceRepository
from shared.data_sources import DataSourceError, DataSourceRepository
from shared.plugin_source_migration import transfer_plugin_sources
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_customer_collection_business_mysql import _collector, _seed_generation
from tests.test_module_data_sources_mysql import database  # noqa: F401


def test_v2_rechecks_existing_source_after_cutover_and_retains_history_on_rollback(database, tmp_path):
    fixture, name = database
    source, target, account, organization = ("isolated-" + uuid4().hex for _ in range(4))
    metadata = {"account_bindings": {"customer_service_source": [account]}}
    _, collect = _collector(source, account, organization)
    with fixture._connection(name) as connection:
        _seed_generation(connection, source)
        _seed_generation(connection, target)
        old_call, old = collect([])
        publish_verified_customer_collection(connection, invocation_id=old_call, automation_id=source,
            metadata=metadata, outcome=old, recheck_items=[])
        transferred = transfer_plugin_sources(connection, source_id=source, target_id=target,
            target_generation=1, request_id=uuid4().hex)
        assert len(transferred) == 1
        source_key = transferred[0]
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO customer_problem_manual_fields(source_id,external_id,source_direction,note,revision,updated_at) VALUES(%s,'synthetic-problem','received','保留人工备注',1,UTC_TIMESTAMP(6))", (source_key,))
        refs = prepare_customer_rechecks(connection, automation_id=target, metadata=metadata)
        assert len(refs) == 1
        assert prepare_customer_rechecks(connection, automation_id=source, metadata=metadata) == []

        def protocol(arguments):
            assert arguments["account_id"] == account
            if arguments["action"] == "detail":
                return {"ok": True, "details": {"REVERSION": "已回复", "REVERSION_STATUS": "已回复"}}
            return {"ok": True, "source_site_code": organization, "rows": [],
                "stats": {"total": 0, "returned": 0, "total_authoritative": True}}

        handlers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
            describe_account=lambda value: {"account_id": value, "system": "ronghui", "session_profile": "isolated"},
            customer_action=protocol))
        context = CoreBrokerInvocationContext(automation_id=target, plugin_version="2.0.0",
            tool_name="sync_customer_service_problems_v2", operation="service.invoke", action="run", role="__system__",
            account_bindings={"customer_service_source": (account,)})
        host = PackagedConnectorHost(tmp_path, "sync_customer_service_problems_v2", ConnectorRegistry(build_customer_connectors(handlers)), context)
        raw = host.execute({"direction": "received", "recheck_items": refs}, operation="run")
        assert raw["status"] == "SUCCESS", raw
        assert len(host.observations) == 2
        assert all(row["service_target"]["service"] == "connector.boyi.customer_sources@1" for row in host.observations)
        call_id = host.write_identity["invocation_id"]
        outcome = host.verified_outcome
        proof = outcome.generation_verification
        invalid_outcome = replace(outcome, generation_verification=replace(proof, host_call_observations=tuple(
            {**row, "service_target": {**row["service_target"], "service": "connector.other@1"}}
            for row in host.observations)))
        with pytest.raises(DataSourceError, match="DETAIL_EVIDENCE_MISSING"):
            publish_verified_customer_collection(connection, invocation_id=call_id, automation_id=target,
                metadata=metadata, outcome=invalid_outcome, recheck_items=refs)
        publish_verified_customer_collection(connection, invocation_id=call_id, automation_id=target,
            metadata=metadata, outcome=outcome, recheck_items=refs)
        rows = CustomerServiceRepository(connection).query(source_ids=[source_key])["rows"]
        assert len(rows) == 1 and rows[0]["resolved"] is True and rows[0]["note"] == "保留人工备注"
        assert transfer_plugin_sources(connection, source_id=target, target_id=source,
            target_generation=1, request_id=uuid4().hex) == transferred
        assert DataSourceRepository(connection).get(source_key)["producer_instance_id"] == source
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM customer_problem_publications WHERE source_id=%s", (source_key,))
            assert cursor.fetchone()["n"] == 2
        connection.rollback()
