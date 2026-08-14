-- Cut over only the production schedule rows that were explicitly reviewed.
--
-- Safety model:
--   * temporary CHECK guards run before any permanent DDL or data mutation;
--   * an unknown governed task or unknown parameter shape aborts the whole
--     migration;
--   * the two reviewed third-party clock writes are optional only as an exact
--     enabled pair and are flattened from their exact c7 shape;
--   * the reviewed c7 finance and Yunda forecast schedules are optional by
--     presence; exact legacy/canonical rows are normalized even while disabled,
--     and their enabled/runtime state remains unchanged;
--   * the thirteen exact enabled R7 polling rows are accepted only as a complete
--     set with the reviewed IDs, crons, and closed parameter shape;
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

DROP TEMPORARY TABLE IF EXISTS cp014_expected_optional_tasks;

CREATE TEMPORARY TABLE cp014_expected_optional_tasks (
    id VARBINARY(128) PRIMARY KEY,
    family VARBINARY(64) NOT NULL,
    tool_name VARBINARY(128) NOT NULL,
    cron_expression VARBINARY(64) NOT NULL
) ENGINE=InnoDB;

INSERT INTO cp014_expected_optional_tasks (id, family, tool_name, cron_expression)
VALUES
    ('finance_bills_0010', 'finance_bills', 'sync_finance_bills', '10 0 * * *'),
    (
        'yunda_dispatch_forecast_1700',
        'yunda_dispatch_forecast',
        'sync_yunda_dispatch_forecast',
        '0 17 * * *'
    ),
    (
        'finance_startup_catchup',
        'finance_startup_catchup',
        'sync_finance_bills',
        '@startup'
    );

DROP TEMPORARY TABLE IF EXISTS cp014_expected_r7_tasks;

CREATE TEMPORARY TABLE cp014_expected_r7_tasks (
    id VARBINARY(128) PRIMARY KEY,
    cron_expression VARBINARY(64) NOT NULL
) ENGINE=InnoDB;

INSERT INTO cp014_expected_r7_tasks (id, cron_expression)
VALUES
    ('r7_arrival_checkin_0900', '0 9 * * *'),
    ('r7_arrival_checkin_0930', '30 9 * * *'),
    ('r7_arrival_checkin_1000', '0 10 * * *'),
    ('r7_arrival_checkin_1030', '30 10 * * *'),
    ('r7_arrival_checkin_1100', '0 11 * * *'),
    ('r7_arrival_checkin_1130', '30 11 * * *'),
    ('r7_arrival_checkin_1200', '0 12 * * *'),
    ('r7_arrival_checkin_1230', '30 12 * * *'),
    ('r7_arrival_checkin_1300', '0 13 * * *'),
    ('r7_arrival_checkin_1330', '30 13 * * *'),
    ('r7_arrival_checkin_1400', '0 14 * * *'),
    ('r7_arrival_checkin_1430', '30 14 * * *'),
    ('r7_arrival_checkin_1900', '0 19 * * *');

DROP TEMPORARY TABLE IF EXISTS cp014_expected_clocks;

CREATE TEMPORARY TABLE cp014_expected_clocks (
    id VARBINARY(128) PRIMARY KEY,
    cron_expression VARBINARY(64) NOT NULL,
    account_id VARBINARY(128) NOT NULL,
    sitecode VARBINARY(64) NOT NULL,
    sitefbcode VARBINARY(64) NOT NULL,
    sitename VARBINARY(128) NOT NULL,
    sitefbname VARBINARY(128) NOT NULL,
    first_type VARBINARY(128) NOT NULL,
    second_type VARBINARY(128) NOT NULL,
    delay_seconds BIGINT NOT NULL,
    legacy_has_site_codes BOOLEAN NOT NULL
) ENGINE=InnoDB;

