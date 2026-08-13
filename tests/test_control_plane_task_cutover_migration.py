from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "agent" / "migrations" / "014_control_plane_task_cutover.sql"


def _load_migration_runner():
    script_path = ROOT / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("test_cp014_migration_runner", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


split_sql_statements = _load_migration_runner().split_sql_statements

EXPECTED_TASK_IDS = {
    "arrive_list": {
        "arrive_list_0830",
        "arrive_list_0900",
        "arrive_list_0930",
    },
    "daily_sign": {
        "daily_sign_0500",
        "daily_sign_0700",
        "daily_sign_0800",
        "daily_sign_0900",
        "daily_sign_1000",
        "daily_sign_1100",
        "daily_sign_1200",
        "daily_sign_1300",
        "daily_sign_1400",
        "daily_sign_1430",
        "daily_sign_1500",
        "daily_sign_1530",
        "daily_sign_1600",
        "daily_sign_1630",
        "daily_sign_1700",
        "daily_sign_1730",
        "daily_sign_1800",
    },
    "delivery_status": {
        "delivery_status_0900",
        "delivery_status_1000",
        "delivery_status_1100",
        "delivery_status_1200",
        "delivery_status_1300",
        "delivery_status_1400",
        "delivery_status_1430",
        "delivery_status_1500",
        "delivery_status_1530",
        "delivery_status_1600",
        "delivery_status_1630",
        "delivery_status_1700",
        "delivery_status_1730",
        "delivery_status_1800",
        "delivery_status_1830",
        "delivery_status_1900",
        "delivery_status_1930",
        "delivery_status_2000",
        "delivery_status_2030",
        "delivery_status_2100",
    },
    "send_order": {"send_order_2359"},
    "site_send": {
        "site_send_0500",
        "site_send_0530",
        "site_send_1800",
        "site_send_1830",
        "site_send_1900",
        "site_send_1930",
        "site_send_2000",
        "site_send_2030",
        "site_send_2100",
    },
    "yunda_send_waybills": {"yunda_send_waybills_2355"},
}


def _sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def _expected_rows(sql: str) -> list[tuple[str, str, str, str]]:
    values = sql.split(
        "INSERT INTO cp014_expected_tasks (id, family, tool_name, cron_expression)\nVALUES\n",
        1,
    )[1].split(";", 1)[0]
    return re.findall(
        r"\('([^']+)', '([^']+)', '([^']+)', '([^']+)'\)",
        values,
    )


def test_task_cutover_guards_before_any_permanent_change() -> None:
    sql = _sql()

    preflight_insert = sql.index("INSERT INTO cp014_preflight_guard")
    permanent_backup = sql.index(
        "CREATE TABLE IF NOT EXISTS control_plane_task_cutover_backup_014"
    )
    first_update = sql.index("UPDATE scheduled_tasks AS task")

    assert preflight_insert < permanent_backup < first_update
    assert "CONSTRAINT cp014_all_reviewed_enabled CHECK" in sql
    assert "WHEN EXISTS (" in sql
    assert "WHERE task.id IS NULL OR NOT COALESCE(task.enabled = TRUE, FALSE)" in sql
    assert "CONSTRAINT cp014_no_unknown_governed CHECK" in sql
    assert "CONSTRAINT cp014_no_unknown_clock CHECK" in sql
    assert "CONSTRAINT cp014_no_binding_mismatch CHECK" in sql
    assert "CONSTRAINT cp014_clock_shape_reviewed CHECK" in sql
    assert "CONSTRAINT cp014_no_clock_write CHECK" in sql
    assert "CONSTRAINT cp014_arrive_shape_closed CHECK" in sql
    assert "CONSTRAINT cp014_daily_shape_closed CHECK" in sql
    assert "CONSTRAINT cp014_all_rows_canonical CHECK" in sql
    assert "enabled = FALSE" not in sql
    assert "REGEXP" not in sql


def test_task_cutover_compares_reviewed_bindings_as_exact_binary_values() -> None:
    sql = _sql()

    # Migration 001 inherits the database collation, while MySQL 8 temporary
    # tables may inherit another default. VARBINARY keeps all cross-table
    # contract comparisons exact without relying on either collation.
    expected_table = sql.split(
        "CREATE TEMPORARY TABLE cp014_expected_tasks (", 1
    )[1].split(") ENGINE=InnoDB;", 1)[0]
    assert "id VARBINARY(128) PRIMARY KEY" in expected_table
    assert "family VARBINARY(64) NOT NULL" in expected_table
    assert "tool_name VARBINARY(128) NOT NULL" in expected_table
    assert "cron_expression VARBINARY(64) NOT NULL" in expected_table
    assert "VARCHAR" not in expected_table


def test_empty_database_is_the_only_allowed_incomplete_reviewed_set() -> None:
    sql = _sql()
    guard_insert = sql.index("INSERT INTO cp014_preflight_guard")
    missing_guard = sql[guard_insert : sql.index("CREATE TABLE IF NOT EXISTS", guard_insert)]

    assert "WHEN EXISTS (" in missing_guard
    assert "candidate_expected.id IS NOT NULL" in missing_guard
    assert "LEFT(candidate.id, CHAR_LENGTH('clockin_')) = 'clockin_'" in missing_guard
    assert "LEFT(candidate.id, CHAR_LENGTH('daily_sign_')) = 'daily_sign_'" in missing_guard
    assert "candidate.tool_name = 'clock_in_dual'" in missing_guard
    assert "'sync_arrival_stats'" not in missing_guard
    assert "'sync_finance_bills'" not in missing_guard
    assert "WHERE task.id IS NULL OR NOT COALESCE(task.enabled = TRUE, FALSE)" in missing_guard
    assert "ELSE 0" in missing_guard


def test_task_cutover_uses_only_the_reviewed_51_internal_projection_ids() -> None:
    rows = _expected_rows(_sql())
    contracts = _load_migration_runner()._load_control_plane_reviewed_task_contracts()
    by_family: dict[str, set[str]] = {}
    for task_id, family, tool_name, cron_expression in rows:
        by_family.setdefault(family, set()).add(task_id)
        hhmm = task_id.rsplit("_", 1)[-1]
        assert cron_expression == f"{int(hhmm[2:])} {int(hhmm[:2])} * * *"
        contract = contracts[task_id]
        assert contract["group_id"] == family
        assert contract["tool_name"] == tool_name
        assert contract["cron_expression"] == cron_expression

    assert len(rows) == 51
    assert set(contracts) == {row[0] for row in rows}
    assert by_family == EXPECTED_TASK_IDS
    assert "finance_bills_0010" not in {row[0] for row in rows}
    assert "yunda_dispatch_forecast_1700" not in {row[0] for row in rows}
    assert "arrival_stats" not in by_family


def test_task_cutover_normalizes_only_reviewed_legacy_semantics() -> None:
    sql = _sql()

    assert "'$.account_id', 'price_default'" in sql
    assert "'$.detail_account_id', 'ronghui_default'" in sql
    assert "'$.problem_account_id', 'ronghui_daxiang_s'" in sql
    assert "'$.sign_account_id', 'ronghui_daxiang_s'" in sql
    assert "'$.days', 7" in sql
    assert "'$.ensure_fields', CAST('false' AS JSON)" in sql

    # c7 and the current tool consume only login-site aliases. The unrelated
    # legacy site_code is accepted only as one non-empty value shared by the
    # three reviewed rows, then both legacy fields are removed.
    assert "CONSTRAINT cp014_arrive_site_consistent CHECK" in sql
    assert "COUNT(DISTINCT JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code')))" in sql
    assert (
        "c33492072957c7cc41ad8769d0c790b50d3b5314427e3912609432ea9d320912"
        in sql
    )
    assert "'$.login_site_code',\n    '$.site_code',\n    '$.target_date'" in sql


def test_task_cutover_blocks_external_clock_writes_without_mutating_them() -> None:
    sql = _sql()

    assert "task.id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')" in sql
    assert "CONSTRAINT cp014_no_unknown_clock CHECK" in sql
    assert "CONSTRAINT cp014_clock_shape_reviewed CHECK" in sql
    assert "= '/clock_in_dual'" in sql
    assert "clockin_daxiang_s_1830" not in sql
    assert "SET task.tool_name = 'clock_in_dual'" not in sql
    assert "sitefbcode" not in sql


def test_task_cutover_is_recoverable_transactional_and_reentrant() -> None:
    sql = _sql()

    assert "CREATE TABLE IF NOT EXISTS control_plane_task_cutover_backup_014" in sql
    assert "INSERT IGNORE INTO control_plane_task_cutover_backup_014" in sql
    assert sql.index("INSERT IGNORE INTO control_plane_task_cutover_backup_014") < sql.index(
        "START TRANSACTION"
    )
    assert sql.index("START TRANSACTION") < sql.index("COMMIT")
    assert "CONSTRAINT cp014_enabled_count_preserved CHECK" in sql
    assert sql.count("DROP TEMPORARY TABLE IF EXISTS") >= 4
    assert "tool_params = JSON_OBJECT(" not in sql


def test_task_cutover_is_compatible_with_the_deployment_sql_splitter() -> None:
    statements = split_sql_statements(_sql())

    assert statements[0] == "DROP TEMPORARY TABLE IF EXISTS cp014_expected_tasks"
    assert any(statement.startswith("INSERT INTO cp014_preflight_guard") for statement in statements)
    assert any(
        statement.startswith(
            "CREATE TABLE IF NOT EXISTS control_plane_task_cutover_backup_014"
        )
        for statement in statements
    )
    assert "START TRANSACTION" in statements
    assert "COMMIT" in statements


def test_single_account_schedule_registry_requires_explicit_top_level_account() -> None:
    import yaml

    manifest = yaml.safe_load(
        (ROOT / "agent" / "tools" / "registry.yaml").read_text(encoding="utf-8")
    )
    tools = {tool["name"]: tool for tool in manifest["tools"]}

    for name in (
        "sync_daily_send_orders",
        "sync_delivery_status",
        "sync_site_send_list",
        "sync_arrive_list",
        "sync_arrival_stats",
        "sync_yunda_dispatch_forecast",
        "sync_yunda_send_waybills",
    ):
        tool = tools[name]
        assert tool["account_scope"] == {
            "required": True,
            "allow_implicit_default": False,
        }
        assert tool["input_schema"]["required"] == ["account_id"]
        assert tool["input_schema"]["properties"]["account_id"]["minLength"] == 1
