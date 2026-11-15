"""Feishu/WeChat side of “需他人复核” review requests (agent layer P4).

* A conversation request or a fixed command that needs review becomes a
  review request instead of running; the person is told its code.
* A super administrator may reply “同意复核 ABC234” or “拒绝复核 ABC234 原因”.
* An approved Feishu plan runs through the same managed command dispatch,
  as its requester, in the original chat (``execute_feishu_plan``).

The review service is bound by the composition root; ``message_handler`` is
looked up at call time to avoid an import cycle.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

_REVIEW_SERVICE: Any = None
_DECISION = re.compile(r"^\s*(同意|拒绝)复核\s*([A-Za-z0-9]{6})\s*(.*)$", re.S)


def bind_review_service(service: Any) -> None:
    global _REVIEW_SERVICE
    _REVIEW_SERVICE = service


def _handler():
    from feishu import message_handler
    return message_handler


async def _requester(handler: Any) -> Any:
    context = handler._COMMAND_CONTEXT.get()
    runtime = handler._FEISHU_BINDING_RUNTIME
    if context is None or not context.actor_id or runtime is None:
        return None
    return await asyncio.to_thread(runtime.resolve_actor, context.actor_id)


async def submit_review(*, chat_id: str, command: str, runtime_inputs: dict[str, Any] | None, quote_result: bool,
                        title: str, summary: str, capability: str) -> None:
    """File a review request for one Feishu plan and tell the person; nothing runs."""
    handler = _handler()
    actor = await _requester(handler) if _REVIEW_SERVICE is not None else None
    if actor is None:
        await handler._reply_text(chat_id, f"“{title}”需要超级管理员复核，但复核服务当前不可用，没有执行。",
                                  reply_type="agent_review_unavailable")
        return
    request = await asyncio.to_thread(
        _REVIEW_SERVICE.submit, kind="plugin.feishu", actor=actor, capability=capability, title=title, summary=summary,
        plan={"command": command, "runtime_inputs": runtime_inputs, "quote_result": quote_result}, reply_chat_id=chat_id)
    await handler._reply_text(chat_id, _REVIEW_SERVICE.submitted_message(request), reply_type="agent_review_submitted")


async def execute_feishu_plan(request: Any, actor: Any) -> tuple[bool, str, str | None]:
    """Run an approved plan as its requester in the original chat (inside ``reviewer_approved()``)."""
    handler = _handler()
    plan, chat_id = request["plan"], request.get("reply_chat_id") or ""
    if not chat_id:
        return False, "原会话信息缺失，没有执行。", None
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-review:{request['request_id']}"))
    token = handler._COMMAND_CONTEXT.set(handler.FeishuCommandContext(event_id, actor.actor_id, chat_id))
    try:
        matched = await handler._dispatch_service_v2_feishu_command(
            text=plan["command"], receive_id=chat_id, runtime_inputs=plan.get("runtime_inputs"),
            quote_result=bool(plan.get("quote_result")))
    finally:
        handler._COMMAND_CONTEXT.reset(token)
    if not matched:
        return False, "插件入口已变化或已停用，没有执行。", None
    return True, "已在原会话发起执行，结果会发到原会话。", None


async def handle_review_reply(text: str, *, chat_id: str, sender_id: str) -> bool:
    """“同意复核 X” / “拒绝复核 X 原因” from a super administrator; other messages pass through."""
    match = _DECISION.match(str(text or ""))
    if match is None:
        return False
    handler = _handler()
    if _REVIEW_SERVICE is None:
        await handler._reply_text(chat_id, "复核服务当前不可用。", reply_type="agent_review_unavailable")
        return True
    actor = await _requester(handler)
    if actor is None or "super_admin" not in actor.roles:
        await handler._reply_text(chat_id, "只有超级管理员可以处理复核请求。", reply_type="agent_review_forbidden")
        return True
    from shared.agent_review_repository import ReviewError
    label = actor.display_name or actor.actor_id
    try:
        if match.group(1) == "同意":
            result = await _REVIEW_SERVICE.approve(match.group(2).upper(), reviewer_label=label)
            await handler._reply_text(chat_id, result.get("result_text") or "已处理。", reply_type="agent_review_approved")
        else:
            await _REVIEW_SERVICE.reject(match.group(2).upper(), reviewer_label=label, note=match.group(3).strip()[:200])
            await handler._reply_text(chat_id, f"已拒绝复核请求 {match.group(2).upper()}。", reply_type="agent_review_rejected")
    except ReviewError as exc:
        await handler._reply_text(chat_id, exc.message, reply_type="agent_review_failed")
    return True


__all__ = ["bind_review_service", "execute_feishu_plan", "handle_review_reply", "submit_review"]
