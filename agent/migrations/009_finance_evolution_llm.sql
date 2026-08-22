-- Finance evolution, durable review workflow, and centrally managed LLM settings.

SET @schema_name = DATABASE();

SET @add_admin_role = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'admin_users' AND COLUMN_NAME = 'role') = 0,
    'ALTER TABLE admin_users ADD COLUMN role VARCHAR(32) NOT NULL DEFAULT ''admin'' AFTER ui_preferences_json',
    'SELECT 1'
);
PREPARE stmt FROM @add_admin_role;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE admin_users
SET role = 'super_admin'
WHERE id = (
    SELECT selected.id FROM (
        SELECT id FROM admin_users
        WHERE is_active = 1
        ORDER BY id ASC
        LIMIT 1
    ) AS selected
)
AND NOT EXISTS (
    SELECT 1 FROM (
        SELECT id FROM admin_users WHERE role = 'super_admin' LIMIT 1
    ) AS existing_super_admin
);

CREATE TABLE IF NOT EXISTS finance_fee_subjects (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    subject_code VARCHAR(96) NOT NULL,
    subject_name VARCHAR(255) NOT NULL,
    default_fee_level VARCHAR(16) NOT NULL,
    booking_fee_name VARCHAR(255) NULL,
    requires_waybill TINYINT(1) NOT NULL DEFAULT 0,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_fee_subject (platform, subject_code),
    KEY idx_finance_fee_subject_name (platform, subject_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO finance_fee_subjects (
    platform, subject_code, subject_name, default_fee_level,
    booking_fee_name, requires_waybill, is_active, created_by, created_at, updated_at
) VALUES
    ('ronghui', 'direct_delivery_service', '直派服务费', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'warehouse_contract_fee', '包仓固定费', 'operating', NULL, 0, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'insurance_fee', '保险费', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'cod_freight_income', '到付运费收入', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'electronic_label_service', '电子标签服务费', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'fixed_transfer_fee', '固定中转费', 'operating', NULL, 0, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'sms_fee', '短信费', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'transfer_fee_adjustment', '中转费调整', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'cod_handling_fee', '到付款手续费', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'electronic_receipt_service', '电子回单服务费', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'site_fee_discount', '场地费折让', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'delivery_fee_discount', '派送费折让', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'terminal_vehicle_fee', '末端请车费', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6)),
    ('ronghui', 'delivery_fee', '派送费', 'waybill', NULL, 1, 1, 'system:user-confirmed-baseline', NOW(6), NOW(6));

-- Hex literals keep user-confirmed Chinese labels byte-exact across runners.
UPDATE finance_fee_subjects
SET subject_name = CASE subject_code
    WHEN 'direct_delivery_service' THEN CONVERT(0xe79bb4e6b4bee69c8de58aa1e8b4b9 USING utf8mb4)
    WHEN 'warehouse_contract_fee' THEN CONVERT(0xe58c85e4bb93e59bbae5ae9ae8b4b9 USING utf8mb4)
    WHEN 'insurance_fee' THEN CONVERT(0xe4bf9de999a9e8b4b9 USING utf8mb4)
    WHEN 'cod_freight_income' THEN CONVERT(0xe588b0e4bb98e8bf90e8b4b9e694b6e585a5 USING utf8mb4)
    WHEN 'electronic_label_service' THEN CONVERT(0xe794b5e5ad90e6a087e7adbee69c8de58aa1e8b4b9 USING utf8mb4)
    WHEN 'fixed_transfer_fee' THEN CONVERT(0xe59bbae5ae9ae4b8ade8bdace8b4b9 USING utf8mb4)
    WHEN 'sms_fee' THEN CONVERT(0xe79fade4bfa1e8b4b9 USING utf8mb4)
    WHEN 'transfer_fee_adjustment' THEN CONVERT(0xe4b8ade8bdace8b4b9e8b083e695b4 USING utf8mb4)
    WHEN 'cod_handling_fee' THEN CONVERT(0xe588b0e4bb98e6acbee6898be7bbade8b4b9 USING utf8mb4)
    WHEN 'electronic_receipt_service' THEN CONVERT(0xe794b5e5ad90e59b9ee58d95e69c8de58aa1e8b4b9 USING utf8mb4)
    WHEN 'site_fee_discount' THEN CONVERT(0xe59cbae59cb0e8b4b9e68a98e8aea9 USING utf8mb4)
    WHEN 'delivery_fee_discount' THEN CONVERT(0xe6b4bee98081e8b4b9e68a98e8aea9 USING utf8mb4)
    WHEN 'terminal_vehicle_fee' THEN CONVERT(0xe69cabe7abafe8afb7e8bda6e8b4b9 USING utf8mb4)
    WHEN 'delivery_fee' THEN CONVERT(0xe6b4bee98081e8b4b9 USING utf8mb4)
    ELSE subject_name
END,
updated_at = NOW(6)
WHERE platform = 'ronghui'
  AND subject_code IN (
      'direct_delivery_service', 'warehouse_contract_fee', 'insurance_fee',
      'cod_freight_income', 'electronic_label_service', 'fixed_transfer_fee',
      'sms_fee', 'transfer_fee_adjustment', 'cod_handling_fee',
      'electronic_receipt_service', 'site_fee_discount',
      'delivery_fee_discount', 'terminal_vehicle_fee', 'delivery_fee'
  );

SET @add_mapping_subject = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'finance_fee_mappings' AND COLUMN_NAME = 'canonical_subject_id') = 0,
    'ALTER TABLE finance_fee_mappings ADD COLUMN canonical_subject_id BIGINT UNSIGNED NULL AFTER fee_level',
    'SELECT 1'
);
PREPARE stmt FROM @add_mapping_subject;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_mapping_requires_waybill = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'finance_fee_mappings' AND COLUMN_NAME = 'requires_waybill') = 0,
    'ALTER TABLE finance_fee_mappings ADD COLUMN requires_waybill TINYINT(1) NOT NULL DEFAULT 0 AFTER booking_fee_name',
    'SELECT 1'
);
PREPARE stmt FROM @add_mapping_requires_waybill;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_mapping_subject_index = IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'finance_fee_mappings'
       AND INDEX_NAME = 'idx_finance_mapping_subject') = 0,
    'ALTER TABLE finance_fee_mappings ADD KEY idx_finance_mapping_subject (canonical_subject_id)',
    'SELECT 1'
);
PREPARE stmt FROM @add_mapping_subject_index;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @add_mapping_subject_fk = IF(
    (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
     WHERE CONSTRAINT_SCHEMA = @schema_name AND TABLE_NAME = 'finance_fee_mappings'
       AND CONSTRAINT_NAME = 'fk_finance_mapping_subject') = 0,
    'ALTER TABLE finance_fee_mappings ADD CONSTRAINT fk_finance_mapping_subject FOREIGN KEY (canonical_subject_id) REFERENCES finance_fee_subjects (id) ON DELETE RESTRICT',
    'SELECT 1'
);
PREPARE stmt FROM @add_mapping_subject_fk;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS finance_review_cases (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    fee_item_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'open',
    first_seen_date DATE NOT NULL,
    last_seen_date DATE NOT NULL,
    transaction_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    income DECIMAL(20,4) NOT NULL DEFAULT 0,
    expense DECIMAL(20,4) NOT NULL DEFAULT 0,
    net_change DECIMAL(20,4) NOT NULL DEFAULT 0,
    waybill_present_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    waybill_missing_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    ai_status VARCHAR(24) NOT NULL DEFAULT 'pending',
    current_ai_run_id BIGINT UNSIGNED NULL,
    notified_at DATETIME(6) NULL,
    reviewed_by VARCHAR(128) NULL,
    reviewed_at DATETIME(6) NULL,
    review_reason VARCHAR(500) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_review_fee_item (fee_item_id),
    KEY idx_finance_review_status (status, updated_at),
    CONSTRAINT fk_finance_review_fee_item FOREIGN KEY (fee_item_id)
        REFERENCES finance_fee_items (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS finance_review_ai_runs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    review_case_id BIGINT UNSIGNED NOT NULL,
    provider VARCHAR(32) NOT NULL,
    model VARCHAR(128) NOT NULL,
    status VARCHAR(24) NOT NULL,
    evidence_json JSON NOT NULL,
    suggestion_json JSON NULL,
    error_code VARCHAR(64) NULL,
    error_message VARCHAR(500) NULL,
    started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    KEY idx_finance_review_ai_case (review_case_id, id),
    CONSTRAINT fk_finance_review_ai_case FOREIGN KEY (review_case_id)
        REFERENCES finance_review_cases (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS finance_waybill_facts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    business_date DATE NOT NULL,
    waybill_no VARCHAR(191) NOT NULL,
    canonical_subject_id BIGINT UNSIGNED NOT NULL,
    mapping_id BIGINT UNSIGNED NOT NULL,
    income DECIMAL(20,4) NOT NULL DEFAULT 0,
    expense DECIMAL(20,4) NOT NULL DEFAULT 0,
    net_change DECIMAL(20,4) NOT NULL DEFAULT 0,
    transaction_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    source_run_id BIGINT UNSIGNED NOT NULL,
    mapping_version INT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_waybill_fact (
        platform, account_id, business_date, waybill_no, canonical_subject_id, mapping_id
    ),
    KEY idx_finance_waybill_fact_waybill (waybill_no, business_date),
    KEY idx_finance_waybill_fact_date (platform, account_id, business_date),
    CONSTRAINT fk_finance_waybill_fact_subject FOREIGN KEY (canonical_subject_id)
        REFERENCES finance_fee_subjects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_finance_waybill_fact_mapping FOREIGN KEY (mapping_id)
        REFERENCES finance_fee_mappings (id) ON DELETE RESTRICT,
    CONSTRAINT fk_finance_waybill_fact_run FOREIGN KEY (source_run_id)
        REFERENCES finance_sync_runs (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS finance_anomalies (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    fingerprint CHAR(64) NOT NULL,
    anomaly_type VARCHAR(48) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    business_date DATE NOT NULL,
    fee_item_id BIGINT UNSIGNED NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'open',
    occurrence_count BIGINT UNSIGNED NOT NULL DEFAULT 1,
    amount DECIMAL(20,4) NOT NULL DEFAULT 0,
    details_json JSON NOT NULL,
    notified_at DATETIME(6) NULL,
    first_seen_at DATETIME(6) NOT NULL,
    last_seen_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_anomaly_fingerprint (fingerprint),
    KEY idx_finance_anomaly_status (status, last_seen_at),
    CONSTRAINT fk_finance_anomaly_fee_item FOREIGN KEY (fee_item_id)
        REFERENCES finance_fee_items (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS finance_knowledge_exports (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    version_no BIGINT UNSIGNED NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    relative_path VARCHAR(500) NOT NULL,
    mapping_count BIGINT UNSIGNED NOT NULL,
    generated_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_knowledge_version (version_no),
    UNIQUE KEY uq_finance_knowledge_sha (content_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS llm_provider_credentials (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    provider VARCHAR(32) NOT NULL,
    credential_version INT UNSIGNED NOT NULL,
    encrypted_api_key LONGBLOB NOT NULL,
    nonce VARBINARY(32) NOT NULL,
    auth_tag VARBINARY(32) NOT NULL,
    key_version VARCHAR(32) NOT NULL,
    key_hint VARCHAR(16) NOT NULL,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_llm_provider_credential_version (provider, credential_version),
    KEY idx_llm_provider_credential_active (provider, revoked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS llm_config_versions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    provider VARCHAR(32) NOT NULL,
    model_id VARCHAR(191) NOT NULL,
    credential_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'draft',
    active_slot TINYINT GENERATED ALWAYS AS (
        CASE WHEN status = 'active' THEN 1 ELSE NULL END
    ) STORED,
    test_result_json JSON NULL,
    test_error_code VARCHAR(64) NULL,
    test_error_message VARCHAR(500) NULL,
    tested_at DATETIME(6) NULL,
    activated_at DATETIME(6) NULL,
    deactivated_at DATETIME(6) NULL,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_llm_single_active (active_slot),
    KEY idx_llm_config_status (status, activated_at),
    KEY idx_llm_config_provider (provider, id),
    CONSTRAINT fk_llm_config_credential FOREIGN KEY (credential_id)
        REFERENCES llm_provider_credentials (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS llm_model_catalog (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    provider VARCHAR(32) NOT NULL,
    model_id VARCHAR(191) NOT NULL,
    source VARCHAR(24) NOT NULL,
    discovered_at DATETIME(6) NOT NULL,
    last_seen_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_llm_model_catalog (provider, model_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS llm_config_audit_logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    action VARCHAR(32) NOT NULL,
    provider VARCHAR(32) NULL,
    config_version_id BIGINT UNSIGNED NULL,
    before_json JSON NULL,
    after_json JSON NULL,
    api_key_changed TINYINT(1) NOT NULL DEFAULT 0,
    changed_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_llm_config_audit_created (created_at),
    CONSTRAINT fk_llm_audit_config FOREIGN KEY (config_version_id)
        REFERENCES llm_config_versions (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
