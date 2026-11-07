"""Boyi waybill confirmation and posting (P3).

The user decided every Boyi waybill is confirmed by a person before it is
posted (amounts may be blank at opening and filled in later through the
existing waybill edit, which keeps its own edit log). Confirmation locks the
waybill row, checks the finance fingerprint the person saw, then — in one
transaction — reverses the previous voucher of the waybill if any and posts
the new one through ``gl/posting.py``.
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
    expected_revision,
    month_bounds,
    month_of,
    optional_text,
    parse_date,
    parse_month,
    parse_request_id,
)
from shared.finance.rules.doc_rules import (
    PARTY_MODES,
    PAYMENT_METHODS,
    build_voucher,
    is_cancelled,
    reversal_of,
    waybill_amounts,
    waybill_fingerprint,
)

MAX_LIST_ROWS = 5000
MAX_BATCH = 200
WAYBILL_COLUMNS = (
    "id, waybill_no, source, open_date, sender_name, sender_phone, receiver_name, payment_method, freight_fee,"
    " transfer_fee, cod_amount, insurance_amount, status, linked_platform"
)
RULE_COLUMNS = (
    "payment_method, receivable_account, freight_income_account, transfer_income_account, party_mode, fixed_party_id,"
    " site_code, business_line, revision, updated_by"
)
STATUS_TEXT = {
    "pending": "待确认", "confirmed": "已确认", "changed": "运单已改，待重新确认",
    "cancelled_posted": "运单已作废，待冲销", "reversed": "已冲销", "cancelled": "已作废（未记账）",
}


def _derived(base: str, label: str) -> str:
    return str(uuid.uuid5(uuid.UUID(base), label))


def _text(value: Decimal) -> str:
    return f"{value:.4f}"


class WaybillPostingMixin:
    # ---------- rules ----------
    def list_doc_rules(self) -> list[dict[str, Any]]:
        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(f"SELECT {RULE_COLUMNS} FROM finance_doc_posting_rules ORDER BY FIELD(payment_method, '寄付', '到付', '月结')")
            return [{**row, "revision": int(row["revision"])} for row in fetchall(cursor)]

        return self._read(work)

    def update_doc_rule(self, payment_method: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        if payment_method not in PAYMENT_METHODS:
            raise FinanceValidationError("结算方式无效。")
        if not isinstance(payload, dict) or set(payload) - {"receivable_account", "freight_income_account", "transfer_income_account",
                                                            "party_mode", "fixed_party_id", "site_code", "business_line",
                                                            "expected_revision"}:
            raise FinanceValidationError("规则内容包含不支持的字段。")
        revision = expected_revision(payload.get("expected_revision"))
        if payload.get("party_mode") not in PARTY_MODES:
            raise FinanceValidationError("往来单位来源无效。")
        fixed = payload.get("fixed_party_id")
        if payload["party_mode"] == "fixed":
            if isinstance(fixed, bool) or not isinstance(fixed, int) or fixed <= 0:
                raise FinanceValidationError("固定往来单位必须选择一个往来单位。")
        else:
            fixed = None

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute(f"SELECT {RULE_COLUMNS} FROM finance_doc_posting_rules WHERE payment_method=%s FOR UPDATE", (payment_method,))
            current = fetchone(cursor)
            if current is None:
                raise FinanceNotFoundError("记账规则不存在。", code="DOC_RULE_MISSING")
            if int(current["revision"]) != revision:
                raise FinanceError("FINANCE_REVISION_CONFLICT", "规则已被他人修改，请刷新后重试。")
            checks = (("receivable_account", "应收科目", ("receivable",), True),
                      ("freight_income_account", "运费收入科目", ("income",), False),
                      ("transfer_income_account", "中转费收入科目", ("income",), False))
            for field, label, types, party in checks:
                code = str(payload.get(field) or "")
                cursor.execute("SELECT account_type, is_group, is_active, requires_party FROM finance_accounts WHERE code=%s FOR SHARE", (code,))
                account = fetchone(cursor)
                if (not account or account["is_group"] or not account["is_active"] or account["account_type"] not in types
                        or bool(account["requires_party"]) != party):
                    rule = "已启用的末级应收类科目且必填往来单位" if party else "已启用的末级收入科目"
                    raise FinanceValidationError(f"{label}必须是{rule}。", code="DOC_RULE_INVALID_ACCOUNT")
            if fixed is not None:
                self._party_row(cursor, fixed)
            dims = {key: optional_text(payload.get(key), label, 64)
                    for key, label in (("site_code", "网点"), ("business_line", "业务线"))}
            cursor.execute(
                "UPDATE finance_doc_posting_rules SET receivable_account=%s, freight_income_account=%s, transfer_income_account=%s,"
                " party_mode=%s, fixed_party_id=%s, site_code=%s, business_line=%s, revision=revision+1, updated_by=%s"
                " WHERE payment_method=%s",
                (payload["receivable_account"], payload["freight_income_account"], payload["transfer_income_account"],
                 payload["party_mode"], fixed, dims["site_code"], dims["business_line"], actor.label, payment_method),
            )
            cursor.execute(f"SELECT {RULE_COLUMNS} FROM finance_doc_posting_rules WHERE payment_method=%s", (payment_method,))
            row = fetchone(cursor)
            return {**row, "revision": int(row["revision"]), "id": payment_method}

        return self._write(request_id=request_id, action="doc_rule.update", target_type="doc_rule", target_id=payment_method,
                           payload=payload, actor=actor, work=work)

    # ---------- queue ----------
    @staticmethod
    def _party_lookups(cursor: Any) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
        cursor.execute("SELECT id, name, party_type, is_active, boyi_sender_name, boyi_sender_phone, platform_code FROM finance_parties")
        rows = fetchall(cursor)
        senders = {(r["boyi_sender_name"], r["boyi_sender_phone"]): r for r in rows if r["boyi_sender_name"] and r["is_active"]}
        platforms = {r["platform_code"]: r for r in rows if r["platform_code"] and r["is_active"]}
        return senders, platforms, {int(r["id"]): r for r in rows}

    @staticmethod
    def _suggest_party(rule: dict[str, Any] | None, row: dict[str, Any], senders: dict, platforms: dict) -> int | None:
        if rule is None:
            return None
        mode = rule["party_mode"]
        if mode == "fixed":
            return int(rule["fixed_party_id"])
        if mode == "sender_customer":
            party = senders.get((str(row.get("sender_name") or "").strip(), str(row.get("sender_phone") or "").strip()))
            return int(party["id"]) if party else None
        if mode == "linked_platform":
            party = platforms.get(str(row.get("linked_platform") or "").strip())
            return int(party["id"]) if party else None
        return None

    @staticmethod
    def _queue_status(row: dict[str, Any], posting: dict[str, Any] | None, fingerprint: str) -> str:
        cancelled = is_cancelled(row)
        if posting is None:
            return "cancelled" if cancelled else "pending"
        if posting["status"] == "reversed":
            return "reversed" if cancelled else "pending"
        if cancelled:
            return "cancelled_posted"
        return "confirmed" if posting["fingerprint"] == fingerprint else "changed"

    def list_waybill_postings(self, *, month: Any, status: Any = None) -> dict[str, Any]:
        month = parse_month(month)
        if status not in (None, "", *STATUS_TEXT):
            raise FinanceValidationError("状态筛选无效。")
        return self._read(lambda cursor: self._waybill_queue(cursor, month, status))

    def _waybill_queue(self, cursor: Any, month: str, status: Any = None) -> dict[str, Any]:
        start, end = month_bounds(month)
        cursor.execute(
            f"SELECT {WAYBILL_COLUMNS} FROM boyi_waybills WHERE open_date BETWEEN %s AND %s ORDER BY open_date, id LIMIT %s",
            (start.isoformat(), end.isoformat(), MAX_LIST_ROWS + 1),
        )
        waybills = fetchall(cursor)
        if len(waybills) > MAX_LIST_ROWS:
            raise FinanceError("WAYBILL_LIST_TOO_LARGE", f"本月博益开单超过 {MAX_LIST_ROWS} 张，请联系管理员。", http_status=422)
        postings: dict[int, dict[str, Any]] = {}
        if waybills:
            ids = [int(w["id"]) for w in waybills]
            cursor.execute(
                "SELECT p.*, v.voucher_no FROM finance_doc_postings p LEFT JOIN finance_vouchers v ON v.id=p.voucher_id"
                f" WHERE p.doc_type='boyi_waybill' AND p.doc_id IN ({', '.join(['%s'] * len(ids))})",
                tuple(ids),
            )
            postings = {int(p["doc_id"]): p for p in fetchall(cursor)}
        cursor.execute(f"SELECT {RULE_COLUMNS} FROM finance_doc_posting_rules")
        rules = {r["payment_method"]: r for r in fetchall(cursor)}
        senders, platforms, parties = self._party_lookups(cursor)
        rows, counts = [], {key: 0 for key in STATUS_TEXT}
        for waybill in waybills:
            fingerprint = waybill_fingerprint(waybill)
            posting = postings.get(int(waybill["id"]))
            state = self._queue_status(waybill, posting, fingerprint)
            counts[state] += 1
            if status and state != status:
                continue
            amounts = waybill_amounts(waybill)
            suggested = self._suggest_party(rules.get(amounts["payment_method"]), waybill, senders, platforms)
            rows.append({
                "waybill_id": int(waybill["id"]), "waybill_no": waybill["waybill_no"], "source": waybill["source"],
                "open_date": str(waybill["open_date"] or ""), "sender_name": waybill["sender_name"],
                "sender_phone": waybill["sender_phone"], "receiver_name": waybill["receiver_name"],
                "payment_method": amounts["payment_method"], "freight_fee": waybill["freight_fee"],
                "transfer_fee": waybill["transfer_fee"], "cod_amount": waybill["cod_amount"],
                "linked_platform": waybill["linked_platform"], "total": _text(amounts["total"]),
                "problems": amounts["problems"], "fingerprint": fingerprint, "status": state,
                "status_text": STATUS_TEXT[state],
                "suggested_party_id": suggested,
                "suggested_party_name": parties[suggested]["name"] if suggested in parties else None,
                "posting": None if posting is None else {
                    "status": posting["status"], "voucher_id": posting["voucher_id"], "voucher_no": posting["voucher_no"],
                    "total_amount": _text(Decimal(posting["total_amount"])), "party_id": posting["party_id"],
                    "confirmed_by": posting["confirmed_by"],
                    "confirmed_at": posting["confirmed_at"].isoformat(sep=" ", timespec="seconds"),
                },
            })
        return {"month": month, "rows": rows, "counts": counts}

    # ---------- confirm ----------
    def _period_is_closed(self, cursor: Any, period_month: str) -> bool:
        cursor.execute("SELECT status FROM finance_periods WHERE period_month=%s", (period_month,))
        row = fetchone(cursor)
        return bool(row and row["status"] != "open")

    def _reverse_doc_voucher(self, cursor: Any, voucher_id: int, reason: str, actor: FinanceActor, request_id: str) -> None:
        """Cancel the voucher in its open period, or post a mirror voucher today when that period is closed."""

        voucher = self._voucher_row(cursor, voucher_id, lock="FOR UPDATE")
        if voucher["status"] != "submitted":
            return
        if not self._period_is_closed(cursor, voucher["period_month"]):
            self._cancel_in_tx(cursor, voucher_id, reason, actor)
            return
        today = dt.date.today()
        mirror = reversal_of(self._voucher_lines(cursor, voucher_id), posting_date=today, period_month=month_of(today),
                             summary=f"冲销 {voucher['voucher_no']}：{reason}")
        draft_id = self._insert_draft(cursor, mirror, _derived(request_id, "reversal"), actor,
                                      ref_type=voucher["ref_type"], ref_id=voucher["ref_id"], reversal_of=voucher_id)
        self._submit_in_tx(cursor, draft_id, actor)

    def confirm_waybill(self, waybill_id: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """Post (or re-post, or reverse after cancellation) one Boyi waybill the person has checked."""

        waybill_id = int(waybill_id)
        if not isinstance(payload, dict) or set(payload) - {"expected_fingerprint", "party_id"}:
            raise FinanceValidationError("确认内容包含不支持的字段。")
        expected = str(payload.get("expected_fingerprint") or "")
        party_override = payload.get("party_id")
        if party_override is not None and (isinstance(party_override, bool) or not isinstance(party_override, int) or party_override <= 0):
            raise FinanceValidationError("往来单位无效。")
        canonical = parse_request_id(request_id)

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute(f"SELECT {WAYBILL_COLUMNS} FROM boyi_waybills WHERE id=%s FOR UPDATE", (waybill_id,))
            row = fetchone(cursor)
            if row is None:
                raise FinanceNotFoundError("博益运单不存在。", code="WAYBILL_NOT_FOUND")
            fingerprint = waybill_fingerprint(row)
            if fingerprint != expected:
                raise FinanceError("WAYBILL_CHANGED", "运单在你查看后被修改过，请刷新清单后重新确认。")
            cursor.execute(
                "SELECT * FROM finance_doc_postings WHERE doc_type='boyi_waybill' AND doc_id=%s FOR UPDATE", (waybill_id,)
            )
            posting = fetchone(cursor)
            state = self._queue_status(row, posting, fingerprint)
            reason = f"博益运单 {row['waybill_no']} " + ("已作废" if is_cancelled(row) else "已修改，重新确认")
            if state == "cancelled_posted":
                if posting["voucher_id"]:
                    self._reverse_doc_voucher(cursor, int(posting["voucher_id"]), reason, actor, canonical)
                cursor.execute(
                    "UPDATE finance_doc_postings SET status='reversed', fingerprint=%s, confirmed_by=%s,"
                    " confirmed_at=CURRENT_TIMESTAMP(6), revision=revision+1 WHERE id=%s",
                    (fingerprint, actor.label, posting["id"]),
                )
                return {"id": waybill_id, "waybill_no": row["waybill_no"], "status": "reversed", "voucher_id": None}
            if state in ("cancelled", "reversed"):
                raise FinanceError("WAYBILL_CANCELLED", "运单已作废，无需记账。")
            if state == "confirmed":
                raise FinanceError("ALREADY_CONFIRMED", "运单已确认记账。")
            amounts = waybill_amounts(row)
            if amounts["problems"]:
                raise FinanceError("WAYBILL_NOT_READY", "；".join(amounts["problems"]) + "。", http_status=422)
            cursor.execute(f"SELECT {RULE_COLUMNS} FROM finance_doc_posting_rules WHERE payment_method=%s FOR SHARE",
                           (amounts["payment_method"],))
            rule = fetchone(cursor)
            if rule is None:
                raise FinanceError("DOC_RULE_MISSING", f"缺少“{amounts['payment_method']}”的记账规则。")
            senders, platforms, parties = self._party_lookups(cursor)
            party_id = party_override or self._suggest_party(rule, row, senders, platforms)
            if amounts["total"] > 0 and not party_id:
                hint = {"sender_customer": "该寄件人还没有关联月结客户", "linked_platform": "运单没有绑定平台或平台未建档"}.get(rule["party_mode"], "")
                raise FinanceError("PARTY_REQUIRED", f"请选择往来单位{('（' + hint + '）') if hint else ''}。", http_status=422)
            open_date = parse_date(row["open_date"], "开单日期")
            start_month = self._load_settings(cursor, lock="FOR SHARE")["start_month"]
            if not start_month:
                raise FinanceError("START_MONTH_REQUIRED", "请先在财务设置中设定总账启用月。")
            if month_of(open_date) < start_month:
                raise FinanceError("BEFORE_START_MONTH", f"开单日期早于总账启用月 {start_month}，请通过期初凭证录入。")
            if posting and posting["status"] == "posted" and posting["voucher_id"]:
                self._reverse_doc_voucher(cursor, int(posting["voucher_id"]), reason, actor, canonical)
            posting_date, note = open_date, ""
            if self._period_is_closed(cursor, month_of(open_date)):
                posting_date, note = dt.date.today(), "（开单月已封账，记入当期）"
            voucher_id = None
            status = "zero"
            if amounts["total"] > 0:
                voucher = build_voucher(row=row, rule=rule, amounts=amounts, party_id=party_id, posting_date=posting_date,
                                        period_month=month_of(posting_date), note=note)
                draft_id = self._insert_draft(cursor, voucher, _derived(canonical, "voucher"), actor,
                                              ref_type="boyi_waybill", ref_id=str(waybill_id))
                self._submit_in_tx(cursor, draft_id, actor)
                voucher_id, status = draft_id, "posted"
            cursor.execute(
                "INSERT INTO finance_doc_postings (doc_type, doc_id, waybill_no, status, fingerprint, payment_method, freight_fee,"
                " transfer_fee, total_amount, party_id, voucher_id, rule_revision, confirmed_by)"
                " VALUES ('boyi_waybill', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE waybill_no=VALUES(waybill_no), status=VALUES(status), fingerprint=VALUES(fingerprint),"
                " payment_method=VALUES(payment_method), freight_fee=VALUES(freight_fee), transfer_fee=VALUES(transfer_fee),"
                " total_amount=VALUES(total_amount), party_id=VALUES(party_id), voucher_id=VALUES(voucher_id),"
                " rule_revision=VALUES(rule_revision), confirmed_by=VALUES(confirmed_by), confirmed_at=CURRENT_TIMESTAMP(6),"
                " revision=revision+1",
                (waybill_id, row["waybill_no"], status, fingerprint, amounts["payment_method"], amounts["freight"],
                 amounts["transfer"], amounts["total"], party_id, voucher_id, int(rule["revision"]), actor.label),
            )
            return {"id": waybill_id, "waybill_no": row["waybill_no"], "status": status, "voucher_id": voucher_id,
                    "total_amount": _text(amounts["total"]), "party_id": party_id}

        return self._write(request_id=canonical, action="waybill.confirm", target_type="boyi_waybill", target_id=waybill_id,
                           payload=payload, actor=actor, work=work)

    def confirm_waybills(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """Confirm several waybills; each is its own transaction with a derived request id."""

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH or set(payload) - {"items"}:
            raise FinanceValidationError(f"一次最多确认 {MAX_BATCH} 张运单。")
        canonical = parse_request_id(request_id)
        results = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {"waybill_id", "expected_fingerprint", "party_id"}:
                raise FinanceValidationError("确认内容无效。")
            waybill_id = int(item.get("waybill_id") or 0)
            body = {key: item[key] for key in ("expected_fingerprint", "party_id") if key in item}
            try:
                result = self.confirm_waybill(waybill_id, body, request_id=_derived(canonical, f"waybill:{waybill_id}"), actor=actor)
                results.append({"waybill_id": waybill_id, "ok": True, **result})
            except FinanceError as exc:
                results.append({"waybill_id": waybill_id, "ok": False, "error_code": exc.code, "message": exc.message})
        return {"results": results, "succeeded": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}
