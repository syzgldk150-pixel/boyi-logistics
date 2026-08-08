"""Financial amount conversion shared by finance collection and reporting.

The storage scale is four decimal places.  Rounding to two decimal places only
happens at the presentation boundary.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


STORAGE_SCALE = Decimal("0.0001")
DISPLAY_SCALE = Decimal("0.01")
ZERO = Decimal("0.0000")


class MissingAmountError(ValueError):
    """Raised when a business amount is missing and zero was not authorised."""


class InvalidAmountError(ValueError):
    """Raised when a value cannot safely represent a finite decimal amount."""


def to_decimal(value: Any, *, missing_as_zero: bool = False) -> Decimal:
    """Convert a business amount without passing through binary float math.

    ``missing_as_zero`` must only be used by an adapter after the source field's
    blank semantics have been verified.  The shared layer defaults to failure.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        if missing_as_zero:
            return ZERO
        raise MissingAmountError("amount is missing; zero was not authorised")
    if isinstance(value, bool):
        raise InvalidAmountError("boolean is not a financial amount")
    text = str(value).strip().replace(",", "")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise InvalidAmountError("amount is not a valid decimal") from exc
    if not amount.is_finite():
        raise InvalidAmountError("amount must be finite")
    return amount


def quantize_storage(value: Any, *, missing_as_zero: bool = False) -> Decimal:
    """Return a value suitable for ``DECIMAL(20,4)`` storage."""

    return to_decimal(value, missing_as_zero=missing_as_zero).quantize(
        STORAGE_SCALE,
        rounding=ROUND_HALF_UP,
    )


def quantize_money(value: Any, *, missing_as_zero: bool = False) -> Decimal:
    """Round a final displayed amount to cents using ``ROUND_HALF_UP``."""

    return to_decimal(value, missing_as_zero=missing_as_zero).quantize(
        DISPLAY_SCALE,
        rounding=ROUND_HALF_UP,
    )


def format_money(value: Any, *, missing_as_zero: bool = False) -> str:
    """Format a final displayed amount with exactly two decimal places."""

    return f"{quantize_money(value, missing_as_zero=missing_as_zero):.2f}"


def decimal_to_storage_text(value: Any, *, missing_as_zero: bool = False) -> str:
    """Serialize an amount for an API or DB driver without using ``float``."""

    return f"{quantize_storage(value, missing_as_zero=missing_as_zero):.4f}"


def optional_decimal(value: Any) -> Decimal | None:
    """Convert a nullable amount while preserving the distinction from zero."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return quantize_storage(value)
