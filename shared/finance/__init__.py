"""Finance domain package.

The platform-statement ledger was retired with migration 067; the double-entry
general ledger is rebuilt here phase by phase: ``FinanceRepository`` is the
only writer of the ``finance_*`` tables and ``gl/posting.py`` the only writer of
the GL.
"""

from .errors import FinanceError, FinanceNotFoundError, FinanceValidationError
from .models import FinanceActor
from .repository import FinanceRepository

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
    "FinanceActor",
    "FinanceError",
    "FinanceNotFoundError",
    "FinanceRepository",
    "FinanceValidationError",
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
