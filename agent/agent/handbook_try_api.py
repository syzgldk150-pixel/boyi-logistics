"""Signed Console API for 业务手册 → 试问.

A person types a question and sees what the robot would reply — the original
passage it would quote, the fee rule and calculation, or a query result — under
that person's own robot permissions, with the same model, knowledge, handbook
and rules as real chats. Read actions only: no plugins, no writes, nothing is
sent to Feishu or WeChat, and the state is kept apart from real chats. The
request middleware already checked the 业务手册 module (viewing is enough).
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
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
            sidecar = runtime.trial_sidecar(actor, request_id)
            result = sidecar.run(messages=(HarnessMessage("user", req.question.strip(), request_id),), timeout_seconds=30)
            return {
                "reply": result.content,
                "quotes": [{"title": quote.title, "location": quote.location, "pages": list(quote.pages),
                            "url": quote.url} for quote in getattr(sidecar, "quotes", [])],
                "steps": [{"action": decision.action_title or decision.action_id,
                           "decision": DECISION_TEXT.get(decision.decision, decision.decision)}
                          for decision in result.decisions],
            }

        try:
            return api_success(await run_in_threadpool(ask))
        except HarnessError as exc:
            return JSONResponse(status_code=503, content=api_failure(exc.code, redact_text(str(exc))))

    return router


__all__ = ["create_handbook_try_router"]
