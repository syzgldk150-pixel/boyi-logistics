"""Fees in conversation by the rules approved in 业务手册 → 计算规则 (报价 P3, 2026-11-20).

Replaces the upstairs-only action (C3). The model only picks the action and copies the
waybill number. Code does the rest:

* reads the Ronghui waybill live (``query_waybill_detail``): billing date (``REGISTER_DATE``,
  picks the rule versions), settlement weight, piece count, actual weight, the upstairs flag
  and the remark; a delivery without a waybill uses today's rules;
* picks the fees by their names in the person's words (“上楼费和卸货费”, “全部”), and lists
  the approved ones when none is named;
* fills each rule parameter from the waybill (``prefill``) or from the person's words by the
  parameter's own label, unit and options (``parse_values``: floor and elevator keep their
  dedicated readers); what is still needed is asked in one list, never guessed;
* computes with ``shared.fee_engine.quote`` (fees in one group: the larger counts) and keeps
  the quote in ``agent_fee_quotes`` like the back office.

The waybill has no weight per piece, so several pieces are taken at their average weight and
the reply says so plainly next to the amount.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from shared.fee_engine import OUTCOME_TEXT

STATE_TTL_SECONDS = 900
_DIGITS = re.compile(r"(\d{1,3})\s*(?:楼|层|f\b)", re.IGNORECASE)
_CHINESE = re.compile(r"([一二两三四五六七八九十]{1,3})\s*(?:楼|层)")
_NO_ELEVATOR = re.compile(r"(?:没有|没|无|不带|不是|未配|未装)\s*(?:电梯|电梯房)|步梯|爬楼梯|楼梯房")
_ELEVATOR = re.compile(r"(?:有|带|配)\s*电梯|电梯房|坐电梯|乘电梯")
_UPSTAIRS_WORDS = re.compile(r"上楼|送楼上|搬上楼|送上楼")
_CHINESE_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_number(text: str) -> int | None:
    if text == "十":
        return 10
    if "十" in text:
        tens, _, ones = text.partition("十")
        if len(text.split("十")) != 2:
            return None
        high = _CHINESE_DIGITS.get(tens, 1 if tens == "" else None)
        low = _CHINESE_DIGITS.get(ones, 0 if ones == "" else None)
        return None if high is None or low is None else high * 10 + low
    return _CHINESE_DIGITS.get(text) if len(text) == 1 else None


def parse_floor(message: str) -> int | None:
    """The floor the person said (5楼, 5层, 五楼, 十二楼); several different floors mean none."""
    text = unicodedata.normalize("NFKC", message or "")
    found = {int(item) for item in _DIGITS.findall(text)}
    found |= {value for value in (_chinese_number(item) for item in _CHINESE.findall(text)) if value}
    return found.pop() if len(found) == 1 and 1 <= next(iter(found)) <= 200 else None


def parse_elevator(message: str) -> bool | None:
    text = unicodedata.normalize("NFKC", message or "")
    no, yes = bool(_NO_ELEVATOR.search(text)), bool(_ELEVATOR.search(_NO_ELEVATOR.sub("", text)))
    if no == yes:
        return None  # neither said, or both: ask rather than guess
    return yes


@dataclass(frozen=True)
class WaybillFacts:
    tracking_number: str
    billing_date: date
    settlement_weight: Decimal
    pieces: int
    actual_weight: Decimal | None
    upstairs_ticked: bool
    remark: str

    def describe(self) -> str:
        return (f"开单日期 {self.billing_date.isoformat()} · 结算重量 {self.settlement_weight} 公斤 · 件数 {self.pieces}"
                + (f" · 实际重量 {self.actual_weight} 公斤" if self.actual_weight is not None else ""))


class FeeActionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    current = payload
    for _ in range(5):
        if isinstance(current, list):
            return [row for row in current if isinstance(row, Mapping)]
        if not isinstance(current, Mapping):
            return []
        current = next((current[key] for key in ("records", "rows", "items", "data") if key in current), None)
    return []


def waybill_facts(tracking_number: str, payload: Any) -> WaybillFacts:
    """The facts a fee needs, from one live ``query_waybill_detail`` result; anything missing fails."""
    matches = [row for row in _rows(payload)
               if str(row.get("tracking_number") or row.get("requested_bill_code") or "").upper() == tracking_number]
    if len(matches) != 1:
        raise FeeActionError("WAYBILL_NOT_FOUND", f"融辉没有查到运单 {tracking_number}，请核对单号。")
    row = matches[0]
    billing = str(row.get("billing_date") or "")[:10]
    try:
        billing_date = date.fromisoformat(billing)
    except ValueError as exc:
        raise FeeActionError("WAYBILL_FIELD_MISSING", f"运单 {tracking_number} 没有读到开单日期，不能选择收费规则。") from exc
    weight = _decimal(row.get("settlement_weight"))
    if weight is None or weight <= 0:
        raise FeeActionError("WAYBILL_FIELD_MISSING", f"运单 {tracking_number} 没有读到结算重量，不能计算。")
    pieces = row.get("quantity")
    try:
        pieces = int(str(pieces).strip())
    except (TypeError, ValueError):
        pieces = 0
    if pieces < 1:
        raise FeeActionError("WAYBILL_FIELD_MISSING", f"运单 {tracking_number} 没有读到件数，不能计算。")
    actual = _decimal(row.get("actual_weight"))
    return WaybillFacts(tracking_number, billing_date, weight, pieces, actual if actual and actual > 0 else None,
                        str(row.get("upstairs_service") or "").strip() in {"1", "true", "True"},
                        str(row.get("remarks") or row.get("remark") or "").strip())


def piece_rows(facts: WaybillFacts) -> tuple[list[dict[str, Any]], str]:
    """The waybill's pieces as rule input. The waybill has no weight per piece, so several pieces
    are taken at their average weight and the reply says so; one piece uses its actual weight."""
    if facts.pieces == 1:
        if facts.actual_weight is not None:
            return [{"count": 1, "weight": str(facts.actual_weight)}], ""
        return ([{"count": 1, "weight": str(facts.settlement_weight)}],
                "单上没有实际重量，单件重量按结算重量计算，请人工核对单件加收。")
    total = facts.actual_weight if facts.actual_weight is not None else facts.settlement_weight
    average = (total / facts.pieces).quantize(Decimal("0.01"))
    return ([{"count": facts.pieces, "weight": str(average)}],
            f"单上没有每件重量：这票 {facts.pieces} 件共 {total} 公斤，按平均每件 {average} 公斤计算单件加收；"
            "如有超重件请人工按实际件重补算。")


def reminders(facts: WaybillFacts) -> list[str]:
    notes = []
    if not facts.upstairs_ticked and _UPSTAIRS_WORDS.search(facts.remark):
        notes.append(f"单上没有勾选上楼服务，但备注写了：“{facts.remark[:80]}”，请核对是否应收上楼费。")
    return notes


# -- fee quote (报价 P3, 2026-11-20) -----------------------------------------------------
#
# Any fees approved in 业务手册 → 计算规则, for one waybill or one delivery the person
# describes. Which fees, and every parameter, come from code: fee names matched in the
# person's words; the waybill's billing date, settlement weight, pieces and actual weight;
# the rest read from the person's words by the parameter's own label, unit and options
# (``parse_values``). Whatever is still needed is asked in one list, never guessed.

_NUMBER_TEXT = r"(\d+(?:\.\d+)?)"
_UNIT_WORDS = {"kg": "kg|公斤|千克", "公斤": "kg|公斤|千克", "千克": "kg|公斤|千克", "件": "件", "个": "个", "楼": "楼|层",
               "层": "楼|层", "级": "级|个台阶|台阶", "米": "米|m", "m": "米|m", "次": "次", "票": "票", "托": "托"}
_ALL = re.compile(r"全部|所有|都算|每一项")
_ADD = re.compile(r"再加|加上|还有|另外|再算")
_NEGATION = re.compile(r"(?:没有|没|无|不|非|否)\s*$")


@dataclass
class QuoteState:
    tracking_number: str | None
    facts: WaybillFacts | None
    fee_kinds: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    piece_note: str = ""
    asked: list[str] = field(default_factory=list)

    @property
    def billing_date(self) -> date | None:
        return self.facts.billing_date if self.facts else None


def _core(name: str) -> str:
    return re.sub(r"（.*?）|\(.*?\)", "", name).strip()


def fee_aliases(name: str) -> list[str]:
    """Words that name a fee: 上楼费 → 上楼费、上楼；拆托/拆木架木箱 → 拆托、拆木架木箱 ..."""
    words = {name, _core(name)}
    for part in re.split(r"[/／、]", _core(name)):
        part = part.strip().removesuffix("费")
        if not part:
            continue
        words.add(part)
        # People say the start of a long name: 拆木架木箱 → 拆木架、二次上门签收 → 二次上门.
        words |= {part[:end] for end in range(3, len(part))}
        # One verb over two things: 拆木架木箱 → 拆木架、拆木箱.
        if len(part) >= 5 and len(part) % 2:
            words |= {part[0] + part[index:index + 2] for index in range(1, len(part), 2)}
    return sorted((word for word in words if len(word) >= 2), key=lambda word: (-len(word), word))


def match_fees(message: str, rules: Sequence[Any], *, none_chosen: bool = False) -> list[str]:
    """Fees named in the message, plus those its floors/yes-no/choices imply. With ``none_chosen``
    (nothing picked yet in this thread) the implied ones count on their own: "有电梯 上2楼" means
    上楼费. Once fees are picked, a bare "那3楼呢" must not replace them, so it needs a named fee."""
    text = unicodedata.normalize("NFKC", message or "")
    if _ALL.search(text):
        return [rule.fee_kind for rule in rules]
    named = [rule.fee_kind for rule in rules if any(alias in text for alias in fee_aliases(rule.name))]
    return [*named, *implied_fees(text, rules, named)] if named or none_chosen else []


def implied_fees(message: str, rules: Sequence[Any], named: Sequence[str]) -> list[str]:
    """Fees not named but described alongside a named one: "拆木架，有电梯，5楼" also means 上楼费,
    because 有电梯 / 5楼 answer parameters only that fee has. Only floors, yes/no and choices
    count (a bare "3件" could belong to many fees)."""
    owners: dict[str, set[str]] = {}
    for rule in rules:
        for param in rule.rule.params:
            owners.setdefault(param.key, set()).add(rule.fee_kind)
    implied = []
    for rule in rules:
        if rule.fee_kind in named:
            continue
        own = [param for param in rule.rule.params if owners[param.key] == {rule.fee_kind}
               and (param.key == "floor" or param.type in {"bool", "choice"})]
        if own and parse_values(own, message):
            implied.append(rule.fee_kind)
    return implied


def _unit_pattern(unit: str) -> str | None:
    unit = (unit or "").strip()
    if not unit:
        return None
    return _UNIT_WORDS.get(unit, re.escape(unit))


def _label_core(label: str) -> str:
    return re.sub(r"^(?:是否|有无|需要|要)", "", _core(label)).strip()


def parse_values(params: Sequence[Any], message: str, *, asked: Sequence[str] = ()) -> dict[str, Any]:
    """Values for rule parameters said in one message, read by each parameter's label, unit and
    options. A value that two parameters could claim is left out (asked instead)."""
    text = unicodedata.normalize("NFKC", message or "")
    found: dict[str, Any] = {}
    numbers = [param for param in params if param.type == "number"]
    for param in params:
        if param.key == "floor":
            floor = parse_floor(text)
            if floor is not None:
                found["floor"] = floor
            continue
        if param.key == "elevator" and param.type == "bool":
            elevator = parse_elevator(text)
            if elevator is not None:
                found["elevator"] = elevator
            continue
        core = _label_core(param.label)
        if param.type == "number":
            by_label = re.findall(re.escape(core) + r"\s*(?:是|为|有|共|:|：)?\s*" + _NUMBER_TEXT, text) if core else []
            if len(set(by_label)) == 1:
                found[param.key] = by_label[0]
                continue
            unit = _unit_pattern(param.unit)
            sharing = [other for other in numbers if _unit_pattern(other.unit) == unit and other.key not in found]
            if unit and len(sharing) == 1:
                by_unit = re.findall(_NUMBER_TEXT + r"\s*(?:" + unit + ")", text)
                if len(set(by_unit)) == 1:
                    found[param.key] = by_unit[0]
        elif param.type == "bool":
            if core and core in text:
                before = text[:text.index(core)]
                found[param.key] = not bool(_NEGATION.search(before[-4:]))
        elif param.type == "choice":
            hits = {value for value, label in param.options
                    if (_core(label) and _core(label) in text) or re.search(rf"(?<![a-z]){re.escape(value)}(?![a-z])", text)}
            if len(hits) == 1:
                found[param.key] = hits.pop()
        elif param.type == "pieces":
            count = re.findall(r"(\d+)\s*件", text)
            weight = re.findall(r"每件\s*" + _NUMBER_TEXT + r"\s*(?:kg|公斤|千克)", text)
            if len(count) == 1 and len(weight) == 1:
                found[param.key] = [{"count": int(count[0]), "weight": weight[0]}]
    # A bare answer to the one thing asked: "5" after "送到几楼？"; "有"/"无" after one yes/no question.
    pending = [param for param in params if param.key in asked and param.key not in found]
    if len(pending) == 1:
        param = pending[0]
        bare = text.strip().rstrip("。.!！")
        if param.type == "number" and re.fullmatch(_NUMBER_TEXT, bare):
            found[param.key] = int(bare) if param.key == "floor" else bare
        elif param.type == "bool" and bare in {"有", "是", "要", "需要", "对"}:
            found[param.key] = True
        elif param.type == "bool" and bare in {"无", "没有", "没", "否", "不", "不要", "不用"}:
            found[param.key] = False
    return found


def prefill(params: Sequence[Any], facts: WaybillFacts | None) -> tuple[dict[str, Any], str]:
    """Parameters the waybill itself answers (its settlement weight, pieces, actual weight)."""
    if facts is None:
        return {}, ""
    values, note = {}, ""
    for param in params:
        if param.prefill == "settlement_weight":
            values[param.key] = str(facts.settlement_weight)
        elif param.prefill == "actual_weight" and facts.actual_weight is not None:
            values[param.key] = str(facts.actual_weight)
        elif param.prefill == "pieces":
            if param.type == "pieces":
                values[param.key], note = piece_rows(facts)
            else:
                values[param.key] = facts.pieces
    return values, note


def ask_text(param: Any) -> str:
    if param.type == "bool":
        return f"{param.label}（有/无）"
    if param.type == "choice":
        return f"{param.label}（{' / '.join(label for _, label in param.options)}）"
    if param.type == "pieces":
        return f"{param.label}（例如“2件 每件80公斤”）"
    if param.key == "floor":
        return "送到几楼（例如“5楼”；步梯没有楼层时说台阶数，例如“48级”）"
    return f"{param.label}（数字{('，单位 ' + param.unit) if param.unit else ''}）"


def _ref_key(ref: Any) -> str | None:
    """The parameter a value reference reads: itself, or inside ``ceil`` / ``excess``."""
    if isinstance(ref, str):
        return None if ref.startswith("piece.") else ref
    if isinstance(ref, Mapping):
        for key in ("ceil", "excess"):
            if key in ref:
                return _ref_key(ref[key])
    return None


def first_groups(rules: Sequence[Any]) -> list[tuple[str, ...]]:
    """Parameters a rule takes “the first one given” of (送到几楼 或 步梯台阶数): each is optional
    alone, but one of them is needed."""
    groups: list[tuple[str, ...]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            first = value.get("first")
            if isinstance(first, list):
                keys = tuple(key for key in (_ref_key(item) for item in first) if key)
                if len(keys) == len(first):
                    groups.append(keys)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for rule in rules:
        walk(rule.rule.formula)
        walk(dict(rule.rule.definitions))
    return list(dict.fromkeys(groups))


def _mention_words(label: str) -> list[str]:
    """What a person says to bring up a count parameter: 拆木架/木箱件数 → 拆木架、木箱."""
    words = []
    for part in re.split(r"[/／、]", _label_core(label)):
        part = re.sub(r"(?:件数|个数|数量|次数|数)$", "", part.strip())
        if len(part) >= 2:
            words.append(part)
    return words


def mentioned(params: Sequence[Any], message: str, values: Mapping[str, Any]) -> list[Any]:
    """Number parameters with a default that the message brings up without giving a value
    ("拆木架" but not how many): asked rather than quietly taken at their default."""
    text = unicodedata.normalize("NFKC", message or "")
    found = []
    for param in params:
        if param.type != "number" or param.default is None or param.key in values:
            continue
        for word in _mention_words(param.label):
            at = text.find(word)
            if at >= 0 and not _NEGATION.search(text[:at][-4:]):
                found.append(param)
                break
    return found


def needed(params: Sequence[Any], values: Mapping[str, Any], groups: Sequence[tuple[str, ...]] = ()) -> list[Any]:
    """Required parameters not given yet, plus the first of each “one of these” group with none given."""
    by_key = {param.key: param for param in params}
    wanted = [param for param in params if param.required and param.default is None and param.key not in values]
    for group in groups:
        if not any(key in values for key in group) and group[0] in by_key and by_key[group[0]] not in wanted:
            wanted.insert(0, by_key[group[0]])
    return wanted


def _value_text(param: Any, value: Any) -> str:
    if param.type == "bool":
        return "是" if value else "否"
    if param.type == "choice":
        return next((label for key, label in param.options if key == value), str(value))
    if param.type == "pieces":
        return "；".join(f"{row.get('count')} 件" + (f" 每件 {row['weight']} 公斤" if row.get("weight") else "") for row in value)
    return f"{value}{(' ' + param.unit) if param.unit else ''}"


def render_quote(state: QuoteState, rules: Sequence[Any], unavailable: Sequence[Mapping[str, Any]], result: Any,
                 params: Sequence[Any], when: date) -> str:
    head = f"运单 {state.tracking_number} 费用" if state.tracking_number else "费用估算"
    lines = [head]
    if state.facts:
        lines.append(state.facts.describe())
    else:
        lines.append(f"没有运单，按今天（{when.isoformat()}）生效的规则计算。")
    shown = [f"{param.label} {_value_text(param, state.values[param.key])}" for param in params if param.key in state.values]
    if shown:
        lines.append("按：" + " · ".join(shown))
    for item in result.items:
        outcome = item.result
        if outcome.outcome == "AMOUNT":
            amount = f"{outcome.amount} 元"
        else:
            amount = OUTCOME_TEXT.get(outcome.outcome, outcome.outcome) + (f"（{outcome.reason}）" if outcome.reason else "")
        lines.append(f"· {item.name}：{amount}{'' if item.counted else '（不计入合计）'}  规则 {item.version}")
        lines.extend(f"    {step}" for step in outcome.steps)
        if item.note:
            lines.append(f"    {item.note}")
    lines.append(f"合计：{result.total} 元" + ("" if result.complete else "（有费用需人工判断，合计不完整）"))
    for item in unavailable:
        lines.append(f"未计算：{item['name']}——{item['reason']}")
    if state.piece_note:
        lines.append(state.piece_note)
    citations = list(dict.fromkeys(cite for item in result.items for cite in item.result.citations))
    if citations:
        lines.append("出处：" + "；".join(citations))
    reminders_ = list(dict.fromkeys([*(text for item in result.items for text in item.result.reminders), *state.notes]))
    lines.extend(f"提醒：{text}" for text in reminders_)
    return "\n".join(lines)


__all__ = ["FeeActionError", "QuoteState", "WaybillFacts", "ask_text", "fee_aliases", "first_groups", "implied_fees", "match_fees", "mentioned", "needed",
           "parse_elevator", "parse_floor", "parse_values", "piece_rows", "prefill", "reminders", "render_quote",
           "waybill_facts"]
