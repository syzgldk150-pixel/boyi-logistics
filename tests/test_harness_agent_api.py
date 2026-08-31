from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.harness.errors import HarnessError
from agent import harness_api
from agent.orchestration.models import ActorType


PRINCIPAL = {
    "actor_type": "console_admin",
    "actor_id": "17",
    "roles": ["admin"],
    "display_name": "Offline admin",
    "authenticated_by": "mysql_admin_session",
}
REQUEST_UUID = "123e4567-e89b-42d3-a456-426614174000"
SESSION_UUID = "123e4567-e89b-42d3-a456-426614174001"
MESSAGE_UUID = "123e4567-e89b-42d3-a456-426614174002"


def _request(principal: dict[str, object] | None = PRINCIPAL) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(console_principal=principal))


def test_harness_request_models_are_closed() -> None:
    with pytest.raises(ValidationError):
        harness_api.HarnessSessionRequest(
            request_uuid=REQUEST_UUID,
            automation_id="forbidden",
        )
    with pytest.raises(ValidationError):
        harness_api.HarnessMessageRequest(
            request_uuid=REQUEST_UUID,
            session_id=SESSION_UUID,
            message="x" * 4_001,
        )


def test_agent_harness_session_binds_signed_actor_and_projects_gate() -> None:
    import main

    calls: list[dict[str, object]] = []

    class Service:
        def create_session(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                session_id=SESSION_UUID,
                request_id=REQUEST_UUID,
                persistence_status="MEMORY_ONLY_NON_PRODUCTION",
            )

    service = Service()
    result = asyncio.run(
        harness_api.create_harness_session_response(
            harness_api.HarnessSessionRequest(request_uuid=REQUEST_UUID),
            _request(),
            conversation_provider=lambda: service,
            tools_provider=lambda _actor, _request_id: [
                {
                    "tool_id": "knowledge.search",
                    "title": "Search knowledge",
                    "description": "Read-only.",
                }
            ],
            actor_provider=main._require_console_admin_request,
        )
    )

    assert result["ok"] is True
    assert result["data"] == {
        "session_id": SESSION_UUID,
        "request_uuid": REQUEST_UUID,
        "persistence_status": "MEMORY_ONLY_NON_PRODUCTION",
        "status": "PRODUCTION_GATED",
        "availability": "PRODUCTION_GATED",
        "blocked_reason": "HARNESS_RUNTIME_PRODUCTION_GATED",
        "read_only": True,
        "tools": [
            {
                "tool_id": "knowledge.search",
                "title": "Search knowledge",
                "description": "Read-only.",
            }
        ],
    }
    actor = calls[0]["actor"]
    assert actor.actor_type is ActorType.CONSOLE_ADMIN
    assert actor.actor_id == "17"
    assert actor.authenticated_by == "mysql_admin_session"


def test_agent_harness_message_returns_only_bounded_conversation_projection() -> None:
    import main

    class Service:
        def send_message(self, **_kwargs):
            return SimpleNamespace(
                session_id=SESSION_UUID,
                request_id=REQUEST_UUID,
                persistence_status="MEMORY_ONLY_NON_PRODUCTION",
                assistant_message=SimpleNamespace(
                    message_id=MESSAGE_UUID,
                    content="Offline result",
                    created_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
                ),
            )

    service = Service()
    result = asyncio.run(
        harness_api.post_harness_message_response(
            harness_api.HarnessMessageRequest(
                request_uuid=REQUEST_UUID,
                session_id=SESSION_UUID,
                message="Read only",
            ),
            _request(),
            conversation_provider=lambda: service,
            tools_provider=lambda _actor, _request_id: [],
            actor_provider=main._require_console_admin_request,
        )
    )

    assert result["ok"] is True
    assert result["data"] == {
        "session_id": SESSION_UUID,
        "request_uuid": REQUEST_UUID,
        "message_id": MESSAGE_UUID,
        "created_at": "2026-08-31T00:00:00+00:00",
        "persistence_status": "MEMORY_ONLY_NON_PRODUCTION",
        "status": "COMPLETED",
        "assistant_message": "Offline result",
        "result": "Offline result",
        "read_only": True,
        "tool_calls": 0,
        "tools": [],
    }


def test_agent_harness_endpoints_reject_unsigned_principal() -> None:
    import main

    with pytest.raises(Exception, match="signed Console"):
        asyncio.run(
            harness_api.create_harness_session_response(
                harness_api.HarnessSessionRequest(request_uuid=REQUEST_UUID),
                _request(None),
                conversation_provider=lambda: SimpleNamespace(
                    create_session=lambda **_kwargs: None
                ),
                tools_provider=lambda _actor, _request_id: [],
                actor_provider=main._require_console_admin_request,
            )
        )


def test_harness_production_gate_maps_to_service_unavailable() -> None:
    import main

    request = SimpleNamespace(
        url=SimpleNamespace(path="/internal/v1/harness/messages")
    )
    response = asyncio.run(
        main.harness_error_handler(
            request,
            HarnessError(
                "Harness production sidecar is not enabled",
                code="HARNESS_RUNTIME_PRODUCTION_GATED",
            ),
        )
    )

    assert response.status_code == 503
    assert b"HARNESS_RUNTIME_PRODUCTION_GATED" in response.body


def test_catalog_excludes_fixed_tools_without_runtime_handlers() -> None:
    import main

    class Registry:
        def active_snapshot(self):
            return ()

    actor = main._require_console_admin_request(_request())
    tools = harness_api.public_harness_tools(
        policy_service=object(),
        contribution_registry=Registry(),
        actor=actor,
        request_id=REQUEST_UUID,
    )

    assert tools == []


def test_agent_harness_session_projects_restricted_runtime_readiness() -> None:
    import main

    service = SimpleNamespace(
        create_session=lambda **_kwargs: SimpleNamespace(
            session_id=SESSION_UUID,
            request_id=REQUEST_UUID,
            persistence_status="MEMORY_ONLY_NON_PRODUCTION",
        )
    )
    result = asyncio.run(
        harness_api.create_harness_session_response(
            harness_api.HarnessSessionRequest(request_uuid=REQUEST_UUID),
            _request(),
            conversation_provider=lambda: service,
            tools_provider=lambda _actor, _request_id: [],
            actor_provider=main._require_console_admin_request,
            availability_provider=lambda: {
                "status": "READY",
                "availability": "OFFLINE_RESTRICTED",
                "blocked_reason": None,
            },
        )
    )
    assert result["data"]["status"] == "READY"
    assert result["data"]["availability"] == "OFFLINE_RESTRICTED"
    assert result["data"]["blocked_reason"] is None
