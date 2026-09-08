from contextlib import nullcontext
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from shared.automation_write_recovery_views import list_work_item_unknown_writes


def repository(rows, attempts=()):
    calls = []
    cursor = SimpleNamespace(execute=lambda sql, params: calls.append((sql, params)),
                             fetchall=lambda: rows if len(calls) == 1 else attempts, description=[])
    connection = SimpleNamespace(cursor=lambda: nullcontext(cursor))
    return SimpleNamespace(unit_of_work=lambda: nullcontext(SimpleNamespace(connection=connection))), calls


def test_query_returns_each_exact_history_identity_without_config_or_resource_values():
    first = {"lease_id": "old-1", "automation_id": "stable-project", "generation": 1,
             "run_id": "old-run", "work_item_id": "work-1", "outcome": "WRITE_OUTCOME_UNKNOWN",
             "command_automation_id": "stable-project", "command_generation": 1,
             "plugin_id": "sync_daily_should_sign", "legacy_scope_unavailable": 1,
             "private_value": "must-not-return"}
    second = {**first, "lease_id": "old-2", "generation": 2, "command_generation": 2}
    repo, calls = repository([first, second])
    result = list_work_item_unknown_writes(repo, "work-1")
    assert [row["lease_id"] for row in result] == ["old-1", "old-2"]
    assert all(row["identity_valid"] and row["legacy_scope_unavailable"] for row in result)
    assert all("private_value" not in row and "command_generation" not in row for row in result)
    sql, params = calls[0]
    assert params == ("work-1",)
    assert "FOR UPDATE" not in sql and "config_json" not in sql and "target_ref_json" not in sql
    assert all(row["write_attempts"] == [] for row in result)


def test_excessive_history_is_explicit_failure_not_silent_truncation():
    repo, _ = repository([{}] * 101)
    with pytest.raises(ValueError, match="WORK_ITEM_RECOVERY_LIMIT_EXCEEDED"):
        list_work_item_unknown_writes(repo, "work-1")


def lease(**updates):
    return {"lease_id": "lease-1", "automation_id": "project-1", "generation": 1,
            "run_id": "run-1", "work_item_id": "work-1", "outcome": "WRITE_OUTCOME_UNKNOWN",
            "command_automation_id": "project-1", "command_generation": 1,
            "plugin_id": "sync_delivery_status", "legacy_scope_unavailable": 0, **updates}


def attempt(**updates):
    return {"receipt_id": "receipt-1", "lease_id": "lease-1", "automation_id": "project-1",
            "generation": 1, "orchestration_run_id": "run-1", "work_item_id": "work-1",
            "operation": "network.request", "action": "feishu.sheet.replace_rows",
            "outcome": "WRITE_OUTCOME_UNKNOWN", "record_count": "12",
            "created_at": datetime(2026, 9, 8, 17, 0), "updated_at": datetime(2026, 9, 8, 17, 1),
            "execution_resource_keys_json": [["account-write", "private-account"], ["physical-write", "feishu_sheet", "private-parent", "private-child"]],
            "legacy_scope_quarantined_at": None, **updates}


def test_write_attempt_projection_is_closed_json_friendly_and_never_returns_scope_values():
    repo, calls = repository([lease()], [attempt(private_value="private-value", target_ref_json={"private": "private-target"})])
    result = list_work_item_unknown_writes(repo, "work-1")
    row = result[0]["write_attempts"][0]
    assert row == {
        "receipt_id": "receipt-1", "operation": "network.request", "action": "feishu.sheet.replace_rows",
        "outcome": "WRITE_OUTCOME_UNKNOWN", "record_count": 12,
        "created_at": "2026-09-08T17:00:00", "updated_at": "2026-09-08T17:01:00",
        "original_scope_key_kinds": ["account-write", "physical-write"],
        "scope_missing": False, "scope_malformed": False, "scope_quarantined": False,
        "scope_derivation_reason": "LEASE_METADATA_UNAVAILABLE",
        "derived_scope_key_kinds": ["account-write", "physical-write"],
    }
    assert "private" not in json.dumps(result)
    sql, params = calls[1]
    assert params == ("work-1",)
    assert "l.lease_id=a.lease_id AND l.automation_id=a.automation_id" in sql
    assert "l.generation=a.generation AND l.orchestration_run_id=a.orchestration_run_id" in sql
    assert "r.work_item_id=%s" in sql and "LIMIT 1001" in sql
    assert "FOR UPDATE" not in sql and "scan_recovery_payload_json" not in sql
    assert "a.target_ref_json, a.target_ref_sha256" in sql
    assert "l.runtime_metadata_json, l.runtime_metadata_sha256" in sql
    assert all(name not in json.dumps(result) for name in ("target_ref_json", "runtime_metadata_json", "sha256"))


