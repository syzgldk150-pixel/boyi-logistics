-- Convert production scheduler rows to closed control-plane contracts without guessing
-- an account or overwriting administrator-selected cron/parameter values.  The backup
-- table intentionally keeps the exact pre-cutover row for data-level recovery.

CREATE TABLE IF NOT EXISTS control_plane_task_cutover_backup_014 LIKE scheduled_tasks;

INSERT IGNORE INTO control_plane_task_cutover_backup_014
SELECT *
FROM scheduled_tasks
WHERE id IN ('finance_bills_0010', 'clockin_daxiang_1830', 'clockin_daxiang_s_1830')
   OR (tool_name = 'sync_daily_send_orders' AND id REGEXP '^send_order_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_delivery_status' AND id REGEXP '^delivery_status_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_daily_should_sign' AND id REGEXP '^daily_sign_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_site_send_list' AND id REGEXP '^site_send_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_arrive_list' AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_yunda_dispatch_forecast' AND id REGEXP '^yunda_dispatch_forecast_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_yunda_send_waybills' AND id REGEXP '^yunda_send_waybills_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_arrival_stats' AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$');

UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.r13_account_id', 'r13_default',
    '$.problem_account_id', 'ronghui_daxiang_s',
    '$.sign_account_id', 'ronghui_daxiang_s',
    '$.detail_account_id', 'ronghui_daxiang_s',
    '$.days', 7
)
WHERE tool_name = 'sync_daily_should_sign'
  AND id REGEXP '^daily_sign_([01][0-9]|2[0-3])[0-5][0-9]$';

UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.platform', 'ronghui'
)
WHERE id = 'finance_bills_0010'
  AND tool_name = 'sync_finance_bills';

-- Arrival projection writes must bind one authoritative top-level account.  A
-- nested request-body account may repeat that value, but may never select a
-- different account.  The known Ronghui account-directory base ID is inserted
-- only for the closed Console task ID families; an existing administrator value
-- wins.  Conflicting rows are retained for repair but disabled before start.
UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.account_id', 'ronghui_default'
)
WHERE (tool_name = 'sync_arrive_list' AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_arrival_stats' AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$');

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = CASE
        WHEN JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.account_id')) <> 'STRING'
          OR NULLIF(
              TRIM(JSON_UNQUOTE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.account_id'))),
              ''
          ) IS NULL
        THEN '控制平面迁移已停用：请在 Console 自动化中选择明确的融辉运行账号后再启用'
        ELSE '控制平面迁移已停用：嵌套请求账号与顶层 account_id 冲突，请在 Console 自动化中修正'
    END
WHERE (
      (tool_name = 'sync_arrive_list' AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_arrival_stats' AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$')
  )
  AND (
      JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.account_id')) <> 'STRING'
      OR NULLIF(
          TRIM(JSON_UNQUOTE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.account_id'))),
          ''
      ) IS NULL
      OR (
          JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.request_body.params.account_id') IS NOT NULL
          AND (
              JSON_TYPE(JSON_EXTRACT(tool_params, '$.request_body.params.account_id')) <> 'STRING'
              OR NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.request_body.params.account_id'))), '') IS NULL
              OR TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.request_body.params.account_id')))
                 <> TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id')))
          )
      )
      OR (
          JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.request_body.params.accountId') IS NOT NULL
          AND (
              JSON_TYPE(JSON_EXTRACT(tool_params, '$.request_body.params.accountId')) <> 'STRING'
              OR NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.request_body.params.accountId'))), '') IS NULL
              OR TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.request_body.params.accountId')))
                 <> TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id')))
          )
      )
      OR (
          JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.scan_request_body.params.account_id') IS NOT NULL
          AND (
              JSON_TYPE(JSON_EXTRACT(tool_params, '$.scan_request_body.params.account_id')) <> 'STRING'
              OR NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.scan_request_body.params.account_id'))), '') IS NULL
              OR TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.scan_request_body.params.account_id')))
                 <> TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id')))
          )
      )
      OR (
          JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.scan_request_body.params.accountId') IS NOT NULL
          AND (
              JSON_TYPE(JSON_EXTRACT(tool_params, '$.scan_request_body.params.accountId')) <> 'STRING'
              OR NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.scan_request_body.params.accountId'))), '') IS NULL
              OR TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.scan_request_body.params.accountId')))
                 <> TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id')))
          )
      )
      OR (
          JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.arrive_list_request_body.params.account_id') IS NOT NULL
          AND (
              JSON_TYPE(JSON_EXTRACT(tool_params, '$.arrive_list_request_body.params.account_id')) <> 'STRING'
              OR NULLIF(
                  TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.arrive_list_request_body.params.account_id'))),
                  ''
              ) IS NULL
              OR TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.arrive_list_request_body.params.account_id')))
                 <> TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id')))
          )
      )
      OR (
          JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.arrive_list_request_body.params.accountId') IS NOT NULL
          AND (
              JSON_TYPE(JSON_EXTRACT(tool_params, '$.arrive_list_request_body.params.accountId')) <> 'STRING'
              OR NULLIF(
                  TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.arrive_list_request_body.params.accountId'))),
                  ''
              ) IS NULL
              OR TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.arrive_list_request_body.params.accountId')))
                 <> TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id')))
          )
      )
  );

-- Close every code-approved Console schedule family over an explicit account
-- directory ID. JSON_INSERT never overwrites an administrator-selected value;
-- a conflicting or unknown value is retained and rejected by the checks below.
UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.account_id', 'ronghui_default'
)
WHERE (tool_name = 'sync_daily_send_orders' AND id REGEXP '^send_order_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_delivery_status' AND id REGEXP '^delivery_status_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_site_send_list' AND id REGEXP '^site_send_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_arrive_list' AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_arrival_stats' AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$');

UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.account_id', 'yunda_default'
)
WHERE (tool_name = 'sync_yunda_dispatch_forecast' AND id REGEXP '^yunda_dispatch_forecast_([01][0-9]|2[0-3])[0-5][0-9]$')
   OR (tool_name = 'sync_yunda_send_waybills' AND id REGEXP '^yunda_send_waybills_([01][0-9]|2[0-3])[0-5][0-9]$');

UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.trigger_flow', CAST('false' AS JSON)
)
WHERE tool_name = 'sync_arrival_stats'
  AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$';

UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.dest_brch', '56739382'
)
WHERE tool_name = 'sync_yunda_dispatch_forecast'
  AND id REGEXP '^yunda_dispatch_forecast_([01][0-9]|2[0-3])[0-5][0-9]$';

UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.ensure_fields', CAST('true' AS JSON)
)
WHERE tool_name = 'sync_yunda_send_waybills'
  AND id REGEXP '^yunda_send_waybills_([01][0-9]|2[0-3])[0-5][0-9]$';

UPDATE scheduled_tasks
SET tool_params = JSON_INSERT(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.mode', 'sync',
    '$.platform', 'ronghui',
    '$.rescan_days', 7
)
WHERE id = 'finance_bills_0010'
  AND tool_name = 'sync_finance_bills';

-- Remove only known legacy presentation/default fields. A dated schedule or any
-- unknown/custom argument remains visible and is disabled instead of being
-- silently broadened into the allowlist.
UPDATE scheduled_tasks
SET tool_params = JSON_REMOVE(COALESCE(tool_params, JSON_OBJECT()), '$.target_date')
WHERE (
      (tool_name = 'sync_daily_send_orders' AND id REGEXP '^send_order_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_arrive_list' AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_arrival_stats' AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_yunda_dispatch_forecast' AND id REGEXP '^yunda_dispatch_forecast_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_yunda_send_waybills' AND id REGEXP '^yunda_send_waybills_([01][0-9]|2[0-3])[0-5][0-9]$')
  )
  AND JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.target_date')) = 'STRING'
  AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.target_date'))) = '';

UPDATE scheduled_tasks
SET tool_params = JSON_REMOVE(
    COALESCE(tool_params, JSON_OBJECT()),
    '$.login_site_code',
    '$.loginSiteCode',
    '$.LOGIN_SITE_CODE'
)
WHERE tool_name = 'sync_arrive_list'
  AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$';

UPDATE scheduled_tasks
SET tool_params = JSON_REMOVE(COALESCE(tool_params, JSON_OBJECT()), '$.accountId')
WHERE (
      (tool_name = 'sync_daily_send_orders' AND id REGEXP '^send_order_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_delivery_status' AND id REGEXP '^delivery_status_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_site_send_list' AND id REGEXP '^site_send_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_arrive_list' AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_arrival_stats' AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$')
  )
  AND JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.accountId')) = 'STRING'
  AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.accountId'))) = 'ronghui_default';

