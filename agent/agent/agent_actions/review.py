"""“需他人复核”: one-time super-administrator approval of a person's write (agent layer P4).

A person whose agent level for a write capability is ``review`` never runs that
write directly. The plan is stored (``shared.agent_review_repository``),
super administrators are notified, and one of them approves or rejects it in
the Console or by replying in Feishu. On approval the service re-checks that
the requester is still active and still allowed the capability, then runs the
stored plan exactly once inside ``reviewer_approved()``. An expired request is
void; nothing retries it. Executors per plan kind are injected by the
composition root, so this module never imports ``feishu`` or ``tools``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Awaitable, Callable, Mapping

from agent.orchestration.agent_confirmation import reviewer_approved
from agent.orchestration.models import Actor, ActorType
from shared.agent_review_repository import AgentReviewRepository, ReviewError
from shared.redaction import redact_text

logger = logging.getLogger(__name__)
REVIEW_TTL_SECONDS = 2 * 3600
# kind -> async executor(request, actor) -> (succeeded, text, invocation_id)
Executor = Callable[[Mapping[str, Any], Actor], Awaitable[tuple[bool, str, str | None]]]
Notifier = Callable[[str, Mapping[str, Any]], None]


def requester_record(actor: Any) -> dict[str, Any]:
    return {"actor_type": actor.actor_type.value, "actor_id": actor.actor_id, "roles": list(actor.roles),
            "authenticated_by": actor.authenticated_by, "display_name": getattr(actor, "display_name", None)}


def requester_actor(record: Mapping[str, Any]) -> Actor:
    actor = Actor(ActorType(record["actor_type"]), str(record["actor_id"]), tuple(record.get("roles") or ()),
                  display_name=record.get("display_name") or "", authenticated_by=str(record["authenticated_by"]))
    return replace(actor, channel="agent")


class ReviewService:
    def __init__(self, repository: AgentReviewRepository, *, identity_access: Any = None,
                 executors: Mapping[str, Executor] | None = None,
                 notify_reviewers: Callable[[str], None] | None = None,
                 notify_requester: Notifier | None = None, ttl_seconds: int = REVIEW_TTL_SECONDS) -> None:
        self._repository, self._identity_access = repository, identity_access
        self._executors = dict(executors or {})
        self._notify_reviewers, self._notify_requester, self._ttl = notify_reviewers, notify_requester, ttl_seconds

    def register_executor(self, kind: str, executor: Executor) -> None:
        self._executors[kind] = executor

    def _label(self, actor: Any) -> str:
        name = getattr(actor, "display_name", None)
        if not name and self._identity_access is not None:
            try:
                name = getattr(self._identity_access.for_actor(actor), "role_name", None)
            except Exception:
                name = None
        return str(name or getattr(actor, "actor_id", ""))[:191]

    def submit(self, *, kind: str, actor: Any, capability: str, title: str, summary: str,
               plan: Mapping[str, Any], reply_chat_id: str | None = None) -> dict[str, Any]:
        if kind not in self._executors:
            raise ReviewError("REVIEW_KIND_UNSUPPORTED", "这类操作暂不支持复核")
        request = self._repository.create(kind=kind, title=title, summary=summary, capability=capability, plan=plan,
                                          requester=requester_record(actor), requester_label=self._label(actor),
                                          reply_chat_id=reply_chat_id, ttl_seconds=self._ttl)
        hours = max(1, self._ttl // 3600)
        if self._notify_reviewers is not None:
            try:
                self._notify_reviewers(
                    f"复核请求 {request['short_code']}：{request['requester_label']} 申请{summary}。\n"
                    f"回复“同意复核 {request['short_code']}”执行，或“拒绝复核 {request['short_code']}”；"
                    f"也可在后台“智能模型 → 复核请求”处理。{hours} 小时内有效，过期作废。")
            except Exception:
                logger.warning("review notification failed | request=%s", request["request_id"], exc_info=True)
        return request

    @staticmethod
    def submitted_message(request: Mapping[str, Any]) -> str:
        return (f"你的身份需要超级管理员复核：已提交复核请求 {request['short_code']}（{request['summary']}）。"
                "同意后会自动执行并通知你；本次没有执行。")

    def list_recent(self) -> list[dict[str, Any]]:
        return self._repository.list_recent()

    def _tell_requester(self, request: Mapping[str, Any], text: str) -> None:
        if self._notify_requester is None:
            return
        try:
            self._notify_requester(text, request)
        except Exception:
            logger.warning("review result notification failed | request=%s", request["request_id"], exc_info=True)

    async def approve(self, request_id: str, *, reviewer_label: str) -> dict[str, Any]:
        request = await asyncio.to_thread(self._repository.decide, request_id, approve=True, actor_label=reviewer_label)
        actor = requester_actor(request["requester"])
        access = None
        if self._identity_access is not None:
            access = await asyncio.to_thread(self._identity_access.for_actor, actor)
        if access is not None and (not access.active or access.agent_level(request["capability"]) is None):
            text = f"复核请求 {request['short_code']} 已同意，但发起人的权限已变化，没有执行。"
            await asyncio.to_thread(self._repository.finish, request["request_id"], executed=False, result_text=text)
            self._tell_requester(request, text)
            return {**request, "status": "FAILED", "result_text": text}
        executor = self._executors.get(request["kind"])
        try:
            if executor is None:
                raise ReviewError("REVIEW_KIND_UNSUPPORTED", "这类操作暂不支持复核")
            with reviewer_approved():
                ok, text, invocation_id = await executor(request, actor)
        except Exception as exc:  # the approval is spent either way; nothing retries it
            ok, text, invocation_id = False, f"执行失败：{redact_text(str(exc))[:200]}", None
        text = f"复核请求 {request['short_code']}（{request['summary']}）已由 {reviewer_label} 同意。{text}"
        await asyncio.to_thread(self._repository.finish, request["request_id"], executed=ok, result_text=text,
                                invocation_id=invocation_id)
        self._tell_requester(request, text)
        return {**request, "status": "EXECUTED" if ok else "FAILED", "result_text": text, "invocation_id": invocation_id}

    async def reject(self, request_id: str, *, reviewer_label: str, note: str = "") -> dict[str, Any]:
        request = await asyncio.to_thread(self._repository.decide, request_id, approve=False,
                                          actor_label=reviewer_label, note=note)
        self._tell_requester(request, f"复核请求 {request['short_code']}（{request['summary']}）被 {reviewer_label} 拒绝"
                                      + (f"：{note}" if note else "") + "，没有执行。")
        return request


__all__ = ["REVIEW_TTL_SECONDS", "ReviewService", "requester_actor", "requester_record"]
