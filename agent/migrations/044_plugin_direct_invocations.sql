-- Calls are execution facts, never a persistent work queue.
CREATE TABLE automation_plugin_invocations (
    invocation_id CHAR(36) NOT NULL PRIMARY KEY,
    request_key_sha256 CHAR(64) NOT NULL,
    request_sha256 CHAR(64) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    automation_id VARCHAR(191) NULL,
    plugin_id VARCHAR(191) NULL,
    plugin_version VARCHAR(64) NULL,
    generation INT NULL,
    operation VARCHAR(191) NOT NULL,
    source VARCHAR(32) NOT NULL,
    actor_id VARCHAR(191) NOT NULL,
    owner_id CHAR(36) NOT NULL,
    status VARCHAR(32) NOT NULL,
    invocation_json JSON NOT NULL,
    arguments_json JSON NOT NULL,
    result_json JSON NULL,
    error_code VARCHAR(128) NULL,
    error_summary VARCHAR(1000) NULL,
    preview_invocation_id CHAR(36) NULL,
    preview_consumed_by CHAR(36) NULL,
    started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_plugin_invocation_request (request_key_sha256),
    KEY idx_plugin_invocation_project (automation_id, started_at),
    KEY idx_plugin_invocation_owner (owner_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE automation_project_generation_leases
    ADD COLUMN invocation_id CHAR(36) NULL AFTER orchestration_run_id,
    ADD KEY idx_plugin_lease_invocation (invocation_id),
    ADD CONSTRAINT fk_plugin_lease_invocation FOREIGN KEY (invocation_id)
        REFERENCES automation_plugin_invocations(invocation_id) ON DELETE RESTRICT;

ALTER TABLE automation_write_attempt_receipts
    MODIFY COLUMN orchestration_run_id CHAR(36) NULL,
    MODIFY COLUMN step_id CHAR(36) NULL,
    ADD COLUMN invocation_id CHAR(36) NULL AFTER step_id,
    ADD KEY idx_plugin_receipt_invocation (invocation_id),
    ADD CONSTRAINT fk_plugin_receipt_invocation FOREIGN KEY (invocation_id)
        REFERENCES automation_plugin_invocations(invocation_id) ON DELETE RESTRICT;

ALTER TABLE automation_scan_snapshot_heads
    ADD COLUMN owner_invocation_id CHAR(36) NULL AFTER owner_run_id;
