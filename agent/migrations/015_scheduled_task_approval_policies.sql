-- Per-task approval policy state. Migration creates no exemption: every
-- existing schedule defaults to per-run approval until a real administrator
-- performs an explicit, audited bootstrap or policy change after deployment.

SET @cp015_schema_name = DATABASE();

SET @cp015_add_configuration_version = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE scheduled_tasks ADD COLUMN configuration_version BIGINT UNSIGNED NOT NULL DEFAULT 1',
        'SELECT 1'
    )
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @cp015_schema_name
      AND TABLE_NAME = 'scheduled_tasks'
      AND COLUMN_NAME = 'configuration_version'
);
PREPARE cp015_configuration_version_stmt FROM @cp015_add_configuration_version;
EXECUTE cp015_configuration_version_stmt;
DEALLOCATE PREPARE cp015_configuration_version_stmt;

SET @cp015_add_updated_at = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE scheduled_tasks ADD COLUMN updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)',
        'SELECT 1'
    )
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @cp015_schema_name
      AND TABLE_NAME = 'scheduled_tasks'
      AND COLUMN_NAME = 'updated_at'
);
PREPARE cp015_updated_at_stmt FROM @cp015_add_updated_at;
EXECUTE cp015_updated_at_stmt;
DEALLOCATE PREPARE cp015_updated_at_stmt;

