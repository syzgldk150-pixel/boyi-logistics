-- Migration 022: restore durable project intent after retired writers changed
-- PROJECT_FULL_AUTO into REQUIRE_EACH_RUN. The credential guard and the
-- first plugin upgrader were both retired; only their exact immutable event
-- shapes are eligible. A later administrator event is authoritative.

INSERT INTO automation_project_policy_events (
    automation_id, request_id, from_mode, to_mode,
    contract_hash, contract_snapshot_json, tool_contract_hash,
    plugin_contract_hash, project_generation, project_configuration_version,
    actor_id, actor_role, actor_display_name, reason, comment, correlation_id
)
SELECT
    policy.automation_id,
    CONCAT(
        CASE source.reason
            WHEN 'ACCOUNT_CREDENTIAL_CHANGED'
                THEN 'migration-022-credential-full-auto:'
            ELSE 'migration-022-plugin-full-auto:'
        END,
        policy.automation_id
    ),
    'REQUIRE_EACH_RUN', 'PROJECT_FULL_AUTO',
    NULL, NULL, NULL, NULL,
    policy.project_generation, policy.project_configuration_version,
    CASE source.reason
        WHEN 'ACCOUNT_CREDENTIAL_CHANGED'
            THEN 'system:migration:automation-credential-full-auto-v1'
        ELSE 'system:migration:automation-plugin-full-auto-v1'
    END,
    'system', 'Migration 022',
    CASE source.reason
        WHEN 'ACCOUNT_CREDENTIAL_CHANGED'
            THEN 'MIGRATION_022_CREDENTIAL_FULL_AUTO'
        ELSE 'MIGRATION_022_PLUGIN_FULL_AUTO'
    END,
    CASE source.reason
        WHEN 'ACCOUNT_CREDENTIAL_CHANGED'
            THEN 'Restored durable full-auto after legacy credential guard'
        ELSE 'Restored durable full-auto after legacy plugin downgrade'
    END,
    LOWER(CONCAT(
        SUBSTRING(SHA2(CONCAT('022:', source.reason, ':', policy.automation_id), 256), 1, 8), '-',
        SUBSTRING(SHA2(CONCAT('022:', source.reason, ':', policy.automation_id), 256), 9, 4), '-4',
        SUBSTRING(SHA2(CONCAT('022:', source.reason, ':', policy.automation_id), 256), 14, 3), '-a',
        SUBSTRING(SHA2(CONCAT('022:', source.reason, ':', policy.automation_id), 256), 18, 3), '-',
        SUBSTRING(SHA2(CONCAT('022:', source.reason, ':', policy.automation_id), 256), 21, 12)
    ))
FROM automation_project_policies AS policy
JOIN automation_project_policy_events AS source
  ON source.event_id = (
      SELECT MAX(candidate.event_id)
      FROM automation_project_policy_events AS candidate
      WHERE BINARY candidate.automation_id = BINARY policy.automation_id
  )
