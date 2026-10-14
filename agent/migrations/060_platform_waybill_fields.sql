-- Platform values are populated only by a fresh native-source synchronization.
-- Do not relabel old costs/weights as newly verified business values.
ALTER TABLE {{waybill_db}}.ronghui_waybills
    ADD COLUMN sender_address TEXT NULL,
    ADD COLUMN recorded_cost VARCHAR(64) NULL,
    ADD COLUMN to_pay_amount VARCHAR(64) NULL,
    ADD COLUMN receipt_required VARCHAR(1) NULL,
    ADD COLUMN receipt_number VARCHAR(64) NULL;

ALTER TABLE {{waybill_db}}.yunda_waybills
    ADD COLUMN sender_address TEXT NULL,
    ADD COLUMN recorded_cost VARCHAR(64) NULL,
    ADD COLUMN to_pay_amount VARCHAR(64) NULL,
    ADD COLUMN receipt_required VARCHAR(1) NULL,
    ADD COLUMN receipt_number VARCHAR(64) NULL;

-- The previous native collector labelled FEE_WEIGHT as actual weight.
-- Preserve historical numbers without claiming an unverified physical meaning.
UPDATE {{waybill_db}}.ronghui_waybills
SET weight_volume = CONCAT('历史重量（待核验） ', SUBSTRING(weight_volume, CHAR_LENGTH('实际重量 ') + 1))
WHERE weight_volume LIKE '实际重量 %' AND weight_volume NOT LIKE '%计费重量%';

-- A complete historical snapshot made under the old projection must be read again.
UPDATE {{waybill_db}}.waybill_source_coverage
SET final_day = 0, complete_through = NULL
WHERE source IN ('ronghui', 'yunda');
