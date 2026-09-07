"""Minimal, configuration-free identities for one work item's unknown writes."""

from collections.abc import Mapping


def list_work_item_unknown_writes(repository, work_item_id: str) -> list[dict]:
    if not isinstance(work_item_id, str) or not work_item_id.strip():
        raise ValueError("work_item_id is required")
    with repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT l.lease_id, l.automation_id, l.generation,
                      r.run_id, r.work_item_id, l.outcome,
                      c.automation_id AS command_automation_id,
                      c.automation_generation AS command_generation,
                      p.plugin_id,
                      EXISTS (SELECT 1 FROM automation_write_attempt_receipts a
                              WHERE a.lease_id=l.lease_id
                                AND a.legacy_scope_quarantined_at IS NOT NULL
                      ) AS legacy_scope_unavailable
               FROM automation_project_generation_leases l
               JOIN agent_runs r ON r.run_id=l.orchestration_run_id
               JOIN agent_commands c ON c.command_id=r.command_id
               LEFT JOIN automation_projects p ON p.automation_id=l.automation_id
               WHERE r.work_item_id=%s AND l.outcome='WRITE_OUTCOME_UNKNOWN'
               ORDER BY r.run_no, l.generation, l.acquired_at, l.lease_id
               LIMIT 101""",
            (work_item_id,),
        )
        raw_rows = cursor.fetchall()
        columns = [column[0] for column in cursor.description]
    if len(raw_rows) > 100:
        raise ValueError("WORK_ITEM_RECOVERY_LIMIT_EXCEEDED")
    result = []
    for raw in raw_rows:
        row = dict(raw) if isinstance(raw, Mapping) else dict(zip(columns, raw))
        row["identity_valid"] = (
            row.pop("command_automation_id") == row["automation_id"]
            and row.pop("command_generation") == row["generation"]
        )
        row["legacy_scope_unavailable"] = bool(row["legacy_scope_unavailable"])
        # Do not expose any unselected fields even if a test adapter returns more.
        result.append({name: row[name] for name in (
            "lease_id", "automation_id", "generation", "run_id", "work_item_id",
            "outcome", "plugin_id", "identity_valid", "legacy_scope_unavailable",
        )})
    return result
