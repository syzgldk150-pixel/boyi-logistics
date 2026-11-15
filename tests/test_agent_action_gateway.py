"""Agent layer P1: the action gateway, argument grounding and their channel wiring.

The model only selects and copies; code grounds every argument in the person's
own words, asks back when it cannot, and never runs a model-filled or
question-triggered write before the person confirms.
"""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.agent_actions.gateway import ActionGateway, looks_like_question
from agent.agent_actions.grounding import GroundingError, ground_inputs
from agent.agent_actions.pending import PendingConfirmations
from agent.agent_actions.spec import ActionSpec
from agent.core import AgentCore
from agent.harness.errors import HarnessError
from agent.orchestration import agent_confirmation
from agent.orchestration.agent_confirmation import (
    AGENT_CONFIRMATION_REQUIRED, confirmation_required, person_confirmed, require_person_confirmation,
)
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.plugin_conversations import ConversationPluginRequest, ConversationPluginTarget, PluginConversationService
from shared.identity_permissions import IdentityAccess, chat_scope
from tests.chat_runtime_support import configure_chat

QUOTE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["address", "weight"], "properties": {
    "address": {"type": "string", "minLength": 1, "maxLength": 500},
    "weight": {"type": "number", "minimum": 0.000001},
    "volume": {"type": "number", "minimum": 0.000001}}}
CONSOLE = Actor(ActorType.CONSOLE_ADMIN, "17", ("admin",), authenticated_by="mysql_admin_session")
FEISHU = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin",), authenticated_by="feishu_admin_binding")


# -- grounding ---------------------------------------------------------------------------

def test_values_copied_from_the_message_are_accepted():
    message = "报个价，测试市测试区示例路 1 号，300 公斤 2 方"
    assert ground_inputs(QUOTE_SCHEMA, {"address": "测试市测试区示例路1号", "weight": 300.0, "volume": 2}, message) == {
        "address": "测试市测试区示例路1号", "weight": 300, "volume": 2}
    assert ground_inputs(QUOTE_SCHEMA, {"address": "测试市", "weight": "12.5"}, "测试市 １２.５公斤")["weight"] == 12.5


@pytest.mark.parametrize("arguments,code", [
    ({"address": "另一个城市", "weight": 300}, "ARGUMENT_NOT_GROUNDED"),   # invented address
    ({"address": "测试市", "weight": 3000}, "ARGUMENT_NOT_GROUNDED"),      # converted / invented number
    ({"address": "测试市"}, "ARGUMENT_MISSING"),                           # required weight missing
    ({"address": "测试市", "weight": 300, "account": "x"}, "ARGUMENT_INVALID"),
    ({"address": "测试市", "weight": True}, "ARGUMENT_NOT_GROUNDED"),
])
def test_ungrounded_arguments_become_questions(arguments, code):
    with pytest.raises(GroundingError) as raised:
        ground_inputs(QUOTE_SCHEMA, arguments, "发到测试市 300 公斤")
    assert raised.value.code == code and raised.value.question


def test_non_scalar_fields_are_never_filled_by_the_model():
    schema = {"type": "object", "properties": {"dry_run": {"type": "boolean"}}}
    with pytest.raises(GroundingError) as raised:
        ground_inputs(schema, {"dry_run": False}, "执行")
    assert raised.value.code == "ARGUMENT_FORBIDDEN"


def test_waybill_numbers_must_be_in_the_message_and_valid():
    schema = {"type": "object", "properties": {"tracking_number": {"type": "string"}}}
    with pytest.raises(GroundingError):
        ground_inputs(schema, {"tracking_number": "R90000000001"}, "查一下物流")
    with pytest.raises(GroundingError):
        ground_inputs(schema, {"tracking_number": "X1"}, "查 X1")


# -- gateway -----------------------------------------------------------------------------

def _authority(**agent):
    access = IdentityAccess(active=True, person_id="p-1", agent=agent, channel="agent")
    return SimpleNamespace(for_actor=lambda actor: access)


QUOTE = ActionSpec(action_id="plugin:quote", title="三平台报价", effect="read", capability="plugins.execute",
                   input_schema=QUOTE_SCHEMA)
