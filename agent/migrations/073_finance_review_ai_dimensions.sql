-- Finance follow-ups after the rebuild: AI suggestions on suspense reviews
-- (advice only; a person still confirms the rule) and default site / business
-- line dimensions for source and Boyi waybill vouchers, so the profit and loss
-- statement can be split by them. Plain DDL; the runner applies it once and
-- then re-projects the agent_db views.

ALTER TABLE {{finance_db}}.finance_reviews
    ADD COLUMN suggestion_json JSON NULL AFTER resolved_at,
    ADD COLUMN suggested_by_model VARCHAR(128) NULL AFTER suggestion_json,
    ADD COLUMN suggested_at DATETIME(6) NULL AFTER suggested_by_model;

ALTER TABLE {{finance_db}}.finance_sources
    ADD COLUMN site_code VARCHAR(64) NULL AFTER fund_account,
    ADD COLUMN business_line VARCHAR(64) NULL AFTER site_code;

ALTER TABLE {{finance_db}}.finance_doc_posting_rules
    ADD COLUMN site_code VARCHAR(64) NULL AFTER fixed_party_id,
    ADD COLUMN business_line VARCHAR(64) NULL AFTER site_code;
