"""MySQL schema for immutable finance snapshots and versioned mappings."""

from __future__ import annotations


MYSQL_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS finance_sync_batches (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        trigger_type VARCHAR(32) NOT NULL,
        requested_start_date DATE NOT NULL,
        requested_end_date DATE NOT NULL,
        rescan_days INT UNSIGNED NOT NULL DEFAULT 0,
        status VARCHAR(32) NOT NULL,
        earliest_date_status VARCHAR(64) NULL,
        requested_by VARCHAR(128) NULL,
        frozen_at DATETIME(6) NOT NULL,
        started_at DATETIME(6) NOT NULL,
        finished_at DATETIME(6) NULL,
        error_code VARCHAR(128) NULL,
        error_message TEXT NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        KEY idx_finance_batches_status_created (status, created_at),
        KEY idx_finance_batches_dates (requested_start_date, requested_end_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS finance_sync_runs (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        batch_id BIGINT UNSIGNED NOT NULL,
        platform VARCHAR(32) NOT NULL,
        account_id VARCHAR(128) NOT NULL,
        login_account VARCHAR(128) NULL,
        session_profile VARCHAR(128) NULL,
        target_date DATE NOT NULL,
        source_site_code VARCHAR(128) NULL,
        source_site_name VARCHAR(255) NULL,
        attempt_no INT UNSIGNED NOT NULL,
        status VARCHAR(32) NOT NULL,
        remote_total BIGINT UNSIGNED NULL,
        page_row_count BIGINT UNSIGNED NULL,
        unique_row_count BIGINT UNSIGNED NULL,
        written_row_count BIGINT UNSIGNED NULL,
        validation_status VARCHAR(32) NULL,
        validation_report_json JSON NULL,
        error_code VARCHAR(128) NULL,
        error_message TEXT NULL,
        started_at DATETIME(6) NOT NULL,
        finished_at DATETIME(6) NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_finance_run_attempt (
            batch_id, platform, account_id, target_date, attempt_no
        ),
        KEY idx_finance_runs_latest (
            platform, account_id, target_date, status, id
        ),
        KEY idx_finance_runs_batch_status (batch_id, status),
        CONSTRAINT fk_finance_runs_batch FOREIGN KEY (batch_id)
            REFERENCES finance_sync_batches (id) ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS finance_fee_items (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        platform VARCHAR(32) NOT NULL,
        raw_primary_fee_name VARCHAR(255) NOT NULL,
        raw_secondary_fee_name VARCHAR(255) NOT NULL DEFAULT '',
        direction VARCHAR(16) NOT NULL,
        first_seen_month DATE NOT NULL,
        last_seen_month DATE NOT NULL,
        created_at DATETIME(6) NOT NULL,
        updated_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_finance_fee_item (
            platform, raw_primary_fee_name, raw_secondary_fee_name, direction
        ),
        KEY idx_finance_fee_items_seen (platform, last_seen_month),
        KEY idx_finance_fee_items_direction (direction)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS finance_transactions (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        run_id BIGINT UNSIGNED NOT NULL,
        fee_item_id BIGINT UNSIGNED NOT NULL,
        platform VARCHAR(32) NOT NULL,
        account_id VARCHAR(128) NOT NULL,
        login_account VARCHAR(128) NOT NULL,
        source_record_key VARCHAR(191) NOT NULL,
        business_date DATE NOT NULL,
        transaction_at DATETIME(6) NULL,
        raw_primary_fee_name VARCHAR(255) NOT NULL,
        raw_secondary_fee_name VARCHAR(255) NOT NULL DEFAULT '',
        direction VARCHAR(16) NOT NULL,
        income DECIMAL(20,4) NOT NULL,
        expense DECIMAL(20,4) NOT NULL,
        before_balance DECIMAL(20,4) NULL,
        after_balance DECIMAL(20,4) NULL,
        waybill_no VARCHAR(191) NULL,
        source_reference VARCHAR(191) NULL,
        remark VARCHAR(1000) NULL,
        source_payload_json JSON NOT NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_finance_transaction_run_source (run_id, source_record_key),
        KEY idx_finance_transactions_visible (platform, account_id, business_date, run_id),
        KEY idx_finance_transactions_fee (fee_item_id, business_date),
        KEY idx_finance_transactions_waybill (waybill_no),
        KEY idx_finance_transactions_direction (direction, business_date),
        CONSTRAINT fk_finance_transactions_run FOREIGN KEY (run_id)
            REFERENCES finance_sync_runs (id) ON DELETE RESTRICT,
        CONSTRAINT fk_finance_transactions_fee_item FOREIGN KEY (fee_item_id)
            REFERENCES finance_fee_items (id) ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS finance_summary_snapshots (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        run_id BIGINT UNSIGNED NOT NULL,
        platform VARCHAR(32) NOT NULL,
        account_id VARCHAR(128) NOT NULL,
        target_date DATE NOT NULL,
        raw_primary_fee_name VARCHAR(255) NOT NULL,
        raw_secondary_fee_name VARCHAR(255) NOT NULL DEFAULT '',
        direction VARCHAR(16) NOT NULL,
        income DECIMAL(20,4) NOT NULL,
        expense DECIMAL(20,4) NOT NULL,
        net_change DECIMAL(20,4) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_finance_summary_run_fee (
            run_id, raw_primary_fee_name, raw_secondary_fee_name, direction
        ),
        KEY idx_finance_summary_visible (platform, account_id, target_date, run_id),
        CONSTRAINT fk_finance_summary_run FOREIGN KEY (run_id)
            REFERENCES finance_sync_runs (id) ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS finance_fee_mappings (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        fee_item_id BIGINT UNSIGNED NOT NULL,
        direction VARCHAR(16) NOT NULL,
        fee_level VARCHAR(16) NOT NULL,
        booking_fee_name VARCHAR(255) NULL,
        effective_start_month DATE NOT NULL,
        effective_end_month DATE NULL,
        include_in_cost TINYINT(1) NOT NULL DEFAULT 0,
        mapping_status VARCHAR(16) NOT NULL DEFAULT 'bound',
        version_no INT UNSIGNED NOT NULL,
        superseded_at DATETIME(6) NULL,
        created_by VARCHAR(128) NOT NULL,
        change_reason VARCHAR(500) NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_finance_mapping_version (
            fee_item_id, effective_start_month, version_no
        ),
        KEY idx_finance_mapping_resolve (
            fee_item_id, effective_start_month, effective_end_month, superseded_at
        ),
        CONSTRAINT fk_finance_mapping_fee_item FOREIGN KEY (fee_item_id)
            REFERENCES finance_fee_items (id) ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS finance_mapping_audit_logs (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        fee_item_id BIGINT UNSIGNED NOT NULL,
        mapping_id BIGINT UNSIGNED NULL,
        action VARCHAR(32) NOT NULL,
        before_json JSON NULL,
        after_json JSON NOT NULL,
        changed_by VARCHAR(128) NOT NULL,
        change_reason VARCHAR(500) NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        KEY idx_finance_mapping_audit_fee (fee_item_id, created_at),
        CONSTRAINT fk_finance_audit_fee_item FOREIGN KEY (fee_item_id)
            REFERENCES finance_fee_items (id) ON DELETE RESTRICT,
        CONSTRAINT fk_finance_audit_mapping FOREIGN KEY (mapping_id)
            REFERENCES finance_fee_mappings (id) ON DELETE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    ALTER TABLE finance_sync_runs
        MODIFY COLUMN login_account VARCHAR(128) NULL,
        MODIFY COLUMN session_profile VARCHAR(128) NULL
    """,
)


def mysql_schema_statements() -> tuple[str, ...]:
    """Return immutable DDL statements in dependency order."""

    return MYSQL_DDL
