-- Customer-service AI, phase 3: handling templates distilled by AI from
-- consistent cases and approved by a person (scenario tier L2).
--
-- status: draft (waiting for review) -> active (in use) | rejected;
--         active -> review (a cited knowledge document changed; not used until
--         a person confirms it) -> active; active/review -> retired.
-- knowledge_json keeps each cited document's edit time so a later change is
-- noticed. rejected_case_count stops the same scenario being re-drafted until
-- enough new cases arrive. No foreign keys; agent_db.

CREATE TABLE IF NOT EXISTS cs_templates (
    template_id CHAR(36) NOT NULL,
    scenario_id VARCHAR(32) NOT NULL,
    version INT UNSIGNED NOT NULL,
    status VARCHAR(16) NOT NULL,
    name VARCHAR(100) NOT NULL,
    conditions_json JSON NOT NULL,
    evidence_needed_json JSON NOT NULL,
    reply_template TEXT NOT NULL,
    action_text VARCHAR(100) NOT NULL,
    note VARCHAR(500) NOT NULL,
    source_case_ids_json JSON NOT NULL,
    cases_total SMALLINT UNSIGNED NOT NULL,
    cases_consistent SMALLINT UNSIGNED NOT NULL,
    knowledge_json JSON NOT NULL,
    run_id CHAR(36) NULL,
    review_reason VARCHAR(500) NULL,
    rejected_case_count INT UNSIGNED NULL,
    reviewed_by VARCHAR(128) NULL,
    reviewed_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (template_id),
    KEY idx_cs_templates_scenario (scenario_id, status, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
