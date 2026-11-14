"""Person confirmation for agent writes set to "需确认" (identity P3).

A Feishu, WeChat or Console AI-assistant request that would run a writing
plugin is rejected with ``AGENT_CONFIRMATION_REQUIRED`` when the person's
agent level for that capability is ``confirm``. The channel then asks the
person, and only after the same person replies "确认" in the same chat does
host code re-submit the same request inside ``person_confirmed()``.

The flag lives in a context variable: it crosses ``asyncio.to_thread`` with
the request and is set only by host code, never by the model or a message.
Plugins with their own preview/confirm flow (scan, self-pickup, split) are
already confirmed by that flow and are not asked twice.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from agent.orchestration.models import ActorType, OrchestrationError

AGENT_CONFIRMATION_REQUIRED = "AGENT_CONFIRMATION_REQUIRED"
CONFIRMATION_TTL_SECONDS = 300
_CONFIRMED: ContextVar[bool] = ContextVar("agent_write_confirmed", default=False)


@contextmanager
def person_confirmed() -> Iterator[None]:
    """Mark the requests submitted inside as confirmed by their person."""
    token = _CONFIRMED.set(True)
    try:
        yield
    finally:
        _CONFIRMED.reset(token)


def is_agent_actor(actor: Any) -> bool:
    """Feishu/WeChat senders and the Console AI assistant (``channel="agent"``)."""
    return getattr(actor, "actor_type", None) is ActorType.FEISHU_USER or getattr(actor, "channel", "") == "agent"


def require_person_confirmation(*, identity_access: Any, actor: Any, capability: str, title: str) -> None:
    """Raise unless this agent write may run now."""
    if identity_access is None or not is_agent_actor(actor) or _CONFIRMED.get():
        return
    if identity_access.for_actor(actor).agent_level(capability) == "confirm":
        raise OrchestrationError(AGENT_CONFIRMATION_REQUIRED, f"“{title}”需要你确认后才执行")


def confirmation_prompt(reason: str) -> str:
    """``reason`` is the AGENT_CONFIRMATION_REQUIRED message naming the plugin."""
    minutes = CONFIRMATION_TTL_SECONDS // 60
    return f"{reason}。回复“确认”立即执行，回复“取消”放弃；{minutes} 分钟内有效。"


__all__ = [
    "AGENT_CONFIRMATION_REQUIRED", "CONFIRMATION_TTL_SECONDS", "confirmation_prompt",
    "is_agent_actor", "person_confirmed", "require_person_confirmation",
]
