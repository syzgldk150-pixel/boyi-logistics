"""Standard source contract ``finance.source_batch/v1``: pure validation by declaration.

A connector submits one batch per source and period. The Host checks structure,
amounts and identity always, and the balance chain, platform summary and
opening/closing balances only when the batch declares them. Every check that
ran (or did not) is recorded so the page can show what was actually verified.
See ``docs/finance_connector_contract.md``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

CONTRACT = "finance.source_batch/v1"
MAX_ROWS = 50_000
MAX_SUMMARY = 2_000
MAX_PERIOD_DAYS = 92
DECLARATIONS = ("balance_chain", "summary", "opening_closing", "complete_period")
_ROW_FIELDS = {"source_key", "business_date", "posted_at", "amount", "item_l1", "item_l2", "counterparty",
               "waybill_no", "reference", "balance_before", "balance_after", "raw"}
_TOP_FIELDS = {"contract", "source_key", "period", "declares", "opening_balance", "closing_balance", "summary", "rows",
               "evidence"}
_TEXT_LIMITS = {"item_l1": 128, "item_l2": 128, "counterparty": 255, "reference": 255, "waybill_no": 128, "posted_at": 40}


class IntakeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _schema(message: str) -> IntakeError:
    return IntakeError("INTAKE_SCHEMA", message)


def _date(value: Any, label: str) -> dt.date:
    if not isinstance(value, str) or len(value) != 10:
        raise _schema(f"{label} 必须是 YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise _schema(f"{label} 必须是 YYYY-MM-DD") from None


def _amount(value: Any, label: str, *, allow_zero: bool = True) -> Decimal:
    if not isinstance(value, str):
        raise IntakeError("AMOUNT_INVALID", f"{label} 必须是十进制字符串")
    try:
        amount = Decimal(value.strip())
    except InvalidOperation:
        raise IntakeError("AMOUNT_INVALID", f"{label} 不是有效金额") from None
    if not amount.is_finite() or amount.as_tuple().exponent < -4 or abs(amount) >= Decimal("1e16"):
        raise IntakeError("AMOUNT_INVALID", f"{label} 超出范围或超过 4 位小数")
    if not allow_zero and amount == 0:
        raise IntakeError("AMOUNT_INVALID", f"{label} 不能为 0")
    return amount


def _text(value: Any, label: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or any(ord(ch) < 32 for ch in value):
        raise _schema(f"{label} 必须是不超过 {limit} 字的文本")
    return value.strip()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_batch(payload: Any) -> dict[str, Any]:
    """Return the normalized batch with ``checks``; raise ``IntakeError`` on the first failure."""

    if not isinstance(payload, dict) or set(payload) - _TOP_FIELDS:
        raise _schema("批次字段不符合契约")
    if payload.get("contract") != CONTRACT:
        raise _schema(f"contract 必须是 {CONTRACT}")
    source_key = payload.get("source_key")
    if not isinstance(source_key, str) or not source_key or len(source_key) > 64:
        raise _schema("source_key 无效")
    period = payload.get("period")
    if not isinstance(period, dict) or set(period) != {"start", "end"}:
        raise _schema("period 必须包含 start 和 end")
    start, end = _date(period["start"], "period.start"), _date(period["end"], "period.end")
    if end < start or (end - start).days >= MAX_PERIOD_DAYS:
        raise _schema(f"期间无效（最长 {MAX_PERIOD_DAYS} 天）")
    declares = payload.get("declares")
    if not isinstance(declares, dict) or set(declares) != set(DECLARATIONS) or any(type(v) is not bool for v in declares.values()):
        raise _schema("declares 必须逐项声明 " + "、".join(DECLARATIONS))
    rows_in = payload.get("rows")
    if not isinstance(rows_in, list) or len(rows_in) > MAX_ROWS:
        raise _schema(f"rows 必须是不超过 {MAX_ROWS} 行的列表")
    rows, keys = [], set()
    for index, raw in enumerate(rows_in, start=1):
        label = f"第 {index} 行"
        if not isinstance(raw, dict) or set(raw) - _ROW_FIELDS or not {"source_key", "business_date", "amount"} <= set(raw):
            raise _schema(f"{label} 字段不符合契约")
        key = raw["source_key"]
        if not isinstance(key, str) or not key.strip() or len(key) > 191 or any(ord(ch) < 32 for ch in key):
            raise _schema(f"{label} source_key 无效")
        if key in keys:
            raise IntakeError("INTAKE_DUPLICATE", f"{label} source_key 在批次内重复：{key}")
        keys.add(key)
        day = _date(raw["business_date"], f"{label} business_date")
        if not start <= day <= end:
            raise _schema(f"{label} 业务日期不在期间内")
        row = {"source_key": key, "business_date": day.isoformat(),
               "amount": _amount(raw["amount"], f"{label} amount", allow_zero=False)}
        for field, limit in _TEXT_LIMITS.items():
            row[field] = _text(raw.get(field), f"{label} {field}", limit)
        row["waybill_no"] = row["waybill_no"] or None
        row["posted_at"] = row["posted_at"] or None
        for field in ("balance_before", "balance_after"):
            row[field] = None if raw.get(field) is None else _amount(raw[field], f"{label} {field}")
        extra = raw.get("raw", {})
        if not isinstance(extra, dict) or len(canonical(extra)) > 16_384:
            raise _schema(f"{label} raw 必须是不超过 16KB 的对象")
        row["raw"] = extra
        row["row_sha256"] = hashlib.sha256(canonical({**row, "amount": format(row["amount"], "f"),
                                                      **{f: None if row[f] is None else format(row[f], "f")
                                                         for f in ("balance_before", "balance_after")}})).hexdigest()
        rows.append(row)
    checks = [{"check": "schema", "ran": True, "ok": True}, {"check": "source_key_unique", "ran": True, "ok": True}]

    if declares["balance_chain"]:
        previous = None
        for index, row in enumerate(rows, start=1):
            if row["balance_before"] is None or row["balance_after"] is None:
                raise IntakeError("INTAKE_BALANCE_CHAIN", f"第 {index} 行缺少前后余额")
            if row["balance_before"] + row["amount"] != row["balance_after"]:
                raise IntakeError("INTAKE_BALANCE_CHAIN", f"第 {index} 行前余额 + 金额 ≠ 后余额")
            if previous is not None and row["balance_before"] != previous:
                raise IntakeError("INTAKE_BALANCE_CHAIN", f"第 {index} 行前余额与上一行后余额不连续")
            previous = row["balance_after"]
    checks.append({"check": "balance_chain", "ran": declares["balance_chain"], "ok": True})

    opening = closing = None
    if declares["opening_closing"]:
        opening = _amount(payload.get("opening_balance"), "opening_balance")
        closing = _amount(payload.get("closing_balance"), "closing_balance")
        if opening + sum((row["amount"] for row in rows), Decimal("0")) != closing:
            raise IntakeError("INTAKE_OPENING_CLOSING", "期初 + 流水合计 ≠ 期末")
        if declares["balance_chain"] and rows and (rows[0]["balance_before"] != opening or rows[-1]["balance_after"] != closing):
            raise IntakeError("INTAKE_OPENING_CLOSING", "余额链首尾与期初期末不一致")
    elif payload.get("opening_balance") is not None or payload.get("closing_balance") is not None:
        raise _schema("未声明 opening_closing 时不能提交期初期末余额")
    checks.append({"check": "opening_closing", "ran": declares["opening_closing"], "ok": True})

    if declares["summary"]:
        summary = payload.get("summary")
        if not isinstance(summary, list) or len(summary) > MAX_SUMMARY:
            raise IntakeError("INTAKE_SUMMARY", "summary 必须是列表")
        expected: dict[tuple[str, str, str], list[Any]] = {}
        for row in rows:
            bucket = expected.setdefault((row["item_l1"], row["item_l2"], "in" if row["amount"] > 0 else "out"), [0, Decimal("0")])
            bucket[0] += 1
            bucket[1] += abs(row["amount"])
        declared: dict[tuple[str, str, str], list[Any]] = {}
        for index, item in enumerate(summary, start=1):
            if not isinstance(item, dict) or set(item) != {"item_l1", "item_l2", "direction", "count", "amount"}:
                raise IntakeError("INTAKE_SUMMARY", f"汇总第 {index} 项字段不符合契约")
            if item["direction"] not in ("in", "out") or type(item["count"]) is not int or item["count"] < 0:
                raise IntakeError("INTAKE_SUMMARY", f"汇总第 {index} 项方向或笔数无效")
            key = (_text(item["item_l1"], "summary.item_l1", 128), _text(item["item_l2"], "summary.item_l2", 128), item["direction"])
            if key in declared:
                raise IntakeError("INTAKE_SUMMARY", f"汇总第 {index} 项重复")
            declared[key] = [item["count"], abs(_amount(item["amount"], f"汇总第 {index} 项金额"))]
        declared = {key: value for key, value in declared.items() if value[0] or value[1]}
        if declared != expected:
            raise IntakeError("INTAKE_SUMMARY", "明细按项目合计与平台汇总不一致")
    elif payload.get("summary") not in (None, []):
        raise _schema("未声明 summary 时不能提交汇总")
    checks.append({"check": "summary", "ran": declares["summary"], "ok": True})
    checks.append({"check": "complete_period", "ran": declares["complete_period"], "ok": True, "declared_only": True})

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"kind", "refs"} or evidence["kind"] not in ("http_observation", "file"):
        raise IntakeError("INTAKE_EVIDENCE", "evidence 必须是 {kind: http_observation|file, refs: [...]}")
    refs = evidence["refs"]
    if not isinstance(refs, list) or not refs or len(refs) > 2_000 or any(not isinstance(r, (str, int)) or isinstance(r, bool) for r in refs):
        raise IntakeError("INTAKE_EVIDENCE", "evidence.refs 必须是非空列表")

    inflow = sum((row["amount"] for row in rows if row["amount"] > 0), Decimal("0"))
    outflow = -sum((row["amount"] for row in rows if row["amount"] < 0), Decimal("0"))
    content = {"source_key": source_key, "period": {"start": start.isoformat(), "end": end.isoformat()}, "declares": declares,
               "opening": None if opening is None else format(opening, "f"), "closing": None if closing is None else format(closing, "f"),
               "rows": [row["row_sha256"] for row in rows]}
    return {"source_key": source_key, "period_start": start, "period_end": end, "declares": declares, "rows": rows,
            "opening_balance": opening, "closing_balance": closing, "checks": checks, "inflow": inflow, "outflow": outflow,
            "evidence": {"kind": evidence["kind"], "refs": [str(r) for r in refs]},
            "content_sha256": hashlib.sha256(canonical(content)).hexdigest()}
