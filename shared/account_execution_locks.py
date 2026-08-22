"""Connection-scoped MySQL locks for account-bound control-plane execution."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable
from typing import Any, Callable

from shared.orchestration_repository_support import (
    OrchestrationPersistenceError,
    RepositoryBase,
    _required_text,
    _row_dict,
)


class AccountExecutionLockUnavailable(OrchestrationPersistenceError):
    """A database-wide account execution lock is currently owned elsewhere."""


class AccountExecutionLockLease:
    """MySQL named locks held by a dedicated, non-transactional connection."""

    def __init__(
        self,
        connection: Any,
        cursor_factory: Any | None,
        lock_names: Iterable[str],
    ) -> None:
        self._connection = connection
        self._cursor_factory = cursor_factory
        self._lock_names = tuple(lock_names)
        self._release_guard = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._release_guard:
            if self._released:
                return
            self._released = True
            connection = self._connection
            self._connection = None
            release_error: Exception | None = None
            try:
                repository = RepositoryBase(connection, self._cursor_factory)
                for lock_name in reversed(self._lock_names):
                    with repository.cursor() as cursor:
                        cursor.execute(
                            "SELECT RELEASE_LOCK(%s) AS released",
                            (lock_name,),
                        )
                        row = _row_dict(cursor, cursor.fetchone()) or {}
                    if int(row.get("released") or 0) != 1:
                        raise OrchestrationPersistenceError(
                            "account execution lock ownership was lost before release"
                        )
            except Exception as exc:  # closing the connection remains the lock fail-safe
                release_error = exc
            finally:
                close = getattr(connection, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:
                        if release_error is None:
                            release_error = exc
            if release_error is not None:
                raise OrchestrationPersistenceError(
                    "account execution lock cleanup failed"
                ) from release_error

    def __enter__(self) -> "AccountExecutionLockLease":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        self.release()
        return False


def account_execution_lock_name(account_id: str) -> str:
    account = _required_text(account_id, "account_id")
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
    return f"boyi:account-exec:{digest[:40]}"


def acquire_account_execution_locks(
    connection_factory: Callable[[], Any],
    cursor_factory: Any | None,
    account_ids: Iterable[str],
    *,
    timeout_seconds: int = 0,
) -> AccountExecutionLockLease:
    """Acquire sorted named locks and return their dedicated connection lease."""

    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise TypeError("timeout_seconds must be an integer")
    if timeout_seconds < 0 or timeout_seconds > 30:
        raise ValueError("timeout_seconds must be between 0 and 30")
    accounts = tuple(
        sorted(
            {
                _required_text(account_id, "account_id")
                for account_id in account_ids
            }
        )
    )
    if not accounts:
        raise ValueError("account_ids is required")
    lock_names = tuple(account_execution_lock_name(account) for account in accounts)
    connection = connection_factory()
    if connection is None:
        raise RuntimeError("connection_factory returned no connection")
    acquired: list[str] = []
    try:
        repository = RepositoryBase(connection, cursor_factory)
        for lock_name in lock_names:
            with repository.cursor() as cursor:
                cursor.execute(
                    "SELECT GET_LOCK(%s, %s) AS acquired",
                    (lock_name, timeout_seconds),
                )
                row = _row_dict(cursor, cursor.fetchone()) or {}
            if int(row.get("acquired") or 0) != 1:
                raise AccountExecutionLockUnavailable(
                    "account execution is already serialized by another operation"
                )
            acquired.append(lock_name)
        return AccountExecutionLockLease(connection, cursor_factory, acquired)
    except Exception:
        cleanup_error: Exception | None = None
        if acquired:
            try:
                AccountExecutionLockLease(
                    connection,
                    cursor_factory,
                    acquired,
                ).release()
            except Exception as exc:
                cleanup_error = exc
        else:
            close = getattr(connection, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    cleanup_error = exc
        if cleanup_error is not None:
            raise OrchestrationPersistenceError(
                "account execution lock acquisition cleanup failed"
            ) from cleanup_error
        raise
