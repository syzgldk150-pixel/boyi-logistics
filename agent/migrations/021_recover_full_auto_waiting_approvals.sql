-- Migration 021: recover typed automation Runs stranded behind approvals
-- after migration 020 made their projects fully automatic.
--
-- The policy join intentionally reads the current administrator intent. A
-- project explicitly changed back to REQUIRE_EACH_RUN after migration 020 is
-- not touched. Services are quiesced while release migrations run, and this
-- single multi-table UPDATE keeps the Run wake-up and approval invalidation in
-- the same MySQL statement.

UPDATE agent_runs AS run
JOIN approval_requests AS approval
  ON approval.run_id = run.run_id
JOIN agent_commands AS command
  ON command.command_id = run.command_id
JOIN automation_project_policies AS policy
  ON policy.automation_id = command.automation_id
SET approval.status='INVALIDATED',
    approval.decided_at=NOW(6),
    run.next_attempt_at=NOW(6),
    run.worker_id=NULL,
    run.lease_expires_at=NULL,
    run.version=run.version+1
WHERE run.status='WAITING_APPROVAL'
  AND approval.status IN ('PENDING', 'APPROVED')
  AND command.command_type='automation.project.invoke'
  AND command.automation_invocation_json IS NOT NULL
  AND policy.mode='PROJECT_FULL_AUTO';
