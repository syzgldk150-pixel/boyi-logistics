-- A closed pre-write scan snapshot survives a lost plugin response.
-- It belongs to the existing receipt and follows its existing purge lifetime.
SET @add_scan_recovery_payload = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE automation_write_attempt_receipts ADD COLUMN scan_recovery_payload_json JSON NULL',
        'SELECT 1')
    FROM information_schema.columns
    WHERE table_schema=DATABASE() AND table_name='automation_write_attempt_receipts'
      AND column_name='scan_recovery_payload_json'
);
PREPARE scan_recovery_payload FROM @add_scan_recovery_payload;
EXECUTE scan_recovery_payload;
DEALLOCATE PREPARE scan_recovery_payload;

SET @add_execution_resource_keys = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE automation_write_attempt_receipts ADD COLUMN execution_resource_keys_json JSON NULL',
        'SELECT 1')
    FROM information_schema.columns
    WHERE table_schema=DATABASE() AND table_name='automation_write_attempt_receipts'
      AND column_name='execution_resource_keys_json'
);
PREPARE execution_resource_keys FROM @add_execution_resource_keys;
EXECUTE execution_resource_keys;
DEALLOCATE PREPARE execution_resource_keys;

SET @add_unknown_resource_index = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE automation_write_attempt_receipts ADD INDEX idx_write_attempt_unknown_scope (outcome, receipt_id)',
        'SELECT 1')
    FROM information_schema.statistics
    WHERE table_schema=DATABASE() AND table_name='automation_write_attempt_receipts'
      AND index_name='idx_write_attempt_unknown_scope'
);
PREPARE unknown_resource_index FROM @add_unknown_resource_index;
EXECUTE unknown_resource_index;
DEALLOCATE PREPARE unknown_resource_index;

-- Business-date ownership is retained with scan history, independently of
-- plugin uninstall. No FK prevents retaining this small provenance record.
CREATE TABLE IF NOT EXISTS automation_scan_snapshot_heads (
    snapshot_date DATE PRIMARY KEY,
    snapshot_sha256 CHAR(64) NOT NULL,
    owner_run_id VARCHAR(191) NOT NULL,
    owner_lease_id VARCHAR(191) NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
