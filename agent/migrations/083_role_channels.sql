-- Two permission sets per identity (access role):
--   console_permissions_json  {module: "view" | "operate"}  back-office pages
--   agent_permissions_json    {capability: "direct" | "confirm"}  Feishu,
--                             WeChat and the Console AI assistant
-- The old single list is translated so that every person keeps what they
-- could do before. Agent writes (plugins, customer-service and finance
-- plugins) start as "confirm": they are confirmed in chat before running.
-- Finance data starts as "private": answered in one-to-one chats only.
-- WeChat device limits keep only agent capabilities.
ALTER TABLE access_roles
    ADD COLUMN console_permissions_json JSON NULL,
    ADD COLUMN agent_permissions_json JSON NULL;

UPDATE access_roles SET console_permissions_json = JSON_MERGE_PATCH(
    JSON_OBJECT(),
    IF(JSON_CONTAINS(permissions_json, '"business.query"'), JSON_OBJECT('business.query', 'view'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"waybill.write"'), JSON_OBJECT('waybill.write', 'operate'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"customer_service"'), JSON_OBJECT('customer_service', 'operate'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"receipt.share"'), JSON_OBJECT('receipt.share', 'operate'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"dispatch"'), JSON_OBJECT('dispatch', 'view'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"line_haul"'), JSON_OBJECT('line_haul', 'operate'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"warehouse.monitor"'), JSON_OBJECT('warehouse.monitor', 'view'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"plugins.execute"'), JSON_OBJECT('plugins.execute', 'operate'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"finance.manage"'), JSON_OBJECT('finance', 'operate'),
       IF(JSON_CONTAINS(permissions_json, '"finance.read"'), JSON_OBJECT('finance', 'view'), JSON_OBJECT())),
    IF(JSON_CONTAINS(permissions_json, '"accounts.manage"'), JSON_OBJECT('accounts.manage', 'operate'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"ai.chat"'), JSON_OBJECT('ai_assistant', 'view'), JSON_OBJECT())
);

UPDATE access_roles SET agent_permissions_json = JSON_MERGE_PATCH(
    JSON_OBJECT(),
    IF(JSON_CONTAINS(permissions_json, '"ai.chat"'), JSON_OBJECT('ai.chat', 'direct'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"business.query"'), JSON_OBJECT('business.query', 'direct'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"warehouse.monitor"'), JSON_OBJECT('warehouse.monitor', 'direct'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"finance.read"'), JSON_OBJECT('finance.read', 'private'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"plugins.execute"'), JSON_OBJECT('plugins.execute', 'confirm'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"customer_service"'), JSON_OBJECT('customer_service', 'confirm'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"finance.manage"'), JSON_OBJECT('finance.manage', 'confirm'), JSON_OBJECT()),
    IF(JSON_CONTAINS(permissions_json, '"accounts.manage"'), JSON_OBJECT('accounts.manage', 'direct'), JSON_OBJECT())
);

ALTER TABLE access_roles
    MODIFY console_permissions_json JSON NOT NULL,
    MODIFY agent_permissions_json JSON NOT NULL,
    DROP COLUMN permissions_json;

UPDATE wechat_bot_devices d SET d.allowed_permissions_json = (
    SELECT COALESCE(JSON_ARRAYAGG(item.capability), JSON_ARRAY())
    FROM JSON_TABLE(d.allowed_permissions_json, '$[*]' COLUMNS (capability VARCHAR(64) PATH '$')) AS item
    WHERE item.capability IN ('ai.chat', 'business.query', 'warehouse.monitor', 'finance.read',
                              'plugins.execute', 'customer_service', 'finance.manage', 'accounts.manage')
) WHERE d.allowed_permissions_json IS NOT NULL;