WHERE BINARY policy.mode=BINARY 'REQUIRE_EACH_RUN'
  AND policy.contract_hash IS NULL
  AND policy.contract_snapshot_json IS NULL
  AND policy.tool_contract_hash IS NULL
  AND policy.plugin_contract_hash IS NULL
  AND policy.project_generation=source.project_generation
  AND policy.project_configuration_version=source.project_configuration_version
  AND BINARY policy.approved_by_actor_id <=> BINARY source.actor_id
  AND BINARY policy.approved_by_actor_role <=> BINARY source.actor_role
  AND BINARY policy.approved_by_actor_display_name <=> BINARY source.actor_display_name
  AND policy.approved_at IS NOT NULL
  AND BINARY policy.comment <=> BINARY source.comment
  AND (
      (
          BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
          AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
          AND source.contract_hash IS NULL
          AND source.contract_snapshot_json IS NULL
          AND source.tool_contract_hash IS NULL
          AND source.plugin_contract_hash IS NULL
          AND BINARY source.actor_id=BINARY 'system:account-credential-change'
          AND BINARY source.actor_role=BINARY 'system'
          AND BINARY source.actor_display_name=BINARY 'Account credential safety guard'
          AND BINARY source.reason=BINARY 'ACCOUNT_CREDENTIAL_CHANGED'
          AND BINARY source.comment=BINARY 'Project full-auto authorization revoked before bound credentials changed'
          AND CHAR_LENGTH(source.request_id)=37+CHAR_LENGTH(policy.automation_id)
          AND BINARY SUBSTRING(source.request_id, 37, 1)=BINARY ':'
          AND BINARY RIGHT(source.request_id, CHAR_LENGTH(policy.automation_id))=BINARY policy.automation_id
          AND BINARY LEFT(source.request_id, 36) REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND BINARY source.correlation_id REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      )
      OR (
          BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
          AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
          AND source.contract_hash IS NULL
          AND source.contract_snapshot_json IS NULL
          AND source.tool_contract_hash IS NULL
          AND source.plugin_contract_hash IS NULL
          AND BINARY source.actor_role=BINARY 'super_admin'
          AND CHAR_LENGTH(source.actor_id)>0
          AND source.actor_display_name IS NULL
          AND BINARY source.reason=BINARY 'PLUGIN_VERSION_CHANGED'
          AND source.comment IS NULL
          AND BINARY source.correlation_id=BINARY source.request_id
          AND BINARY source.request_id REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND EXISTS (
              SELECT 1
              FROM automation_project_events AS plugin_event
              WHERE BINARY plugin_event.automation_id=BINARY source.automation_id
                AND BINARY plugin_event.request_id=BINARY source.request_id
                AND BINARY plugin_event.event_type=BINARY 'PLUGIN_UPGRADE_STAGED'
                AND BINARY plugin_event.from_state IN (BINARY 'INSTALLED', BINARY 'ENABLED', BINARY 'DISABLED')
                AND BINARY plugin_event.to_state=BINARY 'UPGRADING'
                AND BINARY plugin_event.actor_id=BINARY source.actor_id
                AND BINARY plugin_event.actor_role=BINARY source.actor_role
                AND JSON_TYPE(plugin_event.metadata_json)='OBJECT'
                AND JSON_LENGTH(plugin_event.metadata_json)=7
                AND JSON_CONTAINS_PATH(
                    plugin_event.metadata_json, 'all',
                    '$.request_payload_sha256', '$.from_version', '$.to_version',
                    '$.package_sha256', '$.target_generation', '$.previous_state',
                    '$.prepared_configuration_request_id'
                )
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.request_payload_sha256'
                ))='STRING'
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.request_payload_sha256'
                )) REGEXP BINARY '^[0-9a-f]{64}$'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.package_sha256'
                ))='STRING'
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.package_sha256'
                )) REGEXP BINARY '^[0-9a-f]{64}$'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                ))='STRING'
                AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                )))>0
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                ))='STRING'
                AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                )))>0
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                ))<>BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                ))
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.target_generation'
                ))='INTEGER'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.previous_state'
                ))='STRING'
                AND (
                    JSON_TYPE(JSON_EXTRACT(
                        plugin_event.metadata_json,
                        '$.prepared_configuration_request_id'
                    ))='NULL'
                    OR (
                        JSON_TYPE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        ))='STRING'
                        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        )))>0
                    )
                )
                AND JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.target_generation'
                ))=CAST(source.project_generation AS CHAR)
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.previous_state'
                ))=BINARY plugin_event.from_state
                AND BINARY plugin_event.metadata_sha256 REGEXP BINARY '^[0-9a-f]{64}$'
                AND BINARY plugin_event.metadata_sha256=BINARY SHA2(CONCAT(
                    '{"from_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.from_version'
                    ))),
                    ',"package_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.package_sha256'
                    ))),
                    ',"prepared_configuration_request_id":',
                    CASE JSON_TYPE(JSON_EXTRACT(
                        plugin_event.metadata_json,
                        '$.prepared_configuration_request_id'
                    ))
                        WHEN 'NULL' THEN 'null'
                        ELSE JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        )))
                    END,
                    ',"previous_state":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.previous_state'
                    ))),
                    ',"request_payload_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.request_payload_sha256'
                    ))),
                    ',"target_generation":', JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.target_generation'
                    )),
                    ',"to_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.to_version'
                    ))),
                    '}'
                ), 256)
          )
      )
  )
ON DUPLICATE KEY UPDATE request_id=VALUES(request_id);

