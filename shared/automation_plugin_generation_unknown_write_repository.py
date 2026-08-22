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


def block_generation_unknown_write_row(
    repository: Any,
    automation_id: str,
    generation: int,
    *,
    required_text: Callable[[Any, str], str],
    positive_int: Callable[[Any, str], int],
) -> None:
    """Block one generation after durable unknown-write evidence is found."""

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
        if _row_dict(cursor, cursor.fetchone()) is None:
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
        cursor.execute(
            """
            UPDATE automation_projects
            SET reconcile_state='BLOCKED_UNKNOWN_WRITE', updated_at=NOW(6)
            WHERE automation_id=%s AND committed_generation=%s
            """,
            (safe_automation_id, safe_generation),
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
            SELECT state
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
            cursor.execute(
                """
                SELECT lease_id
                FROM automation_project_generation_leases
                WHERE automation_id=%s AND generation=%s
                  AND outcome='WRITE_OUTCOME_UNKNOWN'
                FOR UPDATE
                """,
                (automation_id, expected_committed),
            )
            if not _rows(cursor):
                raise ConcurrentUpdateError(
                    "blocked predecessor has no unknown-write lease"
                )
            archival_unknown_predecessor = True
        elif predecessor_state != "COMMITTED":
            raise ConcurrentUpdateError(
                "previous committed runtime generation is not switchable"
            )
    return archival_unknown_predecessor
