"""Persistence helper for quarantining an unknown generation write outcome.

The public repository method remains on ``AutomationPluginRepository``; this
module only holds the SQL transaction body so the generation repository mixin
stays focused on generation lifecycle operations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
    _required_text,
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


def settle_unknown_write_recovery_row(
    self,
    *,
    automation_id: str,
    generation: int,
    lease_id: str,
    recovery_status: str,
    evidence_sha256: str,
    locked_context: Mapping[str, Mapping[str, Any]] | None = None,
    allow_historical: bool = False,
) -> dict[str, Any]:
    """Persist a receipt-proven recovery while caller-owned locks are held."""

    from shared.automation_plugin_repository import _positive_int, _sha256
    from shared.automation_unknown_write_repository import lock_remaining_unknown_generation_leases

    safe_automation_id = _required_text(automation_id, "automation_id")
    safe_generation = _positive_int(generation, "generation")
    safe_lease_id = _required_text(lease_id, "lease_id")
    safe_evidence = _sha256(evidence_sha256, "evidence_sha256")
    if type(allow_historical) is not bool:
        raise ValueError("historical recovery mode must be boolean")
    status = str(recovery_status or "").upper()
    if status not in {"APPLIED", "NOT_APPLIED"}:
        raise ValueError("unknown-write recovery status is invalid")
    desired_lease_outcome = (
        "WRITE_VERIFIED" if status == "APPLIED" else "FAILED_BEFORE_WRITE"
    )
    with self.cursor() as cursor:
        # Transactional recovery already holds project -> generation ->
        # lease before locking its Run/Step and receipts.  Do not reissue
        # those FOR UPDATE statements after the orchestration locks: even
        # though MySQL treats them as re-entrant, that is an inverse lock
        # trace and obscures the contract.  The standalone compatibility
        # path acquires the same hierarchy itself.
        if locked_context is None:
            cursor.execute(
                """
                SELECT target_generation, committed_generation, reconcile_state
                FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            cursor.execute(
                """
                SELECT state, error_code FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            cursor.execute(
                """
                SELECT outcome, verification_evidence_sha256, automation_id, generation
                FROM automation_project_generation_leases
                WHERE lease_id=%s FOR UPDATE
                """,
                (safe_lease_id,),
            )
            lease = _row_dict(cursor, cursor.fetchone())
        else:
            project = dict(locked_context.get("project") or {})
            generation_row = dict(locked_context.get("generation") or {})
            lease = dict(locked_context.get("lease") or {})
        if project is None or generation_row is None or lease is None:
            raise OrchestrationPersistenceError("runtime recovery rows disappeared")
        if (
            str(lease.get("automation_id") or "") != safe_automation_id
            or int(lease.get("generation") or 0) != safe_generation
            or str(lease.get("lease_id") or safe_lease_id) != safe_lease_id
        ):
            raise IdempotencyConflict("runtime recovery does not match its generation lease")
        historical = allow_historical and safe_generation < int(project.get("committed_generation") or 0)
        if not historical and (
            int(project.get("target_generation") or 0) != safe_generation
            or int(project.get("committed_generation") or 0) != safe_generation
        ):
            raise ConcurrentUpdateError(
                "runtime recovery generation is no longer the current committed target"
            )
        current = str(lease.get("outcome") or "")
        lease_transitioned = False
        if current == desired_lease_outcome:
            existing_evidence = str(lease.get("verification_evidence_sha256") or "")
            if existing_evidence and existing_evidence != safe_evidence:
                raise IdempotencyConflict("runtime recovery was reused with different evidence")
            if existing_evidence != safe_evidence:
                cursor.execute(
                    """
                    UPDATE automation_project_generation_leases
                    SET verification_evidence_sha256=%s,
                        released_at=COALESCE(released_at, NOW(6)), updated_at=NOW(6)
                    WHERE lease_id=%s AND outcome=%s
                      AND verification_evidence_sha256 IS NULL
                    """,
                    (safe_evidence, safe_lease_id, current),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError("runtime recovery lease evidence changed")
                lease_transitioned = True
        else:
            allowed = (
                {"WRITE_OUTCOME_UNKNOWN"}
                if status == "APPLIED"
                else {"WRITE_OUTCOME_UNKNOWN", "FAILED_BEFORE_WRITE"}
            )
            if current not in allowed:
                raise ConcurrentUpdateError("runtime lease is not recoverable")
            cursor.execute(
                """
                UPDATE automation_project_generation_leases
                SET outcome=%s, verification_evidence_sha256=%s,
                    released_at=COALESCE(released_at, NOW(6)), updated_at=NOW(6)
                WHERE lease_id=%s AND outcome=%s
                """,
                (desired_lease_outcome, safe_evidence, safe_lease_id, current),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime recovery lease changed")
            lease_transitioned = True
        if lock_remaining_unknown_generation_leases(cursor, safe_automation_id, safe_generation):
            return {"transitioned": lease_transitioned, "outcome": desired_lease_outcome}
        if historical:
            # The old route is never made current again. A fully verified
            # archive may rejoin the existing disposal lifecycle, so it
            # cannot become a permanent non-unknown BLOCKED predecessor.
            if (
                str(generation_row.get("state") or "") == "BLOCKED"
                and str(generation_row.get("error_code") or "") == "WRITE_OUTCOME_UNKNOWN"
            ):
                cursor.execute(
                    """UPDATE automation_project_generations
                       SET state='DRAINING', error_code=NULL, error_summary=NULL,
                           record_version=record_version+1, updated_at=NOW(6)
                       WHERE automation_id=%s AND generation=%s
                         AND state='BLOCKED' AND error_code='WRITE_OUTCOME_UNKNOWN'""",
                    (safe_automation_id, safe_generation),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError("historical recovery generation changed")
            return {"transitioned": lease_transitioned, "outcome": desired_lease_outcome}
        if (
            str(generation_row.get("state") or "") == "COMMITTED"
            and str(project.get("reconcile_state") or "") == "STABLE"
        ):
            return {"transitioned": lease_transitioned, "outcome": desired_lease_outcome}
        cursor.execute(
            """
            UPDATE automation_project_generations
            SET state='COMMITTED', error_code=NULL, error_summary=NULL,
                committed_at=COALESCE(committed_at, NOW(6)),
                record_version=record_version+1, updated_at=NOW(6)
            WHERE automation_id=%s AND generation=%s
              AND state IN ('BLOCKED', 'COMMITTED')
            """,
            (safe_automation_id, safe_generation),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConcurrentUpdateError("runtime generation is not recoverable")
        cursor.execute(
            """
            UPDATE automation_projects
            SET reconcile_state='STABLE', updated_at=NOW(6)
            WHERE automation_id=%s AND target_generation=%s
              AND committed_generation=%s
              AND reconcile_state IN ('BLOCKED_UNKNOWN_WRITE', 'STABLE')
            """,
            (safe_automation_id, safe_generation, safe_generation),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConcurrentUpdateError("runtime project is not recoverable")
    return {"transitioned": True, "outcome": desired_lease_outcome}
