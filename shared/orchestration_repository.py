"""Transactional persistence for automation projects, plugins, and Feishu bindings.

Callers supply connections; this module never loads configuration or mutates schema.
Every unit of work disables autocommit and rolls back unless explicitly committed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Callable

from shared.orchestration_repository_support import (
    OrchestrationPersistenceError,
    _row_dict,
    _rows,
)
from shared import orchestration_schema
from shared.connection_reuse import acquire_transaction_connection, release_transaction_connection
from shared.automation_project_policy_repository import AutomationProjectPolicyRepository
from shared.feishu_binding_repository import FeishuBindingRepository
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.account_execution_locks import (
    AccountExecutionLockLease,
    acquire_account_execution_locks as _acquire_account_execution_locks,
)

ConnectionFactory = Callable[[], Any]

__all__ = [
    "AccountExecutionLockLease",
    "ConnectionFactory",
    "OrchestrationPersistenceError",
    "OrchestrationRepository",
    "OrchestrationUnitOfWork",
]


class OrchestrationUnitOfWork:
    """One explicit MySQL transaction spanning the runtime repositories."""

    def __init__(self, connection_factory: ConnectionFactory, cursor_factory: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self._cursor_factory = cursor_factory
        self.connection: Any | None = None
        self._connection_owner: Any | None = None  # shared.connection_reuse scope, if any
        self._completed = False
        self._entered = False

    def __enter__(self) -> "OrchestrationUnitOfWork":
        if self._entered:
            raise RuntimeError("unit of work cannot be entered twice")
        connection, self._connection_owner = acquire_transaction_connection(self._connection_factory)
        if connection is None:
            raise RuntimeError("connection_factory returned no connection")
        try:
            set_autocommit = getattr(connection, "autocommit", None)
            if callable(set_autocommit):
                set_autocommit(False)
            elif hasattr(connection, "autocommit"):
                setattr(connection, "autocommit", False)
            begin = getattr(connection, "begin", None)
            if callable(begin):
                begin()
        except Exception:
            release_transaction_connection(connection, self._connection_owner, reusable=False)
            raise
        self.connection = connection
        self._entered = True
        self.automation_projects = AutomationProjectPolicyRepository(
            connection,
            self._cursor_factory,
        )
        self.automation_plugins = AutomationPluginRepository(
            connection,
            self._cursor_factory,
        )
        self.feishu_bindings = FeishuBindingRepository(
            connection,
            self._cursor_factory,
        )
        return self

    def _require_active(self) -> Any:
        if self.connection is None or not self._entered:
            raise RuntimeError("unit of work is not active")
        if self._completed:
            raise RuntimeError("unit of work transaction is already completed")
        return self.connection

    def commit(self) -> None:
        connection = self._require_active()
        connection.commit()
        self._completed = True

    def rollback(self) -> None:
        connection = self._require_active()
        connection.rollback()
        self._completed = True

    def validate_schema(self) -> None:
        self._require_active()
        required_tables, required_columns = orchestration_schema.orchestration_schema_requirements()
        with self.automation_plugins.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
            present = {
                str(item.get("TABLE_NAME") or "")
                for item in _rows(cursor)
            }
        missing = sorted(required_tables - present)
        if missing:
            raise RuntimeError(
                "orchestration schema is not migrated; run deployment migrations first: "
                + ", ".join(missing)
            )
        table_names = sorted({table for table, _ in required_columns})
        placeholders = ", ".join("%s" for _ in table_names)
        with self.automation_plugins.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT TABLE_NAME, COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders})
                """,
                table_names,
            )
            columns = {
                (str(item.get("TABLE_NAME") or ""), str(item.get("COLUMN_NAME") or ""))
                for item in _rows(cursor)
            }
        missing_columns = sorted(
            f"{table}.{column}" for table, column in required_columns - columns
        )
        if missing_columns:
            raise RuntimeError(
                "orchestration schema is not migrated; run deployment migrations first: "
                + ", ".join(missing_columns)
            )

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        connection = self.connection
        try:
            if connection is not None and not self._completed:
                connection.rollback()
                self._completed = True
        finally:
            if connection is not None:  # reused only after a clean, finished transaction
                reusable = exc_type is None and self._completed
                release_transaction_connection(connection, self._connection_owner, reusable=reusable)
            self.connection = None
        return False


class OrchestrationRepository:
    """Stable facade for runtime transactions."""

    def __init__(self, connection_factory: ConnectionFactory, cursor_factory: Any | None = None) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory
        self._cursor_factory = cursor_factory

    def unit_of_work(self) -> OrchestrationUnitOfWork:
        return OrchestrationUnitOfWork(self._connection_factory, self._cursor_factory)

    def validate_mysql8(self) -> str:
        """Require real MySQL 8+ because runtime claims use SKIP LOCKED."""

        with self.unit_of_work() as uow:
            with uow.automation_plugins.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version")
                row = _row_dict(cursor, cursor.fetchone()) or {}
        version = str(row.get("version") or row.get("VERSION()") or "").strip()
        if "mariadb" in version.lower():
            raise RuntimeError(f"orchestration persistence requires MySQL 8+, found {version or 'unknown'}")
        match = re.match(r"^(\d+)\.", version)
        if match is None or int(match.group(1)) < 8:
            raise RuntimeError(f"orchestration persistence requires MySQL 8+, found {version or 'unknown'}")
        return version

    def validate_schema(self) -> None:
        with self.unit_of_work() as uow:
            uow.validate_schema()

    def acquire_account_execution_locks(
        self,
        account_ids: Iterable[str],
        *,
        timeout_seconds: int = 0,
    ) -> AccountExecutionLockLease:
        """Acquire sorted MySQL named locks on a dedicated connection.

        The lease is connection-scoped and deliberately independent of a Unit
        of Work, so callers may commit short database transactions before a
        credential file or broker is changed without losing serialization.
        """

        # Closing the connection is the lock fail-safe: never a pooled connection.
        return _acquire_account_execution_locks(
            getattr(self._connection_factory, "unpooled", self._connection_factory),
            self._cursor_factory,
            account_ids,
            timeout_seconds=timeout_seconds,
        )
