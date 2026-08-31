"""Fixed Feishu entrypoint ownership during the Action-v1 migration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


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
