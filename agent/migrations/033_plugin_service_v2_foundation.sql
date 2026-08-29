-- Plugin Service v2 persistence foundation.
-- Existing signed action rows remain byte-for-byte authoritative and are
-- classified as ACTION_V1 / 1.0.0 by the new column defaults.

-- Generation ownership now records service readiness and reversible service
-- registration explicitly, so restart/uninstall cannot leave memory-only
-- providers or orphaned routes.
SET @cp033_has_coeffect_kind_check = (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_generation_coeffects'
      AND CONSTRAINT_NAME='chk_automation_generation_coeffect_kind'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp033_drop_coeffect_kind_check_sql = IF(
    @cp033_has_coeffect_kind_check > 0,
    'ALTER TABLE automation_project_generation_coeffects DROP CHECK chk_automation_generation_coeffect_kind',
    'SELECT 1'
);
PREPARE cp033_drop_coeffect_kind_check_stmt
    FROM @cp033_drop_coeffect_kind_check_sql;
EXECUTE cp033_drop_coeffect_kind_check_stmt;
DEALLOCATE PREPARE cp033_drop_coeffect_kind_check_stmt;
ALTER TABLE automation_project_generation_coeffects
    ADD CONSTRAINT chk_automation_generation_coeffect_kind CHECK (
        coeffect_kind IN (
            'ACCOUNT', 'SESSION', 'RESOURCE', 'DEVICE', 'CORE_ADAPTER',
            'SERVICE'
        )
    );

SET @cp033_has_effect_kind_check = (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_generation_effects'
      AND CONSTRAINT_NAME='chk_automation_generation_effect_kind'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp033_drop_effect_kind_check_sql = IF(
    @cp033_has_effect_kind_check > 0,
    'ALTER TABLE automation_project_generation_effects DROP CHECK chk_automation_generation_effect_kind',
    'SELECT 1'
);
PREPARE cp033_drop_effect_kind_check_stmt
    FROM @cp033_drop_effect_kind_check_sql;
EXECUTE cp033_drop_effect_kind_check_stmt;
DEALLOCATE PREPARE cp033_drop_effect_kind_check_stmt;
ALTER TABLE automation_project_generation_effects
    ADD CONSTRAINT chk_automation_generation_effect_kind CHECK (
        effect_kind IN (
            'PACKAGE_REFERENCE', 'VENV_REFERENCE', 'INSTANCE_RUNTIME',
            'SCHEDULE_BINDING', 'WEBHOOK_BINDING', 'BROKER_SCOPE',
            'WORKER_DEPLOYMENT', 'ENTRYPOINT_ROUTE',
            'SERVICE_REGISTRATION', 'CONTRIBUTION_REGISTRATION'
        )
    );

SET @cp033_has_version_runtime_model = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_plugin_versions'
      AND COLUMN_NAME='runtime_model'
);
SET @cp033_add_version_runtime_model_sql = IF(
    @cp033_has_version_runtime_model = 0,
    'ALTER TABLE automation_plugin_versions ADD COLUMN runtime_model VARCHAR(16) NOT NULL DEFAULT ''ACTION_V1'' AFTER version',
    'SELECT 1'
);
PREPARE cp033_add_version_runtime_model_stmt
    FROM @cp033_add_version_runtime_model_sql;
EXECUTE cp033_add_version_runtime_model_stmt;
DEALLOCATE PREPARE cp033_add_version_runtime_model_stmt;

SET @cp033_has_version_plugin_api = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_plugin_versions'
      AND COLUMN_NAME='plugin_api'
);
SET @cp033_add_version_plugin_api_sql = IF(
    @cp033_has_version_plugin_api = 0,
    'ALTER TABLE automation_plugin_versions ADD COLUMN plugin_api VARCHAR(32) NOT NULL DEFAULT ''1.0.0'' AFTER runtime_model',
    'SELECT 1'
);
PREPARE cp033_add_version_plugin_api_stmt
    FROM @cp033_add_version_plugin_api_sql;
EXECUTE cp033_add_version_plugin_api_stmt;
DEALLOCATE PREPARE cp033_add_version_plugin_api_stmt;

SET @cp033_has_generation_runtime_model = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_generations'
      AND COLUMN_NAME='runtime_model'
);
SET @cp033_add_generation_runtime_model_sql = IF(
    @cp033_has_generation_runtime_model = 0,
    'ALTER TABLE automation_project_generations ADD COLUMN runtime_model VARCHAR(16) NOT NULL DEFAULT ''ACTION_V1'' AFTER plugin_version',
    'SELECT 1'
);
PREPARE cp033_add_generation_runtime_model_stmt
    FROM @cp033_add_generation_runtime_model_sql;
