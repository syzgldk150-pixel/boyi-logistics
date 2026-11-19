-- 业务手册 → 说明条目：审核工作台 (frontend-spec 52/53).
--
-- Every action on a handbook note (save, compile, trial, ambiguity answers,
-- submit for review, send back, approve, pause, resume, retire, discard a
-- version) is recorded here so the reviewer sees the operation log next to
-- the version history. The version status gains PENDING (submitted for
-- review) and VOID (a discarded version); both fit the existing VARCHAR(16)
-- column, so no column changes. Written by shared/agent_handbook_repository.py.
CREATE TABLE IF NOT EXISTS agent_handbook_events (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    entry_id CHAR(36) NOT NULL,
    version INT UNSIGNED NULL,
    action VARCHAR(16) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    detail_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    KEY idx_agent_handbook_events_entry (entry_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
