"""Identity P3: agent writes set to "需确认" and private-only finance data."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.orchestration import agent_confirmation
from agent.orchestration.agent_confirmation import (
    AGENT_CONFIRMATION_REQUIRED,
    person_confirmed,
    require_person_confirmation,
)
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.identity_permissions import IdentityAccess, chat_scope, validate_agent_permissions
from tests.test_identity_interfaces import MutableAuthority

FEISHU = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin",), authenticated_by="feishu_admin_binding")
CONSOLE = Actor(ActorType.CONSOLE_ADMIN, "7", ("admin",), authenticated_by="mysql_admin_session")
ASSISTANT = Actor(ActorType.CONSOLE_ADMIN, "7", ("admin",), authenticated_by="mysql_admin_session", channel="agent")


def authority(level):
    access = MutableAuthority()
    access.state = IdentityAccess(True, console={"plugins.execute": "operate"}, agent={"plugins.execute": level})
    return access


def test_confirm_level_stops_agent_writes_until_the_person_confirms():
    for actor in (FEISHU, ASSISTANT):
        with pytest.raises(OrchestrationError) as exc:
            require_person_confirmation(identity_access=authority("confirm"), actor=actor,
                                        capability="plugins.execute", title="统计到货")
        assert exc.value.code == AGENT_CONFIRMATION_REQUIRED and "统计到货" in str(exc.value)
        with person_confirmed():
            require_person_confirmation(identity_access=authority("confirm"), actor=actor,
                                        capability="plugins.execute", title="统计到货")
        require_person_confirmation(identity_access=authority("direct"), actor=actor,
                                    capability="plugins.execute", title="统计到货")
    # Console pages, schedulers and webhooks never ask in chat.
    require_person_confirmation(identity_access=authority("confirm"), actor=CONSOLE,
                                capability="plugins.execute", title="统计到货")
    require_person_confirmation(identity_access=authority("confirm"),
                                actor=Actor(ActorType.SCHEDULER, "scheduler", authenticated_by="scheduler"),
                                capability="plugins.execute", title="统计到货")
    assert not agent_confirmation._CONFIRMED.get()  # the flag never leaks out of its block


# -- Feishu / WeChat --------------------------------------------------------------------

@pytest.fixture
def feishu(monkeypatch):
    from feishu import message_handler

    confirmed_calls = []

    async def dispatch(**kwargs):
        if not agent_confirmation._CONFIRMED.get():
            raise OrchestrationError(AGENT_CONFIRMATION_REQUIRED, "“统计到货”需要你确认后才执行")
        confirmed_calls.append(kwargs)
        return {"status": "COMPLETED", "success": True}

    reply = AsyncMock()
    monkeypatch.setattr(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(message_handler, "_reply_text", reply)
    monkeypatch.setattr(message_handler, "_WRITE_CONFIRMATIONS", {})
    return message_handler, reply, confirmed_calls


def _as(message_handler, *, event_id, sender, chat="group-chat"):
    return message_handler._COMMAND_CONTEXT.set(message_handler.FeishuCommandContext(event_id, sender, chat))


def test_feishu_write_waits_for_the_same_person_and_runs_as_a_new_request(feishu):
    message_handler, reply, calls = feishu

    async def flow():
        token = _as(message_handler, event_id="evt-command", sender="ou-clerk")
        try:
            assert await message_handler._dispatch_service_v2_feishu_command(text="统计到货数据", receive_id="group-chat")
        finally:
            message_handler._COMMAND_CONTEXT.reset(token)
        assert reply.call_args.kwargs["reply_type"] == "agent_write_confirmation_required"
        assert "回复“确认”立即执行" in reply.call_args.args[1] and not calls
        # Someone else in the group cannot confirm it.
        token = _as(message_handler, event_id="evt-other", sender="ou-other")
        try:
            assert not await message_handler._handle_write_confirmation("确认", chat_id="group-chat", sender_id="ou-other")
        finally:
            message_handler._COMMAND_CONTEXT.reset(token)
        assert not calls
        token = _as(message_handler, event_id="evt-confirm", sender="ou-clerk")
        try:
            assert await message_handler._handle_write_confirmation("确认", chat_id="group-chat", sender_id="ou-clerk")
        finally:
            message_handler._COMMAND_CONTEXT.reset(token)

    asyncio.run(flow())
    assert len(calls) == 1
    assert calls[0]["command_text"] == "统计到货数据" and calls[0]["event_id"] == "evt-confirm"
    assert message_handler._WRITE_CONFIRMATIONS == {}
    assert not agent_confirmation._CONFIRMED.get()


def test_feishu_cancel_and_expiry_never_run_the_write(feishu):
    message_handler, reply, calls = feishu

    async def ask(event_id):
        token = _as(message_handler, event_id=event_id, sender="ou-clerk")
        try:
            await message_handler._dispatch_service_v2_feishu_command(text="统计到货数据", receive_id="group-chat")
        finally:
            message_handler._COMMAND_CONTEXT.reset(token)

    async def answer(text, event_id):
        token = _as(message_handler, event_id=event_id, sender="ou-clerk")
        try:
            return await message_handler._handle_write_confirmation(text, chat_id="group-chat", sender_id="ou-clerk")
        finally:
            message_handler._COMMAND_CONTEXT.reset(token)

    async def flow():
        await ask("evt-1")
        assert not await answer("查一下单号", "evt-2")  # unrelated messages pass through
        assert await answer("取消", "evt-3")
        assert "没有执行" in reply.call_args.args[1]
        assert not await answer("确认", "evt-4")  # nothing pending any more
        await ask("evt-5")
        message_handler._WRITE_CONFIRMATIONS[("group-chat", "ou-clerk")]["expires_at"] = time.monotonic() - 1
        assert await answer("确认", "evt-6")
        assert reply.call_args.kwargs["reply_type"] == "agent_write_confirmation_expired"

    asyncio.run(flow())
    assert calls == []


# -- Console AI assistant ---------------------------------------------------------------

def test_console_assistant_remembers_the_plugin_and_runs_it_on_confirm():
    from agent.plugin_conversations import ConversationPluginTarget, PluginConversationService

    target = ConversationPluginTarget(handle="run_plugin_stats", automation_id="stats", generation=1, configuration_version=1,
                                      contribution_id="console-run", title="统计到货", description="", effect="write")
    invoked = []

    def invoke_trusted(automation_id, **kwargs):
        if not agent_confirmation._CONFIRMED.get():
            raise OrchestrationError(AGENT_CONFIRMATION_REQUIRED, "“统计到货”需要你确认后才执行")
        invoked.append(kwargs["request_id"])
        return {"invocation_id": "00000000-0000-4000-8000-0000000000aa", "status": "RUNNING"}

    class Service(PluginConversationService):
        def targets(self, **_kwargs):
            return (target,)

    service = Service(SimpleNamespace(invoke_trusted=invoke_trusted))
    first = service.turn(actor=ASSISTANT, source="console", request_id="request-1")
    receipts = first.start_console((target,))
    assert receipts[0]["status"] == "CONFIRMATION_REQUIRED" and "回复“确认”" in receipts[0]["message"]
    assert not invoked
    other = service.turn(actor=Actor(ActorType.CONSOLE_ADMIN, "8", ("admin",), authenticated_by="mysql_admin_session"),
                         source="console", request_id="request-x")
    assert not other.has_pending_writes()  # pending belongs to its own person
    second = service.turn(actor=ASSISTANT, source="console", request_id="request-2")
    assert second.has_pending_writes()
    receipts = second.confirm_pending_writes()
    assert receipts[0]["status"] == "RUNNING" and len(invoked) == 1
    assert not service.turn(actor=ASSISTANT, source="console", request_id="request-3").has_pending_writes()
    # Cancel drops it without running.
    service.turn(actor=ASSISTANT, source="console", request_id="request-4").start_console((target,))
    service.turn(actor=ASSISTANT, source="console", request_id="request-5").cancel_pending_writes()
    assert not service.turn(actor=ASSISTANT, source="console", request_id="request-6").has_pending_writes()
    assert len(invoked) == 1


# -- Finance data only in one-to-one chats ----------------------------------------------

def test_private_finance_level_is_refused_in_group_chats_only():
    assert validate_agent_permissions({"finance.read": "private"}) == {"finance.read": "private"}
    with pytest.raises(ValueError):
        validate_agent_permissions({"business.query": "private"})
    person = IdentityAccess(True, agent={"finance.read": "private", "business.query": "direct"}, channel="agent")
    anywhere = IdentityAccess(True, agent={"finance.read": "direct"}, channel="agent")
    boss = IdentityAccess(True, super_admin=True, channel="agent")
    with chat_scope("private"):
        assert person.allows("finance.read") and boss.allows("finance.read")
    with chat_scope("group"):
        assert not person.allows("finance.read") and person.blocked_in_group("finance.read")
        assert person.allows("business.query") and anywhere.allows("finance.read")
        assert not boss.allows("finance.read")  # other members would read it too
    # A request whose chat type is unknown is treated as a group.
    assert not person.allows("finance.read")


def test_group_finance_query_says_to_ask_in_private():
    from agent.direct_readers import invoke_registered_reader
    from agent.direct_tool_router import format_business_finance_reply
    from agent.tool_registry import ToolRegistry

    access = MutableAuthority()
    access.state = IdentityAccess(True, agent={"finance.read": "private"})
    calls = []

    def read():
        return asyncio.run(invoke_registered_reader(
            catalog=ToolRegistry(), name="query_business_finance", arguments={}, actor=FEISHU, source="feishu",
            handler=lambda args: calls.append(args) or {"success": True}, identity_access=access))

    with chat_scope("group"):
        result = read()
    assert result["error_code"] == "PRIVATE_CHAT_ONLY" and not calls
    assert format_business_finance_reply(result) == "财务数据只在私聊中回答，请私聊机器人查询。"
    access.state = IdentityAccess(True, agent={"business.query": "direct"})
    with chat_scope("group"):
        assert read()["error_code"] == "PERMISSION_DENIED"  # no finance right at all: the usual reply
