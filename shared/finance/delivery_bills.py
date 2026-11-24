"""财务 · 派件账单（邵阳大祥S站）：纯规则，无环境、无 I/O。

2026-11-24 用户定的口径：

- 账单行＝每日到货表（到货统计，``arrival_stat_*``）里目的站为邵阳大祥S站的单，且在融辉
  “预付款管理 / 结算明细查询”（大祥S站账号）里已有“付派送费”记录（只算融辉已结算的单）。
  到货表日期取该单第一次出现在每日到货表的日期；派送、自提、预约自提全部都算。
- “融辉结算金额”＝该单所有以“付”开头的结算类型之和（融辉付给网点的派件收入：派送费、折让、
  回单服务费、到付/代收手续费、上楼/卸货/清点等增值收入），不算派到付款与罚款；设置里可加例外。
- 应付（给司机）＝派送费（每票起步价＋超重加价，按融辉结算重量，规则按到货表日期选版本）
  ＋增值费（业务手册已生效规则，人工选费用、填条件，引擎算金额；需协商等由人填金额）。

计算全部走 ``shared.fee_engine``（与业务手册同一引擎）；金额用 ``Decimal``，存四位、显示两位。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from shared.fee_engine import NO_AMOUNT, QuoteRule, calculate, parse_rule, quote
from shared.finance.db import json_text
from shared.finance.errors import FinanceValidationError
from shared.finance.money import ZERO, InvalidAmountError, MissingAmountError, quantize_storage, to_decimal
from shared.handbook_fee_rules import FeeRuleError

STATION_NAME = "邵阳大祥S站"
SETTLEMENT_ACCOUNT_ID = "ronghui_daxiang_s"
DELIVERY_FEE_TYPE = "付派送费"
COUNTED_PREFIX = "付"
MAX_SPAN_DAYS = 31
MAX_LOOKAHEAD_DAYS = 31
MAX_BATCH = 200
MAX_TYPES = 60
MAX_FEES = 20
MAX_AMOUNT = Decimal("1000000")
ROUNDINGS = {"exact": "按实际重量", "ceil_kg": "不足 1 公斤按 1 公斤"}
RULE_FIELDS = ("effective_from", "start_price", "base_weight", "extra_price", "rounding", "note")
_MANUAL_OUTCOMES = ("NEGOTIATE", "MANUAL", "ACTUAL")


def _invalid(message: str, code: str = "DELIVERY_BILL_INVALID") -> FinanceValidationError:
    return FinanceValidationError(message, code=code)


def _money(value: Any, label: str, *, allow_negative: bool = False) -> Decimal:
    try:
        amount = to_decimal(value)
    except (MissingAmountError, InvalidAmountError):
        raise _invalid(f"{label}不是有效数字。") from None
    if amount.as_tuple().exponent < -4:
        raise _invalid(f"{label}最多保留 4 位小数。")
    if abs(amount) > MAX_AMOUNT or (amount < 0 and not allow_negative):
        raise _invalid(f"{label}必须在 0 到 {MAX_AMOUNT} 之间。")
    return amount


def _text(amount: Decimal | None) -> str | None:
    return None if amount is None else format(quantize_storage(amount), "f")


def parse_day(value: Any, label: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        text = str(value or "").strip()
        if len(text) != 10:
            raise ValueError(text)
        return dt.date.fromisoformat(text)
    except ValueError:
        raise _invalid(f"{label}格式应为 YYYY-MM-DD。") from None


def bill_range(start: Any, end: Any) -> tuple[dt.date, dt.date]:
    first, last = parse_day(start, "开始日期"), parse_day(end, "结束日期")
    if last < first:
        raise _invalid("结束日期不能早于开始日期。")
    if (last - first).days + 1 > MAX_SPAN_DAYS:
        raise _invalid(f"一次最多查询 {MAX_SPAN_DAYS} 天到货表，请缩短日期范围。")
    return first, last


def settlement_window(start: dt.date, end: dt.date, lookahead_days: int, today: dt.date) -> tuple[dt.date, dt.date]:
    """融辉结算日期的查询范围：到货表起始日到“结束日＋N 天”（不超过今天）。"""
    last = min(end + dt.timedelta(days=lookahead_days), max(today, start))
    return start, last


def date_chunks(start: dt.date, end: dt.date, span: int = MAX_SPAN_DAYS) -> list[tuple[dt.date, dt.date]]:
    chunks, cursor = [], start
    while cursor <= end:
        last = min(cursor + dt.timedelta(days=span - 1), end)
        chunks.append((cursor, last))
        cursor = last + dt.timedelta(days=1)
    return chunks


# -- 设置 ------------------------------------------------------------------------------------

def type_counted(kind: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    if kind in set(exclude):
        return False
    return kind in set(include) or kind.startswith(COUNTED_PREFIX)


def _type_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_TYPES:
        raise _invalid(f"{label}格式无效。")
    items = []
    for item in value:
        if not isinstance(item, str) or not 0 < len(item.strip()) <= 60:
            raise _invalid(f"{label}里有无效的结算类型。")
        items.append(item.strip())
    return list(dict.fromkeys(items))


def parse_settings(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) - {"lookahead_days", "include_types", "exclude_types"}:
        raise _invalid("设置内容无效。")
    days = payload.get("lookahead_days")
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_LOOKAHEAD_DAYS:
        raise _invalid(f"往后多查的天数必须是 0 到 {MAX_LOOKAHEAD_DAYS} 的整数。")
    include = _type_list(payload.get("include_types", []), "额外计入的结算类型")
    exclude = _type_list(payload.get("exclude_types", []), "不计入的结算类型")
    if set(include) & set(exclude):
        raise _invalid("同一个结算类型不能既计入又不计入。")
    return {"lookahead_days": days, "include_types": include, "exclude_types": exclude}


# -- 融辉结算明细 ----------------------------------------------------------------------------

def settlement_entries(rows: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """按运单号分组的融辉结算明细行；不挂运单的（系统费、充值等）不进派件账单，已删除的不算。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise _invalid("融辉结算明细格式与核实时不一致。", "DELIVERY_SETTLEMENT_INVALID")
        code = str(row.get("BILL_CODE") or "").strip().upper()
        if not code:
            continue
        deleted = str(row.get("BL_DEL") or "").strip()
        if deleted == "是":
            continue
        if deleted not in {"否", ""} or "BALANCE_TYPE" not in row or "BALANCE_CUR_MONEY" not in row:
            raise _invalid("融辉结算明细缺少结算类型、金额或删除状态，页面可能已变化。", "DELIVERY_SETTLEMENT_INVALID")
        kind = str(row.get("BALANCE_TYPE") or "").strip()
        if not kind:
            raise _invalid(f"融辉结算明细里运单 {code} 有一行没有结算类型。", "DELIVERY_SETTLEMENT_INVALID")
        try:
            amount = to_decimal(row.get("BALANCE_CUR_MONEY"))
        except (MissingAmountError, InvalidAmountError):
            raise _invalid(f"融辉结算明细里运单 {code} 的金额无效。", "DELIVERY_SETTLEMENT_INVALID") from None
        grouped.setdefault(code, []).append({
            "type": kind, "amount": amount, "date": str(row.get("BALANCE_DATE") or "")[:10],
            "weight": row.get("SETTLEMENT_WEIGHT"), "pieces": row.get("PIECE_NUMBER"),
        })
    return grouped


