"""File-backed state persistence for TMS session profiles.

This module is intentionally independent from provider login code.  It owns
directory creation and resilient JSON object reads/writes; callers supply the
file names and their domain defaults.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SessionStateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def read_dict(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def write_dict(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def remove(path: Path) -> None:
        path.unlink(missing_ok=True)
