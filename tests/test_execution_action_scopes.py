"""Action scopes use reviewed host handlers, real admission and real Broker markers."""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party import _ACTION_RESOURCE_ROLES, _FIRST_PARTY_BROKER_ACTIONS
from agent.orchestration.execution_resources import (
    EXECUTION_ACTION_SCOPES, canonical_resource_write_locks, execution_keys_conflict,
)
from agent.orchestration.models import OperationType
from agent.orchestration.workflow_runner import _ResourceWait
from shared.execution_resource_journal import EXECUTION_RESOURCE_KEYS
from tests.test_workflow_runner_release_hold import _ClaimRepository, _plan, _runner, _step


def capability(plugin="sync_delivery_status", *, account="account-a", parent="book-a"):
    operations = [{"operation": item.operation, "action": item.action,
        "roles": list(item.roles), "effect": item.effect}
        for item in _FIRST_PARTY_BROKER_ACTIONS[plugin]]
    resource_roles = {role for operation in operations for role in operation["roles"] if role != "account_id"}
    resources = {role: role for role in resource_roles}
    saved = {role: {
        "resource_kind": "feishu_bitable" if "bitable" in role else "feishu_sheet",
        "base_token": parent, "table_id": role,
        "spreadsheet_token": parent, "sheet_id": role,
        "_meta": {"resource_key": role, "configuration_version": 1, "config_sha256": "a" * 64},
    } for role in resources}
    return {"_plugin_runtime": {
        "plugin_id": plugin, "account_bindings": {"account_id": account},
        "resource_bindings": resources,
        "resource_roles": [dict(item) for item in _ACTION_RESOURCE_ROLES.get(plugin, ())],
        "runtime_permissions": {"browser": True, "network": True, "office": False,
            "max_broker_calls": 20, "broker_operations": operations},
    }}, saved


def scopes(cap, saved):
    actions = {}
    accounts = set(cap["_plugin_runtime"]["account_bindings"].values())
    keys, bounded = canonical_resource_write_locks(cap, accounts, saved.get, action_scopes=actions)
    return keys, bounded, actions


@pytest.mark.parametrize("plugin", ["sync_delivery_status", "sync_scan_codes", "sync_arrival_stats"])
def test_real_first_party_mixed_contracts_have_only_actual_write_scopes(plugin):
    cap, saved = capability(plugin)
    keys, bounded, actions = scopes(cap, saved)
    assert bounded and keys and not any(key[0] == "account-write" for key in keys)
    writes = {(item["operation"], item["action"], role)
        for item in cap["_plugin_runtime"]["runtime_permissions"]["broker_operations"]
        if item["effect"] == "write" for role in item["roles"]}
    assert set(actions) == writes
    assert all(set(value).issubset(keys) for value in actions.values())
    if plugin == "sync_delivery_status":
        assert ("projection-write", "waybills") in keys
        assert all(key[0] in {"physical-write", "account-resource"}
            for key in actions[("network.request", "feishu.bitable.write_records", "delivery_status_bitable")])
    if plugin == "sync_scan_codes":
        assert actions[("browser.invoke", "ronghui.scan_next.submit", "account_id")] == (("browser-write", "account-a"),)


def test_real_runner_admission_separates_stopped_feishu_from_scan_and_protects_global_projection():
    async def exercise():
        runner = _runner(_ClaimRepository())
        delivery, saved = capability()
        scan, _ = capability("sync_scan_codes")
        runner._saved_resource_provider = saved.get
        step = _step(operation=OperationType.EXTERNAL_WRITE, account_id="account-a")
        _keys, _bounded, actions = scopes(delivery, saved)
        old_feishu_keys = actions[("network.request", "feishu.bitable.write_records", "delivery_status_bitable")]
        runner._repository.get_unknown_execution_resource_keys = lambda: old_feishu_keys
        admitted = await runner._acquire_execution_slot(step, _plan(step), scan)
        admitted()
        with pytest.raises(_ResourceWait, match="unresolved external write"):
            await runner._acquire_execution_slot(step, _plan(step), delivery)
        with pytest.raises(_ResourceWait, match="unresolved external write"):
            await runner._acquire_execution_slot(step, _plan(step), {})
        runner._repository.get_unknown_execution_resource_keys = lambda: (("projection-write", "waybills"),)
        other, other_saved = capability(account="account-b", parent="independent-book")
        runner._saved_resource_provider = other_saved.get
        other_step = _step(operation=OperationType.EXTERNAL_WRITE, account_id="account-b")
        with pytest.raises(_ResourceWait, match="unresolved external write"):
            await runner._acquire_execution_slot(other_step, _plan(other_step), other)
        assert not runner._execution_locks
    asyncio.run(exercise())


