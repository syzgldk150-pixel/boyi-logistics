from __future__ import annotations
import pytest

from agent.tool_registry import ToolRegistry
from shared.scheduled_task_contracts import (
    APPROVED_SCHEDULED_TASK_PROFILES,
    ScheduledTaskContractError,
    validate_persisted_scheduled_task,
)


EXPECTED_APPROVED_TASK_IDS = {
    "clockin_daxiang": {"clockin_daxiang_1830"},
    "clockin_daxiang_s": {"clockin_daxiang_s_1833"},
    "finance_bills": {"finance_bills_0010"},
    "finance_startup_catchup": {"finance_startup_catchup"},
    "yunda_dispatch_forecast": {"yunda_dispatch_forecast_1700"},
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
    "r7_arrival_checkin": {
        "r7_arrival_checkin_0900",
        "r7_arrival_checkin_0930",
        "r7_arrival_checkin_1000",
        "r7_arrival_checkin_1030",
        "r7_arrival_checkin_1100",
        "r7_arrival_checkin_1130",
        "r7_arrival_checkin_1200",
        "r7_arrival_checkin_1230",
        "r7_arrival_checkin_1300",
        "r7_arrival_checkin_1330",
        "r7_arrival_checkin_1400",
        "r7_arrival_checkin_1430",
        "r7_arrival_checkin_1900",
    },
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
    assert APPROVED_SCHEDULED_TASK_PROFILES["arrival_stats"].seed_governed_template
    assert not APPROVED_SCHEDULED_TASK_PROFILES["yunda_dispatch_forecast"].seed_governed_template
    assert not APPROVED_SCHEDULED_TASK_PROFILES["finance_bills"].seed_governed_template
    assert not APPROVED_SCHEDULED_TASK_PROFILES["finance_startup_catchup"].seed_governed_template
    assert not APPROVED_SCHEDULED_TASK_PROFILES["r7_arrival_checkin"].seed_governed_template


def test_only_two_complete_clock_contracts_are_code_approved() -> None:
    expected = {
        "clockin_daxiang": {
            "task_id": "clockin_daxiang_1830",
            "account_id": "ronghui_default",
            "sitecode": "7390004",
            "sitefbcode": "73901",
            "sitename": "邵阳大祥站",
            "sitefbname": "邵阳操作场",
        },
        "clockin_daxiang_s": {
            "task_id": "clockin_daxiang_s_1833",
            "account_id": "ronghui_daxiang_s",
            "sitecode": "7390017",
            "sitefbcode": "73901",
            "sitename": "邵阳大祥S站",
            "sitefbname": "邵阳操作场",
        },
    }

    for group_id, locked in expected.items():
        profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
        assert profile.tool_name == "clock_in_dual"
        assert profile.tool_version == "1.1.0"
        assert profile.operation_type == "external_write"
        assert profile.approved_task_ids == frozenset({locked["task_id"]})
        assert profile.dynamic_argument_rules == {}
        assert profile.approved_arguments == {
            "account_id": locked["account_id"],
            "sitecode": locked["sitecode"],
            "sitefbcode": locked["sitefbcode"],
            "sitename": locked["sitename"],
            "sitefbname": locked["sitefbname"],
            "first_type": "交件到港",
            "second_type": "接件离港",
            "delay_seconds": 2,
        }


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
            cron_expression=profile.cron_expression or _cron_for_task_id(task_id),
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
                "account_id": "ronghui_daxiang_s",
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
        (
            "finance_startup_catchup",
            "finance_startup_catchup",
            {
                **dict(
                    APPROVED_SCHEDULED_TASK_PROFILES[
                        "finance_startup_catchup"
                    ].approved_arguments
                ),
                "rescan_days": 6,
            },
        ),
        (
            "r7_arrival_checkin",
            "r7_arrival_checkin_0900",
            {
                **dict(
                    APPROVED_SCHEDULED_TASK_PROFILES[
                        "r7_arrival_checkin"
                    ].approved_arguments
                ),
                "daily_success_limit": 2,
            },
        ),
        (
            "clockin_daxiang",
            "clockin_daxiang_1830",
            {
                **dict(APPROVED_SCHEDULED_TASK_PROFILES["clockin_daxiang"].approved_arguments),
                "account_id": "ronghui_daxiang_s",
            },
        ),
        (
            "clockin_daxiang_s",
            "clockin_daxiang_s_1833",
            {
                **dict(APPROVED_SCHEDULED_TASK_PROFILES["clockin_daxiang_s"].approved_arguments),
                "sitecode": "7390004",
            },
        ),
        (
            "clockin_daxiang_s",
            "clockin_daxiang_s_1833",
            {
                **dict(APPROVED_SCHEDULED_TASK_PROFILES["clockin_daxiang_s"].approved_arguments),
                "delay_seconds": 3,
            },
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
        cron_expression=profile.cron_expression or _cron_for_task_id(task_id),
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
        ("clockin_daxiang_1831", "clock_in_dual"),
        ("clockin_daxiang_s_1830", "clock_in_dual"),
        ("r7_arrival_checkin", "r7_arrival_checkin"),
    ),
)
def test_unreviewed_task_id_is_never_eligible(
    task_id: str,
    tool_name: str,
) -> None:
    with pytest.raises(ScheduledTaskContractError) as error:
        _validate(_row(task_id=task_id, tool_name=tool_name))

    assert error.value.code == "TASK_GROUP_NOT_APPROVED"


@pytest.mark.parametrize(
    "task_id",
    ("clockin_daxiang_1830", "clockin_daxiang_s_1833"),
)
def test_legacy_broad_clock_tool_is_not_eligible(task_id: str) -> None:
    with pytest.raises(ScheduledTaskContractError) as error:
        _validate(
            _row(
                task_id=task_id,
                tool_name="tms_query",
                cron_expression=_cron_for_task_id(task_id),
            )
        )

    assert error.value.code == "TOOL_NOT_APPROVED_FOR_GROUP"


@pytest.mark.parametrize(
    "group_id",
    ("clockin_daxiang", "clockin_daxiang_s"),
)
def test_clock_contract_rejects_unknown_argument(group_id: str) -> None:
    profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
    task_id = next(iter(profile.approved_task_ids))
    arguments = {**dict(profile.approved_arguments), "extra": "not-approved"}

    with pytest.raises(ScheduledTaskContractError) as error:
        _validate(
            _row(
                task_id=task_id,
                tool_name=profile.tool_name,
                tool_params=arguments,
                cron_expression=_cron_for_task_id(task_id),
            )
        )

    assert error.value.code == "ARGUMENT_SCHEMA_MISMATCH"


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


def test_finance_profile_is_reviewed_in_place_but_not_auto_enabled() -> None:
    profile = APPROVED_SCHEDULED_TASK_PROFILES["finance_bills"]

    assert profile.approved_task_ids == frozenset({"finance_bills_0010"})
    assert profile.seed_governed_template is False
    assert profile.approved_arguments == {"mode": "sync", "platform": "ronghui", "rescan_days": 7}
    assert profile.dynamic_argument_rules == {"target_date": "scheduled_previous_day"}
