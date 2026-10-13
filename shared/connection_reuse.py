"""Reuse one MySQL connection for the sequential transactions of one request.

The production database is a cross-region RDS endpoint where opening a TLS
connection costs about 0.27 s.  A plugin settings or schedule save runs dozens
of short, strictly sequential Units of Work; inside
``reuse_transaction_connections()`` they share one physical connection.

Every Unit of Work still begins and ends its own transaction.  A connection is
handed back for reuse only after its transaction ended without an error and the
server reports no open transaction; anything else closes it.  A nested or
concurrent Unit of Work gets its own connection.  Only Units of Work take part:
named-lock leases and other raw connection users keep their own connections.
The scope closes its connection when it exits.  This module reads no
environment and opens nothing by itself.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

# MySQL protocol status flag: a transaction is still open on the session.
_SERVER_STATUS_IN_TRANS = 0x0001


class _ConnectionScope:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: Any | None = None
        self._in_use = False
        self._closed = False

    def acquire(self, factory: Callable[[], Any]) -> tuple[Any, bool]:
        with self._lock:
            if not self._closed and not self._in_use and self._connection is not None:
                self._in_use = True
                return self._connection, True
            owns_slot = not self._closed and not self._in_use
            if owns_slot:
                self._in_use = True
        try:
            connection = factory()
        except BaseException:
            if owns_slot:
                with self._lock:
                    self._in_use = False
            raise
        if owns_slot:
            with self._lock:
                self._connection = connection
        return connection, owns_slot

    def release(self, connection: Any, *, reusable: bool) -> None:
        with self._lock:
            self._in_use = False
            keep = reusable and not self._closed and self._connection is connection
            if not keep and self._connection is connection:
                self._connection = None
        if not keep:
            _close(connection)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            connection = None if self._in_use else self._connection
            if connection is not None:
                self._connection = None
        if connection is not None:
            _close(connection)


_active_scope: ContextVar[_ConnectionScope | None] = ContextVar(
    "boyi_transaction_connection_scope", default=None
)


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _transaction_ended(connection: Any) -> bool:
    if getattr(connection, "open", True) is False:
        return False
    status = getattr(connection, "server_status", None)
    return not (isinstance(status, int) and status & _SERVER_STATUS_IN_TRANS)


@contextmanager
def reuse_transaction_connections() -> Iterator[None]:
    """Share one connection among the Units of Work run inside this scope."""

    scope = _ConnectionScope()
    token = _active_scope.set(scope)
    try:
        yield
    finally:
        _active_scope.reset(token)
        scope.close()


def acquire_transaction_connection(factory: Callable[[], Any]) -> tuple[Any, Any]:
    """Return ``(connection, owner)``; outside a scope the owner is ``None``."""

    scope = _active_scope.get()
    if scope is None:
        return factory(), None
    connection, scoped = scope.acquire(factory)
    return connection, scope if scoped else None


def release_transaction_connection(connection: Any, owner: Any, *, reusable: bool) -> None:
    """Close the connection, or hand a cleanly finished one back to its owner."""

    if owner is None:
        _close(connection)
        return
    owner.release(connection, reusable=reusable and _transaction_ended(connection))
