-- Normalize only the exact scheduler shapes left by the production-applied
-- 014 migration. 014 is immutable; this forward migration owns the upgrade.

SET @cp017_daxiang_canonical = JSON_OBJECT(
    'account_id', 'ronghui_default',
    'sitecode', '7390004',
    'sitefbcode', '73901',
    'sitename', '邵阳大祥站',
    'sitefbname', '邵阳操作场',
    'first_type', '交件到港',
    'second_type', '接件离港',
    'delay_seconds', 2
);
SET @cp017_daxiang_s_canonical = JSON_OBJECT(
    'account_id', 'ronghui_daxiang_s',
    'sitecode', '7390017',
    'sitefbcode', '73901',
    'sitename', '邵阳大祥S站',
    'sitefbname', '邵阳操作场',
    'first_type', '交件到港',
    'second_type', '接件离港',
    'delay_seconds', 2
);
SET @cp017_daxiang_transition = JSON_OBJECT(
    'account_id', 'ronghui_default',
    'delay_seconds', 2,
    'first_type', '交件到港',
    'second_type', '接件离港',
    'sitefbname', '邵阳操作场',
    'sitename', '邵阳大祥站',
    'timeout_sec', 600
);
SET @cp017_daxiang_s_transition = JSON_OBJECT(
    'account_id', 'ronghui_daxiang_s',
    'endpoint', '/clock_in_dual',
    'params', JSON_OBJECT(
        'timeout_sec', 600,
        'params', JSON_OBJECT(
            'mode', 'api',
            'site_name', '邵阳大祥S站',
            'site_fb_name', '邵阳操作场',
            'first_type', '交件到港',
            'second_type', '接件离港',
            'delay_seconds', 2,
            'sitecode', '7390017',
            'sitefbcode', '73901'
        )
    )
);
SET @cp017_finance_canonical = JSON_OBJECT(
    'mode', 'sync',
    'platform', 'ronghui',
    'rescan_days', 7
);
SET @cp017_finance_transition = JSON_OBJECT(
    'account_id', 'ronghui_default',
    'mode', 'sync',
    'platform', 'ronghui',
    'rescan_days', 7
);

SET @cp017_clock_pair_count = (
    SELECT COUNT(*)
    FROM scheduled_tasks
    WHERE id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')
);
SET @cp017_invalid_clock_rows = IF(
    @cp017_clock_pair_count IN (0, 2),
    0,
    1
) + (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE task.id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')
      AND NOT (
          task.enabled = TRUE
          AND task.cron_expression = CASE task.id
              WHEN 'clockin_daxiang_1830' THEN '30 18 * * *'
              WHEN 'clockin_daxiang_s_1833' THEN '33 18 * * *'
          END
          AND (
              (
                  task.id = 'clockin_daxiang_1830'
                  AND task.tool_name = 'clock_in_dual'
                  AND (
                      COALESCE(
                          JSON_CONTAINS(task.tool_params, @cp017_daxiang_canonical)
                          AND JSON_CONTAINS(@cp017_daxiang_canonical, task.tool_params),
                          FALSE
                      )
                      OR COALESCE(
                          JSON_CONTAINS(task.tool_params, @cp017_daxiang_transition)
                          AND JSON_CONTAINS(@cp017_daxiang_transition, task.tool_params),
                          FALSE
                      )
                  )
              )
              OR (
                  task.id = 'clockin_daxiang_s_1833'
                  AND (
                      (
                          task.tool_name = 'clock_in_dual'
                          AND COALESCE(
                              JSON_CONTAINS(task.tool_params, @cp017_daxiang_s_canonical)
                              AND JSON_CONTAINS(@cp017_daxiang_s_canonical, task.tool_params),
                              FALSE
                          )
                      )
                      OR (
                          task.tool_name = 'tms_query'
                          AND COALESCE(
                              JSON_CONTAINS(task.tool_params, @cp017_daxiang_s_transition)
                              AND JSON_CONTAINS(@cp017_daxiang_s_transition, task.tool_params),
                              FALSE
                          )
                      )
                  )
              )
          )
      )
);
SET @cp017_clock_guard_sql = IF(
    @cp017_invalid_clock_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_invalid_clock_contract'
);
PREPARE cp017_clock_guard_stmt FROM @cp017_clock_guard_sql;
EXECUTE cp017_clock_guard_stmt;
DEALLOCATE PREPARE cp017_clock_guard_stmt;

