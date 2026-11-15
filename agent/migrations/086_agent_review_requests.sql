-- Robot agent layer P4: "需他人复核" review requests.
--
-- A person whose agent level for a write capability is "review" asks for a
-- write; instead of running, the plan is stored here, super administrators are
-- notified, and one of them approves or rejects it in the Console or by bot.
-- Approval re-checks the requester and the plan and runs it exactly once.
-- Expired requests are never retried. This is a notification record, not a
-- work queue: nothing picks requests up on its own.
-- Written by shared/agent_review_repository.py.
CREATE TABLE IF NOT EXISTS agent_review_requests (
    request_id CHAR(36) NOT NULL,
    short_code CHAR(6) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    title VARCHAR(191) NOT NULL,
    summary VARCHAR(1000) NOT NULL,
    capability VARCHAR(64) NOT NULL,
    plan_json JSON NOT NULL,
    requester_json JSON NOT NULL,
    requester_label VARCHAR(191) NOT NULL,
    reply_chat_id VARCHAR(191) NULL,
    expires_at DATETIME(6) NOT NULL,
    decided_by VARCHAR(191) NULL,
    decided_at DATETIME(6) NULL,
    decision_note VARCHAR(500) NULL,
    result_text VARCHAR(1000) NULL,
    invocation_id CHAR(36) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (request_id),
    UNIQUE KEY uk_agent_review_short_code (short_code),
    KEY idx_agent_review_status_time (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
