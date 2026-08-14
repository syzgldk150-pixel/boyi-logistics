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
SET @cp017_finance_startup_canonical = JSON_OBJECT(
    'mode', 'sync',
    'platform', 'ronghui',
    'rescan_days', 7,
    '_startup_catchup', CAST('true' AS JSON)
);
SET @cp017_approval_migration_sha256 =
    'fe91354e684013faa63a4b93f71374231ea721cdd16c4a0ec5bc19eda1a2783c';
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
    WHERE BINARY id IN (
        BINARY 'clockin_daxiang_1830',
        BINARY 'clockin_daxiang_s_1833'
    )
);
SET @cp017_invalid_clock_rows = IF(
    @cp017_clock_pair_count IN (0, 2),
    0,
    1
) + (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE BINARY task.id IN (
        BINARY 'clockin_daxiang_1830',
        BINARY 'clockin_daxiang_s_1833'
    )
      AND NOT (
          task.enabled = TRUE
          AND BINARY task.cron_expression = CASE BINARY task.id
              WHEN BINARY 'clockin_daxiang_1830' THEN BINARY '30 18 * * *'
              WHEN BINARY 'clockin_daxiang_s_1833' THEN BINARY '33 18 * * *'
          END
          AND (
              (
                  BINARY task.id = BINARY 'clockin_daxiang_1830'
                  AND BINARY task.tool_name = BINARY 'clock_in_dual'
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
                  BINARY task.id = BINARY 'clockin_daxiang_s_1833'
                  AND (
                      (
                          BINARY task.tool_name = BINARY 'clock_in_dual'
                          AND COALESCE(
                              JSON_CONTAINS(task.tool_params, @cp017_daxiang_s_canonical)
                              AND JSON_CONTAINS(@cp017_daxiang_s_canonical, task.tool_params),
                              FALSE
                          )
                      )
                      OR (
                          BINARY task.tool_name = BINARY 'tms_query'
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
    WHERE BINARY task.id = BINARY 'finance_bills_0010'
      AND NOT (
          BINARY task.tool_name = BINARY 'sync_finance_bills'
          AND BINARY task.cron_expression = BINARY '10 0 * * *'
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

-- The c7 service ran one bounded finance gap scan after every process start,
-- independently of the disabled 00:10 schedule. A failed first control-plane
-- release can leave behind the new runtime's disabled template. Treat that row
-- as release-owned only when its complete virgin-row evidence falls inside the
-- narrow 015 startup window and no administrator policy/audit event exists.
SET @cp017_finance_startup_count = (
    SELECT COUNT(*)
    FROM scheduled_tasks
    WHERE BINARY id = BINARY 'finance_startup_catchup'
);
SET @cp017_finance_startup_seed_owned = (
    SELECT COUNT(*) = 1
    FROM scheduled_tasks AS task
    INNER JOIN schema_migrations AS migration
      ON BINARY migration.version = BINARY '015'
    WHERE BINARY task.id = BINARY 'finance_startup_catchup'
      AND BINARY migration.filename =
          BINARY '015_scheduled_task_approval_policies.sql'
      AND BINARY migration.checksum = BINARY @cp017_approval_migration_sha256
      AND BINARY task.name = BINARY '财务启动缺口扫描'
      AND BINARY task.tool_name = BINARY 'sync_finance_bills'
      AND BINARY task.cron_expression = BINARY '@startup'
      AND task.enabled = FALSE
      AND task.configuration_version = 1
      AND task.last_run IS NULL
      AND task.last_status IS NULL
      AND task.last_duration_ms IS NULL
      AND task.last_message IS NULL
      AND task.created_at > migration.applied_at
      AND task.created_at <= DATE_ADD(migration.applied_at, INTERVAL 30 SECOND)
      AND task.updated_at >= CAST(task.created_at AS DATETIME(6))
      AND task.updated_at < DATE_ADD(task.created_at, INTERVAL 1 SECOND)
      AND COALESCE(
          JSON_CONTAINS(task.tool_params, @cp017_finance_startup_canonical)
          AND JSON_CONTAINS(@cp017_finance_startup_canonical, task.tool_params),
          FALSE
      )
      AND EXISTS (
          SELECT 1
          FROM scheduled_tasks AS finance
          WHERE BINARY finance.id = BINARY 'finance_bills_0010'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM scheduled_task_approval_policies AS policy
          WHERE BINARY policy.task_id = BINARY task.id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM scheduled_task_approval_policy_events AS event
          WHERE BINARY event.task_id = BINARY task.id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM scheduled_task_approval_policy_events AS marker
          WHERE BINARY marker.task_id =
                    BINARY '__control_plane_v1_bootstrap_complete__'
            AND BINARY marker.actor_id =
                BINARY 'system:migration:control-plane-v1'
            AND BINARY marker.actor_role = BINARY 'migration_authority'
            AND BINARY marker.reason =
                BINARY 'control_plane_v1_bootstrap_complete'
      )
);
SET @cp017_invalid_finance_startup_rows = IF(
    @cp017_finance_startup_count IN (0, 1),
    0,
    1
) + (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE BINARY task.id = BINARY 'finance_startup_catchup'
      AND NOT (
          BINARY task.name = BINARY '财务启动缺口扫描'
          AND BINARY task.tool_name = BINARY 'sync_finance_bills'
          AND BINARY task.cron_expression = BINARY '@startup'
          AND COALESCE(
              JSON_CONTAINS(task.tool_params, @cp017_finance_startup_canonical)
              AND JSON_CONTAINS(@cp017_finance_startup_canonical, task.tool_params),
              FALSE
          )
          AND EXISTS (
              SELECT 1
              FROM scheduled_tasks AS finance
              WHERE BINARY finance.id = BINARY 'finance_bills_0010'
          )
          AND (
              task.enabled = TRUE
              OR @cp017_finance_startup_seed_owned = TRUE
          )
      )
);
SET @cp017_finance_startup_guard_sql = IF(
    @cp017_invalid_finance_startup_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_invalid_finance_startup_contract'
);
PREPARE cp017_finance_startup_guard_stmt FROM @cp017_finance_startup_guard_sql;
EXECUTE cp017_finance_startup_guard_stmt;
DEALLOCATE PREPARE cp017_finance_startup_guard_stmt;

SET @cp017_arrive_list_count = (
    SELECT COUNT(*)
    FROM scheduled_tasks
    WHERE BINARY id IN (
        BINARY 'arrive_list_0830',
        BINARY 'arrive_list_0900',
        BINARY 'arrive_list_0930'
    )
);
SET @cp017_invalid_arrive_list_rows = IF(
    @cp017_arrive_list_count IN (0, 3),
    0,
    1
) + (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE BINARY task.id IN (
        BINARY 'arrive_list_0830',
        BINARY 'arrive_list_0900',
        BINARY 'arrive_list_0930'
    )
      AND NOT (
          BINARY task.tool_name = BINARY 'sync_arrive_list'
          AND BINARY task.cron_expression = CASE BINARY task.id
              WHEN BINARY 'arrive_list_0830' THEN BINARY '30 8 * * *'
              WHEN BINARY 'arrive_list_0900' THEN BINARY '0 9 * * *'
              WHEN BINARY 'arrive_list_0930' THEN BINARY '30 9 * * *'
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
                  AND BINARY JSON_TYPE(
                      JSON_EXTRACT(task.tool_params, '$.account_id')
                  ) = BINARY 'STRING'
                  AND BINARY JSON_UNQUOTE(
                      JSON_EXTRACT(task.tool_params, '$.account_id')
                  ) = BINARY 'ronghui_default'
                  AND BINARY JSON_TYPE(
                      JSON_EXTRACT(task.tool_params, '$.site_code')
                  ) = BINARY 'STRING'
                  AND CHAR_LENGTH(TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code')))) > 0
                  AND BINARY SHA2(
                      TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code'))),
                      256
                  ) = BINARY @cp017_arrive_site_sha256,
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
    WHERE BINARY id = BINARY 'yunda_send_waybills_2355'
);
SET @cp017_invalid_yunda_send_rows = IF(
    @cp017_yunda_send_count IN (0, 1),
    0,
    1
) + (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE BINARY task.id = BINARY 'yunda_send_waybills_2355'
      AND NOT (
          BINARY task.tool_name = BINARY 'sync_yunda_send_waybills'
          AND BINARY task.cron_expression = BINARY '55 23 * * *'
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
                  AND BINARY task.last_status = BINARY 'disabled'
                  AND BINARY SHA2(COALESCE(task.last_message, ''), 256) =
                      BINARY @cp017_yunda_disabled_message_sha256
                  AND EXISTS (
                      SELECT 1
                      FROM control_plane_task_cutover_backup_014 AS prior
                      WHERE BINARY prior.id = BINARY task.id
                        AND BINARY prior.tool_name =
                            BINARY 'sync_yunda_send_waybills'
                        AND BINARY prior.cron_expression = BINARY '55 23 * * *'
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

CREATE TABLE IF NOT EXISTS scheduled_task_contract_upgrade_created_017 (
    task_id VARCHAR(64) NOT NULL,
    task_created_at DATETIME NOT NULL,
    task_updated_at DATETIME(6) NOT NULL,
    task_configuration_version BIGINT UNSIGNED NOT NULL,
    marker_created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (task_id),
    CONSTRAINT chk_cp017_created_task_id CHECK (
        BINARY task_id = BINARY 'finance_startup_catchup'
    ),
    CONSTRAINT chk_cp017_created_task_version CHECK (
        task_configuration_version = 1
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO scheduled_task_contract_upgrade_backup_017
SELECT task.*
FROM scheduled_tasks AS task
WHERE BINARY task.id IN (
    BINARY 'clockin_daxiang_1830',
    BINARY 'clockin_daxiang_s_1833',
    BINARY 'finance_bills_0010',
    BINARY 'finance_startup_catchup',
    BINARY 'arrive_list_0830',
    BINARY 'arrive_list_0900',
    BINARY 'arrive_list_0930',
    BINARY 'yunda_send_waybills_2355'
)
AND NOT EXISTS (
    SELECT 1
    FROM scheduled_task_contract_upgrade_created_017 AS marker
    WHERE BINARY marker.task_id = BINARY task.id
      AND task.created_at = marker.task_created_at
      AND task.updated_at = marker.task_updated_at
      AND task.configuration_version = marker.task_configuration_version
);

START TRANSACTION;

INSERT INTO scheduled_tasks (
    id, name, tool_name, tool_params, cron_expression, enabled
)
SELECT
    'finance_startup_catchup',
    '财务启动缺口扫描',
    'sync_finance_bills',
    @cp017_finance_startup_canonical,
    '@startup',
    TRUE
FROM scheduled_tasks AS finance
WHERE BINARY finance.id = BINARY 'finance_bills_0010'
  AND NOT EXISTS (
      SELECT 1
      FROM scheduled_tasks AS startup
      WHERE BINARY startup.id = BINARY 'finance_startup_catchup'
  );

INSERT INTO scheduled_task_contract_upgrade_created_017 (
    task_id,
    task_created_at,
    task_updated_at,
    task_configuration_version
)
SELECT
    task.id,
    task.created_at,
    task.updated_at,
    task.configuration_version
FROM scheduled_tasks AS task
LEFT JOIN scheduled_task_contract_upgrade_backup_017 AS prior
  ON BINARY prior.id = BINARY task.id
WHERE BINARY task.id = BINARY 'finance_startup_catchup'
  AND prior.id IS NULL
  AND BINARY task.name = BINARY '财务启动缺口扫描'
  AND BINARY task.tool_name = BINARY 'sync_finance_bills'
  AND BINARY task.cron_expression = BINARY '@startup'
  AND task.enabled = TRUE
  AND task.configuration_version = 1
  AND COALESCE(
      JSON_CONTAINS(task.tool_params, @cp017_finance_startup_canonical)
      AND JSON_CONTAINS(@cp017_finance_startup_canonical, task.tool_params),
      FALSE
  )
ON DUPLICATE KEY UPDATE
    task_id = IF(
        task_created_at = VALUES(task_created_at)
        AND task_updated_at = VALUES(task_updated_at)
        AND task_configuration_version = VALUES(task_configuration_version),
        task_id,
        NULL
    );

UPDATE scheduled_tasks AS task
SET task.enabled = TRUE,
    task.configuration_version = task.configuration_version + 1
WHERE BINARY task.id = BINARY 'finance_startup_catchup'
  AND task.enabled = FALSE
  AND @cp017_finance_startup_seed_owned = TRUE;

UPDATE scheduled_tasks AS task
SET task.tool_params = @cp017_daxiang_canonical,
    task.configuration_version = task.configuration_version + 1
WHERE BINARY task.id = BINARY 'clockin_daxiang_1830'
  AND BINARY task.tool_name = BINARY 'clock_in_dual'
  AND JSON_CONTAINS(task.tool_params, @cp017_daxiang_transition)
  AND JSON_CONTAINS(@cp017_daxiang_transition, task.tool_params);

UPDATE scheduled_tasks AS task
SET task.tool_name = 'clock_in_dual',
    task.tool_params = @cp017_daxiang_s_canonical,
    task.configuration_version = task.configuration_version + 1
WHERE BINARY task.id = BINARY 'clockin_daxiang_s_1833'
  AND BINARY task.tool_name = BINARY 'tms_query'
  AND JSON_CONTAINS(task.tool_params, @cp017_daxiang_s_transition)
  AND JSON_CONTAINS(@cp017_daxiang_s_transition, task.tool_params);

UPDATE scheduled_tasks AS task
SET task.tool_params = @cp017_finance_canonical,
    task.configuration_version = task.configuration_version + 1
WHERE BINARY task.id = BINARY 'finance_bills_0010'
  AND BINARY task.tool_name = BINARY 'sync_finance_bills'
  AND JSON_CONTAINS(task.tool_params, @cp017_finance_transition)
  AND JSON_CONTAINS(@cp017_finance_transition, task.tool_params);

UPDATE scheduled_tasks AS task
SET task.tool_params = @cp017_arrive_list_canonical,
    task.configuration_version = task.configuration_version + 1
WHERE BINARY task.id IN (
    BINARY 'arrive_list_0830',
    BINARY 'arrive_list_0900',
    BINARY 'arrive_list_0930'
)
  AND JSON_LENGTH(task.tool_params) = 2
  AND BINARY JSON_TYPE(
      JSON_EXTRACT(task.tool_params, '$.account_id')
  ) = BINARY 'STRING'
  AND BINARY JSON_UNQUOTE(
      JSON_EXTRACT(task.tool_params, '$.account_id')
  ) = BINARY 'ronghui_default'
  AND BINARY JSON_TYPE(
      JSON_EXTRACT(task.tool_params, '$.site_code')
  ) = BINARY 'STRING'
  AND BINARY SHA2(
      TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code'))),
      256
  ) = BINARY @cp017_arrive_site_sha256;

UPDATE scheduled_tasks AS task
INNER JOIN control_plane_task_cutover_backup_014 AS prior
  ON BINARY prior.id = BINARY task.id
SET task.enabled = TRUE,
    task.last_status = prior.last_status,
    task.last_message = prior.last_message,
    task.configuration_version = task.configuration_version + 1
WHERE BINARY task.id = BINARY 'yunda_send_waybills_2355'
  AND BINARY task.tool_name = BINARY 'sync_yunda_send_waybills'
  AND BINARY task.cron_expression = BINARY '55 23 * * *'
  AND task.enabled = FALSE
  AND task.configuration_version = 1
  AND BINARY task.last_status = BINARY 'disabled'
  AND BINARY SHA2(COALESCE(task.last_message, ''), 256) =
      BINARY @cp017_yunda_disabled_message_sha256
  AND JSON_CONTAINS(task.tool_params, @cp017_yunda_send_canonical)
  AND JSON_CONTAINS(@cp017_yunda_send_canonical, task.tool_params)
  AND BINARY prior.tool_name = BINARY 'sync_yunda_send_waybills'
  AND BINARY prior.cron_expression = BINARY '55 23 * * *'
  AND prior.enabled = TRUE
  AND JSON_CONTAINS(prior.tool_params, @cp017_yunda_send_pre014)
  AND JSON_CONTAINS(@cp017_yunda_send_pre014, prior.tool_params);

SET @cp017_noncanonical_clock_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE BINARY task.id IN (
        BINARY 'clockin_daxiang_1830',
        BINARY 'clockin_daxiang_s_1833'
    )
      AND NOT (
          BINARY task.tool_name = BINARY 'clock_in_dual'
          AND (
              (
                  BINARY task.id = BINARY 'clockin_daxiang_1830'
                  AND COALESCE(
                      JSON_CONTAINS(task.tool_params, @cp017_daxiang_canonical)
                      AND JSON_CONTAINS(@cp017_daxiang_canonical, task.tool_params),
                      FALSE
                  )
              )
              OR (
                  BINARY task.id = BINARY 'clockin_daxiang_s_1833'
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
    WHERE BINARY task.id = BINARY 'finance_bills_0010'
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

SET @cp017_noncanonical_finance_startup_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE BINARY task.id = BINARY 'finance_startup_catchup'
      AND NOT (
          BINARY task.name = BINARY '财务启动缺口扫描'
          AND BINARY task.tool_name = BINARY 'sync_finance_bills'
          AND BINARY task.cron_expression = BINARY '@startup'
          AND task.enabled = TRUE
          AND COALESCE(
              JSON_CONTAINS(task.tool_params, @cp017_finance_startup_canonical)
              AND JSON_CONTAINS(@cp017_finance_startup_canonical, task.tool_params),
              FALSE
          )
      )
);
SET @cp017_finance_startup_post_guard_sql = IF(
    @cp017_noncanonical_finance_startup_rows = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp017_finance_startup_upgrade_failed'
);
PREPARE cp017_finance_startup_post_guard_stmt
    FROM @cp017_finance_startup_post_guard_sql;
EXECUTE cp017_finance_startup_post_guard_stmt;
DEALLOCATE PREPARE cp017_finance_startup_post_guard_stmt;

SET @cp017_noncanonical_arrive_list_rows = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    WHERE BINARY task.id IN (
        BINARY 'arrive_list_0830',
        BINARY 'arrive_list_0900',
        BINARY 'arrive_list_0930'
    )
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
    WHERE BINARY task.id = BINARY 'yunda_send_waybills_2355'
      AND NOT (
          BINARY task.tool_name = BINARY 'sync_yunda_send_waybills'
          AND BINARY task.cron_expression = BINARY '55 23 * * *'
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
