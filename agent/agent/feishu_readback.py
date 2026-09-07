"""Shared fresh-readback policy for Feishu mutations.

Feishu may acknowledge a mutation while its read model is still serving the
previous snapshot.  Callers use :func:`retry_readback` *after* the one and
only write call.  The helper only performs fresh reads at the signed
``0 / 0.5 / 1 / 2`` second schedule and raises ``WRITE_OUTCOME_UNKNOWN`` when
the expected state cannot be proved.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from typing import Any, NoReturn

from agent.automation_plugins.errors import PluginExecutionError


FEISHU_READBACK_DELAYS = (0.0, 0.5, 1.0, 2.0)
DEFAULT_RETRYABLE_ERROR_CODES = frozenset(
    {
        "BROKER_RESOURCE_UNAVAILABLE",
        "BROKER_SOURCE_FAILED",
        "WRITE_OUTCOME_UNKNOWN",
    }
)


def _raise_unknown(
    message: str,
    *,
    cause: Exception | None = None,
) -> NoReturn:
    error = PluginExecutionError(message, code="WRITE_OUTCOME_UNKNOWN")
    if cause is None:
        raise error
    raise error from cause


def retry_readback(
    reader: Callable[[], Any],
    matches: Callable[[Any], bool],
    *,
    label: str,
    retryable_error_codes: Collection[str] = DEFAULT_RETRYABLE_ERROR_CODES,
    initial_error: Exception | None = None,
    convert_non_retryable: bool = False,
) -> Any:
    """Return the first exact fresh observation, without repeating a write.

    ``reader`` must perform a new authoritative read on every invocation.
    Transient reader or matcher errors, and non-matching observations, consume
    the bounded schedule.  A non-retryable ``PluginExecutionError`` is
    re-raised by default so resource/schema validation keeps its original
    contract; post-write adapters that must close the boundary as unknown can
    opt into ``convert_non_retryable``.
    """

    retryable = frozenset(retryable_error_codes)
    last_error = initial_error
    for delay in FEISHU_READBACK_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            observed = reader()
        except PluginExecutionError as exc:
            if exc.code not in retryable:
                if not convert_non_retryable:
                    raise
                return _raise_unknown(f"{label} fresh readback failed", cause=exc)
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            continue

        try:
            matched = matches(observed)
        except PluginExecutionError as exc:
            if exc.code not in retryable:
                if not convert_non_retryable:
                    raise
                return _raise_unknown(f"{label} fresh readback failed", cause=exc)
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            continue
        if matched:
            return observed

    return _raise_unknown(f"{label} fresh readback did not match", cause=last_error)


def require_exact_readback(
    reader: Callable[[], Any],
    expected: Any,
    *,
    label: str,
    retryable_error_codes: Collection[str] = DEFAULT_RETRYABLE_ERROR_CODES,
    initial_error: Exception | None = None,
    convert_non_retryable: bool = False,
) -> Any:
    """Require an exact expected snapshot using :func:`retry_readback`."""

    return retry_readback(
        reader,
        lambda observed: observed == expected,
        label=label,
        retryable_error_codes=retryable_error_codes,
        initial_error=initial_error,
        convert_non_retryable=convert_non_retryable,
    )


__all__ = [
    "DEFAULT_RETRYABLE_ERROR_CODES",
    "FEISHU_READBACK_DELAYS",
    "require_exact_readback",
    "retry_readback",
]
