from __future__ import annotations

import asyncio
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.direct_tool_router import (
    QUOTE_PLUGIN_COMMAND, direct_tool_request_from_text,
    format_quote_plugin_messages, is_reserved_feishu_command_text,
)
from agent.orchestration.automation_project_entrypoints import ServiceV2FeishuDispatcher, TrustedDynamicArgumentResolver
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.orchestration.automation_project_policy_service import AutomationProjectPolicyService
from agent.tool_registry import validate_schema_instance
from shared.automation_project_authorization import OMIT_DYNAMIC_ARGUMENT


ROOT = Path(__file__).resolve().parents[1]
INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"address": {"type": "string", "minLength": 1},
                   "weight": {"type": "number", "minimum": 0.000001},
                   "volume": {"type": "number", "minimum": 0.000001}},
    "required": ["address", "weight"],
}
INPUT = {"address": "示例市示例区测试路1号", "weight": 1000, "volume": 2.5}


def _manifest():
    # Use the canonical first-party shape, with no real account or network dependencies.
    value = json.loads((ROOT / "agent/service_v2_plugins/r7_vehicle_checkin_v2/manifest.json").read_text())
    value.update(plugin_id="quote_fixture", name="报价合同测试", capabilities=[], account_roles=[])
    value.pop("http_requests", None)
    value["provides"] = [{"service": "plugin.quote_fixture.quote@1", "operations": [{"name": "quote", "effect": "read"}]}]
    value["contributes"] = {kind: [] for kind in ("console", "feishu", "scheduler", "webhook", "events", "harness")}
    for kind in ("console", "feishu"):
        item = {"id": kind, "service": "plugin.quote_fixture.quote@1", "operation": "quote",
                "default_enabled": True, "input_schema": copy.deepcopy(INPUT_SCHEMA)}
        item.update({"title": "查询报价"} if kind == "console" else {"commands": [QUOTE_PLUGIN_COMMAND]})
        value["contributes"][kind] = [item]
    return value


def test_runtime_inputs_compile_separately_from_saved_settings():
    manifest = AutomationPluginManifestV2.from_mapping(_manifest())
    contract = ServiceV2ProjectContract.from_manifest(manifest)
    assert dict(manifest.config_schema["properties"]) == {}
    for channel in ("console", "feishu"):
        invocation = contract.invocation_contracts[channel]
        assert invocation["argument_template"] == {}
        assert invocation["dynamic_resolvers"] == {
            "address": f"verified_{channel}_address", "weight": f"verified_{channel}_weight",
            "volume": f"verified_optional_{channel}_volume",
        }
        validate_schema_instance("quote", INPUT, invocation["input_schema"])
        validate_schema_instance("quote", {"address": INPUT["address"], "weight": 1000}, invocation["input_schema"])
        with pytest.raises(ValueError):
            validate_schema_instance("quote", {"address": INPUT["address"]}, invocation["input_schema"])
        resolver = TrustedDynamicArgumentResolver()
        assert resolver(f"verified_optional_{channel}_volume", "volume", {"entrypoint": channel, "dynamic_inputs": INPUT}) == 2.5
        assert resolver(f"verified_optional_{channel}_volume", "volume", {
            "entrypoint": channel, "dynamic_inputs": {"address": INPUT["address"], "weight": 1000},
        }) is OMIT_DYNAMIC_ARGUMENT
    validate_schema_instance("quote", INPUT, contract.tool_contract["input_schema"])


def test_manifest_editor_schema_accepts_declared_runtime_inputs():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "agent/extension_sdk/schemas/manifest-v2.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(_manifest())


@pytest.mark.parametrize("field", ["account_id", "resource_id", "service", "automation_id"])
def test_runtime_input_schema_cannot_declare_execution_identity(field):
    value = _manifest()
    value["contributes"]["feishu"][0]["input_schema"]["properties"][field] = {"type": "string"}
    with pytest.raises(PluginManifestError):
        AutomationPluginManifestV2.from_mapping(value)


def test_runtime_inputs_cannot_replace_saved_configuration():
    value = _manifest()
    value["config_schema"]["properties"]["weight"] = {"type": "number"}
    with pytest.raises(PluginManifestError, match="saved configuration"):
        ServiceV2ProjectContract.from_manifest(AutomationPluginManifestV2.from_mapping(value))


class _Policy:
    def __init__(self):
        self.calls = []

    def _load_contract(self, automation_id):
        assert automation_id == "quote-instance"
        entry = SimpleNamespace(contributions={"feishu": [{"id": "command", "input_schema": INPUT_SCHEMA}]})
        contract = SimpleNamespace(automation_generation=2, invocation_contracts={"command": SimpleNamespace(
            dynamic_argument_resolvers={name: f"verified_feishu_{name}" for name in INPUT})})
        return entry, contract

    async def invoke_trusted_and_wait(self, automation_id, **kwargs):
        self.calls.append((automation_id, kwargs))
        return {"success": True, "status": "COMPLETED"}


