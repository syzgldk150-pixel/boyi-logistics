-- Robot agent layer: 业务手册 → 计算规则 (C line, rules managed in the back office).
--
-- How a fee is computed is code (shared/handbook_fee_rules.py); every number,
-- tier, effective date and source is a row here, edited in the back office.
-- A version moves DRAFT -> PENDING (submitted) -> APPROVED (super administrator)
-- -> RETIRED. Approved versions are never edited: a change copies them into a
-- new draft. Only APPROVED versions reach calculations, chosen by the
-- waybill's billing date. Written by shared/fee_rule_repository.py.
CREATE TABLE IF NOT EXISTS agent_fee_rule_versions (
    rule_id CHAR(36) NOT NULL,
    fee_kind VARCHAR(32) NOT NULL,
    version_label VARCHAR(60) NOT NULL,
    status VARCHAR(16) NOT NULL,
    effective_from DATE NULL,
    content_json JSON NOT NULL,
    checks_json JSON NOT NULL,
    based_on CHAR(36) NULL,
    created_by VARCHAR(128) NOT NULL,
    updated_by VARCHAR(128) NOT NULL,
    submitted_by VARCHAR(128) NULL,
    submitted_at DATETIME(6) NULL,
    approved_by VARCHAR(128) NULL,
    approved_at DATETIME(6) NULL,
    retired_by VARCHAR(128) NULL,
    retired_at DATETIME(6) NULL,
    row_version INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (rule_id),
    KEY idx_agent_fee_rule_versions_kind (fee_kind, status, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_fee_rule_events (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    rule_id CHAR(36) NOT NULL,
    action VARCHAR(16) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    detail_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    KEY idx_agent_fee_rule_events_rule (rule_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- The V6.1 upstairs draft typed from the plan (方案二 §3), moved out of code. It
-- stays a draft until a person checks it against the manual and a super
-- administrator approves it with the headquarters effective date.
INSERT IGNORE INTO agent_fee_rule_versions
    (rule_id, fee_kind, version_label, status, effective_from, content_json, checks_json, created_by, updated_by)
VALUES ('0f6a1c2e-5b7d-4e3a-9c41-61f2e8d0a087', 'upstairs', 'V6.1-草稿1', 'DRAFT', NULL,
    '{"base":{"fixed":"10","per_kg":"0.12","source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"}},"elevator":{"fixed":"5","per_kg":"0.06","source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"}},"first_floor":"MANUAL","floor_tiers":[{"floor_from":2,"floor_to":2,"per_kg":"0","source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"}},{"floor_from":3,"floor_to":4,"per_kg":"0.05","source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"}},{"floor_from":5,"floor_to":6,"per_kg":"0.10","source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"}},{"floor_from":7,"floor_to":7,"per_kg":"0.15","source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"}}],"max_floor_source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"},"max_floor_without_elevator":7,"piece_limit_kg":"120","piece_limit_source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"},"piece_tiers":[{"over_kg":"50","per_piece":"10","source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"},"up_to_kg":"100"},{"over_kg":"100","per_piece":"30","source":{"document":"融辉物流网络管理手册 V6.1","link":"","location":"第五篇第五章 第 50 页"},"up_to_kg":"120"}],"reminders":["如同时产生卸货费，两者取大值，不同时收取。"]}',
    '[{"text":"数字按方案文字层读数录入，需逐格对照手册第 50–53 页原表核对。","done":false},{"text":"送到 1 楼是否收取上楼费，草稿未写明，目前按“需人工判断”。","done":false},{"text":"单件 100 公斤恰好落在“50–100”还是“100–120”档，需对照原文。","done":false},{"text":"床垫类、整托与木箱的收费表尚未录入。","done":false},{"text":"执行日期以总部通知为准，尚未填写。","done":false}]',
    'migration:087', 'migration:087');
