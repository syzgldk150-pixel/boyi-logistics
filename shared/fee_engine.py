"""Fee rules built from fixed blocks, filled in the back office (业务手册 → 计算规则).

A rule version's ``content`` is plain JSON. It says what people type (``params``) and how
the fee is computed (``formula``: a tree of a fixed set of blocks). People or an AI draft
choose blocks and fill in every number and source; code owns what each block means.
Adding a new kind of fee therefore needs no release, only a new kind of block does.

Content shape (schema 1)::

    {"schema": 1,
     "params": [{"key": "settlement_weight", "label": "结算重量", "type": "number", "unit": "kg", ...}, ...],
     "formula": {"type": "sum", "items": [...]},
     "definitions": {"普货": {...}},          # optional, reused with {"type": "use"}
     "group": "上楼卸货",                     # optional: fees in one group are not charged together, the larger counts
     "reminders": ["..."]}

Value references: a parameter key, ``piece.<field>`` inside a per-piece block, ``const``,
``count`` / ``sum`` / ``max`` over pieces (optionally ``where``), ``excess`` (the part above a
number), ``ceil`` (how many units of ``per``, a part counts as one), ``first`` (the first
value given, e.g. floor or stairs) and ``add`` / ``mul``.

Parameter types: ``number`` (unit, min, max, integer, decimals, default), ``choice``
(options), ``bool`` and ``pieces`` (rows of ``count`` plus declared fields such as each
piece's weight). Blocks: ``fixed``, ``rate`` (rate × value), ``amount`` (a value as
money), ``sum``, ``max`` (the larger counts), ``tiers`` (pick a tier by a value),
``when`` (first matching condition), ``lookup`` (by a choice), ``clamp`` (minimum /
maximum), ``each_piece`` (per pieces row, times its count), ``use`` (a definition) and
``none`` (no amount: 需协商 / 可不提供 / 需人工判断 / 实报实销).

``parse_rule`` validates a content completely and reports every problem at once in
Chinese; nothing is guessed or defaulted. ``calculate`` gives one fee for the values
people typed; ``quote`` combines several fees of one delivery (shared parameters asked
once, groups take the larger fee). Money is ``Decimal`` throughout, each fee is rounded
half-up to the cent at the end. Pure functions: no environment or I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from shared.handbook_fee_rules import FeeRuleError

SCHEMA_VERSION = 1
CENT = Decimal("0.01")
MAX_NUMBER = Decimal("1000000")
MAX_PARAMS = 30
MAX_OPTIONS = 30
MAX_FIELDS = 8
MAX_ROWS = 50
MAX_TIERS = 50
MAX_CASES = 30
MAX_NODES = 500
MAX_DEPTH = 16
MAX_DEFINITIONS = 10
MAX_REMINDERS = 10

PARAM_TYPES = {"number": "数字", "choice": "单选", "bool": "是/否", "pieces": "逐件明细"}
NO_AMOUNT = {"NEGOTIATE": "需协商", "NOT_PROVIDED": "可不提供服务", "MANUAL": "需人工判断", "ACTUAL": "实报实销"}
OUTCOME_TEXT = {"AMOUNT": "金额", "MISSING": "缺少参数", **NO_AMOUNT}
NODE_TYPES = {"fixed": "固定金额", "rate": "单价×数值", "amount": "按数值计金额", "sum": "相加", "max": "取大",
              "tiers": "分档", "when": "按条件", "lookup": "按选项", "clamp": "最低/最高", "each_piece": "逐件计费",
              "use": "引用", "none": "无金额"}
COMPARE_OPS = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "lt": "<", "le": "≤", "in": "属于"}
PREFILL = {"settlement_weight": "运单结算重量", "pieces": "运单件数", "actual_weight": "运单实际重量"}
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PIECE = "piece."


# -- parsed rule ------------------------------------------------------------------------

@dataclass(frozen=True)
class PieceField:
    key: str
    label: str
    unit: str
    minimum: Decimal | None
    maximum: Decimal | None
    integer: bool


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    type: str
    unit: str = ""
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    integer: bool = False
    decimals: int = 4
    options: tuple[tuple[str, str], ...] = ()
    fields: tuple[PieceField, ...] = ()
    required: bool = True
    default: Any = None
    prefill: str = ""
    hint: str = ""

    def shape(self) -> tuple:
        """What must agree when two fees share this parameter in one quote."""
        return (self.type, self.unit, self.integer, self.options, tuple((f.key, f.unit) for f in self.fields))

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"key": self.key, "label": self.label, "type": self.type, "required": self.required}
        if self.unit:
            data["unit"] = self.unit
        if self.minimum is not None:
            data["min"] = _fmt(self.minimum)
        if self.maximum is not None:
            data["max"] = _fmt(self.maximum)
        if self.type == "number":
            data.update(integer=self.integer, decimals=self.decimals)
        if self.options:
            data["options"] = [{"value": value, "label": label} for value, label in self.options]
        if self.fields:
            data["fields"] = [{"key": f.key, "label": f.label, "unit": f.unit, "integer": f.integer} for f in self.fields]
        if self.default is not None:
            data["default"] = _fmt(self.default) if isinstance(self.default, Decimal) else self.default
        if self.prefill:
            data["prefill"] = self.prefill
        if self.hint:
            data["hint"] = self.hint
        return data


@dataclass(frozen=True)
class Rule:
    params: tuple[Param, ...]
    formula: Mapping[str, Any]
    definitions: Mapping[str, Mapping[str, Any]]
    group: str
    reminders: tuple[str, ...]

    def param(self, key: str) -> Param:
        return next(item for item in self.params if item.key == key)


# -- validation -------------------------------------------------------------------------

class _Check:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.nodes = 0

    def add(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    def text(self, value: Any, label: str, limit: int, *, required: bool = True) -> str:
        if value is None and not required:
            return ""
        if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
            self.add(f"{label}{'必须填写，且' if required else ''}不超过 {limit} 字。")
            return ""
        return value.strip()

    def number(self, value: Any, label: str, *, allow_negative: bool = False) -> Decimal | None:
        if isinstance(value, bool) or value is None:
            self.add(f"{label}不是有效数字。")
            return None
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            self.add(f"{label}不是有效数字。")
            return None
        if not number.is_finite() or abs(number) > MAX_NUMBER or (number < 0 and not allow_negative):
            low = f"-{MAX_NUMBER}" if allow_negative else "0"
            self.add(f"{label}必须是 {low} 到 {MAX_NUMBER} 之间的数字。")
            return None
        if number.as_tuple().exponent < -4:
            self.add(f"{label}最多保留 4 位小数。")
        return number

    def source(self, value: Any, label: str) -> None:
        if value is None:
            return
        if not isinstance(value, Mapping):
            self.add(f"{label}的出处格式无效。")
            return
        self.text(value.get("document"), f"{label}的出处文档", 100)
        self.text(value.get("location"), f"{label}的页码或条款", 100)
        self.text(value.get("quote"), f"{label}的原文摘句", 500, required=False)
        link = self.text(value.get("link"), f"{label}的链接", 500, required=False)
        if link and not link.startswith("https://"):
            self.add(f"{label}的链接必须以 https:// 开头。")


def _parse_params(raw: Any, check: _Check) -> tuple[Param, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_PARAMS:
        check.add(f"参数清单必须是列表，最多 {MAX_PARAMS} 个。")
        return ()
    params, seen = [], set()
    for index, item in enumerate(raw, 1):
        where = f"第 {index} 个参数"
        if not isinstance(item, Mapping):
            check.add(f"{where}格式无效。")
            continue
        key = item.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            check.add(f"{where}的标识必须是小写英文字母开头的字母、数字或下划线（最多 32 位）。")
            continue
        if key in seen:
            check.add(f"参数标识“{key}”重复。")
            continue
        seen.add(key)
        label = check.text(item.get("label"), f"{where}的名称", 30) or key
        where = f"参数“{label}”"
        kind = item.get("type")
        if kind not in PARAM_TYPES:
            check.add(f"{where}的类型无效。")
            continue
        unit = check.text(item.get("unit"), f"{where}的单位", 10, required=False)
        hint = check.text(item.get("hint"), f"{where}的提示", 100, required=False)
        required = item.get("required", True)
        if type(required) is not bool:
            check.add(f"{where}的“必填”必须是是/否。")
            required = True
        prefill = item.get("prefill") or ""
        if prefill and prefill not in PREFILL:
            check.add(f"{where}的预填来源无效。")
            prefill = ""
        minimum = maximum = None
        integer, decimals, options, fields, default = False, 4, (), (), None
        if kind == "number":
            integer = item.get("integer", False)
            if type(integer) is not bool:
                check.add(f"{where}的“整数”必须是是/否。")
                integer = False
            decimals = item.get("decimals", 0 if integer else 4)
            if type(decimals) is not int or not 0 <= decimals <= 4:
                check.add(f"{where}的小数位必须是 0 到 4。")
                decimals = 4
            minimum = check.number(item["min"], f"{where}的最小值", allow_negative=True) if "min" in item else None
            maximum = check.number(item["max"], f"{where}的最大值", allow_negative=True) if "max" in item else None
            if minimum is not None and maximum is not None and minimum > maximum:
                check.add(f"{where}的最小值大于最大值。")
            if "default" in item:
                default = check.number(item["default"], f"{where}的默认值", allow_negative=True)
        elif kind == "choice":
            raw_options = item.get("options")
            if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= MAX_OPTIONS:
                check.add(f"{where}要有 2 到 {MAX_OPTIONS} 个选项。")
                raw_options = []
            values = []
            for number, option in enumerate(raw_options, 1):
                option = option if isinstance(option, Mapping) else {}
                value = option.get("value")
                if not isinstance(value, str) or not _KEY.fullmatch(value) or value in values:
                    check.add(f"{where}第 {number} 个选项的取值无效或重复。")
                    continue
                values.append(value)
                options += ((value, check.text(option.get("label"), f"{where}第 {number} 个选项的名称", 30) or value),)
            if "default" in item:
                default = item["default"]
                if default not in values:
                    check.add(f"{where}的默认值不在选项里。")
        elif kind == "bool":
            if "default" in item:
                default = item["default"]
                if type(default) is not bool:
                    check.add(f"{where}的默认值必须是是/否。")
        else:
            raw_fields = item.get("fields")
            if not isinstance(raw_fields, list) or not 1 <= len(raw_fields) <= MAX_FIELDS:
                check.add(f"{where}要有 1 到 {MAX_FIELDS} 个明细字段。")
                raw_fields = []
            names = {"count"}
            for number, raw_field in enumerate(raw_fields, 1):
                raw_field = raw_field if isinstance(raw_field, Mapping) else {}
                field_key = raw_field.get("key")
                if not isinstance(field_key, str) or not _KEY.fullmatch(field_key) or field_key in names:
                    check.add(f"{where}第 {number} 个明细字段的标识无效或重复（count 为件数，已内置）。")
                    continue
                names.add(field_key)
                field_label = check.text(raw_field.get("label"), f"{where}第 {number} 个明细字段的名称", 30) or field_key
                field_integer = raw_field.get("integer", False) is True
                low = check.number(raw_field["min"], f"{where}“{field_label}”的最小值") if "min" in raw_field else None
                high = check.number(raw_field["max"], f"{where}“{field_label}”的最大值") if "max" in raw_field else None
                fields += (PieceField(field_key, field_label,
                                      check.text(raw_field.get("unit"), f"{where}“{field_label}”的单位", 10, required=False),
                                      low, high, field_integer),)
        params.append(Param(key, label, kind, unit, minimum, maximum, integer, decimals, options, fields, required,
                            default, prefill, hint))
    return tuple(params)


class _Scope:
    def __init__(self, params: Mapping[str, Param], definitions: Mapping[str, Any]) -> None:
        self.params, self.definitions = params, definitions
        self.piece: Param | None = None


def _ref_kind(ref: Any, scope: _Scope, check: _Check, where: str) -> str:
    """Validate a value reference; returns number / bool / choice:<param key> / '' when invalid."""
    if isinstance(ref, str):
        if ref.startswith(_PIECE):
            name = ref[len(_PIECE):]
            if scope.piece is None:
                check.add(f"{where}引用了逐件字段“{name}”，但不在“逐件计费”或逐件条件里。")
                return ""
            if name == "count":
                return "number"
            if name not in {f.key for f in scope.piece.fields}:
                check.add(f"{where}引用的逐件字段“{name}”不存在。")
                return ""
            return "number"
        param = scope.params.get(ref)
        if param is None:
            check.add(f"{where}引用的参数“{ref}”不在参数清单里。")
            return ""
        if param.type == "pieces":
            check.add(f"{where}不能直接使用逐件明细“{param.label}”，请用件数、合计或最大值。")
            return ""
        return "choice:" + ref if param.type == "choice" else param.type
    if not isinstance(ref, Mapping) or len(ref) == 0:
        check.add(f"{where}的数值引用格式无效。")
        return ""
    if "const" in ref:
        check.number(ref["const"], f"{where}的常数", allow_negative=True)
        return "number"
    for op in ("count", "sum", "max"):
        if op in ref:
            param = scope.params.get(ref[op])
            if param is None or param.type != "pieces":
                check.add(f"{where}的“{op}”必须引用一个逐件明细参数。")
                return ""
            if op != "count" and ref.get("field") not in {f.key for f in param.fields}:
                check.add(f"{where}的“{op}”要指定逐件明细里的字段。")
                return ""
            if "where" in ref:
                outer, scope.piece = scope.piece, param
                _check_condition(ref["where"], scope, check, where)
                scope.piece = outer
            return "number"
    if "excess" in ref:
        _number_ref(ref["excess"], scope, check, where)
        check.number(ref.get("over"), f"{where}的起算值", allow_negative=True)
        return "number"
    if "ceil" in ref:
        _number_ref(ref["ceil"], scope, check, where)
        per = check.number(ref.get("per"), f"{where}的每份数量")
        if per is not None and per <= 0:
            check.add(f"{where}的每份数量必须大于 0。")
        if "min" in ref:
            check.number(ref["min"], f"{where}的最小结果", allow_negative=True)
        return "number"
    if "first" in ref:
        items = ref["first"]
        if not isinstance(items, list) or not 2 <= len(items) <= 4:
            check.add(f"{where}的“二选一”要有 2 到 4 项。")
            return ""
        for item in items:
            _number_ref(item, scope, check, where)
        return "number"
    for op in ("add", "mul"):
        if op in ref:
            items = ref[op]
            if not isinstance(items, list) or not 2 <= len(items) <= 5:
                check.add(f"{where}的“{op}”要有 2 到 5 项。")
                return ""
            for item in items:
                _number_ref(item, scope, check, where)
            return "number"
    check.add(f"{where}的数值引用格式无效。")
    return ""


def _number_ref(ref: Any, scope: _Scope, check: _Check, where: str) -> None:
    kind = _ref_kind(ref, scope, check, where)
    if kind and kind != "number":
        check.add(f"{where}需要一个数字，但引用的是{'是/否' if kind == 'bool' else '选项'}。")


def _check_condition(cond: Any, scope: _Scope, check: _Check, where: str, depth: int = 0) -> None:
    if depth > MAX_DEPTH or not isinstance(cond, Mapping):
        check.add(f"{where}的条件格式无效。")
        return
    for op in ("all", "any"):
        if op in cond:
            items = cond[op]
            if not isinstance(items, list) or not 1 <= len(items) <= 10:
                check.add(f"{where}的“{'全部满足' if op == 'all' else '满足其一'}”要有 1 到 10 个条件。")
                return
            for item in items:
                _check_condition(item, scope, check, where, depth + 1)
            return
    if "not" in cond:
        _check_condition(cond["not"], scope, check, where, depth + 1)
        return
    op = cond.get("op")
    if op not in COMPARE_OPS:
        check.add(f"{where}的比较方式无效。")
        return
    kind = _ref_kind(cond.get("left"), scope, check, where)
    value = cond.get("value")
    if not kind:
        return
    if kind == "bool":
        if op not in {"eq", "ne"} or type(value) is not bool:
            check.add(f"{where}的是/否条件只能比较“是”或“否”。")
    elif kind.startswith("choice:"):
        options = {item[0] for item in scope.params[kind[7:]].options}
        values = value if op == "in" else [value]
        if op not in {"eq", "ne", "in"} or not isinstance(values, list) or not values or any(v not in options for v in values):
            check.add(f"{where}的选项条件取值不在“{scope.params[kind[7:]].label}”的选项里。")
    else:
        if op == "in":
            check.add(f"{where}的数字条件不能用“属于”。")
        else:
            check.number(value, f"{where}的比较值", allow_negative=True)


def _label(node: Mapping[str, Any], default: str) -> str:
    label = node.get("label")
    return label.strip() if isinstance(label, str) and label.strip() else default


def _check_node(node: Any, scope: _Scope, check: _Check, path: str, depth: int, *, in_definition: bool = False) -> None:
    check.nodes += 1
    if check.nodes > MAX_NODES:
        check.add(f"计算结构超过 {MAX_NODES} 个积木，请拆成多种费用。")
        return
    if depth > MAX_DEPTH:
        check.add(f"{path}嵌套超过 {MAX_DEPTH} 层。")
        return
    if not isinstance(node, Mapping) or node.get("type") not in NODE_TYPES:
        check.add(f"{path}不是有效的积木。")
        return
    kind = node["type"]
    where = f"{path} › {_label(node, NODE_TYPES[kind])}"
    check.text(node.get("label"), f"{where}的名称", 40, required=False)
    check.source(node.get("source"), where)

    def child(value: Any, name: str) -> None:
        _check_node(value, scope, check, f"{where}{name}", depth + 1, in_definition=in_definition)

    if kind == "fixed":
        check.number(node.get("amount"), f"{where}的金额")
    elif kind == "rate":
        check.number(node.get("rate"), f"{where}的单价")
        check.text(node.get("unit"), f"{where}的单价单位", 20, required=False)
        _number_ref(node.get("of"), scope, check, where)
    elif kind == "amount":
        _number_ref(node.get("of"), scope, check, where)
    elif kind in {"sum", "max"}:
        items = node.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_CASES:
            check.add(f"{where}要有 1 到 {MAX_CASES} 项。")
            return
        for index, item in enumerate(items, 1):
            child(item, f" 第 {index} 项")
    elif kind == "tiers":
        _check_tiers(node, scope, check, where, child)
    elif kind == "when":
        cases = node.get("cases")
        if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
            check.add(f"{where}要有 1 到 {MAX_CASES} 种情况。")
            return
        for index, case in enumerate(cases, 1):
            case = case if isinstance(case, Mapping) else {}
            _check_condition(case.get("if"), scope, check, f"{where} 第 {index} 种情况")
            child(case.get("then"), f" 第 {index} 种情况")
        if "else" in node:
            child(node["else"], " 其他情况")
    elif kind == "lookup":
        param = scope.params.get(node.get("by"))
        if param is None or param.type != "choice":
            check.add(f"{where}必须按一个单选参数查表。")
            return
        rows = node.get("rows")
        options = {item[0] for item in param.options}
        if not isinstance(rows, list) or not rows:
            check.add(f"{where}至少要有一行。")
            return
        seen = set()
        for index, row in enumerate(rows, 1):
            row = row if isinstance(row, Mapping) else {}
            if row.get("value") not in options or row.get("value") in seen:
                check.add(f"{where}第 {index} 行的选项不在“{param.label}”里或重复。")
            seen.add(row.get("value"))
            child(row.get("then"), f" 第 {index} 行")
        if "else" in node:
            child(node["else"], " 其他选项")
    elif kind == "clamp":
        low = check.number(node["min"], f"{where}的最低金额") if "min" in node else None
        high = check.number(node["max"], f"{where}的最高金额") if "max" in node else None
        if low is None and high is None and "min" not in node and "max" not in node:
            check.add(f"{where}至少要填最低或最高金额。")
        if low is not None and high is not None and low > high:
            check.add(f"{where}的最低金额大于最高金额。")
        child(node.get("item"), "")
    elif kind == "each_piece":
        param = scope.params.get(node.get("pieces"))
        if param is None or param.type != "pieces":
            check.add(f"{where}必须指定一个逐件明细参数。")
            return
        if scope.piece is not None:
            check.add(f"{where}不能嵌套在另一个逐件计费里。")
            return
        scope.piece = param
        child(node.get("item"), "")
        scope.piece = None
    elif kind == "use":
        if in_definition:
            check.add(f"{where}：公共部分里不能再引用公共部分。")
        elif node.get("name") not in scope.definitions:
            check.add(f"{where}引用的公共部分“{node.get('name')}”不存在。")
    else:
        if node.get("outcome") not in NO_AMOUNT:
            check.add(f"{where}的结果必须是：{'、'.join(NO_AMOUNT.values())}。")
        check.text(node.get("reason"), f"{where}的原因", 200)


def _check_tiers(node: Mapping[str, Any], scope: _Scope, check: _Check, where: str, child) -> None:
    _number_ref(node.get("by"), scope, check, where)
    closed = node.get("closed", "right")
    if closed not in {"right", "left"}:
        check.add(f"{where}的边界归属无效。")
    tiers = node.get("tiers")
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= MAX_TIERS:
        check.add(f"{where}要有 1 到 {MAX_TIERS} 档。")
        return
    previous: Decimal | None = None
    for index, tier in enumerate(tiers, 1):
        tier = tier if isinstance(tier, Mapping) else {}
        label = f"{where}第 {index} 档"
        low = None if tier.get("over") is None else check.number(tier.get("over"), f"{label}的起点", allow_negative=True)
        high = None if tier.get("up_to") is None else check.number(tier.get("up_to"), f"{label}的终点", allow_negative=True)
        if tier.get("over") is None and index != 1:
            check.add(f"{label}缺少起点（只有第一档可以不设起点）。")
        if tier.get("up_to") is None and index != len(tiers):
            check.add(f"{label}缺少终点（只有最后一档可以不设终点）。")
        if low is not None and high is not None and high <= low:
            check.add(f"{label}的终点必须大于起点。")
        if index > 1 and previous is not None and low is not None and low != previous:
            check.add(f"{label}的起点应等于上一档的终点 {_fmt(previous)}（各档要连续、不重叠）。")
        previous = high
        child(tier.get("then"), f" 第 {index} 档")
    if "else" in node:
        child(node["else"], " 不在各档内")


def parse_rule(content: Any) -> Rule:
    """Validate a rule version completely; every problem is reported at once."""
    if not isinstance(content, Mapping):
        raise FeeRuleError("FEE_RULE_CONTENT_INVALID", "规则内容格式无效。")
    check = _Check()
    if content.get("schema") != SCHEMA_VERSION:
        check.add(f"规则格式版本必须是 {SCHEMA_VERSION}。")
    params = _parse_params(content.get("params"), check)
    by_key = {param.key: param for param in params}
    definitions = content.get("definitions", {})
    if not isinstance(definitions, Mapping) or len(definitions) > MAX_DEFINITIONS:
        check.add(f"公共部分最多 {MAX_DEFINITIONS} 个。")
        definitions = {}
    scope = _Scope(by_key, definitions)
    for name, node in definitions.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 20:
            check.add("公共部分的名称必须填写，且不超过 20 字。")
        _check_node(node, scope, check, f"公共部分“{name}”", 1, in_definition=True)
    _check_node(content.get("formula"), scope, check, "计算结构", 1)
    group = check.text(content.get("group"), "互斥组名称", 20, required=False)
    reminders = content.get("reminders", [])
    if not isinstance(reminders, list) or len(reminders) > MAX_REMINDERS:
        check.add(f"提醒最多 {MAX_REMINDERS} 条。")
        reminders = []
    reminders = tuple(item for item in (check.text(r, "提醒", 200) for r in reminders) if item)
    if check.problems:
        raise FeeRuleError("FEE_RULE_CONTENT_INVALID", "；".join(check.problems))
    return Rule(params, content["formula"], dict(definitions), group, reminders)


def unsourced_blocks(content: Any) -> list[str]:
    """Names of blocks that give a result (amount or no amount) without a source on themselves or above.

    Used before a version may take effect: every number must be traceable to the manual.
    """
    if not isinstance(content, Mapping):
        return []
    missing: list[str] = []

    def walk(node: Any, path: str, covered: bool) -> None:
        if not isinstance(node, Mapping) or node.get("type") not in NODE_TYPES:
            return
        kind = node["type"]
        here = f"{path} › {_label(node, NODE_TYPES[kind])}" if path else _label(node, NODE_TYPES[kind])
        covered = covered or isinstance(node.get("source"), Mapping)
        if kind in {"fixed", "rate", "amount", "none"} and not covered:
            missing.append(here)
        for key in ("items",):
            for item in node.get(key) or []:
                walk(item, here, covered)
        for key in ("item", "else"):
            walk(node.get(key), here, covered)
        for key in ("tiers", "cases", "rows"):
            for item in node.get(key) or []:
                if isinstance(item, Mapping):
                    walk(item.get("then"), here, covered)

    definitions = content.get("definitions") if isinstance(content.get("definitions"), Mapping) else {}
    for name, node in definitions.items():
        walk(node, f"公共部分“{name}”", False)
    walk(content.get("formula"), "", False)
    return list(dict.fromkeys(missing))


# -- input values -----------------------------------------------------------------------

def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _range_problem(number: Decimal, label: str, unit: str, low: Decimal | None, high: Decimal | None,
                   integer: bool, decimals: int = 4) -> str | None:
    if integer and number != number.to_integral_value():
        return f"{label}必须是整数。"
    if number.as_tuple().exponent < -decimals:
        return f"{label}最多保留 {decimals} 位小数。"
    if low is not None and number < low:
        return f"{label}不能小于 {_fmt(low)}{unit}。"
    if high is not None and number > high:
        return f"{label}不能大于 {_fmt(high)}{unit}。"
    return None


def normalize_values(rule: Rule, values: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Typed values for the rule's parameters plus the labels of missing required ones.

    Invalid values raise ``FEE_INPUT_INVALID`` listing every problem. Values for other
    parameters are ignored (a quote passes one set of values to several fees).
    """
    if not isinstance(values, Mapping):
        raise FeeRuleError("FEE_INPUT_INVALID", "参数格式无效。")
    typed: dict[str, Any] = {}
    missing: list[str] = []
    problems: list[str] = []
    for param in rule.params:
        raw = values.get(param.key)
        if raw is None or (isinstance(raw, str) and not raw.strip()) or (param.type == "pieces" and raw == []):
            if param.default is not None:
                typed[param.key] = param.default
            elif param.required:
                missing.append(param.label)
            continue
        if param.type == "number":
            number = _decimal(raw)
            if number is None:
                problems.append(f"{param.label}不是有效数字。")
                continue
            problem = _range_problem(number, param.label, param.unit, param.minimum, param.maximum, param.integer,
                                     param.decimals)
            if problem:
                problems.append(problem)
                continue
            typed[param.key] = number
        elif param.type == "bool":
            if type(raw) is not bool:
                problems.append(f"{param.label}只能选是或否。")
                continue
            typed[param.key] = raw
        elif param.type == "choice":
            if raw not in {value for value, _ in param.options}:
                problems.append(f"{param.label}的选项无效。")
                continue
            typed[param.key] = raw
        else:
            rows = _normalize_pieces(param, raw, problems)
            if rows is not None:
                typed[param.key] = rows
    if problems:
        raise FeeRuleError("FEE_INPUT_INVALID", "；".join(problems))
    return typed, tuple(missing)


