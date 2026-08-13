from __future__ import annotations

import pytest
from types import SimpleNamespace

from agent.tool_registry import ToolRegistry
from shared.scheduled_task_contracts import (
    APPROVED_SCHEDULED_TASK_PROFILES,
    ScheduledTaskContractError,
    validate_persisted_scheduled_task,
)


def _row(
    *,
    task_id: str = "arrive_list_0830",
    tool_name: str = "sync_arrive_list",
    tool_params: dict | None = None,
    cron_expression: str = "30 8 * * *",
    enabled: object = True,
) -> dict:
    return {
        "id": task_id,
        "tool_name": tool_name,
        "tool_params": {"account_id": "ronghui_default"} if tool_params is None else tool_params,
        "cron_expression": cron_expression,
        "enabled": enabled,
    }


def _validate(row: dict):
    catalog = ToolRegistry()
    return validate_persisted_scheduled_task(
        row,
        capability=catalog.get_capability(str(row.get("tool_name") or "")),
        validate_arguments=catalog.validate_arguments,
        enabled_finance_platforms=("ronghui",),
    )


def test_every_approved_profile_matches_the_real_governed_catalog() -> None:
    catalog = ToolRegistry()

    for profile in APPROVED_SCHEDULED_TASK_PROFILES.values():
        capability = catalog.get_capability(profile.tool_name)
        assert capability is not None
        assert capability["version"] == profile.tool_version
        assert capability["operation_type"] == profile.operation_type
        assert capability["approval"]["mode"] == "schedule_allowlist"


@pytest.mark.parametrize(
    ("group_id", "task_id", "cron_expression"),
    (
        ("send_order", "send_order_2150", "50 21 * * *"),
        ("delivery_status", "delivery_status_0900", "0 9 * * *"),
        ("daily_sign", "daily_sign_1100", "0 11 * * *"),
        ("site_send", "site_send_1930", "30 19 * * *"),
        ("arrive_list", "arrive_list_0830", "30 8 * * *"),
        ("arrival_stats", "arrival_stats_0900", "0 9 * * *"),
        ("yunda_dispatch_forecast", "yunda_dispatch_forecast_1700", "0 17 * * *"),
        ("yunda_send_waybills", "yunda_send_waybills_2355", "55 23 * * *"),
        ("finance_bills", "finance_bills_0010", "10 0 * * *"),
    ),
)
def test_each_task_family_accepts_only_its_code_owned_static_arguments(
    group_id: str,
    task_id: str,
    cron_expression: str,
) -> None:
    profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
    contract = _validate(
        _row(
            task_id=task_id,
            tool_name=profile.tool_name,
            tool_params=dict(profile.approved_arguments),
            cron_expression=cron_expression,
        )
    )

    assert contract.arguments == profile.approved_arguments
    assert contract.dynamic_argument_rules == profile.dynamic_argument_rules


@pytest.mark.parametrize(
    ("group_id", "changed_arguments"),
    (
        ("send_order", {"account_id": "ronghui_default", "target_date": "2026-08-12"}),
        ("delivery_status", {"account_id": "ronghui_default", "bill_codes": []}),
        (
            "daily_sign",
            {
                "r13_account_id": "r13_default",
                "problem_account_id": "ronghui_daxiang_s",
                "sign_account_id": "ronghui_daxiang_s",
                "detail_account_id": "ronghui_daxiang_s",
                "days": 6,
            },
        ),
        ("site_send", {"account_id": "ronghui_default", "range": "Sheet1!A1:Z999"}),
        ("arrive_list", {"account_id": "ronghui_default", "target_date": "2026-08-12"}),
        ("arrival_stats", {"account_id": "ronghui_default", "trigger_flow": True}),
        (
            "yunda_dispatch_forecast",
            {"account_id": "yunda_default", "dest_brch": "56739383"},
        ),
        ("yunda_send_waybills", {"account_id": "yunda_default", "ensure_fields": False}),
        (
            "finance_bills",
            {"mode": "sync", "platform": "ronghui", "rescan_days": 7, "target_date": "2026-08-12"},
        ),
    ),
)
def test_schema_valid_argument_change_never_becomes_its_own_allowlist_baseline(
    group_id: str,
    changed_arguments: dict,
) -> None:
    profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
    row = _row(
        task_id=f"{group_id}__slot_1",
        tool_name=profile.tool_name,
        tool_params=changed_arguments,
        cron_expression="0 9 * * *",
    )

    with pytest.raises(ScheduledTaskContractError) as error:
        _validate(row)

    assert error.value.code == "ARGUMENTS_NOT_CODE_APPROVED"


@pytest.mark.parametrize(
    ("task_id", "cron_expression"),
    (
        ("arrive_list", "30 8 * * *"),
        ("arrive_list_0830", "30 8 * * *"),
        ("arrive_list__slot_2", "0 9 * * *"),
        ("arrival_stats_0930", "30 9 * * *"),
    ),
)
def test_approved_persisted_task_ids_bind_real_registry_contract(task_id: str, cron_expression: str) -> None:
    tool_name = "sync_arrival_stats" if task_id.startswith("arrival_stats") else "sync_arrive_list"
    tool_params = {"account_id": "ronghui_default"}
    if tool_name == "sync_arrival_stats":
        tool_params["trigger_flow"] = False

    contract = _validate(
        _row(
            task_id=task_id,
            tool_name=tool_name,
            tool_params=tool_params,
            cron_expression=cron_expression,
        )
    )

    assert contract.task_id == task_id
    assert contract.tool_name == tool_name
    assert contract.arguments == tool_params


