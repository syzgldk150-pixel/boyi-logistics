-- 财务 · 派件账单（邵阳大祥S站，2026-11-24）。账单行由每日到货表（arrival_stat_*）与
-- 融辉结算明细即时组成，不在这里存平台数据；这里只存人做过的事：派送费规则版本、
-- 每单的增值费核算与确认。确认时才把当时的融辉金额、结算重量和派送费一起记下，
-- 供下一期“按司机结算、进总账”直接使用。{{finance_db}} is bound by the deployment
-- runner, which then re-projects the agent_db views.

-- 派送费规则：每票起步价＋超重加价，按到货表日期取“执行日期不晚于该日”的最新版本。
-- content_json 是由表单生成的 shared.fee_engine 规则内容，计算与业务手册共用同一引擎。
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_delivery_fee_rules (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    effective_from DATE NOT NULL,
    start_price DECIMAL(20,4) NOT NULL,
    base_weight DECIMAL(20,4) NOT NULL,
    extra_price DECIMAL(20,4) NOT NULL,
    rounding ENUM('exact','ceil_kg') NOT NULL,
    content_json JSON NOT NULL,
    status ENUM('active','retired') NOT NULL DEFAULT 'active',
    note VARCHAR(200) NULL,
    active_from DATE AS (IF(status = 'active', effective_from, NULL)) STORED,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    retired_by VARCHAR(128) NULL,
    retired_at DATETIME(6) NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_delivery_fee_rules_active (active_from),
    KEY idx_finance_delivery_fee_rules_status (status, effective_from),
    CONSTRAINT ck_finance_delivery_fee_rules_amounts CHECK (start_price >= 0 AND base_weight >= 0 AND extra_price >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 一单的派件账单记录：保存增值费（status=open）或确认后（status=confirmed）才有这一行。
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_delivery_bill_items (
    tracking_number VARCHAR(64) NOT NULL,
    arrival_date DATE NOT NULL,
    status ENUM('open','confirmed') NOT NULL DEFAULT 'open',
    billing_date DATE NULL,
    fee_kinds_json JSON NULL,
    values_json JSON NULL,
    manual_json JSON NULL,
    fee_result_json JSON NULL,
    value_added_total DECIMAL(20,4) NOT NULL DEFAULT 0,
    fees_updated_by VARCHAR(128) NULL,
    fees_updated_at DATETIME(6) NULL,
    ronghui_amount DECIMAL(20,4) NULL,
    ronghui_detail_json JSON NULL,
    settlement_weight DECIMAL(20,4) NULL,
    pieces INT NULL,
    delivery_fee DECIMAL(20,4) NULL,
    delivery_rule_id BIGINT UNSIGNED NULL,
    delivery_steps_json JSON NULL,
    payable_total DECIMAL(20,4) NULL,
    confirmed_by VARCHAR(128) NULL,
    confirmed_at DATETIME(6) NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (tracking_number),
    KEY idx_finance_delivery_bill_items_date (arrival_date),
    KEY idx_finance_delivery_bill_items_status (status, arrival_date),
    CONSTRAINT fk_finance_delivery_bill_items_rule FOREIGN KEY (delivery_rule_id)
        REFERENCES {{finance_db}}.finance_delivery_fee_rules (id),
    CONSTRAINT ck_finance_delivery_bill_items_confirmed CHECK (
        status = 'open' OR (delivery_fee IS NOT NULL AND delivery_rule_id IS NOT NULL AND payable_total IS NOT NULL
                            AND ronghui_amount IS NOT NULL AND confirmed_at IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 增值费明细：每次保存整组替换，金额来自业务手册已生效规则（需协商/实报实销等由人填写）。
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_delivery_bill_fee_lines (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    tracking_number VARCHAR(64) NOT NULL,
    position SMALLINT UNSIGNED NOT NULL,
    fee_kind VARCHAR(64) NOT NULL,
    fee_name VARCHAR(40) NOT NULL,
    rule_id CHAR(36) NULL,
    version_label VARCHAR(160) NOT NULL DEFAULT '',
    outcome VARCHAR(16) NOT NULL,
    system_amount DECIMAL(20,4) NULL,
    amount DECIMAL(20,4) NULL,
    counted TINYINT(1) NOT NULL,
    note VARCHAR(400) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_delivery_bill_fee_lines (tracking_number, position),
    CONSTRAINT fk_finance_delivery_bill_fee_lines_item FOREIGN KEY (tracking_number)
        REFERENCES {{finance_db}}.finance_delivery_bill_items (tracking_number) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 单行设置：融辉结算明细比到货表结束日往后多查的天数；计入“融辉结算金额”的结算类型
-- 默认是以“付”开头的全部（融辉付给网点的派件收入），include/exclude 是例外。
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_delivery_bill_settings (
    id TINYINT UNSIGNED NOT NULL,
    lookahead_days SMALLINT UNSIGNED NOT NULL,
    include_types_json JSON NOT NULL,
    exclude_types_json JSON NOT NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    updated_by VARCHAR(128) NOT NULL DEFAULT '',
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT ck_finance_delivery_bill_settings_singleton CHECK (id = 1),
    CONSTRAINT ck_finance_delivery_bill_settings_days CHECK (lookahead_days BETWEEN 0 AND 31)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO {{finance_db}}.finance_delivery_bill_settings
    (id, lookahead_days, include_types_json, exclude_types_json, updated_by)
VALUES (1, 10, JSON_ARRAY(), JSON_ARRAY(), 'migration_093');