def _normalize_pieces(param: Param, raw: Any, problems: list[str]) -> tuple[dict[str, Any], ...] | None:
    if not isinstance(raw, list) or len(raw) > MAX_ROWS:
        problems.append(f"{param.label}最多 {MAX_ROWS} 行。")
        return None
    rows = []
    for index, row in enumerate(raw, 1):
        where = f"{param.label}第 {index} 行"
        if not isinstance(row, Mapping):
            problems.append(f"{where}格式无效。")
            continue
        count = row.get("count", 1)
        if type(count) is not int or not 1 <= count <= 100000:
            problems.append(f"{where}的件数必须是 1 到 100000 的整数。")
            continue
        typed = {"count": Decimal(count)}
        for piece_field in param.fields:
            value = row.get(piece_field.key)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            number = _decimal(value)
            problem = (f"{where}的{piece_field.label}不是有效数字。" if number is None else
                       _range_problem(number, f"{where}的{piece_field.label}", piece_field.unit, piece_field.minimum,
                                      piece_field.maximum, piece_field.integer))
            if problem:
                problems.append(problem)
                continue
            typed[piece_field.key] = number
        rows.append(typed)
    return tuple(rows)


# -- evaluation -------------------------------------------------------------------------

class _Missing(Exception):
    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


@dataclass
class _Result:
    amount: Decimal | None
    outcome: str = "AMOUNT"
    reason: str = ""
    steps: list[str] = field(default_factory=list)
    sources: list[Mapping[str, Any]] = field(default_factory=list)