@pytest.mark.parametrize(
    ("row", "code"),
    (
        (_row(task_id="arrive_list_0830", cron_expression="0 8 * * *"), "TASK_ID_CRON_MISMATCH"),
        (_row(task_id="arrive_list_0830", cron_expression="*/30 8 * * *"), "CRON_NOT_EXACT_DAILY_TIME"),
        (_row(task_id="arrive_list_weekly", cron_expression="30 8 * * *"), "TASK_GROUP_NOT_APPROVED"),
        (_row(task_id="r7_arrival_checkin", tool_name="r7_arrival_checkin"), "TASK_GROUP_NOT_APPROVED"),
        (_row(task_id="clockin_daxiang_1830", tool_name="clock_in_dual"), "TASK_GROUP_NOT_APPROVED"),
        (_row(enabled=False), "TASK_DISABLED"),
    ),
)
def test_invalid_schedule_shape_is_not_eligible(row: dict, code: str) -> None:
    with pytest.raises(ScheduledTaskContractError) as error:
        _validate(row)

    assert error.value.code == code


@pytest.mark.parametrize(
    "tool_params",
    (
        {"account_id": "another_valid_account"},
        {"account_id": "ronghui_default", "target_date": "2026-08-12"},
        {"account_id": "ronghui_default", "dry_run": False},
        {
            "account_id": "ronghui_default",
            "request_body": {"params": {"accountId": "ronghui_default"}},
        },
    ),
)
def test_schema_valid_arrival_override_is_not_code_approved(tool_params: dict) -> None:
    row = _row(tool_params=tool_params)

    with pytest.raises(ScheduledTaskContractError) as error:
        _validate(row)

    assert error.value.code == "ARGUMENTS_NOT_CODE_APPROVED"


def test_registry_version_change_requires_code_profile_update() -> None:
    catalog = ToolRegistry()
    capability = catalog.get_capability("sync_arrive_list")
    capability["version"] = "99.0.0"

    with pytest.raises(ScheduledTaskContractError) as error:
        validate_persisted_scheduled_task(
            _row(),
            capability=capability,
            validate_arguments=catalog.validate_arguments,
            enabled_finance_platforms=("ronghui",),
        )

    assert error.value.code == "TOOL_VERSION_NOT_APPROVED"


def test_finance_schedule_is_limited_to_daily_ronghui_sync() -> None:
    row = _row(
        task_id="finance_bills_0010",
        tool_name="sync_finance_bills",
        tool_params={"mode": "sync", "platform": "ronghui", "rescan_days": 7},
        cron_expression="10 0 * * *",
    )

    contract = _validate(row)

    assert contract.dynamic_argument_rules == {"target_date": "scheduled_previous_day"}
    assert contract.arguments == {"mode": "sync", "platform": "ronghui", "rescan_days": 7}


@pytest.mark.parametrize(
    "tool_params",
    (
        {"mode": "backfill", "platform": "ronghui", "rescan_days": 7},
        {"mode": "sync", "platform": "ronghui", "rescan_days": 7, "target_date": "2026-08-12"},
        {"mode": "sync", "platform": "ronghui", "rescan_days": 6},
    ),
)
def test_finance_backfill_or_fixed_range_never_receives_scheduler_exemption(tool_params: dict) -> None:
    row = _row(
        task_id="finance_bills_0010",
        tool_name="sync_finance_bills",
        tool_params=tool_params,
        cron_expression="10 0 * * *",
    )

    with pytest.raises(ScheduledTaskContractError):
        _validate(row)


def test_finance_schedule_fails_closed_when_production_platform_set_changes() -> None:
    catalog = ToolRegistry()
    row = _row(
        task_id="finance_bills_0010",
        tool_name="sync_finance_bills",
        tool_params={"mode": "sync", "platform": "ronghui", "rescan_days": 7},
        cron_expression="10 0 * * *",
    )

    with pytest.raises(ScheduledTaskContractError) as error:
        validate_persisted_scheduled_task(
            row,
            capability=catalog.get_capability("sync_finance_bills"),
            validate_arguments=catalog.validate_arguments,
            enabled_finance_platforms=("ronghui", "yunda"),
        )

    assert error.value.code == "FINANCE_PLATFORM_NOT_PRODUCTION_READY"


def test_main_provider_reads_enabled_memory_rows_and_omits_invalid_rows() -> None:
    from main import _persisted_scheduler_allowlist

    rows = [
        _row(),
        _row(task_id="r7_arrival_checkin", tool_name="r7_arrival_checkin"),
    ]
    runtime = SimpleNamespace(
        memory=SimpleNamespace(list_enabled_scheduled_tasks=lambda: rows),
    )

    entries = _persisted_scheduler_allowlist(runtime, ToolRegistry())

    assert len(entries) == 1
    assert entries[0].task_id == "arrive_list_0830"
    assert entries[0].tool_name == "sync_arrive_list"
