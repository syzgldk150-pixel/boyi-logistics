"""Typed refusals of the general ledger; each carries a stable code."""

from __future__ import annotations

from typing import Any


class FinanceError(RuntimeError):
    """A refused finance operation. ``code`` is stable; ``message`` is for people."""

    http_status = 409

    def __init__(self, code: str, message: str, *, details: Any = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        if http_status is not None:
            self.http_status = http_status


class FinanceValidationError(FinanceError):
    http_status = 400

    def __init__(self, message: str, *, code: str = "FINANCE_INVALID_REQUEST", details: Any = None) -> None:
        super().__init__(code, message, details=details)


class FinanceNotFoundError(FinanceError):
    http_status = 404

    def __init__(self, message: str, *, code: str = "FINANCE_NOT_FOUND") -> None:
        super().__init__(code, message)
