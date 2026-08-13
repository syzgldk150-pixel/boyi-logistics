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


@dataclass(frozen=True)
class ScheduledTaskProfile:
    tool_name: str
    tool_version: str
    approved_arguments: Mapping[str, Any]
    dynamic_argument_rules: Mapping[str, str]
    approved_task_ids: frozenset[str] = frozenset()
    operation_type: str = "internal_projection_write"


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
        tool_version="2.0.0",
        approved_arguments={
            "r13_account_id": "r13_default",
            "problem_account_id": "ronghui_daxiang_s",
            "sign_account_id": "ronghui_daxiang_s",
            "detail_account_id": "ronghui_default",
            "days": 7,
        },
        dynamic_argument_rules={},
        approved_task_ids=_DAILY_SIGN_TASK_IDS,
    ),
    "site_send": ScheduledTaskProfile(
        tool_name="sync_site_send_list",
        tool_version="1.0.0",
        approved_arguments={"account_id": "ronghui_default"},
        dynamic_argument_rules={},
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
        approved_arguments={"account_id": "ronghui_default", "trigger_flow": False},
        dynamic_argument_rules={},
    ),
    "yunda_dispatch_forecast": ScheduledTaskProfile(
        tool_name="sync_yunda_dispatch_forecast",
        tool_version="1.0.0",
        approved_arguments={"account_id": "yunda_default", "dest_brch": "56739382"},
        dynamic_argument_rules={},
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
    minute, hour = _parse_daily_cron(cron_expression)
    if time_suffix is not None and time_suffix != (hour, minute):
        raise ScheduledTaskContractError("TASK_ID_CRON_MISMATCH")

    profile = APPROVED_SCHEDULED_TASK_PROFILES[group_id]
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
    try:
        validate_arguments(tool_name, arguments)
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

    return PersistedScheduledTaskContract(
        task_id=task_id,
        tool_name=tool_name,
        tool_version=profile.tool_version,
        arguments=arguments,
        cron_expression=cron_expression,
        dynamic_argument_rules=dict(profile.dynamic_argument_rules),
    )


def _resolve_task_group(task_id: str) -> tuple[str | None, tuple[int, int] | None]:
    for group_id, profile in APPROVED_SCHEDULED_TASK_PROFILES.items():
        if task_id not in profile.approved_task_ids:
            continue
        time_match = _TIME_SUFFIX_RE.fullmatch(task_id.rsplit("_", 1)[-1])
        if time_match:
            return group_id, (int(time_match.group("hour")), int(time_match.group("minute")))
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
