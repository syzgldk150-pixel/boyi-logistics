-- Customer-service AI, phase 4: automatic replies for templates a person has
-- explicitly authorized (scenario tier L3).
--
-- cs_templates.auto_enabled: set only by a person on an active template whose
--   recent advice was adopted unchanged often enough; cleared by a person, by
--   a flagged auto reply, by the customer asking again, or when the template
--   leaves the active state.
-- cs_auto_replies: at most one automatic reply per problem, ever (primary key),
--   written before the platform call so a retry can never reply twice.
--   status: started -> confirmed (read back on the platform) | unknown
--   (sent but not confirmed) | skipped (the platform already had a reply).

ALTER TABLE cs_templates
    ADD COLUMN auto_enabled TINYINT(1) NOT NULL DEFAULT 0 AFTER status,
    ADD COLUMN auto_changed_by VARCHAR(128) NULL AFTER auto_enabled,
    ADD COLUMN auto_changed_at DATETIME(6) NULL AFTER auto_changed_by,
    ADD COLUMN auto_reason VARCHAR(500) NULL AFTER auto_changed_at;

CREATE TABLE IF NOT EXISTS cs_auto_replies (
    source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    external_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL,
    source_direction VARCHAR(32) NOT NULL,
    suggestion_id CHAR(36) NOT NULL,
    template_id CHAR(36) NOT NULL,
    platform VARCHAR(16) NOT NULL,
    waybill_no VARCHAR(100) NOT NULL,
    reply_text TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    flagged TINYINT(1) NOT NULL DEFAULT 0,
    flagged_by VARCHAR(128) NULL,
    flagged_reason VARCHAR(500) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (source_id, external_id, source_direction),
    KEY idx_cs_auto_replies_time (created_at),
    KEY idx_cs_auto_replies_template (template_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
