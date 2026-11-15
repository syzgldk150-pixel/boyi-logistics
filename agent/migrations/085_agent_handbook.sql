-- Robot agent layer P2: the business handbook ("业务手册").
--
-- People write business notes in plain words; the model compiles a note into
-- structured content (term aliases, phrase examples, ask-first / forbid rules);
-- code validates it; a super administrator reads the Chinese restatement and
-- activates it. Only ACTIVE versions of ACTIVE entries reach conversations.
-- Editing an active entry pauses it until the new version is approved.
-- Written by shared/agent_handbook_repository.py. No foreign keys to persons
-- or plugins: the handbook outlives them and validation happens at compile time.
CREATE TABLE IF NOT EXISTS agent_handbook_entries (
    entry_id CHAR(36) NOT NULL,
    title VARCHAR(100) NOT NULL,
    status VARCHAR(16) NOT NULL,
    latest_version INT UNSIGNED NOT NULL,
    active_version INT UNSIGNED NULL,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    row_version INT UNSIGNED NOT NULL DEFAULT 1,
    PRIMARY KEY (entry_id),
    KEY idx_agent_handbook_entries_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_handbook_versions (
    entry_id CHAR(36) NOT NULL,
    version INT UNSIGNED NOT NULL,
    body_text TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    compiled_json JSON NULL,
    validation_json JSON NULL,
    trial_json JSON NULL,
    acknowledged_json JSON NULL,
    ai_run_id CHAR(36) NULL,
    edited_by VARCHAR(128) NOT NULL,
    compiled_by VARCHAR(128) NULL,
    approved_by VARCHAR(128) NULL,
    approved_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (entry_id, version),
    KEY idx_agent_handbook_versions_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