UPDATE automation_project_policies AS policy
JOIN automation_project_policy_events AS restored
  ON BINARY restored.automation_id=BINARY policy.automation_id
 AND (
     BINARY restored.request_id=BINARY CONCAT(
         'migration-022-credential-full-auto:', policy.automation_id
     )
     OR BINARY restored.request_id=BINARY CONCAT(
         'migration-022-plugin-full-auto:', policy.automation_id
     )
 )
JOIN automation_project_policy_events AS source
  ON source.event_id = (
      SELECT MAX(predecessor.event_id)
      FROM automation_project_policy_events AS predecessor
      WHERE BINARY predecessor.automation_id=BINARY restored.automation_id
        AND predecessor.event_id<restored.event_id
  )
SET policy.mode='PROJECT_FULL_AUTO',
    policy.contract_hash=NULL,
    policy.contract_snapshot_json=NULL,
    policy.tool_contract_hash=NULL,
    policy.plugin_contract_hash=NULL,
    policy.project_generation=restored.project_generation,
    policy.project_configuration_version=restored.project_configuration_version,
    policy.approved_by_actor_id=restored.actor_id,
    policy.approved_by_actor_role=restored.actor_role,
    policy.approved_by_actor_display_name=restored.actor_display_name,
    policy.approved_at=NOW(6),
    policy.comment=restored.comment,
    policy.version=policy.version+1,
    policy.updated_at=NOW(6)
