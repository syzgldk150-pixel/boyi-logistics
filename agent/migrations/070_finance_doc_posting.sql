-- Boyi waybill posting (P3): every Boyi waybill is confirmed by a person
-- before it is posted; the rule per payment method decides the receivable,
-- income accounts and where the party comes from. {{finance_db}} is bound by
-- the deployment runner, which then re-projects the agent_db views.

CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_doc_posting_rules (
    payment_method VARCHAR(16) NOT NULL,
    receivable_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    freight_income_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    transfer_income_account VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    party_mode ENUM('fixed','sender_customer','linked_platform','choose') NOT NULL,
    fixed_party_id BIGINT UNSIGNED NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    updated_by VARCHAR(128) NOT NULL DEFAULT '',
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (payment_method),
    CONSTRAINT fk_finance_doc_rules_receivable FOREIGN KEY (receivable_account) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT fk_finance_doc_rules_freight FOREIGN KEY (freight_income_account) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT fk_finance_doc_rules_transfer FOREIGN KEY (transfer_income_account) REFERENCES {{finance_db}}.finance_accounts (code),
    CONSTRAINT fk_finance_doc_rules_party FOREIGN KEY (fixed_party_id) REFERENCES {{finance_db}}.finance_parties (id),
    CONSTRAINT ck_finance_doc_rules_fixed CHECK ((party_mode = 'fixed') = (fixed_party_id IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- The current confirmation of one Boyi waybill (boyi_waybills.id). Earlier
-- vouchers of the same waybill stay in finance_vouchers (ref_type/ref_id) and
-- were cancelled or reversed when the waybill was confirmed again.
CREATE TABLE IF NOT EXISTS {{finance_db}}.finance_doc_postings (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doc_type ENUM('boyi_waybill') NOT NULL,
    doc_id BIGINT NOT NULL,
    waybill_no VARCHAR(128) NOT NULL,
    status ENUM('posted','zero','reversed') NOT NULL,
    fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    payment_method VARCHAR(16) NOT NULL,
    freight_fee DECIMAL(20,4) NOT NULL,
    transfer_fee DECIMAL(20,4) NOT NULL,
    total_amount DECIMAL(20,4) NOT NULL,
    party_id BIGINT UNSIGNED NULL,
    voucher_id BIGINT UNSIGNED NULL,
    rule_revision INT UNSIGNED NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 1,
    confirmed_by VARCHAR(128) NOT NULL,
    confirmed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_finance_doc_postings_doc (doc_type, doc_id),
    KEY idx_finance_doc_postings_waybill (waybill_no),
    CONSTRAINT fk_finance_doc_postings_party FOREIGN KEY (party_id) REFERENCES {{finance_db}}.finance_parties (id),
    CONSTRAINT fk_finance_doc_postings_voucher FOREIGN KEY (voucher_id) REFERENCES {{finance_db}}.finance_vouchers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Platforms are matched to boyi_waybills.linked_platform by an exact code.
ALTER TABLE {{finance_db}}.finance_parties
    ADD COLUMN platform_code VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NULL AFTER boyi_sender_phone,
    ADD UNIQUE KEY uq_finance_parties_platform (platform_code);

INSERT IGNORE INTO {{finance_db}}.finance_parties (party_type, name, platform_code, remark, created_by) VALUES
    ('platform', '融辉', 'ronghui', '到付待收按运单绑定的平台记账', 'migration_070'),
    ('platform', '韵达', 'yunda', '到付待收按运单绑定的平台记账', 'migration_070'),
    ('platform', '百世', 'best', '到付待收按运单绑定的平台记账', 'migration_070'),
    ('customer', '寄付散客', NULL, '寄付现收的汇总往来单位，按日汇总核销', 'migration_070');

UPDATE {{finance_db}}.finance_parties party
SET party.platform_code = CASE party.name WHEN '融辉' THEN 'ronghui' WHEN '韵达' THEN 'yunda' WHEN '百世' THEN 'best' END
WHERE party.party_type = 'platform' AND party.platform_code IS NULL AND party.name IN ('融辉', '韵达', '百世');

INSERT IGNORE INTO {{finance_db}}.finance_doc_posting_rules
    (payment_method, receivable_account, freight_income_account, transfer_income_account, party_mode, fixed_party_id, updated_by)
SELECT '寄付', '112203', '500101', '500102', 'fixed', party.id, 'migration_070'
FROM {{finance_db}}.finance_parties party WHERE party.party_type = 'customer' AND party.name = '寄付散客';

INSERT IGNORE INTO {{finance_db}}.finance_doc_posting_rules
    (payment_method, receivable_account, freight_income_account, transfer_income_account, party_mode, fixed_party_id, updated_by) VALUES
    ('到付', '112202', '500101', '500102', 'linked_platform', NULL, 'migration_070'),
    ('月结', '112201', '500101', '500102', 'sender_customer', NULL, 'migration_070');
