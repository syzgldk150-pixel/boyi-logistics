"""“一句话改流程”: AI proposes a new flow from the plugin's own blocks; people review and save (AI phase 5).

The model only re-arranges blocks the plugin declares; it never writes code or
saves anything. Its proposal must pass ``shared.plugin_flow.validate_flow`` and
change at most ``MAX_AI_CHANGES`` steps, or the gateway sends it back with the
error. When the request needs a block the plugin does not have, the model
answers ``out_of_scope`` with a development requirement instead of forcing a
flow. Saving goes through the normal plugin-settings path, which validates the
flow again. A dry run replays recent history through the flow's conditions
without executing any block.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Sequence

from agent.ai_tasks import AITaskGateway
from shared.ai_tasks import AITaskError, AITaskSpec
from shared.plugin_flow import (
    MAX_AI_CHANGES,
    FlowError,
    count_changes,
    describe_condition,
    flow_declaration,
    simulate,
    validate_flow,
)

TASK_ID = "plugin.edit_flow"
PROMPT_VERSION = "1"
_PROMPT = (
    "你负责按用户的一句话调整一个自动化插件的处理流程。流程只能由 blocks 里声明的积木组成，"
    "每一步写成 {\"block\": 积木 id, \"params\": 参数(可省略), \"when\": 条件(只有 condition 为 true 的积木需要)}。"
    "规则：required 的积木不能删；locked 的积木保持原有先后顺序；requires_before 中的积木必须排在前面；"
    "effect 为 external_write 的积木必须是最后一步；条件只能用 fields 中、且前面步骤已经取得（provides_fields）的字段，"
    "写法为 {\"field\": 字段, \"op\": eq/ne/in/not_in/gte/lte, \"value\": 值} 或 {\"all\": [...]}/{\"any\": [...]}，取值必须在字段允许范围内；"
    f"相对当前流程最多改动 {MAX_AI_CHANGES} 个步骤，尽量少改。"
    "如果用户的要求需要插件没有的积木或字段，不要硬凑，status 返回 out_of_scope，并在 missing_capability 写明需要新增什么积木、做什么。"
    "只返回 JSON：{\"status\": \"ok\" 或 \"out_of_scope\", \"flow\": {\"steps\": [...]} 或 null, "
    "\"changes\": [不超过 6 条、每条不超过 40 字的改动说明], \"explanation\": 不超过 150 字的说明, \"missing_capability\": 文字或 null}。"
)


class PluginFlowEditorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _strings(value: Any, *, maximum_items: int, maximum_chars: int, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{label} must list at most {maximum_items} items")
    if any(not isinstance(item, str) or not item.strip() or len(item.strip()) > maximum_chars for item in value):
        raise ValueError(f"each {label} item must be 1 to {maximum_chars} characters")
    return [item.strip() for item in value]


def describe_flow(flow: Mapping[str, Any], declaration: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Plain-language steps for people: title, effect, protection, parameters and condition."""
    blocks = {block["id"]: block for block in declaration["blocks"]}
    described = []
    for step in flow["steps"]:
        block = blocks[step["block"]]
        described.append({
            "block": block["id"], "title": block["title"], "description": block["description"],
            "effect": block["effect"], "locked": block["locked"], "required": block["required"],
            "params": step.get("params") or {},
            "condition": describe_condition(step["when"], declaration["fields"]) if "when" in step else "",
        })
    return described


