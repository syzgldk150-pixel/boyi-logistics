-- Project-scoped automation authorization and plugin-instance persistence.
-- This is a forward-only migration. Production-applied 014-017 stay immutable.
-- plugin_id identifies immutable signed package code; automation_id identifies
-- one independently configured installation instance.

-- This is a migration-only reviewed identity map. It deliberately contains
-- exact legacy task ids; runtime schedule creation must supply automation_id
-- from an installed project and must never infer it from this table.
CREATE TABLE IF NOT EXISTS automation_project_reviewed_schedule_map_018 (
    task_id VARCHAR(128) NOT NULL,
    expected_tool_name VARCHAR(128) NOT NULL,
    automation_id VARCHAR(128) NOT NULL,
    PRIMARY KEY (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO automation_project_reviewed_schedule_map_018 (
    task_id, expected_tool_name, automation_id
) VALUES
    ('clockin_daxiang_1830', 'clock_in_dual', 'clockin_daxiang'),
    ('clockin_daxiang_s_1833', 'clock_in_dual', 'clockin_daxiang_s'),
    ('finance_bills_0010', 'sync_finance_bills', 'finance_bills'),
    ('finance_startup_catchup', 'sync_finance_bills', 'finance_startup_catchup'),
    ('customer_problems_shadow', 'sync_customer_service_problems', 'customer_problems_shadow'),
    ('r7_departure_checkin', 'r7_departure_checkin', 'r7_departure_checkin'),
    ('send_order_2359', 'sync_daily_send_orders', 'send_order'),
    ('delivery_status_0900', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1000', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1100', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1200', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1300', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1400', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1430', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1500', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1530', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1600', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1630', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1700', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1730', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1800', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1830', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1900', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_1930', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_2000', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_2030', 'sync_delivery_status', 'delivery_status'),
    ('delivery_status_2100', 'sync_delivery_status', 'delivery_status'),
    ('daily_sign_0500', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_0700', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_0800', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_0900', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1000', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1100', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1200', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1300', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1400', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1430', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1500', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1530', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1600', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1630', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1700', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1730', 'sync_daily_should_sign', 'daily_sign'),
    ('daily_sign_1800', 'sync_daily_should_sign', 'daily_sign'),
    ('site_send_0500', 'sync_site_send_list', 'site_send'),
    ('site_send_0530', 'sync_site_send_list', 'site_send'),
    ('site_send_1800', 'sync_site_send_list', 'site_send'),
    ('site_send_1830', 'sync_site_send_list', 'site_send'),
    ('site_send_1900', 'sync_site_send_list', 'site_send'),
    ('site_send_1930', 'sync_site_send_list', 'site_send'),
    ('site_send_2000', 'sync_site_send_list', 'site_send'),
    ('site_send_2030', 'sync_site_send_list', 'site_send'),
    ('site_send_2100', 'sync_site_send_list', 'site_send'),
    ('r7_arrival_checkin_0900', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_0930', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1000', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1030', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1100', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1130', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1200', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1230', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1300', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1330', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1400', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1430', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('r7_arrival_checkin_1900', 'r7_arrival_checkin', 'r7_arrival_checkin'),
    ('arrive_list_0830', 'sync_arrive_list', 'arrive_list'),
    ('arrive_list_0900', 'sync_arrive_list', 'arrive_list'),
    ('arrive_list_0930', 'sync_arrive_list', 'arrive_list'),
    ('yunda_dispatch_forecast_1700', 'sync_yunda_dispatch_forecast', 'yunda_dispatch_forecast'),
    ('yunda_send_waybills_2355', 'sync_yunda_send_waybills', 'yunda_send_waybills')
ON DUPLICATE KEY UPDATE
    expected_tool_name = VALUES(expected_tool_name),
    automation_id = VALUES(automation_id);

-- Exact reviewed external-resource identities for the legacy Yunda,
-- problem-action, send-order, arrival, daily-sign and site-send instances,
-- including the code-owned trusted entrypoint routes they bind.
-- Runtime code binds projects by the
-- explicit resource ids below; neither migration nor execution may infer a
-- resource from a project/key suffix. The Yunda and problem-sheet locators
-- have reviewed code-owned defaults. All other resources must already exist
-- and are never guessed or materialized by this migration.
CREATE TABLE IF NOT EXISTS automation_project_reviewed_resource_map_018 (
    resource_key VARCHAR(128) NOT NULL,
    expected_kind VARCHAR(32) NOT NULL,
    materialize_missing BOOLEAN NOT NULL,
    default_config_json JSON NULL,
    PRIMARY KEY (resource_key),
    CONSTRAINT chk_automation_project_reviewed_resource_materialization CHECK (
        (materialize_missing = TRUE AND default_config_json IS NOT NULL)
        OR (materialize_missing = FALSE AND default_config_json IS NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO automation_project_reviewed_resource_map_018 (
    resource_key, expected_kind, materialize_missing, default_config_json
) VALUES
    (
        'phase7.delivery_status_bitable',
        'feishu_bitable',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_bitable',
            'base_token', 'Fcm8b2H7wayK1UsYLjlcFmWhnMh',
            'table_id', 'tblX96gGAuBfJrtW',
            'view_name', '未签收明细',
            'view_id', 'veweDmbdIS'
        )
    ),
    (
        'phase7.delivery_status_webhook',
        'webhook_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'webhook_route',
            'path', 'webhook/sign-status'
        )
    ),
    (
        'phase7.scan_webhook',
        'webhook_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'webhook_route',
            'path', 'webhook/phase7/scan'
        )
    ),
    (
        'phase7.stats_webhook',
        'webhook_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'webhook_route',
            'path', 'webhook/phase7/stats'
        )
    ),
    (
        'automation.feishu_route.arrive_list',
        'feishu_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_route',
            'route_key', 'builtin.arrive_list'
        )
    ),
    (
        'automation.feishu_route.send_order',
        'feishu_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_route',
            'route_key', 'builtin.send_order'
        )
    ),
    (
        'automation.feishu_route.yunda_dispatch_forecast',
        'feishu_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_route',
            'route_key', 'builtin.yunda_dispatch_forecast'
        )
    ),
    (
        'automation.feishu_route.yunda_send_waybills',
        'feishu_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_route',
            'route_key', 'builtin.yunda_send_waybills'
        )
    ),
    (
        'automation.feishu_route.scan_codes',
        'feishu_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_route',
            'route_key', 'builtin.scan_codes'
        )
    ),
    (
        'automation.feishu_route.arrival_stats',
        'feishu_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_route',
            'route_key', 'builtin.arrival_stats'
        )
    ),
    (
        'automation.feishu_route.self_pickup_problem_upload',
        'feishu_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_route',
            'route_key', 'builtin.self_pickup_problem_upload'
        )
    ),
    (
        'automation.feishu_route.split_pending_problem_upload',
        'feishu_route',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_route',
            'route_key', 'builtin.split_pending_problem_upload'
        )
    ),
    (
        'phase7.yunda_dispatch_forecast_bitable',
        'feishu_bitable',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_bitable',
            'base_token', 'Et8sboZiSahfhYsa0i3c6hkwnXg',
            'table_id', 'tblT43ay2KjeXdC0'
        )
    ),
    (
        'phase7.yunda_send_waybills_bitable',
        'feishu_bitable',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_bitable',
            'base_token', 'Fcm8b2H7wayK1UsYLjlcFmWhnMh',
            'table_id', 'tblNHfIVVeaTBB7Y'
        )
    ),
    (
        'phase7.yunda_send_waybills_sheet',
        'feishu_sheet',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_sheet',
            'spreadsheet_token', 'GILYss6KhhBBuRt9FPWcXbben7c',
            'sheet_id', 'Sheet1',
            'sheet_range', 'Sheet1!A2:A2',
            'clear_range', 'Sheet1!A2:Y5000'
        )
    ),
    (
        'phase7.self_pickup_source_sheet',
        'feishu_sheet',
        TRUE,
        JSON_OBJECT(
            'resource_kind', 'feishu_sheet',
            'spreadsheet_token', 'F0NVsI5dlhaWugtw14YcmdrQnvh',
            'sheet_id', 'UeBd3I',
            'range', 'UeBd3I!A1:S5000'
        )
    ),
    (
        'phase7.split_pending_source_sheet',
        'feishu_sheet',
        TRUE,
        JSON_OBJECT(
            'display_name', '每日到货表',
            'resource_kind', 'feishu_sheet',
            'spreadsheet_token', 'F0NVsI5dlhaWugtw14YcmdrQnvh',
            'sheet_id', '8fc516',
            'range', '8fc516!A1:S5000'
        )
    ),
    (
        'phase7.split_pending_target_sheet',
        'feishu_sheet',
        TRUE,
        JSON_OBJECT(
            'display_name', '分批及有发未到表',
            'resource_kind', 'feishu_sheet',
            'spreadsheet_token', 'F0NVsI5dlhaWugtw14YcmdrQnvh',
            'sheet_id', 'bNhh7u',
            'range', 'bNhh7u!A1:S1',
            'clear_range', 'bNhh7u!A2:S5000'
        )
    ),
    (
        'phase7.site_send_bitable',
        'feishu_bitable',
        FALSE,
        NULL
    ),
    (
        'phase7.site_send_sheet',
        'feishu_sheet',
        FALSE,
        NULL
    ),
    (
        'phase7.send_order_bitable',
        'feishu_bitable',
        FALSE,
        NULL
    ),
    (
        'phase7.arrive_primary_sheet',
        'feishu_sheet',
        FALSE,
        NULL
    ),
    (
        'phase7.arrive_secondary_sheet',
        'feishu_sheet',
        FALSE,
        NULL
    ),
    (
        'phase7.stats_archive_sheet',
        'feishu_sheet',
        FALSE,
        NULL
    ),
    (
        'phase7.daily_sign_bitable',
        'feishu_bitable',
        FALSE,
        NULL
    ),
    (
        'phase7.daily_sign_sheet',
        'feishu_sheet',
        FALSE,
        NULL
    )
ON DUPLICATE KEY UPDATE
    expected_kind = VALUES(expected_kind),
    materialize_missing = VALUES(materialize_missing),
    default_config_json = VALUES(default_config_json);

-- The first release candidate included this optional compatibility target in
-- the reviewed set. A failed MySQL pass can have autocommitted that old map
-- row before the 15-row guard failed. Remove only that obsolete identity so a
-- rerun converges to the current fourteen-resource contract.
DELETE FROM automation_project_reviewed_resource_map_018
WHERE BINARY resource_key = BINARY 'phase7.pending_arrivals_sheet';

SET @cp018_reviewed_resource_count = (
    SELECT COUNT(*) FROM automation_project_reviewed_resource_map_018
);
SET @cp018_reviewed_resource_guard_sql = IF(
    @cp018_reviewed_resource_count = 26,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_reviewed_resource_map_changed'
);
PREPARE cp018_reviewed_resource_guard_stmt
    FROM @cp018_reviewed_resource_guard_sql;
EXECUTE cp018_reviewed_resource_guard_stmt;
DEALLOCATE PREPARE cp018_reviewed_resource_guard_stmt;

