"""Deterministic, read-only business queries over reviewed shared repositories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from shared.finance import FinanceQuery


MAX_QUERY_DAYS = 366
_SUMMARY_FIELDS = ("total_income", "total_expense", "net_change")
_CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


class FinanceSummaryRepository(Protocol):
    def get_business_summary(self, query: FinanceQuery) -> dict[str, Any]: ...


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


def _amount(value: object, field_name: str) -> tuple[str, Decimal]:
    if not isinstance(value, str) or not value:
        raise BusinessQueryError(
            "BUSINESS_QUERY_CONTRACT_INVALID",
            f"finance summary field {field_name} is invalid",
        )
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise BusinessQueryError(
            "BUSINESS_QUERY_CONTRACT_INVALID",
            f"finance summary field {field_name} is invalid",
        ) from exc
    if not parsed.is_finite():
        raise BusinessQueryError(
            "BUSINESS_QUERY_CONTRACT_INVALID",
            f"finance summary field {field_name} is invalid",
        )
    return value, parsed


class BusinessFinanceQueryService:
    """Return source-backed finance aggregates without LLM or SQL input."""

    def __init__(
        self,
        repository: FinanceSummaryRepository,
        *,
        enabled_platforms: Sequence[str],
    ) -> None:
        self._repository = repository
        self._enabled_platforms = tuple(enabled_platforms)
        if (
            not self._enabled_platforms
            or any(
                not isinstance(platform, str)
                or not platform
                or platform != platform.strip()
                for platform in self._enabled_platforms
            )
            or len(set(self._enabled_platforms)) != len(self._enabled_platforms)
        ):
            raise ValueError("enabled finance platforms must be a non-empty unique sequence")

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
        platform = platform_value.strip() if platform_value is not None else ""
        if platform and platform not in self._enabled_platforms:
            raise BusinessQueryError(
                "BUSINESS_QUERY_SOURCE_DISABLED",
                "requested finance platform is not enabled",
            )
        try:
            raw = self._repository.get_business_summary(
                FinanceQuery(
                    start_date=start_date,
                    end_date=end_date,
                    platform=platform or None,
                )
            )
        except BusinessQueryError:
            raise
        except Exception as exc:
            raise BusinessQueryError(
                "BUSINESS_QUERY_UNAVAILABLE",
                "finance summary repository is unavailable",
            ) from exc
        return self._validated_result(
            raw,
            start_date=start_date,
            end_date=end_date,
            platform=platform,
        )

    @staticmethod
    def _validated_result(
        raw: object,
        *,
        start_date: date,
        end_date: date,
        platform: str,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise BusinessQueryError(
                "BUSINESS_QUERY_CONTRACT_INVALID",
                "finance summary repository returned an invalid contract",
            )
        failed_sources = raw.get("failed_sources")
        if not isinstance(failed_sources, list):
            raise BusinessQueryError(
                "BUSINESS_QUERY_CONTRACT_INVALID",
                "finance summary source status is invalid",
            )
        validation_status = str(raw.get("validation_status") or "").strip().lower()
        if failed_sources or validation_status in {"failed", "warning"}:
            raise BusinessQueryError(
                "BUSINESS_QUERY_DATA_UNVERIFIED",
                "finance summary did not pass source validation",
            )
        data_through_text = str(raw.get("data_through_date") or "").strip()
        if validation_status != "passed" or not data_through_text:
            raise BusinessQueryError(
                "BUSINESS_QUERY_DATA_UNVERIFIED",
                "finance summary validation status is incomplete",
            )
        if raw.get("coverage_status") != "complete":
            raise BusinessQueryError(
                "BUSINESS_QUERY_DATA_INCOMPLETE",
                "finance summary does not cover every enabled source and requested date",
            )
        if raw.get("reconciliation_status") != "passed":
            raise BusinessQueryError(
                "BUSINESS_QUERY_RECONCILIATION_FAILED",
                "finance summary failed source-level reconciliation",
            )
        data_through = _iso_date(data_through_text, "data_through_date")
        if data_through < end_date:
            raise BusinessQueryError(
                "BUSINESS_QUERY_DATA_INCOMPLETE",
                "finance summary has not been synchronized through the requested end date",
            )
        entry_count = raw.get("entry_count")
        pending_fee_items = raw.get("pending_fee_items")
        if (
            isinstance(entry_count, bool)
            or not isinstance(entry_count, int)
            or entry_count < 0
            or isinstance(pending_fee_items, bool)
            or not isinstance(pending_fee_items, int)
            or pending_fee_items < 0
        ):
            raise BusinessQueryError(
                "BUSINESS_QUERY_CONTRACT_INVALID",
                "finance summary counts are invalid",
            )
        source = {
            "name": "shared_finance_ledger",
            "platform": platform or "all_enabled",
            "validation_status": validation_status,
            "data_through_date": data_through_text,
            "latest_success_at": str(raw.get("latest_success_at") or ""),
        }
        period = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        if entry_count == 0:
            return {
                "query_type": "finance_summary",
                "availability": "NO_DATA",
                "period": period,
                "summary": {},
                "source": source,
                "record_count": 0,
                "warnings": ["所选期间的已验证财务账本没有交易记录。"],
            }
        amounts = {
            field: _amount(raw.get(field), field) for field in _SUMMARY_FIELDS
        }
        warnings = []
        if pending_fee_items:
            warnings.append(
                "存在未分类费用项目；总收入、总支出和净变动可查询，但不得把净变动解释为利润。"
            )
        return {
            "query_type": "finance_summary",
            "availability": "DATA",
            "period": period,
            "summary": {field: amounts[field][0] for field in _SUMMARY_FIELDS}
            | {"pending_fee_items": pending_fee_items},
            "source": source,
            "record_count": entry_count,
            "warnings": warnings,
        }


class MySQLAutomationOperationsRepository:
    """Fixed, read-only aggregation over the durable orchestration tables."""

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
                command_rows = self._select_status_counts(
                    connection,
                    "agent_commands",
                    "requested_at",
                    start_utc,
                    end_exclusive_utc,
                )
                run_rows = self._select_status_counts(
                    connection,
                    "agent_runs",
                    "created_at",
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
            "command_status_counts": _status_counts(command_rows),
            "run_status_counts": _status_counts(run_rows),
            "freshness": dict(freshness_rows[0]) if freshness_rows else {},
        }

    @staticmethod
    def _begin_consistent_read(connection: Any) -> None:
        cursor = connection.cursor()
        try:
            # All three fixed aggregates must observe one InnoDB snapshot.
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
    def _select_status_counts(connection: Any, table: str, column: str, start_utc: datetime, end_exclusive_utc: datetime) -> list[dict[str, Any]]:
        # table and column are code constants, never caller input.
        statement = (
            f"SELECT status, COUNT(*) AS count FROM {table} "
            f"WHERE {column} >= %s AND {column} < %s GROUP BY status"
        )
        return _select_rows(connection, statement, (start_utc, end_exclusive_utc))

    @staticmethod
    def _select_freshness(connection: Any, start_utc: datetime, end_exclusive_utc: datetime) -> list[dict[str, Any]]:
        return _select_rows(
            connection,
            "SELECT "
            "(SELECT MAX(requested_at) FROM agent_commands WHERE requested_at >= %s AND requested_at < %s) AS latest_command_requested_at, "
            "(SELECT MAX(updated_at) FROM agent_runs WHERE created_at >= %s AND created_at < %s) AS latest_run_updated_at",
            (start_utc, end_exclusive_utc, start_utc, end_exclusive_utc),
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
        return self._validated_result(raw, start_date=start_date, end_date=end_date)

    @staticmethod
    def _validated_result(raw: object, *, start_date: date, end_date: date) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations summary is invalid")
        command_counts = _validated_status_counts(raw.get("command_status_counts"))
        run_counts = _validated_status_counts(raw.get("run_status_counts"))
        freshness = raw.get("freshness")
        if not isinstance(freshness, Mapping):
            raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations freshness is invalid")
        normalized_freshness = {
            "latest_command_requested_at": _timestamp_text(freshness.get("latest_command_requested_at")),
            "latest_run_updated_at": _timestamp_text(freshness.get("latest_run_updated_at")),
        }
        terminal = sum(run_counts.get(status, 0) for status in ("COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"))
        completed = run_counts.get("COMPLETED", 0)
        success_rate = None if terminal == 0 else {
            "completed_runs": completed,
            "terminal_runs": terminal,
            "value": format(Decimal(completed) / Decimal(terminal), ".4f"),
        }
        command_total = sum(command_counts.values())
        run_total = sum(run_counts.values())
        return {
            "query_type": "automation_operations",
            "availability": "DATA" if command_total or run_total else "NO_DATA",
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "commands": {"status_counts": command_counts, "total": command_total},
            "runs": {"status_counts": run_counts, "total": run_total, "success_rate": success_rate},
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
        return value if value.endswith("Z") else f"{value}Z"
    raise BusinessQueryError("AUTOMATION_OPERATIONS_CONTRACT_INVALID", "automation operations freshness is invalid")


def _china_day_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Map closed China business dates to UTC-naive MySQL timestamp bounds."""

    start_local = datetime.combine(start_date, datetime.min.time(), tzinfo=_CHINA_TIMEZONE)
    end_local = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=_CHINA_TIMEZONE)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )
