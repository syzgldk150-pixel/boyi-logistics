"""Model boundary checks; no business execution or production configuration."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.core import AgentCore
from agent.harness.errors import HarnessError
from agent.orchestration.models import Actor, ActorType
from agent.plugin_conversations import ConversationPluginTarget, PluginConversationTurn
from tests.chat_runtime_support import configure_chat


ACTOR = Actor(ActorType.FEISHU_USER, "chat-test", ("admin", "super_admin"), authenticated_by="feishu_admin_binding")


def make_core(response):
    targets = tuple(ConversationPluginTarget(handle=f"run_plugin_{index}", automation_id=f"instance-{index}", generation=1,
        configuration_version=1, contribution_id=None, title=title, description="隔离测试插件", effect="write", route_key=f"route-{index}")
        for index, title in enumerate(("统计到货", "扫描", "财务采集")))
    service = SimpleNamespace(targets=lambda **_: targets)
    service.turn = lambda **kwargs: PluginConversationTurn(service, **kwargs)
    core = AgentCore()
    core.memory = SimpleNamespace(get_or_create_conversation=lambda *_: "intent-test", get_recent_messages=lambda *_args, **_kwargs: [],
        search_knowledge=lambda *_args, **_kwargs: [], save_message=lambda *_args, **_kwargs: None)
    core.registry = SimpleNamespace(get_openai_tools=lambda: [])
    async def chat(messages, *, tools):
        assert len(tools) == len(targets)
        assert "不能替用户确认" in messages[0]["content"]
        assert "列出的入口均允许发起调用" in messages[0]["content"]
        assert all("当前调用权限：此工具已对当前管理员开放" in tool["function"]["description"] for tool in tools)
        return response
    core.llm = SimpleNamespace(chat=chat)
    core.configure_plugin_conversations(service)
    configure_chat(core, plugins=service)
    return core, targets


@pytest.mark.parametrize("phrase,names", [("请帮我把统计和扫描都跑起来", [0, 1]), ("执行一下财务采集插件，同步财务数据", [2])])
def test_natural_plugin_requests_reach_trusted_transport(phrase, names):
    core, targets = make_core({"tool_calls": [{"id": f"call-{index}", "function": {"name": f"run_plugin_{index}", "arguments": "{}"}} for index in names]})
    result = asyncio.run(core.handle_message(phrase, conversation_id="isolated-chat", actor=ACTOR, source="feishu", request_id=str(uuid4())))
    assert result["plugin_requests"] == tuple(targets[index] for index in names)
    assert "plugin_invocations" not in result  # The channel owns the actual start and result reply.


@pytest.mark.parametrize("arguments", ['{"dry_run":false}', '{"account_id":"arbitrary"}', '[]'])
def test_model_cannot_supply_plugin_confirmation_or_account(arguments):
    core, _ = make_core({"tool_calls": [{"id": "call", "function": {"name": "run_plugin_0", "arguments": arguments}}]})
    with pytest.raises(HarnessError):
        asyncio.run(core.handle_message("请运行统计到货插件", conversation_id="isolated-chat", actor=ACTOR, source="feishu", request_id=str(uuid4())))


def test_clarification_does_not_create_plugin_requests():
    core, _ = make_core({"content": "请指定要执行的插件名称。"})
    result = asyncio.run(core.handle_message("帮我运行一个插件", conversation_id="isolated-chat", actor=ACTOR, source="feishu", request_id=str(uuid4())))
    assert not result["plugin_requests"]
    assert "本次未发起插件执行" in result["reply"]


def test_custom_feishu_preview_is_not_reported_as_formal_completion(monkeypatch):
    from feishu import message_handler
    from unittest.mock import AsyncMock
    target = ConversationPluginTarget(handle="run_plugin_custom", automation_id="custom-selection", generation=1,
        configuration_version=1, contribution_id="feishu-preview", title="自定义候选插件", description="选择候选", effect="write", command="自定义候选")
    dispatch = AsyncMock(return_value={"status": "COMPLETED", "success": True, "selection_preview": {"can_confirm": True}})
    reply = AsyncMock()
    monkeypatch.setattr(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", SimpleNamespace(dispatch=dispatch))
    monkeypatch.setattr(message_handler, "_reply_text", reply)
    assert asyncio.run(message_handler._dispatch_service_v2_feishu_command(text=target.command, receive_id="isolated-chat", conversation_target=target))
    assert dispatch.call_args.kwargs["conversation_target"] is target
    assert "尚未执行正式处理" in reply.call_args.args[1]
    assert reply.call_args.kwargs["reply_type"] == "service_v2_feishu_preview_ready"


@pytest.mark.parametrize("plugin_id,route,selection", [
    ("sync_scan_codes_v2", "builtin.scan_codes", ""),
    ("self_pickup_problem_upload_v2", "builtin.self_pickup_problem_upload", "self_pickup_problem_upload"),
    ("split_pending_problem_upload_v2", "builtin.split_pending_problem_upload", "split_pending_problem_upload"),
])
def test_installed_v2_chat_targets_keep_signed_commands_and_preview_route(monkeypatch, plugin_id, route, selection):
    import json
    from contextlib import nullcontext
    from pathlib import Path
    from agent import plugin_conversations
    from agent.plugin_conversations import PluginConversationService

    manifest = json.loads((Path(__file__).parents[1] / "agent/service_v2_plugins" / plugin_id / "manifest.json").read_text())
    declaration = manifest["contributes"]["feishu"][0]
    contribution = declaration["id"]
    snapshot = SimpleNamespace(generation=2, enabled_entrypoints=[contribution], execution_metadata={"project_config_version":3})
    entry = SimpleNamespace(plugin_id=plugin_id, automation_id="isolated", name=plugin_id, display_name=plugin_id,
        enabled=True, configured=True, committed_snapshot=snapshot, target_generation=2, committed_generation=2,
        reconcile_state="STABLE", runtime_model="SERVICE_V2", contributions=manifest["contributes"], trust_source="ed25519_upload")
    contract = SimpleNamespace(automation_generation=2, project_configuration_version=3, can_full_auto=True,
        invocation_contracts={contribution:SimpleNamespace(entrypoint="feishu", contribution_id=contribution)})
    policy = SimpleNamespace(_plugin_catalog=SimpleNamespace(list=lambda **_: [entry]), _load_contract=lambda _: (entry, contract),
        _repository=SimpleNamespace(unit_of_work=lambda: nullcontext(SimpleNamespace(automation_projects=SimpleNamespace(get_policy=lambda _: {"mode":"PROJECT_FULL_AUTO"})))))
    monkeypatch.setattr(plugin_conversations, "project_capability_from_snapshot", lambda _: {"operation_type":"write"})
    monkeypatch.setattr(plugin_conversations.PluginExecutionRouter, "_service_contribution_capability", lambda capability, **_: capability)
    targets = PluginConversationService(policy).targets(actor=ACTOR, source="feishu")
    assert len(targets) == 1
    assert (targets[0].route_key, targets[0].selection_tool, targets[0].command) == (route, selection, "")
