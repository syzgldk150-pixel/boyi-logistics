"""Original mixed-write receipts retain truthful outcomes and exact conflicts."""
from copy import deepcopy
import asyncio
import hashlib
import json
import os
from uuid import uuid4

import pytest

from agent.orchestration.execution_resources import execution_keys_conflict
from shared.orchestration_repository_support import _json_hash
from shared.execution_resource_journal import (
    closed_execution_keys, historical_receipt_execution_keys, historical_receipt_execution_scope, unknown_execution_keys,
)
from tests.test_legacy_unknown_scope_migration_mysql import database as database
from tests.test_legacy_unknown_scope_migration_mysql import _projection_runner
from tests.test_workflow_runner_durable_admission import _command
from agent.orchestration.workflow_runner import _ResourceWait


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _case():
    physical = ("physical-write", "feishu_bitable", _hash("original-base"), _hash("original-table"))
    keys = closed_execution_keys([
        ("account-write", "ronghui-read-account"), physical,
        ("resource-write", "ronghui-read-account", "sync_delivery_status",
         "delivery_status_bitable", "original-resource", "INTERNAL_PROJECTION_WRITE"),
    ])
    row = {
        "operation": "network.request", "action": "feishu.bitable.write_records",
        "automation_id": "delivery_status",
        "runtime_metadata_json": {"resource_bindings": {"delivery_status_bitable": "original-resource"}},
        "target_ref_json": {
            "operation": "network.request", "action": "feishu.bitable.write_records",
            "automation_id": "delivery_status", "role_sha256": _hash("delivery_status_bitable"),
            "binding_sha256": _hash("original-resource"),
        },
    }
    row["runtime_metadata_sha256"] = _json_hash(row["runtime_metadata_json"])
    row["target_ref_sha256"] = _json_hash(row["target_ref_json"])
    return row, keys, physical


def test_single_original_resource_proof_preserves_physical_and_generic_conflicts():
    row, keys, physical = _case()
    before = deepcopy(row)
    scoped = historical_receipt_execution_keys(row, keys)
    assert historical_receipt_execution_scope(row, keys) == (scoped, "ORIGINAL_RESOURCE_SCOPE_DERIVED")
    assert scoped == tuple(sorted((physical, ("account-resource", "ronghui-read-account", *physical[1:]))))
    assert row == before
    assert physical in scoped
    assert any(execution_keys_conflict(physical, key) for key in scoped)
    parent = (*physical[:3], "*")
    assert any(execution_keys_conflict(parent, key) for key in scoped)
    generic = ("account-write", "ronghui-read-account")
    assert any(execution_keys_conflict(generic, key) for key in scoped)
    assert any(execution_keys_conflict(key, generic) for key in scoped)
    unrelated = (*physical[:3], _hash("other-table"))
    assert not any(execution_keys_conflict(unrelated, key) for key in scoped)