SET @cp018_required_existing_resource_invalid_count = (
    SELECT COUNT(*)
    FROM automation_project_reviewed_resource_map_018 AS reviewed
    LEFT JOIN workflow_resources AS resource
      ON BINARY resource.resource_key = BINARY reviewed.resource_key
    WHERE reviewed.materialize_missing = FALSE
      AND (
        resource.resource_key IS NULL
        OR NOT COALESCE(
            JSON_EXTRACT(resource.config_json, '$.resource_kind') IS NULL
            OR (
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.resource_kind'
                )) = 'STRING'
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.resource_kind'
                )) = BINARY reviewed.expected_kind
            ),
            FALSE
        )
        OR NOT COALESCE(CASE
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'phase7.site_send_bitable',
                BINARY 'phase7.send_order_bitable',
                BINARY 'phase7.daily_sign_bitable'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.base_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.base_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.table_id'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.table_id'
                ))) <> ''
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'phase7.site_send_sheet',
                BINARY 'phase7.daily_sign_sheet'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                ))) <> ''
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'phase7.arrive_primary_sheet',
                BINARY 'phase7.arrive_secondary_sheet'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.clear_range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.clear_range'
                ))) <> ''
            WHEN BINARY reviewed.resource_key =
                 BINARY 'phase7.stats_archive_sheet'
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND (
                    (
                        JSON_TYPE(JSON_EXTRACT(
                            resource.config_json, '$.default_write_range'
                        )) = 'STRING'
                        AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                            resource.config_json, '$.default_write_range'
                        ))) <> ''
                    )
                    OR (
                        JSON_TYPE(JSON_EXTRACT(
                            resource.config_json, '$.source_snapshot_range'
                        )) = 'STRING'
                        AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                            resource.config_json, '$.source_snapshot_range'
                        ))) <> ''
                    )
                )
            ELSE FALSE
        END, FALSE)
      )
);
SET @cp018_required_existing_resource_guard_sql = IF(
    @cp018_required_existing_resource_invalid_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_required_existing_resource_invalid'
);
PREPARE cp018_required_existing_resource_guard_stmt
    FROM @cp018_required_existing_resource_guard_sql;
EXECUTE cp018_required_existing_resource_guard_stmt;
DEALLOCATE PREPARE cp018_required_existing_resource_guard_stmt;

SET @cp018_unreviewed_schedule_count = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    LEFT JOIN automation_project_reviewed_schedule_map_018 AS reviewed
      ON BINARY reviewed.task_id = BINARY task.id
     AND BINARY reviewed.expected_tool_name = BINARY task.tool_name
    WHERE reviewed.task_id IS NULL
);
SET @cp018_preflight_identity_guard_sql = IF(
    @cp018_unreviewed_schedule_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_unreviewed_scheduled_task_identity'
);
PREPARE cp018_preflight_identity_guard_stmt
    FROM @cp018_preflight_identity_guard_sql;
EXECUTE cp018_preflight_identity_guard_stmt;
DEALLOCATE PREPARE cp018_preflight_identity_guard_stmt;

-- Validate the complete top-level account-role shape before the first ALTER.
-- Financial schedules intentionally contain no account in task params; the
-- bootstrap authority materializes all three reviewed finance role bindings in
-- automation_project_configs instead of consulting a global enabled-account set.
SET @cp018_invalid_account_shape_count = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN automation_project_reviewed_schedule_map_018 AS reviewed
      ON BINARY reviewed.task_id = BINARY task.id
     AND BINARY reviewed.expected_tool_name = BINARY task.tool_name
    WHERE NOT COALESCE(CASE reviewed.automation_id
        WHEN 'finance_bills' THEN NOT EXISTS (
            SELECT 1
            FROM JSON_TABLE(
                JSON_KEYS(task.tool_params), '$[*]'
                COLUMNS (key_name VARCHAR(128) PATH '$')
            ) AS finance_keys
            WHERE BINARY finance_keys.key_name LIKE BINARY '%account%'
        )
        WHEN 'finance_startup_catchup' THEN NOT EXISTS (
            SELECT 1
            FROM JSON_TABLE(
                JSON_KEYS(task.tool_params), '$[*]'
                COLUMNS (key_name VARCHAR(128) PATH '$')
            ) AS finance_startup_keys
            WHERE BINARY finance_startup_keys.key_name LIKE BINARY '%account%'
        )
        WHEN 'customer_problems_shadow' THEN NOT EXISTS (
            SELECT 1
            FROM JSON_TABLE(
                JSON_KEYS(task.tool_params), '$[*]'
                COLUMNS (key_name VARCHAR(128) PATH '$')
            ) AS customer_problem_keys
            WHERE BINARY customer_problem_keys.key_name LIKE BINARY '%account%'
        )
        WHEN 'r7_departure_checkin' THEN NOT EXISTS (
            SELECT 1
            FROM JSON_TABLE(
                JSON_KEYS(task.tool_params), '$[*]'
                COLUMNS (key_name VARCHAR(128) PATH '$')
            ) AS r7_departure_keys
            WHERE BINARY r7_departure_keys.key_name LIKE BINARY '%account%'
        )
        WHEN 'daily_sign' THEN (
            BINARY JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) =
                BINARY 'ronghui_daxiang_s'
            AND BINARY JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.r13_account_id')) =
                BINARY 'r13_default'
            AND NOT EXISTS (
                SELECT 1
                FROM JSON_TABLE(
                    JSON_KEYS(task.tool_params), '$[*]'
                    COLUMNS (key_name VARCHAR(128) PATH '$')
                ) AS daily_keys
                WHERE BINARY daily_keys.key_name LIKE BINARY '%account%'
                  AND BINARY daily_keys.key_name NOT IN (
                      BINARY 'account_id', BINARY 'r13_account_id'
                  )
            )
        )
        ELSE (
            BINARY JSON_UNQUOTE(JSON_EXTRACT(task.tool_params, '$.account_id')) =
                BINARY (CASE reviewed.automation_id
                    WHEN 'send_order' THEN 'price_default'
                    WHEN 'clockin_daxiang_s' THEN 'ronghui_daxiang_s'
                    WHEN 'r7_arrival_checkin' THEN 'r7_default'
                    WHEN 'yunda_dispatch_forecast' THEN 'yunda_default'
                    WHEN 'yunda_send_waybills' THEN 'yunda_default'
                    ELSE 'ronghui_default'
                END)
            AND NOT EXISTS (
                SELECT 1
                FROM JSON_TABLE(
                    JSON_KEYS(task.tool_params), '$[*]'
                    COLUMNS (key_name VARCHAR(128) PATH '$')
                ) AS account_keys
                WHERE BINARY account_keys.key_name LIKE BINARY '%account%'
                  AND BINARY account_keys.key_name <> BINARY 'account_id'
            )
        )
    END, FALSE)
);
SET @cp018_preflight_account_guard_sql = IF(
    @cp018_invalid_account_shape_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_mixed_project_account_bindings'
);
PREPARE cp018_preflight_account_guard_stmt
    FROM @cp018_preflight_account_guard_sql;
EXECUTE cp018_preflight_account_guard_stmt;
DEALLOCATE PREPARE cp018_preflight_account_guard_stmt;

-- The backup is created before the first ALTER so restore retains the exact
-- pre-018 scheduled_tasks shape. Conditional DDL makes partial reruns safe.
SET @cp018_has_automation_id = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scheduled_tasks'
      AND COLUMN_NAME='automation_id'
);
CREATE TABLE IF NOT EXISTS automation_project_migration_capture_018 (
    marker_id TINYINT UNSIGNED NOT NULL,
    capture_state VARCHAR(16) NOT NULL,
    source_row_count BIGINT UNSIGNED NOT NULL,
    captured_at DATETIME(6) NULL,
    PRIMARY KEY (marker_id),
    CONSTRAINT chk_automation_project_capture_marker CHECK (marker_id = 1),
    CONSTRAINT chk_automation_project_capture_state CHECK (
        capture_state IN ('CAPTURING', 'CAPTURED')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS scheduled_task_automation_identity_backup_018 LIKE scheduled_tasks;

-- Capture every reviewed external-resource row before 018 can add or
-- normalize it. ``existed_before`` lets restore distinguish an exact old row
-- from one created by this migration. ``migration_config_sha256`` stays NULL
-- until the post-migration resource state has passed every guard.
CREATE TABLE IF NOT EXISTS automation_project_resource_backup_018 (
    resource_key VARCHAR(128) NOT NULL,
    existed_before BOOLEAN NOT NULL,
    config_json JSON NULL,
    source VARCHAR(128) NULL,
    updated_at DATETIME NULL,
    created_at DATETIME NULL,
    migration_config_sha256 CHAR(64) NULL,
    PRIMARY KEY (resource_key),
    CONSTRAINT chk_automation_project_resource_backup_presence CHECK (
        (existed_before = TRUE AND config_json IS NOT NULL)
        OR (existed_before = FALSE AND config_json IS NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO automation_project_resource_backup_018 (
    resource_key, existed_before, config_json, source, updated_at, created_at,
    migration_config_sha256
)
SELECT
    reviewed.resource_key,
    resource.resource_key IS NOT NULL,
    resource.config_json,
    resource.source,
    resource.updated_at,
    resource.created_at,
    NULL
FROM automation_project_reviewed_resource_map_018 AS reviewed
LEFT JOIN workflow_resources AS resource
  ON BINARY resource.resource_key = BINARY reviewed.resource_key;

SET @cp018_resource_backup_count = (
    SELECT COUNT(*) FROM automation_project_resource_backup_018
);
SET @cp018_legacy_pending_backup_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018
    WHERE BINARY resource_key = BINARY 'phase7.pending_arrivals_sheet'
);
SET @cp018_legacy_pending_backup_invalid_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018
    WHERE BINARY resource_key = BINARY 'phase7.pending_arrivals_sheet'
      AND existed_before <> TRUE
);
SET @cp018_resource_backup_missing_reviewed_count = (
    SELECT COUNT(*)
    FROM automation_project_reviewed_resource_map_018 AS reviewed
    LEFT JOIN automation_project_resource_backup_018 AS backup
      ON BINARY backup.resource_key = BINARY reviewed.resource_key
    WHERE backup.resource_key IS NULL
);
SET @cp018_resource_backup_unexpected_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018 AS backup
    LEFT JOIN automation_project_reviewed_resource_map_018 AS reviewed
      ON BINARY reviewed.resource_key = BINARY backup.resource_key
    WHERE reviewed.resource_key IS NULL
      AND BINARY backup.resource_key <>
          BINARY 'phase7.pending_arrivals_sheet'
);
SET @cp018_resource_backup_guard_sql = IF(
    @cp018_resource_backup_count = 26 + @cp018_legacy_pending_backup_count
    AND @cp018_legacy_pending_backup_count IN (0, 1)
    AND @cp018_legacy_pending_backup_invalid_count = 0
    AND @cp018_resource_backup_missing_reviewed_count = 0
    AND @cp018_resource_backup_unexpected_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_resource_backup_incomplete'
);
PREPARE cp018_resource_backup_guard_stmt FROM @cp018_resource_backup_guard_sql;
EXECUTE cp018_resource_backup_guard_stmt;
DEALLOCATE PREPARE cp018_resource_backup_guard_stmt;

-- A rerun of the expanded reviewed set can observe exactly one mixed hash
-- layout: all fourteen old reviewed rows (and the optional legacy pending row,
-- when present) were captured by the prior pass, while the twelve newly
-- reviewed code-owned rows were just backed up and still have NULL hashes.
-- No other partial hash subset is migration-owned.
SET @cp018_resource_backup_hashed_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018
    WHERE migration_config_sha256 IS NOT NULL
);
SET @cp018_old_reviewed_backup_unhashed_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018 AS backup
    INNER JOIN automation_project_reviewed_resource_map_018 AS reviewed
      ON BINARY reviewed.resource_key = BINARY backup.resource_key
    WHERE BINARY backup.resource_key NOT IN (
        BINARY 'phase7.delivery_status_bitable',
        BINARY 'phase7.delivery_status_webhook',
        BINARY 'phase7.scan_webhook',
        BINARY 'phase7.stats_webhook',
        BINARY 'automation.feishu_route.arrive_list',
        BINARY 'automation.feishu_route.send_order',
        BINARY 'automation.feishu_route.yunda_dispatch_forecast',
        BINARY 'automation.feishu_route.yunda_send_waybills',
        BINARY 'automation.feishu_route.scan_codes',
        BINARY 'automation.feishu_route.arrival_stats',
        BINARY 'automation.feishu_route.self_pickup_problem_upload',
        BINARY 'automation.feishu_route.split_pending_problem_upload'
    )
      AND backup.migration_config_sha256 IS NULL
);
SET @cp018_new_reviewed_backup_hashed_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018
    WHERE BINARY resource_key IN (
        BINARY 'phase7.delivery_status_bitable',
        BINARY 'phase7.delivery_status_webhook',
        BINARY 'phase7.scan_webhook',
        BINARY 'phase7.stats_webhook',
        BINARY 'automation.feishu_route.arrive_list',
        BINARY 'automation.feishu_route.send_order',
        BINARY 'automation.feishu_route.yunda_dispatch_forecast',
        BINARY 'automation.feishu_route.yunda_send_waybills',
        BINARY 'automation.feishu_route.scan_codes',
        BINARY 'automation.feishu_route.arrival_stats',
        BINARY 'automation.feishu_route.self_pickup_problem_upload',
        BINARY 'automation.feishu_route.split_pending_problem_upload'
    )
      AND migration_config_sha256 IS NOT NULL
);
SET @cp018_legacy_pending_backup_unhashed_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018
    WHERE BINARY resource_key = BINARY 'phase7.pending_arrivals_sheet'
      AND migration_config_sha256 IS NULL
);
SET @cp018_resource_backup_hash_layout_guard_sql = IF(
    @cp018_resource_backup_hashed_count = 0
    OR @cp018_resource_backup_hashed_count = @cp018_resource_backup_count
    OR (
        @cp018_resource_backup_hashed_count =
            14 + @cp018_legacy_pending_backup_count
        AND @cp018_old_reviewed_backup_unhashed_count = 0
        AND @cp018_new_reviewed_backup_hashed_count = 0
        AND @cp018_legacy_pending_backup_unhashed_count = 0
    ),
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_resource_backup_hash_layout_invalid'
);
PREPARE cp018_resource_backup_hash_layout_guard_stmt
    FROM @cp018_resource_backup_hash_layout_guard_sql;
