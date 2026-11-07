"""Financial statements read only from the GL (P5).

利润表 excludes the month-end closing vouchers (they only move profit to
本年利润); 资产负债表 includes everything and adds profit not yet closed to
未分配利润 so it balances in open months; 现金流量表 (direct method) attributes
each fund movement to its counter accounts' cash-flow category. Statement
lines come from ``finance_report_lines`` (first-level account → line).
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from shared.finance.db import fetchall, fetchone
from shared.finance.errors import FinanceError, FinanceValidationError
from shared.finance.models import month_bounds, parse_date, parse_month

ZERO = Decimal("0")
FUND_TYPES = ("cash", "bank", "fund", "transit")
CASH_FLOW_CATEGORIES = ("operating", "investing", "financing")
PL_LINES = (
    ("revenue", "一、营业收入", 1), ("cost", "减：营业成本", -1), ("taxes", "税金及附加", -1),
    ("selling_expense", "销售费用", -1), ("admin_expense", "管理费用", -1), ("finance_expense", "财务费用", -1),
    ("other_operating", "其他经营损益", 1),
)
PL_BELOW = (("non_operating_income", "加：营业外收入", 1), ("non_operating_expense", "减：营业外支出", -1))
BS_ASSET_LINES = (("cash", "货币资金"), ("receivables", "应收账款"), ("prepayments", "预付账款"),
                  ("other_receivables", "其他应收款"), ("other_assets", "其他资产"))
BS_LIABILITY_LINES = (("payables", "应付账款"), ("advances_received", "预收账款"), ("payroll_payable", "应付职工薪酬"),
                      ("taxes_payable", "应交税费"), ("other_payables", "其他应付款"), ("other_liabilities", "其他负债"))
BS_EQUITY_LINES = (("paid_in_capital", "实收资本"), ("retained_earnings", "未分配利润（含本年利润）"), ("other_equity", "其他权益"))
MAX_MARGIN_ROWS = 2000
SPLIT_DIMENSIONS = ("site_code", "business_line")


def _text(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}"


class StatementsMixin:
    @staticmethod
    def _accounts_with_lines(cursor: Any) -> dict[str, dict[str, Any]]:
        cursor.execute(
            "SELECT a.code, a.name, a.parent_code, a.root_type, a.account_type, a.normal_balance, a.cash_flow_category,"
            " a.is_group, r.statement, r.line_key FROM finance_accounts a LEFT JOIN finance_report_lines r ON r.account_code=a.code"
        )
        rows = {row["code"]: row for row in fetchall(cursor)}
        for row in rows.values():
            top = row
            while top["parent_code"]:
                top = rows[top["parent_code"]]
            row["top_code"] = top["code"]
            row["top_line"] = top["line_key"]
        return rows

    @staticmethod
    def _movements(cursor: Any, start: dt.date | None, end: dt.date, *, exclude_closing: bool) -> dict[str, tuple[Decimal, Decimal]]:
        conditions = ["g.is_cancelled=0", "g.posting_date <= %s"]
        params: list[Any] = [end]
        if start is not None:
            conditions.append("g.posting_date >= %s")
            params.append(start)
        if exclude_closing:
            conditions.append("v.voucher_type <> 'closing'")
        cursor.execute(
            "SELECT g.account_code, COALESCE(SUM(g.debit), 0) AS d, COALESCE(SUM(g.credit), 0) AS c FROM finance_gl_entries g"
            f" JOIN finance_vouchers v ON v.id=g.voucher_id WHERE {' AND '.join(conditions)} GROUP BY g.account_code",
            tuple(params),
        )
        return {row["account_code"]: (Decimal(row["d"]), Decimal(row["c"])) for row in fetchall(cursor)}

    # ---------- profit and loss ----------
    @staticmethod
    def _add_pl(amounts: dict[str, Decimal], account: dict[str, Any], debit: Decimal, credit: Decimal) -> None:
        line = account["top_line"] or "other_operating"
        # Revenue-like lines show credit - debit; expense lines show debit - credit.
        amounts[line] += (credit - debit) if line in ("revenue", "non_operating_income", "other_operating") else (debit - credit)

    def _profit_loss_amounts(self, cursor: Any, start: dt.date, end: dt.date, accounts: dict[str, dict[str, Any]]) -> dict[str, Decimal]:
        amounts = {key: ZERO for key, _, _ in (*PL_LINES, *PL_BELOW)}
        for code, (debit, credit) in self._movements(cursor, start, end, exclude_closing=True).items():
            if accounts[code]["root_type"] == "profit_loss":
                self._add_pl(amounts, accounts[code], debit, credit)
        return amounts

    def _profit_loss_by_dimension(self, cursor: Any, start: dt.date, end: dt.date, accounts: dict[str, dict[str, Any]],
                                  dimension: str) -> dict[str, dict[str, Decimal]]:
        """P&L line amounts per value of one GL dimension ('' = not specified)."""

        cursor.execute(
            f"SELECT g.account_code, COALESCE(g.{dimension}, '') AS dim, COALESCE(SUM(g.debit), 0) AS d, COALESCE(SUM(g.credit), 0) AS c"
            " FROM finance_gl_entries g JOIN finance_vouchers v ON v.id=g.voucher_id JOIN finance_accounts a ON a.code=g.account_code"
            " WHERE g.is_cancelled=0 AND v.voucher_type <> 'closing' AND a.root_type='profit_loss' AND g.posting_date BETWEEN %s AND %s"
            f" GROUP BY g.account_code, COALESCE(g.{dimension}, '')", (start, end),
        )
        result: dict[str, dict[str, Decimal]] = {}
        for row in fetchall(cursor):
            amounts = result.setdefault(row["dim"], {key: ZERO for key, _, _ in (*PL_LINES, *PL_BELOW)})
            self._add_pl(amounts, accounts[row["account_code"]], Decimal(row["d"]), Decimal(row["c"]))
        return result

    @staticmethod
    def _profit_loss_lines(amounts: dict[str, Decimal]) -> tuple[list[dict[str, Any]], Decimal]:
        lines = []
        operating = ZERO
        for key, label, sign in PL_LINES:
            operating += amounts[key] * sign
            lines.append({"key": key, "label": label, "amount": amounts[key]})
        lines.append({"key": "operating_profit", "label": "二、营业利润", "amount": operating, "subtotal": True})
        total = operating
        for key, label, sign in PL_BELOW:
            total += amounts[key] * sign
            lines.append({"key": key, "label": label, "amount": amounts[key]})
        lines.append({"key": "total_profit", "label": "三、利润总额", "amount": total, "subtotal": True})
        lines.append({"key": "income_tax", "label": "减：所得税费用", "amount": ZERO})
        lines.append({"key": "net_profit", "label": "四、净利润", "amount": total, "subtotal": True})
        return lines, total

    def _profit_loss_in(self, cursor: Any, month: str) -> dict[str, Any]:
        start, end = month_bounds(month)
        accounts = self._accounts_with_lines(cursor)
        month_lines, month_profit = self._profit_loss_lines(self._profit_loss_amounts(cursor, start, end, accounts))
        year_lines, year_profit = self._profit_loss_lines(self._profit_loss_amounts(cursor, dt.date(start.year, 1, 1), end, accounts))
        rows = [{"key": m["key"], "label": m["label"], "subtotal": bool(m.get("subtotal")), "month": _text(m["amount"]),
                 "year_to_date": _text(y["amount"])} for m, y in zip(month_lines, year_lines)]
        return {"month": month, "rows": rows, "net_profit": _text(month_profit), "net_profit_ytd": _text(year_profit)}

    def profit_loss(self, month: Any, *, split: Any = None, scope: Any = "month") -> dict[str, Any]:
        """利润表; with ``split`` (site_code / business_line) one column per dimension value plus 合计."""

        month = parse_month(month)
        if split in (None, ""):
            return self._read(lambda cursor: self._profit_loss_in(cursor, month))
        if split not in SPLIT_DIMENSIONS or scope not in ("month", "year"):
            raise FinanceValidationError("拆分维度或期间范围无效。")
        start, end = month_bounds(month)
        if scope == "year":
            start = dt.date(start.year, 1, 1)

        def work(cursor: Any) -> dict[str, Any]:
            accounts = self._accounts_with_lines(cursor)
            by_value = self._profit_loss_by_dimension(cursor, start, end, accounts, split)
            values = sorted(v for v in by_value if v) + ([""] if "" in by_value else [])
            columns = [{"key": value, "label": value or "未指定"} for value in values]
            per_column = {value: self._profit_loss_lines(by_value[value])[0] for value in values}
            total_amounts = {key: sum((by_value[v][key] for v in values), ZERO) for key, _, _ in (*PL_LINES, *PL_BELOW)}
            total_lines, total_profit = self._profit_loss_lines(total_amounts)
            rows = []
            for index, line in enumerate(total_lines):
                rows.append({"key": line["key"], "label": line["label"], "subtotal": bool(line.get("subtotal")),
                             "values": {value: _text(per_column[value][index]["amount"]) for value in values},
                             "total": _text(line["amount"])})
            return {"month": month, "split": split, "scope": scope, "start_date": start.isoformat(), "end_date": end.isoformat(),
                    "columns": columns, "rows": rows, "net_profit": _text(total_profit)}

        return self._read(work)

    # ---------- balance sheet ----------
    def balance_sheet(self, month: Any) -> dict[str, Any]:
        month = parse_month(month)
        _, end = month_bounds(month)

        def work(cursor: Any) -> dict[str, Any]:
            accounts = self._accounts_with_lines(cursor)
            amounts: dict[str, Decimal] = {key: ZERO for key, _ in (*BS_ASSET_LINES, *BS_LIABILITY_LINES, *BS_EQUITY_LINES)}
            unclosed_profit = ZERO
            for code, (debit, credit) in self._movements(cursor, None, end, exclude_closing=False).items():
                account = accounts[code]
                net = debit - credit
                root = account["root_type"]
                if root == "profit_loss":
                    unclosed_profit -= net
                    continue
                line = account["top_line"] if accounts[account["top_code"]]["statement"] == "balance_sheet" else None
                if root in ("asset", "cost"):
                    amounts[line if line in dict(BS_ASSET_LINES) else "other_assets"] += net
                elif root == "liability":
                    amounts[line if line in dict(BS_LIABILITY_LINES) else "other_liabilities"] -= net
                else:
                    amounts[line if line in dict(BS_EQUITY_LINES) else "other_equity"] -= net
            amounts["retained_earnings"] += unclosed_profit

            def section(lines: tuple[tuple[str, str], ...]) -> tuple[list[dict[str, str]], Decimal]:
                return [{"key": key, "label": label, "amount": _text(amounts[key])} for key, label in lines], sum((amounts[k] for k, _ in lines), ZERO)

            assets, total_assets = section(BS_ASSET_LINES)
            liabilities, total_liabilities = section(BS_LIABILITY_LINES)
            equity, total_equity = section(BS_EQUITY_LINES)
            return {"month": month, "as_of": end.isoformat(), "assets": assets, "liabilities": liabilities, "equity": equity,
                    "total_assets": _text(total_assets), "total_liabilities": _text(total_liabilities), "total_equity": _text(total_equity),
                    "total_liabilities_and_equity": _text(total_liabilities + total_equity),
                    "unclosed_profit": _text(unclosed_profit), "balanced": total_assets == total_liabilities + total_equity}

        return self._read(work)

    # ---------- cash flow (direct method) ----------
    def cash_flow(self, month: Any) -> dict[str, Any]:
        month = parse_month(month)
        start, end = month_bounds(month)

        def work(cursor: Any) -> dict[str, Any]:
            accounts = self._accounts_with_lines(cursor)
            cursor.execute(
                "SELECT voucher_id, source_line_id, account_code, debit, credit FROM finance_gl_entries"
                " WHERE is_cancelled=0 AND posting_date BETWEEN %s AND %s ORDER BY voucher_id, id", (start, end),
            )
            groups: dict[tuple[int, Any], list[dict[str, Any]]] = {}
            for row in fetchall(cursor):
                groups.setdefault((int(row["voucher_id"]), row["source_line_id"]), []).append(row)
            flows = {category: {"inflow": ZERO, "outflow": ZERO} for category in CASH_FLOW_CATEGORIES}
            by_account: dict[tuple[str, str], Decimal] = {}
            fund_change = ZERO
            for entries in groups.values():
                fund = [e for e in entries if accounts[e["account_code"]]["account_type"] in FUND_TYPES]
                fund_net = sum((Decimal(e["debit"]) - Decimal(e["credit"]) for e in fund), ZERO)
                fund_change += fund_net
                if fund_net == 0:
                    continue
                counters = [e for e in entries if accounts[e["account_code"]]["account_type"] not in FUND_TYPES]
                weights = [(e, abs(Decimal(e["credit"]) - Decimal(e["debit"]))) for e in counters]
                total_weight = sum((w for _, w in weights), ZERO)
                if total_weight == 0:
                    weights, total_weight = [({"account_code": "", "debit": 0, "credit": 0}, Decimal(1))], Decimal(1)
                remaining = fund_net
                for index, (entry, weight) in enumerate(weights):
                    share = remaining if index == len(weights) - 1 else (fund_net * weight / total_weight).quantize(Decimal("0.0001"))
                    remaining -= share
                    account = accounts.get(entry["account_code"])
                    category = (account or {}).get("cash_flow_category") or "operating"
                    flows[category]["inflow" if share > 0 else "outflow"] += abs(share)
                    top = account["top_code"] if account else "未分类"
                    key = (category, f"{top} {accounts[top]['name']}" if account else "未分类")
                    by_account[key] = by_account.get(key, ZERO) + share
            rows = []
            net_total = ZERO
            labels = {"operating": "经营活动", "investing": "投资活动", "financing": "筹资活动"}
            for category in CASH_FLOW_CATEGORIES:
                net = flows[category]["inflow"] - flows[category]["outflow"]
                net_total += net
                rows.append({"category": category, "label": labels[category], "inflow": _text(flows[category]["inflow"]),
                             "outflow": _text(flows[category]["outflow"]), "net": _text(net),
                             "details": [{"account": name, "amount": _text(value)} for (cat, name), value in sorted(by_account.items())
                                         if cat == category and value]})
            return {"month": month, "rows": rows, "net_change": _text(net_total), "fund_balance_change": _text(fund_change),
                    "balanced": net_total == fund_change}

        return self._read(work)

    # ---------- per-waybill margin ----------
    def waybill_margin(self, *, start_date: Any, end_date: Any) -> dict[str, Any]:
        start, end = parse_date(start_date, "开始日期"), parse_date(end_date, "结束日期")
        if end < start or (end - start).days >= 366:
            raise FinanceValidationError("日期范围无效（最多 366 天）。")

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute(
                "SELECT g.waybill_no,"
                " COALESCE(SUM(CASE WHEN a.account_type='income' THEN g.credit - g.debit END), 0) AS revenue,"
                " COALESCE(SUM(CASE WHEN a.account_type='cost' THEN g.debit - g.credit END), 0) AS cost"
                " FROM finance_gl_entries g JOIN finance_accounts a ON a.code=g.account_code"
                " WHERE g.is_cancelled=0 AND g.waybill_no IS NOT NULL AND g.waybill_no <> '' AND g.posting_date BETWEEN %s AND %s"
                " AND a.account_type IN ('income', 'cost') GROUP BY g.waybill_no ORDER BY g.waybill_no LIMIT %s",
                (start, end, MAX_MARGIN_ROWS + 1),
            )
            rows = fetchall(cursor)
            if len(rows) > MAX_MARGIN_ROWS:
                raise FinanceError("REPORT_TOO_LARGE", f"超过 {MAX_MARGIN_ROWS} 票，请缩短日期范围。", http_status=422)
            output, revenue, cost = [], ZERO, ZERO
            for row in rows:
                r, c = Decimal(row["revenue"]), Decimal(row["cost"])
                revenue += r
                cost += c
                output.append({"waybill_no": row["waybill_no"], "revenue": _text(r), "cost": _text(c), "margin": _text(r - c)})
            return {"start_date": start.isoformat(), "end_date": end.isoformat(), "rows": output,
                    "totals": {"revenue": _text(revenue), "cost": _text(cost), "margin": _text(revenue - cost)}}

        return self._read(work)

    # ---------- operating summary for AI / Feishu ----------
    def ledger_summary(self, *, start_date: dt.date, end_date: dt.date) -> dict[str, Any]:
        """Revenue, cost, expenses and profit of a date range, with the caveats a reader must see."""

        def work(cursor: Any) -> dict[str, Any]:
            accounts = self._accounts_with_lines(cursor)
            amounts = self._profit_loss_amounts(cursor, start_date, end_date, accounts)
            _, profit = self._profit_loss_lines(amounts)
            expense = amounts["taxes"] + amounts["selling_expense"] + amounts["admin_expense"] + amounts["finance_expense"] + amounts["non_operating_expense"]
            other_income = amounts["other_operating"] + amounts["non_operating_income"]
            cursor.execute(
                "SELECT COUNT(*) AS n FROM finance_vouchers WHERE status='submitted' AND voucher_type <> 'closing'"
                " AND posting_date BETWEEN %s AND %s", (start_date, end_date),
            )
            voucher_count = int(fetchone(cursor)["n"])
            settings = self._load_settings(cursor)
            cursor.execute(
                "SELECT COALESCE(SUM(debit - credit), 0) AS net FROM finance_gl_entries WHERE is_cancelled=0 AND account_code=%s"
                " AND posting_date <= %s", (settings["suspense_account"], end_date),
            )
            suspense = Decimal(fetchone(cursor)["net"])
            cursor.execute("SELECT MAX(period_month) AS m FROM finance_periods WHERE status='closed'")
            closed_through = fetchone(cursor)["m"]
            cursor.execute(
                "SELECT COUNT(*) AS n FROM boyi_waybills w WHERE w.open_date BETWEEN %s AND %s"
                " AND COALESCE(w.status, '') NOT IN ('cancelled', '已作废', '作废', '已取消', '取消')"
                " AND NOT EXISTS (SELECT 1 FROM finance_doc_postings p WHERE p.doc_type='boyi_waybill' AND p.doc_id=w.id"
                " AND p.status IN ('posted', 'zero'))", (start_date.isoformat(), end_date.isoformat()),
            )
            unconfirmed = int(fetchone(cursor)["n"])
            warnings = []
            if not settings["start_month"]:
                warnings.append("总账尚未设定启用月")
            if suspense:
                warnings.append(f"暂记科目还有 {abs(suspense):.2f} 元未分类")
            if unconfirmed:
                warnings.append(f"期间内有 {unconfirmed} 张博益开单尚未确认记账")
            end_month = f"{end_date:%Y-%m}"
            if not closed_through or closed_through < end_month:
                warnings.append("所查期间尚未全部封账，金额可能变动")
            return {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "voucher_count": voucher_count,
                    "revenue": _text(amounts["revenue"]), "cost": _text(amounts["cost"]), "expense": _text(expense),
                    "other_income": _text(other_income), "profit": _text(profit), "closed_through": closed_through,
                    "warnings": warnings}

        return self._read(work)
