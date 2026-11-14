-- The access code of a share link, encrypted with a key derived from the
-- deployment's link secret, so the Console can copy it together with the link.
-- The keyed digest in access_code_sha256 stays the only thing the public share
-- service compares. Links created before this migration keep a NULL value.
-- {{waybill_db}} is bound by the deployment runner, which then re-projects the
-- agent_db view. Plain DDL: the runner applies it once.
ALTER TABLE {{waybill_db}}.receipt_shares
    ADD COLUMN access_code_sealed VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL AFTER access_code_sha256;