EXECUTE cp018_resource_backup_hash_layout_guard_stmt;
DEALLOCATE PREPARE cp018_resource_backup_hash_layout_guard_stmt;

-- A later failed pass can also have captured the obsolete legacy pending row.
-- It was never materialized by 018, so accept it only as an exact pre-existing
-- row, its sole migration normalization (resource_kind), or its already
-- captured post-state. This preserves exact restore ownership without making
-- the optional resource part of the current reviewed contract.
SET @cp018_legacy_pending_backup_drift_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018 AS backup
    LEFT JOIN workflow_resources AS resource
      ON BINARY resource.resource_key = BINARY backup.resource_key
    WHERE BINARY backup.resource_key =
          BINARY 'phase7.pending_arrivals_sheet'
      AND NOT (
          backup.existed_before = TRUE
          AND resource.resource_key IS NOT NULL
          AND BINARY resource.source <=> BINARY backup.source
          AND (
              (
                  backup.migration_config_sha256 IS NOT NULL
                  AND BINARY SHA2(
                      CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                      256
                  ) = BINARY backup.migration_config_sha256
              )
              OR (
                  backup.migration_config_sha256 IS NULL
                  AND (
                      BINARY SHA2(
                          CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                          256
                      ) = BINARY SHA2(
                          CAST(backup.config_json AS CHAR CHARACTER SET utf8mb4),
                          256
                      )
                      OR BINARY SHA2(
                          CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                          256
                      ) = BINARY SHA2(
                          CAST(JSON_SET(
                              backup.config_json,
                              '$.resource_kind',
                              'feishu_sheet'
                          ) AS CHAR CHARACTER SET utf8mb4),
                          256
                      )
                  )
              )
          )
      )
);
SET @cp018_legacy_pending_backup_guard_sql = IF(
    @cp018_legacy_pending_backup_drift_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_legacy_pending_backup_drift'
);
PREPARE cp018_legacy_pending_backup_guard_stmt
    FROM @cp018_legacy_pending_backup_guard_sql;
EXECUTE cp018_legacy_pending_backup_guard_stmt;
DEALLOCATE PREPARE cp018_legacy_pending_backup_guard_stmt;

INSERT IGNORE INTO automation_project_migration_capture_018 (
    marker_id, capture_state, source_row_count, captured_at
) SELECT 1, 'CAPTURING', COUNT(*), NULL FROM scheduled_tasks;
SET @cp018_capture_state = (
    SELECT capture_state
    FROM automation_project_migration_capture_018
    WHERE marker_id = 1
);
SET @cp018_capture_shape_guard_sql = IF(
    @cp018_has_automation_id = 0 OR @cp018_capture_state = 'CAPTURED',
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_scheduler_capture_incomplete'
);
PREPARE cp018_capture_shape_guard_stmt FROM @cp018_capture_shape_guard_sql;
EXECUTE cp018_capture_shape_guard_stmt;
DEALLOCATE PREPARE cp018_capture_shape_guard_stmt;

UPDATE automation_project_migration_capture_018
SET source_row_count = (SELECT COUNT(*) FROM scheduled_tasks)
WHERE marker_id = 1 AND capture_state = 'CAPTURING';
SET @cp018_clear_backup_sql = IF(
    @cp018_capture_state = 'CAPTURING',
    'DELETE FROM scheduled_task_automation_identity_backup_018',
    'SELECT 1'
);
PREPARE cp018_clear_backup_stmt FROM @cp018_clear_backup_sql;
EXECUTE cp018_clear_backup_stmt;
DEALLOCATE PREPARE cp018_clear_backup_stmt;
SET @cp018_capture_backup_sql = IF(
    @cp018_capture_state = 'CAPTURING',
    'INSERT INTO scheduled_task_automation_identity_backup_018 SELECT * FROM scheduled_tasks',
    'SELECT 1'
);
PREPARE cp018_capture_backup_stmt FROM @cp018_capture_backup_sql;
EXECUTE cp018_capture_backup_stmt;
DEALLOCATE PREPARE cp018_capture_backup_stmt;
SET @cp018_backup_matches_source = (
    SELECT
        (SELECT COUNT(*) FROM scheduled_task_automation_identity_backup_018) =
        source_row_count
    FROM automation_project_migration_capture_018
    WHERE marker_id = 1
);
SET @cp018_backup_count_guard_sql = IF(
    @cp018_backup_matches_source,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_scheduler_backup_incomplete'
);
PREPARE cp018_backup_count_guard_stmt FROM @cp018_backup_count_guard_sql;
EXECUTE cp018_backup_count_guard_stmt;
DEALLOCATE PREPARE cp018_backup_count_guard_stmt;
UPDATE automation_project_migration_capture_018
SET capture_state = 'CAPTURED', captured_at = COALESCE(captured_at, CURRENT_TIMESTAMP(6))
WHERE marker_id = 1 AND capture_state = 'CAPTURING';
SET @cp018_add_automation_id_sql = IF(
    @cp018_has_automation_id = 0,
    'ALTER TABLE scheduled_tasks ADD COLUMN automation_id VARCHAR(128) NULL AFTER id',
    'SELECT 1'
);
PREPARE cp018_add_automation_id_stmt FROM @cp018_add_automation_id_sql;
EXECUTE cp018_add_automation_id_stmt;
DEALLOCATE PREPARE cp018_add_automation_id_stmt;

SET @cp018_has_automation_index = (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scheduled_tasks'
      AND INDEX_NAME='idx_scheduled_tasks_automation'
);
SET @cp018_add_automation_index_sql = IF(
    @cp018_has_automation_index = 0,
    'ALTER TABLE scheduled_tasks ADD KEY idx_scheduled_tasks_automation (automation_id, enabled, id)',
    'SELECT 1'
);
PREPARE cp018_add_automation_index_stmt FROM @cp018_add_automation_index_sql;
EXECUTE cp018_add_automation_index_stmt;
DEALLOCATE PREPARE cp018_add_automation_index_stmt;

-- Only code-reviewed identities are backfilled. Shared tools are split by exact
-- task id; no clock suffix, display-name or "first match" inference is allowed.
-- Explicitly retain updated_at so adding migration identity never rewrites a
-- historical task's operational state timestamp.
UPDATE scheduled_tasks
SET automation_id = CASE
    WHEN BINARY id = BINARY 'clockin_daxiang_1830'
         AND BINARY tool_name = BINARY 'clock_in_dual' THEN 'clockin_daxiang'
    WHEN BINARY id = BINARY 'clockin_daxiang_s_1833'
         AND BINARY tool_name = BINARY 'clock_in_dual' THEN 'clockin_daxiang_s'
    WHEN BINARY id = BINARY 'finance_bills_0010'
         AND BINARY tool_name = BINARY 'sync_finance_bills' THEN 'finance_bills'
    WHEN BINARY id = BINARY 'finance_startup_catchup'
         AND BINARY tool_name = BINARY 'sync_finance_bills' THEN 'finance_startup_catchup'
    WHEN BINARY id = BINARY 'customer_problems_shadow'
         AND BINARY tool_name = BINARY 'sync_customer_service_problems'
         THEN 'customer_problems_shadow'
    WHEN BINARY id = BINARY 'send_order_2359'
         AND BINARY tool_name = BINARY 'sync_daily_send_orders' THEN 'send_order'
    WHEN BINARY id IN (
        BINARY 'delivery_status_0900', BINARY 'delivery_status_1000',
        BINARY 'delivery_status_1100', BINARY 'delivery_status_1200',
        BINARY 'delivery_status_1300', BINARY 'delivery_status_1400',
        BINARY 'delivery_status_1430', BINARY 'delivery_status_1500',
        BINARY 'delivery_status_1530', BINARY 'delivery_status_1600',
        BINARY 'delivery_status_1630', BINARY 'delivery_status_1700',
        BINARY 'delivery_status_1730', BINARY 'delivery_status_1800',
        BINARY 'delivery_status_1830', BINARY 'delivery_status_1900',
        BINARY 'delivery_status_1930', BINARY 'delivery_status_2000',
        BINARY 'delivery_status_2030', BINARY 'delivery_status_2100'
    ) AND BINARY tool_name = BINARY 'sync_delivery_status' THEN 'delivery_status'
    WHEN BINARY id IN (
        BINARY 'daily_sign_0500', BINARY 'daily_sign_0700',
        BINARY 'daily_sign_0800', BINARY 'daily_sign_0900',
        BINARY 'daily_sign_1000', BINARY 'daily_sign_1100',
        BINARY 'daily_sign_1200', BINARY 'daily_sign_1300',
        BINARY 'daily_sign_1400', BINARY 'daily_sign_1430',
        BINARY 'daily_sign_1500', BINARY 'daily_sign_1530',
        BINARY 'daily_sign_1600', BINARY 'daily_sign_1630',
        BINARY 'daily_sign_1700', BINARY 'daily_sign_1730',
        BINARY 'daily_sign_1800'
    ) AND BINARY tool_name = BINARY 'sync_daily_should_sign' THEN 'daily_sign'
    WHEN BINARY id IN (
        BINARY 'site_send_0500', BINARY 'site_send_0530',
        BINARY 'site_send_1800', BINARY 'site_send_1830',
        BINARY 'site_send_1900', BINARY 'site_send_1930',
        BINARY 'site_send_2000', BINARY 'site_send_2030',
        BINARY 'site_send_2100'
    ) AND BINARY tool_name = BINARY 'sync_site_send_list' THEN 'site_send'
    WHEN BINARY id IN (
        BINARY 'r7_arrival_checkin_0900', BINARY 'r7_arrival_checkin_0930',
        BINARY 'r7_arrival_checkin_1000', BINARY 'r7_arrival_checkin_1030',
        BINARY 'r7_arrival_checkin_1100', BINARY 'r7_arrival_checkin_1130',
        BINARY 'r7_arrival_checkin_1200', BINARY 'r7_arrival_checkin_1230',
        BINARY 'r7_arrival_checkin_1300', BINARY 'r7_arrival_checkin_1330',
        BINARY 'r7_arrival_checkin_1400', BINARY 'r7_arrival_checkin_1430',
        BINARY 'r7_arrival_checkin_1900'
    ) AND BINARY tool_name = BINARY 'r7_arrival_checkin' THEN 'r7_arrival_checkin'
    WHEN BINARY id = BINARY 'r7_departure_checkin'
         AND BINARY tool_name = BINARY 'r7_departure_checkin'
         THEN 'r7_departure_checkin'
    WHEN BINARY id IN (
        BINARY 'arrive_list_0830', BINARY 'arrive_list_0900',
        BINARY 'arrive_list_0930'
    ) AND BINARY tool_name = BINARY 'sync_arrive_list' THEN 'arrive_list'
    WHEN BINARY id = BINARY 'yunda_dispatch_forecast_1700'
         AND BINARY tool_name = BINARY 'sync_yunda_dispatch_forecast'
         THEN 'yunda_dispatch_forecast'
    WHEN BINARY id = BINARY 'yunda_send_waybills_2355'
         AND BINARY tool_name = BINARY 'sync_yunda_send_waybills'
         THEN 'yunda_send_waybills'
    ELSE automation_id
