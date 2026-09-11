"""Both transports use one model loop, query path and current identity authority."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from agent.core import AgentCore
from agent.channel_chat import reply_to_feishu
from agent.harness.errors import HarnessError
from agent.orchestration.models import Actor, ActorType
from shared.identity_permissions import IdentityAccess
from tests.chat_runtime_support import configure_chat
from tests.test_identity_interfaces import MutableAuthority


CONSOLE = Actor(ActorType.CONSOLE_ADMIN, "17", ("admin",), authenticated_by="mysql_admin_session")
FEISHU = Actor(ActorType.FEISHU_USER, "ou-isolated", ("admin",), authenticated_by="feishu_admin_binding")


def test_console_and_feishu_read_the_same_tracking_data_without_calling_model():
    reader = Mock(return_value={"tracking_number": "R00014513348", "route_rows": []})
    core = AgentCore(direct_tool_runners={"track_waybill": reader})
    core.llm = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("exact reads bypass LLM")))
    service, calls = configure_chat(core)
    result = asyncio.run(core.handle_message("R00014513348", actor=CONSOLE, source="console"))
    feishu = reply_to_feishu(service, actor=FEISHU, chat_id="group", event_id="event", message="R00014513348")
    assert result["reply"] == feishu["reply"]
    assert len(calls) == reader.call_count == 2
    core.llm.chat.assert_not_awaited()


def test_same_model_prompt_for_both_channels_and_group_members_have_separate_history():
    transcripts = []
    async def model(messages, **_):
        transcripts.append(messages)
        return {"content": "你好，请告诉我你的业务问题。"}
    core = AgentCore()
    core.llm = SimpleNamespace(chat=model)
    service, _ = configure_chat(core)
    asyncio.run(core.handle_message("你好", actor=CONSOLE, source="console"))
    reply_to_feishu(service, actor=FEISHU, chat_id="group", event_id="first", message="你好")
    other = Actor(ActorType.FEISHU_USER, "ou-other", ("admin",), authenticated_by="feishu_admin_binding")
    reply_to_feishu(service, actor=other, chat_id="group", event_id="second", message="我有问题")
    assert transcripts[0] == transcripts[1]
    assert [row["content"] for row in transcripts[2] if row["role"] == "user"] == ["我有问题"]


def test_chat_permission_revocation_rejects_existing_session_on_both_transports():
    authority = MutableAuthority("ai.chat", "business.query")
    core = AgentCore()
    core.llm = SimpleNamespace(chat=AsyncMock(return_value={"content": "你好"}))
    service, _ = configure_chat(core, authority=authority)
    first = asyncio.run(core.handle_message("你好", actor=CONSOLE, source="console"))
    reply_to_feishu(service, actor=FEISHU, chat_id="group", event_id="first", message="你好")
    authority.state = IdentityAccess(True, permissions=("business.query",))
    with pytest.raises(HarnessError, match="对话权限"):
        asyncio.run(core.handle_message("继续", actor=CONSOLE, source="console", conversation_id=first["conversation_id"]))
    with pytest.raises(HarnessError, match="对话权限"):
        reply_to_feishu(service, actor=FEISHU, chat_id="group", event_id="second", message="继续")
    assert core.llm.chat.await_count == 2


def test_console_refresh_reuses_its_chat_and_preserves_messages():
    from agent.channel_chat import primary_chat_request
    from agent.harness_api import HarnessSessionRequest, create_harness_session_response
    core = AgentCore()
    core.llm = SimpleNamespace(chat=AsyncMock(return_value={"content": "你好", "tool_calls": []}))
    service, _ = configure_chat(core)
    session = service.create_session(actor=CONSOLE, request_id=primary_chat_request(CONSOLE, channel="console"))
    service.send_message(actor=CONSOLE, session_id=session.session_id, request_id=str(uuid4()), message="你好")
    for _ in range(20):
        response = asyncio.run(create_harness_session_response(HarnessSessionRequest(request_uuid=str(uuid4())), None,
            conversation_provider=lambda: service, tools_provider=lambda *_: [], actor_provider=lambda _: CONSOLE))
        assert response["data"]["session_id"] == session.session_id
        assert [item["content"] for item in response["data"]["messages"]] == ["你好", "你好"]


def test_unverified_feishu_cannot_open_common_chat_and_console_binding_stays_strict():
    from agent.harness_application import bind_signed_console_admin
    core = AgentCore()
    core.llm = SimpleNamespace(chat=AsyncMock())
    service, _ = configure_chat(core)
    fake = Actor(ActorType.FEISHU_USER, "ou-forged", ("admin",), authenticated_by="feishu_event")
    with pytest.raises(HarnessError):
        service.create_session(actor=fake, request_id=str(uuid4()))
    with pytest.raises(HarnessError):
        bind_signed_console_admin(FEISHU)
    core.llm.chat.assert_not_awaited()


def test_one_slow_conversation_does_not_block_another_channel_or_duplicate_execution():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from agent.harness_application import HarnessConversationService
    from agent.harness.sessions import InMemoryHarnessSessionRepository
    from agent.harness.sidecar import SidecarResult

    started, release, parallel_finished = Event(), Event(), Event()
    call_lock, calls = Lock(), []

    class Sidecar:
        def run(self, *, messages, timeout_seconds):
            message = messages[-1].content
            with call_lock:
                calls.append(message)
            if message == "slow":
                started.set()
                assert release.wait(5)
            else:
                parallel_finished.set()
            return SidecarResult(message, 0)

    service = HarnessConversationService(repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=lambda *_: Sidecar())
    session = service.create_session(actor=CONSOLE, request_id=str(uuid4()))
    request_id = str(uuid4())
    def slow():
        return service.send_message(actor=CONSOLE, session_id=session.session_id,
            request_id=request_id, message="slow")
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(slow)
        assert started.wait(2)
        duplicate = pool.submit(slow)
        fast = pool.submit(reply_to_feishu, service, actor=FEISHU,
            chat_id="other-channel", event_id="one", message="fast")
        try:
            assert parallel_finished.wait(2), "an unrelated conversation was blocked"
            assert fast.result(timeout=2)["reply"] == "fast"
            assert not duplicate.done()
        finally:
            release.set()
        assert not first.result(timeout=2).replayed
        assert duplicate.result(timeout=2).replayed
    assert calls.count("slow") == calls.count("fast") == 1


@pytest.mark.parametrize("actor", [CONSOLE, FEISHU])
def test_explicit_clear_recovers_full_chat_without_model_or_plugin_execution(actor):
    from agent.harness_application import HarnessConversationService
    from agent.harness.sessions import InMemoryHarnessSessionRepository
    from agent.harness.sidecar import SidecarResult

    transcripts = []
    def respond(**kwargs):
        transcripts.append(kwargs["messages"])
        return SidecarResult("已收到", 0)
    repository = InMemoryHarnessSessionRepository(max_sessions=1, max_sessions_per_principal=1)
    service = HarnessConversationService(repository=repository, sidecar_factory=lambda *_: SimpleNamespace(run=respond))
    session = service.create_session(actor=actor, request_id=str(uuid4()))
    for index in range(32):
        service.send_message(actor=actor, session_id=session.session_id, request_id=str(uuid4()), message=f"消息{index}")
    assert len(repository.get(principal_id=actor.actor_id, session_id=session.session_id).messages) == 64
    kwargs = dict(actor=actor, session_id=session.session_id, request_id=str(uuid4()), message="清空会话")
    cleared = service.send_message(**kwargs)
    assert len(cleared.session.messages) == 2
    assert cleared.tool_calls == 0 and not cleared.plugin_requests and not cleared.plugin_invocations
    assert service.send_message(**kwargs).replayed
    assert len(transcripts) == 32
    service.send_message(actor=actor, session_id=session.session_id, request_id=str(uuid4()), message="新问题")
    assert [item.content for item in transcripts[-1] if item.role == "user"] == ["清空会话", "新问题"]
    assert len(service._message_tool_calls) == 2
