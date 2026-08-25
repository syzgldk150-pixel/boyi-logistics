-- Lifecycle state for the fixed, code-owned Console module catalog. Runtime
-- code only reads/writes rows; this migration is the sole schema authority.
CREATE TABLE business_modules (
    module_code VARCHAR(64) NOT NULL,
    code_version VARCHAR(32) NOT NULL,
    installed_version VARCHAR(32) NULL,
    lifecycle_state ENUM('NOT_INSTALLED', 'DISABLED', 'ENABLED', 'BLOCKED') NOT NULL,
    record_version BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (module_code),
    CONSTRAINT chk_business_modules_version CHECK (code_version REGEXP '^[0-9]+\\.[0-9]+\\.[0-9]+$'),
    CONSTRAINT chk_business_modules_installed_version CHECK (
        (lifecycle_state = 'NOT_INSTALLED' AND installed_version IS NULL)
        OR (lifecycle_state <> 'NOT_INSTALLED' AND installed_version REGEXP '^[0-9]+\\.[0-9]+\\.[0-9]+$')
    ),
    CONSTRAINT chk_business_modules_record_version CHECK (record_version >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE business_module_events (
    event_id CHAR(36) NOT NULL,
    module_code VARCHAR(64) NOT NULL,
    request_id CHAR(36) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    action ENUM('install', 'enable', 'disable', 'upgrade', 'uninstall') NOT NULL,
    actor_id VARCHAR(191) NOT NULL,
    reason VARCHAR(500) NOT NULL,
    before_json JSON NOT NULL,
    after_json JSON NOT NULL,
    record_version BIGINT UNSIGNED NOT NULL,
    code_version VARCHAR(32) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (event_id),
    UNIQUE KEY uq_business_module_events_request (request_id),
    KEY idx_business_module_events_module_created (module_code, created_at, event_id),
    CONSTRAINT chk_business_module_events_reason CHECK (CHAR_LENGTH(TRIM(reason)) > 0),
    CONSTRAINT chk_business_module_events_version CHECK (record_version >= 1),
    CONSTRAINT fk_business_module_events_module FOREIGN KEY (module_code)
        REFERENCES business_modules (module_code) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TRIGGER business_module_events_no_update
BEFORE UPDATE ON business_module_events FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'business_module_events are immutable';

CREATE TRIGGER business_module_events_no_delete
BEFORE DELETE ON business_module_events FOR EACH ROW
SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'business_module_events are immutable';

INSERT INTO business_modules (module_code, code_version, installed_version, lifecycle_state, record_version, created_at, updated_at)
VALUES
    ('overview', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('waybill_entry', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('waybill_query', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('tracking', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('receipts', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('customer_service', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('finance', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('dispatch', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('line_haul', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('automations', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('automation_accounts', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('llm_settings', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('work_items', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    ('system_settings', '1.0.0', '1.0.0', 'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6));
