-- A login account is an access binding, not the identity of a business record.
-- Historical rows retain NULL identity until the original source is proven.
ALTER TABLE waybills
    ADD COLUMN source_scope VARCHAR(128) COLLATE utf8mb4_bin NULL,
    ADD COLUMN source_record_id VARCHAR(128) COLLATE utf8mb4_bin NULL,
    ADD COLUMN source_account_id VARCHAR(128) COLLATE utf8mb4_bin NULL,
    ADD COLUMN source_permission_scope VARCHAR(128) COLLATE utf8mb4_bin NULL,
    ADD UNIQUE KEY uq_waybill_source_identity (source, source_scope, source_record_id),
    ADD KEY idx_waybill_source_day (source, source_scope, open_date);

CREATE TABLE waybill_source_coverage (
    source VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
    source_scope VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
    permission_scope VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
    business_date DATE NOT NULL,
    account_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
    captured_at DATETIME(6) NOT NULL,
    complete_through DATETIME(6) NULL,
    final_day BOOLEAN NOT NULL DEFAULT FALSE,
    record_count INT UNSIGNED NOT NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
    PRIMARY KEY (source, source_scope, permission_scope, business_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