INSERT INTO cp014_expected_clocks (
    id,
    cron_expression,
    account_id,
    sitecode,
    sitefbcode,
    sitename,
    sitefbname,
    first_type,
    second_type,
    delay_seconds,
    legacy_has_site_codes
)
VALUES
    (
        'clockin_daxiang_1830',
        '30 18 * * *',
        'ronghui_default',
        '7390004',
        '73901',
        '邵阳大祥站',
        '邵阳操作场',
        '交件到港',
        '接件离港',
        2,
        FALSE
    ),
    (
        'clockin_daxiang_s_1833',
        '33 18 * * *',
        'ronghui_daxiang_s',
        '7390017',
        '73901',
        '邵阳大祥S站',
        '邵阳操作场',
        '交件到港',
        '接件离港',
        2,
        TRUE
    );

DROP TEMPORARY TABLE IF EXISTS cp014_enabled_snapshot;

CREATE TEMPORARY TABLE cp014_enabled_snapshot (
    enabled_count BIGINT NOT NULL
) ENGINE=InnoDB;

INSERT INTO cp014_enabled_snapshot (enabled_count)
SELECT
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
    )
    +
    (
        SELECT CASE
            WHEN EXISTS (
                SELECT 1
                FROM scheduled_tasks AS finance
                WHERE finance.id = 'finance_bills_0010'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM scheduled_tasks AS startup
                WHERE startup.id = 'finance_startup_catchup'
            )
            THEN 1
            ELSE 0
        END
    )
    +
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
    )
    +
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_clocks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
    )
    +
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_r7_tasks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
    );

DROP TEMPORARY TABLE IF EXISTS cp014_preflight_guard;

CREATE TEMPORARY TABLE cp014_preflight_guard (
    missing_or_disabled_reviewed BIGINT NOT NULL,
    unknown_governed BIGINT NOT NULL,
    unknown_clock BIGINT NOT NULL,
    reviewed_binding BIGINT NOT NULL,
    optional_binding BIGINT NOT NULL,
    clock_pair BIGINT NOT NULL,
    clock_shape BIGINT NOT NULL,
    arrive_shape BIGINT NOT NULL,
    arrive_site_values BIGINT NOT NULL,
    daily_shape BIGINT NOT NULL,
    delivery_shape BIGINT NOT NULL,
    send_shape BIGINT NOT NULL,
    site_shape BIGINT NOT NULL,
    yunda_send_shape BIGINT NOT NULL,
    finance_shape BIGINT NOT NULL,
    finance_startup_shape BIGINT NOT NULL,
    yunda_dispatch_shape BIGINT NOT NULL,
    r7_binding BIGINT NOT NULL,
    r7_shape BIGINT NOT NULL,
    CONSTRAINT cp014_all_reviewed_enabled CHECK (missing_or_disabled_reviewed = 0),
    CONSTRAINT cp014_no_unknown_governed CHECK (unknown_governed = 0),
    CONSTRAINT cp014_no_unknown_clock CHECK (unknown_clock = 0),
    CONSTRAINT cp014_no_binding_mismatch CHECK (reviewed_binding = 0),
    CONSTRAINT cp014_no_optional_binding_mismatch CHECK (optional_binding = 0),
    CONSTRAINT cp014_clock_pair_complete CHECK (clock_pair = 0),
    CONSTRAINT cp014_clock_shape_reviewed CHECK (clock_shape = 0),
    CONSTRAINT cp014_arrive_shape_closed CHECK (arrive_shape = 0),
    CONSTRAINT cp014_arrive_site_consistent CHECK (arrive_site_values = 0),
    CONSTRAINT cp014_daily_shape_closed CHECK (daily_shape = 0),
    CONSTRAINT cp014_delivery_shape_closed CHECK (delivery_shape = 0),
    CONSTRAINT cp014_send_shape_closed CHECK (send_shape = 0),
    CONSTRAINT cp014_site_shape_closed CHECK (site_shape = 0),
    CONSTRAINT cp014_yunda_send_shape_closed CHECK (yunda_send_shape = 0),
    CONSTRAINT cp014_finance_shape_closed CHECK (finance_shape = 0),
    CONSTRAINT cp014_finance_startup_shape_closed CHECK (finance_startup_shape = 0),
    CONSTRAINT cp014_yunda_dispatch_shape_closed CHECK (yunda_dispatch_shape = 0),
    CONSTRAINT cp014_r7_binding_closed CHECK (r7_binding = 0),
    CONSTRAINT cp014_r7_shape_closed CHECK (r7_shape = 0)
) ENGINE=InnoDB;

