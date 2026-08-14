from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "agent" / "migrations" / "016_daily_sign_single_tms_account.sql"


def _sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_daily_sign_account_migration_accepts_only_reviewed_old_or_new_shapes() -> None:
    sql = _sql()

    assert "@cp016_invalid_daily_sign_rows" in sql
    assert "JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 3" in sql
    assert "JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 5" in sql
    assert "'$.r13_account_id')) = 'r13_default'" in sql
    assert "'$.account_id')) = 'ronghui_daxiang_s'" in sql
    assert "'$.problem_account_id')) = 'ronghui_daxiang_s'" in sql
    assert "'$.sign_account_id')) = 'ronghui_daxiang_s'" in sql
    assert "'$.detail_account_id')) IN ('ronghui_default', 'ronghui_daxiang_s')" in sql
    assert "information_schema.cp016_invalid_daily_sign_account_contract" in sql


def test_daily_sign_account_migration_writes_one_tms_account_contract() -> None:
    sql = _sql()
    update_sql = sql.split("UPDATE scheduled_tasks AS task", 1)[1]

    assert "'r13_account_id', 'r13_default'" in update_sql
    assert "'account_id', 'ronghui_daxiang_s'" in update_sql
    assert "'days', 7" in update_sql
    assert "problem_account_id" not in update_sql
    assert "sign_account_id" not in update_sql
    assert "detail_account_id" not in update_sql
    assert "task.configuration_version = task.configuration_version + 1" in update_sql
    assert "JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 5" in update_sql
