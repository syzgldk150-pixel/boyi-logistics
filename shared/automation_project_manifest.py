"""Reviewed identities used only to migrate the pre-plugin first-party tasks.

This module is deliberately pure.  It is the shared source of truth for the
project identifier written to ``scheduled_tasks.automation_id`` and never
derives a project from a clock suffix or a display name.  These templates are
not plugin manifests and must never seed a normal repeated installation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from shared.finance.sources import enabled_finance_account_ids
from shared.scheduled_task_contracts import APPROVED_SCHEDULED_TASK_PROFILES


TRUSTED_AUTOMATION_ENTRYPOINTS = frozenset({"scheduler", "console", "feishu", "webhook"})


@dataclass(frozen=True)
class FirstPartyMigrationInstanceTemplate:
    automation_id: str
    tool_name: str
    legacy_arguments: Mapping[str, Any]
    allowed_entrypoints: frozenset[str]
    legacy_account_bindings: Mapping[str, str] = field(default_factory=dict)
    scheduled_task_ids: frozenset[str] = frozenset()
    legacy_dynamic_argument_rules: Mapping[str, str] = field(default_factory=dict)
    resource_bindings: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class AutomationProjectInstanceDefinition:
    """Runtime definition assembled from one installed instance and system config."""

    automation_id: str
    plugin_id: str
    tool_name: str
    argument_templates: Mapping[str, Mapping[str, Any]]
    dynamic_argument_resolvers: Mapping[str, Mapping[str, str]]
    account_bindings: Mapping[str, str | tuple[str, ...]]
    allowed_entrypoints: frozenset[str]
    project_config: Mapping[str, Any] = field(default_factory=dict)
    resource_bindings: Mapping[str, str | tuple[str, ...]] = field(default_factory=dict)


def _scheduled_definition(
    automation_id: str,
    *,
    allowed_entrypoints: frozenset[str],
    legacy_account_bindings: Mapping[str, str] | None = None,
    resource_bindings: Mapping[str, str | tuple[str, ...]] | None = None,
) -> FirstPartyMigrationInstanceTemplate:
    profile = APPROVED_SCHEDULED_TASK_PROFILES[automation_id]
    arguments = dict(profile.approved_arguments)
    account_bindings = {
        key: str(value)
        for key, value in arguments.items()
        if (key == "account_id" or key.endswith("_account_id")) and isinstance(value, str) and value.strip()
    }
    account_bindings.update(dict(legacy_account_bindings or {}))
    return FirstPartyMigrationInstanceTemplate(
        automation_id=automation_id,
        tool_name=profile.tool_name,
        legacy_arguments=arguments,
        allowed_entrypoints=allowed_entrypoints,
        legacy_account_bindings=account_bindings,
        scheduled_task_ids=frozenset(profile.approved_task_ids),
        legacy_dynamic_argument_rules=dict(profile.dynamic_argument_rules),
        resource_bindings=dict(resource_bindings or {}),
    )


_SCHEDULER_CONSOLE = frozenset({"scheduler", "console"})
_SCHEDULER_CONSOLE_FEISHU = frozenset({"scheduler", "console", "feishu"})
_CONSOLE_FEISHU = frozenset({"console", "feishu"})
_CONSOLE_FEISHU_WEBHOOK = frozenset({"console", "feishu", "webhook"})

_FINANCE_ACCOUNT_ROLE_NAMES = (
    "finance_quote_source",
    "finance_daxiang_s_source",
    "finance_self_pickup_source",
)
_LEGACY_FINANCE_ACCOUNT_BINDINGS = dict(
    zip(
        _FINANCE_ACCOUNT_ROLE_NAMES,
        enabled_finance_account_ids(),
        strict=True,
    )
)


FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES: Mapping[str, FirstPartyMigrationInstanceTemplate] = {
    "send_order": _scheduled_definition(
        "send_order",
        allowed_entrypoints=_SCHEDULER_CONSOLE_FEISHU,
        resource_bindings={
            "feishu_route": "automation.feishu_route.send_order",
            "send_order_bitable": "phase7.send_order_bitable",
        },
    ),
    "delivery_status": _scheduled_definition(
        "delivery_status",
        allowed_entrypoints=frozenset({"scheduler", "console", "webhook"}),
        resource_bindings={
            "webhook_route": "phase7.delivery_status_webhook",
            "delivery_status_bitable": "phase7.delivery_status_bitable",
        },
    ),
    "daily_sign": _scheduled_definition(
        "daily_sign",
        allowed_entrypoints=_SCHEDULER_CONSOLE,
        resource_bindings={
            "daily_sign_bitable": "phase7.daily_sign_bitable",
            "daily_sign_sheet": "phase7.daily_sign_sheet",
        },
    ),
    "site_send": _scheduled_definition(
        "site_send",
        allowed_entrypoints=_SCHEDULER_CONSOLE,
        resource_bindings={
            "site_send_bitable": "phase7.site_send_bitable",
            "site_send_sheet": "phase7.site_send_sheet",
        },
    ),
    "customer_problems_shadow": FirstPartyMigrationInstanceTemplate(
        automation_id="customer_problems_shadow",
        tool_name="sync_customer_service_problems",
        legacy_arguments={"direction": "both"},
        allowed_entrypoints=_SCHEDULER_CONSOLE,
        scheduled_task_ids=frozenset({"customer_problems_shadow"}),
        legacy_dynamic_argument_rules={},
    ),
    "clockin_daxiang": _scheduled_definition(
        "clockin_daxiang",
        allowed_entrypoints=_SCHEDULER_CONSOLE,
    ),
    "clockin_daxiang_s": _scheduled_definition(
        "clockin_daxiang_s",
        allowed_entrypoints=_SCHEDULER_CONSOLE,
    ),
    "r7_arrival_checkin": _scheduled_definition(
        "r7_arrival_checkin",
        allowed_entrypoints=_SCHEDULER_CONSOLE_FEISHU,
        resource_bindings={
            "feishu_route": "automation.feishu_route.r7_arrival_checkin",
        },
    ),
    "r7_departure_checkin": FirstPartyMigrationInstanceTemplate(
        automation_id="r7_departure_checkin",
        tool_name="r7_departure_checkin",
        legacy_arguments={
            "headless": True,
            "slow_mo_ms": 0,
            "max_login_attempts": 6,
            "status_text": "已调度",
            "verify_status_text": "装车待发",
            "class_name": "邵阳操作场-长沙",
            "departure_time_fixed": "21:30:00",
            "plate_numbers": "湘AK6980",
            "do_departure_checkin": True,
            "after_action_delay_ms": 1500,
            "daily_success_limit": 1,
            "account_id": "r7_default",
        },
        allowed_entrypoints=_SCHEDULER_CONSOLE_FEISHU,
        legacy_account_bindings={"account_id": "r7_default"},
        scheduled_task_ids=frozenset({"r7_departure_checkin"}),
        resource_bindings={
            "feishu_route": "automation.feishu_route.r7_departure_checkin",
        },
    ),
    "arrive_list": _scheduled_definition(
        "arrive_list",
        allowed_entrypoints=_SCHEDULER_CONSOLE_FEISHU,
        resource_bindings={
            "feishu_route": "automation.feishu_route.arrive_list",
            "arrive_primary_sheet": "phase7.arrive_primary_sheet",
            "arrive_secondary_sheet": "phase7.arrive_secondary_sheet",
        },
    ),
    "yunda_dispatch_forecast": _scheduled_definition(
        "yunda_dispatch_forecast",
        allowed_entrypoints=_SCHEDULER_CONSOLE_FEISHU,
        resource_bindings={
            "feishu_route": "automation.feishu_route.yunda_dispatch_forecast",
            "dispatch_forecast_bitable": "phase7.yunda_dispatch_forecast_bitable",
        },
    ),
    "yunda_send_waybills": _scheduled_definition(
        "yunda_send_waybills",
        allowed_entrypoints=_SCHEDULER_CONSOLE_FEISHU,
        resource_bindings={
            "feishu_route": "automation.feishu_route.yunda_send_waybills",
            "send_waybills_bitable": "phase7.yunda_send_waybills_bitable",
            "send_waybills_sheet": "phase7.yunda_send_waybills_sheet",
        },
    ),
    "scan_codes": FirstPartyMigrationInstanceTemplate(
        automation_id="scan_codes",
        tool_name="sync_scan_codes",
        legacy_arguments={
            "target_date": "",
            "account_id": "ronghui_default",
        },
        allowed_entrypoints=_CONSOLE_FEISHU_WEBHOOK,
        legacy_account_bindings={"account_id": "ronghui_default"},
        resource_bindings={
            "webhook_route": "phase7.scan_webhook",
            "feishu_route": "automation.feishu_route.scan_codes",
        },
    ),
    "arrival_stats": FirstPartyMigrationInstanceTemplate(
        automation_id="arrival_stats",
        tool_name="sync_arrival_stats",
        legacy_arguments={
            "account_id": "ronghui_default",
            "pending_sheet_disabled": True,
        },
        allowed_entrypoints=_CONSOLE_FEISHU_WEBHOOK,
        legacy_account_bindings={"account_id": "ronghui_default"},
        resource_bindings={
            "webhook_route": "phase7.stats_webhook",
            "feishu_route": "automation.feishu_route.arrival_stats",
            "arrival_stats_primary_sheet": "phase7.arrive_primary_sheet",
            "arrival_stats_secondary_sheet": "phase7.arrive_secondary_sheet",
            "arrival_stats_archive_sheet": "phase7.stats_archive_sheet",
            "arrival_stats_split_pending_sheet": (
                "phase7.split_pending_target_sheet"
            ),
        },
    ),
    "self_pickup_problem_upload": FirstPartyMigrationInstanceTemplate(
        automation_id="self_pickup_problem_upload",
        tool_name="self_pickup_problem_upload",
        legacy_arguments={
            "dry_run": True,
            "account_id": "ronghui_self_pickup_problem",
            "daxiang_s_account_id": "ronghui_daxiang_s",
        },
        allowed_entrypoints=_CONSOLE_FEISHU,
        legacy_account_bindings={
            "account_id": "ronghui_self_pickup_problem",
            "daxiang_s_account_id": "ronghui_daxiang_s",
        },
        resource_bindings={
            "feishu_route": "automation.feishu_route.self_pickup_problem_upload",
            "self_pickup_source_sheet": "phase7.self_pickup_source_sheet",
        },
    ),
    "split_pending_problem_upload": FirstPartyMigrationInstanceTemplate(
        automation_id="split_pending_problem_upload",
        tool_name="split_pending_problem_upload",
        legacy_arguments={"dry_run": False, "account_id": "ronghui_default"},
        allowed_entrypoints=_SCHEDULER_CONSOLE_FEISHU,
        legacy_account_bindings={"account_id": "ronghui_default"},
        resource_bindings={
            "feishu_route": "automation.feishu_route.split_pending_problem_upload",
            "split_pending_source_sheet": "phase7.split_pending_source_sheet",
            "split_pending_target_sheet": "phase7.split_pending_target_sheet",
        },
    ),
    "finance_bills": _scheduled_definition(
        "finance_bills",
        allowed_entrypoints=_SCHEDULER_CONSOLE,
        legacy_account_bindings=_LEGACY_FINANCE_ACCOUNT_BINDINGS,
    ),
    "finance_startup_catchup": _scheduled_definition(
        "finance_startup_catchup",
        allowed_entrypoints=_SCHEDULER_CONSOLE,
        legacy_account_bindings=_LEGACY_FINANCE_ACCOUNT_BINDINGS,
    ),
}


def get_first_party_automation_project(
    automation_id: str,
) -> FirstPartyMigrationInstanceTemplate | None:
    return FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES.get(str(automation_id or "").strip())


def automation_id_for_reviewed_task(task_id: str) -> str | None:
    """Return an explicit reviewed mapping; never infer from a suffix."""

    normalized = str(task_id or "").strip()
    for automation_id, definition in FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES.items():
        if normalized in definition.scheduled_task_ids:
            return automation_id
    return None