INSERT INTO cp014_preflight_guard (
    missing_or_disabled_reviewed,
    unknown_governed,
    unknown_clock,
    reviewed_binding,
    optional_binding,
    clock_pair,
    clock_shape,
    arrive_shape,
    arrive_site_values,
    daily_shape,
    delivery_shape,
    send_shape,
    site_shape,
    yunda_send_shape,
    finance_shape,
    finance_startup_shape,
    yunda_dispatch_shape,
    r7_binding,
    r7_shape
)
VALUES (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);

-- MySQL cannot reference the same temporary table under multiple aliases in a
-- single statement.  Keep every guard calculation independent so each one
-- reads cp014_expected_tasks at most once and still fails before permanent DDL.
UPDATE cp014_preflight_guard
SET missing_or_disabled_reviewed = (
    SELECT CASE
        WHEN EXISTS (
            SELECT 1
            FROM scheduled_tasks AS candidate
            WHERE candidate.id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1833')
               OR LEFT(candidate.id, CHAR_LENGTH('clockin_')) = 'clockin_'
               OR LEFT(candidate.id, CHAR_LENGTH('arrive_list_')) = 'arrive_list_'
               OR LEFT(candidate.id, CHAR_LENGTH('daily_sign_')) = 'daily_sign_'
               OR LEFT(candidate.id, CHAR_LENGTH('delivery_status_')) = 'delivery_status_'
               OR LEFT(candidate.id, CHAR_LENGTH('send_order_')) = 'send_order_'
               OR LEFT(candidate.id, CHAR_LENGTH('site_send_')) = 'site_send_'
               OR LEFT(candidate.id, CHAR_LENGTH('yunda_send_waybills_'))
                    = 'yunda_send_waybills_'
               OR LEFT(candidate.id, CHAR_LENGTH('finance_bills_')) = 'finance_bills_'
               OR LEFT(candidate.id, CHAR_LENGTH('yunda_dispatch_forecast_'))
                    = 'yunda_dispatch_forecast_'
               OR LEFT(candidate.id, CHAR_LENGTH('r7_arrival_checkin_'))
                    = 'r7_arrival_checkin_'
               OR candidate.tool_name IN (
                    'sync_daily_send_orders',
                    'sync_delivery_status',
                    'sync_daily_should_sign',
                    'sync_site_send_list',
                    'sync_arrive_list',
                    'sync_yunda_send_waybills',
                    'sync_finance_bills',
                    'sync_yunda_dispatch_forecast'
                    ,'r7_arrival_checkin'
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
);

UPDATE cp014_preflight_guard
SET unknown_governed = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    LEFT JOIN cp014_expected_tasks AS expected ON expected.id = task.id
    LEFT JOIN cp014_expected_optional_tasks AS optional ON optional.id = task.id
    LEFT JOIN cp014_expected_r7_tasks AS r7 ON r7.id = task.id
    WHERE expected.id IS NULL
      AND optional.id IS NULL
      AND r7.id IS NULL
      AND (
          task.tool_name IN (
              'sync_daily_send_orders',
              'sync_delivery_status',
              'sync_daily_should_sign',
              'sync_site_send_list',
              'sync_arrive_list',
              'sync_yunda_send_waybills',
              'sync_finance_bills',
              'sync_yunda_dispatch_forecast'
              ,'r7_arrival_checkin'
          )
          OR LEFT(task.id, CHAR_LENGTH('arrive_list_')) = 'arrive_list_'
          OR LEFT(task.id, CHAR_LENGTH('daily_sign_')) = 'daily_sign_'
          OR LEFT(task.id, CHAR_LENGTH('delivery_status_')) = 'delivery_status_'
          OR LEFT(task.id, CHAR_LENGTH('send_order_')) = 'send_order_'
          OR LEFT(task.id, CHAR_LENGTH('site_send_')) = 'site_send_'
          OR LEFT(task.id, CHAR_LENGTH('yunda_send_waybills_')) = 'yunda_send_waybills_'
          OR LEFT(task.id, CHAR_LENGTH('finance_bills_')) = 'finance_bills_'
          OR LEFT(task.id, CHAR_LENGTH('yunda_dispatch_forecast_'))
                = 'yunda_dispatch_forecast_'
          OR LEFT(task.id, CHAR_LENGTH('r7_arrival_checkin_'))
                = 'r7_arrival_checkin_'
      )
);

UPDATE cp014_preflight_guard
SET r7_binding = (
    SELECT CASE
        WHEN EXISTS (
            SELECT 1
            FROM scheduled_tasks AS candidate
            WHERE LEFT(candidate.id, CHAR_LENGTH('r7_arrival_checkin_'))
                    = 'r7_arrival_checkin_'
               OR candidate.tool_name = 'r7_arrival_checkin'
        )
        THEN (
            SELECT COUNT(*)
            FROM cp014_expected_r7_tasks AS expected
            LEFT JOIN scheduled_tasks AS task ON task.id = expected.id
            WHERE task.id IS NULL
               OR task.tool_name <> 'r7_arrival_checkin'
               OR task.cron_expression <> expected.cron_expression
               OR NOT COALESCE(task.enabled = TRUE, FALSE)
        )
        ELSE 0
    END
);

UPDATE cp014_preflight_guard
SET r7_shape = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_r7_tasks AS expected ON expected.id = task.id
      AND NOT COALESCE(
          JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
          AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 10
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'r7_default'
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.headless')) = 'BOOLEAN'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.headless')) = 'true'
          AND JSON_EXTRACT(task.tool_params, '$.flow_mode') = 1
          AND JSON_EXTRACT(task.tool_params, '$.slow_mo_ms') = 0
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.status_text')) = '车辆到达'
          AND JSON_EXTRACT(task.tool_params, '$.max_login_attempts') = 6
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.verify_status_text')) = '已到达'
          AND JSON_EXTRACT(task.tool_params, '$.daily_success_limit') = 1
          AND JSON_EXTRACT(task.tool_params, '$.after_action_delay_ms') = 1500
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.do_arrive_wait_unload')) = 'BOOLEAN'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.do_arrive_wait_unload')) = 'true',
          FALSE
      )
);

