"""Direct business boundaries with real schemas and platform adapter contracts."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from agent.tool_registry import ToolRegistry
from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.tms_runtime.direct_business import DirectBusinessError, DirectBusinessService, business_payload
from agent.tms_runtime.finance_business import FinancePluginBusinessService
from agent.tms_runtime.receipts_query import query_receipts
from agent.tms_runtime.scripts import receipts_sync

ADMIN = {"actor_id": "isolated-admin", "roles": ["super_admin"]}


def make_service(**kwargs):
    kwargs.setdefault("invocation_service", DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False))
    return DirectBusinessService(**kwargs)


@pytest.fixture(scope="module")
def registry():
    return ToolRegistry()


def invoke(service, operation, params, principal=ADMIN):
    return asyncio.run(service.invoke(operation, params, principal=principal, request_id=str(uuid4())))


def test_tracking_calls_actual_registered_target_contract_without_invocation(registry):
    executor = AsyncMock(return_value=(200, {"ok": True, "data": {"type": "waybill", "waybill_no": "isolated-001"}}))
    service = make_service(registry=registry, target_executor=executor)
    result = invoke(service, "tracking_query", {"tracking_number": "isolated-001"})
    assert result == {"ok": True, "data": {"type": "waybill", "waybill_no": "isolated-001"}, "error": None}
    assert executor.call_args.args[0] == "tracking_query"
    assert executor.call_args.kwargs["wait_for_stop"] is True
    assert executor.call_args.args[1].params == {"tracking_number": "isolated-001"}
    assert "run_id" not in result and "invocation_id" not in result


@pytest.mark.parametrize("operation,params,principal,code", [
    ("tracking_query", {}, {}, "TRUSTED_CONSOLE_ACTOR_REQUIRED"),
    ("arbitrary-endpoint", {}, ADMIN, "BUSINESS_OPERATION_NOT_AVAILABLE"),
    ("receipts-audit", {"platform": "ronghui", "direction": "send", "result": "passed", "waybill_no": "R1"},
     {"actor_id": "staff", "roles": ["admin"]}, "BUSINESS_PERMISSION_REQUIRED"),
])
def test_missing_identity_unknown_target_and_write_permission_fail_before_transport(registry, operation, params, principal, code):
    executor = AsyncMock()
    service = make_service(registry=registry, target_executor=executor)
    with pytest.raises(DirectBusinessError) as failure:
        invoke(service, operation, params, principal)
    assert failure.value.code == code
    executor.assert_not_awaited()


def test_nested_platform_failure_is_not_empty_success(registry):
    service = make_service(registry=registry, target_executor=AsyncMock(return_value=(200,
        {"ok": True, "data": {"ok": False, "error": "login expired", "error_code": "AUTH_REQUIRED"}})))
    with pytest.raises(DirectBusinessError, match="login expired"):
        invoke(service, "tracking_query", {"tracking_number": "R1"})


@pytest.mark.parametrize("verification,accepted", [
    ({}, False),
    ({"verified": True, "audit_status": "2", "waybill_no": "OTHER", "external_id": "E1", "observed_at": "2026-09-09T00:00:00Z"}, False),
    ({"verified": True, "audit_status": "2", "waybill_no": "R1", "external_id": "E1", "observed_at": "2026-09-09T00:00:00Z"}, True),
])
def test_receipt_write_accepts_only_matching_independent_readback(registry, verification, accepted):
    seen = []
    async def call_business(**kwargs):
        seen.append(kwargs)
        return {"success": True, "status": "COMPLETED", "invocation_id": "isolated-call",
                "result": dict(await kwargs["handler"]())}
    service = make_service(registry=registry,
        invocation_service=SimpleNamespace(call_business=call_business),
        target_executor=AsyncMock(return_value=(200, {"ok": True,
            "data": {"verification": verification, "audit_status": "已审核"}})))
    args = {"platform": "ronghui", "direction": "send", "result": "passed", "waybill_no": "R1"}
    if accepted:
        result = invoke(service, "receipts-audit", args)
        assert result["ok"] and result["data"]["verification"]["verified"]
    else:
        with pytest.raises(DirectBusinessError, match="read-back"):
            invoke(service, "receipts-audit", args)
    assert seen[0]["arguments"] == args and seen[0]["write"] is True


def test_customer_read_schema_does_not_allow_write_action_injection(registry):
    executor = AsyncMock()
    service = make_service(registry=registry, target_executor=executor)
    with pytest.raises(ValueError):
        invoke(service, "customer-service-query", {"platform": "yunda", "account_id": "isolated-yunda", "action": "reply"})
    executor.assert_not_awaited()


def source_rows(params, *, platform, direction):
    records = [{"platform": platform, "direction": direction, "waybill_no": f"{platform}-{index}",
                "receipt_no": f"receipt-{index}", "remote_updated_at": f"2026-09-09T00:00:0{index}",
                "audit_status": "待审核", "photo_count": index % 2} for index in range(3)]
    return platform, direction, records, {"total": 3, "fetched": 3, "truncated": False}, []


def test_receipt_live_query_paginates_current_source_without_archiving():
    with patch("agent.tms_runtime.receipts_query.resolve_account_params", return_value={"session_profile": "isolated"}), \
         patch.object(receipts_sync, "_fetch_source", side_effect=source_rows):
        result = query_receipts({"platform": "all", "date_from": "2026-09-09", "date_to": "2026-09-09", "page": 2, "page_size": 2})
    assert result["complete"] is True
    assert result["pagination"]["total"] == 6 and len(result["rows"]) == 2
    assert {row["waybill_no"] for row in result["rows"]} == {"ronghui-1", "yunda-1"}


@pytest.mark.parametrize("stats,warnings", [({"total": 3, "fetched": 2}, []), ({"total": None, "fetched": 0}, []),
    ({"total": 0, "fetched": 0}, ["unavailable"]), ({"total": 0, "fetched": 0, "attachment_errors": 1}, [])])
def test_incomplete_receipts_never_masquerade_as_empty_or_complete(stats, warnings):
    with patch("agent.tms_runtime.receipts_query.resolve_account_params", return_value={}), \
         patch.object(receipts_sync, "_fetch_source", return_value=("ronghui", "send", [], stats, warnings)):
        with pytest.raises(DirectBusinessError, match="完整"):
            query_receipts({"platform": "ronghui", "date_from": "2026-09-09", "date_to": "2026-09-09"})


def test_receipt_source_explicit_rejection_is_not_empty_rows():
    with pytest.raises(ValueError, match="rejected"):
        receipts_sync._extract_rows({"success": False, "rows": [], "total": 0})


def test_waybill_partial_result_preserves_cached_rows_without_claiming_complete(registry):
    service = make_service(registry=registry)
    partial = {"ok": False, "complete": False, "data_status": "partial",
        "data": {"rows": [{"waybill_no": "cached-001"}], "total": 1, "snapshot": "local_database"},
        "errors": [{"source": "yunda", "code": "WAYBILL_SOURCE_SCOPE_UNVERIFIED"}], "publications": []}
    service.register_read("send-waybills-query", AsyncMock(return_value=partial))
    result = invoke(service, "send-waybills-query", {"source": "yunda"})
    assert result["ok"] is False and result["data"] == partial
    assert result["data"]["complete"] is False


def finance_policy():
    from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
    from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract

    source = Path(__file__).resolve().parents[1] / "service_v2_plugins/sync_finance_bills_v2/manifest.json"
    manifest = AutomationPluginManifestV2.from_mapping(json.loads(source.read_text(encoding="utf-8")))
    compiled = ServiceV2ProjectContract.from_manifest(manifest)
    target = SimpleNamespace(dynamic_argument_resolvers=dict(compiled.invocation_contracts["manual_run"]["dynamic_resolvers"]))
    return SimpleNamespace(_load_contract=lambda _: (SimpleNamespace(plugin_id=manifest.plugin_id, runtime_model="SERVICE_V2"),
        SimpleNamespace(invocation_contracts={"manual_run": target})),
        invoke_trusted_and_wait=AsyncMock(return_value={"success": True, "status": "COMPLETED", "result": {"data": {"verified": True}}}))


@pytest.mark.parametrize("params", [{"mode": "sync", "target_date": "2026-09-09", "rescan_days": 7},
    {"mode": "backfill", "start_date": "2026-09-01", "end_date": "2026-09-09"}, {"mode": "retry", "batch_id": 9}])
def test_finance_three_actions_resolve_installed_v2_contribution(params):
    policy = finance_policy()
    result = asyncio.run(FinancePluginBusinessService(policy, None)({**params, "automation_id": "finance-v2-instance"}, ADMIN, str(uuid4()), 300))
    assert result["ok"] and "run_id" not in result
    call = policy.invoke_trusted_and_wait.call_args
    assert call.args == ("finance-v2-instance",)
    assert call.kwargs["contribution_id"] == "manual_run"
    assert call.kwargs["trusted_context"] == {"dynamic_inputs": {"collection_request": params}}
    assert call.kwargs["actor"].actor_id == ADMIN["actor_id"]


@pytest.mark.parametrize("params", [{"mode": "sync", "account_id": "other"},
    {"mode": "backfill", "start_date": "2026-09-10", "end_date": "2026-09-01"},
    {"mode": "retry", "batch_id": True}, {"mode": "sync", "session_profile": "other"}])
def test_finance_rejects_account_override_and_invalid_dates_before_plugin(params):
    policy = finance_policy()
    with pytest.raises(DirectBusinessError):
        asyncio.run(FinancePluginBusinessService(policy, None)({**params, "automation_id": "finance-v2-instance"}, ADMIN, str(uuid4()), 300))
    policy.invoke_trusted_and_wait.assert_not_awaited()


def test_finance_old_static_contract_requires_real_plugin_upgrade():
    policy = finance_policy()
    entry, contract = policy._load_contract("finance_bills")
    contract.invocation_contracts["manual_run"].dynamic_argument_resolvers.clear()
    policy._load_contract = lambda _: (entry, contract)
    with pytest.raises(DirectBusinessError) as failure:
        asyncio.run(FinancePluginBusinessService(policy, None)({"mode": "sync", "automation_id": "finance-v2-instance"}, ADMIN, str(uuid4()), 300))
    assert failure.value.code == "FINANCE_PLUGIN_UPDATE_REQUIRED"
    policy.invoke_trusted_and_wait.assert_not_awaited()


@pytest.mark.parametrize("plugin_id,runtime_model", [("sync_finance_bills", "ACTION_V1"), ("other_v2", "SERVICE_V2")])
def test_finance_rejects_non_current_collector(plugin_id, runtime_model):
    policy = finance_policy()
    entry, contract = policy._load_contract("finance-v2-instance")
    entry.plugin_id, entry.runtime_model = plugin_id, runtime_model
    policy._load_contract = lambda _: (entry, contract)
    with pytest.raises(DirectBusinessError) as failure:
        asyncio.run(FinancePluginBusinessService(policy, None)(
            {"mode": "sync", "automation_id": "finance-v2-instance"}, ADMIN, str(uuid4()), 300))
    assert failure.value.code == "FINANCE_PLUGIN_REQUIRED"
    policy.invoke_trusted_and_wait.assert_not_awaited()


def test_finance_requires_explicit_collector_instance():
    policy = finance_policy()
    with pytest.raises(DirectBusinessError) as failure:
        asyncio.run(FinancePluginBusinessService(policy, None)({"mode": "sync"}, ADMIN, str(uuid4()), 300))
    assert failure.value.code == "FINANCE_PLUGIN_REQUIRED"
    policy.invoke_trusted_and_wait.assert_not_awaited()


def test_business_payload_surfaces_task_envelope_reason():
    with pytest.raises(DirectBusinessError) as caught:
        business_payload({"ok": False, "cost_sec": 0.8, "data": {
            "ok": False, "error_code": "YUNDA_AUDIT_SAVE_FAILED", "message": "无权限"}})
    assert (caught.value.code, str(caught.value)) == ("YUNDA_AUDIT_SAVE_FAILED", "无权限")
