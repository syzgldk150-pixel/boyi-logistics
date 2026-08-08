"""Small, dependency-free contracts shared by Agent and Console."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiError:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def api_success(data: Any) -> dict[str, Any]:
    """Return the stable ``ok/data/error`` response envelope."""

    return {"ok": True, "data": data, "error": None}


def api_failure(code: str, message: str, *, data: Any = None) -> dict[str, Any]:
    """Return the stable failure envelope without leaking implementation details."""

    return {
        "ok": False,
        "data": data,
        "error": ApiError(code=str(code), message=str(message)).to_dict(),
    }
