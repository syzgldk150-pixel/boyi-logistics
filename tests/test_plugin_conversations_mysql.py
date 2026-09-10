"""Chat tool selection through the real installed-package/Invocation runtime."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.harness.catalog import HarnessToolCatalog
from agent.harness.errors import HarnessError
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness_application import HarnessConversationService
from agent.harness_online import OnlineHarnessSidecar
from agent.llm_client import LLMClient
from agent.plugin_conversations import PluginConversationService
from tests.direct_invocation_fixture import direct_repository  # noqa: F401
from tests.test_direct_plugin_invocation_mysql import ACTOR, direct_runtime, _legacy_counts  # noqa: F401


def test_chat_executes_installed_plugin_and_replays_original_result(direct_runtime, monkeypatch):  # noqa: F811
    management, runtime, identity = direct_runtime
    plugins = PluginConversationService(management.policy)
    choices = [item for item in plugins.targets(actor=ACTOR, source="console") if item.automation_id == identity]
    entry, contract = management.policy._load_contract(identity)
    assert len(choices) == 1, {"enabled": entry.enabled, "configured": entry.configured,
        "entrypoints": entry.committed_snapshot.enabled_entrypoints, "stable": str(entry.reconcile_state),
        "can_full_auto": contract.can_full_auto, "contracts": tuple(contract.invocation_contracts)}
    selected = choices[0]
    model = LLMClient()
    calls = []
    monkeypatch.setattr(model, "public_status", lambda: {"configured": True})
    async def chat(messages, *, tools):
        calls.append(messages)
        assert selected.model_tool() in tools
        return {"tool_calls": [{"id": "call-test", "function": {"name": selected.handle, "arguments": "{}"}}]}
    monkeypatch.setattr(model, "chat", chat)
    catalog = HarnessToolCatalog(invocation_port=SimpleNamespace(invoke=lambda **_: pytest.fail("unexpected read")), fixed_tools=())
    service = HarnessConversationService(repository=InMemoryHarnessSessionRepository(), plugin_conversations=plugins,
        sidecar_factory=lambda actor, request_id: OnlineHarnessSidecar(catalog=catalog, llm=model,
            plugin_turn=plugins.turn(actor=actor, source="console", request_id=request_id)))
    session = service.create_session(actor=ACTOR, request_id=str(uuid4()))
    before = _legacy_counts(management.repository)
    request_id = str(uuid4())
    kwargs = dict(actor=ACTOR, session_id=session.session_id, request_id=request_id, message="请执行直接执行测试")
    receipt = service.send_message(**kwargs)
    assert len(receipt.plugin_invocations) == 1
    invocation_id = receipt.plugin_invocations[0]["invocation_id"]
    assert runtime.service.wait_sync(invocation_id)["status"] == "COMPLETED"
    replay = service.send_message(**kwargs)
    assert replay.plugin_invocations == receipt.plugin_invocations
    assert len(calls) == 1
    result = service.plugin_action(actor=ACTOR, session_id=session.session_id, request_id=str(uuid4()),
        invocation_id=invocation_id, action="status", selected_indices=[])
    assert result["status"] == "COMPLETED"
    assert json.loads(result["result_text"])["message"] == "Service v2 example is ready."
    other_session = service.create_session(actor=ACTOR, request_id=str(uuid4()))
    with pytest.raises(HarnessError, match="不属于当前会话"):
        service.plugin_action(actor=ACTOR, session_id=other_session.session_id, request_id=str(uuid4()),
            invocation_id=invocation_id, action="cancel", selected_indices=[])
    assert _legacy_counts(management.repository) == before


def test_chat_rechecks_live_settings_and_rejects_model_overrides(direct_runtime):  # noqa: F811
    management, _runtime, identity = direct_runtime
    plugins = PluginConversationService(management.policy)
    turn = plugins.turn(actor=ACTOR, source="console", request_id=str(uuid4()))
    target = next(item for item in turn.choices.values() if item.automation_id == identity)
    for arguments in ({"dry_run": False}, {"account_id": "invented"}, {"date": "2026-09-10"}):
        with pytest.raises(HarnessError):
            turn.select([(target.handle, arguments)])
    with pytest.raises(HarnessError):
        turn.select([(target.handle, {}), (target.handle, {})])
    selected = turn.select([(target.handle, {})])
    entry = management.catalog.require(identity)
    management.management.save_plugin_settings(identity, config={}, account_bindings={}, resource_bindings={},
        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    management.targets.reconcile_project(identity)
    result = turn.start_console(selected)
    assert result[0]["status"] == "REJECTED" and "invocation_id" not in result[0]


def test_conversation_action_requires_signed_principal(direct_runtime):  # noqa: F811
    from agent.orchestration.models import Actor, ActorType
    management, _runtime, _identity = direct_runtime
    plugins = PluginConversationService(management.policy)
    for actor, source in ((Actor(ActorType.LEGACY_API, "guest"), "console"),
                          (Actor(ActorType.FEISHU_USER, "guest", authenticated_by="feishu_event"), "feishu")):
        with pytest.raises(HarnessError):
            plugins.targets(actor=actor, source=source)
