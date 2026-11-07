-- Period close of the rebuilt finance module (P5): one row per close/reopen
-- cycle of a month and the account balance snapshot taken at each close.
-- {{finance_db}} is bound by the deployment runner, which then re-projects
-- the agent_db views listed in LATER_FINANCE_TABLES.

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_period_closes (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    period_month CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    version INT UNSIGNED NOT NULL,
    status ENUM('closed','reopened') NOT NULL,
    closing_voucher_id BIGINT UNSIGNED NULL,
    year_end_voucher_id BIGINT UNSIGNED NULL,
    checks_json JSON NOT NULL,
    trial_balance_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    profit_loss_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    net_profit DECIMAL(20,4) NOT NULL,
    closed_by VARCHAR(128) NOT NULL,
    closed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    reopened_by VARCHAR(128) NULL,
    reopened_at DATETIME(6) NULL,
    reopen_reason VARCHAR(255) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_period_closes_version (period_month, version),
    CONSTRAINT fk_finance_period_closes_voucher FOREIGN KEY (closing_voucher_id) REFERENCES {{finance_db}}.finance_vouchers (id),
    CONSTRAINT fk_finance_period_closes_year_end FOREIGN KEY (year_end_voucher_id) REFERENCES {{finance_db}}.finance_vouchers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Leaf-account balances of one close (after the closing vouchers). Kept when
-- the month is reopened; a later close writes a new version.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_account_balances (
    close_id BIGINT UNSIGNED NOT NULL,
    account_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    opening_net DECIMAL(20,4) NOT NULL,
    period_debit DECIMAL(20,4) NOT NULL,
    period_credit DECIMAL(20,4) NOT NULL,
    closing_net DECIMAL(20,4) NOT NULL,
    PRIMARY KEY (close_id, account_code),
    CONSTRAINT fk_finance_account_balances_close FOREIGN KEY (close_id) REFERENCES {{finance_db}}.finance_period_closes (id),
    CONSTRAINT fk_finance_account_balances_account FOREIGN KEY (account_code) REFERENCES {{finance_db}}.finance_accounts (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Statement line of each first-level account (小企业会计准则 report lines).
-- NULL means the account is reported under its root type's "other" line.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_report_lines (
    account_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    statement ENUM('balance_sheet','profit_loss') NOT NULL,
    line_key VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    PRIMARY KEY (account_code),
    CONSTRAINT fk_finance_report_lines_account FOREIGN KEY (account_code) REFERENCES {{finance_db}}.finance_accounts (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO {{finance_db}}.finance_report_lines (account_code, statement, line_key) VALUES
    ('1001', 'balance_sheet', 'cash'),
    ('1002', 'balance_sheet', 'cash'),
    ('1012', 'balance_sheet', 'cash'),
    ('1122', 'balance_sheet', 'receivables'),
    ('1123', 'balance_sheet', 'prepayments'),
    ('1221', 'balance_sheet', 'other_receivables'),
    ('2202', 'balance_sheet', 'payables'),
    ('2203', 'balance_sheet', 'advances_received'),
    ('2211', 'balance_sheet', 'payroll_payable'),
    ('2221', 'balance_sheet', 'taxes_payable'),
    ('2241', 'balance_sheet', 'other_payables'),
    ('3001', 'balance_sheet', 'paid_in_capital'),
    ('3103', 'balance_sheet', 'retained_earnings'),
    ('3104', 'balance_sheet', 'retained_earnings'),
    ('5001', 'profit_loss', 'revenue'),
    ('5051', 'profit_loss', 'revenue'),
    ('5401', 'profit_loss', 'cost'),
    ('5601', 'profit_loss', 'selling_expense'),
    ('5602', 'profit_loss', 'admin_expense'),
    ('5603', 'profit_loss', 'finance_expense'),
    ('5301', 'profit_loss', 'non_operating_income'),
    ('5711', 'profit_loss', 'non_operating_expense');
