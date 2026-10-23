-- Raw platform finance captures: the statement query (统计查询) of a period
-- and its full detail rows (明细数据), each kept as the exact JSON object the
-- platform returned. A capture is stored only after the Host proved every
-- page was observed in the same plugin invocation and the details close the
-- statement item by item. Publication into the daily ledger is tracked per day.
-- {{finance_db}} is bound by the deployment runner, which then projects the
-- agent_db views.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_platform_captures (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    capture_ref CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    platform VARCHAR(32) NOT NULL,
    source_role VARCHAR(64) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    site_code VARCHAR(64) NOT NULL,
    site_name VARCHAR(255) NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    statement_item_count INT UNSIGNED NOT NULL,
    statement_net DECIMAL(20,4) NOT NULL,
    billing_statement_item_count INT UNSIGNED NULL,
    billing_statement_net DECIMAL(20,4) NULL,
    detail_row_count BIGINT UNSIGNED NOT NULL,
    detail_income DECIMAL(20,4) NOT NULL,
    detail_expense DECIMAL(20,4) NOT NULL,
    detail_net DECIMAL(20,4) NOT NULL,
    field_mapping_json JSON NOT NULL,
    request_profile_json JSON NOT NULL,
    content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    automation_id VARCHAR(191) NOT NULL,
    plugin_version VARCHAR(64) NOT NULL,
    generation BIGINT NOT NULL,
    invocation_id VARCHAR(191) NULL,
    publish_batch_id BIGINT UNSIGNED NULL,
    published_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_platform_capture_ref (capture_ref),
    KEY idx_finance_platform_capture_period (platform, account_id, period_start, period_end, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_platform_statement_rows (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    capture_id BIGINT UNSIGNED NOT NULL,
    basis VARCHAR(16) NOT NULL,
    row_no INT UNSIGNED NOT NULL,
    item_name VARCHAR(255) NOT NULL,
    site_code VARCHAR(64) NOT NULL,
    amount DECIMAL(20,4) NOT NULL,
    raw_json JSON NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_platform_statement_row (capture_id, basis, row_no),
    KEY idx_finance_platform_statement_item (capture_id, basis, item_name),
    CONSTRAINT fk_finance_platform_statement_capture FOREIGN KEY (capture_id)
        REFERENCES {{finance_db}}.finance_platform_captures (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_platform_detail_rows (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    capture_id BIGINT UNSIGNED NOT NULL,
    row_no INT UNSIGNED NOT NULL,
    source_key VARCHAR(191) NOT NULL,
    order_no VARCHAR(191) NULL,
    waybill_no VARCHAR(191) NULL,
    item_name VARCHAR(255) NOT NULL,
    settled_at DATETIME(6) NOT NULL,
    billed_at DATETIME(6) NULL,
    amount DECIMAL(20,4) NOT NULL,
    before_balance DECIMAL(20,4) NULL,
    after_balance DECIMAL(20,4) NULL,
    source_type VARCHAR(64) NULL,
    remark VARCHAR(1000) NULL,
    customer_name VARCHAR(255) NULL,
    goods_name VARCHAR(255) NULL,
    raw_json JSON NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_platform_detail_key (capture_id, source_key),
    KEY idx_finance_platform_detail_item (capture_id, item_name),
    KEY idx_finance_platform_detail_settled (capture_id, settled_at),
    KEY idx_finance_platform_detail_waybill (waybill_no),
    CONSTRAINT fk_finance_platform_detail_capture FOREIGN KEY (capture_id)
        REFERENCES {{finance_db}}.finance_platform_captures (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_platform_capture_days (
    capture_id BIGINT UNSIGNED NOT NULL,
    business_date DATE NOT NULL,
    run_id BIGINT UNSIGNED NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    record_count BIGINT UNSIGNED NOT NULL,
    published_at DATETIME(6) NOT NULL,
    PRIMARY KEY (capture_id, business_date),
    UNIQUE KEY uq_finance_platform_capture_day_run (run_id),
    CONSTRAINT fk_finance_platform_day_capture FOREIGN KEY (capture_id)
        REFERENCES {{finance_db}}.finance_platform_captures (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
