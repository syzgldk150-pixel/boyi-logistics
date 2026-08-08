CREATE TABLE IF NOT EXISTS conversations (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS messages (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id VARCHAR(64) NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT,
    tool_calls JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_conv (conversation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tool_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    message_id BIGINT,
    conversation_id VARCHAR(64),
    tool_name VARCHAR(64) NOT NULL,
    params JSON,
    result JSON,
    success BOOLEAN,
    duration_ms INT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_conv (conversation_id),
    INDEX idx_tool (tool_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS knowledge (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    category VARCHAR(64),
    content TEXT NOT NULL,
    source VARCHAR(256),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FULLTEXT INDEX ft_content (content) WITH PARSER ngram
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS waybill_data (
    tracking_number VARCHAR(64) PRIMARY KEY,
    goods_name VARCHAR(255),
    package_type VARCHAR(64),
    delivery_method VARCHAR(64),
    quantity INT,
    receipt_number VARCHAR(64),
    actual_weight DECIMAL(18,2),
    volume DECIMAL(18,3),
    remarks VARCHAR(255),
    destination_station VARCHAR(128),
    recipient_name VARCHAR(128),
    recipient_phone VARCHAR(64),
    recipient_address VARCHAR(512),
    settlement_weight DECIMAL(18,2),
    volumetric_weight DECIMAL(18,2),
    shipping_fee DECIMAL(18,2),
    payment_type VARCHAR(64),
    pay_on_arrival DECIMAL(18,2),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_destination_station (destination_station),
    INDEX idx_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS split_pending_problem_items (
    tracking_number VARCHAR(64) PRIMARY KEY,
    source_row_no INT NOT NULL,
    destination_station VARCHAR(128),
    expected_quantity INT NOT NULL,
    arrived_quantity INT NOT NULL,
    pending_quantity INT NOT NULL,
    problem_type VARCHAR(32) NOT NULL,
    problem_owner_type VARCHAR(64) NOT NULL,
    problem_cause VARCHAR(255) NOT NULL,
    upload_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    error_summary VARCHAR(500) NULL,
    uploaded_at DATETIME NULL,
    complaint_status VARCHAR(16) NOT NULL DEFAULT 'not_applicable',
    complaint_error_summary VARCHAR(500) NULL,
    complaint_processed_at DATETIME NULL,
    refreshed_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_split_pending_status (upload_status),
    INDEX idx_split_pending_complaint_status (complaint_status),
    INDEX idx_split_pending_refreshed (refreshed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS scan_codes (
    raw_code VARCHAR(64) PRIMARY KEY,
    destination VARCHAR(128),
    code_type VARCHAR(16) NOT NULL,
    main_tracking VARCHAR(64) NULL,
    seen_count INT NOT NULL DEFAULT 1,
    last_seen_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_code_type (code_type),
    INDEX idx_destination (destination),
    INDEX idx_last_seen_at (last_seen_at),
    INDEX idx_main_tracking (main_tracking)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @schema_name = DATABASE();
SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'split_pending_problem_items' AND COLUMN_NAME = 'complaint_status') = 0,
    'ALTER TABLE split_pending_problem_items ADD COLUMN complaint_status VARCHAR(16) NOT NULL DEFAULT ''not_applicable'', ADD INDEX idx_split_pending_complaint_status (complaint_status)',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'split_pending_problem_items' AND COLUMN_NAME = 'complaint_error_summary') = 0,
    'ALTER TABLE split_pending_problem_items ADD COLUMN complaint_error_summary VARCHAR(500) NULL',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'split_pending_problem_items' AND COLUMN_NAME = 'complaint_processed_at') = 0,
    'ALTER TABLE split_pending_problem_items ADD COLUMN complaint_processed_at DATETIME NULL',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'scan_codes' AND COLUMN_NAME = 'main_tracking') = 0,
    'ALTER TABLE scan_codes ADD COLUMN main_tracking VARCHAR(64) NULL, ADD INDEX idx_main_tracking (main_tracking)',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

CREATE OR REPLACE VIEW v_missing_in_waybill AS
SELECT sc.raw_code AS main_tracking
FROM scan_codes sc
LEFT JOIN waybill_data wd ON wd.tracking_number = sc.raw_code
WHERE sc.code_type = 'main'
  AND sc.last_seen_at >= CURDATE()
  AND wd.tracking_number IS NULL
GROUP BY sc.raw_code;

CREATE OR REPLACE VIEW v_arrival_progress AS
SELECT
    wd.tracking_number,
    wd.destination_station,
    wd.quantity AS expected_quantity,
    COALESCE(arrived.cnt, 0) AS arrived_quantity,
    GREATEST(COALESCE(wd.quantity, 0) - COALESCE(arrived.cnt, 0), 0) AS pending_quantity,
    arrived.first_seen AS first_arrival_at,
    arrived.last_seen AS last_arrival_at,
    CASE
        WHEN wd.quantity IS NULL OR wd.quantity <= 0 THEN 'unknown'
        WHEN COALESCE(arrived.cnt, 0) >= wd.quantity THEN 'completed'
        WHEN COALESCE(arrived.cnt, 0) > 0 THEN 'partial'
        ELSE 'pending'
    END AS arrival_status
FROM waybill_data wd
LEFT JOIN (
    SELECT
        main_tracking,
        COUNT(DISTINCT raw_code) AS cnt,
        MIN(last_seen_at) AS first_seen,
        MAX(last_seen_at) AS last_seen
    FROM scan_codes
    WHERE code_type = 'child'
      AND main_tracking IS NOT NULL
      AND main_tracking <> ''
    GROUP BY main_tracking
) arrived ON arrived.main_tracking = wd.tracking_number;
