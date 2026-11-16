-- Robot agent layer: 业务手册 → 知识来源 (收尾第 6 步).
--
-- The last seen state of every document in the authorized knowledge spaces,
-- and reminders when a document cited by a fee rule changes or a new
-- fee-looking document appears. Reminders only ask a person to check the
-- rules; nothing is changed automatically. Written by
-- shared/knowledge_watch_repository.py.
CREATE TABLE IF NOT EXISTS agent_knowledge_documents (
    node_token VARCHAR(160) NOT NULL,
    space_name VARCHAR(100) NOT NULL,
    title VARCHAR(200) NOT NULL,
    obj_type VARCHAR(32) NOT NULL,
    edit_time VARCHAR(40) NOT NULL,
    first_seen_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_seen_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    present TINYINT(1) NOT NULL DEFAULT 1,
    PRIMARY KEY (node_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_knowledge_reminders (
    reminder_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    node_token VARCHAR(160) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    title VARCHAR(200) NOT NULL,
    detail VARCHAR(500) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    detected_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    checked_by VARCHAR(128) NULL,
    checked_at DATETIME(6) NULL,
    PRIMARY KEY (reminder_id),
    KEY idx_agent_knowledge_reminders_status (status, detected_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
