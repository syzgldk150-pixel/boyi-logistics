"""Framework-independent finance domain models."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from shared.finance.money import ZERO, optional_decimal, quantize_storage


class Platform(str, Enum):
    RONGHUI = "ronghui"
    YUNDA = "yunda"


class Direction(str, Enum):
    INCOME = "income"
    EXPENSE = "expense"


class FeeLevel(str, Enum):
    WAYBILL = "waybill"
    OPERATING = "operating"


class MappingStatus(str, Enum):
    PENDING = "pending"
    BOUND = "bound"


class SyncStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    NO_DATA = "no_data"


class ValidationStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class EarliestDateStatus(str, Enum):
    CONFIRMED = "confirmed"
    EARLIEST_DATE_UNCONFIRMED = "EARLIEST_DATE_UNCONFIRMED"


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str:
    return str(value or "").strip()


def _as_date(value: dt.date | str, field_name: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def month_start(value: dt.date | str) -> dt.date:
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 7:
            text = f"{text}-01"
        parsed = _as_date(text, "month")
    else:
        parsed = _as_date(value, "month")
    return parsed.replace(day=1)


def _as_datetime(value: dt.datetime | str | None) -> dt.datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, dt.datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("transaction_at must be an ISO datetime") from exc


TRANSACTION_SOURCE_PAYLOAD_ALLOWLIST = frozenset(
    {
        # Ronghui business identifiers and measures.
        "BALANCE_ORDER",
        "BILL_CODE",
        "FINANCE_DATE",
        "BALANCE_DATE",
        "BALANCE_TYPE",
        "CENTER_NAME",
        "SITE_NAME",
        "QUANTITY",
        "SETTLEMENT_WEIGHT",
        "SOURCE",
        # Yunda business identifiers and organisational labels.
        "serial_no",
        "logistics_Id",
        "trade_time_ex",
        "buz_date",
        "trade_type",
        "trade_amount",
        "project_id",
        "project_name",
        "project_name_s",
        "sub_project_name",
        "parent_project_name",
        "account_no",
        "account_type",
        "charge_org_name",
        "branch_name",
        "trade_code",
        "business_code",
        "data_source",
    }
)

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "cookie",
    "token",
    "secret",
    "captcha",
    "authorization",
    "sso",
)


def sanitize_source_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Retain only reviewed business fields and reject credential-like keys."""

    if not payload:
        return {}
    for key in payload:
        normalized = str(key).strip().lower()
        if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
            raise ValueError(f"sensitive source field is forbidden: {key}")
    return {
        str(key): value
        for key, value in payload.items()
        if str(key) in TRANSACTION_SOURCE_PAYLOAD_ALLOWLIST
    }


@dataclass(frozen=True)
class AccountBinding:
    account_id: str
    system: str
    login_account: str
    session_profile: str
    display_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _required_text(self.account_id, "account_id"))
        object.__setattr__(self, "system", _required_text(self.system, "system"))
        object.__setattr__(
            self,
            "login_account",
            _required_text(self.login_account, "login_account"),
        )
        object.__setattr__(
            self,
            "session_profile",
            _required_text(self.session_profile, "session_profile"),
        )
        object.__setattr__(self, "display_name", _optional_text(self.display_name))


@dataclass(frozen=True)
class FeeItemKey:
    platform: Platform
    primary_fee_name: str
    secondary_fee_name: str
    direction: Direction

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", Platform(self.platform))
        object.__setattr__(self, "direction", Direction(self.direction))
        object.__setattr__(
            self,
            "primary_fee_name",
            _required_text(self.primary_fee_name, "primary_fee_name"),
        )
        object.__setattr__(self, "secondary_fee_name", _optional_text(self.secondary_fee_name))


@dataclass(frozen=True)
class TransactionRecord:
    platform: Platform
    account_id: str
    login_account: str
    source_record_key: str
    business_date: dt.date | str
    primary_fee_name: str
    direction: Direction
    income: Any
    expense: Any
    secondary_fee_name: str = ""
    transaction_at: dt.datetime | str | None = None
    before_balance: Any | None = None
    after_balance: Any | None = None
    waybill_no: str = ""
    source_reference: str = ""
    remark: str = ""
    source_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", Platform(self.platform))
        object.__setattr__(self, "direction", Direction(self.direction))
        for field_name in ("account_id", "login_account", "source_record_key", "primary_fee_name"):
            object.__setattr__(self, field_name, _required_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "secondary_fee_name", _optional_text(self.secondary_fee_name))
        object.__setattr__(self, "business_date", _as_date(self.business_date, "business_date"))
        object.__setattr__(self, "transaction_at", _as_datetime(self.transaction_at))
        income = quantize_storage(self.income)
        expense = quantize_storage(self.expense)
        if income < ZERO or expense < ZERO:
            raise ValueError("normalized income and expense must be non-negative")
        if self.direction is Direction.INCOME and expense != ZERO:
            raise ValueError("income transaction cannot contain expense")
        if self.direction is Direction.EXPENSE and income != ZERO:
            raise ValueError("expense transaction cannot contain income")
        object.__setattr__(self, "income", income)
        object.__setattr__(self, "expense", expense)
        object.__setattr__(self, "before_balance", optional_decimal(self.before_balance))
        object.__setattr__(self, "after_balance", optional_decimal(self.after_balance))
        for field_name in ("waybill_no", "source_reference", "remark"):
            object.__setattr__(self, field_name, _optional_text(getattr(self, field_name)))
        object.__setattr__(self, "source_payload", sanitize_source_payload(self.source_payload))

    @property
    def net_change(self) -> Decimal:
        return self.income - self.expense

    @property
    def fee_key(self) -> FeeItemKey:
        return FeeItemKey(
            platform=self.platform,
            primary_fee_name=self.primary_fee_name,
            secondary_fee_name=self.secondary_fee_name,
            direction=self.direction,
        )

    def comparison_payload(self) -> tuple[Any, ...]:
        """Stable, non-sensitive content used to detect same-key conflicts."""

        return (
            self.platform.value,
            self.account_id,
            self.business_date.isoformat(),
            self.transaction_at.isoformat() if self.transaction_at else "",
            self.primary_fee_name,
            self.secondary_fee_name,
            self.direction.value,
            str(self.income),
            str(self.expense),
            str(self.before_balance) if self.before_balance is not None else "",
            str(self.after_balance) if self.after_balance is not None else "",
            self.waybill_no,
            self.source_reference,
            self.remark,
        )


