"""Signed Console API for the business handbook (agent layer P2); super administrators only."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from shared.agent_handbook import HandbookError
from shared.contracts import api_failure, api_success
from shared.redaction import redact_text

_STATUS = {"HANDBOOK_ENTRY_NOT_FOUND": 404, "HANDBOOK_VERSION_NOT_FOUND": 404, "HANDBOOK_CONFLICT": 409,
           "HANDBOOK_ENTRY_RETIRED": 409, "HANDBOOK_ALREADY_ACTIVE": 409, "HANDBOOK_NOT_RESUMABLE": 409,
           "HANDBOOK_NOT_APPROVABLE": 409, "HANDBOOK_TRIAL_UNAVAILABLE": 503, "AI_UNAVAILABLE": 503,
           "AI_FAILED": 503, "AI_AUDIT_FAILED": 503}


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    body: str


class SaveRequest(NoteRequest):
    row_version: int


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int


class AcknowledgeRequest(VersionRequest):
    indices: list[int]


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int


def _failure(exc: HandbookError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS.get(exc.code, 422), content=api_failure(exc.code, redact_text(exc.message)))


def create_handbook_router(*, service_provider: Callable[[], Any],
                           super_admin_provider: Callable[[Request], Any]) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/admin/handbook", route_class=EnvelopedRoute)

    def service():
        provided = service_provider()
        if provided is None:
            raise HandbookError("HANDBOOK_UNAVAILABLE", "业务手册当前不可用")
        return provided

    @router.get("")
    async def list_entries(request: Request):
        super_admin_provider(request)
        try:
            return api_success({"entries": await run_in_threadpool(service().list_entries)})
        except HandbookError as exc:
            return _failure(exc)

    @router.post("")
    async def create_entry(req: NoteRequest, request: Request):
        actor = super_admin_provider(request)
        try:
            entry_id = await run_in_threadpool(lambda: service().create(title=req.title, body=req.body,
                                                                        actor_id=actor.actor_id))
            return api_success({"entry_id": entry_id})
        except HandbookError as exc:
            return _failure(exc)

    @router.get("/{entry_id}")
    async def get_entry(entry_id: str, request: Request):
        actor = super_admin_provider(request)
        try:
            return api_success(await run_in_threadpool(lambda: service().get_entry(entry_id, actor=actor)))
        except HandbookError as exc:
            return _failure(exc)

    @router.post("/{entry_id}/save")
    async def save_entry(entry_id: str, req: SaveRequest, request: Request):
        actor = super_admin_provider(request)
        try:
            version = await run_in_threadpool(lambda: service().save(
                entry_id, title=req.title, body=req.body, actor_id=actor.actor_id, row_version=req.row_version))
            return api_success({"version": version})
        except HandbookError as exc:
            return _failure(exc)

    @router.post("/{entry_id}/compile")
    async def compile_entry(entry_id: str, request: Request):
        actor = super_admin_provider(request)
        try:
            return api_success(await service().compile(entry_id, actor=actor))
        except HandbookError as exc:
            return _failure(exc)

    @router.post("/{entry_id}/trial")
    async def trial_entry(entry_id: str, request: Request):
        actor = super_admin_provider(request)
        try:
            return api_success(await service().trial(entry_id, actor=actor))
        except HandbookError as exc:
            return _failure(exc)

    @router.post("/{entry_id}/acknowledge")
    async def acknowledge(entry_id: str, req: AcknowledgeRequest, request: Request):
        super_admin_provider(request)
        try:
            await run_in_threadpool(lambda: service().acknowledge(entry_id, req.version, req.indices))
            return api_success({"acknowledged": sorted(set(req.indices))})
        except HandbookError as exc:
            return _failure(exc)

    @router.post("/{entry_id}/approve")
    async def approve(entry_id: str, req: VersionRequest, request: Request):
        actor = super_admin_provider(request)
        try:
            await run_in_threadpool(lambda: service().approve(entry_id, req.version, actor_id=actor.actor_id))
            return api_success({"status": "ACTIVE", "version": req.version})
        except HandbookError as exc:
            return _failure(exc)

    @router.post("/{entry_id}/{action}")
    async def change_status(entry_id: str, action: str, req: StatusRequest, request: Request):
        super_admin_provider(request)
        handlers = {"pause": "pause", "retire": "retire", "resume": "resume"}
        if action not in handlers:
            return JSONResponse(status_code=404, content=api_failure("NOT_FOUND", "不支持的操作"))
        try:
            await run_in_threadpool(lambda: getattr(service(), handlers[action])(entry_id, row_version=req.row_version))
            return api_success({"action": action})
        except HandbookError as exc:
            return _failure(exc)

    return router


__all__ = ["create_handbook_router"]