UPDATE cp014_preflight_guard
SET unknown_clock = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    LEFT JOIN cp014_expected_clocks AS expected ON expected.id = task.id
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
      AND expected.id IS NULL
);

UPDATE cp014_preflight_guard
SET reviewed_binding = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
    WHERE task.enabled = TRUE
      AND (
          COALESCE(task.tool_name, '') <> expected.tool_name
          OR COALESCE(task.cron_expression, '') <> expected.cron_expression
      )
);

UPDATE cp014_preflight_guard
SET optional_binding = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
    WHERE COALESCE(task.tool_name, '') <> expected.tool_name
       OR COALESCE(task.cron_expression, '') <> expected.cron_expression
);

UPDATE cp014_preflight_guard
SET clock_pair = (
    SELECT CASE
        WHEN COUNT(*) IN (0, 2) THEN 0
        ELSE 1
    END
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_clocks AS expected ON expected.id = task.id
);

UPDATE cp014_preflight_guard
SET clock_shape = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_clocks AS expected ON expected.id = task.id
    WHERE NOT COALESCE(
        task.enabled = TRUE
        AND task.cron_expression = expected.cron_expression
        AND (
            (
                task.tool_name = 'clock_in_dual'
                AND JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 8
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id'))
                    = expected.account_id
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sitecode')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sitecode'))
                    = expected.sitecode
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sitefbcode')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sitefbcode'))
                    = expected.sitefbcode
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sitename')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sitename'))
                    = expected.sitename
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sitefbname')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sitefbname'))
                    = expected.sitefbname
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.first_type')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.first_type'))
                    = expected.first_type
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.second_type')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.second_type'))
                    = expected.second_type
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.delay_seconds')) = 'INTEGER'
                AND JSON_EXTRACT(task.tool_params, '$.delay_seconds') = expected.delay_seconds
            )
            OR
            (
                task.tool_name = 'tms_query'
                AND JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 2
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.endpoint')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.endpoint'))
                    = '/clock_in_dual'
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params')) = 'OBJECT'
                AND JSON_LENGTH(JSON_EXTRACT(task.tool_params, '$.params')) = 2
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params.timeout_sec')) = 'INTEGER'
                AND JSON_EXTRACT(task.tool_params, '$.params.timeout_sec') = 600
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params.params')) = 'OBJECT'
                AND JSON_LENGTH(JSON_EXTRACT(task.tool_params, '$.params.params'))
                    = CASE WHEN expected.legacy_has_site_codes THEN 8 ELSE 6 END
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params.params.mode')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.params.params.mode')) = 'api'
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params.params.site_name')) = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.params.params.site_name'))
                    = expected.sitename
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params.params.site_fb_name'))
                    = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.params.params.site_fb_name'))
                    = expected.sitefbname
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params.params.first_type'))
                    = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.params.params.first_type'))
                    = expected.first_type
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params.params.second_type'))
                    = 'STRING'
                AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.params.params.second_type'))
                    = expected.second_type
                AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.params.params.delay_seconds'))
                    = 'INTEGER'
                AND JSON_EXTRACT(task.tool_params, '$.params.params.delay_seconds')
                    = expected.delay_seconds
                AND (
                    (
                        expected.legacy_has_site_codes = FALSE
                        AND JSON_EXTRACT(task.tool_params, '$.params.params.sitecode') IS NULL
                        AND JSON_EXTRACT(task.tool_params, '$.params.params.sitefbcode') IS NULL
                    )
                    OR
                    (
                        expected.legacy_has_site_codes = TRUE
                        AND JSON_TYPE(
                            JSON_EXTRACT(task.tool_params, '$.params.params.sitecode')
                        ) = 'STRING'
                        AND JSON_UNQUOTE(
                            JSON_EXTRACT(task.tool_params, '$.params.params.sitecode')
                        ) = expected.sitecode
                        AND JSON_TYPE(
                            JSON_EXTRACT(task.tool_params, '$.params.params.sitefbcode')
                        ) = 'STRING'
                        AND JSON_UNQUOTE(
                            JSON_EXTRACT(task.tool_params, '$.params.params.sitefbcode')
                        ) = expected.sitefbcode
                    )
                )
            )
        ),
        FALSE
    )
);

