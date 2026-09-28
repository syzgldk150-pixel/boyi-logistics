-- Sender address is optional; existing Boyi waybills keep an empty value.
ALTER TABLE boyi_waybills
    ADD COLUMN sender_address TEXT NULL;