def _fmt(number: Decimal) -> str:
    text = format(number.normalize(), "f") if number == number.to_integral_value() else format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class _Evaluator:
    def __init__(self, rule: Rule, values: Mapping[str, Any]) -> None:
        self.rule, self.values = rule, values
        self.params = {param.key: param for param in rule.params}
        self.piece: Mapping[str, Decimal] | None = None
        self.piece_param: Param | None = None

    # values
    def _param_value(self, key: str) -> Any:
        if key not in self.values:
            raise _Missing(self.params[key].label)
        return self.values[key]

    def value(self, ref: Any) -> tuple[Any, str]:
        """(value, description) of a reference."""
        if isinstance(ref, str):
            if ref.startswith(_PIECE):
                name = ref[len(_PIECE):]
                assert self.piece is not None and self.piece_param is not None
                if name == "count":
                    return self.piece["count"], "件数"
                piece_field = next(f for f in self.piece_param.fields if f.key == name)
                if name not in self.piece:
                    raise _Missing(f"{self.piece_param.label}的{piece_field.label}")
                return self.piece[name], piece_field.label
            return self._param_value(ref), self.params[ref].label
        if "const" in ref:
            number = Decimal(str(ref["const"]))
            return number, _fmt(number)
        for op in ("count", "sum", "max"):
            if op in ref:
                param = self.params[ref[op]]
                rows = self._param_value(param.key)
                if "where" in ref:
                    rows = [row for row in rows if self._piece_condition(param, row, ref["where"])]
                if op == "count":
                    return sum((row["count"] for row in rows), Decimal("0")), f"{param.label}件数"
                piece_field = next(f for f in param.fields if f.key == ref["field"])
                if any(piece_field.key not in row for row in rows):
                    raise _Missing(f"{param.label}的{piece_field.label}")
                if op == "sum":
                    return (sum((row[piece_field.key] * row["count"] for row in rows), Decimal("0")),
                            f"{piece_field.label}合计")
                return max((row[piece_field.key] for row in rows), default=Decimal("0")), f"最大{piece_field.label}"
        if "excess" in ref:
            number, text = self.value(ref["excess"])
            over = Decimal(str(ref["over"]))
            return max(number - over, Decimal("0")), f"（{text} {_fmt(number)} 超出 {_fmt(over)} 的部分）"
        if "ceil" in ref:
            number, text = self.value(ref["ceil"])
            per = Decimal(str(ref["per"]))
            result = (number / per).to_integral_value(rounding=ROUND_CEILING)
            if "min" in ref:
                result = max(result, Decimal(str(ref["min"])))
            return result, f"（{text} {_fmt(number)} 按每 {_fmt(per)} 计 1，不足 {_fmt(per)} 按 1 计）"
        if "first" in ref:
            labels = []
            for item in ref["first"]:
                try:
                    return self.value(item)
                except _Missing as exc:
                    labels.append(exc.label)
            raise _Missing("或".join(labels))
        for op, sign in (("add", " + "), ("mul", " × ")):
            if op in ref:
                parts = [self.value(item) for item in ref[op]]
                result = parts[0][0]
                for number, _ in parts[1:]:
                    result = result + number if op == "add" else result * number
                return result, "（" + sign.join(text if text == _fmt(n) else f"{text} {_fmt(n)}"
                                               for n, text in parts) + "）"
        raise AssertionError("validated reference")

    def _piece_condition(self, param: Param, row: Mapping[str, Decimal], cond: Any) -> bool:
        outer = (self.piece, self.piece_param)
        self.piece, self.piece_param = row, param
        try:
            return self.condition(cond)[0]
        finally:
            self.piece, self.piece_param = outer

    def condition(self, cond: Mapping[str, Any]) -> tuple[bool, str]:
        if "all" in cond or "any" in cond:
            # A missing value only matters when the other conditions cannot decide on their own.
            every = "all" in cond
            results, missing = [], None
            for item in cond["all" if every else "any"]:
                try:
                    results.append(self.condition(item))
                except _Missing as exc:
                    missing = missing or exc
                    continue
                if results[-1][0] != every:
                    return results[-1]
            if missing is not None:
                raise missing
            return every, ("且" if every else "或").join(r[1] for r in results)
        if "not" in cond:
            truth, text = self.condition(cond["not"])
            return not truth, f"不是（{text}）"
        left, text = self.value(cond["left"])
        op, expected = cond["op"], cond["value"]
        if isinstance(left, bool):
            truth = (left == expected) if op == "eq" else (left != expected)
            return truth, f"{text}：{'是' if left else '否'}"
        if isinstance(left, str):
            param = self.params[cond["left"]]
            names = dict(param.options)
            truth = left in expected if op == "in" else (left == expected) if op == "eq" else (left != expected)
            return truth, f"{text}：{names.get(left, left)}"
        expected = Decimal(str(expected))
        truth = {"eq": left == expected, "ne": left != expected, "gt": left > expected, "ge": left >= expected,
                 "lt": left < expected, "le": left <= expected}[op]
        return truth, f"{text} {_fmt(left)} {COMPARE_OPS[op]} {_fmt(expected)}"

    # nodes
    def node(self, node: Mapping[str, Any]) -> _Result:
        kind = node["type"]
        label = _label(node, "")
        result = getattr(self, "_" + kind)(node, label)
        if node.get("source"):
            result.sources.insert(0, node["source"])
        return result

    def _fixed(self, node, label) -> _Result:
        amount = Decimal(str(node["amount"]))
        return _Result(amount, steps=[f"{label}：{_fmt(amount)}"] if label else [])

    def _rate(self, node, label) -> _Result:
        rate = Decimal(str(node["rate"]))
        number, text = self.value(node["of"])
        amount = rate * number
        unit = f" {node['unit']}" if node.get("unit") else ""
        prefix = f"{label}：" if label else ""
        return _Result(amount, steps=[f"{prefix}{_fmt(rate)}{unit} × {text} {_fmt(number)} = {_fmt(amount)}"])

    def _amount(self, node, label) -> _Result:
        number, text = self.value(node["of"])
        shown = "" if text == _fmt(number) else f"{text} = "
        return _Result(number, steps=[f"{label + '：' if label else ''}{shown}{_fmt(number)}"])

    def _children(self, items: Iterable[Mapping[str, Any]]) -> tuple[list[_Result], _Result | None]:
        results = []
        for item in items:
            result = self.node(item)
            results.append(result)
            if result.amount is None:
                return results, result
        return results, None

    def _sum(self, node, label) -> _Result:
        results, stop = self._children(node["items"])
        steps = [step for item in results for step in item.steps]
        sources = [source for item in results for source in item.sources]
        if stop is not None:
            return _Result(None, stop.outcome, stop.reason, steps, sources)
        total = sum((item.amount for item in results), Decimal("0"))
        if label and len(results) > 1:
            steps.append(f"{label}：{' + '.join(_fmt(item.amount) for item in results)} = {_fmt(total)}")
        return _Result(total, steps=steps, sources=sources)

    def _max(self, node, label) -> _Result:
        results, stop = self._children(node["items"])
        steps = [step for item in results for step in item.steps]
        if stop is not None:
            return _Result(None, stop.outcome, stop.reason, steps, [s for item in results for s in item.sources])
        best = max(range(len(results)), key=lambda index: (results[index].amount, -index))
        chosen = results[best]
        if len(results) > 1:
            names = [_label(item, f"第 {index + 1} 项") for index, item in enumerate(node["items"])]
            steps.append(f"{label or '取大'}：{'、'.join(f'{n} {_fmt(r.amount)}' for n, r in zip(names, results))}，"
                         f"按{names[best]} {_fmt(chosen.amount)}")
        return _Result(chosen.amount, steps=steps, sources=list(chosen.sources))

    def _tiers(self, node, label) -> _Result:
        number, text = self.value(node["by"])
        right = node.get("closed", "right") == "right"
        for tier in node["tiers"]:
            low = None if tier.get("over") is None else Decimal(str(tier["over"]))
            high = None if tier.get("up_to") is None else Decimal(str(tier["up_to"]))
            above = low is None or (number > low if right else number >= low)
            below = high is None or (number <= high if right else number < high)
            if above and below:
                result = self.node(tier["then"])
                note = f"{label + '：' if label else ''}{text} {_fmt(number)}，{_range_text(low, high, right)}"
                result.steps.insert(0, note)
                return result
        if "else" in node:
            return self.node(node["else"])
        return _Result(None, "MANUAL", f"{text} {_fmt(number)} 不在规则的任何一档内。")

    def _when(self, node, label) -> _Result:
        for case in node["cases"]:
            truth, text = self.condition(case["if"])
            if truth:
                result = self.node(case["then"])
                result.steps.insert(0, f"{label + '：' if label else ''}{text}")
                return result
        if "else" in node:
            return self.node(node["else"])
        return _Result(None, "MANUAL", f"{label or '规则'}没有写明这种情况。")

    def _lookup(self, node, label) -> _Result:
        param = self.params[node["by"]]
        value = self._param_value(param.key)
        names = dict(param.options)
        for row in node["rows"]:
            if row["value"] == value:
                result = self.node(row["then"])
                result.steps.insert(0, f"{label or param.label}：{names[value]}")
                return result
        if "else" in node:
            return self.node(node["else"])
        return _Result(None, "MANUAL", f"{param.label}为“{names[value]}”时规则没有写明。")

    def _clamp(self, node, label) -> _Result:
        result = self.node(node["item"])
        if result.amount is None:
            return result
        low = Decimal(str(node["min"])) if "min" in node else None
        high = Decimal(str(node["max"])) if "max" in node else None
        name = label or "限额"
        if low is not None and result.amount < low:
            result.steps.append(f"{name}：{_fmt(result.amount)} 低于最低 {_fmt(low)}，按 {_fmt(low)}")
            result.amount = low
        elif high is not None and result.amount > high:
            result.steps.append(f"{name}：{_fmt(result.amount)} 高于最高 {_fmt(high)}，按 {_fmt(high)}")
            result.amount = high
        return result

    def _each_piece(self, node, label) -> _Result:
        param = self.params[node["pieces"]]
        rows = self._param_value(param.key)
        total, steps, sources = Decimal("0"), [], []
        for row in rows:
            self.piece, self.piece_param = row, param
            try:
                result = self.node(node["item"])
            finally:
                self.piece, self.piece_param = None, None
            describe = "、".join(f"{f.label} {_fmt(row[f.key])}{f.unit}" for f in param.fields if f.key in row)
            if result.amount is None:
                return _Result(None, result.outcome, f"{describe} 的 {_fmt(row['count'])} 件：{result.reason}",
                               steps, sources + result.sources)
            subtotal = result.amount * row["count"]
            if subtotal:
                steps.append(f"{describe} 的 {_fmt(row['count'])} 件：{_fmt(result.amount)} × {_fmt(row['count'])}"
                             f" = {_fmt(subtotal)}")
                sources.extend(result.sources)
            total += subtotal
        steps.append(f"{label or param.label}合计：{_fmt(total)}")
        return _Result(total, steps=steps, sources=sources)

    def _use(self, node, label) -> _Result:
        return self.node(self.rule.definitions[node["name"]])

    def _none(self, node, label) -> _Result:
        return _Result(None, node["outcome"], node["reason"])


