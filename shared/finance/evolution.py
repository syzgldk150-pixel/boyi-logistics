"""Deterministic finance derivatives, review cases, and knowledge snapshots.

The mixin deliberately owns no configuration or filesystem access.  Its
caller supplies the DB connection factory through ``FinanceRepository``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

from shared.finance.money import ZERO, format_money
from shared.finance.sources import (
    enabled_finance_platforms,
    enabled_finance_source_specs,
    is_finance_source_enabled,
)


def _enabled_platform_clause(column: str) -> tuple[str, list[Any]]:
    platforms = enabled_finance_platforms()
    if not platforms:
        return "1 = 0", []
    placeholders = ", ".join(["%s"] * len(platforms))
    return f"{column} IN ({placeholders})", list(platforms)


def _enabled_source_clause(
    *, platform_column: str, account_column: str
) -> tuple[str, list[Any]]:
    specs = enabled_finance_source_specs()
    if not specs:
        return "1 = 0", []
    pairs = " OR ".join(
        f"({platform_column} = %s AND {account_column} = %s)" for _ in specs
    )
    return (
        f"({pairs})",
        [value for spec in specs for value in (spec.platform, spec.account_id)],
    )


def _row(cursor: Any, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    names = [str(item[0]) for item in (getattr(cursor, "description", None) or ())]
    return dict(zip(names, value))


def _one(cursor: Any) -> dict[str, Any] | None:
    return _row(cursor, cursor.fetchone())


def _all(cursor: Any) -> list[dict[str, Any]]:
    return [item for value in (cursor.fetchall() or []) if (item := _row(cursor, value))]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> dt.datetime:
    return dt.datetime.now().replace(tzinfo=None)


def _serialize(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, dt.datetime):
            result[str(key)] = value.isoformat(sep=" ")
        elif isinstance(value, dt.date):
            result[str(key)] = value.isoformat()
        elif isinstance(value, Decimal):
            result[str(key)] = format_money(value, missing_as_zero=True)
        elif isinstance(value, bytes):
            result[str(key)] = value.decode("utf-8", errors="replace")
        else:
            result[str(key)] = value
    return result


def _fingerprint(*parts: Any) -> str:
    value = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_EFFECTIVE_MAPPING_JOIN = """
    LEFT JOIN finance_fee_mappings fm ON fm.id = (
        SELECT fm2.id
        FROM finance_fee_mappings fm2
        WHERE fm2.fee_item_id = t.fee_item_id
          AND fm2.superseded_at IS NULL
          AND fm2.mapping_status = 'bound'
          AND fm2.effective_start_month <= CAST(DATE_FORMAT(t.business_date, '%%Y-%%m-01') AS DATE)
          AND (
                fm2.effective_end_month IS NULL
                OR fm2.effective_end_month >= CAST(DATE_FORMAT(t.business_date, '%%Y-%%m-01') AS DATE)
          )
        ORDER BY fm2.effective_start_month DESC, fm2.version_no DESC, fm2.id DESC
        LIMIT 1
    )
"""


_VISIBLE_TRANSACTION_JOIN = """
    INNER JOIN (
        SELECT platform, account_id, target_date, MAX(id) AS latest_run_id
        FROM finance_sync_runs
        WHERE status IN ('success', 'no_data')
        GROUP BY platform, account_id, target_date
    ) latest
      ON latest.platform = t.platform
     AND latest.account_id = t.account_id
     AND latest.target_date = t.business_date
     AND latest.latest_run_id = t.run_id
