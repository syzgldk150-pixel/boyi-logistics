"""Closed internal HTTP adapter for the fixed Harness workspace."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.harness import HarnessError, HarnessToolCatalog
from agent.harness_application import (
    HarnessConversationService,
    TrustedHarnessInvocationAdapter,
    build_fixed_harness_tools,
)
from agent.orchestration.models import Actor
from shared.contracts import api_failure, api_success
from shared.redaction import redact_text


class HarnessSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_uuid: str


class HarnessMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_uuid: str
    session_id: str
    message: str = Field(min_length=1, max_length=4_000)


def public_harness_tools(
    *,
    policy_service: object,
    contribution_registry: object,
    actor: Actor,
    request_id: str,
) -> list[dict[str, str]]:
    """Project the safe catalog without exposing invocation identity."""

    adapter = TrustedHarnessInvocationAdapter(
        policy_service=policy_service,
        actor=actor,
        base_request_id=request_id,
    )
    catalog = HarnessToolCatalog(
        invocation_port=adapter,
        fixed_tools=build_fixed_harness_tools(),
        snapshot_provider=contribution_registry,
    )
    return [
        {
            "tool_id": str(item["tool_id"]),
            "title": str(item["title"]),
            "description": str(item["description"]),
        }
        for item in catalog.public_tools()
    ]


async def create_harness_session_response(
    payload: HarnessSessionRequest,
    request: Request,
    *,
    conversation_provider: Callable[[], HarnessConversationService],
    tools_provider: Callable[[Actor, str], list[dict[str, str]]],
    actor_provider: Callable[[Request], Actor],
) -> dict[str, Any]:
    actor = actor_provider(request)
    receipt = await asyncio.to_thread(
        conversation_provider().create_session,
        actor=actor,
        request_id=payload.request_uuid,
    )
    tools = await asyncio.to_thread(tools_provider, actor, payload.request_uuid)
    return api_success(
        {
            "session_id": receipt.session_id,
            "request_uuid": receipt.request_id,
            "persistence_status": receipt.persistence_status,
            "status": "PRODUCTION_GATED",
            "availability": "PRODUCTION_GATED",
            "blocked_reason": "HARNESS_RUNTIME_PRODUCTION_GATED",
            "read_only": True,
            "tools": tools,
        }
    )


async def post_harness_message_response(
    payload: HarnessMessageRequest,
    request: Request,
    *,
    conversation_provider: Callable[[], HarnessConversationService],
    tools_provider: Callable[[Actor, str], list[dict[str, str]]],
    actor_provider: Callable[[Request], Actor],
) -> dict[str, Any]:
    actor = actor_provider(request)
    receipt = await asyncio.to_thread(
        conversation_provider().send_message,
        actor=actor,
        session_id=payload.session_id,
        request_id=payload.request_uuid,
        message=payload.message,
    )
    tools = await asyncio.to_thread(tools_provider, actor, payload.request_uuid)
    return api_success(
        {
            "session_id": receipt.session_id,
            "request_uuid": receipt.request_id,
            "message_id": receipt.assistant_message.message_id,
            "created_at": receipt.assistant_message.created_at.isoformat(),
            "persistence_status": receipt.persistence_status,
            "status": "COMPLETED",
            "assistant_message": receipt.assistant_message.content,
            "result": receipt.assistant_message.content,
            "read_only": True,
            "tools": tools,
        }
    )


def create_harness_router(
    *,
    conversation_provider: Callable[[], HarnessConversationService],
    tools_provider: Callable[[Actor, str], list[dict[str, str]]],
    actor_provider: Callable[[Request], Actor],
) -> APIRouter:
    router = APIRouter()

    @router.post("/internal/v1/harness/sessions")
    async def create_session(
        payload: HarnessSessionRequest,
        request: Request,
    ) -> dict[str, Any]:
        return await create_harness_session_response(
            payload,
            request,
            conversation_provider=conversation_provider,
            tools_provider=tools_provider,
            actor_provider=actor_provider,
        )

    @router.post("/internal/v1/harness/messages")
    async def post_message(
        payload: HarnessMessageRequest,
        request: Request,
    ) -> dict[str, Any]:
        return await post_harness_message_response(
            payload,
            request,
            conversation_provider=conversation_provider,
            tools_provider=tools_provider,
            actor_provider=actor_provider,
        )

    return router


def harness_error_response(request: Request, exc: HarnessError) -> JSONResponse:
    if not request.url.path.startswith("/internal/v1/harness/"):
        raise exc
    status_by_code = {
        "HARNESS_PRINCIPAL_INVALID": 403,
        "HARNESS_PRINCIPAL_MISMATCH": 403,
        "HARNESS_SESSION_NOT_FOUND": 404,
        "HARNESS_TOOL_NOT_FOUND": 404,
        "HARNESS_IDEMPOTENCY_CONFLICT": 409,
        "HARNESS_TOOL_STALE": 409,
        "HARNESS_LIMIT_EXCEEDED": 429,
        "HARNESS_RUNTIME_PRODUCTION_GATED": 503,
        "HARNESS_SANDBOX_UNAVAILABLE": 503,
        "HARNESS_SIDECAR_UNAVAILABLE": 503,
        "HARNESS_CATALOG_UNAVAILABLE": 503,
        "HARNESS_GATEWAY_UNAVAILABLE": 503,
        "HARNESS_GATEWAY_FAILED": 502,
    }
    return JSONResponse(
        status_code=status_by_code.get(exc.code, 422),
        content=api_failure(
            exc.code,
            redact_text(str(exc))[:500] or "Harness request failed",
        ),
    )


__all__ = [
    "HarnessMessageRequest",
    "HarnessSessionRequest",
    "create_harness_router",
    "create_harness_session_response",
    "harness_error_response",
    "post_harness_message_response",
    "public_harness_tools",
]
