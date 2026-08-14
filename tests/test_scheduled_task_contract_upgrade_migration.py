from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "agent" / "migrations" / "017_scheduled_task_contract_upgrade.sql"
REVIEWED_ARRIVE_SITE_SHA256 = (
    "5ff8d6c00584886090be588977393764370cbcac7f7d983a2f0b330c5f37b135"
)
APPLIED_014_YUNDA_DISABLED_MESSAGE_SHA256 = (
    "19129e9c68d5e20050a7d8c8e8489f4f1313f9fb6188adc55229aaeacad9c0e3"
)


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


def test_upgrade_accepts_only_exact_applied_014_or_canonical_arrive_shapes() -> None:
    sql = _sql()
    guard_start = sql.index("SET @cp017_arrive_list_count")
    guard_end = sql.index(
        "CREATE TABLE IF NOT EXISTS scheduled_task_contract_upgrade_backup_017"
    )
    guard = sql[guard_start:guard_end]

    assert "@cp017_arrive_list_canonical" in sql
    assert REVIEWED_ARRIVE_SITE_SHA256 in sql
    assert "@cp017_arrive_list_count IN (0, 3)" in guard
    assert "task.tool_name = 'sync_arrive_list'" in guard
    assert "task.enabled = TRUE" in guard
    for task_id, cron_expression in (
        ("arrive_list_0830", "30 8 * * *"),
        ("arrive_list_0900", "0 9 * * *"),
        ("arrive_list_0930", "30 9 * * *"),
    ):
        assert f"WHEN '{task_id}' THEN '{cron_expression}'" in guard
    assert "JSON_LENGTH(task.tool_params) = 2" in guard
    assert (
        "JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'"
        in guard
    )
    assert (
        "JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.site_code')) = 'STRING'"
        in guard
    )
    assert ") = @cp017_arrive_site_sha256" in guard
    assert "information_schema.cp017_invalid_arrive_list_contract" in guard


def test_upgrade_reenables_yunda_only_with_exact_applied_014_proof() -> None:
    sql = _sql()
    guard_start = sql.index("SET @cp017_yunda_send_count")
    guard_end = sql.index(
        "CREATE TABLE IF NOT EXISTS scheduled_task_contract_upgrade_backup_017"
    )
    guard = sql[guard_start:guard_end]
    update_start = sql.index(
        "UPDATE scheduled_tasks AS task\n"
        "INNER JOIN control_plane_task_cutover_backup_014 AS prior"
    )
    update_end = sql.index("SET @cp017_noncanonical_clock_rows")
    update = sql[update_start:update_end]
    post_start = sql.index("SET @cp017_noncanonical_yunda_send_rows")
    post = sql[post_start:sql.index("COMMIT", post_start)]

    assert "@cp017_yunda_send_canonical" in sql
    assert "@cp017_yunda_send_pre014" in sql
    assert APPLIED_014_YUNDA_DISABLED_MESSAGE_SHA256 in sql
    assert "@cp017_yunda_send_count IN (0, 1)" in guard
    assert "task.id = 'yunda_send_waybills_2355'" in guard
    assert "task.tool_name = 'sync_yunda_send_waybills'" in guard
    assert "task.cron_expression = '55 23 * * *'" in guard
    assert "task.enabled = TRUE" in guard
    assert "task.enabled = FALSE" in guard
    assert "task.last_status = 'disabled'" in guard
    assert "task.configuration_version = 1" in guard
    assert "SHA2(COALESCE(task.last_message, ''), 256)" in guard
    assert "FROM control_plane_task_cutover_backup_014 AS prior" in guard
    assert "prior.enabled = TRUE" in guard
    assert (
        "JSON_CONTAINS(prior.tool_params, @cp017_yunda_send_pre014)" in guard
    )
    assert (
        "JSON_CONTAINS(@cp017_yunda_send_pre014, prior.tool_params)" in guard
    )
    assert "information_schema.cp017_invalid_yunda_send_contract" in guard

    assert "SET task.enabled = TRUE" in update
    assert "task.last_status = prior.last_status" in update
    assert "task.last_message = prior.last_message" in update
    assert "task.configuration_version = task.configuration_version + 1" in update
    assert "task.enabled = FALSE" in update
    assert "task.configuration_version = 1" in update
    assert "prior.enabled = TRUE" in update
    assert "@cp017_yunda_disabled_message_sha256" in update

    assert "task.enabled = TRUE" in post
    assert "@cp017_yunda_send_canonical" in post
    assert "information_schema.cp017_yunda_send_upgrade_failed" in post


def test_upgrade_backs_up_then_transactionally_normalizes_contracts() -> None:
    sql = _sql()
    backup = sql.index(
        "CREATE TABLE IF NOT EXISTS scheduled_task_contract_upgrade_backup_017"
    )
    transaction = sql.index("START TRANSACTION")
    first_update = sql.index("UPDATE scheduled_tasks AS task")
    arrive_update = sql.index(
        "UPDATE scheduled_tasks AS task\n"
        "SET task.tool_params = @cp017_arrive_list_canonical"
    )
    post_guard = sql.index("@cp017_noncanonical_clock_rows")
    arrive_post_guard = sql.index("@cp017_noncanonical_arrive_list_rows")
    commit = sql.index("COMMIT")

    assert backup < transaction < first_update < post_guard < commit
    assert transaction < arrive_update < arrive_post_guard < commit
    assert "INSERT IGNORE INTO scheduled_task_contract_upgrade_backup_017" in sql
    backup_rows = sql[backup:transaction]
    for task_id in (
        "arrive_list_0830",
        "arrive_list_0900",
        "arrive_list_0930",
        "yunda_send_waybills_2355",
    ):
        assert f"'{task_id}'" in backup_rows
    assert "SET task.tool_params = @cp017_arrive_list_canonical" in sql
    assert "task.configuration_version = task.configuration_version + 1" in sql
    assert "information_schema.cp017_clock_upgrade_failed" in sql
    assert "information_schema.cp017_finance_upgrade_failed" in sql
    assert "information_schema.cp017_arrive_list_upgrade_failed" in sql


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
    assert sum(
        statement.startswith("UPDATE scheduled_tasks AS task")
        for statement in statements
    ) == 5
