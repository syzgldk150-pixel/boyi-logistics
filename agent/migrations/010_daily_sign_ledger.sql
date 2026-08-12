-- Shared, versioned data backbone for the daily-sign workflow.

CREATE TABLE IF NOT EXISTS arrival_forecast_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    business_date DATE NOT NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'arrive-list',
    status VARCHAR(16) NOT NULL,
    row_count INT NOT NULL DEFAULT 0,
    fingerprint CHAR(64) NOT NULL,
    completed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_arrival_forecast_run_id (run_id),
    INDEX idx_arrival_forecast_business_date (business_date, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS arrival_forecast_items (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    tracking_number VARCHAR(64) NOT NULL,
    expected_quantity INT NULL,
    destination_station VARCHAR(128) NULL,
    goods_name VARCHAR(255) NULL,
    package_type VARCHAR(64) NULL,
    delivery_method VARCHAR(64) NULL,
    recipient_address VARCHAR(512) NULL,
    payload_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_arrival_forecast_item (run_id, tracking_number),
    INDEX idx_arrival_forecast_tracking (tracking_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS arrival_stat_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    business_date DATE NOT NULL,
    status VARCHAR(16) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    row_count INT NOT NULL DEFAULT 0,
    fingerprint CHAR(64) NOT NULL,
    completed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_arrival_stat_run_id (run_id),
    INDEX idx_arrival_stat_active (business_date, is_active, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS arrival_stat_items (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    tracking_number VARCHAR(64) NOT NULL,
    destination_station VARCHAR(128) NULL,
    expected_quantity INT NULL,
    arrived_quantity INT NULL,
    goods_name VARCHAR(255) NULL,
    package_type VARCHAR(64) NULL,
    delivery_method VARCHAR(64) NULL,
    recipient_address VARCHAR(512) NULL,
    payload_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_arrival_stat_item (run_id, tracking_number),
    INDEX idx_arrival_stat_tracking (tracking_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS waybill_problem_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    external_id VARCHAR(128) NOT NULL,
    tracking_number VARCHAR(64) NOT NULL,
    problem_type VARCHAR(128) NOT NULL,
    registered_at DATETIME NOT NULL,
    registered_site VARCHAR(128) NULL,
    upload_complete BOOLEAN NOT NULL DEFAULT FALSE,
    before_cutoff BOOLEAN NOT NULL DEFAULT FALSE,
    postpones_sign BOOLEAN NOT NULL DEFAULT FALSE,
    payload_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_waybill_problem_source_id (source, external_id),
    INDEX idx_waybill_problem_tracking (tracking_number, registered_at),
    INDEX idx_waybill_problem_valid (before_cutoff, postpones_sign)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS waybill_sign_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(32) NOT NULL DEFAULT 'tms_scan',
    external_id VARCHAR(191) NOT NULL,
    tracking_number VARCHAR(64) NOT NULL,
    scan_code VARCHAR(64) NOT NULL,
    scan_type VARCHAR(32) NOT NULL,
    scanned_at DATETIME NOT NULL,
    scan_site VARCHAR(128) NULL,
    is_main_waybill BOOLEAN NOT NULL DEFAULT FALSE,
    payload_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_waybill_sign_source_id (source, external_id),
    INDEX idx_waybill_sign_tracking (tracking_number, scanned_at),
    INDEX idx_waybill_sign_main (is_main_waybill, scan_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS daily_sign_ledger (
    tracking_number VARCHAR(64) PRIMARY KEY,
    r13_plan_sign_at DATETIME NULL,
    r13_sign_status VARCHAR(64) NULL,
    r13_sign_at DATETIME NULL,
    first_seen_r13_at DATETIME NULL,
    last_seen_r13_at DATETIME NULL,
    r13_current BOOLEAN NOT NULL DEFAULT FALSE,
    first_arrival_date DATE NULL,
    completion_date DATE NULL,
    expected_quantity INT NULL,
    arrived_quantity INT NULL,
    arrival_status VARCHAR(16) NOT NULL DEFAULT 'unknown',
    system_sign_due_at DATETIME NULL,
    tms_signed BOOLEAN NOT NULL DEFAULT FALSE,
    tms_signed_at DATETIME NULL,
    goods_name VARCHAR(255) NULL,
    package_type VARCHAR(64) NULL,
    delivery_method VARCHAR(64) NULL,
    recipient_address VARCHAR(512) NULL,
    data_quality_flags JSON NULL,
    calculation_trace JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_daily_sign_open (tms_signed, system_sign_due_at),
    INDEX idx_daily_sign_r13_due (tms_signed, r13_plan_sign_at),
    INDEX idx_daily_sign_r13_current (r13_current)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS daily_sign_sync_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    started_at DATETIME NOT NULL,
    completed_at DATETIME NULL,
    status VARCHAR(24) NOT NULL,
    degraded BOOLEAN NOT NULL DEFAULT FALSE,
    r13_complete BOOLEAN NOT NULL DEFAULT FALSE,
    problems_complete BOOLEAN NOT NULL DEFAULT FALSE,
    signs_complete BOOLEAN NOT NULL DEFAULT FALSE,
    r13_rows INT NOT NULL DEFAULT 0,
    arrival_rows INT NOT NULL DEFAULT 0,
    problem_rows INT NOT NULL DEFAULT 0,
    sign_rows INT NOT NULL DEFAULT 0,
    candidate_rows INT NOT NULL DEFAULT 0,
    published_rows INT NOT NULL DEFAULT 0,
    unmatched_rows INT NOT NULL DEFAULT 0,
    fingerprint CHAR(64) NULL,
    diagnostics_json JSON NULL,
    error_summary VARCHAR(500) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_daily_sign_sync_run_id (run_id),
    INDEX idx_daily_sign_sync_started (started_at, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