SET @cp017_invalid_finance_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE task.id = 'finance_bills_0010'
      AND NOT (
          task.tool_name = 'sync_finance_bills'
          AND task.cron_expression = '10 0 * * *'
          AND task.enabled IN (FALSE, TRUE)
          AND (
              COALESCE(
                  JSON_CONTAINS(task.tool_params, @cp017_finance_canonical)
                  AND JSON_CONTAINS(@cp017_finance_canonical, task.tool_params),
                  FALSE
              )
              OR COALESCE(
                  JSON_CONTAINS(task.tool_params, @cp017_finance_transition)
                  AND JSON_CONTAINS(@cp017_finance_transition, task.tool_params),
                  FALSE
              )
          )
      )
);
SET @cp017_finance_guard_sql = IF(
    @cp017_invalid_finance_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_invalid_finance_contract'
);
PREPARE cp017_finance_guard_stmt FROM @cp017_finance_guard_sql;
EXECUTE cp017_finance_guard_stmt;
DEALLOCATE PREPARE cp017_finance_guard_stmt;

CREATE TABLE IF NOT EXISTS scheduled_task_contract_upgrade_backup_017 LIKE scheduled_tasks;

INSERT IGNORE INTO scheduled_task_contract_upgrade_backup_017
SELECT task.*
FROM scheduled_tasks AS task
WHERE task.id IN (
    'clockin_daxiang_1830',
    'clockin_daxiang_s_1833',
    'finance_bills_0010'
);

START TRANSACTION;

UPDATE scheduled_tasks AS task
SET task.tool_params = @cp017_daxiang_canonical,
    task.configuration_version = task.configuration_version + 1
WHERE task.id = 'clockin_daxiang_1830'
  AND task.tool_name = 'clock_in_dual'
  AND JSON_CONTAINS(task.tool_params, @cp017_daxiang_transition)
  AND JSON_CONTAINS(@cp017_daxiang_transition, task.tool_params);

UPDATE scheduled_tasks AS task
SET task.tool_name = 'clock_in_dual',
    task.tool_params = @cp017_daxiang_s_canonical,
    task.configuration_version = task.configuration_version + 1
WHERE task.id = 'clockin_daxiang_s_1833'
  AND task.tool_name = 'tms_query'
  AND JSON_CONTAINS(task.tool_params, @cp017_daxiang_s_transition)
  AND JSON_CONTAINS(@cp017_daxiang_s_transition, task.tool_params);

UPDATE scheduled_tasks AS task
SET task.tool_params = @cp017_finance_canonical,
    task.configuration_version = task.configuration_version + 1
WHERE task.id = 'finance_bills_0010'
  AND task.tool_name = 'sync_finance_bills'
  AND JSON_CONTAINS(task.tool_params, @cp017_finance_transition)
  AND JSON_CONTAINS(@cp017_finance_transition, task.tool_params);

SET @cp017_noncanonical_clock_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE task.id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')
      AND NOT (
          task.tool_name = 'clock_in_dual'
          AND (
              (
                  task.id = 'clockin_daxiang_1830'
                  AND COALESCE(
                      JSON_CONTAINS(task.tool_params, @cp017_daxiang_canonical)
                      AND JSON_CONTAINS(@cp017_daxiang_canonical, task.tool_params),
                      FALSE
                  )
              )
              OR (
                  task.id = 'clockin_daxiang_s_1833'
                  AND COALESCE(
                      JSON_CONTAINS(task.tool_params, @cp017_daxiang_s_canonical)
                      AND JSON_CONTAINS(@cp017_daxiang_s_canonical, task.tool_params),
                      FALSE
                  )
              )
          )
      )
);
SET @cp017_clock_post_guard_sql = IF(
    @cp017_noncanonical_clock_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_clock_upgrade_failed'
);
PREPARE cp017_clock_post_guard_stmt FROM @cp017_clock_post_guard_sql;
EXECUTE cp017_clock_post_guard_stmt;
DEALLOCATE PREPARE cp017_clock_post_guard_stmt;

SET @cp017_noncanonical_finance_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE task.id = 'finance_bills_0010'
      AND NOT COALESCE(
          JSON_CONTAINS(task.tool_params, @cp017_finance_canonical)
          AND JSON_CONTAINS(@cp017_finance_canonical, task.tool_params),
          FALSE
      )
);
SET @cp017_finance_post_guard_sql = IF(
    @cp017_noncanonical_finance_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_finance_upgrade_failed'
);
PREPARE cp017_finance_post_guard_stmt FROM @cp017_finance_post_guard_sql;
EXECUTE cp017_finance_post_guard_stmt;
DEALLOCATE PREPARE cp017_finance_post_guard_stmt;

COMMIT;