def summarize_settlement(entries: Sequence[Mapping[str, Any]], include: Iterable[str],
                         exclude: Iterable[str]) -> dict[str, Any]:
    """一单的融辉结算：计入的合计、按结算类型的分项、是否已结算派送费、结算重量与件数。"""
    include, exclude = list(include), list(exclude)
    types: dict[str, dict[str, Any]] = {}
    for entry in entries:
        item = types.setdefault(entry["type"], {"type": entry["type"], "amount": ZERO, "count": 0, "date": "",
                                                "counted": type_counted(entry["type"], include, exclude)})
        item["amount"] += entry["amount"]
        item["count"] += 1
        item["date"] = max(item["date"], entry["date"])
    total = sum((item["amount"] for item in types.values() if item["counted"]), ZERO)
    fee_rows = [entry for entry in entries if entry["type"] == DELIVERY_FEE_TYPE]
    problems: list[str] = []
    weight = pieces = None
    if len(fee_rows) > 1:
        problems.append("融辉对这单有多笔“付派送费”，请到融辉核对。")
    if fee_rows:
        try:
            weight = to_decimal(fee_rows[0]["weight"])
        except (MissingAmountError, InvalidAmountError):
            weight = None
        if weight is None or weight < 0:
            problems.append("融辉结算明细缺少结算重量，不能算派送费。")
            weight = None
        try:
            pieces = int(str(fee_rows[0]["pieces"]).strip())
        except (TypeError, ValueError):
            pieces = None
    return {
        "amount": total, "settled": bool(fee_rows), "settled_on": min((e["date"] for e in fee_rows), default=""),
        "settlement_weight": weight, "pieces": pieces, "problems": problems,
        "types": sorted(({**item, "amount": item["amount"]} for item in types.values()),
                        key=lambda item: (not item["counted"], item["type"] != DELIVERY_FEE_TYPE, item["type"])),
    }


