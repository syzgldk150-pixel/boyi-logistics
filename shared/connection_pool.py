"""Process-wide reuse of idle MySQL connections made by one connection factory.

The production database is a cross-region RDS endpoint: opening a TLS
connection costs about 0.27 s, one statement about 35 ms.  Every Agent request
used to open fresh connections for its identity check and catalog reads.

``PooledConnectionFactory`` wraps an existing factory.  Callers keep calling
``close()`` (directly, through ``with``, or via ``shared.connection_reuse``);
a connection goes back to the idle pool only when its transaction has ended,
autocommit is still off, and the session is open.  Anything else is closed for
real.  An idle connection older than ``ping_after_idle_seconds`` is pinged
before reuse, and connections are dropped after ``max_idle_seconds`` idle or
``max_lifetime_seconds`` in total.

Session state that outlives a transaction must never use a pooled connection.
MySQL named locks rely on closing their connection as the release fail-safe,
so lock leases take ``factory.unpooled`` (the original factory) instead.

This module reads no environment and opens nothing by itself; the composition
root decides which factory is pooled.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

# MySQL protocol status flags reported on every response.
_SERVER_STATUS_IN_TRANS = 0x0001
_SERVER_STATUS_AUTOCOMMIT = 0x0002


def _close_quietly(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - a broken idle connection is simply discarded
            pass


def _clean_for_reuse(connection: Any) -> bool:
    if getattr(connection, "open", True) is False:
        return False
    status = getattr(connection, "server_status", None)
    if not isinstance(status, int):
        return False  # unknown session state is never shared
    return not status & _SERVER_STATUS_IN_TRANS and not status & _SERVER_STATUS_AUTOCOMMIT


class PooledConnectionFactory:
    """Callable factory returning connections whose ``close()`` may recycle them."""

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        max_idle: int = 8,
        ping_after_idle_seconds: float = 2.0,
        max_idle_seconds: float = 60.0,
        max_lifetime_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(factory):
            raise TypeError("factory must be callable")
        self.unpooled = factory
        self._max_idle = max(0, int(max_idle))
        self._ping_after = float(ping_after_idle_seconds)
        self._max_idle_seconds = float(max_idle_seconds)
        self._max_lifetime = float(max_lifetime_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._idle: list[tuple[Any, float, float]] = []  # (connection, created_at, idle_since)
        self._closed = False

    def __call__(self) -> "_PooledConnection":
        while True:
            with self._lock:
                candidate = self._idle.pop() if self._idle else None
            if candidate is None:
                break
            connection, created_at, idle_since = candidate
            now = self._clock()
            if now - idle_since > self._max_idle_seconds or now - created_at > self._max_lifetime:
                _close_quietly(connection)
                continue
            if now - idle_since > self._ping_after:
                try:
                    connection.ping(reconnect=False)
                except Exception:  # noqa: BLE001 - dead idle connection: discard and try the next
                    _close_quietly(connection)
                    continue
            return _PooledConnection(self, connection, created_at)
        connection = self.unpooled()
        if connection is None:
            raise RuntimeError("connection factory returned no connection")
        return _PooledConnection(self, connection, self._clock())

    def _give_back(self, connection: Any, created_at: float) -> None:
        now = self._clock()
        keep = (
            _clean_for_reuse(connection)
            and now - created_at <= self._max_lifetime
        )
        if keep:
            with self._lock:
                if not self._closed and len(self._idle) < self._max_idle:
                    self._idle.append((connection, created_at, now))
                    return
        _close_quietly(connection)

    def idle_count(self) -> int:
        with self._lock:
            return len(self._idle)

    def close(self) -> None:
        """Close every idle connection; later returns are closed, not pooled."""

        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for connection, _created, _since in idle:
            _close_quietly(connection)


class _PooledConnection:
    """A borrowed connection; ``close()`` hands it back instead of closing."""

    __slots__ = ("_pool", "_connection", "_created_at")

    def __init__(self, pool: PooledConnectionFactory, connection: Any, created_at: float) -> None:
        self._pool = pool
        self._connection = connection
        self._created_at = created_at

    def _active(self) -> Any:
        connection = self._connection
        if connection is None:
            raise RuntimeError("connection was already returned")
        return connection

    @property
    def open(self) -> bool:
        connection = self._connection
        return connection is not None and getattr(connection, "open", True) is not False

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            self._pool._give_back(connection, self._created_at)

    def __enter__(self) -> "_PooledConnection":
        self._active()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._active(), name)
