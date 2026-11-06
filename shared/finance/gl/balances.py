"""Ledger reports read only from the GL: trial balance, account ledger, journal.

Cancelled entries (both the original and its mirror) are excluded. Balances are
signed debit-minus-credit internally and shown on the debit or credit side.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from shared.finance.db import fetchall, fetchone
from shared.finance.errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from shared.finance.gl.accounts import ACCOUNT_COLUMNS
from shared.finance.models import month_bounds, parse_account_code, parse_date, parse_month

ZERO = Decimal("0")
MAX_LEDGER_DAYS = 366
MAX_LEDGER_ROWS = 5000


def _text(value: Decimal) -> str:
    return f"{value:.4f}"


def _sides(net: Decimal) -> tuple[str, str]:
    return (_text(net), _text(ZERO)) if net >= 0 else (_text(ZERO), _text(-net))


def _direction(net: Decimal) -> str:
    return "平" if net == 0 else ("借" if net > 0 else "贷")


class ReportsMixin:
    def trial_balance(self, month: Any, *, include_zero: bool = False) -> dict[str, Any]:
        """科目余额表: opening, period debit/credit and closing per account, rolled up."""

        month = parse_month(month)
        start, end = month_bounds(month)

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute(f"SELECT {ACCOUNT_COLUMNS} FROM finance_accounts ORDER BY code")
            accounts = {row["code"]: row for row in fetchall(cursor)}
            cursor.execute(
                "SELECT account_code,"
                " COALESCE(SUM(CASE WHEN posting_date < %s THEN debit - credit END), 0) AS opening,"
                " COALESCE(SUM(CASE WHEN posting_date >= %s THEN debit END), 0) AS period_debit,"
                " COALESCE(SUM(CASE WHEN posting_date >= %s THEN credit END), 0) AS period_credit"
                " FROM finance_gl_entries WHERE is_cancelled=0 AND posting_date <= %s GROUP BY account_code",
                (start, start, start, end),
            )
            totals = {code: {"opening": ZERO, "period_debit": ZERO, "period_credit": ZERO} for code in accounts}
            for row in fetchall(cursor):
                node = row["account_code"]
                while node:
                    bucket = totals[node]
                    bucket["opening"] += Decimal(row["opening"])
                    bucket["period_debit"] += Decimal(row["period_debit"])
                    bucket["period_credit"] += Decimal(row["period_credit"])
                    node = accounts[node]["parent_code"]
            rows, grand = [], {"opening": ZERO, "period_debit": ZERO, "period_credit": ZERO, "closing": ZERO}
            grand_sides = {key: ZERO for key in ("opening_debit", "opening_credit", "closing_debit", "closing_credit")}
            for code, account in accounts.items():
                bucket = totals[code]
                closing = bucket["opening"] + bucket["period_debit"] - bucket["period_credit"]
                if not include_zero and not any((bucket["opening"], bucket["period_debit"], bucket["period_credit"], closing)):
                    continue
                opening_debit, opening_credit = _sides(bucket["opening"])
                closing_debit, closing_credit = _sides(closing)
                rows.append({
                    "code": code, "name": account["name"], "parent_code": account["parent_code"],
                    "level": (len(code) - 2) // 2, "is_group": bool(account["is_group"]),
                    "normal_balance": account["normal_balance"],
                    "opening_debit": opening_debit, "opening_credit": opening_credit,
                    "period_debit": _text(bucket["period_debit"]), "period_credit": _text(bucket["period_credit"]),
                    "closing_debit": closing_debit, "closing_credit": closing_credit,
                })
                if account["parent_code"] is None:
                    grand["period_debit"] += bucket["period_debit"]
                    grand["period_credit"] += bucket["period_credit"]
                    for prefix, net in (("opening", bucket["opening"]), ("closing", closing)):
                        if net >= 0:
                            grand_sides[f"{prefix}_debit"] += net
                        else:
                            grand_sides[f"{prefix}_credit"] -= net
            summary = {key: _text(value) for key, value in grand_sides.items()}
            summary["period_debit"] = _text(grand["period_debit"])
            summary["period_credit"] = _text(grand["period_credit"])
            balanced = (
                grand_sides["opening_debit"] == grand_sides["opening_credit"]
                and grand["period_debit"] == grand["period_credit"]
                and grand_sides["closing_debit"] == grand_sides["closing_credit"]
            )
            return {"month": month, "rows": rows, "totals": summary, "balanced": balanced}

        return self._read(work)

    def account_ledger(self, account_code: Any, *, start_date: Any, end_date: Any) -> dict[str, Any]:
        """明细账: one account (with its sub-accounts) entry by entry with a running balance."""

        code = parse_account_code(account_code)
        start = parse_date(start_date, "开始日期")
        end = parse_date(end_date, "结束日期")
        if end < start:
            raise FinanceValidationError("结束日期不能早于开始日期。")
        if (end - start).days >= MAX_LEDGER_DAYS:
            raise FinanceValidationError("明细账一次最多查询 366 天。")

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute(f"SELECT {ACCOUNT_COLUMNS} FROM finance_accounts WHERE code=%s", (code,))
            account = fetchone(cursor)
            if account is None:
                raise FinanceNotFoundError(f"科目 {code} 不存在。", code="ACCOUNT_NOT_FOUND")
            pattern = code + "%"
            cursor.execute(
                "SELECT COALESCE(SUM(debit - credit), 0) AS net FROM finance_gl_entries"
                " WHERE is_cancelled=0 AND account_code LIKE %s AND posting_date < %s",
                (pattern, start),
            )
            balance = Decimal(fetchone(cursor)["net"])
            opening = balance
            cursor.execute(
                "SELECT g.posting_date, g.voucher_id, v.voucher_no, g.account_code, a.name AS account_name, g.summary,"
                " g.debit, g.credit, g.waybill_no, g.site_code, g.source_id, g.business_line"
                " FROM finance_gl_entries g JOIN finance_vouchers v ON v.id=g.voucher_id"
                " JOIN finance_accounts a ON a.code=g.account_code"
                " WHERE g.is_cancelled=0 AND g.account_code LIKE %s AND g.posting_date BETWEEN %s AND %s"
                " ORDER BY g.posting_date, v.voucher_no, g.id LIMIT %s",
                (pattern, start, end, MAX_LEDGER_ROWS + 1),
            )
            entries = fetchall(cursor)
            if len(entries) > MAX_LEDGER_ROWS:
                raise FinanceError("LEDGER_TOO_LARGE", f"明细超过 {MAX_LEDGER_ROWS} 行，请缩短日期范围。", http_status=422)
            rows, total_debit, total_credit = [], ZERO, ZERO
            for entry in entries:
                debit, credit = Decimal(entry["debit"]), Decimal(entry["credit"])
                balance += debit - credit
                total_debit += debit
                total_credit += credit
                rows.append({
                    "posting_date": entry["posting_date"].isoformat(), "voucher_id": int(entry["voucher_id"]),
                    "voucher_no": entry["voucher_no"], "account_code": entry["account_code"],
                    "account_name": entry["account_name"], "summary": entry["summary"],
                    "debit": _text(debit), "credit": _text(credit), "waybill_no": entry["waybill_no"],
                    "site_code": entry["site_code"], "source_id": entry["source_id"], "business_line": entry["business_line"],
                    "direction": _direction(balance), "balance": _text(abs(balance)),
                })
            return {
                "account": {"code": code, "name": account["name"], "normal_balance": account["normal_balance"]},
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "opening": {"direction": _direction(opening), "balance": _text(abs(opening))},
                "rows": rows,
                "totals": {"debit": _text(total_debit), "credit": _text(total_credit)},
                "closing": {"direction": _direction(balance), "balance": _text(abs(balance))},
            }

        return self._read(work)

    def journal(self, month: Any) -> dict[str, Any]:
        """序时账: submitted and cancelled vouchers of one month with their lines."""

        month = parse_month(month)
        vouchers = [v for v in self.list_vouchers(month=month, with_lines=True) if v["status"] != "draft"]
        return {"month": month, "vouchers": vouchers}

