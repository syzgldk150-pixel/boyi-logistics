"""Persistence helper for quarantining an unknown generation write outcome.

The public repository method remains on ``AutomationPluginRepository``; this
module only holds the SQL transaction body so the generation repository mixin
stays focused on generation lifecycle operations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    OrchestrationPersistenceError,
    _row_dict,
    _rows,
)


def stabilize_project_after_archival_unknown(
    cursor: Any,
    *,
    automation_id: str,
    generation: int,
) -> None:
    """Close a completed drain while retaining historical unknown evidence."""

    cursor.execute(
        """
        UPDATE automation_projects AS project
        SET project.reconcile_state='STABLE', project.updated_at=NOW(6)
        WHERE project.automation_id=%s
          AND project.target_generation=project.committed_generation
          AND project.committed_generation<>%s
          AND project.reconcile_state='DRAINING'
          AND EXISTS (
              SELECT 1
              FROM automation_project_generations AS committed
              WHERE committed.automation_id=project.automation_id
                AND committed.generation=project.committed_generation
                AND committed.state='COMMITTED'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM automation_project_generations AS pending
              WHERE pending.automation_id=project.automation_id
                AND pending.generation<>project.committed_generation
                AND pending.state IN (
                    'DRAINING', 'DISPOSING', 'FAILED', 'BLOCKED'
                )
                AND (
                    pending.state<>'BLOCKED'
                    OR NOT EXISTS (
                        SELECT 1
                        FROM automation_project_generation_leases AS archival_lease
                        WHERE archival_lease.automation_id=pending.automation_id
                          AND archival_lease.generation=pending.generation
                          AND archival_lease.outcome='WRITE_OUTCOME_UNKNOWN'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM automation_project_generation_leases AS active_lease
                        WHERE active_lease.automation_id=pending.automation_id
                          AND active_lease.generation=pending.generation
                          AND active_lease.outcome IN ('RUNNING', 'VERIFYING')
                    )
                )
          )
        """,
        (automation_id, generation),
    )


def block_generation_unknown_write_row(
    repository: Any,
    automation_id: str,
    generation: int,
    *,
    required_text: Callable[[Any, str], str],
    positive_int: Callable[[Any, str], int],
) -> None:
    """Retain unknown-write evidence without freezing the current route.

    A non-current generation is still blocked from disposal so its immutable
    code and receipts remain available for audit. The current committed
    generation stays runnable; a later command always receives a new lease.
    """

    safe_automation_id = required_text(automation_id, "automation_id")
    safe_generation = positive_int(generation, "generation")
    with repository.cursor() as cursor:
        # Disposal/reconciliation can race release/finalization; a lease-first count deadlocks.
        # Parent locks must precede the lease set in project -> generation -> lease order.
        cursor.execute(
            """
            SELECT automation_id, committed_generation FROM automation_projects
            WHERE automation_id=%s FOR UPDATE
            """,
            (safe_automation_id,),
        )
        project = _row_dict(cursor, cursor.fetchone())
        if project is None:
            raise OrchestrationPersistenceError(
                "automation project disappeared during unknown-write block"
            )
        cursor.execute(
            """
            SELECT state FROM automation_project_generations
            WHERE automation_id=%s AND generation=%s FOR UPDATE
            """,
            (safe_automation_id, safe_generation),
        )
        generation_row = _row_dict(cursor, cursor.fetchone())
        if generation_row is None:
            raise OrchestrationPersistenceError(
                "runtime generation disappeared during unknown-write block"
            )
        if str(generation_row.get("state") or "") == "DISPOSED":
            raise ConcurrentUpdateError("runtime generation is already disposed")
        cursor.execute(
            """
            SELECT lease_id
            FROM automation_project_generation_leases
            WHERE automation_id=%s AND generation=%s
            AND outcome='WRITE_OUTCOME_UNKNOWN' FOR UPDATE
            """,
            (safe_automation_id, safe_generation),
        )
        if not _rows(cursor):
            raise ConcurrentUpdateError(
                "runtime generation has no unknown write evidence"
            )
        if int(project.get("committed_generation") or 0) == safe_generation:
            return
        cursor.execute(
            """
            UPDATE automation_project_generations
            SET state='BLOCKED', error_code='WRITE_OUTCOME_UNKNOWN',
                error_summary='Unknown external write outcome requires reconciliation',
                record_version=record_version+1, updated_at=NOW(6)
            WHERE automation_id=%s AND generation=%s AND state<>'DISPOSED'
            """,
            (safe_automation_id, safe_generation),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConcurrentUpdateError(
                "runtime generation unknown-write state changed"
            )
        # The unknown predecessor is now a durable audit archive, not active
        # runtime work.  If the successor is already the committed target and
        # no other non-archival generation remains, close the drain journal in
        # this same transaction.  Otherwise the project can remain stuck in
        # DRAINING forever even though no process is still using the old route.
        stabilize_project_after_archival_unknown(
            cursor,
            automation_id=safe_automation_id,
            generation=safe_generation,
        )


def lock_archival_unknown_predecessor(
    cursor: Any,
    *,
    automation_id: str,
    expected_committed: int | None,
) -> bool:
    """Lock and validate a predecessor before committing a prepared generation."""

    archival_unknown_predecessor = False
    if expected_committed is not None:
        cursor.execute(
            """
            SELECT state, error_code
            FROM automation_project_generations
            WHERE automation_id=%s AND generation=%s FOR UPDATE
            """,
            (automation_id, expected_committed),
        )
        predecessor = _row_dict(cursor, cursor.fetchone())
        if predecessor is None:
            raise ConcurrentUpdateError(
                "previous committed runtime generation disappeared"
            )
        predecessor_state = str(predecessor.get("state") or "")
        if predecessor_state == "BLOCKED":
            if predecessor.get("error_code") != "WRITE_OUTCOME_UNKNOWN":
                raise ConcurrentUpdateError(
                    "blocked predecessor does not carry unknown-write evidence"
                )
            cursor.execute(
                """
                SELECT lease_id, outcome
                FROM automation_project_generation_leases
                WHERE automation_id=%s AND generation=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (automation_id, expected_committed),
            )
            leases = tuple(_rows(cursor))
            if any(
                row.get("outcome") in {"RUNNING", "VERIFYING"} for row in leases
            ):
                raise ConcurrentUpdateError(
                    "blocked predecessor still has active runtime leases"
                )
            if not any(
                row.get("outcome") == "WRITE_OUTCOME_UNKNOWN" for row in leases
            ):
                raise ConcurrentUpdateError(
                    "blocked predecessor has no unknown-write lease"
                )
            archival_unknown_predecessor = True
        elif predecessor_state != "COMMITTED":
            raise ConcurrentUpdateError(
                "previous committed runtime generation is not switchable"
            )
    return archival_unknown_predecessor
