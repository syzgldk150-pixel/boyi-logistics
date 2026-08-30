"""Signed Console administration API for code-owned business modules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from shared.business_module_repository import BusinessModuleLifecycleError, BusinessModuleLifecycleService
from shared.contracts import api_failure, api_success


def _error_response(exc: BusinessModuleLifecycleError) -> JSONResponse:
    status_code = 422 if exc.code in {"INVALID_ACTION", "INVALID_REQUEST"} else 409
    return JSONResponse(status_code=status_code, content=api_failure(exc.code, str(exc)))


def create_business_module_router(
    *,
    service_provider: Callable[[], BusinessModuleLifecycleService],
    admin_actor_provider: Callable[[Request], Any],
) -> APIRouter:
    """Expose read-only compatibility views of retired lifecycle records."""

    router = APIRouter(prefix="/internal/v1/admin/modules", route_class=EnvelopedRoute)

    @router.get("")
    async def list_modules(request: Request):
        admin_actor_provider(request)
        try:
            return api_success(await run_in_threadpool(service_provider().list_modules))
        except BusinessModuleLifecycleError as exc:
            return _error_response(exc)

    @router.get("/catalog")
    async def get_catalog(request: Request):
        admin_actor_provider(request)
        try:
            return api_success(await run_in_threadpool(service_provider().catalog))
        except BusinessModuleLifecycleError as exc:
            return _error_response(exc)

    @router.get("/{module_code}")
    async def get_module(module_code: str, request: Request):
        admin_actor_provider(request)
        try:
            return api_success(await run_in_threadpool(service_provider().get_module, module_code))
        except BusinessModuleLifecycleError as exc:
            return _error_response(exc)

    @router.get("/{module_code}/audit")
    async def list_module_audit(module_code: str, request: Request, limit: int = 200):
        admin_actor_provider(request)
        try:
            return api_success(
                await run_in_threadpool(service_provider().list_audit, module_code, limit=limit)
            )
        except BusinessModuleLifecycleError as exc:
            return _error_response(exc)

    return router
