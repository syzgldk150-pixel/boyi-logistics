-- Customer share links for sign-for photos, receipt photos and sender labels.
-- A customer is a set of sender-matching rules; a share is one read-only link
-- for one customer. Images are archived once per waybill on the NAS and listed
-- per share in receipt_share_items, the only table the public share service
-- reads to decide what a link may show.
-- {{waybill_db}} is bound by the deployment runner, which then projects the
-- agent_db views. No foreign keys; every statement can run again.
CREATE TABLE IF NOT EXISTS {{waybill_db}}.receipt_share_customers (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    code VARCHAR(32) NOT NULL,
    name VARCHAR(160) NOT NULL,
    short_name VARCHAR(40) NOT NULL,
    match_json JSON NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_by VARCHAR(128) NOT NULL DEFAULT '',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_receipt_share_customer_code (code),
    UNIQUE KEY uq_receipt_share_customer_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{waybill_db}}.receipt_shares (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    customer_id BIGINT UNSIGNED NOT NULL,
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    token_version INT UNSIGNED NOT NULL DEFAULT 1,
    token_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    access_code_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    scope VARCHAR(16) NOT NULL DEFAULT 'all',
    date_from DATE NULL,
    date_to DATE NULL,
    image_types_json JSON NOT NULL,
    naming_template VARCHAR(160) NOT NULL,
    expires_at DATETIME NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    view_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    last_viewed_at DATETIME NULL,
    refresh_state VARCHAR(16) NOT NULL DEFAULT 'pending',
    refresh_message VARCHAR(500) NOT NULL DEFAULT '',
    last_refreshed_at DATETIME NULL,
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    created_by VARCHAR(128) NOT NULL DEFAULT '',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_receipt_share_request (request_id),
    UNIQUE KEY uq_receipt_share_token (token_sha256),
    KEY idx_receipt_share_customer (customer_id),
    KEY idx_receipt_share_status (status),
    CONSTRAINT chk_receipt_share_scope CHECK (scope IN ('all', 'approved', 'by_date')),
    CONSTRAINT chk_receipt_share_status CHECK (status IN ('active', 'disabled'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One archived image of one waybill, independent of customers and shares.
-- kind: sender_label / sign / receipt. source_key identifies the origin
-- (digest of the platform image address, or of the label data) so a refresh
-- never stores the same picture twice.
CREATE TABLE IF NOT EXISTS {{waybill_db}}.receipt_share_images (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    platform VARCHAR(16) NOT NULL,
    waybill_no VARCHAR(128) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    source_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    seq INT UNSIGNED NOT NULL DEFAULT 1,
    content_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    nas_path VARCHAR(512) NOT NULL,
    mime_type VARCHAR(64) NOT NULL,
    file_size BIGINT UNSIGNED NOT NULL,
    doc_date DATE NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_receipt_share_image_source (platform, waybill_no, kind, source_key),
    KEY idx_receipt_share_image_waybill (waybill_no),
    CONSTRAINT chk_receipt_share_image_kind CHECK (kind IN ('sender_label', 'sign', 'receipt'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- What one link shows. Replaced as a whole by each refresh of that share.
CREATE TABLE IF NOT EXISTS {{waybill_db}}.receipt_share_items (
    share_id BIGINT UNSIGNED NOT NULL,
    image_id BIGINT UNSIGNED NOT NULL,
    platform VARCHAR(16) NOT NULL,
    waybill_no VARCHAR(128) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    seq INT UNSIGNED NOT NULL DEFAULT 1,
    doc_date DATE NULL,
    PRIMARY KEY (share_id, image_id),
    KEY idx_receipt_share_item_list (share_id, kind, doc_date, waybill_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{waybill_db}}.receipt_share_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    share_id BIGINT UNSIGNED NULL,
    customer_id BIGINT UNSIGNED NULL,
    action VARCHAR(32) NOT NULL,
    actor_id VARCHAR(128) NOT NULL DEFAULT '',
    actor_name VARCHAR(128) NOT NULL DEFAULT '',
    detail_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_receipt_share_event_share (share_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{waybill_db}}.receipt_share_access_logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    share_id BIGINT UNSIGNED NOT NULL,
    action VARCHAR(16) NOT NULL,
    client_ip VARCHAR(64) NOT NULL DEFAULT '',
    user_agent VARCHAR(255) NOT NULL DEFAULT '',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_receipt_share_access_share (share_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- When one waybill's platform photos were last read, so a refresh asks the
-- platform again only for waybills that may still gain pictures.
CREATE TABLE IF NOT EXISTS {{waybill_db}}.receipt_share_waybill_state (
    platform VARCHAR(16) NOT NULL,
    waybill_no VARCHAR(128) NOT NULL,
    sign_count INT UNSIGNED NOT NULL DEFAULT 0,
    receipt_count INT UNSIGNED NOT NULL DEFAULT 0,
    check_count INT UNSIGNED NOT NULL DEFAULT 0,
    last_error VARCHAR(200) NOT NULL DEFAULT '',
    last_checked_at DATETIME NOT NULL,
    PRIMARY KEY (platform, waybill_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
