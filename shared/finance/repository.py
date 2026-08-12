"""DB-API repository for finance snapshots, mappings, and BI queries.

The caller owns connection configuration.  This module never reads environment
variables or account credentials.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from shared.finance.mappings import (
    RONGHUI_BOOKING_FEE_ITEMS,
    YUNDA_BOOKING_FEE_ITEMS,
    mapping_seed_for_fee_item,
    validate_booking_fee_name,
)
from shared.finance.models import (
    Direction,
    EarliestDateStatus,
    FeeItemKey,
    FeeLevel,
    FeeMappingSeed,
    FinanceQuery,
    MappingStatus,
    Platform,
    SummarySnapshot,
    SyncStatus,
    TransactionRecord,
    ValidationStatus,
    month_start,
)
from shared.finance.money import ZERO, format_money
from shared.finance.schema import validate_finance_schema
from shared.finance.validation import ValidationReport
from shared.finance.evolution import FinanceEvolutionMixin


ConnectionFactory = Callable[[], Any]


class FinanceRepositoryError(RuntimeError):
    pass


class FinanceSnapshotRejectedError(FinanceRepositoryError):
    pass


class FinanceMappingConflictError(FinanceRepositoryError):
    pass


class FinanceNotFoundError(FinanceRepositoryError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now().replace(tzinfo=None)


def _date(value: dt.date | str, field_name: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _row_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    description = getattr(cursor, "description", None) or ()
    names = [str(item[0]) for item in description]
    return dict(zip(names, row))


def _fetchone(cursor: Any) -> dict[str, Any] | None:
    return _row_dict(cursor, cursor.fetchone())


def _fetchall(cursor: Any) -> list[dict[str, Any]]:
    return [item for row in (cursor.fetchall() or []) if (item := _row_dict(cursor, row))]


@contextmanager
def _managed_cursor(connection: Any) -> Iterator[Any]:
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


class FinanceRepository(FinanceEvolutionMixin):
    """Finance persistence over a caller-supplied DB-API connection factory."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        resource = self._connection_factory()
        if hasattr(resource, "__enter__") and hasattr(resource, "__exit__"):
            with resource as connection:
                yield connection
            return
        connection = resource
        try:
            yield connection
            commit = getattr(connection, "commit", None)
            if callable(commit):
                commit()
        except Exception:
            rollback = getattr(connection, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def initialize_schema(self) -> None:
        """Validate the migration-owned schema without executing runtime DDL."""
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            validate_finance_schema(cursor)

    def create_batch(
        self,
        *,
        trigger_type: str,
        start_date: dt.date | str,
        end_date: dt.date | str,
        rescan_days: int = 0,
        requested_by: str | None = None,
        earliest_date_status: EarliestDateStatus | str | None = None,
    ) -> int:
        trigger = _required_text(trigger_type, "trigger_type")
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start > end:
            raise ValueError("start_date must not be after end_date")
        if int(rescan_days) < 0:
            raise ValueError("rescan_days cannot be negative")
        earliest = (
            EarliestDateStatus(earliest_date_status).value
            if earliest_date_status is not None
            else None
        )
        now = _now()
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                """
                INSERT INTO finance_sync_batches (
                    trigger_type, requested_start_date, requested_end_date,
                    rescan_days, status, earliest_date_status, requested_by,
                    frozen_at, started_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    trigger,
                    start,
                    end,
                    int(rescan_days),
                    SyncStatus.RUNNING.value,
                    earliest,
                    str(requested_by or "").strip() or None,
                    now,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def start_run(
        self,
        *,
        batch_id: int,
        platform: Platform | str,
        account_id: str,
        login_account: str,
        session_profile: str,
        target_date: dt.date | str,
        source_site_code: str | None = None,
        source_site_name: str | None = None,
    ) -> int:
        platform_value = Platform(platform).value
        account = _required_text(account_id, "account_id")
        login = _required_text(login_account, "login_account")
        profile = _required_text(session_profile, "session_profile")
        target = _date(target_date, "target_date")
        now = _now()
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                "SELECT id, status FROM finance_sync_batches WHERE id = %s FOR UPDATE",
                (int(batch_id),),
            )
            batch = _fetchone(cursor)
            if not batch:
                raise FinanceNotFoundError("sync batch does not exist")
            if str(batch.get("status")) != SyncStatus.RUNNING.value:
                raise FinanceRepositoryError("sync batch is not running")
            cursor.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) AS max_attempt
                FROM finance_sync_runs
                WHERE batch_id = %s AND platform = %s AND account_id = %s AND target_date = %s
                """,
                (int(batch_id), platform_value, account, target),
            )
            attempt = int((_fetchone(cursor) or {}).get("max_attempt") or 0) + 1
            cursor.execute(
                """
                INSERT INTO finance_sync_runs (
                    batch_id, platform, account_id, login_account, session_profile,
                    target_date, source_site_code, source_site_name, attempt_no,
                    status, started_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    int(batch_id),
                    platform_value,
                    account,
                    login,
                    profile,
                    target,
                    str(source_site_code or "").strip() or None,
                    str(source_site_name or "").strip() or None,
                    attempt,
                    SyncStatus.RUNNING.value,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def start_failed_run(
        self,
        *,
        batch_id: int,
        platform: Platform | str,
        account_id: str,
        target_date: dt.date | str,
        error_code: str,
        error_message: str,
    ) -> int:
        """Record a role/account binding failure without fabricating login data."""

        platform_value = Platform(platform).value
        account = _required_text(account_id, "account_id")
        target = _date(target_date, "target_date")
        code = _required_text(error_code, "error_code")
        message = _required_text(error_message, "error_message")
        now = _now()
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                "SELECT id, status FROM finance_sync_batches WHERE id = %s FOR UPDATE",
                (int(batch_id),),
            )
            batch = _fetchone(cursor)
            if not batch:
                raise FinanceNotFoundError("sync batch does not exist")
            if str(batch.get("status")) != SyncStatus.RUNNING.value:
                raise FinanceRepositoryError("sync batch is not running")
            cursor.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) AS max_attempt
                FROM finance_sync_runs
                WHERE batch_id = %s AND platform = %s AND account_id = %s AND target_date = %s
                """,
                (int(batch_id), platform_value, account, target),
            )
            attempt = int((_fetchone(cursor) or {}).get("max_attempt") or 0) + 1
            cursor.execute(
                """
                INSERT INTO finance_sync_runs (
                    batch_id, platform, account_id, login_account, session_profile,
                    target_date, attempt_no, status, validation_status,
                    error_code, error_message, started_at, finished_at, created_at
                ) VALUES (%s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    int(batch_id),
                    platform_value,
                    account,
                    target,
                    attempt,
                    SyncStatus.FAILED.value,
                    ValidationStatus.FAILED.value,
                    code,
                    message,
                    now,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def _load_run_for_update(self, cursor: Any, run_id: int) -> dict[str, Any]:
        cursor.execute(
            "SELECT * FROM finance_sync_runs WHERE id = %s FOR UPDATE",
            (int(run_id),),
        )
        row = _fetchone(cursor)
        if not row:
            raise FinanceNotFoundError("sync run does not exist")
        if str(row.get("status")) != SyncStatus.RUNNING.value:
            raise FinanceRepositoryError("sync run is not running")
        return row

    def _upsert_fee_item(self, cursor: Any, record: TransactionRecord) -> int:
        seen_month = record.business_date.replace(day=1)
        now = _now()
        cursor.execute(
            """
            INSERT INTO finance_fee_items (
                platform, raw_primary_fee_name, raw_secondary_fee_name, direction,
                first_seen_month, last_seen_month, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                first_seen_month = LEAST(first_seen_month, VALUES(first_seen_month)),
                last_seen_month = GREATEST(last_seen_month, VALUES(last_seen_month)),
                updated_at = VALUES(updated_at)
            """,
            (
                record.platform.value,
                record.primary_fee_name,
                record.secondary_fee_name,
                record.direction.value,
                seen_month,
                seen_month,
                now,
                now,
            ),
        )
        cursor.execute(
            """
            SELECT id, first_seen_month FROM finance_fee_items
            WHERE platform = %s AND raw_primary_fee_name = %s
              AND raw_secondary_fee_name = %s AND direction = %s
            """,
            (
                record.platform.value,
                record.primary_fee_name,
                record.secondary_fee_name,
                record.direction.value,
            ),
        )
        row = _fetchone(cursor)
        if not row:
            raise FinanceRepositoryError("fee item upsert did not return an id")
        fee_item_id = int(row["id"])
        seed = mapping_seed_for_fee_item(record.fee_key)
        if seed is not None:
            self._seed_mapping_if_missing(
                cursor,
                fee_item_id=fee_item_id,
                first_seen_month=_date(row["first_seen_month"], "first_seen_month"),
                seed=seed,
            )
        return fee_item_id

    def _seed_mapping_if_missing(
        self,
        cursor: Any,
        *,
        fee_item_id: int,
        first_seen_month: dt.date,
        seed: FeeMappingSeed,
    ) -> bool:
        cursor.execute(
            """
            SELECT platform, raw_primary_fee_name, raw_secondary_fee_name
            FROM finance_fee_items WHERE id = %s
            """,
            (int(fee_item_id),),
        )
        fee_item = _fetchone(cursor)
        if not fee_item:
            raise FinanceNotFoundError("fee item does not exist")
        subject_name = (
            seed.canonical_subject_name
            or seed.booking_fee_name
            or str(fee_item.get("raw_secondary_fee_name") or "")
            or str(fee_item["raw_primary_fee_name"])
        )
        subject_id = self._ensure_fee_subject(
            cursor,
            platform=str(fee_item["platform"]),
            subject_code=seed.canonical_subject_code,
            subject_name=subject_name,
            fee_level=seed.fee_level.value,
            booking_fee_name=seed.booking_fee_name,
            requires_waybill=seed.requires_waybill,
            created_by="system:verified-baseline",
        )
        cursor.execute(
            """
            SELECT id, direction, fee_level, canonical_subject_id,
                   booking_fee_name, requires_waybill, effective_start_month,
                   include_in_cost, created_by
            FROM finance_fee_mappings
            WHERE fee_item_id = %s AND superseded_at IS NULL
            ORDER BY effective_start_month ASC, version_no ASC, id ASC
            LIMIT 1
            """,
            (int(fee_item_id),),
        )
        current = _fetchone(cursor)
        if current:
            legacy_fields_match = (
                str(current.get("direction") or "") == seed.direction.value
                and str(current.get("fee_level") or "") == seed.fee_level.value
                and str(current.get("booking_fee_name") or "") == seed.booking_fee_name
                and bool(current.get("include_in_cost")) is seed.include_in_cost
            )
            if not current.get("canonical_subject_id") and legacy_fields_match:
                before = dict(current)
                cursor.execute(
                    """
                    UPDATE finance_fee_mappings
                    SET canonical_subject_id = %s, requires_waybill = %s
                    WHERE id = %s AND canonical_subject_id IS NULL
                    """,
                    (subject_id, 1 if seed.requires_waybill else 0, int(current["id"])),
                )
                if int(cursor.rowcount or 0) == 1:
                    after = {
                        **before,
                        "canonical_subject_id": subject_id,
                        "requires_waybill": seed.requires_waybill,
                    }
                    cursor.execute(
                        """
                        INSERT INTO finance_mapping_audit_logs (
                            fee_item_id, mapping_id, action, before_json, after_json,
                            changed_by, change_reason, created_at
                        ) VALUES (%s, %s, 'canonical_backfill', %s, %s, %s, %s, %s)
                        """,
                        (
                            int(fee_item_id),
                            int(current["id"]),
                            _json_text(before),
                            _json_text(after),
                            "system:verified-baseline",
                            "backfill canonical subject for an exact confirmed legacy mapping",
                            _now(),
                        ),
                    )
                    current["canonical_subject_id"] = subject_id
                    current["requires_waybill"] = seed.requires_waybill
            current_start = _date(
                current["effective_start_month"],
                "effective_start_month",
            )
            is_same_verified_seed = (
                str(current.get("created_by") or "") == "system:verified-baseline"
                and str(current.get("direction") or "") == seed.direction.value
                and str(current.get("fee_level") or "") == seed.fee_level.value
                and int(current.get("canonical_subject_id") or 0) == subject_id
                and str(current.get("booking_fee_name") or "") == seed.booking_fee_name
                and bool(current.get("requires_waybill")) is seed.requires_waybill
                and bool(current.get("include_in_cost")) is seed.include_in_cost
            )
            if not is_same_verified_seed or first_seen_month >= current_start:
                return False
            prior_end_month = (current_start - dt.timedelta(days=1)).replace(day=1)
            self._insert_mapping(
                cursor,
                fee_item_id=fee_item_id,
                direction=seed.direction,
                fee_level=seed.fee_level,
                canonical_subject_id=subject_id,
                booking_fee_name=seed.booking_fee_name,
                requires_waybill=seed.requires_waybill,
                effective_start_month=first_seen_month,
                effective_end_month=prior_end_month,
                include_in_cost=seed.include_in_cost,
                changed_by="system:verified-baseline",
                reason="verified baseline extended to actual earlier first-seen month",
                action="seed_history",
            )
            return True
        self._insert_mapping(
            cursor,
            fee_item_id=fee_item_id,
            direction=seed.direction,
            fee_level=seed.fee_level,
            canonical_subject_id=subject_id,
            booking_fee_name=seed.booking_fee_name,
            requires_waybill=seed.requires_waybill,
            effective_start_month=first_seen_month,
            effective_end_month=None,
            include_in_cost=seed.include_in_cost,
            changed_by="system:verified-baseline",
            reason="real-page verified exact baseline",
            action="seed",
        )
        return True

    def commit_run_snapshot(
        self,
        *,
        run_id: int,
        transactions: Sequence[TransactionRecord],
        summaries: Sequence[SummarySnapshot],
        validation: ValidationReport,
    ) -> dict[str, Any]:
        if not validation.passed:
            raise FinanceSnapshotRejectedError("validation failed; snapshot was not committed")
        rows = tuple(transactions)
        summary_rows = tuple(summaries)
        if not rows:
            raise FinanceSnapshotRejectedError("empty snapshot must use mark_run_no_data")
        now = _now()
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            run = self._load_run_for_update(cursor, run_id)
            expected_platform = str(run["platform"])
            expected_account = str(run["account_id"])
            expected_login = str(run.get("login_account") or "")
            expected_date = _date(run["target_date"], "target_date")
            for record in rows:
                if (
                    record.platform.value != expected_platform
                    or record.account_id != expected_account
                    or record.login_account != expected_login
                    or record.business_date != expected_date
                ):
                    raise FinanceSnapshotRejectedError(
                        "transaction platform/account/login/business_date does not match the run"
                    )
            for summary in summary_rows:
                if (
                    summary.platform.value != expected_platform
                    or summary.account_id != expected_account
                    or summary.target_date != expected_date
                ):
                    raise FinanceSnapshotRejectedError(
                        "summary platform/account/target_date does not match the run"
                    )

            written = 0
            for record in rows:
                fee_item_id = self._upsert_fee_item(cursor, record)
                cursor.execute(
                    """
                    INSERT INTO finance_transactions (
                        run_id, fee_item_id, platform, account_id, login_account,
                        source_record_key, business_date, transaction_at,
                        raw_primary_fee_name, raw_secondary_fee_name, direction,
                        income, expense, before_balance, after_balance, waybill_no,
                        source_reference, remark, source_payload_json, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        int(run_id),
                        fee_item_id,
                        record.platform.value,
                        record.account_id,
                        record.login_account,
                        record.source_record_key,
                        record.business_date,
                        record.transaction_at,
                        record.primary_fee_name,
                        record.secondary_fee_name,
                        record.direction.value,
                        record.income,
                        record.expense,
                        record.before_balance,
                        record.after_balance,
                        record.waybill_no or None,
                        record.source_reference or None,
                        record.remark or None,
                        _json_text(dict(record.source_payload)),
                        now,
                    ),
                )
                written += int(cursor.rowcount or 0)

            if written != len(rows):
                raise FinanceSnapshotRejectedError(
                    "database write count does not equal parsed transaction count"
                )
            for summary in summary_rows:
                cursor.execute(
                    """
                    INSERT INTO finance_summary_snapshots (
                        run_id, platform, account_id, target_date,
                        raw_primary_fee_name, raw_secondary_fee_name, direction,
                        income, expense, net_change, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        int(run_id),
                        summary.platform.value,
                        summary.account_id,
                        summary.target_date,
                        summary.primary_fee_name,
                        summary.secondary_fee_name,
                        summary.direction.value,
                        summary.income,
                        summary.expense,
                        summary.net_change,
                        now,
                    ),
                )

            report = validation.to_dict()
            report["metrics"]["written_row_count"] = written
            remote_total = int(report["metrics"]["remote_total"])
            unique_count = int(report["metrics"]["unique_row_count"])
            if written != unique_count:
                raise FinanceSnapshotRejectedError(
                    "database write count does not equal validated unique row count"
                )
            cursor.execute(
                """
                UPDATE finance_sync_runs
                SET status = %s, remote_total = %s, page_row_count = %s,
                    unique_row_count = %s, written_row_count = %s,
                    validation_status = %s, validation_report_json = %s,
                    finished_at = %s, error_code = NULL, error_message = NULL
                WHERE id = %s
                """,
                (
                    SyncStatus.SUCCESS.value,
                    remote_total,
                    int(report["metrics"]["page_row_count"]),
                    unique_count,
                    written,
                    validation.status.value,
                    _json_text(report),
                    now,
                    int(run_id),
                ),
            )
            derivatives = self._refresh_run_derivatives(cursor, run)
            report["metrics"].update(derivatives)
            cursor.execute(
                "UPDATE finance_sync_runs SET validation_report_json = %s WHERE id = %s",
                (_json_text(report), int(run_id)),
            )
        return {
            "run_id": int(run_id),
            "status": SyncStatus.SUCCESS.value,
            "remote_total": remote_total,
            "unique_row_count": unique_count,
            "written_row_count": written,
            "validation_status": validation.status.value,
            "derivatives": derivatives,
        }

    def mark_run_no_data(self, *, run_id: int, validation: ValidationReport) -> None:
        report = validation.to_dict()
        if not validation.passed or not bool(report.get("metrics", {}).get("eligible_no_data")):
            raise FinanceSnapshotRejectedError(
                "no-data status requires a valid explicit remote total of zero"
            )
        now = _now()
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            self._load_run_for_update(cursor, run_id)
            report["metrics"]["written_row_count"] = 0
            cursor.execute(
                """
                UPDATE finance_sync_runs
                SET status = %s, remote_total = 0, page_row_count = 0,
                    unique_row_count = 0, written_row_count = 0,
                    validation_status = %s, validation_report_json = %s,
                    finished_at = %s, error_code = NULL, error_message = NULL
                WHERE id = %s
                """,
                (
                    SyncStatus.NO_DATA.value,
                    validation.status.value,
                    _json_text(report),
                    now,
                    int(run_id),
                ),
            )

    def fail_run(self, *, run_id: int, error_code: str, error_message: str) -> None:
        code = _required_text(error_code, "error_code")
        message = _required_text(error_message, "error_message")
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                "SELECT id FROM finance_sync_runs WHERE id = %s FOR UPDATE",
                (int(run_id),),
            )
            if not _fetchone(cursor):
                raise FinanceNotFoundError("sync run does not exist")
            cursor.execute(
                """
                UPDATE finance_sync_runs
                SET status = %s, validation_status = %s, error_code = %s,
                    error_message = %s, finished_at = %s
                WHERE id = %s
                """,
                (
                    SyncStatus.FAILED.value,
                    ValidationStatus.FAILED.value,
                    code,
                    message,
                    _now(),
                    int(run_id),
                ),
            )

    def finalize_batch(self, batch_id: int) -> SyncStatus:
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                "SELECT id FROM finance_sync_batches WHERE id = %s FOR UPDATE",
                (int(batch_id),),
            )
            if not _fetchone(cursor):
                raise FinanceNotFoundError("sync batch does not exist")
            cursor.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM finance_sync_runs WHERE batch_id = %s GROUP BY status
                """,
                (int(batch_id),),
            )
            counts = {str(row["status"]): int(row["total"]) for row in _fetchall(cursor)}
            if counts.get(SyncStatus.RUNNING.value) or counts.get(SyncStatus.QUEUED.value):
                raise FinanceRepositoryError("cannot finalize a batch with unfinished runs")
            failed = counts.get(SyncStatus.FAILED.value, 0)
            completed = counts.get(SyncStatus.SUCCESS.value, 0) + counts.get(
                SyncStatus.NO_DATA.value, 0
            )
            if not counts:
                status = SyncStatus.NO_DATA
            elif failed and completed:
                status = SyncStatus.PARTIAL_FAILED
            elif failed:
                status = SyncStatus.FAILED
            else:
                status = SyncStatus.SUCCESS
            cursor.execute(
                """
                UPDATE finance_sync_batches
                SET status = %s, finished_at = %s
                WHERE id = %s
                """,
                (status.value, _now(), int(batch_id)),
            )
            return status

    def list_missing_dates(
        self,
        *,
        platform: Platform | str,
        account_id: str,
        start_date: dt.date | str,
        end_date: dt.date | str,
    ) -> list[dt.date]:
        platform_value = Platform(platform).value
        account = _required_text(account_id, "account_id")
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start > end:
            raise ValueError("start_date must not be after end_date")
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                """
                SELECT DISTINCT target_date
                FROM finance_sync_runs
                WHERE platform = %s AND account_id = %s
                  AND target_date BETWEEN %s AND %s
                  AND status IN (%s, %s)
                """,
                (
                    platform_value,
                    account,
                    start,
                    end,
                    SyncStatus.SUCCESS.value,
                    SyncStatus.NO_DATA.value,
                ),
            )
            present = {
                _date(row["target_date"], "target_date") for row in _fetchall(cursor)
            }
        result: list[dt.date] = []
        current = start
        while current <= end:
            if current not in present:
                result.append(current)
            current += dt.timedelta(days=1)
        return result

    def list_retry_targets(self, batch_id: int) -> list[dict[str, Any]]:
        """Return only latest failed run targets from an existing batch."""

        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                "SELECT id FROM finance_sync_batches WHERE id = %s",
                (int(batch_id),),
            )
            if not _fetchone(cursor):
                raise FinanceNotFoundError("sync batch does not exist")
            cursor.execute(
                """
                SELECT r.platform, r.account_id, r.login_account, r.session_profile,
                       r.target_date, r.source_site_code, r.source_site_name,
                       r.error_code, r.error_message
                FROM finance_sync_runs r
                INNER JOIN (
                    SELECT platform, account_id, target_date, MAX(id) AS latest_run_id
                    FROM finance_sync_runs
                    WHERE batch_id = %s
                    GROUP BY platform, account_id, target_date
                ) latest ON latest.latest_run_id = r.id
                WHERE r.status = %s
                ORDER BY r.target_date, r.platform, r.account_id
                """,
                (int(batch_id), SyncStatus.FAILED.value),
            )
            rows = _fetchall(cursor)
        if not rows:
            raise FinanceNotFoundError("sync batch has no failed retry targets")
        return [self._serialize_general_row(row) for row in rows]

    def get_validation_context(
        self,
        *,
        platform: Platform | str,
        account_id: str,
        target_date: dt.date | str,
        source_record_keys: Iterable[str],
    ) -> dict[str, Any]:
        """Load exact category and prior-snapshot evidence for validation."""

        platform_value = Platform(platform)
        account = _required_text(account_id, "account_id")
        target = _date(target_date, "target_date")
        keys = sorted(
            {
                _required_text(value, "source_record_key")
                for value in source_record_keys
            }
        )
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                """
                SELECT raw_primary_fee_name, raw_secondary_fee_name, direction
                FROM finance_fee_items WHERE platform = %s
                """,
                (platform_value.value,),
            )
            known_fee_items = {
                FeeItemKey(
                    platform=platform_value,
                    primary_fee_name=str(row["raw_primary_fee_name"]),
                    secondary_fee_name=str(row.get("raw_secondary_fee_name") or ""),
                    direction=Direction(str(row["direction"])),
                )
                for row in _fetchall(cursor)
            }
            previous_payloads: dict[str, tuple[Any, ...]] = {}
            if keys:
                placeholders = ", ".join(["%s"] * len(keys))
                cursor.execute(
                    f"""
                    SELECT t.*
                    FROM finance_transactions t
                    INNER JOIN (
                        SELECT MAX(id) AS latest_run_id
                        FROM finance_sync_runs
                        WHERE platform = %s AND account_id = %s
                          AND target_date = %s AND status = %s
                    ) latest ON latest.latest_run_id = t.run_id
                    WHERE t.source_record_key IN ({placeholders})
                    """,
                    (
                        platform_value.value,
                        account,
                        target,
                        SyncStatus.SUCCESS.value,
                        *keys,
                    ),
                )
                for row in _fetchall(cursor):
                    record = TransactionRecord(
                        platform=platform_value,
                        account_id=str(row["account_id"]),
                        login_account=str(row["login_account"]),
                        source_record_key=str(row["source_record_key"]),
                        business_date=row["business_date"],
                        transaction_at=row.get("transaction_at"),
                        primary_fee_name=str(row["raw_primary_fee_name"]),
                        secondary_fee_name=str(row.get("raw_secondary_fee_name") or ""),
                        direction=Direction(str(row["direction"])),
                        income=row["income"],
                        expense=row["expense"],
                        before_balance=row.get("before_balance"),
                        after_balance=row.get("after_balance"),
                        waybill_no=str(row.get("waybill_no") or ""),
                        source_reference=str(row.get("source_reference") or ""),
                        remark=str(row.get("remark") or ""),
                    )
                    previous_payloads[record.source_record_key] = record.comparison_payload()
        return {
            "known_fee_items": known_fee_items,
            "previous_record_payloads": previous_payloads,
        }

    def _insert_mapping(
        self,
        cursor: Any,
        *,
        fee_item_id: int,
        direction: Direction,
        fee_level: FeeLevel,
        canonical_subject_id: int,
        booking_fee_name: str,
        requires_waybill: bool,
        effective_start_month: dt.date,
        effective_end_month: dt.date | None,
        include_in_cost: bool,
        changed_by: str,
        reason: str,
        action: str,
        before: Mapping[str, Any] | None = None,
    ) -> int:
        cursor.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS max_version FROM finance_fee_mappings WHERE fee_item_id = %s",
            (int(fee_item_id),),
        )
        version_no = int((_fetchone(cursor) or {}).get("max_version") or 0) + 1
        now = _now()
        cursor.execute(
            """
            INSERT INTO finance_fee_mappings (
                fee_item_id, direction, fee_level, canonical_subject_id,
                booking_fee_name, requires_waybill,
                effective_start_month, effective_end_month, include_in_cost,
                mapping_status, version_no, created_by, change_reason, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(fee_item_id),
                direction.value,
                fee_level.value,
                int(canonical_subject_id),
                booking_fee_name or None,
                1 if requires_waybill else 0,
                effective_start_month,
                effective_end_month,
                1 if include_in_cost else 0,
                MappingStatus.BOUND.value,
                version_no,
                changed_by,
                reason or None,
                now,
            ),
        )
        mapping_id = int(cursor.lastrowid)
        after = {
            "mapping_id": mapping_id,
            "direction": direction.value,
            "fee_level": fee_level.value,
            "canonical_subject_id": int(canonical_subject_id),
            "booking_fee_name": booking_fee_name,
            "requires_waybill": bool(requires_waybill),
            "effective_start_month": effective_start_month.isoformat(),
            "effective_end_month": (
                effective_end_month.isoformat() if effective_end_month else None
            ),
            "include_in_cost": bool(include_in_cost),
            "mapping_status": MappingStatus.BOUND.value,
            "version_no": version_no,
        }
        cursor.execute(
            """
            INSERT INTO finance_mapping_audit_logs (
                fee_item_id, mapping_id, action, before_json, after_json,
                changed_by, change_reason, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                int(fee_item_id),
                mapping_id,
                action,
                _json_text(dict(before)) if before else None,
                _json_text(after),
                changed_by,
                reason or None,
                now,
            ),
        )
        return mapping_id

    def save_fee_mapping(
        self,
        *,
        fee_item_id: int,
        fee_level: FeeLevel | str,
        canonical_subject_name: str,
        booking_fee_name: str = "",
        canonical_subject_code: str = "",
        requires_waybill: bool | None = None,
        effective_start_month: dt.date | str,
        effective_end_month: dt.date | str | None = None,
        include_in_cost: bool,
        changed_by: str,
        reason: str,
    ) -> int:
        level = FeeLevel(fee_level)
        start = month_start(effective_start_month)
        end = month_start(effective_end_month) if effective_end_month is not None else None
        if end is not None and end < start:
            raise ValueError("effective_end_month must not precede effective_start_month")
        actor = _required_text(changed_by, "changed_by")
        change_reason = _required_text(reason, "reason")
        subject_name = _required_text(canonical_subject_name, "canonical_subject_name")
        mapping_id = 0
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                "SELECT * FROM finance_fee_items WHERE id = %s FOR UPDATE",
                (int(fee_item_id),),
            )
            fee_item = _fetchone(cursor)
            if not fee_item:
                raise FinanceNotFoundError("fee item does not exist")
            source_direction = Direction(str(fee_item["direction"]))
            if include_in_cost and source_direction is not Direction.EXPENSE:
                raise ValueError("only expense mappings may be included in cost")
            target_name = validate_booking_fee_name(
                platform=Platform(str(fee_item["platform"])),
                fee_level=level,
                booking_fee_name=booking_fee_name,
            )
            require_waybill = level is FeeLevel.WAYBILL if requires_waybill is None else bool(requires_waybill)
            if level is FeeLevel.OPERATING and require_waybill:
                raise ValueError("operating mapping cannot require a waybill")
            subject_id = self._ensure_fee_subject(
                cursor,
                platform=str(fee_item["platform"]),
                subject_code=str(canonical_subject_code or "").strip(),
                subject_name=subject_name,
                fee_level=level.value,
                booking_fee_name=target_name,
                requires_waybill=require_waybill,
                created_by=actor,
            )
            cursor.execute(
                """
                SELECT * FROM finance_fee_mappings
                WHERE fee_item_id = %s AND effective_start_month = %s
                  AND superseded_at IS NULL
                ORDER BY version_no DESC LIMIT 1 FOR UPDATE
                """,
                (int(fee_item_id), start),
            )
            current = _fetchone(cursor)
            if current:
                cursor.execute(
                    "UPDATE finance_fee_mappings SET superseded_at = %s WHERE id = %s",
                    (_now(), int(current["id"])),
                )
            mapping_id = self._insert_mapping(
                cursor,
                fee_item_id=int(fee_item_id),
                direction=source_direction,
                fee_level=level,
                canonical_subject_id=subject_id,
                booking_fee_name=target_name,
                requires_waybill=require_waybill,
                effective_start_month=start,
                effective_end_month=end,
                include_in_cost=bool(include_in_cost),
                changed_by=actor,
                reason=change_reason,
                action="update" if current else "create",
                before=current,
            )
        self.rebuild_waybill_facts_for_fee_item(
            fee_item_id=int(fee_item_id),
            reviewed_by=actor,
            review_reason=change_reason,
        )
        return mapping_id

    def seed_fee_mappings(
        self,
        seeds: Sequence[FeeMappingSeed] | None = None,
    ) -> int:
        explicit = tuple(seeds) if seeds is not None else None
        seeded = 0
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute("SELECT * FROM finance_fee_items ORDER BY id")
            for row in _fetchall(cursor):
                key = FeeItemKey(
                    platform=Platform(str(row["platform"])),
                    primary_fee_name=str(row["raw_primary_fee_name"]),
                    secondary_fee_name=str(row.get("raw_secondary_fee_name") or ""),
                    direction=Direction(str(row["direction"])),
                )
                if explicit is None:
                    seed = mapping_seed_for_fee_item(key)
                else:
                    matches = [
                        item
                        for item in explicit
                        if item.platform is key.platform
                        and item.primary_fee_name == key.primary_fee_name
                        and item.secondary_fee_name == key.secondary_fee_name
                        and item.direction is key.direction
                    ]
                    if len(matches) > 1:
                        raise FinanceMappingConflictError(
                            "multiple explicit seeds match one fee item"
                        )
                    seed = matches[0] if matches else None
                if seed and self._seed_mapping_if_missing(
                    cursor,
                    fee_item_id=int(row["id"]),
                    first_seen_month=_date(row["first_seen_month"], "first_seen_month"),
                    seed=seed,
                ):
                    seeded += 1
        return seeded

    _VISIBLE_ENTRY_FROM = """
        FROM finance_transactions t
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
        INNER JOIN finance_sync_runs visible_run ON visible_run.id = t.run_id
        INNER JOIN finance_fee_items fi ON fi.id = t.fee_item_id
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

    def _entry_filters(self, query: FinanceQuery) -> tuple[list[str], list[Any]]:
        clauses = ["t.business_date BETWEEN %s AND %s"]
        params: list[Any] = [query.start_date, query.end_date]
        if query.platform:
            clauses.append("t.platform = %s")
            params.append(query.platform.value)
        if query.account_id:
            clauses.append("t.account_id = %s")
            params.append(query.account_id)
        if query.direction:
            clauses.append("t.direction = %s")
            params.append(query.direction.value)
        if query.fee_level:
            clauses.append("fm.fee_level = %s")
            params.append(query.fee_level.value)
        if query.fee_name:
            clauses.append(
                "(t.raw_primary_fee_name = %s OR t.raw_secondary_fee_name = %s OR fm.booking_fee_name = %s)"
            )
            params.extend([query.fee_name, query.fee_name, query.fee_name])
        if query.waybill_no:
            clauses.append("t.waybill_no = %s")
            params.append(query.waybill_no)
        return clauses, params

    @staticmethod
    def _format_aggregate(value: Any) -> str:
        return format_money(value if value is not None else ZERO, missing_as_zero=True)

    @staticmethod
    def _serialize_general_row(row: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, dt.datetime):
                result[str(key)] = value.isoformat(sep=" ")
            elif isinstance(value, dt.date):
                result[str(key)] = value.isoformat()
            elif isinstance(value, Decimal):
                result[str(key)] = f"{value:.4f}"
            elif isinstance(value, bytes):
                result[str(key)] = value.decode("utf-8")
            else:
                result[str(key)] = value
        return result

    def _failed_sources(self, query: FinanceQuery) -> list[dict[str, Any]]:
        clauses = ["r.target_date BETWEEN %s AND %s", "r.status = %s"]
        params: list[Any] = [query.start_date, query.end_date, SyncStatus.FAILED.value]
        if query.platform:
            clauses.append("r.platform = %s")
            params.append(query.platform.value)
        if query.account_id:
            clauses.append("r.account_id = %s")
            params.append(query.account_id)
        sql = f"""
            SELECT r.platform, r.account_id, r.target_date, r.error_code, r.error_message
            FROM finance_sync_runs r
            INNER JOIN (
                SELECT platform, account_id, target_date, MAX(id) AS latest_run_id
                FROM finance_sync_runs
                GROUP BY platform, account_id, target_date
            ) latest ON latest.latest_run_id = r.id
            WHERE {' AND '.join(clauses)}
            ORDER BY r.target_date DESC, r.platform, r.account_id
        """
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(sql, tuple(params))
            return [self._serialize_general_row(row) for row in _fetchall(cursor)]

    def _freshness(self, query: FinanceQuery) -> dict[str, Any]:
        clauses = ["r.target_date BETWEEN %s AND %s"]
        params: list[Any] = [query.start_date, query.end_date]
        if query.platform:
            clauses.append("r.platform = %s")
            params.append(query.platform.value)
        if query.account_id:
            clauses.append("r.account_id = %s")
            params.append(query.account_id)
        sql = f"""
            SELECT MAX(r.finished_at) AS latest_success_at,
                   MAX(r.target_date) AS data_through_date,
                   SUM(CASE WHEN r.validation_status = %s THEN 1 ELSE 0 END) AS warning_runs
            FROM finance_sync_runs r
            INNER JOIN (
                SELECT platform, account_id, target_date, MAX(id) AS latest_run_id
                FROM finance_sync_runs
                WHERE status IN (%s, %s)
                GROUP BY platform, account_id, target_date
            ) latest ON latest.latest_run_id = r.id
            WHERE {' AND '.join(clauses)}
        """
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(
                sql,
                (
                    ValidationStatus.WARNING.value,
                    SyncStatus.SUCCESS.value,
                    SyncStatus.NO_DATA.value,
                    *params,
                ),
            )
            return _fetchone(cursor) or {}

    def get_summary(self, query: FinanceQuery) -> dict[str, Any]:
        clauses, params = self._entry_filters(query)
        where = " AND ".join(clauses)
        aggregate_sql = f"""
            SELECT
                COALESCE(SUM(t.income), 0) AS total_income,
                COALESCE(SUM(t.expense), 0) AS total_expense,
                COALESCE(SUM(t.income - t.expense), 0) AS net_change,
                COALESCE(SUM(CASE
                    WHEN fm.fee_level = %s AND fm.include_in_cost = 1 THEN t.expense
                    ELSE 0 END), 0) AS waybill_cost,
                COALESCE(SUM(CASE
                    WHEN fm.fee_level = %s AND fm.include_in_cost = 1 THEN t.expense
                    ELSE 0 END), 0) AS operating_cost,
                COUNT(DISTINCT CASE WHEN fm.id IS NULL OR fm.canonical_subject_id IS NULL THEN t.fee_item_id END) AS pending_fee_items,
                SUM(CASE WHEN visible_run.validation_status = %s THEN 1 ELSE 0 END) AS warning_rows
            {self._VISIBLE_ENTRY_FROM}
            WHERE {where}
        """
        account_sql = f"""
            SELECT t.platform, t.account_id, MAX(t.login_account) AS login_account,
                   COALESCE(SUM(t.income), 0) AS total_income,
                   COALESCE(SUM(t.expense), 0) AS total_expense,
                   COALESCE(SUM(CASE WHEN fm.fee_level = %s AND fm.include_in_cost = 1
                                THEN t.expense ELSE 0 END), 0) AS waybill_cost,
                   COALESCE(SUM(CASE WHEN fm.fee_level = %s AND fm.include_in_cost = 1
                                THEN t.expense ELSE 0 END), 0) AS operating_cost
                   ,COALESCE(SUM(CASE WHEN fm.canonical_subject_id IS NOT NULL
                                      AND fm.fee_level = 'waybill'
                                      AND COALESCE(TRIM(t.waybill_no), '') <> ''
                                THEN t.income - t.expense ELSE 0 END), 0) AS waybill_net
                   ,COALESCE(SUM(CASE WHEN fm.canonical_subject_id IS NOT NULL
                                      AND fm.fee_level = 'operating'
                                THEN t.income - t.expense ELSE 0 END), 0) AS operating_net
            {self._VISIBLE_ENTRY_FROM}
            WHERE {where}
            GROUP BY t.platform, t.account_id
            ORDER BY t.platform, t.account_id
        """
        presence_clauses = ["r.target_date BETWEEN %s AND %s"]
        presence_params: list[Any] = [query.start_date, query.end_date]
        if query.platform:
            presence_clauses.append("r.platform = %s")
            presence_params.append(query.platform.value)
        if query.account_id:
            presence_clauses.append("r.account_id = %s")
            presence_params.append(query.account_id)
        account_presence_sql = f"""
            SELECT r.platform, r.account_id,
                   MAX(COALESCE(r.login_account, '')) AS login_account
            FROM finance_sync_runs r
            INNER JOIN (
                SELECT platform, account_id, target_date, MAX(id) AS latest_run_id
                FROM finance_sync_runs
                WHERE status IN (%s, %s)
                GROUP BY platform, account_id, target_date
            ) latest ON latest.latest_run_id = r.id
            WHERE {' AND '.join(presence_clauses)}
            GROUP BY r.platform, r.account_id
            ORDER BY r.platform, r.account_id
        """
        aggregate_params = [
            FeeLevel.WAYBILL.value,
            FeeLevel.OPERATING.value,
            ValidationStatus.WARNING.value,
            *params,
        ]
        account_params = [FeeLevel.WAYBILL.value, FeeLevel.OPERATING.value, *params]
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(aggregate_sql, tuple(aggregate_params))
            row = _fetchone(cursor) or {}
            cursor.execute(account_sql, tuple(account_params))
            account_rows = _fetchall(cursor)
            cursor.execute(
                account_presence_sql,
                (
                    SyncStatus.SUCCESS.value,
                    SyncStatus.NO_DATA.value,
                    *presence_params,
                ),
            )
            account_presence_rows = _fetchall(cursor)
        failed_sources = self._failed_sources(query)
        freshness = self._freshness(query)
        if failed_sources:
            validation_status = ValidationStatus.FAILED.value
        elif int(freshness.get("warning_runs") or 0):
            validation_status = ValidationStatus.WARNING.value
        elif freshness.get("data_through_date"):
            validation_status = ValidationStatus.PASSED.value
        else:
            validation_status = "unavailable"
        account_rows_by_key = {
            (str(account_row["platform"]), str(account_row["account_id"])): dict(account_row)
            for account_row in account_rows
        }
        for presence_row in account_presence_rows:
            key = (str(presence_row["platform"]), str(presence_row["account_id"]))
            if key not in account_rows_by_key:
                account_rows_by_key[key] = {
                    "platform": key[0],
                    "account_id": key[1],
                    "login_account": str(presence_row.get("login_account") or ""),
                    "total_income": ZERO,
                    "total_expense": ZERO,
                    "waybill_cost": ZERO,
                    "operating_cost": ZERO,
                }
            elif not account_rows_by_key[key].get("login_account"):
                account_rows_by_key[key]["login_account"] = str(
                    presence_row.get("login_account") or ""
                )
        accounts = []
        for key in sorted(account_rows_by_key):
            account_row = account_rows_by_key[key]
            accounts.append(
                {
                    "platform": str(account_row["platform"]),
                    "account_id": str(account_row["account_id"]),
                    "login_account": str(account_row.get("login_account") or ""),
                    "total_income": self._format_aggregate(account_row.get("total_income")),
                    "total_expense": self._format_aggregate(account_row.get("total_expense")),
                    "waybill_cost": self._format_aggregate(account_row.get("waybill_cost")),
                    "operating_cost": self._format_aggregate(account_row.get("operating_cost")),
                    "waybill_net": self._format_aggregate(account_row.get("waybill_net")),
                    "operating_net": self._format_aggregate(account_row.get("operating_net")),
                }
            )
        result = {
            "total_income": self._format_aggregate(row.get("total_income")),
            "total_expense": self._format_aggregate(row.get("total_expense")),
            "net_change": self._format_aggregate(row.get("net_change")),
            "waybill_cost": self._format_aggregate(row.get("waybill_cost")),
            "operating_cost": self._format_aggregate(row.get("operating_cost")),
            "pending_fee_items": int(row.get("pending_fee_items") or 0),
            "latest_success_at": self._serialize_general_row(
                {"value": freshness.get("latest_success_at")}
            )["value"],
            "data_through_date": self._serialize_general_row(
                {"value": freshness.get("data_through_date")}
            )["value"],
            "validation_status": validation_status,
            "failed_sources": failed_sources,
            "accounts": accounts,
        }
        result.update(self.get_evolution_summary(query))
        return result

    def get_trend(self, query: FinanceQuery) -> list[dict[str, Any]]:
        clauses, params = self._entry_filters(query)
        sql = f"""
            SELECT t.business_date AS date,
                   COALESCE(SUM(t.income), 0) AS income,
                   COALESCE(SUM(t.expense), 0) AS expense,
                   COALESCE(SUM(t.income - t.expense), 0) AS net_change
            {self._VISIBLE_ENTRY_FROM}
            WHERE {' AND '.join(clauses)}
            GROUP BY t.business_date
            ORDER BY t.business_date
        """
        presence_clauses = ["r.target_date BETWEEN %s AND %s"]
        presence_params: list[Any] = [query.start_date, query.end_date]
        if query.platform:
            presence_clauses.append("r.platform = %s")
            presence_params.append(query.platform.value)
        if query.account_id:
            presence_clauses.append("r.account_id = %s")
            presence_params.append(query.account_id)
        date_presence_sql = f"""
            SELECT DISTINCT r.target_date AS date
            FROM finance_sync_runs r
            INNER JOIN (
                SELECT platform, account_id, target_date, MAX(id) AS latest_run_id
                FROM finance_sync_runs
                WHERE status IN (%s, %s)
                GROUP BY platform, account_id, target_date
            ) latest ON latest.latest_run_id = r.id
            WHERE {' AND '.join(presence_clauses)}
            ORDER BY r.target_date
        """
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(sql, tuple(params))
            rows = _fetchall(cursor)
            cursor.execute(
                date_presence_sql,
                (
                    SyncStatus.SUCCESS.value,
                    SyncStatus.NO_DATA.value,
                    *presence_params,
                ),
            )
            presence_rows = _fetchall(cursor)
        rows_by_date = {
            _date(row["date"], "date"): dict(row)
            for row in rows
        }
        for presence_row in presence_rows:
            present_date = _date(presence_row["date"], "date")
            rows_by_date.setdefault(
                present_date,
                {
                    "date": present_date,
                    "income": ZERO,
                    "expense": ZERO,
                    "net_change": ZERO,
                },
            )
        return [
            {
                "date": _date(row["date"], "date").isoformat(),
                "income": self._format_aggregate(row.get("income")),
                "expense": self._format_aggregate(row.get("expense")),
                "net_change": self._format_aggregate(row.get("net_change")),
            }
            for _, row in sorted(rows_by_date.items())
        ]

    def get_expense_ranking(
        self,
        query: FinanceQuery,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        safe_limit = int(limit)
        if safe_limit < 1 or safe_limit > 100:
            raise ValueError("limit must be between 1 and 100")
        clauses, params = self._entry_filters(query)
        clauses.append("t.expense > 0")
        sql = f"""
            SELECT t.raw_primary_fee_name AS primary_fee_name,
                   t.raw_secondary_fee_name AS secondary_fee_name,
                   COALESCE(fm.booking_fee_name, '') AS booking_fee_name,
                   COALESCE(fm.fee_level, '') AS fee_level,
                   COALESCE(SUM(t.expense), 0) AS expense
            {self._VISIBLE_ENTRY_FROM}
            WHERE {' AND '.join(clauses)}
            GROUP BY t.raw_primary_fee_name, t.raw_secondary_fee_name,
                     COALESCE(fm.booking_fee_name, ''), COALESCE(fm.fee_level, '')
            ORDER BY expense DESC, t.raw_primary_fee_name, t.raw_secondary_fee_name
            LIMIT %s
        """
        params.append(safe_limit)
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(sql, tuple(params))
            rows = _fetchall(cursor)
        return [
            {
                "fee_name": str(
                    row.get("secondary_fee_name") or row.get("primary_fee_name") or ""
                ),
                "primary_fee_name": str(row["primary_fee_name"]),
                "secondary_fee_name": str(row.get("secondary_fee_name") or ""),
                "booking_fee_name": str(row.get("booking_fee_name") or ""),
                "fee_level": str(row.get("fee_level") or ""),
                "direction": Direction.EXPENSE.value,
                "expense": self._format_aggregate(row.get("expense")),
            }
            for row in rows
        ]

    def list_entries(
        self,
        query: FinanceQuery,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        safe_limit = int(limit)
        safe_offset = int(offset)
        if safe_limit < 1 or safe_limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if safe_offset < 0:
            raise ValueError("offset cannot be negative")
        clauses, params = self._entry_filters(query)
        where = " AND ".join(clauses)
        count_sql = f"SELECT COUNT(*) AS total {self._VISIBLE_ENTRY_FROM} WHERE {where}"
        data_sql = f"""
            SELECT t.id, t.platform, t.account_id, t.login_account,
                   t.business_date, t.transaction_at, t.source_record_key,
                   t.waybill_no, t.raw_primary_fee_name AS primary_fee_name,
                   t.raw_secondary_fee_name AS secondary_fee_name, t.direction,
                   t.income, t.expense, (t.income - t.expense) AS net_change,
                   t.before_balance, t.after_balance,
                   COALESCE(fm.fee_level, '') AS fee_level,
                   CASE WHEN fm.id IS NULL THEN %s ELSE %s END AS mapping_status,
                   COALESCE(fm.booking_fee_name, '') AS booking_fee_name,
                   COALESCE(fm.include_in_cost, 0) AS include_in_cost,
                   t.source_reference, t.remark
            {self._VISIBLE_ENTRY_FROM}
            WHERE {where}
            ORDER BY t.business_date DESC, t.transaction_at DESC, t.id DESC
            LIMIT %s OFFSET %s
        """
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(count_sql, tuple(params))
            total = int((_fetchone(cursor) or {}).get("total") or 0)
            cursor.execute(
                data_sql,
                (
                    MappingStatus.PENDING.value,
                    MappingStatus.BOUND.value,
                    *params,
                    safe_limit,
                    safe_offset,
                ),
            )
            rows = _fetchall(cursor)
        items: list[dict[str, Any]] = []
        for row in rows:
            serialized = self._serialize_general_row(row)
            for field_name in (
                "income",
                "expense",
                "net_change",
                "before_balance",
                "after_balance",
            ):
                serialized[field_name] = (
                    None
                    if row.get(field_name) is None
                    else self._format_aggregate(row.get(field_name))
                )
            serialized["include_in_cost"] = bool(row.get("include_in_cost"))
            items.append(serialized)
        return {"items": items, "total": total, "limit": safe_limit, "offset": safe_offset}

    def list_fee_mappings(
        self,
        *,
        platform: Platform | str | None = None,
        effective_month: dt.date | str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        month = month_start(effective_month or dt.date.today())
        safe_limit = int(limit)
        safe_offset = int(offset)
        if safe_limit < 1 or safe_limit > 2000:
            raise ValueError("limit must be between 1 and 2000")
        if safe_offset < 0:
            raise ValueError("offset cannot be negative")
        clauses = ["1 = 1"]
        params: list[Any] = []
        if platform is not None:
            clauses.append("fi.platform = %s")
            params.append(Platform(platform).value)
        where = " AND ".join(clauses)
        mapping_join = """
            LEFT JOIN finance_fee_mappings fm ON fm.id = (
                SELECT fm2.id FROM finance_fee_mappings fm2
                WHERE fm2.fee_item_id = fi.id
                  AND fm2.superseded_at IS NULL
                  AND fm2.mapping_status = 'bound'
                  AND fm2.effective_start_month <= %s
                  AND (fm2.effective_end_month IS NULL OR fm2.effective_end_month >= %s)
                ORDER BY fm2.effective_start_month DESC, fm2.version_no DESC, fm2.id DESC
                LIMIT 1
            )
        """
        data_sql = f"""
            SELECT fi.id AS fee_item_id, fi.platform,
                   fi.raw_primary_fee_name AS primary_fee_name,
                   fi.raw_secondary_fee_name AS secondary_fee_name,
                   fi.direction, fi.first_seen_month, fi.last_seen_month,
                   fm.id AS mapping_id,
                   CASE WHEN fm.id IS NULL THEN %s ELSE %s END AS mapping_status,
                   fm.fee_level, fm.canonical_subject_id,
                   s.subject_code AS canonical_subject_code,
                   s.subject_name AS canonical_subject_name,
                   fm.booking_fee_name, fm.requires_waybill,
                   fm.effective_start_month, fm.effective_end_month,
                   COALESCE(fm.include_in_cost, 0) AS include_in_cost,
                   fm.version_no
            FROM finance_fee_items fi
            {mapping_join}
            LEFT JOIN finance_fee_subjects s ON s.id = fm.canonical_subject_id
            WHERE {where}
            ORDER BY (fm.id IS NULL) DESC, fi.platform,
                     fi.raw_primary_fee_name, fi.raw_secondary_fee_name, fi.direction
            LIMIT %s OFFSET %s
        """
        count_sql = f"SELECT COUNT(*) AS total FROM finance_fee_items fi WHERE {where}"
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(count_sql, tuple(params))
            total = int((_fetchone(cursor) or {}).get("total") or 0)
            cursor.execute(
                data_sql,
                (
                    MappingStatus.PENDING.value,
                    MappingStatus.BOUND.value,
                    month,
                    month,
                    *params,
                    safe_limit,
                    safe_offset,
                ),
            )
            rows = _fetchall(cursor)
        items = []
        for row in rows:
            item = self._serialize_general_row(row)
            item["include_in_cost"] = bool(row.get("include_in_cost"))
            item["requires_waybill"] = bool(row.get("requires_waybill"))
            for field_name in (
                "first_seen_month",
                "last_seen_month",
                "effective_start_month",
                "effective_end_month",
            ):
                value = item.get(field_name)
                if isinstance(value, str) and len(value) >= 7:
                    item[field_name] = value[:7]
            items.append(item)
        return {
            "items": items,
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
            "booking_fee_items": {
                Platform.RONGHUI.value: sorted(RONGHUI_BOOKING_FEE_ITEMS),
                Platform.YUNDA.value: sorted(YUNDA_BOOKING_FEE_ITEMS),
            },
            "fee_subjects": self.list_fee_subjects(platform=platform),
        }

    def list_sync_batches(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: SyncStatus | str | None = None,
    ) -> dict[str, Any]:
        safe_limit = int(limit)
        safe_offset = int(offset)
        if safe_limit < 1 or safe_limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if safe_offset < 0:
            raise ValueError("offset cannot be negative")
        clauses = ["1 = 1"]
        params: list[Any] = []
        if status is not None:
            clauses.append("b.status = %s")
            params.append(SyncStatus(status).value)
        where = " AND ".join(clauses)
        count_sql = f"SELECT COUNT(*) AS total FROM finance_sync_batches b WHERE {where}"
        data_sql = f"""
            SELECT b.id, b.trigger_type, b.requested_start_date, b.requested_end_date,
                   b.rescan_days, b.status, b.earliest_date_status, b.requested_by,
                   b.started_at, b.finished_at, b.error_code, b.error_message, b.created_at,
                   COUNT(r.id) AS total_runs,
                   SUM(CASE WHEN r.status IN (%s, %s) THEN 1 ELSE 0 END) AS success_runs,
                   SUM(CASE WHEN r.status = %s THEN 1 ELSE 0 END) AS failed_runs
            FROM finance_sync_batches b
            LEFT JOIN finance_sync_runs r ON r.batch_id = b.id
            WHERE {where}
            GROUP BY b.id, b.trigger_type, b.requested_start_date, b.requested_end_date,
                     b.rescan_days, b.status, b.earliest_date_status, b.requested_by,
                     b.started_at, b.finished_at, b.error_code, b.error_message, b.created_at
            ORDER BY b.id DESC
            LIMIT %s OFFSET %s
        """
        with self._connection() as connection, _managed_cursor(connection) as cursor:
            cursor.execute(count_sql, tuple(params))
            total = int((_fetchone(cursor) or {}).get("total") or 0)
            cursor.execute(
                data_sql,
                (
                    SyncStatus.SUCCESS.value,
                    SyncStatus.NO_DATA.value,
                    SyncStatus.FAILED.value,
                    *params,
                    safe_limit,
                    safe_offset,
                ),
            )
            items = [self._serialize_general_row(row) for row in _fetchall(cursor)]
            failed_rows: list[dict[str, Any]] = []
            if items:
                batch_ids = [int(item["id"]) for item in items]
                placeholders = ", ".join(["%s"] * len(batch_ids))
                cursor.execute(
                    f"""
                    SELECT r.batch_id, r.platform, r.account_id, r.target_date,
                           r.error_code, r.error_message
                    FROM finance_sync_runs r
                    INNER JOIN (
                        SELECT batch_id, platform, account_id, target_date,
                               MAX(id) AS latest_run_id
                        FROM finance_sync_runs
                        WHERE batch_id IN ({placeholders})
                        GROUP BY batch_id, platform, account_id, target_date
                    ) latest ON latest.latest_run_id = r.id
                    WHERE r.status = %s
                    ORDER BY r.batch_id DESC, r.target_date, r.platform, r.account_id
                    """,
                    (*batch_ids, SyncStatus.FAILED.value),
                )
                failed_rows = [
                    self._serialize_general_row(row)
                    for row in _fetchall(cursor)
                ]
        failed_by_batch: dict[int, list[dict[str, Any]]] = {}
        for row in failed_rows:
            failed_by_batch.setdefault(int(row.pop("batch_id")), []).append(row)
        for item in items:
            for key in ("total_runs", "success_runs", "failed_runs"):
                item[key] = int(item.get(key) or 0)
            item["failed_sources"] = failed_by_batch.get(int(item["id"]), [])
        return {"items": items, "total": total, "limit": safe_limit, "offset": safe_offset}
