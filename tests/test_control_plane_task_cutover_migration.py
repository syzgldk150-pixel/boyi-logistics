from __future__ import annotations

from pathlib import Path


def test_task_cutover_migration_preserves_existing_parameters_and_is_recoverable() -> None:
    root = Path(__file__).resolve().parents[1]
    sql = (root / "agent" / "migrations" / "014_control_plane_task_cutover.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS control_plane_task_cutover_backup_014" in sql
    assert "INSERT IGNORE INTO control_plane_task_cutover_backup_014" in sql
    assert sql.count("JSON_INSERT(") >= 9
    assert "'$.days', 7" in sql
    assert "'$.platform', 'ronghui'" in sql
    assert "JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.params.params')" in sql
    assert "tool_params = JSON_OBJECT(" not in sql
    assert "JSON_SET(" in sql
    assert "JSON_MERGE_PATCH(" in sql
    assert "JSON_REMOVE(" in sql
    assert "WHERE id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1830')" in sql
    assert "enabled = FALSE" in sql


def test_task_cutover_uses_closed_console_task_id_families() -> None:
    root = Path(__file__).resolve().parents[1]
    sql = (root / "agent" / "migrations" / "014_control_plane_task_cutover.sql").read_text(
        encoding="utf-8"
    )

    for task_id in (
        "finance_bills_0010",
        "clockin_daxiang_1830",
        "clockin_daxiang_s_1830",
    ):
        assert f"'{task_id}'" in sql

    for base_id in (
        "send_order",
        "delivery_status",
        "daily_sign",
        "site_send",
        "arrive_list",
        "yunda_dispatch_forecast",
        "yunda_send_waybills",
        "arrival_stats",
    ):
        assert f"^{base_id}_([01][0-9]|2[0-3])[0-5][0-9]$" in sql
    assert "tool_name IN ('sync_arrive_list', 'sync_arrival_stats')" not in sql
    assert "^scan_codes_([01][0-9]|2[0-3])[0-5][0-9]$" not in sql


def test_arrival_cutover_inserts_only_authoritative_base_account_and_disables_conflicts() -> None:
    root = Path(__file__).resolve().parents[1]
    sql = (root / "agent" / "migrations" / "014_control_plane_task_cutover.sql").read_text(
        encoding="utf-8"
    )

    assert "'$.account_id', 'ronghui_default'" in sql
    assert "JSON_INSERT(" in sql
    assert "$.request_body.params.account_id" in sql
    assert "$.request_body.params.accountId" in sql
    assert "$.scan_request_body.params.account_id" in sql
    assert "$.arrive_list_request_body.params.account_id" in sql
    assert "嵌套请求账号与顶层 account_id 冲突" in sql


def test_single_account_schedule_registry_requires_explicit_top_level_account() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "agent" / "tools" / "registry.yaml").read_text(encoding="utf-8"))
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
