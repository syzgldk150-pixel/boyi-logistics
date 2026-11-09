-- One audit row per AI task run through the shared gateway
-- (agent/agent/ai_tasks/gateway.py). The model input is stored only as a
-- SHA-256 of the redacted payload; the output is the redacted model text and,
-- when it passed validation, the validated result. Whether a person later
-- adopted, edited or rejected the advice is recorded in the outcome columns.
-- No foreign keys: the audit outlives the subject it refers to.
CREATE TABLE IF NOT EXISTS ai_task_runs (
    run_id CHAR(36) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    prompt_version VARCHAR(32) NOT NULL,
    subject_ref VARCHAR(191) NOT NULL,
    status VARCHAR(16) NOT NULL,
    provider VARCHAR(32) NULL,
    model VARCHAR(128) NULL,
    config_version_id BIGINT NULL,
    input_sha256 CHAR(64) NULL,
    output_text MEDIUMTEXT NULL,
    result_json JSON NULL,
    error_code VARCHAR(64) NULL,
    error_message VARCHAR(500) NULL,
    attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    duration_ms INT UNSIGNED NOT NULL DEFAULT 0,
    actor_id VARCHAR(128) NULL,
    outcome VARCHAR(16) NULL,
    outcome_actor_id VARCHAR(128) NULL,
    outcome_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (run_id),
    KEY idx_ai_task_runs_task_time (task_id, created_at),
    KEY idx_ai_task_runs_subject (subject_ref, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
