-- 派件账单按融辉结算明细分项（2026-11-26 用户定）：
-- 1. 增值费不再人工按业务手册选，直接取融辉结算明细里指定的结算类型（默认“付上楼费收入”
--    “付卸货费”），算给司机；其余计入融辉结算金额的类型（代收货款手续费、到付款手续费、
--    等通知放货费、围板箱服务费等）作为“其他收入”单独一列，不给司机。类型清单在设置里可改。
-- 2. 到货表配送方式在清单里的单（默认“自提”，即开单自提）不算派送费，派送费留空；这类单
--    仍可确认，应付只含增值费，所以确认记录允许派送费与规则同时为空。
-- {{finance_db}} is bound by the deployment runner, which then re-projects the agent_db views.

ALTER TABLE {{finance_db}}.finance_delivery_bill_settings
    ADD COLUMN value_added_types_json JSON NULL AFTER exclude_types_json,
    ADD COLUMN no_fee_methods_json JSON NULL AFTER value_added_types_json;

UPDATE {{finance_db}}.finance_delivery_bill_settings
SET value_added_types_json = JSON_ARRAY('付上楼费收入', '付卸货费'),
    no_fee_methods_json = JSON_ARRAY('自提')
WHERE value_added_types_json IS NULL OR no_fee_methods_json IS NULL;

ALTER TABLE {{finance_db}}.finance_delivery_bill_settings
    MODIFY COLUMN value_added_types_json JSON NOT NULL,
    MODIFY COLUMN no_fee_methods_json JSON NOT NULL;

ALTER TABLE {{finance_db}}.finance_delivery_bill_items
    DROP CHECK ck_finance_delivery_bill_items_confirmed;

ALTER TABLE {{finance_db}}.finance_delivery_bill_items
    ADD CONSTRAINT ck_finance_delivery_bill_items_confirmed CHECK (
        status = 'open' OR (payable_total IS NOT NULL AND ronghui_amount IS NOT NULL AND confirmed_at IS NOT NULL
                            AND (delivery_fee IS NULL) = (delivery_rule_id IS NULL)));
