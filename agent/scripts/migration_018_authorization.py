"""Restartable MySQL 018 restore/status helpers.

Every dependency is supplied by ``run_migrations`` so test patches and
the deployment runner keep one authoritative runtime boundary.
"""

from __future__ import annotations


_LEGACY_PENDING_RESOURCE_KEY = "phase7.pending_arrivals_sheet"
_OLD_REVIEWED_RESOURCE_KEYS = frozenset(
    {
        "phase7.yunda_dispatch_forecast_bitable",
        "phase7.yunda_send_waybills_bitable",
        "phase7.yunda_send_waybills_sheet",
        "phase7.self_pickup_source_sheet",
        "phase7.split_pending_source_sheet",
        "phase7.split_pending_target_sheet",
        "phase7.site_send_bitable",
        "phase7.site_send_sheet",
        "phase7.send_order_bitable",
        "phase7.arrive_primary_sheet",
        "phase7.arrive_secondary_sheet",
        "phase7.stats_archive_sheet",
        "phase7.daily_sign_bitable",
        "phase7.daily_sign_sheet",
    }
)
_EXPANDED_CODE_OWNED_RESOURCE_KEYS = frozenset(
    {
        "phase7.delivery_status_bitable",
        "phase7.delivery_status_webhook",
        "phase7.scan_webhook",
        "phase7.stats_webhook",
        "automation.feishu_route.arrive_list",
        "automation.feishu_route.send_order",
        "automation.feishu_route.yunda_dispatch_forecast",
        "automation.feishu_route.yunda_send_waybills",
        "automation.feishu_route.scan_codes",
        "automation.feishu_route.arrival_stats",
        "automation.feishu_route.self_pickup_problem_upload",
        "automation.feishu_route.split_pending_problem_upload",
    }
)
_CURRENT_REVIEWED_RESOURCE_KEYS = (
    _OLD_REVIEWED_RESOURCE_KEYS | _EXPANDED_CODE_OWNED_RESOURCE_KEYS
)
_VALID_RESOURCE_BACKUP_LAYOUTS = frozenset(
    {
        _OLD_REVIEWED_RESOURCE_KEYS,
        _OLD_REVIEWED_RESOURCE_KEYS | {_LEGACY_PENDING_RESOURCE_KEY},
        _CURRENT_REVIEWED_RESOURCE_KEYS,
        _CURRENT_REVIEWED_RESOURCE_KEYS | {_LEGACY_PENDING_RESOURCE_KEY},
    }
)

# This is the immutable project scope admitted by migration 018.  It must not
# follow later release manifests: restore is allowed to remove only the exact
# one-time bootstrap that 018 could have written.
_BOOTSTRAP_PROJECT_IDS_018 = frozenset(
    {
        "arrival_stats",
        "arrive_list",
        "clockin_daxiang",
        "clockin_daxiang_s",
        "customer_problems_shadow",
        "daily_sign",
        "delivery_status",
        "finance_bills",
        "finance_startup_catchup",
        "scan_codes",
        "self_pickup_problem_upload",
        "send_order",
        "site_send",
        "split_pending_problem_upload",
        "yunda_dispatch_forecast",
        "yunda_send_waybills",
    }
)
_BOOTSTRAP_COMPLETED_BY = "system:automation-project-bootstrap-018"
_BOOTSTRAP_RESTORE_IN_PROGRESS = (
    "system:automation-project-bootstrap-018:restore-in-progress"
)
_SELF_PICKUP_RESOURCE_KEY = "phase7.self_pickup_source_sheet"
_SELF_PICKUP_FORMULA_SOURCE_SHEET_ID = "8fc516"
_SELF_PICKUP_FORMULA_SOURCE_RANGE = "8fc516!A1:S197"
_BOOTSTRAP_SOURCE_FIELDS = frozenset(
    {
        "schema_version",
        "automation_id",
        "automation_generation",
        "project_configuration_version",
        "contract_hash",
        "configuration_request_id",
        "configuration_event_metadata_sha256",
        "scheduled_tasks",
    }
)
_BOOTSTRAP_SOURCE_TASK_FIELDS = frozenset(
    {
        "task_id",
        "tool_name",
        "automation_generation",
        "configuration_version",
        "enabled",
        "cron_expression_hash",
        "arguments_hash",
        "source_policy_mode",
        "source_policy_version",
        "legacy_authorized",
        "legacy_grant_request_id",
        "legacy_grant_contract_hash",
        "legacy_grant_tool_contract_hash",
        "retirement_kind",
        "retirement_request_id",
    }
)