UPDATE cp014_preflight_guard
SET arrive_shape = (
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
              ) = 'c33492072957c7cc41ad8769d0c790b50d3b5314427e3912609432ea9d320912'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.site_code')) = 'STRING'
              AND NULLIF(
                  TRIM(JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code'))),
                  ''
              ) IS NOT NULL
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.target_date')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.target_date')) = ''
          ),
          FALSE
      )
);

UPDATE cp014_preflight_guard
SET arrive_site_values = (
    SELECT CASE
        WHEN COUNT(DISTINCT JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.site_code'))) <= 1
        THEN 0
        ELSE 1
    END
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
    WHERE task.enabled = TRUE
      AND expected.family = 'arrive_list'
      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.site_code')) = 'STRING'
);

UPDATE cp014_preflight_guard
SET daily_shape = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
    WHERE task.enabled = TRUE
      AND expected.family = 'daily_sign'
      AND NOT COALESCE(
          (
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 3
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
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
);

UPDATE cp014_preflight_guard
SET delivery_shape = (
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
);

UPDATE cp014_preflight_guard
SET send_shape = (
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
);

UPDATE cp014_preflight_guard
SET site_shape = (
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
);

UPDATE cp014_preflight_guard
SET yunda_send_shape = (
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

-- The c7 finance schedule had no persisted platform selector.  Runtime added
-- the only production-ready platform (Ronghui) before execution, so adding
-- that same locked selector is an exact semantic normalization.  target_date
-- remains a per-occurrence scheduler argument and must not be persisted here.
UPDATE cp014_preflight_guard
SET finance_shape = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
    WHERE expected.family = 'finance_bills'
      AND NOT COALESCE(
          (
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 3
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'sync'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.platform')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.platform')) = 'ronghui'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.rescan_days')) = 'INTEGER'
              AND JSON_EXTRACT(task.tool_params, '$.rescan_days') = 7
          )
          OR
          (
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 2
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'sync'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.rescan_days')) = 'INTEGER'
              AND JSON_EXTRACT(task.tool_params, '$.rescan_days') = 7
          ),
          FALSE
      )
);