EXECUTE cp033_add_generation_runtime_model_stmt;
DEALLOCATE PREPARE cp033_add_generation_runtime_model_stmt;

SET @cp033_has_generation_plugin_api = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_generations'
      AND COLUMN_NAME='plugin_api'
);
SET @cp033_add_generation_plugin_api_sql = IF(
    @cp033_has_generation_plugin_api = 0,
    'ALTER TABLE automation_project_generations ADD COLUMN plugin_api VARCHAR(32) NOT NULL DEFAULT ''1.0.0'' AFTER runtime_model',
    'SELECT 1'
);
PREPARE cp033_add_generation_plugin_api_stmt
    FROM @cp033_add_generation_plugin_api_sql;
EXECUTE cp033_add_generation_plugin_api_stmt;
DEALLOCATE PREPARE cp033_add_generation_plugin_api_stmt;

SET @cp033_has_version_runtime_model_check = (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_plugin_versions'
      AND CONSTRAINT_NAME='chk_automation_plugin_runtime_model'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp033_add_version_runtime_model_check_sql = IF(
    @cp033_has_version_runtime_model_check = 0,
    'ALTER TABLE automation_plugin_versions ADD CONSTRAINT chk_automation_plugin_runtime_model CHECK (BINARY runtime_model IN (BINARY ''ACTION_V1'', BINARY ''SERVICE_V2''))',
    'SELECT 1'
);
PREPARE cp033_add_version_runtime_model_check_stmt
    FROM @cp033_add_version_runtime_model_check_sql;
EXECUTE cp033_add_version_runtime_model_check_stmt;
DEALLOCATE PREPARE cp033_add_version_runtime_model_check_stmt;

SET @cp033_has_version_plugin_api_check = (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_plugin_versions'
      AND CONSTRAINT_NAME='chk_automation_plugin_api'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp033_add_version_plugin_api_check_sql = IF(
    @cp033_has_version_plugin_api_check = 0,
    'ALTER TABLE automation_plugin_versions ADD CONSTRAINT chk_automation_plugin_api CHECK (CHAR_LENGTH(plugin_api) BETWEEN 5 AND 32 AND BINARY plugin_api=BINARY TRIM(plugin_api) AND plugin_api NOT LIKE ''% %'')',
    'SELECT 1'
);
PREPARE cp033_add_version_plugin_api_check_stmt
    FROM @cp033_add_version_plugin_api_check_sql;
EXECUTE cp033_add_version_plugin_api_check_stmt;
DEALLOCATE PREPARE cp033_add_version_plugin_api_check_stmt;

SET @cp033_has_generation_runtime_model_check = (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_generations'
      AND CONSTRAINT_NAME='chk_automation_generation_runtime_model'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp033_add_generation_runtime_model_check_sql = IF(
    @cp033_has_generation_runtime_model_check = 0,
    'ALTER TABLE automation_project_generations ADD CONSTRAINT chk_automation_generation_runtime_model CHECK (BINARY runtime_model IN (BINARY ''ACTION_V1'', BINARY ''SERVICE_V2''))',
    'SELECT 1'
);
PREPARE cp033_add_generation_runtime_model_check_stmt
    FROM @cp033_add_generation_runtime_model_check_sql;
EXECUTE cp033_add_generation_runtime_model_check_stmt;
DEALLOCATE PREPARE cp033_add_generation_runtime_model_check_stmt;

SET @cp033_has_generation_plugin_api_check = (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_generations'
      AND CONSTRAINT_NAME='chk_automation_generation_plugin_api'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp033_add_generation_plugin_api_check_sql = IF(
    @cp033_has_generation_plugin_api_check = 0,
    'ALTER TABLE automation_project_generations ADD CONSTRAINT chk_automation_generation_plugin_api CHECK (CHAR_LENGTH(plugin_api) BETWEEN 5 AND 32 AND BINARY plugin_api=BINARY TRIM(plugin_api) AND plugin_api NOT LIKE ''% %'')',
    'SELECT 1'
);
PREPARE cp033_add_generation_plugin_api_check_stmt
    FROM @cp033_add_generation_plugin_api_check_sql;
EXECUTE cp033_add_generation_plugin_api_check_stmt;
DEALLOCATE PREPARE cp033_add_generation_plugin_api_check_stmt;

-- Replace the old trust checks without rewriting historical trust_source.
SET @cp033_has_version_trust_check = (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_plugin_versions'
      AND CONSTRAINT_NAME='chk_automation_plugin_trust_source'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp033_drop_version_trust_check_sql = IF(
    @cp033_has_version_trust_check > 0,
    'ALTER TABLE automation_plugin_versions DROP CHECK chk_automation_plugin_trust_source',
    'SELECT 1'
);
PREPARE cp033_drop_version_trust_check_stmt
    FROM @cp033_drop_version_trust_check_sql;
EXECUTE cp033_drop_version_trust_check_stmt;
DEALLOCATE PREPARE cp033_drop_version_trust_check_stmt;
ALTER TABLE automation_plugin_versions
    ADD CONSTRAINT chk_automation_plugin_trust_source CHECK (
        BINARY trust_source IN (
            BINARY 'ed25519_upload', BINARY 'ed25519_first_party',
            BINARY 'builtin_release', BINARY 'super_admin_upload',
            BINARY 'builtin_bundle'
        )
    );

SET @cp033_has_generation_trust_check = (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_generations'
      AND CONSTRAINT_NAME='chk_automation_generation_trust'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp033_drop_generation_trust_check_sql = IF(
    @cp033_has_generation_trust_check > 0,
    'ALTER TABLE automation_project_generations DROP CHECK chk_automation_generation_trust',
    'SELECT 1'
);
PREPARE cp033_drop_generation_trust_check_stmt
    FROM @cp033_drop_generation_trust_check_sql;
EXECUTE cp033_drop_generation_trust_check_stmt;
DEALLOCATE PREPARE cp033_drop_generation_trust_check_stmt;
ALTER TABLE automation_project_generations
    ADD CONSTRAINT chk_automation_generation_trust CHECK (
        BINARY trust_source IN (
            BINARY 'ed25519_upload', BINARY 'ed25519_first_party',
            BINARY 'builtin_release', BINARY 'super_admin_upload',
            BINARY 'builtin_bundle'
        )
    );

CREATE TABLE IF NOT EXISTS automation_plugin_migration_pairs (
    migration_pair_id CHAR(36) NOT NULL,
    source_automation_id VARCHAR(128) NOT NULL,
    target_automation_id VARCHAR(128) NOT NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'PREPARING',
    entrypoint_snapshot_json JSON NOT NULL,
    entrypoint_snapshot_sha256 CHAR(64) NOT NULL,
    create_request_id VARCHAR(191) NOT NULL,
    created_by_actor_id VARCHAR(128) NOT NULL,
    created_by_actor_role VARCHAR(64) NOT NULL,
    last_transition_request_id VARCHAR(191) NOT NULL,
    last_transition_actor_id VARCHAR(128) NOT NULL,
    last_transition_actor_role VARCHAR(64) NOT NULL,
    last_transition_reason VARCHAR(500) NOT NULL,
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    cutover_at DATETIME(6) NULL,
    rolled_back_at DATETIME(6) NULL,
    completed_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (migration_pair_id),
    UNIQUE KEY uq_automation_plugin_migration_pair (
        source_automation_id, target_automation_id
    ),
    UNIQUE KEY uq_automation_plugin_migration_create_request (create_request_id),
    KEY idx_automation_plugin_migration_state (state, updated_at),
    KEY idx_automation_plugin_migration_target_state (
        target_automation_id, state, updated_at
    ),
    -- Project identifiers intentionally remain as immutable audit identities
    -- after a completed migration allows either project to be uninstalled.
    CONSTRAINT chk_automation_plugin_migration_distinct CHECK (
        BINARY source_automation_id <> BINARY target_automation_id
    ),
    CONSTRAINT chk_automation_plugin_migration_state CHECK (
        BINARY state IN (
            BINARY 'PREPARING', BINARY 'TESTING', BINARY 'READY', BINARY 'CUTTING_OVER',
            BINARY 'CUTOVER', BINARY 'ROLLING_BACK', BINARY 'ROLLED_BACK',
            BINARY 'COMPLETED', BINARY 'ERROR'
        )
    ),
    CONSTRAINT chk_automation_plugin_migration_version CHECK (record_version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_plugin_migration_pair_events (
    event_id CHAR(36) NOT NULL,
    migration_pair_id CHAR(36) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    from_state VARCHAR(24) NULL,
    to_state VARCHAR(24) NOT NULL,
    from_record_version INT UNSIGNED NOT NULL,
    to_record_version INT UNSIGNED NOT NULL,
    entrypoint_snapshot_sha256 CHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    reason VARCHAR(500) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    UNIQUE KEY uq_automation_plugin_migration_event_request (
        migration_pair_id, request_id
    ),
    KEY idx_automation_plugin_migration_event_time (
        migration_pair_id, created_at
    ),
    CONSTRAINT fk_automation_plugin_migration_event_pair FOREIGN KEY (
        migration_pair_id
    ) REFERENCES automation_plugin_migration_pairs (migration_pair_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_automation_plugin_migration_event_states CHECK (
        (from_state IS NULL OR BINARY from_state IN (
            BINARY 'PREPARING', BINARY 'TESTING', BINARY 'READY', BINARY 'CUTTING_OVER',
            BINARY 'CUTOVER', BINARY 'ROLLING_BACK', BINARY 'ROLLED_BACK',
            BINARY 'COMPLETED', BINARY 'ERROR'
        ))
        AND BINARY to_state IN (
            BINARY 'PREPARING', BINARY 'TESTING', BINARY 'READY', BINARY 'CUTTING_OVER',
            BINARY 'CUTOVER', BINARY 'ROLLING_BACK', BINARY 'ROLLED_BACK',
            BINARY 'COMPLETED', BINARY 'ERROR'
        )
    ),
    CONSTRAINT chk_automation_plugin_migration_event_versions CHECK (
        from_record_version < to_record_version AND to_record_version > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_plugin_migration_run_locks (
    migration_pair_id CHAR(36) NOT NULL,
    business_run_key VARCHAR(191) CHARACTER SET utf8mb4
        COLLATE utf8mb4_bin NOT NULL,
    lease_id CHAR(36) NOT NULL,
    owner_automation_id VARCHAR(128) NOT NULL,
    orchestration_run_id CHAR(36) NULL,
    target_generation BIGINT UNSIGNED NULL,
    contribution_id VARCHAR(128) NULL,
    contribution_kind VARCHAR(32) NULL,
    dry_run TINYINT(1) NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'ACTIVE',
    acquire_request_id VARCHAR(191) NOT NULL,
    acquired_by_actor_id VARCHAR(128) NOT NULL,
    acquired_by_actor_role VARCHAR(64) NOT NULL,
    acquired_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    terminal_request_id VARCHAR(191) NULL,
    terminal_actor_id VARCHAR(128) NULL,
    terminal_actor_role VARCHAR(64) NULL,
    terminal_outcome_code VARCHAR(64) NULL,
    terminal_at DATETIME(6) NULL,
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (migration_pair_id, business_run_key),
    UNIQUE KEY uq_automation_plugin_migration_lock_lease (lease_id),
    UNIQUE KEY uq_automation_plugin_migration_lock_request (
        migration_pair_id, acquire_request_id
    ),
    KEY idx_automation_plugin_migration_lock_expiry (state, expires_at),
    KEY idx_automation_plugin_migration_lock_owner (
        owner_automation_id, state
    ),
    KEY idx_automation_plugin_migration_lock_execution (
        migration_pair_id, owner_automation_id, orchestration_run_id, state
    ),
    CONSTRAINT fk_automation_plugin_migration_lock_pair FOREIGN KEY (
        migration_pair_id
    ) REFERENCES automation_plugin_migration_pairs (migration_pair_id)
        ON DELETE RESTRICT,
    -- Owner/run identifiers intentionally outlive projects and Agent run rows
    -- as migration audit evidence; create/claim paths validate live ownership.
    CONSTRAINT chk_automation_plugin_migration_lock_state CHECK (
        BINARY state IN (
            BINARY 'ACTIVE', BINARY 'SUCCEEDED', BINARY 'FAILED',
            BINARY 'CANCELLED', BINARY 'EXPIRED', BINARY 'OUTCOME_UNKNOWN'
        )
    ),
    CONSTRAINT chk_automation_plugin_migration_lock_ttl CHECK (
        expires_at > acquired_at
    ),
    CONSTRAINT chk_automation_plugin_migration_lock_terminal CHECK (
        (
            BINARY state = BINARY 'ACTIVE'
            AND terminal_request_id IS NULL
            AND terminal_actor_id IS NULL
            AND terminal_actor_role IS NULL
            AND terminal_outcome_code IS NULL
            AND terminal_at IS NULL
        ) OR (
            BINARY state <> BINARY 'ACTIVE'
            AND terminal_request_id IS NOT NULL
            AND terminal_actor_id IS NOT NULL
            AND terminal_actor_role IS NOT NULL
            AND terminal_at IS NOT NULL
        )
    ),
    CONSTRAINT chk_automation_plugin_migration_lock_version CHECK (
        record_version > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_plugin_documents (
    automation_id VARCHAR(128) NOT NULL,
    collection_name VARCHAR(64) CHARACTER SET utf8mb4
        COLLATE utf8mb4_bin NOT NULL,
    document_key VARCHAR(191) CHARACTER SET utf8mb4
        COLLATE utf8mb4_bin NOT NULL,
    document_json JSON NULL,
    document_sha256 CHAR(64) NULL,
    document_version BIGINT UNSIGNED NOT NULL DEFAULT 1,
    retention_state VARCHAR(24) NOT NULL DEFAULT 'ACTIVE',
    retention_until DATETIME(6) NULL,
    clear_requested_at DATETIME(6) NULL,
    cleared_at DATETIME(6) NULL,
    clear_reason VARCHAR(500) NULL,
    last_request_id VARCHAR(191) NOT NULL,
    updated_by_actor_id VARCHAR(128) NOT NULL,
    updated_by_actor_role VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (automation_id, collection_name, document_key),
    KEY idx_automation_plugin_document_retention (
        retention_state, retention_until, automation_id
    ),
    CONSTRAINT chk_automation_plugin_document_version CHECK (
        document_version > 0
    ),
    CONSTRAINT chk_automation_plugin_document_retention CHECK (
        BINARY retention_state IN (
            BINARY 'ACTIVE', BINARY 'RETAINED',
            BINARY 'CLEAR_PENDING', BINARY 'CLEARED'
        )
    ),
    CONSTRAINT chk_automation_plugin_document_content CHECK (
        (
            BINARY retention_state = BINARY 'CLEARED'
            AND document_json IS NULL
            AND document_sha256 IS NULL
            AND cleared_at IS NOT NULL
        ) OR (
            BINARY retention_state <> BINARY 'CLEARED'
            AND document_json IS NOT NULL
            AND document_sha256 IS NOT NULL
            AND cleared_at IS NULL
        )
    ),
    CONSTRAINT chk_automation_plugin_document_clear_request CHECK (
        (
            BINARY retention_state IN (BINARY 'CLEAR_PENDING', BINARY 'CLEARED')
            AND clear_requested_at IS NOT NULL
            AND clear_reason IS NOT NULL
        ) OR (
            BINARY retention_state IN (BINARY 'ACTIVE', BINARY 'RETAINED')
            AND clear_requested_at IS NULL
            AND clear_reason IS NULL
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Plugins never create SQL indexes themselves.  The Host projects every
-- declared equality index/unique key into canonical JSON SHA-256 values.
-- No indexed business value is duplicated into this metadata table.
CREATE TABLE IF NOT EXISTS automation_plugin_document_indexes (
    automation_id VARCHAR(128) NOT NULL,
    collection_name VARCHAR(64) CHARACTER SET utf8mb4
        COLLATE utf8mb4_bin NOT NULL,
    index_kind VARCHAR(16) NOT NULL,
    index_name VARCHAR(64) CHARACTER SET utf8mb4
        COLLATE utf8mb4_bin NOT NULL,
    value_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    -- NULL for ordinary indexes.  For UNIQUE rows it duplicates only the
    -- digest so MySQL can enforce uniqueness without retaining cleartext.
    unique_value_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    document_key VARCHAR(191) CHARACTER SET utf8mb4
        COLLATE utf8mb4_bin NOT NULL,
    document_version BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (
        automation_id, collection_name, index_kind, index_name, document_key
    ),
    KEY idx_automation_plugin_document_index_lookup (
        automation_id, collection_name, index_kind, index_name,
        value_sha256, document_key
    ),
    UNIQUE KEY uq_automation_plugin_document_unique_value (
        automation_id, collection_name, index_kind, index_name,
        unique_value_sha256
    ),
    CONSTRAINT chk_automation_plugin_document_index_kind CHECK (
        BINARY index_kind IN (BINARY 'INDEX', BINARY 'UNIQUE')
    ),
    CONSTRAINT chk_automation_plugin_document_index_projection CHECK (
        (
            BINARY index_kind = BINARY 'INDEX'
            AND unique_value_sha256 IS NULL
        ) OR (
            BINARY index_kind = BINARY 'UNIQUE'
            AND unique_value_sha256 = value_sha256
        )
    ),
    CONSTRAINT chk_automation_plugin_document_index_version CHECK (
        document_version > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
