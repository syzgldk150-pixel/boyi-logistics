"""Fee rules taken from the network manual, computed by code (C line, step C1: upstairs fee).

The manual's 上楼费 is not one formula but a set of rules; some situations have
no amount at all ("需协商" / "可不提供服务"). A rule set records every number with
the manual page it came from, an effective date filled in by a person (the
headquarters notice decides it, not the file date) and a status. Only an
``APPROVED`` rule set may be reported to people; the draft below was typed in
from the plan's reading of the V6.1 text layer and must be checked against the
original table before approval. Money is ``Decimal`` throughout and the final
amount is rounded half-up to the cent. Pure functions: no environment or I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping

RULE_STATUSES = ("DRAFT", "APPROVED")
OUTCOMES = ("AMOUNT", "NEGOTIATE", "NOT_PROVIDED", "MANUAL")
GOODS_TYPES = ("general", "mattress", "pallet")
CENT = Decimal("0.01")


class FeeRuleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Rate:
    value: Decimal
    page: int


@dataclass(frozen=True)
class FeeRuleSet:
    fee_kind: str
    version: str
    status: str
    effective_from: date | None
    source: Mapping[str, Any]
    rates: Mapping[str, Rate]
    floor_tiers: tuple[tuple[int, int, str], ...]  # (from floor, to floor, rate key)
    max_floor_without_elevator: int
    ambiguities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in RULE_STATUSES:
            raise ValueError("unknown rule status")
        if self.status == "APPROVED" and self.effective_from is None:
            raise ValueError("an approved rule set needs the effective date from the headquarters notice")


def _rate(value: str, page: int) -> Rate:
    return Rate(Decimal(value), page)


# Draft typed from 方案二 §3 (V6.1 第五篇第五章, pp. 50–53). Not approved: the
# table cells were split in the text layer and must be checked against the
# original before any amount is shown to people.
UPSTAIRS_V61_DRAFT = FeeRuleSet(
    fee_kind="upstairs", version="V6.1-草稿1", status="DRAFT", effective_from=None,
    source={"document": "融辉物流网络管理手册 V6.1", "chapter": "第五篇 第五章 送货上下楼服务收费标准", "pages": [50, 53]},
    rates={
        "base_fixed": _rate("10", 50), "base_per_kg": _rate("0.12", 50),
        "floor_3_4_per_kg": _rate("0.05", 50), "floor_5_6_per_kg": _rate("0.10", 50), "floor_7_per_kg": _rate("0.15", 50),
        "piece_50_100": _rate("10", 50), "piece_100_120": _rate("30", 50),
        "elevator_fixed": _rate("5", 50), "elevator_per_kg": _rate("0.06", 50),
    },
    floor_tiers=((2, 2, ""), (3, 4, "floor_3_4_per_kg"), (5, 6, "floor_5_6_per_kg"), (7, 7, "floor_7_per_kg")),
    max_floor_without_elevator=7,
    ambiguities=(
        "送到 1 楼是否收取上楼费，草稿未写明，按“需人工判断”处理。",
        "单件 100 公斤恰好落在“50–100”还是“100–120”档，需对照原文；计算按提问时由人分档的件数。",
        "床垫类、整托与木箱的收费表尚未录入。",
        "执行日期以总部通知为准，尚未填写。",
    ),
)


@dataclass(frozen=True)
class UpstairsInputs:
    settlement_weight_kg: Decimal
    elevator: bool
    floor: int
    goods_type: str = "general"
    pieces_50_100: int = 0
    pieces_100_120: int = 0
    pieces_over_120: int = 0
    total_pieces: int | None = None


@dataclass(frozen=True)
class FeeResult:
    outcome: str
    amount: Decimal | None
    steps: tuple[str, ...]
    citations: tuple[str, ...]
    reminders: tuple[str, ...]
    rule_version: str
    rule_status: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reportable(self) -> bool:
        """Only an approved rule set may give people an amount."""
        return self.rule_status == "APPROVED"


def select_rule(rules: Iterable[FeeRuleSet], *, fee_kind: str, business_date: date) -> FeeRuleSet:
    """The approved rule set in force on ``business_date``; none means nobody reports an amount."""
    candidates = [rule for rule in rules if rule.fee_kind == fee_kind and rule.status == "APPROVED"
                  and rule.effective_from is not None and rule.effective_from <= business_date]
    if not candidates:
        raise FeeRuleError("FEE_RULE_NOT_IN_FORCE", "这个日期没有已审核生效的收费规则，不能给出金额。")
    latest = max(rule.effective_from for rule in candidates)
    chosen = [rule for rule in candidates if rule.effective_from == latest]
    if len(chosen) != 1:
        raise FeeRuleError("FEE_RULE_AMBIGUOUS", "同一执行日期有多版已生效规则，请先处理规则冲突。")
    return chosen[0]


def check_inputs(inputs: UpstairsInputs) -> tuple[str, ...]:
    """Reasonableness checks; any problem stops the calculation."""
    problems = []
    weight = Decimal(str(inputs.settlement_weight_kg))
    if weight <= 0:
        problems.append("结算重量必须大于 0。")
    if inputs.goods_type not in GOODS_TYPES:
        problems.append("货物类型无效。")
    if inputs.floor < 1 or inputs.floor > 200:
        problems.append("楼层无效。")
    counts = (inputs.pieces_50_100, inputs.pieces_100_120, inputs.pieces_over_120)
    if any(type(count) is not int or count < 0 for count in counts):
        problems.append("分档件数必须是 0 或正整数。")
    else:
        heavy = sum(counts)
        if inputs.total_pieces is not None and heavy > inputs.total_pieces:
            problems.append(f"分档件数合计 {heavy} 超过整票件数 {inputs.total_pieces}。")
        minimum = Decimal(50 * inputs.pieces_50_100 + 100 * inputs.pieces_100_120 + 120 * inputs.pieces_over_120)
        if weight > 0 and minimum > weight:
            problems.append(f"按分档件数，单件重量合计至少 {minimum} 公斤，超过整票结算重量 {weight} 公斤，请核对。")
    return tuple(problems)


def calculate_upstairs(rule: FeeRuleSet, inputs: UpstairsInputs) -> FeeResult:
    if rule.fee_kind != "upstairs":
        raise FeeRuleError("FEE_RULE_KIND", "这不是上楼费规则。")
    problems = check_inputs(inputs)
    if problems:
        raise FeeRuleError("FEE_INPUT_INVALID", "；".join(problems))
    weight = Decimal(str(inputs.settlement_weight_kg))
    rates, pages = rule.rates, sorted({rate.page for rate in rule.rates.values()})
    cite = (f"{rule.source['document']} {rule.source['chapter']}（第 {pages[0]} 页）",)
    reminders = ("如同时产生卸货费，两者取大值，不同时收取。",)

    def result(outcome: str, steps: Iterable[str], amount: Decimal | None = None) -> FeeResult:
        return FeeResult(outcome, None if amount is None else amount.quantize(CENT, rounding=ROUND_HALF_UP),
                         tuple(steps), cite, reminders, rule.version, rule.status)

    if inputs.goods_type != "general":
        kind = "床垫类" if inputs.goods_type == "mattress" else "整托或木箱"
        return result("MANUAL", (f"{kind}的收费表尚未录入规则，需人工按手册判断。",))
    if inputs.pieces_over_120:
        return result("NEGOTIATE", (f"有 {inputs.pieces_over_120} 件单件超过 120 公斤：手册规定可不提供服务或协商，没有固定金额。",))
    if inputs.elevator:
        fixed, per_kg = rates["elevator_fixed"].value, rates["elevator_per_kg"].value
        amount = fixed + per_kg * weight
        return result("AMOUNT", (f"有电梯：{fixed} + {per_kg}×{weight} = {amount}",), amount)
    if inputs.floor == 1:
        return result("MANUAL", ("送到 1 楼：规则未写明是否收取上楼费，需人工判断。",))
    if inputs.floor > rule.max_floor_without_elevator:
        return result("NEGOTIATE", (f"无电梯送 {inputs.floor} 楼（{rule.max_floor_without_elevator} 楼以上）：可不提供服务或协商，没有固定金额。",))
    base = rates["base_fixed"].value + rates["base_per_kg"].value * weight
    steps = [f"基础 {rates['base_fixed'].value} + {rates['base_per_kg'].value}×{weight} = {base}"]
    tier = next(key for low, high, key in rule.floor_tiers if low <= inputs.floor <= high)
    floor_fee = Decimal("0")
    if tier:
        floor_fee = rates[tier].value * weight
        steps.append(f"楼层加收（{inputs.floor} 楼）{rates[tier].value}×{weight} = {floor_fee}")
    else:
        steps.append(f"楼层加收（{inputs.floor} 楼）不收")
    piece_fee = rates["piece_50_100"].value * inputs.pieces_50_100 + rates["piece_100_120"].value * inputs.pieces_100_120
    if piece_fee:
        steps.append(f"单件加收 {rates['piece_50_100'].value}×{inputs.pieces_50_100} + "
                     f"{rates['piece_100_120'].value}×{inputs.pieces_100_120} = {piece_fee}")
    total = base + floor_fee + piece_fee
    steps.append(f"合计 {total.quantize(CENT, rounding=ROUND_HALF_UP)}")
    return result("AMOUNT", steps, total)


def boundary_table(rule: FeeRuleSet, *, weight: Decimal = Decimal("200")) -> list[dict[str, Any]]:
    """Trial values on both sides of each boundary, for the person reviewing a rule set."""
    rows = []
    for elevator in (True, False):
        for floor in (1, 2, 3, 4, 5, 6, 7, 8):
            for pieces in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)):
                outcome = calculate_upstairs(rule, UpstairsInputs(weight, elevator, floor, "general", *pieces))
                rows.append({"elevator": elevator, "floor": floor, "pieces": pieces, "outcome": outcome.outcome,
                             "amount": None if outcome.amount is None else str(outcome.amount)})
    return rows


__all__ = [
    "FeeResult", "FeeRuleError", "FeeRuleSet", "GOODS_TYPES", "OUTCOMES", "RULE_STATUSES", "Rate",
    "UPSTAIRS_V61_DRAFT", "UpstairsInputs", "boundary_table", "calculate_upstairs", "check_inputs", "select_rule",
]
