from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from console.services.automation_invocation_output import invocation_start_feedback
from console.app_support import automation_run_feedback_message
from feishu import message_handler


def test_failed_or_unknown_initial_receipt_never_claims_running():
    for status in ("FAILED", "CANCELLED", "WRITE_OUTCOME_UNKNOWN"):
        value = invocation_start_feedback({"invocation_id": "call-1", "status": status})
        assert value["pending"] is False
        assert value["ok"] is False
        assert value["next_poll_after_ms"] == 0
    assert invocation_start_feedback({"status": "RUNNING"})["pending"] is True


def test_terminal_failure_feedback_never_claims_status_is_pending():
    failed = automation_run_feedback_message(error_code="UNMAPPED_PLUGIN_ERROR", status="FAILED")
    capability = automation_run_feedback_message(error_code="CAPABILITY_UNAVAILABLE", status="FAILED")
    unknown_write = automation_run_feedback_message(error_code="", status="WRITE_OUTCOME_UNKNOWN")
    assert "已失败" in failed and "稍后刷新" not in failed
    assert "平台功能" in capability and "已失败" in capability
    assert "核对目标数据" in unknown_write
    immediate = invocation_start_feedback({"status": "FAILED", "error_code": "CAPABILITY_UNAVAILABLE"})
    assert immediate["message"] == capability


def test_cancelling_previous_call_cannot_remove_new_call_tracking():
    key = ("chat", "actor", "builtin.arrival_stats")

    async def cancel(invocation_id, **kwargs):
        assert invocation_id == "previous-call"
        assert kwargs["sender_id"] == "actor"
        message_handler._ACTIVE_PLUGIN_INVOCATIONS[key] = "new-call"
        return {"status": "CANCELLED", "invocation_id": invocation_id}

    with (patch.dict(message_handler._ACTIVE_PLUGIN_INVOCATIONS, {key: "previous-call"}, clear=True),
          patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", SimpleNamespace(cancel_feishu_invocation=cancel)),
          patch.object(message_handler, "_reply_text", new_callable=AsyncMock)):
        token = message_handler._COMMAND_CONTEXT.set(message_handler.FeishuCommandContext("cancel-event", "actor", "chat"))
        try:
            assert asyncio.run(message_handler._cancel_direct_plugin("取消统计", chat_id="chat", sender_id="actor"))
            assert message_handler._ACTIVE_PLUGIN_INVOCATIONS[key] == "new-call"
        finally:
            message_handler._COMMAND_CONTEXT.reset(token)


def test_cancel_only_selects_current_senders_calls():
    with (patch.dict(message_handler._ACTIVE_PLUGIN_INVOCATIONS, {("chat", "other", "builtin.arrival_stats"): "other-call"}, clear=True),
          patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", None)):
        assert asyncio.run(message_handler._cancel_direct_plugin("取消统计", chat_id="chat", sender_id="actor")) is False
