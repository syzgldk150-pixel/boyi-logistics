"""Real plugin algorithm + Connector Registry + reviewed primitives.

Only external infrastructure ports use isolated data. No whole-business mock,
production credentials, live business writes or generated success totals.
"""
import asyncio
import copy
import hashlib
import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from agent.automation_plugins.arrival_connectors_v2 import build_arrival_connectors
from agent.automation_plugins.connector_registry import (
    ConnectorBindingRef, ConnectorBindingKind, ConnectorBindingInvalid,
    ConnectorHostInternalBindingRef, ConnectorInvocationError, ConnectorResourceBindingRef, ConnectorRegistry,
)
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext, RegisteredCoreAutomationBrokerAdapter
from agent.automation_plugins.capability_proxy_v2 import build_service_v2_capability_handler_map
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from agent.automation_plugins.first_party_handler_support import _ARRIVE_FIELDS
from agent.automation_plugins.manifest import canonical_json_bytes
from tests.first_party_action_payload_support import load_first_party_action
from tests.test_automation_plugin_connector_runtime_v2 import _manifest, _Orchestration, _AccountResolver, _grant


_ROOT = Path(__file__).resolve().parents[1]
_ACCOUNT = "isolated-arrival-account"
_DATE = "2026-09-11"


def _setup(**port_overrides):
    row = {key: "" for key in _ARRIVE_FIELDS}
    row.update(tracking_number="R12345678901", goods_name="配件", package_type="纸箱",
        delivery_method="自提", quantity=2, actual_weight="10", volume="0.1",
        recipient_name="测试收件人", recipient_phone="13800000000",
        recipient_address="隔离测试地址", destination_station="测试站", settlement_weight="10",
        volumetric_weight="10", shipping_fee="0", payment_type="现付", pay_on_arrival="0")
    scans = [{"bill_code": "R123456789010001", "destination": "测试站", "scan_type": "arrival",
              "scan_time": "2026-09-11 08:00:00", "scan_site": "测试站"}]
    calls = []
    writes = []
    def descriptor(account):
        assert account == _ACCOUNT
        return {"account_id": account, "system": "ronghui", "session_profile": "isolated-only"}
    def page(items):
        def read(account, day, page_number, size):
            calls.append((account["account_id"], day, page_number, size))
            return {"items": copy.deepcopy(items), "returned": len(items), "total": len(items), "total_authoritative": True}
        return read
    def commit(records, day):
        writes.append(("projection", copy.deepcopy(records), day))
        return {"ok": True, "verified": True, "record_count": len(records)}
    def scan_commit(records, day):
        result = commit(records,day)
        return {**result,"readback_count":len(records),
                "identities_sha256":hashlib.sha256(canonical_json_bytes(sorted(records,key=lambda row:row["raw_code"]))).hexdigest()}
    def sheet(resource, layout, records, day):
        writes.append((resource, layout, copy.deepcopy(records), day))
        return {"ok": True, "verified": True, "record_count": len(records)}
    ports = FirstPartyCoreHandlerPorts(describe_account=descriptor,
        arrive_list_read_page=page([row]), scan_read_page=page(scans),
        waybill_detail_read=lambda _a, code: copy.deepcopy(row) if code == row["tracking_number"] else None,
        read_scan_snapshot=lambda _d: [], read_completed_arrivals_before=lambda _d: [],
        read_pending_waybills=lambda _d: [], replace_scan_snapshot=scan_commit,
        cleanup_scan_snapshot=lambda days: {"ok": True, "verified": True, "deleted": 0},
        replace_waybill_snapshot=commit, replace_arrival_snapshot=commit, refresh_split_pending_snapshot=commit,
        replace_arrival_stats_sheet=sheet,
        archive_arrival_stats_sheet=lambda resource, records, day: sheet(resource,"archive",records,day))
    ports = replace(ports, **port_overrides)
    context = CoreBrokerInvocationContext(automation_id="isolated-arrival", plugin_version="1.1.0",
        tool_name="sync_arrival_stats_v2", operation="service.invoke", action="run", role="__system__",
        account_bindings={"arrival_stats_tms": (_ACCOUNT,)}, resource_bindings={
            f"arrival_stats_{suffix}_sheet": f"isolated-{suffix}-table" for suffix in ("primary","secondary","pending","archive","split_pending")},
        mark_write_started=lambda: calls.append("write-started"))
    reviewed = build_first_party_core_handler_map(ports)
    registry = ConnectorRegistry(build_arrival_connectors(reviewed))
    context = replace(context,connector_binding_resolver=lambda requirement: _binding(registry,requirement.service,context))
    return registry, context, row, calls, writes