@pytest.mark.parametrize("scope,missing,malformed,kinds", [
    (None, True, False, []), ("null", True, False, []), ("[]", True, False, []),
    ("{}", False, True, []), ("not-json", False, True, []),
    ([["account-write"]], False, True, []),
    ([["account-write", "private"], ["account-write", "private"]], False, True, []),
    ([["private-unrecognized-kind", "private"]], False, True, ["unknown"]),
    ([["browser-write", "private"], ["projection-write", "waybills"]], False, False, ["browser-write", "projection-write"]),
])
def test_missing_malformed_and_quarantined_scopes_are_explicit(scope, missing, malformed, kinds):
    repo, _ = repository([lease(legacy_scope_unavailable=1)], [attempt(
        execution_resource_keys_json=scope, legacy_scope_quarantined_at=datetime(2026, 9, 1), record_count=None,
    )])
    row = list_work_item_unknown_writes(repo, "work-1")[0]["write_attempts"][0]
    assert row["scope_missing"] is missing and row["scope_malformed"] is malformed
    assert row["original_scope_key_kinds"] == kinds and row["scope_quarantined"]
    assert row["record_count"] is None and "private" not in json.dumps(row)
    if missing:
        assert row["scope_derivation_reason"] == "LEGACY_SCOPE_QUARANTINED"
    elif malformed and not kinds:
        assert row["scope_derivation_reason"] == "ORIGINAL_SCOPE_MALFORMED"


@pytest.mark.parametrize("tamper,reason,kinds", [
    (None, "ORIGINAL_RESOURCE_SCOPE_DERIVED", ["account-resource", "physical-write"]),
    ("runtime_metadata_sha256", "LEASE_METADATA_DIGEST_MISMATCH", ["account-write", "physical-write", "resource-write"]),
    ("target_ref_sha256", "TARGET_LOCATOR_DIGEST_MISMATCH", ["account-write", "physical-write", "resource-write"]),
])
def test_public_scope_reason_uses_the_same_proof_without_returning_original_material(tamper, reason, kinds):
    from shared.orchestration_repository_support import _json_hash
    from tests.test_historical_receipt_execution_scopes import _case

    proof, keys, _physical = _case()
    proof["automation_id"] = proof["target_ref_json"]["automation_id"] = "project-1"
    proof["target_ref_sha256"] = _json_hash(proof["target_ref_json"])
    if tamper:
        proof[tamper] = "0" * 64
    repo, _ = repository([lease()], [attempt(**proof, execution_resource_keys_json=keys)])
    result = list_work_item_unknown_writes(repo, "work-1")[0]["write_attempts"][0]
    assert result["scope_derivation_reason"] == reason and result["derived_scope_key_kinds"] == kinds
    serialized = json.dumps(result)
    assert all(value not in serialized for value in ("original-resource", "ronghui-read-account", "target_ref_json", "runtime_metadata_json"))


@pytest.mark.parametrize("updates", [
    {"lease_id": "other-lease"}, {"automation_id": "other-project"}, {"generation": 2},
    {"orchestration_run_id": "other-run"}, {"work_item_id": "other-work"},
])
def test_cross_identity_receipt_from_adapter_is_explicit_failure(updates):
    repo, _ = repository([lease()], [attempt(**updates)])
    with pytest.raises(ValueError, match="WORK_ITEM_WRITE_ATTEMPT_IDENTITY_MISMATCH"):
        list_work_item_unknown_writes(repo, "work-1")


def test_receipts_are_grouped_by_exact_lease_and_limit_is_not_truncated():
    first, second = lease(), lease(lease_id="lease-2")
    repo, _ = repository([first, second], [attempt(), attempt(lease_id="lease-2", receipt_id="receipt-2")])
    result = list_work_item_unknown_writes(repo, "work-1")
    assert [[item["receipt_id"] for item in row["write_attempts"]] for row in result] == [["receipt-1"], ["receipt-2"]]
    repo, _ = repository([first], [{}] * 1001)
    with pytest.raises(ValueError, match="WORK_ITEM_WRITE_ATTEMPT_LIMIT_EXCEEDED"):
        list_work_item_unknown_writes(repo, "work-1")


def test_public_metadata_rejects_non_identifier_action_without_leaking_value():
    repo, _ = repository([lease()], [attempt(action="private=value")])
    with pytest.raises(ValueError, match="^WRITE_ATTEMPT_PUBLIC_METADATA_INVALID$"):
        list_work_item_unknown_writes(repo, "work-1")