WRITE = ActionSpec(action_id="plugin:list", title="到货清单", effect="internal_projection_write", capability="plugins.execute")
SCAN = ActionSpec(action_id="plugin:scan", title="扫描", effect="external_write", capability="plugins.execute", preview=True)
FINANCE = ActionSpec(action_id="finance", title="财务汇总", effect="read", capability="finance.read")


def test_gateway_checks_identity_and_capability_live():
    inactive = SimpleNamespace(for_actor=lambda actor: IdentityAccess())
    assert ActionGateway(identity_access=inactive).decide(WRITE, actor=FEISHU, arguments={}, message="同步到货清单").decision == "REJECT"
    denied = ActionGateway(identity_access=_authority(**{"ai.chat": "direct"}))
    assert denied.decide(WRITE, actor=FEISHU, arguments={}, message="同步到货清单").error_code == "PERMISSION_DENIED"
    private = ActionGateway(identity_access=_authority(**{"finance.read": "private"}))
    with chat_scope("group"):
        assert private.decide(FINANCE, actor=FEISHU, arguments={}, message="财务").error_code == "PRIVATE_CHAT_ONLY"
    with chat_scope("private"):
        assert private.decide(FINANCE, actor=FEISHU, arguments={}, message="财务").decision == "EXECUTE"


def test_gateway_decisions():
    gateway = ActionGateway()
    assert gateway.decide(WRITE, actor=None, arguments={}, message="同步一下到货清单").decision == "EXECUTE"
    assert gateway.decide(WRITE, actor=None, arguments={}, message="到货清单为什么没更新").decision == "CONFIRM"
    assert gateway.decide(SCAN, actor=None, arguments={}, message="今天扫描了多少票").decision == "EXECUTE"  # preview only
    assert gateway.decide(WRITE, actor=None, arguments={"dry_run": False}, message="同步").decision == "REJECT"
    quote = gateway.decide(QUOTE, actor=None, arguments={"address": "测试市", "weight": 50}, message="发到测试市 50 公斤多少钱")
    assert quote.decision == "EXECUTE" and quote.arguments == {"address": "测试市", "weight": 50}
    clarify = gateway.decide(QUOTE, actor=None, arguments={"address": "测试市"}, message="发到测试市多少钱")
    assert clarify.decision == "CLARIFY" and "重量" in clarify.question
    filled_write = ActionSpec(action_id="w", title="回复", effect="external_write", capability="customer_service",
                              input_schema={"type": "object", "properties": {"content": {"type": "string"}}})
    assert gateway.decide(filled_write, actor=None, arguments={"content": "已安排派送"},
                          message="回复已安排派送").decision == "CONFIRM"


@pytest.mark.parametrize("text,question", [("今天扫描多少件", True), ("执行扫描", False), ("同步一下到货清单", False),
                                           ("查扫描量", True), ("扫描是怎么判断下一站的？", True)])
def test_question_detection(text, question):
    assert looks_like_question(text) is question


def test_pending_store_takes_once_and_expires():
    now = [0.0]
    store = PendingConfirmations(10, clock=lambda: now[0])
    store.remember("a", "x")
    assert store.peek("a") == "x" and store.take("a") == "x" and store.take("a") is None
    store.remember("a", "y")
    now[0] = 11
    assert store.peek("a") is None
    store.remember("b", "z")
    now[0] = 30
    assert store.take_with_state("b") == ("expired", None) and store.take_with_state("b") == ("missing", None)


def test_confirmation_required_overrides_direct_level():
    authority = _authority(**{"plugins.execute": "direct"})
    actor = Actor(ActorType.FEISHU_USER, "ou-1", ("admin",), authenticated_by="feishu_admin_binding")
    require_person_confirmation(identity_access=authority, actor=actor, capability="plugins.execute", title="统计")
    with confirmation_required():
        with pytest.raises(OrchestrationError) as raised:
            require_person_confirmation(identity_access=authority, actor=actor, capability="plugins.execute", title="统计")
        assert raised.value.code == AGENT_CONFIRMATION_REQUIRED
        with person_confirmed():
            require_person_confirmation(identity_access=authority, actor=actor, capability="plugins.execute", title="统计")


# -- conversation wiring -----------------------------------------------------------------

