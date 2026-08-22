-- Durable, payload-free evidence for a started Broker mutation.  Runtime code
-- only writes rows; this migration is the sole schema authority.
CREATE TABLE automation_write_attempt_receipts (
    receipt_id CHAR(36) NOT NULL,
    automation_id VARCHAR(191) NOT NULL,
    generation INT NOT NULL,
    lease_id CHAR(36) NOT NULL,
    orchestration_run_id CHAR(36) NOT NULL,
    step_id CHAR(36) NOT NULL,
    request_id CHAR(36) NOT NULL,
    operation VARCHAR(64) NOT NULL,
    action VARCHAR(128) NOT NULL,
    argument_sha256 CHAR(64) NOT NULL,
    target_ref_sha256 CHAR(64) NOT NULL,
    target_ref_json JSON NOT NULL,
    outcome ENUM('STARTED','WRITE_VERIFIED','WRITE_OUTCOME_UNKNOWN','NOT_APPLIED') NOT NULL,
    evidence_sha256 CHAR(64) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (receipt_id),
    UNIQUE KEY uq_automation_write_attempt_lease_request (lease_id, request_id),
    KEY idx_automation_write_attempt_recovery (automation_id, generation, lease_id, outcome),
    CONSTRAINT fk_automation_write_attempt_lease FOREIGN KEY (lease_id)
        REFERENCES automation_project_generation_leases (lease_id) ON DELETE RESTRICT,
    CONSTRAINT fk_automation_write_attempt_run FOREIGN KEY (orchestration_run_id)
        REFERENCES agent_runs (run_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
