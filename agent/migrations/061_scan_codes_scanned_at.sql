-- Actual Ronghui scan time (source SCAN_DATE) for each scanned code.
-- last_seen_at keeps its snapshot meaning; historical rows stay NULL because
-- their real scan times were never captured and must not be guessed.
ALTER TABLE scan_codes
    ADD COLUMN scanned_at DATETIME NULL AFTER last_seen_at,
    ADD INDEX idx_scan_codes_scanned_at (scanned_at);
