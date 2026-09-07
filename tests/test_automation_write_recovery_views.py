from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from shared.automation_write_recovery_views import list_work_item_unknown_writes


def repository(rows):
    calls = []
    cursor = SimpleNamespace(execute=lambda sql, params: calls.append((sql, params)),
                             fetchall=lambda: rows, description=[])
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


def test_excessive_history_is_explicit_failure_not_silent_truncation():
    repo, _ = repository([{}] * 101)
    with pytest.raises(ValueError, match="WORK_ITEM_RECOVERY_LIMIT_EXCEEDED"):
        list_work_item_unknown_writes(repo, "work-1")