WHERE BINARY policy.mode=BINARY 'REQUIRE_EACH_RUN'
  AND NOT EXISTS (
      SELECT 1
      FROM automation_project_policy_events AS newer
      WHERE BINARY newer.automation_id=BINARY restored.automation_id
        AND newer.event_id>restored.event_id
  )
  AND policy.contract_hash IS NULL
  AND policy.contract_snapshot_json IS NULL
  AND policy.tool_contract_hash IS NULL
  AND policy.plugin_contract_hash IS NULL
  AND policy.project_generation=source.project_generation
  AND policy.project_generation=restored.project_generation
  AND policy.project_configuration_version=source.project_configuration_version
  AND policy.project_configuration_version=restored.project_configuration_version
  AND BINARY policy.approved_by_actor_id <=> BINARY source.actor_id
  AND BINARY policy.approved_by_actor_role <=> BINARY source.actor_role
  AND BINARY policy.approved_by_actor_display_name <=> BINARY source.actor_display_name
  AND policy.approved_at IS NOT NULL
  AND BINARY policy.comment <=> BINARY source.comment
  AND BINARY restored.from_mode=BINARY 'REQUIRE_EACH_RUN'
  AND BINARY restored.to_mode=BINARY 'PROJECT_FULL_AUTO'
  AND restored.contract_hash IS NULL
  AND restored.contract_snapshot_json IS NULL
  AND restored.tool_contract_hash IS NULL
  AND restored.plugin_contract_hash IS NULL
  AND BINARY restored.actor_role=BINARY 'system'
  AND BINARY restored.actor_display_name=BINARY 'Migration 022'
  AND (
      (
          BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
          AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
          AND BINARY source.reason=BINARY 'ACCOUNT_CREDENTIAL_CHANGED'
          AND BINARY source.actor_id=BINARY 'system:account-credential-change'
          AND BINARY source.actor_role=BINARY 'system'
          AND BINARY source.actor_display_name=BINARY 'Account credential safety guard'
          AND BINARY source.comment=BINARY 'Project full-auto authorization revoked before bound credentials changed'
          AND source.contract_hash IS NULL
          AND source.contract_snapshot_json IS NULL
          AND source.tool_contract_hash IS NULL
          AND source.plugin_contract_hash IS NULL
          AND CHAR_LENGTH(source.request_id)=37+CHAR_LENGTH(policy.automation_id)
          AND BINARY SUBSTRING(source.request_id, 37, 1)=BINARY ':'
          AND BINARY RIGHT(source.request_id, CHAR_LENGTH(policy.automation_id))=BINARY policy.automation_id
          AND BINARY LEFT(source.request_id, 36) REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND BINARY source.correlation_id REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND BINARY restored.reason=BINARY 'MIGRATION_022_CREDENTIAL_FULL_AUTO'
          AND BINARY restored.actor_id=BINARY 'system:migration:automation-credential-full-auto-v1'
          AND BINARY restored.comment=BINARY 'Restored durable full-auto after legacy credential guard'
      )
      OR (
          BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
          AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
          AND BINARY source.reason=BINARY 'PLUGIN_VERSION_CHANGED'
          AND BINARY source.actor_role=BINARY 'super_admin'
          AND CHAR_LENGTH(source.actor_id)>0
          AND source.actor_display_name IS NULL
          AND source.comment IS NULL
          AND BINARY source.correlation_id=BINARY source.request_id
          AND BINARY source.request_id REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND source.contract_hash IS NULL
          AND source.contract_snapshot_json IS NULL
          AND source.tool_contract_hash IS NULL
          AND source.plugin_contract_hash IS NULL
          AND BINARY restored.reason=BINARY 'MIGRATION_022_PLUGIN_FULL_AUTO'
          AND BINARY restored.actor_id=BINARY 'system:migration:automation-plugin-full-auto-v1'
          AND BINARY restored.comment=BINARY 'Restored durable full-auto after legacy plugin downgrade'
          AND EXISTS (
              SELECT 1
              FROM automation_project_events AS plugin_event
              WHERE BINARY plugin_event.automation_id=BINARY source.automation_id
                AND BINARY plugin_event.request_id=BINARY source.request_id
                AND BINARY plugin_event.event_type=BINARY 'PLUGIN_UPGRADE_STAGED'
                AND BINARY plugin_event.from_state IN (BINARY 'INSTALLED', BINARY 'ENABLED', BINARY 'DISABLED')
                AND BINARY plugin_event.to_state=BINARY 'UPGRADING'
                AND BINARY plugin_event.actor_id=BINARY source.actor_id
                AND BINARY plugin_event.actor_role=BINARY source.actor_role
                AND JSON_TYPE(plugin_event.metadata_json)='OBJECT'
                AND JSON_LENGTH(plugin_event.metadata_json)=7
                AND JSON_CONTAINS_PATH(
                    plugin_event.metadata_json, 'all',
                    '$.request_payload_sha256', '$.from_version', '$.to_version',
                    '$.package_sha256', '$.target_generation', '$.previous_state',
                    '$.prepared_configuration_request_id'
                )
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.request_payload_sha256'
                ))='STRING'
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.request_payload_sha256'
                )) REGEXP BINARY '^[0-9a-f]{64}$'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.package_sha256'
                ))='STRING'
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.package_sha256'
                )) REGEXP BINARY '^[0-9a-f]{64}$'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                ))='STRING'
                AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                )))>0
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                ))='STRING'
                AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                )))>0
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                ))<>BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                ))
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.target_generation'
                ))='INTEGER'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.previous_state'
                ))='STRING'
                AND (
                    JSON_TYPE(JSON_EXTRACT(
                        plugin_event.metadata_json,
                        '$.prepared_configuration_request_id'
                    ))='NULL'
                    OR (
                        JSON_TYPE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        ))='STRING'
                        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        )))>0
                    )
                )
                AND JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.target_generation'
                ))=CAST(source.project_generation AS CHAR)
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.previous_state'
                ))=BINARY plugin_event.from_state
                AND BINARY plugin_event.metadata_sha256 REGEXP BINARY '^[0-9a-f]{64}$'
                AND BINARY plugin_event.metadata_sha256=BINARY SHA2(CONCAT(
                    '{"from_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.from_version'
                    ))),
                    ',"package_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.package_sha256'
                    ))),
                    ',"prepared_configuration_request_id":',
                    CASE JSON_TYPE(JSON_EXTRACT(
                        plugin_event.metadata_json,
                        '$.prepared_configuration_request_id'
                    ))
                        WHEN 'NULL' THEN 'null'
                        ELSE JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        )))
                    END,
                    ',"previous_state":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.previous_state'
                    ))),
                    ',"request_payload_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.request_payload_sha256'
                    ))),
                    ',"target_generation":', JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.target_generation'
                    )),
                    ',"to_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.to_version'
                    ))),
                    '}'
                ), 256)
          )
      )
  );

UPDATE agent_runs AS run
JOIN approval_requests AS approval ON BINARY approval.run_id=BINARY run.run_id
JOIN agent_commands AS command ON BINARY command.command_id=BINARY run.command_id
JOIN automation_project_policies AS policy
  ON BINARY policy.automation_id=BINARY command.automation_id
