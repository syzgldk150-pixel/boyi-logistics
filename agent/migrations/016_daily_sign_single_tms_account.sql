-- Collapse the three duplicated Ronghui roles used by daily-sign schedules
-- into the one reviewed Shaoyang Daxiang Station TMS account.  R13 remains a
-- separate source system and keeps its own explicit account binding.

SET @cp016_invalid_daily_sign_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE (task.id = 'daily_sign' OR task.id LIKE 'daily_sign\_%')
      AND task.tool_name = 'sync_daily_should_sign'
      AND NOT (
          (
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 3
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_daxiang_s'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.days')) = 'INTEGER'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.days')) = '7'
          )
          OR
          (
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 5
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.problem_account_id')) = 'ronghui_daxiang_s'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sign_account_id')) = 'ronghui_daxiang_s'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.detail_account_id')) IN ('ronghui_default', 'ronghui_daxiang_s')
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.days')) = 'INTEGER'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.days')) = '7'
          )
          OR
          (
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 6
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'r13_default'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.problem_account_id')) = 'ronghui_daxiang_s'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sign_account_id')) = 'ronghui_daxiang_s'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.detail_account_id')) = 'ronghui_default'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.days')) = 'INTEGER'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.days')) = '7'
          )
      )
);

SET @cp016_guard_sql = IF(
    @cp016_invalid_daily_sign_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp016_invalid_daily_sign_account_contract'
);
PREPARE cp016_guard_stmt FROM @cp016_guard_sql;
EXECUTE cp016_guard_stmt;
DEALLOCATE PREPARE cp016_guard_stmt;

CREATE TABLE IF NOT EXISTS daily_sign_single_tms_backup_016 LIKE scheduled_tasks;

INSERT IGNORE INTO daily_sign_single_tms_backup_016
SELECT task.*
FROM scheduled_tasks AS task
WHERE (task.id = 'daily_sign' OR task.id LIKE 'daily_sign\_%')
  AND task.tool_name = 'sync_daily_should_sign'
  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) IN (5, 6);

START TRANSACTION;

UPDATE scheduled_tasks AS task
SET task.tool_params = JSON_OBJECT(
        'r13_account_id', 'r13_default',
        'account_id', 'ronghui_daxiang_s',
        'days', 7
    ),
    task.configuration_version = task.configuration_version + 1
WHERE (task.id = 'daily_sign' OR task.id LIKE 'daily_sign\_%')
  AND task.tool_name = 'sync_daily_should_sign'
  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) IN (5, 6);

SET @cp016_noncanonical_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE (task.id = 'daily_sign' OR task.id LIKE 'daily_sign\_%')
      AND task.tool_name = 'sync_daily_should_sign'
      AND NOT (
          JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
          AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 3
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_daxiang_s'
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.days')) = 'INTEGER'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.days')) = '7'
      )
);

SET @cp016_post_guard_sql = IF(
    @cp016_noncanonical_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp016_daily_sign_account_upgrade_failed'
);
PREPARE cp016_post_guard_stmt FROM @cp016_post_guard_sql;
EXECUTE cp016_post_guard_stmt;
DEALLOCATE PREPARE cp016_post_guard_stmt;

COMMIT;