def _dispatch(inputs):
    policy = _Policy()
    dispatcher = ServiceV2FeishuDispatcher(policy_service=policy,
        contribution_registry=SimpleNamespace(resolve_active_feishu_command=lambda command: SimpleNamespace(
            automation_id="quote-instance", generation=2, contribution_id="command")),
        resolve_actor=lambda sender: Actor(ActorType.FEISHU_USER, sender, authenticated_by="feishu_verified_event"))
    return policy, dispatcher.dispatch(command_text=QUOTE_PLUGIN_COMMAND, event_id="event-one", sender_id="sender-one",
                                      chat_id="chat-one", runtime_inputs=inputs)


@pytest.mark.parametrize("inputs", [INPUT, {"address": INPUT["address"], "weight": 1000}])
def test_dispatch_keeps_inputs_and_project_identity_in_separate_channels(inputs):
    policy, pending = _dispatch(inputs)
    asyncio.run(pending)
    automation_id, kwargs = policy.calls[0]
    assert automation_id == "quote-instance"
    assert kwargs["trusted_context"] == {"event_id": "event-one", "chat_id": "chat-one", "dynamic_inputs": inputs}
    assert kwargs["idempotency_key"] == "feishu:event-one"
    assert kwargs["expected_automation_generation"] == 2


@pytest.mark.parametrize("inputs", [
    {"address": INPUT["address"]},
    {**INPUT, "volume": 0}, {**INPUT, "weight": float("inf")},
    {**INPUT, "account_id": "forbidden"}, {**INPUT, "automation_id": "another-instance"},
])
def test_dispatch_rejects_incomplete_values_and_identity_overrides(inputs):
    policy, pending = _dispatch(inputs)
    with pytest.raises(OrchestrationError):
        asyncio.run(pending)
    assert policy.calls == []


def test_console_input_adapter_passes_only_trusted_dynamic_context():
    calls = []
    service = SimpleNamespace(invoke_trusted=lambda *args, **kwargs: calls.append((args, kwargs)))
    actor = Actor(ActorType.CONSOLE_ADMIN, "admin-one", roles=("admin",), authenticated_by="mysql_admin_session")
    AutomationProjectPolicyService.invoke_console(service, "quote-instance", request_id="request-one", actor=actor,
                                                 contribution_id="quote", runtime_inputs=INPUT)
    assert calls[0][1]["trusted_context"] == {"dynamic_inputs": INPUT}


def test_quote_text_routes_only_to_plugin_and_converts_dimension_expression():
    request = direct_tool_request_from_text("报价：示例市测试路1号，1000公斤，100*100*100*2")
    assert request["mode"] == "service_v2_quote"
    assert request["tool_name"] == "quote_prices_v2"
    assert request["runtime_inputs"] == {"address": "示例市测试路1号", "weight": 1000, "volume": 2}
    assert request["params"] == {}
    assert not is_reserved_feishu_command_text(QUOTE_PLUGIN_COMMAND)


@pytest.mark.parametrize("text", ["报价", QUOTE_PLUGIN_COMMAND, "示例市测试路1号，1000，0", "报价：示例市测试路1号，inf，2"])
def test_incomplete_quote_is_clarified_without_running_a_tool(text):
    request = direct_tool_request_from_text(text)
    assert request["mode"] == "service_v2_quote"
    assert "runtime_inputs" not in request
    assert "重量" in request["local_result"]["error"]
    assert "体积" in request["local_result"]["error"]


def test_three_provider_results_show_actual_quotes_and_individual_failures():
    result = {"success": True, "result": {"data": {
        "ronghui": {"status": "success", "quotes": [{"label": "产品甲自提", "amount": "12.50"}]},
        "yunda": {"status": "failed", "quotes": [], "error": "登录已失效"},
        "best": {"status": "success", "quotes": [{"label": "派送", "amount": "18.00"}], "warnings": ["特殊区域另计"]},
    }}}
    assert format_quote_plugin_messages(result) == ["融辉价格\n产品甲自提：12.50元", "韵达价格\n报价失败：登录已失效", "百世价格\n派送：18.00元\n提醒：特殊区域另计"]


def test_failed_invocation_preserves_three_provider_failure_reasons():
    result = {"success": False, "status": "FAILED_TERMINAL", "result": {
        "status": "FAILED", "error": {"code": "QUOTE_UNAVAILABLE", "message": "三个平台均无报价"},
        "data": {key: {"status": "failed", "quotes": [], "error": f"{key}未登录"}
                 for key in ("ronghui", "yunda", "best")},
    }}
    messages = format_quote_plugin_messages(result)
    assert len(messages) == 3
    assert all("报价失败" in message and "未登录" in message for message in messages)


