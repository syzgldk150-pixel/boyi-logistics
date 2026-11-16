"""Fee rules from the network manual: the structure is code, the numbers come from 业务手册 → 计算规则.

The manual's 上楼费 is not one formula but a set of rules, and some situations
have no amount at all ("需协商" / "需人工判断"). Code owns how a fee is computed
(the structure below); people fill in every number, tier, effective date and
source in the back office, and a super administrator approves a version before
it is used. Changing numbers or publishing a new version therefore needs no
release; only a new kind of fee or a new structure does.

A rule version's ``content`` is plain JSON so it can be stored and edited.
``parse_upstairs_content`` validates it completely and fails with every problem
listed in Chinese; nothing is guessed or defaulted. Only an ``APPROVED`` version
whose effective date is not after the waybill's billing (开单) date may give
people an amount. Money is ``Decimal`` throughout and the final amount is
rounded half-up to the cent. Pure functions: no environment or I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

FEE_KINDS = {"upstairs": "上楼费"}
RULE_STATUSES = ("DRAFT", "PENDING", "APPROVED", "RETIRED")
OUTCOMES = ("AMOUNT", "NEGOTIATE", "NOT_PROVIDED", "MANUAL")
GOODS_TYPES = ("general", "mattress", "pallet")
FIRST_FLOOR_RULES = {"MANUAL": "需人工判断", "NO_CHARGE": "不收上楼费", "BASE": "只收基础费"}
CENT = Decimal("0.01")
MAX_MONEY = Decimal("100000")
MAX_FLOOR = 200
# Which date of a waybill picks the rule version: its billing (开单) date,
# confirmed by the user on 2026-11-16. Callers pass that date to select_rule.
VERSION_DATE_BASIS = "billing_date"


class FeeRuleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Source:
    document: str
    location: str
    link: str = ""

    def cite(self) -> str:
        return f"{self.document} {self.location}".strip()


@dataclass(frozen=True)
class Charge:
    fixed: Decimal
    per_kg: Decimal
    source: Source


@dataclass(frozen=True)
class FloorTier:
    floor_from: int
    floor_to: int
    per_kg: Decimal
    source: Source


@dataclass(frozen=True)
class PieceTier:
    over_kg: Decimal
    up_to_kg: Decimal
    per_piece: Decimal
    source: Source

    @property
    def label(self) -> str:
        return f"单件 {self.over_kg}–{self.up_to_kg} 公斤"


@dataclass(frozen=True)
class UpstairsContent:
    base: Charge
    elevator: Charge
    floor_tiers: tuple[FloorTier, ...]
    max_floor_without_elevator: int
    max_floor_source: Source
    first_floor: str
    piece_tiers: tuple[PieceTier, ...]
    piece_limit_kg: Decimal
    piece_limit_source: Source
    reminders: tuple[str, ...]


@dataclass(frozen=True)
class FeeRuleSet:
    fee_kind: str
    version: str
    status: str
    effective_from: date | None
    content: UpstairsContent

    def __post_init__(self) -> None:
        if self.status not in RULE_STATUSES:
            raise ValueError("unknown rule status")
        if self.status == "APPROVED" and self.effective_from is None:
            raise ValueError("an approved rule set needs the effective date from the headquarters notice")


# -- content parsing ------------------------------------------------------------------

class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, text: str) -> None:
        self.items.append(text)

    def money(self, value: Any, label: str) -> Decimal:
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            self.add(f"{label}不是有效数字。")
            return Decimal("0")
        if not amount.is_finite() or amount < 0 or amount > MAX_MONEY:
            self.add(f"{label}必须是 0 到 {MAX_MONEY} 之间的数字。")
            return Decimal("0")
        if amount.as_tuple().exponent < -4:
            self.add(f"{label}最多保留 4 位小数。")
        return amount

    def integer(self, value: Any, label: str, low: int, high: int) -> int:
        if type(value) is not int or not low <= value <= high:
            self.add(f"{label}必须是 {low} 到 {high} 之间的整数。")
            return low
        return value

    def text(self, value: Any, label: str, limit: int, required: bool = True) -> str:
        if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
            self.add(f"{label}{'必须填写，且' if required else ''}不超过 {limit} 字。")
            return ""
        return value.strip()

    def source(self, value: Any, label: str) -> Source:
        if not isinstance(value, Mapping):
            self.add(f"{label}缺少出处。")
            return Source("", "")
        document = self.text(value.get("document"), f"{label}的出处文档", 100)
        location = self.text(value.get("location"), f"{label}的页码或条款", 100)
        link = self.text(value.get("link", ""), f"{label}的链接", 500, required=False)
        if link and not link.startswith("https://"):
            self.add(f"{label}的链接必须以 https:// 开头。")
        return Source(document, location, link)

    def charge(self, value: Any, label: str) -> Charge:
        if not isinstance(value, Mapping):
            self.add(f"缺少{label}。")
            return Charge(Decimal("0"), Decimal("0"), Source("", ""))
        return Charge(self.money(value.get("fixed"), f"{label}固定金额"),
                      self.money(value.get("per_kg"), f"{label}每公斤单价"), self.source(value.get("source"), label))


def parse_upstairs_content(content: Any) -> UpstairsContent:
    """Validate an upstairs rule version completely; every problem is reported at once."""
    p = _Problems()
    if not isinstance(content, Mapping):
        raise FeeRuleError("FEE_RULE_CONTENT_INVALID", "规则内容格式无效。")
    base = p.charge(content.get("base"), "基础费")
    elevator = p.charge(content.get("elevator"), "有电梯收费")
    max_floor = p.integer(content.get("max_floor_without_elevator"), "无电梯最高楼层", 2, MAX_FLOOR)
    max_floor_source = p.source(content.get("max_floor_source"), "无电梯最高楼层")
    tiers = []
    raw_tiers = content.get("floor_tiers")
    if not isinstance(raw_tiers, list) or not raw_tiers or len(raw_tiers) > 50:
        p.add("楼层加收至少要有一档，最多 50 档。")
        raw_tiers = []
    for index, raw in enumerate(raw_tiers, 1):
        label = f"楼层加收第 {index} 档"
        raw = raw if isinstance(raw, Mapping) else {}
        tiers.append(FloorTier(p.integer(raw.get("floor_from"), f"{label}起始楼层", 2, MAX_FLOOR),
                               p.integer(raw.get("floor_to"), f"{label}结束楼层", 2, MAX_FLOOR),
                               p.money(raw.get("per_kg"), f"{label}每公斤单价"), p.source(raw.get("source"), label)))
    expected = 2
    for index, tier in enumerate(tiers, 1):
        if tier.floor_from != expected or tier.floor_to < tier.floor_from:
            p.add(f"楼层加收第 {index} 档应从 {expected} 楼开始，且结束楼层不小于起始楼层（各档要从 2 楼起连续、不重叠）。")
            break
        expected = tier.floor_to + 1
    else:
        if tiers and tiers[-1].floor_to != max_floor:
            p.add(f"楼层加收最后一档应到无电梯最高楼层 {max_floor} 楼。")
    first_floor = content.get("first_floor")
    if first_floor not in FIRST_FLOOR_RULES:
        p.add("送到 1 楼的处理方式无效。")
        first_floor = "MANUAL"
    pieces = []
    raw_pieces = content.get("piece_tiers")
    if not isinstance(raw_pieces, list) or len(raw_pieces) > 20:
        p.add("单件加收最多 20 档。")
        raw_pieces = []
    for index, raw in enumerate(raw_pieces, 1):
        label = f"单件加收第 {index} 档"
        raw = raw if isinstance(raw, Mapping) else {}
        pieces.append(PieceTier(p.money(raw.get("over_kg"), f"{label}起始重量"), p.money(raw.get("up_to_kg"), f"{label}结束重量"),
                                p.money(raw.get("per_piece"), f"{label}每件金额"), p.source(raw.get("source"), label)))
    limit = p.money(content.get("piece_limit_kg"), "单件重量上限")
    limit_source = p.source(content.get("piece_limit_source"), "单件重量上限")
    previous = None
    for index, tier in enumerate(pieces, 1):
        if tier.up_to_kg <= tier.over_kg or (previous is not None and tier.over_kg != previous):
            p.add(f"单件加收第 {index} 档的重量区间无效（结束要大于起始，并紧接上一档）。")
            break
        previous = tier.up_to_kg
    else:
        if pieces and pieces[-1].up_to_kg != limit:
            p.add(f"单件加收最后一档应到单件重量上限 {limit} 公斤。")
    if limit <= 0:
        p.add("单件重量上限必须大于 0。")
    reminders = content.get("reminders", [])
    if not isinstance(reminders, list) or len(reminders) > 10:
        p.add("提醒最多 10 条。")
        reminders = []
    reminders = [p.text(item, "提醒", 200) for item in reminders]
    if p.items:
        raise FeeRuleError("FEE_RULE_CONTENT_INVALID", "；".join(p.items))
    return UpstairsContent(base, elevator, tuple(tiers), max_floor, max_floor_source, first_floor, tuple(pieces), limit,
                           limit_source, tuple(item for item in reminders if item))


def parse_content(fee_kind: str, content: Any) -> UpstairsContent:
    if fee_kind == "upstairs":
        return parse_upstairs_content(content)
    raise FeeRuleError("FEE_KIND_UNKNOWN", "不支持的费用类型。")


def rule_from_record(record: Mapping[str, Any]) -> FeeRuleSet:
    """Build a rule set from a stored version (shared.fee_rule_repository)."""
    effective = record.get("effective_from")
    if isinstance(effective, str):
        effective = date.fromisoformat(effective)
    return FeeRuleSet(record["fee_kind"], record["version_label"], record["status"], effective,
                      parse_content(record["fee_kind"], record["content"]))


def approval_blockers(record: Mapping[str, Any]) -> list[str]:
    """Why a submitted version may not take effect yet; empty means it may."""
    problems = []
    try:
        parse_content(record["fee_kind"], record["content"])
    except FeeRuleError as exc:
        problems.append(exc.message)
    if not record.get("effective_from"):
        problems.append("还没有填写执行日期（以总部通知为准）。")
    pending = [item["text"] for item in record.get("checks") or [] if not item.get("done")]
    if pending:
        problems.append("还有未核对的事项：" + "；".join(pending))
    return problems


# -- calculation -----------------------------------------------------------------------

@dataclass(frozen=True)
class UpstairsInputs:
    settlement_weight_kg: Decimal
    elevator: bool
    floor: int
    goods_type: str = "general"
    piece_counts: tuple[int, ...] = ()  # pieces in each 单件加收 tier, in tier order
    pieces_over_limit: int = 0
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
    """The approved rule set in force on ``business_date`` (the waybill's 开单日期); none means no amount."""
    candidates = [rule for rule in rules if rule.fee_kind == fee_kind and rule.status == "APPROVED"
                  and rule.effective_from is not None and rule.effective_from <= business_date]
    if not candidates:
        raise FeeRuleError("FEE_RULE_NOT_IN_FORCE", "这个日期没有已审核生效的收费规则，不能给出金额。")
    latest = max(rule.effective_from for rule in candidates)
    chosen = [rule for rule in candidates if rule.effective_from == latest]
    if len(chosen) != 1:
        raise FeeRuleError("FEE_RULE_AMBIGUOUS", "同一执行日期有多版已生效规则，请先处理规则冲突。")
    return chosen[0]


def check_inputs(content: UpstairsContent, inputs: UpstairsInputs) -> tuple[str, ...]:
    """Reasonableness checks; any problem stops the calculation."""
    problems = []
    try:
        weight = Decimal(str(inputs.settlement_weight_kg))
    except (InvalidOperation, ValueError):
        return ("结算重量不是有效数字。",)
    if not weight.is_finite() or weight <= 0:
        problems.append("结算重量必须大于 0。")
    if inputs.goods_type not in GOODS_TYPES:
        problems.append("货物类型无效。")
    if type(inputs.floor) is not int or inputs.floor < 1 or inputs.floor > MAX_FLOOR:
        problems.append("楼层无效。")
    counts = tuple(inputs.piece_counts) + (inputs.pieces_over_limit,)
    if len(inputs.piece_counts) != len(content.piece_tiers):
        problems.append(f"分档件数应有 {len(content.piece_tiers)} 项，与规则的单件加收档数一致。")
    elif any(type(count) is not int or count < 0 for count in counts):
        problems.append("分档件数必须是 0 或正整数。")
    else:
        heavy = sum(counts)
        if inputs.total_pieces is not None and heavy > inputs.total_pieces:
            problems.append(f"分档件数合计 {heavy} 超过整票件数 {inputs.total_pieces}。")
        minimum = sum((tier.over_kg * count for tier, count in zip(content.piece_tiers, inputs.piece_counts)), Decimal("0"))
        minimum += content.piece_limit_kg * inputs.pieces_over_limit
        if weight.is_finite() and weight > 0 and minimum > weight:
            problems.append(f"按分档件数，单件重量合计至少 {minimum} 公斤，超过整票结算重量 {weight} 公斤，请核对。")
    return tuple(problems)


def calculate_upstairs(rule: FeeRuleSet, inputs: UpstairsInputs) -> FeeResult:
    if rule.fee_kind != "upstairs":
        raise FeeRuleError("FEE_RULE_KIND", "这不是上楼费规则。")
    content = rule.content
    problems = check_inputs(content, inputs)
    if problems:
        raise FeeRuleError("FEE_INPUT_INVALID", "；".join(problems))
    weight = Decimal(str(inputs.settlement_weight_kg))
    used: list[Source] = []

    def result(outcome: str, steps: Iterable[str], amount: Decimal | None = None) -> FeeResult:
        citations = tuple(dict.fromkeys(source.cite() for source in used))
        return FeeResult(outcome, None if amount is None else amount.quantize(CENT, rounding=ROUND_HALF_UP),
                         tuple(steps), citations, content.reminders, rule.version, rule.status)

    if inputs.goods_type != "general":
        kind = "床垫类" if inputs.goods_type == "mattress" else "整托或木箱"
        return result("MANUAL", (f"{kind}的收费表尚未录入规则，需人工按手册判断。",))
    if inputs.pieces_over_limit:
        used.append(content.piece_limit_source)
        return result("NEGOTIATE", (f"有 {inputs.pieces_over_limit} 件单件超过 {content.piece_limit_kg} 公斤："
                                    "手册规定可不提供服务或协商，没有固定金额。",))
    if inputs.elevator:
        charge = content.elevator
        used.append(charge.source)
        amount = charge.fixed + charge.per_kg * weight
        return result("AMOUNT", (f"有电梯：{charge.fixed} + {charge.per_kg}×{weight} = {amount}",), amount)
    if inputs.floor > content.max_floor_without_elevator:
        used.append(content.max_floor_source)
        return result("NEGOTIATE", (f"无电梯送 {inputs.floor} 楼（{content.max_floor_without_elevator} 楼以上）："
                                    "可不提供服务或协商，没有固定金额。",))
    if inputs.floor == 1 and content.first_floor == "MANUAL":
        return result("MANUAL", ("送到 1 楼：规则未写明是否收取上楼费，需人工判断。",))
    if inputs.floor == 1 and content.first_floor == "NO_CHARGE":
        return result("AMOUNT", ("送到 1 楼：按规则不收上楼费。",), Decimal("0"))
    base = content.base
    used.append(base.source)
    total = base.fixed + base.per_kg * weight
    steps = [f"基础 {base.fixed} + {base.per_kg}×{weight} = {total}"]
    if inputs.floor >= 2:
        tier = next(item for item in content.floor_tiers if item.floor_from <= inputs.floor <= item.floor_to)
        floor_fee = tier.per_kg * weight
        if floor_fee:
            used.append(tier.source)
            steps.append(f"楼层加收（{inputs.floor} 楼）{tier.per_kg}×{weight} = {floor_fee}")
        else:
            steps.append(f"楼层加收（{inputs.floor} 楼）不收")
        total += floor_fee
    piece_fee = Decimal("0")
    parts = []
    for tier, count in zip(content.piece_tiers, inputs.piece_counts):
        if count:
            used.append(tier.source)
            piece_fee += tier.per_piece * count
            parts.append(f"{tier.per_piece}×{count}")
    if piece_fee:
        steps.append(f"单件加收 {' + '.join(parts)} = {piece_fee}")
    total += piece_fee
    steps.append(f"合计 {total.quantize(CENT, rounding=ROUND_HALF_UP)}")
    return result("AMOUNT", steps, total)


def boundary_table(rule: FeeRuleSet, *, weight: Decimal = Decimal("200")) -> list[dict[str, Any]]:
    """Trial values on both sides of each boundary, for the person reviewing a rule set."""
    content = rule.content
    rows = []
    tiers = len(content.piece_tiers)
    piece_cases = [((0,) * tiers, 0)] + [(tuple(int(i == j) for j in range(tiers)), 0) for i in range(tiers)]
    piece_cases.append(((0,) * tiers, 1))
    for elevator in (True, False):
        for floor in range(1, content.max_floor_without_elevator + 2):
            for counts, over in piece_cases:
                inputs = UpstairsInputs(weight, elevator, floor, "general", counts, over)
                try:
                    outcome = calculate_upstairs(rule, inputs)
                except FeeRuleError as exc:
                    rows.append({"elevator": elevator, "floor": floor, "piece_counts": list(counts), "pieces_over_limit": over,
                                 "outcome": "INVALID", "amount": None, "note": exc.message})
                    continue
                rows.append({"elevator": elevator, "floor": floor, "piece_counts": list(counts), "pieces_over_limit": over,
                             "outcome": outcome.outcome, "amount": None if outcome.amount is None else str(outcome.amount)})
    return rows


def result_dict(result: FeeResult) -> dict[str, Any]:
    return {"outcome": result.outcome, "amount": None if result.amount is None else str(result.amount),
            "steps": list(result.steps), "citations": list(result.citations), "reminders": list(result.reminders),
            "rule_version": result.rule_version, "rule_status": result.rule_status, "reportable": result.reportable}


__all__ = [
    "FEE_KINDS", "FIRST_FLOOR_RULES", "FeeResult", "FeeRuleError", "FeeRuleSet", "GOODS_TYPES", "OUTCOMES",
    "RULE_STATUSES", "Source", "UpstairsContent", "UpstairsInputs", "VERSION_DATE_BASIS", "approval_blockers",
    "boundary_table", "calculate_upstairs", "check_inputs", "parse_content", "parse_upstairs_content", "result_dict",
    "rule_from_record", "select_rule",
]
