-- Robot agent layer P0: one row per natural-conversation turn and one row
-- per action decision made in that turn (Feishu, WeChat, Console AI
-- assistant). Written by shared/agent_action_audit.py.
--
-- The user's words are stored only after shared redaction (phones, emails,
-- addresses, account labels hidden). Arguments are the redacted, minimized
-- arguments the model proposed, never credentials or raw platform data.
-- No foreign keys: the audit outlives persons, plugins and sessions.
CREATE TABLE IF NOT EXISTS agent_conversation_turns (
    turn_id CHAR(36) NOT NULL,
    person_id VARCHAR(64) NULL,
    actor_id VARCHAR(191) NOT NULL,
    channel VARCHAR(16) NOT NULL,
    chat_scope VARCHAR(16) NOT NULL,
    message_text VARCHAR(2000) NOT NULL,
    model VARCHAR(128) NULL,
    outcome VARCHAR(32) NOT NULL,
    tool_calls SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    duration_ms INT UNSIGNED NOT NULL DEFAULT 0,
    error_code VARCHAR(64) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (turn_id),
    KEY idx_agent_turns_time (created_at),
    KEY idx_agent_turns_outcome_time (outcome, created_at),
    KEY idx_agent_turns_actor_time (actor_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_action_decisions (
    decision_id CHAR(36) NOT NULL,
    turn_id CHAR(36) NOT NULL,
    seq SMALLINT UNSIGNED NOT NULL,
    action_id VARCHAR(191) NOT NULL,
    action_title VARCHAR(191) NULL,
    effect VARCHAR(32) NOT NULL,
    arguments_json JSON NULL,
    decision VARCHAR(16) NOT NULL,
    invocation_id CHAR(36) NULL,
    error_code VARCHAR(64) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (decision_id),
    UNIQUE KEY uk_agent_decisions_turn_seq (turn_id, seq),
    KEY idx_agent_decisions_action_time (action_id, created_at),
    KEY idx_agent_decisions_decision_time (decision, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
