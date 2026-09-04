"""Automation-project Run lookups shared by orchestration repositories."""

from __future__ import annotations

from typing import Any

from shared.orchestration_repository_support import _required_text, _row_dict, _rows


AUTOMATION_UNFINISHED_RUN_LIMIT = 100


class AutomationRunLookupMixin:
    def list_unfinished_for_automation(
        self,
        automation_id: str,
        *,
        limit: int = AUTOMATION_UNFINISHED_RUN_LIMIT + 1,
    ) -> list[str]:
        """Discover a bounded, deterministic set of unfinished project Runs.

        Discovery deliberately does not lock a join.  Callers lock and
        revalidate each aggregate in the canonical Run -> Command -> Work Item
        order before changing state.
        """

        bounded_limit = max(
            1,
            min(int(limit), AUTOMATION_UNFINISHED_RUN_LIMIT + 1),
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.run_id
                FROM agent_commands AS c
                INNER JOIN agent_runs AS r ON r.command_id=c.command_id
                WHERE BINARY c.automation_id=BINARY %s
                  AND r.status NOT IN (
                      'COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED'
                  )
                ORDER BY c.requested_at, r.created_at, r.run_id
                LIMIT %s
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    bounded_limit,
                ),
            )
            return [str(row["run_id"]) for row in _rows(cursor)]

    def get_automation_supersession_facts(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        """Return fail-closed execution and write facts for one locked Run."""

        safe_run_id = _required_text(run_id, "run_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM agent_run_steps AS step
                        WHERE step.run_id=%s
                          AND step.status IN ('RUNNING', 'VERIFYING')
                    ) AS has_inflight_step,
                    EXISTS (
                        SELECT 1
                        FROM agent_run_steps AS step
                        WHERE step.run_id=%s
                          AND step.error_code='WRITE_OUTCOME_UNKNOWN'
                    ) AS has_unknown_step,
                    EXISTS (
                        SELECT 1
                        FROM automation_project_generation_leases AS lease
                        WHERE lease.orchestration_run_id=%s
                          AND lease.outcome IN ('RUNNING', 'VERIFYING')
                          AND lease.expires_at > UTC_TIMESTAMP(6)
                    ) AS has_live_generation_lease,
                    EXISTS (
                        SELECT 1
                        FROM automation_project_generation_leases AS lease
                        WHERE lease.orchestration_run_id=%s
                          AND lease.outcome='WRITE_OUTCOME_UNKNOWN'
                    ) AS has_unknown_generation_lease,
                    EXISTS (
                        SELECT 1
                        FROM automation_write_attempt_receipts AS receipt
                        WHERE receipt.orchestration_run_id=%s
                          AND receipt.outcome='WRITE_OUTCOME_UNKNOWN'
                    ) AS has_unknown_write_receipt,
                    EXISTS (
                        SELECT 1
                        FROM automation_write_attempt_receipts AS receipt
                        INNER JOIN agent_run_steps AS step
                            ON step.step_id=receipt.step_id
                           AND step.run_id=receipt.orchestration_run_id
                        WHERE receipt.orchestration_run_id=%s
                          AND step.operation_type IN (
                              'EXTERNAL_WRITE', 'FINANCIAL_WRITE', 'DESTRUCTIVE'
                          )
                          AND receipt.outcome='STARTED'
                    ) AS has_unclosed_protected_write,
                    EXISTS (
                        SELECT 1
                        FROM automation_write_attempt_receipts AS receipt
                        INNER JOIN agent_run_steps AS step
                            ON step.step_id=receipt.step_id
                           AND step.run_id=receipt.orchestration_run_id
                        WHERE receipt.orchestration_run_id=%s
                          AND step.operation_type IN (
                              'EXTERNAL_WRITE', 'FINANCIAL_WRITE', 'DESTRUCTIVE'
                          )
                          AND receipt.outcome IN (
                              'STARTED', 'WRITE_VERIFIED',
                              'WRITE_OUTCOME_UNKNOWN'
                          )
                    ) AS has_protected_write_receipt
                """,
                (safe_run_id,) * 7,
            )
            row = _row_dict(cursor, cursor.fetchone()) or {}
        return {
            "has_inflight_step": bool(row.get("has_inflight_step")),
            "has_unknown_step": bool(row.get("has_unknown_step")),
            "has_live_generation_lease": bool(
                row.get("has_live_generation_lease")
            ),
            "has_unknown_generation_lease": bool(
                row.get("has_unknown_generation_lease")
            ),
            "has_unknown_write_receipt": bool(
                row.get("has_unknown_write_receipt")
            ),
            "has_unclosed_protected_write": bool(
                row.get("has_unclosed_protected_write")
            ),
            "has_protected_write_receipt": bool(
                row.get("has_protected_write_receipt")
            ),
        }

    def get_active_for_automation(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        """Return the oldest unfinished Run for one exact automation project."""

        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT r.run_id, r.work_item_id, r.command_id, r.status,
                       r.cancel_requested_at, r.created_at, r.updated_at,
                       c.source AS command_source, c.requested_at
                FROM agent_commands c
                INNER JOIN agent_runs r ON r.command_id=c.command_id
                WHERE BINARY c.automation_id=BINARY %s
                  AND r.status NOT IN (
                      'COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED'
                  )
                ORDER BY c.requested_at, r.created_at, r.run_id
                LIMIT 1{suffix}
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            return _row_dict(cursor, cursor.fetchone())
