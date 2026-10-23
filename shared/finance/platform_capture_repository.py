"""Persist verified platform captures and track their daily ledger publication."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any, Mapping

from shared.finance.evolution import _all, _enabled_platform_clause, _now, _one
from shared.finance.models import TRANSACTION_SOURCE_PAYLOAD_ALLOWLIST
from shared.finance.money import format_money
from shared.finance.platform_capture import (
    PlatformCapture,
    canonical_json,
    number_shape,
    shape_label,
    waybill_pattern_match,
)
from shared.finance.publication import LATEST_PUBLISHED_RUNS


class FinancePlatformCaptureMixin:
    """Platform capture storage over the repository's connection factory."""

    def store_platform_capture(
        self,
        capture: PlatformCapture,
        *,
        platform: str,
        source_role: str,
        account_id: str,
        field_mapping: Mapping[str, Any],
        request_profile: Mapping[str, Any],
        automation_id: str,
        plugin_version: str,
        generation: int,
        invocation_id: str | None,
    ) -> dict[str, Any]:
        capture_ref = str(uuid.uuid4())
        metrics = capture.metrics
        now = _now()
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO finance_platform_captures (
                        capture_ref, platform, source_role, account_id, site_code, site_name,
                        period_start, period_end, statement_item_count, statement_net,
                        billing_statement_item_count, billing_statement_net, detail_row_count,
                        detail_income, detail_expense, detail_net, field_mapping_json,
                        request_profile_json, content_sha256, automation_id, plugin_version,
                        generation, invocation_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        capture_ref, platform, source_role, account_id, capture.site_code, capture.site_name,
                        capture.period_start, capture.period_end, metrics["statement_item_count"],
                        metrics["statement_net"], metrics["billing_statement_item_count"],
                        metrics["billing_statement_net"], metrics["detail_row_count"],
                        metrics["detail_income"], metrics["detail_expense"], metrics["detail_net"],
                        canonical_json(field_mapping), canonical_json(request_profile),
                        capture.content_sha256, automation_id, plugin_version, int(generation),
                        invocation_id, now,
                    ),
                )
                capture_id = int(cursor.lastrowid)
                statements = [
                    (capture_id, row["basis"], row["row_no"], row["item_name"], row["site_code"],
                     row["amount"], canonical_json(row["raw"]))
                    for row in (*capture.statement_rows, *capture.billing_statement_rows)
                ]
                if statements:
                    cursor.executemany(
                        "INSERT INTO finance_platform_statement_rows "
                        "(capture_id, basis, row_no, item_name, site_code, amount, raw_json) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        statements,
                    )
                details = [
                    (capture_id, row["row_no"], row["source_key"], row["order_no"], row["waybill_no"],
                     row["item_name"], row["settled_at"], row["billed_at"], row["amount"],
                     row["before_balance"], row["after_balance"], row["source_type"], row["remark"],
                     row["customer_name"], row["goods_name"], canonical_json(row["raw"]))
                    for row in capture.detail_rows
                ]
                if details:
                    cursor.executemany(
                        "INSERT INTO finance_platform_detail_rows "
                        "(capture_id, row_no, source_key, order_no, waybill_no, item_name, settled_at, "
                        "billed_at, amount, before_balance, after_balance, source_type, remark, "
                        "customer_name, goods_name, raw_json) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        details,
                    )
                cursor.execute(
                    "SELECT COUNT(*) AS n FROM finance_platform_detail_rows WHERE capture_id = %s",
                    (capture_id,),
                )
                written_details = int((_one(cursor) or {}).get("n") or 0)
                cursor.execute(
                    "SELECT COUNT(*) AS n FROM finance_platform_statement_rows WHERE capture_id = %s",
                    (capture_id,),
                )
                written_statements = int((_one(cursor) or {}).get("n") or 0)
                if written_details != len(details) or written_statements != len(statements):
                    raise RuntimeError("platform capture readback count differs")
            finally:
                cursor.close()
        return {"capture_id": capture_id, "capture_ref": capture_ref,
                "detail_row_count": len(details), "statement_row_count": len(statements)}

    def _platform_capture(self, cursor: Any, capture_ref: str, *, for_update: bool = False) -> dict[str, Any]:
        cursor.execute(
            "SELECT * FROM finance_platform_captures WHERE capture_ref = %s" + (" FOR UPDATE" if for_update else ""),
            (str(capture_ref),),
        )
        row = _one(cursor)
        if row is None:
            raise LookupError("platform capture does not exist")
        return row

    def set_platform_capture_batch(self, capture_ref: str, batch_id: int) -> None:
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                capture = self._platform_capture(cursor, capture_ref, for_update=True)
                if capture.get("publish_batch_id") is not None:
                    raise RuntimeError("platform capture already has a ledger batch")
                cursor.execute(
                    "UPDATE finance_platform_captures SET publish_batch_id = %s WHERE id = %s",
                    (int(batch_id), int(capture["id"])),
                )
            finally:
                cursor.close()

    def platform_capture_day(self, capture_ref: str, business_date: dt.date) -> dict[str, Any]:
        """The capture header and the day's non-zero detail rows in balance order."""

        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                capture = self._platform_capture(cursor, capture_ref)
                cursor.execute(
                    """
                    SELECT row_no, source_key, order_no, waybill_no, item_name, settled_at, billed_at,
                           amount, before_balance, after_balance, remark, raw_json
                    FROM finance_platform_detail_rows
                    WHERE capture_id = %s AND settled_at >= %s AND settled_at < %s AND amount <> 0
                    ORDER BY settled_at, row_no
                    """,
                    (int(capture["id"]), business_date, business_date + dt.timedelta(days=1)),
                )
                rows = _all(cursor)
                cursor.execute(
                    "SELECT business_date FROM finance_platform_capture_days WHERE capture_id = %s AND business_date = %s",
                    (int(capture["id"]), business_date),
                )
                published = _one(cursor) is not None
            finally:
                cursor.close()
        for row in rows:
            raw = row.pop("raw_json")
            raw = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else dict(raw or {})
            row["source_payload"] = {key: raw[key] for key in sorted(TRANSACTION_SOURCE_PAYLOAD_ALLOWLIST)
                                     if key in raw and raw[key] is not None}
        return {"capture": capture, "rows": rows, "published": published}

    def record_platform_capture_day(
        self, capture_ref: str, business_date: dt.date, *, run_id: int, outcome: str, record_count: int
    ) -> None:
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                capture = self._platform_capture(cursor, capture_ref, for_update=True)
                cursor.execute(
                    "INSERT INTO finance_platform_capture_days "
                    "(capture_id, business_date, run_id, outcome, record_count, published_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (int(capture["id"]), business_date, int(run_id), outcome, int(record_count), _now()),
                )
            finally:
                cursor.close()

    def finish_platform_capture(self, capture_ref: str) -> dict[str, Any]:
        """Require every day of the period published; mark the capture published."""

        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                capture = self._platform_capture(cursor, capture_ref, for_update=True)
                if capture.get("published_at") is not None:
                    raise RuntimeError("platform capture is already published")
                cursor.execute(
                    "SELECT business_date, run_id, outcome, record_count FROM finance_platform_capture_days "
                    "WHERE capture_id = %s ORDER BY business_date",
                    (int(capture["id"]),),
                )
                days = _all(cursor)
                start, end = capture["period_start"], capture["period_end"]
                expected = {(start + dt.timedelta(days=offset)) for offset in range((end - start).days + 1)}
                published = {row["business_date"] for row in days}
                if published != expected:
                    raise RuntimeError("platform capture days are not all published")
                cursor.execute(
                    "SELECT COUNT(*) AS n FROM finance_platform_detail_rows WHERE capture_id = %s AND amount <> 0",
                    (int(capture["id"]),),
                )
                nonzero = int((_one(cursor) or {}).get("n") or 0)
                if nonzero != sum(int(row["record_count"]) for row in days):
                    raise RuntimeError("published day rows differ from the capture")
                cursor.execute(
                    "UPDATE finance_platform_captures SET published_at = %s WHERE id = %s",
                    (_now(), int(capture["id"])),
                )
            finally:
                cursor.close()
        return {"capture": capture, "days": days}

    # ------------------------------------------------------------------
    # Month workbench: the raw statements beside the ledger, read in the
    # caller's consistent snapshot. Nothing here classifies; the number
    # shapes only help the person who confirms each fee item.

    def _month_captures(self, cursor: Any, month: dt.date, end: dt.date) -> list[dict[str, Any]]:
        """The latest published full-month capture of each source."""

        cursor.execute(
            """
            SELECT c.* FROM finance_platform_captures c
            INNER JOIN (
              SELECT MAX(id) AS id FROM finance_platform_captures
              WHERE period_start = %s AND period_end = %s AND published_at IS NOT NULL
              GROUP BY platform, account_id
            ) latest ON latest.id = c.id
            ORDER BY c.site_name, c.id
            """,
            (month, end),
        )
        return _all(cursor)

    def _platform_month(
        self, cursor: Any, month: dt.date, end: dt.date, labels: Mapping[tuple[str, str], str]
    ) -> dict[str, Any]:
        captures = self._month_captures(cursor, month, end)
        if not captures:
            return {"sources": [], "items": [], "settlement_net": None, "billing_net": None}
        ids = [int(row["id"]) for row in captures]
        marks = ", ".join(["%s"] * len(ids))
        cursor.execute(
            f"SELECT capture_id, basis, item_name, amount FROM finance_platform_statement_rows "
            f"WHERE capture_id IN ({marks})",
            ids,
        )
        statements = _all(cursor)
        cursor.execute(
            f"SELECT capture_id, item_name, waybill_no, amount FROM finance_platform_detail_rows "
            f"WHERE capture_id IN ({marks})",
            ids,
        )
        details = _all(cursor)
        by_id = {int(row["id"]): row for row in captures}
        waybill_shapes = {
            capture_id: list(_json(row["request_profile_json"]).get("waybill_number_shapes") or [])
            for capture_id, row in by_id.items()
        }
        items: dict[str, dict[str, Any]] = {}

        def item(name: str) -> dict[str, Any]:
            return items.setdefault(name, {
                "settlement": Decimal(0), "billing": Decimal(0), "rows": 0, "zero_rows": 0,
                "sources": {}, "shapes": {}, "waybill_rows": 0, "other_rows": 0, "blank_rows": 0,
            })

        def source(entry: dict[str, Any], capture_id: int) -> dict[str, Any]:
            return entry["sources"].setdefault(capture_id, {
                "settlement": Decimal(0), "billing": Decimal(0), "rows": 0})

        for row in statements:
            entry = item(str(row["item_name"]))
            amount = Decimal(str(row["amount"]))
            key = "settlement" if row["basis"] == "settlement" else "billing"
            entry[key] += amount
            source(entry, int(row["capture_id"]))[key] += amount
        zero_rows: dict[int, int] = {}
        for row in details:
            capture_id = int(row["capture_id"])
            entry = item(str(row["item_name"]))
            entry["rows"] += 1
            source(entry, capture_id)["rows"] += 1
            if Decimal(str(row["amount"])) == 0:
                entry["zero_rows"] += 1
                zero_rows[capture_id] = zero_rows.get(capture_id, 0) + 1
            matched = waybill_pattern_match(row["waybill_no"], waybill_shapes[capture_id])
            shape = matched or number_shape(row["waybill_no"])
            is_waybill = matched is not None
            counted = entry["shapes"].setdefault(shape, {"rows": 0, "waybill": is_waybill})
            counted["rows"] += 1
            counted["waybill"] = counted["waybill"] or is_waybill
            entry["waybill_rows" if is_waybill else "blank_rows" if not shape else "other_rows"] += 1

        sources = []
        for row in captures:
            capture_id = int(row["id"])
            sources.append({
                "capture_key": str(row["capture_ref"]),
                "source": self._label(labels, str(row["platform"]), str(row["account_id"])),
                "site_name": str(row["site_name"]), "site_code": str(row["site_code"]),
                "captured_at": _date_text(row["created_at"]), "published_at": _date_text(row["published_at"]),
                "plugin_version": str(row["plugin_version"]),
                "statement_item_count": int(row["statement_item_count"]),
                "settlement_net": format_money(row["statement_net"]),
                "billing_statement_item_count": row["billing_statement_item_count"],
                "billing_net": (format_money(row["billing_statement_net"])
                                if row["billing_statement_net"] is not None else None),
                "detail_row_count": int(row["detail_row_count"]),
                "zero_row_count": zero_rows.get(capture_id, 0),
                "waybill_number_shapes": [shape_label(shape) for shape in waybill_shapes[capture_id]],
            })
        result = []
        for name, entry in sorted(items.items(), key=lambda pair: (pair[1]["settlement"], pair[0])):
            result.append({
                "item_name": name,
                "settlement_amount": format_money(entry["settlement"]),
                "billing_amount": format_money(entry["billing"]),
                "detail_rows": entry["rows"],
                "zero_rows": entry["zero_rows"],
                "zero_only": entry["rows"] > 0 and entry["rows"] == entry["zero_rows"],
                "sources": [
                    {"capture_key": str(by_id[capture_id]["capture_ref"]),
                     "site_name": str(by_id[capture_id]["site_name"]),
                     "settlement_amount": format_money(values["settlement"]),
                     "billing_amount": format_money(values["billing"]), "rows": values["rows"]}
                    for capture_id, values in sorted(entry["sources"].items())
                ],
                "number_shapes": [
                    {"label": shape_label(shape), "rows": values["rows"], "waybill": values["waybill"]}
                    for shape, values in sorted(entry["shapes"].items(), key=lambda pair: -pair[1]["rows"])
                ],
                "suggestion": _level_suggestion(entry),
            })
        return {
            "sources": sources,
            "items": result,
            "settlement_net": format_money(sum((Decimal(str(row["statement_net"])) for row in captures), Decimal(0))),
            "billing_net": format_money(sum(
                (Decimal(str(row["billing_statement_net"] or 0)) for row in captures), Decimal(0))),
        }

    def _platform_capture_check(
        self, cursor: Any, month: dt.date, end: dt.date, labels: Mapping[tuple[str, str], str]
    ) -> dict[str, Any]:
        """Every day in the ledger must come from one verified full-month capture."""

        enabled_clause, enabled_params = _enabled_platform_clause("r.platform")
        cursor.execute(
            f"""
            SELECT r.platform, r.account_id, r.target_date, c.id AS capture_id
            FROM finance_sync_runs r
            INNER JOIN ({LATEST_PUBLISHED_RUNS}) latest ON latest.latest_run_id = r.id
            LEFT JOIN finance_platform_capture_days d ON d.run_id = r.id
            LEFT JOIN finance_platform_captures c ON c.id = d.capture_id
              AND c.period_start = %s AND c.period_end = %s AND c.published_at IS NOT NULL
            WHERE r.target_date BETWEEN %s AND %s AND {enabled_clause}
            """,
            (month, end, month, end, *enabled_params),
        )
        by_source: dict[tuple[str, str], dict[str, Any]] = {}
        for row in _all(cursor):
            entry = by_source.setdefault((str(row["platform"]), str(row["account_id"])),
                                         {"captures": set(), "other": []})
            if row["capture_id"] is None:
                entry["other"].append(_as_date(row["target_date"]).isoformat())
            else:
                entry["captures"].add(int(row["capture_id"]))
        issues = []
        for (platform, account_id), entry in sorted(by_source.items()):
            label = self._label(labels, platform, account_id)
            if entry["other"]:
                issues.append({"source": label, "reason": "这些日期的账本不是来自整月统计抓取",
                               "dates": sorted(entry["other"])})
            if len(entry["captures"]) > 1:
                issues.append({"source": label, "reason": "本月账本混用了多次抓取，请重新拉取该月", "dates": []})
        return {
            "code": "platform_capture",
            "label": "账本来自整月统计抓取",
            "passed": bool(by_source) and not issues,
            "summary": (
                "每个来源本月每天的账本都来自同一次已核对的统计查询 + 明细抓取。"
                if by_source and not issues
                else "本月还没有账本数据。" if not by_source
                else "部分账本来自旧的逐日同步或多次抓取，请在“自动化 → 财务”重新拉取该月。"
            ),
            "issues": issues,
        }

    def platform_month_details(
        self, month: Any, *, capture_key: str, item_name: str, basis: str, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """Raw detail rows of one item of one source, on either date basis."""

        from shared.finance.periods import parse_period_month, period_month_end

        period_month = parse_period_month(month)
        end = period_month_end(period_month)
        if basis not in {"settlement", "billing"}:
            raise ValueError("basis must be settlement or billing")
        offset, limit = max(0, int(offset)), min(max(1, int(limit)), 200)
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                capture = self._platform_capture(cursor, capture_key)
                if (_as_date(capture["period_start"]) != period_month or _as_date(capture["period_end"]) != end
                        or capture["published_at"] is None):
                    raise LookupError("capture does not belong to this month")
                if basis == "settlement":
                    capture_ids = [int(capture["id"])]
                    where, params = "capture_id = %s", [int(capture["id"])]
                else:
                    # Billing dates of this month settle in this or a neighbouring
                    # month; read the latest capture of every period of the source.
                    cursor.execute(
                        """
                        SELECT MAX(id) AS id, period_start FROM finance_platform_captures
                        WHERE platform = %s AND account_id = %s AND published_at IS NOT NULL
                          AND period_start <= %s AND period_end >= %s
                        GROUP BY period_start, period_end ORDER BY period_start
                        """,
                        (capture["platform"], capture["account_id"], end + dt.timedelta(days=62),
                         period_month - dt.timedelta(days=62)),
                    )
                    periods = _all(cursor)
                    capture_ids = [int(row["id"]) for row in periods]
                    marks = ", ".join(["%s"] * len(capture_ids))
                    where = f"capture_id IN ({marks}) AND billed_at >= %s AND billed_at < %s"
                    params = [*capture_ids, period_month, end + dt.timedelta(days=1)]
                cursor.execute(
                    f"SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount FROM finance_platform_detail_rows "
                    f"WHERE {where} AND item_name = %s",
                    (*params, item_name),
                )
                totals = _one(cursor) or {}
                cursor.execute(
                    f"SELECT capture_id, row_no, order_no, waybill_no, settled_at, billed_at, amount, before_balance, "
                    f"after_balance, source_type, remark, customer_name, goods_name, raw_json "
                    f"FROM finance_platform_detail_rows WHERE {where} AND item_name = %s "
                    f"ORDER BY settled_at, row_no LIMIT %s OFFSET %s",
                    (*params, item_name, limit, offset),
                )
                rows = _all(cursor)
                cursor.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS amount, COUNT(*) AS n FROM finance_platform_statement_rows "
                    "WHERE capture_id = %s AND basis = %s AND item_name = %s",
                    (int(capture["id"]), basis, item_name),
                )
                statement = _one(cursor) or {}
            finally:
                cursor.close()
        shapes = list(_json(capture["request_profile_json"]).get("waybill_number_shapes") or [])
        detail_amount = Decimal(str(totals.get("amount") or 0))
        statement_amount = Decimal(str(statement.get("amount") or 0))
        has_statement = basis == "settlement" or capture["billing_statement_net"] is not None
        return {
            "month": period_month.strftime("%Y-%m"), "basis": basis, "item_name": item_name,
            "site_name": str(capture["site_name"]),
            "total": int(totals.get("n") or 0), "offset": offset, "limit": limit,
            "detail_amount": format_money(detail_amount),
            "statement_amount": format_money(statement_amount) if has_statement else None,
            # On the billing basis the neighbouring month may not be captured yet.
            "complete": has_statement and detail_amount == statement_amount,
            "capture_months": len(capture_ids),
            "rows": [
                {
                    "settled_at": _date_text(row["settled_at"]), "billed_at": _date_text(row["billed_at"]),
                    "waybill_no": row["waybill_no"],
                    "number_shape": shape_label(waybill_pattern_match(row["waybill_no"], shapes)
                                                or number_shape(row["waybill_no"])),
                    "is_waybill_number": waybill_pattern_match(row["waybill_no"], shapes) is not None,
                    "order_no": row["order_no"], "amount": format_money(row["amount"]),
                    "before_balance": (format_money(row["before_balance"])
                                       if row["before_balance"] is not None else None),
                    "after_balance": (format_money(row["after_balance"])
                                      if row["after_balance"] is not None else None),
                    "customer_name": row["customer_name"], "goods_name": row["goods_name"],
                    "source_type": row["source_type"], "remark": row["remark"], "raw": _json(row["raw_json"]),
                }
                for row in rows
            ],
        }


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return dict(value or {})


def _date_text(value: Any) -> str | None:
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return None if value is None else str(value)


def _as_date(value: Any) -> dt.date:
    return value.date() if isinstance(value, dt.datetime) else value if isinstance(value, dt.date) \
        else dt.date.fromisoformat(str(value)[:10])


def _level_suggestion(entry: Mapping[str, Any]) -> dict[str, str]:
    """User rule (2026-10-23): rows carrying waybill numbers are waybill-level;
    rows with only serial or other numbers are operating costs."""

    waybill, other, blank = entry["waybill_rows"], entry["other_rows"], entry["blank_rows"]
    if waybill and not other and not blank:
        return {"level": "waybill", "label": "建议运单级", "reason": f"{waybill} 行单号都是运单号格式"}
    if not waybill:
        return {"level": "operating", "label": "建议运营级", "reason": "没有运单号格式的单号，只有流水号或其他单号"}
    return {"level": "mixed", "label": "需人工判断",
            "reason": f"{waybill} 行是运单号格式，{other + blank} 行是其他单号或无单号"}
