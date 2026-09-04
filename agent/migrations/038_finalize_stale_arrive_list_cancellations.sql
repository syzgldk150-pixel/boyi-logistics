-- Finalize only the orphaned arrive-list scheduler Runs that migration 037
-- already marked for cancellation.  A suspended Run has no live Runner task
-- to observe that request, so leaving it nonterminal keeps project-level
-- single-flight blocked indefinitely.  This migration revalidates the exact
-- historical identity and allowed state transitions, then closes the Run and
-- Work Item together with the same audit event emitted by WorkflowRunner.

START TRANSACTION;

SET @cp038_cutoff_utc = TIMESTAMP('2026-09-03 16:00:00.000000');
SET @cp038_error_summary = 'Superseded suspended arrive-list scheduler occurrence';

DROP TEMPORARY TABLE IF EXISTS cp038_arrive_list_candidates;

CREATE TEMPORARY TABLE cp038_arrive_list_candidates (
    run_id CHAR(36) NOT NULL,
    work_item_id CHAR(36) NOT NULL,
    correlation_id CHAR(36) NOT NULL,
    causation_id CHAR(36) NULL,
    previous_status VARCHAR(32) NOT NULL,
    previous_work_item_status VARCHAR(32) NOT NULL,
    event_id CHAR(36) NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    PRIMARY KEY (run_id),
    UNIQUE KEY uq_cp038_event_id (event_id)
) ENGINE=InnoDB;

INSERT INTO cp038_arrive_list_candidates (
    run_id,
    work_item_id,
    correlation_id,
    causation_id,
    previous_status,
    previous_work_item_status,
    event_id,
    occurred_at
)
SELECT
    run.run_id,
    run.work_item_id,
    run.correlation_id,
    run.causation_id,
    run.status,
    item.status,
    UUID(),
    UTC_TIMESTAMP(6)
FROM agent_commands AS command
INNER JOIN agent_runs AS run
  ON run.command_id = command.command_id
INNER JOIN work_items AS item
  ON item.work_item_id = run.work_item_id
WHERE BINARY command.automation_id = BINARY 'arrive_list'
  AND BINARY command.source = BINARY 'scheduler'
  AND command.requested_at < @cp038_cutoff_utc
  AND run.status IN ('NEEDS_CLARIFICATION', 'BLOCKED_LOGIN', 'BLOCKED_DATA')
  AND run.cancel_requested_at IS NOT NULL
  AND BINARY run.cancel_requested_by_type = BINARY 'system'
  AND BINARY run.cancel_requested_by_id = BINARY 'migration-037';

SET @cp038_live_lease_count = (
    SELECT COUNT(*)
    FROM cp038_arrive_list_candidates AS candidate
    INNER JOIN agent_runs AS run
      ON run.run_id = candidate.run_id
    WHERE run.worker_id IS NOT NULL
      AND run.lease_expires_at > UTC_TIMESTAMP(6)
);

SET @cp038_lease_guard_sql = IF(
    @cp038_live_lease_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp038_arrive_list_live_lease'
);
PREPARE cp038_lease_guard_stmt FROM @cp038_lease_guard_sql;
EXECUTE cp038_lease_guard_stmt;
DEALLOCATE PREPARE cp038_lease_guard_stmt;

SET @cp038_inconsistent_item_count = (
    SELECT COUNT(*)
    FROM cp038_arrive_list_candidates
    WHERE BINARY previous_work_item_status <> BINARY previous_status
       OR previous_work_item_status NOT IN (
           'NEEDS_CLARIFICATION', 'BLOCKED_LOGIN', 'BLOCKED_DATA'
       )
);

SET @cp038_item_guard_sql = IF(
    @cp038_inconsistent_item_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp038_arrive_list_item_state_mismatch'
);
PREPARE cp038_item_guard_stmt FROM @cp038_item_guard_sql;
EXECUTE cp038_item_guard_stmt;
DEALLOCATE PREPARE cp038_item_guard_stmt;

