"""Real MySQL: pooled Units of Work reuse one session; named locks and dead sessions stay safe."""
from __future__ import annotations

import pytest

from shared.account_execution_locks import account_execution_lock_name
from shared.connection_pool import PooledConnectionFactory
from shared.identity_repository import IdentityRepository
from shared.orchestration_repository import OrchestrationRepository
from tests.mysql_isolated_fixtures import repository, pytestmark  # noqa: F401


def _session_id(uow):
    with uow.automation_plugins.cursor() as cursor:
        cursor.execute("SELECT CONNECTION_ID() AS id")
        return int(cursor.fetchone()["id"])


def _lock_owner(uow, name):
    with uow.automation_plugins.cursor() as cursor:
        cursor.execute("SELECT IS_USED_LOCK(%s) AS owner", (name,))
        return cursor.fetchone()["owner"]


def test_units_of_work_and_identity_checks_reuse_one_session(repository):
    pool = PooledConnectionFactory(repository._connection_factory)
    pooled = OrchestrationRepository(pool)
    try:
        with pooled.unit_of_work() as uow:
            first = _session_id(uow)
            uow.commit()
        with pytest.raises(ValueError):
            with pooled.unit_of_work() as uow:
                assert _session_id(uow) == first
                raise ValueError("business failure rolls back")
        with pooled.unit_of_work() as uow:
            assert _session_id(uow) == first
        IdentityRepository(pool).account(999999)  # unknown account: just a read on the pooled session
        assert pool.idle_count() == 1
    finally:
        pool.close()


def test_named_lock_uses_its_own_session_and_is_released(repository):
    pool = PooledConnectionFactory(repository._connection_factory)
    pooled = OrchestrationRepository(pool)
    name = account_execution_lock_name("pool-lock-account")
    try:
        with pooled.unit_of_work() as uow:
            pooled_session = _session_id(uow)
        lease = pooled.acquire_account_execution_locks(["pool-lock-account"])
        with pooled.unit_of_work() as uow:
            owner = _lock_owner(uow, name)
            assert owner is not None and int(owner) != pooled_session
        lease.release()
        with pooled.unit_of_work() as uow:
            assert _lock_owner(uow, name) is None
        assert pool.idle_count() == 1
    finally:
        pool.close()


def test_session_killed_while_idle_is_replaced(repository):
    pool = PooledConnectionFactory(repository._connection_factory, ping_after_idle_seconds=0)
    pooled = OrchestrationRepository(pool)
    try:
        with pooled.unit_of_work() as uow:
            idle_session = _session_id(uow)
        with repository.unit_of_work() as admin:
            with admin.automation_plugins.cursor() as cursor:
                cursor.execute(f"KILL {idle_session}")
        with pooled.unit_of_work() as uow:
            assert _session_id(uow) != idle_session
    finally:
        pool.close()
