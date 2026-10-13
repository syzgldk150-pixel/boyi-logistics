"""Sequential Units of Work of one request share a cleanly finished connection."""
import threading

import pytest

from shared.connection_reuse import reuse_transaction_connections
from shared.orchestration_repository import OrchestrationUnitOfWork


class FakeConnection:
    def __init__(self, number):
        self.number = number
        self.open = True
        self.server_status = 0
        self.closed = False
        self.events = []

    def autocommit(self, value):
        self.events.append(("autocommit", value))

    def begin(self):
        self.server_status |= 1
        self.events.append("begin")

    def commit(self):
        self.server_status &= ~1
        self.events.append("commit")

    def rollback(self):
        self.server_status &= ~1
        self.events.append("rollback")

    def close(self):
        self.closed = True
        self.open = False


class Factory:
    def __init__(self):
        self.created = []

    def __call__(self):
        connection = FakeConnection(len(self.created))
        self.created.append(connection)
        return connection


def _transaction(factory, *, commit=True):
    with OrchestrationUnitOfWork(factory) as uow:
        connection = uow.connection
        if commit:
            uow.commit()
    return connection


def test_outside_a_scope_every_unit_of_work_opens_and_closes_its_own():
    factory = Factory()
    first, second = _transaction(factory), _transaction(factory)
    assert first is not second
    assert first.closed and second.closed


def test_inside_a_scope_sequential_transactions_share_one_connection():
    factory = Factory()
    with reuse_transaction_connections():
        first = _transaction(factory)
        second = _transaction(factory, commit=False)
        third = _transaction(factory)
        assert first is second is third
        assert not first.closed
        # Each Unit of Work still ran its own transaction.
        assert first.events.count("begin") == 3
        assert first.events.count("commit") == 2 and first.events.count("rollback") == 1
    assert len(factory.created) == 1 and first.closed


def test_a_failed_unit_of_work_discards_its_connection():
    factory = Factory()
    with reuse_transaction_connections():
        with pytest.raises(RuntimeError):
            with OrchestrationUnitOfWork(factory) as uow:
                failed = uow.connection
                raise RuntimeError("boom")
        assert failed.closed
        replacement = _transaction(factory)
        assert replacement is not failed
    assert len(factory.created) == 2


def test_a_connection_still_inside_a_transaction_is_not_reused():
    factory = Factory()
    with reuse_transaction_connections():
        with OrchestrationUnitOfWork(factory) as uow:
            leaked = uow.connection
            uow.commit()
            leaked.server_status |= 1  # e.g. a statement reopened a transaction
        assert leaked.closed
        assert _transaction(factory) is not leaked


def test_nested_and_concurrent_units_of_work_get_their_own_connections():
    factory = Factory()
    with reuse_transaction_connections():
        with OrchestrationUnitOfWork(factory) as outer:
            shared = outer.connection
            nested = _transaction(factory)
            assert nested is not shared and nested.closed
            seen = []
            worker = threading.Thread(target=lambda: seen.append(_transaction(factory)))
            worker.start()
            worker.join()
            assert seen[0] is not shared and seen[0].closed
            outer.commit()
        assert _transaction(factory) is shared
