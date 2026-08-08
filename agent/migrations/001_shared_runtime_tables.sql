CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    tool_name VARCHAR(64) NOT NULL,
    tool_params JSON,
    cron_expression VARCHAR(64) NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    last_run DATETIME NULL,
    last_status VARCHAR(16) NULL,
    last_duration_ms INT NULL,
    last_message TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS workflow_resources (
    resource_key VARCHAR(128) PRIMARY KEY,
    config_json JSON NOT NULL,
    source VARCHAR(128),
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS waybills (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    document_id BIGINT NULL,
    waybill_no VARCHAR(128) NOT NULL DEFAULT '',
    destination_site VARCHAR(256) NOT NULL DEFAULT '',
    open_date VARCHAR(64) NOT NULL DEFAULT '',
    receiver_address TEXT NOT NULL,
    receiver_name VARCHAR(128) NOT NULL DEFAULT '',
    receiver_phone VARCHAR(64) NOT NULL DEFAULT '',
    sender_name VARCHAR(128) NOT NULL DEFAULT '',
    sender_phone VARCHAR(64) NOT NULL DEFAULT '',
    goods_name_lines TEXT NOT NULL,
    package_type_lines TEXT NOT NULL,
    quantity_lines TEXT NOT NULL,
    weight_volume VARCHAR(128) NOT NULL DEFAULT '',
    delivery_method VARCHAR(32) NOT NULL DEFAULT '',
    freight_fee VARCHAR(64) NOT NULL DEFAULT '',
    pickup_fee VARCHAR(64) NOT NULL DEFAULT '',
    delivery_fee VARCHAR(64) NOT NULL DEFAULT '',
    transfer_fee VARCHAR(64) NOT NULL DEFAULT '',
    payment_method VARCHAR(64) NOT NULL DEFAULT '',
    insurance_amount VARCHAR(64) NOT NULL DEFAULT '',
    cod_amount VARCHAR(64) NOT NULL DEFAULT '',
    remark TEXT NOT NULL,
    writer_id VARCHAR(64) NOT NULL DEFAULT '',
    source VARCHAR(32) NOT NULL DEFAULT 'ocr',
    status VARCHAR(32) NOT NULL DEFAULT 'in_transit',
    scan_status VARCHAR(128) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    INDEX idx_wb_waybill_no (waybill_no),
    INDEX idx_wb_source (source),
    INDEX idx_wb_status (status),
    INDEX idx_wb_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
