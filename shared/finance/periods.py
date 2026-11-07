"""Month-end close (P5): server-side checks, closing vouchers, balance snapshot, reopen.

Close (super administrator, one transaction): lock the period → recompute
every check (any blocking failure refuses with the details) → move the
month's profit and loss to 本年利润 (and, in December, 本年利润 to 未分配利润)
→ snapshot every account's opening, debit, credit and closing → read the
snapshot back against the GL → mark the period closed. A closed period
refuses every GL write. Reopen cancels the closing vouchers in the reopened
period, keeps the old snapshot marked ``reopened`` and bumps the version.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

from shared.finance.db import fetchall, fetchone, json_text
from shared.finance.errors import FinanceError, FinanceValidationError
from shared.finance.models import FinanceActor, month_bounds, optional_text, parse_month, parse_request_id

ZERO = Decimal("0")


def _previous_month(month: str) -> str:
    year, number = int(month[:4]), int(month[5:])
    return f"{year - (number == 1):04d}-{(number - 2) % 12 + 1:02d}"


def _check(key: str, label: str, ok: bool, detail: str = "", *, blocking: bool = True) -> dict[str, Any]:
    return {"key": key, "label": label, "ok": bool(ok), "blocking": blocking, "detail": detail}


class PeriodCloseMixin:
    def _period_checks(self, cursor: Any, month: str) -> list[dict[str, Any]]:
        start, end = month_bounds(month)
        settings = self._load_settings(cursor)
        checks = []
        start_month = settings["start_month"]
        if not start_month or month < start_month:
            checks.append(_check("start_month", "月份不早于总账启用月", False, f"启用月：{start_month or '未设定'}"))
            return checks
        if month > start_month:
            previous = _previous_month(month)
            cursor.execute("SELECT status FROM finance_periods WHERE period_month=%s", (previous,))
            row = fetchone(cursor)
            checks.append(_check("previous_closed", "上月已封账", bool(row and row["status"] == "closed"), f"{previous}"))
        # 1. every enabled source with a fund account is fully covered by complete batches.
        cursor.execute("SELECT id, display_name, fund_account FROM finance_sources WHERE is_enabled=1 AND fund_account IS NOT NULL")
        sources = fetchall(cursor)
        missing = []
        for source in sources:
            cursor.execute(
                "SELECT period_start, period_end, declares_json FROM finance_source_batches WHERE source_id=%s AND status='accepted'"
                " AND period_start <= %s AND period_end >= %s ORDER BY period_start", (source["id"], end, start),
            )
            covered_to = start - dt.timedelta(days=1)
            for batch in fetchall(cursor):
                declares = batch["declares_json"]
                declares = json.loads(declares) if isinstance(declares, (str, bytes)) else declares
                if not declares.get("complete_period") or batch["period_start"] > covered_to + dt.timedelta(days=1):
                    break
                covered_to = max(covered_to, batch["period_end"])
            if covered_to < end:
                missing.append(source["display_name"])
        checks.append(_check("sources_complete", "每个来源本月都有声明完整的批次覆盖", not missing,
                             "未覆盖：" + "、".join(missing) if missing else f"{len(sources)} 个来源"))
        # 2. every accepted line of the month is posted (or knowingly unposted).
        cursor.execute("SELECT COUNT(*) AS n FROM finance_source_lines WHERE is_active=1 AND posting_status='unposted'"
                       " AND business_date BETWEEN %s AND %s", (start, end))
        unposted = int(fetchone(cursor)["n"])
        checks.append(_check("lines_posted", "流水已全部入账", unposted == 0, f"未入账 {unposted} 笔" if unposted else ""))

        def balance(code: str) -> Decimal:
            cursor.execute("SELECT COALESCE(SUM(debit - credit), 0) AS net FROM finance_gl_entries WHERE is_cancelled=0"
                           " AND account_code=%s AND posting_date <= %s", (code, end))
            return Decimal(fetchone(cursor)["net"])

        suspense = balance(settings["suspense_account"])
        checks.append(_check("suspense_zero", "暂记科目余额为 0", suspense == 0, f"余额 {abs(suspense):.2f}" if suspense else ""))
        transit = balance(settings["transit_account"])
        checks.append(_check("transit_zero", "资金在途余额为 0", transit == 0, f"余额 {abs(transit):.2f}" if transit else ""))
        cursor.execute("SELECT COUNT(*) AS n FROM finance_vouchers WHERE status='draft' AND period_month=%s", (month,))
        drafts = int(fetchone(cursor)["n"])
        checks.append(_check("no_drafts", "没有草稿凭证", drafts == 0, f"草稿 {drafts} 张" if drafts else ""))
        cursor.execute("SELECT COALESCE(SUM(debit), 0) AS d, COALESCE(SUM(credit), 0) AS c FROM finance_gl_entries"
                       " WHERE is_cancelled=0 AND posting_date <= %s", (end,))
        totals = fetchone(cursor)
        checks.append(_check("trial_balanced", "试算平衡", Decimal(totals["d"]) == Decimal(totals["c"])))
        # 7. fund account balance equals the declared closing balance of its sources.
        by_account: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            by_account.setdefault(source["fund_account"], []).append(source)
        mismatched, unverifiable = [], []
        for code, account_sources in by_account.items():
            declared = ZERO
            complete = True
            for source in account_sources:
                cursor.execute("SELECT closing_balance FROM finance_source_batches WHERE source_id=%s AND status='accepted'"
                               " AND period_end=%s AND closing_balance IS NOT NULL", (source["id"], end))
                row = fetchone(cursor)
                if row is None:
                    complete = False
                    break
                declared += Decimal(row["closing_balance"])
            if not complete:
                unverifiable.append(code)
            elif balance(code) != declared:
                mismatched.append(f"{code}（账面 {balance(code):.2f}，来源 {declared:.2f}）")
        checks.append(_check("fund_reconciled", "资金科目余额等于来源声明的期末余额", not mismatched, "；".join(mismatched)))
        if unverifiable:
            checks.append(_check("fund_unverifiable", "部分资金科目无法对账（来源未声明期末余额）", False,
                                 "、".join(unverifiable), blocking=False))
        # 8. every Boyi waybill opened in the month is confirmed.
        counts = self._waybill_queue(cursor, month)["counts"]
        waiting = counts["pending"] + counts["changed"] + counts["cancelled_posted"]
        checks.append(_check("waybills_confirmed", "本月博益开单已全部确认记账", waiting == 0,
                             f"待确认 {counts['pending']}、待重新确认 {counts['changed']}、待冲销 {counts['cancelled_posted']}" if waiting else ""))
        # 9. no open review or anomaly.
        cursor.execute("SELECT COUNT(*) AS n FROM finance_reviews WHERE status='open' AND line_count > 0")
        reviews = int(fetchone(cursor)["n"])
        cursor.execute("SELECT COUNT(*) AS n FROM finance_anomalies WHERE status='open' AND (period_month IS NULL OR period_month <= %s)", (month,))
        anomalies = int(fetchone(cursor)["n"])
        checks.append(_check("no_open_items", "没有未处理的待审和异常", reviews == 0 and anomalies == 0,
                             f"待审 {reviews}、异常 {anomalies}" if reviews or anomalies else ""))
        return checks

    def _snapshot_rows(self, cursor: Any, month: str) -> list[dict[str, Any]]:
        start, end = month_bounds(month)
        cursor.execute(
            "SELECT account_code, COALESCE(SUM(CASE WHEN posting_date < %s THEN debit - credit END), 0) AS opening_net,"
            " COALESCE(SUM(CASE WHEN posting_date >= %s THEN debit END), 0) AS period_debit,"
            " COALESCE(SUM(CASE WHEN posting_date >= %s THEN credit END), 0) AS period_credit"
            " FROM finance_gl_entries WHERE is_cancelled=0 AND posting_date <= %s GROUP BY account_code ORDER BY account_code",
            (start, start, start, end),
        )
        rows = []
        for row in fetchall(cursor):
            opening, debit, credit = Decimal(row["opening_net"]), Decimal(row["period_debit"]), Decimal(row["period_credit"])
            rows.append({"account_code": row["account_code"], "opening_net": opening, "period_debit": debit,
                         "period_credit": credit, "closing_net": opening + debit - credit})
        return rows

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()

    def period_status(self, month: Any) -> dict[str, Any]:
        month = parse_month(month)

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT status, version FROM finance_periods WHERE period_month=%s", (month,))
            period = fetchone(cursor) or {"status": "open", "version": 1}
            cursor.execute("SELECT * FROM finance_period_closes WHERE period_month=%s ORDER BY version DESC", (month,))
            closes = []
            for row in fetchall(cursor):
                closes.append({"id": int(row["id"]), "version": int(row["version"]), "status": row["status"],
                               "net_profit": f"{Decimal(row['net_profit']):.4f}", "closing_voucher_id": row["closing_voucher_id"],
                               "year_end_voucher_id": row["year_end_voucher_id"], "closed_by": row["closed_by"],
                               "closed_at": row["closed_at"].isoformat(sep=" ", timespec="seconds"),
                               "reopened_by": row["reopened_by"], "reopen_reason": row["reopen_reason"],
                               "trial_balance_sha256": row["trial_balance_sha256"]})
            snapshot_matches = None
            if period["status"] == "closed" and closes:
                cursor.execute("SELECT account_code, opening_net, period_debit, period_credit, closing_net FROM finance_account_balances"
                               " WHERE close_id=%s ORDER BY account_code", (closes[0]["id"],))
                stored = [{**r, **{k: Decimal(r[k]) for k in ("opening_net", "period_debit", "period_credit", "closing_net")}}
                          for r in fetchall(cursor)]
                snapshot_matches = stored == self._snapshot_rows(cursor, month)
            checks = self._period_checks(cursor, month) if period["status"] != "closed" else []
            return {"month": month, "status": period["status"], "version": int(period["version"]), "checks": checks,
                    "can_close": period["status"] != "closed" and all(c["ok"] for c in checks if c["blocking"]),
                    "closes": closes, "snapshot_matches": snapshot_matches}

        return self._read(work)

    def close_period(self, month: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        month = parse_month(month)
        if payload and set(payload) - {"note"}:
            raise FinanceValidationError("封账请求包含不支持的字段。")
        canonical = parse_request_id(request_id)
        start, end = month_bounds(month)

        def work(cursor: Any) -> dict[str, Any]:
            settings = self._load_settings(cursor, lock="FOR SHARE")
            cursor.execute("INSERT IGNORE INTO finance_periods (period_month) VALUES (%s)", (month,))
            cursor.execute("SELECT status, version FROM finance_periods WHERE period_month=%s FOR UPDATE", (month,))
            period = fetchone(cursor)
            if period["status"] != "open":
                raise FinanceError("PERIOD_ALREADY_CLOSED", f"{month} 已封账。")
            checks = self._period_checks(cursor, month)
            failed = [c for c in checks if c["blocking"] and not c["ok"]]
            if failed:
                raise FinanceError("PERIOD_CHECKS_FAILED", "封账前检查未通过：" + "；".join(c["label"] for c in failed) + "。",
                                   details={"checks": checks}, http_status=422)
            accounts = self._accounts_with_lines(cursor)
            pl_report = self._profit_loss_in(cursor, month)
            movements = self._movements(cursor, start, end, exclude_closing=True)
            lines, profit = [], ZERO
            base = {"site_code": None, "source_id": None, "business_line": None, "fee_level": None, "waybill_no": None,
                    "party_id": None, "summary": f"期末结转损益 {month}"}
            for code in sorted(movements):
                if accounts[code]["root_type"] != "profit_loss":
                    continue
                debit, credit = movements[code]
                net = debit - credit
                if net == 0:
                    continue
                profit -= net
                lines.append({**base, "account_code": code, "debit": ZERO if net > 0 else -net, "credit": net if net > 0 else ZERO})
            closing_voucher_id = None
            if lines:
                if profit:
                    lines.append({**base, "account_code": settings["current_profit_account"],
                                  "debit": -profit if profit < 0 else ZERO, "credit": profit if profit > 0 else ZERO})
                for index, line in enumerate(lines, start=1):
                    line["line_no"] = index
                voucher = {"voucher_type": "closing", "posting_date": end, "period_month": month,
                           "summary": f"期末结转损益 {month}", "lines": lines}
                closing_voucher_id = self._insert_draft(cursor, voucher, str(uuid.uuid5(uuid.UUID(canonical), "closing")), actor,
                                                        ref_type="period_close", ref_id=month)
                self._submit_in_tx(cursor, closing_voucher_id, actor)
            year_end_voucher_id = None
            if month.endswith("-12"):
                cursor.execute("SELECT COALESCE(SUM(debit - credit), 0) AS net FROM finance_gl_entries WHERE is_cancelled=0"
                               " AND account_code=%s AND posting_date <= %s", (settings["current_profit_account"], end))
                year_net = Decimal(fetchone(cursor)["net"])
                if year_net:
                    summary = f"年末结转本年利润 {month[:4]}"
                    year_lines = [
                        {**base, "summary": summary, "line_no": 1, "account_code": settings["current_profit_account"],
                         "debit": -year_net if year_net < 0 else ZERO, "credit": year_net if year_net > 0 else ZERO},
                        {**base, "summary": summary, "line_no": 2, "account_code": settings["retained_earnings_account"],
                         "debit": year_net if year_net > 0 else ZERO, "credit": -year_net if year_net < 0 else ZERO},
                    ]
                    year_end_voucher_id = self._insert_draft(
                        cursor, {"voucher_type": "closing", "posting_date": end, "period_month": month, "summary": summary,
                                 "lines": year_lines},
                        str(uuid.uuid5(uuid.UUID(canonical), "year_end")), actor, ref_type="period_close", ref_id=month)
                    self._submit_in_tx(cursor, year_end_voucher_id, actor)
            snapshot = self._snapshot_rows(cursor, month)
            if sum((r["closing_net"] for r in snapshot), ZERO) != 0:
                raise FinanceError("SNAPSHOT_MISMATCH", "余额快照借贷不平，封账已撤销。", http_status=500)
            tb_hash = self._digest([{k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in r.items()} for r in snapshot])
            pl_hash = self._digest(pl_report)
            cursor.execute(
                "INSERT INTO finance_period_closes (period_month, version, status, closing_voucher_id, year_end_voucher_id, checks_json,"
                " trial_balance_sha256, profit_loss_sha256, net_profit, closed_by) VALUES (%s, %s, 'closed', %s, %s, %s, %s, %s, %s, %s)",
                (month, int(period["version"]), closing_voucher_id, year_end_voucher_id, json_text(checks), tb_hash, pl_hash, profit,
                 actor.label),
            )
            close_id = int(cursor.lastrowid)
            cursor.executemany(
                "INSERT INTO finance_account_balances (close_id, account_code, opening_net, period_debit, period_credit, closing_net)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [(close_id, r["account_code"], r["opening_net"], r["period_debit"], r["period_credit"], r["closing_net"]) for r in snapshot],
            )
            cursor.execute("SELECT COUNT(*) AS n, COALESCE(SUM(closing_net), 0) AS total, COALESCE(SUM(period_debit), 0) AS d"
                           " FROM finance_account_balances WHERE close_id=%s", (close_id,))
            readback = fetchone(cursor)
            if (int(readback["n"]) != len(snapshot) or Decimal(readback["total"]) != 0
                    or Decimal(readback["d"]) != sum((r["period_debit"] for r in snapshot), ZERO)):
                raise FinanceError("SNAPSHOT_MISMATCH", "余额快照回读不一致，封账已撤销。", http_status=500)
            cursor.execute("UPDATE finance_periods SET status='closed', updated_by=%s WHERE period_month=%s", (actor.label, month))
            return {"id": close_id, "month": month, "version": int(period["version"]), "status": "closed",
                    "closing_voucher_id": closing_voucher_id, "year_end_voucher_id": year_end_voucher_id,
                    "net_profit": f"{profit:.4f}", "trial_balance_sha256": tb_hash}

        return self._write(request_id=canonical, action="period.close", target_type="period", target_id=month,
                           payload=payload or {}, actor=actor, work=work)

    def reopen_period(self, month: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        month = parse_month(month)
        reason = optional_text((payload or {}).get("reason"), "反结账原因", 255)
        if not reason:
            raise FinanceValidationError("请填写反结账原因。", code="REOPEN_REASON_REQUIRED")

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT status, version FROM finance_periods WHERE period_month=%s FOR UPDATE", (month,))
            period = fetchone(cursor)
            if period is None or period["status"] != "closed":
                raise FinanceError("PERIOD_NOT_CLOSED", f"{month} 未封账。")
            cursor.execute("SELECT period_month FROM finance_periods WHERE status='closed' AND period_month > %s LIMIT 1", (month,))
            if cursor.fetchone():
                raise FinanceError("PERIOD_REOPEN_ORDER", "请先反结账更晚的月份。")
            cursor.execute("SELECT * FROM finance_period_closes WHERE period_month=%s AND status='closed' ORDER BY version DESC LIMIT 1 FOR UPDATE",
                           (month,))
            close = fetchone(cursor)
            cursor.execute("UPDATE finance_periods SET status='open', version=version+1, updated_by=%s WHERE period_month=%s",
                           (actor.label, month))
            for voucher_id in (close or {}).get("year_end_voucher_id"), (close or {}).get("closing_voucher_id"):
                if voucher_id:
                    self._cancel_in_tx(cursor, int(voucher_id), f"反结账 {month}：{reason}", actor)
            if close:
                cursor.execute("UPDATE finance_period_closes SET status='reopened', reopened_by=%s, reopened_at=CURRENT_TIMESTAMP(6),"
                               " reopen_reason=%s WHERE id=%s", (actor.label, reason, close["id"]))
            return {"month": month, "status": "open", "version": int(period["version"]) + 1}

        return self._write(request_id=request_id, action="period.reopen", target_type="period", target_id=month,
                           payload={"reason": reason}, actor=actor, work=work)
