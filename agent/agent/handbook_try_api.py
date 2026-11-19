"""Signed Console API for 业务手册 → 试问.

A person types a question and sees what the robot would reply — the original
passage it would quote, the fee rule and calculation, or a query result — under
that person's own robot permissions, with the same model, knowledge, handbook
and rules as real chats. Read actions only: no plugins, no writes, nothing is
sent to Feishu or WeChat, and the state is kept apart from real chats. The
request middleware already checked the 业务手册 module (viewing is enough).
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage
from shared.contracts import api_failure, api_success
from shared.redaction import redact_text

DECISION_TEXT = {"EXECUTE": "执行", "CONFIRM": "需本人确认", "REVIEW": "需他人复核", "CLARIFY": "反问", "REJECT": "未执行"}
FEE_ACTION_PREFIX = "fee."
ACTION_TITLES = {"knowledge.search": "查询知识库", "knowledge.quote": "引用原文", "ask_user": "反问"}


def _title(decision: Any) -> str:
    return decision.action_title or ACTION_TITLES.get(decision.action_id, decision.action_id)


def trial_steps(decisions: Sequence[Any], *, quotes: int, reply: str) -> list[dict[str, str]]:
    """The six checkable steps of one 试问 (frontend-spec 57), each derived from what this turn actually did.

    ``state`` is ``done`` (执行), ``skipped`` (未触发) or ``blocked`` (未执行).
    """
    chosen = [item for item in decisions if item.decision != "REJECT"]
    clarified = [item for item in decisions if item.decision == "CLARIFY"]
    knowledge = [item for item in decisions if str(item.action_id).startswith("knowledge.")]
    fees = [item for item in decisions if str(item.action_id).startswith(FEE_ACTION_PREFIX)]
    refused = [item for item in decisions if item.decision == "REJECT"]
    titles = "、".join(dict.fromkeys(_title(item) for item in chosen if item.decision != "CLARIFY"))
    steps = [
        {"title": "理解问题", "state": "done", "result": f"选择了：{titles}" if titles else "直接回答，没有调用查询"},
        {"title": "信息检查", "state": "done", "result": "已执行反问，等待补充信息" if clarified else "信息足够，未触发反问"},
        {"title": "检索知识库", "state": "done" if knowledge else "skipped",
         "result": (f"检索知识库并引用原文 {quotes} 段" if quotes else "检索了知识库，未引用原文") if knowledge else "本次未检索"},
        {"title": "匹配规则", "state": "done" if fees else "skipped",
         "result": "按开单日期选用已生效的计算规则" if fees else "本次不涉及计算规则"},
        {"title": "计算", "state": ("done" if any(item.decision == "EXECUTE" for item in fees) else "blocked") if fees else "skipped",
         "result": ("已按规则计算" if any(item.decision == "EXECUTE" for item in fees) else "缺少条件，未计算") if fees
         else "本次没有计算"},
        {"title": "权限检查", "state": "done",
         "result": "未执行插件 / 未写入 / 未发送" + (f"；{len(refused)} 项按权限或规则未执行" if refused else "")},
    ]
    if not reply:
        steps[0]["result"] = "没有生成回复"
    return steps


class TryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)


def create_handbook_try_router(*, runtime_provider: Callable[[], Any],
                               admin_provider: Callable[[Request], Any]) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/admin/handbook-try", route_class=EnvelopedRoute)

    @router.post("")
    async def try_question(req: TryRequest, request: Request):
        actor = admin_provider(request)
        runtime = runtime_provider()
        if runtime is None:
            return JSONResponse(status_code=503, content=api_failure("HANDBOOK_TRY_UNAVAILABLE", "试问当前不可用。"))

        def ask() -> dict[str, Any]:
            request_id = str(uuid.uuid4())
            started = time.monotonic()
            sidecar = runtime.trial_sidecar(actor, request_id)
            result = sidecar.run(messages=(HarnessMessage("user", req.question.strip(), request_id),), timeout_seconds=30)
            quotes = [{"title": quote.title, "location": quote.location, "pages": list(quote.pages),
                       "url": quote.url} for quote in getattr(sidecar, "quotes", [])]
            handbook = getattr(sidecar, "handbook_versions", None)
            return {
                "reply": result.content,
                "quotes": quotes,
                "decisions": [{"action": _title(decision),
                               "decision": DECISION_TEXT.get(decision.decision, decision.decision)}
                              for decision in result.decisions],
                "steps": trial_steps(result.decisions, quotes=len(quotes), reply=result.content or ""),
                "seconds": round(time.monotonic() - started, 1),
                "handbook_entries": len(handbook) if handbook is not None else None,
            }

        try:
            return api_success(await run_in_threadpool(ask))
        except HarnessError as exc:
            return JSONResponse(status_code=503, content=api_failure(exc.code, redact_text(str(exc))))

    return router


__all__ = ["create_handbook_try_router", "trial_steps"]