def _is_number_text(text: str) -> bool:
    return _decimal(text) is not None


def _range_text(low: Decimal | None, high: Decimal | None, right: bool) -> str:
    lower = "" if low is None else f"{'>' if right else '≥'} {_fmt(low)}"
    upper = "" if high is None else f"{'≤' if right else '<'} {_fmt(high)}"
    return "落在 " + ("、".join(part for part in (lower, upper) if part) or "全部") + " 这一档"


# -- results ----------------------------------------------------------------------------

@dataclass(frozen=True)
class FeeOutcome:
    outcome: str
    amount: Decimal | None
    reason: str
    steps: tuple[str, ...]
    citations: tuple[str, ...]
    reminders: tuple[str, ...]
    missing: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "outcome_text": OUTCOME_TEXT[self.outcome],
                "amount": None if self.amount is None else str(self.amount), "reason": self.reason,
                "steps": list(self.steps), "citations": list(self.citations), "reminders": list(self.reminders),
                "missing": list(self.missing)}


def _cite(source: Mapping[str, Any]) -> str:
    return f"{source.get('document', '')} {source.get('location', '')}".strip()


def calculate(rule: Rule, values: Mapping[str, Any]) -> FeeOutcome:
    """One fee for the values people typed. Missing values give outcome MISSING with their names."""
    typed, missing = normalize_values(rule, values)
    if missing:
        return FeeOutcome("MISSING", None, "还缺少：" + "、".join(missing), (), (), rule.reminders, missing)
    try:
        result = _Evaluator(rule, typed).node(rule.formula)
    except _Missing as exc:
        return FeeOutcome("MISSING", None, f"还缺少：{exc.label}", (), (), rule.reminders, (exc.label,))
    citations = tuple(dict.fromkeys(_cite(source) for source in result.sources if _cite(source)))
    if result.amount is None:
        return FeeOutcome(result.outcome, None, result.reason, tuple(result.steps), citations, rule.reminders)
    amount = result.amount.quantize(CENT, rounding=ROUND_HALF_UP)
    steps = tuple(result.steps) + (f"合计 {amount}",)
    return FeeOutcome("AMOUNT", amount, "", steps, citations, rule.reminders)


