from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from shared.finance import (
    InvalidAmountError,
    MissingAmountError,
    format_money,
    quantize_storage,
    to_decimal,
)


def test_decimal_conversion_and_display_rounding_are_financially_explicit() -> None:
    assert to_decimal("1,234.56789") == Decimal("1234.56789")
    assert quantize_storage("1.23456") == Decimal("1.2346")
    assert format_money("1.005") == "1.01"
    assert Decimal(format_money("1.005")) == Decimal("1.005").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def test_missing_non_finite_and_boolean_amounts_fail() -> None:
    with pytest.raises(MissingAmountError):
        to_decimal("")
    with pytest.raises(InvalidAmountError):
        to_decimal("NaN")
    with pytest.raises(InvalidAmountError):
        to_decimal(True)
    assert to_decimal(None, missing_as_zero=True) == Decimal("0.0000")
