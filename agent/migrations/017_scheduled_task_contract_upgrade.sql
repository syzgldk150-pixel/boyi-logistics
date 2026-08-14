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
SET @cp017_arrive_list_canonical = JSON_OBJECT(
    'account_id', 'ronghui_default'
);
SET @cp017_arrive_site_sha256 =
    '5ff8d6c00584886090be588977393764370cbcac7f7d983a2f0b330c5f37b135';
SET @cp017_yunda_send_canonical = JSON_OBJECT(
    'account_id', 'yunda_default',
    'ensure_fields', CAST('false' AS JSON)
);
SET @cp017_yunda_send_pre014 = JSON_OBJECT(
    'account_id', 'yunda_default',
    'session_profile', 'yunda',
    'ensure_fields', CAST('false' AS JSON),
    'target_date', ''
);
SET @cp017_yunda_disabled_message_sha256 =
    '19129e9c68d5e20050a7d8c8e8489f4f1313f9fb6188adc55229aaeacad9c0e3';

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

SET @cp017_arrive_list_count = (
    SELECT COUNT(*)
    FROM scheduled_tasks
    WHERE id IN ('arrive_list_0830', 'arrive_list_0900', 'arrive_list_0930')
);
SET @cp017_invalid_arrive_list_rows = IF(
    @cp017_arrive_list_count IN (0, 3),
    0,
    1
) + (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE task.id IN ('arrive_list_0830', 'arrive_list_0900', 'arrive_list_0930')
      AND NOT (
          task.tool_name = 'sync_arrive_list'
          AND task.cron_expression = CASE task.id
              WHEN 'arrive_list_0830' THEN '30 8 * * *'
              WHEN 'arrive_list_0900' THEN '0 9 * * *'
              WHEN 'arrive_list_0930' THEN '30 9 * * *'
          END
          AND task.enabled = TRUE
          AND (
              COALESCE(
                  JSON_CONTAINS(task.tool_params, @cp017_arrive_list_canonical)
                  AND JSON_CONTAINS(@cp017_arrive_list_canonical, task.tool_params),
                  FALSE
              )
              OR COALESCE(
                  JSON_LENGTH(task.tool_params) = 2
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.site_code')) = 'STRING'
                  AND CHAR_LENGTH(TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code')))) > 0
                  AND SHA2(
                      TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code'))),
                      256
                  ) = @cp017_arrive_site_sha256,
                  FALSE
              )
          )
      )
);
SET @cp017_arrive_list_guard_sql = IF(
    @cp017_invalid_arrive_list_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_invalid_arrive_list_contract'
);
PREPARE cp017_arrive_list_guard_stmt FROM @cp017_arrive_list_guard_sql;
EXECUTE cp017_arrive_list_guard_stmt;
DEALLOCATE PREPARE cp017_arrive_list_guard_stmt;

SET @cp017_yunda_send_count = (
    SELECT COUNT(*)
    FROM scheduled_tasks
    WHERE id = 'yunda_send_waybills_2355'
);
SET @cp017_invalid_yunda_send_rows = IF(
    @cp017_yunda_send_count IN (0, 1),
    0,
    1
) + (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE task.id = 'yunda_send_waybills_2355'
      AND NOT (
          task.tool_name = 'sync_yunda_send_waybills'
          AND task.cron_expression = '55 23 * * *'
          AND COALESCE(
              JSON_CONTAINS(task.tool_params, @cp017_yunda_send_canonical)
              AND JSON_CONTAINS(@cp017_yunda_send_canonical, task.tool_params),
              FALSE
          )
          AND (
              task.enabled = TRUE
              OR (
                  task.enabled = FALSE
                  AND task.configuration_version = 1
                  AND task.last_status = 'disabled'
                  AND SHA2(COALESCE(task.last_message, ''), 256) =
                      @cp017_yunda_disabled_message_sha256
                  AND EXISTS (
                      SELECT 1
                      FROM control_plane_task_cutover_backup_014 AS prior
                      WHERE BINARY prior.id = BINARY task.id
                        AND prior.tool_name = 'sync_yunda_send_waybills'
                        AND prior.cron_expression = '55 23 * * *'
                        AND prior.enabled = TRUE
                        AND COALESCE(
                            JSON_CONTAINS(prior.tool_params, @cp017_yunda_send_pre014)
                            AND JSON_CONTAINS(@cp017_yunda_send_pre014, prior.tool_params),
                            FALSE
                        )
                  )
              )
          )
      )
);
SET @cp017_yunda_send_guard_sql = IF(
    @cp017_invalid_yunda_send_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_invalid_yunda_send_contract'
);
PREPARE cp017_yunda_send_guard_stmt FROM @cp017_yunda_send_guard_sql;
EXECUTE cp017_yunda_send_guard_stmt;
DEALLOCATE PREPARE cp017_yunda_send_guard_stmt;

CREATE TABLE IF NOT EXISTS scheduled_task_contract_upgrade_backup_017 LIKE scheduled_tasks;

INSERT IGNORE INTO scheduled_task_contract_upgrade_backup_017
SELECT task.*
FROM scheduled_tasks AS task
WHERE task.id IN (
    'clockin_daxiang_1830',
    'clockin_daxiang_s_1833',
    'finance_bills_0010',
    'arrive_list_0830',
    'arrive_list_0900',
    'arrive_list_0930',
    'yunda_send_waybills_2355'
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

UPDATE scheduled_tasks AS task
SET task.tool_params = @cp017_arrive_list_canonical,
    task.configuration_version = task.configuration_version + 1
WHERE task.id IN ('arrive_list_0830', 'arrive_list_0900', 'arrive_list_0930')
  AND JSON_LENGTH(task.tool_params) = 2
  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default'
  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.site_code')) = 'STRING'
  AND SHA2(
      TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code'))),
      256
  ) = @cp017_arrive_site_sha256;

UPDATE scheduled_tasks AS task
INNER JOIN control_plane_task_cutover_backup_014 AS prior
  ON BINARY prior.id = BINARY task.id
SET task.enabled = TRUE,
    task.last_status = prior.last_status,
    task.last_message = prior.last_message,
    task.configuration_version = task.configuration_version + 1
WHERE task.id = 'yunda_send_waybills_2355'
  AND task.tool_name = 'sync_yunda_send_waybills'
  AND task.cron_expression = '55 23 * * *'
  AND task.enabled = FALSE
  AND task.configuration_version = 1
  AND task.last_status = 'disabled'
  AND SHA2(COALESCE(task.last_message, ''), 256) =
      @cp017_yunda_disabled_message_sha256
  AND JSON_CONTAINS(task.tool_params, @cp017_yunda_send_canonical)
  AND JSON_CONTAINS(@cp017_yunda_send_canonical, task.tool_params)
  AND prior.tool_name = 'sync_yunda_send_waybills'
  AND prior.cron_expression = '55 23 * * *'
  AND prior.enabled = TRUE
  AND JSON_CONTAINS(prior.tool_params, @cp017_yunda_send_pre014)
  AND JSON_CONTAINS(@cp017_yunda_send_pre014, prior.tool_params);

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

SET @cp017_noncanonical_arrive_list_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE task.id IN ('arrive_list_0830', 'arrive_list_0900', 'arrive_list_0930')
      AND NOT COALESCE(
          JSON_CONTAINS(task.tool_params, @cp017_arrive_list_canonical)
          AND JSON_CONTAINS(@cp017_arrive_list_canonical, task.tool_params),
          FALSE
      )
);
SET @cp017_arrive_list_post_guard_sql = IF(
    @cp017_noncanonical_arrive_list_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_arrive_list_upgrade_failed'
);
PREPARE cp017_arrive_list_post_guard_stmt FROM @cp017_arrive_list_post_guard_sql;
EXECUTE cp017_arrive_list_post_guard_stmt;
DEALLOCATE PREPARE cp017_arrive_list_post_guard_stmt;

SET @cp017_noncanonical_yunda_send_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE task.id = 'yunda_send_waybills_2355'
      AND NOT (
          task.tool_name = 'sync_yunda_send_waybills'
          AND task.cron_expression = '55 23 * * *'
          AND task.enabled = TRUE
          AND COALESCE(
              JSON_CONTAINS(task.tool_params, @cp017_yunda_send_canonical)
              AND JSON_CONTAINS(@cp017_yunda_send_canonical, task.tool_params),
              FALSE
          )
      )
);
SET @cp017_yunda_send_post_guard_sql = IF(
    @cp017_noncanonical_yunda_send_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_yunda_send_upgrade_failed'
);
PREPARE cp017_yunda_send_post_guard_stmt FROM @cp017_yunda_send_post_guard_sql;
EXECUTE cp017_yunda_send_post_guard_stmt;
DEALLOCATE PREPARE cp017_yunda_send_post_guard_stmt;

COMMIT;
