from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "agent" / "migrations" / "017_scheduled_task_contract_upgrade.sql"


def _sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def _split_sql_statements(sql: str) -> list[str]:
    runner_path = ROOT / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("test_cp017_runner", runner_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.split_sql_statements(sql)


def test_upgrade_accepts_only_exact_applied_014_or_canonical_clock_shapes() -> None:
    sql = _sql()

    assert "@cp017_daxiang_transition" in sql
    assert "@cp017_daxiang_s_transition" in sql
    assert "@cp017_daxiang_canonical" in sql
    assert "@cp017_daxiang_s_canonical" in sql
    assert "task.id = 'clockin_daxiang_1830'" in sql
    assert "task.id = 'clockin_daxiang_s_1833'" in sql
    assert "task.tool_name = 'tms_query'" in sql
    assert "task.tool_name = 'clock_in_dual'" in sql
    assert "task.enabled = TRUE" in sql
    assert "@cp017_clock_pair_count IN (0, 2)" in sql
    assert "information_schema.cp017_invalid_clock_contract" in sql


def test_upgrade_backs_up_then_transactionally_normalizes_contracts() -> None:
    sql = _sql()
    backup = sql.index(
        "CREATE TABLE IF NOT EXISTS scheduled_task_contract_upgrade_backup_017"
    )
    transaction = sql.index("START TRANSACTION")
    first_update = sql.index("UPDATE scheduled_tasks AS task")
    post_guard = sql.index("@cp017_noncanonical_clock_rows")
    commit = sql.index("COMMIT")

    assert backup < transaction < first_update < post_guard < commit
    assert "INSERT IGNORE INTO scheduled_task_contract_upgrade_backup_017" in sql
    assert "task.configuration_version = task.configuration_version + 1" in sql
    assert "information_schema.cp017_clock_upgrade_failed" in sql
    assert "information_schema.cp017_finance_upgrade_failed" in sql


def test_upgrade_keeps_optional_finance_state_and_never_creates_startup_task() -> None:
    sql = _sql()

    assert "@cp017_finance_transition" in sql
    assert "@cp017_finance_canonical" in sql
    assert "task.id = 'finance_bills_0010'" in sql
    assert "task.enabled IN (FALSE, TRUE)" in sql
    assert "finance_startup_catchup" not in sql
    assert "INSERT INTO scheduled_tasks" not in sql


def test_upgrade_is_compatible_with_deployment_sql_splitter() -> None:
    statements = _split_sql_statements(_sql())

    assert statements
    assert statements[-1] == "COMMIT"
    assert any(statement.startswith("START TRANSACTION") for statement in statements)
    assert sum(statement.startswith("UPDATE scheduled_tasks AS task") for statement in statements) == 3