@dataclass(frozen=True)
class QuoteRule:
    fee_kind: str
    name: str
    version: str
    rule: Rule


@dataclass(frozen=True)
class QuoteItem:
    fee_kind: str
    name: str
    version: str
    result: FeeOutcome
    counted: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"fee_kind": self.fee_kind, "name": self.name, "version": self.version, "counted": self.counted,
                "note": self.note, **self.result.as_dict()}


@dataclass(frozen=True)
class Quote:
    items: tuple[QuoteItem, ...]
    total: Decimal
    complete: bool
    missing: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"items": [item.as_dict() for item in self.items], "total": str(self.total), "complete": self.complete,
                "missing": list(self.missing)}


def shared_params(rules: Sequence[QuoteRule]) -> tuple[Param, ...]:
    """The parameters of several fees, each asked once; the same key must mean the same thing."""
    seen: dict[str, tuple[Param, str]] = {}
    for item in rules:
        for param in item.rule.params:
            if param.key in seen and seen[param.key][0].shape() != param.shape():
                other = seen[param.key][1]
                raise FeeRuleError("FEE_PARAM_CONFLICT",
                                   f"“{other}”和“{item.name}”都用参数“{param.key}”，但类型、单位或选项不一致，请先统一。")
            seen.setdefault(param.key, (param, item.name))
    return tuple(param for param, _ in seen.values())


