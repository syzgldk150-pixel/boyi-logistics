"""Deterministic, read-only business queries over reviewed shared repositories."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from shared.plugin_invocation_repository import ACTIVE_INVOCATION_STATUSES, TERMINAL_INVOCATION_STATUSES


MAX_QUERY_DAYS = 366
FINANCE_REBUILDING_CODE = "BUSINESS_QUERY_FINANCE_REBUILDING"
_CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


class AutomationOperationsRepository(Protocol):
    def get_operations_summary(self, *, start_date: date, end_date: date) -> dict[str, Any]: ...


class BusinessQueryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _iso_date(value: object, field_name: str) -> date:
    if not isinstance(value, str) or value != value.strip():
        raise BusinessQueryError(
            "BUSINESS_QUERY_INVALID",
            f"{field_name} must use YYYY-MM-DD",
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BusinessQueryError(
            "BUSINESS_QUERY_INVALID",
            f"{field_name} must use YYYY-MM-DD",
        ) from exc


class BusinessFinanceQueryService:
    """Validate finance read requests while the general ledger is being rebuilt.

    The previous platform-statement ledger was retired with migration 067.
    Until the double-entry ledger reports are wired in, every well-formed
    request fails closed with ``BUSINESS_QUERY_FINANCE_REBUILDING`` so no
    caller can mistake missing data for a zero amount.
    """

    def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise BusinessQueryError("BUSINESS_QUERY_INVALID", "arguments must be an object")
        unknown = set(arguments) - {"start_date", "end_date", "platform"}
        if unknown:
            raise BusinessQueryError(
                "BUSINESS_QUERY_INVALID",
                "business finance query contains unsupported fields",
            )
        start_date = _iso_date(arguments.get("start_date"), "start_date")
        end_date = _iso_date(arguments.get("end_date"), "end_date")
        if start_date > end_date or (end_date - start_date).days + 1 > MAX_QUERY_DAYS:
            raise BusinessQueryError(
                "BUSINESS_QUERY_INVALID",
                f"finance query period must be ordered and at most {MAX_QUERY_DAYS} days",
            )
        platform_value = arguments.get("platform")
        if platform_value is not None and not isinstance(platform_value, str):
            raise BusinessQueryError(
                "BUSINESS_QUERY_INVALID",
                "platform must be a string",
            )
        raise BusinessQueryError(
            FINANCE_REBUILDING_CODE,
            "finance ledger is being rebuilt; no amounts are available",
        )


class MySQLAutomationOperationsRepository:
    """Read current invocations in one snapshot, grouped by China start date."""

    def __init__(self, connection_factory: Any) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    def get_operations_summary(self, *, start_date: date, end_date: date) -> dict[str, Any]:
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        start_utc, end_exclusive_utc = _china_day_bounds(start_date, end_date)
        try:
            with self._connection() as connection:
                self._begin_consistent_read(connection)
                invocation_rows = self._select_status_counts(
                    connection,
                    start_utc,
                    end_exclusive_utc,
                )
                freshness_rows = self._select_freshness(
                    connection,
                    start_utc,
                    end_exclusive_utc,
                )
        except BusinessQueryError:
            raise
        except Exception as exc:
            raise BusinessQueryError(
                "AUTOMATION_OPERATIONS_UNAVAILABLE",
                "automation operations repository is unavailable",
            ) from exc
        return {
            "invocation_status_counts": _status_counts(invocation_rows),
            "freshness": dict(freshness_rows[0]) if freshness_rows else {},
        }

    @staticmethod
    def _begin_consistent_read(connection: Any) -> None:
        cursor = connection.cursor()
        try:
            # Status counts and freshness must observe the same InnoDB snapshot.
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    @contextmanager
    def _connection(self):
        resource = self._connection_factory()
        if hasattr(resource, "__enter__") and hasattr(resource, "__exit__"):
            with resource as connection:
                yield connection
            return
        connection = resource
        try:
            yield connection
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _select_status_counts(connection: Any, start_utc: datetime, end_exclusive_utc: datetime) -> list[dict[str, Any]]:
        statement = (
            "SELECT status, COUNT(*) AS count FROM automation_plugin_invocations "
            "WHERE started_at >= %s AND started_at < %s GROUP BY status"
        )
        return _select_rows(connection, statement, (start_utc, end_exclusive_utc))

    @staticmethod
    def _select_freshness(connection: Any, start_utc: datetime, end_exclusive_utc: datetime) -> list[dict[str, Any]]:
        return _select_rows(
            connection,
            "SELECT "
            "MAX(started_at) AS latest_invocation_started_at, "
            "MAX(updated_at) AS latest_invocation_updated_at "
            "FROM automation_plugin_invocations WHERE started_at >= %s AND started_at < %s",
            (start_utc, end_exclusive_utc),
        )


class AutomationOperationsQueryService:
    """Validate a closed date-range status summary without accepting SQL or filters."""

    def __init__(self, repository: AutomationOperationsRepository) -> None:
        self._repository = repository

    def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping) or set(arguments) != {"start_date", "end_date"}:
            raise BusinessQueryError(
                "AUTOMATION_OPERATIONS_INVALID",
                "automation operations query requires only start_date and end_date",
            )
        start_date = _iso_date(arguments.get("start_date"), "start_date")
        end_date = _iso_date(arguments.get("end_date"), "end_date")
        if start_date > end_date or (end_date - start_date).days + 1 > MAX_QUERY_DAYS:
            raise BusinessQueryError(
                "AUTOMATION_OPERATIONS_INVALID",
                f"automation operations period must be ordered and at most {MAX_QUERY_DAYS} days",
            )
        try:
            raw = self._repository.get_operations_summary(start_date=start_date, end_date=end_date)
        except BusinessQueryError:
            raise
        except Exception as exc:
            raise BusinessQueryError(
                "AUTOMATION_OPERATIONS_UNAVAILABLE",
                "automation operations repository is unavailable",
            ) from exc
        return self.validated_result(raw, start_date=start_date, end_date=end_date)

    @staticmethod
    def validated_result(raw: object, *, start_date: date, end_date: date) -> dict[str, Any]:
        """Build the closed result; callers and renderers share its count checks."""
        if not isinstance(raw, Mapping):
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations summary is invalid")
        invocation_counts = _validated_status_counts(raw.get("invocation_status_counts"))
        if set(invocation_counts) - (ACTIVE_INVOCATION_STATUSES | TERMINAL_INVOCATION_STATUSES):
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation invocation status is invalid")
        freshness = raw.get("freshness")
        if not isinstance(freshness, Mapping):
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations freshness is invalid")
        normalized_freshness = {
            "latest_invocation_started_at": _timestamp_text(freshness.get("latest_invocation_started_at")),
            "latest_invocation_updated_at": _timestamp_text(freshness.get("latest_invocation_updated_at")),
        }
        invocation_total = sum(invocation_counts.values())
        if any(value is None for value in normalized_freshness.values()) != (invocation_total == 0) or (
            invocation_total == 0 and any(value is not None for value in normalized_freshness.values())
        ):
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation invocation freshness disagrees with counts")
        terminal = sum(invocation_counts.get(status, 0) for status in TERMINAL_INVOCATION_STATUSES)
        completed = invocation_counts.get("COMPLETED", 0)
        success_rate = None if terminal == 0 else {
            "completed_invocations": completed,
            "terminal_invocations": terminal,
            "value": format(Decimal(completed) / Decimal(terminal), ".4f"),
        }
        return {
            "query_type": "automation_operations",
            "record_source": "automation_plugin_invocations",
            "availability": "DATA" if invocation_total else "NO_DATA",
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "invocations": {"status_counts": invocation_counts, "total": invocation_total, "success_rate": success_rate},
            "freshness": normalized_freshness,
        }


def _select_rows(connection: Any, statement: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = connection.cursor()
    try:
        cursor.execute(statement, params)
        names = [str(item[0]) for item in (getattr(cursor, "description", None) or ())]
        rows = cursor.fetchall() or []
        return [dict(row) if isinstance(row, Mapping) else dict(zip(names, row)) for row in rows]
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def _status_counts(rows: object) -> dict[str, int]:
    if not isinstance(rows, list):
        raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations status rows are invalid")
    result: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations status rows are invalid")
        status = str(row.get("status") or "").strip().upper()
        count = row.get("count")
        if not status or isinstance(count, bool) or not isinstance(count, int) or count < 0 or status in result:
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations status rows are invalid")
        result[status] = count
    return result


def _validated_status_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations status counts are invalid")
    return _status_counts([{"status": key, "count": item} for key, item in value.items()])


def _timestamp_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, str) and value == value.strip() and value:
        try:
            return _timestamp_text(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as exc:
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations freshness is invalid") from exc
    raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations freshness is invalid")


def _china_day_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Map closed China business dates to UTC-naive MySQL timestamp bounds."""

    start_local = datetime.combine(start_date, datetime.min.time(), tzinfo=_CHINA_TIMEZONE)
    end_local = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=_CHINA_TIMEZONE)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )
