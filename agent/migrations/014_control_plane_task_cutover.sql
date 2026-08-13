-- Cut over only the production schedule rows that were explicitly reviewed.
--
-- Safety model:
--   * temporary CHECK guards run before any permanent DDL or data mutation;
--   * an unknown governed task, an unknown parameter shape, or an enabled
--     third-party clock write aborts the whole migration;
--   * only proven legacy no-op/default fields are normalized;
--   * enabled flags and cron expressions are never changed;
--   * the first pre-cutover form of every changed row remains recoverable.

DROP TEMPORARY TABLE IF EXISTS cp014_expected_tasks;

CREATE TEMPORARY TABLE cp014_expected_tasks (
    id VARBINARY(128) PRIMARY KEY,
    family VARBINARY(64) NOT NULL,
    tool_name VARBINARY(128) NOT NULL,
    cron_expression VARBINARY(64) NOT NULL
) ENGINE=InnoDB;

INSERT INTO cp014_expected_tasks (id, family, tool_name, cron_expression)
VALUES
    ('arrive_list_0830', 'arrive_list', 'sync_arrive_list', '30 8 * * *'),
    ('arrive_list_0900', 'arrive_list', 'sync_arrive_list', '0 9 * * *'),
    ('arrive_list_0930', 'arrive_list', 'sync_arrive_list', '30 9 * * *'),
    ('daily_sign_0500', 'daily_sign', 'sync_daily_should_sign', '0 5 * * *'),
    ('daily_sign_0700', 'daily_sign', 'sync_daily_should_sign', '0 7 * * *'),
    ('daily_sign_0800', 'daily_sign', 'sync_daily_should_sign', '0 8 * * *'),
    ('daily_sign_0900', 'daily_sign', 'sync_daily_should_sign', '0 9 * * *'),
    ('daily_sign_1000', 'daily_sign', 'sync_daily_should_sign', '0 10 * * *'),
    ('daily_sign_1100', 'daily_sign', 'sync_daily_should_sign', '0 11 * * *'),
    ('daily_sign_1200', 'daily_sign', 'sync_daily_should_sign', '0 12 * * *'),
    ('daily_sign_1300', 'daily_sign', 'sync_daily_should_sign', '0 13 * * *'),
    ('daily_sign_1400', 'daily_sign', 'sync_daily_should_sign', '0 14 * * *'),
    ('daily_sign_1430', 'daily_sign', 'sync_daily_should_sign', '30 14 * * *'),
    ('daily_sign_1500', 'daily_sign', 'sync_daily_should_sign', '0 15 * * *'),
    ('daily_sign_1530', 'daily_sign', 'sync_daily_should_sign', '30 15 * * *'),
    ('daily_sign_1600', 'daily_sign', 'sync_daily_should_sign', '0 16 * * *'),
    ('daily_sign_1630', 'daily_sign', 'sync_daily_should_sign', '30 16 * * *'),
    ('daily_sign_1700', 'daily_sign', 'sync_daily_should_sign', '0 17 * * *'),
    ('daily_sign_1730', 'daily_sign', 'sync_daily_should_sign', '30 17 * * *'),
    ('daily_sign_1800', 'daily_sign', 'sync_daily_should_sign', '0 18 * * *'),
    ('delivery_status_0900', 'delivery_status', 'sync_delivery_status', '0 9 * * *'),
    ('delivery_status_1000', 'delivery_status', 'sync_delivery_status', '0 10 * * *'),
    ('delivery_status_1100', 'delivery_status', 'sync_delivery_status', '0 11 * * *'),
    ('delivery_status_1200', 'delivery_status', 'sync_delivery_status', '0 12 * * *'),
    ('delivery_status_1300', 'delivery_status', 'sync_delivery_status', '0 13 * * *'),
    ('delivery_status_1400', 'delivery_status', 'sync_delivery_status', '0 14 * * *'),
    ('delivery_status_1430', 'delivery_status', 'sync_delivery_status', '30 14 * * *'),
    ('delivery_status_1500', 'delivery_status', 'sync_delivery_status', '0 15 * * *'),
    ('delivery_status_1530', 'delivery_status', 'sync_delivery_status', '30 15 * * *'),
    ('delivery_status_1600', 'delivery_status', 'sync_delivery_status', '0 16 * * *'),
    ('delivery_status_1630', 'delivery_status', 'sync_delivery_status', '30 16 * * *'),
    ('delivery_status_1700', 'delivery_status', 'sync_delivery_status', '0 17 * * *'),
    ('delivery_status_1730', 'delivery_status', 'sync_delivery_status', '30 17 * * *'),
    ('delivery_status_1800', 'delivery_status', 'sync_delivery_status', '0 18 * * *'),
    ('delivery_status_1830', 'delivery_status', 'sync_delivery_status', '30 18 * * *'),
    ('delivery_status_1900', 'delivery_status', 'sync_delivery_status', '0 19 * * *'),
    ('delivery_status_1930', 'delivery_status', 'sync_delivery_status', '30 19 * * *'),
    ('delivery_status_2000', 'delivery_status', 'sync_delivery_status', '0 20 * * *'),
    ('delivery_status_2030', 'delivery_status', 'sync_delivery_status', '30 20 * * *'),
    ('delivery_status_2100', 'delivery_status', 'sync_delivery_status', '0 21 * * *'),
    ('send_order_2359', 'send_order', 'sync_daily_send_orders', '59 23 * * *'),
    ('site_send_0500', 'site_send', 'sync_site_send_list', '0 5 * * *'),
    ('site_send_0530', 'site_send', 'sync_site_send_list', '30 5 * * *'),
    ('site_send_1800', 'site_send', 'sync_site_send_list', '0 18 * * *'),
    ('site_send_1830', 'site_send', 'sync_site_send_list', '30 18 * * *'),
    ('site_send_1900', 'site_send', 'sync_site_send_list', '0 19 * * *'),
    ('site_send_1930', 'site_send', 'sync_site_send_list', '30 19 * * *'),
    ('site_send_2000', 'site_send', 'sync_site_send_list', '0 20 * * *'),
    ('site_send_2030', 'site_send', 'sync_site_send_list', '30 20 * * *'),
    ('site_send_2100', 'site_send', 'sync_site_send_list', '0 21 * * *'),
    ('yunda_send_waybills_2355', 'yunda_send_waybills', 'sync_yunda_send_waybills', '55 23 * * *');

DROP TEMPORARY TABLE IF EXISTS cp014_enabled_snapshot;

CREATE TEMPORARY TABLE cp014_enabled_snapshot (
    enabled_count BIGINT NOT NULL
) ENGINE=InnoDB;

INSERT INTO cp014_enabled_snapshot (enabled_count)
SELECT COUNT(*)
FROM scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
WHERE task.enabled = TRUE;

DROP TEMPORARY TABLE IF EXISTS cp014_preflight_guard;

CREATE TEMPORARY TABLE cp014_preflight_guard (
    missing_or_disabled_reviewed BIGINT NOT NULL,
    unknown_governed BIGINT NOT NULL,
    unknown_clock BIGINT NOT NULL,
    reviewed_binding BIGINT NOT NULL,
    clock_shape BIGINT NOT NULL,
    clock_policy_conflict BIGINT NOT NULL,
    arrive_shape BIGINT NOT NULL,
    arrive_site_values BIGINT NOT NULL,
    daily_shape BIGINT NOT NULL,
    delivery_shape BIGINT NOT NULL,
    send_shape BIGINT NOT NULL,
    site_shape BIGINT NOT NULL,
    yunda_send_shape BIGINT NOT NULL,
    CONSTRAINT cp014_all_reviewed_enabled CHECK (missing_or_disabled_reviewed = 0),
    CONSTRAINT cp014_no_unknown_governed CHECK (unknown_governed = 0),
    CONSTRAINT cp014_no_unknown_clock CHECK (unknown_clock = 0),
    CONSTRAINT cp014_no_binding_mismatch CHECK (reviewed_binding = 0),
    CONSTRAINT cp014_clock_shape_reviewed CHECK (clock_shape = 0),
    CONSTRAINT cp014_no_clock_write CHECK (clock_policy_conflict = 0),
    CONSTRAINT cp014_arrive_shape_closed CHECK (arrive_shape = 0),
    CONSTRAINT cp014_arrive_site_consistent CHECK (arrive_site_values = 0),
    CONSTRAINT cp014_daily_shape_closed CHECK (daily_shape = 0),
    CONSTRAINT cp014_delivery_shape_closed CHECK (delivery_shape = 0),
    CONSTRAINT cp014_send_shape_closed CHECK (send_shape = 0),
    CONSTRAINT cp014_site_shape_closed CHECK (site_shape = 0),
    CONSTRAINT cp014_yunda_send_shape_closed CHECK (yunda_send_shape = 0)
) ENGINE=InnoDB;

INSERT INTO cp014_preflight_guard (
    missing_or_disabled_reviewed,
    unknown_governed,
    unknown_clock,
    reviewed_binding,
    clock_shape,
    clock_policy_conflict,
    arrive_shape,
    arrive_site_values,
    daily_shape,
    delivery_shape,
    send_shape,
    site_shape,
    yunda_send_shape
)
SELECT
    (
        SELECT CASE
            WHEN EXISTS (
                SELECT 1
                FROM scheduled_tasks AS candidate
                LEFT JOIN cp014_expected_tasks AS candidate_expected
                    ON candidate_expected.id = candidate.id
                WHERE candidate_expected.id IS NOT NULL
                   OR candidate.id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')
                   OR LEFT(candidate.id, CHAR_LENGTH('clockin_')) = 'clockin_'
                   OR LEFT(candidate.id, CHAR_LENGTH('arrive_list_')) = 'arrive_list_'
                   OR LEFT(candidate.id, CHAR_LENGTH('daily_sign_')) = 'daily_sign_'
                   OR LEFT(candidate.id, CHAR_LENGTH('delivery_status_')) = 'delivery_status_'
                   OR LEFT(candidate.id, CHAR_LENGTH('send_order_')) = 'send_order_'
                   OR LEFT(candidate.id, CHAR_LENGTH('site_send_')) = 'site_send_'
                   OR LEFT(candidate.id, CHAR_LENGTH('yunda_send_waybills_'))
                       = 'yunda_send_waybills_'
                   OR candidate.tool_name IN (
                       'sync_daily_send_orders',
                       'sync_delivery_status',
                       'sync_daily_should_sign',
                       'sync_site_send_list',
                       'sync_arrive_list',
                       'sync_yunda_send_waybills'
                   )
                   OR candidate.tool_name = 'clock_in_dual'
                   OR (
                       candidate.tool_name = 'tms_query'
                       AND COALESCE(
                           JSON_UNQUOTE(JSON_EXTRACT(candidate.tool_params, '$.endpoint')),
                           JSON_UNQUOTE(JSON_EXTRACT(candidate.tool_params, '$.params.endpoint')),
                           ''
                       ) = '/clock_in_dual'
                   )
            )
            THEN (
                SELECT COUNT(*)
                FROM cp014_expected_tasks AS expected
                LEFT JOIN scheduled_tasks AS task ON task.id = expected.id
                WHERE task.id IS NULL OR NOT COALESCE(task.enabled = TRUE, FALSE)
            )
            ELSE 0
        END
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        LEFT JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE expected.id IS NULL
          AND (
              task.tool_name IN (
                  'sync_daily_send_orders',
                  'sync_delivery_status',
                  'sync_daily_should_sign',
                  'sync_site_send_list',
                  'sync_arrive_list',
                  'sync_yunda_send_waybills'
              )
              OR LEFT(task.id, CHAR_LENGTH('arrive_list_')) = 'arrive_list_'
              OR LEFT(task.id, CHAR_LENGTH('daily_sign_')) = 'daily_sign_'
              OR LEFT(task.id, CHAR_LENGTH('delivery_status_')) = 'delivery_status_'
              OR LEFT(task.id, CHAR_LENGTH('send_order_')) = 'send_order_'
              OR LEFT(task.id, CHAR_LENGTH('site_send_')) = 'site_send_'
              OR LEFT(task.id, CHAR_LENGTH('yunda_send_waybills_'))
                  = 'yunda_send_waybills_'
          )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        WHERE (
              LEFT(task.id, CHAR_LENGTH('clockin_')) = 'clockin_'
              OR task.tool_name = 'clock_in_dual'
              OR (
                  task.tool_name = 'tms_query'
                  AND COALESCE(
                      JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.endpoint')),
                      JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.params.endpoint')),
                      ''
                  ) = '/clock_in_dual'
              )
          )
          AND task.id NOT IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND (
              COALESCE(task.tool_name, '') <> expected.tool_name
              OR COALESCE(task.cron_expression, '') <> expected.cron_expression
          )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        WHERE task.id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')
          AND (
              task.tool_name NOT IN ('tms_query', 'clock_in_dual')
              OR task.tool_name IS NULL
              OR task.enabled NOT IN (FALSE, TRUE)
              OR task.enabled IS NULL
          )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        WHERE task.enabled = TRUE
          AND (
              task.id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')
              OR task.tool_name = 'clock_in_dual'
              OR (
                  task.tool_name = 'tms_query'
                  AND COALESCE(
                      JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.endpoint')),
                      JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.params.endpoint')),
                      ''
                  ) = '/clock_in_dual'
              )
          )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND expected.family = 'arrive_list'
          AND NOT COALESCE(
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 1
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default'
              )
              OR
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 4
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.login_site_code')) = 'STRING'
                  AND SHA2(
                      TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.login_site_code'))),
                      256
                  )
                      = 'c33492072957c7cc41ad8769d0c790b50d3b5314427e3912609432ea9d320912'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.site_code')) = 'STRING'
                  AND NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code'))), '') IS NOT NULL
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.target_date')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.target_date')) = ''
              ),
              FALSE
          )
    ),
    (
        SELECT CASE
            WHEN COUNT(DISTINCT JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code'))) <= 1 THEN 0
            ELSE 1
        END
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND expected.family = 'arrive_list'
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.site_code')) = 'STRING'
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND expected.family = 'daily_sign'
          AND NOT COALESCE(
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 5
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.problem_account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.problem_account_id')) = 'ronghui_daxiang_s'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sign_account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sign_account_id')) = 'ronghui_daxiang_s'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.detail_account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.detail_account_id')) = 'ronghui_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.days')) = 'INTEGER'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.days')) = '7'
              )
              OR
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 3
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'r13_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.detail_account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.detail_account_id')) = 'ronghui_default'
              ),
              FALSE
          )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND expected.family = 'delivery_status'
          AND NOT COALESCE(
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 0
              )
              OR
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 1
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default'
              ),
              FALSE
          )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND expected.family = 'send_order'
          AND NOT COALESCE(
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 1
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'price_default'
              )
              OR
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 2
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'price_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.target_date')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.target_date')) = ''
              ),
              FALSE
          )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND expected.family = 'site_send'
          AND NOT COALESCE(
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 1
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default',
              FALSE
          )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND expected.family = 'yunda_send_waybills'
          AND NOT COALESCE(
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 2
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'yunda_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.ensure_fields')) = 'BOOLEAN'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.ensure_fields')) = 'false'
              )
              OR
              (
                  JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                  AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 4
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'yunda_default'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.ensure_fields')) = 'BOOLEAN'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.ensure_fields')) = 'false'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.session_profile')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.session_profile')) = 'yunda'
                  AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.target_date')) = 'STRING'
                  AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.target_date')) = ''
              ),
              FALSE
          )
    );

CREATE TABLE IF NOT EXISTS control_plane_task_cutover_backup_014 LIKE scheduled_tasks;

INSERT IGNORE INTO control_plane_task_cutover_backup_014
SELECT task.*
FROM scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
WHERE task.enabled = TRUE;

START TRANSACTION;

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
SET task.tool_params = JSON_REMOVE(
    JSON_SET(
        COALESCE(task.tool_params, JSON_OBJECT()),
        '$.account_id', 'ronghui_default'
    ),
    '$.login_site_code',
    '$.site_code',
    '$.target_date'
)
WHERE task.enabled = TRUE
  AND expected.family = 'arrive_list';

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
SET task.tool_params = JSON_REMOVE(
    JSON_SET(
        COALESCE(task.tool_params, JSON_OBJECT()),
        '$.r13_account_id', 'r13_default',
        '$.problem_account_id', 'ronghui_daxiang_s',
        '$.sign_account_id', 'ronghui_daxiang_s',
        '$.detail_account_id', 'ronghui_default',
        '$.days', 7
    ),
    '$.account_id'
)
WHERE task.enabled = TRUE
  AND expected.family = 'daily_sign';

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
SET task.tool_params = JSON_SET(
    COALESCE(task.tool_params, JSON_OBJECT()),
    '$.account_id', 'ronghui_default'
)
WHERE task.enabled = TRUE
  AND expected.family = 'delivery_status';

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
SET task.tool_params = JSON_REMOVE(
    JSON_SET(
        COALESCE(task.tool_params, JSON_OBJECT()),
        '$.account_id', 'price_default'
    ),
    '$.target_date'
)
WHERE task.enabled = TRUE
  AND expected.family = 'send_order';

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
SET task.tool_params = JSON_SET(
    COALESCE(task.tool_params, JSON_OBJECT()),
    '$.account_id', 'ronghui_default'
)
WHERE task.enabled = TRUE
  AND expected.family = 'site_send';

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
SET task.tool_params = JSON_REMOVE(
    JSON_SET(
        COALESCE(task.tool_params, JSON_OBJECT()),
        '$.account_id', 'yunda_default',
        '$.ensure_fields', CAST('false' AS JSON)
    ),
    '$.session_profile',
    '$.target_date'
)
WHERE task.enabled = TRUE
  AND expected.family = 'yunda_send_waybills';

DROP TEMPORARY TABLE IF EXISTS cp014_postflight_guard;

CREATE TEMPORARY TABLE cp014_postflight_guard (
    enabled_count_changed BIGINT NOT NULL,
    noncanonical_rows BIGINT NOT NULL,
    CONSTRAINT cp014_enabled_count_preserved CHECK (enabled_count_changed = 0),
    CONSTRAINT cp014_all_rows_canonical CHECK (noncanonical_rows = 0)
) ENGINE=InnoDB;

INSERT INTO cp014_postflight_guard (enabled_count_changed, noncanonical_rows)
SELECT
    ABS(
        snapshot.enabled_count
        - (
            SELECT COUNT(*)
            FROM scheduled_tasks AS task
            INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
            WHERE task.enabled = TRUE
        )
    ),
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND NOT COALESCE(
              CASE expected.family
                  WHEN 'arrive_list' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 1
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default'
                  WHEN 'daily_sign' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 5
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.problem_account_id')) = 'ronghui_daxiang_s'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sign_account_id')) = 'ronghui_daxiang_s'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.detail_account_id')) = 'ronghui_default'
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.days')) = 'INTEGER'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.days')) = '7'
                  WHEN 'delivery_status' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 1
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default'
                  WHEN 'send_order' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 1
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'price_default'
                  WHEN 'site_send' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 1
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_default'
                  WHEN 'yunda_send_waybills' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 2
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'yunda_default'
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.ensure_fields')) = 'BOOLEAN'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.ensure_fields')) = 'false'
                  ELSE FALSE
              END,
              FALSE
          )
    )
FROM cp014_enabled_snapshot AS snapshot;

COMMIT;

DROP TEMPORARY TABLE IF EXISTS cp014_postflight_guard;
DROP TEMPORARY TABLE IF EXISTS cp014_preflight_guard;
DROP TEMPORARY TABLE IF EXISTS cp014_enabled_snapshot;
DROP TEMPORARY TABLE IF EXISTS cp014_expected_tasks;
