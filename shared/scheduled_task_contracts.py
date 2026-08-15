"""Pure validation for scheduler tasks eligible for policy exemption.

The database row is configuration, not authority.  A row becomes eligible only
when its task group, tool version, operation type, approval mode, arguments,
and daily cron expression all match this code-owned contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


_DAILY_CRON_RE = re.compile(r"^(?P<minute>\d{1,2}) (?P<hour>\d{1,2}) \* \* \*$")
_TIME_SUFFIX_RE = re.compile(r"^(?P<hour>[01]\d|2[0-3])(?P<minute>[0-5]\d)$")

# Code-owned dynamic arguments do not belong in persisted task configuration,
# but the registry schema still validates the fully compiled invocation.  These
# values are validation-only witnesses for the declared resolver output shape:
# they are never returned, persisted, or executed.
_DYNAMIC_SCHEMA_VALIDATION_VALUES: Mapping[str, Any] = {
    "current_business_day": "2000-01-01",
    "scheduled_previous_day": "2000-01-01",
}


@dataclass(frozen=True)
class ScheduledTaskProfile:
    tool_name: str
    tool_version: str
    approved_arguments: Mapping[str, Any]
    dynamic_argument_rules: Mapping[str, str]
    approved_task_ids: frozenset[str] = frozenset()
    operation_type: str = "internal_projection_write"
    seed_governed_template: bool = True
    cron_expression: str | None = None


@dataclass(frozen=True)
class PersistedScheduledTaskContract:
    task_id: str
    tool_name: str
    tool_version: str
    arguments: Mapping[str, Any]
    cron_expression: str
    dynamic_argument_rules: Mapping[str, str]


class ScheduledTaskContractError(ValueError):
    """A persisted task is not eligible for scheduler exemption."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Changing a governed tool version deliberately requires a matching code
# review here.  Merely editing a database row or registry entry cannot expand
# the scheduler's approval exemption.
_ARRIVE_LIST_TASK_IDS = frozenset(
    {"arrive_list_0830", "arrive_list_0900", "arrive_list_0930"}
)
_DAILY_SIGN_TASK_IDS = frozenset(
    {
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
    }
)
_DELIVERY_STATUS_TASK_IDS = frozenset(
    {
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
    }
)
_SITE_SEND_TASK_IDS = frozenset(
    {
        "site_send_0500",
        "site_send_0530",
        "site_send_1800",
        "site_send_1830",
        "site_send_1900",
        "site_send_1930",
        "site_send_2000",
        "site_send_2030",
        "site_send_2100",
    }
)
_R7_ARRIVAL_TASK_IDS = frozenset(
    {
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
    }
)
R7_ARRIVAL_ARGUMENTS: Mapping[str, Any] = {
    "headless": True,
    "flow_mode": 1,
    "account_id": "r7_default",
    "slow_mo_ms": 0,
    "status_text": "车辆到达",
    "max_login_attempts": 6,
    "verify_status_text": "已到达",
    "daily_success_limit": 1,
    "after_action_delay_ms": 1500,
    "do_arrive_wait_unload": True,
}


# These two clock-in profiles are cutover/bootstrap contracts for the reviewed
# production rows. Runtime exemption authority lives in the persisted,
# super-admin-managed task approval policy and its exact contract hash. The
# values below only prove that migration preserves the legacy business meaning;
# they do not grant a permanent exemption by task ID.
CLOCK_IN_DAXIANG_ARGUMENTS: Mapping[str, Any] = {
    "account_id": "ronghui_default",
    "sitecode": "7390004",
    "sitefbcode": "73901",
    "sitename": "邵阳大祥站",
    "sitefbname": "邵阳操作场",
    "first_type": "交件到港",
    "second_type": "接件离港",
    "delay_seconds": 2,
}
CLOCK_IN_DAXIANG_S_ARGUMENTS: Mapping[str, Any] = {
    "account_id": "ronghui_daxiang_s",
    "sitecode": "7390017",
    "sitefbcode": "73901",
    "sitename": "邵阳大祥S站",
    "sitefbname": "邵阳操作场",
    "first_type": "交件到港",
    "second_type": "接件离港",
    "delay_seconds": 2,
}


