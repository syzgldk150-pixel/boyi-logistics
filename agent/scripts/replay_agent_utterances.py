"""Replay fixed utterances against the conversation model; select only, never execute.

Agent layer P0 evaluation. Every case is sent with the production system prompt
and the production tool list (fixed read tools plus the fixture's plugin stubs,
under the production visibility rule) and only the model's first tool choice is
recorded. Nothing is invoked: no platform, database or plugin is touched.

Report: action hit rate, argument hit rate, and mis-executions (a writing plugin
chosen where the case expects something else). Mis-executions must stay at 0.

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

from agent.harness_application import build_fixed_harness_tools
from agent.harness_online import _model_tools, build_system_prompt, plugin_tools_visible
from agent.shipment_conversation import ShipmentConversation

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "agent_replay" / "agent_utterances.json"
WRITE_EFFECTS = frozenset({"internal_write", "external_write", "destructive"})
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


def build_turn(text: str, plugins: Sequence[Mapping[str, Any]], *, with_parameters: bool
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    descriptors = [tool.descriptor for tool in build_fixed_harness_tools()]
    tools, name_to_id = _model_tools(descriptors)
    visible = plugin_tools_visible(text)
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
          *, with_parameters: bool = False) -> dict[str, Any]:
    effects = {f"plugin:{item['key']}": item.get("effect", "read") for item in plugins}
    rows, action_hits, argument_cases, argument_hits, misexecutions = [], 0, 0, 0, 0
    for case in cases:
        messages, tools, name_to_id = build_turn(case["text"], plugins, with_parameters=with_parameters)
        response = model(messages, tools)
        selected, arguments = [], {}
        for call in response.get("tool_calls") or ():
            function = call.get("function") or {}
            selected.append(name_to_id.get(str(function.get("name")), f"unknown:{function.get('name')}"))
            try:
                parsed = json.loads(function.get("arguments") or "{}")
            except ValueError:
                parsed = {}
            if not arguments and isinstance(parsed, dict):
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
        wrong_write = [action for action in selected
                       if effects.get(action) in WRITE_EFFECTS and action not in expected_actions]
        misexecutions += bool(wrong_write)
        rows.append({"id": case["id"], "expected": expected["action"], "selected": "+".join(sorted(set(selected))) or "none",
                     "arguments": arguments, "action_hit": action_hit, "argument_hit": argument_hit,
                     "misexecution": bool(wrong_write)})
    total = len(cases) or 1
    return {
        "cases": len(cases),
        "action_hit_rate": round(action_hits / total, 4),
        "argument_hit_rate": round(argument_hits / argument_cases, 4) if argument_cases else None,
        "misexecutions": misexecutions,
        "rows": rows,
    }


def offline_model(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Mapping[str, Any]:
    """Deterministic keyword stand-in; proves the pipeline, says nothing about the real model."""
    import re
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
    parser.add_argument("--with-parameters", action="store_true",
                        help="offer plugins that take this turn's input (P1 behaviour)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not (args.offline or args.active or (args.provider and args.model)):
        parser.error("use --offline, --active, or --provider with --model")
    fixture = json.loads(args.cases.read_text(encoding="utf-8"))
    model = (offline_model if args.offline else active_model() if args.active
             else live_model(args.provider, args.model))
    report = score(fixture["cases"], fixture["plugins"], model, with_parameters=args.with_parameters)
    report["mode"] = "offline" if args.offline else "active" if args.active else f"{args.provider}:{args.model}"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    summary = {key: report[key] for key in ("mode", "cases", "action_hit_rate", "argument_hit_rate", "misexecutions")}
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if report["misexecutions"] else 0


if __name__ == "__main__":
    sys.exit(main())
