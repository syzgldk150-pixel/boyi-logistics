"""Monthly fee-item inventory, period close and reopen for the finance ledger.

A month may be closed only when every enabled source has a verified run for
each day taken after its routine rescan window, the account balance chain
continues across days, and every fee item seen in the month is bound and
confirmed against the mapping version in effect.  Closing stores an immutable
snapshot version; reopening keeps it and the next close adds a new version.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from shared.finance.evolution import (
    _EFFECTIVE_MAPPING_JOIN,
    _all,
    _enabled_platform_clause,
    _fingerprint,
    _json,
    _now,
    _one,
)
from shared.finance.models import FinanceQuery
from shared.finance.money import ZERO, format_money
from shared.finance.publication import LATEST_PUBLISHED_RUNS, VISIBLE_TRANSACTION_JOIN
from shared.finance.sources import enabled_finance_platforms


PERIOD_OPEN = "open"
PERIOD_CLOSED = "closed"
PERIOD_REOPENED = "reopened"
# The routine sync rescans the previous seven business days, so a day's data
# is final for closing once a run started on or after that day plus seven days.
RESCAN_WINDOW_DAYS = 7
# Warnings that do not question a day's completeness: new names go through
# item confirmation, and a revision means the latest run replaced older data.
ACCEPTED_WARNING_CODES = frozenset({"NEW_FEE_ITEM", "HISTORICAL_REVISION"})
MAX_CONFIRM_ITEMS = 500
ACKNOWLEDGEABLE_ANOMALIES = frozenset({"MISSING_WAYBILL"})
_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_LEVEL_LABELS = {"waybill": "运单级", "operating": "运营级", "unbound": "待绑定"}


class FinancePeriodError(RuntimeError):
    """A period operation was refused; ``code`` is stable for callers."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def parse_period_month(value: Any) -> dt.date:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", text):
        raise FinancePeriodError("INVALID_MONTH", "月份必须使用 YYYY-MM 格式。")
    try:
        return dt.date(int(text[:4]), int(text[5:7]), 1)
    except ValueError as exc:
        raise FinancePeriodError("INVALID_MONTH", "月份必须使用 YYYY-MM 格式。") from exc


def period_month_end(month: dt.date) -> dt.date:
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def _previous_month(month: dt.date) -> dt.date:
    return (month - dt.timedelta(days=1)).replace(day=1)


def _request_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _UUID_PATTERN.fullmatch(text):
        raise FinancePeriodError("INVALID_REQUEST", "请求标识无效，请刷新页面后重试。")
    return text


