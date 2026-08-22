"""Account-blind, package-owned finance synchronization orchestration.

The signed package owns request planning, the explicit three-source fan-out,
pagination, Decimal reconciliation, immutable-snapshot commit order and result
evidence.  Core calls are deliberately narrow capture, independent verification
and ledger primitives; this module never imports the legacy service or tool.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
)


ACTION_ID = "sync_finance_bills"
_SOURCE_ROLES = (
    "finance_quote_source",
    "finance_daxiang_s_source",
    "finance_self_pickup_source",
)
_COORDINATOR_ROLE = _SOURCE_ROLES[0]
_ALLOWED_ARGUMENTS = frozenset(
    {
        "mode",
        "target_date",
        "start_date",
        "end_date",
        "platform",
        "batch_id",
        "rescan_days",
        "_startup_catchup",
    }
)
_BROKER_OWNED_MARKERS = (
    "password",
    "cookie",
    "credential",
    "secret",
    "token",
    "session",
)
_STORAGE_SCALE = Decimal("0.0001")
_ZERO = Decimal("0.0000")
_DEFAULT_RESCAN_DAYS = 7
_PAGE_SIZE = 100
_MAX_PAGES = 200
_MAX_BROKER_CALLS = 768
_MAX_TARGETS = (_MAX_BROKER_CALLS - 2) // 3
_EARLIEST_DATE_UNCONFIRMED = "EARLIEST_DATE_UNCONFIRMED"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_TRANSACTION_FIELDS = frozenset(
    {
        "source_record_key",
        "business_date",
        "transaction_at",
        "primary_fee_name",
        "secondary_fee_name",
        "income",
        "expense",
        "before_balance",
        "after_balance",
        "waybill_no",
        "source_reference",
        "remark",
        "source_payload",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "target_date",
        "primary_fee_name",
        "secondary_fee_name",
        "income",
        "expense",
    }
)
_CAPTURE_RESPONSE_FIELDS = frozenset(
    {
        "schema_version",
        "capture_ref",
        "source_context_ref",
        "page_number",
        "page_row_count",
        "source_total",
        "items",
        "pagination_complete",
        "next_page_number",
        "evidence_ref",
    }
)
_VERIFY_RESPONSE_FIELDS = frozenset(
    {
        "schema_version",
        "verified",
        "capture_ref",
        "source_context_ref",
        "capture_sha256",
        "remote_total",
        "summary_semantics",
        "summaries",
        "observed_metrics",
        "evidence_ref",
    }
)
_OBSERVED_METRIC_FIELDS = frozenset(
    {
        "transaction_count",
        "detail_income",
        "detail_expense",
        "detail_net_change",
        "minimum_net_amount",
        "maximum_net_amount",
        "maximum_absolute_amount",
    }
)
_SNAPSHOT_RESPONSE_FIELDS = frozenset(
    {
        "schema_version",
        "committed",
        "batch_id",
        "outcome",
        "record_count",
        "summary_count",
        "written_row_count",
        "run_ref",
        "validation_sha256",
        "new_fee_item_count",
        "historical_revision_count",
        "evidence_ref",
    }
)


class FinanceActionError(ValueError):
    """Safe, account-blind failure retained in the finance batch ledger."""

    def __init__(self, code: str, stage: str) -> None:
        super().__init__(str(code or "FINANCE_ACTION_FAILED"))
        self.code = str(code or "FINANCE_ACTION_FAILED")[:64]
        self.stage = str(stage or "unknown")[:64]


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise FinanceActionError("FIELD_DRIFT", label)
    return dict(value)


def _exact_object(
    value: object,
    expected_fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    result = _object(value, label)
    if set(result) != expected_fields:
        raise FinanceActionError("FIELD_DRIFT", label)
    return result


def _assert_account_blind(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            if (
                key in {
                    "account_id",
                    "account_ids",
                    "login_account",
                    "username",
                }
                or key.endswith(("_account_id", "_account_ids"))
                or any(marker in key for marker in _BROKER_OWNED_MARKERS)
            ):
                raise ValueError("finance JSON contains a broker-owned field")
            _assert_account_blind(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_account_blind(nested)


def _required_text(value: object, label: str, *, maximum: int = 2048) -> str:
    if not isinstance(value, str):
        raise FinanceActionError("FIELD_DRIFT", label)
    result = value.strip()
    if not result or len(result) > maximum:
        raise FinanceActionError("FIELD_DRIFT", label)
    return result


def _optional_text(value: object, label: str, *, maximum: int = 10000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FinanceActionError("FIELD_DRIFT", label)
    result = value.strip()
    if len(result) > maximum:
        raise FinanceActionError("FIELD_DRIFT", label)
    return result


def _integer(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FinanceActionError("FIELD_DRIFT", label)
    if value < minimum or (maximum is not None and value > maximum):
        raise FinanceActionError("FIELD_DRIFT", label)
    return value


def _input_positive_integer(value: object, label: str, *, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if str(result) != str(value).strip() or result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _decimal(value: object, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise FinanceActionError("AMOUNT_MISSING", label)
    text = str(value).strip().replace(",", "")
    if not text:
        raise FinanceActionError("AMOUNT_MISSING", label)
    try:
        result = Decimal(str(text))
    except (InvalidOperation, ValueError) as exc:
        raise FinanceActionError("AMOUNT_INVALID", label) from exc
    if not result.is_finite():
        raise FinanceActionError("AMOUNT_INVALID", label)
    return result.quantize(_STORAGE_SCALE, rounding=ROUND_HALF_UP)


def _amount_text(value: Decimal) -> str:
    return f"{value.quantize(_STORAGE_SCALE, rounding=ROUND_HALF_UP):.4f}"


def _date_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise FinanceActionError("DATE_INVALID", label)
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise FinanceActionError("DATE_INVALID", label) from exc
    if parsed.isoformat() != text:
        raise FinanceActionError("DATE_INVALID", label)
    return text


def _input_date(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc


def _datetime_text(value: object, target_date: str) -> str:
    text = _required_text(value, "transaction_at", maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinanceActionError("TRANSACTION_TIME_INVALID", "normalize") from exc
    if parsed.date().isoformat() != target_date:
        raise FinanceActionError("TRANSACTION_DATE_MISMATCH", "normalize")
    return parsed.isoformat()


def _sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FinanceActionError("CANONICAL_JSON_INVALID", "canonicalize") from exc
    return hashlib.sha256(payload).hexdigest()


def _month_chunks(start: date, end: date) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    cursor = start
    while cursor <= end:
        month_end = date(
            cursor.year,
            cursor.month,
            calendar.monthrange(cursor.year, cursor.month)[1],
        )
        chunk_end = min(month_end, end)
        result.append(
            {
                "start_date": cursor.isoformat(),
                "end_date": chunk_end.isoformat(),
            }
        )
        cursor = chunk_end + timedelta(days=1)
    return result


def _plan_request(arguments: Mapping[str, object]) -> tuple[dict[str, object], str]:
    values = dict(arguments)
    _assert_account_blind(values)
    unknown = set(values) - _ALLOWED_ARGUMENTS
    if unknown:
        raise ValueError("finance arguments contain undeclared fields")
    mode = str(values.get("mode") or "sync").strip().lower()
    if mode not in {"sync", "backfill", "retry"}:
        raise ValueError("mode must be sync, backfill or retry")
    platform = str(values.get("platform") or "").strip().lower()
    if platform not in {"", "ronghui"}:
        raise ValueError("only the signed Ronghui finance sources are enabled")
    startup_catchup = values.get("_startup_catchup", False)
    if not isinstance(startup_catchup, bool):
        raise ValueError("_startup_catchup must be boolean")

    if mode == "retry":
        forbidden = (
            "target_date",
            "start_date",
            "end_date",
            "platform",
            "rescan_days",
        )
        if any(values.get(name) not in (None, "") for name in forbidden):
            raise ValueError("retry accepts only batch_id")
        if startup_catchup:
            raise ValueError("retry cannot be a startup catch-up")
        retry_batch_id = _input_positive_integer(
            values.get("batch_id"),
            "batch_id",
            default=0,
        )
        contract: dict[str, object] = {
            "mode": mode,
            "trigger_type": "retry",
            "start_date": None,
            "end_date": None,
            "rescan_days": _DEFAULT_RESCAN_DAYS,
            "earliest_date_status": None,
            "startup_catchup": False,
            "retry_batch_id": retry_batch_id,
            "source_roles": list(_SOURCE_ROLES),
            "requested_targets": [],
            "month_chunks": [],
            "max_targets": _MAX_TARGETS,
        }
        return contract, _sha256(contract)

    if values.get("batch_id") not in (None, ""):
        raise ValueError("batch_id is only valid in retry mode")
    rescan_days = _input_positive_integer(
        values.get("rescan_days"),
        "rescan_days",
        default=_DEFAULT_RESCAN_DAYS,
    )
    raw_start = values.get("start_date")
    raw_end = values.get("end_date")
    raw_target = values.get("target_date")
    if mode == "backfill":
        if raw_start in (None, "") or raw_end in (None, ""):
            raise ValueError("backfill requires start_date and end_date")
        if raw_target not in (None, ""):
            raise ValueError("backfill does not accept target_date")
        start = _input_date(raw_start, "start_date")
        end = _input_date(raw_end, "end_date")
    elif raw_start not in (None, "") or raw_end not in (None, ""):
        if raw_start in (None, "") or raw_end in (None, ""):
            raise ValueError("range sync requires start_date and end_date")
        if raw_target not in (None, ""):
            raise ValueError("range sync does not accept target_date")
        start = _input_date(raw_start, "start_date")
        end = _input_date(raw_end, "end_date")
    else:
        end = (
            _input_date(raw_target, "target_date")
            if raw_target not in (None, "")
            else datetime.now(_SHANGHAI).date() - timedelta(days=1)
        )
        start = end - timedelta(days=rescan_days - 1)
    if start > end:
        raise ValueError("start_date cannot be after end_date")
    dates = [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]
    requested_targets = [
        {"source_role": role, "target_date": target.isoformat()}
        for role in _SOURCE_ROLES
        for target in dates
    ]
    if len(requested_targets) > _MAX_TARGETS:
        raise ValueError("finance request exceeds the signed broker call budget")
    contract = {
        "mode": mode,
        "trigger_type": "startup" if startup_catchup else mode,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "rescan_days": rescan_days,
        "earliest_date_status": (
            _EARLIEST_DATE_UNCONFIRMED if mode == "backfill" else None
        ),
        "startup_catchup": startup_catchup,
        "retry_batch_id": None,
        "source_roles": list(_SOURCE_ROLES),
        "requested_targets": requested_targets,
        "month_chunks": _month_chunks(start, end),
        "max_targets": _MAX_TARGETS,
    }
    return contract, _sha256(contract)


class _BudgetedBroker:
    def __init__(self, broker: Callable[..., object]) -> None:
        self._broker = broker
        self.calls = 0

    def call(
        self,
        operation: str,
        *,
        action: str,
        role: str,
        arguments: Mapping[str, object],
        reserve: int = 0,
    ) -> object:
        if self.calls + 1 + reserve > _MAX_BROKER_CALLS:
            raise FinanceActionError("BROKER_CALL_BUDGET_EXHAUSTED", action)
        request = dict(arguments)
        _assert_account_blind(request)
        self.calls += 1
        result = self._broker(
            operation,
            action=action,
            role=role,
            arguments=request,
        )
        _assert_account_blind(result)
        return result


def _target(value: object) -> dict[str, str]:
    row = _exact_object(
        value,
        frozenset({"source_role", "target_date"}),
        "batch_target",
    )
    role = _required_text(row.get("source_role"), "source_role", maximum=64)
    if role not in _SOURCE_ROLES:
        raise FinanceActionError("ACCOUNT_ROLE_MISMATCH", "batch_target")
    return {
        "source_role": role,
        "target_date": _date_text(row.get("target_date"), "target_date"),
    }


def _acquire_batch(
    contract: Mapping[str, object],
    contract_sha256: str,
    broker: _BudgetedBroker,
) -> tuple[int, list[dict[str, str]], int, str]:
    try:
        raw = broker.call(
            "ledger.invoke",
            action="finance.batch.acquire",
            role=_COORDINATOR_ROLE,
            arguments={
                "schema_version": 1,
                "contract": dict(contract),
                "contract_sha256": contract_sha256,
            },
        )
    except FinanceActionError:
        raise
    except Exception as exc:
        raise FinanceActionError("BATCH_ACQUIRE_FAILED", "batch_acquire") from exc
    result = _exact_object(
        raw,
        frozenset(
            {
                "schema_version",
                "acquired",
                "batch_id",
                "contract_sha256",
                "targets",
                "skipped_disabled_count",
                "evidence_ref",
            }
        ),
        "batch_acquire",
    )
    if result.get("schema_version") != 1 or result.get("acquired") is not True:
        raise FinanceActionError("BATCH_ACQUIRE_FAILED", "batch_acquire")
    if result.get("contract_sha256") != contract_sha256:
        raise FinanceActionError("BATCH_CONTRACT_MISMATCH", "batch_acquire")
    batch_id = _integer(result.get("batch_id"), "batch_id", minimum=1)
    target_values = result.get("targets")
    if not isinstance(target_values, list):
        raise FinanceActionError("FIELD_DRIFT", "batch_targets")
    targets = [_target(value) for value in target_values]
    identities = [(row["source_role"], row["target_date"]) for row in targets]
    if len(identities) != len(set(identities)):
        raise FinanceActionError("DUPLICATE_BATCH_TARGET", "batch_targets")
    if len(targets) > _MAX_TARGETS:
        raise FinanceActionError("BROKER_CALL_BUDGET_EXHAUSTED", "batch_targets")
    requested = list(contract.get("requested_targets") or [])
    requested_identities = [
        (str(row["source_role"]), str(row["target_date"]))
        for row in requested
        if isinstance(row, Mapping)
    ]
    mode = str(contract.get("mode"))
    if mode == "retry":
        if not targets:
            raise FinanceActionError("RETRY_TARGETS_EMPTY", "batch_targets")
    elif bool(contract.get("startup_catchup")):
        if not set(identities).issubset(set(requested_identities)):
            raise FinanceActionError("BATCH_TARGET_DRIFT", "batch_targets")
    elif identities != requested_identities:
        raise FinanceActionError("BATCH_TARGET_DRIFT", "batch_targets")
    order = {role: index for index, role in enumerate(_SOURCE_ROLES)}
    targets.sort(key=lambda row: (order[row["source_role"]], row["target_date"]))
    skipped_disabled_count = _integer(
        result.get("skipped_disabled_count"),
        "skipped_disabled_count",
    )
    evidence_ref = broker_evidence_ref(result, "finance batch acquire")
    return batch_id, targets, skipped_disabled_count, evidence_ref


def _normalize_transaction(value: object, target_date: str) -> dict[str, object]:
    row = _exact_object(value, _TRANSACTION_FIELDS, "finance_transaction")
    business_date = _date_text(row.get("business_date"), "business_date")
    if business_date != target_date:
        raise FinanceActionError("TRANSACTION_DATE_MISMATCH", "normalize")
    income = _decimal(row.get("income"), "income")
    expense = _decimal(row.get("expense"), "expense")
    if income < _ZERO or expense < _ZERO or (income == _ZERO) == (expense == _ZERO):
        raise FinanceActionError("AMOUNT_DIRECTION_INVALID", "normalize")
    before = _decimal(row.get("before_balance"), "before_balance")
    after = _decimal(row.get("after_balance"), "after_balance")
    net_change = income - expense
    if (before + net_change).quantize(
        _STORAGE_SCALE,
        rounding=ROUND_HALF_UP,
    ) != after:
        raise FinanceActionError("BALANCE_EQUATION_MISMATCH", "inverse_check")
    source_payload = _object(row.get("source_payload"), "source_payload")
    _assert_account_blind(source_payload)
    return {
        "source_record_key": _required_text(
            row.get("source_record_key"),
            "source_record_key",
            maximum=512,
        ),
        "business_date": business_date,
        "transaction_at": _datetime_text(row.get("transaction_at"), target_date),
        "primary_fee_name": _required_text(
            row.get("primary_fee_name"),
            "primary_fee_name",
            maximum=512,
        ),
        "secondary_fee_name": _optional_text(
            row.get("secondary_fee_name"),
            "secondary_fee_name",
            maximum=512,
        ),
        "direction": "income" if income > _ZERO else "expense",
        "income": _amount_text(income),
        "expense": _amount_text(expense),
        "before_balance": _amount_text(before),
        "after_balance": _amount_text(after),
        "waybill_no": _optional_text(
            row.get("waybill_no"),
            "waybill_no",
            maximum=512,
        ),
        "source_reference": _optional_text(
            row.get("source_reference"),
            "source_reference",
            maximum=512,
        ),
        "remark": _optional_text(row.get("remark"), "remark"),
        "source_payload": source_payload,
    }


def _normalize_summary(value: object, target_date: str) -> dict[str, str]:
    row = _exact_object(value, _SUMMARY_FIELDS, "finance_summary")
    observed_date = _date_text(row.get("target_date"), "summary_target_date")
    if observed_date != target_date:
        raise FinanceActionError("SUMMARY_DATE_MISMATCH", "summary_reconcile")
    income = _decimal(row.get("income"), "summary_income")
    expense = _decimal(row.get("expense"), "summary_expense")
    if income < _ZERO or expense < _ZERO or (income == _ZERO) == (expense == _ZERO):
        raise FinanceActionError(
            "AMOUNT_DIRECTION_INVALID",
            "summary_reconcile",
        )
    return {
        "target_date": observed_date,
        "primary_fee_name": _required_text(
            row.get("primary_fee_name"),
            "summary_primary_fee_name",
            maximum=512,
        ),
        "secondary_fee_name": _optional_text(
            row.get("secondary_fee_name"),
            "summary_secondary_fee_name",
            maximum=512,
        ),
        "direction": "income" if income > _ZERO else "expense",
        "income": _amount_text(income),
        "expense": _amount_text(expense),
    }


def _detail_metrics(transactions: list[dict[str, object]]) -> dict[str, object]:
    amounts = [
        _decimal(row["income"], "income") - _decimal(row["expense"], "expense")
        for row in transactions
    ]
    detail_income = sum(
        (_decimal(row["income"], "income") for row in transactions),
        _ZERO,
    )
    detail_expense = sum(
        (_decimal(row["expense"], "expense") for row in transactions),
        _ZERO,
    )
    return {
        "transaction_count": len(transactions),
        "detail_income": _amount_text(detail_income),
        "detail_expense": _amount_text(detail_expense),
        "detail_net_change": _amount_text(detail_income - detail_expense),
        "minimum_net_amount": _amount_text(min(amounts, default=_ZERO)),
        "maximum_net_amount": _amount_text(max(amounts, default=_ZERO)),
        "maximum_absolute_amount": _amount_text(
            max((abs(value) for value in amounts), default=_ZERO)
        ),
    }


def _validate_balance_chain(transactions: list[dict[str, object]]) -> int:
    if len(transactions) <= 1:
        return 0
    keys = [
        (str(row["transaction_at"]), str(row["source_reference"]))
        for row in transactions
    ]
    counts = Counter(keys)
    if any(not reference or count > 1 for (_timestamp, reference), count in counts.items()):
        raise FinanceActionError("BALANCE_ORDER_AMBIGUOUS", "balance_chain")
    ordered = sorted(
        transactions,
        key=lambda row: (str(row["transaction_at"]), str(row["source_reference"])),
    )
    for previous, current in zip(ordered, ordered[1:]):
        if _decimal(previous["after_balance"], "after_balance") != _decimal(
            current["before_balance"],
            "before_balance",
        ):
            raise FinanceActionError("BALANCE_CHAIN_MISMATCH", "balance_chain")
    return len(ordered) - 1


def _reconcile_summaries(
    transactions: list[dict[str, object]],
    summaries: list[dict[str, str]],
) -> tuple[dict[str, str], int]:
    detail_by_fee: dict[tuple[str, str], Decimal] = defaultdict(lambda: _ZERO)
    for row in transactions:
        key = (str(row["primary_fee_name"]), str(row["secondary_fee_name"]))
        detail_by_fee[key] += _decimal(row["income"], "income") - _decimal(
            row["expense"],
            "expense",
        )
    summary_by_fee: dict[tuple[str, str], Decimal] = defaultdict(lambda: _ZERO)
    for row in summaries:
        key = (row["primary_fee_name"], row["secondary_fee_name"])
        summary_by_fee[key] += _decimal(row["income"], "summary_income") - _decimal(
            row["expense"],
            "summary_expense",
        )
    if transactions and any(value != _ZERO for value in detail_by_fee.values()) and not summaries:
        raise FinanceActionError("SUMMARY_MISSING", "summary_reconcile")
    mismatches = sum(
        1
        for key in set(detail_by_fee) | set(summary_by_fee)
        if detail_by_fee.get(key, _ZERO) != summary_by_fee.get(key, _ZERO)
    )
    if mismatches:
        raise FinanceActionError("FEE_SUMMARY_MISMATCH", "summary_reconcile")
    summary_income = sum(
        (_decimal(row["income"], "summary_income") for row in summaries),
        _ZERO,
    )
    summary_expense = sum(
        (_decimal(row["expense"], "summary_expense") for row in summaries),
        _ZERO,
    )
    detail_net = sum(detail_by_fee.values(), _ZERO)
    if detail_net != summary_income - summary_expense:
        raise FinanceActionError("TOTAL_AMOUNT_MISMATCH", "summary_reconcile")
    return (
        {
            "summary_income": _amount_text(summary_income),
            "summary_expense": _amount_text(summary_expense),
            "summary_net_change": _amount_text(summary_income - summary_expense),
        },
        mismatches,
    )


def _collect_and_verify(
    target: Mapping[str, str],
    broker: _BudgetedBroker,
    *,
    reserve: int,
) -> dict[str, object]:
    role = target["source_role"]
    target_date = target["target_date"]
    transactions_by_key: dict[str, dict[str, object]] = {}
    page_row_counts: list[int] = []
    evidence_refs: list[str] = []
    capture_ref = ""
    source_context_ref = ""
    source_total: int | None = None
    duplicate_rows = 0
    for page_number in range(1, _MAX_PAGES + 1):
        try:
            raw_page = broker.call(
                "browser.invoke",
                action="ronghui.finance.capture_page",
                role=role,
                arguments={
                    "schema_version": 1,
                    "target_date": target_date,
                    "page_number": page_number,
                    "page_size": _PAGE_SIZE,
                    "capture_ref": capture_ref or None,
                },
                reserve=reserve,
            )
        except FinanceActionError:
            raise
        except Exception as exc:
            raise FinanceActionError(
                "CAPTURE_PRIMITIVE_FAILED",
                "capture_page",
            ) from exc
        page = _exact_object(raw_page, _CAPTURE_RESPONSE_FIELDS, "capture_page")
        if page.get("schema_version") != 1:
            raise FinanceActionError("FIELD_DRIFT", "capture_page")
        observed_capture_ref = _required_text(
            page.get("capture_ref"),
            "capture_ref",
            maximum=512,
        )
        observed_context_ref = _required_text(
            page.get("source_context_ref"),
            "source_context_ref",
            maximum=512,
        )
        if capture_ref and capture_ref != observed_capture_ref:
            raise FinanceActionError("CAPTURE_CONTEXT_MISMATCH", "capture_page")
        if source_context_ref and source_context_ref != observed_context_ref:
            raise FinanceActionError("SOURCE_CONTEXT_MISMATCH", "capture_page")
        capture_ref = observed_capture_ref
        source_context_ref = observed_context_ref
        if _integer(page.get("page_number"), "page_number", minimum=1) != page_number:
            raise FinanceActionError("PAGINATION_DRIFT", "capture_page")
        items = page.get("items")
        if not isinstance(items, list):
            raise FinanceActionError("FIELD_DRIFT", "capture_items")
        page_row_count = _integer(page.get("page_row_count"), "page_row_count")
        if page_row_count != len(items):
            raise FinanceActionError("PAGE_DETAIL_COUNT_MISMATCH", "capture_page")
        observed_total = _integer(page.get("source_total"), "source_total")
        if source_total is None:
            source_total = observed_total
        elif source_total != observed_total:
            raise FinanceActionError("PAGINATION_TOTAL_DRIFT", "capture_page")
        page_row_counts.append(page_row_count)
        for item in items:
            row = _normalize_transaction(item, target_date)
            key = str(row["source_record_key"])
            previous = transactions_by_key.get(key)
            if previous is not None:
                duplicate_rows += 1
                if previous != row:
                    raise FinanceActionError("STABLE_KEY_CONFLICT", "capture_page")
                continue
            transactions_by_key[key] = row
        evidence_refs.append(broker_evidence_ref(page, "finance capture page"))
        complete = page.get("pagination_complete")
        if not isinstance(complete, bool):
            raise FinanceActionError("FIELD_DRIFT", "capture_page")
        next_page = page.get("next_page_number")
        if complete:
            if next_page is not None:
                raise FinanceActionError("PAGINATION_DRIFT", "capture_page")
            break
        if _integer(next_page, "next_page_number", minimum=2) != page_number + 1:
            raise FinanceActionError("PAGINATION_DRIFT", "capture_page")
    else:
        raise FinanceActionError("PAGINATION_TRUNCATED", "capture_page")

    transactions = list(transactions_by_key.values())
    if source_total is None or source_total != len(transactions):
        raise FinanceActionError("REMOTE_UNIQUE_COUNT_MISMATCH", "row_count")
    if source_total == 0 and (transactions or any(page_row_counts)):
        raise FinanceActionError("INVALID_NO_DATA_EVIDENCE", "row_count")
    balance_chain_checked_count = _validate_balance_chain(transactions)
    computed_metrics = _detail_metrics(transactions)
    capture_sha256 = _sha256(
        {
            "target_date": target_date,
            "transactions": transactions,
        }
    )
    try:
        raw_verify = broker.call(
            "browser.invoke",
            action="ronghui.finance.verify_source_totals",
            role=role,
            arguments={
                "schema_version": 1,
                "target_date": target_date,
                "capture_ref": capture_ref,
                "source_context_ref": source_context_ref,
                "capture_sha256": capture_sha256,
                "transaction_count": len(transactions),
                "page_row_counts": page_row_counts,
                "computed_metrics": computed_metrics,
            },
            reserve=reserve,
        )
    except FinanceActionError:
        raise
    except Exception as exc:
        raise FinanceActionError(
            "VERIFY_PRIMITIVE_FAILED",
            "verify_source_totals",
        ) from exc
    verify = _exact_object(
        raw_verify,
        _VERIFY_RESPONSE_FIELDS,
        "verify_source_totals",
    )
    if verify.get("schema_version") != 1 or verify.get("verified") is not True:
        raise FinanceActionError("SOURCE_TOTALS_UNVERIFIED", "verify_source_totals")
    if (
        verify.get("capture_ref") != capture_ref
        or verify.get("source_context_ref") != source_context_ref
        or verify.get("capture_sha256") != capture_sha256
    ):
        raise FinanceActionError("VERIFICATION_CONTEXT_MISMATCH", "verify_source_totals")
    verified_total = _integer(verify.get("remote_total"), "remote_total")
    if verified_total != source_total:
        raise FinanceActionError("REMOTE_UNIQUE_COUNT_MISMATCH", "verify_source_totals")
    if verify.get("summary_semantics") != "signed_net_by_fee":
        raise FinanceActionError("INVALID_SUMMARY_SEMANTICS", "verify_source_totals")
    observed = _exact_object(
        verify.get("observed_metrics"),
        _OBSERVED_METRIC_FIELDS,
        "observed_metrics",
    )
    if _integer(observed.get("transaction_count"), "transaction_count") != len(
        transactions
    ):
        raise FinanceActionError("REMOTE_UNIQUE_COUNT_MISMATCH", "verify_source_totals")
    for name in ("detail_income", "detail_expense", "detail_net_change"):
        if _decimal(observed.get(name), name) != _decimal(computed_metrics[name], name):
            raise FinanceActionError("TOTAL_AMOUNT_MISMATCH", "verify_source_totals")
    for name in (
        "minimum_net_amount",
        "maximum_net_amount",
        "maximum_absolute_amount",
    ):
        if _decimal(observed.get(name), name) != _decimal(computed_metrics[name], name):
            raise FinanceActionError("AMOUNT_EXTREMA_MISMATCH", "verify_source_totals")
    raw_summaries = verify.get("summaries")
    if not isinstance(raw_summaries, list):
        raise FinanceActionError("FIELD_DRIFT", "finance_summaries")
    summaries = [_normalize_summary(row, target_date) for row in raw_summaries]
    if source_total == 0 and summaries:
        raise FinanceActionError("INVALID_NO_DATA_EVIDENCE", "summary_reconcile")
    summary_metrics, fee_mismatches = _reconcile_summaries(transactions, summaries)
    evidence_refs.append(
        broker_evidence_ref(verify, "finance source total verification")
    )
    warnings: list[dict[str, object]] = []
    if duplicate_rows:
        warnings.append({"code": "PAGINATION_OVERLAP", "count": duplicate_rows})
    validation = {
        "status": "warning" if warnings else "passed",
        "metrics": {
            "remote_total": source_total,
            "page_row_count": sum(page_row_counts),
            "parsed_row_count": len(transactions),
            "duplicate_page_row_count": duplicate_rows,
            "unique_row_count": len(transactions),
            "intended_write_count": len(transactions),
            **computed_metrics,
            **summary_metrics,
            "summary_semantics": "signed_net_by_fee",
            "fee_summary_mismatch_count": fee_mismatches,
            "minimum_net_amount": computed_metrics["minimum_net_amount"],
            "maximum_net_amount": computed_metrics["maximum_net_amount"],
            "maximum_absolute_amount": computed_metrics["maximum_absolute_amount"],
            "inverse_checked_count": len(transactions),
            "balance_chain_checked_count": balance_chain_checked_count,
            "eligible_no_data": bool(source_total == 0 and not summaries),
        },
        "page_row_counts": page_row_counts,
        "warnings": warnings,
        "capture_sha256": capture_sha256,
        "verification_evidence_ref": evidence_refs[-1],
    }
    return {
        "source_role": role,
        "target_date": target_date,
        "capture_ref": capture_ref,
        "source_context_ref": source_context_ref,
        "transactions": transactions,
        "summaries": summaries,
        "validation": validation,
        "validation_sha256": _sha256(validation),
        "evidence_refs": evidence_refs,
    }


def _commit_snapshot(
    *,
    batch_id: int,
    contract_sha256: str,
    target: Mapping[str, str],
    capture: Mapping[str, object] | None,
    failure: FinanceActionError | None,
    broker: _BudgetedBroker,
    reserve: int,
) -> tuple[dict[str, object], str]:
    role = target["source_role"]
    target_date = target["target_date"]
    if failure is not None:
        request: dict[str, object] = {
            "schema_version": 1,
            "batch_id": batch_id,
            "contract_sha256": contract_sha256,
            "target_date": target_date,
            "outcome": "failed",
            "failure": {"code": failure.code, "stage": failure.stage},
        }
        expected_records = expected_summaries = 0
        expected_validation_sha256: str | None = None
    else:
        if capture is None:
            raise FinanceActionError("CAPTURE_MISSING", "snapshot_write")
        transactions = list(capture["transactions"])
        summaries = list(capture["summaries"])
        outcome = "no_data" if not transactions else "success"
        request = {
            "schema_version": 1,
            "batch_id": batch_id,
            "contract_sha256": contract_sha256,
            "target_date": target_date,
            "outcome": outcome,
            "capture_ref": capture["capture_ref"],
            "source_context_ref": capture["source_context_ref"],
            "transactions": transactions,
            "summaries": summaries,
            "validation": capture["validation"],
            "validation_sha256": capture["validation_sha256"],
        }
        expected_records = len(transactions)
        expected_summaries = len(summaries)
        expected_validation_sha256 = str(capture["validation_sha256"])
    try:
        raw = broker.call(
            "ledger.invoke",
            action="finance.source_snapshot.write",
            role=role,
            arguments=request,
            reserve=reserve,
        )
    except FinanceActionError:
        raise
    except Exception as exc:
        raise FinanceActionError("SNAPSHOT_WRITE_FAILED", "snapshot_write") from exc
    result = _exact_object(
        raw,
        _SNAPSHOT_RESPONSE_FIELDS,
        "source_snapshot_write",
    )
    if result.get("schema_version") != 1 or result.get("committed") is not True:
        raise FinanceActionError("SNAPSHOT_WRITE_FAILED", "snapshot_write")
    outcome = str(request["outcome"])
    if (
        _integer(result.get("batch_id"), "batch_id", minimum=1) != batch_id
        or result.get("outcome") != outcome
        or result.get("validation_sha256") != expected_validation_sha256
    ):
        raise FinanceActionError("SNAPSHOT_COMMIT_MISMATCH", "snapshot_write")
    record_count = _integer(result.get("record_count"), "record_count")
    summary_count = _integer(result.get("summary_count"), "summary_count")
    written_count = _integer(result.get("written_row_count"), "written_row_count")
    if (
        record_count != expected_records
        or summary_count != expected_summaries
        or written_count != expected_records
    ):
        raise FinanceActionError("SNAPSHOT_WRITE_COUNT_MISMATCH", "snapshot_write")
    run_ref = _required_text(result.get("run_ref"), "run_ref", maximum=512)
    committed = {
        "source_role": role,
        "target_date": target_date,
        "run_ref": run_ref,
        "outcome": outcome,
        "record_count": record_count,
        "summary_count": summary_count,
        "validation_sha256": expected_validation_sha256,
        "new_fee_item_count": _integer(
            result.get("new_fee_item_count"),
            "new_fee_item_count",
        ),
        "historical_revision_count": _integer(
            result.get("historical_revision_count"),
            "historical_revision_count",
        ),
    }
    return committed, broker_evidence_ref(result, "finance source snapshot write")


def _commit_projection(
    *,
    batch_id: int,
    contract_sha256: str,
    outcomes: list[dict[str, object]],
    broker: _BudgetedBroker,
) -> tuple[dict[str, object], str]:
    public_outcomes = [
        {
            "source_role": row["source_role"],
            "target_date": row["target_date"],
            "run_ref": row["run_ref"],
            "outcome": row["outcome"],
            "record_count": row["record_count"],
            "validation_sha256": row["validation_sha256"],
        }
        for row in outcomes
    ]
    try:
        raw = broker.call(
            "ledger.invoke",
            action="finance.projection.commit",
            role=_COORDINATOR_ROLE,
            arguments={
                "schema_version": 1,
                "batch_id": batch_id,
                "contract_sha256": contract_sha256,
                "outcomes": public_outcomes,
            },
        )
    except FinanceActionError:
        raise
    except Exception as exc:
        raise FinanceActionError("PROJECTION_COMMIT_FAILED", "projection_commit") from exc
    result = _exact_object(
        raw,
        frozenset(
            {
                "schema_version",
                "committed",
                "batch_id",
                "contract_sha256",
                "status",
                "successful_runs",
                "no_data_runs",
                "failed_runs",
                "written_record_count",
                "evidence_ref",
            }
        ),
        "projection_commit",
    )
    if (
        result.get("schema_version") != 1
        or result.get("committed") is not True
        or _integer(result.get("batch_id"), "batch_id", minimum=1) != batch_id
        or result.get("contract_sha256") != contract_sha256
    ):
        raise FinanceActionError("PROJECTION_COMMIT_FAILED", "projection_commit")
    completed = [row for row in outcomes if row["outcome"] != "failed"]
    failed = [row for row in outcomes if row["outcome"] == "failed"]
    no_data = [row for row in outcomes if row["outcome"] == "no_data"]
    expected_status = (
        "partial_failed"
        if failed and completed
        else "failed"
        if failed
        else "no_data"
        if not outcomes
        else "success"
    )
    if result.get("status") != expected_status:
        raise FinanceActionError("BATCH_STATUS_MISMATCH", "projection_commit")
    expected_counts = {
        "successful_runs": len(completed),
        "no_data_runs": len(no_data),
        "failed_runs": len(failed),
        "written_record_count": sum(int(row["record_count"]) for row in completed),
    }
    for name, expected in expected_counts.items():
        if _integer(result.get(name), name) != expected:
            raise FinanceActionError("PROJECTION_COUNT_MISMATCH", "projection_commit")
    return result, broker_evidence_ref(result, "finance projection commit")


def _failed_result(
    *,
    data: Mapping[str, object],
    evidence_refs: list[str],
    warnings: list[str],
    observed_at: str,
) -> dict[str, object]:
    if not evidence_refs or len(evidence_refs) != len(set(evidence_refs)):
        raise FinanceActionError("EVIDENCE_INVALID", "result")
    code = (
        "FINANCE_SYNC_PARTIAL_FAILED"
        if data.get("status") == "partial_failed"
        else "FINANCE_SYNC_FAILED"
    )
    return {
        "status": "FAILED",
        "data": dict(data),
        "meta": {
            "source_system": "ronghui+finance_ledger",
            "observed_at": observed_at,
            "record_count": int(data.get("written_transactions") or 0),
            "pagination_complete": False,
            "evidence_refs": evidence_refs,
        },
        "warnings": warnings,
        "error": {
            "code": code,
            "message": "finance batch completed with one or more failed source targets",
        },
    }


def run_action(
    arguments: dict[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    values = _object(arguments, "arguments")
    contract, contract_sha256 = _plan_request(values)
    budgeted = _BudgetedBroker(broker)
    batch_id, targets, skipped_disabled_count, batch_evidence = _acquire_batch(
        contract,
        contract_sha256,
        budgeted,
    )
    if len(targets) * 3 + 2 > _MAX_BROKER_CALLS:
        raise FinanceActionError("BROKER_CALL_BUDGET_EXHAUSTED", "batch_targets")

    evidence_refs = [batch_evidence]
    captured: dict[tuple[str, str], dict[str, object]] = {}
    failures: dict[tuple[str, str], FinanceActionError] = {}
    for index, target in enumerate(targets):
        identity = (target["source_role"], target["target_date"])
        remaining_targets = len(targets) - index - 1
        reserve = (remaining_targets * 2) + len(targets) + 1
        try:
            item = _collect_and_verify(target, budgeted, reserve=reserve)
        except FinanceActionError as exc:
            failures[identity] = exc
        except Exception as exc:
            failures[identity] = FinanceActionError(
                "SOURCE_VALIDATION_FAILED",
                "source_orchestration",
            )
            failures[identity].__cause__ = exc
        else:
            captured[identity] = item
            evidence_refs.extend(list(item["evidence_refs"]))

    committed: list[dict[str, object]] = []
    public_runs: list[dict[str, object]] = []
    warnings: list[str] = []
    for index, target in enumerate(targets):
        identity = (target["source_role"], target["target_date"])
        capture = captured.get(identity)
        failure = failures.get(identity)
        commit, commit_evidence = _commit_snapshot(
            batch_id=batch_id,
            contract_sha256=contract_sha256,
            target=target,
            capture=capture,
            failure=failure,
            broker=budgeted,
            reserve=len(targets) - index,
        )
        evidence_refs.append(commit_evidence)
        committed.append(commit)
        if failure is not None:
            public_runs.append(
                {
                    "source_role": target["source_role"],
                    "target_date": target["target_date"],
                    "status": "failed",
                    "transactions": 0,
                    "summaries": 0,
                    "failure_code": failure.code,
                    "failure_stage": failure.stage,
                }
            )
            warnings.append(
                f"SOURCE_FAILED:{target['source_role']}:{target['target_date']}:{failure.code}"
            )
            continue
        if capture is None:
            raise FinanceActionError("CAPTURE_MISSING", "result")
        metrics = dict(capture["validation"])["metrics"]
        public_runs.append(
            {
                "source_role": target["source_role"],
                "target_date": target["target_date"],
                "status": commit["outcome"],
                "transactions": commit["record_count"],
                "summaries": commit["summary_count"],
                "validation_status": dict(capture["validation"])["status"],
                "detail_income": metrics["detail_income"],
                "detail_expense": metrics["detail_expense"],
                "detail_net_change": metrics["detail_net_change"],
                "minimum_net_amount": metrics["minimum_net_amount"],
                "maximum_net_amount": metrics["maximum_net_amount"],
                "maximum_absolute_amount": metrics["maximum_absolute_amount"],
                "inverse_checked_count": metrics["inverse_checked_count"],
                "balance_chain_checked_count": metrics[
                    "balance_chain_checked_count"
                ],
                "new_fee_item_count": commit["new_fee_item_count"],
                "historical_revision_count": commit[
                    "historical_revision_count"
                ],
            }
        )
        for warning in dict(capture["validation"])["warnings"]:
            warnings.append(
                f"{warning['code']}:{target['source_role']}:{target['target_date']}:{warning['count']}"
            )
        if int(commit["new_fee_item_count"]):
            warnings.append(
                f"NEW_FEE_ITEM:{target['source_role']}:{target['target_date']}:{commit['new_fee_item_count']}"
            )
        if int(commit["historical_revision_count"]):
            warnings.append(
                "HISTORICAL_REVISION:"
                f"{target['source_role']}:{target['target_date']}:"
                f"{commit['historical_revision_count']}"
            )

    projection, projection_evidence = _commit_projection(
        batch_id=batch_id,
        contract_sha256=contract_sha256,
        outcomes=committed,
        broker=budgeted,
    )
    evidence_refs.append(projection_evidence)
    if len(evidence_refs) != len(set(evidence_refs)):
        raise FinanceActionError("EVIDENCE_INVALID", "result")
    if contract.get("earliest_date_status"):
        warnings.append(str(contract["earliest_date_status"]))
    if contract.get("mode") == "retry":
        target_dates = [date.fromisoformat(row["target_date"]) for row in targets]
        start_date = min(target_dates).isoformat()
        end_date = max(target_dates).isoformat()
    else:
        start_date = str(contract["start_date"])
        end_date = str(contract["end_date"])
    observed_at = datetime.now(_SHANGHAI).isoformat()
    data: dict[str, object] = {
        "batch_id": batch_id,
        "mode": contract["mode"],
        "start_date": start_date,
        "end_date": end_date,
        "status": projection["status"],
        "successful_runs": projection["successful_runs"],
        "no_data_runs": projection["no_data_runs"],
        "failed_runs": projection["failed_runs"],
        "skipped_disabled_count": skipped_disabled_count,
        "written_transactions": projection["written_record_count"],
        "summary_rows": sum(int(row["summary_count"]) for row in committed),
        "runs": public_runs,
        "earliest_date_status": contract.get("earliest_date_status"),
        "evidence": {
            "source": "signed_first_party_plugin",
            "observed_at": observed_at,
            "execution_result": (
                "batch_committed"
                if projection["status"] in {"success", "no_data"}
                else "batch_committed_with_failures"
            ),
        },
    }
    if projection["status"] in {"partial_failed", "failed"}:
        result = _failed_result(
            data=data,
            evidence_refs=evidence_refs,
            warnings=warnings,
            observed_at=observed_at,
        )
    else:
        result_ref, result_proof = executor_success_evidence(
            action_id=ACTION_ID,
            data=data,
            observed_at=observed_at,
        )
        evidence_refs.append(result_ref)
        result = success_result(
            data=data,
            source_system="ronghui+finance_ledger",
            record_count=int(projection["written_record_count"]),
            pagination_complete=True,
            evidence_refs=evidence_refs,
            observed_at=observed_at,
            postconditions={"0": True},
            postcondition_evidence={"0": result_proof},
            warnings=warnings,
        )
    _assert_account_blind(result)
    return result
