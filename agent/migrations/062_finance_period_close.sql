-- Monthly fee-item inventory and period close for the Ronghui finance ledger.
-- A month is confirmed item by item against the mapping version in effect,
-- then closed by a super administrator into an immutable snapshot version.
-- Reopening keeps every earlier version; re-closing adds the next one.
-- {{finance_db}} is bound by the deployment runner, which then projects the
-- agent_db views the Agent and Console read and write through.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_periods (
    period_month DATE NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    current_close_id BIGINT UNSIGNED NULL,
    updated_by VARCHAR(128) NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (period_month),
    KEY idx_finance_periods_status (status, period_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_period_item_reviews (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    period_month DATE NOT NULL,
    fee_item_id BIGINT UNSIGNED NOT NULL,
    mapping_id BIGINT UNSIGNED NOT NULL,
    confirmed_by VARCHAR(128) NOT NULL,
    confirmed_at DATETIME(6) NOT NULL,
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_period_item_review (period_month, fee_item_id),
    KEY idx_finance_period_item_review_request (request_id),
    CONSTRAINT fk_finance_period_review_fee_item FOREIGN KEY (fee_item_id)
        REFERENCES {{finance_db}}.finance_fee_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_finance_period_review_mapping FOREIGN KEY (mapping_id)
        REFERENCES {{finance_db}}.finance_fee_mappings (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_period_closes (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    period_month DATE NOT NULL,
    version_no INT UNSIGNED NOT NULL,
    status VARCHAR(16) NOT NULL,
    line_count BIGINT UNSIGNED NOT NULL,
    transaction_count BIGINT UNSIGNED NOT NULL,
    total_income DECIMAL(20,4) NOT NULL,
    total_expense DECIMAL(20,4) NOT NULL,
    net_change DECIMAL(20,4) NOT NULL,
    content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    check_report_json JSON NOT NULL,
    closed_by VARCHAR(128) NOT NULL,
    closed_at DATETIME(6) NOT NULL,
    close_request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    reopened_by VARCHAR(128) NULL,
    reopened_at DATETIME(6) NULL,
    reopen_reason VARCHAR(500) NULL,
    reopen_request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_period_close_version (period_month, version_no),
    UNIQUE KEY uq_finance_period_close_request (close_request_id),
    UNIQUE KEY uq_finance_period_reopen_request (reopen_request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_period_close_lines (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    close_id BIGINT UNSIGNED NOT NULL,
    platform VARCHAR(32) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    business_date DATE NOT NULL,
    fee_item_id BIGINT UNSIGNED NOT NULL,
    direction VARCHAR(16) NOT NULL,
    mapping_id BIGINT UNSIGNED NULL,
    fee_level VARCHAR(16) NULL,
    canonical_subject_id BIGINT UNSIGNED NULL,
    include_in_cost TINYINT(1) NOT NULL DEFAULT 0,
    transaction_count BIGINT UNSIGNED NOT NULL,
    income DECIMAL(20,4) NOT NULL,
    expense DECIMAL(20,4) NOT NULL,
    net_change DECIMAL(20,4) NOT NULL,
    source_run_id BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_period_close_line (
        close_id, platform, account_id, business_date, fee_item_id, direction
    ),
    KEY idx_finance_period_close_line_date (close_id, platform, account_id, business_date),
    CONSTRAINT fk_finance_period_line_close FOREIGN KEY (close_id)
        REFERENCES {{finance_db}}.finance_period_closes (id) ON DELETE RESTRICT,
    CONSTRAINT fk_finance_period_line_fee_item FOREIGN KEY (fee_item_id)
        REFERENCES {{finance_db}}.finance_fee_items (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_period_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    period_month DATE NOT NULL,
    action VARCHAR(32) NOT NULL,
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    actor VARCHAR(128) NOT NULL,
    details_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_period_event_request (action, request_id),
    KEY idx_finance_period_event_month (period_month, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- An acknowledged anomaly stays acknowledged only while a rescan reports the
-- same count and amount it was acknowledged with.
ALTER TABLE {{finance_db}}.finance_anomalies
    ADD COLUMN acknowledged_by VARCHAR(128) NULL AFTER notified_at,
    ADD COLUMN acknowledged_at DATETIME(6) NULL AFTER acknowledged_by,
    ADD COLUMN acknowledge_reason VARCHAR(500) NULL AFTER acknowledged_at,
    ADD COLUMN acknowledged_count BIGINT UNSIGNED NULL AFTER acknowledge_reason,
    ADD COLUMN acknowledged_amount DECIMAL(20,4) NULL AFTER acknowledged_count;
