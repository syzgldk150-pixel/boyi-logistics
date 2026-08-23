ALTER TABLE scan_codes
    ADD COLUMN snapshot_date DATE NULL AFTER main_tracking;

UPDATE scan_codes
SET snapshot_date = DATE(last_seen_at)
WHERE snapshot_date IS NULL;

ALTER TABLE scan_codes
    MODIFY COLUMN snapshot_date DATE NOT NULL,
    DROP PRIMARY KEY,
    ADD PRIMARY KEY (snapshot_date, raw_code),
    ADD INDEX idx_scan_codes_raw_code (raw_code);
