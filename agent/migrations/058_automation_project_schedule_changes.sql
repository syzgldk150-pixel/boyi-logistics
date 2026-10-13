-- Daily schedule times belong to the host timer. Changing only those times
-- keeps the committed generation, its configuration version and scheduler
-- arguments, rewrites the project's scheduled_tasks rows in one transaction
-- and records one idempotent, audited fact per Console request.
-- No foreign key: the audit outlives an uninstalled project instance.
CREATE TABLE IF NOT EXISTS automation_project_schedule_changes (
    change_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    automation_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    request_payload_sha256 CHAR(64) NOT NULL,
    project_configuration_version BIGINT UNSIGNED NOT NULL,
    automation_generation BIGINT UNSIGNED NOT NULL,
    before_schedule_json JSON NOT NULL,
    before_schedule_sha256 CHAR(64) NOT NULL,
    after_schedule_json JSON NOT NULL,
    after_schedule_sha256 CHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    occurred_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (change_id),
    UNIQUE KEY uq_automation_project_schedule_change_request (automation_id, request_id),
    KEY idx_automation_project_schedule_change_time (automation_id, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
