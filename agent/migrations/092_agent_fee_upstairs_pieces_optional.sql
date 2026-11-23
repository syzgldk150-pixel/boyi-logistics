-- 业务手册 → 计算规则：上楼费的“货物明细”改为选填 (2026-11-23).
--
-- 有电梯的普货上楼费只看结算重量，用不到逐件重量；但“货物明细”被标成必填，试算页又
-- 只把填了重量/尺寸的行算作明细，所以有电梯也非填单件实际重量不可。改为选填后，引擎
-- 只在真正用到时（无电梯查 ≥120kg、托/木箱看 150kg、床垫看长宽）才报缺少。
-- 只改仍是草稿、且第 6 个参数仍是 pieces 的上楼费版本；已生效版本不动。
UPDATE agent_fee_rule_versions
SET content_json=JSON_SET(content_json,
        '$.params[5].required', CAST('false' AS JSON),
        '$.params[5].hint', '没电梯、托/木箱、床垫才需要填：按“件数 × 每件重量”；床垫还要填长度和宽度'),
    updated_by='migration:092', row_version=row_version+1
WHERE rule_id='29e79711-0f15-5421-9cbc-ec74d21d0e3d' AND status='DRAFT'
  AND JSON_UNQUOTE(JSON_EXTRACT(content_json, '$.params[5].key'))='pieces'
  AND JSON_EXTRACT(content_json, '$.params[5].required') IS NULL;