UPDATE scheduled_tasks
SET tool_params = JSON_REMOVE(COALESCE(tool_params, JSON_OBJECT()), '$.accountId')
WHERE (
      (tool_name = 'sync_yunda_dispatch_forecast' AND id REGEXP '^yunda_dispatch_forecast_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_yunda_send_waybills' AND id REGEXP '^yunda_send_waybills_([01][0-9]|2[0-3])[0-5][0-9]$')
  )
  AND JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.accountId')) = 'STRING'
  AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.accountId'))) = 'yunda_default';

UPDATE scheduled_tasks
SET tool_params = JSON_REMOVE(COALESCE(tool_params, JSON_OBJECT()), '$.session_profile')
WHERE (
      (tool_name = 'sync_daily_send_orders' AND id REGEXP '^send_order_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_delivery_status' AND id REGEXP '^delivery_status_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_site_send_list' AND id REGEXP '^site_send_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_arrive_list' AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_arrival_stats' AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$')
  )
  AND JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.session_profile')) = 'STRING'
  AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.session_profile'))) = 'default';

UPDATE scheduled_tasks
SET tool_params = JSON_REMOVE(COALESCE(tool_params, JSON_OBJECT()), '$.session_profile')
WHERE (
      (tool_name = 'sync_yunda_dispatch_forecast' AND id REGEXP '^yunda_dispatch_forecast_([01][0-9]|2[0-3])[0-5][0-9]$')
      OR (tool_name = 'sync_yunda_send_waybills' AND id REGEXP '^yunda_send_waybills_([01][0-9]|2[0-3])[0-5][0-9]$')
  )
  AND JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.session_profile')) = 'STRING'
  AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.session_profile'))) = 'yunda';

-- Every enabled schedule must now equal one code-owned canonical argument set.
-- Rows outside these exact contracts remain recoverable in both scheduled_tasks
-- and the backup table, but cannot obtain schedule_allowlist exemption.
UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：融辉账号或定时参数不符合代码批准契约，请在 Console 自动化中修正'
WHERE tool_name = 'sync_daily_send_orders'
  AND id REGEXP '^send_order_([01][0-9]|2[0-3])[0-5][0-9]$'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 1
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id'))) = 'ronghui_default',
      FALSE
  );

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：融辉账号或定时参数不符合代码批准契约，请在 Console 自动化中修正'
WHERE tool_name = 'sync_delivery_status'
  AND id REGEXP '^delivery_status_([01][0-9]|2[0-3])[0-5][0-9]$'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 1
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id'))) = 'ronghui_default',
      FALSE
  );

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：融辉账号或定时参数不符合代码批准契约，请在 Console 自动化中修正'
WHERE tool_name = 'sync_site_send_list'
  AND id REGEXP '^site_send_([01][0-9]|2[0-3])[0-5][0-9]$'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 1
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id'))) = 'ronghui_default',
      FALSE
  );

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：到货清单账号或参数不符合代码批准契约，请在 Console 自动化中修正'
WHERE tool_name = 'sync_arrive_list'
  AND id REGEXP '^arrive_list_([01][0-9]|2[0-3])[0-5][0-9]$'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 1
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id'))) = 'ronghui_default',
      FALSE
  );

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：到货统计账号或参数不符合代码批准契约，请在 Console 自动化中修正'
WHERE tool_name = 'sync_arrival_stats'
  AND id REGEXP '^arrival_stats_([01][0-9]|2[0-3])[0-5][0-9]$'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 2
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id'))) = 'ronghui_default'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.trigger_flow')) = 'BOOLEAN'
      AND JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.trigger_flow')) = 'false',
      FALSE
  );

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：韵达账号、目的网点或定时参数不符合代码批准契约，请在 Console 自动化中修正'
WHERE tool_name = 'sync_yunda_dispatch_forecast'
  AND id REGEXP '^yunda_dispatch_forecast_([01][0-9]|2[0-3])[0-5][0-9]$'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 2
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id'))) = 'yunda_default'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.dest_brch')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.dest_brch'))) = '56739382',
      FALSE
  );

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：韵达账号、字段策略或定时参数不符合代码批准契约，请在 Console 自动化中修正'
WHERE tool_name = 'sync_yunda_send_waybills'
  AND id REGEXP '^yunda_send_waybills_([01][0-9]|2[0-3])[0-5][0-9]$'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 2
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.account_id'))) = 'yunda_default'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.ensure_fields')) = 'BOOLEAN'
      AND JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.ensure_fields')) = 'true',
      FALSE
  );

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：每日应签四账号或查询范围不符合代码批准契约，请在 Console 自动化中修正'
WHERE tool_name = 'sync_daily_should_sign'
  AND id REGEXP '^daily_sign_([01][0-9]|2[0-3])[0-5][0-9]$'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 5
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.r13_account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.r13_account_id'))) = 'r13_default'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.problem_account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.problem_account_id'))) = 'ronghui_daxiang_s'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.sign_account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.sign_account_id'))) = 'ronghui_daxiang_s'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.detail_account_id')) = 'STRING'
      AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.detail_account_id'))) = 'ronghui_daxiang_s'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.days')) = 'INTEGER'
      AND JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.days')) = '7',
      FALSE
  );

