-- Customer-service AI, phase 1 (advice + case library). agent_db, no foreign
-- keys to published problem rows: cases and advice outlive a publication.
--
-- cs_scenarios: the closed set of problem scenarios the model may choose from
--   and how far each has matured (L0 new -> L1 AI advice; L2/L3 come later).
--   Locked scenarios (damage, loss/compensation) never become automatic.
-- cs_suggestions: one row per advice run (suggestion_id = ai_task_runs.run_id).
-- cs_cases: one row per reply a person actually sent, the learning record.

CREATE TABLE IF NOT EXISTS cs_scenarios (
    scenario_id VARCHAR(32) NOT NULL,
    name VARCHAR(64) NOT NULL,
    hint VARCHAR(255) NOT NULL,
    sort_order SMALLINT UNSIGNED NOT NULL,
    tier VARCHAR(4) NOT NULL DEFAULT 'L0',
    locked TINYINT(1) NOT NULL DEFAULT 0,
    case_count INT UNSIGNED NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (scenario_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO cs_scenarios (scenario_id, name, hint, sort_order, locked) VALUES
    ('urge', '催件 / 查件', '客户问件到哪了、什么时候到', 10, 0),
    ('undelivered', '未派送 / 滞留', '到站后长时间未派送或轨迹长时间不更新', 20, 0),
    ('sign_dispute', '签收疑问', '显示已签收但客户称未收到，或代收、签收人有疑问', 30, 0),
    ('address', '地址 / 电话错误', '地址不详、电话错误或联系不上收件人', 40, 0),
    ('reroute', '改址 / 拦截 / 退回', '客户要求改地址、拦截或退回', 50, 0),
    ('modify_info', '修改收件信息', '客户要求修改收件电话、收件人等信息', 60, 0),
    ('damage', '破损', '外包装或货物破损、受潮、变形', 70, 1),
    ('lost', '丢件 / 赔付', '找不到件、少件或要求赔偿', 80, 1),
    ('other', '其他', '不属于以上任何一类', 90, 0);

CREATE TABLE IF NOT EXISTS cs_suggestions (
    suggestion_id CHAR(36) NOT NULL,
    source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    external_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL,
    source_direction VARCHAR(32) NOT NULL,
    waybill_no VARCHAR(100) NOT NULL,
    scenario_id VARCHAR(32) NOT NULL,
    tier VARCHAR(4) NOT NULL,
    confidence DECIMAL(3,2) NOT NULL,
    reply_text TEXT NULL,
    reason VARCHAR(500) NOT NULL,
    evidence_json JSON NOT NULL,
    knowledge_json JSON NOT NULL,
    actor_id VARCHAR(128) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (suggestion_id),
    KEY idx_cs_suggestions_problem (source_id, external_id, source_direction, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cs_cases (
    case_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id CHAR(36) NOT NULL,
    source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    external_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL,
    source_direction VARCHAR(32) NOT NULL,
    platform VARCHAR(16) NOT NULL,
    waybill_no VARCHAR(100) NOT NULL,
    problem_type VARCHAR(100) NOT NULL,
    problem_text TEXT NOT NULL,
    scenario_id VARCHAR(32) NULL,
    suggestion_id CHAR(36) NULL,
    suggestion_use VARCHAR(16) NOT NULL,
    reply_text TEXT NOT NULL,
    prob_status VARCHAR(32) NOT NULL,
    evidence_json JSON NULL,
    actor_id VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (case_id),
    UNIQUE KEY uq_cs_cases_request (request_id),
    KEY idx_cs_cases_scenario (scenario_id, created_at),
    KEY idx_cs_cases_problem (source_id, external_id, source_direction)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