CREATE TABLE IF NOT EXISTS scheduled_task_approval_policies (
    task_id VARCHAR(64) NOT NULL,
    mode VARCHAR(32) NOT NULL DEFAULT 'REQUIRE_EACH_RUN',
    contract_hash CHAR(64) NULL,
    contract_snapshot_json JSON NULL,
    tool_contract_hash CHAR(64) NULL,
    approved_by_actor_id VARCHAR(191) NULL,
    approved_by_actor_role VARCHAR(32) NULL,
    approved_by_actor_display_name VARCHAR(191) NULL,
    approved_at DATETIME(6) NULL,
    comment VARCHAR(1000) NULL,
    version BIGINT UNSIGNED NOT NULL DEFAULT 1,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (task_id),
    KEY idx_scheduled_task_policy_mode (mode, task_id),
    CONSTRAINT chk_scheduled_task_policy_mode CHECK (
        mode IN ('REQUIRE_EACH_RUN', 'EXACT_SCHEDULE_EXEMPT')
    ),
    CONSTRAINT chk_scheduled_task_policy_version CHECK (version > 0),
    CONSTRAINT chk_scheduled_task_policy_exact_fields CHECK (
        mode = 'REQUIRE_EACH_RUN'
        OR (
            contract_hash IS NOT NULL
            AND CHAR_LENGTH(contract_hash) = 64
            AND contract_hash REGEXP '^[0-9a-f]{64}$'
            AND contract_snapshot_json IS NOT NULL
            AND JSON_TYPE(contract_snapshot_json) = 'OBJECT'
            AND tool_contract_hash IS NOT NULL
            AND CHAR_LENGTH(tool_contract_hash) = 64
            AND tool_contract_hash REGEXP '^[0-9a-f]{64}$'
            AND approved_by_actor_id IS NOT NULL
            AND (
                approved_by_actor_role = 'super_admin'
                OR (
                    approved_by_actor_role = 'migration_authority'
                    AND approved_by_actor_id = 'system:migration:control-plane-v1'
                )
            )
            AND approved_at IS NOT NULL
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS scheduled_task_approval_policy_events (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    task_id VARCHAR(64) NOT NULL,
    from_mode VARCHAR(32) NULL,
    to_mode VARCHAR(32) NOT NULL,
    contract_hash CHAR(64) NULL,
    contract_snapshot_json JSON NULL,
    tool_contract_hash CHAR(64) NULL,
    actor_id VARCHAR(191) NOT NULL,
    actor_role VARCHAR(32) NOT NULL,
    actor_display_name VARCHAR(191) NULL,
    reason VARCHAR(128) NOT NULL,
    comment VARCHAR(1000) NULL,
    occurred_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    correlation_id CHAR(36) NOT NULL,
    request_id CHAR(36) NOT NULL,
    PRIMARY KEY (event_id),
    UNIQUE KEY uq_scheduled_task_policy_event_request (task_id, request_id),
    KEY idx_scheduled_task_policy_events_task_time (task_id, occurred_at, event_id),
    KEY idx_scheduled_task_policy_events_correlation (correlation_id),
    CONSTRAINT chk_scheduled_task_policy_event_from_mode CHECK (
        from_mode IS NULL
        OR from_mode IN ('REQUIRE_EACH_RUN', 'EXACT_SCHEDULE_EXEMPT')
    ),
    CONSTRAINT chk_scheduled_task_policy_event_to_mode CHECK (
        to_mode IN ('REQUIRE_EACH_RUN', 'EXACT_SCHEDULE_EXEMPT')
    ),
    CONSTRAINT chk_scheduled_task_policy_event_exact_fields CHECK (
        to_mode = 'REQUIRE_EACH_RUN'
        OR (
            contract_hash IS NOT NULL
            AND CHAR_LENGTH(contract_hash) = 64
            AND contract_hash REGEXP '^[0-9a-f]{64}$'
            AND contract_snapshot_json IS NOT NULL
            AND JSON_TYPE(contract_snapshot_json) = 'OBJECT'
            AND tool_contract_hash IS NOT NULL
            AND CHAR_LENGTH(tool_contract_hash) = 64
            AND tool_contract_hash REGEXP '^[0-9a-f]{64}$'
            AND (
                actor_role = 'super_admin'
                OR (
                    actor_role = 'migration_authority'
                    AND actor_id = 'system:migration:control-plane-v1'
                )
            )
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Audit events are immutable history and must outlive a scheduled-task row.
-- Drop the earlier partial-migration constraint as well, because MySQL DDL
-- commits independently and CREATE TABLE IF NOT EXISTS cannot repair it.
SET @cp015_drop_event_task_fk = (
    SELECT IF(
        COUNT(*) > 0,
        'ALTER TABLE scheduled_task_approval_policy_events DROP FOREIGN KEY fk_scheduled_task_policy_event_task',
        'SELECT 1'
    )
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = @cp015_schema_name
      AND TABLE_NAME = 'scheduled_task_approval_policy_events'
      AND CONSTRAINT_NAME = 'fk_scheduled_task_policy_event_task'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
PREPARE cp015_event_fk_stmt FROM @cp015_drop_event_task_fk;
EXECUTE cp015_event_fk_stmt;
DEALLOCATE PREPARE cp015_event_fk_stmt;

-- A prior interrupted 015 can have created this constraint inline before
-- migration history was recorded. Drop it before MODIFY: MySQL rejects a
-- charset/collation change for a column that is currently used by an FK.
-- It is re-added below after the child column is aligned to the parent.
SET @cp015_drop_policy_task_fk = (
    SELECT IF(
        COUNT(*) > 0,
        'ALTER TABLE scheduled_task_approval_policies DROP FOREIGN KEY fk_scheduled_task_policy_task',
        'SELECT 1'
    )
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = @cp015_schema_name
      AND TABLE_NAME = 'scheduled_task_approval_policies'
      AND CONSTRAINT_NAME = 'fk_scheduled_task_policy_task'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
PREPARE cp015_policy_fk_stmt FROM @cp015_drop_policy_task_fk;
EXECUTE cp015_policy_fk_stmt;
DEALLOCATE PREPARE cp015_policy_fk_stmt;

-- ``scheduled_tasks`` predates the control plane and can use a different
-- table-default collation. MySQL requires matching character set/collation
-- for VARCHAR foreign keys. Read the authoritative parent-column metadata,
-- validate it before it becomes an identifier, and align both new task IDs.
-- The event table remains deliberately without a foreign key so immutable
-- audit history survives scheduled-task deletion.
SET @cp015_parent_task_id_charset = (
    SELECT CHARACTER_SET_NAME
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @cp015_schema_name
      AND TABLE_NAME = 'scheduled_tasks'
      AND COLUMN_NAME = 'id'
    LIMIT 1
);
SET @cp015_parent_task_id_collation = (
    SELECT COLLATION_NAME
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @cp015_schema_name
      AND TABLE_NAME = 'scheduled_tasks'
      AND COLUMN_NAME = 'id'
    LIMIT 1
);
SET @cp015_parent_task_id_modify_policy_sql = IF(
    @cp015_parent_task_id_charset REGEXP '^[0-9A-Za-z_]+$'
    AND @cp015_parent_task_id_collation REGEXP '^[0-9A-Za-z_]+$',
    CONCAT(
        'ALTER TABLE scheduled_task_approval_policies MODIFY task_id VARCHAR(64) CHARACTER SET `',
        @cp015_parent_task_id_charset,
        '` COLLATE `',
        @cp015_parent_task_id_collation,
        '` NOT NULL'
    ),
    'SELECT * FROM information_schema.cp015_invalid_parent_task_id_metadata'
);
PREPARE cp015_modify_policy_task_id_stmt FROM @cp015_parent_task_id_modify_policy_sql;
EXECUTE cp015_modify_policy_task_id_stmt;
DEALLOCATE PREPARE cp015_modify_policy_task_id_stmt;

SET @cp015_parent_task_id_modify_event_sql = IF(
    @cp015_parent_task_id_charset REGEXP '^[0-9A-Za-z_]+$'
    AND @cp015_parent_task_id_collation REGEXP '^[0-9A-Za-z_]+$',
    CONCAT(
        'ALTER TABLE scheduled_task_approval_policy_events MODIFY task_id VARCHAR(64) CHARACTER SET `',
        @cp015_parent_task_id_charset,
        '` COLLATE `',
        @cp015_parent_task_id_collation,
        '` NOT NULL'
    ),
    'SELECT * FROM information_schema.cp015_invalid_parent_task_id_metadata'
);
PREPARE cp015_modify_event_task_id_stmt FROM @cp015_parent_task_id_modify_event_sql;
EXECUTE cp015_modify_event_task_id_stmt;
DEALLOCATE PREPARE cp015_modify_event_task_id_stmt;

SET @cp015_add_policy_task_fk_sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE scheduled_task_approval_policies ADD CONSTRAINT fk_scheduled_task_policy_task FOREIGN KEY (task_id) REFERENCES scheduled_tasks (id) ON DELETE CASCADE',
        'SELECT 1'
    )
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = @cp015_schema_name
      AND TABLE_NAME = 'scheduled_task_approval_policies'
      AND CONSTRAINT_NAME = 'fk_scheduled_task_policy_task'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
PREPARE cp015_add_policy_task_fk_stmt FROM @cp015_add_policy_task_fk_sql;
EXECUTE cp015_add_policy_task_fk_stmt;
DEALLOCATE PREPARE cp015_add_policy_task_fk_stmt;

INSERT INTO scheduled_task_approval_policies (task_id, mode)
SELECT task.id, 'REQUIRE_EACH_RUN'
FROM scheduled_tasks AS task
LEFT JOIN scheduled_task_approval_policies AS policy ON policy.task_id = task.id
WHERE policy.task_id IS NULL;
