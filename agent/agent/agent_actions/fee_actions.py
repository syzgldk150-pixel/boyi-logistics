"""Upstairs fee for one waybill in conversation (C line, step C3).

The model only picks the action and copies the waybill number. Code does the rest:

* reads the Ronghui waybill live (``query_waybill_detail``): billing date
  (``REGISTER_DATE``, picks the rule version), settlement weight
  (``SETTLEMENT_WEIGHT``, confirmed by the user), piece count (``PIECE_NUMBER``,
  the waybill's own count, confirmed by the user), actual weight, the upstairs
  service flag and the remark;
* reads the floor and the elevator only from the person's own words (digits or
  Chinese numerals before 楼/层; 有/无电梯). The waybill has no floor or elevator
  fact, so a missing one is asked, never guessed;
* uses only rule versions approved in 业务手册 → 计算规则, chosen by billing date;
* reminds when the upstairs service is not ticked but the remark mentions 上楼.

Single-piece surcharges need each piece's weight, which the waybill does not
have for multi-piece shipments; several pieces are taken at their average weight
and the reply says so plainly next to the amount. The rule itself is block-built
(``shared.fee_engine``); the robot fills its upstairs parameters from the waybill
and the person's words, and points to 业务手册 → 报价 for anything it cannot ask.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from shared.fee_engine import OUTCOME_TEXT, calculate
from shared.handbook_fee_rules import FeeRuleError, rule_from_record, select_rule

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
                        str(row.get("remark") or "").strip())


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
    notes.append("按普通货物计算；床垫类、整托或木箱需人工判断。")
    return notes


@dataclass
class UpstairsState:
    tracking_number: str
    facts: WaybillFacts
    floor: int | None = None
    elevator: bool | None = None
    notes: list[str] = field(default_factory=list)


def missing_question(state: UpstairsState) -> str:
    wanted = [text for text, missing in (("送到几楼", state.floor is None), ("有没有电梯", state.elevator is None)) if missing]
    return (f"运单 {state.tracking_number}：{state.facts.describe()}。\n单上没有楼层和电梯信息，请告诉我{'、'.join(wanted)}"
            "（例如“5楼 无电梯”）。")


def render(state: UpstairsState, rules: Callable[[], Sequence[Mapping[str, Any]]]) -> str:
    facts = state.facts
    head = (f"运单 {facts.tracking_number} 上楼费\n{facts.describe()} · 送 {state.floor} 楼 · "
            f"{'有电梯' if state.elevator else '无电梯'}")
    try:
        rule = select_rule([rule_from_record(record) for record in rules()], fee_kind="upstairs",
                           business_date=facts.billing_date)
    except FeeRuleError as exc:
        if exc.code == "FEE_RULE_NOT_IN_FORCE":
            return (f"{head}\n开单日期 {facts.billing_date.isoformat()} 没有已审核生效的上楼费规则，不能给出金额。"
                    "规则在后台“业务手册 → 计算规则”审核生效后才会使用。")
        return f"{head}\n收费规则有问题，不能给出金额：{exc.message}"
    pieces, piece_note = piece_rows(facts)
    try:
        result = calculate(rule.content, {"settlement_weight": str(facts.settlement_weight), "floor": int(state.floor),
                                          "elevator": bool(state.elevator), "goods_type": "general", "pieces": pieces})
    except FeeRuleError as exc:
        return f"{head}\n不能计算：{exc.message}"
    if result.outcome == "MISSING":
        return f"{head}\n这版上楼费规则还需要：{'、'.join(result.missing)}，机器人暂时问不到，请在后台“业务手册 → 报价”里计算。"
    lines = [head, f"规则：{rule.version}（执行日期 {rule.effective_from.isoformat()}）"]
    if result.outcome == "AMOUNT":
        lines.append(f"上楼费：{result.amount} 元")
    else:
        lines.append(f"上楼费：{OUTCOME_TEXT.get(result.outcome, result.outcome)}" + (f"（{result.reason}）" if result.reason else ""))
    lines.extend(f"· {step}" for step in result.steps)
    if piece_note:
        lines.append(piece_note)
    if result.citations:
        lines.append("出处：" + "；".join(result.citations))
    lines.extend(f"提醒：{item}" for item in (*result.reminders, *state.notes))
    return "\n".join(lines)


__all__ = ["FeeActionError", "UpstairsState", "WaybillFacts", "missing_question", "parse_elevator", "parse_floor",
           "piece_rows", "reminders", "render", "waybill_facts"]
