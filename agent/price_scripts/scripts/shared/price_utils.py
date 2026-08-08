# -*- coding: utf-8 -*-
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.01")
DEFAULT_MARGIN = Decimal("0.15")


def to_decimal(value: Any, default: str | Decimal = "0") -> Decimal:
    """稳定将任意数值转为 Decimal，空值回退到 default。"""
    if value is None:
        return Decimal(str(default))
    if isinstance(value, Decimal):
        return value

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return Decimal(str(default))
    return Decimal(text)


def maybe_decimal(value: Any) -> Decimal | None:
    try:
        return to_decimal(value)
    except (InvalidOperation, ValueError):
        return None


def quantize_money(value: Any, rounding=ROUND_CEILING) -> Decimal:
    return to_decimal(value).quantize(MONEY_QUANT, rounding=rounding)


def money_to_float(value: Any, rounding=ROUND_CEILING) -> float:
    return float(quantize_money(value, rounding=rounding))


def safe_div(
    numerator: Any,
    denominator: Any,
    *,
    quant: Decimal | None = None,
    rounding=ROUND_CEILING,
) -> Decimal:
    den = to_decimal(denominator)
    if den == ZERO:
        raise ZeroDivisionError("denominator must not be 0")
    result = to_decimal(numerator) / den
    if quant is not None:
        return result.quantize(quant, rounding=rounding)
    return result


def calc_sum_fee(
    calc_data: Mapping[str, Any],
    discount_keys: Iterable[str],
    skip_keys: Iterable[str] = ("INSURANCE",),
) -> Decimal:
    """按 TMS 前端同口径汇总费用。"""
    discount_set = set(discount_keys)
    skip_set = set(skip_keys)
    total = ZERO

    for key, value in calc_data.items():
        if key in skip_set or value is None:
            continue
        num = maybe_decimal(value)
        if num is None:
            continue
        if key in discount_set:
            total -= num
        else:
            total += num

    return total.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def add_margin(value: Any, margin: Decimal = DEFAULT_MARGIN, rounding=ROUND_CEILING) -> Decimal:
    return quantize_money(to_decimal(value) + margin, rounding=rounding)
