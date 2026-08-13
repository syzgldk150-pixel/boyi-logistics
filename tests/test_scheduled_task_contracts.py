from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.tool_registry import ToolRegistry
from shared.scheduled_task_contracts import (
    APPROVED_SCHEDULED_TASK_PROFILES,
    ScheduledTaskContractError,
    validate_persisted_scheduled_task,
)


EXPECTED_APPROVED_TASK_IDS = {
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


def _cron_for_task_id(task_id: str) -> str:
    hhmm = task_id.rsplit("_", 1)[-1]
    return f"{int(hhmm[2:])} {int(hhmm[:2])} * * *"


def test_every_approved_profile_matches_the_real_governed_catalog() -> None:
    catalog = ToolRegistry()

    for profile in APPROVED_SCHEDULED_TASK_PROFILES.values():
        capability = catalog.get_capability(profile.tool_name)
        assert capability is not None
        assert capability["version"] == profile.tool_version
        assert capability["operation_type"] == profile.operation_type
        assert capability["approval"]["mode"] == "schedule_allowlist"


def test_only_reviewed_production_task_ids_are_code_approved() -> None:
    actual = {
        group_id: set(profile.approved_task_ids)
        for group_id, profile in APPROVED_SCHEDULED_TASK_PROFILES.items()
        if profile.approved_task_ids
    }

    assert actual == EXPECTED_APPROVED_TASK_IDS
    assert not APPROVED_SCHEDULED_TASK_PROFILES["arrival_stats"].approved_task_ids
    assert not APPROVED_SCHEDULED_TASK_PROFILES["yunda_dispatch_forecast"].approved_task_ids
    assert not APPROVED_SCHEDULED_TASK_PROFILES["finance_bills"].approved_task_ids


@pytest.mark.parametrize(
    ("group_id", "task_id"),
    tuple(
        (group_id, task_id)
        for group_id, task_ids in EXPECTED_APPROVED_TASK_IDS.items()
        for task_id in sorted(task_ids)
    ),
)
def test_each_reviewed_task_id_accepts_only_its_code_owned_contract(
    group_id: str,
    task_id: str,
) -> None:
    profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
    contract = _validate(
        _row(
            task_id=task_id,
            tool_name=profile.tool_name,
            tool_params=dict(profile.approved_arguments),
            cron_expression=_cron_for_task_id(task_id),
        )
    )

    assert contract.task_id == task_id
    assert contract.arguments == profile.approved_arguments
    assert contract.dynamic_argument_rules == profile.dynamic_argument_rules


@pytest.mark.parametrize(
    ("group_id", "task_id", "changed_arguments"),
    (
        (
            "send_order",
            "send_order_2359",
            {"account_id": "price_default", "target_date": "2026-08-12"},
        ),
        (
            "delivery_status",
            "delivery_status_0900",
            {"account_id": "ronghui_default", "bill_codes": []},
        ),
        (
            "daily_sign",
            "daily_sign_1100",
            {
                "r13_account_id": "r13_default",
                "problem_account_id": "ronghui_daxiang_s",
                "sign_account_id": "ronghui_daxiang_s",
                "detail_account_id": "ronghui_default",
                "days": 6,
            },
        ),
        (
            "site_send",
            "site_send_1930",
            {"account_id": "ronghui_default", "range": "Sheet1!A1:Z999"},
        ),
        (
            "arrive_list",
            "arrive_list_0830",
            {"account_id": "ronghui_default", "target_date": "2026-08-12"},
        ),
        (
            "yunda_send_waybills",
            "yunda_send_waybills_2355",
            {"account_id": "yunda_default", "ensure_fields": True},
        ),
    ),
)
def test_schema_valid_argument_change_never_becomes_its_own_allowlist_baseline(
    group_id: str,
    task_id: str,
    changed_arguments: dict,
) -> None:
    profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
    row = _row(
        task_id=task_id,
        tool_name=profile.tool_name,
        tool_params=changed_arguments,
        cron_expression=_cron_for_task_id(task_id),
    )

    with pytest.raises(ScheduledTaskContractError) as error:
        _validate(row)

    assert error.value.code == "ARGUMENTS_NOT_CODE_APPROVED"


@pytest.mark.parametrize(
    ("task_id", "tool_name"),
    (
        ("arrive_list", "sync_arrive_list"),
        ("arrive_list_0831", "sync_arrive_list"),
        ("arrive_list__slot_2", "sync_arrive_list"),
        ("arrival_stats_0900", "sync_arrival_stats"),
        ("finance_bills_0010", "sync_finance_bills"),
        ("yunda_dispatch_forecast_1700", "sync_yunda_dispatch_forecast"),
        ("clockin_daxiang_1830", "tms_query"),
        ("clockin_daxiang_s_1833", "tms_query"),
        ("r7_arrival_checkin", "r7_arrival_checkin"),
    ),
)
def test_unreviewed_or_external_write_task_id_is_never_eligible(
    task_id: str,
    tool_name: str,
) -> None:
    with pytest.raises(ScheduledTaskContractError) as error:
        _validate(_row(task_id=task_id, tool_name=tool_name))

    assert error.value.code == "TASK_GROUP_NOT_APPROVED"


@pytest.mark.parametrize(
    ("row", "code"),
    (
        (_row(task_id="arrive_list_0830", cron_expression="0 8 * * *"), "TASK_ID_CRON_MISMATCH"),
        (_row(task_id="arrive_list_0830", cron_expression="*/30 8 * * *"), "CRON_NOT_EXACT_DAILY_TIME"),
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


def test_finance_profile_remains_available_only_for_static_startup_contract() -> None:
    profile = APPROVED_SCHEDULED_TASK_PROFILES["finance_bills"]

    assert not profile.approved_task_ids
    assert profile.approved_arguments == {"mode": "sync", "platform": "ronghui", "rescan_days": 7}
    assert profile.dynamic_argument_rules == {"target_date": "scheduled_previous_day"}


def test_main_provider_reads_enabled_memory_rows_and_omits_invalid_rows() -> None:
    from main import _persisted_scheduler_allowlist

    rows = [
        _row(),
        _row(task_id="clockin_daxiang_1830", tool_name="tms_query"),
    ]
    runtime = SimpleNamespace(
        memory=SimpleNamespace(list_enabled_scheduled_tasks=lambda: rows),
    )

    entries = _persisted_scheduler_allowlist(runtime, ToolRegistry())

    assert len(entries) == 1
    assert entries[0].task_id == "arrive_list_0830"
    assert entries[0].tool_name == "sync_arrive_list"
