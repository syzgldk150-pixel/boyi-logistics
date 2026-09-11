"""Public output of one invocation, never recovered from a previous tool log."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from shared.redaction import redact_sensitive, redact_text
from shared.invocation_summary import invocation_count_summary
from shared.plugin_invocation_repository import ACTIVE_INVOCATION_STATUSES
from console.app_support import automation_run_feedback_message


def invocation_output_lines(invocation: Mapping[str, Any], *, state_label: str) -> list[str]:
    lines = [f"状态：{state_label}"]
    if counts := invocation_count_summary(invocation):
        lines.append(counts)
    summary = invocation.get("error_summary")
    if summary:
        lines.append(redact_text(summary))
    # Keep the actual failure identifiable even when the plugin message is generic.
    error_code = invocation.get("error_code")
    if isinstance(error_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", error_code):
        lines.append(f"错误代码：{error_code}")
    result = invocation.get("result")
    if isinstance(result, Mapping):
        # The result belongs to this exact invocation; missing counts stay missing.
        for field in ("summary", "message"):
            if isinstance(result.get(field), str) and result[field].strip():
                lines.append(redact_text(result[field]))
        data = result.get("data")
        if data is not None:
            lines.append(json.dumps(redact_sensitive(data), ensure_ascii=False, default=str))
    return lines


def invocation_start_feedback(invocation: Mapping[str, Any]) -> dict[str, Any]:
    status = invocation.get("status")
    pending = status in ACTIVE_INVOCATION_STATUSES
    completed = status == "COMPLETED"
    return {
        "ok": pending or completed, "pending": pending, "status": status,
        "cancelled": status == "CANCELLED",
        "title": "执行已发起" if pending else "已完成" if completed else "本次执行已结束",
        "message": ("脚本直接执行，完成后显示本次结果。" if pending else
                    invocation_count_summary(invocation) if completed else
                    automation_run_feedback_message(error_code=invocation.get("error_code"), status=status)),
        "invocation_id": invocation.get("invocation_id"),
        "next_poll_after_ms": 1000 if pending else 0,
    }
