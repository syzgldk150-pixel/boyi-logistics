"""Shared error types for the embedded TMS runtime."""

from __future__ import annotations


class TMSAuthStateError(RuntimeError):
    """Raised when the shared TMS login state is not ready for use."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code or "AUTH_REQUIRED").strip() or "AUTH_REQUIRED"


def auth_error_payload(exc: TMSAuthStateError) -> dict:
    return {
        "ok": False,
        "error_code": exc.code,
        "error": str(exc),
    }
