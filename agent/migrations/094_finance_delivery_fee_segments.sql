-- 派送费规则改为分公斤段（2026-11-25 用户定）：每段有自己的起点重量、首重（含 X 公斤）、首重价与
-- 续重单价；重量正好等于下一段起点时按下一段算。段表存在 segments_json（数字一律为文本）；
-- 计算仍用 content_json（shared.fee_engine 规则内容）。迁移 093 的单段字段改为一段的段表后删除：
-- 已有版本（若有）原样换成从 0 公斤起的一段，原 content_json 计算结果不变。
-- {{finance_db}} is bound by the deployment runner, which then re-projects the agent_db views.

ALTER TABLE {{finance_db}}.finance_delivery_fee_rules
    ADD COLUMN segments_json JSON NULL AFTER effective_from;

UPDATE {{finance_db}}.finance_delivery_fee_rules
SET segments_json = JSON_ARRAY(JSON_OBJECT(
        'from', '0',
        'first_weight', TRIM(TRAILING '.' FROM TRIM(TRAILING '0' FROM CAST(base_weight AS CHAR))),
        'first_price', TRIM(TRAILING '.' FROM TRIM(TRAILING '0' FROM CAST(start_price AS CHAR))),
        'extra_price', TRIM(TRAILING '.' FROM TRIM(TRAILING '0' FROM CAST(extra_price AS CHAR)))))
WHERE segments_json IS NULL;

ALTER TABLE {{finance_db}}.finance_delivery_fee_rules
    MODIFY COLUMN segments_json JSON NOT NULL,
    DROP CHECK ck_finance_delivery_fee_rules_amounts,
    DROP COLUMN start_price,
    DROP COLUMN base_weight,
    DROP COLUMN extra_price;
