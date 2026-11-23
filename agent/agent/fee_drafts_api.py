"""Signed Console API for 业务手册 → 计算规则 → 从手册起草 (方案 B, 2026-11-20).

Reading needs the 业务手册 module visible; starting, cancelling a draft and keeping drafts needs it
operable (``shared.identity_routes``). The work is in ``agent.fee_drafting.FeeDraftService``.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from agent.fee_rules_api import _failure
from shared.contracts import api_success
from shared.handbook_fee_rules import FeeRuleError


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_token: str = Field(min_length=1, max_length=160)
    start: int
    end: int
    chapter: str = Field(default="", max_length=200)
    rule_id: str | None = Field(default=None, min_length=36, max_length=36)


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indices: list[int] = Field(min_length=1, max_length=12)


def create_fee_drafts_router(*, service_provider: Callable[[], Any], admin_provider: Callable[[Request], Any]) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/admin/fee-drafts", route_class=EnvelopedRoute)

    def service():
        provided = service_provider()
        if provided is None:
            raise FeeRuleError("FEE_DRAFT_UNAVAILABLE", "从手册起草当前不可用。")
        return provided

    async def run(call):
        try:
            return api_success(await run_in_threadpool(call))
        except FeeRuleError as exc:
            return _failure(exc)

    @router.get("/documents")
    async def documents(request: Request):
        admin_provider(request)
        return await run(lambda: {"documents": service().documents()})

    @router.get("/outline")
    async def outline(request: Request, node_token: str = ""):
        admin_provider(request)
        return await run(lambda: service().outline(node_token))

    @router.post("/jobs")
    async def start(req: StartRequest, request: Request):
        actor = admin_provider(request)
        try:
            target = await run_in_threadpool(service().draft_target, req.rule_id) if req.rule_id else ""
            return api_success(service().start(node_token=req.node_token, start=req.start, end=req.end,
                                               chapter=req.chapter, actor_id=actor.actor_id, rule_id=req.rule_id,
                                               target=target))
        except FeeRuleError as exc:
            return _failure(exc)

    @router.get("/jobs")
    async def latest(request: Request, rule_id: str = ""):
        """The newest job started from one draft by this person (null when there is none)."""
        actor = admin_provider(request)
        return await run(lambda: {"job": service().latest_for_rule(rule_id, actor_id=actor.actor_id)})

    @router.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str, request: Request):
        actor = admin_provider(request)
        try:  # in memory only, and on the event loop: Task.cancel is not thread-safe
            return api_success(service().cancel(job_id, actor_id=actor.actor_id))
        except FeeRuleError as exc:
            return _failure(exc)

    @router.get("/jobs/{job_id}")
    async def job(job_id: str, request: Request):
        actor = admin_provider(request)
        return await run(lambda: service().job(job_id, actor_id=actor.actor_id))

    @router.post("/jobs/{job_id}/save")
    async def save(job_id: str, req: SaveRequest, request: Request):
        actor = admin_provider(request)
        return await run(lambda: service().save(job_id, indices=req.indices, actor_id=actor.actor_id))

    return router


__all__ = ["create_fee_drafts_router"]
