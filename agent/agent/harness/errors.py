"""Stable, non-sensitive errors for the offline Harness core."""

from __future__ import annotations


class HarnessError(RuntimeError):
    """A fail-closed Harness error with a stable machine-readable code."""

    code = "HARNESS_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.code