def quote(rules: Sequence[QuoteRule], values: Mapping[str, Any]) -> Quote:
    """Several fees of one delivery. Fees in one group are not charged together: the larger counts.

    A group with a fee that has no amount cannot pick the larger one, so none of it is
    counted and the quote is marked incomplete.
    """
    shared_params(rules)
    results = [(item, calculate(item.rule, values)) for item in rules]
    counted = {index: result.outcome == "AMOUNT" for index, (_, result) in enumerate(results)}
    notes: dict[int, str] = {}
    groups: dict[str, list[int]] = {}
    for index, (item, _) in enumerate(results):
        if item.rule.group:
            groups.setdefault(item.rule.group, []).append(index)
    for group, members in groups.items():
        if len(members) < 2:
            continue
        undecided = [i for i in members if results[i][1].outcome != "AMOUNT"]
        if undecided:
            names = "、".join(results[i][0].name for i in undecided)
            for i in members:
                counted[i] = False
                notes[i] = f"“{group}”组内不可同时收取、取大；{names}没有确定金额，需人工判断取哪一项。"
            continue
        best = max(members, key=lambda i: (results[i][1].amount, -i))
        for i in members:
            if i != best:
                counted[i] = False
                notes[i] = f"与“{results[best][0].name}”不可同时收取（“{group}”组取大），按{results[best][0].name}收取。"
    items = tuple(QuoteItem(item.fee_kind, item.name, item.version, result, counted[index], notes.get(index, ""))
                  for index, (item, result) in enumerate(results))
    total = sum((item.result.amount for item in items if item.counted), Decimal("0"))
    complete = all(item.counted or (item.result.outcome == "AMOUNT" and item.note) for item in items)
    missing = tuple(dict.fromkeys(label for item in items for label in item.result.missing))
    return Quote(items, total, complete, missing)


