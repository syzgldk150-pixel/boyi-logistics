"""Pre-commit reconciliation for immutable finance snapshots."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from shared.finance.models import (
    FeeItemKey,
    SummarySnapshot,
    TransactionRecord,
    ValidationStatus,
)
from shared.finance.money import ZERO, quantize_storage


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "count": self.count}


@dataclass(frozen=True)
class ValidationReport:
    status: ValidationStatus
    metrics: Mapping[str, Any]
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is not ValidationStatus.FAILED

    def to_dict(self) -> dict[str, Any]:
        def normalize(value: Any) -> Any:
            if isinstance(value, Decimal):
                return f"{value:.4f}"
            if isinstance(value, dict):
                return {str(key): normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            return value

        return {
            "status": self.status.value,
            "metrics": normalize(dict(self.metrics)),
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
        }


@dataclass(frozen=True)
class CaptureEvidence:
    remote_total: int
    page_row_counts: Sequence[int]
    transactions: Sequence[TransactionRecord]
    summaries: Sequence[SummarySnapshot]
    intended_write_count: int | None = None
    response_valid: bool = True
    known_fee_items: Iterable[FeeItemKey] = field(default_factory=tuple)
    previous_record_payloads: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    extreme_abs_threshold: Decimal | str | None = None


def _aggregate_transactions(
    rows: Iterable[TransactionRecord],
) -> dict[tuple[str, str, str], tuple[Decimal, Decimal]]:
    totals: dict[tuple[str, str, str], list[Decimal]] = defaultdict(
        lambda: [ZERO, ZERO]
    )
    for row in rows:
        key = (row.primary_fee_name, row.secondary_fee_name, row.direction.value)
        totals[key][0] += row.income
        totals[key][1] += row.expense
    return {key: (values[0], values[1]) for key, values in totals.items()}


def _aggregate_summaries(
    rows: Iterable[SummarySnapshot],
) -> dict[tuple[str, str, str], tuple[Decimal, Decimal]]:
    totals: dict[tuple[str, str, str], list[Decimal]] = defaultdict(
        lambda: [ZERO, ZERO]
    )
    for row in rows:
        totals[row.grouping_key][0] += row.income
        totals[row.grouping_key][1] += row.expense
    return {key: (values[0], values[1]) for key, values in totals.items()}


def validate_finance_capture(evidence: CaptureEvidence) -> ValidationReport:
    """Validate a platform/account/date capture before it may be committed.

    Adapters must provide normalized non-negative income and expense amounts.
    A remote total of zero is treated as valid no-data evidence only when the
    response itself was valid and all detail/summary pages are explicitly empty.
    """

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    transactions = tuple(evidence.transactions)
    summaries = tuple(evidence.summaries)

    try:
        remote_total = int(evidence.remote_total)
    except (TypeError, ValueError):
        remote_total = -1
    page_counts: list[int] = []
    try:
        page_counts = [int(value) for value in evidence.page_row_counts]
    except (TypeError, ValueError):
        errors.append(ValidationIssue("INVALID_PAGE_COUNTS", "page row counts are invalid"))

    if not evidence.response_valid:
        errors.append(
            ValidationIssue(
                "INVALID_REMOTE_RESPONSE",
                "remote response was empty, non-JSON, a login page, or failed schema validation",
            )
        )
    if remote_total < 0:
        errors.append(ValidationIssue("INVALID_REMOTE_TOTAL", "remote total is missing or negative"))
    if any(value < 0 for value in page_counts):
        errors.append(ValidationIssue("INVALID_PAGE_COUNTS", "page row counts cannot be negative"))

    paged_rows = sum(page_counts)
    if remote_total >= 0 and remote_total != len(transactions):
        errors.append(
            ValidationIssue(
                "REMOTE_UNIQUE_COUNT_MISMATCH",
                "remote total does not equal the deduplicated detail row count",
            )
        )
    if paged_rows < len(transactions):
        errors.append(
            ValidationIssue(
                "PAGE_DETAIL_COUNT_MISMATCH",
                "raw page row counts are lower than parsed detail rows",
            )
        )
    duplicate_page_rows = max(0, paged_rows - len(transactions))
    if duplicate_page_rows:
        warnings.append(
            ValidationIssue(
                "PAGINATION_OVERLAP",
                "raw pages overlapped; stable source keys were deduplicated before commit",
                duplicate_page_rows,
            )
        )
    if remote_total == 0 and (transactions or summaries or any(page_counts)):
        errors.append(
            ValidationIssue(
                "INVALID_NO_DATA_EVIDENCE",
                "remote total zero may only accompany empty detail and summary rows",
            )
        )

    keys = [row.source_record_key for row in transactions]
    key_counts = Counter(keys)
    duplicates = {key for key, count in key_counts.items() if count > 1}
    if duplicates:
        errors.append(
            ValidationIssue(
                "DUPLICATE_SOURCE_KEY",
                "stable source keys are duplicated in the captured pages",
                len(duplicates),
            )
        )
    conflicting_keys = 0
    by_key: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    for row in transactions:
        by_key[row.source_record_key].add(row.comparison_payload())
    conflicting_keys = sum(1 for payloads in by_key.values() if len(payloads) > 1)
    if conflicting_keys:
        errors.append(
            ValidationIssue(
                "SAME_KEY_CONTENT_CONFLICT",
                "the same stable source key has different content in one capture",
                conflicting_keys,
            )
        )

    unique_rows = len(key_counts)
    intended_write_count = (
        unique_rows
        if evidence.intended_write_count is None
        else int(evidence.intended_write_count)
    )
    if intended_write_count != unique_rows:
        errors.append(
            ValidationIssue(
                "INTENDED_WRITE_COUNT_MISMATCH",
                "intended write count does not equal unique source row count",
            )
        )

    detail_by_fee = _aggregate_transactions(transactions)
    summary_by_fee = _aggregate_summaries(summaries)
    if remote_total > 0 and not summaries:
        errors.append(
            ValidationIssue(
                "SUMMARY_MISSING",
                "non-empty detail capture has no platform summary evidence",
            )
        )
    fee_mismatches = 0
    for key in set(detail_by_fee) | set(summary_by_fee):
        if detail_by_fee.get(key, (ZERO, ZERO)) != summary_by_fee.get(key, (ZERO, ZERO)):
            fee_mismatches += 1
    if fee_mismatches:
        errors.append(
            ValidationIssue(
                "FEE_SUMMARY_MISMATCH",
                "detail and platform summary differ by fee item and direction",
                fee_mismatches,
            )
        )

    detail_income = sum((row.income for row in transactions), ZERO)
    detail_expense = sum((row.expense for row in transactions), ZERO)
    summary_income = sum((row.income for row in summaries), ZERO)
    summary_expense = sum((row.expense for row in summaries), ZERO)
    if (detail_income, detail_expense) != (summary_income, summary_expense):
        errors.append(
            ValidationIssue(
                "TOTAL_AMOUNT_MISMATCH",
                "detail income/expense/net does not equal platform summary",
            )
        )

    missing_balance_pairs = 0
    balance_equation_errors = 0
    missing_transaction_time = 0
    for row in transactions:
        if row.transaction_at is None:
            missing_transaction_time += 1
        if (row.before_balance is None) != (row.after_balance is None):
            missing_balance_pairs += 1
        elif row.before_balance is not None and row.after_balance is not None:
            if quantize_storage(row.before_balance + row.net_change) != row.after_balance:
                balance_equation_errors += 1
    if missing_transaction_time:
        errors.append(
            ValidationIssue(
                "TRANSACTION_TIME_MISSING",
                "transaction time is required for balance-chain validation",
                missing_transaction_time,
            )
        )
    if missing_balance_pairs:
        errors.append(
            ValidationIssue(
                "BALANCE_FIELD_INCOMPLETE",
                "before and after balance must either both exist or both be absent",
                missing_balance_pairs,
            )
        )
    if balance_equation_errors:
        errors.append(
            ValidationIssue(
                "BALANCE_EQUATION_MISMATCH",
                "before balance plus net change does not equal after balance",
                balance_equation_errors,
            )
        )

    chain_rows = [
        row
        for row in transactions
        if row.transaction_at is not None
        and row.before_balance is not None
        and row.after_balance is not None
    ]
    ordering_keys = Counter((row.transaction_at, row.source_reference) for row in chain_rows)
    ambiguous_order = sum(
        1
        for (transaction_at, source_reference), count in ordering_keys.items()
        if count > 1 or not source_reference
    )
    if ambiguous_order and len(chain_rows) > 1:
        errors.append(
            ValidationIssue(
                "BALANCE_ORDER_AMBIGUOUS",
                "balance sequence requires unique transaction time and source order references",
                ambiguous_order,
            )
        )
    elif len(chain_rows) > 1:
        ordered = sorted(chain_rows, key=lambda row: (row.transaction_at, row.source_reference))
        chain_breaks = sum(
            1
            for previous, current in zip(ordered, ordered[1:])
            if previous.after_balance != current.before_balance
        )
        if chain_breaks:
            errors.append(
                ValidationIssue(
                    "BALANCE_CHAIN_MISMATCH",
                    "one transaction's after balance does not equal the next before balance",
                    chain_breaks,
                )
            )

    known_fee_items = set(evidence.known_fee_items)
    observed_fee_items = {row.fee_key for row in transactions}
    new_fee_items = observed_fee_items - known_fee_items
    if known_fee_items and new_fee_items:
        warnings.append(
            ValidationIssue(
                "NEW_FEE_ITEM",
                "new exact fee names will enter pending mapping review",
                len(new_fee_items),
            )
        )

    historical_revisions = sum(
        1
        for row in transactions
        if row.source_record_key in evidence.previous_record_payloads
        and evidence.previous_record_payloads[row.source_record_key]
        != row.comparison_payload()
    )
    if historical_revisions:
        warnings.append(
            ValidationIssue(
                "HISTORICAL_REVISION",
                "stable source keys changed content compared with the latest successful snapshot",
                historical_revisions,
            )
        )

    amounts = [row.income - row.expense for row in transactions]
    minimum_amount = min(amounts) if amounts else ZERO
    maximum_amount = max(amounts) if amounts else ZERO
    maximum_absolute_amount = max((abs(value) for value in amounts), default=ZERO)
    if evidence.extreme_abs_threshold is not None:
        threshold = quantize_storage(evidence.extreme_abs_threshold)
        extreme_count = sum(1 for value in amounts if abs(value) > threshold)
        if extreme_count:
            warnings.append(
                ValidationIssue(
                    "AMOUNT_EXTREME",
                    "captured amount exceeds the explicitly configured review threshold",
                    extreme_count,
                )
            )

    status = (
        ValidationStatus.FAILED
        if errors
        else ValidationStatus.WARNING
        if warnings
        else ValidationStatus.PASSED
    )
    return ValidationReport(
        status=status,
        metrics={
            "remote_total": remote_total,
            "page_row_count": paged_rows,
            "parsed_row_count": len(transactions),
            "duplicate_page_row_count": duplicate_page_rows,
            "unique_row_count": unique_rows,
            "intended_write_count": intended_write_count,
            "duplicate_source_key_count": len(duplicates),
            "same_key_conflict_count": conflicting_keys,
            "detail_income": detail_income,
            "detail_expense": detail_expense,
            "detail_net_change": detail_income - detail_expense,
            "summary_income": summary_income,
            "summary_expense": summary_expense,
            "summary_net_change": summary_income - summary_expense,
            "new_fee_item_count": len(new_fee_items),
            "historical_revision_count": historical_revisions,
            "minimum_net_amount": minimum_amount,
            "maximum_net_amount": maximum_amount,
            "maximum_absolute_amount": maximum_absolute_amount,
            "eligible_no_data": bool(
                evidence.response_valid
                and remote_total == 0
                and not transactions
                and not summaries
                and not errors
            ),
        },
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
