-- Cache exact TMS main-waybill sign checks so historical reroutes stay accurate
-- without querying every open waybill on every daily-sign run.

CREATE TABLE IF NOT EXISTS waybill_sign_verification_state (
    tracking_number VARCHAR(64) PRIMARY KEY,
    last_checked_at DATETIME NOT NULL,
    last_result VARCHAR(24) NOT NULL,
    next_check_at DATETIME NULL,
    consecutive_not_signed INT NOT NULL DEFAULT 0,
    last_error VARCHAR(500) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_waybill_sign_verification_due (next_check_at, last_result)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
