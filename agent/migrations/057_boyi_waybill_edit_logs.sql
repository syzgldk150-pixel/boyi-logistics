-- Edits of Boyi manual waybills from the Console query page: one immutable row
-- per saved edit request, with each changed field's previous and new value.
-- {{waybill_db}} is bound by the deployment runner, which then projects the
-- agent_db view the Console reads and writes through.
CREATE TABLE IF NOT EXISTS {{waybill_db}}.boyi_waybill_edit_logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    waybill_id BIGINT NOT NULL,
    waybill_no VARCHAR(128) NOT NULL,
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_name VARCHAR(128) NOT NULL DEFAULT '',
    changes_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_boyi_waybill_edit_request (request_id),
    KEY idx_boyi_waybill_edit_waybill (waybill_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