@pytest.mark.parametrize("change", [
    "unknown_action", "unknown_operation", "missing_lease", "multiple_resource_keys",
    "wrong_role", "wrong_binding", "wrong_project", "wrong_locator_action",
    "missing_physical", "multiple_physical", "wrong_kind", "invalid_physical",
    "missing_resource_key", "wrong_resource_key", "malformed_bindings", "malformed_target",
    "metadata_integrity", "target_integrity",
])
def test_incomplete_original_proof_cannot_shrink_scope(change):
    row, keys, physical = _case()
    if change == "unknown_action":
        row["action"] = "unknown.write"
    elif change == "unknown_operation":
        row["operation"] = "browser.invoke"
    elif change == "missing_lease":
        row["runtime_metadata_json"]["resource_bindings"] = None
    elif change == "multiple_resource_keys":
        key = next(key for key in keys if key[0] == "resource-write")
        keys = (*keys, (key[0], "another-account", *key[2:]))
    elif change in {"wrong_role", "wrong_binding", "wrong_project", "wrong_locator_action"}:
        field = {"wrong_role": "role_sha256", "wrong_binding": "binding_sha256",
                 "wrong_project": "automation_id", "wrong_locator_action": "action"}[change]
        row["target_ref_json"][field] = "mismatch"
    elif change == "missing_physical":
        keys = tuple(key for key in keys if key != physical)
    elif change == "multiple_physical":
        keys = (*keys, (*physical[:3], _hash("another-table")))
    elif change in {"wrong_kind", "invalid_physical"}:
        replacement = ("physical-write", "feishu_sheet", *physical[2:]) if change == "wrong_kind" else physical[:3]
        keys = tuple(replacement if key == physical else key for key in keys)
    elif change == "missing_resource_key":
        keys = tuple(key for key in keys if key[0] != "resource-write")
    elif change == "wrong_resource_key":
        row["runtime_metadata_json"]["resource_bindings"] = {"other-role": "other-resource"}
        row["target_ref_json"].update(role_sha256=_hash("other-role"), binding_sha256=_hash("other-resource"))
    elif change == "malformed_bindings":
        row["runtime_metadata_json"]["resource_bindings"] = "{broken"
    elif change == "malformed_target":
        row["target_ref_json"] = "{broken"
    row["runtime_metadata_sha256"] = _json_hash(row["runtime_metadata_json"])
    row["target_ref_sha256"] = _json_hash(row["target_ref_json"])
    if change == "metadata_integrity":
        row["runtime_metadata_sha256"] = _hash("mismatch")
    if change == "target_integrity":
        row["target_ref_sha256"] = _hash("mismatch")
    assert historical_receipt_execution_keys(row, keys) == keys
    reasons = {
        "unknown_action": "ACTION_SCOPE_UNREVIEWED", "unknown_operation": "ACTION_SCOPE_UNREVIEWED",
        "missing_lease": "ORIGINAL_BINDINGS_UNAVAILABLE", "malformed_bindings": "ORIGINAL_BINDINGS_UNAVAILABLE",
        "malformed_target": "TARGET_LOCATOR_UNAVAILABLE", "metadata_integrity": "LEASE_METADATA_DIGEST_MISMATCH",
        "target_integrity": "TARGET_LOCATOR_DIGEST_MISMATCH", "wrong_role": "TARGET_BINDING_NOT_UNIQUE",
        "wrong_binding": "TARGET_BINDING_NOT_UNIQUE", "wrong_project": "TARGET_IDENTITY_MISMATCH",
        "wrong_locator_action": "TARGET_IDENTITY_MISMATCH", "missing_resource_key": "ORIGINAL_RESOURCE_KEY_NOT_UNIQUE",
        "wrong_resource_key": "ORIGINAL_RESOURCE_KEY_NOT_UNIQUE", "multiple_resource_keys": "ORIGINAL_RESOURCE_KEY_NOT_UNIQUE",
        "missing_physical": "ORIGINAL_PHYSICAL_SCOPE_NOT_SINGLE", "multiple_physical": "ORIGINAL_PHYSICAL_SCOPE_NOT_SINGLE",
        "wrong_kind": "ORIGINAL_PHYSICAL_SCOPE_INVALID", "invalid_physical": "ORIGINAL_PHYSICAL_SCOPE_INVALID",
    }
    assert historical_receipt_execution_scope(row, keys) == (keys, reasons[change])


@pytest.mark.parametrize("condition", [
    "ingress_only", "business_resource", "missing_descriptor", "duplicate_declaration",
    "missing_operations", "malformed_operation", "read_operation", "write_operation", "mixed_kinds",
])
def test_only_declared_non_callable_ingress_bindings_can_be_ignored(condition):
    row, keys, physical = _case()
    metadata = row["runtime_metadata_json"]
    # Deliberately not named webhook_route: authority comes from the original
    # declaration and call graph, never a role-name heuristic.
    metadata["resource_bindings"]["extra"] = "original-extra-binding"
    descriptor = {"resource_roles": [{"role": "extra", "allowed_kinds": ["webhook_route"]}],
                  "runtime_permissions": {"broker_operations": []}}
    metadata["runtime_descriptor"] = descriptor
    if condition == "business_resource":
        descriptor["resource_roles"][0]["allowed_kinds"] = ["feishu_bitable"]
    elif condition == "missing_descriptor":
        metadata.pop("runtime_descriptor")
    elif condition == "duplicate_declaration":
        descriptor["resource_roles"] *= 2
    elif condition == "missing_operations":
        descriptor["runtime_permissions"].clear()
    elif condition == "malformed_operation":
        descriptor["runtime_permissions"]["broker_operations"] = [{"roles": "extra"}]
    elif condition in {"read_operation", "write_operation"}:
        descriptor["runtime_permissions"]["broker_operations"] = [{
            "operation": "network.request", "action": "synthetic.action", "roles": ["extra"],
            "effect": "read" if condition == "read_operation" else "write",
        }]
    elif condition == "mixed_kinds":
        descriptor["resource_roles"][0]["allowed_kinds"].append("feishu_bitable")
    row["runtime_metadata_sha256"] = _json_hash(metadata)
    scoped, reason = historical_receipt_execution_scope(row, keys)
    if condition != "ingress_only":
        assert (scoped, reason) == (keys, "ADDITIONAL_BINDING_SCOPE_UNPROVEN")
        return
    assert reason == "ORIGINAL_RESOURCE_SCOPE_DERIVED" and physical in scoped
    assert not any(key[0] == "account-write" for key in scoped)
    second_physical = (*physical[:3], _hash("another-table"))
    assert historical_receipt_execution_keys(row, (*keys, second_physical)) == (*keys, second_physical)


