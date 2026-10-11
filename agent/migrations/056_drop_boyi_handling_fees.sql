-- The Boyi waybill label, entry form and OCR template no longer carry pickup or
-- delivery fees; Ronghui/Yunda platform waybills keep both columns.
-- {{waybill_db}} is bound by the deployment runner, which then refreshes the
-- agent_db views (the waybills view reads NULL for Boyi OCR rows).
-- Retain the removed values by waybill ID for recovery after this release.
CREATE TABLE {{waybill_db}}._migration_056_boyi_handling_fees AS
    SELECT id, pickup_fee, delivery_fee FROM {{waybill_db}}.boyi_waybills;

ALTER TABLE {{waybill_db}}.boyi_waybills
    DROP COLUMN pickup_fee,
    DROP COLUMN delivery_fee;
