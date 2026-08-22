from __future__ import annotations

import pytest

from shared.orchestration_repository import (
    AccountExecutionLockUnavailable,
    OrchestrationPersistenceError,
    OrchestrationRepository,
    _account_execution_lock_name,
)


class _Cursor:
    def __init__(self, connection):
        self._connection = connection
        self._row = None

    def execute(self, sql, params):
        operation = "GET" if "GET_LOCK" in sql else "RELEASE"
        lock_name = str(params[0])
        self._connection.operations.append((operation, lock_name, tuple(params[1:])))
        if operation == "GET":
            if self._connection.fail_get:
                raise RuntimeError("synthetic get-lock failure")
            value = self._connection.get_results.pop(0)
            self._row = {"acquired": value}
            return
        if self._connection.fail_release:
            raise RuntimeError("synthetic release-lock failure")
        value = self._connection.release_results.pop(0)
        self._row = {"released": value}

    def fetchone(self):
        return self._row

    def close(self):
        return None


class _Connection:
    def __init__(
        self,
        *,
        get_results=(1,),
        release_results=(1,),
        fail_get=False,
        fail_release=False,
    ):
        self.get_results = list(get_results)
        self.release_results = list(release_results)
        self.fail_get = fail_get
        self.fail_release = fail_release
        self.operations = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


def test_account_execution_locks_are_sorted_and_released_in_reverse_order():
    connection = _Connection(
        get_results=(1, 1),
        release_results=(1, 1),
    )
    repository = OrchestrationRepository(lambda: connection)

    lease = repository.acquire_account_execution_locks(
        ("z-account", "a-account", "z-account"),
        timeout_seconds=3,
    )
    lease.release()
    lease.release()

    a_lock = _account_execution_lock_name("a-account")
    z_lock = _account_execution_lock_name("z-account")
    assert connection.operations == [
        ("GET", a_lock, (3,)),
        ("GET", z_lock, (3,)),
        ("RELEASE", z_lock, ()),
        ("RELEASE", a_lock, ()),
    ]
    assert connection.closed is True


def test_partial_account_execution_lock_contention_releases_owned_lock():
    connection = _Connection(
        get_results=(1, 0),
        release_results=(1,),
    )
    repository = OrchestrationRepository(lambda: connection)

    with pytest.raises(AccountExecutionLockUnavailable):
        repository.acquire_account_execution_locks(("a-account", "z-account"))

    assert connection.operations[-1] == (
        "RELEASE",
        _account_execution_lock_name("a-account"),
        (),
    )
    assert connection.closed is True


def test_account_execution_lock_query_failure_closes_dedicated_connection():
    connection = _Connection(fail_get=True)
    repository = OrchestrationRepository(lambda: connection)

    with pytest.raises(RuntimeError, match="synthetic get-lock failure"):
        repository.acquire_account_execution_locks(("a-account",))

    assert connection.closed is True


def test_account_execution_lock_release_failure_still_closes_connection():
    connection = _Connection(fail_release=True)
    repository = OrchestrationRepository(lambda: connection)
    lease = repository.acquire_account_execution_locks(("a-account",))

    with pytest.raises(
        OrchestrationPersistenceError,
        match="account execution lock cleanup failed",
    ):
        lease.release()

    assert connection.closed is True


@pytest.mark.parametrize("timeout", (-1, 31, True, 1.5))
def test_account_execution_lock_rejects_invalid_timeout_without_connecting(timeout):
    connected = False

    def connection_factory():
        nonlocal connected
        connected = True
        return _Connection()

    repository = OrchestrationRepository(connection_factory)

    with pytest.raises((TypeError, ValueError)):
        repository.acquire_account_execution_locks(
            ("a-account",),
            timeout_seconds=timeout,
        )

    assert connected is False
