"""Process-local runtime event ports shared without reversing package dependencies."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


_LOCK = threading.RLock()
_tms_session_alert: Callable[[dict[str, Any]], bool] | None = None
_account_session_restored: Callable[[dict[str, Any]], bool] | None = None
_account_session_degraded: Callable[[dict[str, Any]], bool] | None = None
_finance_alert: Callable[[dict[str, Any]], bool] | None = None


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


def register_account_session_restored(
    callback: Callable[[dict[str, Any]], bool] | None,
) -> None:
    """Bind the composition-root adapter for persisted login restoration."""

    global _account_session_restored
    with _LOCK:
        _account_session_restored = callback


def publish_account_session_restored(payload: dict[str, Any]) -> bool:
    """Publish a non-sensitive account restoration signal in-process."""

    with _LOCK:
        callback = _account_session_restored
    if callback is None:
        return False
    return bool(callback(dict(payload)))


def register_account_session_degraded(
    callback: Callable[[dict[str, Any]], bool] | None,
) -> None:
    global _account_session_degraded
    with _LOCK:
        _account_session_degraded = callback


def publish_account_session_degraded(payload: dict[str, Any]) -> bool:
    with _LOCK:
        callback = _account_session_degraded
    if callback is None:
        return False
    return bool(callback(dict(payload)))


def register_finance_alert(callback: Callable[[dict[str, Any]], bool] | None) -> None:
    global _finance_alert
    with _LOCK:
        _finance_alert = callback


def publish_finance_alert(payload: dict[str, Any]) -> bool:
    with _LOCK:
        callback = _finance_alert
    if callback is None:
        return False
    return bool(callback(dict(payload)))