APPROVED_SCHEDULED_TASK_PROFILES: Mapping[str, ScheduledTaskProfile] = {
    "send_order": ScheduledTaskProfile(
        tool_name="sync_daily_send_orders",
        tool_version="1.0.0",
        approved_arguments={"account_id": "price_default"},
        dynamic_argument_rules={},
        approved_task_ids=frozenset({"send_order_2359"}),
    ),
    "delivery_status": ScheduledTaskProfile(
        tool_name="sync_delivery_status",
        tool_version="1.0.0",
        approved_arguments={"account_id": "ronghui_default"},
        dynamic_argument_rules={},
        approved_task_ids=_DELIVERY_STATUS_TASK_IDS,
    ),
    "daily_sign": ScheduledTaskProfile(
        tool_name="sync_daily_should_sign",
        tool_version="2.1.0",
        approved_arguments={
            "r13_account_id": "r13_default",
            "account_id": "ronghui_daxiang_s",
            "days": 7,
        },
        dynamic_argument_rules={},
        approved_task_ids=_DAILY_SIGN_TASK_IDS,
    ),
    "site_send": ScheduledTaskProfile(
        tool_name="sync_site_send_list",
        tool_version="1.0.0",
        approved_arguments={"account_id": "ronghui_default"},
        dynamic_argument_rules={"target_date": "current_business_day"},
        approved_task_ids=_SITE_SEND_TASK_IDS,
    ),
    "arrive_list": ScheduledTaskProfile(
        tool_name="sync_arrive_list",
        tool_version="1.0.0",
        approved_arguments={"account_id": "ronghui_default"},
        dynamic_argument_rules={},
        approved_task_ids=_ARRIVE_LIST_TASK_IDS,
    ),
    "arrival_stats": ScheduledTaskProfile(
        tool_name="sync_arrival_stats",
        tool_version="1.0.0",
        approved_arguments={
            "account_id": "ronghui_default",
            "pending_sheet_disabled": True,
        },
        dynamic_argument_rules={},
    ),
    "yunda_dispatch_forecast": ScheduledTaskProfile(
        tool_name="sync_yunda_dispatch_forecast",
        tool_version="1.0.0",
        approved_arguments={"account_id": "yunda_default", "dest_brch": "56739382"},
        dynamic_argument_rules={},
        approved_task_ids=frozenset({"yunda_dispatch_forecast_1700"}),
        # This c7 production row is reviewed in place.  A missing row remains
        # only the existing disabled static placeholder; it must never be
        # introduced as enabled merely because it is policy-eligible.
        seed_governed_template=False,
    ),
    "yunda_send_waybills": ScheduledTaskProfile(
        tool_name="sync_yunda_send_waybills",
        tool_version="1.0.0",
        approved_arguments={"account_id": "yunda_default", "ensure_fields": False},
        dynamic_argument_rules={},
        approved_task_ids=frozenset({"yunda_send_waybills_2355"}),
    ),
    "finance_bills": ScheduledTaskProfile(
        tool_name="sync_finance_bills",
        tool_version="1.0.0",
        approved_arguments={"mode": "sync", "platform": "ronghui", "rescan_days": 7},
        dynamic_argument_rules={"target_date": "scheduled_previous_day"},
        approved_task_ids=frozenset({"finance_bills_0010"}),
        # Preserve an existing reviewed schedule, while keeping fresh installs
        # and absent production rows disabled through the static seed template.
        seed_governed_template=False,
    ),
    "finance_startup_catchup": ScheduledTaskProfile(
        tool_name="sync_finance_bills",
        tool_version="1.0.0",
        approved_arguments={
            "mode": "sync",
            "platform": "ronghui",
            "rescan_days": 7,
            "_startup_catchup": True,
        },
        dynamic_argument_rules={},
        approved_task_ids=frozenset({"finance_startup_catchup"}),
        seed_governed_template=False,
        cron_expression="@startup",
    ),
    "r7_arrival_checkin": ScheduledTaskProfile(
        tool_name="r7_arrival_checkin",
        tool_version="1.0.0",
        approved_arguments=R7_ARRIVAL_ARGUMENTS,
        dynamic_argument_rules={},
        approved_task_ids=_R7_ARRIVAL_TASK_IDS,
        operation_type="external_write",
        # These exact Console-expanded production rows are reviewed in place.
        # They are never introduced on a fresh installation.
        seed_governed_template=False,
    ),
    "clockin_daxiang": ScheduledTaskProfile(
        tool_name="clock_in_dual",
        tool_version="1.1.0",
        approved_arguments=CLOCK_IN_DAXIANG_ARGUMENTS,
        dynamic_argument_rules={},
        approved_task_ids=frozenset({"clockin_daxiang_1830"}),
        operation_type="external_write",
    ),
    "clockin_daxiang_s": ScheduledTaskProfile(
        tool_name="clock_in_dual",
        tool_version="1.1.0",
        approved_arguments=CLOCK_IN_DAXIANG_S_ARGUMENTS,
        dynamic_argument_rules={},
        approved_task_ids=frozenset({"clockin_daxiang_s_1833"}),
        operation_type="external_write",
    ),
}