def _replay_policy(*, runtime_schema=INPUT_SCHEMA, arguments=None, preview_invocation_id=None):
    row = {
        "invocation_id": "11111111-1111-4111-8111-111111111111", "request_id": "request-one",
        "automation_id": "quote-instance", "generation": 4, "status": "COMPLETED",
        "arguments_json": dict(INPUT if arguments is None else arguments),
        "invocation_json": {"contract_id": "quote"}, "preview_invocation_id": preview_invocation_id,
        "result_json": {"status": "SUCCESS", "data": {}},
    }
    declaration = {"id": "quote"}
    if runtime_schema is not None:
        declaration["input_schema"] = copy.deepcopy(runtime_schema)
    reads = []

    def historical_generation(automation_id, generation, *, include_execution_details):
        reads.append((automation_id, generation, include_execution_details))
        return {"runtime_model": "SERVICE_V2", "snapshot_json": {"execution_metadata": {
            "contributions": {"console": [declaration]},
        }}}

    @contextmanager
    def unit_of_work():
        yield SimpleNamespace(automation_plugins=SimpleNamespace(get_generation_row=historical_generation))

    service = AutomationProjectPolicyService(SimpleNamespace(unit_of_work=unit_of_work), None, None,
        direct_invocations=SimpleNamespace(repository=SimpleNamespace(by_request=lambda _key: row)))

    def forbid_current_contract(_identity):
        raise AssertionError("Replay must not infer input fields from the current configuration")

    service._load_contract = forbid_current_contract
    actor = Actor(ActorType.CONSOLE_ADMIN, "admin-one", roles=("admin",), authenticated_by="mysql_admin_session")
    return service, actor, reads


@pytest.mark.parametrize("inputs", [
    None, {}, {"address": INPUT["address"], "weight": INPUT["weight"]},
    {**INPUT, "extra": None}, {**INPUT, "weight": 1001}, {**INPUT, "volume": "2.5"},
])
def test_console_replay_rejects_omitted_added_or_changed_runtime_inputs(inputs):
    service, actor, reads = _replay_policy()
    kwargs = {"runtime_inputs": inputs} if inputs is not None else {}
    with pytest.raises(OrchestrationError) as rejected:
        service.invoke_console("quote-instance", request_id="request-one", actor=actor, contribution_id="quote", **kwargs)
    assert rejected.value.code == "IDEMPOTENCY_CONFLICT"
    assert reads == [("quote-instance", 4, False)]


def test_console_replay_accepts_the_same_complete_inputs_from_the_original_generation():
    service, actor, reads = _replay_policy(arguments={**INPUT, "saved_currency": "CNY"})
    result = service.invoke_console("quote-instance", request_id="request-one", actor=actor,
                                    contribution_id="quote", runtime_inputs=dict(INPUT))
    assert result["success"] is True
    assert result["invocation_id"] == "11111111-1111-4111-8111-111111111111"
    assert reads == [("quote-instance", 4, False)]


@pytest.mark.parametrize("preview", [None, "22222222-2222-4222-8222-222222222222"])
def test_static_and_preview_replays_do_not_require_runtime_input_envelopes(preview):
    service, actor, reads = _replay_policy(runtime_schema=None, arguments={"dry_run": preview is None},
                                          preview_invocation_id=preview)
    result = service.invoke_console("quote-instance", request_id="request-one", actor=actor,
                                    contribution_id="quote", preview_invocation_id=preview)
    assert result["success"] is True
    assert reads == [("quote-instance", 4, False)]


def test_console_replay_preserves_omitted_volume_from_original_request():
    inputs = {"address": INPUT["address"], "weight": 1000}
    service, actor, _ = _replay_policy(arguments=inputs)
    result = service.invoke_console("quote-instance", request_id="request-one", actor=actor,
                                    contribution_id="quote", runtime_inputs=inputs)
    assert result["success"] is True
    with pytest.raises(OrchestrationError) as rejected:
        service.invoke_console("quote-instance", request_id="request-one", actor=actor,
                               contribution_id="quote", runtime_inputs={**inputs, "volume": 0.1})
    assert rejected.value.code == "IDEMPOTENCY_CONFLICT"


def test_site_details_render_for_success_and_restricted_platform_without_losing_default_note():
    details = ["匹配网点：测试网点", "网点地址：测试路1号", "联系电话：无数据", "特殊区域：测试园区需人工核实"]
    result = {"result": {"data": {
        "request": {"volume": "0.1", "volume_defaulted": True},
        "ronghui": {"status": "success", "quotes": [{"label": "测试派送", "amount": "12.50"}],
                    "details": details, "warnings": ["测试提醒"]},
        "yunda": {"status": "failed", "error": "特殊区域暂不能自动报价", "details": details},
        "best": {"status": "success", "quotes": [{"label": "测试自提", "amount": "10.00"}],
                 "details": [None, {}, "", *details]},
    }}}
    messages = format_quote_plugin_messages(result)
    for message in messages:
        assert "\n".join(details) in message
    assert "体积未填写，本次按0.1立方米计算。" in messages[0]
    assert "测试派送：12.50元" in messages[0]
    assert messages[0].endswith("提醒：测试提醒")
    assert "报价失败：特殊区域暂不能自动报价" in messages[1]
    assert "None" not in messages[2]
