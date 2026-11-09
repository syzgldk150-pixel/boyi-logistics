"""AI phase 5: plugin flow rules, the pilot declaration, the AI flow editor and the package interpreter."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.ai_tasks import AITaskGateway
from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.manifest_v2 import parse_manifest_v2
from agent.plugin_flow_editor import PluginFlowEditor, PluginFlowEditorError
from shared.plugin_flow import (
    FlowError, count_changes, describe_condition, evaluate_condition, flow_declaration, simulate, validate_flow,
    validate_flow_declaration,
)

MANIFEST = Path(__file__).resolve().parents[1] / "agent/service_v2_plugins/customer_problem_auto_reply_v2/manifest.json"
RAW = json.loads(MANIFEST.read_text(encoding="utf-8"))
DECLARATION = flow_declaration(RAW["config_schema"])
DEFAULT = DECLARATION["default"]
YUNDA_ONLY = {"steps": [{"block": "candidates", "params": {"max_items": 5}},
                        {"block": "filter", "when": {"field": "platform", "op": "eq", "value": "yunda"}},
                        {"block": "draft"}, {"block": "reply"}]}


def test_pilot_manifest_declares_valid_blocks_and_parses():
    manifest = parse_manifest_v2(RAW)
    assert [block["id"] for block in flow_declaration(manifest.config_schema)["blocks"]] == ["candidates", "filter", "draft", "reply"]
    assert validate_flow(YUNDA_ONLY, DECLARATION)["steps"][1]["when"]["value"] == "yunda"


@pytest.mark.parametrize(("flow", "message"), [
    ({"steps": [{"block": "candidates"}, {"block": "reply"}]}, "之前必须先经过"),
    ({"steps": [{"block": "candidates"}, {"block": "draft"}]}, "不能删除"),
    ({"steps": [{"block": "candidates"}, {"block": "draft"}, {"block": "reply"},
                {"block": "filter", "when": {"field": "platform", "op": "eq", "value": "yunda"}}]}, "必须是最后一步"),
    ({"steps": [{"block": "candidates"}, {"block": "filter", "when": {"field": "scenario", "op": "eq", "value": "urge"}},
                {"block": "draft"}, {"block": "reply"}]}, "还没有取得"),
    ({"steps": [{"block": "candidates"}, {"block": "filter", "when": {"field": "platform", "op": "eq", "value": "sf"}},
                {"block": "draft"}, {"block": "reply"}]}, "不在可选范围"),
    ({"steps": [{"block": "candidates"}, {"block": "filter", "when": {"field": "platform", "op": "gte", "value": "a"}},
                {"block": "draft"}, {"block": "reply"}]}, "不能比较大小"),
    ({"steps": [{"block": "candidates"}, {"block": "filter"}, {"block": "draft"}, {"block": "reply"}]}, "需要写明条件"),
    ({"steps": [{"block": "candidates", "params": {"max_items": 99}}, {"block": "draft"}, {"block": "reply"}]}, "取值无效"),
    ({"steps": [{"block": "candidates"}, {"block": "send_sms"}, {"block": "draft"}, {"block": "reply"}]}, "没有的积木"),
    ({"steps": [{"block": "candidates"}, {"block": "draft"}, {"block": "draft"}, {"block": "reply"}]}, "只能出现一次"),
    ({"steps": [{"block": "candidates", "code": "print()"}, {"block": "draft"}, {"block": "reply"}]}, "写法无效"),
    ({"steps": [{"block": "candidates"}, {"block": "draft", "when": {"field": "platform", "op": "eq", "value": "yunda"}},
                {"block": "reply"}]}, "不能带条件"),
])
def test_flow_rules_are_enforced_in_code(flow, message):
    with pytest.raises(FlowError, match=message):
        validate_flow(flow, DECLARATION)


def test_conditions_combine_and_evaluate():
    condition = {"all": [{"field": "platform", "op": "in", "value": ["yunda"]},
                         {"field": "confidence", "op": "gte", "value": 0.9}]}
    flow = {"steps": [{"block": "candidates"}, {"block": "draft"},
                      {"block": "filter", "when": condition}, {"block": "reply"}]}
    validate_flow(flow, DECLARATION)
    assert evaluate_condition(condition, {"platform": "yunda", "confidence": 0.95})
    assert not evaluate_condition(condition, {"platform": "yunda", "confidence": 0.8})
    assert not evaluate_condition(condition, {"platform": "yunda"})
    assert describe_condition(condition, DECLARATION["fields"]) == "（平台属于yunda 且 AI 把握不低于0.9）"
    result = simulate(flow, [{"platform": "yunda", "confidence": 0.95}, {"platform": "ronghui", "confidence": 0.99}])
    assert result["reach_end"] == 1 and result["steps"][2]["remaining"] == 1


def test_change_counting():
    assert count_changes(DEFAULT, DEFAULT) == 0
    assert count_changes(DEFAULT, YUNDA_ONLY) == 1


def test_declarations_are_checked_at_install_time():
    for broken in (
        {**DECLARATION, "blocks": [{**DECLARATION["blocks"][3], "requires_before": []}]},
        {**DECLARATION, "blocks": [{**DECLARATION["blocks"][1], "effect": "read"}]},
        {key: value for key, value in DECLARATION.items() if key != "default"},
    ):
        with pytest.raises(FlowError):
            validate_flow_declaration(broken)
    raw = copy.deepcopy(RAW)
    raw["config_schema"]["properties"]["flow"]["x-flow"]["default"] = {"steps": [{"block": "candidates"}]}
    with pytest.raises(PluginManifestError, match="flow"):
        parse_manifest_v2(raw)


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def public_status(self):
        return {"configured": True, "provider": "deepseek", "model": "fake", "config_version_id": 1}

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return {"content": json.dumps(self.answers.pop(0), ensure_ascii=False)}


def _entry(flow=None, plugin_id="customer_problem_auto_reply_v2"):
    return SimpleNamespace(config_schema=RAW["config_schema"], project_config={} if flow is None else {"flow": flow},
                           project_config_version=4, plugin_id=plugin_id, automation_id="auto-1")


def _editor(*answers, samples=None):
    providers = {"customer_problem_auto_reply_v2": lambda: samples or []}
    return PluginFlowEditor(AITaskGateway(FakeLLM(*answers)), sample_providers=providers)


def test_ai_proposal_is_validated_and_never_saved():
    editor = _editor({"status": "ok", "flow": YUNDA_ONLY, "changes": ["只处理韵达"], "explanation": "加一步筛选", "missing_capability": None})
    entry = _entry()
    result = asyncio.run(editor.propose(entry, "只处理韵达的问题件", actor_id="admin"))
    assert result["status"] == "ok" and result["change_count"] == 1 and result["steps"][1]["condition"] == "平台等于yunda"
    assert entry.project_config == {}


def test_invalid_or_too_large_proposals_are_sent_back_then_refused():
    too_many = {"steps": [{"block": "candidates", "params": {"max_items": 3}},
                          {"block": "filter", "when": {"field": "platform", "op": "eq", "value": "yunda"}},
                          {"block": "filter", "when": {"field": "platform", "op": "ne", "value": "ronghui"}},
                          {"block": "draft"},
                          {"block": "filter", "when": {"field": "confidence", "op": "gte", "value": 0.9}},
                          {"block": "reply"}]}
    bad = {"status": "ok", "flow": {"steps": [{"block": "candidates"}, {"block": "reply"}]}, "changes": [], "explanation": "x"}
    editor = _editor({"status": "ok", "flow": too_many, "changes": [], "explanation": "x"}, bad)
    with pytest.raises(PluginFlowEditorError) as refused:
        asyncio.run(editor.propose(_entry(), "改很多", actor_id="admin"))
    assert refused.value.code == "FLOW_AI_INVALID"
    sent = json.dumps(editor._gateway._llm.calls, ensure_ascii=False)
    assert "at most 3" in sent


def test_out_of_scope_requests_become_a_development_requirement():
    editor = _editor({"status": "out_of_scope", "flow": None, "changes": [], "explanation": "没有查监控的积木",
                      "missing_capability": "新增积木：查仓库监控录像"})
    result = asyncio.run(editor.propose(_entry(), "韵达件先查一下仓库监控", actor_id="admin"))
    assert result["status"] == "out_of_scope" and "监控" in result["missing_capability"]


def test_describe_and_dry_run():
    editor = _editor(samples=[{"platform": "yunda", "scenario": "urge", "confidence": 0.9},
                              {"platform": "ronghui", "scenario": "urge", "confidence": 0.9}])
    described = editor.describe(_entry(YUNDA_ONLY))
    assert described["is_default"] is False and described["steps"][1]["condition"] == "平台等于yunda"
    assert editor.describe(_entry())["flow"] == DEFAULT
    result = asyncio.run(editor.dry_run(_entry(), YUNDA_ONLY))
    assert result["proposed"]["reach_end"] == 1 and result["current"]["reach_end"] == 2
    with pytest.raises(PluginFlowEditorError):
        asyncio.run(editor.dry_run(_entry(), {"steps": [{"block": "reply"}]}))
    assert asyncio.run(_editor().dry_run(_entry(plugin_id="other"), YUNDA_ONLY))["available"] is False
    with pytest.raises(PluginFlowEditorError) as unsupported:
        _editor().describe(SimpleNamespace(config_schema={"type": "object", "properties": {}}, project_config={}))
    assert unsupported.value.code == "FLOW_NOT_SUPPORTED"


def test_package_interprets_the_saved_flow():
    from tests.test_customer_auto_reply import _load_action
    action = _load_action()
    calls = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        ref = {"evidence_ref": f"ref-{len(calls)}"}
        if action.endswith("candidates"):
            return {**ref, "items": [{"source_id": "s1", "external_id": "e", "source_direction": "received", "platform": "yunda"},
                                     {"source_id": "s2", "external_id": "e", "source_direction": "received", "platform": "ronghui"}]}
        if action.endswith("draft"):
            return {**ref, "eligible": True, "suggestion_id": "11111111-2222-4333-8444-555555555555",
                    "scenario_id": "urge", "confidence": "0.95"}
        return {**ref, "confirmed": True, "skipped": "", "sent": True}

    result = action.run_action({"flow": YUNDA_ONLY}, broker)
    assert result["data"]["sent"] == 1 and result["data"]["filtered"] == 1
    assert calls.count("customer_auto_reply.draft") == 1
    with pytest.raises(FlowError):
        action.run_action({"flow": {"steps": [{"block": "candidates"}, {"block": "reply"}]}}, broker)
