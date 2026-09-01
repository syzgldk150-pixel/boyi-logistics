-- Finalize stale pre-write generation leases left behind when ResultVerifier
-- rejected invalid lease metadata before a write-attempt receipt was created.
-- The linked terminal Run and absence of every receipt prove that the signed
-- external-write boundary was never crossed.
START TRANSACTION;

UPDATE automation_project_generation_leases AS lease
INNER JOIN agent_runs AS run
  ON run.run_id = lease.orchestration_run_id
SET lease.outcome = 'FAILED_BEFORE_WRITE',
    lease.released_at = COALESCE(lease.released_at, UTC_TIMESTAMP(6)),
    lease.updated_at = UTC_TIMESTAMP(6)
WHERE lease.outcome IN ('RUNNING', 'VERIFYING')
  AND lease.expires_at <= UTC_TIMESTAMP(6)
  AND run.status = 'FAILED_TERMINAL'
  AND run.error_code = 'GENERATION_LEASE_INVALID'
  AND NOT EXISTS (
      SELECT 1
      FROM automation_write_attempt_receipts AS receipt
      WHERE receipt.lease_id = lease.lease_id
  );

COMMIT;