UPDATE scheduled_tasks
SET enabled = FALSE,
    last_status = 'disabled',
    last_message = '控制平面迁移已停用：财务多来源范围不符合代码批准契约，请在 Console 自动化中修正'
WHERE id = 'finance_bills_0010'
  AND tool_name = 'sync_finance_bills'
  AND NOT COALESCE(
      JSON_LENGTH(COALESCE(tool_params, JSON_OBJECT())) = 3
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.mode')) = 'STRING'
      AND JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.mode')) = 'sync'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.platform')) = 'STRING'
      AND JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.platform')) = 'ronghui'
      AND JSON_TYPE(JSON_EXTRACT(tool_params, '$.rescan_days')) = 'INTEGER'
      AND JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.rescan_days')) = '7',
      FALSE
  );

-- The legacy tms_query wrapper stored the administrator's clock parameters in
-- $.params.params.  Promote that object as a whole, then rename only the two
-- legacy aliases.  No site code is invented and all other business parameters
-- in the nested object survive the conversion.
UPDATE scheduled_tasks
SET tool_name = 'clock_in_dual',
    tool_params = JSON_MERGE_PATCH(
        COALESCE(tool_params, JSON_OBJECT()),
        COALESCE(
            JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.params'),
            JSON_OBJECT()
        ),
        COALESCE(
            JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.params.params'),
            JSON_OBJECT()
        )
    )
WHERE id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1830')
  AND tool_name = 'tms_query';

UPDATE scheduled_tasks
SET tool_params = JSON_SET(
        COALESCE(tool_params, JSON_OBJECT()),
        '$.sitename', JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.site_name'))
    )
WHERE id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1830')
  AND tool_name = 'clock_in_dual'
  AND NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.sitename'))), '') IS NULL
  AND NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.site_name'))), '') IS NOT NULL;

UPDATE scheduled_tasks
SET tool_params = JSON_SET(
        COALESCE(tool_params, JSON_OBJECT()),
        '$.sitefbname', JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.site_fb_name'))
    )
WHERE id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1830')
  AND tool_name = 'clock_in_dual'
  AND NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.sitefbname'))), '') IS NULL
  AND NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.site_fb_name'))), '') IS NOT NULL;

UPDATE scheduled_tasks
SET tool_params = JSON_REMOVE(
        COALESCE(tool_params, JSON_OBJECT()),
        '$.site_name',
        '$.site_fb_name',
        '$.endpoint',
        '$.mode',
        '$.params'
    )
WHERE id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1830')
  AND tool_name = 'clock_in_dual';

UPDATE scheduled_tasks
SET
    enabled = FALSE,
    last_status = 'disabled',
    last_message = '缺少明确 sitecode/sitefbcode，控制平面迁移已安全停用；请在 Console 自动化中配置后再启用'
WHERE id IN ('clockin_daxiang_1830', 'clockin_daxiang_s_1830')
  AND tool_name = 'clock_in_dual'
  AND (
      JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.sitecode')) <> 'STRING'
      OR NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.sitecode'))), '') IS NULL
      OR JSON_TYPE(JSON_EXTRACT(COALESCE(tool_params, JSON_OBJECT()), '$.sitefbcode')) <> 'STRING'
      OR NULLIF(TRIM(JSON_UNQUOTE(JSON_EXTRACT(tool_params, '$.sitefbcode'))), '') IS NULL
  );
