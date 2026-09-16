-- Historical Boyi labels used the unchecked receipt template.
ALTER TABLE boyi_waybills
    ADD COLUMN receipt_required TINYINT(1) NOT NULL DEFAULT 0,
    ADD CONSTRAINT chk_boyi_receipt_required CHECK (receipt_required IN (0, 1));