JOIN automation_project_policy_events AS restored
  ON BINARY restored.automation_id=BINARY command.automation_id
 AND (
     BINARY restored.request_id=BINARY CONCAT(
         'migration-022-credential-full-auto:', command.automation_id
     )
     OR BINARY restored.request_id=BINARY CONCAT(
         'migration-022-plugin-full-auto:', command.automation_id
     )
 )
JOIN automation_project_policy_events AS source
  ON source.event_id = (
      SELECT MAX(predecessor.event_id)
      FROM automation_project_policy_events AS predecessor
      WHERE BINARY predecessor.automation_id=BINARY restored.automation_id
        AND predecessor.event_id<restored.event_id
  )
SET approval.status='INVALIDATED',
    approval.decided_at=NOW(6),
    run.next_attempt_at=NOW(6),
    run.worker_id=NULL,
    run.lease_expires_at=NULL,
    run.version=run.version+1
WHERE BINARY run.status=BINARY 'WAITING_APPROVAL'
  AND NOT EXISTS (
      SELECT 1
      FROM automation_project_policy_events AS newer
      WHERE BINARY newer.automation_id=BINARY restored.automation_id
        AND newer.event_id>restored.event_id
  )
  AND BINARY approval.status IN (BINARY 'PENDING', BINARY 'APPROVED')
  AND BINARY command.command_type=BINARY 'automation.project.invoke'
  AND command.automation_invocation_json IS NOT NULL
  AND BINARY policy.mode=BINARY 'PROJECT_FULL_AUTO'
  AND policy.contract_hash IS NULL
  AND policy.contract_snapshot_json IS NULL
  AND policy.tool_contract_hash IS NULL
  AND policy.plugin_contract_hash IS NULL
  AND policy.project_generation=restored.project_generation
  AND policy.project_configuration_version=restored.project_configuration_version
  AND BINARY policy.approved_by_actor_id <=> BINARY restored.actor_id
  AND BINARY policy.approved_by_actor_role <=> BINARY restored.actor_role
  AND BINARY policy.approved_by_actor_display_name <=> BINARY restored.actor_display_name
  AND policy.approved_at IS NOT NULL
  AND BINARY policy.comment <=> BINARY restored.comment
  AND BINARY restored.from_mode=BINARY 'REQUIRE_EACH_RUN'
  AND BINARY restored.to_mode=BINARY 'PROJECT_FULL_AUTO'
  AND restored.contract_hash IS NULL
  AND restored.contract_snapshot_json IS NULL
  AND restored.tool_contract_hash IS NULL
  AND restored.plugin_contract_hash IS NULL
  AND (
      (
          BINARY restored.reason=BINARY 'MIGRATION_022_CREDENTIAL_FULL_AUTO'
          AND BINARY restored.actor_id=BINARY 'system:migration:automation-credential-full-auto-v1'
          AND BINARY restored.actor_role=BINARY 'system'
          AND BINARY restored.actor_display_name=BINARY 'Migration 022'
          AND BINARY restored.comment=BINARY 'Restored durable full-auto after legacy credential guard'
          AND BINARY source.reason=BINARY 'ACCOUNT_CREDENTIAL_CHANGED'
          AND BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
          AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
          AND BINARY source.actor_id=BINARY 'system:account-credential-change'
          AND BINARY source.actor_role=BINARY 'system'
          AND BINARY source.actor_display_name=BINARY 'Account credential safety guard'
          AND BINARY source.comment=BINARY 'Project full-auto authorization revoked before bound credentials changed'
          AND source.contract_hash IS NULL
          AND source.contract_snapshot_json IS NULL
          AND source.tool_contract_hash IS NULL
          AND source.plugin_contract_hash IS NULL
          AND CHAR_LENGTH(source.request_id)=37+CHAR_LENGTH(command.automation_id)
          AND BINARY SUBSTRING(source.request_id, 37, 1)=BINARY ':'
          AND BINARY RIGHT(source.request_id, CHAR_LENGTH(command.automation_id))=BINARY command.automation_id
          AND BINARY LEFT(source.request_id, 36) REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND BINARY source.correlation_id REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      )
      OR (
          BINARY restored.reason=BINARY 'MIGRATION_022_PLUGIN_FULL_AUTO'
          AND BINARY restored.actor_id=BINARY 'system:migration:automation-plugin-full-auto-v1'
          AND BINARY restored.actor_role=BINARY 'system'
          AND BINARY restored.actor_display_name=BINARY 'Migration 022'
          AND BINARY restored.comment=BINARY 'Restored durable full-auto after legacy plugin downgrade'
          AND BINARY source.reason=BINARY 'PLUGIN_VERSION_CHANGED'
          AND BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
          AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
          AND BINARY source.actor_role=BINARY 'super_admin'
          AND CHAR_LENGTH(source.actor_id)>0
          AND source.actor_display_name IS NULL
          AND source.comment IS NULL
          AND BINARY source.correlation_id=BINARY source.request_id
          AND BINARY source.request_id REGEXP BINARY '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          AND source.contract_hash IS NULL
          AND source.contract_snapshot_json IS NULL
          AND source.tool_contract_hash IS NULL
          AND source.plugin_contract_hash IS NULL
          AND EXISTS (
              SELECT 1
              FROM automation_project_events AS plugin_event
              WHERE BINARY plugin_event.automation_id=BINARY source.automation_id
                AND BINARY plugin_event.request_id=BINARY source.request_id
                AND BINARY plugin_event.event_type=BINARY 'PLUGIN_UPGRADE_STAGED'
                AND BINARY plugin_event.from_state IN (BINARY 'INSTALLED', BINARY 'ENABLED', BINARY 'DISABLED')
                AND BINARY plugin_event.to_state=BINARY 'UPGRADING'
                AND BINARY plugin_event.actor_id=BINARY source.actor_id
                AND BINARY plugin_event.actor_role=BINARY source.actor_role
                AND JSON_TYPE(plugin_event.metadata_json)='OBJECT'
                AND JSON_LENGTH(plugin_event.metadata_json)=7
                AND JSON_CONTAINS_PATH(
                    plugin_event.metadata_json, 'all',
                    '$.request_payload_sha256', '$.from_version', '$.to_version',
                    '$.package_sha256', '$.target_generation', '$.previous_state',
                    '$.prepared_configuration_request_id'
                )
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.request_payload_sha256'
                ))='STRING'
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.request_payload_sha256'
                )) REGEXP BINARY '^[0-9a-f]{64}$'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.package_sha256'
                ))='STRING'
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.package_sha256'
                )) REGEXP BINARY '^[0-9a-f]{64}$'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                ))='STRING'
                AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                )))>0
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                ))='STRING'
                AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                )))>0
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.from_version'
                ))<>BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.to_version'
                ))
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.target_generation'
                ))='INTEGER'
                AND JSON_TYPE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.previous_state'
                ))='STRING'
                AND (
                    JSON_TYPE(JSON_EXTRACT(
                        plugin_event.metadata_json,
                        '$.prepared_configuration_request_id'
                    ))='NULL'
                    OR (
                        JSON_TYPE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        ))='STRING'
                        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        )))>0
                    )
                )
                AND JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.target_generation'
                ))=CAST(source.project_generation AS CHAR)
                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                    plugin_event.metadata_json, '$.previous_state'
                ))=BINARY plugin_event.from_state
                AND BINARY plugin_event.metadata_sha256 REGEXP BINARY '^[0-9a-f]{64}$'
                AND BINARY plugin_event.metadata_sha256=BINARY SHA2(CONCAT(
                    '{"from_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.from_version'
                    ))),
                    ',"package_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.package_sha256'
                    ))),
                    ',"prepared_configuration_request_id":',
                    CASE JSON_TYPE(JSON_EXTRACT(
                        plugin_event.metadata_json,
                        '$.prepared_configuration_request_id'
                    ))
                        WHEN 'NULL' THEN 'null'
                        ELSE JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                            plugin_event.metadata_json,
                            '$.prepared_configuration_request_id'
                        )))
                    END,
                    ',"previous_state":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.previous_state'
                    ))),
                    ',"request_payload_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.request_payload_sha256'
                    ))),
                    ',"target_generation":', JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.target_generation'
                    )),
                    ',"to_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(
                        plugin_event.metadata_json, '$.to_version'
                    ))),
                    '}'
                ), 256)
          )
      )
  );
