"""Run gateway-approved plugin requests from a Feishu/WeChat conversation (agent layer P1).

The conversation service hands back :class:`ConversationPluginRequest` objects:
the target, the arguments the gateway grounded in the person's words, and
whether the gateway requires the person's "确认" first. This module runs them
through the existing Feishu paths in ``message_handler`` (looked up at call time
to avoid an import cycle) and never invents its own execution route.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from agent.orchestration.agent_confirmation import CONFIRMATION_TTL_SECONDS, confirmation_prompt
from agent.plugin_conversations import ConversationPluginRequest

QUOTE_PLUGIN_ID = "quote_prices_v2"


async def run_conversation_plugins(requests: Any, *, chat_id: str) -> None:
    from feishu import message_handler as handler

    if not isinstance(requests, tuple) or not all(isinstance(item, ConversationPluginRequest) for item in requests):
        await handler._reply_text(chat_id, "插件选择无效，本次未执行。")
        return
    await asyncio.gather(*(_run_one(handler, request, chat_id) for request in requests))


async def _run_one(handler: Any, request: ConversationPluginRequest, chat_id: str) -> None:
    target = request.target
    inputs = dict(request.inputs) or None
    quote = target.plugin_id == QUOTE_PLUGIN_ID
    if request.confirm:
        # The gateway requires "确认" first: remember the exact command and ask; nothing runs now.
        context = handler._COMMAND_CONTEXT.get()
        if context is None or not context.event_id or not target.command:
            await handler._reply_text(chat_id, f"{target.title}需要确认后执行，但当前消息身份不完整，本次没有执行。",
                                      reply_type="agent_write_confirmation_rejected")
            return
        reason = request.confirmation_reason()
        handler._WRITE_CONFIRMATIONS[(context.chat_id, context.actor_id)] = {
            "text": target.command, "receive_id": chat_id, "conversation_target": target,
            "runtime_inputs": inputs, "quote_result": quote, "reason": reason,
            "expires_at": time.monotonic() + CONFIRMATION_TTL_SECONDS,
        }
        await handler._reply_text(chat_id, confirmation_prompt(reason), reply_type="agent_write_confirmation_required")
        return
    if target.command:
        matched = await handler._dispatch_service_v2_feishu_command(
            text=target.command, receive_id=chat_id, conversation_target=target,
            **({"runtime_inputs": inputs, "quote_result": quote} if inputs else {}))
        if not matched:
            await handler._reply_text(chat_id, f"{target.title}入口已变化，请重新发起。")
    elif target.selection_tool:
        await handler._invoke_selection_preview_and_reply(
            route_key=target.route_key,
            tool_name=(handler.SELF_PICKUP_PREVIEW_TOOL_NAME if target.selection_tool == "self_pickup_problem_upload_v2"
                       else "preview_split_pending_problems"),
            receive_id=chat_id, conversation_target=target)
    else:
        await handler._invoke_automation_project_and_reply(
            route_key=target.route_key, dynamic_inputs=inputs or {}, receive_id=chat_id, conversation_target=target)


__all__ = ["run_conversation_plugins"]
