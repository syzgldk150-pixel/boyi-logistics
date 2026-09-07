-- A one-time compatibility boundary for stopped writes made before migration
-- 041 introduced the original-scope journal. Its persisted applied_at uses the
-- same database-local clock as receipt/Run created_at; lease expiry uses UTC.
-- Never recompute the boundary from the current session timezone or wall clock.
-- Preserve UNKNOWN outcomes, payloads, evidence and Run/Step history. A missing
-- historical scope is a review item, not an invented lock on every new write.
-- New missing scopes remain errors; runtime writers cannot set this marker.
SET @add_legacy_scope_quarantine = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE automation_write_attempt_receipts ADD COLUMN legacy_scope_quarantined_at DATETIME(6) NULL',
        'SELECT 1')
    FROM information_schema.columns
    WHERE table_schema=DATABASE() AND table_name='automation_write_attempt_receipts'
      AND column_name='legacy_scope_quarantined_at'
);
PREPARE legacy_scope_quarantine FROM @add_legacy_scope_quarantine;
EXECUTE legacy_scope_quarantine;
DEALLOCATE PREPARE legacy_scope_quarantine;

UPDATE automation_write_attempt_receipts AS receipt
INNER JOIN agent_runs AS run ON run.run_id=receipt.orchestration_run_id
INNER JOIN schema_migrations AS boundary ON boundary.version='041'
INNER JOIN automation_project_generation_leases AS lease
    ON lease.lease_id=receipt.lease_id
   AND lease.orchestration_run_id=receipt.orchestration_run_id
   AND BINARY lease.automation_id=BINARY receipt.automation_id
   AND lease.generation=receipt.generation
SET receipt.legacy_scope_quarantined_at=UTC_TIMESTAMP(6)
WHERE receipt.legacy_scope_quarantined_at IS NULL
  AND receipt.outcome='WRITE_OUTCOME_UNKNOWN'
  AND (receipt.execution_resource_keys_json IS NULL
       OR (JSON_TYPE(receipt.execution_resource_keys_json)='ARRAY'
           AND JSON_LENGTH(receipt.execution_resource_keys_json)=0))
  AND receipt.created_at < boundary.applied_at
  AND run.created_at < boundary.applied_at
  AND run.status IN ('CANCELLED','PARTIAL','FAILED_TERMINAL','BLOCKED_DATA')
  AND (NULLIF(TRIM(run.worker_id),'') IS NULL
       OR run.lease_expires_at IS NULL OR run.lease_expires_at<=UTC_TIMESTAMP(6))
  AND lease.outcome='WRITE_OUTCOME_UNKNOWN'
  AND lease.expires_at<=UTC_TIMESTAMP(6)
  AND NOT EXISTS (
      SELECT 1 FROM automation_project_generation_leases AS live_lease
      WHERE live_lease.orchestration_run_id=run.run_id
        AND live_lease.outcome IN ('RUNNING','VERIFYING')
        AND live_lease.expires_at>UTC_TIMESTAMP(6)
  );
