-- Durable control-plane state for commands, work items, runs, approvals, and evidence.

CREATE TABLE IF NOT EXISTS agent_commands (
    command_id CHAR(36) NOT NULL,
    command_type VARCHAR(64) NOT NULL,
    source VARCHAR(32) NOT NULL,
    actor_type VARCHAR(32) NOT NULL,
    actor_id VARCHAR(128) NULL,
    actor_roles_json JSON NOT NULL,
    entity_refs_json JSON NOT NULL,
    parameters_json JSON NOT NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    correlation_id CHAR(36) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'RECEIVED',
    rejection_code VARCHAR(64) NULL,
    rejection_summary VARCHAR(500) NULL,
    requested_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (command_id),
    UNIQUE KEY uq_agent_commands_source_idempotency (source, idempotency_key),
    KEY idx_agent_commands_correlation (correlation_id),
    KEY idx_agent_commands_status_requested (status, requested_at),
    KEY idx_agent_commands_type_requested (command_type, requested_at),
    CONSTRAINT chk_agent_commands_status CHECK (
        status IN ('RECEIVED', 'ACCEPTED', 'REJECTED')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS work_items (
    work_item_id CHAR(36) NOT NULL,
    command_id CHAR(36) NOT NULL,
    type VARCHAR(64) NOT NULL,
    title VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'OPEN',
    priority VARCHAR(16) NOT NULL DEFAULT 'NORMAL',
    source VARCHAR(32) NOT NULL,
    dedupe_key VARCHAR(191) NOT NULL,
    owner_type VARCHAR(32) NULL,
    owner_id VARCHAR(128) NULL,
    sla_deadline DATETIME(6) NULL,
    current_reason_code VARCHAR(64) NULL,
    current_reason_summary VARCHAR(500) NULL,
    resolution_json JSON NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    closed_at DATETIME(6) NULL,
    PRIMARY KEY (work_item_id),
    UNIQUE KEY uq_work_items_type_dedupe (type, dedupe_key),
    KEY idx_work_items_command (command_id),
    KEY idx_work_items_queue (status, priority, sla_deadline),
    KEY idx_work_items_owner_queue (owner_type, owner_id, status),
    CONSTRAINT fk_work_items_command FOREIGN KEY (command_id)
        REFERENCES agent_commands (command_id) ON DELETE RESTRICT,
    CONSTRAINT chk_work_items_status CHECK (
        status IN (
            'OPEN', 'IN_PROGRESS', 'NEEDS_CLARIFICATION', 'WAITING_APPROVAL',
            'BLOCKED_LOGIN', 'BLOCKED_DATA', 'RESOLVED', 'CANCELLED'
        )
    ),
    CONSTRAINT chk_work_items_priority CHECK (
        priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')
    ),
    CONSTRAINT chk_work_items_version CHECK (version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS work_item_entities (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    work_item_id CHAR(36) NOT NULL,
    relation_type VARCHAR(32) NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    entity_id VARCHAR(191) NOT NULL,
    source_system VARCHAR(32) NOT NULL,
    metadata_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_work_item_entity (
        work_item_id, relation_type, entity_type, source_system, entity_id
    ),
    KEY idx_work_item_entity_reverse (
        entity_type, source_system, entity_id, work_item_id
    ),
    CONSTRAINT fk_work_item_entities_item FOREIGN KEY (work_item_id)
        REFERENCES work_items (work_item_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id CHAR(36) NOT NULL,
    work_item_id CHAR(36) NOT NULL,
    command_id CHAR(36) NOT NULL,
    run_no INT UNSIGNED NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'RECEIVED',
    mode VARCHAR(32) NOT NULL,
    planner_kind VARCHAR(32) NOT NULL,
    planner_provider VARCHAR(32) NULL,
    planner_model VARCHAR(128) NULL,
    plan_schema_version SMALLINT UNSIGNED NULL,
    plan_json JSON NULL,
    plan_hash CHAR(64) NULL,
    tool_catalog_sha256 CHAR(64) NULL,
    context_fingerprint_sha256 CHAR(64) NULL,
    correlation_id CHAR(36) NOT NULL,
    causation_id CHAR(36) NULL,
    retry_of_run_id CHAR(36) NULL,
    error_code VARCHAR(64) NULL,
    error_summary VARCHAR(500) NULL,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    execution_attempt_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    next_attempt_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    worker_id VARCHAR(128) NULL,
    lease_expires_at DATETIME(6) NULL,
    cancel_requested_at DATETIME(6) NULL,
    cancel_requested_by_type VARCHAR(32) NULL,
    cancel_requested_by_id VARCHAR(128) NULL,
    cancel_reason VARCHAR(500) NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (run_id),
    UNIQUE KEY uq_agent_runs_item_number (work_item_id, run_no),
    KEY idx_agent_runs_command (command_id),
    KEY idx_agent_runs_status_updated (status, updated_at),
    KEY idx_agent_runs_claim (status, next_attempt_at, lease_expires_at, run_id),
    KEY idx_agent_runs_worker_lease (worker_id, lease_expires_at),
    KEY idx_agent_runs_cancel_requested (status, cancel_requested_at, lease_expires_at, run_id),
    KEY idx_agent_runs_correlation (correlation_id),
    KEY idx_agent_runs_plan_hash (plan_hash),
    UNIQUE KEY uq_agent_runs_retry_of (retry_of_run_id),
    CONSTRAINT fk_agent_runs_work_item FOREIGN KEY (work_item_id)
        REFERENCES work_items (work_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_runs_command FOREIGN KEY (command_id)
        REFERENCES agent_commands (command_id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_runs_retry_of FOREIGN KEY (retry_of_run_id)
        REFERENCES agent_runs (run_id) ON DELETE RESTRICT,
    CONSTRAINT chk_agent_runs_status CHECK (
        status IN (
            'RECEIVED', 'CONTEXT_READY', 'PLANNED', 'VALIDATED',
            'WAITING_APPROVAL', 'RUNNING', 'VERIFYING', 'COMPLETED',
            'NEEDS_CLARIFICATION', 'BLOCKED_LOGIN', 'BLOCKED_DATA', 'PARTIAL',
            'FAILED_RETRYABLE', 'FAILED_TERMINAL', 'CANCELLED'
        )
    ),
    CONSTRAINT chk_agent_runs_number CHECK (run_no > 0),
    CONSTRAINT chk_agent_runs_version CHECK (version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_run_steps (
    step_id CHAR(36) NOT NULL,
    run_id CHAR(36) NOT NULL,
    step_key VARCHAR(64) NOT NULL,
    step_order INT UNSIGNED NOT NULL,
    tool_name VARCHAR(64) NOT NULL,
    tool_version VARCHAR(32) NOT NULL,
    operation_type VARCHAR(32) NOT NULL,
    risk_level VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
    retry_safe BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key VARCHAR(191) NOT NULL,
    account_id VARCHAR(128) NULL,
    input_summary_json JSON NULL,
    input_sha256 CHAR(64) NULL,
    result_summary_json JSON NULL,
    result_sha256 CHAR(64) NULL,
    attempt_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    postcondition_status VARCHAR(24) NULL,
    postcondition_json JSON NULL,
    error_code VARCHAR(64) NULL,
    error_summary VARCHAR(500) NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (step_id),
    UNIQUE KEY uq_agent_run_step_key (run_id, step_key),
    UNIQUE KEY uq_agent_run_step_order (run_id, step_order),
    UNIQUE KEY uq_agent_step_tool_idempotency (tool_name, idempotency_key),
    KEY idx_agent_run_steps_run_status (run_id, status, step_order),
    KEY idx_agent_run_steps_account (account_id, run_id),
    KEY idx_agent_run_steps_status_updated (status, updated_at),
    CONSTRAINT fk_agent_run_steps_run FOREIGN KEY (run_id)
        REFERENCES agent_runs (run_id) ON DELETE RESTRICT,
    CONSTRAINT chk_agent_run_steps_status CHECK (
        status IN (
            'PENDING', 'WAITING_APPROVAL', 'RUNNING', 'VERIFYING',
            'BLOCKED_LOGIN', 'BLOCKED_DATA',
            'COMPLETED', 'SKIPPED', 'FAILED_RETRYABLE', 'FAILED_TERMINAL', 'CANCELLED'
        )
    ),
    CONSTRAINT chk_agent_run_steps_operation CHECK (
        operation_type IN (
            'READ', 'COMPUTE', 'INTERNAL_PROJECTION_WRITE', 'EXTERNAL_WRITE',
            'FINANCIAL_WRITE', 'DESTRUCTIVE'
        )
    ),
    CONSTRAINT chk_agent_run_steps_risk CHECK (
        risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'EXTREME')
    ),
    CONSTRAINT chk_agent_run_steps_order CHECK (step_order > 0),
    CONSTRAINT chk_agent_run_steps_version CHECK (version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS approval_requests (
    approval_id CHAR(36) NOT NULL,
    work_item_id CHAR(36) NOT NULL,
    run_id CHAR(36) NOT NULL,
    approval_round SMALLINT UNSIGNED NOT NULL,
    plan_hash CHAR(64) NOT NULL,
    impact_json JSON NOT NULL,
    impact_sha256 CHAR(64) NOT NULL,
    risk_level VARCHAR(16) NOT NULL,
    required_role VARCHAR(64) NOT NULL,
    required_approvals SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    requested_by_type VARCHAR(32) NOT NULL,
    requested_by_id VARCHAR(128) NULL,
    expires_at DATETIME(6) NOT NULL,
    decided_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (approval_id),
    UNIQUE KEY uq_approval_request_plan_round (run_id, plan_hash, approval_round),
    KEY idx_approval_requests_item (work_item_id, created_at),
    KEY idx_approval_requests_queue (status, expires_at),
    CONSTRAINT fk_approval_requests_item FOREIGN KEY (work_item_id)
        REFERENCES work_items (work_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_approval_requests_run FOREIGN KEY (run_id)
        REFERENCES agent_runs (run_id) ON DELETE RESTRICT,
    CONSTRAINT chk_approval_requests_status CHECK (
        status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'INVALIDATED')
    ),
    CONSTRAINT chk_approval_requests_risk CHECK (
        risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'EXTREME')
    ),
    CONSTRAINT chk_approval_requests_count CHECK (required_approvals > 0),
    CONSTRAINT chk_approval_requests_round CHECK (approval_round > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS approval_decisions (
    decision_id CHAR(36) NOT NULL,
    approval_id CHAR(36) NOT NULL,
    actor_type VARCHAR(32) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_roles_json JSON NOT NULL,
    decision VARCHAR(16) NOT NULL,
    reason VARCHAR(500) NULL,
    decided_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (decision_id),
    UNIQUE KEY uq_approval_decision_actor (approval_id, actor_id),
    KEY idx_approval_decisions_actor (actor_type, actor_id, decided_at),
    CONSTRAINT fk_approval_decisions_request FOREIGN KEY (approval_id)
        REFERENCES approval_requests (approval_id) ON DELETE RESTRICT,
    CONSTRAINT chk_approval_decisions_value CHECK (
        decision IN ('APPROVED', 'REJECTED')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS evidence_records (
    evidence_id CHAR(36) NOT NULL,
    work_item_id CHAR(36) NOT NULL,
    run_id CHAR(36) NULL,
    step_id CHAR(36) NULL,
    source_system VARCHAR(32) NOT NULL,
    account_id VARCHAR(128) NULL,
    source_record_type VARCHAR(64) NOT NULL,
    source_record_id VARCHAR(191) NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    entity_id VARCHAR(191) NOT NULL,
    occurred_at DATETIME(6) NULL,
    observed_at DATETIME(6) NOT NULL,
    completeness_status VARCHAR(16) NOT NULL,
    pagination_complete BOOLEAN NULL,
    record_count BIGINT UNSIGNED NULL,
    content_sha256 CHAR(64) NOT NULL,
    summary_json JSON NULL,
    storage_ref VARCHAR(512) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (evidence_id),
    KEY idx_evidence_work_item_observed (work_item_id, observed_at),
    KEY idx_evidence_run_step (run_id, step_id),
    KEY idx_evidence_entity (entity_type, entity_id),
    KEY idx_evidence_source_record (source_system, source_record_type, source_record_id),
    CONSTRAINT fk_evidence_work_item FOREIGN KEY (work_item_id)
        REFERENCES work_items (work_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_evidence_run FOREIGN KEY (run_id)
        REFERENCES agent_runs (run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_evidence_step FOREIGN KEY (step_id)
        REFERENCES agent_run_steps (step_id) ON DELETE RESTRICT,
    CONSTRAINT chk_evidence_completeness CHECK (
        completeness_status IN ('COMPLETE', 'INCOMPLETE', 'UNKNOWN')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS external_entity_links (
    link_id CHAR(36) NOT NULL,
    canonical_entity_type VARCHAR(64) NOT NULL,
    canonical_entity_id VARCHAR(191) NOT NULL,
    source_system VARCHAR(32) NOT NULL,
    account_scope VARCHAR(128) NOT NULL,
    external_entity_type VARCHAR(64) NOT NULL,
    external_id VARCHAR(191) NOT NULL,
    parent_external_id VARCHAR(191) NULL,
    relation_type VARCHAR(32) NOT NULL,
    verified_at DATETIME(6) NULL,
    valid_from DATETIME(6) NULL,
    valid_to DATETIME(6) NULL,
    metadata_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (link_id),
    UNIQUE KEY uq_external_entity_identity (
        source_system, account_scope, external_entity_type, external_id
    ),
    KEY idx_external_entity_canonical (canonical_entity_type, canonical_entity_id),
    KEY idx_external_entity_parent (source_system, account_scope, parent_external_id),
    CONSTRAINT chk_external_entity_validity CHECK (
        valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @schema_name = DATABASE();
SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'tool_logs' AND COLUMN_NAME = 'run_id') = 0,
    'ALTER TABLE tool_logs ADD COLUMN run_id CHAR(36) NULL, ADD KEY idx_tool_logs_run (run_id)',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'admin_users' AND COLUMN_NAME = 'control_plane_role') = 0,
    'ALTER TABLE admin_users ADD COLUMN control_plane_role ENUM(''admin'', ''super_admin'') NOT NULL DEFAULT ''admin''',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

-- Seed exactly one privileged control-plane administrator for the existing
-- all-powerful Console model.  The nested derived tables keep this safe for
-- MySQL's target-table update rule and make reruns a no-op once seeded.
UPDATE admin_users
SET control_plane_role = 'super_admin'
WHERE id = (
    SELECT seed.id
    FROM (
        SELECT id
        FROM admin_users
        WHERE is_active = 1
        ORDER BY created_at, id
        LIMIT 1
    ) AS seed
)
AND NOT EXISTS (
    SELECT 1
    FROM (
        SELECT id
        FROM admin_users
        WHERE control_plane_role = 'super_admin'
        LIMIT 1
    ) AS existing_super_admin
);

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'tool_logs' AND COLUMN_NAME = 'step_id') = 0,
    'ALTER TABLE tool_logs ADD COLUMN step_id CHAR(36) NULL, ADD KEY idx_tool_logs_step (step_id)',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'tool_logs' AND COLUMN_NAME = 'correlation_id') = 0,
    'ALTER TABLE tool_logs ADD COLUMN correlation_id CHAR(36) NULL, ADD KEY idx_tool_logs_correlation (correlation_id)',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;
