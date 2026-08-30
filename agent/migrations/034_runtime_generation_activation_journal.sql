-- Durable before-image for the database-commit -> process-projection boundary.
-- A generation route is not ACTIVE until the strict process projection acks
-- the token written by the same transaction that committed the database route.

CREATE TABLE IF NOT EXISTS automation_project_generation_transitions (
    automation_id VARCHAR(128) NOT NULL,
    generation BIGINT UNSIGNED NOT NULL,
    transition_token CHAR(36) NOT NULL,
    base_committed_generation BIGINT UNSIGNED NULL,
    phase VARCHAR(32) NOT NULL DEFAULT 'PENDING_PROJECTION',
    before_project_plugin_version VARCHAR(64) NOT NULL,
    pending_project_plugin_version VARCHAR(64) NOT NULL,
    rollback_project_plugin_version VARCHAR(64) NOT NULL,
    before_project_enabled BOOLEAN NOT NULL,
    before_project_state VARCHAR(24) NOT NULL,
    before_project_reconcile_state VARCHAR(32) NOT NULL,
    pending_project_state VARCHAR(24) NOT NULL,
    pending_project_reconcile_state VARCHAR(32) NOT NULL,
    before_project_record_version INT UNSIGNED NOT NULL,
    pending_project_record_version INT UNSIGNED NOT NULL,
    rolled_back_project_record_version INT UNSIGNED NULL,
    before_policy_generation BIGINT UNSIGNED NOT NULL,
    before_policy_configuration_version BIGINT UNSIGNED NOT NULL,
    before_policy_version BIGINT UNSIGNED NOT NULL,
    pending_policy_generation BIGINT UNSIGNED NOT NULL,
    pending_policy_configuration_version BIGINT UNSIGNED NOT NULL,
    pending_policy_version BIGINT UNSIGNED NOT NULL,
    before_tasks_sha256 CHAR(64) NOT NULL,
    pending_tasks_sha256 CHAR(64) NULL,
    activated_at DATETIME(6) NULL,
    rolled_back_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (automation_id, generation),
    UNIQUE KEY uq_automation_generation_transition_token (transition_token),
    KEY idx_automation_generation_transition_phase (
        phase, updated_at, automation_id
    ),
    CONSTRAINT fk_automation_generation_transition_generation FOREIGN KEY (
        automation_id, generation
    ) REFERENCES automation_project_generations (
        automation_id, generation
    ) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_generation_transition_phase CHECK (
        phase IN ('PENDING_PROJECTION', 'ACTIVE', 'ROLLED_BACK', 'BLOCKED')
    ),
    CONSTRAINT chk_automation_generation_transition_numbers CHECK (
        generation > 0
        AND (
            base_committed_generation IS NULL
            OR base_committed_generation > 0
        )
        AND before_project_record_version > 0
        AND pending_project_record_version > 0
        AND (
            rolled_back_project_record_version IS NULL
            OR rolled_back_project_record_version > 0
        )
        AND before_policy_generation > 0
        AND before_policy_configuration_version > 0
        AND before_policy_version > 0
        AND pending_policy_generation > 0
        AND pending_policy_configuration_version > 0
        AND pending_policy_version > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_generation_transition_tasks (
    transition_token CHAR(36) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    automation_generation BIGINT UNSIGNED NOT NULL,
    name VARCHAR(128) NOT NULL,
    tool_name VARCHAR(64) NOT NULL,
    tool_params JSON NULL,
    cron_expression VARCHAR(64) NOT NULL,
    enabled BOOLEAN NOT NULL,
    last_run DATETIME(6) NULL,
    last_status VARCHAR(16) NULL,
    last_duration_ms INT NULL,
    last_message TEXT NULL,
    configuration_version BIGINT UNSIGNED NOT NULL,
    task_created_at DATETIME(6) NOT NULL,
    task_updated_at DATETIME(6) NOT NULL,
    policy_mode VARCHAR(32) NOT NULL,
    policy_contract_hash CHAR(64) NULL,
    policy_contract_snapshot_json JSON NULL,
    policy_tool_contract_hash CHAR(64) NULL,
    policy_approved_by_actor_id VARCHAR(191) NULL,
    policy_approved_by_actor_role VARCHAR(32) NULL,
    policy_approved_by_actor_display_name VARCHAR(191) NULL,
    policy_approved_at DATETIME(6) NULL,
    policy_comment VARCHAR(1000) NULL,
    policy_version BIGINT UNSIGNED NOT NULL,
    policy_updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (transition_token, task_id),
    CONSTRAINT fk_automation_generation_transition_task FOREIGN KEY (
        transition_token
    ) REFERENCES automation_project_generation_transitions (
        transition_token
    ) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT chk_automation_generation_transition_task_numbers CHECK (
        automation_generation > 0
        AND configuration_version > 0
        AND policy_version > 0
    ),
    CONSTRAINT chk_automation_generation_transition_task_policy CHECK (
        policy_mode IN ('REQUIRE_EACH_RUN', 'EXACT_SCHEDULE_EXEMPT')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
