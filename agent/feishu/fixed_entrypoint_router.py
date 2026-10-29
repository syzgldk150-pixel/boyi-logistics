"""Host-parsed fixed Feishu commands, dispatched to their owning Service V2 plugin."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent.direct_tool_router import direct_tool_request_from_text
from agent.orchestration.models import OrchestrationError
from agent.orchestration.preview_entrypoints import PREVIEW_ROUTES


async def invoke_fixed_preview(*, dispatcher, route_key, event_id, sender_id, chat_id,
                               dynamic_inputs, preview_invocation_id, on_accepted, conversation_target=None):
    """Keep the host candidate/confirmation UI while calling the owning plugin."""
    command = PREVIEW_ROUTES.get(route_key)
    if command is None:
        raise OrchestrationError("PROJECT_ROUTE_NOT_FOUND", "当前插件入口不可用")
    if dispatcher is None:
        raise OrchestrationError("PROJECT_RUNTIME_PROJECTION_STALE", "当前插件入口不可用")
    if set(dynamic_inputs) - {"selected_bill_codes"}:
        raise OrchestrationError("SELECTION_INPUT_INVALID", "确认不能修改插件设置")
    result = await dispatcher.dispatch(command_text=command, event_id=event_id, sender_id=sender_id, chat_id=chat_id,
        on_accepted=on_accepted, conversation_target=conversation_target, preview_invocation_id=preview_invocation_id,
        selected_bill_codes=dynamic_inputs.get("selected_bill_codes"))
    if result is None:
        raise OrchestrationError("PROJECT_ENTRYPOINT_DISABLED", "插件尚未启用或未配置飞书入口")
    return result


async def dispatch_fixed_feishu_command(
    *,
    mode: Any,
    automation_route_key: Any,
    command_text: str,
    receive_id: str,
    dispatch_service_v2: Callable[..., Awaitable[bool]],
    reply_text: Callable[..., Awaitable[Any]],
) -> bool:
    """Send one fixed command to its plugin; preview routes stay with the caller.

    ``False`` means the caller still owns the message (preview routes and
    non-plugin requests); ``True`` means it was dispatched or explicitly
    rejected, so nothing else may run for it.
    """

    route_key = str(automation_route_key or "").strip()
    if route_key in PREVIEW_ROUTES or mode != "automation_project" or not route_key:
        return False
    # The host parser accepts the statistics aliases; the plugin declares one command.
    if route_key == "builtin.arrival_stats":
        canonical_command = "统计到货数据"
        if direct_tool_request_from_text(command_text) == direct_tool_request_from_text(canonical_command):
            command_text = canonical_command
    if await dispatch_service_v2(text=command_text, receive_id=receive_id):
        return True
    await reply_text(
        receive_id,
        "插件尚未启用或未配置飞书入口，请在后台自动化页检查。",
        reply_type="service_v2_feishu_failed",
    )
    return True