def test_browser_scope_uses_only_the_write_role_and_generic_writers_still_conflict_both_orders():
    cap, saved = capability("sync_scan_codes")
    cap["_plugin_runtime"]["account_bindings"]["readonly"] = "account-read"
    keys, bounded, _ = scopes(cap, saved)
    assert bounded and ("browser-write", "account-read") not in keys
    for key in keys:
        if key[0] == "projection-write":
            continue
        assert execution_keys_conflict(key, ("account-write", "account-a"))
        assert execution_keys_conflict(("account-write", "account-a"), key)
        assert not execution_keys_conflict(key, ("account-write", "account-read"))


@pytest.mark.parametrize("change", ["unknown", "dynamic", "missing", "wrong-kind"])
def test_unproven_actions_remain_conservative_without_losing_known_scopes(change):
    cap, saved = capability()
    runtime = cap["_plugin_runtime"]
    operation = next(item for item in runtime["runtime_permissions"]["broker_operations"] if item["action"] == "feishu.bitable.write_records")
    if change == "unknown":
        operation["action"] = "custom.write"
    elif change == "dynamic":
        operation["dynamic_effect"] = True
    elif change == "missing":
        saved.clear()
    else:
        saved["delivery_status_bitable"]["resource_kind"] = "feishu_sheet"
    _keys, bounded, actions = scopes(cap, saved)
    assert not bounded and ("network.request", operation["action"], "delivery_status_bitable") not in actions
    runner = _runner(_ClaimRepository())
    runner._saved_resource_provider = saved.get
    step = _step(operation=OperationType.EXTERNAL_WRITE, account_id="account-a")
    assert ("account-write", "account-a") in runner._execution_lock_keys(step, _plan(step), cap)


def test_unknown_projection_conflicts_across_accounts_and_known_table_writers_share_domains():
    cap, saved = capability("sync_scan_codes")
    cap["_plugin_runtime"]["runtime_permissions"]["broker_operations"][1]["action"] = "custom.projection.write"
    keys, bounded, _ = scopes(cap, saved)
    assert not bounded and ("projection-write", "*") in keys
    assert execution_keys_conflict(("projection-write", "*"), ("projection-write", "waybills"))
    assert execution_keys_conflict(("projection-write", "waybills"), ("projection-write", "*"))
    for action in ("waybill.delivery_status.update", "waybill.yunda.replace_date"):
        cap["_plugin_runtime"]["runtime_permissions"]["broker_operations"] = [{
            "operation": "projection.invoke", "action": action, "effect": "write", "roles": ["account_id"],
        }]
        scoped, bounded, _ = scopes(cap, saved)
        assert bounded and ("projection-write", "waybills") in scoped


def test_first_party_unbound_optional_sheet_does_not_block_stats_and_broker_cannot_call_it(tmp_path):
    cap, saved = capability("sync_arrival_stats")
    runtime = cap["_plugin_runtime"]
    role = "arrival_stats_pending_sheet"
    assert next(item for item in runtime["resource_roles"] if item["role"] == role)["required"] is False
    runtime["resource_bindings"].pop(role)
    saved.pop(role)
    keys, bounded, actions = scopes(cap, saved)
    assert bounded and ("network.request", "feishu.sheet.replace", role) not in actions
    assert not any(key[0] == "account-write" for key in keys)
    delivery, delivery_saved = capability()
    _, _, delivery_actions = scopes(delivery, delivery_saved)
    old_feishu = delivery_actions[("network.request", "feishu.bitable.write_records", "delivery_status_bitable")]

    async def admit():
        runner = _runner(_ClaimRepository())
        runner._saved_resource_provider = saved.get
        runner._repository.get_unknown_execution_resource_keys = lambda: old_feishu
        step = _step(operation=OperationType.INTERNAL_PROJECTION_WRITE, account_id="account-a")
        slot = await runner._acquire_execution_slot(step, _plan(step), cap)
        assert set(slot.resource_keys) == keys
        slot()
    asyncio.run(admit())

    receipts = []
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "broker.sock", write_attempt_recorder=receipts.append)
    token = issuer.issue(
        automation_id="synthetic-stats", plugin_version="1.0.0", tool_name="automation.synthetic-stats.run",
        ttl_seconds=60, runtime_permissions=runtime["runtime_permissions"],
        account_roles=({"role": "account_id"},), resource_roles=tuple(runtime["resource_roles"]),
        account_bindings=runtime["account_bindings"], resource_bindings=runtime["resource_bindings"],
        write_attempt_context={"automation_id": "synthetic-stats", "plugin_id": "sync_arrival_stats",
            "generation": 1, "lease_id": str(uuid4()), "orchestration_run_id": str(uuid4()), "step_id": str(uuid4())},
    )
    try:
        with pytest.raises(PluginExecutionError) as error:
            issuer.consume(token, request_id=str(uuid4()), operation="network.request",
                action="feishu.sheet.replace", role=role, arguments={"records": []})
        assert error.value.code == "BROKER_ROLE_UNBOUND"
        assert issuer.consumed_call_count(token) == issuer.started_mutating_call_count(token) == 0
        assert receipts == []
    finally:
        issuer.revoke(token)


