-- 派件账单：“付清点费收入”也作为增值费算给司机（2026-11-26 用户定）。只改数据：
-- 设置里的增值费结算类型还没有这一项时补在末尾，已有的不动、不重复。
-- {{finance_db}} is bound by the deployment runner, which then re-projects the agent_db views.

UPDATE {{finance_db}}.finance_delivery_bill_settings
SET value_added_types_json = JSON_ARRAY_APPEND(value_added_types_json, '$', '付清点费收入'),
    revision = revision + 1,
    updated_by = 'migration_096'
WHERE id = 1
  AND NOT JSON_CONTAINS(value_added_types_json, JSON_QUOTE('付清点费收入'));
