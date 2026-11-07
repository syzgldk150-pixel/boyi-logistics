-- Connector intake of the rebuilt finance module (P4): finance sources, the
-- uploaded bill files connectors parse, immutable batches and lines of the
-- standard source contract v1, line posting rules, suspense reviews and
-- anomalies. {{finance_db}} is bound by the deployment runner, which then
-- re-projects the agent_db views listed in LATER_FINANCE_TABLES.

-- A finance source (a platform account, a bank card, a WeChat/Alipay merchant
-- account...). Only the plugin instance named as producer may submit for it.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_sources (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    kind ENUM('online','file') NOT NULL,
    provider VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    producer_instance_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    fund_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    is_enabled TINYINT(1) NOT NULL DEFAULT 1,
    remark VARCHAR(255) NOT NULL DEFAULT '',
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_sources_key (source_key),
    KEY idx_finance_sources_producer (producer_instance_id),
    CONSTRAINT fk_finance_sources_fund FOREIGN KEY (fund_account) REFERENCES {{finance_db}}.finance_accounts (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_source_files (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id BIGINT UNSIGNED NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    content_type VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream',
    size_bytes BIGINT UNSIGNED NOT NULL,
    sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    chunk_count INT UNSIGNED NOT NULL,
    status ENUM('pending','consumed','discarded') NOT NULL DEFAULT 'pending',
    batch_id BIGINT UNSIGNED NULL,
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    uploaded_by VARCHAR(128) NOT NULL,
    uploaded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_source_files_request (request_id),
    KEY idx_finance_source_files_source (source_id, status),
    CONSTRAINT fk_finance_source_files_source FOREIGN KEY (source_id) REFERENCES {{finance_db}}.finance_sources (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_source_file_chunks (
    file_id BIGINT UNSIGNED NOT NULL,
    chunk_no INT UNSIGNED NOT NULL,
    data MEDIUMBLOB NOT NULL,
    PRIMARY KEY (file_id, chunk_no),
    CONSTRAINT fk_finance_source_file_chunks_file FOREIGN KEY (file_id) REFERENCES {{finance_db}}.finance_source_files (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One submitted batch; accepted batches and their lines are never changed
-- except to be marked superseded by a later batch of the same period.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_source_batches (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id BIGINT UNSIGNED NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    status ENUM('accepted','rejected','superseded') NOT NULL,
    declares_json JSON NOT NULL,
    checks_json JSON NOT NULL,
    row_count INT UNSIGNED NOT NULL,
    inflow DECIMAL(20,4) NOT NULL,
    outflow DECIMAL(20,4) NOT NULL,
    opening_balance DECIMAL(20,4) NULL,
    closing_balance DECIMAL(20,4) NULL,
    content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    evidence_kind ENUM('http_observation','file') NOT NULL,
    evidence_json JSON NOT NULL,
    file_id BIGINT UNSIGNED NULL,
    automation_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    plugin_version VARCHAR(64) NOT NULL,
    error_code VARCHAR(64) NULL,
    error_message VARCHAR(512) NULL,
    superseded_by BIGINT UNSIGNED NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_finance_source_batches_source (source_id, status, period_start),
    KEY idx_finance_source_batches_content (source_id, content_sha256),
    CONSTRAINT fk_finance_source_batches_source FOREIGN KEY (source_id) REFERENCES {{finance_db}}.finance_sources (id),
    CONSTRAINT fk_finance_source_batches_file FOREIGN KEY (file_id) REFERENCES {{finance_db}}.finance_source_files (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_source_lines (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    batch_id BIGINT UNSIGNED NOT NULL,
    source_id BIGINT UNSIGNED NOT NULL,
    source_key VARCHAR(191) NOT NULL,
    business_date DATE NOT NULL,
    posted_at VARCHAR(40) NULL,
    amount DECIMAL(20,4) NOT NULL,
    item_l1 VARCHAR(128) NOT NULL DEFAULT '',
    item_l2 VARCHAR(128) NOT NULL DEFAULT '',
    counterparty VARCHAR(255) NOT NULL DEFAULT '',
    waybill_no VARCHAR(128) NULL,
    reference VARCHAR(255) NOT NULL DEFAULT '',
    balance_before DECIMAL(20,4) NULL,
    balance_after DECIMAL(20,4) NULL,
    raw_json JSON NOT NULL,
    row_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    posting_status ENUM('unposted','posted','suspense','no_account','closed_period') NOT NULL DEFAULT 'unposted',
    voucher_id BIGINT UNSIGNED NULL,
    rule_id BIGINT UNSIGNED NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_source_lines_batch_key (batch_id, source_key),
    KEY idx_finance_source_lines_active (source_id, is_active, business_date),
    KEY idx_finance_source_lines_key (source_id, source_key),
    KEY idx_finance_source_lines_waybill (waybill_no),
    KEY idx_finance_source_lines_voucher (voucher_id),
    CONSTRAINT fk_finance_source_lines_batch FOREIGN KEY (batch_id) REFERENCES {{finance_db}}.finance_source_batches (id),
    CONSTRAINT fk_finance_source_lines_voucher FOREIGN KEY (voucher_id) REFERENCES {{finance_db}}.finance_vouchers (id),
    CONSTRAINT ck_finance_source_lines_amount CHECK (amount <> 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Exact-match posting rule for source lines: provider + item_l1 + item_l2 +
-- direction. No fuzzy matching; unmatched lines go to the suspense account.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_line_rules (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    provider VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    item_l1 VARCHAR(128) NOT NULL,
    item_l2 VARCHAR(128) NOT NULL DEFAULT '',
    direction ENUM('in','out') NOT NULL,
    counter_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    party_action ENUM('none','settle_by_waybill','party_fixed','internal_transfer') NOT NULL DEFAULT 'none',
    party_id BIGINT UNSIGNED NULL,
    requires_waybill TINYINT(1) NOT NULL DEFAULT 0,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    remark VARCHAR(255) NOT NULL DEFAULT '',
    updated_by VARCHAR(128) NOT NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_line_rules_key (provider, item_l1, item_l2, direction),
    CONSTRAINT fk_finance_line_rules_account FOREIGN KEY (counter_account) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT fk_finance_line_rules_party FOREIGN KEY (party_id) REFERENCES {{finance_db}}.finance_parties (id),
    CONSTRAINT ck_finance_line_rules_target CHECK ((party_action = 'internal_transfer') = (counter_account IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_reviews (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id BIGINT UNSIGNED NOT NULL,
    provider VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    item_l1 VARCHAR(128) NOT NULL,
    item_l2 VARCHAR(128) NOT NULL DEFAULT '',
    direction ENUM('in','out') NOT NULL,
    line_count INT UNSIGNED NOT NULL DEFAULT 0,
    amount_total DECIMAL(20,4) NOT NULL DEFAULT 0,
    status ENUM('open','resolved') NOT NULL DEFAULT 'open',
    resolved_rule_id BIGINT UNSIGNED NULL,
    resolved_by VARCHAR(128) NULL,
    resolved_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_reviews_key (source_id, item_l1, item_l2, direction),
    CONSTRAINT fk_finance_reviews_source FOREIGN KEY (source_id) REFERENCES {{finance_db}}.finance_sources (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_anomalies (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    kind VARCHAR(48) NOT NULL,
    source_id BIGINT UNSIGNED NULL,
    batch_id BIGINT UNSIGNED NULL,
    period_month CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NULL,
    detail_json JSON NOT NULL,
    status ENUM('open','acknowledged') NOT NULL DEFAULT 'open',
    acknowledged_by VARCHAR(128) NULL,
    acknowledged_at DATETIME(6) NULL,
    acknowledge_reason VARCHAR(255) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_finance_anomalies_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Allocations created automatically when a source day is posted; reposting
-- that day reverses them first. Kept last: plain DDL.
ALTER TABLE {{finance_db}}.finance_allocations
    ADD COLUMN origin_voucher_id BIGINT UNSIGNED NULL AFTER method,
    ADD KEY idx_finance_allocations_origin (origin_voucher_id);
