-- Keep historical unknown-write leases for audit while restoring the current
-- committed automation route. Re-running an automation creates a new Command,
-- Run and generation lease; it never replays the historical lease.
START TRANSACTION;

UPDATE automation_project_generations AS generation
JOIN automation_projects AS project
  ON project.automation_id = generation.automation_id
 AND project.committed_generation = generation.generation
 AND project.target_generation = generation.generation
SET generation.state = 'COMMITTED',
    generation.error_code = NULL,
    generation.error_summary = NULL,
    generation.committed_at = COALESCE(generation.committed_at, UTC_TIMESTAMP(6)),
    generation.record_version = generation.record_version + 1,
    generation.updated_at = UTC_TIMESTAMP(6)
WHERE project.reconcile_state = 'BLOCKED_UNKNOWN_WRITE'
  AND generation.state = 'BLOCKED'
  AND generation.error_code = 'WRITE_OUTCOME_UNKNOWN'
  AND EXISTS (
      SELECT 1
      FROM automation_project_generation_leases AS unknown_lease
      WHERE unknown_lease.automation_id = generation.automation_id
        AND unknown_lease.generation = generation.generation
        AND unknown_lease.outcome = 'WRITE_OUTCOME_UNKNOWN'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM automation_project_generation_leases AS active_lease
      WHERE active_lease.automation_id = generation.automation_id
        AND active_lease.generation = generation.generation
        AND active_lease.outcome IN ('RUNNING', 'VERIFYING')
  );

UPDATE automation_projects AS project
JOIN automation_project_generations AS generation
  ON generation.automation_id = project.automation_id
 AND generation.generation = project.committed_generation
SET project.reconcile_state = 'STABLE',
    project.updated_at = UTC_TIMESTAMP(6)
WHERE project.reconcile_state = 'BLOCKED_UNKNOWN_WRITE'
  AND project.target_generation = project.committed_generation
  AND generation.state = 'COMMITTED'
  AND EXISTS (
      SELECT 1
      FROM automation_project_generation_leases AS unknown_lease
      WHERE unknown_lease.automation_id = project.automation_id
        AND unknown_lease.generation = project.committed_generation
        AND unknown_lease.outcome = 'WRITE_OUTCOME_UNKNOWN'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM automation_project_generation_leases AS active_lease
      WHERE active_lease.automation_id = project.automation_id
        AND active_lease.generation = project.committed_generation
        AND active_lease.outcome IN ('RUNNING', 'VERIFYING')
  );

COMMIT;
