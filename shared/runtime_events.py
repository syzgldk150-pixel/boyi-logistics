"""Process-local runtime event ports shared without reversing package dependencies."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


_LOCK = threading.RLock()
_tms_session_alert: Callable[[dict[str, Any]], bool] | None = None


def register_tms_session_alert(callback: Callable[[dict[str, Any]], bool] | None) -> None:
    global _tms_session_alert
    with _LOCK:
        _tms_session_alert = callback


def publish_tms_session_alert(payload: dict[str, Any]) -> bool:
    with _LOCK:
        callback = _tms_session_alert
    if callback is None:
        return False
    return bool(callback(dict(payload)))
