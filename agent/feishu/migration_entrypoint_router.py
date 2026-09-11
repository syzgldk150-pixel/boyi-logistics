"""Fixed Feishu entrypoint ownership during the Action-v1 migration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from agent.orchestration.models import OrchestrationError
from agent.orchestration.preview_entrypoints import PREVIEW_ROUTES


_PREVIEW_ROUTES = PREVIEW_ROUTES


async def invoke_migrated_preview(*, dispatcher, route_key, event_id, sender_id, chat_id,
                                  dynamic_inputs, preview_invocation_id, on_accepted, conversation_target=None):
    """Keep the existing candidate/confirmation UI while routing to its V2 owner."""
    identity = _PREVIEW_ROUTES.get(route_key)
    if identity is None or dispatcher is None:
        return False, None
    tool, command = identity
    owner = await asyncio.to_thread(dispatcher.fixed_feishu_owner,
        source_tool_name=tool,source_route_key=route_key,command_text=command)
    if owner == "ACTION_V1":
        return False, None
    if owner != "SERVICE_V2":
        raise OrchestrationError("PROJECT_RUNTIME_PROJECTION_STALE", "当前插件入口不可用")
    if set(dynamic_inputs) - {"selected_bill_codes"}:
        raise OrchestrationError("SELECTION_INPUT_INVALID", "确认不能修改插件设置")
    result = await dispatcher.dispatch(command_text=command,event_id=event_id,sender_id=sender_id,chat_id=chat_id,
        on_accepted=on_accepted,conversation_target=conversation_target,preview_invocation_id=preview_invocation_id,
        selected_bill_codes=dynamic_inputs.get("selected_bill_codes"))
    if result is None:
        raise OrchestrationError("PROJECT_RUNTIME_PROJECTION_STALE", "已迁移的插件入口不可用")
    return True, result


async def dispatch_migrated_fixed_feishu_entrypoint(
    *,
    mode: Any,
    automation_route_key: Any,
    tool_name: Any,
    command_text: str,
    receive_id: str,
    dispatcher: Any | None,
    dispatch_service_v2: Callable[..., Awaitable[bool]],
    reply_text: Callable[..., Awaitable[Any]],
) -> bool:
    """Route one fixed command according to its durable migration owner.

    The caller invokes this adapter only after all login and pending-state
    branches have had priority.  ``False`` means the Action-v1 route remains
    local to the caller; ``True`` means this helper has either dispatched or
    explicitly rejected the command, so no fallback route may run.
    """

    route_key = str(automation_route_key or "").strip()
    if route_key in _PREVIEW_ROUTES:
        # The common preview adapter also owns confirmation and cancellation.
        return False
    if mode != "automation_project" or not route_key:
        return False

    owner = "ACTION_V1"
    owner_reader = getattr(dispatcher, "fixed_feishu_owner", None)
    if dispatcher is not None and callable(owner_reader):
        try:
            owner = await asyncio.to_thread(
                owner_reader,
                source_tool_name=str(tool_name or ""),
                source_route_key=route_key,
                command_text=command_text,
            )
        except Exception:
            owner = "BLOCKED"

    if owner == "SERVICE_V2":
        if await dispatch_service_v2(text=command_text, receive_id=receive_id):
            return True
        await reply_text(
            receive_id,
            "扩展任务暂时无法执行，请稍后重试。",
            reply_type="service_v2_feishu_failed",
        )
        return True

    if owner == "BLOCKED":
        await reply_text(
            receive_id,
            "扩展任务未能执行：迁移入口所有权当前不可用。",
            reply_type="service_v2_feishu_rejected",
        )
        return True

    return False
