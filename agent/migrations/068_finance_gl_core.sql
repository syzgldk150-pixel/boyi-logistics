-- General-ledger core of the rebuilt finance module (P1).
-- Chart of accounts (Small Enterprise Accounting Standards seed), vouchers and
-- their lines, the append-only GL, settings, periods, voucher numbering and the
-- idempotent event log. {{finance_db}} is bound by the deployment runner, which
-- then projects the agent_db INVOKER views listed in LATER_FINANCE_TABLES.

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_accounts (
    code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    name VARCHAR(64) NOT NULL,
    parent_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
    root_type ENUM('asset','liability','equity','cost','profit_loss') NOT NULL,
    account_type ENUM('cash','bank','fund','transit','receivable','payable','suspense','income','cost','expense','equity','other') NOT NULL,
    normal_balance ENUM('debit','credit') NOT NULL,
    is_group TINYINT(1) NOT NULL DEFAULT 0,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    frozen TINYINT(1) NOT NULL DEFAULT 0,
    requires_party TINYINT(1) NOT NULL DEFAULT 0,
    requires_waybill TINYINT(1) NOT NULL DEFAULT 0,
    cash_flow_category ENUM('operating','investing','financing') NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (code),
    KEY idx_finance_accounts_parent (parent_code),
    CONSTRAINT fk_finance_accounts_parent FOREIGN KEY (parent_code) REFERENCES {{finance_db}}.finance_accounts (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_settings (
    id TINYINT UNSIGNED NOT NULL,
    start_month CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NULL,
    suspense_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    transit_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    current_profit_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    retained_earnings_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    updated_by VARCHAR(128) NOT NULL DEFAULT '',
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    CONSTRAINT ck_finance_settings_singleton CHECK (id = 1),
    CONSTRAINT fk_finance_settings_suspense FOREIGN KEY (suspense_account) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT fk_finance_settings_transit FOREIGN KEY (transit_account) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT fk_finance_settings_profit FOREIGN KEY (current_profit_account) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT fk_finance_settings_retained FOREIGN KEY (retained_earnings_account) REFERENCES {{finance_db}}.finance_accounts (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_periods (
    period_month CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    status ENUM('open','closed') NOT NULL DEFAULT 'open',
    version INT UNSIGNED NOT NULL DEFAULT 1,
    updated_by VARCHAR(128) NOT NULL DEFAULT '',
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (period_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_voucher_sequences (
    word VARCHAR(8) NOT NULL,
    period_month CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    last_no INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (word, period_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_vouchers (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    voucher_no VARCHAR(32) NULL,
    voucher_type ENUM('manual','opening','business_doc','source_line','settlement','reversal','closing') NOT NULL,
    status ENUM('draft','submitted','cancelled') NOT NULL DEFAULT 'draft',
    posting_date DATE NOT NULL,
    period_month CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    summary VARCHAR(255) NOT NULL DEFAULT '',
    ref_type VARCHAR(64) NULL,
    ref_id VARCHAR(128) NULL,
    reversal_of BIGINT UNSIGNED NULL,
    total_debit DECIMAL(20,4) NOT NULL DEFAULT 0,
    total_credit DECIMAL(20,4) NOT NULL DEFAULT 0,
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    submitted_by VARCHAR(128) NULL,
    submitted_at DATETIME(6) NULL,
    cancelled_by VARCHAR(128) NULL,
    cancelled_at DATETIME(6) NULL,
    cancel_reason VARCHAR(255) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_vouchers_no (voucher_no),
    UNIQUE KEY uq_finance_vouchers_request (request_id),
    KEY idx_finance_vouchers_period (period_month, status),
    KEY idx_finance_vouchers_ref (ref_type, ref_id),
    CONSTRAINT fk_finance_vouchers_reversal FOREIGN KEY (reversal_of) REFERENCES {{finance_db}}.finance_vouchers (id),
    CONSTRAINT ck_finance_vouchers_totals CHECK (total_debit >= 0 AND total_credit >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_voucher_lines (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    voucher_id BIGINT UNSIGNED NOT NULL,
    line_no SMALLINT UNSIGNED NOT NULL,
    account_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    debit DECIMAL(20,4) NOT NULL DEFAULT 0,
    credit DECIMAL(20,4) NOT NULL DEFAULT 0,
    summary VARCHAR(255) NOT NULL DEFAULT '',
    site_code VARCHAR(64) NULL,
    source_id VARCHAR(64) NULL,
    business_line VARCHAR(64) NULL,
    fee_level ENUM('waybill','operation') NULL,
    waybill_no VARCHAR(128) NULL,
    party_id BIGINT UNSIGNED NULL,
    source_line_id BIGINT UNSIGNED NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_voucher_lines_no (voucher_id, line_no),
    KEY idx_finance_voucher_lines_account (account_code),
    CONSTRAINT fk_finance_voucher_lines_voucher FOREIGN KEY (voucher_id) REFERENCES {{finance_db}}.finance_vouchers (id) ON DELETE CASCADE,
    CONSTRAINT fk_finance_voucher_lines_account FOREIGN KEY (account_code) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT ck_finance_voucher_lines_side CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Append-only: rows are only inserted; cancellation inserts reversal rows and
-- flags both sides is_cancelled=1. No other column is ever updated.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_gl_entries (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    voucher_id BIGINT UNSIGNED NOT NULL,
    voucher_line_id BIGINT UNSIGNED NULL,
    entry_kind ENUM('original','reversal') NOT NULL,
    posting_date DATE NOT NULL,
    period_month CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    account_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    debit DECIMAL(20,4) NOT NULL DEFAULT 0,
    credit DECIMAL(20,4) NOT NULL DEFAULT 0,
    summary VARCHAR(255) NOT NULL DEFAULT '',
    site_code VARCHAR(64) NULL,
    source_id VARCHAR(64) NULL,
    business_line VARCHAR(64) NULL,
    fee_level ENUM('waybill','operation') NULL,
    waybill_no VARCHAR(128) NULL,
    party_id BIGINT UNSIGNED NULL,
    source_line_id BIGINT UNSIGNED NULL,
    is_cancelled TINYINT(1) NOT NULL DEFAULT 0,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_finance_gl_account_period (account_code, period_month),
    KEY idx_finance_gl_period (period_month, posting_date),
    KEY idx_finance_gl_voucher (voucher_id),
    KEY idx_finance_gl_waybill (waybill_no),
    KEY idx_finance_gl_party (party_id),
    KEY idx_finance_gl_source_line (source_line_id),
    CONSTRAINT fk_finance_gl_voucher FOREIGN KEY (voucher_id) REFERENCES {{finance_db}}.finance_vouchers (id),
    CONSTRAINT fk_finance_gl_account FOREIGN KEY (account_code) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT ck_finance_gl_side CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    request_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    action VARCHAR(48) NOT NULL,
    target_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_name VARCHAR(128) NOT NULL DEFAULT '',
    result_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_events_request (request_id),
    KEY idx_finance_events_target (target_type, target_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Default chart of accounts (小企业会计准则). Second-level accounts are
-- suggestions the super administrator may extend or deactivate.
INSERT IGNORE INTO {{finance_db}}.finance_accounts
    (code, name, parent_code, root_type, account_type, normal_balance, is_group, cash_flow_category) VALUES
    ('1001', '库存现金', NULL, 'asset', 'cash', 'debit', 0, NULL),
    ('1002', '银行存款', NULL, 'asset', 'bank', 'debit', 0, NULL),
    ('1012', '其他货币资金', NULL, 'asset', 'fund', 'debit', 1, NULL),
    ('1122', '应收账款', NULL, 'asset', 'receivable', 'debit', 1, 'operating'),
    ('1123', '预付账款', NULL, 'asset', 'receivable', 'debit', 1, 'operating'),
    ('1221', '其他应收款', NULL, 'asset', 'receivable', 'debit', 1, 'operating'),
    ('2202', '应付账款', NULL, 'liability', 'payable', 'credit', 1, 'operating'),
    ('2203', '预收账款', NULL, 'liability', 'payable', 'credit', 1, 'operating'),
    ('2211', '应付职工薪酬', NULL, 'liability', 'payable', 'credit', 0, 'operating'),
    ('2221', '应交税费', NULL, 'liability', 'payable', 'credit', 0, 'operating'),
    ('2241', '其他应付款', NULL, 'liability', 'payable', 'credit', 1, 'operating'),
    ('3001', '实收资本', NULL, 'equity', 'equity', 'credit', 0, 'financing'),
    ('3103', '本年利润', NULL, 'equity', 'equity', 'credit', 0, NULL),
    ('3104', '利润分配', NULL, 'equity', 'equity', 'credit', 1, 'financing'),
    ('5001', '主营业务收入', NULL, 'profit_loss', 'income', 'credit', 1, 'operating'),
    ('5051', '其他业务收入', NULL, 'profit_loss', 'income', 'credit', 1, 'operating'),
    ('5301', '营业外收入', NULL, 'profit_loss', 'income', 'credit', 0, 'operating'),
    ('5401', '主营业务成本', NULL, 'profit_loss', 'cost', 'debit', 1, 'operating'),
    ('5601', '销售费用', NULL, 'profit_loss', 'expense', 'debit', 0, 'operating'),
    ('5602', '管理费用', NULL, 'profit_loss', 'expense', 'debit', 1, 'operating'),
    ('5603', '财务费用', NULL, 'profit_loss', 'expense', 'debit', 1, 'operating'),
    ('5711', '营业外支出', NULL, 'profit_loss', 'expense', 'debit', 1, 'operating');

INSERT IGNORE INTO {{finance_db}}.finance_accounts
    (code, name, parent_code, root_type, account_type, normal_balance, is_group, cash_flow_category) VALUES
    ('101201', '微信', '1012', 'asset', 'fund', 'debit', 0, NULL),
    ('101202', '支付宝', '1012', 'asset', 'fund', 'debit', 0, NULL),
    ('101203', '融辉主发货', '1012', 'asset', 'fund', 'debit', 0, NULL),
    ('101204', '融辉主派件', '1012', 'asset', 'fund', 'debit', 0, NULL),
    ('101205', '融辉自提部', '1012', 'asset', 'fund', 'debit', 0, NULL),
    ('101299', '资金在途', '1012', 'asset', 'transit', 'debit', 0, NULL),
    ('112201', '月结客户', '1122', 'asset', 'receivable', 'debit', 0, 'operating'),
    ('112202', '到付待收', '1122', 'asset', 'receivable', 'debit', 0, 'operating'),
    ('112203', '寄付待收', '1122', 'asset', 'receivable', 'debit', 0, 'operating'),
    ('112301', '平台预存', '1123', 'asset', 'receivable', 'debit', 0, 'operating'),
    ('122101', '押金保证金', '1221', 'asset', 'receivable', 'debit', 0, 'operating'),
    ('122102', '员工借款', '1221', 'asset', 'receivable', 'debit', 0, 'operating'),
    ('220201', '平台', '2202', 'liability', 'payable', 'credit', 0, 'operating'),
    ('220202', '供应商', '2202', 'liability', 'payable', 'credit', 0, 'operating'),
    ('220301', '客户预存', '2203', 'liability', 'payable', 'credit', 0, 'operating'),
    ('224101', '代收货款', '2241', 'liability', 'payable', 'credit', 0, 'operating'),
    ('224199', '待分类款项（暂记）', '2241', 'liability', 'suspense', 'credit', 0, 'operating'),
    ('310401', '未分配利润', '3104', 'equity', 'equity', 'credit', 0, 'financing'),
    ('500101', '运费', '5001', 'profit_loss', 'income', 'credit', 0, 'operating'),
    ('500102', '中转费', '5001', 'profit_loss', 'income', 'credit', 0, 'operating'),
    ('500103', '派送收入', '5001', 'profit_loss', 'income', 'credit', 0, 'operating'),
    ('500104', '到付手续费', '5001', 'profit_loss', 'income', 'credit', 0, 'operating'),
    ('505101', '代收货款手续费', '5051', 'profit_loss', 'income', 'credit', 0, 'operating'),
    ('505102', '平台补贴', '5051', 'profit_loss', 'income', 'credit', 0, 'operating'),
    ('540101', '平台中转费', '5401', 'profit_loss', 'cost', 'debit', 0, 'operating'),
    ('540102', '平台派送费', '5401', 'profit_loss', 'cost', 'debit', 0, 'operating'),
    ('540103', '面单', '5401', 'profit_loss', 'cost', 'debit', 0, 'operating'),
    ('540104', '保险', '5401', 'profit_loss', 'cost', 'debit', 0, 'operating'),
    ('540105', '短信', '5401', 'profit_loss', 'cost', 'debit', 0, 'operating'),
    ('560201', '工资', '5602', 'profit_loss', 'expense', 'debit', 0, 'operating'),
    ('560202', '房租', '5602', 'profit_loss', 'expense', 'debit', 0, 'operating'),
    ('560203', '车辆', '5602', 'profit_loss', 'expense', 'debit', 0, 'operating'),
    ('560204', '通讯', '5602', 'profit_loss', 'expense', 'debit', 0, 'operating'),
    ('560205', '办公', '5602', 'profit_loss', 'expense', 'debit', 0, 'operating'),
    ('560301', '手续费', '5603', 'profit_loss', 'expense', 'debit', 0, 'operating'),
    ('560302', '利息', '5603', 'profit_loss', 'expense', 'debit', 0, 'operating'),
    ('571101', '罚款', '5711', 'profit_loss', 'expense', 'debit', 0, 'operating');

INSERT IGNORE INTO {{finance_db}}.finance_settings
    (id, start_month, suspense_account, transit_account, current_profit_account, retained_earnings_account, updated_by)
    VALUES (1, NULL, '224199', '101299', '3103', '310401', 'migration_068');
