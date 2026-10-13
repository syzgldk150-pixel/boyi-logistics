-- A Boyi manual waybill may be linked to one Ronghui, Yunda or Best platform
-- waybill; its tracking then queries the linked platform number. Both columns
-- are empty until an operator links one; changes are recorded in
-- boyi_waybill_edit_logs by the Console in the same transaction.
-- {{waybill_db}} is bound by the deployment runner, which then refreshes the
-- agent_db views (the boyi_waybills view projects the new columns).
ALTER TABLE {{waybill_db}}.boyi_waybills
    ADD COLUMN linked_platform VARCHAR(16) NULL,
    ADD COLUMN linked_waybill_no VARCHAR(64) NULL;
