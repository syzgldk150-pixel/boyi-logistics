"""Business-facing messages for managed Feishu automation runs."""

from __future__ import annotations

import re
from typing import Any


TOOL_DISPLAY_NAMES = {
    "sync_scan_codes": "扫描任务",
    "sync_arrival_stats": "统计到货数据任务",
    "sync_arrive_list": "到货清单任务",
    "sync_daily_send_orders": "当日寄件数据任务",
    "sync_yunda_dispatch_forecast": "韵达派件预测任务",
    "sync_yunda_send_waybills": "韵达寄件运单任务",
    "self_pickup_problem_upload": "自提到货问题件任务",
    "split_pending_problem_upload": "分批问题件任务",
}


def accepted_result_pending_message(task_name: str) -> str:
    """Describe a post-commit wait failure without inviting a duplicate run."""

    return (
        f"{task_name}已提交，当前结果暂未返回，任务仍会在后台继续执行。"
        "请勿重复触发，完成结果可在自动化页面查看。"
    )


def _public_failure_detail(value: Any) -> str:
    """Keep plain business explanations while dropping internal diagnostics."""

    detail = " ".join(str(value or "").split())[:160]
    if not detail or re.search(r"[A-Za-z_]|[/\\]", detail):
        return ""
    return detail


def automation_result_reply(
    *,
    task_name: str,
    result: dict[str, Any],
) -> tuple[str, str]:
    """Render one terminal project result without control-plane jargon."""

    status = str(result.get("status") or "").strip().upper()
    reason = str(result.get("error_summary") or "").strip()
    problem_code = str(
        result.get("public_problem_code") or result.get("error_code") or ""
    ).strip().upper()
    if status in {"WAITING_APPROVAL", "PENDING_APPROVAL"}:
        return f"{task_name}已提交，正在等待审批。", "automation_project_waiting_approval"
    if status == "COMPLETED":
        return f"{task_name}已完成。", "automation_project_completed"
    if status == "BLOCKED_LOGIN":
        return (
            f"{task_name}未完成：绑定的业务账号需要重新登录。",
            "automation_project_blocked_login",
        )
    if status == "CANCELLED":
        return f"{task_name}已取消。", "automation_project_cancelled"
    if problem_code == "WRITE_OUTCOME_UNKNOWN" or "WRITE_OUTCOME_UNKNOWN" in reason:
        return (
            f"{task_name}的目标表可能已更新，但最终核验暂未确认。"
            "系统已保留核验记录，新任务不会因此被阻塞。",
            "automation_project_write_outcome_unknown",
        )
    if status == "PARTIAL":
        detail = _public_failure_detail(reason)
        return (
            (
                f"{task_name}部分完成：{detail}"
                if detail
                else f"{task_name}部分完成，请在自动化页面查看未完成部分。"
            ),
            "automation_project_partial",
        )
    if status in {"BLOCKED_DATA", "FAILED_RETRYABLE", "FAILED_TERMINAL"}:
        if (
            task_name == TOOL_DISPLAY_NAMES["self_pickup_problem_upload"]
            and "SELECTION_PREVIEW_EXPIRED" in reason
        ):
            return (
                "候选清单已变化，请重新发送“自提到货问题件”；本次未写入。",
                "self_pickup_preview_stale",
            )
        if (
            task_name == TOOL_DISPLAY_NAMES["split_pending_problem_upload"]
            and "ACTION_VALUE_ERROR:FRAME=action.py:642:run_action" in reason
        ):
            return (
                "分批候选清单或执行参数已变化，请重新发送“分批”生成最新清单；"
                "本次未执行外部写入。",
                "split_preview_stale",
            )
        detail = _public_failure_detail(reason) or {
            "BLOCKED_DATA": "数据或资源校验未通过，请检查账号和数据表配置后重试。",
            "FAILED_RETRYABLE": "执行暂时失败，系统会按既定策略自动重试。",
            "FAILED_TERMINAL": "执行未完成，请在自动化页面查看处理建议。",
        }[status]
        return f"{task_name}执行失败：{detail}", "automation_project_failed"
    return (
        f"{task_name}未完成，结果暂时无法确认，请在自动化页面查看状态。",
        "automation_project_status",
    )


__all__ = [
    "TOOL_DISPLAY_NAMES",
    "accepted_result_pending_message",
    "automation_result_reply",
]
