-- Migration 024: restore durable full-auto intent only for the original
-- plugin upgrader's historical six-key staged metadata.  Migration 022
-- intentionally handles the later seven-key writer; this migration must not
-- widen that contract or accept a mixed/extra-key payload.
--
-- The source policy event must still be the project's newest event when the
-- restoration event is inserted.  The policy update then uses the exact
-- predecessor as a CAS guard, so a later administrator choice always wins.

INSERT INTO automation_project_policy_events (
    automation_id, request_id, from_mode, to_mode,
    contract_hash, contract_snapshot_json, tool_contract_hash,
    plugin_contract_hash, project_generation, project_configuration_version,
    actor_id, actor_role, actor_display_name, reason, comment, correlation_id
)
SELECT
    policy.automation_id,
    CONCAT('migration-024-plugin-full-auto:', policy.automation_id),
    'REQUIRE_EACH_RUN', 'PROJECT_FULL_AUTO',
    NULL, NULL, NULL, NULL,
    policy.project_generation, policy.project_configuration_version,
    'system:migration:automation-plugin-full-auto-v2',
    'system', 'Migration 024',
    'MIGRATION_024_PLUGIN_FULL_AUTO',
    'Restored durable full-auto after original plugin downgrade',
    LOWER(CONCAT(
        SUBSTRING(SHA2(CONCAT('024:PLUGIN_VERSION_CHANGED:', policy.automation_id), 256), 1, 8), '-',
        SUBSTRING(SHA2(CONCAT('024:PLUGIN_VERSION_CHANGED:', policy.automation_id), 256), 9, 4), '-4',
        SUBSTRING(SHA2(CONCAT('024:PLUGIN_VERSION_CHANGED:', policy.automation_id), 256), 14, 3), '-a',
        SUBSTRING(SHA2(CONCAT('024:PLUGIN_VERSION_CHANGED:', policy.automation_id), 256), 18, 3), '-',
        SUBSTRING(SHA2(CONCAT('024:PLUGIN_VERSION_CHANGED:', policy.automation_id), 256), 21, 12)
    ))
FROM automation_project_policies AS policy
JOIN automation_project_policy_events AS source
  ON source.event_id=(
      SELECT MAX(candidate.event_id)
      FROM automation_project_policy_events AS candidate
      WHERE BINARY candidate.automation_id=BINARY policy.automation_id
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
  AND BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
  AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
  AND source.contract_hash IS NULL
  AND source.contract_snapshot_json IS NULL
  AND source.tool_contract_hash IS NULL
  AND source.plugin_contract_hash IS NULL
  AND BINARY source.reason=BINARY 'PLUGIN_VERSION_CHANGED'
  AND BINARY source.actor_role=BINARY 'super_admin'
  AND CHAR_LENGTH(source.actor_id)>0
  AND source.actor_display_name IS NULL
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
        AND JSON_LENGTH(plugin_event.metadata_json)=6
        AND JSON_CONTAINS_PATH(
            plugin_event.metadata_json, 'all',
            '$.request_payload_sha256', '$.from_version', '$.to_version',
            '$.package_sha256', '$.target_generation', '$.previous_state'
        )
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256'))='STRING'
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256')) REGEXP BINARY '^[0-9a-f]{64}$'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256'))='STRING'
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256')) REGEXP BINARY '^[0-9a-f]{64}$'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))='STRING'
        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version')))>0
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))='STRING'
        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version')))>0
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))<>BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation'))='INTEGER'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))='STRING'
        AND JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation'))=CAST(source.project_generation AS CHAR)
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))=BINARY plugin_event.from_state
        AND BINARY plugin_event.metadata_sha256 REGEXP BINARY '^[0-9a-f]{64}$'
        AND BINARY plugin_event.metadata_sha256=BINARY SHA2(CONCAT(
            '{"from_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))),
            ',"package_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256'))),
            ',"previous_state":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))),
            ',"request_payload_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256'))),
            ',"target_generation":', JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation')),
            ',"to_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))),
            '}'
        ), 256)
  )
ON DUPLICATE KEY UPDATE request_id=VALUES(request_id);

UPDATE automation_project_policies AS policy
JOIN automation_project_policy_events AS restored
  ON BINARY restored.automation_id=BINARY policy.automation_id
 AND BINARY restored.request_id=BINARY CONCAT('migration-024-plugin-full-auto:', policy.automation_id)
