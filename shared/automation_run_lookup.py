"""Automation-project Run lookups shared by orchestration repositories."""

from __future__ import annotations

from typing import Any

from shared.orchestration_repository_support import _required_text, _row_dict


class AutomationRunLookupMixin:
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
