-- Retire suspended arrive-list scheduler occurrences from before the
-- project-level single-flight cutover.  Those historical Runs cannot execute
-- on their own, but they still block the next daily occurrence.  Request
-- cancellation through the normal Runner path so Run/Work Item state and
-- audit events are closed by the existing control-plane transition logic.

START TRANSACTION;

SET @cp037_cutoff_utc = TIMESTAMP('2026-09-03 16:00:00.000000');

SET @cp037_executing_run_count = (
    SELECT COUNT(*)
    FROM agent_commands AS command
    INNER JOIN agent_runs AS run
      ON run.command_id = command.command_id
    WHERE BINARY command.automation_id = BINARY 'arrive_list'
      AND BINARY command.source = BINARY 'scheduler'
      AND command.requested_at < @cp037_cutoff_utc
      AND run.status IN (
          'RECEIVED', 'CONTEXT_READY', 'PLANNED', 'VALIDATED',
          'WAITING_APPROVAL', 'RUNNING', 'VERIFYING', 'FAILED_RETRYABLE'
      )
);

SET @cp037_executing_guard_sql = IF(
    @cp037_executing_run_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp037_arrive_list_run_still_executing'
);
PREPARE cp037_executing_guard_stmt FROM @cp037_executing_guard_sql;
EXECUTE cp037_executing_guard_stmt;
DEALLOCATE PREPARE cp037_executing_guard_stmt;

SET @cp037_live_lease_count = (
    SELECT COUNT(*)
    FROM agent_commands AS command
    INNER JOIN agent_runs AS run
      ON run.command_id = command.command_id
    WHERE BINARY command.automation_id = BINARY 'arrive_list'
      AND BINARY command.source = BINARY 'scheduler'
      AND command.requested_at < @cp037_cutoff_utc
      AND run.status IN ('NEEDS_CLARIFICATION', 'BLOCKED_LOGIN', 'BLOCKED_DATA')
      AND run.worker_id IS NOT NULL
      AND run.lease_expires_at > UTC_TIMESTAMP(6)
);

SET @cp037_lease_guard_sql = IF(
    @cp037_live_lease_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp037_arrive_list_live_lease'
);
PREPARE cp037_lease_guard_stmt FROM @cp037_lease_guard_sql;
EXECUTE cp037_lease_guard_stmt;
DEALLOCATE PREPARE cp037_lease_guard_stmt;

UPDATE agent_runs AS run
INNER JOIN agent_commands AS command
  ON command.command_id = run.command_id
SET run.cancel_requested_at = UTC_TIMESTAMP(6),
    run.cancel_requested_by_type = 'system',
    run.cancel_requested_by_id = 'migration-037',
    run.cancel_reason = 'Superseded suspended arrive-list scheduler occurrence',
    run.version = run.version + 1,
    run.updated_at = UTC_TIMESTAMP(6)
WHERE BINARY command.automation_id = BINARY 'arrive_list'
  AND BINARY command.source = BINARY 'scheduler'
  AND command.requested_at < @cp037_cutoff_utc
  AND run.status IN ('NEEDS_CLARIFICATION', 'BLOCKED_LOGIN', 'BLOCKED_DATA')
  AND run.cancel_requested_at IS NULL
  AND (
      run.worker_id IS NULL
      OR run.lease_expires_at IS NULL
      OR run.lease_expires_at <= UTC_TIMESTAMP(6)
  );

SET @cp037_unmarked_run_count = (
    SELECT COUNT(*)
    FROM agent_commands AS command
    INNER JOIN agent_runs AS run
      ON run.command_id = command.command_id
    WHERE BINARY command.automation_id = BINARY 'arrive_list'
      AND BINARY command.source = BINARY 'scheduler'
      AND command.requested_at < @cp037_cutoff_utc
      AND run.status IN ('NEEDS_CLARIFICATION', 'BLOCKED_LOGIN', 'BLOCKED_DATA')
      AND run.cancel_requested_at IS NULL
);

SET @cp037_post_guard_sql = IF(
    @cp037_unmarked_run_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp037_arrive_list_cancel_request_failed'
);
PREPARE cp037_post_guard_stmt FROM @cp037_post_guard_sql;
EXECUTE cp037_post_guard_stmt;
DEALLOCATE PREPARE cp037_post_guard_stmt;

COMMIT;
