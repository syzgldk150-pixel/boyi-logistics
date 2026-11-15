"""Unified pending-confirmation store (agent layer P1).

One unexpired item per (channel key, person); taken exactly once; expired items
are dropped on read. Process memory only: a restart forgets them and the person
starts again, so nothing ever runs from a stale confirmation.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class PendingConfirmations(Generic[T]):
    def __init__(self, ttl_seconds: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl must be positive")
        self._ttl, self._clock = ttl_seconds, clock
        self._items: dict[Any, tuple[float, T]] = {}
        self._lock = Lock()

    def remember(self, key: Any, item: T) -> None:
        with self._lock:
            self._items[key] = (self._clock() + self._ttl, item)

    def peek(self, key: Any) -> T | None:
        with self._lock:
            return self._live(key, take=False)

    def take(self, key: Any) -> T | None:
        with self._lock:
            return self._live(key, take=True)

    def take_with_state(self, key: Any) -> tuple[str, T | None]:
        """``("missing"|"expired"|"live", item)``; the item is removed in every case."""
        with self._lock:
            entry = self._items.pop(key, None)
        if entry is None:
            return "missing", None
        return ("expired", None) if entry[0] < self._clock() else ("live", entry[1])

    def _live(self, key: Any, *, take: bool) -> T | None:
        entry = self._items.pop(key, None) if take else self._items.get(key)
        if entry is None:
            return None
        if entry[0] < self._clock():
            self._items.pop(key, None)
            return None
        return entry[1]


__all__ = ["PendingConfirmations"]
