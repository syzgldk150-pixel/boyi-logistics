"""Restartable MySQL 018 restore/status helpers.

Every dependency is supplied by ``run_migrations`` so test patches and
the deployment runner keep one authoritative runtime boundary.
"""

from __future__ import annotations


def _automation_project_authorization_artifacts(runtime, cursor) -> set[str]:
    artifacts = {table_name for table_name in (runtime["AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE"], runtime["AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE"], runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"], *runtime["AUTOMATION_PROJECT_AUTHORIZATION_TABLES_REVERSE"]) if runtime["_table_exists"](cursor, table_name)}
    for table_name, column_name in (('scheduled_tasks', 'automation_id'), ('scheduled_tasks', 'automation_generation'), ('workflow_resources', 'configuration_version'), ('workflow_resources', 'config_sha256'), ('agent_commands', 'automation_id'), ('agent_commands', 'automation_generation'), ('agent_commands', 'automation_invocation_json')):
        if runtime["_column_exists"](cursor, table_name, column_name):
            artifacts.add(f'{table_name}.{column_name}')
    return artifacts


def _validate_automation_project_authorization_restore(runtime, cursor) -> bool:
    """Lock and validate that 018 contains no post-bootstrap user state.

    Returns whether the pre-018 scheduled-task backup is complete and should be
    restored. A failed/partial migration that never completed capture is safe to
    clean only while the production tables still have their pre-018 shape.
    """
    schedule_column_exists = runtime["_column_exists"](cursor, 'scheduled_tasks', 'automation_id')
    backup_exists = runtime["_table_exists"](cursor, runtime["AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE"])
    capture_exists = runtime["_table_exists"](cursor, runtime["AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE"])
    captured = False
    capture_started = False
    if capture_exists:
        cursor.execute(f'\n            SELECT capture_state, source_row_count\n            FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE"]}\n            WHERE marker_id = 1\n            FOR UPDATE\n            ')
        capture = cursor.fetchone()
        capture_started = capture is not None
        captured = bool(capture and capture.get('capture_state') == 'CAPTURED')
        if capture and (not captured) and schedule_column_exists:
            raise RuntimeError('018 restore cannot prove the pre-migration scheduler capture')
        if captured and (not backup_exists):
            raise RuntimeError('018 capture marker exists without its backup table')
        if captured:
            cursor.execute(f'SELECT COUNT(*) AS row_count FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE"]}')
            backup_count = int((cursor.fetchone() or {}).get('row_count') or 0)
            if backup_count != int(capture.get('source_row_count') or 0):
                raise RuntimeError('018 scheduler backup count no longer matches marker')
    elif schedule_column_exists:
        raise RuntimeError('018 scheduler identity column exists without a complete capture marker')
    resource_backup_exists = runtime["_table_exists"](
        cursor,
        runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"],
    )
    resource_version_exists = runtime["_column_exists"](
        cursor, 'workflow_resources', 'configuration_version'
    )
    resource_hash_exists = runtime["_column_exists"](
        cursor, 'workflow_resources', 'config_sha256'
    )
    if (not resource_backup_exists) and (
        capture_started
        or schedule_column_exists
        or resource_version_exists
        or resource_hash_exists
    ):
        raise RuntimeError('018 reviewed-resource backup is missing after migration start')
    if resource_backup_exists:
        cursor.execute(
            f'''\n            SELECT\n                COUNT(*) AS row_count,\n                SUM(migration_config_sha256 IS NOT NULL) AS captured_count,\n                SUM(\n                    BINARY resource_key =\n                    BINARY 'phase7.pending_arrivals_sheet'\n                ) AS legacy_pending_count,\n                SUM(\n                    BINARY resource_key =\n                    BINARY 'phase7.pending_arrivals_sheet'\n                    AND existed_before = TRUE\n                ) AS legacy_pending_existed_count\n            FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]}\n            FOR UPDATE\n            '''
        )
        resource_capture = cursor.fetchone() or {}
        resource_backup_count = int(resource_capture.get('row_count') or 0)
        resource_hash_count = int(resource_capture.get('captured_count') or 0)
        legacy_pending_count = int(
            resource_capture.get('legacy_pending_count') or 0
        )
        legacy_pending_existed_count = int(
            resource_capture.get('legacy_pending_existed_count') or 0
        )
        valid_resource_backup_layout = (
            resource_backup_count == 0
            or (
                resource_backup_count == 14
                and legacy_pending_count == 0
            )
            or (
                resource_backup_count == 15
                and legacy_pending_count == 1
                and legacy_pending_existed_count == 1
            )
        )
        if not valid_resource_backup_layout:
            raise RuntimeError('018 reviewed-resource backup is incomplete')
        if resource_hash_count not in {0, resource_backup_count}:
            raise RuntimeError('018 reviewed-resource capture is incomplete')
        if resource_backup_count == 0 and (
            capture_started
            or schedule_column_exists
            or resource_version_exists
            or resource_hash_exists
        ):
            raise RuntimeError('018 reviewed-resource backup is empty after migration start')
        if resource_backup_count:
            if resource_hash_count and not (
                resource_version_exists and resource_hash_exists
            ):
                raise RuntimeError(
                    '018 reviewed-resource hashes exist without their schema'
                )
            if resource_hash_count:
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS changed_count
                    FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]} AS backup
                    LEFT JOIN workflow_resources AS resource
                      ON BINARY resource.resource_key = BINARY backup.resource_key
                    WHERE resource.resource_key IS NULL
                       OR resource.configuration_version <> 1
                       OR BINARY resource.config_sha256 <>
                          BINARY backup.migration_config_sha256
                       OR BINARY resource.config_sha256 <> BINARY SHA2(
                            CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                            256
                       )
                       OR (
                            backup.existed_before = TRUE
                            AND NOT (
                                BINARY resource.source <=>
                                BINARY backup.source
                            )
                       )
                       OR (
                            backup.existed_before = FALSE
                            AND BINARY resource.source <>
                                BINARY 'migration-018-reviewed-builtin'
                       )
                    FOR UPDATE
                    """
                )
                if int((cursor.fetchone() or {}).get('changed_count') or 0):
                    raise RuntimeError(
                        '018 restore refuses changed reviewed resources'
                    )
            elif runtime["_table_exists"](
                cursor,
                runtime[
                    "AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_RESOURCE_MAP_TABLE"
                ],
            ):
                version_guard = (
                    'AND resource.configuration_version = 1'
                    if resource_version_exists
                    else ''
                )
                hash_guard = (
                    '''\n                    AND BINARY resource.config_sha256 = BINARY SHA2(\n                        CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),\n                        256\n                    )'''
                    if resource_hash_exists
                    else ''
                )
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS changed_count
                    FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]} AS backup
                    INNER JOIN {runtime["AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_RESOURCE_MAP_TABLE"]} AS reviewed
                      ON BINARY reviewed.resource_key = BINARY backup.resource_key
                    LEFT JOIN workflow_resources AS resource
                      ON BINARY resource.resource_key = BINARY backup.resource_key
                    WHERE NOT (
                        (
                            backup.existed_before = TRUE
                            AND resource.resource_key IS NOT NULL
                            AND BINARY resource.source <=> BINARY backup.source
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
                                        reviewed.expected_kind
                                    ) AS CHAR CHARACTER SET utf8mb4),
                                    256
                                )
                            )
                            {version_guard}
                            {hash_guard}
                        )
                        OR (
                            backup.existed_before = FALSE
                            AND (
                                resource.resource_key IS NULL
                                OR (
                                    BINARY SHA2(
                                        CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                                        256
                                    ) = BINARY SHA2(
                                        CAST(reviewed.default_config_json AS CHAR CHARACTER SET utf8mb4),
                                        256
                                    )
                                    AND BINARY resource.source =
                                        BINARY 'migration-018-reviewed-builtin'
                                    {version_guard}
                                    {hash_guard}
                                )
                            )
                        )
                    )
                    FOR UPDATE
                    """
                )
                if int((cursor.fetchone() or {}).get('changed_count') or 0):
                    raise RuntimeError(
                        '018 restore refuses dirty partial reviewed resources'
                    )
                if legacy_pending_count:
                    cursor.execute(
                        f"""
                        SELECT COUNT(*) AS changed_count
                        FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]} AS backup
                        LEFT JOIN workflow_resources AS resource
                          ON BINARY resource.resource_key = BINARY backup.resource_key
                        WHERE BINARY backup.resource_key =
                              BINARY 'phase7.pending_arrivals_sheet'
                          AND NOT (
                              backup.existed_before = TRUE
                              AND resource.resource_key IS NOT NULL
                              AND BINARY resource.source <=> BINARY backup.source
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
                              {version_guard}
                              {hash_guard}
                          )
                        FOR UPDATE
                        """
                    )
                    if int(
                        (cursor.fetchone() or {}).get('changed_count') or 0
                    ):
                        raise RuntimeError(
                            '018 restore refuses dirty legacy pending resource'
                        )
            else:
                cursor.execute(
                    f'''\n                    SELECT COUNT(*) AS changed_count\n                    FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]} AS backup\n                    LEFT JOIN workflow_resources AS resource\n                      ON BINARY resource.resource_key = BINARY backup.resource_key\n                    WHERE NOT (\n                        (\n                            backup.existed_before = TRUE\n                            AND resource.resource_key IS NOT NULL\n                            AND BINARY resource.source <=> BINARY backup.source\n                            AND BINARY CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4) =\n                                BINARY CAST(backup.config_json AS CHAR CHARACTER SET utf8mb4)\n                        )\n                        OR (\n                            backup.existed_before = FALSE\n                            AND resource.resource_key IS NULL\n                        )\n                    )\n                    FOR UPDATE\n                    '''
                )
                if int((cursor.fetchone() or {}).get('changed_count') or 0):
                    raise RuntimeError(
                        '018 restore cannot prove partial reviewed-resource ownership'
                    )
    if captured:
        cursor.execute(f'\n            SELECT COUNT(*) AS unexpected_count\n            FROM scheduled_tasks AS task\n            LEFT JOIN {runtime["AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE"]} AS backup\n              ON BINARY backup.id = BINARY task.id\n            WHERE backup.id IS NULL\n            ')
        if int((cursor.fetchone() or {}).get('unexpected_count') or 0):
            raise RuntimeError('018 restore refuses to remove schedules created after migration capture')
    if runtime["_table_exists"](cursor, 'automation_worker_jobs'):
        cursor.execute("\n            SELECT COUNT(*) AS active_count\n            FROM automation_worker_jobs\n            WHERE status IN ('CLAIMED', 'RUNNING', 'OUTCOME_UNKNOWN', 'BLOCKED_DATA')\n            FOR UPDATE\n            ")
        if int((cursor.fetchone() or {}).get('active_count') or 0):
            raise RuntimeError('018 restore blocked by active or unresolved worker jobs')
    if runtime["_column_exists"](cursor, 'agent_commands', 'automation_id'):
        cursor.execute('\n            SELECT command_id, status\n            FROM agent_commands\n            WHERE automation_id IS NOT NULL\n            ORDER BY command_id\n            FOR UPDATE\n            ')
        project_commands = cursor.fetchall() or ()
        project_command_ids = {str(row.get('command_id') or '') for row in project_commands if row.get('command_id')}
        if any((str(row.get('status') or '') == 'RECEIVED' for row in project_commands)):
            raise RuntimeError('018 restore blocked by a received project command')
        if project_command_ids:
            placeholders = ', '.join(['%s'] * len(project_command_ids))
            command_id_params = tuple(sorted(project_command_ids))
            cursor.execute(f'\n                SELECT run_id, command_id, status\n                FROM agent_runs\n                WHERE command_id IN ({placeholders})\n                ORDER BY run_id\n                FOR UPDATE\n                ', command_id_params)
            project_runs = cursor.fetchall() or ()
            if any((str(row.get('status') or '') not in {'COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED'} for row in project_runs)):
                raise RuntimeError('018 restore blocked by a non-terminal project run')
            commands_with_runs = {str(row.get('command_id') or '') for row in project_runs if row.get('command_id')}
            if any((str(row.get('status') or '') == 'ACCEPTED' and str(row.get('command_id') or '') not in commands_with_runs for row in project_commands)):
                raise RuntimeError('018 restore blocked by an accepted project command without a run')
            cursor.execute(f'\n                SELECT step.step_id, step.status\n                FROM agent_run_steps AS step\n                INNER JOIN agent_runs AS run ON run.run_id = step.run_id\n                WHERE run.command_id IN ({placeholders})\n                ORDER BY step.step_id\n                FOR UPDATE\n                ', command_id_params)
            project_steps = cursor.fetchall() or ()
            if any((str(row.get('status') or '') not in {'COMPLETED', 'SKIPPED', 'FAILED_TERMINAL', 'CANCELLED'} for row in project_steps)):
                raise RuntimeError('018 restore blocked by a non-terminal project step')
            cursor.execute(f'\n                SELECT approval.approval_id, approval.status\n                FROM approval_requests AS approval\n                INNER JOIN agent_runs AS run ON run.run_id = approval.run_id\n                WHERE run.command_id IN ({placeholders})\n                ORDER BY approval.approval_id\n                FOR UPDATE\n                ', command_id_params)
            if any((str(row.get('status') or '') == 'PENDING' for row in cursor.fetchall() or ())):
                raise RuntimeError('018 restore blocked by a pending project approval')
    if runtime["_table_exists"](cursor, 'automation_project_generation_leases'):
        cursor.execute("\n            SELECT lease_id\n            FROM automation_project_generation_leases\n            WHERE outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')\n            ORDER BY lease_id\n            FOR UPDATE\n            ")
        if cursor.fetchone() is not None:
            raise RuntimeError('018 restore blocked by an active or unresolved generation lease')
    if runtime["_table_exists"](cursor, 'automation_plugin_purge_journal'):
        cursor.execute("\n            SELECT COUNT(*) AS active_count\n            FROM automation_plugin_purge_journal\n            WHERE phase <> 'COMMITTED'\n            FOR UPDATE\n            ")
        if int((cursor.fetchone() or {}).get('active_count') or 0):
            raise RuntimeError('018 restore blocked by an incomplete plugin purge')
    if runtime["_column_exists"](cursor, 'workflow_resources', 'configuration_version'):
        cursor.execute('\n            SELECT COUNT(*) AS changed_count FROM workflow_resources\n            WHERE configuration_version <> 1 FOR UPDATE\n            ')
        if int((cursor.fetchone() or {}).get('changed_count') or 0):
            raise RuntimeError('018 restore refuses to discard post-migration resource revisions')
    if runtime["_table_exists"](cursor, 'automation_projects'):
        cursor.execute('\n            SELECT COUNT(*) AS user_project_count\n            FROM automation_projects\n            WHERE migration_authority = FALSE\n            FOR UPDATE\n            ')
        if int((cursor.fetchone() or {}).get('user_project_count') or 0):
            raise RuntimeError('018 restore refuses to delete user-installed projects')
    if runtime["_table_exists"](cursor, 'automation_project_approval_batches'):
        cursor.execute('SELECT COUNT(*) AS decision_count FROM automation_project_approval_batches FOR UPDATE')
        if int((cursor.fetchone() or {}).get('decision_count') or 0):
            raise RuntimeError('018 restore refuses to delete project approval decisions')
    for table_name in ('automation_project_events', 'automation_project_policy_events', 'automation_plugin_package_events', 'automation_worker_pairing_events'):
        if not runtime["_table_exists"](cursor, table_name):
            continue
        cursor.execute(f'SELECT COUNT(*) AS user_event_count FROM {table_name} WHERE actor_role <> %s FOR UPDATE', (runtime["CONTROL_PLANE_MIGRATION_ACTOR_ROLE"],))
        if int((cursor.fetchone() or {}).get('user_event_count') or 0):
            raise RuntimeError('018 restore refuses to delete non-migration audit events')
    return captured


