"""The only code path that writes ``finance_gl_entries``.

``submit_voucher`` and ``cancel_voucher`` each run in one transaction:
settings → voucher row lock → period lock → account checks → numbering →
GL insert → read-back. GL rows are only inserted; cancellation inserts the
mirror rows and flags both sides ``is_cancelled`` (ERPNext style), so every
report that filters ``is_cancelled=0`` sees neither side.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from shared.finance.db import fetchall, fetchone
from shared.finance.errors import FinanceError, FinanceValidationError
from shared.finance.models import VOUCHER_WORD, FinanceActor, expected_revision, optional_text

_GL_COPY_COLUMNS = (
    "account_code, debit, credit, summary, site_code, source_id, business_line, fee_level, waybill_no,"
    " party_id, source_line_id"
)


class PostingMixin:
    @staticmethod
    def _lock_open_period(cursor: Any, period_month: str) -> None:
        cursor.execute("INSERT IGNORE INTO finance_periods (period_month) VALUES (%s)", (period_month,))
        cursor.execute("SELECT status FROM finance_periods WHERE period_month=%s FOR SHARE", (period_month,))
        if fetchone(cursor)["status"] != "open":
            raise FinanceError("PERIOD_CLOSED", f"{period_month} 已封账，不能再记账。")

    @staticmethod
    def _next_voucher_no(cursor: Any, period_month: str) -> str:
        cursor.execute(
            "INSERT IGNORE INTO finance_voucher_sequences (word, period_month, last_no) VALUES (%s, %s, 0)",
            (VOUCHER_WORD, period_month),
        )
        cursor.execute(
            "SELECT last_no FROM finance_voucher_sequences WHERE word=%s AND period_month=%s FOR UPDATE",
            (VOUCHER_WORD, period_month),
        )
        number = int(fetchone(cursor)["last_no"]) + 1
        cursor.execute(
            "UPDATE finance_voucher_sequences SET last_no=%s WHERE word=%s AND period_month=%s",
            (number, VOUCHER_WORD, period_month),
        )
        return f"{VOUCHER_WORD}-{period_month}-{number:04d}"

    @staticmethod
    def _check_postable_accounts(cursor: Any, lines: list[dict[str, Any]]) -> None:
        # The used accounts and every ancestor (parents are code prefixes), read
        # with shared locks so a concurrent freeze or restructure serializes.
        needed = set()
        for code in {line["account_code"] for line in lines}:
            while len(code) >= 4:
                needed.add(code)
                code = code[:-2]
        cursor.execute(
            "SELECT code, name, parent_code, is_group, is_active, frozen, requires_party, requires_waybill"
            f" FROM finance_accounts WHERE code IN ({', '.join(['%s'] * len(needed))}) FOR SHARE",
            tuple(sorted(needed)),
        )
        accounts = {row["code"]: row for row in fetchall(cursor)}
        for line in lines:
            code = line["account_code"]
            account = accounts.get(code)
            label = f"第 {line['line_no']} 行科目 {code}"
            if account is None:
                raise FinanceError("ACCOUNT_NOT_FOUND", f"{label} 不存在。")
            if account["is_group"]:
                raise FinanceError("ACCOUNT_NOT_LEAF", f"{label}（{account['name']}）有下级科目，只能记到末级科目。")
            if account["frozen"]:
                raise FinanceError("ACCOUNT_FROZEN", f"{label}（{account['name']}）已冻结。")
            node: dict[str, Any] | None = account
            while node is not None:
                if not node["is_active"]:
                    raise FinanceError("ACCOUNT_INACTIVE", f"{label}（{account['name']}）或其上级科目已停用。")
                node = accounts.get(node["parent_code"]) if node["parent_code"] else None
            if account["requires_waybill"] and not line["waybill_no"]:
                raise FinanceError("DIMENSION_REQUIRED", f"{label}（{account['name']}）必须填写运单号。")
            if account["requires_party"] and not line["party_id"]:
                raise FinanceError("DIMENSION_REQUIRED", f"{label}（{account['name']}）必须选择往来单位。")

    def submit_voucher(self, voucher_id: int, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        voucher_id = int(voucher_id)
        revision = expected_revision((payload or {}).get("expected_revision"))

        def work(cursor: Any) -> dict[str, Any]:
            settings = self._load_settings(cursor, lock="FOR SHARE")
            start_month = settings["start_month"]
            if not start_month:
                raise FinanceError("START_MONTH_REQUIRED", "请先在财务设置中设定总账启用月。")
            voucher = self._voucher_row(cursor, voucher_id, lock="FOR UPDATE")
            if voucher["status"] != "draft":
                raise FinanceError("VOUCHER_NOT_DRAFT", "凭证已提交或已冲销。")
            if int(voucher["revision"]) != revision:
                raise FinanceError("FINANCE_REVISION_CONFLICT", "凭证已被他人修改，请刷新后重试。")
            period = voucher["period_month"]
            if period < start_month:
                raise FinanceError("BEFORE_START_MONTH", f"记账日期早于总账启用月 {start_month}。")
            if voucher["voucher_type"] == "opening" and period != start_month:
                raise FinanceError("OPENING_OUTSIDE_START_MONTH", f"期初凭证只能记在启用月 {start_month}。")
            cursor.execute(
                f"SELECT id, line_no, {_GL_COPY_COLUMNS} FROM finance_voucher_lines WHERE voucher_id=%s ORDER BY line_no FOR SHARE",
                (voucher_id,),
            )
            lines = fetchall(cursor)
            if len(lines) < 2:
                raise FinanceError("VOUCHER_TOO_FEW_LINES", "凭证至少需要两行分录。")
            debit = sum((Decimal(line["debit"]) for line in lines), Decimal("0"))
            credit = sum((Decimal(line["credit"]) for line in lines), Decimal("0"))
            if debit != credit:
                raise FinanceError("VOUCHER_UNBALANCED", f"借贷不平：借方 {debit:.2f}，贷方 {credit:.2f}。",
                                   details={"total_debit": format(debit, "f"), "total_credit": format(credit, "f")})
            if debit <= 0:
                raise FinanceError("VOUCHER_ZERO", "凭证金额不能为 0。")
            self._lock_open_period(cursor, period)
            self._check_postable_accounts(cursor, lines)
            voucher_no = self._next_voucher_no(cursor, period)
            cursor.executemany(
                "INSERT INTO finance_gl_entries (voucher_id, voucher_line_id, entry_kind, posting_date, period_month,"
                f" {_GL_COPY_COLUMNS}, created_by) VALUES (%s, %s, 'original', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (voucher_id, line["id"], voucher["posting_date"], period, line["account_code"], line["debit"],
                     line["credit"], line["summary"], line["site_code"], line["source_id"], line["business_line"],
                     line["fee_level"], line["waybill_no"], line["party_id"], line["source_line_id"], actor.label)
                    for line in lines
                ],
            )
            cursor.execute(
                "UPDATE finance_vouchers SET status='submitted', voucher_no=%s, total_debit=%s, total_credit=%s,"
                " submitted_by=%s, submitted_at=CURRENT_TIMESTAMP(6), revision=revision+1 WHERE id=%s",
                (voucher_no, debit, credit, actor.label, voucher_id),
            )
            self._verify_posted(cursor, voucher_id, count=len(lines), debit=debit, credit=credit)
            return self._full_voucher(cursor, voucher_id)

        return self._write(request_id=request_id, action="voucher.submit", target_type="voucher", target_id=voucher_id,
                           payload={"expected_revision": revision}, actor=actor, work=work)

    @staticmethod
    def _verify_posted(cursor: Any, voucher_id: int, *, count: int, debit: Decimal, credit: Decimal) -> None:
        cursor.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(debit), 0) AS d, COALESCE(SUM(credit), 0) AS c"
            " FROM finance_gl_entries WHERE voucher_id=%s AND entry_kind='original' AND is_cancelled=0",
            (voucher_id,),
        )
        row = fetchone(cursor)
        if int(row["n"]) != count or Decimal(row["d"]) != debit or Decimal(row["c"]) != credit or debit != credit:
            raise FinanceError("GL_READBACK_MISMATCH", "总账回读与凭证不一致，本次提交已撤销。", http_status=500)

    def cancel_voucher(self, voucher_id: int, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """Reverse a submitted voucher in its own (still open) period."""

        voucher_id = int(voucher_id)
        payload = payload or {}
        revision = expected_revision(payload.get("expected_revision"))
        reason = optional_text(payload.get("reason"), "冲销原因", 255)
        if not reason:
            raise FinanceValidationError("请填写冲销原因。", code="CANCEL_REASON_REQUIRED")

        def work(cursor: Any) -> dict[str, Any]:
            voucher = self._voucher_row(cursor, voucher_id, lock="FOR UPDATE")
            if voucher["status"] != "submitted":
                raise FinanceError("VOUCHER_NOT_SUBMITTED", "只有已提交的凭证可以冲销。")
            if int(voucher["revision"]) != revision:
                raise FinanceError("FINANCE_REVISION_CONFLICT", "凭证已被他人修改，请刷新后重试。")
            self._lock_open_period(cursor, voucher["period_month"])
            cursor.execute(
                f"SELECT id, voucher_line_id, posting_date, period_month, {_GL_COPY_COLUMNS} FROM finance_gl_entries"
                " WHERE voucher_id=%s AND entry_kind='original' AND is_cancelled=0 ORDER BY id FOR UPDATE",
                (voucher_id,),
            )
            originals = fetchall(cursor)
            if not originals:
                raise FinanceError("GL_READBACK_MISMATCH", "该凭证没有有效分录，无法冲销。", http_status=500)
            cursor.executemany(
                "INSERT INTO finance_gl_entries (voucher_id, voucher_line_id, entry_kind, posting_date, period_month,"
                f" {_GL_COPY_COLUMNS}, is_cancelled, created_by) VALUES (%s, %s, 'reversal', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)",
                [
                    (voucher_id, row["voucher_line_id"], row["posting_date"], row["period_month"], row["account_code"],
                     row["credit"], row["debit"], f"冲销：{row['summary']}"[:255], row["site_code"], row["source_id"],
                     row["business_line"], row["fee_level"], row["waybill_no"], row["party_id"], row["source_line_id"],
                     actor.label)
                    for row in originals
                ],
            )
            cursor.execute(
                "UPDATE finance_gl_entries SET is_cancelled=1 WHERE voucher_id=%s AND entry_kind='original'", (voucher_id,)
            )
            cursor.execute(
                "UPDATE finance_vouchers SET status='cancelled', cancelled_by=%s, cancelled_at=CURRENT_TIMESTAMP(6),"
                " cancel_reason=%s, revision=revision+1 WHERE id=%s",
                (actor.label, reason, voucher_id),
            )
            cursor.execute(
                "SELECT account_code, SUM(debit) - SUM(credit) AS net, SUM(is_cancelled=0) AS active"
                " FROM finance_gl_entries WHERE voucher_id=%s GROUP BY account_code",
                (voucher_id,),
            )
            if any(Decimal(row["net"]) != 0 or int(row["active"]) for row in fetchall(cursor)):
                raise FinanceError("GL_READBACK_MISMATCH", "冲销回读不为零，本次冲销已撤销。", http_status=500)
            return self._full_voucher(cursor, voucher_id)

        return self._write(request_id=request_id, action="voucher.cancel", target_type="voucher", target_id=voucher_id,
                           payload={"expected_revision": revision, "reason": reason}, actor=actor, work=work)
