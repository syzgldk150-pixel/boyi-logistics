"""Read-model facade methods for orchestration persistence.

Imported by ``shared.orchestration_repository``; the stable public facade type
remains available from its original module.
"""

from __future__ import annotations

from shared import orchestration_repository as _repository

Any = _repository.Any
AccountExecutionLockLease = _repository.AccountExecutionLockLease
ConnectionFactory = _repository.ConnectionFactory
Iterable = _repository.Iterable
OrchestrationUnitOfWork = _repository.OrchestrationUnitOfWork
_acquire_account_execution_locks = _repository._acquire_account_execution_locks
_row_dict = _repository._row_dict
datetime = _repository.datetime
re = _repository.re


class OrchestrationRepositoryFacadeMixin:
    """Stable facade for orchestration transactions and read models."""

    def __init__(self, connection_factory: ConnectionFactory, cursor_factory: Any | None = None) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory
        self._cursor_factory = cursor_factory

    def unit_of_work(self) -> OrchestrationUnitOfWork:
        return OrchestrationUnitOfWork(self._connection_factory, self._cursor_factory)

    def validate_mysql8(self) -> str:
        """Require real MySQL 8+ because worker claims use SKIP LOCKED."""

        with self.unit_of_work() as uow:
            with uow.commands.cursor() as cursor:
                cursor.execute("SELECT VERSION() AS version")
                row = _row_dict(cursor, cursor.fetchone()) or {}
        version = str(row.get("version") or row.get("VERSION()") or "").strip()
        if "mariadb" in version.lower():
            raise RuntimeError(f"orchestration persistence requires MySQL 8+, found {version or 'unknown'}")
        match = re.match(r"^(\d+)\.", version)
        if match is None or int(match.group(1)) < 8:
            raise RuntimeError(f"orchestration persistence requires MySQL 8+, found {version or 'unknown'}")
        return version

    def validate_schema(self, *, include_windows_worker: bool = True) -> None:
        with self.unit_of_work() as uow:
            uow.validate_schema(include_windows_worker=include_windows_worker)

    def outbox_health(self) -> dict[str, Any]:
        with self.unit_of_work() as uow:
            return uow.outbox.health()

    def list_scheduled_task_policy_rows(self) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.scheduled_policies.list_with_tasks()

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

        return _acquire_account_execution_locks(
            self._connection_factory,
            self._cursor_factory,
            account_ids,
            timeout_seconds=timeout_seconds,
        )

    def list_nonterminal_runs_with_commands(self) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.runs.list_nonterminal_with_commands()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.unit_of_work() as uow:
            run = uow.runs.get(run_id)
            if run is not None:
                run["steps"] = uow.steps.list_for_run(run_id)
            return run

    def list_runs_for_work_item(
        self,
        work_item_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.runs.list_for_work_item(work_item_id, limit=limit, offset=offset)

    def create_linked_retry_run(
        self,
        source_run_id: str,
        *,
        new_run_id: str,
        new_command_id: str,
        expected_statuses: Iterable[str] = ("PARTIAL", "FAILED_TERMINAL"),
        now: Any = None,
    ) -> dict[str, Any]:
        with self.unit_of_work() as uow:
            retry = uow.runs.create_linked_retry(
                source_run_id,
                new_run_id=new_run_id,
                new_command_id=new_command_id,
                expected_statuses=expected_statuses,
                now=now,
            )
            uow.commit()
            return retry

    def list_blocked_login_for_account(
        self,
        account_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.runs.list_blocked_login_for_account(
                account_id,
                limit=limit,
                offset=offset,
            )

    def page_blocked_login_runs_for_account(
        self,
        account_id: str,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self.unit_of_work() as uow:
            return uow.runs.page_blocked_login_for_account(
                account_id,
                limit=limit,
                offset=offset,
            )

    def list_runnable_runs(
        self,
        *,
        statuses: Iterable[str],
        limit: int = 100,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.runs.list_runnable(statuses=statuses, limit=limit, now=now)

    def claim_runs(
        self,
        worker_id: str,
        statuses: Iterable[str],
        *,
        limit: int = 20,
        lease_seconds: int = 60,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            claimed = uow.runs.claim(
                worker_id,
                statuses,
                limit=limit,
                lease_seconds=lease_seconds,
                now=now,
            )
            uow.commit()
            return claimed

    def claim_cancel_requested_runs(
        self,
        worker_id: str,
        *,
        limit: int = 20,
        lease_seconds: int = 60,
        now: Any = None,
    ) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            claimed = uow.runs.claim_cancel_requested(
                worker_id,
                limit=limit,
                lease_seconds=lease_seconds,
                now=now,
            )
            uow.commit()
            return claimed

    def renew_run_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now: Any = None,
    ) -> dict[str, Any]:
        with self.unit_of_work() as uow:
            run = uow.runs.renew_lease(
                run_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                now=now,
            )
            uow.commit()
            return run

    def request_run_cancel(
        self,
        run_id: str,
        *,
        requested_by_type: str,
        requested_by_id: str | None = None,
        reason: str | None = None,
        requested_at: Any = None,
    ) -> dict[str, Any]:
        with self.unit_of_work() as uow:
            run = uow.runs.request_cancel(
                run_id,
                requested_by_type=requested_by_type,
                requested_by_id=requested_by_id,
                reason=reason,
                requested_at=requested_at,
            )
            uow.commit()
            return run

    def list_work_items(
        self,
        *,
        status: str | None = None,
        item_type: str | None = None,
        priority: str | None = None,
        source: str | None = None,
        query: str | None = None,
        owner_type: str | None = None,
        owner_id: str | None = None,
        sla_from: datetime | None = None,
        sla_before: datetime | None = None,
        sla_missing: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.work_items.list(
                status=status,
                item_type=item_type,
                priority=priority,
                source=source,
                query=query,
                owner_type=owner_type,
                owner_id=owner_id,
                sla_from=sla_from,
                sla_before=sla_before,
                sla_missing=sla_missing,
                limit=limit,
                offset=offset,
            )

    def get_work_item(self, work_item_id: str) -> dict[str, Any] | None:
        with self.unit_of_work() as uow:
            item = uow.work_items.get(work_item_id)
            if item is not None:
                item["entities"] = uow.work_items.list_entities(work_item_id)
            return item

    def assign_work_item(
        self,
        work_item_id: str,
        *,
        expected_version: int,
        owner_type: str,
        owner_id: str,
    ) -> dict[str, Any]:
        with self.unit_of_work() as uow:
            item = uow.work_items.assign(
                work_item_id,
                expected_version,
                owner_type,
                owner_id,
            )
            uow.commit()
            return item

    def get_timeline(self, work_item_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.events.list_for_work_item(work_item_id, limit=limit)

    def list_evidence(
        self,
        work_item_id: str,
        *,
        run_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self.unit_of_work() as uow:
            return uow.evidence.list(work_item_id, run_id=run_id, limit=limit)

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        """Return one exact evidence record for a trusted read-only projection."""

        with self.unit_of_work() as uow:
            return uow.evidence.get(evidence_id)

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self.unit_of_work() as uow:
            return uow.approvals.get(approval_id)

    def get_current_approval(self, run_id: str) -> dict[str, Any] | None:
        with self.unit_of_work() as uow:
            return uow.approvals.get_current_by_run(run_id)