JOIN automation_project_policy_events AS source
  ON source.event_id=(
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
  AND BINARY restored.actor_id=BINARY 'system:migration:automation-plugin-full-auto-v2'
  AND BINARY restored.actor_role=BINARY 'system'
  AND BINARY restored.actor_display_name=BINARY 'Migration 024'
  AND BINARY restored.reason=BINARY 'MIGRATION_024_PLUGIN_FULL_AUTO'
  AND BINARY restored.comment=BINARY 'Restored durable full-auto after original plugin downgrade'
  AND BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
  AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
  AND source.contract_hash IS NULL
  AND source.contract_snapshot_json IS NULL
  AND source.tool_contract_hash IS NULL
  AND source.plugin_contract_hash IS NULL
  AND BINARY source.reason=BINARY 'PLUGIN_VERSION_CHANGED'
  AND BINARY source.actor_role=BINARY 'super_admin'
  AND CHAR_LENGTH(source.actor_id)>0
  AND source.actor_display_name IS NULL
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
        AND JSON_LENGTH(plugin_event.metadata_json)=6
        AND JSON_CONTAINS_PATH(plugin_event.metadata_json, 'all', '$.request_payload_sha256', '$.from_version', '$.to_version', '$.package_sha256', '$.target_generation', '$.previous_state')
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256'))='STRING'
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256')) REGEXP BINARY '^[0-9a-f]{64}$'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256'))='STRING'
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256')) REGEXP BINARY '^[0-9a-f]{64}$'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))='STRING'
        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version')))>0
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))='STRING'
        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version')))>0
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))<>BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation'))='INTEGER'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))='STRING'
        AND JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation'))=CAST(source.project_generation AS CHAR)
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))=BINARY plugin_event.from_state
        AND BINARY plugin_event.metadata_sha256 REGEXP BINARY '^[0-9a-f]{64}$'
        AND BINARY plugin_event.metadata_sha256=BINARY SHA2(CONCAT(
            '{"from_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))),
            ',"package_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256'))),
            ',"previous_state":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))),
            ',"request_payload_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256'))),
            ',"target_generation":', JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation')),
            ',"to_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))),
            '}'
        ), 256)
  );

UPDATE agent_runs AS run
JOIN approval_requests AS approval ON BINARY approval.run_id=BINARY run.run_id
JOIN agent_commands AS command ON BINARY command.command_id=BINARY run.command_id
JOIN automation_project_policies AS policy
  ON BINARY policy.automation_id=BINARY command.automation_id
JOIN automation_project_policy_events AS restored
  ON BINARY restored.automation_id=BINARY command.automation_id
 AND BINARY restored.request_id=BINARY CONCAT('migration-024-plugin-full-auto:', command.automation_id)
JOIN automation_project_policy_events AS source
  ON source.event_id=(
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
  AND BINARY restored.actor_id=BINARY 'system:migration:automation-plugin-full-auto-v2'
  AND BINARY restored.actor_role=BINARY 'system'
  AND BINARY restored.actor_display_name=BINARY 'Migration 024'
  AND BINARY restored.reason=BINARY 'MIGRATION_024_PLUGIN_FULL_AUTO'
  AND BINARY restored.comment=BINARY 'Restored durable full-auto after original plugin downgrade'
  AND NOT EXISTS (
      SELECT 1
      FROM automation_project_policy_events AS newer
      WHERE BINARY newer.automation_id=BINARY restored.automation_id
        AND newer.event_id>restored.event_id
  )
  AND BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'
  AND BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'
  AND source.contract_hash IS NULL
  AND source.contract_snapshot_json IS NULL
  AND source.tool_contract_hash IS NULL
  AND source.plugin_contract_hash IS NULL
  AND BINARY source.reason=BINARY 'PLUGIN_VERSION_CHANGED'
  AND BINARY source.actor_role=BINARY 'super_admin'
  AND CHAR_LENGTH(source.actor_id)>0
  AND source.actor_display_name IS NULL
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
        AND JSON_LENGTH(plugin_event.metadata_json)=6
        AND JSON_CONTAINS_PATH(plugin_event.metadata_json, 'all', '$.request_payload_sha256', '$.from_version', '$.to_version', '$.package_sha256', '$.target_generation', '$.previous_state')
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256'))='STRING'
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256')) REGEXP BINARY '^[0-9a-f]{64}$'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256'))='STRING'
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256')) REGEXP BINARY '^[0-9a-f]{64}$'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))='STRING'
        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version')))>0
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))='STRING'
        AND CHAR_LENGTH(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version')))>0
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))<>BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation'))='INTEGER'
        AND JSON_TYPE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))='STRING'
        AND JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation'))=CAST(source.project_generation AS CHAR)
        AND BINARY JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))=BINARY plugin_event.from_state
        AND BINARY plugin_event.metadata_sha256 REGEXP BINARY '^[0-9a-f]{64}$'
        AND BINARY plugin_event.metadata_sha256=BINARY SHA2(CONCAT(
            '{"from_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.from_version'))),
            ',"package_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.package_sha256'))),
            ',"previous_state":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.previous_state'))),
            ',"request_payload_sha256":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.request_payload_sha256'))),
            ',"target_generation":', JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.target_generation')),
            ',"to_version":', JSON_QUOTE(JSON_UNQUOTE(JSON_EXTRACT(plugin_event.metadata_json, '$.to_version'))),
            '}'
        ), 256)
  );