def _restore_automation_project_resources(runtime, cursor) -> None:
    """Restore every reviewed resource row captured by migration 018."""
    backup_table = runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]
    if not runtime["_table_exists"](cursor, backup_table):
        return
    versioned_resources = runtime["_column_exists"](
        cursor, 'workflow_resources', 'configuration_version'
    ) and runtime["_column_exists"](
        cursor, 'workflow_resources', 'config_sha256'
    )
    version_restore_sql = (
        """resource.config_sha256 = SHA2(
                CAST(backup.config_json AS CHAR CHARACTER SET utf8mb4),
                256
            ),
            resource.configuration_version = 1,"""
        if versioned_resources
        else ''
    )
    cursor.execute(
        f"""
        UPDATE workflow_resources AS resource
        INNER JOIN {backup_table} AS backup
          ON BINARY backup.resource_key = BINARY resource.resource_key
        SET
            resource.config_json = backup.config_json,
            resource.source = backup.source,
            {version_restore_sql}
            resource.updated_at = backup.updated_at,
            resource.created_at = backup.created_at
        WHERE backup.existed_before = TRUE
        """
    )
    cursor.execute(
        f"""
        DELETE resource
        FROM workflow_resources AS resource
        INNER JOIN {backup_table} AS backup
          ON BINARY backup.resource_key = BINARY resource.resource_key
        WHERE backup.existed_before = FALSE
        """
    )
    # The first following DDL auto-commit persists this marker transition with
    # the restored rows.  A crashed restore can then prove the exact pre-018
    # state and continue even if the reviewed map was already dropped.
    cursor.execute(
        f"""
        UPDATE {backup_table}
        SET migration_config_sha256 = NULL
        """
    )