@pytest.mark.parametrize("change", [
    "required", "undeclared", "duplicate", "account-role", "null-binding", "empty-binding",
    "unknown-action", "dynamic-action",
])
def test_only_proven_absent_optional_roles_can_be_omitted(change):
    cap, saved = capability("sync_arrival_stats")
    runtime = cap["_plugin_runtime"]
    role = "arrival_stats_pending_sheet"
    declaration = next(item for item in runtime["resource_roles"] if item["role"] == role)
    runtime["resource_bindings"].pop(role)
    saved.pop(role)
    if change == "required":
        declaration["required"] = True
    elif change == "undeclared":
        runtime["resource_roles"].remove(declaration)
    elif change == "duplicate":
        runtime["resource_roles"].append(dict(declaration))
    elif change == "account-role":
        runtime["account_roles"] = [{"role": role}]
    elif change in {"null-binding", "empty-binding"}:
        runtime["resource_bindings"][role] = None if change == "null-binding" else ""
    else:
        operation = next(item for item in runtime["runtime_permissions"]["broker_operations"]
            if item["action"] == "feishu.sheet.replace")
        if change == "unknown-action":
            operation["action"] = "custom.write"
        else:
            operation["dynamic_effect"] = True
    _, bounded, _ = scopes(cap, saved)
    assert not bounded
    runner = _runner(_ClaimRepository())
    runner._saved_resource_provider = saved.get
    step = _step(operation=OperationType.INTERNAL_PROJECTION_WRITE, account_id="account-a")
    assert ("account-write", "account-a") in runner._execution_lock_keys(step, _plan(step), cap)


def issue_receipts(issuer, cap):
    runtime = cap["_plugin_runtime"]
    capability_token = issuer.issue(
        automation_id="synthetic-delivery", plugin_version="1.0.0", tool_name="automation.synthetic-delivery.run",
        ttl_seconds=60, runtime_permissions=runtime["runtime_permissions"],
        account_roles=({"role": "account_id"},),
        resource_roles=tuple({"role": role} for role in runtime["resource_bindings"]),
        account_bindings=runtime["account_bindings"], resource_bindings=runtime["resource_bindings"],
        write_attempt_context={"automation_id": "synthetic-delivery", "plugin_id": "sync_delivery_status",
            "generation": 1, "lease_id": str(uuid4()), "orchestration_run_id": str(uuid4()), "step_id": str(uuid4())},
    )
    try:
        for operation in runtime["runtime_permissions"]["broker_operations"]:
            if operation["effect"] != "write":
                continue
            for role in operation["roles"]:
                request_id = str(uuid4())
                arguments = ({"bill_codes": [], "status": "已签收"}
                    if operation["action"] == "waybill.delivery_status.update" else {"records": []})
                issuer.consume(capability_token, request_id=request_id, operation=operation["operation"],
                    action=operation["action"], role=role, arguments=arguments)
                issuer.mark_write_started_hook(capability_token, request_id=request_id)()
    finally:
        issuer.revoke(capability_token)


def test_real_broker_snapshots_admitted_action_scopes_and_rejects_unheld_keys(tmp_path):
    cap, saved = capability()
    keys, _bounded, actions = scopes(cap, saved)
    receipts = []
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "broker.sock", write_attempt_recorder=receipts.append)
    held = EXECUTION_RESOURCE_KEYS.set(tuple(sorted(keys)))
    scoped = EXECUTION_ACTION_SCOPES.set(actions)
    try:
        issue_receipts(issuer, cap)
        assert len(receipts) == 2
        for receipt in receipts:
            role = "delivery_status_bitable" if receipt["operation"] == "network.request" else "account_id"
            assert receipt["execution_resource_keys_json"] == [list(key) for key in actions[(receipt["operation"], receipt["action"], role)]]
        EXECUTION_RESOURCE_KEYS.set((("account-write", "unheld"),))
        with pytest.raises(PluginExecutionError, match="not held"):
            issue_receipts(issuer, cap)
        assert len(receipts) == 2
        EXECUTION_ACTION_SCOPES.set({})
        issue_receipts(issuer, cap)
        assert all(item["execution_resource_keys_json"] == [["account-write", "unheld"]] for item in receipts[2:])
    finally:
        EXECUTION_ACTION_SCOPES.reset(scoped)
        EXECUTION_RESOURCE_KEYS.reset(held)