def _binding(registry, service, context):
    descriptor = registry.resolve(service)
    if descriptor.binding_kind is ConnectorBindingKind.ACCOUNT:
        return ConnectorBindingRef(service,"arrival_stats_tms",_ACCOUNT,"ronghui",context)
    if descriptor.binding_kind is ConnectorBindingKind.RESOURCE:
        role = descriptor.resource_role
        return ConnectorResourceBindingRef(service,role,context.resource_bindings[role],"feishu_sheet",context)
    return ConnectorHostInternalBindingRef(service,context)


def _invoke(registry, context, suffix, operation, arguments):
    service = f"connector.boyi.{suffix}@1"
    return asyncio.run(registry.invoke(resolved=registry.require_operation(service,operation),
        binding=_binding(registry,service,context), arguments=arguments))


@pytest.mark.parametrize("dry_run", [True, False])
def test_real_arrival_algorithm_reads_through_v2_connectors_with_actual_piece_counts(dry_run):
    registry, context, row, calls, writes = _setup()
    path = _ROOT / "agent/service_v2_plugins/sync_arrival_stats_v2/payload/plugin.py"
    spec = importlib.util.spec_from_file_location("isolated_arrival_connector_adapter",path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    class Receipt(dict):
        pass
    receipts = []
    def host(_operation, *, action, role, arguments):
        assert _operation == "service.invoke" and role == "__system__"
        service = arguments["service"]
        result = asyncio.run(registry.invoke(resolved=registry.require_operation(service,action),
            binding=_binding(registry,service,context),arguments=arguments["arguments"]))
        receipt = Receipt(result)
        receipts.append((service, action))
        receipt.host_evidence_ref = f"isolated-receipt-{len(receipts)}"
        return receipt
    def broker(operation, **kwargs):
        return adapter.service_invoke_adapter(host,operation,**kwargs)
    result = load_first_party_action("sync_arrival_stats").run_action({"target_date":_DATE,"dry_run":dry_run},broker)
    assert result["status"] == "SUCCESS"
    assert result["data"]["records"] == 1
    assert result["data"]["count_result"]["child_scan_rows"] == 1
    if dry_run:
        assert writes == [] and "write-started" not in calls
    else:
        output = [item for item in writes if item[0] == "isolated-primary-table"]
        assert len(output) == 1
        assert output[0][2][0]["tracking_number"] == row["tracking_number"]
        assert output[0][2][0]["arrived_quantity"] == 1
        assert output[0][2][0]["quantity"] == 2
        assert result["data"]["evidence"]["execution_result"] == "all_required_outputs_committed"
    assert all(call[0] == _ACCOUNT for call in calls if call != "write-started")


def test_sheet_connector_preserves_exact_resource_and_verified_write_result():
    registry, context, row, calls, writes = _setup()
    result = _invoke(registry,context,"arrival_stats_primary_sheet","replace",{
        "resource_slot":"arrival_stats_primary","records":[{**row,"arrived_quantity":1}],"target_date":_DATE})
    assert result == {"resource_slot":"arrival_stats_primary","record_count":1,"committed":True,"verified":True}
    assert writes[0][0:2] == ("isolated-primary-table","stats")
    assert writes[0][2][0]["arrived_quantity"] == 1
    # The outer service broker owns the write receipt. The primitive must not
    # start it again; the actual socket/bwrap test checks the outer receipt.
    assert calls == []
    assert "evidence_ref" not in result


def test_changed_account_binding_fails_before_source_or_write():
    registry, context, _, calls, writes = _setup()
    service = "connector.boyi.arrival_stats_tms@1"
    bad = replace(_binding(registry,service,context),account_id="other-account")
    with pytest.raises(ConnectorBindingInvalid):
        asyncio.run(registry.invoke(resolved=registry.require_operation(service,"scan_read_page"),
            binding=bad,arguments={"target_date":_DATE,"page_size":100}))
    assert calls == [] and writes == []


def test_missing_host_context_cannot_call_production_primitive():
    registry, context, _, calls, writes = _setup()
    service = "connector.boyi.arrival_stats_projection@1"
    with pytest.raises(ConnectorBindingInvalid):
        asyncio.run(registry.invoke(resolved=registry.require_operation(service,"scan_cleanup"),
            binding=ConnectorHostInternalBindingRef(service), arguments={"retention_days":30}))
    assert calls == [] and writes == []


def test_unknown_record_fields_fail_before_writing():
    registry, context, row, calls, writes = _setup()
    with pytest.raises(ConnectorInvocationError):
        _invoke(registry,context,"arrival_stats_primary_sheet","replace",{
            "resource_slot":"arrival_stats_primary","records":[{**row,"arrived_quantity":1,"unknown":True}],"target_date":_DATE})
    assert calls == [] and writes == []


def test_external_write_without_verified_readback_keeps_unknown_outcome():
    registry, context, row, calls, _ = _setup(replace_arrival_stats_sheet=lambda *_: {"ok":True,"record_count":1})
    with pytest.raises(ConnectorInvocationError) as failure:
        _invoke(registry,context,"arrival_stats_primary_sheet","replace",{
            "resource_slot":"arrival_stats_primary","records":[{**row,"arrived_quantity":1}],"target_date":_DATE})
    assert failure.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert calls == []


def test_expired_platform_session_is_reported_as_login_required():
    from agent.tms_runtime.errors import TMSAuthStateError
    def expired(*_):
        raise TMSAuthStateError("AUTH_REQUIRED", "isolated expired login")
    registry, context, _, _, writes = _setup(scan_read_page=expired)
    with pytest.raises(ConnectorInvocationError) as failure:
        _invoke(registry,context,"arrival_stats_tms","scan_read_page",{"target_date":_DATE,"page_size":100})
    assert failure.value.code == "BLOCKED_LOGIN"
    assert writes == []


def test_actual_host_broker_supplies_the_private_invocation_context_to_production_connector():
    registry, context, _, calls, writes = _setup()
    service = "connector.boyi.arrival_stats_tms@1"
    manifest = _manifest(requires=[{"service":service,"account_role":"arrival_stats_tms"}],
        account_role="arrival_stats_tms",allowed_systems=("ronghui",))
    handlers = build_service_v2_capability_handler_map(_Orchestration(manifest),connector_registry=registry)
    adapter = RegisteredCoreAutomationBrokerAdapter(handlers=handlers,
        account_resolver=_AccountResolver(system="ronghui"),connector_registry=registry)
    grant = _grant(account_role="arrival_stats_tms",allowed_systems=("ronghui",),
        account_bindings={"arrival_stats_tms":_ACCOUNT})
    permission = dict(grant.runtime_permissions)
    permission["broker_operations"] = [{**permission["broker_operations"][0],"action":"scan_read_page"}]
    grant = replace(grant,runtime_permissions=permission)
    result = asyncio.run(adapter.invoke(grant=grant,operation="service.invoke",action="scan_read_page",
        role="__system__",binding=None,arguments={"service":service,"operation":"scan_read_page",
            "arguments":{"target_date":_DATE,"page_size":100}}))
    assert result["pagination_complete"] is True
    assert result["items"][0]["bill_code"] == "R123456789010001"
    assert writes == [] and calls[0][0] == _ACCOUNT