# -- 派送费规则 ------------------------------------------------------------------------------

def _fmt(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text if text not in {"-0", ""} else "0"


def delivery_rule_content(start_price: Decimal, base_weight: Decimal, extra_price: Decimal, rounding: str) -> dict[str, Any]:
    """表单 → ``shared.fee_engine`` 规则内容：起步价＋超过首重部分 × 续重单价。"""
    over: dict[str, Any] = {"excess": "settlement_weight", "over": _fmt(base_weight)}
    of = {"ceil": over, "per": "1"} if rounding == "ceil_kg" else over
    return {
        "schema": 1,
        "params": [{"key": "settlement_weight", "label": "结算重量", "type": "number", "unit": "kg", "min": "0",
                    "prefill": "settlement_weight"}],
        "formula": {"type": "sum", "label": "派送费", "items": [
            {"type": "fixed", "label": "起步价", "amount": _fmt(start_price)},
            {"type": "rate", "label": f"超过 {_fmt(base_weight)} 公斤加价", "rate": _fmt(extra_price), "unit": "元/kg",
             "of": of},
        ]},
    }


def parse_rule_form(form: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(form, Mapping) or set(form) - set(RULE_FIELDS):
        raise _invalid("派送费规则内容无效。")
    effective = parse_day(form.get("effective_from"), "执行日期")
    start = _money(form.get("start_price"), "起步价")
    base = _money(form.get("base_weight"), "首重")
    extra = _money(form.get("extra_price"), "续重单价")
    rounding = form.get("rounding")
    if rounding not in ROUNDINGS:
        raise _invalid("续重取整方式无效。")
    note = form.get("note")
    if note is not None and (not isinstance(note, str) or len(note.strip()) > 200):
        raise _invalid("备注不能超过 200 个字符。")
    content = delivery_rule_content(start, base, extra, rounding)
    try:
        parse_rule(content)
    except FeeRuleError as exc:  # pragma: no cover - the form only produces valid content
        raise _invalid(exc.message) from None
    return {"effective_from": effective, "start_price": start, "base_weight": base, "extra_price": extra,
            "rounding": rounding, "note": (note or "").strip() or None, "content": content}


def describe_rule(rule: Mapping[str, Any]) -> str:
    base = _fmt(Decimal(str(rule["base_weight"])))
    return (f"每票 {_fmt(Decimal(str(rule['start_price'])))} 元（含 {base} 公斤），超过部分每公斤 "
            f"{_fmt(Decimal(str(rule['extra_price'])))} 元，{ROUNDINGS[rule['rounding']]}")


def select_delivery_rule(rules: Iterable[Mapping[str, Any]], day: dt.date) -> Mapping[str, Any] | None:
    """在用的规则里执行日期不晚于 ``day`` 的最新一版（同一执行日期只能有一版在用，由数据库保证）。"""
    candidates = [rule for rule in rules if rule["status"] == "active" and parse_day(rule["effective_from"], "执行日期") <= day]
    return max(candidates, key=lambda rule: parse_day(rule["effective_from"], "执行日期")) if candidates else None


def delivery_fee(rule: Mapping[str, Any], weight: Decimal) -> tuple[Decimal, list[str]]:
    content = rule["content"]
    try:
        outcome = calculate(parse_rule(content), {"settlement_weight": _fmt(weight)})
    except FeeRuleError as exc:
        raise _invalid(f"派送费规则无法计算：{exc.message}") from None
    if outcome.outcome != "AMOUNT" or outcome.amount is None:
        raise _invalid(f"派送费规则没有给出金额：{outcome.reason}")
    return outcome.amount, list(outcome.steps)


# -- 增值费（业务手册规则，人工选择与填写） ---------------------------------------------------

def parse_fee_choice(payload: Mapping[str, Any]) -> tuple[list[str], dict[str, Any], dict[str, dict[str, Any]]]:
    kinds = payload.get("fee_kinds", [])
    values = payload.get("values", {})
    manual = payload.get("manual", {})
    if not isinstance(kinds, list) or len(kinds) > MAX_FEES or not all(isinstance(k, str) and 0 < len(k) <= 64 for k in kinds):
        raise _invalid(f"增值费最多选 {MAX_FEES} 项。")
    if not isinstance(values, Mapping) or len(values) > 60:
        raise _invalid("增值费条件格式无效。")
    if not isinstance(manual, Mapping) or len(manual) > MAX_FEES:
        raise _invalid("人工金额格式无效。")
    cleaned: dict[str, dict[str, Any]] = {}
    for kind, item in manual.items():
        if not isinstance(item, Mapping) or set(item) - {"amount", "note"}:
            raise _invalid("人工金额格式无效。")
        note = item.get("note")
        if note is not None and (not isinstance(note, str) or len(note.strip()) > 200):
            raise _invalid("人工金额说明不能超过 200 个字符。")
        amount = item.get("amount")
        cleaned[str(kind)] = {"amount": None if amount in (None, "") else _money(amount, "人工金额"),
                              "note": (note or "").strip()}
    return list(dict.fromkeys(kinds)), dict(values), cleaned


def value_added(catalog: Sequence[QuoteRule], kinds: Sequence[str], values: Mapping[str, Any],
                manual: Mapping[str, Mapping[str, Any]], *, strict: bool) -> dict[str, Any]:
    """所选增值费的明细与合计。``strict`` 时（保存）缺条件或缺人工金额即拒绝；试算时只标出来。

    同组费用不能同时收取、取大，与 ``shared.fee_engine.quote`` 一致；``quote`` 在组内有费用没有金额
    时整组不计，这里在人填了金额后按同一取大规则计入。
    """
    by_kind = {item.fee_kind: item for item in catalog}
    unknown = [kind for kind in kinds if kind not in by_kind]
    if unknown:
        raise _invalid("选择的增值费在这单的开单日期没有已生效规则，请刷新后重选。", "DELIVERY_FEE_UNKNOWN")
    if not kinds:
        return {"lines": [], "total": "0.0000", "complete": True, "missing": []}
    chosen = [by_kind[kind] for kind in kinds]
    try:
        result = quote(chosen, values)
    except FeeRuleError as exc:
        raise _invalid(exc.message, exc.code) from None
    lines, problems = [], []
    for position, (item, rule) in enumerate(zip(result.items, chosen)):
        outcome, system = item.result.outcome, item.result.amount
        entered = manual.get(item.fee_kind) or {}
        amount, note = system, item.note
        if outcome == "MISSING":
            problems.append(f"{item.name}：{item.result.reason}")
            amount = None
        elif outcome in _MANUAL_OUTCOMES:
            amount = entered.get("amount")
            if amount is None:
                problems.append(f"{item.name}的结果是“{NO_AMOUNT[outcome]}”，请填写实际金额。")
            note = entered.get("note") or note
        elif outcome == "NOT_PROVIDED":
            amount = None
        lines.append({"position": position, "fee_kind": item.fee_kind, "fee_name": item.name, "rule_id": rule.rule_id,
                      "version": item.version, "group": rule.rule.group, "outcome": outcome,
                      "outcome_text": item.result.as_dict()["outcome_text"], "system_amount": _text(system),
                      "amount": amount, "counted": amount is not None, "note": note, "reason": item.result.reason,
                      "steps": list(item.result.steps), "reminders": list(item.result.reminders)})
    groups: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        if line["group"] and line["amount"] is not None:
            groups.setdefault(line["group"], []).append(line)
    for group, members in groups.items():
        if len(members) < 2:
            continue
        best = max(members, key=lambda line: (line["amount"], -line["position"]))
        for line in members:
            if line is not best:
                line["counted"] = False
                line["note"] = f"与“{best['fee_name']}”不可同时收取（“{group}”组取大），按{best['fee_name']}收取。"
    if strict and problems:
        raise _invalid("；".join(problems), "DELIVERY_FEE_INCOMPLETE")
    total = sum((line["amount"] for line in lines if line["counted"]), ZERO)
    for line in lines:
        line["amount"] = _text(line["amount"])
    return {"lines": lines, "total": _text(total), "complete": not problems, "missing": problems}


# -- 组账单 ----------------------------------------------------------------------------------

def fingerprint(row: Mapping[str, Any]) -> str:
    keys = ("tracking_number", "arrival_date", "ronghui_amount", "settlement_weight", "pieces", "delivery_rule_id",
            "delivery_fee", "value_added_total", "item_revision", "status")
    return hashlib.sha256(json_text({key: row.get(key) for key in keys}).encode("utf-8")).hexdigest()


def _type_view(types: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": item["type"], "amount": _text(item["amount"]), "count": item["count"], "date": item["date"],
             "counted": item["counted"]} for item in types]


def build_bill(*, start: dt.date, end: dt.date, arrivals: Sequence[Mapping[str, Any]],
               settlements: Mapping[str, Sequence[Mapping[str, Any]]], known_arrivals: Iterable[str],
               missing_dates: Sequence[str], settings: Mapping[str, Any], rules: Sequence[Mapping[str, Any]],
               items: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """账单行与汇总。``arrivals`` 是日期范围内第一次出现在每日到货表的大祥S站单（已去重）；
    ``known_arrivals`` 是融辉已结算单号里在每日到货表任何日期出现过的；``items`` 是已保存的记录。"""
    include, exclude = settings["include_types"], settings["exclude_types"]
    rows, unsettled = [], []
    totals = {key: ZERO for key in ("ronghui", "delivery", "value_added", "payable")}
    counts = {"confirmed": 0, "pending": 0, "blocked": 0}
    for arrival in sorted(arrivals, key=lambda item: (str(item["arrival_date"]), item["tracking_number"])):
        code = arrival["tracking_number"]
        day = parse_day(arrival["arrival_date"], "到货表日期")
        summary = summarize_settlement(settlements.get(code, ()), include, exclude)
        base = {"tracking_number": code, "arrival_date": day.isoformat(), "goods_name": arrival.get("goods_name") or "",
                "delivery_method": arrival.get("delivery_method") or "", "package_type": arrival.get("package_type") or ""}
        if not summary["settled"]:
            unsettled.append({**base, "types": _type_view(summary["types"])})
            continue
        item = items.get(code) or {}
        problems = list(summary["problems"])
        confirmed = item.get("status") == "confirmed"
        if confirmed:
            fee, rule_id = Decimal(str(item["delivery_fee"])), item["delivery_rule_id"]
            steps = list(item.get("delivery_steps") or [])
            payable = Decimal(str(item["payable_total"]))
            if Decimal(str(item["ronghui_amount"])) != summary["amount"]:
                problems.append(f"融辉结算金额与确认时（{_text(Decimal(str(item['ronghui_amount'])))}）不同，请核对。")
        else:
            rule = select_delivery_rule(rules, day)
            fee = rule_id = None
            steps: list[str] = []
            if rule is None:
                problems.append("到货表日期没有适用的派送费规则，请先设置派送费规则。")
            elif summary["settlement_weight"] is not None:
                fee, steps = delivery_fee(rule, summary["settlement_weight"])
                rule_id = rule["id"]
            payable = None
        value_total = Decimal(str(item.get("value_added_total") or "0"))
        if payable is None and fee is not None:
            payable = fee + value_total
        row = {**base, "status": "confirmed" if confirmed else "pending",
               "ronghui_amount": _text(summary["amount"]), "ronghui_types": _type_view(summary["types"]),
               "settled_on": summary["settled_on"],
               "settlement_weight": _text(summary["settlement_weight"]), "pieces": summary["pieces"],
               "delivery_rule_id": rule_id, "delivery_fee": _text(fee), "delivery_steps": steps,
               "value_added_total": _text(value_total), "fee_lines": list(item.get("lines") or []),
               "billing_date": item.get("billing_date"), "payable_total": _text(payable),
               "difference": _text(summary["amount"] - payable) if payable is not None else None,
               "item_revision": item.get("revision"), "confirmed_by": item.get("confirmed_by"),
               "confirmed_at": item.get("confirmed_at"), "problems": problems,
               "can_confirm": not confirmed and fee is not None and not summary["problems"]}
        row["fingerprint"] = fingerprint(row)
        rows.append(row)
        totals["ronghui"] += summary["amount"]
        if fee is not None:
            totals["delivery"] += fee
        totals["value_added"] += value_total
        if payable is not None:
            totals["payable"] += payable
        counts["confirmed" if confirmed else "pending"] += 1
        if not confirmed and not row["can_confirm"]:
            counts["blocked"] += 1
    arrival_codes = {item["tracking_number"] for item in arrivals}
    known = set(known_arrivals) | arrival_codes
    missing_in_arrival = []
    for code, entries in sorted(settlements.items()):
        summary = summarize_settlement(entries, include, exclude)
        if code in known or not summary["settled"]:
            continue
        if start.isoformat() <= summary["settled_on"] <= end.isoformat():
            missing_in_arrival.append({"tracking_number": code, "settled_on": summary["settled_on"],
                                       "ronghui_amount": _text(summary["amount"])})
    return {
        "rows": rows, "unsettled": unsettled, "missing_in_arrival": missing_in_arrival,
        "missing_dates": list(missing_dates),
        "summary": {"arrival_count": len(arrivals), "settled_count": len(rows), "unsettled_count": len(unsettled),
                    "confirmed_count": counts["confirmed"], "pending_count": counts["pending"],
                    "blocked_count": counts["blocked"], "ronghui_total": _text(totals["ronghui"]),
                    "delivery_total": _text(totals["delivery"]), "value_added_total": _text(totals["value_added"]),
                    "payable_total": _text(totals["payable"]),
                    "difference_total": _text(totals["ronghui"] - totals["payable"])},
    }