def test_receipt_target_b_cannot_inherit_the_only_physical_key_from_business_resource_a():
    row, keys, physical = _case()
    metadata = row["runtime_metadata_json"]
    metadata["resource_bindings"]["resource_a"] = "parseable-resource-a"
    metadata["runtime_descriptor"] = {
        "resource_roles": [
            {"role": role, "allowed_kinds": ["feishu_bitable"]}
            for role in ("resource_a", "delivery_status_bitable")
        ],
        "runtime_permissions": {"broker_operations": [{
            "operation": row["operation"], "action": row["action"], "effect": "write",
            "roles": ["resource_a", "delivery_status_bitable"],
        }]},
    }
    row["runtime_metadata_sha256"] = _json_hash(metadata)
    physical_a = (*physical[:2], _hash("only-a-base"), _hash("only-a-table"))
    keys = tuple(physical_a if key == physical else key for key in keys) + (
        ("resource-write", "ronghui-read-account", "sync_delivery_status", "resource_a",
         "parseable-resource-a", "INTERNAL_PROJECTION_WRITE"),
    )
    # The locator still identifies B and its exact resource-write key, while
    # only A yielded a physical scope. The original account protection stays.
    assert historical_receipt_execution_scope(row, keys) == (keys, "ADDITIONAL_BINDING_SCOPE_UNPROVEN")


def _production_case():
    """Use the actual release compiler, Broker locator and repository JSON format."""
    from agent.automation_plugins.broker import _extract_write_target_ref
    from shared.orchestration_repository_support import _json_param
    from tests.test_automation_plugin_release_generation_stability import _build_release_world

    world = _build_release_world()
    snapshot = world.snapshots["delivery_status"]
    metadata = snapshot.execution_metadata
    assert set(metadata["resource_bindings"]) == {"webhook_route", "delivery_status_bitable"}
    role = "delivery_status_bitable"
    resource = metadata["resource_bindings"][role]
    account = metadata["account_bindings"]["account_id"]
    assert isinstance(account, str)
    target, target_hash = _extract_write_target_ref(
        automation_id=snapshot.automation_id, plugin_id=snapshot.plugin_id,
        operation="network.request", action="feishu.bitable.write_records", role=role,
        binding=resource, request_id=str(uuid4()), arguments={"records": []},
    )
    physical = ("physical-write", "feishu_bitable", _hash("original-base"), _hash("original-table"))
    keys = closed_execution_keys([
        ("account-write", account), physical,
        ("resource-write", account, snapshot.plugin_id, role, resource, "INTERNAL_PROJECTION_WRITE"),
        ("resource-write", account, snapshot.plugin_id, "webhook_route",
         metadata["resource_bindings"]["webhook_route"], "INTERNAL_PROJECTION_WRITE"),
    ])
    row = {"automation_id": snapshot.automation_id, "operation": target["operation"], "action": target["action"],
           "runtime_metadata_json": _json_param(metadata, {}), "runtime_metadata_sha256": _json_hash(metadata),
           "target_ref_json": _json_param(target, {}), "target_ref_sha256": target_hash}
    return row, keys, physical