UPDATE agent_runs AS run
INNER JOIN cp038_arrive_list_candidates AS candidate
  ON candidate.run_id = run.run_id
SET run.status = 'CANCELLED',
    run.error_code = 'CANCELLED_BY_ACTOR',
    run.error_summary = @cp038_error_summary,
    run.retryable = FALSE,
    run.worker_id = NULL,
    run.lease_expires_at = NULL,
    run.finished_at = candidate.occurred_at,
    run.version = run.version + 1,
    run.updated_at = candidate.occurred_at
WHERE run.status = candidate.previous_status
  AND run.cancel_requested_at IS NOT NULL
  AND BINARY run.cancel_requested_by_id = BINARY 'migration-037';

UPDATE work_items AS item
INNER JOIN cp038_arrive_list_candidates AS candidate
  ON candidate.work_item_id = item.work_item_id
SET item.status = 'CANCELLED',
    item.current_reason_code = 'CANCELLED_BY_ACTOR',
    item.current_reason_summary = @cp038_error_summary,
    item.resolution_json = NULL,
    item.closed_at = candidate.occurred_at,
    item.version = item.version + 1,
    item.updated_at = candidate.occurred_at
WHERE item.status = candidate.previous_work_item_status;

INSERT INTO domain_events (
    event_id,
    event_type,
    schema_version,
    source_system,
    source_event_id,
    entity_type,
    entity_id,
    work_item_id,
    run_id,
    step_id,
    occurred_at,
    observed_at,
    correlation_id,
    causation_id,
    payload_json,
    payload_sha256,
    headers_json
)
SELECT
    candidate.event_id,
    'agent.run.status_changed',
    1,
    'agent',
    CONCAT('migration-038:', candidate.run_id),
    'agent_run',
    candidate.run_id,
    candidate.work_item_id,
    candidate.run_id,
    NULL,
    candidate.occurred_at,
    candidate.occurred_at,
    candidate.correlation_id,
    candidate.causation_id,
    JSON_OBJECT(
        'from', candidate.previous_status,
        'to', 'CANCELLED',
        'error_code', 'CANCELLED_BY_ACTOR',
        'error_summary', @cp038_error_summary
    ),
    SHA2(
        CAST(
            JSON_OBJECT(
                'from', candidate.previous_status,
                'to', 'CANCELLED',
                'error_code', 'CANCELLED_BY_ACTOR',
                'error_summary', @cp038_error_summary
            ) AS CHAR CHARACTER SET utf8mb4
        ),
        256
    ),
    NULL
FROM cp038_arrive_list_candidates AS candidate;

INSERT INTO outbox_events (
    event_id,
    consumer_name,
    topic,
    partition_key,
    status,
    available_at,
    attempt_count,
    max_attempts
)
SELECT
    candidate.event_id,
    'orchestration.audit',
    'agent.run.status_changed',
    candidate.work_item_id,
    'PENDING',
    candidate.occurred_at,
    0,
    10
FROM cp038_arrive_list_candidates AS candidate;

SET @cp038_unclosed_count = (
    SELECT COUNT(*)
    FROM cp038_arrive_list_candidates AS candidate
    INNER JOIN agent_runs AS run
      ON run.run_id = candidate.run_id
    INNER JOIN work_items AS item
      ON item.work_item_id = candidate.work_item_id
    WHERE run.status <> 'CANCELLED'
       OR item.status <> 'CANCELLED'
       OR run.finished_at IS NULL
       OR item.closed_at IS NULL
);

SET @cp038_post_guard_sql = IF(
    @cp038_unclosed_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp038_arrive_list_close_failed'
);
PREPARE cp038_post_guard_stmt FROM @cp038_post_guard_sql;
EXECUTE cp038_post_guard_stmt;
DEALLOCATE PREPARE cp038_post_guard_stmt;

DROP TEMPORARY TABLE cp038_arrive_list_candidates;

COMMIT;
