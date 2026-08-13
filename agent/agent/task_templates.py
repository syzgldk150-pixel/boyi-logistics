"""Safe scheduler seed templates derived from governed task contracts.

Persisted rows remain administrator-owned.  These templates are only used to
insert missing rows, and every new row starts disabled so a fresh database
cannot begin business automation before accounts and resources are verified.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from shared.scheduled_task_contracts import APPROVED_SCHEDULED_TASK_PROFILES


_TIME_SUFFIX_RE = re.compile(r"_(?P<hour>[01]\d|2[0-3])(?P<minute>[0-5]\d)$")
_GROUP_DISPLAY_NAMES = {
    "arrive_list": "到货清单",
    "daily_sign": "每日应签",
    "delivery_status": "派送状态",
    "send_order": "当日寄件数据",
    "site_send": "网点出港清单",
    "yunda_send_waybills": "韵达寄件运单",
}


def _cron_and_label(task_id: str) -> tuple[str, str]:
    match = _TIME_SUFFIX_RE.search(task_id)
    if match is None:
        raise RuntimeError(f"Governed scheduled task id has no HHMM suffix: {task_id}")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    return f"{minute} {hour} * * *", f"{hour:02d}:{minute:02d}"


def _build_governed_schedule_templates() -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for group_id, profile in APPROVED_SCHEDULED_TASK_PROFILES.items():
        if not profile.approved_task_ids:
            continue
        display_name = _GROUP_DISPLAY_NAMES.get(group_id)
        if display_name is None:
            raise RuntimeError(f"Governed scheduled task group has no display name: {group_id}")
        for task_id in sorted(profile.approved_task_ids):
            cron_expression, time_label = _cron_and_label(task_id)
            templates.append(
                {
                    "id": task_id,
                    "name": f"{display_name}-{time_label}",
                    "tool_name": profile.tool_name,
                    "tool_params": copy.deepcopy(dict(profile.approved_arguments)),
                    "cron_expression": cron_expression,
                    "enabled": False,
                    "source": "control-plane-v1",
                }
            )
    return templates


GOVERNED_SCHEDULED_TASK_TEMPLATES = _build_governed_schedule_templates()
GOVERNED_SCHEDULED_TASK_IDS = frozenset(task["id"] for task in GOVERNED_SCHEDULED_TASK_TEMPLATES)

# These rows are useful configuration placeholders but are deliberately not
# scheduler-policy exemptions.  A fresh database receives them disabled; an
# existing administrator choice is never overwritten by seed operations.
STATIC_DISABLED_SCHEDULED_TASK_TEMPLATES = [
    {
        "id": "customer_problems_shadow",
        "name": "客服问题件事项影子采集",
        "tool_name": "sync_customer_service_problems",
        "tool_params": {"direction": "both"},
        "cron_expression": "*/15 * * * *",
        "enabled": False,
        "source": "control-plane-pilot",
    },
    {
        "id": "finance_bills_0010",
        "name": "财务账单同步-00:10",
        "tool_name": "sync_finance_bills",
        "tool_params": {
            "mode": "sync",
            "platform": "ronghui",
            "rescan_days": 7,
        },
        "cron_expression": "10 0 * * *",
        "enabled": False,
        "source": "finance-ledger",
    },
    {
        "id": "yunda_dispatch_forecast_1700",
        "name": "韵达网点派件量预测-17:00",
        "tool_name": "sync_yunda_dispatch_forecast",
        "tool_params": {
            "account_id": "yunda_default",
            "dest_brch": "56739382",
        },
        "cron_expression": "0 17 * * *",
        "enabled": False,
        "source": "yunda-dispatch-forecast",
    },
]

# Compatibility name retained for the existing seed API.  External writes
# such as clock-in and R7 check-in are intentionally absent: they must never be
# introduced by an automatic template seed.
PHASE7_SCHEDULED_TASK_TEMPLATES = [
    *GOVERNED_SCHEDULED_TASK_TEMPLATES,
    *STATIC_DISABLED_SCHEDULED_TASK_TEMPLATES,
]