@dataclass(frozen=True)
class SummarySnapshot:
    platform: Platform
    account_id: str
    target_date: dt.date | str
    primary_fee_name: str
    direction: Direction
    income: Any
    expense: Any
    secondary_fee_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", Platform(self.platform))
        object.__setattr__(self, "direction", Direction(self.direction))
        object.__setattr__(self, "account_id", _required_text(self.account_id, "account_id"))
        object.__setattr__(
            self,
            "primary_fee_name",
            _required_text(self.primary_fee_name, "primary_fee_name"),
        )
        object.__setattr__(self, "secondary_fee_name", _optional_text(self.secondary_fee_name))
        object.__setattr__(self, "target_date", _as_date(self.target_date, "target_date"))
        income = quantize_storage(self.income)
        expense = quantize_storage(self.expense)
        if income < ZERO or expense < ZERO:
            raise ValueError("normalized summary income and expense must be non-negative")
        if self.direction is Direction.INCOME and expense != ZERO:
            raise ValueError("income summary cannot contain expense")
        if self.direction is Direction.EXPENSE and income != ZERO:
            raise ValueError("expense summary cannot contain income")
        object.__setattr__(self, "income", income)
        object.__setattr__(self, "expense", expense)

    @property
    def net_change(self) -> Decimal:
        return self.income - self.expense

    @property
    def grouping_key(self) -> tuple[str, str, str]:
        return (self.primary_fee_name, self.secondary_fee_name, self.direction.value)


@dataclass(frozen=True)
class FeeMappingSeed:
    platform: Platform
    primary_fee_name: str
    direction: Direction
    fee_level: FeeLevel
    booking_fee_name: str
    include_in_cost: bool
    secondary_fee_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", Platform(self.platform))
        object.__setattr__(self, "direction", Direction(self.direction))
        object.__setattr__(self, "fee_level", FeeLevel(self.fee_level))
        object.__setattr__(
            self,
            "primary_fee_name",
            _required_text(self.primary_fee_name, "primary_fee_name"),
        )
        object.__setattr__(self, "secondary_fee_name", _optional_text(self.secondary_fee_name))
        booking_fee_name = _optional_text(self.booking_fee_name)
        if self.fee_level is FeeLevel.WAYBILL and not booking_fee_name:
            raise ValueError("waybill mapping requires booking_fee_name")
        if self.fee_level is FeeLevel.OPERATING and booking_fee_name:
            raise ValueError("operating mapping cannot target a waybill-entry fee item")
        object.__setattr__(self, "booking_fee_name", booking_fee_name)
        if self.include_in_cost and self.direction is not Direction.EXPENSE:
            raise ValueError("only an expense mapping may be included in cost")


@dataclass(frozen=True)
class FinanceQuery:
    start_date: dt.date | str
    end_date: dt.date | str
    platform: Platform | str | None = None
    account_id: str | None = None
    direction: Direction | str | None = None
    fee_level: FeeLevel | str | None = None
    fee_name: str | None = None
    waybill_no: str | None = None

    def __post_init__(self) -> None:
        start_date = _as_date(self.start_date, "start_date")
        end_date = _as_date(self.end_date, "end_date")
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        object.__setattr__(self, "start_date", start_date)
        object.__setattr__(self, "end_date", end_date)
        if self.platform is not None and str(self.platform).strip():
            object.__setattr__(self, "platform", Platform(self.platform))
        else:
            object.__setattr__(self, "platform", None)
        if self.direction is not None and str(self.direction).strip():
            object.__setattr__(self, "direction", Direction(self.direction))
        else:
            object.__setattr__(self, "direction", None)
        if self.fee_level is not None and str(self.fee_level).strip():
            object.__setattr__(self, "fee_level", FeeLevel(self.fee_level))
        else:
            object.__setattr__(self, "fee_level", None)
        for field_name in ("account_id", "fee_name", "waybill_no"):
            object.__setattr__(self, field_name, _optional_text(getattr(self, field_name)))