END,
updated_at = updated_at
WHERE automation_id IS NULL;

SET @cp018_unbound_schedule_count = (
    SELECT COUNT(*) FROM scheduled_tasks
    WHERE automation_id IS NULL OR TRIM(automation_id) = ''
);
SET @cp018_unbound_guard_sql = IF(
    @cp018_unbound_schedule_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_unreviewed_scheduled_task_identity'
);
PREPARE cp018_unbound_guard_stmt FROM @cp018_unbound_guard_sql;
EXECUTE cp018_unbound_guard_stmt;
DEALLOCATE PREPARE cp018_unbound_guard_stmt;

-- A partial rerun must not silently repair or accept an identity that was
-- changed after the reviewed backfill.
SET @cp018_identity_mismatch_count = (
    SELECT COUNT(*)
    FROM scheduled_tasks AS task
    INNER JOIN automation_project_reviewed_schedule_map_018 AS reviewed
      ON BINARY reviewed.task_id = BINARY task.id
     AND BINARY reviewed.expected_tool_name = BINARY task.tool_name
    WHERE BINARY task.automation_id <> BINARY reviewed.automation_id
);
SET @cp018_identity_match_guard_sql = IF(
    @cp018_identity_mismatch_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_project_identity_changed'
);
PREPARE cp018_identity_match_guard_stmt FROM @cp018_identity_match_guard_sql;
EXECUTE cp018_identity_match_guard_stmt;
DEALLOCATE PREPARE cp018_identity_match_guard_stmt;

SET @cp018_automation_id_nullable = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scheduled_tasks'
      AND COLUMN_NAME='automation_id' AND IS_NULLABLE='YES'
);
SET @cp018_make_automation_id_required_sql = IF(
    @cp018_automation_id_nullable = 1,
    'ALTER TABLE scheduled_tasks MODIFY COLUMN automation_id VARCHAR(128) NOT NULL',
    'SELECT 1'
);
PREPARE cp018_make_automation_id_required_stmt
    FROM @cp018_make_automation_id_required_sql;
EXECUTE cp018_make_automation_id_required_stmt;
DEALLOCATE PREPARE cp018_make_automation_id_required_stmt;

SET @cp018_has_schedule_generation = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scheduled_tasks'
      AND COLUMN_NAME='automation_generation'
);
SET @cp018_add_schedule_generation_sql = IF(
    @cp018_has_schedule_generation = 0,
    'ALTER TABLE scheduled_tasks ADD COLUMN automation_generation BIGINT UNSIGNED NOT NULL DEFAULT 1 AFTER automation_id',
    'SELECT 1'
);
PREPARE cp018_add_schedule_generation_stmt
    FROM @cp018_add_schedule_generation_sql;
EXECUTE cp018_add_schedule_generation_stmt;
DEALLOCATE PREPARE cp018_add_schedule_generation_stmt;

SET @cp018_has_resource_version = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='workflow_resources'
      AND COLUMN_NAME='configuration_version'
);
SET @cp018_add_resource_version_sql = IF(
    @cp018_has_resource_version = 0,
    'ALTER TABLE workflow_resources ADD COLUMN configuration_version BIGINT UNSIGNED NOT NULL DEFAULT 1 AFTER source',
    'SELECT 1'
);
PREPARE cp018_add_resource_version_stmt FROM @cp018_add_resource_version_sql;
EXECUTE cp018_add_resource_version_stmt;
DEALLOCATE PREPARE cp018_add_resource_version_stmt;

SET @cp018_has_resource_hash = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='workflow_resources'
      AND COLUMN_NAME='config_sha256'
);
SET @cp018_add_resource_hash_sql = IF(
    @cp018_has_resource_hash = 0,
    'ALTER TABLE workflow_resources ADD COLUMN config_sha256 CHAR(64) NULL AFTER config_json',
    'SELECT 1'
);
PREPARE cp018_add_resource_hash_stmt FROM @cp018_add_resource_hash_sql;
EXECUTE cp018_add_resource_hash_stmt;
DEALLOCATE PREPARE cp018_add_resource_hash_stmt;
UPDATE workflow_resources
SET
    config_sha256=SHA2(CAST(config_json AS CHAR CHARACTER SET utf8mb4), 256),
    updated_at = updated_at
WHERE config_sha256 IS NULL;
SET @cp018_resource_hash_nullable = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='workflow_resources'
      AND COLUMN_NAME='config_sha256' AND IS_NULLABLE='YES'
);
SET @cp018_require_resource_hash_sql = IF(
    @cp018_resource_hash_nullable = 1,
    'ALTER TABLE workflow_resources MODIFY COLUMN config_sha256 CHAR(64) NOT NULL',
    'SELECT 1'
);
PREPARE cp018_require_resource_hash_stmt FROM @cp018_require_resource_hash_sql;
EXECUTE cp018_require_resource_hash_stmt;
DEALLOCATE PREPARE cp018_require_resource_hash_stmt;

-- A prior failed pass may have stopped after the non-transactional DDL or
-- after materializing a reviewed row.  Before writing again, accept only the
-- exact pre-018 capture, the one post-state this migration can produce, or the
-- already captured migration hash.  Any other row is user/external drift.
SET @cp018_resource_partial_drift_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018 AS backup
    INNER JOIN automation_project_reviewed_resource_map_018 AS reviewed
      ON BINARY reviewed.resource_key = BINARY backup.resource_key
    LEFT JOIN workflow_resources AS resource
      ON BINARY resource.resource_key = BINARY backup.resource_key
    WHERE NOT (
        (
            backup.migration_config_sha256 IS NOT NULL
            AND resource.resource_key IS NOT NULL
            AND resource.configuration_version = 1
            AND BINARY resource.config_sha256 =
                BINARY backup.migration_config_sha256
            AND BINARY resource.config_sha256 = BINARY SHA2(
                CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                256
            )
            AND (
                (
                    backup.existed_before = TRUE
                    AND BINARY resource.source <=> BINARY backup.source
                )
                OR (
                    backup.existed_before = FALSE
                    AND BINARY resource.source =
                        BINARY 'migration-018-reviewed-builtin'
                )
            )
        )
        OR (
            backup.migration_config_sha256 IS NULL
            AND backup.existed_before = TRUE
            AND resource.resource_key IS NOT NULL
            AND resource.configuration_version = 1
            AND BINARY resource.source <=> BINARY backup.source
            AND BINARY resource.config_sha256 = BINARY SHA2(
                CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                256
            )
            AND (
                BINARY resource.config_sha256 = BINARY SHA2(
                    CAST(backup.config_json AS CHAR CHARACTER SET utf8mb4),
                    256
                )
                OR BINARY resource.config_sha256 = BINARY SHA2(
                    CAST(
                        JSON_SET(
                            backup.config_json,
                            '$.resource_kind',
                            reviewed.expected_kind
                        ) AS CHAR CHARACTER SET utf8mb4
                    ),
                    256
                )
            )
        )
        OR (
            backup.migration_config_sha256 IS NULL
            AND backup.existed_before = FALSE
            AND (
                resource.resource_key IS NULL
                OR (
                    resource.configuration_version = 1
                    AND BINARY resource.source =
                        BINARY 'migration-018-reviewed-builtin'
                    AND BINARY resource.config_sha256 = BINARY SHA2(
                        CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                        256
                    )
                    AND BINARY resource.config_sha256 = BINARY SHA2(
                        CAST(
                            reviewed.default_config_json
                            AS CHAR CHARACTER SET utf8mb4
                        ),
                        256
                    )
                )
            )
        )
    )
);
SET @cp018_legacy_pending_partial_drift_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018 AS backup
    LEFT JOIN workflow_resources AS resource
      ON BINARY resource.resource_key = BINARY backup.resource_key
    WHERE BINARY backup.resource_key =
          BINARY 'phase7.pending_arrivals_sheet'
      AND NOT (
          backup.existed_before = TRUE
          AND resource.resource_key IS NOT NULL
          AND resource.configuration_version = 1
          AND BINARY resource.source <=> BINARY backup.source
          AND BINARY resource.config_sha256 = BINARY SHA2(
              CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
              256
          )
          AND (
              (
                  backup.migration_config_sha256 IS NOT NULL
                  AND BINARY resource.config_sha256 =
                      BINARY backup.migration_config_sha256
              )
              OR (
                  backup.migration_config_sha256 IS NULL
                  AND (
                      BINARY resource.config_sha256 = BINARY SHA2(
                          CAST(backup.config_json AS CHAR CHARACTER SET utf8mb4),
                          256
                      )
                      OR BINARY resource.config_sha256 = BINARY SHA2(
                          CAST(JSON_SET(
                              backup.config_json,
                              '$.resource_kind',
                              'feishu_sheet'
                          ) AS CHAR CHARACTER SET utf8mb4),
                          256
                      )
                  )
              )
          )
      )
);
SET @cp018_resource_partial_drift_guard_sql = IF(
    @cp018_resource_partial_drift_count = 0
    AND @cp018_legacy_pending_partial_drift_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_reviewed_resource_partial_drift'
);
PREPARE cp018_resource_partial_drift_guard_stmt
    FROM @cp018_resource_partial_drift_guard_sql;
EXECUTE cp018_resource_partial_drift_guard_stmt;
DEALLOCATE PREPARE cp018_resource_partial_drift_guard_stmt;

-- Materialize missing reviewed rows without overwriting an existing target.
-- An existing exact row may predate resource-kind governance; only that absent
-- discriminator is added. All routing is by the twenty-six BINARY resource ids.
INSERT INTO workflow_resources (
    resource_key, config_json, config_sha256, source, configuration_version
)
SELECT
    reviewed.resource_key,
    reviewed.default_config_json,
    SHA2(
        CAST(reviewed.default_config_json AS CHAR CHARACTER SET utf8mb4),
        256
    ),
    'migration-018-reviewed-builtin',
    1
FROM automation_project_reviewed_resource_map_018 AS reviewed
LEFT JOIN workflow_resources AS resource
  ON BINARY resource.resource_key = BINARY reviewed.resource_key
WHERE resource.resource_key IS NULL
  AND reviewed.materialize_missing = TRUE;

UPDATE workflow_resources AS resource
INNER JOIN automation_project_reviewed_resource_map_018 AS reviewed
  ON BINARY reviewed.resource_key = BINARY resource.resource_key
SET
    resource.config_json = JSON_SET(
        resource.config_json,
        '$.resource_kind',
        reviewed.expected_kind
    ),
    resource.config_sha256 = SHA2(
        CAST(
            JSON_SET(
                resource.config_json,
                '$.resource_kind',
                reviewed.expected_kind
            ) AS CHAR CHARACTER SET utf8mb4
        ),
        256
    ),
    resource.updated_at = resource.updated_at
WHERE JSON_EXTRACT(resource.config_json, '$.resource_kind') IS NULL;

