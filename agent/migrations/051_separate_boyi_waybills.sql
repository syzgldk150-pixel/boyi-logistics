-- Preserve historical manual IDs and all stored fields in a dedicated table.
-- Schema creation may be retried after interrupted DDL. Data moves atomically.
CREATE TABLE IF NOT EXISTS boyi_waybills LIKE waybills;

START TRANSACTION;
INSERT INTO boyi_waybills (
    id, document_id, waybill_no, destination_site, open_date,
    receiver_address, receiver_name, receiver_phone, sender_name, sender_phone,
    goods_name_lines, package_type_lines, quantity_lines, weight_volume,
    delivery_method, freight_fee, pickup_fee, delivery_fee, transfer_fee,
    payment_method, insurance_amount, cod_amount, remark, writer_id, source,
    status, scan_status, created_at, updated_at, source_scope, source_record_id,
    source_account_id, source_permission_scope
)
SELECT
    id, document_id, waybill_no, destination_site, open_date,
    receiver_address, receiver_name, receiver_phone, sender_name, sender_phone,
    goods_name_lines, package_type_lines, quantity_lines, weight_volume,
    delivery_method, freight_fee, pickup_fee, delivery_fee, transfer_fee,
    payment_method, insurance_amount, cod_amount, remark, writer_id, source,
    status, scan_status, created_at, updated_at, source_scope, source_record_id,
    source_account_id, source_permission_scope
FROM waybills WHERE source = 'manual';
DELETE FROM waybills WHERE source = 'manual';
COMMIT;
