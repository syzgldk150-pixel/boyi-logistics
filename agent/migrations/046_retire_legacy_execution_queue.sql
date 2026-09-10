-- Apply only after release hold and process quiescence. This retires old
-- resumable facts; it never invokes a tool, changes a schedule or replays work.
-- Historical errors, results, approval decisions and write receipts stay intact.
START TRANSACTION;

SET @cp046_at = UTC_TIMESTAMP(6);
SET @cp046_reason = 'Legacy execution retired at direct-plugin cutover. Trigger a new invocation explicitly';

SET @cp046_busy = (
    SELECT COUNT(*) FROM agent_runs
    WHERE status IN ('RUNNING','VERIFYING')
       OR lease_expires_at > @cp046_at
       OR ((worker_id IS NOT NULL OR lease_expires_at IS NOT NULL)
           AND status NOT IN ('COMPLETED','PARTIAL','FAILED_TERMINAL','CANCELLED'))
) + (
    SELECT COUNT(*) FROM agent_run_steps step
    WHERE step.status IN ('RUNNING','VERIFYING')
      -- A terminal parent with a recorded finish and no remaining ownership
      -- makes this an immutable historical Step, not executable work. Preserve
      -- its original status and all write uncertainty; elapsed time is no proof.
      AND NOT EXISTS (SELECT 1 FROM agent_runs parent
          WHERE parent.run_id=step.run_id
            AND parent.status IN ('COMPLETED','PARTIAL','FAILED_TERMINAL','CANCELLED')
            AND parent.finished_at IS NOT NULL
            AND parent.worker_id IS NULL AND parent.lease_expires_at IS NULL)
) + (
    SELECT COUNT(*) FROM automation_project_generation_leases WHERE outcome IN ('RUNNING','VERIFYING')
) + (
    SELECT COUNT(*) FROM automation_plugin_invocations WHERE status IN ('STARTING','RUNNING','CANCELLING')
);
SET @cp046_guard_sql = IF(@cp046_busy=0, 'SELECT 1',
    'SELECT * FROM information_schema.cp046_execution_not_quiescent');
PREPARE cp046_guard FROM @cp046_guard_sql;
EXECUTE cp046_guard;
DEALLOCATE PREPARE cp046_guard;

DROP TEMPORARY TABLE IF EXISTS cp046_candidates;
CREATE TEMPORARY TABLE cp046_candidates (
    run_id CHAR(36) PRIMARY KEY,
    work_item_id CHAR(36) NOT NULL,
    correlation_id CHAR(36) NOT NULL,
    old_status VARCHAR(32) NOT NULL,
    old_item_status VARCHAR(32) NOT NULL,
    target_status VARCHAR(32) NOT NULL,
    unconfirmed_reasons_json JSON NOT NULL,
    event_id CHAR(36) NOT NULL,
    payload_json JSON NOT NULL
) ENGINE=InnoDB;

INSERT INTO cp046_candidates
SELECT run.run_id, run.work_item_id, run.correlation_id, run.status, item.status,
    'CANCELLED', JSON_ARRAY(), UUID(),
    JSON_OBJECT('reason_code','LEGACY_EXECUTION_RETIRED','reason',@cp046_reason,
        'from',run.status,'to','CANCELLED','execution_retired',CAST('true' AS JSON),
        'original_error_code',run.error_code,'original_error_summary',run.error_summary,
        'original_item_status',item.status,'original_item_reason_code',item.current_reason_code,
        'original_item_reason_summary',item.current_reason_summary)
FROM agent_runs run JOIN work_items item ON item.work_item_id=run.work_item_id
WHERE run.status IN ('RECEIVED','CONTEXT_READY','PLANNED','VALIDATED','WAITING_APPROVAL',
    'FAILED_RETRYABLE','NEEDS_CLARIFICATION','BLOCKED_LOGIN','BLOCKED_DATA')
FOR UPDATE;

