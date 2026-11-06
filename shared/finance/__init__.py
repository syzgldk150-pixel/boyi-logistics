"""Finance domain package.

The platform-statement ledger was retired with migration 067; the double-entry
general ledger is rebuilt here phase by phase. Only the shared Decimal money
rules survive from the previous implementation.
"""

from .money import (
    DISPLAY_SCALE,
    STORAGE_SCALE,
    ZERO,
    InvalidAmountError,
    MissingAmountError,
    decimal_to_storage_text,
    format_money,
    optional_decimal,
    quantize_money,
    quantize_storage,
    to_decimal,
)

__all__ = [
    "DISPLAY_SCALE",
    "STORAGE_SCALE",
    "ZERO",
    "InvalidAmountError",
    "MissingAmountError",
    "decimal_to_storage_text",
    "format_money",
    "optional_decimal",
    "quantize_money",
    "quantize_storage",
    "to_decimal",
]
