-- The customer's delivery-note number(s) of each shared picture, read from the
-- waybill remark at refresh time ("1001-2600000001", several joined by "_"),
-- for the {发货单号} file-name field. shipment_seq/shipment_of number the
-- pictures of one kind under one delivery note, so names stay unique when a
-- template carries {发货单号} without {运单号}. Rows are rebuilt by every
-- refresh, so older rows simply wait for the next one.
-- {{waybill_db}} is bound by the deployment runner, which then re-projects the
-- agent_db view. Plain DDL: the runner applies it once.
ALTER TABLE {{waybill_db}}.receipt_share_items
    ADD COLUMN shipment_no VARCHAR(64) NOT NULL DEFAULT '' AFTER waybill_no,
    ADD COLUMN shipment_seq INT UNSIGNED NOT NULL DEFAULT 1 AFTER seq,
    ADD COLUMN shipment_of INT UNSIGNED NOT NULL DEFAULT 1 AFTER shipment_seq;