-- Quiescence proves execution has stopped, never that a write did not happen.
-- End unknown-write executions as failures while preserving every original
-- business fact. Record why their result remains unconfirmed, without replay.
UPDATE cp046_candidates candidate JOIN agent_runs run ON run.run_id=candidate.run_id
SET candidate.unconfirmed_reasons_json=JSON_MERGE_PRESERVE(
    IF(run.error_code IN ('WRITE_OUTCOME_UNKNOWN','MANUAL_VERIFICATION_REQUIRED'),
        JSON_ARRAY('RUN_WRITE_OUTCOME_UNCONFIRMED'), JSON_ARRAY()),
    IF(EXISTS(SELECT 1 FROM automation_write_attempt_receipts receipt
            WHERE receipt.orchestration_run_id=run.run_id
              AND receipt.outcome IN ('STARTED','WRITE_OUTCOME_UNKNOWN')),
        JSON_ARRAY('WRITE_RECEIPT_UNCONFIRMED'), JSON_ARRAY()),
    IF(EXISTS(SELECT 1 FROM agent_run_steps step
            WHERE step.run_id=run.run_id
              AND step.operation_type IN ('INTERNAL_PROJECTION_WRITE','EXTERNAL_WRITE','FINANCIAL_WRITE','DESTRUCTIVE')
              AND (step.attempt_count>0 OR step.started_at IS NOT NULL)
              AND NOT (step.status='COMPLETED'
                  AND COALESCE(step.postcondition_status,'') IN ('VERIFIED','VERIFIED_AFTER_RECOVERY'))
              AND NOT (EXISTS(SELECT 1 FROM automation_write_attempt_receipts receipt
                    WHERE receipt.orchestration_run_id=run.run_id AND receipt.step_id=step.step_id)
                  AND NOT EXISTS(SELECT 1 FROM automation_write_attempt_receipts receipt
                    WHERE receipt.orchestration_run_id=run.run_id AND receipt.step_id=step.step_id
                      AND receipt.outcome NOT IN ('WRITE_VERIFIED','NOT_APPLIED')))),
        JSON_ARRAY('ATTEMPTED_WRITE_WITHOUT_VERIFICATION'), JSON_ARRAY())
);

UPDATE cp046_candidates
SET target_status=IF(JSON_LENGTH(unconfirmed_reasons_json)>0,'FAILED_TERMINAL','CANCELLED'),
    payload_json=JSON_SET(payload_json,
        '$.to',IF(JSON_LENGTH(unconfirmed_reasons_json)>0,'FAILED_TERMINAL','CANCELLED'),
        '$.write_outcome_unconfirmed',CAST(IF(JSON_LENGTH(unconfirmed_reasons_json)>0,'true','false') AS JSON),
        '$.unconfirmed_reasons',unconfirmed_reasons_json);

-- A historical terminal Run may share a Work Item with its latest retry.
-- Close the item only when every remaining nonterminal Run is a candidate.
UPDATE agent_runs run JOIN cp046_candidates candidate ON candidate.run_id=run.run_id
SET run.status=candidate.target_status, run.retryable=FALSE,
    run.cancel_requested_at=IF(candidate.target_status='CANCELLED',COALESCE(run.cancel_requested_at,@cp046_at),run.cancel_requested_at),
    run.cancel_requested_by_type=IF(candidate.target_status='CANCELLED',COALESCE(run.cancel_requested_by_type,'system'),run.cancel_requested_by_type),
    run.cancel_requested_by_id=IF(candidate.target_status='CANCELLED',COALESCE(run.cancel_requested_by_id,'migration-046'),run.cancel_requested_by_id),
    run.cancel_reason=IF(candidate.target_status='CANCELLED',COALESCE(run.cancel_reason,@cp046_reason),run.cancel_reason),
    run.finished_at=COALESCE(run.finished_at,@cp046_at),
    run.version=run.version+1, run.updated_at=@cp046_at;

UPDATE agent_run_steps step JOIN cp046_candidates candidate ON candidate.run_id=step.run_id
SET step.status=candidate.target_status, step.finished_at=COALESCE(step.finished_at,@cp046_at),
    step.version=step.version+1, step.updated_at=@cp046_at
WHERE step.status IN ('PENDING','WAITING_APPROVAL','BLOCKED_LOGIN','BLOCKED_DATA','FAILED_RETRYABLE');

-- Work Items have no failed-terminal state. CANCELLED closes execution only;
-- original reason/resolution and the Run's unconfirmed-write audit stay intact.
UPDATE work_items item JOIN (SELECT DISTINCT work_item_id FROM cp046_candidates) candidate
  ON candidate.work_item_id=item.work_item_id
SET item.status='CANCELLED', item.closed_at=COALESCE(item.closed_at,@cp046_at),
    item.version=item.version+1, item.updated_at=@cp046_at
WHERE item.status NOT IN ('RESOLVED','COMPLETED','PARTIAL','FAILED_TERMINAL','CANCELLED');

INSERT INTO domain_events(event_id,event_type,schema_version,source_system,source_event_id,
    entity_type,entity_id,work_item_id,run_id,occurred_at,observed_at,correlation_id,payload_json,payload_sha256)
SELECT event_id,'agent.legacy_execution.retired',1,'migration-046',run_id,
    'agent_run',run_id,work_item_id,run_id,@cp046_at,@cp046_at,correlation_id,payload_json,
    SHA2(CAST(payload_json AS CHAR CHARACTER SET utf8mb4),256)
FROM cp046_candidates;

-- No outbox row: retirement is an offline audit fact, not a pending action.
DROP TEMPORARY TABLE cp046_candidates;
COMMIT;
