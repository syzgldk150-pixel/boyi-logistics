"""财务 · 派件账单（邵阳大祥S站）：纯规则，无环境、无 I/O。

2026-11-24 用户定的口径：

- 账单行＝每日到货表（到货统计，``arrival_stat_*``）里目的站为邵阳大祥S站的单，且在融辉
  “预付款管理 / 结算明细查询”（大祥S站账号）里已有“付派送费”记录（只算融辉已结算的单）。
  到货表日期取该单第一次出现在每日到货表的日期；派送、自提、预约自提全部都算。
- “融辉结算金额”＝该单所有以“付”开头的结算类型之和（融辉付给网点的派件收入：派送费、折让、
  回单服务费、到付/代收手续费、上楼/卸货/清点等增值收入），不算派到付款与罚款；设置里可加例外。
- 应付（给司机）＝派送费（按融辉结算重量分公斤段，每段有自己的起步价（含若干公斤）与超出单价；
  重量正好等于分界时按下一段算；规则按到货表日期选版本，2026-11-25 用户定；页面用语 2026-11-26
  改为“起步价／起步价含／超出单价”，存储字段仍是 first_price/first_weight/extra_price）
  ＋增值费。
- 2026-11-26 用户改口径：增值费以融辉结算明细为准，取设置里的“增值费结算类型”（默认“付上楼费收入”
  “付卸货费”，算给司机），不再人工按业务手册选；融辉结算金额里其余计入的类型（付派送费除外，如代收
  货款手续费、到付款手续费、等通知放货费、围板箱服务费）是“其他收入”，单独一列，不给司机。到货表配送
  方式在“不算派送费的配送方式”里的单（默认“自提”＝开单自提）派送费留空，仍可确认，应付只含增值费。

计算全部走 ``shared.fee_engine``（与业务手册同一引擎）；金额用 ``Decimal``，存四位、显示两位。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from shared.fee_engine import calculate, parse_rule
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
MAX_METHODS = 10
DEFAULT_VALUE_ADDED_TYPES = ("付上楼费收入", "付卸货费")
DEFAULT_NO_FEE_METHODS = ("自提",)
SETTINGS_FIELDS = ("lookahead_days", "include_types", "exclude_types", "value_added_types", "no_fee_methods")
MAX_AMOUNT = Decimal("1000000")
ROUNDINGS = {"exact": "按实际重量", "ceil_kg": "不足 1 公斤按 1 公斤"}
RULE_FIELDS = ("effective_from", "segments", "rounding", "note")
SEGMENT_FIELDS = ("from", "first_weight", "first_price", "extra_price")
MAX_SEGMENTS = 20


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


def _type_list(value: Any, label: str, *, limit: int = MAX_TYPES, what: str = "结算类型") -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise _invalid(f"{label}格式无效。")
    items = []
    for item in value:
        if not isinstance(item, str) or not 0 < len(item.strip()) <= 60:
            raise _invalid(f"{label}里有无效的{what}。")
        items.append(item.strip())
    return list(dict.fromkeys(items))


def parse_settings(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) - set(SETTINGS_FIELDS):
        raise _invalid("设置内容无效。")
    days = payload.get("lookahead_days")
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_LOOKAHEAD_DAYS:
        raise _invalid(f"往后多查的天数必须是 0 到 {MAX_LOOKAHEAD_DAYS} 的整数。")
    include = _type_list(payload.get("include_types", []), "额外计入的结算类型")
    exclude = _type_list(payload.get("exclude_types", []), "不计入的结算类型")
    if set(include) & set(exclude):
        raise _invalid("同一个结算类型不能既计入又不计入。")
    value_added_types = _type_list(payload.get("value_added_types", list(DEFAULT_VALUE_ADDED_TYPES)), "增值费结算类型")
    if DELIVERY_FEE_TYPE in value_added_types:
        raise _invalid(f"“{DELIVERY_FEE_TYPE}”按派送费规则算，不能设为增值费。")
    if set(value_added_types) & set(exclude):
        raise _invalid("增值费结算类型不能同时设为不计入。")
    methods = _type_list(payload.get("no_fee_methods", list(DEFAULT_NO_FEE_METHODS)), "不算派送费的配送方式",
                         limit=MAX_METHODS, what="配送方式")
    return {"lookahead_days": days, "include_types": include, "exclude_types": exclude,
            "value_added_types": value_added_types, "no_fee_methods": methods}


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


def _part(kind: str, value_added_types: Iterable[str]) -> str:
    """计入的结算类型属于哪一部分：派送费、增值费（给司机）或其他收入。"""
    if kind == DELIVERY_FEE_TYPE:
        return "delivery"
    return "value_added" if kind in set(value_added_types) else "other"


def summarize_settlement(entries: Sequence[Mapping[str, Any]], include: Iterable[str],
                         exclude: Iterable[str], value_added_types: Iterable[str] = DEFAULT_VALUE_ADDED_TYPES) -> dict[str, Any]:
    """一单的融辉结算：计入的合计及其中的增值费、其他收入，按结算类型的分项、是否已结算派送费、结算重量与件数。"""
    include, exclude, value_added_types = list(include), list(exclude), list(value_added_types)
    types: dict[str, dict[str, Any]] = {}
    for entry in entries:
        counted = type_counted(entry["type"], include, exclude)
        item = types.setdefault(entry["type"], {"type": entry["type"], "amount": ZERO, "count": 0, "date": "",
                                                "counted": counted,
                                                "part": _part(entry["type"], value_added_types) if counted else ""})
        item["amount"] += entry["amount"]
        item["count"] += 1
        item["date"] = max(item["date"], entry["date"])
    counted_items = [item for item in types.values() if item["counted"]]
    total = sum((item["amount"] for item in counted_items), ZERO)
    value_added_total = sum((item["amount"] for item in counted_items if item["part"] == "value_added"), ZERO)
    other_total = sum((item["amount"] for item in counted_items if item["part"] == "other"), ZERO)
    fee_rows = [entry for entry in entries if entry["type"] == DELIVERY_FEE_TYPE]
    problems: list[str] = []
    weight = pieces = None
    weight_missing = False
    if len(fee_rows) > 1:
        problems.append("融辉对这单有多笔“付派送费”，请到融辉核对。")
    if fee_rows:
        try:
            weight = to_decimal(fee_rows[0]["weight"])
        except (MissingAmountError, InvalidAmountError):
            weight = None
        if weight is None or weight < 0:
            weight_missing = True
            weight = None
        try:
            pieces = int(str(fee_rows[0]["pieces"]).strip())
        except (TypeError, ValueError):
            pieces = None
    return {
        "amount": total, "value_added": value_added_total, "other_income": other_total,
        "settled": bool(fee_rows), "settled_on": min((e["date"] for e in fee_rows), default=""),
        "settlement_weight": weight, "weight_missing": weight_missing, "pieces": pieces, "problems": problems,
        "types": sorted(({**item, "amount": item["amount"]} for item in types.values()),
                        key=lambda item: (not item["counted"], item["type"] != DELIVERY_FEE_TYPE, item["type"])),
    }


# -- 派送费规则 ------------------------------------------------------------------------------

def _fmt(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return text if text not in {"-0", ""} else "0"


def _segment_node(segment: Mapping[str, Decimal], rounding: str) -> dict[str, Any]:
    over: dict[str, Any] = {"excess": "settlement_weight", "over": _fmt(segment["first_weight"])}
    of = {"ceil": over, "per": "1"} if rounding == "ceil_kg" else over
    return {"type": "sum", "items": [
        {"type": "fixed", "label": f"起步价（含 {_fmt(segment['first_weight'])} 公斤）", "amount": _fmt(segment["first_price"])},
        {"type": "rate", "label": f"超出 {_fmt(segment['first_weight'])} 公斤的部分", "rate": _fmt(segment["extra_price"]),
         "unit": "元/kg", "of": of},
    ]}


def delivery_rule_content(segments: Sequence[Mapping[str, Decimal]], rounding: str) -> dict[str, Any]:
    """公斤段表 → ``shared.fee_engine`` 规则内容：按结算重量分档（起点含、终点不含，正好等于分界按下一段），
    每档＝该段起步价＋超过“起步价含”重量的部分 × 该段超出单价。"""
    tiers = []
    for index, segment in enumerate(segments):
        tier: dict[str, Any] = {"over": _fmt(segment["from"]), "then": _segment_node(segment, rounding)}
        if index + 1 < len(segments):
            tier["up_to"] = _fmt(segments[index + 1]["from"])
        tiers.append(tier)
    return {
        "schema": 1,
        "params": [{"key": "settlement_weight", "label": "结算重量", "type": "number", "unit": "kg", "min": "0",
                    "prefill": "settlement_weight"}],
        "formula": {"type": "tiers", "label": "派送费", "by": "settlement_weight", "closed": "left", "tiers": tiers},
    }


def parse_segments(value: Any) -> list[dict[str, Decimal]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SEGMENTS:
        raise _invalid(f"公斤段要有 1 到 {MAX_SEGMENTS} 段。")
    segments: list[dict[str, Decimal]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, Mapping) or set(item) != set(SEGMENT_FIELDS):
            raise _invalid(f"第 {index} 段的内容不完整。")
        label = f"第 {index} 段"
        # 页面上每段填“到多少公斤”，第 N 段的起点就是第 N−1 段的“到”，错误按页面上填的位置说。
        segment = {"from": _money(item["from"], f"第 {index - 1} 段的“到”" if index > 1 else "第 1 段的起点"),
                   "first_weight": _money(item["first_weight"], f"{label}的“起步价含”"),
                   "first_price": _money(item["first_price"], f"{label}的起步价"),
                   "extra_price": _money(item["extra_price"], f"{label}的超出单价")}
        if index == 1 and segment["from"] != 0:
            raise _invalid("第 1 段必须从 0 公斤起。")
        if segments and segment["from"] <= segments[-1]["from"]:
            raise _invalid(f"第 {index - 1} 段的“到”必须大于这一段的起点 {_fmt(segments[-1]['from'])} 公斤。")
        segments.append(segment)
    return segments


def parse_rule_form(form: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(form, Mapping) or set(form) - set(RULE_FIELDS):
        raise _invalid("派送费规则内容无效。")
    effective = parse_day(form.get("effective_from"), "执行日期")
    segments = parse_segments(form.get("segments"))
    rounding = form.get("rounding")
    if rounding not in ROUNDINGS:
        raise _invalid("超出部分零头的算法无效。")
    note = form.get("note")
    if note is not None and (not isinstance(note, str) or len(note.strip()) > 200):
        raise _invalid("备注不能超过 200 个字符。")
    content = delivery_rule_content(segments, rounding)
    try:
        parse_rule(content)
    except FeeRuleError as exc:  # pragma: no cover - the form only produces valid content
        raise _invalid(exc.message) from None
    return {"effective_from": effective, "segments": segments, "rounding": rounding,
            "note": (note or "").strip() or None, "content": content}


def segments_text(segments: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """存储用：公斤段的数字一律存成文本（不经浮点）。"""
    return [{key: _fmt(Decimal(str(segment[key]))) for key in SEGMENT_FIELDS} for segment in segments]


def describe_rule(rule: Mapping[str, Any]) -> str:
    segments = rule["segments"]
    parts = []
    for index, segment in enumerate(segments):
        low = _fmt(Decimal(str(segment["from"])))
        span = (f"{low}–{_fmt(Decimal(str(segments[index + 1]['from'])))} 公斤" if index + 1 < len(segments)
                else f"{low} 公斤以上")
        parts.append(f"{span}：起步价 {_fmt(Decimal(str(segment['first_price'])))} 元"
                     f"（含 {_fmt(Decimal(str(segment['first_weight'])))} 公斤），"
                     f"超出每公斤 {_fmt(Decimal(str(segment['extra_price'])))} 元")
    return "；".join(parts) + f"；超出部分{ROUNDINGS[rule['rounding']]}；正好在分界按下一段算"


def segment_examples(rule: Mapping[str, Any]) -> list[list[dict[str, str]]]:
    """每段的示例金额：段起点、到下一段前 1 公斤（最后一段只给起点）。

    设置人不用试算就能看出每段收多少、分界前后怎么变；金额用规则本身（``content``）算，与账单同一引擎。
    """

    segments = rule["segments"]
    examples: list[list[dict[str, str]]] = []
    for index, segment in enumerate(segments):
        low = Decimal(str(segment["from"]))
        points = [low]
        if index + 1 < len(segments):
            high = Decimal(str(segments[index + 1]["from"])) - 1
            if high > low:
                points.append(high)
        examples.append([{"weight": _fmt(point), "amount": format(delivery_fee(rule, point)[0], "f")}
                         for point in points])
    return examples


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


# -- 组账单 ----------------------------------------------------------------------------------

def fingerprint(row: Mapping[str, Any]) -> str:
    keys = ("tracking_number", "arrival_date", "delivery_method", "ronghui_amount", "settlement_weight", "pieces",
            "delivery_rule_id", "delivery_fee", "value_added_total", "other_income", "item_revision", "status")
    return hashlib.sha256(json_text({key: row.get(key) for key in keys}).encode("utf-8")).hexdigest()


def _type_view(types: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": item["type"], "amount": _text(item["amount"]), "count": item["count"], "date": item["date"],
             "counted": item["counted"], "part": item.get("part", "")} for item in types]


def build_bill(*, start: dt.date, end: dt.date, arrivals: Sequence[Mapping[str, Any]],
               settlements: Mapping[str, Sequence[Mapping[str, Any]]], known_arrivals: Iterable[str],
               missing_dates: Sequence[str], settings: Mapping[str, Any], rules: Sequence[Mapping[str, Any]],
               items: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """账单行与汇总。``arrivals`` 是日期范围内第一次出现在每日到货表的大祥S站单（已去重）；
    ``known_arrivals`` 是融辉已结算单号里在每日到货表任何日期出现过的；``items`` 是已保存的记录。"""
    include, exclude = settings["include_types"], settings["exclude_types"]
    value_added_types, no_fee_methods = settings["value_added_types"], set(settings["no_fee_methods"])
    rows, unsettled = [], []
    totals = {key: ZERO for key in ("ronghui", "delivery", "value_added", "other_income", "payable")}
    counts = {"confirmed": 0, "pending": 0, "blocked": 0}
    for arrival in sorted(arrivals, key=lambda item: (str(item["arrival_date"]), item["tracking_number"])):
        code = arrival["tracking_number"]
        day = parse_day(arrival["arrival_date"], "到货表日期")
        summary = summarize_settlement(settlements.get(code, ()), include, exclude, value_added_types)
        method = arrival.get("delivery_method") or ""
        base = {"tracking_number": code, "arrival_date": day.isoformat(), "goods_name": arrival.get("goods_name") or "",
                "delivery_method": method, "package_type": arrival.get("package_type") or ""}
        if not summary["settled"]:
            unsettled.append({**base, "types": _type_view(summary["types"])})
            continue
        item = items.get(code) or {}
        problems = list(summary["problems"])
        confirmed = item.get("status") == "confirmed"
        no_fee = method in no_fee_methods
        if confirmed:
            fee = None if item.get("delivery_fee") is None else Decimal(str(item["delivery_fee"]))
            rule_id = item.get("delivery_rule_id")
            steps = list(item.get("delivery_steps") or [])
            value_total = Decimal(str(item.get("value_added_total") or "0"))
            payable = Decimal(str(item["payable_total"]))
            if Decimal(str(item["ronghui_amount"])) != summary["amount"]:
                problems.append(f"融辉结算金额与确认时（{_text(Decimal(str(item['ronghui_amount'])))}）不同，请核对。")
            ready = True
        else:
            fee = rule_id = None
            steps: list[str] = []
            value_total = summary["value_added"]
            if no_fee:
                steps = [f"到货表配送方式为“{method}”，不算派送费。"]
                ready = True
            else:
                rule = select_delivery_rule(rules, day)
                if summary["weight_missing"]:
                    problems.append("融辉结算明细缺少结算重量，不能算派送费。")
                if rule is None:
                    problems.append("到货表日期没有适用的派送费规则，请先设置派送费规则。")
                elif summary["settlement_weight"] is not None:
                    fee, steps = delivery_fee(rule, summary["settlement_weight"])
                    rule_id = rule["id"]
                ready = fee is not None
            payable = (fee or ZERO) + value_total if ready else None
        row = {**base, "status": "confirmed" if confirmed else "pending", "no_delivery_fee": no_fee,
               "ronghui_amount": _text(summary["amount"]), "ronghui_types": _type_view(summary["types"]),
               "settled_on": summary["settled_on"],
               "settlement_weight": _text(summary["settlement_weight"]), "pieces": summary["pieces"],
               "delivery_rule_id": rule_id, "delivery_fee": _text(fee), "delivery_steps": steps,
               "value_added_total": _text(value_total), "other_income": _text(summary["other_income"]),
               "payable_total": _text(payable),
               "difference": _text(summary["amount"] - payable) if payable is not None else None,
               "item_revision": item.get("revision"), "confirmed_by": item.get("confirmed_by"),
               "confirmed_at": item.get("confirmed_at"), "problems": problems,
               "can_confirm": not confirmed and ready and not summary["problems"]}
        row["fingerprint"] = fingerprint(row)
        rows.append(row)
        totals["ronghui"] += summary["amount"]
        if fee is not None:
            totals["delivery"] += fee
        totals["value_added"] += value_total
        totals["other_income"] += summary["other_income"]
        if payable is not None:
            totals["payable"] += payable
        counts["confirmed" if confirmed else "pending"] += 1
        if not confirmed and not row["can_confirm"]:
            counts["blocked"] += 1
    arrival_codes = {item["tracking_number"] for item in arrivals}
    known = set(known_arrivals) | arrival_codes
    missing_in_arrival = []
    for code, entries in sorted(settlements.items()):
        summary = summarize_settlement(entries, include, exclude, value_added_types)
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
                    "other_income_total": _text(totals["other_income"]), "payable_total": _text(totals["payable"]),
                    "difference_total": _text(totals["ronghui"] - totals["payable"])},
    }
