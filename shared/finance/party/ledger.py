"""Open-item party ledger: open items, allocations (核销), aging and statements.

Posting a voucher adds one open item per GL entry on a party account
(``gl/posting.py``). An allocation pairs items of opposite sign on the same
party and account and appends two rows (one against each item) that sum to
zero, so the GL is untouched and "account balance = open items" always holds.
Reversing an allocation flags its rows cancelled and the items reopen.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from shared.finance.db import fetchall, fetchone
from shared.finance.errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from shared.finance.models import (
    FinanceActor,
    month_bounds,
    month_of,
    optional_text,
    parse_account_code,
    parse_amount,
    parse_date,
    parse_month,
    parse_request_id,
)

ZERO = Decimal("0")
AGING_BUCKETS = (("0_30", 0, 30), ("31_60", 31, 60), ("61_90", 61, 90), ("over_90", 91, None))
MAX_ALLOCATION_ITEMS = 500
MAX_STATEMENT_DAYS = 366


def _text(value: Decimal) -> str:
    return f"{value:.4f}"


def _item_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": int(row["id"]), "party_id": int(row["party_id"]), "account_code": row["account_code"],
        "posting_date": row["posting_date"].isoformat(), "voucher_id": row["voucher_id"], "voucher_no": row.get("voucher_no"),
        "waybill_no": row["waybill_no"], "summary": row["summary"], "amount": _text(Decimal(row["amount"])),
        "outstanding": _text(Decimal(row["outstanding"])),
    }


_OUTSTANDING_SQL = (
    "SELECT i.id, i.party_id, i.account_code, i.posting_date, i.voucher_id, v.voucher_no, i.waybill_no, i.summary,"
    " i.amount, i.amount + COALESCE(SUM(a.amount), 0) AS outstanding"
    " FROM finance_party_ledger i"
    " LEFT JOIN finance_party_ledger a ON a.against_item_id=i.id AND a.is_cancelled=0 AND a.posting_date <= %s"
    " LEFT JOIN finance_vouchers v ON v.id=i.voucher_id"
    " WHERE i.against_item_id IS NULL AND i.is_cancelled=0 AND i.posting_date <= %s {extra}"
    " GROUP BY i.id, i.party_id, i.account_code, i.posting_date, i.voucher_id, v.voucher_no, i.waybill_no, i.summary, i.amount"
)


class PartyLedgerMixin:
    # ---------- posting hooks (called inside submit/cancel transactions) ----------
    @staticmethod
    def _record_open_items(cursor: Any, voucher_id: int) -> int:
        cursor.execute(
            "INSERT INTO finance_party_ledger (party_id, account_code, posting_date, period_month, voucher_id, gl_entry_id,"
            " amount, waybill_no, summary, created_by)"
            " SELECT g.party_id, g.account_code, g.posting_date, g.period_month, g.voucher_id, g.id,"
            " CASE a.normal_balance WHEN 'debit' THEN g.debit - g.credit ELSE g.credit - g.debit END,"
            " g.waybill_no, g.summary, g.created_by"
            " FROM finance_gl_entries g JOIN finance_accounts a ON a.code=g.account_code"
            " WHERE g.voucher_id=%s AND g.entry_kind='original' AND g.is_cancelled=0 AND a.requires_party=1",
            (voucher_id,),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _release_open_items(cursor: Any, voucher_id: int) -> None:
        """Refuse while any item of the voucher is allocated; otherwise cancel them."""

        cursor.execute(
            "SELECT id FROM finance_party_ledger WHERE voucher_id=%s AND against_item_id IS NULL AND is_cancelled=0 FOR UPDATE",
            (voucher_id,),
        )
        items = [int(row["id"]) for row in fetchall(cursor)]
        if not items:
            return
        cursor.execute(
            f"SELECT COUNT(*) AS n FROM finance_party_ledger WHERE is_cancelled=0 AND against_item_id IN ({', '.join(['%s'] * len(items))})",
            tuple(items),
        )
        if int(fetchone(cursor)["n"]):
            raise FinanceError("VOUCHER_HAS_ALLOCATIONS", "凭证的往来款项已经核销，请先撤销核销再冲销凭证。")
        cursor.execute("UPDATE finance_party_ledger SET is_cancelled=1 WHERE voucher_id=%s", (voucher_id,))

    # ---------- reads ----------
    def open_items(self, *, party_id: Any, account_code: Any = None, as_of: Any = None) -> dict[str, Any]:
        party_id = int(party_id)
        as_of_date = parse_date(as_of, "截止日期") if as_of else dt.date.today()
        code = parse_account_code(account_code) if account_code else None

        def work(cursor: Any) -> dict[str, Any]:
            self._party_row(cursor, party_id)
            extra, params = "AND i.party_id=%s", [as_of_date, as_of_date, party_id]
            if code:
                extra += " AND i.account_code=%s"
                params.append(code)
            cursor.execute(_OUTSTANDING_SQL.format(extra=extra) + " HAVING outstanding <> 0 ORDER BY i.posting_date, i.id", tuple(params))
            items = [_item_view(row) for row in fetchall(cursor)]
            totals: dict[str, Decimal] = {}
            for item in items:
                totals[item["account_code"]] = totals.get(item["account_code"], ZERO) + Decimal(item["outstanding"])
            return {"party_id": party_id, "as_of": as_of_date.isoformat(), "items": items,
                    "totals": {key: _text(value) for key, value in sorted(totals.items())}}

        return self._read(work)

    def list_allocations(self, *, party_id: Any = None, month: Any = None) -> list[dict[str, Any]]:
        def work(cursor: Any) -> list[dict[str, Any]]:
            conditions, params = ["1=1"], []
            if party_id:
                conditions.append("a.party_id=%s")
                params.append(int(party_id))
            if month:
                start, end = month_bounds(parse_month(month))
                conditions.append("a.allocation_date BETWEEN %s AND %s")
                params += [start, end]
            cursor.execute(
                "SELECT a.id, a.party_id, p.name AS party_name, a.account_code, a.allocation_date, a.method, a.status,"
                " a.total_amount, a.remark, a.created_by, a.reversed_by, a.reverse_reason"
                f" FROM finance_allocations a JOIN finance_parties p ON p.id=a.party_id WHERE {' AND '.join(conditions)}"
                " ORDER BY a.allocation_date DESC, a.id DESC LIMIT 500",
                tuple(params),
            )
            return [{**row, "id": int(row["id"]), "party_id": int(row["party_id"]),
                     "allocation_date": row["allocation_date"].isoformat(),
                     "total_amount": _text(Decimal(row["total_amount"]))} for row in fetchall(cursor)]

        return self._read(work)

    # ---------- allocation ----------
    def _lock_items(self, cursor: Any, item_ids: list[int], allocation_date: dt.date) -> dict[int, dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(item_ids))
        cursor.execute(
            f"SELECT id FROM finance_party_ledger WHERE id IN ({placeholders}) AND against_item_id IS NULL AND is_cancelled=0"
            " ORDER BY id FOR UPDATE",
            tuple(item_ids),
        )
        if len(fetchall(cursor)) != len(item_ids):
            raise FinanceError("OPEN_ITEM_NOT_FOUND", "所选未清项不存在或已作废，请刷新后重试。")
        far_future = dt.date(9999, 12, 31)
        cursor.execute(_OUTSTANDING_SQL.format(extra=f"AND i.id IN ({placeholders})"), (far_future, far_future, *item_ids))
        rows = {int(row["id"]): row for row in fetchall(cursor)}
        for row in rows.values():
            if row["posting_date"] > allocation_date:
                raise FinanceError("ALLOCATION_BEFORE_ITEM", "核销日期不能早于被核销款项的记账日期。")
        return rows

    def _allocate(
        self, cursor: Any, *, party_id: int, account_code: str, allocation_date: dt.date, method: str, remark: str,
        request_id: str, actor: FinanceActor, requested: list[tuple[int, Decimal]],
    ) -> dict[str, Any]:
        settings = self._load_settings(cursor, lock="FOR SHARE")
        if not settings["start_month"] or month_of(allocation_date) < settings["start_month"]:
            raise FinanceError("BEFORE_START_MONTH", "核销日期早于总账启用月，或尚未设定启用月。")
        self._lock_open_period(cursor, month_of(allocation_date))
        ids = [item_id for item_id, _ in requested]
        if len(set(ids)) != len(ids):
            raise FinanceValidationError("同一未清项不能重复选择。")
        rows = self._lock_items(cursor, ids, allocation_date)
        positives: list[list[Any]] = []
        negatives: list[list[Any]] = []
        for item_id, amount in requested:
            row = rows[item_id]
            if int(row["party_id"]) != party_id or row["account_code"] != account_code:
                raise FinanceError("ALLOCATION_MISMATCH", "核销的款项必须属于同一往来单位和同一科目。")
            outstanding = Decimal(row["outstanding"])
            if outstanding == 0 or amount <= 0 or amount > abs(outstanding):
                raise FinanceError("ALLOCATION_EXCEEDS_OUTSTANDING", f"核销金额超过未清余额（未清 {abs(outstanding):.2f}）。")
            (positives if outstanding > 0 else negatives).append([item_id, amount])
        total_positive = sum((amount for _, amount in positives), ZERO)
        if not positives or not negatives or total_positive != sum((amount for _, amount in negatives), ZERO):
            raise FinanceError("ALLOCATION_UNBALANCED", "核销两边金额必须相等：应收（应付）与收款（付款）各至少选一笔。")
        cursor.execute(
            "INSERT INTO finance_allocations (party_id, account_code, allocation_date, method, total_amount, remark, request_id, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (party_id, account_code, allocation_date, method, total_positive, remark, request_id, actor.label),
        )
        allocation_id = int(cursor.lastrowid)
        pairs = []
        p_index = n_index = 0
        while p_index < len(positives) and n_index < len(negatives):
            take = min(positives[p_index][1], negatives[n_index][1])
            pairs.append((positives[p_index][0], negatives[n_index][0], take))
            positives[p_index][1] -= take
            negatives[n_index][1] -= take
            p_index += positives[p_index][1] == 0
            n_index += negatives[n_index][1] == 0
        period = month_of(allocation_date)
        rows_to_insert = []
        for positive_id, negative_id, take in pairs:
            for against, signed in ((positive_id, -take), (negative_id, take)):
                rows_to_insert.append((party_id, account_code, allocation_date, period, allocation_id, against, signed,
                                       rows[against]["waybill_no"], remark or "核销", actor.label))
        cursor.executemany(
            "INSERT INTO finance_party_ledger (party_id, account_code, posting_date, period_month, allocation_id,"
            " against_item_id, amount, waybill_no, summary, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows_to_insert,
        )
        cursor.execute("SELECT COALESCE(SUM(amount), 0) AS net FROM finance_party_ledger WHERE allocation_id=%s", (allocation_id,))
        if Decimal(fetchone(cursor)["net"]) != 0:
            raise FinanceError("ALLOCATION_READBACK_MISMATCH", "核销回读不平，本次核销已撤销。", http_status=500)
        return {"id": allocation_id, "party_id": party_id, "account_code": account_code, "method": method,
                "allocation_date": allocation_date.isoformat(), "total_amount": _text(total_positive), "pairs": len(pairs)}

    def allocate(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """Manual allocation: ``items`` = [{item_id, amount}] with amounts as positive text."""

        if not isinstance(payload, dict) or set(payload) - {"party_id", "account_code", "allocation_date", "remark", "items"}:
            raise FinanceValidationError("核销内容包含不支持的字段。")
        party_id = int(payload.get("party_id") or 0)
        account_code = parse_account_code(payload.get("account_code"))
        allocation_date = parse_date(payload.get("allocation_date"), "核销日期")
        remark = optional_text(payload.get("remark"), "核销备注", 255) or ""
        items = payload.get("items")
        if not isinstance(items, list) or not 2 <= len(items) <= MAX_ALLOCATION_ITEMS:
            raise FinanceValidationError("请至少选择一笔应收（应付）和一笔收款（付款）。")
        requested = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict) or set(item) - {"item_id", "amount"}:
                raise FinanceValidationError(f"第 {index} 项核销内容无效。")
            requested.append((int(item.get("item_id") or 0), parse_amount(item.get("amount"), f"第 {index} 项核销金额")))
        canonical_id = parse_request_id(request_id)

        def work(cursor: Any) -> dict[str, Any]:
            self._party_row(cursor, party_id)
            return self._allocate(cursor, party_id=party_id, account_code=account_code, allocation_date=allocation_date,
                                  method="manual", remark=remark, request_id=canonical_id, actor=actor, requested=requested)

        comparable = {**payload, "items": [{"item_id": i, "amount": format(a, "f")} for i, a in requested]}
        return self._write(request_id=canonical_id, action="allocation.create", target_type="allocation", target_id="new",
                           payload=comparable, actor=actor, work=work)

    def auto_allocate_by_waybill(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """Pair open items of one party and account that carry the same waybill number (FIFO)."""

        if not isinstance(payload, dict) or set(payload) - {"party_id", "account_code", "allocation_date"}:
            raise FinanceValidationError("自动核销内容包含不支持的字段。")
        party_id = int(payload.get("party_id") or 0)
        account_code = parse_account_code(payload.get("account_code"))
        allocation_date = parse_date(payload.get("allocation_date"), "核销日期")
        canonical_id = parse_request_id(request_id)

        def work(cursor: Any) -> dict[str, Any]:
            self._party_row(cursor, party_id)
            cursor.execute(
                _OUTSTANDING_SQL.format(extra="AND i.party_id=%s AND i.account_code=%s AND i.waybill_no IS NOT NULL AND i.waybill_no <> ''")
                + " HAVING outstanding <> 0 ORDER BY i.waybill_no, i.posting_date, i.id",
                (allocation_date, allocation_date, party_id, account_code),
            )
            by_waybill: dict[str, list[dict[str, Any]]] = {}
            for row in fetchall(cursor):
                by_waybill.setdefault(row["waybill_no"], []).append(row)
            created = []
            for index, (waybill_no, rows) in enumerate(sorted(by_waybill.items())):
                positive = sum((Decimal(r["outstanding"]) for r in rows if Decimal(r["outstanding"]) > 0), ZERO)
                negative = -sum((Decimal(r["outstanding"]) for r in rows if Decimal(r["outstanding"]) < 0), ZERO)
                matched = min(positive, negative)
                if matched == 0:
                    continue
                requested = []
                for sign_positive in (True, False):
                    remaining = matched
                    for r in rows:
                        value = Decimal(r["outstanding"])
                        if remaining == 0 or (value > 0) != sign_positive or value == 0:
                            continue
                        take = min(abs(value), remaining)
                        requested.append((int(r["id"]), take))
                        remaining -= take
                # Each waybill is its own allocation so it can be reversed alone.
                sub_request = parse_request_id(_derived_uuid(canonical_id, index))
                created.append({**self._allocate(cursor, party_id=party_id, account_code=account_code,
                                                 allocation_date=allocation_date, method="auto_waybill",
                                                 remark=f"按运单号 {waybill_no} 自动核销", request_id=sub_request,
                                                 actor=actor, requested=requested), "waybill_no": waybill_no})
            return {"id": canonical_id, "allocations": created, "count": len(created)}

        return self._write(request_id=canonical_id, action="allocation.auto_waybill", target_type="party", target_id=party_id,
                           payload=payload, actor=actor, work=work)

    def reverse_allocation(self, allocation_id: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        allocation_id = int(allocation_id)
        reason = optional_text((payload or {}).get("reason"), "撤销原因", 255)
        if not reason:
            raise FinanceValidationError("请填写撤销核销的原因。", code="REVERSE_REASON_REQUIRED")

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT id, status, allocation_date FROM finance_allocations WHERE id=%s FOR UPDATE", (allocation_id,))
            row = fetchone(cursor)
            if row is None:
                raise FinanceNotFoundError("核销记录不存在。", code="ALLOCATION_NOT_FOUND")
            if row["status"] != "active":
                raise FinanceError("ALLOCATION_NOT_ACTIVE", "该核销已撤销。")
            self._lock_open_period(cursor, month_of(row["allocation_date"]))
            cursor.execute("UPDATE finance_party_ledger SET is_cancelled=1 WHERE allocation_id=%s", (allocation_id,))
            cursor.execute(
                "UPDATE finance_allocations SET status='reversed', reversed_by=%s, reversed_at=CURRENT_TIMESTAMP(6),"
                " reverse_reason=%s WHERE id=%s",
                (actor.label, reason, allocation_id),
            )
            return {"id": allocation_id, "status": "reversed"}

        return self._write(request_id=request_id, action="allocation.reverse", target_type="allocation", target_id=allocation_id,
                           payload={"reason": reason}, actor=actor, work=work)

    # ---------- reports ----------
    def aging(self, *, as_of: Any, kind: str = "receivable", party_id: Any = None) -> dict[str, Any]:
        """账龄: outstanding items by party in 0–30 / 31–60 / 61–90 / 90+ days (from posting date)."""

        as_of_date = parse_date(as_of, "截止日期")
        if kind not in ("receivable", "payable"):
            raise FinanceValidationError("账龄类型无效。")

        def work(cursor: Any) -> dict[str, Any]:
            extra = "AND i.account_code IN (SELECT code FROM finance_accounts WHERE root_type=%s AND requires_party=1)"
            params: list[Any] = [as_of_date, as_of_date, "asset" if kind == "receivable" else "liability"]
            if party_id:
                extra += " AND i.party_id=%s"
                params.append(int(party_id))
            cursor.execute(_OUTSTANDING_SQL.format(extra=extra) + " HAVING outstanding <> 0", tuple(params))
            rows = fetchall(cursor)
            cursor.execute("SELECT id, name, party_type FROM finance_parties")
            parties = {int(row["id"]): row for row in fetchall(cursor)}
            result: dict[int, dict[str, Any]] = {}
            keys = [key for key, _, _ in AGING_BUCKETS]
            for row in rows:
                entry = result.setdefault(int(row["party_id"]), {key: ZERO for key in (*keys, "unapplied", "total")})
                value = Decimal(row["outstanding"])
                entry["total"] += value
                if value < 0:
                    entry["unapplied"] += value
                    continue
                days = (as_of_date - row["posting_date"]).days
                for key, low, high in AGING_BUCKETS:
                    if days >= low and (high is None or days <= high):
                        entry[key] += value
                        break
            output = []
            grand = {key: ZERO for key in (*keys, "unapplied", "total")}
            for pid, entry in sorted(result.items(), key=lambda item: -item[1]["total"]):
                for key in grand:
                    grand[key] += entry[key]
                output.append({"party_id": pid, "party_name": parties[pid]["name"], "party_type": parties[pid]["party_type"],
                               **{key: _text(value) for key, value in entry.items()}})
            return {"as_of": as_of_date.isoformat(), "kind": kind, "rows": output,
                    "totals": {key: _text(value) for key, value in grand.items()}}

        return self._read(work)

    def party_statement(self, *, party_id: Any, start_date: Any, end_date: Any) -> dict[str, Any]:
        """对账单: the party's postings (not allocations) with a running balance per account."""

        party_id = int(party_id)
        start = parse_date(start_date, "开始日期")
        end = parse_date(end_date, "结束日期")
        if end < start or (end - start).days >= MAX_STATEMENT_DAYS:
            raise FinanceValidationError("对账单日期范围无效（最多 366 天）。")

        def work(cursor: Any) -> dict[str, Any]:
            party = self._party_row(cursor, party_id)
            cursor.execute(
                "SELECT account_code, COALESCE(SUM(amount), 0) AS net FROM finance_party_ledger"
                " WHERE party_id=%s AND against_item_id IS NULL AND is_cancelled=0 AND posting_date < %s GROUP BY account_code",
                (party_id, start),
            )
            balances = {row["account_code"]: Decimal(row["net"]) for row in fetchall(cursor)}
            opening = {code: _text(value) for code, value in sorted(balances.items()) if value}
            cursor.execute(
                "SELECT l.posting_date, l.account_code, a.name AS account_name, v.voucher_no, l.voucher_id, l.waybill_no,"
                " l.summary, l.amount FROM finance_party_ledger l JOIN finance_accounts a ON a.code=l.account_code"
                " LEFT JOIN finance_vouchers v ON v.id=l.voucher_id"
                " WHERE l.party_id=%s AND l.against_item_id IS NULL AND l.is_cancelled=0 AND l.posting_date BETWEEN %s AND %s"
                " ORDER BY l.posting_date, l.id LIMIT 5001",
                (party_id, start, end),
            )
            entries = fetchall(cursor)
            if len(entries) > 5000:
                raise FinanceError("STATEMENT_TOO_LARGE", "对账单超过 5000 行，请缩短日期范围。", http_status=422)
            rows = []
            for entry in entries:
                amount = Decimal(entry["amount"])
                balances[entry["account_code"]] = balances.get(entry["account_code"], ZERO) + amount
                rows.append({"posting_date": entry["posting_date"].isoformat(), "account_code": entry["account_code"],
                             "account_name": entry["account_name"], "voucher_no": entry["voucher_no"],
                             "voucher_id": entry["voucher_id"], "waybill_no": entry["waybill_no"], "summary": entry["summary"],
                             "increase": _text(amount if amount > 0 else ZERO), "decrease": _text(-amount if amount < 0 else ZERO),
                             "balance": _text(balances[entry["account_code"]])})
            return {"party": {"id": party_id, "name": party["name"], "party_type": party["party_type"]},
                    "start_date": start.isoformat(), "end_date": end.isoformat(), "opening": opening, "rows": rows,
                    "closing": {code: _text(value) for code, value in sorted(balances.items()) if value}}

        return self._read(work)

    def party_balance_check(self) -> list[dict[str, Any]]:
        """Rows where a party account's GL balance differs from its open items (should be empty)."""

        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(
                "SELECT g.party_id, g.account_code,"
                " SUM(CASE a.normal_balance WHEN 'debit' THEN g.debit - g.credit ELSE g.credit - g.debit END) AS gl"
                " FROM finance_gl_entries g JOIN finance_accounts a ON a.code=g.account_code"
                " WHERE g.is_cancelled=0 AND a.requires_party=1 GROUP BY g.party_id, g.account_code"
            )
            gl = {(row["party_id"], row["account_code"]): Decimal(row["gl"]) for row in fetchall(cursor)}
            cursor.execute(
                "SELECT party_id, account_code, SUM(amount) AS items FROM finance_party_ledger WHERE is_cancelled=0"
                " GROUP BY party_id, account_code"
            )
            items = {(row["party_id"], row["account_code"]): Decimal(row["items"]) for row in fetchall(cursor)}
            return [{"party_id": key[0], "account_code": key[1], "gl": _text(gl.get(key, ZERO)), "items": _text(items.get(key, ZERO))}
                    for key in sorted(set(gl) | set(items), key=lambda k: (k[0] or 0, k[1]))
                    if gl.get(key, ZERO) != items.get(key, ZERO)]

        return self._read(work)


def _derived_uuid(base: str, index: int) -> str:
    """A stable per-waybill request id derived from the caller's request id."""

    return str(uuid.uuid5(uuid.UUID(base), f"auto_waybill:{index}"))
