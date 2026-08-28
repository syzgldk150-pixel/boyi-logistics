"""Production broker primitives for the signed finance action.

The subprocess receives logical roles and account-blind rows only. Exact local
business-account bindings, target capability sessions, source-site identity
and database run identifiers remain in this core-owned adapter.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Collection, Mapping, Protocol, Sequence

from agent.automation_plugins.core_adapter import (
    CoreBrokerHandler,
    CoreBrokerInvocationContext,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.tms_runtime.account_manager import AutomationAccountManager, get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError
from plugin_core_adapters.capability_session import (
    CapabilityAuthorizer,
    authorize_target_capability,
)
from agent.tms_runtime.scripts.finance_capture_common import CaptureResult
from agent.tms_runtime.scripts.finance_live_capture import build_live_finance_adapter
from shared.finance import FinanceRepository, SummarySemantics, SyncStatus
from tools.finance_sync_service import (
    FinanceAccountBinding,
    finance_summary_from_capture_row,
    finance_transaction_from_capture_row,
    validate_finance_capture_result,
)


_TOOL = "sync_finance_bills"
_ROLES = (
    "finance_quote_source",
    "finance_daxiang_s_source",
    "finance_self_pickup_source",
)
_COORDINATOR = _ROLES[0]
_PAGE_SIZE = 100
_MAX_PAGES = 200
_MAX_TARGETS = 255
_CAPTURE_TTL_SECONDS = 3_600.0
MARKED_WRITE_ACTION_KEYS = frozenset(
    {
        ("ledger.invoke", "finance.batch.acquire"),
        ("ledger.invoke", "finance.source_snapshot.write"),
        ("ledger.invoke", "finance.projection.commit"),
    }
)
_CONTRACT_FIELDS = {
    "mode",
    "trigger_type",
    "start_date",
    "end_date",
    "rescan_days",
    "earliest_date_status",
    "startup_catchup",
    "retry_batch_id",
    "source_roles",
    "requested_targets",
    "month_chunks",
    "max_targets",
}
_CAPTURE_TRANSACTION_FIELDS = {
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
_SNAPSHOT_TRANSACTION_FIELDS = _CAPTURE_TRANSACTION_FIELDS | {"direction"}
_CAPTURE_SUMMARY_FIELDS = {
    "target_date",
    "primary_fee_name",
    "secondary_fee_name",
    "income",
    "expense",
}
_SNAPSHOT_SUMMARY_FIELDS = _CAPTURE_SUMMARY_FIELDS | {"direction"}
_METRIC_FIELDS = {
    "transaction_count",
    "detail_income",
    "detail_expense",
    "detail_net_change",
    "minimum_net_amount",
    "maximum_net_amount",
    "maximum_absolute_amount",
}


class FinanceRepositoryPort(Protocol):
    def initialize_schema(self) -> None: ...

    def seed_fee_mappings(self, *args: Any, **kwargs: Any) -> int: ...

    def create_batch(self, **kwargs: Any) -> int: ...

    def list_missing_dates(self, **kwargs: Any) -> list[date]: ...

    def list_retry_targets(self, batch_id: int) -> Sequence[Mapping[str, Any]]: ...

    def start_run(self, **kwargs: Any) -> int: ...

    def start_failed_run(self, **kwargs: Any) -> int: ...

    def commit_run_snapshot(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def mark_run_no_data(self, **kwargs: Any) -> Any: ...

    def fail_run(self, **kwargs: Any) -> Any: ...

    def finalize_batch(self, batch_id: int) -> Any: ...

    def get_validation_context(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def read_batch_commit_proof(self, batch_id: int) -> Mapping[str, Any]: ...

    def read_run_commit_proof(self, run_id: int) -> Mapping[str, Any]: ...


RepositoryFactory = Callable[[], FinanceRepositoryPort]
CapturePort = Callable[[Mapping[str, Any], date], CaptureResult]


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _strict(value: Mapping[str, Any], fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _error(f"{label} fields are invalid", "BROKER_ARGUMENT_INVALID")
    return dict(value)


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if value is None or isinstance(value, (bool, Mapping, list, tuple, set)):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    result = str(value).strip()
    if not result or len(result) > maximum:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    return result


def _optional_text(value: object, label: str, *, maximum: int = 2_000) -> str:
    if value in (None, ""):
        return ""
    return _text(value, label, maximum=maximum)


def _integer(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if str(result) != str(value).strip() or result < minimum:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    if maximum is not None and result > maximum:
        raise _error(f"{label} exceeds its signed limit", "BROKER_ARGUMENT_INVALID")
    return result


def _business_date(value: object, label: str = "target_date") -> str:
    text = _text(value, label, maximum=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise _error(f"{label} must use YYYY-MM-DD", "BROKER_ARGUMENT_INVALID") from exc
    if parsed.isoformat() != text:
        raise _error(f"{label} must use YYYY-MM-DD", "BROKER_ARGUMENT_INVALID")
    return text


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha_text(value: object, label: str) -> str:
    text = _text(value, label, maximum=64).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    return text


def _amount(value: object, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except Exception as exc:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if not number.is_finite():
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    return number.quantize(Decimal("0.0001"))


def _amount_text(value: object, label: str) -> str:
    return f"{_amount(value, label):.4f}"


def _context_key(context: CoreBrokerInvocationContext) -> tuple[str, str]:
    return (context.automation_id, context.plugin_version)


def _require_context(
    context: CoreBrokerInvocationContext,
    *,
    operation: str,
    action: str,
    roles: Collection[str],
) -> None:
    if (
        context.tool_name != _TOOL
        or context.operation != operation
        or context.action != action
        or context.role not in set(roles)
    ):
        raise _error("finance broker context is invalid", "BROKER_CONTEXT_INVALID")


def _role_accounts(context: CoreBrokerInvocationContext) -> dict[str, str]:
    result: dict[str, str] = {}
    for role in _ROLES:
        values = context.account_bindings.get(role)
        if not isinstance(values, tuple) or len(values) != 1:
            raise _error(
                "finance requires one exact account for every logical role",
                "BROKER_ROLE_UNBOUND",
            )
        account_id = str(values[0] or "").strip()
        if not account_id:
            raise _error("finance account binding is empty", "BROKER_ROLE_UNBOUND")
        result[role] = account_id
    if len(set(result.values())) != len(_ROLES):
        raise _error(
            "finance logical roles must use distinct accounts",
            "BROKER_ACCOUNT_MISMATCH",
        )
    if context.role in result and tuple(context.account_ids) != (result[context.role],):
        raise _error("finance selected binding changed", "BROKER_ACCOUNT_MISMATCH")
    return result


def _canonical_transaction(record: Any) -> dict[str, Any]:
    if record.before_balance is None or record.after_balance is None:
        raise _error(
            "finance source omitted balance-equation fields",
            "BROKER_SOURCE_INVALID",
        )
    transaction_at = record.transaction_at
    if transaction_at is None:
        raise _error("finance source omitted transaction time", "BROKER_SOURCE_INVALID")
    return {
        "source_record_key": str(record.source_record_key),
        "business_date": record.business_date.isoformat(),
        "transaction_at": transaction_at.isoformat(),
        "primary_fee_name": str(record.primary_fee_name),
        "secondary_fee_name": str(record.secondary_fee_name),
        "income": f"{record.income:.4f}",
        "expense": f"{record.expense:.4f}",
        "before_balance": f"{record.before_balance:.4f}",
        "after_balance": f"{record.after_balance:.4f}",
        "waybill_no": str(record.waybill_no),
        "source_reference": str(record.source_reference),
        "remark": str(record.remark),
        "source_payload": dict(record.source_payload),
    }


def _canonical_summary(record: Any) -> dict[str, Any]:
    return {
        "target_date": record.target_date.isoformat(),
        "primary_fee_name": str(record.primary_fee_name),
        "secondary_fee_name": str(record.secondary_fee_name),
        "income": f"{record.income:.4f}",
        "expense": f"{record.expense:.4f}",
    }


def _plugin_transaction(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    income = _amount(row.get("income"), "income")
    expense = _amount(row.get("expense"), "expense")
    if income < 0 or expense < 0 or (income == 0) == (expense == 0):
        raise _error("finance transaction direction is invalid", "BROKER_SOURCE_INVALID")
    row["direction"] = "income" if income > 0 else "expense"
    return row


def _plugin_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    income = _amount(row.get("income"), "summary income")
    expense = _amount(row.get("expense"), "summary expense")
    if income < 0 or expense < 0 or (income == 0) == (expense == 0):
        raise _error("finance summary direction is invalid", "BROKER_SOURCE_INVALID")
    row["direction"] = "income" if income > 0 else "expense"
    return row


def _observed_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    incomes = [_amount(row.get("income"), "income") for row in rows]
    expenses = [_amount(row.get("expense"), "expense") for row in rows]
    net = [income - expense for income, expense in zip(incomes, expenses)]
    total_income = sum(incomes, Decimal("0.0000"))
    total_expense = sum(expenses, Decimal("0.0000"))
    return {
        "transaction_count": len(rows),
        "detail_income": f"{total_income:.4f}",
        "detail_expense": f"{total_expense:.4f}",
        "detail_net_change": f"{total_income - total_expense:.4f}",
        "minimum_net_amount": f"{min(net, default=Decimal('0.0000')):.4f}",
        "maximum_net_amount": f"{max(net, default=Decimal('0.0000')):.4f}",
        "maximum_absolute_amount": (f"{max((abs(value) for value in net), default=Decimal('0.0000')):.4f}"),
    }


_BATCH_COMMIT_PROOF_FIELDS = {
    "batch_id",
    "trigger_type",
    "start_date",
    "end_date",
    "rescan_days",
    "status",
    "earliest_date_status",
    "requested_by",
    "run_counts",
}
_RUN_COMMIT_PROOF_FIELDS = {
    "run_id",
    "batch_id",
    "platform",
    "account_id",
    "target_date",
    "status",
    "remote_total",
    "unique_row_count",
    "written_row_count",
    "transaction_count",
    "transaction_unique_count",
    "transaction_income",
    "transaction_expense",
    "summary_count",
    "summary_income",
    "summary_expense",
}


def _canonical_batch_commit_proof(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _strict(value, _BATCH_COMMIT_PROOF_FIELDS, "finance batch readback")
    raw_counts = row.get("run_counts")
    if not isinstance(raw_counts, Mapping):
        raise _error("finance batch run counts are invalid", "BROKER_SOURCE_INVALID")
    run_counts: dict[str, int] = {}
    for raw_status, raw_count in raw_counts.items():
        status = _text(raw_status, "run status", maximum=32)
        if status in run_counts:
            raise _error("finance batch run status is duplicated", "BROKER_SOURCE_INVALID")
        run_counts[status] = _integer(raw_count, "run count")
    earliest = row.get("earliest_date_status")
    if earliest is not None:
        earliest = _text(earliest, "earliest_date_status", maximum=64)
    return {
        "batch_id": _integer(row.get("batch_id"), "batch_id", minimum=1),
        "trigger_type": _text(row.get("trigger_type"), "trigger_type", maximum=32),
        "start_date": _business_date(row.get("start_date"), "start_date"),
        "end_date": _business_date(row.get("end_date"), "end_date"),
        "rescan_days": _integer(row.get("rescan_days"), "rescan_days"),
        "status": _text(row.get("status"), "batch status", maximum=32),
        "earliest_date_status": earliest,
        "requested_by": _text(row.get("requested_by"), "requested_by", maximum=512),
        "run_counts": run_counts,
    }


def _canonical_run_commit_proof(value: Mapping[str, Any]) -> dict[str, Any]:
    row = _strict(value, _RUN_COMMIT_PROOF_FIELDS, "finance run readback")
    result = {
        "run_id": _integer(row.get("run_id"), "run_id", minimum=1),
        "batch_id": _integer(row.get("batch_id"), "batch_id", minimum=1),
        "platform": _text(row.get("platform"), "platform", maximum=32),
        "account_id": _text(row.get("account_id"), "account_id", maximum=191),
        "target_date": _business_date(row.get("target_date")),
        "status": _text(row.get("status"), "run status", maximum=32),
    }
    for field_name in (
        "remote_total",
        "unique_row_count",
        "written_row_count",
        "transaction_count",
        "transaction_unique_count",
        "summary_count",
    ):
        result[field_name] = _integer(row.get(field_name), field_name)
    for field_name in (
        "transaction_income",
        "transaction_expense",
        "summary_income",
        "summary_expense",
    ):
        result[field_name] = _amount_text(row.get(field_name), field_name)
    return result


@dataclass(frozen=True)
class _CapturedSource:
    capture: CaptureResult
    transactions: tuple[Any, ...]
    summaries: tuple[Any, ...]
    public_transactions: tuple[dict[str, Any], ...]
    public_summaries: tuple[dict[str, Any], ...]
    plugin_transactions: tuple[dict[str, Any], ...]
    plugin_summaries: tuple[dict[str, Any], ...]
    source_total: int
    source_site_code: str
    source_site_name: str


@dataclass
class _CaptureState:
    context_key: tuple[str, str]
    role: str
    account_id: str
    target_date: str
    capture_ref: str
    source_context_ref: str
    captured: _CapturedSource
    expires_at: float
    verified: bool = False


@dataclass
class _RunReceipt:
    role: str
    target_date: str
    run_id: int
    run_ref: str
    outcome: str
    record_count: int
    validation_sha256: str | None
    summary_count: int


@dataclass
class _BatchState:
    context_key: tuple[str, str]
    batch_id: int
    contract_sha256: str
    targets: tuple[tuple[str, str], ...]
    accounts: dict[str, str]
    descriptors: dict[str, Mapping[str, Any]]
    repository: FinanceRepositoryPort
    batch_identity: dict[str, Any]
    receipts: dict[tuple[str, str], _RunReceipt] = field(default_factory=dict)
    finalized_request_sha256: str = ""
    finalized_response: dict[str, Any] | None = None


class _FinanceBrokerHandlers:
    def __init__(
        self,
        *,
        account_manager: AutomationAccountManager,
        repository_factory: RepositoryFactory,
        capture_port: CapturePort,
        cursor_secret: bytes,
        capability_authorizer: CapabilityAuthorizer,
    ) -> None:
        if not isinstance(cursor_secret, bytes) or len(cursor_secret) < 32:
            raise ValueError("finance cursor secret must contain at least 32 bytes")
        self._manager = account_manager
        self._repository_factory = repository_factory
        self._capture_port = capture_port
        self._capability_authorizer = capability_authorizer
        self._secret = bytes(cursor_secret)
        self._lock = threading.RLock()
        self._captures: dict[str, _CaptureState] = {}
        self._source_contexts: dict[str, str] = {}
        self._batches: dict[int, _BatchState] = {}

    @staticmethod
    def _mark_write_started(context: CoreBrokerInvocationContext) -> None:
        if context.mark_write_started is not None:
            context.mark_write_started()

    def _opaque(self, context: CoreBrokerInvocationContext, purpose: str) -> str:
        nonce = secrets.token_urlsafe(24)
        material = canonical_json_bytes(
            {
                "automation_id": context.automation_id,
                "plugin_version": context.plugin_version,
                "purpose": purpose,
                "nonce": nonce,
            }
        )
        signature = hmac.new(self._secret, material, hashlib.sha256).hexdigest()
        return f"finance:{purpose}:v1:{nonce}:{signature}"

    def _evidence(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        value: Mapping[str, Any],
    ) -> str:
        material = canonical_json_bytes(
            {
                "automation_id": context.automation_id,
                "plugin_version": context.plugin_version,
                "operation": context.operation,
                "action": context.action,
                "role": context.role,
                "purpose": purpose,
                "value": dict(value),
            }
        )
        digest = hmac.new(self._secret, material, hashlib.sha256).hexdigest()
        return f"finance:{purpose}:evidence:v1:{digest}"

    def _descriptor(self, account_id: str) -> Mapping[str, Any]:
        try:
            descriptor = self._manager.require_active_binding_descriptor(account_id)
        except TMSAuthStateError as exc:
            raise _error(
                "the exact finance account is unavailable",
                "BROKER_ACCOUNT_UNAVAILABLE",
            ) from exc
        if not isinstance(descriptor, Mapping):
            raise _error("finance account descriptor is invalid", "BROKER_ACCOUNT_INVALID")
        result = dict(descriptor)
        if str(result.get("account_id") or "").strip() != account_id:
            raise _error("finance account resolver changed the binding", "BROKER_ACCOUNT_MISMATCH")
        if str(result.get("system") or "").strip().lower() != "ronghui":
            raise _error("finance requires Ronghui accounts", "BROKER_ACCOUNT_SYSTEM_MISMATCH")
        credentials = self._manager.public_credentials(account_id)
        login_account = str((credentials or {}).get("username") or "").strip()
        if not login_account:
            raise _error(
                "finance account has no public login identity",
                "BROKER_ACCOUNT_UNAVAILABLE",
            )
        result["login_account"] = login_account
        if not str(result.get("session_profile") or "").strip():
            raise _error(
                "finance account has no session profile binding",
                "BROKER_ACCOUNT_UNAVAILABLE",
            )
        return result

    @staticmethod
    def _binding(descriptor: Mapping[str, Any]) -> FinanceAccountBinding:
        return FinanceAccountBinding(
            system=str(descriptor["system"]),
            account_id=str(descriptor["account_id"]),
            login_account=str(descriptor["login_account"]),
            session_profile=str(descriptor["session_profile"]),
        )

    def _capture_source(
        self,
        descriptor: Mapping[str, Any],
        target_date: str,
    ) -> _CapturedSource:
        target = date.fromisoformat(target_date)
        capture = self._capture_port(descriptor, target)
        if not isinstance(capture, CaptureResult):
            raise _error("finance capture returned an invalid contract", "BROKER_SOURCE_INVALID")
        if capture.summary_semantics is not SummarySemantics.SIGNED_NET_BY_FEE:
            raise _error(
                "finance source summary semantics changed",
                "BROKER_SOURCE_INVALID",
            )
        binding = self._binding(descriptor)
        transactions = tuple(finance_transaction_from_capture_row(row, binding) for row in capture.transactions)
        summaries = tuple(finance_summary_from_capture_row(row, binding) for row in capture.summaries)
        public_transactions = tuple(_canonical_transaction(row) for row in transactions)
        public_summaries = tuple(_canonical_summary(row) for row in summaries)
        plugin_transactions = tuple(_plugin_transaction(row) for row in public_transactions)
        plugin_summaries = tuple(_plugin_summary(row) for row in public_summaries)
        if any(row["business_date"] != target_date for row in public_transactions):
            raise _error("finance capture returned another date", "BROKER_SOURCE_INVALID")
        if any(row["target_date"] != target_date for row in public_summaries):
            raise _error("finance summary returned another date", "BROKER_SOURCE_INVALID")
        source_total = _integer(
            capture.validation.get("source_total"),
            "source_total",
            maximum=_PAGE_SIZE * _MAX_PAGES,
        )
        if source_total != len(public_transactions):
            raise _error("finance source total is not closed", "BROKER_SOURCE_INVALID")
        site_code = str(capture.source_site_code or "").strip()
        site_name = str(capture.source_site_name or "").strip()
        if not site_code or not site_name:
            raise _error("finance source site identity is incomplete", "BROKER_SOURCE_INVALID")
        return _CapturedSource(
            capture=capture,
            transactions=transactions,
            summaries=summaries,
            public_transactions=public_transactions,
            public_summaries=public_summaries,
            plugin_transactions=plugin_transactions,
            plugin_summaries=plugin_summaries,
            source_total=source_total,
            source_site_code=site_code,
            source_site_name=site_name,
        )

    @staticmethod
    def _contract_targets(contract: Mapping[str, Any]) -> list[tuple[str, str]]:
        raw_targets = contract.get("requested_targets")
        if not isinstance(raw_targets, list) or len(raw_targets) > _MAX_TARGETS:
            raise _error("finance target set is invalid", "BROKER_ARGUMENT_INVALID")
        targets: list[tuple[str, str]] = []
        for raw in raw_targets:
            row = _strict(raw, {"source_role", "target_date"}, "finance target")
            role = _text(row.get("source_role"), "source_role", maximum=64)
            if role not in _ROLES:
                raise _error("finance source role is invalid", "BROKER_ARGUMENT_INVALID")
            targets.append((role, _business_date(row.get("target_date"))))
        if len(targets) != len(set(targets)):
            raise _error("finance target set contains duplicates", "BROKER_ARGUMENT_INVALID")
        return targets

    @staticmethod
    def _validate_contract(contract: Mapping[str, Any], contract_sha256: str) -> None:
        values = _strict(contract, _CONTRACT_FIELDS, "finance contract")
        if _sha256(values) != contract_sha256:
            raise _error("finance contract digest changed", "BROKER_ARGUMENT_INVALID")
        mode = _text(values.get("mode"), "mode", maximum=16)
        trigger = _text(values.get("trigger_type"), "trigger_type", maximum=16)
        if mode not in {"sync", "backfill", "retry"}:
            raise _error("finance mode is invalid", "BROKER_ARGUMENT_INVALID")
        startup = values.get("startup_catchup")
        if not isinstance(startup, bool):
            raise _error("finance startup flag is invalid", "BROKER_ARGUMENT_INVALID")
        expected_trigger = "startup" if startup else mode
        if trigger != expected_trigger:
            raise _error("finance trigger type is invalid", "BROKER_ARGUMENT_INVALID")
        roles = values.get("source_roles")
        if roles != list(_ROLES):
            raise _error("finance source-role order changed", "BROKER_ARGUMENT_INVALID")
        if _integer(values.get("max_targets"), "max_targets", minimum=1) != _MAX_TARGETS:
            raise _error("finance target limit changed", "BROKER_ARGUMENT_INVALID")
        rescan_days = _integer(values.get("rescan_days"), "rescan_days", minimum=1)
        targets = _FinanceBrokerHandlers._contract_targets(values)
        if mode == "retry":
            if any(values.get(field) is not None for field in ("start_date", "end_date", "earliest_date_status")):
                raise _error("finance retry contract contains a date range", "BROKER_ARGUMENT_INVALID")
            if startup or targets or values.get("month_chunks") != []:
                raise _error("finance retry contract is not closed", "BROKER_ARGUMENT_INVALID")
            _integer(values.get("retry_batch_id"), "retry_batch_id", minimum=1)
            if rescan_days != 7:
                raise _error("finance retry rescan window changed", "BROKER_ARGUMENT_INVALID")
            return
        if values.get("retry_batch_id") is not None:
            raise _error("finance non-retry contract has a retry batch", "BROKER_ARGUMENT_INVALID")
        start = _business_date(values.get("start_date"), "start_date")
        end = _business_date(values.get("end_date"), "end_date")
        if start > end:
            raise _error("finance date range is invalid", "BROKER_ARGUMENT_INVALID")
        if mode == "backfill":
            if values.get("earliest_date_status") != "EARLIEST_DATE_UNCONFIRMED":
                raise _error("finance backfill status is invalid", "BROKER_ARGUMENT_INVALID")
        elif values.get("earliest_date_status") is not None:
            raise _error("finance earliest-date status is invalid", "BROKER_ARGUMENT_INVALID")
        expected_targets = [
            (role, target.isoformat())
            for role in _ROLES
            for target in (
                date.fromordinal(ordinal)
                for ordinal in range(date.fromisoformat(start).toordinal(), date.fromisoformat(end).toordinal() + 1)
            )
        ]
        if targets != expected_targets:
            raise _error("finance requested targets changed", "BROKER_ARGUMENT_INVALID")
        raw_chunks = values.get("month_chunks")
        if not isinstance(raw_chunks, list) or not raw_chunks:
            raise _error("finance month chunks are invalid", "BROKER_ARGUMENT_INVALID")
        chunk_start = start
        for raw in raw_chunks:
            row = _strict(raw, {"start_date", "end_date"}, "finance month chunk")
            observed_start = _business_date(row.get("start_date"), "chunk_start_date")
            observed_end = _business_date(row.get("end_date"), "chunk_end_date")
            if observed_start != chunk_start or observed_end < observed_start or observed_end > end:
                raise _error("finance month chunks are not contiguous", "BROKER_ARGUMENT_INVALID")
            parsed_start = date.fromisoformat(observed_start)
            expected_end = min(
                date(
                    parsed_start.year,
                    parsed_start.month,
                    calendar.monthrange(parsed_start.year, parsed_start.month)[1],
                ),
                date.fromisoformat(end),
            ).isoformat()
            if observed_end != expected_end:
                raise _error("finance month chunk changed", "BROKER_ARGUMENT_INVALID")
            chunk_start = date.fromordinal(date.fromisoformat(observed_end).toordinal() + 1).isoformat()
        if chunk_start != date.fromordinal(date.fromisoformat(end).toordinal() + 1).isoformat():
            raise _error("finance month chunks do not cover the range", "BROKER_ARGUMENT_INVALID")

    def _descriptors(
        self,
        context: CoreBrokerInvocationContext,
    ) -> tuple[dict[str, str], dict[str, Mapping[str, Any]]]:
        accounts = _role_accounts(context)
        descriptors = {role: self._descriptor(account_id) for role, account_id in accounts.items()}
        return accounts, descriptors

    def acquire_batch(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="ledger.invoke",
            action="finance.batch.acquire",
            roles={_COORDINATOR},
        )
        values = _strict(
            arguments,
            {"schema_version", "contract", "contract_sha256"},
            "finance batch acquire",
        )
        if values.get("schema_version") != 1 or not isinstance(values.get("contract"), Mapping):
            raise _error("finance batch contract is invalid", "BROKER_ARGUMENT_INVALID")
        contract = dict(values["contract"])
        contract_sha256 = _sha_text(values.get("contract_sha256"), "contract_sha256")
        self._validate_contract(contract, contract_sha256)
        accounts, descriptors = self._descriptors(context)
        repository = self._repository_factory()
        self._mark_write_started(context)
        repository.initialize_schema()
        try:
            seeded = repository.seed_fee_mappings()
            seed_readback = repository.seed_fee_mappings()
            if (
                _integer(seeded, "seeded mapping count") < 0
                or _integer(seed_readback, "seed readback count") != 0
            ):
                raise _error(
                    "finance mapping seed was not stable on a fresh pass",
                    "WRITE_OUTCOME_UNKNOWN",
                )
        except PluginExecutionError as exc:
            if exc.code == "WRITE_OUTCOME_UNKNOWN":
                raise
            raise _error(
                "finance mapping seed outcome is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        except Exception as exc:
            raise _error(
                "finance mapping seed outcome is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        mode = str(contract["mode"])
        requested_targets = self._contract_targets(contract)
        if mode == "retry":
            retry_batch_id = int(contract["retry_batch_id"])
            retry_rows = repository.list_retry_targets(retry_batch_id)
            account_roles = {account_id: role for role, account_id in accounts.items()}
            targets: list[tuple[str, str]] = []
            for raw in retry_rows:
                platform = str(raw.get("platform") or "").strip().lower()
                account_id = str(raw.get("account_id") or "").strip()
                role = account_roles.get(account_id)
                if platform != "ronghui" or role is None:
                    raise _error(
                        "finance retry batch no longer matches this instance",
                        "BROKER_ACCOUNT_MISMATCH",
                    )
                targets.append((role, _business_date(raw.get("target_date"))))
            if not targets or len(targets) != len(set(targets)):
                raise _error("finance retry target set is invalid", "BROKER_SOURCE_INVALID")
            targets.sort(key=lambda item: (_ROLES.index(item[0]), item[1]))
            start_date = min(target_date for _role, target_date in targets)
            end_date = max(target_date for _role, target_date in targets)
        else:
            start_date = str(contract["start_date"])
            end_date = str(contract["end_date"])
            if contract.get("startup_catchup") is True:
                targets = []
                for role, account_id in accounts.items():
                    missing = repository.list_missing_dates(
                        platform="ronghui",
                        account_id=account_id,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    targets.extend((role, item.isoformat()) for item in missing)
                targets.sort(key=lambda item: (_ROLES.index(item[0]), item[1]))
                if not set(targets).issubset(set(requested_targets)):
                    raise _error("finance startup targets changed", "BROKER_SOURCE_INVALID")
            else:
                targets = requested_targets
        marker = (
            f"plugin:v1:{_sha256(context.automation_id)[:24]}:{_sha256(context.plugin_version)[:16]}:{contract_sha256}"
        )
        try:
            batch_id = repository.create_batch(
                trigger_type=str(contract["trigger_type"]),
                start_date=start_date,
                end_date=end_date,
                rescan_days=int(contract["rescan_days"]),
                requested_by=marker,
                earliest_date_status=contract.get("earliest_date_status"),
            )
            batch_id = _integer(batch_id, "batch_id", minimum=1)
            fresh_batch = _canonical_batch_commit_proof(
                repository.read_batch_commit_proof(batch_id)
            )
        except Exception as exc:
            raise _error(
                "finance batch acquisition outcome is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        batch_identity = {
            "batch_id": batch_id,
            "trigger_type": str(contract["trigger_type"]),
            "start_date": start_date,
            "end_date": end_date,
            "rescan_days": int(contract["rescan_days"]),
            "earliest_date_status": contract.get("earliest_date_status"),
            "requested_by": marker,
        }
        if fresh_batch != {
            **batch_identity,
            "status": SyncStatus.RUNNING.value,
            "run_counts": {},
        }:
            raise _error(
                "finance batch acquisition fresh readback changed",
                "WRITE_OUTCOME_UNKNOWN",
            )
        state = _BatchState(
            context_key=_context_key(context),
            batch_id=int(batch_id),
            contract_sha256=contract_sha256,
            targets=tuple(targets),
            accounts=accounts,
            descriptors=descriptors,
            repository=repository,
            batch_identity=batch_identity,
        )
        with self._lock:
            if state.batch_id in self._batches:
                raise _error("finance batch identity collided", "WRITE_OUTCOME_UNKNOWN")
            self._batches[state.batch_id] = state
        safe_targets = [{"source_role": role, "target_date": target_date} for role, target_date in targets]
        proof = {
            "batch_id": state.batch_id,
            "contract_sha256": contract_sha256,
            "targets_sha256": _sha256(safe_targets),
        }
        return {
            "schema_version": 1,
            "acquired": True,
            "batch_id": state.batch_id,
            "contract_sha256": contract_sha256,
            "targets": safe_targets,
            "skipped_disabled_count": 0,
            "evidence_ref": self._evidence(context, "batch-acquire", proof),
        }

    def capture_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="browser.invoke",
            action="ronghui.finance.capture_page",
            roles=set(_ROLES),
        )
        values = _strict(
            arguments,
            {"schema_version", "target_date", "page_number", "page_size", "capture_ref"},
            "finance capture page",
        )
        if values.get("schema_version") != 1:
            raise _error("finance capture version is invalid", "BROKER_ARGUMENT_INVALID")
        target_date = _business_date(values.get("target_date"))
        page_number = _integer(values.get("page_number"), "page_number", minimum=1, maximum=_MAX_PAGES)
        page_size = _integer(values.get("page_size"), "page_size", minimum=1, maximum=_PAGE_SIZE)
        if page_size != _PAGE_SIZE:
            raise _error("finance page size changed", "BROKER_ARGUMENT_INVALID")
        accounts, descriptors = self._descriptors(context)
        account_id = accounts[context.role]
        now = time.monotonic()
        capture_ref = values.get("capture_ref")
        with self._lock:
            expired = [key for key, state in self._captures.items() if state.expires_at <= now]
            for key in expired:
                self._captures.pop(key, None)
                self._source_contexts.pop(key, None)
            state = self._captures.get(str(capture_ref)) if capture_ref else None
        if page_number == 1:
            if capture_ref is not None:
                raise _error("first finance page must not carry a cursor", "BROKER_ARGUMENT_INVALID")
            self._capability_authorizer(
                descriptors[context.role],
                "ronghui_finance",
            )
            captured = self._capture_source(descriptors[context.role], target_date)
            token = self._opaque(context, "capture")
            source_context_ref = self._opaque(context, "source-context")
            state = _CaptureState(
                context_key=_context_key(context),
                role=context.role,
                account_id=account_id,
                target_date=target_date,
                capture_ref=token,
                source_context_ref=source_context_ref,
                captured=captured,
                expires_at=now + _CAPTURE_TTL_SECONDS,
            )
            with self._lock:
                self._captures[token] = state
                self._source_contexts[source_context_ref] = token
        elif state is None:
            raise _error("finance capture cursor expired", "BROKER_CURSOR_INVALID")
        if state is None or (
            state.context_key != _context_key(context)
            or state.role != context.role
            or state.account_id != account_id
            or state.target_date != target_date
        ):
            raise _error("finance capture cursor changed context", "BROKER_CURSOR_INVALID")
        start = (page_number - 1) * page_size
        end = min(start + page_size, state.captured.source_total)
        if start > state.captured.source_total or (
            start == state.captured.source_total and state.captured.source_total > 0
        ):
            raise _error("finance page exceeds the source total", "BROKER_ARGUMENT_INVALID")
        items = [dict(row) for row in state.captured.public_transactions[start:end]]
        complete = end >= state.captured.source_total
        proof = {
            "capture_ref_sha256": _sha256(state.capture_ref),
            "page_number": page_number,
            "page_row_count": len(items),
            "source_total": state.captured.source_total,
        }
        return {
            "schema_version": 1,
            "capture_ref": state.capture_ref,
            "source_context_ref": state.source_context_ref,
            "page_number": page_number,
            "page_row_count": len(items),
            "source_total": state.captured.source_total,
            "items": items,
            "pagination_complete": complete,
            "next_page_number": None if complete else page_number + 1,
            "evidence_ref": self._evidence(context, "capture-page", proof),
        }

    def verify_source_totals(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="browser.invoke",
            action="ronghui.finance.verify_source_totals",
            roles=set(_ROLES),
        )
        values = _strict(
            arguments,
            {
                "schema_version",
                "target_date",
                "capture_ref",
                "source_context_ref",
                "capture_sha256",
                "transaction_count",
                "page_row_counts",
                "computed_metrics",
            },
            "finance source verification",
        )
        if values.get("schema_version") != 1:
            raise _error("finance verify version is invalid", "BROKER_ARGUMENT_INVALID")
        target_date = _business_date(values.get("target_date"))
        capture_ref = _text(values.get("capture_ref"), "capture_ref", maximum=512)
        source_context_ref = _text(
            values.get("source_context_ref"),
            "source_context_ref",
            maximum=512,
        )
        accounts, descriptors = self._descriptors(context)
        with self._lock:
            state = self._captures.get(capture_ref)
            linked_capture = self._source_contexts.get(source_context_ref)
        if state is None or linked_capture != capture_ref or state.expires_at <= time.monotonic():
            raise _error("finance verification context expired", "BROKER_CURSOR_INVALID")
        if (
            state.context_key != _context_key(context)
            or state.role != context.role
            or state.account_id != accounts[context.role]
            or state.target_date != target_date
            or state.source_context_ref != source_context_ref
        ):
            raise _error("finance verification context changed", "BROKER_CURSOR_INVALID")
        expected_capture_sha256 = _sha256(
            {
                "target_date": target_date,
                "transactions": list(state.captured.plugin_transactions),
            }
        )
        supplied_capture_sha256 = _sha_text(values.get("capture_sha256"), "capture_sha256")
        if supplied_capture_sha256 != expected_capture_sha256:
            raise _error("finance capture digest changed", "BROKER_SOURCE_MISMATCH")
        expected_counts = [
            min(_PAGE_SIZE, state.captured.source_total - start)
            for start in range(0, state.captured.source_total, _PAGE_SIZE)
        ] or [0]
        raw_counts = values.get("page_row_counts")
        if not isinstance(raw_counts, list):
            raise _error("finance page counts are invalid", "BROKER_ARGUMENT_INVALID")
        observed_counts = [_integer(value, "page_row_count", maximum=_PAGE_SIZE) for value in raw_counts]
        if observed_counts != expected_counts:
            raise _error("finance page counts changed", "BROKER_SOURCE_MISMATCH")
        if _integer(values.get("transaction_count"), "transaction_count") != state.captured.source_total:
            raise _error("finance transaction count changed", "BROKER_SOURCE_MISMATCH")
        metrics = _strict(values.get("computed_metrics"), _METRIC_FIELDS, "finance metrics")
        expected_metrics = _observed_metrics(state.captured.public_transactions)
        if {
            key: (_integer(metrics[key], key) if key == "transaction_count" else _amount_text(metrics[key], key))
            for key in _METRIC_FIELDS
        } != expected_metrics:
            raise _error("finance computed metrics changed", "BROKER_SOURCE_MISMATCH")

        self._capability_authorizer(
            descriptors[context.role],
            "ronghui_finance",
        )
        fresh = self._capture_source(descriptors[context.role], target_date)
        if (
            fresh.public_transactions != state.captured.public_transactions
            or fresh.public_summaries != state.captured.public_summaries
            or fresh.source_total != state.captured.source_total
            or fresh.source_site_code != state.captured.source_site_code
            or fresh.source_site_name != state.captured.source_site_name
        ):
            raise _error(
                "finance source changed during independent verification",
                "BROKER_SOURCE_CHANGED",
            )
        fresh_metrics = _observed_metrics(fresh.public_transactions)
        if fresh_metrics != expected_metrics:
            raise _error("finance source totals are not repeatable", "BROKER_SOURCE_CHANGED")
        with self._lock:
            current = self._captures.get(capture_ref)
            if current is not state:
                raise _error("finance verification state changed", "BROKER_STATE_CONFLICT")
            state.verified = True
        proof = {
            "capture_sha256": supplied_capture_sha256,
            "remote_total": fresh.source_total,
            "summary_sha256": _sha256(list(fresh.public_summaries)),
        }
        return {
            "schema_version": 1,
            "verified": True,
            "capture_ref": capture_ref,
            "source_context_ref": source_context_ref,
            "capture_sha256": supplied_capture_sha256,
            "remote_total": fresh.source_total,
            "summary_semantics": "signed_net_by_fee",
            "summaries": [dict(row) for row in fresh.public_summaries],
            "observed_metrics": fresh_metrics,
            "evidence_ref": self._evidence(context, "source-verify", proof),
        }

    def _batch(
        self,
        context: CoreBrokerInvocationContext,
        batch_id: int,
        contract_sha256: str,
    ) -> _BatchState:
        with self._lock:
            state = self._batches.get(batch_id)
        if state is None:
            raise _error("finance batch state is unavailable", "BROKER_STATE_UNAVAILABLE")
        if state.context_key != _context_key(context) or state.contract_sha256 != contract_sha256:
            raise _error("finance batch context changed", "BROKER_STATE_CONFLICT")
        if state.finalized_response is not None:
            raise _error("finance batch is already finalized", "BROKER_STATE_CONFLICT")
        return state

    @staticmethod
    def _validate_public_transactions(
        value: object,
        target_date: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > _PAGE_SIZE * _MAX_PAGES:
            raise _error("finance transactions are invalid", "BROKER_ARGUMENT_INVALID")
        rows: list[dict[str, Any]] = []
        identities: set[str] = set()
        for raw in value:
            row = _strict(raw, _SNAPSHOT_TRANSACTION_FIELDS, "finance transaction")
            identity = _text(row.get("source_record_key"), "source_record_key", maximum=191)
            if identity in identities:
                raise _error("finance transaction identity is duplicated", "BROKER_ARGUMENT_INVALID")
            identities.add(identity)
            if _business_date(row.get("business_date"), "business_date") != target_date:
                raise _error("finance transaction date changed", "BROKER_ARGUMENT_INVALID")
            for field_name in ("income", "expense", "before_balance", "after_balance"):
                row[field_name] = _amount_text(row.get(field_name), field_name)
            expected_direction = "income" if _amount(row["income"], "income") > 0 else "expense"
            if row.get("direction") != expected_direction:
                raise _error("finance transaction direction changed", "BROKER_ARGUMENT_INVALID")
            if not isinstance(row.get("source_payload"), Mapping):
                raise _error("finance source payload is invalid", "BROKER_ARGUMENT_INVALID")
            row["source_payload"] = dict(row["source_payload"])
            rows.append(row)
        return rows

    @staticmethod
    def _validate_public_summaries(
        value: object,
        target_date: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > _PAGE_SIZE * _MAX_PAGES:
            raise _error("finance summaries are invalid", "BROKER_ARGUMENT_INVALID")
        rows: list[dict[str, Any]] = []
        for raw in value:
            row = _strict(raw, _SNAPSHOT_SUMMARY_FIELDS, "finance summary")
            if _business_date(row.get("target_date"), "summary target_date") != target_date:
                raise _error("finance summary date changed", "BROKER_ARGUMENT_INVALID")
            row["income"] = _amount_text(row.get("income"), "summary income")
            row["expense"] = _amount_text(row.get("expense"), "summary expense")
            expected_direction = "income" if _amount(row["income"], "summary income") > 0 else "expense"
            if row.get("direction") != expected_direction:
                raise _error("finance summary direction changed", "BROKER_ARGUMENT_INVALID")
            rows.append(row)
        return rows

    def write_snapshot(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="ledger.invoke",
            action="finance.source_snapshot.write",
            roles=set(_ROLES),
        )
        accounts, _descriptors = self._descriptors(context)
        common = {"schema_version", "batch_id", "contract_sha256", "target_date", "outcome"}
        if not isinstance(arguments, Mapping):
            raise _error("finance snapshot request is invalid", "BROKER_ARGUMENT_INVALID")
        outcome = str(arguments.get("outcome") or "").strip()
        expected_fields = (
            common | {"failure"}
            if outcome == "failed"
            else common
            | {
                "capture_ref",
                "source_context_ref",
                "transactions",
                "summaries",
                "validation",
                "validation_sha256",
            }
        )
        values = _strict(arguments, expected_fields, "finance snapshot request")
        if values.get("schema_version") != 1 or outcome not in {"success", "no_data", "failed"}:
            raise _error("finance snapshot outcome is invalid", "BROKER_ARGUMENT_INVALID")
        batch_id = _integer(values.get("batch_id"), "batch_id", minimum=1)
        contract_sha256 = _sha_text(values.get("contract_sha256"), "contract_sha256")
        target_date = _business_date(values.get("target_date"))
        batch = self._batch(context, batch_id, contract_sha256)
        target_key = (context.role, target_date)
        if target_key not in set(batch.targets):
            raise _error("finance snapshot target was not acquired", "BROKER_STATE_CONFLICT")
        if batch.accounts.get(context.role) != accounts.get(context.role):
            raise _error("finance snapshot account binding changed", "BROKER_ACCOUNT_MISMATCH")
        with self._lock:
            if target_key in batch.receipts:
                raise _error("finance snapshot target is already committed", "BROKER_STATE_CONFLICT")

        repository = batch.repository
        descriptor = batch.descriptors[context.role]
        validation_sha256: str | None = None
        summary_count = 0
        detail_income = Decimal("0.0000")
        detail_expense = Decimal("0.0000")
        summary_income = Decimal("0.0000")
        summary_expense = Decimal("0.0000")
        if outcome == "failed":
            failure = _strict(values.get("failure"), {"code", "stage"}, "finance failure")
            code = _text(failure.get("code"), "failure code", maximum=64)
            stage = _text(failure.get("stage"), "failure stage", maximum=64)
            self._mark_write_started(context)
            run_id = repository.start_failed_run(
                batch_id=batch_id,
                platform="ronghui",
                account_id=str(descriptor["account_id"]),
                target_date=target_date,
                error_code=code,
                error_message=f"signed finance capture failed at {stage}",
            )
            record_count = written_count = 0
            new_fee_item_count = historical_revision_count = 0
        else:
            capture_ref = _text(values.get("capture_ref"), "capture_ref", maximum=512)
            source_context_ref = _text(
                values.get("source_context_ref"),
                "source_context_ref",
                maximum=512,
            )
            with self._lock:
                capture_state = self._captures.get(capture_ref)
            if (
                capture_state is None
                or capture_state.source_context_ref != source_context_ref
                or capture_state.context_key != _context_key(context)
                or capture_state.role != context.role
                or capture_state.account_id != accounts[context.role]
                or capture_state.target_date != target_date
                or capture_state.verified is not True
                or capture_state.expires_at <= time.monotonic()
            ):
                raise _error("finance snapshot lacks verified capture evidence", "BROKER_STATE_CONFLICT")
            transactions = self._validate_public_transactions(values.get("transactions"), target_date)
            summaries = self._validate_public_summaries(values.get("summaries"), target_date)
            detail_income = sum(
                (_amount(row["income"], "income") for row in transactions),
                Decimal("0.0000"),
            )
            detail_expense = sum(
                (_amount(row["expense"], "expense") for row in transactions),
                Decimal("0.0000"),
            )
            summary_income = sum(
                (_amount(row["income"], "summary income") for row in summaries),
                Decimal("0.0000"),
            )
            summary_expense = sum(
                (_amount(row["expense"], "summary expense") for row in summaries),
                Decimal("0.0000"),
            )
            if transactions != list(capture_state.captured.plugin_transactions):
                raise _error("finance snapshot transactions changed", "BROKER_SOURCE_MISMATCH")
            if summaries != list(capture_state.captured.plugin_summaries):
                raise _error("finance snapshot summaries changed", "BROKER_SOURCE_MISMATCH")
            validation = values.get("validation")
            if not isinstance(validation, Mapping):
                raise _error("finance validation report is invalid", "BROKER_ARGUMENT_INVALID")
            validation_sha256 = _sha_text(
                values.get("validation_sha256"),
                "validation_sha256",
            )
            if _sha256(validation) != validation_sha256:
                raise _error("finance validation digest changed", "BROKER_ARGUMENT_INVALID")
            if str(validation.get("capture_sha256") or "") != _sha256(
                {
                    "target_date": target_date,
                    "transactions": transactions,
                }
            ):
                raise _error("finance validation capture digest changed", "BROKER_ARGUMENT_INVALID")
            internal_validation = validate_finance_capture_result(
                capture_state.captured.capture,
                capture_state.captured.transactions,
                capture_state.captured.summaries,
                repository=repository,
            )
            if not bool(getattr(internal_validation, "passed", False)):
                raise _error("finance shared validation failed", "BROKER_SOURCE_INVALID")
            if outcome == "no_data":
                if transactions or summaries:
                    raise _error("finance no-data snapshot is not empty", "BROKER_ARGUMENT_INVALID")
            elif not transactions:
                raise _error("finance success snapshot is empty", "BROKER_ARGUMENT_INVALID")
            self._mark_write_started(context)
            run_id = repository.start_run(
                batch_id=batch_id,
                platform="ronghui",
                account_id=str(descriptor["account_id"]),
                login_account=str(descriptor["login_account"]),
                session_profile=str(descriptor["session_profile"]),
                target_date=target_date,
                source_site_code=capture_state.captured.source_site_code,
                source_site_name=capture_state.captured.source_site_name,
            )
            try:
                if outcome == "no_data":
                    repository.mark_run_no_data(
                        run_id=run_id,
                        validation=internal_validation,
                    )
                    commit_result: Mapping[str, Any] = {}
                else:
                    commit_result = repository.commit_run_snapshot(
                        run_id=run_id,
                        transactions=capture_state.captured.transactions,
                        summaries=capture_state.captured.summaries,
                        validation=internal_validation,
                    )
            except Exception as exc:
                raise _error(
                    "finance snapshot commit outcome is unknown",
                    "WRITE_OUTCOME_UNKNOWN",
                ) from exc
            record_count = len(transactions)
            summary_count = len(summaries)
            written_count = record_count
            derivatives = commit_result.get("derivatives") if isinstance(commit_result, Mapping) else {}
            derivatives = derivatives if isinstance(derivatives, Mapping) else {}
            new_fee_item_count = _integer(
                derivatives.get("new_fee_item_count", 0),
                "new_fee_item_count",
            )
            historical_revision_count = _integer(
                derivatives.get("historical_revision_count", 0),
                "historical_revision_count",
            )

        expected_run_proof = {
            "run_id": int(run_id),
            "batch_id": batch_id,
            "platform": "ronghui",
            "account_id": str(descriptor["account_id"]),
            "target_date": target_date,
            "status": {
                "failed": SyncStatus.FAILED.value,
                "no_data": SyncStatus.NO_DATA.value,
                "success": SyncStatus.SUCCESS.value,
            }[outcome],
            "remote_total": record_count,
            "unique_row_count": record_count,
            "written_row_count": record_count,
            "transaction_count": record_count,
            "transaction_unique_count": record_count,
            "transaction_income": f"{detail_income:.4f}",
            "transaction_expense": f"{detail_expense:.4f}",
            "summary_count": summary_count,
            "summary_income": f"{summary_income:.4f}",
            "summary_expense": f"{summary_expense:.4f}",
        }
        try:
            fresh_run_proof = _canonical_run_commit_proof(
                repository.read_run_commit_proof(int(run_id))
            )
        except Exception as exc:
            raise _error(
                "finance snapshot commit readback is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        if fresh_run_proof != expected_run_proof:
            raise _error(
                "finance snapshot commit fresh readback changed",
                "WRITE_OUTCOME_UNKNOWN",
            )

        run_ref = self._opaque(context, "run")
        receipt = _RunReceipt(
            role=context.role,
            target_date=target_date,
            run_id=int(run_id),
            run_ref=run_ref,
            outcome=outcome,
            record_count=record_count,
            validation_sha256=validation_sha256,
            summary_count=summary_count,
        )
        with self._lock:
            if target_key in batch.receipts:
                raise _error("finance snapshot target raced", "BROKER_STATE_CONFLICT")
            batch.receipts[target_key] = receipt
        proof = {
            "batch_id": batch_id,
            "role": context.role,
            "target_date": target_date,
            "outcome": outcome,
            "run_ref_sha256": _sha256(run_ref),
        }
        return {
            "schema_version": 1,
            "committed": True,
            "batch_id": batch_id,
            "outcome": outcome,
            "record_count": record_count,
            "summary_count": summary_count,
            "written_row_count": written_count,
            "run_ref": run_ref,
            "validation_sha256": validation_sha256,
            "new_fee_item_count": new_fee_item_count,
            "historical_revision_count": historical_revision_count,
            "evidence_ref": self._evidence(context, "snapshot-write", proof),
        }

    def commit_projection(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="ledger.invoke",
            action="finance.projection.commit",
            roles={_COORDINATOR},
        )
        values = _strict(
            arguments,
            {"schema_version", "batch_id", "contract_sha256", "outcomes"},
            "finance projection commit",
        )
        if values.get("schema_version") != 1:
            raise _error("finance projection version is invalid", "BROKER_ARGUMENT_INVALID")
        batch_id = _integer(values.get("batch_id"), "batch_id", minimum=1)
        contract_sha256 = _sha_text(values.get("contract_sha256"), "contract_sha256")
        request_sha256 = _sha256(values)
        with self._lock:
            existing = self._batches.get(batch_id)
            if (
                existing is not None
                and existing.finalized_response is not None
                and existing.finalized_request_sha256 == request_sha256
            ):
                return dict(existing.finalized_response)
        batch = self._batch(context, batch_id, contract_sha256)
        raw_outcomes = values.get("outcomes")
        if not isinstance(raw_outcomes, list):
            raise _error("finance projection outcomes are invalid", "BROKER_ARGUMENT_INVALID")
        observed: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in raw_outcomes:
            row = _strict(
                raw,
                {
                    "source_role",
                    "target_date",
                    "run_ref",
                    "outcome",
                    "record_count",
                    "validation_sha256",
                },
                "finance projection outcome",
            )
            role = _text(row.get("source_role"), "source_role", maximum=64)
            if role not in _ROLES:
                raise _error("finance projection role is invalid", "BROKER_ARGUMENT_INVALID")
            target_date = _business_date(row.get("target_date"))
            key = (role, target_date)
            if key in observed:
                raise _error("finance projection outcome is duplicated", "BROKER_ARGUMENT_INVALID")
            observed[key] = row
        if tuple(observed) != batch.targets or set(batch.receipts) != set(batch.targets):
            raise _error("finance projection run set is incomplete", "BROKER_STATE_CONFLICT")
        for key, row in observed.items():
            receipt = batch.receipts[key]
            supplied_validation = row.get("validation_sha256")
            if supplied_validation is not None:
                supplied_validation = _sha_text(supplied_validation, "validation_sha256")
            if (
                _text(row.get("run_ref"), "run_ref", maximum=512) != receipt.run_ref
                or str(row.get("outcome") or "") != receipt.outcome
                or _integer(row.get("record_count"), "record_count") != receipt.record_count
                or supplied_validation != receipt.validation_sha256
            ):
                raise _error("finance projection receipt changed", "BROKER_STATE_CONFLICT")
        self._mark_write_started(context)
        try:
            status = batch.repository.finalize_batch(batch_id)
        except Exception as exc:
            raise _error(
                "finance batch finalization outcome is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        status_value = str(getattr(status, "value", status))
        completed = [receipt for receipt in batch.receipts.values() if receipt.outcome != "failed"]
        no_data = [receipt for receipt in completed if receipt.outcome == "no_data"]
        failed = [receipt for receipt in batch.receipts.values() if receipt.outcome == "failed"]
        expected_status = (
            "partial_failed"
            if failed and completed
            else "failed"
            if failed
            else "no_data"
            if not batch.receipts
            else "success"
        )
        repository_expected = {
            "partial_failed": SyncStatus.PARTIAL_FAILED.value,
            "failed": SyncStatus.FAILED.value,
            "no_data": SyncStatus.NO_DATA.value,
            "success": SyncStatus.SUCCESS.value,
        }[expected_status]
        if status_value != repository_expected:
            raise _error(
                "finance finalized status changed",
                "WRITE_OUTCOME_UNKNOWN",
            )
        expected_run_counts: dict[str, int] = {}
        for receipt in batch.receipts.values():
            persisted_status = {
                "failed": SyncStatus.FAILED.value,
                "no_data": SyncStatus.NO_DATA.value,
                "success": SyncStatus.SUCCESS.value,
            }[receipt.outcome]
            expected_run_counts[persisted_status] = (
                expected_run_counts.get(persisted_status, 0) + 1
            )
        try:
            fresh_batch = _canonical_batch_commit_proof(
                batch.repository.read_batch_commit_proof(batch_id)
            )
        except Exception as exc:
            raise _error(
                "finance batch finalization readback is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        if fresh_batch != {
            **batch.batch_identity,
            "status": repository_expected,
            "run_counts": expected_run_counts,
        }:
            raise _error(
                "finance batch finalization fresh readback changed",
                "WRITE_OUTCOME_UNKNOWN",
            )
        written = sum(receipt.record_count for receipt in completed)
        proof = {
            "batch_id": batch_id,
            "contract_sha256": contract_sha256,
            "status": expected_status,
            "receipts_sha256": _sha256(values["outcomes"]),
        }
        response = {
            "schema_version": 1,
            "committed": True,
            "batch_id": batch_id,
            "contract_sha256": contract_sha256,
            "status": expected_status,
            "successful_runs": len(completed),
            "no_data_runs": len(no_data),
            "failed_runs": len(failed),
            "written_record_count": written,
            "evidence_ref": self._evidence(context, "projection-commit", proof),
        }
        with self._lock:
            if batch.finalized_response is not None:
                raise _error("finance projection commit raced", "BROKER_STATE_CONFLICT")
            batch.finalized_request_sha256 = request_sha256
            batch.finalized_response = dict(response)
        return response

    def handler_map(self) -> dict[tuple[str, str], CoreBrokerHandler]:
        return {
            ("ledger.invoke", "finance.batch.acquire"): self.acquire_batch,
            ("browser.invoke", "ronghui.finance.capture_page"): self.capture_page,
            (
                "browser.invoke",
                "ronghui.finance.verify_source_totals",
            ): self.verify_source_totals,
            ("ledger.invoke", "finance.source_snapshot.write"): self.write_snapshot,
            ("ledger.invoke", "finance.projection.commit"): self.commit_projection,
        }


def _default_repository_factory() -> FinanceRepositoryPort:
    from agent.workflow_resource_store import _connect

    return FinanceRepository(_connect)


def _default_capture_port(
    descriptor: Mapping[str, Any],
    target_date: date,
) -> CaptureResult:
    binding = FinanceAccountBinding(
        system=str(descriptor["system"]),
        account_id=str(descriptor["account_id"]),
        login_account=str(descriptor["login_account"]),
        session_profile=str(descriptor["session_profile"]),
    )
    adapter = build_live_finance_adapter(binding)
    try:
        discovered = adapter.discover()
        if not isinstance(discovered, Mapping):
            raise _error("finance source discovery is invalid", "BROKER_SOURCE_INVALID")
        capture = adapter.fetch_day(target_date)
        source_site_code = str(discovered.get("source_site_code") or "").strip()
        source_site_name = str(discovered.get("source_site_name") or "").strip()
        if (
            source_site_code != str(capture.source_site_code or "").strip()
            or source_site_name != str(capture.source_site_name or "").strip()
        ):
            raise _error("finance source site changed during capture", "BROKER_SOURCE_MISMATCH")
        return capture
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def build_production_finance_handler_map(
    *,
    cursor_secret: bytes,
    account_manager: AutomationAccountManager | None = None,
    repository_factory: RepositoryFactory | None = None,
    capture_port: CapturePort | None = None,
    capability_authorizer: CapabilityAuthorizer | None = None,
) -> dict[tuple[str, str], CoreBrokerHandler]:
    handlers = _FinanceBrokerHandlers(
        account_manager=account_manager or get_account_manager(),
        repository_factory=repository_factory or _default_repository_factory,
        capture_port=capture_port or _default_capture_port,
        cursor_secret=cursor_secret,
        capability_authorizer=capability_authorizer or authorize_target_capability,
    )
    return handlers.handler_map()


__all__ = ["MARKED_WRITE_ACTION_KEYS", "build_production_finance_handler_map"]