SET @cp018_invalid_reviewed_resource_count = (
    SELECT COUNT(*)
    FROM automation_project_reviewed_resource_map_018 AS reviewed
    LEFT JOIN workflow_resources AS resource
      ON BINARY resource.resource_key = BINARY reviewed.resource_key
    WHERE resource.resource_key IS NULL
       OR NOT COALESCE(
            JSON_TYPE(JSON_EXTRACT(resource.config_json, '$.resource_kind')) = 'STRING'
            AND BINARY JSON_UNQUOTE(
                JSON_EXTRACT(resource.config_json, '$.resource_kind')
            ) = BINARY reviewed.expected_kind,
            FALSE
       )
       OR NOT COALESCE(CASE
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'phase7.self_pickup_source_sheet',
                BINARY 'phase7.split_pending_source_sheet'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.sheet_id'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.sheet_id'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                ))) <> ''
            WHEN BINARY reviewed.resource_key =
                 BINARY 'phase7.split_pending_target_sheet'
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.sheet_id'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.sheet_id'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.clear_range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.clear_range'
                ))) <> ''
            WHEN BINARY reviewed.resource_key =
                 BINARY 'phase7.delivery_status_bitable'
            THEN
                JSON_TYPE(JSON_EXTRACT(resource.config_json, '$.base_token')) = 'STRING'
                AND TRIM(JSON_UNQUOTE(
                    JSON_EXTRACT(resource.config_json, '$.base_token')
                )) <> ''
                AND JSON_TYPE(JSON_EXTRACT(resource.config_json, '$.table_id')) = 'STRING'
                AND TRIM(JSON_UNQUOTE(
                    JSON_EXTRACT(resource.config_json, '$.table_id')
                )) <> ''
                AND JSON_TYPE(JSON_EXTRACT(resource.config_json, '$.view_name')) = 'STRING'
                AND TRIM(JSON_UNQUOTE(
                    JSON_EXTRACT(resource.config_json, '$.view_name')
                )) <> ''
                AND JSON_TYPE(JSON_EXTRACT(resource.config_json, '$.view_id')) = 'STRING'
                AND TRIM(JSON_UNQUOTE(
                    JSON_EXTRACT(resource.config_json, '$.view_id')
                )) <> ''
            WHEN BINARY reviewed.resource_key =
                 BINARY 'phase7.yunda_dispatch_forecast_bitable'
              OR BINARY reviewed.resource_key =
                 BINARY 'phase7.yunda_send_waybills_bitable'
            THEN
                JSON_TYPE(JSON_EXTRACT(resource.config_json, '$.base_token')) = 'STRING'
                AND TRIM(JSON_UNQUOTE(
                    JSON_EXTRACT(resource.config_json, '$.base_token')
                )) <> ''
                AND JSON_TYPE(JSON_EXTRACT(resource.config_json, '$.table_id')) = 'STRING'
                AND TRIM(JSON_UNQUOTE(
                    JSON_EXTRACT(resource.config_json, '$.table_id')
                )) <> ''
            WHEN BINARY reviewed.resource_key =
                 BINARY 'phase7.yunda_send_waybills_sheet'
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(resource.config_json, '$.sheet_id')) = 'STRING'
                AND TRIM(JSON_UNQUOTE(
                    JSON_EXTRACT(resource.config_json, '$.sheet_id')
                )) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.sheet_range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.sheet_range'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.clear_range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.clear_range'
                ))) <> ''
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'phase7.delivery_status_webhook',
                BINARY 'phase7.scan_webhook',
                BINARY 'phase7.stats_webhook'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.path'
                )) = 'STRING'
                AND CHAR_LENGTH(
                    TRIM(BOTH '/' FROM TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                        resource.config_json, '$.path'
                    ))))
                ) BETWEEN 1 AND 191
                AND BINARY TRIM(
                    BOTH '/' FROM TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                        resource.config_json, '$.path'
                    )))
                ) = BINARY TRIM(
                    BOTH '/' FROM TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                        reviewed.default_config_json, '$.path'
                    )))
                )
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'automation.feishu_route.arrive_list',
                BINARY 'automation.feishu_route.send_order',
                BINARY 'automation.feishu_route.yunda_dispatch_forecast',
                BINARY 'automation.feishu_route.yunda_send_waybills',
                BINARY 'automation.feishu_route.scan_codes',
                BINARY 'automation.feishu_route.arrival_stats',
                BINARY 'automation.feishu_route.self_pickup_problem_upload',
                BINARY 'automation.feishu_route.split_pending_problem_upload'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.route_key'
                )) = 'STRING'
                AND CHAR_LENGTH(TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.route_key'
                )))) BETWEEN 1 AND 191
                AND BINARY TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.route_key'
                ))) = BINARY TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    reviewed.default_config_json, '$.route_key'
                )))
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'phase7.site_send_bitable',
                BINARY 'phase7.send_order_bitable',
                BINARY 'phase7.daily_sign_bitable'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.base_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.base_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.table_id'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.table_id'
                ))) <> ''
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'phase7.site_send_sheet',
                BINARY 'phase7.daily_sign_sheet'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                ))) <> ''
            WHEN BINARY reviewed.resource_key IN (
                BINARY 'phase7.arrive_primary_sheet',
                BINARY 'phase7.arrive_secondary_sheet'
            )
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.range'
                ))) <> ''
                AND JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.clear_range'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.clear_range'
                ))) <> ''
            WHEN BINARY reviewed.resource_key =
                 BINARY 'phase7.stats_archive_sheet'
            THEN
                JSON_TYPE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                )) = 'STRING'
                AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                    resource.config_json, '$.spreadsheet_token'
                ))) <> ''
                AND (
                    (
                        JSON_TYPE(JSON_EXTRACT(
                            resource.config_json, '$.default_write_range'
                        )) = 'STRING'
                        AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                            resource.config_json, '$.default_write_range'
                        ))) <> ''
                    )
                    OR (
                        JSON_TYPE(JSON_EXTRACT(
                            resource.config_json, '$.source_snapshot_range'
                        )) = 'STRING'
                        AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(
                            resource.config_json, '$.source_snapshot_range'
                        ))) <> ''
                    )
                )
            ELSE FALSE
       END, FALSE)
       OR BINARY resource.config_sha256 <> BINARY SHA2(
            CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
            256
       )
       OR resource.configuration_version <> 1
);
SET @cp018_reviewed_resource_shape_guard_sql = IF(
    @cp018_invalid_reviewed_resource_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_reviewed_resource_missing_or_invalid'
);
PREPARE cp018_reviewed_resource_shape_guard_stmt
    FROM @cp018_reviewed_resource_shape_guard_sql;
EXECUTE cp018_reviewed_resource_shape_guard_stmt;
DEALLOCATE PREPARE cp018_reviewed_resource_shape_guard_stmt;

UPDATE automation_project_resource_backup_018 AS backup
INNER JOIN workflow_resources AS resource
  ON BINARY resource.resource_key = BINARY backup.resource_key
SET backup.migration_config_sha256 = resource.config_sha256
WHERE backup.migration_config_sha256 IS NULL;

SET @cp018_resource_capture_drift_count = (
    SELECT COUNT(*)
    FROM automation_project_resource_backup_018 AS backup
    LEFT JOIN workflow_resources AS resource
      ON BINARY resource.resource_key = BINARY backup.resource_key
    WHERE backup.migration_config_sha256 IS NULL
       OR resource.resource_key IS NULL
       OR BINARY resource.config_sha256 <>
          BINARY backup.migration_config_sha256
);
SET @cp018_resource_capture_guard_sql = IF(
    @cp018_resource_capture_drift_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_reviewed_resource_changed_after_capture'
);
PREPARE cp018_resource_capture_guard_stmt
    FROM @cp018_resource_capture_guard_sql;
EXECUTE cp018_resource_capture_guard_stmt;
DEALLOCATE PREPARE cp018_resource_capture_guard_stmt;

SET @cp018_has_command_automation_id = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_commands'
      AND COLUMN_NAME='automation_id'
);
SET @cp018_add_command_automation_id_sql = IF(
    @cp018_has_command_automation_id = 0,
    'ALTER TABLE agent_commands ADD COLUMN automation_id VARCHAR(128) NULL AFTER command_type',
    'SELECT 1'
);
PREPARE cp018_add_command_automation_id_stmt FROM @cp018_add_command_automation_id_sql;
EXECUTE cp018_add_command_automation_id_stmt;
DEALLOCATE PREPARE cp018_add_command_automation_id_stmt;

SET @cp018_has_command_generation = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_commands'
      AND COLUMN_NAME='automation_generation'
);
SET @cp018_add_command_generation_sql = IF(
    @cp018_has_command_generation = 0,
    'ALTER TABLE agent_commands ADD COLUMN automation_generation BIGINT UNSIGNED NULL AFTER automation_id',
    'SELECT 1'
);
PREPARE cp018_add_command_generation_stmt FROM @cp018_add_command_generation_sql;
EXECUTE cp018_add_command_generation_stmt;
DEALLOCATE PREPARE cp018_add_command_generation_stmt;

SET @cp018_has_command_invocation = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_commands'
      AND COLUMN_NAME='automation_invocation_json'
);
SET @cp018_add_command_invocation_sql = IF(
    @cp018_has_command_invocation = 0,
    'ALTER TABLE agent_commands ADD COLUMN automation_invocation_json JSON NULL AFTER parameters_json',
    'SELECT 1'
);
PREPARE cp018_add_command_invocation_stmt FROM @cp018_add_command_invocation_sql;
EXECUTE cp018_add_command_invocation_stmt;
DEALLOCATE PREPARE cp018_add_command_invocation_stmt;

SET @cp018_has_command_automation_index = (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_commands'
      AND INDEX_NAME='idx_agent_commands_automation_requested'
);
SET @cp018_add_command_automation_index_sql = IF(
    @cp018_has_command_automation_index = 0,
    'ALTER TABLE agent_commands ADD KEY idx_agent_commands_automation_requested (automation_id, requested_at)',
    'SELECT 1'
);
PREPARE cp018_add_command_automation_index_stmt
    FROM @cp018_add_command_automation_index_sql;
EXECUTE cp018_add_command_automation_index_stmt;
DEALLOCATE PREPARE cp018_add_command_automation_index_stmt;