def _quote_target(source="console"):
    return ConversationPluginTarget(
        handle="run_plugin_quote", automation_id="quote-1", generation=1, configuration_version=1,
        contribution_id="command", title="三平台报价", description="报价", effect="read",
        command="三平台报价" if source == "feishu" else "", plugin_id="quote_prices_v2",
        input_schema_json=json.dumps(QUOTE_SCHEMA))


class _Service(PluginConversationService):
    def __init__(self, policy, targets):
        super().__init__(policy)
        self._targets = targets

    def targets(self, **_kwargs):
        return self._targets


def _model(*responses):
    replies = iter(responses)
    async def chat(messages, **_):
        return next(replies)
    return chat


def _call(name, arguments):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


def test_console_quote_runs_with_grounded_inputs():
    invoked = []
    def invoke_trusted(automation_id, **kwargs):
        invoked.append(kwargs)
        return {"invocation_id": "00000000-0000-4000-8000-0000000000ab", "status": "RUNNING"}
    service = _Service(SimpleNamespace(invoke_trusted=invoke_trusted), (_quote_target(),))
    core = AgentCore()
    core.llm = SimpleNamespace(chat=_model(_call("run_plugin_quote", {"address": "测试市测试区", "weight": 300, "volume": 2})))
    configure_chat(core, plugins=service)
    result = asyncio.run(core.handle_message("报个价 测试市测试区 300公斤 2方", actor=CONSOLE, source="console"))
    assert invoked[0]["trusted_context"] == {"dynamic_inputs": {"address": "测试市测试区", "weight": 300, "volume": 2}}
    assert result["plugin_invocations"][0]["status"] == "RUNNING"


def test_console_quote_with_invented_value_asks_back_and_runs_nothing():
    service = _Service(SimpleNamespace(invoke_trusted=lambda *a, **k: pytest.fail("must not run")), (_quote_target(),))
    core = AgentCore()
    core.llm = SimpleNamespace(chat=_model(_call("run_plugin_quote", {"address": "测试市测试区", "weight": 30})))
    configure_chat(core, plugins=service)
    result = asyncio.run(core.handle_message("报个价 测试市测试区 300公斤", actor=CONSOLE, source="console"))
    assert "没有在你的话里找到重量" in result["reply"] and not result["plugin_invocations"]


def test_ask_user_question_is_returned_verbatim_with_options():
    core = AgentCore()
    core.llm = SimpleNamespace(chat=_model(_call("ask_user", {"question": "要报价的收货地址是哪里？", "options": ["长沙", "株洲"]})))
    configure_chat(core)
    result = asyncio.run(core.handle_message("帮我算一下运费", actor=CONSOLE, source="console"))
    assert result["reply"] == "要报价的收货地址是哪里？\n1. 长沙\n2. 株洲"


def test_question_triggered_console_write_waits_for_confirm():
    invoked = []
    def invoke_trusted(automation_id, **kwargs):
        if not agent_confirmation._CONFIRMED.get():
            pytest.fail("submitted before confirmation")
        invoked.append(kwargs)
        return {"invocation_id": "00000000-0000-4000-8000-0000000000ac", "status": "RUNNING"}
    target = ConversationPluginTarget(handle="run_plugin_list", automation_id="list-1", generation=1, configuration_version=1,
                                      contribution_id="run", title="到货清单", description="同步", effect="internal_projection_write")
    service = _Service(SimpleNamespace(invoke_trusted=invoke_trusted), (target,))
    core = AgentCore()
    core.llm = SimpleNamespace(chat=_model(_call("run_plugin_list", {}), {"content": "unused"}))
    configure_chat(core, plugins=service)
    first = asyncio.run(core.handle_message("到货清单为什么没更新？", actor=CONSOLE, source="console"))
    assert "回复“确认”" in first["reply"] and not invoked
    second = asyncio.run(core.handle_message("确认", actor=CONSOLE, source="console", conversation_id=first["conversation_id"]))
    assert len(invoked) == 1 and "到货清单" in second["reply"]


class _FailingRecorder:
    def __init__(self):
        self.submitted = []

    def submit(self, turn, *, actor=None):
        self.submitted.append(turn)

    def record_now(self, turn, *, actor=None):
        raise RuntimeError("audit down")