UPDATE cp014_preflight_guard
SET finance_startup_shape = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
    WHERE expected.family = 'finance_startup_catchup'
      AND NOT COALESCE(
          JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
          AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 4
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'STRING'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'sync'
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.platform')) = 'STRING'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.platform')) = 'ronghui'
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.rescan_days')) = 'INTEGER'
          AND JSON_EXTRACT(task.tool_params, '$.rescan_days') = 7
          AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$._startup_catchup')) = 'BOOLEAN'
          AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$._startup_catchup')) = 'true',
          FALSE
      )
);

-- The c7 Yunda profile name selected the same isolated account now represented
-- by account_id=yunda_default.  The reviewed destination branch is retained
-- byte-for-byte; no default or first-match branch is inferred.
UPDATE cp014_preflight_guard
SET yunda_dispatch_shape = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
    WHERE expected.family = 'yunda_dispatch_forecast'
      AND NOT COALESCE(
          (
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 2
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id'))
                    = 'yunda_default'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.dest_brch')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.dest_brch')) = '56739382'
          )
          OR
          (
              JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 2
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.session_profile')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.session_profile')) = 'yunda'
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.dest_brch')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.dest_brch')) = '56739382'
          ),
          FALSE
      )
);

CREATE TABLE IF NOT EXISTS control_plane_task_cutover_backup_014 LIKE scheduled_tasks;

CREATE TABLE IF NOT EXISTS control_plane_task_cutover_created_014 (
    task_id VARCHAR(128) PRIMARY KEY,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO control_plane_task_cutover_backup_014 (
    id, name, tool_name, tool_params, cron_expression, enabled,
    last_run, last_status, last_duration_ms, last_message, created_at
)
SELECT
    task.id, task.name, task.tool_name, task.tool_params, task.cron_expression,
    task.enabled, task.last_run, task.last_status, task.last_duration_ms,
    task.last_message, task.created_at
FROM scheduled_tasks AS task
INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
WHERE task.enabled = TRUE;

INSERT IGNORE INTO control_plane_task_cutover_backup_014 (
    id, name, tool_name, tool_params, cron_expression, enabled,
    last_run, last_status, last_duration_ms, last_message, created_at
)
SELECT
    task.id, task.name, task.tool_name, task.tool_params, task.cron_expression,
    task.enabled, task.last_run, task.last_status, task.last_duration_ms,
    task.last_message, task.created_at
FROM scheduled_tasks AS task
INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
WHERE TRUE;

INSERT IGNORE INTO control_plane_task_cutover_backup_014 (
    id, name, tool_name, tool_params, cron_expression, enabled,
    last_run, last_status, last_duration_ms, last_message, created_at
)
SELECT
    task.id, task.name, task.tool_name, task.tool_params, task.cron_expression,
    task.enabled, task.last_run, task.last_status, task.last_duration_ms,
    task.last_message, task.created_at
FROM scheduled_tasks AS task
INNER JOIN cp014_expected_clocks AS expected ON expected.id = task.id
WHERE task.enabled = TRUE;

INSERT IGNORE INTO control_plane_task_cutover_backup_014 (
    id, name, tool_name, tool_params, cron_expression, enabled,
    last_run, last_status, last_duration_ms, last_message, created_at
)
SELECT
    task.id, task.name, task.tool_name, task.tool_params, task.cron_expression,
    task.enabled, task.last_run, task.last_status, task.last_duration_ms,
    task.last_message, task.created_at
FROM scheduled_tasks AS task
INNER JOIN cp014_expected_r7_tasks AS expected ON expected.id = task.id;

START TRANSACTION;

INSERT IGNORE INTO control_plane_task_cutover_created_014 (task_id)
SELECT 'finance_startup_catchup'
FROM scheduled_tasks AS finance
WHERE finance.id = 'finance_bills_0010'
  AND NOT EXISTS (
      SELECT 1
      FROM scheduled_tasks AS startup
      WHERE startup.id = 'finance_startup_catchup'
  );

-- c7 ran this gap scan after every service start independently of the 00:10
-- task's enabled switch.  Materialize that independent behavior as its own
-- persisted task only when the reviewed c7 finance row already exists.  A
-- fresh database receives only the disabled template after migration.
INSERT IGNORE INTO scheduled_tasks (
    id, name, tool_name, tool_params, cron_expression, enabled
)
SELECT
    'finance_startup_catchup',
    '财务启动缺口扫描',
    'sync_finance_bills',
    JSON_OBJECT(
        'mode', 'sync',
        'platform', 'ronghui',
        'rescan_days', 7,
        '_startup_catchup', CAST('true' AS JSON)
    ),
    '@startup',
    TRUE
FROM scheduled_tasks AS finance
WHERE finance.id = 'finance_bills_0010';

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
        '$.account_id', 'ronghui_daxiang_s',
        '$.days', 7
    ),
    '$.problem_account_id',
    '$.sign_account_id',
    '$.detail_account_id'
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

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
SET task.tool_params = JSON_SET(
    COALESCE(task.tool_params, JSON_OBJECT()),
    '$.platform', 'ronghui'
)
WHERE expected.family = 'finance_bills';

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
SET task.tool_params = JSON_REMOVE(
    JSON_SET(
        COALESCE(task.tool_params, JSON_OBJECT()),
        '$.account_id', 'yunda_default'
    ),
    '$.session_profile'
)
WHERE expected.family = 'yunda_dispatch_forecast';

