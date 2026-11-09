"""Plugin flows: code-owned blocks a person (or AI on their behalf) may re-arrange (AI phase 5).

A plugin declares its blocks in ``config_schema.properties.flow["x-flow"]``;
an instance's flow lives in its normal configuration under ``flow`` and so
reuses the configuration CAS, versions and rollback. The Host validates every
flow statically before it is saved; the rules here are enforced in code, not
in a prompt:

* only declared blocks, each at most once unless ``repeatable``;
* ``required`` blocks may not be removed; ``locked`` blocks keep their
  declared relative order;
* a block runs only after everything in its ``requires_before``;
* an ``external_write`` block must be last and after its guards;
* conditions use a closed grammar (``all``/``any`` of field comparisons) over
  fields that an earlier step provides, with values checked against the field;
* params are checked against the block's closed parameter schema;
* at most ``MAX_STEPS`` steps, and an AI edit may change at most ``MAX_AI_CHANGES``.

Pure functions; no environment, database or network access.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Mapping, Sequence

FLOW_KEYWORD = "x-flow"
MAX_STEPS = 10
MAX_AI_CHANGES = 3
EFFECTS = ("read", "compute", "internal_write", "external_write")
OPERATORS = ("eq", "ne", "in", "not_in", "gte", "lte")
_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PARAM_TYPES = ("integer", "number", "string", "boolean")


class FlowError(ValueError):
    """A declaration or flow broke a rule; the message is shown to people as is."""


def _thaw(value: Any) -> Any:
    """Plain dicts and lists from frozen manifest values (read-only mappings, tuples)."""
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise FlowError(f"{label}必须是 1 到 {maximum} 个字的文字")
    return value.strip()


def _param_schema(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {"type": "object", "additionalProperties": False, "properties": {}, "required": []}
    if (not isinstance(value, Mapping) or value.get("type") != "object" or value.get("additionalProperties") is not False
            or not isinstance(value.get("properties"), Mapping)):
        raise FlowError(f"{label}的参数定义必须是封闭对象")
    for name, spec in value["properties"].items():
        if not _ID.fullmatch(str(name)) or not isinstance(spec, Mapping) or spec.get("type") not in _PARAM_TYPES:
            raise FlowError(f"{label}的参数 {name} 定义无效")
    required = value.get("required", [])
    if not isinstance(required, list) or not set(required) <= set(value["properties"]):
        raise FlowError(f"{label}的必填参数无效")
    return copy.deepcopy(dict(value))


def _field_schema(name: str, value: Any) -> dict[str, Any]:
    if not _ID.fullmatch(name) or not isinstance(value, Mapping) or value.get("type") not in ("string", "number", "integer"):
        raise FlowError(f"字段 {name} 定义无效")
    _text(value.get("title"), f"字段 {name} 的名称", 20)
    if "enum" in value and (not isinstance(value["enum"], list) or not value["enum"]):
        raise FlowError(f"字段 {name} 的可选值无效")
    return copy.deepcopy(dict(value))


def validate_flow_declaration(raw: Any) -> dict[str, Any]:
    """Check a plugin's block declaration (done when the package is parsed)."""
    raw = _thaw(raw)
    if not isinstance(raw, Mapping) or set(raw) - {"fields", "blocks", "default"}:
        raise FlowError("流程声明只能包含 fields、blocks、default")
    fields = raw.get("fields") or {}
    if not isinstance(fields, Mapping):
        raise FlowError("流程字段必须是对象")
    normalized_fields = {str(name): _field_schema(str(name), spec) for name, spec in fields.items()}
    blocks = raw.get("blocks")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 20:
        raise FlowError("流程必须声明 1 到 20 个积木")
    normalized_blocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_keys = {"id", "title", "description", "effect", "required", "locked", "repeatable", "condition",
                    "params", "provides_fields", "requires_before"}
    for item in blocks:
        if not isinstance(item, Mapping) or set(item) - allowed_keys:
            raise FlowError("积木声明包含不支持的字段")
        block_id = str(item.get("id") or "")
        if not _ID.fullmatch(block_id) or block_id in seen:
            raise FlowError(f"积木标识 {block_id} 无效或重复")
        seen.add(block_id)
        effect = item.get("effect")
        if effect not in EFFECTS:
            raise FlowError(f"积木 {block_id} 的效果类型无效")
        provides = list(item.get("provides_fields") or [])
        if any(name not in normalized_fields for name in provides):
            raise FlowError(f"积木 {block_id} 提供了未声明的字段")
        before = list(item.get("requires_before") or [])
        if any(name not in seen - {block_id} for name in before):
            raise FlowError(f"积木 {block_id} 依赖的积木必须先声明")
        if bool(item.get("condition")) and effect != "compute":
            raise FlowError(f"条件积木 {block_id} 只能是计算类")
        if effect == "external_write" and not before:
            raise FlowError(f"外部写入积木 {block_id} 必须声明它之前必须经过的核对积木")
        normalized_blocks.append({
            "id": block_id, "title": _text(item.get("title"), f"积木 {block_id} 的名称", 30),
            "description": str(item.get("description") or "")[:120], "effect": effect,
            "required": bool(item.get("required")), "locked": bool(item.get("locked")),
            "repeatable": bool(item.get("repeatable")), "condition": bool(item.get("condition")),
            "params": _param_schema(item.get("params"), f"积木 {block_id}"),
            "provides_fields": provides, "requires_before": before,
        })
    declaration = {"fields": normalized_fields, "blocks": normalized_blocks}
    if "default" in raw:
        declaration["default"] = validate_flow(raw["default"], declaration)
    else:
        raise FlowError("流程声明必须给出默认流程")
    return declaration