class PluginFlowEditor:
    def __init__(self, gateway: AITaskGateway, *, sample_providers: Mapping[str, Callable[[], Sequence[Mapping[str, Any]]]]):
        self._gateway = gateway
        self._samples = dict(sample_providers)

    @staticmethod
    def _declaration(entry: Any) -> dict[str, Any]:
        try:
            declaration = flow_declaration(entry.config_schema)
        except FlowError as exc:
            raise PluginFlowEditorError("FLOW_DECLARATION_INVALID", str(exc)) from exc
        if declaration is None:
            raise PluginFlowEditorError("FLOW_NOT_SUPPORTED", "这个插件还没有声明可调整的流程。")
        return declaration

    def _current(self, entry: Any, declaration: Mapping[str, Any]) -> dict[str, Any]:
        raw = (entry.project_config or {}).get("flow")
        if raw is None:
            return declaration["default"]
        try:
            return validate_flow(raw, declaration)
        except FlowError:
            return declaration["default"]

    def describe(self, entry: Any) -> dict[str, Any]:
        declaration = self._declaration(entry)
        current = self._current(entry, declaration)
        return {"declaration": declaration, "flow": current, "steps": describe_flow(current, declaration),
                "is_default": (entry.project_config or {}).get("flow") is None,
                "config_version": int(entry.project_config_version), "dry_run_available": entry.plugin_id in self._samples}

    async def propose(self, entry: Any, instruction: str, *, actor_id: str) -> dict[str, Any]:
        text = str(instruction or "").strip()
        if not text or len(text) > 300:
            raise PluginFlowEditorError("FLOW_INSTRUCTION_INVALID", "请用 1 到 300 字描述想怎么调整流程。")
        declaration = self._declaration(entry)
        current = self._current(entry, declaration)

        def validate(answer: Any) -> dict[str, Any]:
            if not isinstance(answer, Mapping) or answer.get("status") not in ("ok", "out_of_scope"):
                raise ValueError("status must be ok or out_of_scope")
            explanation = answer.get("explanation")
            if not isinstance(explanation, str) or not explanation.strip() or len(explanation.strip()) > 300:
                raise ValueError("explanation must be 1 to 300 characters")
            if answer["status"] == "out_of_scope":
                missing = answer.get("missing_capability")
                if not isinstance(missing, str) or not missing.strip() or len(missing.strip()) > 300:
                    raise ValueError("missing_capability must describe the block that is missing")
                return {"status": "out_of_scope", "flow": None, "changes": [], "explanation": explanation.strip(),
                        "missing_capability": missing.strip()}
            try:
                flow = validate_flow(answer.get("flow"), declaration)
            except FlowError as exc:
                raise ValueError(str(exc)) from exc
            changed = count_changes(current, flow)
            if changed == 0:
                raise ValueError("the proposed flow is identical to the current one")
            if changed > MAX_AI_CHANGES:
                raise ValueError(f"the proposal changes {changed} steps; at most {MAX_AI_CHANGES} are allowed")
            return {"status": "ok", "flow": flow, "explanation": explanation.strip(), "missing_capability": None,
                    "changes": _strings(answer.get("changes", []), maximum_items=6, maximum_chars=80, label="changes")}

        spec = AITaskSpec(task_id=TASK_ID, prompt_version=PROMPT_VERSION, system_prompt=_PROMPT, validate=validate)
        payload = {"instruction": text, "current_flow": current, "fields": declaration["fields"],
                   "blocks": declaration["blocks"]}
        try:
            outcome = await self._gateway.run(spec, payload, subject_ref=f"plugin_flow:{entry.automation_id}",
                                              actor_id=actor_id)
        except AITaskError as exc:
            message = {"AI_UNAVAILABLE": exc.message,
                       "AI_INVALID": f"AI 没能给出符合规则的流程（{exc.detail}），请换个说法或减少一次改动的内容。"}.get(
                exc.code, "AI 暂时无法生成流程，请稍后重试。")
            raise PluginFlowEditorError("FLOW_AI_" + exc.code.removeprefix("AI_"), message) from exc
        result = dict(outcome.result)
        result["run_id"] = outcome.run_id
        if result["flow"] is not None:
            result["steps"] = describe_flow(result["flow"], declaration)
            result["change_count"] = count_changes(current, result["flow"])
        return result

    async def dry_run(self, entry: Any, flow: Any) -> dict[str, Any]:
        declaration = self._declaration(entry)
        try:
            normalized = validate_flow(flow, declaration)
        except FlowError as exc:
            raise PluginFlowEditorError("FLOW_INVALID", str(exc)) from exc
        provider = self._samples.get(entry.plugin_id)
        if provider is None:
            return {"available": False, "message": "这个插件还没有可用于试运行的历史记录。"}
        records = list(await asyncio.to_thread(provider))
        current = self._current(entry, declaration)
        return {"available": True, "proposed": simulate(normalized, records), "current": simulate(current, records),
                "steps": describe_flow(normalized, declaration)}


__all__ = ["PluginFlowEditor", "PluginFlowEditorError", "describe_flow"]