def validate_persisted_scheduled_task(
    row: Mapping[str, Any],
    *,
    capability: Mapping[str, Any] | None,
    validate_arguments: Callable[[str, Any], None],
    enabled_finance_platforms: Sequence[str] = (),
) -> PersistedScheduledTaskContract:
    """Return the exact allowlist contract for one enabled persisted row.

    Validation errors are intentionally precise but contain no task arguments,
    so callers may log the code without exposing persisted business data.
    """

    if not isinstance(row, Mapping):
        raise ScheduledTaskContractError("INVALID_ROW")
    enabled = row.get("enabled")
    if not (enabled is True or type(enabled) is int and enabled == 1):
        raise ScheduledTaskContractError("TASK_DISABLED")

    task_id = str(row.get("id") or "").strip()
    cron_expression = str(row.get("cron_expression") or "").strip()
    group_id, time_suffix = _resolve_task_group(task_id)
    if group_id is None:
        raise ScheduledTaskContractError("TASK_GROUP_NOT_APPROVED")
    profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
    if profile.cron_expression is not None:
        if cron_expression != profile.cron_expression:
            raise ScheduledTaskContractError("TASK_CRON_NOT_APPROVED")
    else:
        minute, hour = _parse_daily_cron(cron_expression)
        if time_suffix is not None and time_suffix != (hour, minute):
            raise ScheduledTaskContractError("TASK_ID_CRON_MISMATCH")
    tool_name = str(row.get("tool_name") or "").strip()
    if tool_name != profile.tool_name:
        raise ScheduledTaskContractError("TOOL_NOT_APPROVED_FOR_GROUP")
    if not isinstance(capability, Mapping):
        raise ScheduledTaskContractError("UNKNOWN_TOOL")
    if str(capability.get("version") or "") != profile.tool_version:
        raise ScheduledTaskContractError("TOOL_VERSION_NOT_APPROVED")
    if str(capability.get("operation_type") or "") != profile.operation_type:
        raise ScheduledTaskContractError("OPERATION_TYPE_NOT_APPROVED")
    approval = capability.get("approval")
    if not isinstance(approval, Mapping) or approval.get("mode") != "schedule_allowlist":
        raise ScheduledTaskContractError("SCHEDULE_APPROVAL_NOT_ENABLED")

    raw_arguments = row.get("tool_params")
    if not isinstance(raw_arguments, dict):
        raise ScheduledTaskContractError("INVALID_TOOL_ARGUMENTS")
    arguments = dict(raw_arguments)
    validation_arguments = _arguments_for_schema_validation(
        arguments,
        profile.dynamic_argument_rules,
    )
    try:
        validate_arguments(tool_name, validation_arguments)
    except Exception as exc:
        raise ScheduledTaskContractError("ARGUMENT_SCHEMA_MISMATCH") from exc

    # Schema validity is necessary but not sufficient for exemption.  The
    # approved static arguments are code-owned: any valid override, extra
    # field, account change, or type change must return to approval.
    if not _strict_json_equal(arguments, profile.approved_arguments):
        raise ScheduledTaskContractError("ARGUMENTS_NOT_CODE_APPROVED")

    if group_id in {"arrive_list", "arrival_stats"}:
        _validate_nested_account_scope(arguments)
    if group_id == "finance_bills":
        _validate_finance_schedule(arguments, enabled_finance_platforms)
    if group_id == "finance_startup_catchup":
        _validate_finance_startup(arguments, enabled_finance_platforms)

    return PersistedScheduledTaskContract(
        task_id=task_id,
        tool_name=tool_name,
        tool_version=profile.tool_version,
        arguments=arguments,
        cron_expression=cron_expression,
        dynamic_argument_rules=dict(profile.dynamic_argument_rules),
    )


