from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.harness.catalog import HarnessToolCatalog
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness_application import HarnessConversationService, TrustedHarnessInvocationAdapter, build_fixed_harness_tools
from agent.harness_online import OnlineHarnessSidecar, _minimize_value
from agent.llm_client import LLMClient
from agent.orchestration.models import Actor, ActorType
from agent.shipment_conversation import ShipmentConversation
from agent.shipment_queries import ShipmentQueryService
from shared.shipment_metrics import ShipmentQueryError
from tests.test_shipment_metrics import NOW, arguments, snapshot


ACTOR = Actor(ActorType.CONSOLE_ADMIN, "synthetic-one", ("super_admin",), authenticated_by="mysql_admin_session")


class ScriptedModel(LLMClient):
    def __init__(self):
        self.calls = []

    def public_status(self):
        return {"configured": True}

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append((messages, tools))
        text = messages[-1]["content"]
        params = arguments(start_date="", end_date="") if "测试站" in text else arguments(station="", period="yesterday", start_date="", end_date="")
        return {"tool_calls": [{"id": "synthetic-call", "function": {"name": "query_shipments", "arguments": json.dumps(params)}}]}


def build_service():
    reads, model = [], ScriptedModel()
    query = ShipmentQueryService(read_day=lambda station, day: reads.append((station, day)) or snapshot(day), clock=lambda: NOW)
    def factory(actor, request_id):
        adapter = TrustedHarnessInvocationAdapter(policy_service=SimpleNamespace(), actor=actor,
            base_request_id=request_id, fixed_handlers={"shipment.query": query})
        catalog = HarnessToolCatalog(invocation_port=adapter, fixed_tools=build_fixed_harness_tools())
        sidecar = OnlineHarnessSidecar(catalog=catalog, llm=model)
        sidecar._shipments = ShipmentConversation(clock=lambda: NOW)
        return sidecar
    return HarnessConversationService(repository=InMemoryHarnessSessionRepository(), sidecar_factory=factory), reads, model


def test_multi_turn_context_numeric_template_and_replay():
    service, reads, model = build_service()
    session = service.create_session(actor=ACTOR, request_id=str(uuid4())).session
    request_id = str(uuid4())
    first = service.send_message(actor=ACTOR, session_id=session.session_id, request_id=request_id, message="今天测试站发货多少吨？")
    assert "2.001 吨" in first.assistant_message.content
    again = service.send_message(actor=ACTOR, session_id=session.session_id, request_id=request_id, message="今天测试站发货多少吨？")
    assert again.replayed is True and len(reads) == 1 and len(model.calls) == 1
    result = service.send_message(actor=ACTOR, session_id=session.session_id, request_id=str(uuid4()), message="昨天呢？")
    assert "2026-09-23" in result.assistant_message.content
    assert reads[-1][0] == "测试站"
    # No second model turn can rewrite the program's result.
    assert len(model.calls) == 2


def test_new_member_and_new_session_cannot_inherit_another_query():
    service, reads, model = build_service()
    first = service.create_session(actor=ACTOR, request_id=str(uuid4())).session
    service.send_message(actor=ACTOR, session_id=first.session_id, request_id=str(uuid4()), message="今天测试站发货多少吨？")
    other = Actor(ActorType.CONSOLE_ADMIN, "synthetic-two", ("super_admin",), authenticated_by="mysql_admin_session")
    second = service.create_session(actor=other, request_id=str(uuid4())).session
    result = service.send_message(actor=other, session_id=second.session_id, request_id=str(uuid4()), message="昨天呢？")
    assert "请说明" in result.assistant_message.content
    assert len(reads) == 1
    with pytest.raises(HarnessError):
        service.send_message(actor=other, session_id=first.session_id, request_id=str(uuid4()), message="昨天呢？")


def test_expired_context_cannot_be_reconstructed_by_model_guessing():
    context = {"station": "测试站", "requested_at": (NOW-timedelta(minutes=21)).isoformat()}
    turn = ShipmentConversation(clock=lambda: NOW)
    turn.bind(context)
    with pytest.raises(ShipmentQueryError):
        turn.prepare(arguments(), message="昨天呢？")


@pytest.mark.parametrize("phrase", ["不要扫描，只查发货吨位", "今天扫描多少件", "查扫描量", "发货按什么规则统计"])
def test_read_questions_never_receive_plugin_write_tools(phrase):
    model = ScriptedModel()
    async def answer(messages, tools=None, **kwargs):
        assert not any(item["function"]["name"].startswith("run_plugin") for item in tools)
        return {"content": "只读查询说明。"}
    model.chat = answer
    def forbidden():
        raise AssertionError("write tools exposed to read question")
    plugins = SimpleNamespace(selected=(), model_tools=forbidden)
    adapter = TrustedHarnessInvocationAdapter(policy_service=SimpleNamespace(), actor=ACTOR, base_request_id=str(uuid4()), fixed_handlers={})
    sidecar = OnlineHarnessSidecar(catalog=HarnessToolCatalog(invocation_port=adapter), llm=model, plugin_turn=plugins)
    sidecar.run(messages=(HarnessMessage("user", phrase, str(uuid4())),), timeout_seconds=5)


def test_knowledge_exceptions_after_short_preview_survive_model_projection():
    body = "规则说明。" * 500 + "\n例外：作废单排除，不能套用示例数字。"
    assert _minimize_value({"body": body})["body"] == body
