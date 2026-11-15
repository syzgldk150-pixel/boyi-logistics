"""Signed Console API for “需他人复核” review requests (agent layer P4); super administrators only."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from shared.agent_review_repository import ReviewError
from shared.contracts import api_failure, api_success
from shared.redaction import redact_text

_STATUS = {"REVIEW_NOT_FOUND": 404, "REVIEW_ALREADY_DECIDED": 409, "REVIEW_EXPIRED": 409, "REVIEW_UNAVAILABLE": 503}


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = ""


def _failure(exc: ReviewError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS.get(exc.code, 422), content=api_failure(exc.code, redact_text(exc.message)))


def _public(item: dict[str, Any]) -> dict[str, Any]:
    """Never send the stored plan or requester identity to the browser."""
    return {key: item.get(key) for key in ("request_id", "short_code", "kind", "status", "title", "summary",
                                           "requester_label", "expires_at", "decided_by", "decided_at",
                                           "decision_note", "result_text", "created_at")}


def create_review_router(*, service_provider: Callable[[], Any], super_admin_provider: Callable[[Request], Any]) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/admin/reviews", route_class=EnvelopedRoute)

    def service():
        provided = service_provider()
        if provided is None:
            raise ReviewError("REVIEW_UNAVAILABLE", "复核服务当前不可用")
        return provided

    def label(actor: Any) -> str:
        return str(getattr(actor, "display_name", "") or getattr(actor, "actor_id", ""))

    @router.get("")
    async def list_reviews(request: Request):
        super_admin_provider(request)
        try:
            return api_success({"reviews": [_public(item) for item in await run_in_threadpool(service().list_recent)]})
        except ReviewError as exc:
            return _failure(exc)

    @router.post("/{request_id}/approve")
    async def approve(request_id: str, request: Request):
        actor = super_admin_provider(request)
        try:
            return api_success(_public(await service().approve(request_id, reviewer_label=label(actor))))
        except ReviewError as exc:
            return _failure(exc)

    @router.post("/{request_id}/reject")
    async def reject(request_id: str, req: RejectRequest, request: Request):
        actor = super_admin_provider(request)
        try:
            return api_success(_public(await service().reject(request_id, reviewer_label=label(actor), note=req.note[:200])))
        except ReviewError as exc:
            return _failure(exc)

    return router


__all__ = ["create_review_router"]