UPDATE scheduled_tasks AS task
INNER JOIN cp014_expected_clocks AS expected ON expected.id = task.id
SET
    task.tool_name = 'clock_in_dual',
    task.tool_params = JSON_OBJECT(
        'account_id', CONVERT(expected.account_id USING utf8mb4),
        'sitecode', CONVERT(expected.sitecode USING utf8mb4),
        'sitefbcode', CONVERT(expected.sitefbcode USING utf8mb4),
        'sitename', CONVERT(expected.sitename USING utf8mb4),
        'sitefbname', CONVERT(expected.sitefbname USING utf8mb4),
        'first_type', CONVERT(expected.first_type USING utf8mb4),
        'second_type', CONVERT(expected.second_type USING utf8mb4),
        'delay_seconds', expected.delay_seconds
    )
WHERE task.enabled = TRUE;

DROP TEMPORARY TABLE IF EXISTS cp014_postflight_guard;

CREATE TEMPORARY TABLE cp014_postflight_guard (
    enabled_count_changed BIGINT NOT NULL,
    noncanonical_rows BIGINT NOT NULL,
    CONSTRAINT cp014_enabled_count_preserved CHECK (enabled_count_changed = 0),
    CONSTRAINT cp014_all_rows_canonical CHECK (noncanonical_rows = 0)
) ENGINE=InnoDB;

INSERT INTO cp014_postflight_guard (enabled_count_changed, noncanonical_rows)
VALUES (0, 0);

UPDATE cp014_postflight_guard AS guard
CROSS JOIN cp014_enabled_snapshot AS snapshot
SET guard.enabled_count_changed = ABS(
    snapshot.enabled_count
    - (
        (
            SELECT COUNT(*)
            FROM scheduled_tasks AS task
            INNER JOIN cp014_expected_tasks AS expected ON expected.id = task.id
            WHERE task.enabled = TRUE
        )
        +
        (
            SELECT COUNT(*)
            FROM scheduled_tasks AS task
            INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
            WHERE task.enabled = TRUE
        )
        +
        (
            SELECT COUNT(*)
            FROM scheduled_tasks AS task
            INNER JOIN cp014_expected_clocks AS expected ON expected.id = task.id
            WHERE task.enabled = TRUE
        )
        +
        (
            SELECT COUNT(*)
            FROM scheduled_tasks AS task
            INNER JOIN cp014_expected_r7_tasks AS expected ON expected.id = task.id
            WHERE task.enabled = TRUE
        )
    )
);

