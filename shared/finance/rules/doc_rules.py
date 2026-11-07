"""Boyi waybill → voucher rules (pure functions, no database).

Every Boyi waybill is confirmed by a person before it is posted. The finance
fingerprint covers only the fields that change the voucher, so an unrelated
edit (receiver address, remark) does not ask for confirmation again.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

PAYMENT_METHODS = ("寄付", "到付", "月结")
PARTY_MODES = ("fixed", "sender_customer", "linked_platform", "choose")
FINGERPRINT_FIELDS = (
    "waybill_no", "open_date", "payment_method", "freight_fee", "transfer_fee",
    "sender_name", "sender_phone", "linked_platform", "status",
)
_CANCELLED = {"cancelled", "已取消", "已作废", "作废", "取消"}


def waybill_fingerprint(row: dict[str, Any]) -> str:
    material = {field: "" if row.get(field) is None else str(row.get(field)).strip() for field in FINGERPRINT_FIELDS}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_cancelled(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").strip().lower() in _CANCELLED


def parse_fee(value: Any) -> tuple[Decimal | None, str | None]:
    """(amount, problem). A blank fee is ``None`` (missing), never zero."""

    text = "" if value is None else str(value).strip().replace(",", "")
    if not text:
        return None, None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None, f"金额“{text}”不是有效数字"
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -4:
        return None, f"金额“{text}”无效"
    return amount, None


def waybill_amounts(row: dict[str, Any]) -> dict[str, Any]:
    """Freight and transfer fee of one waybill and why it cannot be confirmed yet."""

    freight, freight_problem = parse_fee(row.get("freight_fee"))
    transfer, transfer_problem = parse_fee(row.get("transfer_fee"))
    problems = [p for p in (freight_problem, transfer_problem) if p]
    if freight is None and transfer is None and not problems:
        problems.append("运费和中转费都未填写，请先补录金额")
    method = str(row.get("payment_method") or "").strip()
    if method not in PAYMENT_METHODS:
        problems.append(f"结算方式“{method or '空'}”不是寄付、到付或月结")
    freight = freight or Decimal("0")
    transfer = transfer or Decimal("0")
    return {"freight": freight, "transfer": transfer, "total": freight + transfer, "payment_method": method,
            "problems": problems}


def build_voucher(
    *, row: dict[str, Any], rule: dict[str, Any], amounts: dict[str, Any], party_id: int | None, posting_date: Any,
    period_month: str, note: str = "",
) -> dict[str, Any]:
    """The voucher (in ``parse_voucher`` shape) for one confirmed waybill."""

    waybill_no = str(row["waybill_no"])
    summary = f"博益开单 {waybill_no} {amounts['payment_method']}{note}"[:255]
    base = {"site_code": None, "source_id": None, "business_line": None, "fee_level": "waybill", "waybill_no": waybill_no}
    lines = [{**base, "account_code": rule["receivable_account"], "debit": amounts["total"], "credit": Decimal("0"),
              "summary": summary, "party_id": party_id}]
    for amount, account, label in ((amounts["freight"], rule["freight_income_account"], "运费"),
                                   (amounts["transfer"], rule["transfer_income_account"], "中转费")):
        if amount > 0:
            lines.append({**base, "account_code": account, "debit": Decimal("0"), "credit": amount,
                          "summary": f"{summary} {label}"[:255], "party_id": None})
    for index, line in enumerate(lines, start=1):
        line["line_no"] = index
    return {"voucher_type": "business_doc", "posting_date": posting_date, "period_month": period_month,
            "summary": summary, "lines": lines}


def reversal_of(voucher_lines: list[dict[str, Any]], *, posting_date: Any, period_month: str, summary: str) -> dict[str, Any]:
    """A voucher mirroring ``voucher_lines`` (debit ↔ credit), for a period that is already closed."""

    lines = []
    for index, line in enumerate(voucher_lines, start=1):
        lines.append({"line_no": index, "account_code": line["account_code"], "debit": Decimal(line["credit"]),
                      "credit": Decimal(line["debit"]), "summary": f"冲销：{line['summary']}"[:255],
                      "site_code": line.get("site_code"), "source_id": line.get("source_id"),
                      "business_line": line.get("business_line"), "fee_level": line.get("fee_level"),
                      "waybill_no": line.get("waybill_no"), "party_id": line.get("party_id")})
    return {"voucher_type": "reversal", "posting_date": posting_date, "period_month": period_month,
            "summary": summary[:255], "lines": lines}
