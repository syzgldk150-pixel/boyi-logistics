"""AI advice for suspense reviews: evidence, strict validation and storage (no posting).

The Agent asks the active model to pick a counter account (and party action)
for one review's exact key, using only the evidence built here. The answer is
accepted only if it names a candidate leaf account and a party the ledger
already has, and is stored on the review as advice. A person still resolves
the review (which saves the rule and reposts); nothing here changes the GL.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from shared.finance.db import fetchall, fetchone, json_text
from shared.finance.errors import FinanceError, FinanceNotFoundError
from shared.finance.rules.line_rules import PARTY_ACTIONS

MAX_SAMPLE_LINES = 15
MAX_RULE_EXAMPLES = 30


def validate_review_suggestion(raw: Any, *, accounts: Mapping[str, Mapping[str, Any]], parties: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    """Normalize a model answer or raise ``ValueError``; only ledger-known identities pass."""

    if not isinstance(raw, Mapping):
        raise ValueError("answer must be a JSON object")
    action = raw.get("party_action")
    if action not in PARTY_ACTIONS:
        raise ValueError("party_action is invalid")
    reason = str(raw.get("reason") or "").strip()
    if not reason or len(reason) > 500:
        raise ValueError("reason is required (≤500 chars)")
    try:
        confidence = Decimal(str(raw.get("confidence")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("confidence must be numeric") from exc
    if not confidence.is_finite() or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    counter = raw.get("counter_account")
    party_id = raw.get("party_id")
    if action == "internal_transfer":
        counter, party_id = None, None
    else:
        account = accounts.get(str(counter or ""))
        if account is None:
            raise ValueError("counter_account is not a candidate account")
        counter = str(counter)
        if account["requires_party"]:
            if action not in ("settle_by_waybill", "party_fixed"):
                raise ValueError("party account needs settle_by_waybill or party_fixed")
            if isinstance(party_id, bool) or not isinstance(party_id, int) or party_id not in parties:
                raise ValueError("party_id is not a known active party")
        else:
            if action in ("settle_by_waybill", "party_fixed"):
                raise ValueError("party actions need a party account")
            party_id = None
    return {"counter_account": counter, "party_action": action, "party_id": party_id,
            "confidence": format(confidence.quantize(Decimal("0.01")), "f"), "reason": reason}


class ReviewAdviceMixin:
    def review_evidence(self, review_id: Any) -> dict[str, Any]:
        """Aggregate, non-credential evidence of one open review plus the allowed answer space."""

        review_id = int(review_id)

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute(
                "SELECT r.*, s.display_name, s.fund_account FROM finance_reviews r JOIN finance_sources s ON s.id=r.source_id WHERE r.id=%s",
                (review_id,),
            )
            review = fetchone(cursor)
            if review is None:
                raise FinanceNotFoundError("待审不存在。", code="REVIEW_NOT_FOUND")
            if review["status"] != "open":
                raise FinanceError("REVIEW_NOT_OPEN", "该待审已处理。")
            sign = ">" if review["direction"] == "in" else "<"
            cursor.execute(
                "SELECT business_date, amount, counterparty, reference, waybill_no FROM finance_source_lines WHERE source_id=%s"
                f" AND is_active=1 AND posting_status='suspense' AND item_l1=%s AND item_l2=%s AND amount {sign} 0"
                " ORDER BY business_date DESC, id DESC LIMIT %s",
                (review["source_id"], review["item_l1"], review["item_l2"], MAX_SAMPLE_LINES),
            )
            samples = fetchall(cursor)
            settings = self._load_settings(cursor)
            cursor.execute(
                "SELECT code, name, root_type, account_type, requires_party FROM finance_accounts"
                " WHERE is_group=0 AND is_active=1 AND frozen=0 AND code NOT IN (%s, %s) ORDER BY code",
                (settings["suspense_account"], review["fund_account"] or ""),
            )
            accounts = {row["code"]: {**row, "requires_party": bool(row["requires_party"])} for row in fetchall(cursor)}
            cursor.execute("SELECT id, name, party_type FROM finance_parties WHERE is_active=1 ORDER BY party_type, name")
            parties = {int(row["id"]): row for row in fetchall(cursor)}
            cursor.execute(
                "SELECT item_l1, item_l2, direction, counter_account, party_action FROM finance_line_rules WHERE provider=%s AND is_active=1"
                " ORDER BY id DESC LIMIT %s", (review["provider"], MAX_RULE_EXAMPLES),
            )
            examples = fetchall(cursor)
            with_waybill = sum(1 for line in samples if line["waybill_no"])
            return {
                "review": {"id": review_id, "source": review["display_name"], "provider": review["provider"],
                           "item_l1": review["item_l1"], "item_l2": review["item_l2"],
                           "direction": "流入资金账户" if review["direction"] == "in" else "流出资金账户",
                           "line_count": int(review["line_count"]), "amount_total": f"{Decimal(review['amount_total']):.2f}",
                           "waybill_share_in_samples": f"{with_waybill}/{len(samples)}"},
                "samples": [{"date": line["business_date"].isoformat(), "amount": f"{Decimal(line['amount']):.2f}",
                             "counterparty": line["counterparty"][:60], "reference": line["reference"][:60],
                             "has_waybill": bool(line["waybill_no"])} for line in samples],
                "existing_rules_of_provider": examples,
                "candidate_accounts": [{"code": code, "name": row["name"], "root_type": row["root_type"],
                                        "account_type": row["account_type"], "requires_party": row["requires_party"]}
                                       for code, row in accounts.items()],
                "candidate_parties": [{"id": pid, "name": row["name"], "party_type": row["party_type"]} for pid, row in parties.items()],
                "_accounts": accounts, "_parties": parties,
            }

        return self._read(work)

    def save_review_suggestion(self, review_id: Any, suggestion: Mapping[str, Any], *, model: str) -> dict[str, Any]:
        review_id = int(review_id)

        def work(cursor: Any) -> dict[str, Any]:
            cursor.execute("SELECT status FROM finance_reviews WHERE id=%s FOR UPDATE", (review_id,))
            row = fetchone(cursor)
            if row is None:
                raise FinanceNotFoundError("待审不存在。", code="REVIEW_NOT_FOUND")
            if row["status"] != "open":
                raise FinanceError("REVIEW_NOT_OPEN", "该待审已处理。")
            cursor.execute("UPDATE finance_reviews SET suggestion_json=%s, suggested_by_model=%s, suggested_at=CURRENT_TIMESTAMP(6)"
                           " WHERE id=%s", (json_text(dict(suggestion)), str(model)[:128], review_id))
            return {"id": review_id, "suggestion": dict(suggestion), "model": str(model)[:128]}

        return self._read(work)