def test_real_production_snapshot_route_binding_and_broker_locator_survive_storage_serialization():
    row, keys, physical = _production_case()
    before = deepcopy(row)
    result, reason = historical_receipt_execution_scope(row, keys)
    assert reason == "ORIGINAL_RESOURCE_SCOPE_DERIVED" and physical in result
    assert {key[0] for key in result} == {"physical-write", "account-resource"}
    assert row == before


def test_parent_write_requires_captured_parent_scope():
    row, keys, physical = _case()
    row["action"] = row["target_ref_json"]["action"] = "feishu.sheet.add"
    row["target_ref_sha256"] = _json_hash(row["target_ref_json"])
    sheet = ("physical-write", "feishu_sheet", *physical[2:])
    keys = tuple(sheet if key == physical else key for key in keys)
    assert historical_receipt_execution_keys(row, keys) == keys
    parent = (*sheet[:3], "*")
    keys = tuple(parent if key == sheet else key for key in keys)
    assert parent in historical_receipt_execution_keys(row, keys)
    assert ("account-write", "ronghui-read-account") not in historical_receipt_execution_keys(row, keys)


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8")
def test_real_mysql_scope_projection_never_changes_receipt_or_resolves_old_run(database):
    row, keys, physical = _case()
    old = database.seed(keys=keys)
    row["automation_id"] = row["target_ref_json"]["automation_id"] = database.project_id
    row["target_ref_sha256"] = _json_hash(row["target_ref_json"])
    metadata = {"resource_bindings": row["runtime_metadata_json"]["resource_bindings"]}
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE automation_project_generation_leases SET runtime_metadata_json=%s,runtime_metadata_sha256=%s WHERE lease_id=%s",
                       (json.dumps(metadata), _json_hash(metadata), old["lease_id"]))
        cursor.execute("UPDATE automation_write_attempt_receipts SET operation=%s,action=%s,target_ref_json=%s,target_ref_sha256=%s WHERE receipt_id=%s",
                       (row["operation"], row["action"], json.dumps(row["target_ref_json"]), row["target_ref_sha256"], old["receipt_id"]))
        connection.commit()
    before = database.snapshot()
    assert unknown_execution_keys(database.repository) == historical_receipt_execution_keys(row, keys)
    assert database.snapshot() == before, "read-only projection must preserve UNKNOWN, raw scopes and all Run evidence"
    runner = _projection_runner(database)
    command = _command("new-scan-admission", account="ronghui-read-account")
    plan = runner._planner.plan(command, runner._context_builder.build(command))
    base = runner._catalog.get_capability(plan.steps[0].tool_name)
    runtime = {
        "account_bindings": {"account_id": "ronghui-read-account"}, "resource_bindings": {},
        "runtime_permissions": {"broker_operations": [{
            "operation": "browser.invoke", "action": "ronghui.scan_next.submit",
            "roles": ["account_id"], "effect": "write",
        }]},
    }
    async def check_admission():
        # Actual Runner admission reads the unresolved real-MySQL receipt.
        # This verifies lock behavior, not a fabricated TMS business result.
        slot = await runner._acquire_execution_slot(plan.steps[0], plan, {**base, "_plugin_runtime": runtime})
        slot()
        alias_runtime = deepcopy(runtime)
        alias_runtime["resource_bindings"] = {"target": "resource-alias"}
        alias_runtime["runtime_permissions"]["broker_operations"] = [{
            "operation": "network.request", "action": "feishu.bitable.write_records",
            "roles": ["target"], "effect": "write",
        }]
        runner._saved_resource_provider = lambda identity: {
            "resource_kind": "feishu_bitable", "base_token": "original-base", "table_id": "original-table",
            "_meta": {"resource_key": identity, "configuration_version": 1, "config_sha256": _hash("saved")},
        }
        with pytest.raises(_ResourceWait):
            await runner._acquire_execution_slot(plan.steps[0], plan, {**base, "_plugin_runtime": alias_runtime})
        with pytest.raises(_ResourceWait):
            await runner._acquire_execution_slot(plan.steps[0], plan, base)
    asyncio.run(check_admission())
    assert database.snapshot() == before
    # Removing the exact original lease metadata prevents scope narrowing.
    with database.helper._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE automation_project_generation_leases SET runtime_metadata_json='{}' WHERE lease_id=%s",
                       (old["lease_id"],))
        connection.commit()
    assert unknown_execution_keys(database.repository) == keys