# -- review aids ------------------------------------------------------------------------

def _primary(ref: Any) -> str | None:
    """The parameter a reference mostly stands for: itself, or the first choice of ``first``."""
    if isinstance(ref, str):
        return ref
    if isinstance(ref, Mapping) and isinstance(ref.get("first"), list) and ref["first"]:
        return _primary(ref["first"][0])
    return None


def _boundaries(node: Any, out: list[tuple[str, Decimal]], rule: Rule) -> None:
    """(reference, number) pairs where a tier or a numeric condition changes."""
    if isinstance(node, Mapping):
        if node.get("type") == "tiers" and _primary(node.get("by")):
            for tier in node.get("tiers", []):
                for key in ("over", "up_to"):
                    if tier.get(key) is not None:
                        out.append((_primary(node["by"]), Decimal(str(tier[key]))))
        value = node.get("value")
        if ("op" in node and _primary(node.get("left")) and node["op"] != "in" and not isinstance(value, bool)
                and _decimal(value) is not None):
            out.append((_primary(node["left"]), Decimal(str(value))))
        if node.get("type") == "use":
            _boundaries(rule.definitions.get(node.get("name")), out, rule)
        for value in node.values():
            if isinstance(value, (Mapping, list)):
                _boundaries(value, out, rule)
    elif isinstance(node, list):
        for item in node:
            _boundaries(item, out, rule)


