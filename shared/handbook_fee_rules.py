"""Fee rule versions from 业务手册 → 计算规则: which version is in force and when one may take effect.

How a fee is computed is block-built content (``shared.fee_engine``): parameters people
type, a formula from a fixed set of blocks, every number and its source. People or an AI
draft fill it in the back office and a super administrator approves a version before it is
used, so neither a new number nor a new kind of fee needs a release; only a new kind of
block does.

Only an ``APPROVED`` version whose effective date is not after the waybill's billing
(开单) date may give people an amount (``select_rule``). Pure functions: no environment or I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping

RULE_STATUSES = ("DRAFT", "PENDING", "APPROVED", "RETIRED")
# Which date of a waybill picks the rule version: its billing (开单) date,
# confirmed by the user on 2026-11-16. Callers pass that date to select_rule.
VERSION_DATE_BASIS = "billing_date"
FEE_NAME_LIMIT = 40


class FeeRuleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class FeeRuleSet:
    fee_kind: str
    fee_name: str
    version: str
    status: str
    effective_from: date | None
    content: Any  # shared.fee_engine.Rule

    def __post_init__(self) -> None:
        if self.status not in RULE_STATUSES:
            raise ValueError("unknown rule status")
        if self.status == "APPROVED" and self.effective_from is None:
            raise ValueError("an approved rule set needs the effective date from the headquarters notice")

    @property
    def reportable(self) -> bool:
        """Only an approved rule set may give people an amount."""
        return self.status == "APPROVED"


def parse_content(content: Any):
    from shared.fee_engine import parse_rule

    return parse_rule(content)


def rule_from_record(record: Mapping[str, Any]) -> FeeRuleSet:
    """Build a rule set from a stored version (shared.fee_rule_repository)."""
    effective = record.get("effective_from")
    if isinstance(effective, str):
        effective = date.fromisoformat(effective)
    return FeeRuleSet(record["fee_kind"], str(record.get("fee_name") or record["fee_kind"]), record["version_label"],
                      record["status"], effective, parse_content(record["content"]))


def review_lines(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The 收费表 of a version (``shared.fee_rule_lines``) with each line's tick from its checks."""
    from shared.fee_rule_lines import fee_lines

    ticked = {item.get("sig") for item in record.get("checks") or [] if item.get("kind") == "line"}
    return [{**line, "done": line["sig"] in ticked} for line in fee_lines(record.get("content"))]


def approval_blockers(record: Mapping[str, Any]) -> list[str]:
    """Why a submitted version may not take effect yet; empty means it may.

    核对 (2026-11-20): every 收费表 line ticked against the manual, every question answered
    and every remaining item ticked.
    """
    from shared.fee_engine import unsourced_blocks

    problems = []
    if not str(record.get("fee_name") or "").strip():
        problems.append("还没有填写费用名称。")
    try:
        parse_content(record["content"])
    except FeeRuleError as exc:
        problems.append(exc.message)
    else:
        missing = unsourced_blocks(record["content"])
        if missing:
            problems.append("这些积木还没有出处：" + "、".join(missing[:8]) + (" 等" if len(missing) > 8 else "") + "。")
    if not record.get("effective_from"):
        problems.append("还没有填写执行日期（以总部通知为准）。")
    open_lines = sum(not line["done"] for line in review_lines(record))
    if open_lines:
        problems.append(f"收费表还有 {open_lines} 行没有对照手册打勾。")
    checks = record.get("checks") or []
    questions = [item["text"] for item in checks if item.get("kind") == "question" and item.get("answer") is None]
    if questions:
        problems.append("还有需要拍板的问题没有回答：" + "；".join(questions))
    pending = [item["text"] for item in checks if item.get("kind") is None and not item.get("done")]
    if pending:
        problems.append("还有未核对的事项：" + "；".join(pending))
    return problems


def submit_blockers(record: Mapping[str, Any], *, trial_count: int) -> list[str]:
    """Why a draft may not be submitted yet (frontend-spec 54: basic info, content and sources,
    核对 complete, at least one trial since the last save); empty means it may."""
    problems = approval_blockers(record)
    if not str(record.get("version_label") or "").strip():
        problems.insert(0, "还没有填写版本名称。")
    if trial_count < 1:
        problems.append("保存后还没有试算过，请至少试算一个场景。")
    return problems


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


def quote_catalog(records: Iterable[Mapping[str, Any]], *, business_date: date) -> tuple[list[Any], list[dict[str, str]]]:
    """The fees that can be quoted on ``business_date``: one approved version per fee, plus why others cannot.

    Returns (``shared.fee_engine.QuoteRule`` list in fee-name order, unavailable fees with a reason).
    """
    from shared.fee_engine import QuoteRule

    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_kind.setdefault(record["fee_kind"], []).append(record)
    rules, unavailable = [], []
    for kind, versions in by_kind.items():
        name = str(versions[-1].get("fee_name") or kind)
        try:
            parsed = []
            for record in versions:
                parsed.append(rule_from_record(record))
            chosen = select_rule(parsed, fee_kind=kind, business_date=business_date)
        except FeeRuleError as exc:
            unavailable.append({"fee_kind": kind, "name": name, "reason": exc.message})
            continue
        label = f"{chosen.version}（执行日期 {chosen.effective_from.isoformat()}）"
        rules.append(QuoteRule(kind, chosen.fee_name, label, chosen.content))
    rules.sort(key=lambda item: item.name)
    unavailable.sort(key=lambda item: item["name"])
    return rules, unavailable


__all__ = [
    "FEE_NAME_LIMIT", "FeeRuleError", "FeeRuleSet", "RULE_STATUSES", "VERSION_DATE_BASIS", "approval_blockers",
    "parse_content", "quote_catalog", "review_lines", "rule_from_record", "select_rule", "submit_blockers",
]
