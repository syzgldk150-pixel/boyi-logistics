-- Only old, untouched first Runs whose SQL-default due time was written in the
-- database-local clock can be corrected. Explicit UTC retries and all execution
-- history remain unchanged. Do not change the session zone or historical clocks.
-- The runner autocommits statements, so persist the first clock capture before
-- updating rows. An interrupted deployment must reuse this exact boundary.
CREATE TABLE IF NOT EXISTS pending_run_timebase_snapshot_043 (
    singleton_id TINYINT UNSIGNED NOT NULL,
    captured_database_at DATETIME(6) NOT NULL,
    captured_utc_at DATETIME(6) NOT NULL,
    offset_seconds INT NOT NULL,
    candidate_run_ids JSON NOT NULL,
    PRIMARY KEY (singleton_id),
    CONSTRAINT chk_pending_run_timebase_singleton_043 CHECK (singleton_id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO pending_run_timebase_snapshot_043 (
    singleton_id, captured_database_at, captured_utc_at, offset_seconds, candidate_run_ids
)
SELECT 1, clock.database_at, clock.utc_at, clock.offset_seconds,
    COALESCE((
        SELECT JSON_ARRAYAGG(run.run_id)
        FROM agent_runs AS run
        WHERE clock.offset_seconds > 0
          AND run.status = 'RECEIVED'
          AND run.run_no = 1
          AND run.retry_of_run_id IS NULL
          AND run.version = 1
          AND run.execution_attempt_count = 0
          AND run.started_at IS NULL
          AND run.finished_at IS NULL
          AND run.worker_id IS NULL
          AND run.lease_expires_at IS NULL
          AND run.cancel_requested_at IS NULL
          AND run.plan_json IS NULL
          AND run.plan_hash IS NULL
          AND run.error_code IS NULL
          AND run.retryable = FALSE
          AND run.next_attempt_at = run.created_at
          AND run.next_attempt_at > clock.utc_at
          AND run.created_at <= clock.database_at
          AND TIMESTAMPADD(SECOND, -clock.offset_seconds, run.next_attempt_at) <= clock.utc_at
          AND NOT EXISTS (
              SELECT 1 FROM agent_run_steps AS step WHERE step.run_id = run.run_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM automation_project_generation_leases AS lease
              WHERE lease.orchestration_run_id = run.run_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM automation_write_attempt_receipts AS receipt
              WHERE receipt.orchestration_run_id = run.run_id
          )
    ), JSON_ARRAY())
FROM (
    SELECT NOW(6) AS database_at, UTC_TIMESTAMP(6) AS utc_at,
        TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(6), NOW(6)) AS offset_seconds
) AS clock;

UPDATE agent_runs AS run
INNER JOIN pending_run_timebase_snapshot_043 AS snapshot
    ON snapshot.singleton_id = 1
SET run.next_attempt_at = TIMESTAMPADD(SECOND, -snapshot.offset_seconds, run.next_attempt_at),
    run.version = run.version + 1
WHERE snapshot.offset_seconds > 0
  AND JSON_CONTAINS(snapshot.candidate_run_ids, JSON_QUOTE(run.run_id)) = 1
  AND run.status = 'RECEIVED'
  AND run.run_no = 1
  AND run.retry_of_run_id IS NULL
  AND run.version = 1
  AND run.execution_attempt_count = 0
  AND run.started_at IS NULL
  AND run.finished_at IS NULL
  AND run.worker_id IS NULL
  AND run.lease_expires_at IS NULL
  AND run.cancel_requested_at IS NULL
  AND run.plan_json IS NULL
  AND run.plan_hash IS NULL
  AND run.error_code IS NULL
  AND run.retryable = FALSE
  AND run.next_attempt_at = run.created_at
  AND run.next_attempt_at > snapshot.captured_utc_at
  AND run.created_at <= snapshot.captured_database_at
  AND TIMESTAMPADD(SECOND, -snapshot.offset_seconds, run.next_attempt_at) <= snapshot.captured_utc_at
  AND NOT EXISTS (
      SELECT 1 FROM agent_run_steps AS step WHERE step.run_id = run.run_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM automation_project_generation_leases AS lease
      WHERE lease.orchestration_run_id = run.run_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM automation_write_attempt_receipts AS receipt
      WHERE receipt.orchestration_run_id = run.run_id
  );
