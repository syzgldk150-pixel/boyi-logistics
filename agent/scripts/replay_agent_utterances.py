"""Replay fixed utterances against the conversation model; select only, never execute.

Agent layer P0 evaluation. Every case is sent with the production system prompt
and the production tool list (fixed read tools plus the fixture's plugin stubs,
under the production visibility rule) and only the model's first tool choice is
recorded. Nothing is invoked: no platform, database or plugin is touched.

Every plugin choice also passes the real action gateway (without identity), so
the report separates a wrong write the model *chose* (it would only be asked
"确认") from a mis-execution (a wrong write the gateway would let run). The
latter must stay at 0. ``ask_user`` counts as asking back (expected "none").
``--baseline-p0`` reproduces the P0 rules (verb-gated plugin tools, no per-call
inputs, no ask-back tool) for before/after comparison.

    PYTHONPATH=agent:. python agent/scripts/replay_agent_utterances.py --offline
    PYTHONPATH=agent:. python agent/scripts/replay_agent_utterances.py --active   # on the Agent host
    PYTHONPATH=agent:. DEEPSEEK_API_KEY=... python agent/scripts/replay_agent_utterances.py \\
        --provider deepseek --model deepseek-chat --output report.json

``--offline`` uses a deterministic keyword model and only proves the pipeline.
The API key is read from the provider's environment variable and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import re

from agent.agent_actions.gateway import ActionGateway
from agent.agent_actions.spec import ActionSpec
from agent.harness_application import build_fixed_harness_tools
from agent.harness_online import _ASK_USER_TOOL, ASK_USER, _model_tools, build_system_prompt, plugin_tools_visible
from agent.shipment_conversation import ShipmentConversation

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "agent_replay" / "agent_utterances.json"
# The P0 visibility rule, kept here only to reproduce the baseline.
_P0_READ_OR_NEGATED = re.compile(r"查询|查一下|查数据|查扫描|多少|几票|几件|几吨|吨位|扫描量|规则|依据|为什么|怎么定义|不要|不用|别(?:给我|帮我)?(?:扫|执行|运行|上传)|昨天呢|按目的地")
_P0_EXPLICIT_ACTION = re.compile(r"执行|运行|启动|同步|上传|扫描|处理|跑起来|跑一下")
ModelCall = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Mapping[str, Any]]


def plugin_tool(plugin: Mapping[str, Any], *, with_parameters: bool) -> dict[str, Any] | None:
    """Same shape as ConversationPluginTarget.model_tool; parameterized plugins only from P1."""
    parameters = plugin.get("parameters")
    if parameters and not with_parameters:
        return None  # production skips plugins that need this turn's input
    return {"type": "function", "function": {
        "name": f"run_plugin_{plugin['key']}",
        "description": f"执行插件：{plugin['title']}。业务用途：{plugin['description']} 当前调用权限：此工具已对当前管理员开放，"
                       "发起时由宿主再次复核；使用该实例当前已保存设置，仅在用户明确要求执行时调用。预览及正式确认以宿主实际返回为准。",
        "parameters": parameters or {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    }}


def build_turn(text: str, plugins: Sequence[Mapping[str, Any]], *, baseline_p0: bool = False
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    descriptors = [tool.descriptor for tool in build_fixed_harness_tools()]
    tools, name_to_id = _model_tools(descriptors)
    with_parameters = not baseline_p0
    if baseline_p0:
        visible = not _P0_READ_OR_NEGATED.search(text) and bool(_P0_EXPLICIT_ACTION.search(text))
    else:
        visible = plugin_tools_visible(text)
        tools.append(_ASK_USER_TOOL)
        name_to_id[ASK_USER] = ASK_USER
    if visible:
        for plugin in plugins:
            tool = plugin_tool(plugin, with_parameters=with_parameters)
            if tool is not None:
                tools.append(tool)
                name_to_id[tool["function"]["name"]] = f"plugin:{plugin['key']}"
    system = build_system_prompt(clock_hint=ShipmentConversation().clock_hint(), query_context=None, with_plugins=visible)
    return [{"role": "system", "content": system}, {"role": "user", "content": text}], tools, name_to_id


def _matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    for key, value in expected.items():
        got = actual.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if float(got) != float(value):
                    return False
            except (TypeError, ValueError):
                return False
        elif str(got or "").strip() != str(value):
            return False
    return True


def score(cases: Sequence[Mapping[str, Any]], plugins: Sequence[Mapping[str, Any]], model: ModelCall,
          *, baseline_p0: bool = False) -> dict[str, Any]:
    specs = {f"plugin:{item['key']}": ActionSpec(
        action_id=f"plugin:{item['key']}", title=item["title"], effect=item.get("effect", "read"),
        capability="plugins.execute", input_schema=item.get("parameters"), preview=bool(item.get("preview")))
        for item in plugins}
    gateway = ActionGateway()
    rows, action_hits, argument_cases, argument_hits, misexecutions, wrong_writes = [], 0, 0, 0, 0, 0
    for case in cases:
        messages, tools, name_to_id = build_turn(case["text"], plugins, baseline_p0=baseline_p0)
        response = model(messages, tools)
        selected, arguments, per_call = [], {}, []
        for call in response.get("tool_calls") or ():
            function = call.get("function") or {}
            action = name_to_id.get(str(function.get("name")), f"unknown:{function.get('name')}")
            try:
                parsed = json.loads(function.get("arguments") or "{}")
            except ValueError:
                parsed = {}
            parsed = parsed if isinstance(parsed, dict) else {}
            if action != ASK_USER:
                selected.append(action)
                per_call.append((action, parsed))
            if not arguments and action != ASK_USER:
                arguments = parsed
        expected = case["expect"]
        expected_actions = sorted(expected["action"].split("+")) if expected["action"] != "none" else []
        action_hit = sorted(set(selected)) == expected_actions
        action_hits += action_hit
        argument_hit = None
        if expected.get("arguments"):
            argument_cases += 1
            argument_hit = action_hit and _matches(expected["arguments"], arguments)
            argument_hits += bool(argument_hit)
        wrong = [(action, parsed) for action, parsed in per_call
                 if action in specs and specs[action].writes and action not in expected_actions]
        decisions = {action: (gateway.decide(specs[action], actor=None, arguments=parsed, message=case["text"]).decision
                              if not baseline_p0 else "EXECUTE") for action, parsed in wrong}
        # A preview action's first call only lists candidates; the person confirms the real run.
        runs = [action for action, decision in decisions.items() if decision == "EXECUTE" and not specs[action].preview]
        wrong_writes += bool(wrong)
        misexecutions += bool(runs)
        rows.append({"id": case["id"], "expected": expected["action"], "selected": "+".join(sorted(set(selected))) or "none",
                     "asked_back": any(name_to_id.get(str((c.get("function") or {}).get("name"))) == ASK_USER
                                       for c in response.get("tool_calls") or ()),
                     "arguments": arguments, "action_hit": action_hit, "argument_hit": argument_hit,
                     "wrong_write_chosen": bool(wrong), "gateway": decisions, "misexecution": bool(runs)})
    total = len(cases) or 1
    return {
        "cases": len(cases),
        "action_hit_rate": round(action_hits / total, 4),
        "argument_hit_rate": round(argument_hits / argument_cases, 4) if argument_cases else None,
        "wrong_writes_chosen": wrong_writes,
        "misexecutions": misexecutions,
        "rows": rows,
    }


def offline_model(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Mapping[str, Any]:
    """Deterministic keyword stand-in; proves the pipeline, says nothing about the real model."""
    text = messages[-1]["content"]
    names = {tool["function"]["name"] for tool in tools}
    number = re.search(r"R\d{11}", text)
    def call(name: str, arguments: Mapping[str, Any]):
        return {"tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
    if number and "物流" in text or number and "到哪" in text:
        return call("query_tracking", {"tracking_number": number.group(0)})
    if number:
        return call("query_waybill", {"waybill_number": number.group(0)})
    if "run_plugin_scan" in names and "扫描" in text and not re.search(r"怎么|为什么|多少|不要", text):
        return call("run_plugin_scan", {})
    return {"content": "请再说明一下。"}


def live_model(provider_name: str, model_id: str) -> ModelCall:
    from openai import OpenAI
    from agent.llm_settings import PROVIDERS

    provider = PROVIDERS[provider_name]
    api_key = str(os.environ.get(str(provider["env_key"])) or "").strip()
    if not api_key:
        raise SystemExit(f"{provider['env_key']} is not configured")
    client = OpenAI(api_key=api_key, base_url=str(provider["base_url"]), timeout=float(provider["timeout"]))

    def call(messages, tools):
        message = client.chat.completions.create(model=model_id, messages=messages, tools=tools,
                                                 tool_choice="auto", temperature=0).choices[0].message
        return {"content": message.content or "", "tool_calls": [
            {"id": item.id, "type": "function",
             "function": {"name": item.function.name, "arguments": item.function.arguments}}
            for item in (message.tool_calls or ())]}
    return call


def active_model() -> ModelCall:
    """The Agent's own activated model (run on the Agent host, read-only use of its settings)."""
    import asyncio
    from agent.llm_client import LLMClient
    from agent.llm_settings import LLMSettingsRepository
    from agent.memory import Memory
    from agent.runtime_config import load_agent_environment

    load_agent_environment()
    client = LLMClient()
    memory = Memory()
    memory.init()
    asyncio.run(client.bind_repository(LLMSettingsRepository(memory.connection_factory)))
    if not client.public_status().get("configured"):
        raise SystemExit("no active model")

    def call(messages, tools):
        return asyncio.run(client.chat(messages, tools=tools))
    return call


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--active", action="store_true", help="use the Agent's activated model (Agent host only)")
    parser.add_argument("--provider", choices=("deepseek", "glm"))
    parser.add_argument("--model")
    parser.add_argument("--baseline-p0", action="store_true",
                        help="reproduce the P0 rules for a before/after comparison")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not (args.offline or args.active or (args.provider and args.model)):
        parser.error("use --offline, --active, or --provider with --model")
    fixture = json.loads(args.cases.read_text(encoding="utf-8"))
    model = (offline_model if args.offline else active_model() if args.active
             else live_model(args.provider, args.model))
    report = score(fixture["cases"], fixture["plugins"], model, baseline_p0=args.baseline_p0)
    report["rules"] = "p0" if args.baseline_p0 else "current"
    report["mode"] = "offline" if args.offline else "active" if args.active else f"{args.provider}:{args.model}"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    summary = {key: report[key] for key in ("mode", "rules", "cases", "action_hit_rate", "argument_hit_rate",
                                            "wrong_writes_chosen", "misexecutions")}
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if report["misexecutions"] else 0


if __name__ == "__main__":
    sys.exit(main())