def boundary_rows(rule: Rule, base: Mapping[str, Any], *, limit: int = 200) -> list[dict[str, Any]]:
    """Trial values just below, at and above every boundary, one parameter at a time, for reviewers."""
    found: list[tuple[str, Decimal]] = []
    _boundaries(rule.formula, found, rule)
    pieces = next((param for param in rule.params if param.type == "pieces"), None)
    rows, seen = [], set()
    for ref, point in found:
        if ref.startswith(_PIECE) and pieces is None:
            continue
        step = Decimal("1") if _is_integer_ref(rule, ref) else Decimal("0.01")
        for number in (point - step, point, point + step):
            if (ref, number) in seen or number < 0:
                continue
            seen.add((ref, number))
            values = dict(base)
            if ref.startswith(_PIECE):
                first = dict((base.get(pieces.key) or [{}])[0])
                first.update(count=1)
                first[ref[len(_PIECE):]] = str(number)
                values[pieces.key] = [first]
                name = next((f.label for f in pieces.fields if f.key == ref[len(_PIECE):]), ref)
                label = name if name.startswith("单件") else f"单件{name}"
            elif ref in {param.key for param in rule.params}:
                values[ref] = str(number)
                label = rule.param(ref).label
            else:
                continue
            try:
                outcome = calculate(rule, values)
                rows.append({"param": label, "value": _fmt(number), "outcome": outcome.outcome,
                             "amount": None if outcome.amount is None else str(outcome.amount),
                             "note": outcome.reason})
            except FeeRuleError as exc:
                rows.append({"param": label, "value": _fmt(number), "outcome": "INVALID", "amount": None,
                             "note": exc.message})
            if len(rows) >= limit:
                return rows
    rows.sort(key=lambda row: (row["param"], Decimal(row["value"])))
    return rows


def _is_integer_ref(rule: Rule, ref: str) -> bool:
    if ref.startswith(_PIECE):
        name = ref[len(_PIECE):]
        return name == "count" or any(f.key == name and f.integer for p in rule.params for f in p.fields)
    return any(param.key == ref and param.integer for param in rule.params)


__all__ = [
    "FeeOutcome", "NODE_TYPES", "NO_AMOUNT", "OUTCOME_TEXT", "PARAM_TYPES", "Param", "PieceField", "Quote", "QuoteItem",
    "QuoteRule", "Rule", "SCHEMA_VERSION", "boundary_rows", "calculate", "normalize_values", "parse_rule", "quote",
    "shared_params", "unsourced_blocks",
]
