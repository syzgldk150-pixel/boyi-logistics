CREATE TABLE IF NOT EXISTS r7_task_events (
    id BIGINT NOT NULL AUTO_INCREMENT,
    event_ts DATETIME NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    task_number VARCHAR(32) NULL,
    class_name VARCHAR(128) NULL,
    task_status INT NULL,
    task_status_name VARCHAR(32) NULL,
    plan_go_time VARCHAR(19) NULL,
    plan_arrive_time VARCHAR(19) NULL,
    ok TINYINT NULL,
    manual_arrive_time VARCHAR(19) NULL,
    message TEXT NULL,
    detail_json LONGTEXT NULL,
    PRIMARY KEY (id),
    INDEX idx_r7_task_events_task_ts (task_number, event_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS r7_task_status (
    task_number VARCHAR(32) NOT NULL,
    class_name VARCHAR(128) NULL,
    task_status INT NULL,
    task_status_name VARCHAR(32) NULL,
    plan_go_time VARCHAR(19) NULL,
    plan_arrive_time VARCHAR(19) NULL,
    last_seen_ts DATETIME NOT NULL,
    checkin_success TINYINT NOT NULL DEFAULT 0,
    manual_arrive_time VARCHAR(19) NULL,
    detail_json LONGTEXT NULL,
    PRIMARY KEY (task_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS r7_arrival_checkin_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    business_date DATE NOT NULL,
    task_id VARCHAR(128),
    trigger_mode VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    ok BOOLEAN,
    skipped BOOLEAN DEFAULT FALSE,
    daily_success_limit INT,
    success_count_before INT,
    success_count_after INT,
    stage VARCHAR(64),
    message TEXT,
    detail_json JSON,
    params_json JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_r7_checkin_date_status (business_date, status),
    INDEX idx_r7_checkin_task_date (task_id, business_date),
    INDEX idx_r7_checkin_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS r7_departure_checkin_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    business_date DATE NOT NULL,
    task_id VARCHAR(128),
    trigger_mode VARCHAR(32),
    status VARCHAR(32) NOT NULL,
    ok BOOLEAN,
    skipped BOOLEAN DEFAULT FALSE,
    daily_success_limit INT,
    success_count_before INT,
    success_count_after INT,
    target_plate_numbers JSON,
    target_departure_time VARCHAR(32),
    class_name VARCHAR(128),
    stage VARCHAR(64),
    message TEXT,
    detail_json JSON,
    params_json JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_r7_departure_date_status (business_date, status),
    INDEX idx_r7_departure_task_date (task_id, business_date),
    INDEX idx_r7_departure_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