UPDATE cp014_postflight_guard
SET noncanonical_rows = (
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
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 3
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) = 'r13_default'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'ronghui_daxiang_s'
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
    +
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_optional_tasks AS expected ON expected.id = task.id
        WHERE NOT COALESCE(
              CASE expected.family
                  WHEN 'finance_bills' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 3
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'STRING'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'sync'
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.platform')) = 'STRING'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.platform')) = 'ronghui'
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.rescan_days')) = 'INTEGER'
                      AND JSON_EXTRACT(task.tool_params, '$.rescan_days') = 7
                  WHEN 'finance_startup_catchup' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 4
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'STRING'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.mode')) = 'sync'
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.platform')) = 'STRING'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.platform')) = 'ronghui'
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.rescan_days')) = 'INTEGER'
                      AND JSON_EXTRACT(task.tool_params, '$.rescan_days') = 7
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$._startup_catchup')) = 'BOOLEAN'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$._startup_catchup')) = 'true'
                  WHEN 'yunda_dispatch_forecast' THEN
                      JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
                      AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 2
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id'))
                            = 'yunda_default'
                      AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.dest_brch')) = 'STRING'
                      AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.dest_brch')) = '56739382'
                  ELSE FALSE
              END,
              FALSE
          )
    )
    +
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_clocks AS expected ON expected.id = task.id
        WHERE task.enabled = TRUE
          AND NOT COALESCE(
              task.tool_name = 'clock_in_dual'
              AND task.cron_expression = expected.cron_expression
              AND JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
              AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 8
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id'))
                  = expected.account_id
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sitecode')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sitecode'))
                  = expected.sitecode
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sitefbcode')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sitefbcode'))
                  = expected.sitefbcode
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sitename')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sitename'))
                  = expected.sitename
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.sitefbname')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.sitefbname'))
                  = expected.sitefbname
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.first_type')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.first_type'))
                  = expected.first_type
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.second_type')) = 'STRING'
              AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.second_type'))
                  = expected.second_type
              AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.delay_seconds')) = 'INTEGER'
              AND JSON_EXTRACT(task.tool_params, '$.delay_seconds') = expected.delay_seconds,
              FALSE
          )
    )
    +
    (
        SELECT COUNT(*)
        FROM scheduled_tasks AS task
        INNER JOIN cp014_expected_r7_tasks AS expected ON expected.id = task.id
        WHERE NOT COALESCE(
            task.enabled = TRUE
            AND task.tool_name = 'r7_arrival_checkin'
            AND task.cron_expression = expected.cron_expression
            AND JSON_TYPE(COALESCE(task.tool_params, JSON_OBJECT())) = 'OBJECT'
            AND JSON_LENGTH(COALESCE(task.tool_params, JSON_OBJECT())) = 10
            AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) = 'r7_default'
            AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.headless')) = 'BOOLEAN'
            AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.headless')) = 'true'
            AND JSON_EXTRACT(task.tool_params, '$.flow_mode') = 1
            AND JSON_EXTRACT(task.tool_params, '$.slow_mo_ms') = 0
            AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.status_text')) = '车辆到达'
            AND JSON_EXTRACT(task.tool_params, '$.max_login_attempts') = 6
            AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.verify_status_text')) = '已到达'
            AND JSON_EXTRACT(task.tool_params, '$.daily_success_limit') = 1
            AND JSON_EXTRACT(task.tool_params, '$.after_action_delay_ms') = 1500
            AND JSON_TYPE(JSON_EXTRACT(task.tool_params, '$.do_arrive_wait_unload')) = 'BOOLEAN'
            AND JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.do_arrive_wait_unload')) = 'true',
            FALSE
        )
    )
);

COMMIT;

DROP TEMPORARY TABLE IF EXISTS cp014_postflight_guard;
DROP TEMPORARY TABLE IF EXISTS cp014_preflight_guard;
DROP TEMPORARY TABLE IF EXISTS cp014_enabled_snapshot;
DROP TEMPORARY TABLE IF EXISTS cp014_expected_clocks;
DROP TEMPORARY TABLE IF EXISTS cp014_expected_r7_tasks;
DROP TEMPORARY TABLE IF EXISTS cp014_expected_optional_tasks;
DROP TEMPORARY TABLE IF EXISTS cp014_expected_tasks;