def _database_flag(value: object) -> bool:
    return value is True or value == 1


def _bootstrap_json_object(runtime, value):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = runtime["json"].loads(value)
        except Exception as exc:
            raise RuntimeError(
                "018 project policy bootstrap evidence is invalid"
            ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("018 project policy bootstrap evidence is invalid")
    return value


def _bootstrap_json_sha256(runtime, value) -> str:
    try:
        encoded = runtime["json"].dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return runtime["hashlib"].sha256(encoded).hexdigest()
    except Exception as exc:
        raise RuntimeError(
            "018 project policy bootstrap evidence is invalid"
        ) from exc


def _bootstrap_is_sha256(value: object) -> bool:
    normalized = str(value or "")
    return len(normalized) == 64 and all(
        character in "0123456789abcdef" for character in normalized
    )


def _bootstrap_uuid5(runtime, name: str) -> str:
    return str(runtime["uuid"].uuid5(runtime["uuid"].NAMESPACE_URL, name))


def _validate_bootstrap_source_snapshot(
    runtime,
    snapshot,
    *,
    release_sha: str,
) -> str:
    """Validate retained 018 evidence and derive its only safe policy mode."""

    if (
        set(snapshot) != _BOOTSTRAP_SOURCE_FIELDS
        or type(snapshot.get("schema_version")) is not int
        or snapshot.get("schema_version") != 1
        or str(snapshot.get("automation_id") or "")
        not in _BOOTSTRAP_PROJECT_IDS_018
        or type(snapshot.get("automation_generation")) is not int
        or snapshot.get("automation_generation") <= 0
        or type(snapshot.get("project_configuration_version")) is not int
        or snapshot.get("project_configuration_version") <= 0
        or not _bootstrap_is_sha256(snapshot.get("contract_hash"))
        or not _bootstrap_is_sha256(
            snapshot.get("configuration_event_metadata_sha256")
        )
        or snapshot.get("configuration_request_id")
        != _bootstrap_uuid5(
            runtime,
            "boyi:first-party-plugin-config:"
            f"{release_sha}:{snapshot.get('automation_id')}",
        )
        or not isinstance(snapshot.get("scheduled_tasks"), list)
    ):
        raise RuntimeError("018 project policy bootstrap items are invalid")

    generation = snapshot["automation_generation"]
    configuration_version = snapshot["project_configuration_version"]
    configuration_request_id = snapshot["configuration_request_id"]
    task_ids = []
    all_legacy_authorized = bool(snapshot["scheduled_tasks"])
    for task in snapshot["scheduled_tasks"]:
        if not isinstance(task, dict) or set(task) != _BOOTSTRAP_SOURCE_TASK_FIELDS:
            raise RuntimeError("018 project policy bootstrap items are invalid")
        task_id = str(task.get("task_id") or "")
        grant_request_id = str(task.get("legacy_grant_request_id") or "")
        grant_contract_hash = str(
            task.get("legacy_grant_contract_hash") or ""
        )
        grant_tool_hash = str(
            task.get("legacy_grant_tool_contract_hash") or ""
        )
        retirement_kind = str(task.get("retirement_kind") or "")
        retirement_request_id = str(task.get("retirement_request_id") or "")
        legacy_authorized = task.get("legacy_authorized")
        if (
            not task_id
            or not str(task.get("tool_name") or "")
            or task.get("automation_generation") != generation
            or task.get("configuration_version") != configuration_version
            or type(task.get("enabled")) is not bool
            or not _bootstrap_is_sha256(task.get("cron_expression_hash"))
            or not _bootstrap_is_sha256(task.get("arguments_hash"))
            or task.get("source_policy_mode") != "REQUIRE_EACH_RUN"
            or type(task.get("source_policy_version")) is not int
            or task.get("source_policy_version") <= 0
            or type(legacy_authorized) is not bool
        ):
            raise RuntimeError("018 project policy bootstrap items are invalid")
        if grant_request_id:
            if (
                grant_request_id
                != _bootstrap_uuid5(
                    runtime,
                    f"boyi:control-plane-v1:{task_id}",
                )
                or not _bootstrap_is_sha256(grant_contract_hash)
                or not _bootstrap_is_sha256(grant_tool_hash)
            ):
                raise RuntimeError("018 project policy bootstrap items are invalid")
        elif grant_contract_hash or grant_tool_hash:
            raise RuntimeError("018 project policy bootstrap items are invalid")
        if retirement_kind == "CONFIGURATION_MIGRATION":
            if retirement_request_id != configuration_request_id:
                raise RuntimeError("018 project policy bootstrap items are invalid")
        elif retirement_kind == "NONE":
            if retirement_request_id:
                raise RuntimeError("018 project policy bootstrap items are invalid")
        else:
            raise RuntimeError("018 project policy bootstrap items are invalid")
        if legacy_authorized and (
            task.get("enabled") is not True
            or not grant_request_id
            or retirement_kind != "CONFIGURATION_MIGRATION"
        ):
            raise RuntimeError("018 project policy bootstrap items are invalid")
        task_ids.append(task_id)
        all_legacy_authorized = all_legacy_authorized and legacy_authorized
    if task_ids != sorted(task_ids) or len(task_ids) != len(set(task_ids)):
        raise RuntimeError("018 project policy bootstrap items are invalid")
    return (
        "LEGACY_SCHEDULE_ONLY"
        if all_legacy_authorized
        else "REQUIRE_EACH_RUN"
    )


def _bootstrap_restore_table_state(runtime, cursor) -> tuple[bool, ...]:
    return tuple(
        runtime["_table_exists"](cursor, table_name)
        for table_name in runtime["AUTOMATION_PROJECT_AUTHORIZATION_TABLES_REVERSE"]
    )


def _bootstrap_restore_prefix_is_removed(table_state: tuple[bool, ...]) -> bool:
    """True for a zero-or-more removed prefix followed by intact tables.

    MySQL commits the in-progress marker before attempting the first DDL.  The
    DROP itself can fail after that implicit commit, so an intact table set is
    one valid retry state once (and only once) the marker carries the sentinel.
    """

    first_present = next(
        (index for index, present in enumerate(table_state) if present),
        len(table_state),
    )
    return table_state == (
        (False,) * first_present + (True,) * (len(table_state) - first_present)
    )


def _validated_resource_backup_layout(rows):
    """Return exact backup/hash identities for the four owned 018 layouts."""

    resource_rows = tuple(rows or ())
    resource_keys = tuple(str(row.get("resource_key") or "") for row in resource_rows)
    resource_key_set = frozenset(resource_keys)
    if len(resource_keys) != len(resource_key_set):
        raise RuntimeError("018 reviewed-resource backup is incomplete")
    if resource_key_set and resource_key_set not in _VALID_RESOURCE_BACKUP_LAYOUTS:
        raise RuntimeError("018 reviewed-resource backup is incomplete")
    if _LEGACY_PENDING_RESOURCE_KEY in resource_key_set:
        pending_row = next(
            row
            for row in resource_rows
            if row.get("resource_key") == _LEGACY_PENDING_RESOURCE_KEY
        )
        if not _database_flag(pending_row.get("existed_before")):
            raise RuntimeError("018 reviewed-resource backup is incomplete")

    captured_keys = frozenset(
        str(row.get("resource_key") or "")
        for row in resource_rows
        if _database_flag(row.get("captured"))
    )
    valid_captured_layouts = {frozenset(), resource_key_set}
    if _EXPANDED_CODE_OWNED_RESOURCE_KEYS <= resource_key_set:
        valid_captured_layouts.add(
            _OLD_REVIEWED_RESOURCE_KEYS
            | (resource_key_set & {_LEGACY_PENDING_RESOURCE_KEY})
        )
    if captured_keys not in valid_captured_layouts:
        raise RuntimeError("018 reviewed-resource capture is incomplete")
    return resource_key_set, captured_keys


def _automation_project_authorization_artifacts(runtime, cursor) -> set[str]:
    artifacts = {table_name for table_name in (runtime["AUTOMATION_PROJECT_AUTHORIZATION_BACKUP_TABLE"], runtime["AUTOMATION_PROJECT_AUTHORIZATION_CAPTURE_TABLE"], runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"], *runtime["AUTOMATION_PROJECT_AUTHORIZATION_TABLES_REVERSE"]) if runtime["_table_exists"](cursor, table_name)}
    for table_name, column_name in (('scheduled_tasks', 'automation_id'), ('scheduled_tasks', 'automation_generation'), ('workflow_resources', 'configuration_version'), ('workflow_resources', 'config_sha256'), ('agent_commands', 'automation_id'), ('agent_commands', 'automation_generation'), ('agent_commands', 'automation_invocation_json')):
        if runtime["_column_exists"](cursor, table_name, column_name):
            artifacts.add(f'{table_name}.{column_name}')
    return artifacts


def _validate_project_policy_bootstrap_restore(runtime, cursor) -> bool:
    """Allow deletion only for the exact release-owned 018 policy bootstrap."""

    marker_table = "automation_project_bootstrap_marker_018"
    item_table = "automation_project_bootstrap_items_018"
    event_table = "automation_project_policy_events"
    marker_exists = runtime["_table_exists"](cursor, marker_table)
    items_exist = runtime["_table_exists"](cursor, item_table)
    events_exist = runtime["_table_exists"](cursor, event_table)
    if not marker_exists and not items_exist:
        return False
    if items_exist and not marker_exists:
        raise RuntimeError("018 project policy bootstrap is incomplete")

    reverse_tables = tuple(
        runtime["AUTOMATION_PROJECT_AUTHORIZATION_TABLES_REVERSE"]
    )
    if (
        not reverse_tables
        or reverse_tables[-2:] != (item_table, marker_table)
        or event_table not in reverse_tables
    ):
        raise RuntimeError("018 project policy bootstrap restore order is invalid")
    table_state = _bootstrap_restore_table_state(runtime, cursor)

    cursor.execute(
        f"""
        SELECT marker_id, release_sha, project_set_sha256, completed_by
        FROM {marker_table}
        FOR UPDATE
        """
    )
    markers = tuple(cursor.fetchall() or ())
    if not markers:
        if not items_exist:
            return False
        cursor.execute(
            f"SELECT automation_id FROM {item_table} LIMIT 1 FOR UPDATE"
        )
        if cursor.fetchone() is None:
            return False
        raise RuntimeError("018 project policy bootstrap is incomplete")
    if len(markers) != 1:
        raise RuntimeError("018 project policy bootstrap marker is invalid")
    marker = markers[0]
    release_sha = str(marker.get("release_sha") or "")
    project_set_sha256 = str(marker.get("project_set_sha256") or "")
    completed_by = str(marker.get("completed_by") or "")
    if (
        marker.get("marker_id") != 1
        or len(release_sha) != 40
        or any(character not in "0123456789abcdef" for character in release_sha)
        or len(project_set_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in project_set_sha256
        )
        or completed_by
        not in {_BOOTSTRAP_COMPLETED_BY, _BOOTSTRAP_RESTORE_IN_PROGRESS}
    ):
        raise RuntimeError("018 project policy bootstrap marker is invalid")
    if completed_by == _BOOTSTRAP_COMPLETED_BY:
        if not all(table_state):
            raise RuntimeError("018 project policy bootstrap is incomplete")
    elif not _bootstrap_restore_prefix_is_removed(table_state):
        raise RuntimeError("018 project policy bootstrap is incomplete")

    if not items_exist:
        # Items are deliberately penultimate and the in-progress marker is
        # deliberately last.  This is the only evidence-safe marker-only
        # state possible after a MySQL DDL auto-commit interruption.
        if table_state != (False,) * (len(table_state) - 1) + (True,):
            raise RuntimeError("018 project policy bootstrap is incomplete")
        return True
    if not events_exist and completed_by != _BOOTSTRAP_RESTORE_IN_PROGRESS:
        raise RuntimeError("018 project policy bootstrap is incomplete")
    if not events_exist:
        event_index = reverse_tables.index(event_table)
        if any(table_state[: event_index + 1]):
            raise RuntimeError("018 project policy bootstrap is incomplete")

    cursor.execute(
        f"""
        SELECT automation_id, initial_mode, source_set_sha256,
               source_snapshot_json, policy_version,
               JSON_UNQUOTE(JSON_EXTRACT(
                   source_snapshot_json, '$.automation_id'
               )) AS source_automation_id,
               CAST(JSON_UNQUOTE(JSON_EXTRACT(
                   source_snapshot_json, '$.automation_generation'
               )) AS UNSIGNED) AS source_generation,
               CAST(JSON_UNQUOTE(JSON_EXTRACT(
                   source_snapshot_json, '$.project_configuration_version'
               )) AS UNSIGNED) AS source_configuration_version
        FROM {item_table}
        ORDER BY BINARY automation_id
        FOR UPDATE
        """
    )
    items = tuple(cursor.fetchall() or ())
    item_ids = tuple(str(item.get("automation_id") or "") for item in items)
    project_hash_items = []
    for item in items:
        snapshot = _bootstrap_json_object(
            runtime,
            item.get("source_snapshot_json"),
        )
        derived_mode = _validate_bootstrap_source_snapshot(
            runtime,
            snapshot,
            release_sha=release_sha,
        )
        if (
            str(snapshot.get("automation_id") or "")
            != str(item.get("automation_id") or "")
            or snapshot.get("automation_generation")
            != item.get("source_generation")
            or snapshot.get("project_configuration_version")
            != item.get("source_configuration_version")
            or derived_mode != str(item.get("initial_mode") or "")
            or _bootstrap_json_sha256(runtime, snapshot)
            != str(item.get("source_set_sha256") or "")
        ):
            raise RuntimeError("018 project policy bootstrap items are invalid")
        project_hash_items.append(
            {
                "automation_id": str(item.get("automation_id") or ""),
                "initial_mode": str(item.get("initial_mode") or ""),
                "source_set_sha256": str(item.get("source_set_sha256") or ""),
                "policy_version": item.get("policy_version"),
            }
        )
    if (
        frozenset(item_ids) != _BOOTSTRAP_PROJECT_IDS_018
        or len(item_ids) != len(_BOOTSTRAP_PROJECT_IDS_018)
        or any(
            not automation_id
            or str(item.get("source_automation_id") or "") != automation_id
            or type(item.get("policy_version")) is not int
            or item.get("policy_version") <= 0
            or type(item.get("source_generation")) is not int
            or item.get("source_generation") <= 0
            or type(item.get("source_configuration_version")) is not int
            or item.get("source_configuration_version") <= 0
            or len(str(item.get("source_set_sha256") or "")) != 64
            or any(
                character not in "0123456789abcdef"
                for character in str(item.get("source_set_sha256") or "")
            )
            for automation_id, item in zip(item_ids, items, strict=True)
        )
    ):
        raise RuntimeError("018 project policy bootstrap items are invalid")
    expected_project_set_sha256 = _bootstrap_json_sha256(
        runtime,
        {
            "schema_version": 1,
            "release_sha": release_sha,
            "projects": sorted(
                project_hash_items,
                key=lambda item: item["automation_id"],
            ),
        },
    )
    if expected_project_set_sha256 != project_set_sha256:
        raise RuntimeError("018 project policy bootstrap marker is invalid")
    if not events_exist:
        return True

    cursor.execute(
        f"""
        SELECT event.event_id, event.automation_id, event.request_id,
               event.from_mode,
               event.to_mode, event.contract_hash,
               event.contract_snapshot_json, event.tool_contract_hash,
               event.plugin_contract_hash,
               event.project_configuration_version,
               event.project_generation, event.actor_id, event.actor_role,
               event.actor_display_name, event.reason, event.comment,
               event.correlation_id,
               item.initial_mode,
               JSON_UNQUOTE(JSON_EXTRACT(
                   item.source_snapshot_json, '$.contract_hash'
               )) AS source_contract_hash,
               CAST(JSON_UNQUOTE(JSON_EXTRACT(
                   item.source_snapshot_json, '$.automation_generation'
               )) AS UNSIGNED) AS source_generation,
               CAST(JSON_UNQUOTE(JSON_EXTRACT(
                   item.source_snapshot_json,
                   '$.project_configuration_version'
               )) AS UNSIGNED) AS source_configuration_version
        FROM {event_table} AS event
        LEFT JOIN {item_table} AS item
          ON BINARY item.automation_id = BINARY event.automation_id
        WHERE BINARY event.actor_id =
              BINARY 'system:automation-project-bootstrap-018'
        ORDER BY event.event_id
        FOR UPDATE
        """
    )
    bootstrap_events = tuple(cursor.fetchall() or ())
    if len(bootstrap_events) != len(items):
        raise RuntimeError("018 project policy bootstrap events are invalid")
    event_ids = tuple(
        str(event.get("automation_id") or "") for event in bootstrap_events
    )
    if len(set(event_ids)) != len(items) or set(event_ids) != set(item_ids):
        raise RuntimeError("018 project policy bootstrap events are invalid")
    for event in bootstrap_events:
        mode = str(event.get("initial_mode") or "")
        automation_id = str(event.get("automation_id") or "")
        expected_request_id = _bootstrap_uuid5(
            runtime,
            f"boyi:automation-project-bootstrap-018:{automation_id}",
        )
        contract_snapshot = event.get("contract_snapshot_json")
        if isinstance(contract_snapshot, str):
            contract_snapshot = _bootstrap_json_object(
                runtime,
                contract_snapshot,
            )
        contract_fields = (
            event.get("contract_hash"),
            contract_snapshot,
            event.get("tool_contract_hash"),
            event.get("plugin_contract_hash"),
        )
        if (
            type(event.get("event_id")) is not int
            or event.get("event_id") <= 0
            or event.get("request_id") != expected_request_id
            or event.get("from_mode") != "REQUIRE_EACH_RUN"
            or event.get("to_mode") != mode
            or event.get("project_generation")
            != event.get("source_generation")
            or event.get("project_configuration_version")
            != event.get("source_configuration_version")
            or event.get("actor_id") != _BOOTSTRAP_COMPLETED_BY
            or event.get("actor_role") != "system"
            or event.get("actor_display_name")
            != "Automation project bootstrap 018"
            or event.get("reason") != "AUTOMATION_PROJECT_BOOTSTRAP_018"
            or event.get("comment")
            != "Release-held one-time policy bootstrap"
            or event.get("correlation_id") != expected_request_id
            or (
                mode == "LEGACY_SCHEDULE_ONLY"
                and (
                    any(value is None for value in contract_fields)
                    or event.get("contract_hash")
                    != event.get("source_contract_hash")
                    or _bootstrap_json_sha256(runtime, contract_snapshot)
                    != event.get("source_contract_hash")
                    or any(
                        len(str(value or "")) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in str(value or "")
                        )
                        for value in (
                            event.get("tool_contract_hash"),
                            event.get("plugin_contract_hash"),
                        )
                    )
                )
            )
            or (
                mode == "REQUIRE_EACH_RUN"
                and any(value is not None for value in contract_fields)
            )
        ):
            raise RuntimeError("018 project policy bootstrap events are invalid")
    return True


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
    migration_036_applied = False
    if runtime.get("_migration_table_exists") and runtime["_migration_table_exists"](
        cursor
    ):
        cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE BINARY version = BINARY %s",
            ("036",),
        )
        migration_036_applied = cursor.fetchone() is not None
    if (not resource_backup_exists) and (
        capture_started
        or schedule_column_exists
        or resource_version_exists
        or resource_hash_exists
    ):
        raise RuntimeError('018 reviewed-resource backup is missing after migration start')
    if resource_backup_exists:
        cursor.execute(
            f"""
            SELECT
                resource_key,
                existed_before,
                migration_config_sha256 IS NOT NULL AS captured
            FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]}
            ORDER BY BINARY resource_key
            FOR UPDATE
            """
        )
        resource_keys, resource_hash_keys = _validated_resource_backup_layout(
            cursor.fetchall()
        )
        resource_backup_count = len(resource_keys)
        resource_hash_count = len(resource_hash_keys)
        legacy_pending_count = int(
            _LEGACY_PENDING_RESOURCE_KEY in resource_keys
        )
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
                migration_018_config = """
                    CASE
                        WHEN backup.existed_before = TRUE THEN
                            CASE
                                WHEN JSON_EXTRACT(
                                    backup.config_json, '$.resource_kind'
                                ) IS NULL
                                THEN JSON_SET(
                                    backup.config_json,
                                    '$.resource_kind',
                                    reviewed.expected_kind
                                )
                                ELSE backup.config_json
                            END
                        ELSE reviewed.default_config_json
                    END
                """
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS changed_count
                    FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]} AS backup
                    INNER JOIN {runtime["AUTOMATION_PROJECT_AUTHORIZATION_REVIEWED_RESOURCE_MAP_TABLE"]} AS reviewed
                      ON BINARY reviewed.resource_key = BINARY backup.resource_key
                    LEFT JOIN workflow_resources AS resource
                      ON BINARY resource.resource_key = BINARY backup.resource_key
                    WHERE backup.migration_config_sha256 IS NOT NULL
                      AND NOT (
                        (
                            resource.resource_key IS NOT NULL
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
                            %s = TRUE
                            AND BINARY backup.resource_key = BINARY %s
                            AND resource.resource_key IS NOT NULL
                            AND resource.configuration_version = 2
                            AND BINARY backup.migration_config_sha256 = BINARY SHA2(
                                CAST(({migration_018_config}) AS CHAR CHARACTER SET utf8mb4),
                                256
                            )
                            AND NOT COALESCE((
                                BINARY JSON_UNQUOTE(JSON_EXTRACT(
                                    ({migration_018_config}),
                                    '$.formula_source_sheet_id'
                                )) = BINARY %s
                                AND BINARY JSON_UNQUOTE(JSON_EXTRACT(
                                    ({migration_018_config}),
                                    '$.formula_source_range'
                                )) = BINARY %s
                            ), FALSE)
                            AND BINARY resource.config_sha256 = BINARY SHA2(
                                CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                                256
                            )
                            AND BINARY resource.config_sha256 = BINARY SHA2(
                                CAST(JSON_SET(
                                    ({migration_018_config}),
                                    '$.formula_source_sheet_id', %s,
                                    '$.formula_source_range', %s
                                ) AS CHAR CHARACTER SET utf8mb4),
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
                      )
                    FOR UPDATE
                    """,
                    (
                        migration_036_applied,
                        _SELF_PICKUP_RESOURCE_KEY,
                        _SELF_PICKUP_FORMULA_SOURCE_SHEET_ID,
                        _SELF_PICKUP_FORMULA_SOURCE_RANGE,
                        _SELF_PICKUP_FORMULA_SOURCE_SHEET_ID,
                        _SELF_PICKUP_FORMULA_SOURCE_RANGE,
                    ),
                )
                if int((cursor.fetchone() or {}).get('changed_count') or 0):
                    raise RuntimeError(
                        '018 restore refuses changed reviewed resources'
                    )
            if resource_hash_count < resource_backup_count and runtime["_table_exists"](
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
                    WHERE backup.migration_config_sha256 IS NULL
                      AND NOT (
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
                          AND backup.migration_config_sha256 IS NULL
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
            elif resource_hash_count < resource_backup_count:
                cursor.execute(
                    f'''\n                    SELECT COUNT(*) AS changed_count\n                    FROM {runtime["AUTOMATION_PROJECT_AUTHORIZATION_RESOURCE_BACKUP_TABLE"]} AS backup\n                    LEFT JOIN workflow_resources AS resource\n                      ON BINARY resource.resource_key = BINARY backup.resource_key\n                    WHERE backup.migration_config_sha256 IS NULL\n                      AND NOT (\n                        (\n                            backup.existed_before = TRUE\n                            AND resource.resource_key IS NOT NULL\n                            AND BINARY resource.source <=> BINARY backup.source\n                            AND BINARY CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4) =\n                                BINARY CAST(backup.config_json AS CHAR CHARACTER SET utf8mb4)\n                        )\n                        OR (\n                            backup.existed_before = FALSE\n                            AND resource.resource_key IS NULL\n                        )\n                    )\n                    FOR UPDATE\n                    '''
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
    project_policy_bootstrap_present = _validate_project_policy_bootstrap_restore(
        runtime,
        cursor,
    )
    for table_name in ('automation_project_events', 'automation_project_policy_events', 'automation_plugin_package_events', 'automation_worker_pairing_events'):
        if not runtime["_table_exists"](cursor, table_name):
            continue
        if (
            table_name == 'automation_project_policy_events'
            and project_policy_bootstrap_present
        ):
            cursor.execute(
                f'''SELECT COUNT(*) AS user_event_count
                    FROM {table_name}
                    WHERE actor_role <> %s
                      AND BINARY actor_id <>
                          BINARY 'system:automation-project-bootstrap-018'
                    FOR UPDATE''',
                (runtime["CONTROL_PLANE_MIGRATION_ACTOR_ROLE"],),
            )
        else:
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


def _clear_automation_project_authorization_reapply_history(runtime, cursor) -> None:
    """Invalidate every migration record whose owned schema was restored.

    The restore drops 018 tables plus later lease, policy/routing, receipt,
    Service v2, and activation-journal state.  Leaving any one of those
    ledger rows would make the next normal migration run skip a missing
    dependency or policy transformation.
    """

    versions = tuple(
        runtime["AUTOMATION_PROJECT_AUTHORIZATION_REAPPLY_MIGRATION_VERSIONS"]
    )
    if not versions:
        raise RuntimeError("018 restore reapply migration history is empty")
    placeholders = ", ".join("%s" for _version in versions)
    cursor.execute(
        "DELETE FROM schema_migrations "
        f"WHERE BINARY version IN ({placeholders})",
        versions,
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
            if runtime["_table_exists"](
                cursor,
                "automation_project_bootstrap_marker_018",
            ):
                # The first following DDL auto-commits this transition.  A
                # retry may then accept only the exact sequential table-drop
                # prefix and can safely finish with the marker as the final
                # durable witness.
                cursor.execute(
                    """
                    UPDATE automation_project_bootstrap_marker_018
                    SET completed_by=%s
                    WHERE marker_id=1 AND completed_by IN (%s, %s)
                    """,
                    (
                        _BOOTSTRAP_RESTORE_IN_PROGRESS,
                        _BOOTSTRAP_COMPLETED_BY,
                        _BOOTSTRAP_RESTORE_IN_PROGRESS,
                    ),
                )
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
                _clear_automation_project_authorization_reapply_history(
                    runtime, cursor
                )
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
