"""Signed Console API for 业务手册 → 知识来源.

The request middleware already checked the 业务手册 module: viewing lists the
documents and reminders; operating runs a scan or marks one or all reminders checked.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from agent.knowledge_watch import KnowledgeWatchError
from shared.contracts import api_failure, api_success
from shared.redaction import redact_text

_STATUS = {"KNOWLEDGE_SCAN_BUSY": 409, "KNOWLEDGE_REMINDER_NOT_OPEN": 409, "KNOWLEDGE_SCAN_FAILED": 503,
           "KNOWLEDGE_WATCH_UNAVAILABLE": 503}


def create_knowledge_watch_router(*, service_provider: Callable[[], Any],
                                  admin_provider: Callable[[Request], Any]) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/admin/knowledge-sources", route_class=EnvelopedRoute)

    async def run(call):
        try:
            service = service_provider()
            if service is None:
                raise KnowledgeWatchError("KNOWLEDGE_WATCH_UNAVAILABLE", "知识来源检查当前不可用。")
            return api_success(await run_in_threadpool(lambda: call(service)))
        except KnowledgeWatchError as exc:
            return JSONResponse(status_code=_STATUS.get(exc.code, 422), content=api_failure(exc.code, redact_text(str(exc))))

    @router.get("")
    async def view(request: Request):
        admin_provider(request)
        return await run(lambda service: service.view())

    @router.post("/scan")
    async def scan(request: Request):
        admin_provider(request)
        return await run(lambda service: service.scan(source="manual"))

    @router.post("/reminders/check-all")
    async def check_all(request: Request):
        actor = admin_provider(request)
        return await run(lambda service: {"checked": service.mark_all_checked(actor_id=actor.actor_id)})

    @router.post("/reminders/{reminder_id}/check")
    async def check(reminder_id: int, request: Request):
        actor = admin_provider(request)
        return await run(lambda service: service.mark_checked(reminder_id, actor_id=actor.actor_id) or {"checked": True})

    return router


__all__ = ["create_knowledge_watch_router"]