def restore_automation_project_authorization(runtime) -> int:
    """Remove only migration-owned 018 state and restore the pre-018 schema.

    MySQL DDL auto-commits, so every step is conditional and the operation is
    deliberately restartable after interruption. Safety checks run before the
    first destructive statement.
    """
    connection = runtime["_connect"]()
    transaction_started = False
    try:
        with connection.cursor() as cursor:
            runtime["_require_mysql8"](cursor)
            applied = False
            if runtime["_migration_table_exists"](cursor):
                cursor.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (runtime["AUTOMATION_PROJECT_AUTHORIZATION_VERSION"],))
                applied = cursor.fetchone() is not None
            artifacts = runtime["_automation_project_authorization_artifacts"](cursor)
            if not applied and (not artifacts):
                print('automation_project_authorization_restore=skipped reason=clean')
                return 0
            connection.begin()
            transaction_started = True
            restore_scheduler = runtime["_validate_automation_project_authorization_restore"](cursor)
            _restore_automation_project_resources(runtime, cursor)
            for table_name in runtime["AUTOMATION_PROJECT_AUTHORIZATION_TABLES_REVERSE"]:
                if runtime["_table_exists"](cursor, table_name):
                    cursor.execute(f'DROP TABLE {table_name}')
            transaction_started = False
            if runtime["_index_exists"](cursor, 'agent_commands', runtime["AUTOMATION_PROJECT_AUTHORIZATION_AGENT_COMMAND_INDEX"]):
                cursor.execute(f'ALTER TABLE agent_commands DROP INDEX {runtime["AUTOMATION_PROJECT_AUTHORIZATION_AGENT_COMMAND_INDEX"]}')
            for column_name in ('automation_invocation_json', 'automation_generation', 'automation_id'):
                if runtime["_column_exists"](cursor, 'agent_commands', column_name):
                    cursor.execute(f'ALTER TABLE agent_commands DROP COLUMN {column_name}')
            if runtime["_index_exists"](cursor, 'scheduled_tasks', runtime["AUTOMATION_PROJECT_AUTHORIZATION_SCHEDULE_INDEX"]):
                cursor.execute(f'ALTER TABLE scheduled_tasks DROP INDEX {runtime["AUTOMATION_PROJECT_AUTHORIZATION_SCHEDULE_INDEX"]}')
            for column_name in ('automation_generation', 'automation_id'):
                if runtime["_column_exists"](cursor, 'scheduled_tasks', column_name):
                    cursor.execute(f'ALTER TABLE scheduled_tasks DROP COLUMN {column_name}')
            for column_name in ('config_sha256', 'configuration_version'):
                if runtime["_column_exists"](cursor, 'workflow_resources', column_name):
                    cursor.execute(f'ALTER TABLE workflow_resources DROP COLUMN {column_name}')
            if restore_scheduler:
                cursor.execute(f'\n                    INSERT INTO scheduled_tasks (\n                        id, name, tool_name, tool_params, cron_expression, enabled,\n                        last_run, last_status, last_duration_ms, last_message,\n                        created_at, configuration_version, updated_at\n                    )\n                    SELECT\n                        id, name, tool_name, tool_params, cron_expression, enabled,\n                        last_run, last_status, last_duration_ms, last_message,\n                        created_at, configuration_version, updated_at\n                    FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE"]}\n                    WHERE TRUE\n                    ON DUPLICATE KEY UPDATE\n                        name = VALUES(name),\n                        tool_name = VALUES(tool_name),\n                        tool_params = VALUES(tool_params),\n                        cron_expression = VALUES(cron_expression),\n                        enabled = VALUES(enabled),\n                        last_run = VALUES(last_run),\n                        last_status = VALUES(last_status),\n                        last_duration_ms = VALUES(last_duration_ms),\n                        last_message = VALUES(last_message),\n                        created_at = VALUES(created_at),\n                        configuration_version = VALUES(configuration_version),\n                        updated_at = VALUES(updated_at)\n                    ')
            if runtime["_migration_table_exists"](cursor):
                cursor.execute('DELETE FROM schema_migrations WHERE version=%s', (runtime["AUTOMATION_PROJECT_AUTHORIZATION_VERSION"],))
            for table_name in (runtime["AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE"], runtime["AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE"], runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]):
                if runtime["_table_exists"](cursor, table_name):
                    cursor.execute(f'DROP TABLE {table_name}')
            print('automation_project_authorization_restore=ok')
    except Exception:
        if transaction_started:
            connection.rollback()
        raise
    finally:
        connection.close()
    return 0
