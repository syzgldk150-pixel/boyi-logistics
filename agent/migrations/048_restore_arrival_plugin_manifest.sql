-- One-time restoration of the verified statistics V2 package's schema definition.
-- Generic audit redaction previously replaced this object with [REDACTED].
-- Match the exact released archive and manifest; do not rewrite other versions,
-- project settings, schedules, invocation history, or business records.
UPDATE automation_plugin_versions
SET manifest_json = JSON_SET(
    manifest_json, '$.config_schema.properties.arrive_list_request_body',
    JSON_OBJECT('type', 'object', 'additionalProperties', CAST('false' AS JSON),
                'properties', JSON_OBJECT(), 'required', JSON_ARRAY())
)
WHERE plugin_id = 'sync_arrival_stats_v2' AND version = '2.0.0'
  AND runtime_model = 'SERVICE_V2'
  AND package_sha256 = 'cfe0d70275a41770e23897edea1cb1279773812e0fb98e244d8668b1c4fac7eb'
  AND manifest_sha256 = 'ea0d3efb0db644f1cbbb2d15d37778812d68db0b0bfcf6485ce7a472363e37c5'
  AND JSON_TYPE(JSON_EXTRACT(manifest_json, '$.config_schema.properties.arrive_list_request_body')) = 'STRING'
  AND JSON_UNQUOTE(JSON_EXTRACT(manifest_json, '$.config_schema.properties.arrive_list_request_body')) = '[REDACTED]';