def test_console_write_is_not_submitted_when_it_cannot_be_recorded():
    target = ConversationPluginTarget(handle="run_plugin_list", automation_id="list-1", generation=1, configuration_version=1,
                                      contribution_id="run", title="到货清单", description="同步", effect="internal_projection_write")
    service = _Service(SimpleNamespace(invoke_trusted=lambda *a, **k: pytest.fail("ran without audit")), (target,))
    core = AgentCore()
    core.llm = SimpleNamespace(chat=_model(_call("run_plugin_list", {})))
    recorder = _FailingRecorder()
    configure_chat(core, plugins=service, turn_recorder=recorder)
    with pytest.raises(HarnessError) as raised:
        asyncio.run(core.handle_message("同步一下到货清单", actor=CONSOLE, source="console"))
    assert raised.value.code == "AGENT_AUDIT_UNAVAILABLE"
    assert recorder.submitted[-1].outcome == "ERROR"


def test_feishu_write_handoff_requires_audit_first():
    target = ConversationPluginTarget(handle="run_plugin_list", automation_id="list-1", generation=1, configuration_version=1,
                                      contribution_id="run", title="到货清单", description="同步", effect="internal_projection_write",
                                      command="到货清单")
    service = _Service(SimpleNamespace(), (target,))
    core = AgentCore()
    core.llm = SimpleNamespace(chat=_model(_call("run_plugin_list", {})))
    configure_chat(core, plugins=service, turn_recorder=_FailingRecorder())
    with pytest.raises(HarnessError):
        asyncio.run(core.handle_message("同步一下到货清单", conversation_id="chat", actor=FEISHU, source="feishu",
                                        request_id=str(uuid4())))


def test_feishu_quote_request_carries_inputs_to_the_existing_dispatch(monkeypatch):
    from feishu import conversation_plugins, message_handler

    calls, replies = [], []
    async def dispatch(**kwargs):
        calls.append(kwargs)
        return True
    async def reply(chat_id, text, **kwargs):
        replies.append((text, kwargs.get("reply_type")))
    monkeypatch.setattr(message_handler, "_dispatch_service_v2_feishu_command", dispatch)
    monkeypatch.setattr(message_handler, "_reply_text", reply)
    request = ConversationPluginRequest(_quote_target("feishu"), {"address": "测试市", "weight": 50})
    asyncio.run(conversation_plugins.run_conversation_plugins((request,), chat_id="chat"))
    assert calls[0]["runtime_inputs"] == {"address": "测试市", "weight": 50} and calls[0]["quote_result"] is True
    assert calls[0]["text"] == "三平台报价" and not replies


def test_feishu_forced_confirmation_is_remembered_then_run_with_same_inputs(monkeypatch):
    from feishu import conversation_plugins, message_handler

    calls, replies = [], []
    async def dispatch(**kwargs):
        calls.append((kwargs, agent_confirmation._CONFIRMED.get()))
        return True
    async def reply(chat_id, text, **kwargs):
        replies.append(text)
    monkeypatch.setattr(message_handler, "_dispatch_service_v2_feishu_command", dispatch)
    monkeypatch.setattr(message_handler, "_reply_text", reply)
    monkeypatch.setattr(message_handler, "_WRITE_CONFIRMATIONS", {})
    target = ConversationPluginTarget(handle="h", automation_id="list-1", generation=1, configuration_version=1,
                                      contribution_id="run", title="到货清单", description="", effect="internal_projection_write",
                                      command="到货清单")
    token = message_handler._COMMAND_CONTEXT.set(message_handler.FeishuCommandContext("evt-1", "ou-clerk", "chat"))
    try:
        asyncio.run(conversation_plugins.run_conversation_plugins((ConversationPluginRequest(target, confirm=True),), chat_id="chat"))
        assert not calls and "回复“确认”" in replies[-1]
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)
    token = message_handler._COMMAND_CONTEXT.set(message_handler.FeishuCommandContext("evt-2", "ou-clerk", "chat"))
    try:
        assert asyncio.run(message_handler._handle_write_confirmation("确认", chat_id="chat", sender_id="ou-clerk"))
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)
    (kwargs, confirmed), = calls
    assert confirmed and kwargs["text"] == "到货清单" and kwargs["conversation_target"] is target