CREATE TABLE IF NOT EXISTS automation_plugin_packages (
    plugin_id VARCHAR(64) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    description VARCHAR(1000) NOT NULL,
    latest_version VARCHAR(64) NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'REGISTERED',
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (plugin_id),
    CONSTRAINT chk_automation_plugin_package_state CHECK (
        state IN ('REGISTERED', 'ACTIVE', 'DISABLED', 'UNINSTALLING', 'ERROR')
    ),
    CONSTRAINT chk_automation_plugin_package_version CHECK (record_version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_plugin_versions (
    plugin_id VARCHAR(64) NOT NULL,
    version VARCHAR(64) NOT NULL,
    package_sha256 CHAR(64) NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL,
    manifest_json JSON NOT NULL,
    tool_contract_sha256 CHAR(64) NOT NULL,
    config_schema_sha256 CHAR(64) NOT NULL,
    allowed_entrypoints_sha256 CHAR(64) NOT NULL,
    invocation_contracts_sha256 CHAR(64) NOT NULL,
    worker_requirement_sha256 CHAR(64) NOT NULL,
    runtime_sha256 CHAR(64) NOT NULL,
    scheduling_sha256 CHAR(64) NOT NULL,
    project_full_auto_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    trust_source VARCHAR(24) NOT NULL,
    install_root_metadata_json JSON NOT NULL,
    install_root_metadata_sha256 CHAR(64) NOT NULL,
    installed_by_actor_id VARCHAR(128) NOT NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'INSTALLED',
    installed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (plugin_id, version),
    UNIQUE KEY uq_automation_plugin_version_package (plugin_id, package_sha256),
    KEY idx_automation_plugin_versions_manifest (manifest_sha256),
    CONSTRAINT fk_automation_plugin_version_package FOREIGN KEY (plugin_id)
        REFERENCES automation_plugin_packages (plugin_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_plugin_version_state CHECK (
        state IN ('INSTALLED', 'ACTIVE', 'RETIRED')
    ),
    CONSTRAINT chk_automation_plugin_trust_source CHECK (
        trust_source IN (
            'ed25519_upload', 'ed25519_first_party', 'builtin_release'
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_projects (
    automation_id VARCHAR(128) NOT NULL,
    plugin_id VARCHAR(64) NOT NULL,
    plugin_version VARCHAR(64) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    state VARCHAR(24) NOT NULL DEFAULT 'INSTALLED',
    install_request_id VARCHAR(191) NOT NULL,
    install_payload_sha256 CHAR(64) NOT NULL,
    installed_by_actor_id VARCHAR(128) NOT NULL,
    migration_authority BOOLEAN NOT NULL DEFAULT FALSE,
    target_generation BIGINT UNSIGNED NOT NULL DEFAULT 1,
    committed_generation BIGINT UNSIGNED NULL,
    reconcile_state VARCHAR(32) NOT NULL DEFAULT 'WAITING_COEFFECTS',
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    installed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (automation_id),
    UNIQUE KEY uq_automation_project_install_request (install_request_id),
    KEY idx_automation_projects_plugin_version (plugin_id, plugin_version),
    KEY idx_automation_projects_state (enabled, state, automation_id),
    CONSTRAINT fk_automation_project_version FOREIGN KEY (plugin_id, plugin_version)
        REFERENCES automation_plugin_versions (plugin_id, version) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_project_state CHECK (
        state IN ('INSTALLED', 'ENABLED', 'DISABLED', 'UPGRADING', 'UNINSTALLING', 'ERROR')
    ),
    CONSTRAINT chk_automation_project_enabled_state CHECK (
        enabled = FALSE OR state IN ('ENABLED', 'UPGRADING')
    ),
    CONSTRAINT chk_automation_project_generations CHECK (
        target_generation > 0
        AND (committed_generation IS NULL OR committed_generation > 0)
    ),
    CONSTRAINT chk_automation_project_reconcile_state CHECK (
        reconcile_state IN (
            'STABLE', 'PREPARING', 'WAITING_COEFFECTS',
            'READY_TO_COMMIT', 'DRAINING', 'DISPOSING',
            'BLOCKED_UNKNOWN_WRITE', 'ERROR'
        )
    ),
    CONSTRAINT chk_automation_project_record_version CHECK (record_version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- System-owned project settings are separate from signed package installation.
-- Account bindings contain role -> account_id only, never credentials/sessions.
CREATE TABLE IF NOT EXISTS automation_project_configs (
    automation_id VARCHAR(128) NOT NULL,
    config_json JSON NOT NULL,
    config_sha256 CHAR(64) NOT NULL,
    account_bindings_json JSON NOT NULL,
    account_bindings_sha256 CHAR(64) NOT NULL,
    resource_bindings_json JSON NOT NULL,
    resource_bindings_sha256 CHAR(64) NOT NULL,
    enabled_entrypoints_json JSON NOT NULL,
    enabled_entrypoints_sha256 CHAR(64) NOT NULL,
    desired_schedule_json JSON NOT NULL,
    desired_schedule_sha256 CHAR(64) NOT NULL,
    compiled_invocations_json JSON NOT NULL,
    compiled_invocations_sha256 CHAR(64) NOT NULL,
    device_id VARCHAR(128) NULL,
    device_binding_sha256 CHAR(64) NOT NULL,
    configured BOOLEAN NOT NULL DEFAULT FALSE,
    config_version BIGINT UNSIGNED NOT NULL DEFAULT 1,
    updated_by_actor_id VARCHAR(128) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (automation_id),
    CONSTRAINT fk_automation_project_config_project FOREIGN KEY (automation_id)
        REFERENCES automation_projects (automation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_project_config_version CHECK (config_version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Cordis-style immutable runtime generations. A new configuration/version is
-- prepared as a complete target generation; entrypoints remain on the prior
-- committed generation until every coeffect and reversible infrastructure
-- effect is ready and the project row CAS commits the switch.
CREATE TABLE IF NOT EXISTS automation_project_generations (
    automation_id VARCHAR(128) NOT NULL,
    generation BIGINT UNSIGNED NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    base_committed_generation BIGINT UNSIGNED NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'TARGET',
    plugin_id VARCHAR(64) NOT NULL,
    plugin_version VARCHAR(64) NOT NULL,
    package_sha256 CHAR(64) NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL,
    trust_source VARCHAR(24) NOT NULL,
    project_config_sha256 CHAR(64) NOT NULL,
    account_bindings_sha256 CHAR(64) NOT NULL,
    resource_bindings_sha256 CHAR(64) NOT NULL,
    device_binding_sha256 CHAR(64) NOT NULL,
    schedule_sha256 CHAR(64) NOT NULL,
    core_registry_sha256 CHAR(64) NOT NULL,
    tool_contract_sha256 CHAR(64) NOT NULL,
    invocation_contracts_sha256 CHAR(64) NOT NULL,
    compiled_invocations_sha256 CHAR(64) NOT NULL,
    runtime_descriptor_sha256 CHAR(64) NOT NULL,
    governance_anchor_sha256 CHAR(64) NOT NULL,
    policy_contract_sha256 CHAR(64) NOT NULL,
    enabled_entrypoints_sha256 CHAR(64) NOT NULL,
    snapshot_json JSON NOT NULL,
    snapshot_sha256 CHAR(64) NOT NULL,
    expected_effect_set_sha256 CHAR(64) NULL,
    error_code VARCHAR(64) NULL,
    error_summary VARCHAR(500) NULL,
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    prepared_at DATETIME(6) NULL,
    committed_at DATETIME(6) NULL,
    draining_at DATETIME(6) NULL,
    disposed_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (automation_id, generation),
    UNIQUE KEY uq_automation_generation_request (automation_id, request_id),
    KEY idx_automation_generation_reconcile (state, updated_at, automation_id),
    CONSTRAINT fk_automation_generation_project FOREIGN KEY (automation_id)
        REFERENCES automation_projects (automation_id) ON DELETE RESTRICT,
    CONSTRAINT fk_automation_generation_plugin_version
        FOREIGN KEY (plugin_id, plugin_version)
        REFERENCES automation_plugin_versions (plugin_id, version) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_generation_number CHECK (
        generation > 0
        AND (
            base_committed_generation IS NULL
            OR base_committed_generation > 0
        )
    ),
    CONSTRAINT chk_automation_generation_record_version CHECK (record_version > 0),
    CONSTRAINT chk_automation_generation_state CHECK (
        state IN (
            'TARGET', 'PREPARING', 'WAITING_COEFFECTS',
            'PREPARED', 'COMMITTED',
            'DRAINING', 'DISPOSING', 'DISPOSED', 'FAILED', 'BLOCKED'
        )
    ),
    CONSTRAINT chk_automation_generation_trust CHECK (
        trust_source IN (
            'ed25519_upload', 'ed25519_first_party', 'builtin_release'
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_generation_coeffects (
    automation_id VARCHAR(128) NOT NULL,
    generation BIGINT UNSIGNED NOT NULL,
    coeffect_kind VARCHAR(24) NOT NULL,
    coeffect_key VARCHAR(191) NOT NULL,
    revision VARCHAR(191) NOT NULL,
    ready BOOLEAN NOT NULL,
    observation_json JSON NOT NULL,
    observation_sha256 CHAR(64) NOT NULL,
    observed_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (automation_id, generation, coeffect_kind, coeffect_key),
    KEY idx_automation_generation_coeffect_ready (
        automation_id, generation, ready, coeffect_kind
    ),
    CONSTRAINT fk_automation_generation_coeffect FOREIGN KEY (
        automation_id, generation
    ) REFERENCES automation_project_generations (
        automation_id, generation
    ) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_generation_coeffect_kind CHECK (
        coeffect_kind IN ('ACCOUNT', 'SESSION', 'RESOURCE', 'DEVICE', 'CORE_ADAPTER')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_generation_effects (
    effect_id CHAR(36) NOT NULL,
    automation_id VARCHAR(128) NOT NULL,
    generation BIGINT UNSIGNED NOT NULL,
    effect_kind VARCHAR(32) NOT NULL,
    effect_key VARCHAR(191) NOT NULL,
    effect_sequence INT UNSIGNED NOT NULL,
    reversible BOOLEAN NOT NULL DEFAULT TRUE,
    state VARCHAR(24) NOT NULL DEFAULT 'PLANNED',
    evidence_json JSON NOT NULL,
    evidence_sha256 CHAR(64) NOT NULL,
    error_code VARCHAR(64) NULL,
    error_summary VARCHAR(500) NULL,
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    applied_at DATETIME(6) NULL,
    disposed_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (effect_id),
    UNIQUE KEY uq_automation_generation_effect_key (
        automation_id, generation, effect_key
    ),
    UNIQUE KEY uq_automation_generation_effect_sequence (
        automation_id, generation, effect_sequence
    ),
    KEY idx_automation_generation_effect_dispose (
        automation_id, generation, state, effect_sequence
    ),
    CONSTRAINT fk_automation_generation_effect FOREIGN KEY (
        automation_id, generation
    ) REFERENCES automation_project_generations (
        automation_id, generation
    ) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_generation_effect_reversible CHECK (
        reversible = TRUE
    ),
    CONSTRAINT chk_automation_generation_effect_record_version CHECK (
        record_version > 0 AND effect_sequence > 0
    ),
    CONSTRAINT chk_automation_generation_effect_kind CHECK (
        effect_kind IN (
            'PACKAGE_REFERENCE', 'VENV_REFERENCE', 'INSTANCE_RUNTIME',
            'SCHEDULE_BINDING', 'WEBHOOK_BINDING', 'BROKER_SCOPE',
            'WORKER_DEPLOYMENT', 'ENTRYPOINT_ROUTE'
        )
    ),
    CONSTRAINT chk_automation_generation_effect_state CHECK (
        state IN ('PLANNED', 'APPLIED', 'DISPOSING', 'DISPOSED', 'FAILED', 'BLOCKED')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_generation_leases (
    lease_id CHAR(36) NOT NULL,
    automation_id VARCHAR(128) NOT NULL,
    generation BIGINT UNSIGNED NOT NULL,
    lease_owner VARCHAR(191) NOT NULL,
    runtime_metadata_json JSON NOT NULL,
    runtime_metadata_sha256 CHAR(64) NOT NULL,
    verification_evidence_sha256 CHAR(64) NULL,
    outcome VARCHAR(32) NOT NULL DEFAULT 'RUNNING',
    acquired_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at DATETIME(6) NOT NULL,
    released_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (lease_id),
    KEY idx_automation_generation_lease_active (
        automation_id, generation, outcome, expires_at
    ),
    CONSTRAINT fk_automation_generation_lease FOREIGN KEY (
        automation_id, generation
    ) REFERENCES automation_project_generations (
        automation_id, generation
    ) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_generation_lease_outcome CHECK (
        outcome IN (
            'RUNNING', 'VERIFYING', 'SUCCEEDED', 'FAILED_BEFORE_WRITE',
            'WRITE_VERIFIED', 'WRITE_OUTCOME_UNKNOWN'
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_plugin_package_events (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    plugin_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    event_type VARCHAR(48) NOT NULL,
    from_version VARCHAR(64) NULL,
    to_version VARCHAR(64) NULL,
    metadata_json JSON NOT NULL,
    metadata_sha256 CHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    occurred_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    UNIQUE KEY uq_automation_plugin_package_event_request (plugin_id, request_id),
    KEY idx_automation_plugin_package_events_time (plugin_id, occurred_at),
    CONSTRAINT fk_automation_plugin_package_event FOREIGN KEY (plugin_id)
        REFERENCES automation_plugin_packages (plugin_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_events (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    automation_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    event_type VARCHAR(48) NOT NULL,
    from_state VARCHAR(24) NULL,
    to_state VARCHAR(24) NULL,
    metadata_json JSON NOT NULL,
    metadata_sha256 CHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    occurred_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    UNIQUE KEY uq_automation_project_event_request (automation_id, request_id),
    KEY idx_automation_project_events_time (automation_id, occurred_at),
    CONSTRAINT fk_automation_project_event_project FOREIGN KEY (automation_id)
        REFERENCES automation_projects (automation_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_policies (
    automation_id VARCHAR(128) NOT NULL,
    project_generation BIGINT UNSIGNED NOT NULL DEFAULT 1,
    mode VARCHAR(32) NOT NULL DEFAULT 'REQUIRE_EACH_RUN',
    contract_hash CHAR(64) NULL,
    contract_snapshot_json JSON NULL,
    tool_contract_hash CHAR(64) NULL,
    plugin_contract_hash CHAR(64) NULL,
    project_configuration_version BIGINT UNSIGNED NOT NULL DEFAULT 1,
    approved_by_actor_id VARCHAR(128) NULL,
    approved_by_actor_role VARCHAR(64) NULL,
    approved_by_actor_display_name VARCHAR(255) NULL,
    approved_at DATETIME(6) NULL,
    comment VARCHAR(500) NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (automation_id),
    CONSTRAINT fk_automation_project_policy_project FOREIGN KEY (automation_id)
        REFERENCES automation_projects (automation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_project_policy_mode CHECK (
        mode IN ('PROJECT_FULL_AUTO', 'REQUIRE_EACH_RUN', 'LEGACY_SCHEDULE_ONLY')
    ),
    CONSTRAINT chk_automation_project_policy_version CHECK (version > 0),
    CONSTRAINT chk_automation_project_policy_config_version CHECK (
        project_configuration_version > 0
    ),
    CONSTRAINT chk_automation_project_policy_generation CHECK (
        project_generation > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_policy_events (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    automation_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    from_mode VARCHAR(32) NULL,
    to_mode VARCHAR(32) NOT NULL,
    contract_hash CHAR(64) NULL,
    contract_snapshot_json JSON NULL,
    tool_contract_hash CHAR(64) NULL,
    plugin_contract_hash CHAR(64) NULL,
    project_configuration_version BIGINT UNSIGNED NOT NULL,
    project_generation BIGINT UNSIGNED NOT NULL DEFAULT 1,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    actor_display_name VARCHAR(255) NULL,
    reason VARCHAR(64) NOT NULL,
    comment VARCHAR(500) NULL,
    correlation_id CHAR(36) NOT NULL,
    occurred_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    UNIQUE KEY uq_automation_project_policy_request (automation_id, request_id),
    KEY idx_automation_project_policy_events_time (automation_id, occurred_at),
    CONSTRAINT fk_automation_project_policy_event_policy FOREIGN KEY (automation_id)
        REFERENCES automation_project_policies (automation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_project_policy_event_mode CHECK (
        to_mode IN ('PROJECT_FULL_AUTO', 'REQUIRE_EACH_RUN', 'LEGACY_SCHEDULE_ONLY')
    ),
    CONSTRAINT chk_automation_project_policy_event_generation CHECK (
        project_generation > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_approval_batches (
    batch_id CHAR(36) NOT NULL,
    automation_id VARCHAR(128) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    decision VARCHAR(16) NOT NULL,
    expected_pending_set_hash CHAR(64) NOT NULL,
    decided_pending_set_hash CHAR(64) NOT NULL,
    decided_count INT UNSIGNED NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    comment VARCHAR(500) NULL,
    result_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (batch_id),
    UNIQUE KEY uq_automation_project_approval_batch_request (automation_id, request_id),
    KEY idx_automation_project_approval_batch_time (automation_id, created_at),
    CONSTRAINT fk_automation_project_approval_batch_policy FOREIGN KEY (automation_id)
        REFERENCES automation_project_policies (automation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_project_approval_batch_decision CHECK (
        decision IN ('APPROVED', 'REJECTED')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Populated only by the release-held bootstrap service after it has compiled
-- every real-time project contract and downgraded legacy cron policies safely.
CREATE TABLE IF NOT EXISTS automation_project_bootstrap_items_018 (
    automation_id VARCHAR(128) NOT NULL,
    initial_mode VARCHAR(32) NOT NULL,
    source_set_sha256 CHAR(64) NOT NULL,
    source_snapshot_json JSON NOT NULL,
    policy_version INT UNSIGNED NOT NULL,
    completed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (automation_id),
    CONSTRAINT chk_automation_project_bootstrap_mode CHECK (
        initial_mode IN ('REQUIRE_EACH_RUN', 'LEGACY_SCHEDULE_ONLY')
    ),
    CONSTRAINT chk_automation_project_bootstrap_source_snapshot CHECK (
        JSON_TYPE(source_snapshot_json) = 'OBJECT'
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_project_bootstrap_marker_018 (
    marker_id TINYINT UNSIGNED NOT NULL,
    release_sha VARCHAR(64) NOT NULL,
    project_set_sha256 CHAR(64) NOT NULL,
    completed_by VARCHAR(128) NOT NULL,
    completed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (marker_id),
    CONSTRAINT chk_automation_project_bootstrap_marker_id CHECK (marker_id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ``bootstrap_items`` was introduced by this still-pending migration, but a
-- failed prior attempt may already have created the old empty table without
-- the retained evidence column. Repair only that empty layout. Once an item
-- or marker exists, missing evidence cannot be reconstructed safely and the
-- rerun must fail closed instead of manufacturing an authorization source.
SET @cp018_bootstrap_source_snapshot_column_count = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_bootstrap_items_018'
      AND COLUMN_NAME='source_snapshot_json'
);
SET @cp018_bootstrap_source_snapshot_wrong_type_count = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_bootstrap_items_018'
      AND COLUMN_NAME='source_snapshot_json'
      AND DATA_TYPE <> 'json'
);
SET @cp018_bootstrap_source_snapshot_shape_guard_sql = IF(
    @cp018_bootstrap_source_snapshot_column_count IN (0, 1)
    AND @cp018_bootstrap_source_snapshot_wrong_type_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_bootstrap_source_snapshot_shape_invalid'
);
PREPARE cp018_bootstrap_source_snapshot_shape_guard_stmt
    FROM @cp018_bootstrap_source_snapshot_shape_guard_sql;
EXECUTE cp018_bootstrap_source_snapshot_shape_guard_stmt;
DEALLOCATE PREPARE cp018_bootstrap_source_snapshot_shape_guard_stmt;

SET @cp018_bootstrap_evidence_missing_persisted_count = IF(
    @cp018_bootstrap_source_snapshot_column_count = 0,
    (SELECT COUNT(*) FROM automation_project_bootstrap_items_018)
        + (SELECT COUNT(*) FROM automation_project_bootstrap_marker_018),
    0
);
SET @cp018_bootstrap_evidence_missing_guard_sql = IF(
    @cp018_bootstrap_evidence_missing_persisted_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_bootstrap_evidence_unrecoverable'
);
PREPARE cp018_bootstrap_evidence_missing_guard_stmt
    FROM @cp018_bootstrap_evidence_missing_guard_sql;
EXECUTE cp018_bootstrap_evidence_missing_guard_stmt;
DEALLOCATE PREPARE cp018_bootstrap_evidence_missing_guard_stmt;

SET @cp018_add_bootstrap_source_snapshot_sql = IF(
    @cp018_bootstrap_source_snapshot_column_count = 0,
    'ALTER TABLE automation_project_bootstrap_items_018 ADD COLUMN source_snapshot_json JSON NULL AFTER source_set_sha256',
    'SELECT 1'
);
PREPARE cp018_add_bootstrap_source_snapshot_stmt
    FROM @cp018_add_bootstrap_source_snapshot_sql;
EXECUTE cp018_add_bootstrap_source_snapshot_stmt;
DEALLOCATE PREPARE cp018_add_bootstrap_source_snapshot_stmt;

SET @cp018_bootstrap_source_snapshot_null_count = (
    SELECT COUNT(*)
    FROM automation_project_bootstrap_items_018
    WHERE source_snapshot_json IS NULL
);
SET @cp018_bootstrap_source_snapshot_null_guard_sql = IF(
    @cp018_bootstrap_source_snapshot_null_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_bootstrap_source_snapshot_missing'
);
PREPARE cp018_bootstrap_source_snapshot_null_guard_stmt
    FROM @cp018_bootstrap_source_snapshot_null_guard_sql;
EXECUTE cp018_bootstrap_source_snapshot_null_guard_stmt;
DEALLOCATE PREPARE cp018_bootstrap_source_snapshot_null_guard_stmt;

SET @cp018_bootstrap_source_snapshot_nullable_count = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_bootstrap_items_018'
      AND COLUMN_NAME='source_snapshot_json'
      AND IS_NULLABLE='YES'
);
SET @cp018_require_bootstrap_source_snapshot_sql = IF(
    @cp018_bootstrap_source_snapshot_nullable_count = 1,
    'ALTER TABLE automation_project_bootstrap_items_018 MODIFY COLUMN source_snapshot_json JSON NOT NULL AFTER source_set_sha256',
    'SELECT 1'
);
PREPARE cp018_require_bootstrap_source_snapshot_stmt
    FROM @cp018_require_bootstrap_source_snapshot_sql;
EXECUTE cp018_require_bootstrap_source_snapshot_stmt;
DEALLOCATE PREPARE cp018_require_bootstrap_source_snapshot_stmt;

SET @cp018_bootstrap_source_snapshot_check_count = (
    SELECT COUNT(*)
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_bootstrap_items_018'
      AND CONSTRAINT_NAME='chk_automation_project_bootstrap_source_snapshot'
      AND CONSTRAINT_TYPE='CHECK'
);
SET @cp018_add_bootstrap_source_snapshot_check_sql = IF(
    @cp018_bootstrap_source_snapshot_check_count = 0,
    'ALTER TABLE automation_project_bootstrap_items_018 ADD CONSTRAINT chk_automation_project_bootstrap_source_snapshot CHECK (JSON_TYPE(source_snapshot_json) = ''OBJECT'')',
    'SELECT 1'
);
PREPARE cp018_add_bootstrap_source_snapshot_check_stmt
    FROM @cp018_add_bootstrap_source_snapshot_check_sql;
EXECUTE cp018_add_bootstrap_source_snapshot_check_stmt;
DEALLOCATE PREPARE cp018_add_bootstrap_source_snapshot_check_stmt;

SET @cp018_bootstrap_source_snapshot_final_check_count = (
    SELECT COUNT(*)
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_bootstrap_items_018'
      AND CONSTRAINT_NAME='chk_automation_project_bootstrap_source_snapshot'
      AND CONSTRAINT_TYPE='CHECK'
);

SET @cp018_bootstrap_source_snapshot_final_shape_count = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='automation_project_bootstrap_items_018'
      AND COLUMN_NAME='source_snapshot_json'
      AND DATA_TYPE='json'
      AND IS_NULLABLE='NO'
);
SET @cp018_bootstrap_source_snapshot_final_guard_sql = IF(
    @cp018_bootstrap_source_snapshot_final_shape_count = 1
    AND @cp018_bootstrap_source_snapshot_final_check_count = 1,
    'SELECT 1',
    'SELECT * FROM information_schema.cp018_bootstrap_source_snapshot_final_invalid'
);
PREPARE cp018_bootstrap_source_snapshot_final_guard_stmt
    FROM @cp018_bootstrap_source_snapshot_final_guard_sql;
EXECUTE cp018_bootstrap_source_snapshot_final_guard_stmt;
DEALLOCATE PREPARE cp018_bootstrap_source_snapshot_final_guard_stmt;

CREATE TABLE IF NOT EXISTS automation_worker_devices (
    device_id VARCHAR(128) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    service_state VARCHAR(24) NOT NULL DEFAULT 'OFFLINE',
    interactive_session_state VARCHAR(24) NOT NULL DEFAULT 'LOGGED_OUT',
    agent_version VARCHAR(64) NOT NULL,
    identity_json JSON NOT NULL,
    identity_sha256 CHAR(64) NOT NULL,
    paired_public_key_fingerprint CHAR(64) NOT NULL,
    capabilities_json JSON NOT NULL,
    capabilities_sha256 CHAR(64) NOT NULL,
    dispatch_sequence BIGINT UNSIGNED NOT NULL DEFAULT 0,
    inbound_sequence BIGINT UNSIGNED NULL,
    last_inbound_message_id CHAR(36) NULL,
    last_inbound_envelope_sha256 CHAR(64) NULL,
    lease_owner VARCHAR(191) NULL,
    lease_expires_at DATETIME(6) NULL,
    last_seen_at DATETIME(6) NULL,
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (device_id),
    KEY idx_automation_worker_devices_available (
        service_state, interactive_session_state, lease_expires_at, device_id
    ),
    CONSTRAINT chk_automation_worker_device_service_state CHECK (
        service_state IN ('ONLINE', 'OFFLINE', 'DRAINING', 'DISABLED')
    ),
    CONSTRAINT chk_automation_worker_device_session_state CHECK (
        interactive_session_state IN ('AVAILABLE', 'LOCKED', 'LOGGED_OUT')
    ),
    CONSTRAINT chk_automation_worker_device_version CHECK (record_version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_worker_pairing_events (
    event_id CHAR(36) NOT NULL,
    device_id VARCHAR(128) NOT NULL,
    request_id CHAR(36) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    metadata_json JSON NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    identity_sha256 CHAR(64) NOT NULL,
    capabilities_sha256 CHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    occurred_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    UNIQUE KEY uq_automation_worker_pairing_request (request_id),
    KEY idx_automation_worker_pairing_device (device_id, occurred_at),
    CONSTRAINT fk_automation_worker_pairing_device FOREIGN KEY (device_id)
        REFERENCES automation_worker_devices (device_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_worker_pairing_event_type CHECK (
        event_type = 'PAIRED'
    ),
    CONSTRAINT chk_automation_worker_pairing_role CHECK (
        actor_role = 'super_admin'
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_worker_jobs (
    job_id CHAR(36) NOT NULL,
    automation_id VARCHAR(128) NOT NULL,
    automation_generation BIGINT UNSIGNED NOT NULL,
    plugin_id VARCHAR(64) NOT NULL,
    plugin_version VARCHAR(64) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    job_type VARCHAR(24) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    payload_json JSON NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    worker_requirement_json JSON NOT NULL,
    worker_requirement_sha256 CHAR(64) NOT NULL,
    operation_type VARCHAR(32) NOT NULL,
    requires_interactive_session BOOLEAN NOT NULL DEFAULT FALSE,
    cleanup_scope VARCHAR(16) NULL,
    target_device_id VARCHAR(128) NOT NULL,
    assigned_device_id VARCHAR(128) NULL,
    lease_owner VARCHAR(191) NULL,
    lease_expires_at DATETIME(6) NULL,
    dispatch_message_id CHAR(36) NULL,
    dispatch_sequence BIGINT UNSIGNED NULL,
    dispatch_envelope_json JSON NULL,
    dispatch_envelope_sha256 CHAR(64) NULL,
    dispatch_release_sha VARCHAR(64) NULL,
    dispatch_authorization_id CHAR(36) NULL,
    dispatched_at DATETIME(6) NULL,
    attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
    max_attempts INT UNSIGNED NOT NULL DEFAULT 1,
    result_json JSON NULL,
    result_sha256 CHAR(64) NULL,
    error_code VARCHAR(64) NULL,
    error_summary VARCHAR(500) NULL,
    available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    deadline_at DATETIME(6) NOT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    record_version INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (job_id),
    UNIQUE KEY uq_automation_worker_job_request (automation_id, request_id),
    UNIQUE KEY uq_automation_worker_job_message (dispatch_message_id),
    UNIQUE KEY uq_automation_worker_job_authorization (dispatch_authorization_id),
    UNIQUE KEY uq_automation_worker_job_device_sequence (
        assigned_device_id, dispatch_sequence
    ),
    KEY idx_automation_worker_jobs_claim (status, available_at, deadline_at, created_at),
    KEY idx_automation_worker_jobs_device (assigned_device_id, status, lease_expires_at),
    CONSTRAINT fk_automation_worker_job_project FOREIGN KEY (automation_id)
        REFERENCES automation_projects (automation_id) ON DELETE RESTRICT,
    CONSTRAINT fk_automation_worker_job_generation FOREIGN KEY (
        automation_id, automation_generation
    ) REFERENCES automation_project_generations (
        automation_id, generation
    ) ON DELETE RESTRICT,
    CONSTRAINT fk_automation_worker_job_version FOREIGN KEY (plugin_id, plugin_version)
        REFERENCES automation_plugin_versions (plugin_id, version) ON DELETE RESTRICT,
    CONSTRAINT fk_automation_worker_job_target_device FOREIGN KEY (target_device_id)
        REFERENCES automation_worker_devices (device_id) ON DELETE RESTRICT,
    CONSTRAINT fk_automation_worker_job_assigned_device FOREIGN KEY (assigned_device_id)
        REFERENCES automation_worker_devices (device_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_worker_job_type CHECK (
        job_type IN ('INSTALL', 'UPGRADE', 'UNINSTALL', 'INVOKE', 'CLEANUP')
    ),
    CONSTRAINT chk_automation_worker_job_status CHECK (
        status IN (
            'PENDING', 'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED',
            'CANCELLED', 'BLOCKED_DATA', 'OUTCOME_UNKNOWN'
        )
    ),
    CONSTRAINT chk_automation_worker_job_attempts CHECK (
        max_attempts > 0 AND attempt_count <= max_attempts
    ),
    CONSTRAINT chk_automation_worker_job_operation CHECK (
        operation_type IN (
            'read', 'compute', 'internal_projection_write',
            'external_write', 'financial_write', 'destructive'
        )
    ),
    CONSTRAINT chk_automation_worker_job_cleanup_scope CHECK (
        cleanup_scope IS NULL OR cleanup_scope IN ('INSTANCE', 'PACKAGE')
    ),
    CONSTRAINT chk_automation_worker_job_dispatch CHECK (
        (
            dispatch_message_id IS NULL AND dispatch_sequence IS NULL
            AND dispatch_envelope_json IS NULL
            AND dispatch_envelope_sha256 IS NULL
            AND dispatch_release_sha IS NULL
            AND dispatch_authorization_id IS NULL
            AND dispatched_at IS NULL
        ) OR (
            dispatch_message_id IS NOT NULL AND dispatch_sequence > 0
            AND dispatch_envelope_json IS NOT NULL
            AND dispatch_envelope_sha256 IS NOT NULL
            AND dispatch_release_sha IS NOT NULL
            AND dispatch_authorization_id IS NOT NULL
            AND dispatched_at IS NOT NULL
        )
    ),
    CONSTRAINT chk_automation_worker_job_version CHECK (record_version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Signed Worker replies are immutable and replay-safe.  A response loss can
-- return the already persisted result for the same device/message/sequence.
CREATE TABLE IF NOT EXISTS automation_worker_job_messages (
    message_id CHAR(36) NOT NULL,
    device_id VARCHAR(128) NOT NULL,
    sequence BIGINT UNSIGNED NOT NULL,
    job_id CHAR(36) NOT NULL,
    dispatch_message_id CHAR(36) NOT NULL,
    dispatch_authorization_id CHAR(36) NOT NULL,
    lease_owner VARCHAR(191) NOT NULL,
    message_kind VARCHAR(16) NOT NULL,
    envelope_json JSON NOT NULL,
    envelope_sha256 CHAR(64) NOT NULL,
    body_json JSON NOT NULL,
    body_sha256 CHAR(64) NOT NULL,
    processed_status VARCHAR(24) NOT NULL,
    received_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (message_id),
    UNIQUE KEY uq_automation_worker_message_sequence (device_id, sequence),
    KEY idx_automation_worker_message_job (job_id, received_at),
    CONSTRAINT fk_automation_worker_message_device FOREIGN KEY (device_id)
        REFERENCES automation_worker_devices (device_id) ON DELETE RESTRICT,
    CONSTRAINT fk_automation_worker_message_job FOREIGN KEY (job_id)
        REFERENCES automation_worker_jobs (job_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_worker_message_kind CHECK (
        message_kind IN ('ACK', 'JOB_STATUS')
    ),
    CONSTRAINT chk_automation_worker_message_sequence CHECK (sequence >= 0),
    CONSTRAINT chk_automation_worker_message_status CHECK (
        processed_status IN (
            'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED',
            'BLOCKED_DATA', 'OUTCOME_UNKNOWN'
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- No project/package FK: offline device cleanup must survive hard deletion.
CREATE TABLE IF NOT EXISTS automation_worker_cleanup_directives (
    directive_id CHAR(36) NOT NULL,
    purge_id CHAR(36) NOT NULL,
    device_id VARCHAR(128) NOT NULL,
    automation_id VARCHAR(128) NOT NULL,
    plugin_id VARCHAR(64) NOT NULL,
    plugin_version VARCHAR(64) NOT NULL,
    package_sha256 CHAR(64) NOT NULL,
    cleanup_scope VARCHAR(16) NOT NULL DEFAULT 'INSTANCE',
    state VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    request_id VARCHAR(191) NOT NULL,
    deadline_at DATETIME(6) NOT NULL,
    claimed_by VARCHAR(191) NULL,
    claimed_at DATETIME(6) NULL,
    acknowledged_by VARCHAR(191) NULL,
    acknowledged_result_sha256 CHAR(64) NULL,
    acknowledged_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (directive_id),
    UNIQUE KEY uq_automation_cleanup_device_request (device_id, request_id),
    UNIQUE KEY uq_automation_cleanup_purge_device (purge_id, device_id),
    KEY idx_automation_cleanup_claim (device_id, state, deadline_at, created_at),
    CONSTRAINT fk_automation_cleanup_device FOREIGN KEY (device_id)
        REFERENCES automation_worker_devices (device_id) ON DELETE RESTRICT,
    CONSTRAINT chk_automation_cleanup_scope CHECK (cleanup_scope IN ('INSTANCE', 'PACKAGE')),
    CONSTRAINT chk_automation_cleanup_state CHECK (
        state IN ('PENDING', 'CLAIMED', 'ACKNOWLEDGED', 'EXPIRED', 'BLOCKED_DATA')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Short-lived and FK-free so an interrupted ordered purge is recoverable.
CREATE TABLE IF NOT EXISTS automation_plugin_purge_journal (
    purge_id CHAR(36) NOT NULL,
    automation_id VARCHAR(128) NOT NULL,
    plugin_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(191) NOT NULL,
    plugin_version VARCHAR(64) NOT NULL,
    package_sha256 CHAR(64) NOT NULL,
    cleanup_scope VARCHAR(16) NOT NULL,
    delete_shared_package BOOLEAN NOT NULL DEFAULT FALSE,
    phase VARCHAR(32) NOT NULL,
    cleanup_devices_json JSON NOT NULL,
    cleanup_devices_sha256 CHAR(64) NOT NULL,
    instance_snapshot_json JSON NOT NULL,
    instance_snapshot_sha256 CHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    actor_role VARCHAR(64) NOT NULL,
    error_code VARCHAR(64) NULL,
    error_summary VARCHAR(500) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (purge_id),
    UNIQUE KEY uq_automation_plugin_purge_request (automation_id, request_id),
    KEY idx_automation_plugin_purge_phase (phase, updated_at),
    CONSTRAINT chk_automation_plugin_purge_scope CHECK (cleanup_scope IN ('INSTANCE', 'PACKAGE')),
    CONSTRAINT chk_automation_plugin_purge_phase CHECK (
        phase IN (
            'PREPARED', 'DIRECTIVES_WRITTEN', 'FINALIZE_RESERVED',
            'CONTROL_PLANE_DELETED',
            'COMMITTED', 'BLOCKED_DATA'
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
