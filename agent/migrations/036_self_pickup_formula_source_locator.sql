-- Feishu renders UeBd3I from a FILTER dynamic array, but its Open API does
-- not expose the spilled cells. Persist the exact, reviewed source range from
-- that formula without changing the logical worksheet bound to the project.

SET @cp036_resource_count = (
    SELECT COUNT(*)
    FROM workflow_resources
    WHERE BINARY resource_key = BINARY 'phase7.self_pickup_source_sheet'
);

SET @cp036_invalid_resource_count = (
    SELECT COUNT(*)
    FROM workflow_resources
    WHERE BINARY resource_key = BINARY 'phase7.self_pickup_source_sheet'
      AND NOT COALESCE(
          JSON_TYPE(config_json) = 'OBJECT'
          AND JSON_TYPE(JSON_EXTRACT(config_json, '$.resource_kind')) = 'STRING'
          AND BINARY JSON_UNQUOTE(JSON_EXTRACT(config_json, '$.resource_kind')) =
              BINARY 'feishu_sheet'
          AND JSON_TYPE(JSON_EXTRACT(config_json, '$.spreadsheet_token')) = 'STRING'
          AND BINARY JSON_UNQUOTE(JSON_EXTRACT(config_json, '$.spreadsheet_token')) =
              BINARY 'F0NVsI5dlhaWugtw14YcmdrQnvh'
          AND JSON_TYPE(JSON_EXTRACT(config_json, '$.sheet_id')) = 'STRING'
          AND BINARY JSON_UNQUOTE(JSON_EXTRACT(config_json, '$.sheet_id')) =
              BINARY 'UeBd3I'
          AND JSON_TYPE(JSON_EXTRACT(config_json, '$.range')) = 'STRING'
          AND BINARY JSON_UNQUOTE(JSON_EXTRACT(config_json, '$.range')) =
              BINARY 'UeBd3I!A1:S5000'
          AND (
              JSON_EXTRACT(config_json, '$.formula_source_sheet_id') IS NULL
              OR (
                  JSON_TYPE(JSON_EXTRACT(config_json, '$.formula_source_sheet_id')) = 'STRING'
                  AND BINARY JSON_UNQUOTE(
                      JSON_EXTRACT(config_json, '$.formula_source_sheet_id')
                  ) = BINARY '8fc516'
              )
          )
          AND (
              JSON_EXTRACT(config_json, '$.formula_source_range') IS NULL
              OR (
                  JSON_TYPE(JSON_EXTRACT(config_json, '$.formula_source_range')) = 'STRING'
                  AND BINARY JSON_UNQUOTE(
                      JSON_EXTRACT(config_json, '$.formula_source_range')
                  ) = BINARY '8fc516!A1:S197'
              )
          )
          AND configuration_version > 0
          AND BINARY config_sha256 = BINARY SHA2(
              CAST(config_json AS CHAR CHARACTER SET utf8mb4),
              256
          ),
          FALSE
      )
);

SET @cp036_resource_guard_sql = IF(
    @cp036_resource_count = 1 AND @cp036_invalid_resource_count = 0,
    'SELECT 1',
    'SELECT * FROM information_schema.cp036_self_pickup_resource_invalid'
);
PREPARE cp036_resource_guard_stmt FROM @cp036_resource_guard_sql;
EXECUTE cp036_resource_guard_stmt;
DEALLOCATE PREPARE cp036_resource_guard_stmt;

UPDATE workflow_resources
SET
    configuration_version = configuration_version + IF(
        JSON_TYPE(JSON_EXTRACT(config_json, '$.formula_source_sheet_id')) = 'STRING'
        AND BINARY JSON_UNQUOTE(
            JSON_EXTRACT(config_json, '$.formula_source_sheet_id')
        ) = BINARY '8fc516'
        AND JSON_TYPE(JSON_EXTRACT(config_json, '$.formula_source_range')) = 'STRING'
        AND BINARY JSON_UNQUOTE(
            JSON_EXTRACT(config_json, '$.formula_source_range')
        ) = BINARY '8fc516!A1:S197',
        0,
        1
    ),
    config_sha256 = SHA2(
        CAST(
            JSON_SET(
                config_json,
                '$.formula_source_sheet_id',
                '8fc516',
                '$.formula_source_range',
                '8fc516!A1:S197'
            ) AS CHAR CHARACTER SET utf8mb4
        ),
        256
    ),
    config_json = JSON_SET(
        config_json,
        '$.formula_source_sheet_id',
        '8fc516',
        '$.formula_source_range',
        '8fc516!A1:S197'
    ),
    updated_at = updated_at
WHERE BINARY resource_key = BINARY 'phase7.self_pickup_source_sheet';
