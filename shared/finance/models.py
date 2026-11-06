"""Value types and closed vocabularies of the general ledger."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from shared.finance.errors import FinanceValidationError

ROOT_TYPES = ("asset", "liability", "equity", "cost", "profit_loss")
ACCOUNT_TYPES = (
    "cash", "bank", "fund", "transit", "receivable", "payable", "suspense",
    "income", "cost", "expense", "equity", "other",
)
NORMAL_BALANCES = ("debit", "credit")
CASH_FLOW_CATEGORIES = ("operating", "investing", "financing")
FEE_LEVELS = ("waybill", "operation")
# Voucher types a person may create by hand; the others are produced by rules,
# connectors and period closing in later phases.
MANUAL_VOUCHER_TYPES = ("manual", "opening")
VOUCHER_WORD = "记"
DIMENSIONS = ("site_code", "source_id", "business_line", "fee_level", "waybill_no")

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ACCOUNT_CODE = re.compile(r"^[1-6]\d{3}(\d{2}){0,4}$")


@dataclass(frozen=True, slots=True)
class FinanceActor:
    """The authenticated person behind a write; ids come from the session only."""

    actor_id: str
    name: str

    def __post_init__(self) -> None:
        if not str(self.actor_id or "").strip():
            raise FinanceValidationError("无法识别当前操作人，请重新登录后重试。", code="FINANCE_ACTOR_REQUIRED")

    @property
    def label(self) -> str:
        return (self.name or self.actor_id).strip()[:128]


def parse_month(value: Any, field_name: str = "月份") -> str:
    text = str(value or "").strip()
    if not _MONTH.fullmatch(text):
        raise FinanceValidationError(f"{field_name}格式应为 YYYY-MM。", code="FINANCE_INVALID_MONTH")
    return text


def parse_date(value: Any, field_name: str = "日期") -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    try:
        if len(text) != 10:
            raise ValueError(text)
        return dt.date.fromisoformat(text)
    except ValueError:
        raise FinanceValidationError(f"{field_name}格式应为 YYYY-MM-DD。", code="FINANCE_INVALID_DATE") from None


def month_of(day: dt.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    year, number = int(month[:4]), int(month[5:])
    start = dt.date(year, number, 1)
    end = dt.date(year + (number == 12), number % 12 + 1, 1) - dt.timedelta(days=1)
    return start, end


def parse_account_code(value: Any) -> str:
    text = str(value or "").strip()
    if not _ACCOUNT_CODE.fullmatch(text):
        raise FinanceValidationError("科目代码应为 4 位一级代码，下级每级加 2 位数字。", code="FINANCE_INVALID_ACCOUNT_CODE")
    return text


def parse_request_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    try:
        canonical = str(uuid.UUID(text))
    except ValueError:
        canonical = ""
    if canonical != text:
        raise FinanceValidationError("请求标识无效，请刷新页面后重试。", code="FINANCE_INVALID_REQUEST_ID")
    return canonical


def parse_amount(value: Any, field_name: str) -> Decimal:
    """An entered amount: a decimal string with at most 4 places, never a float."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return Decimal("0")
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise FinanceValidationError(f"{field_name}必须是数字文本。", code="AMOUNT_INVALID")
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise FinanceValidationError(f"{field_name}不是有效金额。", code="AMOUNT_INVALID") from None
    if not amount.is_finite() or amount.as_tuple().exponent < -4:
        raise FinanceValidationError(f"{field_name}最多保留 4 位小数。", code="AMOUNT_INVALID")
    if amount < 0:
        raise FinanceValidationError(f"{field_name}不能为负数，请改填另一方向。", code="AMOUNT_INVALID")
    if amount >= Decimal("1e16"):
        raise FinanceValidationError(f"{field_name}超出允许范围。", code="AMOUNT_INVALID")
    return amount


def optional_text(value: Any, field_name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise FinanceValidationError(f"{field_name}必须是文本。")
    text = str(value).strip()
    if len(text) > limit:
        raise FinanceValidationError(f"{field_name}不能超过 {limit} 个字符。")
    return text or None


def expected_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FinanceValidationError("缺少版本号，请刷新页面后重试。", code="FINANCE_REVISION_REQUIRED")
    return value