"""


class FinanceEvolutionMixin:
    """Methods mixed into the shared ``FinanceRepository``."""

    @staticmethod
    def _subject_code(platform: str, subject_name: str) -> str:
        digest = hashlib.sha256(f"{platform}\x1f{subject_name}".encode("utf-8")).hexdigest()[:20]
        return f"custom_{digest}"

    def _ensure_fee_subject(
        self,
        cursor: Any,
        *,
        platform: str,
        subject_code: str,
        subject_name: str,
        fee_level: str,
        booking_fee_name: str,
        requires_waybill: bool,
        created_by: str,
    ) -> int:
        code = str(subject_code or "").strip() or self._subject_code(platform, subject_name)
        name = str(subject_name or "").strip()
        if not name:
            raise ValueError("canonical_subject_name is required")
        if fee_level not in {"waybill", "operating"}:
            raise ValueError("invalid canonical subject fee level")
        if fee_level == "operating" and requires_waybill:
            raise ValueError("operating subject cannot require a waybill")
        now = _now()
        cursor.execute(
            """
            INSERT INTO finance_fee_subjects (
                platform, subject_code, subject_name, default_fee_level,
                booking_fee_name, requires_waybill, is_active,
                created_by, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                subject_name = VALUES(subject_name),
                default_fee_level = VALUES(default_fee_level),
                booking_fee_name = VALUES(booking_fee_name),
                requires_waybill = VALUES(requires_waybill),
                is_active = 1,
                updated_at = VALUES(updated_at)
            """,
            (
                platform,
                code,
                name,
                fee_level,
                booking_fee_name or None,
                1 if requires_waybill else 0,
                created_by,
                now,
                now,
            ),
        )
        cursor.execute(
            "SELECT id FROM finance_fee_subjects WHERE platform = %s AND subject_code = %s",
            (platform, code),
        )
        row = _one(cursor)
        if not row:
            raise RuntimeError("canonical subject upsert did not return an id")
        return int(row["id"])

    def _refresh_run_derivatives(self, cursor: Any, run: Mapping[str, Any]) -> dict[str, int]:
        run_id = int(run["id"])
        platform = str(run["platform"])
        account_id = str(run["account_id"])
        if not is_finance_source_enabled(platform, account_id):
            raise ValueError("finance source is not enabled")
        business_date = run["target_date"]
        now = _now()

        cursor.execute(
            """
            DELETE FROM finance_waybill_facts
            WHERE platform = %s AND account_id = %s AND business_date = %s
            """,
            (platform, account_id, business_date),
        )
        cursor.execute(
            f"""
            INSERT INTO finance_waybill_facts (
                platform, account_id, business_date, waybill_no,
                canonical_subject_id, mapping_id, income, expense, net_change,
                transaction_count, source_run_id, mapping_version, created_at, updated_at
            )
            SELECT t.platform, t.account_id, t.business_date, TRIM(t.waybill_no),
                   fm.canonical_subject_id, fm.id,
                   SUM(t.income), SUM(t.expense), SUM(t.income - t.expense),
                   COUNT(*), t.run_id, fm.version_no, %s, %s
            FROM finance_transactions t
            {_EFFECTIVE_MAPPING_JOIN}
            WHERE t.run_id = %s
              AND fm.id IS NOT NULL
              AND fm.fee_level = 'waybill'
              AND fm.canonical_subject_id IS NOT NULL
              AND COALESCE(TRIM(t.waybill_no), '') <> ''
            GROUP BY t.platform, t.account_id, t.business_date, TRIM(t.waybill_no),
                     fm.canonical_subject_id, fm.id, t.run_id, fm.version_no
            """,
            (now, now, run_id),
        )
        fact_count = int(cursor.rowcount or 0)

        cursor.execute(
            """
            UPDATE finance_anomalies
            SET status = 'resolved', last_seen_at = %s
            WHERE anomaly_type = 'MISSING_WAYBILL'
              AND platform = %s AND account_id = %s AND business_date = %s
              AND status = 'open'
            """,
            (now, platform, account_id, business_date),
        )

        cursor.execute(
            f"""
            SELECT t.fee_item_id, t.raw_primary_fee_name, t.raw_secondary_fee_name,
                   SUM(t.income - t.expense) AS amount, COUNT(*) AS occurrence_count
            FROM finance_transactions t
            {_EFFECTIVE_MAPPING_JOIN}
            WHERE t.run_id = %s
              AND fm.id IS NOT NULL
              AND fm.fee_level = 'waybill'
              AND fm.canonical_subject_id IS NOT NULL
              AND fm.requires_waybill = 1
              AND COALESCE(TRIM(t.waybill_no), '') = ''
            GROUP BY t.fee_item_id, t.raw_primary_fee_name, t.raw_secondary_fee_name
            """,
            (run_id,),
        )
        missing_rows = _all(cursor)
        for item in missing_rows:
            fingerprint = _fingerprint(
                "MISSING_WAYBILL", platform, account_id, business_date, item["fee_item_id"]
            )
            details = {
                "primary_fee_name": str(item.get("raw_primary_fee_name") or ""),
                "secondary_fee_name": str(item.get("raw_secondary_fee_name") or ""),
            }
            cursor.execute(
                """
                INSERT INTO finance_anomalies (
                    fingerprint, anomaly_type, platform, account_id, business_date,
                    fee_item_id, status, occurrence_count, amount, details_json,
                    first_seen_at, last_seen_at
                ) VALUES (%s, 'MISSING_WAYBILL', %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    occurrence_count = VALUES(occurrence_count),
                    amount = VALUES(amount),
                    details_json = VALUES(details_json),
                    notified_at = IF(status = 'resolved', NULL, notified_at),
                    status = 'open',
                    last_seen_at = VALUES(last_seen_at)
                """,
                (
                    fingerprint,
                    platform,
                    account_id,
                    business_date,
                    int(item["fee_item_id"]),
                    int(item.get("occurrence_count") or 0),
                    item.get("amount") or ZERO,
                    _json(details),
                    now,
                    now,
                ),
            )

        cursor.execute(
            f"""
            SELECT DISTINCT t.fee_item_id
            FROM finance_transactions t
            {_EFFECTIVE_MAPPING_JOIN}
            WHERE t.run_id = %s
              AND (fm.id IS NULL OR fm.canonical_subject_id IS NULL)
            """,
            (run_id,),
        )
        unknown_fee_ids = [int(item["fee_item_id"]) for item in _all(cursor)]
        for fee_item_id in unknown_fee_ids:
            cursor.execute(
                f"""
                SELECT MIN(t.business_date) AS first_seen_date,
                       MAX(t.business_date) AS last_seen_date,
                       COUNT(*) AS transaction_count,
                       COALESCE(SUM(t.income), 0) AS income,
                       COALESCE(SUM(t.expense), 0) AS expense,
                       COALESCE(SUM(t.income - t.expense), 0) AS net_change,
                       SUM(CASE WHEN COALESCE(TRIM(t.waybill_no), '') <> '' THEN 1 ELSE 0 END)
                           AS waybill_present_count,
                       SUM(CASE WHEN COALESCE(TRIM(t.waybill_no), '') = '' THEN 1 ELSE 0 END)
                           AS waybill_missing_count
                FROM finance_transactions t
                {_VISIBLE_TRANSACTION_JOIN}
                WHERE t.fee_item_id = %s
                """,
                (fee_item_id,),
            )
            evidence = _one(cursor) or {}
            if not evidence.get("first_seen_date"):
                continue
            cursor.execute(
                """
                INSERT INTO finance_review_cases (
                    fee_item_id, status, first_seen_date, last_seen_date,
                    transaction_count, income, expense, net_change,
                    waybill_present_count, waybill_missing_count,
                    ai_status, created_at, updated_at
                ) VALUES (%s, 'open', %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                ON DUPLICATE KEY UPDATE
                    ai_status = IF(
                        status = 'open' AND (
                            first_seen_date <> VALUES(first_seen_date)
                            OR last_seen_date <> VALUES(last_seen_date)
                            OR transaction_count <> VALUES(transaction_count)
                            OR income <> VALUES(income)
                            OR expense <> VALUES(expense)
                            OR net_change <> VALUES(net_change)
                            OR waybill_present_count <> VALUES(waybill_present_count)
                            OR waybill_missing_count <> VALUES(waybill_missing_count)
                        ),
                        'pending',
                        ai_status
                    ),
                    first_seen_date = VALUES(first_seen_date),
                    last_seen_date = VALUES(last_seen_date),
                    transaction_count = VALUES(transaction_count),
                    income = VALUES(income), expense = VALUES(expense),
                    net_change = VALUES(net_change),
                    waybill_present_count = VALUES(waybill_present_count),
                    waybill_missing_count = VALUES(waybill_missing_count),
                    updated_at = VALUES(updated_at)
                """,
                (
                    fee_item_id,
                    evidence["first_seen_date"],
                    evidence["last_seen_date"],
                    int(evidence.get("transaction_count") or 0),
                    evidence.get("income") or ZERO,
                    evidence.get("expense") or ZERO,
                    evidence.get("net_change") or ZERO,
                    int(evidence.get("waybill_present_count") or 0),
                    int(evidence.get("waybill_missing_count") or 0),
                    now,
                    now,
                ),
            )
            fingerprint = _fingerprint("UNKNOWN_FEE", platform, fee_item_id)
            cursor.execute(
                """
                INSERT INTO finance_anomalies (
                    fingerprint, anomaly_type, platform, account_id, business_date,
                    fee_item_id, status, occurrence_count, amount, details_json,
                    first_seen_at, last_seen_at
                ) VALUES (%s, 'UNKNOWN_FEE', %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    occurrence_count = VALUES(occurrence_count), amount = VALUES(amount),
                    details_json = VALUES(details_json), status = 'open',
                    last_seen_at = VALUES(last_seen_at)
                """,
                (
                    fingerprint,
                    platform,
                    account_id,
                    business_date,
                    fee_item_id,
                    int(evidence.get("transaction_count") or 0),
                    evidence.get("net_change") or ZERO,
                    _json({"review_case": True}),
                    now,
                    now,
                ),
            )
        return {
            "waybill_fact_count": fact_count,
            "unknown_fee_count": len(unknown_fee_ids),
            "missing_waybill_fee_count": len(missing_rows),
        }

    def rebuild_waybill_facts_for_fee_item(
        self,
        *,
        fee_item_id: int,
        reviewed_by: str,
        review_reason: str,
    ) -> dict[str, Any]:
        now = _now()
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT id, platform FROM finance_fee_items WHERE id = %s FOR UPDATE",
                    (int(fee_item_id),),
                )
                fee_item = _one(cursor)
                if not fee_item:
                    raise ValueError("fee item does not exist")
                if str(fee_item["platform"]) not in enabled_finance_platforms():
                    raise ValueError("fee item's finance source is not enabled")
                cursor.execute(
                    """
                    DELETE f FROM finance_waybill_facts f
                    INNER JOIN finance_fee_mappings fm ON fm.id = f.mapping_id
                    WHERE fm.fee_item_id = %s
                    """,
                    (int(fee_item_id),),
                )
                cursor.execute(
                    f"""
                    INSERT INTO finance_waybill_facts (
                        platform, account_id, business_date, waybill_no,
                        canonical_subject_id, mapping_id, income, expense, net_change,
                        transaction_count, source_run_id, mapping_version, created_at, updated_at
                    )
                    SELECT t.platform, t.account_id, t.business_date, TRIM(t.waybill_no),
                           fm.canonical_subject_id, fm.id,
                           SUM(t.income), SUM(t.expense), SUM(t.income - t.expense),
                           COUNT(*), t.run_id, fm.version_no, %s, %s
                    FROM finance_transactions t
                    {_VISIBLE_TRANSACTION_JOIN}
                    {_EFFECTIVE_MAPPING_JOIN}
                    WHERE t.fee_item_id = %s
                      AND fm.id IS NOT NULL
                      AND fm.fee_level = 'waybill'
                      AND fm.canonical_subject_id IS NOT NULL
                      AND COALESCE(TRIM(t.waybill_no), '') <> ''
                    GROUP BY t.platform, t.account_id, t.business_date, TRIM(t.waybill_no),
                             fm.canonical_subject_id, fm.id, t.run_id, fm.version_no
                    """,
                    (now, now, int(fee_item_id)),
                )
                rebuilt = int(cursor.rowcount or 0)
                cursor.execute(
                    """
                    UPDATE finance_anomalies
                    SET status = 'resolved', last_seen_at = %s
                    WHERE fee_item_id = %s AND anomaly_type = 'MISSING_WAYBILL'
                      AND status = 'open'
                    """,
                    (now, int(fee_item_id)),
                )
                cursor.execute(
                    f"""
                    SELECT t.platform, t.account_id, t.business_date,
                           t.raw_primary_fee_name, t.raw_secondary_fee_name,
                           COUNT(*) AS occurrence_count,
                           COALESCE(SUM(t.income - t.expense), 0) AS amount
                    FROM finance_transactions t
                    {_VISIBLE_TRANSACTION_JOIN}
                    {_EFFECTIVE_MAPPING_JOIN}
                    WHERE t.fee_item_id = %s
                      AND fm.id IS NOT NULL
                      AND fm.fee_level = 'waybill'
                      AND fm.requires_waybill = 1
                      AND COALESCE(TRIM(t.waybill_no), '') = ''
                    GROUP BY t.platform, t.account_id, t.business_date,
                             t.raw_primary_fee_name, t.raw_secondary_fee_name
                    """,
                    (int(fee_item_id),),
                )
                missing_rows = _all(cursor)
                for item in missing_rows:
                    fingerprint = _fingerprint(
                        "MISSING_WAYBILL",
                        item["platform"],
                        item["account_id"],
                        item["business_date"],
                        fee_item_id,
                    )
                    cursor.execute(
                        """
                        INSERT INTO finance_anomalies (
                            fingerprint, anomaly_type, platform, account_id,
                            business_date, fee_item_id, status, occurrence_count,
                            amount, details_json, first_seen_at, last_seen_at
                        ) VALUES (%s, 'MISSING_WAYBILL', %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            occurrence_count = VALUES(occurrence_count),
                            amount = VALUES(amount), details_json = VALUES(details_json),
                            notified_at = IF(status = 'resolved', NULL, notified_at),
                            status = 'open', last_seen_at = VALUES(last_seen_at)
                        """,
                        (
                            fingerprint,
                            item["platform"],
                            item["account_id"],
                            item["business_date"],
                            int(fee_item_id),
                            int(item.get("occurrence_count") or 0),
                            item.get("amount") or ZERO,
                            _json(
                                {
                                    "primary_fee_name": str(item.get("raw_primary_fee_name") or ""),
                                    "secondary_fee_name": str(item.get("raw_secondary_fee_name") or ""),
                                }
                            ),
                            now,
                            now,
                        ),
                    )
                cursor.execute(
                    """
                    UPDATE finance_review_cases
                    SET status = 'approved', reviewed_by = %s, reviewed_at = %s,
                        review_reason = %s, updated_at = %s
                    WHERE fee_item_id = %s
                    """,
                    (reviewed_by, now, review_reason, now, int(fee_item_id)),
                )
                cursor.execute(
                    "UPDATE finance_anomalies SET status = 'resolved', last_seen_at = %s WHERE fee_item_id = %s AND anomaly_type = 'UNKNOWN_FEE'",
                    (now, int(fee_item_id)),
                )
            finally:
                cursor.close()
        return {
            "fee_item_id": int(fee_item_id),
            "rebuilt_fact_count": rebuilt,
            "missing_waybill_group_count": len(missing_rows),
        }

    def list_fee_subjects(self, *, platform: str | None = None) -> list[dict[str, Any]]:
        enabled_clause, enabled_params = _enabled_platform_clause("platform")
        clauses = ["is_active = 1", enabled_clause]
        params: list[Any] = list(enabled_params)
        if platform:
            clauses.append("platform = %s")
            params.append(str(platform))
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT id, platform, subject_code, subject_name, default_fee_level,
                           booking_fee_name, requires_waybill
                    FROM finance_fee_subjects
                    WHERE {' AND '.join(clauses)}
                    ORDER BY platform, default_fee_level, subject_name
                    """,
                    tuple(params),
                )
                rows = _all(cursor)
            finally:
                cursor.close()
        return [{**_serialize(item), "requires_waybill": bool(item.get("requires_waybill"))} for item in rows]

    def list_review_cases(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        enabled_clause, enabled_params = _enabled_platform_clause("fi.platform")
        clauses = [enabled_clause]
        params: list[Any] = list(enabled_params)
        if status:
            clauses.append("rc.status = %s")
            params.append(str(status))
        where = " AND ".join(clauses)
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM finance_review_cases rc
                    INNER JOIN finance_fee_items fi ON fi.id = rc.fee_item_id
                    WHERE {where}
                    """,
                    tuple(params),
                )
                total = int((_one(cursor) or {}).get("total") or 0)
                cursor.execute(
                    f"""
                    SELECT rc.*, fi.platform,
                           fi.raw_primary_fee_name AS primary_fee_name,
                           fi.raw_secondary_fee_name AS secondary_fee_name,
                           fi.direction, ai.provider AS ai_provider, ai.model AS ai_model,
                           ai.suggestion_json, ai.error_code AS ai_error_code,
                           ai.error_message AS ai_error_message
                    FROM finance_review_cases rc
                    INNER JOIN finance_fee_items fi ON fi.id = rc.fee_item_id
                    LEFT JOIN finance_review_ai_runs ai ON ai.id = rc.current_ai_run_id
                    WHERE {where}
                    ORDER BY (rc.status = 'open') DESC, rc.updated_at DESC, rc.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*params, int(limit), int(offset)),
                )
                rows = _all(cursor)
            finally:
                cursor.close()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _serialize(row)
            suggestion = row.get("suggestion_json")
            if isinstance(suggestion, str):
                try:
                    suggestion = json.loads(suggestion)
                except ValueError:
                    suggestion = None
            item["suggestion"] = suggestion if isinstance(suggestion, dict) else None
            item.pop("suggestion_json", None)
            items.append(item)
        return {"items": items, "total": total, "limit": int(limit), "offset": int(offset)}

    def pending_review_evidence(self, *, limit: int = 20) -> list[dict[str, Any]]:
        payload = self.list_review_cases(status="open", limit=limit, offset=0)
        return [item for item in payload["items"] if item.get("ai_status") in {"pending", "failed"}]

    def mark_review_notified(self, review_case_id: int) -> None:
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "UPDATE finance_review_cases SET notified_at = %s, updated_at = %s WHERE id = %s",
                    (_now(), _now(), int(review_case_id)),
                )
            finally:
                cursor.close()

    def reject_review_case(self, review_case_id: int, *, reviewed_by: str, reason: str) -> None:
        actor = str(reviewed_by or "").strip()
        review_reason = str(reason or "").strip()
        if not actor or not review_reason:
            raise ValueError("reviewed_by and reason are required")
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT rc.id, fi.platform
                    FROM finance_review_cases rc
                    INNER JOIN finance_fee_items fi ON fi.id = rc.fee_item_id
                    WHERE rc.id = %s AND rc.status = 'open'
                    FOR UPDATE
                    """,
                    (int(review_case_id),),
                )
                review_case = _one(cursor)
                if not review_case:
                    raise ValueError("only an open review case can be rejected")
                if str(review_case["platform"]) not in enabled_finance_platforms():
                    raise ValueError("review case's finance source is not enabled")
                cursor.execute(
                    """
                    UPDATE finance_review_cases
                    SET status = 'rejected', reviewed_by = %s, reviewed_at = %s,
                        review_reason = %s, updated_at = %s
                    WHERE id = %s AND status = 'open'
                    """,
                    (actor, _now(), review_reason[:500], _now(), int(review_case_id)),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise ValueError("only an open review case can be rejected")
            finally:
                cursor.close()

    def list_unnotified_anomalies(self, *, limit: int = 50) -> list[dict[str, Any]]:
        enabled_clause, enabled_params = _enabled_source_clause(
            platform_column="a.platform", account_column="a.account_id"
        )
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT a.id, a.anomaly_type, a.platform, a.account_id,
                           a.business_date, a.occurrence_count, a.amount,
                           a.details_json, fi.raw_primary_fee_name AS primary_fee_name,
                           fi.raw_secondary_fee_name AS secondary_fee_name
                    FROM finance_anomalies a
                    LEFT JOIN finance_fee_items fi ON fi.id = a.fee_item_id
                    WHERE a.status = 'open' AND a.notified_at IS NULL
                      AND {enabled_clause}
                    ORDER BY a.first_seen_at, a.id LIMIT %s
                    """.format(enabled_clause=enabled_clause),
                    (*enabled_params, max(1, min(int(limit), 200))),
                )
                rows = _all(cursor)
            finally:
                cursor.close()
        return [_serialize(row) for row in rows]

    def mark_anomalies_notified(self, anomaly_ids: list[int]) -> None:
        ids = sorted({int(value) for value in anomaly_ids if int(value) > 0})
        if not ids:
            return
        placeholders = ", ".join(["%s"] * len(ids))
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"UPDATE finance_anomalies SET notified_at = %s WHERE id IN ({placeholders})",
                    (_now(), *ids),
                )
            finally:
                cursor.close()

    def start_review_ai_run(
        self,
        *,
        review_case_id: int,
        provider: str,
        model: str,
        evidence: Mapping[str, Any],
    ) -> int:
        now = _now()
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO finance_review_ai_runs (
                        review_case_id, provider, model, status, evidence_json, started_at
                    ) VALUES (%s, %s, %s, 'running', %s, %s)
                    """,
                    (int(review_case_id), provider, model, _json(dict(evidence)), now),
                )
                run_id = int(cursor.lastrowid)
                cursor.execute(
                    "UPDATE finance_review_cases SET ai_status = 'running', current_ai_run_id = %s, updated_at = %s WHERE id = %s",
                    (run_id, now, int(review_case_id)),
                )
            finally:
                cursor.close()
        return run_id

    def finish_review_ai_run(
        self,
        *,
        review_case_id: int,
        ai_run_id: int,
        suggestion: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = _now()
        status = "success" if suggestion is not None else "failed"
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE finance_review_ai_runs
                    SET status = %s, suggestion_json = %s, error_code = %s,
                        error_message = %s, finished_at = %s
                    WHERE id = %s AND review_case_id = %s
                    """,
                    (
                        status,
                        _json(dict(suggestion)) if suggestion is not None else None,
                        error_code,
                        error_message,
                        now,
                        int(ai_run_id),
                        int(review_case_id),
                    ),
                )
                cursor.execute(
                    "UPDATE finance_review_cases SET ai_status = %s, updated_at = %s WHERE id = %s AND current_ai_run_id = %s",
                    (status, now, int(review_case_id), int(ai_run_id)),
                )
            finally:
                cursor.close()

    def list_waybill_facts(
        self,
        *,
        start_date: dt.date,
        end_date: dt.date,
        platform: str | None = None,
        account_id: str | None = None,
        waybill_no: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        enabled_clause, enabled_params = _enabled_source_clause(
            platform_column="f.platform", account_column="f.account_id"
        )
        clauses = ["f.business_date BETWEEN %s AND %s", enabled_clause]
        params: list[Any] = [start_date, end_date, *enabled_params]
        for column, value in (("f.platform", platform), ("f.account_id", account_id), ("f.waybill_no", waybill_no)):
            if value:
                clauses.append(f"{column} = %s")
                params.append(value)
        where = " AND ".join(clauses)
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(f"SELECT COUNT(*) AS total FROM finance_waybill_facts f WHERE {where}", tuple(params))
                total = int((_one(cursor) or {}).get("total") or 0)
                cursor.execute(
                    f"""
                    SELECT f.platform, f.account_id, f.business_date, f.waybill_no,
                           s.subject_code, s.subject_name, f.income, f.expense,
                           f.net_change, f.transaction_count, f.mapping_version
                    FROM finance_waybill_facts f
                    INNER JOIN finance_fee_subjects s ON s.id = f.canonical_subject_id
                    WHERE {where}
                    ORDER BY f.business_date DESC, f.waybill_no, s.subject_name
                    LIMIT %s OFFSET %s
                    """,
                    (*params, int(limit), int(offset)),
                )
                rows = _all(cursor)
            finally:
                cursor.close()
        return {"items": [_serialize(item) for item in rows], "total": total, "limit": int(limit), "offset": int(offset)}

    def get_evolution_summary(self, query: Any) -> dict[str, Any]:
        enabled_clause, enabled_params = _enabled_source_clause(
            platform_column="t.platform", account_column="t.account_id"
        )
        clauses = ["t.business_date BETWEEN %s AND %s", enabled_clause]
        params: list[Any] = [query.start_date, query.end_date, *enabled_params]
        if query.platform:
            clauses.append("t.platform = %s")
            params.append(query.platform.value)
        if query.account_id:
            clauses.append("t.account_id = %s")
            params.append(query.account_id)
        where = " AND ".join(clauses)
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT
                        COALESCE(SUM(CASE WHEN fm.canonical_subject_id IS NOT NULL AND fm.fee_level = 'waybill' AND COALESCE(TRIM(t.waybill_no), '') <> '' THEN t.income - t.expense ELSE 0 END), 0) AS waybill_net,
                        COALESCE(SUM(CASE WHEN fm.canonical_subject_id IS NOT NULL AND fm.fee_level = 'operating' THEN t.income - t.expense ELSE 0 END), 0) AS operating_net,
                        COALESCE(SUM(CASE WHEN fm.canonical_subject_id IS NOT NULL THEN t.income - t.expense ELSE 0 END), 0) AS classified_net,
                        COALESCE(SUM(CASE WHEN fm.id IS NULL OR fm.canonical_subject_id IS NULL THEN t.income - t.expense ELSE 0 END), 0) AS unclassified_net,
                        COALESCE(SUM(CASE WHEN fm.canonical_subject_id IS NOT NULL AND fm.fee_level = 'waybill' AND fm.requires_waybill = 1 AND COALESCE(TRIM(t.waybill_no), '') = '' THEN t.income - t.expense ELSE 0 END), 0) AS missing_waybill_net,
                        SUM(CASE WHEN fm.canonical_subject_id IS NOT NULL THEN 1 ELSE 0 END) AS classified_rows,
                        SUM(CASE WHEN fm.id IS NULL OR fm.canonical_subject_id IS NULL THEN 1 ELSE 0 END) AS unclassified_rows,
                        SUM(CASE WHEN fm.canonical_subject_id IS NOT NULL AND fm.fee_level = 'waybill' AND fm.requires_waybill = 1 AND COALESCE(TRIM(t.waybill_no), '') = '' THEN 1 ELSE 0 END) AS missing_waybill_rows
                    FROM finance_transactions t
                    {_VISIBLE_TRANSACTION_JOIN}
                    {_EFFECTIVE_MAPPING_JOIN}
                    WHERE {where}
                    """,
                    tuple(params),
                )
                row = _one(cursor) or {}
            finally:
                cursor.close()
        unclassified_rows = int(row.get("unclassified_rows") or 0)
        missing_rows = int(row.get("missing_waybill_rows") or 0)
        return {
            "waybill_net": format_money(row.get("waybill_net") or ZERO, missing_as_zero=True),
            "operating_net": format_money(row.get("operating_net") or ZERO, missing_as_zero=True),
            "classified_net": format_money(row.get("classified_net") or ZERO, missing_as_zero=True),
            "unclassified_net": format_money(row.get("unclassified_net") or ZERO, missing_as_zero=True),
            "missing_waybill_net": format_money(row.get("missing_waybill_net") or ZERO, missing_as_zero=True),
            "classified_rows": int(row.get("classified_rows") or 0),
            "unclassified_rows": unclassified_rows,
            "missing_waybill_rows": missing_rows,
            "classification_complete": unclassified_rows == 0 and missing_rows == 0,
        }

    def get_knowledge_snapshot(self) -> dict[str, Any]:
        enabled_clause, enabled_params = _enabled_platform_clause("fi.platform")
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT COALESCE(MAX(log.id), 0) AS version_no
                    FROM finance_mapping_audit_logs log
                    INNER JOIN finance_fee_items fi ON fi.id = log.fee_item_id
                    WHERE {enabled_clause}
                    """.format(enabled_clause=enabled_clause),
                    tuple(enabled_params),
                )
                version_no = int((_one(cursor) or {}).get("version_no") or 0)
                cursor.execute(
                    """
                    SELECT fi.platform, fi.raw_primary_fee_name AS primary_fee_name,
                           fi.raw_secondary_fee_name AS secondary_fee_name, fi.direction,
                           fm.fee_level, s.subject_code, s.subject_name,
                           fm.booking_fee_name, fm.requires_waybill,
                           fm.effective_start_month, fm.effective_end_month,
                           fm.version_no, fm.created_by, fm.change_reason
                    FROM finance_fee_mappings fm
                    INNER JOIN finance_fee_items fi ON fi.id = fm.fee_item_id
                    INNER JOIN finance_fee_subjects s ON s.id = fm.canonical_subject_id
                    WHERE fm.superseded_at IS NULL AND fm.mapping_status = 'bound'
                      AND {enabled_clause}
                    ORDER BY fi.platform, fi.raw_primary_fee_name,
                             fi.raw_secondary_fee_name, fi.direction,
                             fm.effective_start_month, fm.version_no
                    """.format(enabled_clause=enabled_clause),
                    tuple(enabled_params),
                )
                rows = _all(cursor)
            finally:
                cursor.close()
        return {"version_no": version_no, "items": [_serialize(item) for item in rows]}

    def record_knowledge_export(
        self,
        *,
        version_no: int,
        content_sha256: str,
        relative_path: str,
        mapping_count: int,
        generated_by: str,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO finance_knowledge_exports (
                        version_no, content_sha256, relative_path, mapping_count,
                        generated_by, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        content_sha256 = VALUES(content_sha256),
                        relative_path = VALUES(relative_path),
                        mapping_count = VALUES(mapping_count),
                        generated_by = VALUES(generated_by)
                    """,
                    (
                        int(version_no),
                        content_sha256,
                        relative_path,
                        int(mapping_count),
                        generated_by,
                        _now(),
                    ),
                )
                export_id = int(cursor.lastrowid or 0)
                if export_id == 0:
                    cursor.execute("SELECT id FROM finance_knowledge_exports WHERE version_no = %s", (int(version_no),))
                    export_id = int((_one(cursor) or {}).get("id") or 0)
            finally:
                cursor.close()
        return export_id

    def latest_knowledge_export(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT * FROM finance_knowledge_exports ORDER BY version_no DESC LIMIT 1")
                row = _one(cursor)
            finally:
                cursor.close()
        return _serialize(row) if row else None
