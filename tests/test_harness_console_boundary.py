"""Exercise actual Agent conversation output at the Console response boundary."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness.sidecar import SidecarResult
from agent.harness_api import (
    HarnessMessageRequest,
    HarnessSessionRequest,
    create_harness_session_response,
    post_harness_message_response,
)
from agent.harness_application import HarnessConversationService
from agent.orchestration.models import Actor, ActorType
from console.services.harness import _project_harness_response


def test_console_accepts_actual_agent_session_and_restored_messages():
    actor = Actor(ActorType.CONSOLE_ADMIN, "17", roles=("super_admin",),
                  authenticated_by="mysql_admin_session")
    service = HarnessConversationService(
        repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=lambda *_: SimpleNamespace(
            run=lambda **_: SidecarResult(content="可以查询和执行已授权插件。", tool_calls=0)),
    )
    providers = dict(
        conversation_provider=lambda: service,
        tools_provider=lambda *_: [],
        actor_provider=lambda _: actor,
    )

    def session():
        result = asyncio.run(create_harness_session_response(
            HarnessSessionRequest(request_uuid=str(uuid4())), None, **providers))
        assert result["ok"]
        return _project_harness_response(result["data"])

    initial = session()
    assert initial["messages"] == []
    response = asyncio.run(post_harness_message_response(
        HarnessMessageRequest(request_uuid=str(uuid4()), session_id=initial["session_id"],
                              message="你好"), None, **providers))
    reply = _project_harness_response(response["data"])
    assert reply["assistant_message"] == "可以查询和执行已授权插件。"
    restored = session()
    assert restored["session_id"] == initial["session_id"]
    assert [(m["role"], m["content"]) for m in restored["messages"]] == [
        ("user", "你好"), ("assistant", reply["assistant_message"]),
    ]
    # Restored history must still use the same recursive field restrictions.
    restored["messages"][0]["automation_id"] = "must-not-be-exposed"
    with pytest.raises(ValueError, match="不允许展示"):
        _project_harness_response(restored)
