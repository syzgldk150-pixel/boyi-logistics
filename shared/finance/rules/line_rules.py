"""Source line posting (P4): exact-match rules, suspense reviews, day vouchers, anomalies.

One voucher per source and business day, one fund line plus one counter line
per source line. A rule matches provider + item_l1 + item_l2 + direction
exactly; anything else is posted to the suspense account and grouped into an
open review. Resolving a review saves a rule and reposts the affected days of
open periods. ``settle_by_waybill`` lines are allocated against the party's
open items carrying the same waybill number right after posting.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

from shared.finance.db import fetchall, fetchone, json_text
from shared.finance.errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from shared.finance.models import FinanceActor, month_of, optional_text, parse_account_code, parse_request_id

PARTY_ACTIONS = ("none", "settle_by_waybill", "party_fixed", "internal_transfer")
RULE_COLUMNS = (
    "id, provider, item_l1, item_l2, direction, counter_account, party_action, party_id, requires_waybill, is_active,"
    " revision, remark, updated_by"
)
ZERO = Decimal("0")


def _derived(base: str, label: str) -> str:
    return str(uuid.uuid5(uuid.UUID(base), label))


def _rule_view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "id": int(row["id"]), "requires_waybill": bool(row["requires_waybill"]), "is_active": bool(row["is_active"]),
            "revision": int(row["revision"])}


class LinePostingMixin:
    # ---------- rules ----------
    def list_line_rules(self) -> list[dict[str, Any]]:
        def work(cursor: Any) -> list[dict[str, Any]]:
            cursor.execute(f"SELECT {RULE_COLUMNS} FROM finance_line_rules ORDER BY provider, item_l1, item_l2, direction")
            return [_rule_view(row) for row in fetchall(cursor)]

        return self._read(work)

    def _validated_rule(self, cursor: Any, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"provider", "item_l1", "item_l2", "direction", "counter_account", "party_action", "party_id",
                   "requires_waybill", "is_active", "remark"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise FinanceValidationError("规则内容包含不支持的字段。")
        provider = str(payload.get("provider") or "").strip()
        item_l1 = optional_text(payload.get("item_l1"), "一级项目", 128) or ""
        item_l2 = optional_text(payload.get("item_l2"), "二级项目", 128) or ""
        direction = payload.get("direction")
        action = payload.get("party_action", "none")
        if not provider or direction not in ("in", "out") or action not in PARTY_ACTIONS:
            raise FinanceValidationError("规则的提供方、方向或往来动作无效。")
        party_id = payload.get("party_id")
        if party_id is not None and (isinstance(party_id, bool) or not isinstance(party_id, int) or party_id <= 0):
            raise FinanceValidationError("往来单位无效。")
        requires_waybill = payload.get("requires_waybill", False)
        is_active = payload.get("is_active", True)
        if type(requires_waybill) is not bool or type(is_active) is not bool:
            raise FinanceValidationError("开关字段无效。")
        counter = None
        if action == "internal_transfer":
            if payload.get("counter_account") or party_id:
                raise FinanceValidationError("内部转账固定记入资金在途科目，不能再指定对方科目或往来单位。")
        else:
            counter = parse_account_code(payload.get("counter_account"))
            cursor.execute("SELECT is_group, is_active, frozen, requires_party FROM finance_accounts WHERE code=%s FOR SHARE", (counter,))
            account = fetchone(cursor)
            if not account or account["is_group"] or not account["is_active"] or account["frozen"]:
                raise FinanceValidationError("对方科目必须是已启用、未冻结的末级科目。", code="LINE_RULE_INVALID_ACCOUNT")
            if account["requires_party"]:
                if action not in ("settle_by_waybill", "party_fixed") or not party_id:
                    raise FinanceValidationError("对方科目要求往来单位：往来动作需选“按运单号核销”或“固定往来单位”，并选择往来单位。",
                                                 code="LINE_RULE_PARTY_REQUIRED")
            elif action in ("settle_by_waybill", "party_fixed"):
                raise FinanceValidationError("只有往来科目才能使用往来动作。", code="LINE_RULE_PARTY_ACTION")
            if party_id:
                self._party_row(cursor, party_id)
        if action == "settle_by_waybill":
            requires_waybill = True
        return {"provider": provider, "item_l1": item_l1, "item_l2": item_l2, "direction": direction, "counter_account": counter,
                "party_action": action, "party_id": party_id if action in ("settle_by_waybill", "party_fixed") else None,
                "requires_waybill": int(requires_waybill), "is_active": int(is_active),
                "remark": optional_text(payload.get("remark"), "备注", 255) or ""}

    def _save_rule(self, cursor: Any, rule: dict[str, Any], actor: FinanceActor) -> int:
        cursor.execute(
            "SELECT id FROM finance_line_rules WHERE provider=%s AND item_l1=%s AND item_l2=%s AND direction=%s FOR UPDATE",
            (rule["provider"], rule["item_l1"], rule["item_l2"], rule["direction"]),
        )
        existing = fetchone(cursor)
        if existing:
            cursor.execute(
                "UPDATE finance_line_rules SET counter_account=%s, party_action=%s, party_id=%s, requires_waybill=%s, is_active=%s,"
                " remark=%s, revision=revision+1, updated_by=%s WHERE id=%s",
                (rule["counter_account"], rule["party_action"], rule["party_id"], rule["requires_waybill"], rule["is_active"],
                 rule["remark"], actor.label, existing["id"]),
            )
            return int(existing["id"])
        columns = [*rule, "updated_by"]
        cursor.execute(f"INSERT INTO finance_line_rules ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
                       (*rule.values(), actor.label))
        return int(cursor.lastrowid)

    def save_line_rule(self, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """Create or update the rule for one exact key; affects later postings only."""

        def work(cursor: Any) -> dict[str, Any]:
            rule_id = self._save_rule(cursor, self._validated_rule(cursor, payload), actor)
            cursor.execute(f"SELECT {RULE_COLUMNS} FROM finance_line_rules WHERE id=%s", (rule_id,))
            return _rule_view(fetchone(cursor))

        return self._write(request_id=request_id, action="line_rule.save", target_type="line_rule", target_id="key",
                           payload=payload, actor=actor, work=work)

    # ---------- reviews & anomalies ----------
    def list_reviews(self, *, status: Any = "open") -> list[dict[str, Any]]:
        if status not in ("open", "resolved", "", None):
            raise FinanceValidationError("待审状态无效。")

        def work(cursor: Any) -> list[dict[str, Any]]:
            condition = "WHERE r.status=%s" if status else ""
            cursor.execute(
                "SELECT r.*, s.display_name AS source_name FROM finance_reviews r JOIN finance_sources s ON s.id=r.source_id "
                f"{condition} ORDER BY r.amount_total DESC, r.id LIMIT 500", (status,) if status else (),
            )
            return [{"id": int(row["id"]), "source_id": int(row["source_id"]), "source_name": row["source_name"],
                     "provider": row["provider"], "item_l1": row["item_l1"], "item_l2": row["item_l2"], "direction": row["direction"],
                     "line_count": int(row["line_count"]), "amount_total": f"{Decimal(row['amount_total']):.4f}",
                     "status": row["status"], "resolved_rule_id": row["resolved_rule_id"], "resolved_by": row["resolved_by"]}
                    for row in fetchall(cursor)]

        return self._read(work)

    def resolve_review(self, review_id: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        """Save the rule for the review's exact key and repost its suspense days in open periods."""

        review_id = int(review_id)
        canonical = parse_request_id(request_id)
        rule_payload = {key: value for key, value in (payload or {}).items()
                        if key in ("counter_account", "party_action", "party_id", "requires_waybill", "remark")}

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT * FROM finance_reviews WHERE id=%s FOR UPDATE", (review_id,))
            review = fetchone(cursor)
            if review is None:
                raise FinanceNotFoundError("待审不存在。", code="REVIEW_NOT_FOUND")
            if review["status"] != "open":
                raise FinanceError("REVIEW_NOT_OPEN", "该待审已处理。")
            rule = self._validated_rule(cursor, {**rule_payload, "provider": review["provider"], "item_l1": review["item_l1"],
                                                 "item_l2": review["item_l2"], "direction": review["direction"], "is_active": True})
            rule_id = self._save_rule(cursor, rule, actor)
            sign = ">" if review["direction"] == "in" else "<"
            cursor.execute(
                "SELECT DISTINCT business_date FROM finance_source_lines WHERE source_id=%s AND is_active=1 AND posting_status='suspense'"
                f" AND item_l1=%s AND item_l2=%s AND amount {sign} 0 ORDER BY business_date",
                (review["source_id"], review["item_l1"], review["item_l2"]),
            )
            days = [row["business_date"] for row in fetchall(cursor)]
            source = self._source_row(cursor, int(review["source_id"]), lock="FOR UPDATE")
            reposted, skipped = [], []
            for day in days:
                if self._period_is_closed(cursor, month_of(day)):
                    skipped.append(day.isoformat())
                    continue
                self._repost_day(cursor, source, day, actor, _derived(canonical, f"day:{day.isoformat()}"))
                reposted.append(day.isoformat())
            self._refresh_reviews(cursor, int(review["source_id"]))
            cursor.execute(
                "UPDATE finance_reviews SET status='resolved', resolved_rule_id=%s, resolved_by=%s, resolved_at=CURRENT_TIMESTAMP(6)"
                " WHERE id=%s AND line_count=0", (rule_id, actor.label, review_id),
            )
            cursor.execute("SELECT status FROM finance_reviews WHERE id=%s", (review_id,))
            remaining_open = fetchone(cursor)["status"] == "open"
            return {"id": review_id, "rule_id": rule_id, "reposted_days": reposted, "closed_period_days": skipped,
                    "still_open": remaining_open}

        return self._write(request_id=canonical, action="review.resolve", target_type="review", target_id=review_id,
                           payload=payload, actor=actor, work=work)

    def list_anomalies(self, *, status: Any = "open") -> list[dict[str, Any]]:
        def work(cursor: Any) -> list[dict[str, Any]]:
            condition = "WHERE status=%s" if status else ""
            cursor.execute(f"SELECT * FROM finance_anomalies {condition} ORDER BY id DESC LIMIT 500", (status,) if status else ())
            return [{"id": int(row["id"]), "kind": row["kind"], "source_id": row["source_id"], "batch_id": row["batch_id"],
                     "period_month": row["period_month"], "status": row["status"],
                     "detail": json.loads(row["detail_json"]) if isinstance(row["detail_json"], (str, bytes)) else row["detail_json"],
                     "acknowledged_by": row["acknowledged_by"], "acknowledge_reason": row["acknowledge_reason"],
                     "created_at": row["created_at"].isoformat(sep=" ", timespec="seconds")} for row in fetchall(cursor)]

        return self._read(work)

    def acknowledge_anomaly(self, anomaly_id: Any, payload: dict[str, Any], *, request_id: Any, actor: FinanceActor) -> dict[str, Any]:
        anomaly_id = int(anomaly_id)
        reason = optional_text((payload or {}).get("reason"), "确认原因", 255)
        if not reason:
            raise FinanceValidationError("请填写确认知悉的原因。", code="ACK_REASON_REQUIRED")

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT status FROM finance_anomalies WHERE id=%s FOR UPDATE", (anomaly_id,))
            row = fetchone(cursor)
            if row is None:
                raise FinanceNotFoundError("异常不存在。", code="ANOMALY_NOT_FOUND")
            if row["status"] != "open":
                raise FinanceError("ANOMALY_NOT_OPEN", "该异常已确认。")
            cursor.execute("UPDATE finance_anomalies SET status='acknowledged', acknowledged_by=%s, acknowledged_at=CURRENT_TIMESTAMP(6),"
                           " acknowledge_reason=%s WHERE id=%s", (actor.label, reason, anomaly_id))
            return {"id": anomaly_id, "status": "acknowledged"}

        return self._write(request_id=request_id, action="anomaly.acknowledge", target_type="anomaly", target_id=anomaly_id,
                           payload={"reason": reason}, actor=actor, work=work)

    @staticmethod
    def _record_anomaly(cursor: Any, kind: str, *, source_id: int | None, batch_id: int | None, period_month: str | None,
                        detail: dict[str, Any]) -> None:
        cursor.execute("INSERT INTO finance_anomalies (kind, source_id, batch_id, period_month, detail_json) VALUES (%s, %s, %s, %s, %s)",
                       (kind, source_id, batch_id, period_month, json_text(detail)))

    # ---------- posting ----------
    @staticmethod
    def _refresh_reviews(cursor: Any, source_id: int) -> None:
        """Recompute open reviews of a source from its active suspense lines."""

        cursor.execute(
            "SELECT s.provider, l.item_l1, l.item_l2, IF(l.amount > 0, 'in', 'out') AS direction, COUNT(*) AS n, SUM(ABS(l.amount)) AS total"
            " FROM finance_source_lines l JOIN finance_sources s ON s.id=l.source_id"
            " WHERE l.source_id=%s AND l.is_active=1 AND l.posting_status='suspense' GROUP BY s.provider, l.item_l1, l.item_l2, direction",
            (source_id,),
        )
        current = {(r["item_l1"], r["item_l2"], r["direction"]): r for r in fetchall(cursor)}
        cursor.execute("UPDATE finance_reviews SET line_count=0, amount_total=0 WHERE source_id=%s AND status='open'", (source_id,))
        for (item_l1, item_l2, direction), row in current.items():
            cursor.execute(
                "INSERT INTO finance_reviews (source_id, provider, item_l1, item_l2, direction, line_count, amount_total)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE line_count=VALUES(line_count), amount_total=VALUES(amount_total), status='open',"
                " resolved_rule_id=NULL, resolved_by=NULL, resolved_at=NULL",
                (source_id, row["provider"], item_l1, item_l2, direction, int(row["n"]), row["total"]),
            )
        # Reviews whose suspense lines were all reposted or replaced close by themselves.
        cursor.execute(
            "UPDATE finance_reviews SET status='resolved', resolved_by='system', resolved_at=CURRENT_TIMESTAMP(6)"
            " WHERE source_id=%s AND status='open' AND line_count=0", (source_id,),
        )

    def _reverse_day_vouchers(self, cursor: Any, voucher_ids: list[int], actor: FinanceActor, reason: str) -> None:
        for voucher_id in sorted(set(voucher_ids)):
            cursor.execute("SELECT id FROM finance_allocations WHERE origin_voucher_id=%s AND status='active' FOR UPDATE", (voucher_id,))
            for allocation in fetchall(cursor):
                cursor.execute("UPDATE finance_party_ledger SET is_cancelled=1 WHERE allocation_id=%s", (allocation["id"],))
                cursor.execute("UPDATE finance_allocations SET status='reversed', reversed_by=%s, reversed_at=CURRENT_TIMESTAMP(6),"
                               " reverse_reason=%s WHERE id=%s", (actor.label, reason, allocation["id"]))
            voucher = self._voucher_row(cursor, voucher_id, lock="FOR UPDATE")
            if voucher["status"] == "submitted":
                self._cancel_in_tx(cursor, voucher_id, reason, actor)

    def _repost_day(self, cursor: Any, source: dict[str, Any], day: dt.date, actor: FinanceActor, request_id: str,
                    *, extra_vouchers: list[int] | None = None) -> int | None:
        """Reverse the day's vouchers of this source and post its active lines again (open period only)."""

        cursor.execute(
            "SELECT * FROM finance_source_lines WHERE source_id=%s AND business_date=%s AND is_active=1 ORDER BY id FOR UPDATE",
            (source["id"], day),
        )
        lines = fetchall(cursor)
        old = [int(line["voucher_id"]) for line in lines if line["voucher_id"]] + list(extra_vouchers or [])
        self._reverse_day_vouchers(cursor, old, actor, f"{source['display_name']} {day.isoformat()} 流水重记")
        if not lines:
            return None
        if not source["fund_account"]:
            cursor.execute("UPDATE finance_source_lines SET posting_status='no_account', voucher_id=NULL, rule_id=NULL"
                           " WHERE source_id=%s AND business_date=%s AND is_active=1", (source["id"], day))
            return None
        settings = self._load_settings(cursor, lock="FOR SHARE")
        cursor.execute(f"SELECT {RULE_COLUMNS} FROM finance_line_rules WHERE provider=%s AND is_active=1", (source["provider"],))
        rules = {(r["item_l1"], r["item_l2"], r["direction"]): r for r in fetchall(cursor)}
        voucher_lines, outcomes, settlements = [], [], []
        base = {"site_code": None, "source_id": source["source_key"], "business_line": None, "fee_level": None}
        for line in lines:
            amount = Decimal(line["amount"])
            direction = "in" if amount > 0 else "out"
            rule = rules.get((line["item_l1"], line["item_l2"], direction))
            if rule and rule["requires_waybill"] and not line["waybill_no"]:
                rule = None
            summary = f"{source['display_name']} {line['item_l1']}{('/' + line['item_l2']) if line['item_l2'] else ''} {line['reference']}".strip()[:255]
            party_id = None
            if rule is None:
                counter, status = settings["suspense_account"], "suspense"
            elif rule["party_action"] == "internal_transfer":
                counter, status = settings["transit_account"], "posted"
            else:
                counter, status = rule["counter_account"], "posted"
                party_id = rule["party_id"]
            value = abs(amount)
            fund = {**base, "account_code": source["fund_account"], "debit": value if amount > 0 else ZERO,
                    "credit": ZERO if amount > 0 else value, "summary": summary, "waybill_no": line["waybill_no"],
                    "party_id": None, "source_line_id": int(line["id"])}
            other = {**base, "account_code": counter, "debit": ZERO if amount > 0 else value, "credit": value if amount > 0 else ZERO,
                     "summary": summary, "waybill_no": line["waybill_no"], "party_id": party_id, "source_line_id": int(line["id"])}
            voucher_lines += [fund, other]
            outcomes.append((int(line["id"]), status, int(rule["id"]) if rule else None))
            if rule and rule["party_action"] == "settle_by_waybill":
                settlements.append((int(rule["party_id"]), counter, line["waybill_no"]))
        for index, item in enumerate(voucher_lines, start=1):
            item["line_no"] = index
        voucher = {"voucher_type": "source_line", "posting_date": day, "period_month": month_of(day),
                   "summary": f"{source['display_name']} {day.isoformat()} 流水 {len(lines)} 笔"[:255], "lines": voucher_lines}
        voucher_id = self._insert_draft(cursor, voucher, request_id, actor, ref_type="finance_source_day",
                                        ref_id=f"{source['id']}:{day.isoformat()}")
        self._submit_in_tx(cursor, voucher_id, actor)
        cursor.executemany("UPDATE finance_source_lines SET posting_status=%s, voucher_id=%s, rule_id=%s WHERE id=%s",
                           [(status, voucher_id, rule_id, line_id) for line_id, status, rule_id in outcomes])
        for index, (party_id, account_code, waybill_no) in enumerate(sorted(set(settlements))):
            self._settle_waybill(cursor, party_id=party_id, account_code=account_code, waybill_no=waybill_no, day=day,
                                 origin_voucher_id=voucher_id, request_id=_derived(request_id, f"settle:{index}"), actor=actor)
        return voucher_id

    def _settle_waybill(self, cursor: Any, *, party_id: int, account_code: str, waybill_no: str, day: dt.date,
                        origin_voucher_id: int, request_id: str, actor: FinanceActor) -> None:
        cursor.execute(
            "SELECT i.id, i.posting_date, i.amount + COALESCE(SUM(a.amount), 0) AS outstanding FROM finance_party_ledger i"
            " LEFT JOIN finance_party_ledger a ON a.against_item_id=i.id AND a.is_cancelled=0"
            " WHERE i.against_item_id IS NULL AND i.is_cancelled=0 AND i.party_id=%s AND i.account_code=%s AND i.waybill_no=%s"
            " AND i.posting_date <= %s GROUP BY i.id, i.posting_date, i.amount HAVING outstanding <> 0 ORDER BY i.posting_date, i.id",
            (party_id, account_code, waybill_no, day),
        )
        rows = fetchall(cursor)
        positive = sum((Decimal(r["outstanding"]) for r in rows if Decimal(r["outstanding"]) > 0), ZERO)
        negative = -sum((Decimal(r["outstanding"]) for r in rows if Decimal(r["outstanding"]) < 0), ZERO)
        matched = min(positive, negative)
        if matched == 0:
            return
        requested = []
        for sign_positive in (True, False):
            remaining = matched
            for r in rows:
                value = Decimal(r["outstanding"])
                if remaining == 0 or (value > 0) != sign_positive:
                    continue
                take = min(abs(value), remaining)
                requested.append((int(r["id"]), take))
                remaining -= take
        self._allocate(cursor, party_id=party_id, account_code=account_code, allocation_date=day, method="auto_waybill",
                       remark=f"流水按运单号 {waybill_no} 自动核销", request_id=request_id, actor=actor, requested=requested,
                       origin_voucher_id=origin_voucher_id)
