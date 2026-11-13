"""Idle connections are reused only in a clean, verified session state."""
from __future__ import annotations

import pytest

from shared.connection_pool import PooledConnectionFactory
from shared.connection_reuse import reuse_transaction_connections
from shared.orchestration_repository import OrchestrationRepository

IN_TRANS, AUTOCOMMIT = 0x0001, 0x0002


class _Connection:
    def __init__(self, number):
        self.number = number
        self.open = True
        self.server_status = 0
        self.closed = 0
        self.pings = 0
        self.ping_fails = False

    def close(self):
        self.closed += 1
        self.open = False

    def ping(self, reconnect=False):
        assert reconnect is False
        self.pings += 1
        if self.ping_fails:
            raise OSError("gone")

    def autocommit(self, value):
        self.server_status = (self.server_status | AUTOCOMMIT) if value else (self.server_status & ~AUTOCOMMIT)

    def begin(self):
        self.server_status |= IN_TRANS

    def commit(self):
        self.server_status &= ~IN_TRANS

    def rollback(self):
        self.server_status &= ~IN_TRANS

    def cursor(self, *_args):
        raise AssertionError("not used")


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _pool(**kwargs):
    made = []
    clock = _Clock()

    def factory():
        made.append(_Connection(len(made) + 1))
        return made[-1]

    return PooledConnectionFactory(factory, clock=clock, **kwargs), made, clock


def test_clean_connection_is_reused_without_reconnecting():
    pool, made, _clock = _pool()
    with pool() as first:
        number = first.number
    with pool() as second:
        assert second.number == number
    assert len(made) == 1 and made[0].closed == 0 and pool.idle_count() == 1


@pytest.mark.parametrize("status", [IN_TRANS, AUTOCOMMIT])
def test_open_transaction_or_autocommit_session_is_closed_not_pooled(status):
    pool, made, _clock = _pool()
    borrowed = pool()
    made[0].server_status = status
    borrowed.close()
    assert made[0].closed == 1 and pool.idle_count() == 0
    pool().close()
    assert len(made) == 2


def test_broken_connection_is_not_pooled():
    pool, made, _clock = _pool()
    borrowed = pool()
    made[0].open = False
    borrowed.close()
    assert pool.idle_count() == 0


def test_idle_connection_is_pinged_and_a_dead_one_replaced():
    pool, made, clock = _pool(ping_after_idle_seconds=2)
    pool().close()
    clock.now += 1
    pool().close()
    assert made[0].pings == 0  # recently used: no extra round trip
    clock.now += 5
    made[0].ping_fails = True
    replacement = pool()
    assert made[0].pings == 1 and made[0].closed == 1
    assert replacement.number == 2


def test_idle_and_lifetime_limits_drop_old_connections():
    pool, made, clock = _pool(max_idle_seconds=60, max_lifetime_seconds=600)
    pool().close()
    clock.now += 61
    assert pool().number == 2 and made[0].closed == 1
    pool, made, clock = _pool(max_idle_seconds=60, max_lifetime_seconds=600)
    for _ in range(12):
        clock.now += 55
        pool().close()
    assert len(made) == 2  # recycled after ten minutes of life


def test_pool_keeps_at_most_max_idle_connections():
    pool, made, _clock = _pool(max_idle=2)
    borrowed = [pool() for _ in range(3)]
    for connection in borrowed:
        connection.close()
    assert pool.idle_count() == 2 and made[2].closed == 1


def test_closing_the_pool_closes_idle_and_later_returns():
    pool, made, _clock = _pool()
    held = pool()
    pool().close()
    pool.close()
    assert made[1].closed == 1
    held.close()
    assert made[0].closed == 1 and pool.idle_count() == 0


def test_returned_connection_cannot_be_used_again():
    pool, _made, _clock = _pool()
    borrowed = pool()
    borrowed.close()
    borrowed.close()  # a second close is harmless
    assert borrowed.open is False
    with pytest.raises(RuntimeError):
        borrowed.commit()


def test_units_of_work_share_one_pooled_connection_across_requests():
    pool, made, _clock = _pool()
    repository = OrchestrationRepository(pool)
    for _ in range(3):
        with repository.unit_of_work() as uow:
            uow.commit()
    with reuse_transaction_connections():
        with repository.unit_of_work() as uow:
            uow.rollback()
    assert len(made) == 1 and pool.idle_count() == 1


def test_failed_unit_of_work_rolls_back_before_reuse():
    pool, made, _clock = _pool()
    repository = OrchestrationRepository(pool)
    with pytest.raises(ValueError):
        with repository.unit_of_work():
            raise ValueError("business failure")
    assert made[0].server_status & IN_TRANS == 0 and pool.idle_count() == 1


def test_named_locks_never_use_a_pooled_connection(monkeypatch):
    pool, made, _clock = _pool()
    captured = {}

    def acquire(factory, _cursor_factory, account_ids, *, timeout_seconds):
        captured["factory"] = factory
        return "lease"

    monkeypatch.setattr("shared.orchestration_repository._acquire_account_execution_locks", acquire)
    assert OrchestrationRepository(pool).acquire_account_execution_locks(["a"]) == "lease"
    assert captured["factory"] is pool.unpooled
