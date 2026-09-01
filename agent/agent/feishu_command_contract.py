"""Pure code-owned Feishu command parsing shared by admission and dispatch."""

from __future__ import annotations

import re
from typing import Any


_SCAN_CONFIRM_RE = re.compile(r"^\s*确认\s*扫描\s*$")
_SCAN_CANCEL_RE = re.compile(r"^\s*取消\s*扫描\s*$")
_APPROVAL_BIND_RE = re.compile(
    r"^绑定审批\s+([0-9A-HJKMNP-TV-Z]{10})$",
    re.IGNORECASE,
)

_TOOL_CANCEL_COMMANDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "sync_scan_codes",
        re.compile(
            r"^\s*取消\s*(?:扫描|扫描数据|扫描任务|sync_scan_codes)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "sync_arrival_stats",
        re.compile(
            r"^\s*取消\s*(?:统计|到货统计|统计到货数据|sync_arrival_stats)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "sync_arrive_list",
        re.compile(
            r"^\s*取消\s*(?:arrive[-_\s]*list|到货清单|预到达清单|sync_arrive_list)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "sync_daily_send_orders",
        re.compile(
            r"^\s*取消\s*(?:当日寄件数据|寄件数据|融辉寄件数据|sync_daily_send_orders)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "sync_yunda_dispatch_forecast",
        re.compile(
            r"^\s*取消\s*(?:韵达派件预测|派件预测|sync_yunda_dispatch_forecast)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "sync_yunda_send_waybills",
        re.compile(
            r"^\s*取消\s*(?:韵达寄件运单|韵达寄件|sync_yunda_send_waybills)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "self_pickup_problem_upload",
        re.compile(
            r"^\s*取消\s*(?:自提到货问题件|自提问题件|self_pickup_problem_upload)\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "split_pending_problem_upload",
        re.compile(
            r"^\s*取消\s*(?:分批|split_pending_problem_upload)\s*$",
            re.IGNORECASE,
        ),
    ),
)


def is_scan_confirm_text(value: Any) -> bool:
    return bool(_SCAN_CONFIRM_RE.fullmatch(str(value or "")))


def is_scan_cancel_text(value: Any) -> bool:
    return bool(_SCAN_CANCEL_RE.fullmatch(str(value or "")))


def cancel_tool_name_from_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    for tool_name, pattern in _TOOL_CANCEL_COMMANDS:
        if pattern.fullmatch(normalized):
            return tool_name
    return None


def match_feishu_approval_binding(value: Any) -> re.Match[str] | None:
    return _APPROVAL_BIND_RE.fullmatch(str(value or "").strip())


def is_unconditional_host_feishu_command_text(value: Any) -> bool:
    """Return whether a host branch always consumes this exact message."""

    return bool(
        cancel_tool_name_from_text(value)
        or is_scan_confirm_text(value)
        or match_feishu_approval_binding(value)
    )