def flow_declaration(config_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    """The validated declaration a config schema carries, or ``None`` for plugins without flows."""
    prop = (config_schema.get("properties") or {}).get("flow") if isinstance(config_schema, Mapping) else None
    if not isinstance(prop, Mapping) or FLOW_KEYWORD not in prop:
        return None
    return validate_flow_declaration(prop[FLOW_KEYWORD])


def _check_value(field: Mapping[str, Any], value: Any, label: str) -> None:
    if field["type"] == "string":
        if not isinstance(value, str) or len(value) > 64:
            raise FlowError(f"{label}的取值必须是文字")
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FlowError(f"{label}的取值必须是数字")
    if "enum" in field and value not in field["enum"]:
        raise FlowError(f"{label}的取值 {value} 不在可选范围内")
    for bound, test in (("minimum", lambda v, b: v < b), ("maximum", lambda v, b: v > b)):
        if bound in field and isinstance(value, (int, float)) and test(value, field[bound]):
            raise FlowError(f"{label}的取值超出范围")


def _check_condition(node: Any, fields: Mapping[str, Any], available: set[str], depth: int = 0) -> dict[str, Any]:
    if depth > 3 or not isinstance(node, Mapping):
        raise FlowError("条件写法无效")
    if set(node) in ({"all"}, {"any"}):
        key = next(iter(node))
        items = node[key]
        if not isinstance(items, list) or not 1 <= len(items) <= 6:
            raise FlowError("组合条件需要 1 到 6 个子条件")
        return {key: [_check_condition(item, fields, available, depth + 1) for item in items]}
    if set(node) != {"field", "op", "value"}:
        raise FlowError("单个条件只能包含 field、op、value")
    name, op, value = str(node["field"]), node["op"], node["value"]
    if name not in fields:
        raise FlowError(f"条件使用了未声明的字段 {name}")
    if name not in available:
        raise FlowError(f"字段「{fields[name]['title']}」在这一步之前还没有取得")
    if op not in OPERATORS:
        raise FlowError(f"不支持的比较方式 {op}")
    label = f"条件「{fields[name]['title']}」"
    if op in ("in", "not_in"):
        if not isinstance(value, list) or not 1 <= len(value) <= 20:
            raise FlowError(f"{label}需要 1 到 20 个取值")
        for item in value:
            _check_value(fields[name], item, label)
    else:
        if op in ("gte", "lte") and fields[name]["type"] == "string":
            raise FlowError(f"{label}是文字，不能比较大小")
        _check_value(fields[name], value, label)
    return {"field": name, "op": op, "value": copy.deepcopy(value)}


def _check_params(block: Mapping[str, Any], params: Any) -> dict[str, Any]:
    params = {} if params is None else params
    schema = block["params"]
    if not isinstance(params, Mapping) or set(params) - set(schema["properties"]):
        raise FlowError(f"「{block['title']}」包含不支持的参数")
    missing = [name for name in schema.get("required", []) if name not in params]
    if missing:
        raise FlowError(f"「{block['title']}」缺少参数 {'、'.join(missing)}")
    for name, value in params.items():
        spec = schema["properties"][name]
        kind = spec["type"]
        valid = (isinstance(value, bool) if kind == "boolean" else
                 isinstance(value, str) if kind == "string" else
                 (isinstance(value, int) and not isinstance(value, bool)) if kind == "integer" else
                 (isinstance(value, (int, float)) and not isinstance(value, bool)))
        if not valid or ("enum" in spec and value not in spec["enum"]) or (
                isinstance(value, (int, float)) and not isinstance(value, bool) and (
                    ("minimum" in spec and value < spec["minimum"]) or ("maximum" in spec and value > spec["maximum"]))):
            raise FlowError(f"「{block['title']}」的参数 {name} 取值无效")
    return copy.deepcopy(dict(params))


def validate_flow(flow: Any, declaration: Mapping[str, Any]) -> dict[str, Any]:
    """Check one flow against its declaration; returns the normalized flow."""
    flow, declaration = _thaw(flow), _thaw(declaration)
    if not isinstance(flow, Mapping) or set(flow) != {"steps"}:
        raise FlowError("流程只能包含 steps")
    steps = flow["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise FlowError(f"流程需要 1 到 {MAX_STEPS} 个步骤")
    blocks = {block["id"]: block for block in declaration["blocks"]}
    fields = declaration["fields"]
    available: set[str] = set()
    used: list[str] = []
    normalized: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping) or "block" not in step or set(step) - {"block", "params", "when"}:
            raise FlowError(f"第 {index + 1} 步写法无效")
        block = blocks.get(step["block"])
        if block is None:
            raise FlowError(f"第 {index + 1} 步使用了插件没有的积木 {step['block']}")
        if block["id"] in used and not block["repeatable"]:
            raise FlowError(f"「{block['title']}」只能出现一次")
        missing = [name for name in block["requires_before"] if name not in used]
        if missing:
            titles = "、".join(blocks[name]["title"] for name in missing)
            raise FlowError(f"「{block['title']}」之前必须先经过「{titles}」")
        entry: dict[str, Any] = {"block": block["id"]}
        if step.get("params") not in (None, {}):
            entry["params"] = _check_params(block, step.get("params"))
        if block["condition"]:
            if "when" not in step:
                raise FlowError(f"「{block['title']}」需要写明条件")
            entry["when"] = _check_condition(step["when"], fields, available)
        elif "when" in step:
            raise FlowError(f"「{block['title']}」不能带条件")
        if block["effect"] == "external_write" and index != len(steps) - 1:
            raise FlowError(f"「{block['title']}」会写入外部系统，必须是最后一步")
        used.append(block["id"])
        available.update(block["provides_fields"])
        normalized.append(entry)
    for block in declaration["blocks"]:
        if block["required"] and block["id"] not in used:
            raise FlowError(f"「{block['title']}」是必需的步骤，不能删除")
    locked_order = [block["id"] for block in declaration["blocks"] if block["locked"]]
    observed = [block_id for block_id in used if block_id in locked_order]
    if observed != [block_id for block_id in locked_order if block_id in observed]:
        raise FlowError("系统保护的步骤不能调换顺序")
    return {"steps": normalized}


def count_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> int:
    """Steps added, removed or changed between two flows (order-aware, cheap and explainable)."""
    old = [json.dumps(step, sort_keys=True, ensure_ascii=False) for step in before.get("steps", [])]
    new = [json.dumps(step, sort_keys=True, ensure_ascii=False) for step in after.get("steps", [])]
    # Longest common subsequence: what stays; everything else counts as a change.
    table = [[0] * (len(new) + 1) for _ in range(len(old) + 1)]
    for i in range(len(old) - 1, -1, -1):
        for j in range(len(new) - 1, -1, -1):
            table[i][j] = table[i + 1][j + 1] + 1 if old[i] == new[j] else max(table[i + 1][j], table[i][j + 1])
    kept = table[0][0]
    return max(len(old), len(new)) - kept


def evaluate_condition(node: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """Evaluate a validated condition against one record; a missing field never matches."""
    if "all" in node:
        return all(evaluate_condition(item, record) for item in node["all"])
    if "any" in node:
        return any(evaluate_condition(item, record) for item in node["any"])
    if node["field"] not in record or record[node["field"]] is None:
        return False
    value, expected, op = record[node["field"]], node["value"], node["op"]
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op == "in":
        return value in expected
    if op == "not_in":
        return value not in expected
    try:
        return value >= expected if op == "gte" else value <= expected
    except TypeError:
        return False


def describe_condition(node: Mapping[str, Any], fields: Mapping[str, Any]) -> str:
    """Plain Chinese for a validated condition, for people reviewing a flow."""
    if "all" in node:
        return "（" + " 且 ".join(describe_condition(item, fields) for item in node["all"]) + "）"
    if "any" in node:
        return "（" + " 或 ".join(describe_condition(item, fields) for item in node["any"]) + "）"
    title = fields.get(node["field"], {}).get("title", node["field"])
    words = {"eq": "等于", "ne": "不等于", "in": "属于", "not_in": "不属于", "gte": "不低于", "lte": "不高于"}
    value = "、".join(map(str, node["value"])) if isinstance(node["value"], list) else str(node["value"])
    return f"{title}{words[node['op']]}{value}"


def simulate(flow: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Dry run over historical records: how many pass each condition step. No block is executed."""
    remaining = list(records)
    counts = []
    for index, step in enumerate(flow["steps"]):
        if "when" in step:
            remaining = [record for record in remaining if evaluate_condition(step["when"], record)]
        counts.append({"step": index, "block": step["block"], "remaining": len(remaining)})
    return {"total": len(records), "steps": counts, "reach_end": len(remaining)}


__all__ = [
    "EFFECTS", "FLOW_KEYWORD", "FlowError", "MAX_AI_CHANGES", "MAX_STEPS", "OPERATORS", "count_changes",
    "describe_condition", "evaluate_condition", "flow_declaration", "simulate", "validate_flow",
    "validate_flow_declaration",
]
