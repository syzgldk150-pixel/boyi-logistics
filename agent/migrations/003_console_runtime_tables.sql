CREATE TABLE IF NOT EXISTS documents (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    doc_token VARCHAR(64) NOT NULL UNIQUE,
    original_name VARCHAR(255) NOT NULL,
    source_relpath VARCHAR(1024) NOT NULL,
    template_name VARCHAR(128) NOT NULL,
    status VARCHAR(64) NOT NULL,
    original_path VARCHAR(1024) NOT NULL,
    processed_path VARCHAR(1024) NOT NULL,
    artifacts_dir VARCHAR(1024) NOT NULL,
    fields_json LONGTEXT NOT NULL,
    raw_ocr_json LONGTEXT NOT NULL,
    notes LONGTEXT NOT NULL,
    error_message LONGTEXT NOT NULL,
    writer_id VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    reviewed_at DATETIME NULL,
    INDEX idx_documents_status (status),
    INDEX idx_documents_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS training_samples (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    document_id BIGINT NOT NULL,
    field_name VARCHAR(128) NOT NULL,
    template_name VARCHAR(128) NOT NULL,
    writer_id VARCHAR(64) NOT NULL DEFAULT '',
    ocr_value TEXT NOT NULL,
    correct_value TEXT NOT NULL,
    is_correction TINYINT NOT NULL,
    confidence_original FLOAT NOT NULL,
    crop_image_path VARCHAR(1024) NOT NULL,
    source_image_path VARCHAR(1024) NOT NULL,
    bbox_json TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    INDEX idx_ts_field (field_name),
    INDEX idx_ts_writer (writer_id),
    INDEX idx_ts_correction (is_correction),
    INDEX idx_ts_document (document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS model_versions (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    version_tag VARCHAR(128) NOT NULL UNIQUE,
    base_model VARCHAR(256) NOT NULL,
    training_sample_count INT NOT NULL,
    field_scope TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    model_path VARCHAR(1024) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    activated_at DATETIME NULL,
    INDEX idx_mv_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS accuracy_log (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    document_id BIGINT NOT NULL,
    field_name VARCHAR(128) NOT NULL,
    writer_id VARCHAR(64) NOT NULL DEFAULT '',
    ocr_provider VARCHAR(64) NOT NULL,
    model_version VARCHAR(128) NOT NULL DEFAULT '',
    ocr_value TEXT NOT NULL,
    final_value TEXT NOT NULL,
    is_correct TINYINT NOT NULL,
    confidence FLOAT NOT NULL,
    created_at DATETIME NOT NULL,
    INDEX idx_al_field (field_name),
    INDEX idx_al_provider (ocr_provider)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS writers (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    writer_id VARCHAR(64) NOT NULL UNIQUE,
    display_name VARCHAR(128) NOT NULL DEFAULT '',
    sample_count INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS waybill_sequences (
    sequence_key VARCHAR(64) PRIMARY KEY,
    current_value BIGINT NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS waybill_provider_snapshots (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    waybill_id BIGINT NULL,
    provider VARCHAR(32) NOT NULL,
    remote_waybill_no VARCHAR(128) NOT NULL DEFAULT '',
    snapshot_kind VARCHAR(32) NOT NULL,
    payload_json LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL,
    INDEX idx_wps_provider_remote (provider, remote_waybill_no),
    INDEX idx_wps_waybill_id (waybill_id),
    INDEX idx_wps_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS receipt_records (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    direction VARCHAR(32) NOT NULL,
    waybill_no VARCHAR(128) NOT NULL DEFAULT '',
    receipt_no VARCHAR(128) NOT NULL DEFAULT '',
    return_waybill_no VARCHAR(128) NOT NULL DEFAULT '',
    receipt_status VARCHAR(128) NOT NULL DEFAULT '',
    audit_status VARCHAR(128) NOT NULL DEFAULT '',
    photo_status VARCHAR(64) NOT NULL DEFAULT '',
    photo_count INT NOT NULL DEFAULT 0,
    signed_confirmed VARCHAR(64) NOT NULL DEFAULT '',
    remote_updated_at VARCHAR(64) NOT NULL DEFAULT '',
    raw_payload_json LONGTEXT NOT NULL,
    synced_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uniq_receipt_record (platform, direction, waybill_no, receipt_no),
    INDEX idx_receipt_platform_direction (platform, direction),
    INDEX idx_receipt_waybill_no (waybill_no),
    INDEX idx_receipt_return_waybill_no (return_waybill_no),
    INDEX idx_receipt_status (receipt_status),
    INDEX idx_receipt_audit_status (audit_status),
    INDEX idx_receipt_photo_count (photo_count),
    INDEX idx_receipt_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS receipt_attachments (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    record_id BIGINT NOT NULL,
    attachment_type VARCHAR(64) NOT NULL DEFAULT '',
    display_name VARCHAR(255) NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    local_path VARCHAR(1024) NOT NULL DEFAULT '',
    file_hash VARCHAR(128) NULL,
    mime_type VARCHAR(128) NOT NULL DEFAULT '',
    file_size BIGINT NOT NULL DEFAULT 0,
    uploaded_at VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    INDEX idx_receipt_attachment_record (record_id),
    INDEX idx_receipt_attachment_hash (file_hash),
    INDEX idx_receipt_attachment_type (attachment_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS receipt_audit_logs (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    receipt_id BIGINT NULL,
    platform VARCHAR(32) NOT NULL DEFAULT '',
    direction VARCHAR(32) NOT NULL DEFAULT '',
    action VARCHAR(64) NOT NULL DEFAULT '',
    result_status VARCHAR(64) NOT NULL DEFAULT '',
    operator VARCHAR(128) NOT NULL DEFAULT '',
    request_summary_json LONGTEXT NOT NULL,
    response_status VARCHAR(64) NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    INDEX idx_receipt_audit_receipt_id (receipt_id),
    INDEX idx_receipt_audit_platform_direction (platform, direction),
    INDEX idx_receipt_audit_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_users (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    username VARCHAR(128) NOT NULL UNIQUE,
    display_name VARCHAR(128) NOT NULL DEFAULT '',
    avatar_path VARCHAR(1024) NOT NULL DEFAULT '',
    password_hash VARCHAR(512) NOT NULL,
    is_active TINYINT NOT NULL DEFAULT 1,
    last_login_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    INDEX idx_admin_users_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_sessions (
    session_id VARCHAR(128) PRIMARY KEY,
    user_id BIGINT NOT NULL,
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    INDEX idx_admin_sessions_user (user_id),
    INDEX idx_admin_sessions_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS line_haul_contacts (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    company_name VARCHAR(128) NOT NULL DEFAULT '',
    service_area VARCHAR(128) NOT NULL DEFAULT '',
    address TEXT NOT NULL,
    contact_name VARCHAR(128) NOT NULL DEFAULT '',
    phone_numbers VARCHAR(512) NOT NULL DEFAULT '',
    remark TEXT NOT NULL,
    source_text TEXT NOT NULL,
    is_active TINYINT NOT NULL DEFAULT 1,
    sort_order INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    INDEX idx_lhc_company (company_name),
    INDEX idx_lhc_service_area (service_area),
    INDEX idx_lhc_active (is_active),
    INDEX idx_lhc_sort (sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @schema_name = DATABASE();
SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'documents' AND COLUMN_NAME = 'writer_id') = 0,
    'ALTER TABLE documents ADD COLUMN writer_id VARCHAR(64) NOT NULL DEFAULT ''''',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'admin_users' AND COLUMN_NAME = 'avatar_path') = 0,
    'ALTER TABLE admin_users ADD COLUMN avatar_path VARCHAR(1024) NOT NULL DEFAULT ''''',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;