def _actor(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128:
        raise FinancePeriodError("INVALID_REQUEST", "无法识别当前操作人，请重新登录后重试。")
    return text


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _money(value: Any) -> str:
    return format_money(value if value is not None else ZERO, missing_as_zero=True)


def _storage_text(value: Any) -> str:
    return f"{_decimal(value):.4f}"


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _warning_codes(report: Any) -> set[str]:
    if isinstance(report, (bytes, bytearray)):
        report = report.decode("utf-8")
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except ValueError:
            return {"UNREADABLE_REPORT"}
    if not isinstance(report, Mapping):
        return set()
    return {
        str(item.get("code") or "UNKNOWN")
        for item in report.get("warnings") or []
        if isinstance(item, Mapping)
    }


def _day_is_verified(run: Mapping[str, Any]) -> bool:
    status = str(run.get("validation_status") or "")
    if status == "passed":
        return True
    if status == "warning":
        return _warning_codes(run.get("validation_report_json")) <= ACCEPTED_WARNING_CODES
    return False


def _dates(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def _item_key(row: Mapping[str, Any]) -> tuple[int, str]:
    return int(row["fee_item_id"]), str(row["direction"])


class FinancePeriodMixin:
    """Period inventory and close over the repository's connection factory."""

    _PERIOD_ROW_SQL = (
        "SELECT period_month, status, current_close_id, updated_by, updated_at "
        "FROM finance_periods WHERE period_month = %s"
    )

    # ------------------------------------------------------------------ reads

    def get_period_view(self, month: Any, *, today: dt.date) -> dict[str, Any]:
        period_month = parse_period_month(month)
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                return self._period_view(cursor, period_month, today=today)
            finally:
                cursor.close()

    def _period_view(self, cursor: Any, month: dt.date, *, today: dt.date) -> dict[str, Any]:
        end = period_month_end(month)
        cursor.execute(self._PERIOD_ROW_SQL, (month,))
        period = _one(cursor) or {}
        status = str(period.get("status") or PERIOD_OPEN)
        closes = self._period_closes(cursor, month)
        current_close = next(
            (row for row in closes if int(row["id"]) == int(period.get("current_close_id") or 0)),
            None,
        )
        labels = self._source_labels(cursor)
        if status == PERIOD_CLOSED and current_close is not None:
            rows = self._snapshot_rows(cursor, int(current_close["id"]))
            checks = json.loads(current_close["check_report_json"]) if isinstance(
                current_close["check_report_json"], str
            ) else (current_close["check_report_json"] or [])
            earliest_close_date = None
        else:
            rows = self._live_rows(cursor, month, end)
            checks, earliest_close_date = self._period_checks(cursor, month, today=today, rows=rows, labels=labels)
        reviews = self._period_reviews(cursor, month)
        previous = self._month_rows(cursor, _previous_month(month))
        items = self._items(rows, reviews, previous, closed=status == PERIOD_CLOSED)
        platform = self._platform_month(cursor, month, end, labels)
        captured = {entry["item_name"]: entry for entry in platform["items"]}
        capture_platforms = {"ronghui"} if platform["sources"] else set()
        for item in items:
            entry = captured.get(str(item["primary_fee_name"])) if (
                item["platform"] in capture_platforms and not item["secondary_fee_name"]) else None
            item["platform_capture"] = None if entry is None else {
                key: entry[key] for key in ("settlement_amount", "billing_amount", "detail_rows", "suggestion")
            }
        comparison = self._comparison(cursor, rows, closes, status=status, current_close=current_close)
        return {
            "month": month.strftime("%Y-%m"),
            "start_date": month.isoformat(),
            "end_date": end.isoformat(),
            "status": status,
            "data_source": "snapshot" if status == PERIOD_CLOSED and current_close else "live",
            "current_version": int(current_close["version_no"]) if current_close else None,
            "closes": [self._close_summary(row) for row in closes],
            "items": items,
            "report": self._report(rows, labels),
            "checks": checks,
            "can_close": status != PERIOD_CLOSED and all(check.get("passed") for check in checks),
            "earliest_close_date": earliest_close_date.isoformat() if earliest_close_date else None,
            "comparison": comparison,
            "drift": self._open_drift(cursor, month, end, labels),
            "platform": platform,
            "catalog_note": "费用项目目录 = 本月流水中实际出现的项目；融辉暂无已核实的独立项目字典接口。",
        }

    def _period_closes(self, cursor: Any, month: dt.date) -> list[dict[str, Any]]:
        cursor.execute(
            """
            SELECT id, period_month, version_no, status, line_count, transaction_count,
                   total_income, total_expense, net_change, content_sha256, check_report_json,
                   closed_by, closed_at, reopened_by, reopened_at, reopen_reason
            FROM finance_period_closes WHERE period_month = %s
            ORDER BY version_no DESC
            """,
            (month,),
        )
        return _all(cursor)

    @staticmethod
    def _close_summary(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "version": int(row["version_no"]),
            "status": str(row["status"]),
            "line_count": int(row["line_count"]),
            "transaction_count": int(row["transaction_count"]),
            "total_income": _money(row["total_income"]),
            "total_expense": _money(row["total_expense"]),
            "net_change": _money(row["net_change"]),
            "content_sha256": str(row["content_sha256"]),
            "closed_by": str(row["closed_by"]),
            "closed_at": _date_text(row["closed_at"]),
            "reopened_by": row.get("reopened_by"),
            "reopened_at": _date_text(row.get("reopened_at")),
            "reopen_reason": row.get("reopen_reason"),
        }

    def _source_labels(self, cursor: Any) -> dict[tuple[str, str], str]:
        cursor.execute(
            """
            SELECT a.provider AS platform, a.account_id, a.source_id, s.display_name
            FROM module_data_source_accounts a
            LEFT JOIN module_data_sources s ON s.source_id = a.source_id
            WHERE a.module = 'finance'
            """,
            (),
        )
        labels: dict[tuple[str, str], str] = {}
        for row in _all(cursor):
            label = str(row.get("display_name") or row.get("account_id") or "")
            labels[(str(row["platform"]), "account:" + str(row["account_id"]))] = label
            labels[(str(row["platform"]), str(row["source_id"]))] = label
        return labels

    @staticmethod
    def _label(labels: Mapping[tuple[str, str], str], platform: str, account_id: str) -> str:
        return labels.get((platform, "account:" + account_id), account_id)

    _ROW_FIELDS = """
        t.platform, t.account_id, t.fee_item_id, t.direction,
        fm.id AS mapping_id, fm.fee_level, fm.canonical_subject_id, fm.include_in_cost
    """

    def _live_rows(self, cursor: Any, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        enabled_clause, enabled_params = _enabled_platform_clause("t.platform")
        cursor.execute(
            f"""
            SELECT {self._ROW_FIELDS},
                   COUNT(*) AS transaction_count,
                   COALESCE(SUM(t.income), 0) AS income,
                   COALESCE(SUM(t.expense), 0) AS expense,
                   COALESCE(SUM(t.income - t.expense), 0) AS net_change
            FROM finance_transactions t
            {VISIBLE_TRANSACTION_JOIN}
            {_EFFECTIVE_MAPPING_JOIN}
            WHERE t.business_date BETWEEN %s AND %s AND {enabled_clause}
            GROUP BY t.platform, t.account_id, t.fee_item_id, t.direction,
                     fm.id, fm.fee_level, fm.canonical_subject_id, fm.include_in_cost
            """,
            (start, end, *enabled_params),
        )
        return self._with_item_details(cursor, _all(cursor))

    def _snapshot_rows(self, cursor: Any, close_id: int) -> list[dict[str, Any]]:
        cursor.execute(
            """
            SELECT platform, account_id, fee_item_id, direction, mapping_id, fee_level,
                   canonical_subject_id, include_in_cost,
                   SUM(transaction_count) AS transaction_count, SUM(income) AS income,
                   SUM(expense) AS expense, SUM(net_change) AS net_change
            FROM finance_period_close_lines WHERE close_id = %s
            GROUP BY platform, account_id, fee_item_id, direction, mapping_id, fee_level,
                     canonical_subject_id, include_in_cost
            """,
            (int(close_id),),
        )
        return self._with_item_details(cursor, _all(cursor))

    def _with_item_details(self, cursor: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach names, subjects and mapping versions with two bounded reads."""

        fee_ids = sorted({int(row["fee_item_id"]) for row in rows})
        mapping_ids = sorted({int(row["mapping_id"]) for row in rows if row.get("mapping_id")})
        names: dict[int, dict[str, Any]] = {}
        mappings: dict[int, dict[str, Any]] = {}
        if fee_ids:
            cursor.execute(
                "SELECT id, platform, raw_primary_fee_name, raw_secondary_fee_name, direction "
                f"FROM finance_fee_items WHERE id IN ({', '.join(['%s'] * len(fee_ids))})",
                tuple(fee_ids),
            )
            names = {int(row["id"]): row for row in _all(cursor)}
        if mapping_ids:
            cursor.execute(
                "SELECT fm.id, fm.version_no, fm.effective_start_month, fm.requires_waybill, "
                "fs.subject_name FROM finance_fee_mappings fm "
                "LEFT JOIN finance_fee_subjects fs ON fs.id = fm.canonical_subject_id "
                f"WHERE fm.id IN ({', '.join(['%s'] * len(mapping_ids))})",
                tuple(mapping_ids),
            )
            mappings = {int(row["id"]): row for row in _all(cursor)}
        for row in rows:
            name = names.get(int(row["fee_item_id"])) or {}
            mapping = mappings.get(int(row["mapping_id"])) if row.get("mapping_id") else None
            row["primary_fee_name"] = str(name.get("raw_primary_fee_name") or "")
            row["secondary_fee_name"] = str(name.get("raw_secondary_fee_name") or "")
            row["subject_name"] = str((mapping or {}).get("subject_name") or "") or None
            row["version_no"] = int(mapping["version_no"]) if mapping else None
            row["effective_start_month"] = (
                _as_date(mapping["effective_start_month"]).strftime("%Y-%m") if mapping else None
            )
        return rows

    def _month_rows(self, cursor: Any, month: dt.date) -> list[dict[str, Any]]:
        """A month's rows as reported: the close snapshot once closed, else live."""

        cursor.execute(self._PERIOD_ROW_SQL, (month,))
        period = _one(cursor) or {}
        if str(period.get("status") or "") == PERIOD_CLOSED and period.get("current_close_id"):
            return self._snapshot_rows(cursor, int(period["current_close_id"]))
        return self._live_rows(cursor, month, period_month_end(month))

    def _period_reviews(self, cursor: Any, month: dt.date) -> dict[int, dict[str, Any]]:
        cursor.execute(
            "SELECT fee_item_id, mapping_id, confirmed_by, confirmed_at "
            "FROM finance_period_item_reviews WHERE period_month = %s",
            (month,),
        )
        return {int(row["fee_item_id"]): row for row in _all(cursor)}

    @staticmethod
    def _is_bound(row: Mapping[str, Any]) -> bool:
        return bool(row.get("mapping_id")) and bool(row.get("canonical_subject_id"))

    def _items(
        self,
        rows: Sequence[Mapping[str, Any]],
        reviews: Mapping[int, Mapping[str, Any]],
        previous: Sequence[Mapping[str, Any]],
        *,
        closed: bool,
    ) -> list[dict[str, Any]]:
        previous_net: dict[tuple[int, str], Decimal] = {}
        for row in previous:
            key = _item_key(row)
            previous_net[key] = previous_net.get(key, ZERO) + _decimal(row["net_change"])
        grouped: dict[tuple[int, str], dict[str, Any]] = {}
        for row in rows:
            key = _item_key(row)
            item = grouped.get(key)
            if item is None:
                item = grouped[key] = {"row": row, "count": 0, "income": ZERO, "expense": ZERO, "net": ZERO, "accounts": set()}
            elif item["row"].get("mapping_id") != row.get("mapping_id"):
                raise FinancePeriodError("MAPPING_AMBIGUOUS", "同一费用项目在本月出现了多个生效绑定，请联系维护人员。")
            item["count"] += int(row["transaction_count"])
            item["income"] += _decimal(row["income"])
            item["expense"] += _decimal(row["expense"])
            item["net"] += _decimal(row["net_change"])
            item["accounts"].add(str(row["account_id"]))
        result = []
        for key, item in grouped.items():
            row = item["row"]
            review = reviews.get(key[0])
            bound = self._is_bound(row)
            if not bound:
                review_status = "unbound"
            elif review is None:
                review_status = "pending"
            elif int(review["mapping_id"]) == int(row["mapping_id"]):
                review_status = "confirmed"
            else:
                review_status = "stale"
            prior = previous_net.get(key)
            result.append({
                "fee_item_id": key[0],
                "platform": str(row["platform"]),
                "primary_fee_name": row["primary_fee_name"],
                "secondary_fee_name": row["secondary_fee_name"],
                "direction": key[1],
                "transaction_count": item["count"],
                "income": _money(item["income"]),
                "expense": _money(item["expense"]),
                "net_change": _money(item["net"]),
                "previous_net_change": _money(prior) if prior is not None else None,
                "net_change_delta": _money(item["net"] - prior) if prior is not None else None,
                "first_seen_this_month": prior is None,
                "account_count": len(item["accounts"]),
                "mapping": {
                    "mapping_id": int(row["mapping_id"]),
                    "fee_level": row.get("fee_level"),
                    "fee_level_label": _LEVEL_LABELS.get(str(row.get("fee_level") or ""), ""),
                    "subject_name": row.get("subject_name"),
                    "include_in_cost": bool(row.get("include_in_cost")),
                    "version_no": row.get("version_no"),
                    "effective_start_month": row.get("effective_start_month"),
                } if bound else None,
                "review_status": review_status,
                "confirmed_by": review.get("confirmed_by") if review else None,
                "confirmed_at": _date_text(review.get("confirmed_at")) if review else None,
                "closed": closed,
            })
        order = {"unbound": 0, "stale": 1, "pending": 2, "confirmed": 3}
        result.sort(key=lambda item: (order[item["review_status"]], -abs(Decimal(item["net_change"])), item["fee_item_id"]))
        return result

    def _report(
        self,
        rows: Sequence[Mapping[str, Any]],
        labels: Mapping[tuple[str, str], str],
    ) -> dict[str, Any]:
        totals = {"income": ZERO, "expense": ZERO, "net": ZERO, "count": 0, "waybill_cost": ZERO, "operating_cost": ZERO}
        levels = {name: {"income": ZERO, "expense": ZERO, "net": ZERO, "count": 0} for name in _LEVEL_LABELS}
        accounts: dict[tuple[str, str], dict[str, Any]] = {}
        subjects: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            income, expense, net = _decimal(row["income"]), _decimal(row["expense"]), _decimal(row["net_change"])
            count = int(row["transaction_count"])
            level = str(row.get("fee_level") or "") if self._is_bound(row) else "unbound"
            level = level if level in levels else "unbound"
            cost = expense if row.get("include_in_cost") and level != "unbound" else ZERO
            account = accounts.setdefault(
                (str(row["platform"]), str(row["account_id"])),
                {"income": ZERO, "expense": ZERO, "net": ZERO, "count": 0, "waybill_cost": ZERO, "operating_cost": ZERO},
            )
            for bucket in (totals, levels[level], account):
                bucket["income"] += income
                bucket["expense"] += expense
                bucket["net"] += net
                bucket["count"] += count
            if level in {"waybill", "operating"}:
                totals[f"{level}_cost"] += cost
                account[f"{level}_cost"] += cost
                subject = subjects.setdefault(
                    (level, str(row.get("subject_name") or "")),
                    {"income": ZERO, "expense": ZERO, "net": ZERO, "count": 0},
                )
                subject["income"] += income
                subject["expense"] += expense
                subject["net"] += net
                subject["count"] += count
        return {
            "transaction_count": totals["count"],
            "total_income": _money(totals["income"]),
            "total_expense": _money(totals["expense"]),
            "net_change": _money(totals["net"]),
            "waybill_cost": _money(totals["waybill_cost"]),
            "operating_cost": _money(totals["operating_cost"]),
            "levels": [
                {"level": name, "label": label, "transaction_count": levels[name]["count"],
                 "income": _money(levels[name]["income"]), "expense": _money(levels[name]["expense"]),
                 "net_change": _money(levels[name]["net"])}
                for name, label in _LEVEL_LABELS.items()
            ],
            "accounts": [
                {"platform": key[0], "account_id": key[1], "label": self._label(labels, key[0], key[1]),
                 "transaction_count": value["count"], "income": _money(value["income"]),
                 "expense": _money(value["expense"]), "net_change": _money(value["net"]),
                 "waybill_cost": _money(value["waybill_cost"]), "operating_cost": _money(value["operating_cost"])}
                for key, value in sorted(accounts.items())
            ],
            "subjects": [
                {"fee_level": key[0], "fee_level_label": _LEVEL_LABELS[key[0]], "subject_name": key[1],
                 "transaction_count": value["count"], "income": _money(value["income"]),
                 "expense": _money(value["expense"]), "net_change": _money(value["net"])}
                for key, value in sorted(subjects.items(), key=lambda pair: (-abs(pair[1]["net"]), pair[0]))
            ],
        }

    def _comparison(
        self,
        cursor: Any,
        rows: Sequence[Mapping[str, Any]],
        closes: Sequence[Mapping[str, Any]],
        *,
        status: str,
        current_close: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Compare the shown data with the previous close version, item by item."""

        if status == PERIOD_CLOSED and current_close is not None:
            base = next((row for row in closes if int(row["version_no"]) < int(current_close["version_no"])), None)
        else:
            base = closes[0] if closes else None
        if base is None:
            return None
        base_rows = self._snapshot_rows(cursor, int(base["id"]))

        def totals(source: Sequence[Mapping[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
            result: dict[tuple[int, str], dict[str, Any]] = {}
            for row in source:
                item = result.setdefault(_item_key(row), {
                    "count": 0, "net": ZERO, "mapping_id": row.get("mapping_id"),
                    "primary_fee_name": row.get("primary_fee_name"), "secondary_fee_name": row.get("secondary_fee_name"),
                })
                item["count"] += int(row["transaction_count"])
                item["net"] += _decimal(row["net_change"])
            return result

        current, previous = totals(rows), totals(base_rows)
        differences = []
        for key in sorted(set(current) | set(previous)):
            now_item, old_item = current.get(key), previous.get(key)
            now_net = now_item["net"] if now_item else ZERO
            old_net = old_item["net"] if old_item else ZERO
            if (
                now_item and old_item
                and now_item["count"] == old_item["count"] and now_net == old_net
                and now_item["mapping_id"] == old_item["mapping_id"]
            ):
                continue
            named = now_item or old_item
            differences.append({
                "fee_item_id": key[0],
                "direction": key[1],
                "primary_fee_name": named["primary_fee_name"],
                "secondary_fee_name": named["secondary_fee_name"],
                "base_transaction_count": old_item["count"] if old_item else 0,
                "transaction_count": now_item["count"] if now_item else 0,
                "base_net_change": _money(old_net),
                "net_change": _money(now_net),
                "delta": _money(now_net - old_net),
                "mapping_changed": bool(now_item and old_item and now_item["mapping_id"] != old_item["mapping_id"]),
            })
        base_net = sum((item["net"] for item in previous.values()), ZERO)
        now_net = sum((item["net"] for item in current.values()), ZERO)
        return {
            "base_version": int(base["version_no"]),
            "base_net_change": _money(base_net),
            "net_change": _money(now_net),
            "delta": _money(now_net - base_net),
            "differences": differences,
        }

    def _open_drift(
        self, cursor: Any, start: dt.date, end: dt.date, labels: Mapping[tuple[str, str], str]
    ) -> list[dict[str, Any]]:
        cursor.execute(
            """
            SELECT id, platform, account_id, business_date, occurrence_count, amount, details_json
            FROM finance_anomalies
            WHERE anomaly_type = 'CLOSED_PERIOD_DRIFT' AND status = 'open'
              AND business_date BETWEEN %s AND %s
            ORDER BY business_date, platform, account_id
            """,
            (start, end),
        )
        return [
            {"anomaly_id": int(row["id"]), "business_date": _date_text(row["business_date"]),
             "account_label": self._label(labels, str(row["platform"]), str(row["account_id"])),
             "changed_items": int(row["occurrence_count"]), "delta": _money(row["amount"])}
            for row in _all(cursor)
        ]

    # ----------------------------------------------------------------- checks

    def _period_checks(
        self,
        cursor: Any,
        month: dt.date,
        *,
        today: dt.date,
        rows: Sequence[Mapping[str, Any]],
        labels: Mapping[tuple[str, str], str] | None = None,
    ) -> tuple[list[dict[str, Any]], dt.date | None]:
        end = period_month_end(month)
        labels = self._source_labels(cursor) if labels is None else labels
        checks = [{
            "code": "month_finished",
            "label": "月份已结束",
            "passed": end < today,
            "summary": "本月已结束。" if end < today else "本月尚未结束，月末之后才能封账。",
            "issues": [],
        }]
        coverage, rescan, earliest = self._coverage_checks(cursor, month, end, labels)
        checks.extend([coverage, rescan, self._balance_chain_check(cursor, month, end, labels),
                       self._platform_capture_check(cursor, month, end, labels)])
        checks.extend(self._item_checks(cursor, month, end, rows))
        return checks, earliest

    def _coverage_checks(
        self,
        cursor: Any,
        start: dt.date,
        end: dt.date,
        labels: Mapping[tuple[str, str], str],
    ) -> tuple[dict[str, Any], dict[str, Any], dt.date | None]:
        enabled = set(enabled_finance_platforms())
        expected = {
            source for source in self._expected_finance_sources(cursor, FinanceQuery(start_date=start, end_date=end))
            if source[0] in enabled
        }
        enabled_clause, enabled_params = _enabled_platform_clause("r.platform")
        cursor.execute(
            f"""
            SELECT r.platform, r.account_id, r.target_date, r.status, r.validation_status,
                   r.validation_report_json, r.started_at,
                   COALESCE(source_alias.source_id, CONCAT('legacy-account:', r.account_id)) AS source_identity
            FROM finance_sync_runs r
            INNER JOIN ({LATEST_PUBLISHED_RUNS}) latest ON latest.latest_run_id = r.id
            LEFT JOIN module_data_source_accounts source_alias ON source_alias.module = 'finance'
              AND BINARY source_alias.provider = BINARY r.platform
              AND BINARY source_alias.account_id = BINARY r.account_id
            WHERE r.target_date BETWEEN %s AND %s AND {enabled_clause}
            """,
            (start, end, *enabled_params),
        )
        runs: dict[tuple[str, str, dt.date], list[dict[str, Any]]] = {}
        for row in _all(cursor):
            key = (str(row["platform"]), str(row["source_identity"]), _as_date(row["target_date"]))
            runs.setdefault(key, []).append(row)
        coverage_issues: list[dict[str, Any]] = []
        rescan_issues: list[dict[str, Any]] = []
        earliest: dt.date | None = None
        for platform, identity in sorted(expected | {(key[0], key[1]) for key in runs}):
            label = labels.get((platform, identity)) or identity.removeprefix("legacy-account:")
            if (platform, identity) not in expected:
                coverage_issues.append({"source": label, "reason": "该来源不在当前财务数据源中", "dates": []})
                continue
            missing, unverified, ambiguous, early = [], [], [], []
            for day in _dates(start, end):
                day_runs = runs.get((platform, identity, day)) or []
                if not day_runs:
                    missing.append(day.isoformat())
                    continue
                if len(day_runs) > 1:
                    ambiguous.append(day.isoformat())
                    continue
                run = day_runs[0]
                if not _day_is_verified(run):
                    unverified.append(day.isoformat())
                final_after = dt.datetime.combine(day + dt.timedelta(days=RESCAN_WINDOW_DAYS), dt.time.min)
                started = run.get("started_at")
                if not isinstance(started, dt.datetime) or started < final_after:
                    early.append(day.isoformat())
                    ready = final_after.date()
                    earliest = ready if earliest is None or ready > earliest else earliest
            for reason, dates in (
                ("缺少同步数据", missing),
                ("校验未通过或有需处理的校验提示", unverified),
                ("同一来源同一天有多条最新运行", ambiguous),
            ):
                if dates:
                    coverage_issues.append({"source": label, "reason": reason, "dates": dates})
            if early:
                rescan_issues.append({"source": label, "reason": "最新数据取得于例行回扫完成之前", "dates": early})
        day_count = (end - start).days + 1
        coverage = {
            "code": "coverage",
            "label": "数据覆盖完整",
            "passed": bool(expected) and not coverage_issues,
            "summary": (
                f"{len(expected)} 个来源 × {day_count} 天均已同步并通过校验。"
                if expected and not coverage_issues
                else "没有已配置的财务数据源。" if not expected
                else "部分日期缺少数据或校验未通过，请在“自动化 → 财务”重新拉取该月。"
            ),
            "issues": coverage_issues,
        }
        rescan = {
            "code": "rescan_window",
            "label": "回扫窗口已过",
            "passed": not rescan_issues,
            "summary": (
                "每天的最新数据都取得于该日 7 天回扫之后。"
                if not rescan_issues
                else f"部分日期的数据可能还会被融辉修订；{earliest.isoformat()} 及以后在“自动化 → 财务”重新拉取该月即可。"
                if earliest else "部分日期缺少运行开始时间。"
            ),
            "issues": rescan_issues,
        }
        return coverage, rescan, earliest

    def _balance_chain_check(
        self,
        cursor: Any,
        start: dt.date,
        end: dt.date,
        labels: Mapping[tuple[str, str], str],
    ) -> dict[str, Any]:
        enabled_clause, enabled_params = _enabled_platform_clause("t.platform")
        cursor.execute(
            f"""
            SELECT t.platform, t.account_id, t.business_date, t.transaction_at, t.source_reference,
                   t.before_balance, t.after_balance
            FROM finance_transactions t
            {VISIBLE_TRANSACTION_JOIN}
            WHERE t.business_date BETWEEN %s AND %s AND {enabled_clause}
              AND t.before_balance IS NOT NULL AND t.after_balance IS NOT NULL
            ORDER BY t.platform, t.account_id, t.business_date, t.transaction_at, t.source_reference
            """,
            (start, end, *enabled_params),
        )
        by_account: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in _all(cursor):
            by_account.setdefault((str(row["platform"]), str(row["account_id"])), []).append(row)
        issues: list[dict[str, Any]] = []
        notes: list[str] = []
        previous_day = start - dt.timedelta(days=1)
        for (platform, account_id), items in sorted(by_account.items()):
            label = self._label(labels, platform, account_id)
            first_by_day: dict[dt.date, dict[str, Any]] = {}
            last_by_day: dict[dt.date, dict[str, Any]] = {}
            for row in items:
                day = _as_date(row["business_date"])
                first_by_day.setdefault(day, row)
                last_by_day[day] = row
            days = sorted(first_by_day)
            for before_day, after_day in zip(days, days[1:]):
                closing = _decimal(last_by_day[before_day]["after_balance"])
                opening = _decimal(first_by_day[after_day]["before_balance"])
                if closing != opening:
                    issues.append({
                        "source": label, "from_date": before_day.isoformat(), "to_date": after_day.isoformat(),
                        "closing_balance": _money(closing), "opening_balance": _money(opening),
                        "difference": _money(opening - closing),
                    })
            prior = self._prior_balance(cursor, platform, account_id, start, previous_day)
            if prior is None:
                notes.append(f"{label}：上月末日没有完整数据，月初余额未与上月衔接核对。")
                continue
            closing = _decimal(prior["after_balance"])
            opening = _decimal(first_by_day[days[0]]["before_balance"])
            if closing != opening:
                issues.append({
                    "source": label, "from_date": _as_date(prior["business_date"]).isoformat(),
                    "to_date": days[0].isoformat(), "closing_balance": _money(closing),
                    "opening_balance": _money(opening), "difference": _money(opening - closing),
                })
        return {
            "code": "balance_chain",
            "label": "跨日余额连续",
            "passed": not issues,
            "summary": (
                "各账号每天的期初余额都等于上一笔流水的期末余额。"
                if not issues else "余额在下列日期之间不连续，通常是中间日期缺少数据。"
            ) + ("" if not notes else " " + " ".join(notes)),
            "issues": issues,
        }

    def _prior_balance(
        self, cursor: Any, platform: str, account_id: str, start: dt.date, previous_day: dt.date
    ) -> dict[str, Any] | None:
        """The last balance before the month, only if the prior day is published."""

        cursor.execute(
            f"""
            SELECT r.id FROM finance_sync_runs r
            INNER JOIN ({LATEST_PUBLISHED_RUNS}) latest ON latest.latest_run_id = r.id
            WHERE r.platform = %s AND r.account_id = %s AND r.target_date = %s
            """,
            (platform, account_id, previous_day),
        )
        if not _all(cursor):
            return None
        cursor.execute(
            f"""
            SELECT t.business_date, t.after_balance
            FROM finance_transactions t
            {VISIBLE_TRANSACTION_JOIN}
            WHERE t.platform = %s AND t.account_id = %s AND t.business_date < %s
              AND t.before_balance IS NOT NULL AND t.after_balance IS NOT NULL
            ORDER BY t.business_date DESC, t.transaction_at DESC, t.source_reference DESC
            LIMIT 1
            """,
            (platform, account_id, start),
        )
        return _one(cursor)

    def _item_checks(
        self,
        cursor: Any,
        start: dt.date,
        end: dt.date,
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        reviews = self._period_reviews(cursor, start)
        items = self._items(rows, reviews, (), closed=False)
        unbound = [item for item in items if item["review_status"] == "unbound"]
        unconfirmed = [item for item in items if item["review_status"] in {"pending", "stale"}]

        def name(item: Mapping[str, Any]) -> str:
            return " / ".join(part for part in (item["primary_fee_name"], item["secondary_fee_name"]) if part)

        fee_ids = sorted({item["fee_item_id"] for item in items})
        review_cases: list[dict[str, Any]] = []
        if fee_ids:
            cursor.execute(
                "SELECT rc.id, rc.fee_item_id FROM finance_review_cases rc "
                f"WHERE rc.status = 'open' AND rc.fee_item_id IN ({', '.join(['%s'] * len(fee_ids))})",
                tuple(fee_ids),
            )
            review_cases = _all(cursor)
        names = {item["fee_item_id"]: name(item) for item in items}
        cursor.execute(
            """
            SELECT a.id, a.platform, a.account_id, a.business_date, a.fee_item_id,
                   a.occurrence_count, a.amount
            FROM finance_anomalies a
            WHERE a.anomaly_type = 'MISSING_WAYBILL' AND a.status = 'open'
              AND a.business_date BETWEEN %s AND %s
            ORDER BY a.business_date, a.id
            """,
            (start, end),
        )
        anomalies = _all(cursor)
        return [
            {
                "code": "items_bound",
                "label": "费用项目均已绑定",
                "passed": not unbound,
                "summary": "本月出现的费用项目都已绑定标准科目。" if not unbound else
                           f"{len(unbound)} 个项目还没有绑定，请先在“异常审批”或“费用项目绑定”页处理。",
                "issues": [{"fee_item_id": item["fee_item_id"], "name": name(item)} for item in unbound],
            },
            {
                "code": "items_confirmed",
                "label": "本月归类已核对",
                "passed": not unconfirmed,
                "summary": "所有项目都已按当前绑定确认。" if not unconfirmed else
                           f"{len(unconfirmed)} 个项目待核对或绑定已改动需重新核对。",
                "issues": [{"fee_item_id": item["fee_item_id"], "name": name(item), "review_status": item["review_status"]}
                           for item in unconfirmed],
            },
            {
                "code": "reviews_and_anomalies",
                "label": "待审与异常已处理",
                "passed": not review_cases and not anomalies,
                "summary": "没有未处理的待审或缺运单号异常。" if not review_cases and not anomalies else
                           "请处理下列待审项目；缺运单号异常可确认知悉并写明原因。",
                "issues": [
                    {"kind": "review_case", "review_case_id": int(row["id"]),
                     "name": names.get(int(row["fee_item_id"]), "")} for row in review_cases
                ] + [
                    {"kind": "missing_waybill", "anomaly_id": int(row["id"]),
                     "business_date": _date_text(row["business_date"]),
                     "name": names.get(int(row["fee_item_id"] or 0), ""),
                     "occurrence_count": int(row["occurrence_count"]), "amount": _money(row["amount"])}
                    for row in anomalies
                ],
            },
        ]

    # ----------------------------------------------------------------- writes

    def _lock_period(self, cursor: Any, month: dt.date, actor: str) -> dict[str, Any]:
        cursor.execute(
            "INSERT INTO finance_periods (period_month, status, updated_by, updated_at) "
            "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE period_month = period_month",
            (month, PERIOD_OPEN, actor, _now()),
        )
        cursor.execute(self._PERIOD_ROW_SQL + " FOR UPDATE", (month,))
        row = _one(cursor)
        if not row:
            raise FinancePeriodError("PERIOD_UNAVAILABLE", "期间记录不可用，请稍后重试。")
        return row

    def _event(self, cursor: Any, month: dt.date, action: str, request_id: str) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT period_month, details_json FROM finance_period_events WHERE action = %s AND request_id = %s",
            (action, request_id),
        )
        row = _one(cursor)
        if row is None:
            return None
        if _as_date(row["period_month"]) != month:
            raise FinancePeriodError("REQUEST_CONFLICT", "该请求标识已用于其他月份，请刷新页面后重试。")
        details = row["details_json"]
        return json.loads(details) if isinstance(details, str) else dict(details or {})

    def _record_event(
        self, cursor: Any, month: dt.date, action: str, request_id: str, actor: str, details: Mapping[str, Any]
    ) -> None:
        cursor.execute(
            "INSERT INTO finance_period_events (period_month, action, request_id, actor, details_json, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (month, action, request_id, actor, _json(dict(details)), _now()),
        )

    def confirm_period_items(
        self,
        month: Any,
        items: Sequence[Mapping[str, Any]],
        *,
        actor: str,
        request_id: str,
    ) -> dict[str, Any]:
        period_month = parse_period_month(month)
        end = period_month_end(period_month)
        operator = _actor(actor)
        request = _request_id(request_id)
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence) or not items or len(items) > MAX_CONFIRM_ITEMS:
            raise FinancePeriodError("INVALID_REQUEST", f"每次确认 1 到 {MAX_CONFIRM_ITEMS} 个项目。")
        expected: dict[int, int] = {}
        for item in items:
            try:
                fee_item_id, mapping_id = int(item["fee_item_id"]), int(item["mapping_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FinancePeriodError("INVALID_REQUEST", "确认的项目或绑定版本无效。") from exc
            if fee_item_id <= 0 or mapping_id <= 0 or fee_item_id in expected:
                raise FinancePeriodError("INVALID_REQUEST", "确认的项目重复或无效。")
            expected[fee_item_id] = mapping_id
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                replay = self._event(cursor, period_month, "confirm", request)
                if replay is not None:
                    return replay
                period = self._lock_period(cursor, period_month, operator)
                if str(period["status"]) == PERIOD_CLOSED:
                    raise FinancePeriodError("PERIOD_CLOSED", "该月已封账，不能再修改核对结果；需要时请先反结账。")
                ids = sorted(expected)
                enabled_clause, enabled_params = _enabled_platform_clause("t.platform")
                cursor.execute(
                    f"""
                    SELECT t.fee_item_id, fm.id AS mapping_id, fm.canonical_subject_id
                    FROM finance_transactions t
                    {VISIBLE_TRANSACTION_JOIN}
                    {_EFFECTIVE_MAPPING_JOIN}
                    WHERE t.business_date BETWEEN %s AND %s AND {enabled_clause}
                      AND t.fee_item_id IN ({', '.join(['%s'] * len(ids))})
                    GROUP BY t.fee_item_id, fm.id, fm.canonical_subject_id
                    """,
                    (period_month, end, *enabled_params, *ids),
                )
                effective: dict[int, list[dict[str, Any]]] = {}
                for row in _all(cursor):
                    effective.setdefault(int(row["fee_item_id"]), []).append(row)
                for fee_item_id, mapping_id in expected.items():
                    current = effective.get(fee_item_id) or []
                    if not current:
                        raise FinancePeriodError("ITEM_NOT_ACTIVE", "所选项目本月没有发生，请刷新后重试。")
                    if len(current) != 1 or not current[0].get("mapping_id") or not current[0].get("canonical_subject_id"):
                        raise FinancePeriodError("ITEM_UNBOUND", "所选项目还没有绑定标准科目，请先完成绑定。")
                    if int(current[0]["mapping_id"]) != mapping_id:
                        raise FinancePeriodError("MAPPING_CHANGED", "项目的绑定已被修改，请刷新后按最新绑定重新核对。")
                now = _now()
                cursor.executemany(
                    """
                    INSERT INTO finance_period_item_reviews (
                        period_month, fee_item_id, mapping_id, confirmed_by, confirmed_at, request_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE mapping_id = VALUES(mapping_id),
                        confirmed_by = VALUES(confirmed_by), confirmed_at = VALUES(confirmed_at),
                        request_id = VALUES(request_id)
                    """,
                    [(period_month, fee_item_id, expected[fee_item_id], operator, now, request) for fee_item_id in ids],
                )
                result = {
                    "month": period_month.strftime("%Y-%m"),
                    "confirmed": [{"fee_item_id": fee_item_id, "mapping_id": expected[fee_item_id]} for fee_item_id in ids],
                    "confirmed_by": operator,
                    "confirmed_at": _date_text(now),
                }
                self._record_event(cursor, period_month, "confirm", request, operator, result)
                return result
            finally:
                cursor.close()

    def close_period(self, month: Any, *, actor: str, request_id: str, today: dt.date) -> dict[str, Any]:
        period_month = parse_period_month(month)
        end = period_month_end(period_month)
        operator = _actor(actor)
        request = _request_id(request_id)
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT id, period_month FROM finance_period_closes WHERE close_request_id = %s",
                    (request,),
                )
                replay = _one(cursor)
                if replay is not None:
                    if _as_date(replay["period_month"]) != period_month:
                        raise FinancePeriodError("REQUEST_CONFLICT", "该请求标识已用于其他月份，请刷新页面后重试。")
                    return self._close_result(cursor, int(replay["id"]))
                period = self._lock_period(cursor, period_month, operator)
                if str(period["status"]) == PERIOD_CLOSED:
                    raise FinancePeriodError("PERIOD_CLOSED", "该月已封账。")
                rows = self._live_rows(cursor, period_month, end)
                checks, _earliest = self._period_checks(cursor, period_month, today=today, rows=rows)
                if not all(check["passed"] for check in checks):
                    raise FinancePeriodError(
                        "PERIOD_CHECKS_FAILED", "封账前检查未全部通过，请按检查结果处理后再封账。", details=checks
                    )
                cursor.execute(
                    "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM finance_period_closes WHERE period_month = %s",
                    (period_month,),
                )
                version_no = int((_one(cursor) or {}).get("version_no") or 0) + 1
                closed_at = _now()
                cursor.execute(
                    """
                    INSERT INTO finance_period_closes (
                        period_month, version_no, status, line_count, transaction_count,
                        total_income, total_expense, net_change, content_sha256, check_report_json,
                        closed_by, closed_at, close_request_id
                    ) VALUES (%s, %s, %s, 0, 0, 0, 0, 0, %s, %s, %s, %s, %s)
                    """,
                    (period_month, version_no, PERIOD_CLOSED, "0" * 64, _json(checks), operator, closed_at, request),
                )
                close_id = int(cursor.lastrowid)
                lines = self._write_snapshot_lines(cursor, close_id, period_month, end)
                summary = self._get_summary_core(FinanceQuery(start_date=period_month, end_date=end), connection=connection)
                totals = self._verify_snapshot(cursor, close_id, period_month, end, lines, summary)
                content = hashlib.sha256(
                    _json({"month": period_month.isoformat(), "lines": lines}).encode("utf-8")
                ).hexdigest()
                cursor.execute(
                    """
                    UPDATE finance_period_closes
                    SET line_count = %s, transaction_count = %s, total_income = %s,
                        total_expense = %s, net_change = %s, content_sha256 = %s
                    WHERE id = %s
                    """,
                    (len(lines), totals["count"], totals["income"], totals["expense"], totals["net"], content, close_id),
                )
                cursor.execute(
                    "UPDATE finance_periods SET status = %s, current_close_id = %s, updated_by = %s, updated_at = %s "
                    "WHERE period_month = %s",
                    (PERIOD_CLOSED, close_id, operator, closed_at, period_month),
                )
                cursor.execute(
                    "UPDATE finance_anomalies SET status = 'resolved', last_seen_at = %s "
                    "WHERE anomaly_type = 'CLOSED_PERIOD_DRIFT' AND status = 'open' AND business_date BETWEEN %s AND %s",
                    (closed_at, period_month, end),
                )
                self._record_event(cursor, period_month, "close", request, operator, {
                    "version": version_no, "close_id": close_id, "line_count": len(lines),
                    "transaction_count": totals["count"], "net_change": _storage_text(totals["net"]),
                    "content_sha256": content,
                })
                return self._close_result(cursor, close_id)
            finally:
                cursor.close()

    def _write_snapshot_lines(
        self, cursor: Any, close_id: int, start: dt.date, end: dt.date
    ) -> list[dict[str, Any]]:
        enabled_clause, enabled_params = _enabled_platform_clause("t.platform")
        cursor.execute(
            f"""
            INSERT INTO finance_period_close_lines (
                close_id, platform, account_id, business_date, fee_item_id, direction,
                mapping_id, fee_level, canonical_subject_id, include_in_cost,
                transaction_count, income, expense, net_change, source_run_id
            )
            SELECT %s, t.platform, t.account_id, t.business_date, t.fee_item_id, t.direction,
                   fm.id, fm.fee_level, fm.canonical_subject_id, COALESCE(fm.include_in_cost, 0),
                   COUNT(*), SUM(t.income), SUM(t.expense), SUM(t.income - t.expense), MIN(t.run_id)
            FROM finance_transactions t
            {VISIBLE_TRANSACTION_JOIN}
            {_EFFECTIVE_MAPPING_JOIN}
            WHERE t.business_date BETWEEN %s AND %s AND {enabled_clause}
            GROUP BY t.platform, t.account_id, t.business_date, t.fee_item_id, t.direction,
                     fm.id, fm.fee_level, fm.canonical_subject_id, fm.include_in_cost
            HAVING MIN(t.run_id) = MAX(t.run_id)
            """,
            (close_id, start, end, *enabled_params),
        )
        cursor.execute(
            """
            SELECT platform, account_id, business_date, fee_item_id, direction, mapping_id,
                   fee_level, canonical_subject_id, include_in_cost, transaction_count,
                   income, expense, net_change, source_run_id
            FROM finance_period_close_lines WHERE close_id = %s
            ORDER BY platform, account_id, business_date, fee_item_id, direction
            """,
            (close_id,),
        )
        return [
            {
                "platform": str(row["platform"]), "account_id": str(row["account_id"]),
                "business_date": _as_date(row["business_date"]).isoformat(),
                "fee_item_id": int(row["fee_item_id"]), "direction": str(row["direction"]),
                "mapping_id": int(row["mapping_id"]) if row.get("mapping_id") else None,
                "fee_level": row.get("fee_level"),
                "canonical_subject_id": int(row["canonical_subject_id"]) if row.get("canonical_subject_id") else None,
                "include_in_cost": bool(row.get("include_in_cost")),
                "transaction_count": int(row["transaction_count"]),
                "income": _storage_text(row["income"]), "expense": _storage_text(row["expense"]),
                "net_change": _storage_text(row["net_change"]), "source_run_id": int(row["source_run_id"]),
            }
            for row in _all(cursor)
        ]

    def _verify_snapshot(
        self,
        cursor: Any,
        close_id: int,
        start: dt.date,
        end: dt.date,
        lines: Sequence[Mapping[str, Any]],
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Snapshot lines must equal the ledger and the BI summary, else roll back."""

        totals = {"count": 0, "income": ZERO, "expense": ZERO, "net": ZERO}
        for line in lines:
            totals["count"] += int(line["transaction_count"])
            totals["income"] += _decimal(line["income"])
            totals["expense"] += _decimal(line["expense"])
            totals["net"] += _decimal(line["net_change"])
        enabled_clause, enabled_params = _enabled_platform_clause("t.platform")
        cursor.execute(
            f"""
            SELECT COUNT(*) AS transaction_count, COALESCE(SUM(t.income), 0) AS income,
                   COALESCE(SUM(t.expense), 0) AS expense,
                   COALESCE(SUM(t.income - t.expense), 0) AS net_change,
                   COUNT(DISTINCT t.platform, t.account_id, t.business_date, t.fee_item_id, t.direction) AS group_count
            FROM finance_transactions t
            {VISIBLE_TRANSACTION_JOIN}
            WHERE t.business_date BETWEEN %s AND %s AND {enabled_clause}
            """,
            (start, end, *enabled_params),
        )
        ledger = _one(cursor) or {}
        mismatches = []
        if int(ledger.get("group_count") or 0) != len(lines):
            mismatches.append("快照行数与流水分组数不一致")
        if int(ledger.get("transaction_count") or 0) != totals["count"]:
            mismatches.append("快照笔数与逐笔流水不一致")
        for name in ("income", "expense"):
            if _decimal(ledger.get(name)) != totals[name]:
                mismatches.append(f"快照{name}与逐笔流水不一致")
        if _decimal(ledger.get("net_change")) != totals["net"] or totals["income"] - totals["expense"] != totals["net"]:
            mismatches.append("快照净额与逐笔流水反算不一致")
        if int(summary.get("entry_count") or 0) != totals["count"]:
            mismatches.append("快照笔数与 BI 汇总不一致")
        for summary_key, total_key in (("total_income", "income"), ("total_expense", "expense"), ("net_change", "net")):
            if str(summary.get(summary_key)) != _money(totals[total_key]):
                mismatches.append(f"快照{total_key}与 BI 汇总不一致")
        if mismatches:
            raise FinancePeriodError(
                "SNAPSHOT_MISMATCH", "封账快照与流水或 BI 汇总不一致，本次未封账；如刚有同步完成，请稍后重试。",
                details=mismatches,
            )
        return totals

    def _close_result(self, cursor: Any, close_id: int) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT id, period_month, version_no, status, line_count, transaction_count,
                   total_income, total_expense, net_change, content_sha256, closed_by, closed_at,
                   reopened_by, reopened_at, reopen_reason
            FROM finance_period_closes WHERE id = %s
            """,
            (int(close_id),),
        )
        row = _one(cursor)
        if row is None:
            raise FinancePeriodError("PERIOD_UNAVAILABLE", "封账记录不可用。")
        result = self._close_summary(row)
        result["month"] = _as_date(row["period_month"]).strftime("%Y-%m")
        return result

    def reopen_period(self, month: Any, *, actor: str, request_id: str, reason: str) -> dict[str, Any]:
        period_month = parse_period_month(month)
        operator = _actor(actor)
        request = _request_id(request_id)
        text = str(reason or "").strip()
        if len(text) < 2 or len(text) > 500:
            raise FinancePeriodError("INVALID_REQUEST", "反结账原因需填写 2 到 500 个字。")
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT id, period_month FROM finance_period_closes WHERE reopen_request_id = %s",
                    (request,),
                )
                replay = _one(cursor)
                if replay is not None:
                    if _as_date(replay["period_month"]) != period_month:
                        raise FinancePeriodError("REQUEST_CONFLICT", "该请求标识已用于其他月份，请刷新页面后重试。")
                    return self._close_result(cursor, int(replay["id"]))
                period = self._lock_period(cursor, period_month, operator)
                if str(period["status"]) != PERIOD_CLOSED or not period.get("current_close_id"):
                    raise FinancePeriodError("PERIOD_NOT_CLOSED", "该月尚未封账，无需反结账。")
                close_id = int(period["current_close_id"])
                reopened_at = _now()
                cursor.execute(
                    """
                    UPDATE finance_period_closes
                    SET status = %s, reopened_by = %s, reopened_at = %s, reopen_reason = %s, reopen_request_id = %s
                    WHERE id = %s AND status = %s
                    """,
                    (PERIOD_REOPENED, operator, reopened_at, text, request, close_id, PERIOD_CLOSED),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise FinancePeriodError("PERIOD_CONFLICT", "封账状态已变化，请刷新后重试。")
                cursor.execute(
                    "UPDATE finance_periods SET status = %s, updated_by = %s, updated_at = %s WHERE period_month = %s",
                    (PERIOD_REOPENED, operator, reopened_at, period_month),
                )
                self._record_event(cursor, period_month, "reopen", request, operator, {"close_id": close_id, "reason": text})
                return self._close_result(cursor, close_id)
            finally:
                cursor.close()

    def acknowledge_anomaly(
        self, anomaly_id: int, *, actor: str, request_id: str, reason: str
    ) -> dict[str, Any]:
        operator = _actor(actor)
        request = _request_id(request_id)
        text = str(reason or "").strip()
        if len(text) < 2 or len(text) > 500:
            raise FinancePeriodError("INVALID_REQUEST", "确认知悉需填写 2 到 500 个字的原因。")
        with self._connection() as connection:
            cursor = connection.cursor()
            try:
                anomaly_sql = (
                    "SELECT id, anomaly_type, status, business_date, occurrence_count, amount "
                    "FROM finance_anomalies WHERE id = %s"
                )
                cursor.execute(anomaly_sql, (int(anomaly_id),))
                anomaly = _one(cursor)
                if anomaly is None:
                    raise FinancePeriodError("NOT_FOUND", "异常记录不存在。")
                month = _as_date(anomaly["business_date"]).replace(day=1)
                replay = self._event(cursor, month, "acknowledge", request)
                if replay is not None:
                    return replay
                # Same lock order as close: the period first, then its rows.
                period = self._lock_period(cursor, month, operator)
                cursor.execute(anomaly_sql + " FOR UPDATE", (int(anomaly_id),))
                anomaly = _one(cursor) or anomaly
                if str(anomaly["anomaly_type"]) not in ACKNOWLEDGEABLE_ANOMALIES:
                    raise FinancePeriodError("ANOMALY_NOT_ACKNOWLEDGEABLE", "该类异常需要处理，不能只确认知悉。")
                if str(anomaly["status"]) != "open":
                    raise FinancePeriodError("ANOMALY_NOT_OPEN", "该异常已处理或已确认知悉。")
                if str(period["status"]) == PERIOD_CLOSED:
                    raise FinancePeriodError("PERIOD_CLOSED", "该月已封账；需要时请先反结账。")
                now = _now()
                cursor.execute(
                    """
                    UPDATE finance_anomalies
                    SET status = 'acknowledged', acknowledged_by = %s, acknowledged_at = %s,
                        acknowledge_reason = %s, acknowledged_count = occurrence_count,
                        acknowledged_amount = amount
                    WHERE id = %s AND status = 'open'
                    """,
                    (operator, now, text, int(anomaly_id)),
                )
                result = {"anomaly_id": int(anomaly_id), "status": "acknowledged", "reason": text,
                          "acknowledged_by": operator, "acknowledged_at": _date_text(now)}
                self._record_event(cursor, month, "acknowledge", request, operator, result)
                return result
            finally:
                cursor.close()

    # ------------------------------------------------- hooks for other writers

    def _closed_month_mappings(self, cursor: Any, fee_item_id: int) -> dict[dt.date, int | None]:
        """Effective mapping per closed month whose snapshot contains this item."""

        cursor.execute(
            """
            SELECT p.period_month, (
                SELECT fm2.id FROM finance_fee_mappings fm2
                WHERE fm2.fee_item_id = %s AND fm2.superseded_at IS NULL
                  AND fm2.mapping_status = 'bound'
                  AND fm2.effective_start_month <= p.period_month
                  AND (fm2.effective_end_month IS NULL OR fm2.effective_end_month >= p.period_month)
                ORDER BY fm2.effective_start_month DESC, fm2.version_no DESC, fm2.id DESC
                LIMIT 1
            ) AS mapping_id
            FROM finance_periods p
            WHERE p.status = %s AND EXISTS (
                SELECT 1 FROM finance_period_close_lines l
                WHERE l.close_id = p.current_close_id AND l.fee_item_id = %s
            )
            """,
            (int(fee_item_id), PERIOD_CLOSED, int(fee_item_id)),
        )
        return {
            _as_date(row["period_month"]): int(row["mapping_id"]) if row.get("mapping_id") else None
            for row in _all(cursor)
        }

    def _assert_closed_mappings_unchanged(
        self, cursor: Any, fee_item_id: int, before: Mapping[dt.date, int | None]
    ) -> None:
        after = self._closed_month_mappings(cursor, fee_item_id)
        changed = sorted(month for month in set(before) | set(after) if before.get(month) != after.get(month))
        if changed:
            months = "、".join(month.strftime("%Y-%m") for month in changed)
            raise FinancePeriodError(
                "PERIOD_CLOSED",
                f"该修改会改变已封账月份 {months} 的归类；请把生效月份设在封账月份之后，或先反结账。",
            )

    def _record_closed_period_drift(self, cursor: Any, runs: Iterable[Mapping[str, Any]]) -> int:
        """Compare newly published runs in closed months with their snapshot."""

        runs = [run for run in runs if run.get("target_date")]
        months = sorted({_as_date(run["target_date"]).replace(day=1) for run in runs})
        if not months:
            return 0
        cursor.execute(
            "SELECT period_month, current_close_id FROM finance_periods "
            f"WHERE status = %s AND period_month IN ({', '.join(['%s'] * len(months))})",
            (PERIOD_CLOSED, *months),
        )
        closed = {_as_date(row["period_month"]): int(row["current_close_id"]) for row in _all(cursor) if row.get("current_close_id")}
        recorded = 0
        for run in runs:
            day = _as_date(run["target_date"])
            close_id = closed.get(day.replace(day=1))
            if close_id is None:
                continue
            platform, account_id = str(run["platform"]), str(run["account_id"])
            cursor.execute(
                f"""
                SELECT t.fee_item_id, t.direction, fm.id AS mapping_id, COUNT(*) AS transaction_count,
                       SUM(t.income) AS income, SUM(t.expense) AS expense
                FROM finance_transactions t
                {VISIBLE_TRANSACTION_JOIN}
                {_EFFECTIVE_MAPPING_JOIN}
                WHERE t.platform = %s AND t.account_id = %s AND t.business_date = %s
                GROUP BY t.fee_item_id, t.direction, fm.id
                """,
                (platform, account_id, day),
            )
            live = {_item_key(row): row for row in _all(cursor)}
            cursor.execute(
                "SELECT fee_item_id, direction, mapping_id, transaction_count, income, expense "
                "FROM finance_period_close_lines WHERE close_id = %s AND platform = %s AND account_id = %s "
                "AND business_date = %s",
                (close_id, platform, account_id, day),
            )
            snapshot = {_item_key(row): row for row in _all(cursor)}

            def signature(row: Mapping[str, Any] | None) -> tuple[Any, ...] | None:
                if row is None:
                    return None
                return (row.get("mapping_id") and int(row["mapping_id"]), int(row["transaction_count"]),
                        _decimal(row["income"]), _decimal(row["expense"]))

            changed = [key for key in set(live) | set(snapshot) if signature(live.get(key)) != signature(snapshot.get(key))]
            fingerprint = _fingerprint("CLOSED_PERIOD_DRIFT", close_id, platform, account_id, day)
            now = _now()
            if not changed:
                cursor.execute(
                    "UPDATE finance_anomalies SET status = 'resolved', last_seen_at = %s "
                    "WHERE fingerprint = %s AND status = 'open'",
                    (now, fingerprint),
                )
                continue
            live_net = sum((_decimal(row["income"]) - _decimal(row["expense"]) for row in live.values()), ZERO)
            snapshot_net = sum((_decimal(row["income"]) - _decimal(row["expense"]) for row in snapshot.values()), ZERO)
            cursor.execute(
                """
                INSERT INTO finance_anomalies (
                    fingerprint, anomaly_type, platform, account_id, business_date, fee_item_id,
                    status, occurrence_count, amount, details_json, first_seen_at, last_seen_at
                ) VALUES (%s, 'CLOSED_PERIOD_DRIFT', %s, %s, %s, NULL, 'open', %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    occurrence_count = VALUES(occurrence_count), amount = VALUES(amount),
                    details_json = VALUES(details_json),
                    notified_at = IF(status = 'open', notified_at, NULL),
                    status = 'open', last_seen_at = VALUES(last_seen_at)
                """,
                (
                    fingerprint, platform, account_id, day, len(changed), live_net - snapshot_net,
                    _json({"period_month": day.strftime("%Y-%m"), "close_id": close_id,
                           "snapshot_net": _storage_text(snapshot_net), "live_net": _storage_text(live_net)}),
                    now, now,
                ),
            )
            recorded += 1
        return recorded
