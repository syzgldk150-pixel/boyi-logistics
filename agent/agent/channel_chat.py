"""Transport adapter for the same conversation service used by Console."""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5, uuid4

from agent.harness_application import HarnessConversationService
from agent.orchestration.models import Actor, ActorType


def primary_chat_request(actor: Actor, *, channel: str) -> str:
    """One explicit ongoing conversation per authenticated channel identity."""
    return str(uuid5(NAMESPACE_URL, f"boyi-chat:{channel}:{actor.actor_type.value}:{actor.actor_id}:{actor.roles}"))


def reply_to_console(conversations, *, actor, session_id, request_id, message) -> dict:
    if not isinstance(actor, Actor) or actor.actor_type is not ActorType.CONSOLE_ADMIN:
        raise ValueError("后台对话需要已验证的账号")
    request_id = request_id or str(uuid4())
    if not session_id:
        session_id = conversations.create_session(actor=actor, request_id=primary_chat_request(actor, channel="console")).session_id
    receipt = conversations.send_message(actor=actor, session_id=session_id, request_id=request_id, message=message)
    return {"reply": receipt.assistant_message.content, "conversation_id": receipt.session_id,
            "plugin_invocations": receipt.plugin_invocations, "replayed": receipt.replayed}


def reply_to_feishu(
    conversations: HarnessConversationService,
    *,
    actor: Actor,
    chat_id: str,
    event_id: str,
    message: str,
) -> dict:
    if not isinstance(actor, Actor) or actor.actor_type is not ActorType.FEISHU_USER or not chat_id or not event_id:
        raise ValueError("已验证的飞书会话与事件不能为空")
    # A group chat never shares one member's model history with another member.
    session_request = primary_chat_request(actor, channel=f"feishu:{chat_id}")
    session = conversations.create_session(actor=actor, request_id=session_request)
    receipt = conversations.send_message(
        actor=actor, session_id=session.session_id,
        request_id=str(uuid5(NAMESPACE_URL, f"feishu-message:{chat_id}:{actor.actor_id}:{event_id}")),
        message=message,
    )
    return {"reply": receipt.assistant_message.content,
            "plugin_requests": receipt.plugin_requests,
            "replayed": receipt.replayed}
