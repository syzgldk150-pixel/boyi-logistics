-- Party layer of the rebuilt finance module (P2): parties, the open-item
-- ledger (ERPNext Payment Ledger Entry) and allocations. {{finance_db}} is
-- bound by the deployment runner, which then projects the agent_db views.

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_parties (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    party_type ENUM('customer','platform','sender','supplier','employee','other') NOT NULL,
    name VARCHAR(128) NOT NULL,
    phone VARCHAR(64) NULL,
    boyi_sender_name VARCHAR(128) NULL,
    boyi_sender_phone VARCHAR(64) NULL,
    remark VARCHAR(255) NOT NULL DEFAULT '',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_parties_name (party_type, name),
    UNIQUE KEY uq_finance_parties_sender (boyi_sender_name, boyi_sender_phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_allocations (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    party_id BIGINT UNSIGNED NOT NULL,
    account_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    allocation_date DATE NOT NULL,
    method ENUM('manual','auto_waybill','daily_aggregate') NOT NULL,
    status ENUM('active','reversed') NOT NULL DEFAULT 'active',
    total_amount DECIMAL(20,4) NOT NULL,
    remark VARCHAR(255) NOT NULL DEFAULT '',
    request_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    reversed_by VARCHAR(128) NULL,
    reversed_at DATETIME(6) NULL,
    reverse_reason VARCHAR(255) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_allocations_request (request_id),
    KEY idx_finance_allocations_party (party_id, account_code, allocation_date),
    CONSTRAINT fk_finance_allocations_party FOREIGN KEY (party_id) REFERENCES {{finance_db}}.finance_parties (id),
    CONSTRAINT fk_finance_allocations_account FOREIGN KEY (account_code) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT ck_finance_allocations_amount CHECK (total_amount > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One row per GL entry on a party account (an open item, against_item_id NULL)
-- and two rows per allocation pair (against the two items they settle).
-- amount is signed in the account's normal direction: positive raises what the
-- party owes (receivable) or is owed (payable). Rows are only inserted; a
-- cancelled voucher or reversed allocation flags its rows is_cancelled=1.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_party_ledger (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    party_id BIGINT UNSIGNED NOT NULL,
    account_code VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    posting_date DATE NOT NULL,
    period_month CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    voucher_id BIGINT UNSIGNED NULL,
    gl_entry_id BIGINT UNSIGNED NULL,
    allocation_id BIGINT UNSIGNED NULL,
    against_item_id BIGINT UNSIGNED NULL,
    amount DECIMAL(20,4) NOT NULL,
    waybill_no VARCHAR(128) NULL,
    summary VARCHAR(255) NOT NULL DEFAULT '',
    is_cancelled TINYINT(1) NOT NULL DEFAULT 0,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_party_ledger_gl (gl_entry_id),
    KEY idx_finance_party_ledger_party (party_id, account_code, posting_date),
    KEY idx_finance_party_ledger_against (against_item_id),
    KEY idx_finance_party_ledger_voucher (voucher_id),
    KEY idx_finance_party_ledger_allocation (allocation_id),
    KEY idx_finance_party_ledger_waybill (waybill_no),
    CONSTRAINT fk_finance_party_ledger_party FOREIGN KEY (party_id) REFERENCES {{finance_db}}.finance_parties (id),
    CONSTRAINT fk_finance_party_ledger_account FOREIGN KEY (account_code) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT fk_finance_party_ledger_voucher FOREIGN KEY (voucher_id) REFERENCES {{finance_db}}.finance_vouchers (id),
    CONSTRAINT fk_finance_party_ledger_gl FOREIGN KEY (gl_entry_id) REFERENCES {{finance_db}}.finance_gl_entries (id),
    CONSTRAINT fk_finance_party_ledger_allocation FOREIGN KEY (allocation_id) REFERENCES {{finance_db}}.finance_allocations (id),
    CONSTRAINT fk_finance_party_ledger_against FOREIGN KEY (against_item_id) REFERENCES {{finance_db}}.finance_party_ledger (id),
    CONSTRAINT ck_finance_party_ledger_kind CHECK ((allocation_id IS NULL) = (against_item_id IS NULL)),
    CONSTRAINT ck_finance_party_ledger_amount CHECK (amount <> 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Receivable/payable leaves now require a party (the suspense account 224199
-- and payroll/tax accounts do not). Skipped for any account that already has
-- GL entries without a party, so no existing entry becomes invalid.
UPDATE {{finance_db}}.finance_accounts account
SET account.requires_party = 1, account.revision = account.revision + 1
WHERE account.code IN ('112201','112202','112203','112301','122101','122102','220201','220202','220301','224101')
  AND account.requires_party = 0
  AND NOT EXISTS (
      SELECT 1 FROM {{finance_db}}.finance_gl_entries entry
      WHERE entry.account_code = account.code AND entry.party_id IS NULL
  );

-- Voucher lines and GL entries may now name a party (kept last: plain DDL).
ALTER TABLE {{finance_db}}.finance_voucher_lines
    ADD CONSTRAINT fk_finance_voucher_lines_party FOREIGN KEY (party_id) REFERENCES {{finance_db}}.finance_parties (id);
ALTER TABLE {{finance_db}}.finance_gl_entries
    ADD CONSTRAINT fk_finance_gl_party FOREIGN KEY (party_id) REFERENCES {{finance_db}}.finance_parties (id);