def _arguments_for_schema_validation(
    arguments: Mapping[str, Any],
    dynamic_argument_rules: Mapping[str, str],
) -> dict[str, Any]:
    validation_arguments = dict(arguments)
    for field_name, resolver_name in dynamic_argument_rules.items():
        witness = _DYNAMIC_SCHEMA_VALIDATION_VALUES.get(resolver_name)
        if witness is None:
            raise ScheduledTaskContractError("DYNAMIC_ARGUMENT_RULE_NOT_SUPPORTED")
        validation_arguments.setdefault(field_name, witness)
    return validation_arguments


def _resolve_task_group(task_id: str) -> tuple[str | None, tuple[int, int] | None]:
    for group_id, profile in APPROVED_SCHEDULED_TASK_PROFILES.items():
        if task_id not in profile.approved_task_ids:
            continue
        time_match = _TIME_SUFFIX_RE.fullmatch(task_id.rsplit("_", 1)[-1])
        if time_match:
            return group_id, (int(time_match.group("hour")), int(time_match.group("minute")))
        if profile.cron_expression is not None:
            return group_id, None
    return None, None


def _parse_daily_cron(cron_expression: str) -> tuple[int, int]:
    match = _DAILY_CRON_RE.fullmatch(cron_expression)
    if not match:
        raise ScheduledTaskContractError("CRON_NOT_EXACT_DAILY_TIME")
    minute = int(match.group("minute"))
    hour = int(match.group("hour"))
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise ScheduledTaskContractError("CRON_TIME_OUT_OF_RANGE")
    return minute, hour


def _validate_nested_account_scope(arguments: Mapping[str, Any]) -> None:
    account_id = arguments.get("account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ScheduledTaskContractError("ACCOUNT_ID_REQUIRED")
    expected = account_id.strip()

    def visit(value: Any, *, root: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, nested_value in value.items():
                if not root and str(key) in {"account_id", "accountId"}:
                    if not isinstance(nested_value, str) or nested_value.strip() != expected:
                        raise ScheduledTaskContractError("NESTED_ACCOUNT_CONFLICT")
                visit(nested_value)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(arguments, root=True)


def _validate_finance_schedule(arguments: Mapping[str, Any], platforms: Sequence[str]) -> None:
    normalized_platforms = tuple(str(value).strip() for value in platforms if str(value).strip())
    if normalized_platforms != ("ronghui",):
        raise ScheduledTaskContractError("FINANCE_PLATFORM_NOT_PRODUCTION_READY")
    if set(arguments) != {"mode", "platform", "rescan_days"}:
        raise ScheduledTaskContractError("FINANCE_SCHEDULE_ARGUMENTS_NOT_LOCKED")
    if arguments.get("mode") != "sync" or arguments.get("platform") != "ronghui":
        raise ScheduledTaskContractError("FINANCE_SCHEDULE_MODE_NOT_APPROVED")
    if type(arguments.get("rescan_days")) is not int or arguments.get("rescan_days") != 7:
        raise ScheduledTaskContractError("FINANCE_RESCAN_RANGE_NOT_APPROVED")


def _validate_finance_startup(arguments: Mapping[str, Any], platforms: Sequence[str]) -> None:
    normalized_platforms = tuple(str(value).strip() for value in platforms if str(value).strip())
    if normalized_platforms != ("ronghui",):
        raise ScheduledTaskContractError("FINANCE_PLATFORM_NOT_PRODUCTION_READY")
    if set(arguments) != {"mode", "platform", "rescan_days", "_startup_catchup"}:
        raise ScheduledTaskContractError("FINANCE_STARTUP_ARGUMENTS_NOT_LOCKED")
    if (
        arguments.get("mode") != "sync"
        or arguments.get("platform") != "ronghui"
        or arguments.get("_startup_catchup") is not True
    ):
        raise ScheduledTaskContractError("FINANCE_STARTUP_MODE_NOT_APPROVED")
    if type(arguments.get("rescan_days")) is not int or arguments.get("rescan_days") != 7:
        raise ScheduledTaskContractError("FINANCE_RESCAN_RANGE_NOT_APPROVED")


def _strict_json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int equality coercion."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        return all(_strict_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)
