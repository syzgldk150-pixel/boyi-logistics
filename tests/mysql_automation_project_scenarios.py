"""Real-MySQL scenarios for migration 018 and durable Worker dispatch."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import json
import os
from unittest.mock import patch
from uuid import uuid4


REQUIRED_PROJECT_RESOURCE_CONFIGS = (
    (
        "phase7.site_send_bitable",
        {"base_token": "integration-site-base", "table_id": "integration-site-table"},
    ),
    (
        "phase7.site_send_sheet",
        {
            "spreadsheet_token": "integration-site-sheet",
            "range": "Sheet1!A2:F5000",
        },
    ),
    (
        "phase7.send_order_bitable",
        {
            "base_token": "integration-send-order-base",
            "table_id": "integration-send-order-table",
        },
    ),
    (
        "phase7.arrive_primary_sheet",
        {
            "spreadsheet_token": "integration-arrive-primary",
            "range": "Primary!A1:R5000",
            "clear_range": "Primary!A2:R5000",
        },
    ),
    (
        "phase7.arrive_secondary_sheet",
        {
            "spreadsheet_token": "integration-arrive-secondary",
            "range": "Secondary!A1:R5000",
            "clear_range": "Secondary!A2:R5000",
        },
    ),
    (
        "phase7.stats_archive_sheet",
        {
            "spreadsheet_token": "integration-stats-archive",
            "default_write_range": "Archive!A1:L5000",
        },
    ),
    (
        "phase7.daily_sign_bitable",
        {
            "base_token": "integration-daily-sign-base",
            "table_id": "integration-daily-sign-table",
        },
    ),
    (
        "phase7.daily_sign_sheet",
        {
            "spreadsheet_token": "integration-daily-sign-sheet",
            "range": "Daily!A1:Z5000",
        },
    ),
)

REQUIRED_PROJECT_RESOURCE_KINDS = {
    "phase7.site_send_bitable": "feishu_bitable",
    "phase7.site_send_sheet": "feishu_sheet",
    "phase7.send_order_bitable": "feishu_bitable",
    "phase7.arrive_primary_sheet": "feishu_sheet",
    "phase7.arrive_secondary_sheet": "feishu_sheet",
    "phase7.stats_archive_sheet": "feishu_sheet",
    "phase7.daily_sign_bitable": "feishu_bitable",
    "phase7.daily_sign_sheet": "feishu_sheet",
}

DEFERRED_R7_RESOURCE_KEYS = frozenset(
    {
        "automation.feishu_route.r7_arrival_checkin",
        "automation.feishu_route.r7_departure_checkin",
    }
)
OLD_CODE_OWNED_RESOURCE_KEYS = frozenset(
    {
        "phase7.yunda_dispatch_forecast_bitable",
        "phase7.yunda_send_waybills_bitable",
        "phase7.yunda_send_waybills_sheet",
        "phase7.self_pickup_source_sheet",
        "phase7.split_pending_source_sheet",
        "phase7.split_pending_target_sheet",
    }
)


def _project_resource_contract():
    """Return the exact 018 reviewed sets from the authoritative defaults."""

    from agent.phase7_resource_import import BUILTIN_RESOURCES

    code_owned = {
        resource_key: dict(config)
        for resource_key, config in BUILTIN_RESOURCES.items()
        if resource_key not in DEFERRED_R7_RESOURCE_KEYS
    }
    required_existing = {
        resource_key: dict(config)
        for resource_key, config in REQUIRED_PROJECT_RESOURCE_CONFIGS
    }
    if set(required_existing) != set(REQUIRED_PROJECT_RESOURCE_KINDS):
        raise AssertionError("required resource kind fixture is incomplete")
    reviewed_keys = tuple(sorted({*code_owned, *required_existing}))
    old_reviewed_keys = frozenset(
        {*OLD_CODE_OWNED_RESOURCE_KEYS, *required_existing}
    )
    if len(code_owned) != 18 or len(required_existing) != 8:
        raise AssertionError("integration fixture drifted from migration 018")
    if len(reviewed_keys) != 26 or len(old_reviewed_keys) != 14:
        raise AssertionError("migration 018 reviewed identity set is not exact")
    return code_owned, required_existing, reviewed_keys, old_reviewed_keys

LEGACY_R7_DEPARTURE_TASK_ID = "r7_departure_checkin"
LEGACY_R7_DEPARTURE_PARAMS = {
    "do_departure_checkin": False,
    "history_marker": "preserve-exactly",
}
LEGACY_R7_DEPARTURE_CREATED_AT = datetime(2025, 5, 6, 7, 8, 9)
LEGACY_R7_DEPARTURE_UPDATED_AT = datetime(2025, 6, 7, 8, 9, 10)
LEGACY_R7_DEPARTURE_LAST_RUN = datetime(2025, 6, 6, 21, 35, 11)


def seed_legacy_r7_departure_task(case, database: str) -> None:
    """Seed the exact deferred historical identity without enabling it."""

    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, name, tool_name, tool_params, cron_expression, enabled,
                    last_run, last_status, last_duration_ms, last_message,
                    created_at, configuration_version, updated_at
                ) VALUES (%s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    LEGACY_R7_DEPARTURE_TASK_ID,
                    "Integration deferred R7 departure history",
                    "r7_departure_checkin",
                    json.dumps(LEGACY_R7_DEPARTURE_PARAMS, separators=(",", ":")),
                    "5 21 * * *",
                    LEGACY_R7_DEPARTURE_LAST_RUN,
                    "FAILED",
                    9876,
                    "integration-r7-history",
                    LEGACY_R7_DEPARTURE_CREATED_AT,
                    7,
                    LEGACY_R7_DEPARTURE_UPDATED_AT,
                ),
            )


def assert_legacy_r7_departure_state(case, row) -> None:
    """Assert migration added no executable or operational state changes."""

    case.assertIsNotNone(row)
    case.assertEqual(row["id"], LEGACY_R7_DEPARTURE_TASK_ID)
    case.assertEqual(row["name"], "Integration deferred R7 departure history")
    case.assertEqual(row["tool_name"], "r7_departure_checkin")
    case.assertEqual(json.loads(row["tool_params"]), LEGACY_R7_DEPARTURE_PARAMS)
    case.assertEqual(row["cron_expression"], "5 21 * * *")
    case.assertEqual(row["enabled"], 0)
    case.assertEqual(row["last_run"], LEGACY_R7_DEPARTURE_LAST_RUN)
    case.assertEqual(row["last_status"], "FAILED")
    case.assertEqual(row["last_duration_ms"], 9876)
    case.assertEqual(row["last_message"], "integration-r7-history")
    case.assertEqual(row["created_at"], LEGACY_R7_DEPARTURE_CREATED_AT)
    case.assertEqual(row["configuration_version"], 7)
    case.assertEqual(row["updated_at"], LEGACY_R7_DEPARTURE_UPDATED_AT)


def select_legacy_r7_departure_task(cursor, *, include_identity: bool):
    identity_columns = ", automation_id, automation_generation" if include_identity else ""
    cursor.execute(
        f"""
        SELECT
            id, name, tool_name, tool_params, cron_expression, enabled,
            last_run, last_status, last_duration_ms, last_message,
            created_at, configuration_version, updated_at{identity_columns}
        FROM scheduled_tasks
        WHERE BINARY id = BINARY %s
        """,
        (LEGACY_R7_DEPARTURE_TASK_ID,),
    )
    return cursor.fetchone()


def seed_required_project_resources(case, database: str) -> None:
    """Seed only the eight external resources migration 018 must not invent."""

    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version='018'"
            )
            if int(cursor.fetchone()["n"]):
                return
            cursor.executemany(
                """
                INSERT IGNORE INTO workflow_resources (
                    resource_key, config_json, source
                ) VALUES (%s, %s, 'integration-required-resource')
                """,
                tuple(
                    (
                        resource_key,
                        json.dumps(config, separators=(",", ":")),
                    )
                    for resource_key, config in REQUIRED_PROJECT_RESOURCE_CONFIGS
                ),
            )


def run_test_automation_project_018_forward_restore_reapply_and_atomic_config(case):
    from shared.automation_plugin_repository import AutomationPluginRepository

    database = case.project_authorization_database
    (
        code_owned_resource_configs,
        required_existing_resource_configs,
        reviewed_resource_keys,
        _old_reviewed_resource_keys,
    ) = _project_resource_contract()
    reviewed_resource_placeholders = ", ".join(
        "%s" for _resource_key in reviewed_resource_keys
    )
    original_resource_configs = {
        "phase7.yunda_dispatch_forecast_bitable": {
            "base_token": "integration-custom-base",
            "table_id": "integration-custom-table",
        },
        "phase7.delivery_status_bitable": {
            "base_token": "integration-delivery-base",
            "table_id": "integration-delivery-table",
            "view_id": "integration-delivery-view-id",
            "view_name": "integration-delivery-view-name",
        },
        "phase7.delivery_status_webhook": dict(
            code_owned_resource_configs["phase7.delivery_status_webhook"]
        ),
        "automation.feishu_route.send_order": dict(
            code_owned_resource_configs["automation.feishu_route.send_order"]
        ),
        **required_existing_resource_configs,
    }
    original_created_at = datetime(2025, 1, 2, 3, 4, 5)
    original_updated_at = datetime(2025, 2, 3, 4, 5, 6)
    seed_legacy_r7_departure_task(case, database)
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO workflow_resources (
                    resource_key, config_json, source, updated_at, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                tuple(
                    (
                        resource_key,
                        json.dumps(config, separators=(",", ":")),
                        "integration-preexisting",
                        original_updated_at,
                        original_created_at,
                    )
                    for resource_key, config in original_resource_configs.items()
                ),
            )
    case._run_migrations(database)
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            migrated_r7 = select_legacy_r7_departure_task(
                cursor,
                include_identity=True,
            )
            assert_legacy_r7_departure_state(case, migrated_r7)
            case.assertEqual(
                migrated_r7["automation_id"],
                LEGACY_R7_DEPARTURE_TASK_ID,
            )
            case.assertEqual(migrated_r7["automation_generation"], 1)
            cursor.execute(
                f"""
                SELECT
                    resource_key,
                    config_json,
                    source,
                    updated_at,
                    created_at,
                    configuration_version,
                    BINARY config_sha256 = BINARY SHA2(
                        CAST(config_json AS CHAR CHARACTER SET utf8mb4),
                        256
                    ) AS hash_valid
                FROM workflow_resources
                WHERE BINARY resource_key IN ({reviewed_resource_placeholders})
                ORDER BY BINARY resource_key
                """,
                reviewed_resource_keys,
            )
            reviewed_resources = {
                row["resource_key"]: row for row in cursor.fetchall()
            }
            case.assertEqual(set(reviewed_resources), set(reviewed_resource_keys))
            for resource_key, row in reviewed_resources.items():
                case.assertEqual(row["configuration_version"], 1)
                case.assertEqual(row["hash_valid"], 1)
                if resource_key in original_resource_configs:
                    expected_config = dict(original_resource_configs[resource_key])
                    expected_kind = (
                        code_owned_resource_configs.get(resource_key, {}).get(
                            "resource_kind"
                        )
                        or REQUIRED_PROJECT_RESOURCE_KINDS[resource_key]
                    )
                    expected_config.setdefault("resource_kind", expected_kind)
                    expected_source = "integration-preexisting"
                    case.assertEqual(row["updated_at"], original_updated_at)
                    case.assertEqual(row["created_at"], original_created_at)
                else:
                    expected_config = code_owned_resource_configs[resource_key]
                    expected_source = "migration-018-reviewed-builtin"
                case.assertEqual(json.loads(row["config_json"]), expected_config)
                case.assertEqual(row["source"], expected_source)
            cursor.execute(
                """
                SELECT
                    resource_key,
                    existed_before,
                    config_json,
                    source,
                    migration_config_sha256 IS NOT NULL AS captured
                FROM automation_project_resource_backup_018
                ORDER BY BINARY resource_key
                """
            )
            resource_backups = {
                row["resource_key"]: row for row in cursor.fetchall()
            }
            case.assertEqual(set(resource_backups), set(reviewed_resource_keys))
            for resource_key, row in resource_backups.items():
                expected_existed = resource_key in original_resource_configs
                case.assertEqual(
                    bool(row["existed_before"]),
                    expected_existed,
                )
                case.assertEqual(row["captured"], 1)
                if expected_existed:
                    case.assertEqual(
                        json.loads(row["config_json"]),
                        original_resource_configs[resource_key],
                    )
                    case.assertEqual(row["source"], "integration-preexisting")
                else:
                    case.assertIsNone(row["config_json"])
                    case.assertIsNone(row["source"])
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM automation_project_reviewed_resource_map_018)
                        AS map_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018)
                        AS backup_count,
                    (SELECT COUNT(*)
                     FROM workflow_resources
                     WHERE BINARY resource_key IN (%s, %s))
                        AS deferred_r7_count
                """,
                tuple(sorted(DEFERRED_R7_RESOURCE_KEYS)),
            )
            resource_counts = cursor.fetchone()
            case.assertEqual(resource_counts["map_count"], 26)
            case.assertEqual(resource_counts["backup_count"], 26)
            case.assertEqual(resource_counts["deferred_r7_count"], 0)
    with case._connection(database) as connection:
        repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        manifest = {
            "allowed_entrypoints": ["scheduler", "console"],
            "scheduling": {"supported": True},
            "runtime": {
                "kind": "core_tool_ref",
                "tool_name": "sync_scan_codes",
            },
            "tool_contract": {"name": "sync_scan_codes"},
        }
        digest_fields = {
            "package_sha256": "1" * 64,
            "manifest_sha256": "2" * 64,
            "tool_contract_sha256": "3" * 64,
            "config_schema_sha256": "4" * 64,
            "allowed_entrypoints_sha256": "5" * 64,
            "invocation_contracts_sha256": "6" * 64,
            "worker_requirement_sha256": "7" * 64,
            "runtime_sha256": "8" * 64,
            "scheduling_sha256": "9" * 64,
            "install_root_metadata_sha256": "a" * 64,
        }
        repository.register_package_version(
            package={
                "plugin_id": "integration_scan_plugin",
                "display_name": "Integration Scan",
                "description": "test-only signed package",
            },
            version={
                "version": "1.0.0",
                **digest_fields,
                "manifest_json": manifest,
                "project_full_auto_allowed": True,
                "trust_source": "ed25519_first_party",
                "install_root_metadata_json": {},
                "installed_by_actor_id": "integration-admin",
            },
        )
        with case.assertRaises(ValueError):
            repository.register_package_version(
                package={
                    "plugin_id": "invalid_trust_plugin",
                    "display_name": "Invalid Trust",
                    "description": "must fail before SQL",
                },
                version={
                    "version": "1.0.0",
                    **digest_fields,
                    "manifest_json": manifest,
                    "project_full_auto_allowed": True,
                    "trust_source": "ED25519_FIRST_PARTY",
                    "install_root_metadata_json": {},
                    "installed_by_actor_id": "integration-admin",
                },
            )
        repository.install_project_instance(
            {
                "automation_id": "integration_scan_instance",
                "plugin_id": "integration_scan_plugin",
                "plugin_version": "1.0.0",
                "display_name": "Integration Scan Instance",
                "install_request_id": str(uuid4()),
                "install_payload_sha256": "b" * 64,
                "installed_by_actor_id": "integration-admin",
                "migration_authority": False,
            }
        )
        repository.initialize_project_config(
            "integration_scan_instance",
            enabled_entrypoints=("scheduler", "console"),
        )
        compiled = {
            "scheduler": {
                "arguments": {"account_id": "integration-account"},
                "dynamic_resolvers": {},
            },
            "console": {
                "arguments": {"account_id": "integration-account"},
                "dynamic_resolvers": {},
            },
        }
        saved = repository.save_project_config(
            "integration_scan_instance",
            config={},
            account_bindings={"primary": "integration-account"},
            resource_bindings={},
            enabled_entrypoints=("scheduler", "console"),
            schedule={
                "kind": "daily_times",
                "times": ["08:05", "18:30"],
                "enabled": True,
            },
            compiled_invocations=compiled,
            device_binding=None,
            actor_id="integration-admin",
            actor_role="super_admin",
            request_id=str(uuid4()),
            expected_project_configuration_version=1,
        )
        case.assertEqual(saved["config_version"], 2)
        case.assertEqual(
            saved["schedule"],
            {
                "kind": "daily_times",
                "times": ["08:05", "18:30"],
                "enabled": True,
            },
        )
        connection.commit()

    def save_schedule(expected_version, schedule):
        with case._connection(database) as connection:
            repository = AutomationPluginRepository(
                connection,
                cursor_factory=case.pymysql.cursors.DictCursor,
            )
            result = repository.save_project_config(
                "integration_scan_instance",
                config={},
                account_bindings={"primary": "integration-account"},
                resource_bindings={},
                enabled_entrypoints=("scheduler", "console"),
                schedule=schedule,
                compiled_invocations=compiled,
                device_binding=None,
                actor_id="integration-admin",
                actor_role="super_admin",
                request_id=str(uuid4()),
                expected_project_configuration_version=expected_version,
            )
            connection.commit()
            return result

    startup = save_schedule(
        2,
        {"kind": "startup", "times": [], "enabled": False},
    )
    case.assertEqual(startup["schedule"]["kind"], "startup")
    none = save_schedule(
        3,
        {"kind": "none", "times": [], "enabled": False},
    )
    case.assertEqual(
        none["schedule"],
        {"kind": "none", "times": [], "enabled": False},
    )

    with case._connection(database) as connection:
        repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        repository.save_project_config(
            "integration_scan_instance",
            config={},
            account_bindings={"primary": "integration-account"},
            resource_bindings={},
            enabled_entrypoints=("scheduler", "console"),
            schedule={
                "kind": "daily_times",
                "times": ["09:00"],
                "enabled": True,
            },
            compiled_invocations=compiled,
            device_binding=None,
            actor_id="integration-admin",
            actor_role="super_admin",
            request_id=str(uuid4()),
            expected_project_configuration_version=4,
        )
        connection.rollback()
    with case._connection(database) as connection:
        repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        rolled_back = repository.get_project_config("integration_scan_instance")
        case.assertEqual(rolled_back["config_version"], 4)
        case.assertEqual(rolled_back["schedule"]["kind"], "none")

    # The exact restore owns every 018 object and can be reapplied cleanly.
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM automation_project_events WHERE automation_id=%s",
                ("integration_scan_instance",),
            )
            cursor.execute(
                "DELETE FROM automation_project_policy_events WHERE automation_id=%s",
                ("integration_scan_instance",),
            )
            cursor.execute(
                "DELETE FROM automation_project_policies WHERE automation_id=%s",
                ("integration_scan_instance",),
            )
            cursor.execute(
                "DELETE FROM automation_project_configs WHERE automation_id=%s",
                ("integration_scan_instance",),
            )
            cursor.execute(
                "DELETE FROM scheduled_tasks WHERE automation_id=%s",
                ("integration_scan_instance",),
            )
            cursor.execute(
                "DELETE FROM automation_projects WHERE automation_id=%s",
                ("integration_scan_instance",),
            )
            cursor.execute(
                "DELETE FROM automation_plugin_versions WHERE plugin_id=%s",
                ("integration_scan_plugin",),
            )
            cursor.execute(
                "DELETE FROM automation_plugin_packages WHERE plugin_id=%s",
                ("integration_scan_plugin",),
            )
    with patch.dict(os.environ, case._environment(database), clear=False):
        case.assertEqual(0, case.runner.restore_automation_project_authorization())
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            restored_r7 = select_legacy_r7_departure_task(
                cursor,
                include_identity=False,
            )
            assert_legacy_r7_departure_state(case, restored_r7)
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='scheduled_tasks' "
                "AND COLUMN_NAME='automation_id'",
                (database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 0)

            cursor.execute(
                f"""
                SELECT resource_key, config_json, source, updated_at, created_at
                FROM workflow_resources
                WHERE BINARY resource_key IN ({reviewed_resource_placeholders})
                ORDER BY BINARY resource_key
                """,
                reviewed_resource_keys,
            )
            restored_resources = cursor.fetchall()
            case.assertEqual(len(restored_resources), len(original_resource_configs))
            restored_by_key = {
                row["resource_key"]: row for row in restored_resources
            }
            case.assertEqual(set(restored_by_key), set(original_resource_configs))
            for resource_key, original_config in original_resource_configs.items():
                restored_resource = restored_by_key[resource_key]
                case.assertEqual(
                    json.loads(restored_resource["config_json"]),
                    original_config,
                )
                case.assertEqual(
                    restored_resource["source"],
                    "integration-preexisting",
                )
                case.assertEqual(
                    restored_resource["updated_at"],
                    original_updated_at,
                )
                case.assertEqual(
                    restored_resource["created_at"],
                    original_created_at,
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS n
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=%s
                  AND TABLE_NAME IN (
                    'automation_project_resource_backup_018',
                    'automation_project_reviewed_resource_map_018'
                  )
                """,
                (database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 0)
    case._run_migrations(database)
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            reapplied_r7 = select_legacy_r7_departure_task(
                cursor,
                include_identity=True,
            )
            assert_legacy_r7_departure_state(case, reapplied_r7)
            case.assertEqual(
                reapplied_r7["automation_id"],
                LEGACY_R7_DEPARTURE_TASK_ID,
            )
            case.assertEqual(reapplied_r7["automation_generation"], 1)
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='scheduled_tasks' "
                "AND COLUMN_NAME='automation_id' AND IS_NULLABLE='NO'",
                (database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 1)
            cursor.execute(
                f"""
                SELECT
                    (SELECT COUNT(*)
                     FROM workflow_resources
                     WHERE BINARY resource_key IN ({reviewed_resource_placeholders}))
                        AS n,
                    (SELECT COUNT(*)
                     FROM workflow_resources
                     WHERE BINARY resource_key IN (%s, %s))
                        AS deferred_r7_count
                """,
                (*reviewed_resource_keys, *tuple(sorted(DEFERRED_R7_RESOURCE_KEYS))),
            )
            reapplied_counts = cursor.fetchone()
            case.assertEqual(reapplied_counts["n"], 26)
            case.assertEqual(reapplied_counts["deferred_r7_count"], 0)
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM automation_project_reviewed_resource_map_018)
                        AS map_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018)
                        AS backup_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018
                     WHERE migration_config_sha256 IS NOT NULL)
                        AS hashed_backup_count
                """
            )
            reapplied_backup_counts = cursor.fetchone()
            case.assertEqual(reapplied_backup_counts["map_count"], 26)
            case.assertEqual(reapplied_backup_counts["backup_count"], 26)
            case.assertEqual(reapplied_backup_counts["hashed_backup_count"], 26)


def run_test_automation_project_018_partial_rerun_is_safe(case):
    partial_database = case.project_authorization_partial_database
    (
        code_owned_resource_configs,
        required_existing_resource_configs,
        reviewed_resource_keys,
        old_reviewed_resource_keys,
    ) = _project_resource_contract()
    expanded_resource_keys = tuple(
        sorted(set(reviewed_resource_keys) - old_reviewed_resource_keys)
    )
    expanded_resource_placeholders = ", ".join(
        "%s" for _resource_key in expanded_resource_keys
    )
    reviewed_resource_placeholders = ", ".join(
        "%s" for _resource_key in reviewed_resource_keys
    )
    seed_legacy_r7_departure_task(case, partial_database)
    legacy_pending_key = "phase7.pending_arrivals_sheet"
    legacy_pending_config = {
        "spreadsheet_token": "integration-legacy-pending",
        "sheet_id": "LegacyPending",
        "snapshot_range": "LegacyPending!A1:S5000",
    }
    legacy_pending_source = "integration-legacy-pending-source"
    legacy_pending_created_at = datetime(2025, 3, 4, 5, 6, 7)
    legacy_pending_updated_at = datetime(2025, 4, 5, 6, 7, 8)
    migration = next(
        path
        for version, path in case.runner.discover_migrations()
        if version == "018"
    )
    statements = case.runner.split_sql_statements(
        migration.read_text(encoding="utf-8")
    )
    # Required-existing resources fail before the first ALTER and are never
    # synthesized by 018. The migration-owned review map may remain after this
    # expected failure and must be safe to reuse on the next pass.
    with case.assertRaises(Exception):
        with case._connection(partial_database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for statement in statements:
                    cursor.execute(statement)
    with case._connection(partial_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS n
                FROM automation_project_reviewed_resource_map_018 AS reviewed
                INNER JOIN workflow_resources AS resource
                  ON BINARY resource.resource_key = BINARY reviewed.resource_key
                WHERE reviewed.materialize_missing = FALSE
                """
            )
            case.assertEqual(cursor.fetchone()["n"], 0)
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='scheduled_tasks' "
                "AND COLUMN_NAME='automation_id'",
                (partial_database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 0)
            # Simulate the exact autocommitted 70-row map left by the previous
            # release candidate. The corrected rerun must add only the missing
            # deferred historical identity.
            cursor.execute(
                """
                DELETE FROM automation_project_reviewed_schedule_map_018
                WHERE BINARY task_id = BINARY %s
                """,
                (LEGACY_R7_DEPARTURE_TASK_ID,),
            )
            cursor.execute(
                """
                SELECT COUNT(*) AS n
                FROM automation_project_reviewed_schedule_map_018
                """
            )
            case.assertEqual(cursor.fetchone()["n"], 70)
            # Simulate the first release candidate's autocommitted 15-row map
            # after its required-resource preflight failed.
            cursor.execute(
                f"""
                DELETE FROM automation_project_reviewed_resource_map_018
                WHERE BINARY resource_key IN ({expanded_resource_placeholders})
                """,
                expanded_resource_keys,
            )
            case.assertEqual(cursor.rowcount, 12)
            cursor.execute(
                """
                INSERT INTO automation_project_reviewed_resource_map_018 (
                    resource_key, expected_kind, materialize_missing,
                    default_config_json
                ) VALUES (%s, 'feishu_sheet', FALSE, NULL)
                """,
                (legacy_pending_key,),
            )
            cursor.execute(
                """
                INSERT INTO workflow_resources (
                    resource_key, config_json, source, updated_at, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    legacy_pending_key,
                    json.dumps(legacy_pending_config, separators=(",", ":")),
                    legacy_pending_source,
                    legacy_pending_updated_at,
                    legacy_pending_created_at,
                ),
            )
            cursor.execute(
                """
                SELECT COUNT(*) AS n
                FROM automation_project_reviewed_resource_map_018
                """
            )
            case.assertEqual(cursor.fetchone()["n"], 15)
    case._seed_required_project_resources(partial_database)
    with case._connection(partial_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
                if "DEALLOCATE PREPARE cp018_add_automation_id_stmt" in statement:
                    break
            # Simulate the legacy candidate completing its old fourteen-row
            # resource pass before a later crash. The new migration has
            # already extended the backup to current26, so hashing exactly the
            # old14 plus the legacy pending row creates the only accepted
            # legacy15 -> current27 transition: old hashes set, new12 NULL.
            cursor.execute(
                """
                INSERT INTO automation_project_resource_backup_018 (
                    resource_key, existed_before, config_json, source,
                    updated_at, created_at, migration_config_sha256
                )
                SELECT
                    resource_key, TRUE, config_json, source,
                    updated_at, created_at, NULL
                FROM workflow_resources
                WHERE BINARY resource_key = BINARY %s
                """,
                (legacy_pending_key,),
            )
            cursor.executemany(
                """
                INSERT INTO workflow_resources (
                    resource_key, config_json, source
                ) VALUES (%s, %s, 'migration-018-reviewed-builtin')
                """,
                tuple(
                    (
                        resource_key,
                        json.dumps(
                            code_owned_resource_configs[resource_key],
                            separators=(",", ":"),
                        ),
                    )
                    for resource_key in sorted(OLD_CODE_OWNED_RESOURCE_KEYS)
                ),
            )
            old_expected_kinds = {
                **{
                    resource_key: config["resource_kind"]
                    for resource_key, config in code_owned_resource_configs.items()
                    if resource_key in OLD_CODE_OWNED_RESOURCE_KEYS
                },
                **REQUIRED_PROJECT_RESOURCE_KINDS,
                legacy_pending_key: "feishu_sheet",
            }
            for resource_key, expected_kind in old_expected_kinds.items():
                cursor.execute(
                    """
                    UPDATE workflow_resources
                    SET config_json = JSON_SET(
                        config_json,
                        '$.resource_kind',
                        %s
                    )
                    WHERE BINARY resource_key = BINARY %s
                    """,
                    (expected_kind, resource_key),
                )
            cursor.execute(
                """
                INSERT INTO automation_project_reviewed_resource_map_018 (
                    resource_key, expected_kind, materialize_missing,
                    default_config_json
                ) VALUES (%s, 'feishu_sheet', FALSE, NULL)
                """,
                (legacy_pending_key,),
            )
            legacy_hashed_resource_keys = tuple(
                sorted({*old_reviewed_resource_keys, legacy_pending_key})
            )
            legacy_hashed_placeholders = ", ".join(
                "%s" for _resource_key in legacy_hashed_resource_keys
            )
            cursor.execute(
                f"""
                UPDATE automation_project_resource_backup_018 AS backup
                INNER JOIN workflow_resources AS resource
                  ON BINARY resource.resource_key = BINARY backup.resource_key
                SET backup.migration_config_sha256 = SHA2(
                    CAST(resource.config_json AS CHAR CHARACTER SET utf8mb4),
                    256
                )
                WHERE BINARY backup.resource_key IN (
                    {legacy_hashed_placeholders}
                )
                """,
                legacy_hashed_resource_keys,
            )
            case.assertEqual(cursor.rowcount, 15)
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM automation_project_reviewed_schedule_map_018)
                        AS schedule_map_count,
                    (SELECT COUNT(*)
                     FROM automation_project_reviewed_resource_map_018)
                        AS map_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018)
                        AS backup_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018
                     WHERE migration_config_sha256 IS NOT NULL)
                        AS hashed_backup_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018
                     WHERE migration_config_sha256 IS NULL)
                        AS unhashed_backup_count
                """
            )
            partial_counts = cursor.fetchone()
            case.assertEqual(partial_counts["schedule_map_count"], 71)
            case.assertEqual(partial_counts["map_count"], 27)
            case.assertEqual(partial_counts["backup_count"], 27)
            case.assertEqual(partial_counts["hashed_backup_count"], 15)
            case.assertEqual(partial_counts["unhashed_backup_count"], 12)
            cursor.execute(
                f"""
                SELECT resource_key
                FROM automation_project_resource_backup_018
                WHERE migration_config_sha256 IS NULL
                  AND BINARY resource_key IN ({expanded_resource_placeholders})
                ORDER BY BINARY resource_key
                """,
                expanded_resource_keys,
            )
            case.assertEqual(
                tuple(row["resource_key"] for row in cursor.fetchall()),
                expanded_resource_keys,
            )
    # Simulate an autocommitted older 018 candidate that created the bootstrap
    # item table before retained source snapshots existed. Persisted items make
    # that evidence impossible to recover and must stop the rerun before ALTER.
    with case._connection(partial_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE automation_project_bootstrap_items_018 (
                    automation_id VARCHAR(128) NOT NULL,
                    initial_mode VARCHAR(32) NOT NULL,
                    source_set_sha256 CHAR(64) NOT NULL,
                    policy_version INT UNSIGNED NOT NULL,
                    completed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                    PRIMARY KEY (automation_id),
                    CONSTRAINT chk_automation_project_bootstrap_mode CHECK (
                        initial_mode IN (
                            'REQUIRE_EACH_RUN', 'LEGACY_SCHEDULE_ONLY'
                        )
                    )
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                INSERT INTO automation_project_bootstrap_items_018 (
                    automation_id, initial_mode, source_set_sha256,
                    policy_version
                ) VALUES ('unrecoverable_partial', 'REQUIRE_EACH_RUN', %s, 1)
                """,
                ("0" * 64,),
            )
    with case.assertRaises(Exception):
        case._run_migrations(partial_database)
    with case._connection(partial_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s "
                "AND TABLE_NAME='automation_project_bootstrap_items_018' "
                "AND COLUMN_NAME='source_snapshot_json'",
                (partial_database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 0)
            cursor.execute(
                "DELETE FROM automation_project_bootstrap_items_018 "
                "WHERE automation_id='unrecoverable_partial'"
            )

    # Once the old partial table is proven empty, the rerun may add the JSON
    # evidence column, make it required, and install its object-shape check.
    case._run_migrations(partial_database)
    with case._connection(partial_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s "
                "AND TABLE_NAME='automation_project_bootstrap_items_018' "
                "AND COLUMN_NAME='source_snapshot_json' "
                "AND DATA_TYPE='json' AND IS_NULLABLE='NO'",
                (partial_database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 1)
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.TABLE_CONSTRAINTS "
                "WHERE TABLE_SCHEMA=%s "
                "AND TABLE_NAME='automation_project_bootstrap_items_018' "
                "AND CONSTRAINT_NAME="
                "'chk_automation_project_bootstrap_source_snapshot' "
                "AND CONSTRAINT_TYPE='CHECK'",
                (partial_database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 1)
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='scheduled_tasks' "
                "AND COLUMN_NAME='automation_id' AND IS_NULLABLE='NO'",
                (partial_database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 1)
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM automation_project_reviewed_schedule_map_018)
                        AS schedule_map_count,
                    (SELECT COUNT(*)
                     FROM automation_project_reviewed_resource_map_018)
                        AS map_count,
                    (SELECT COUNT(*)
                     FROM automation_project_reviewed_resource_map_018
                     WHERE BINARY resource_key = BINARY %s)
                        AS obsolete_map_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018)
                        AS backup_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018
                     WHERE BINARY resource_key = BINARY %s
                       AND existed_before = TRUE
                       AND migration_config_sha256 IS NOT NULL)
                        AS legacy_backup_count,
                    (SELECT COUNT(*)
                     FROM automation_project_resource_backup_018
                     WHERE migration_config_sha256 IS NOT NULL)
                        AS hashed_backup_count,
                    (SELECT COUNT(*)
                     FROM workflow_resources
                     WHERE BINARY resource_key IN (%s, %s))
                        AS deferred_r7_count
                """,
                (
                    legacy_pending_key,
                    legacy_pending_key,
                    *tuple(sorted(DEFERRED_R7_RESOURCE_KEYS)),
                ),
            )
            rerun_counts = cursor.fetchone()
            case.assertEqual(rerun_counts["schedule_map_count"], 71)
            case.assertEqual(rerun_counts["map_count"], 26)
            case.assertEqual(rerun_counts["obsolete_map_count"], 0)
            case.assertEqual(rerun_counts["backup_count"], 27)
            case.assertEqual(rerun_counts["legacy_backup_count"], 1)
            case.assertEqual(rerun_counts["hashed_backup_count"], 27)
            case.assertEqual(rerun_counts["deferred_r7_count"], 0)
            cursor.execute(
                f"""
                SELECT resource_key, config_json, source
                FROM workflow_resources
                WHERE BINARY resource_key IN ({reviewed_resource_placeholders})
                ORDER BY BINARY resource_key
                """,
                reviewed_resource_keys,
            )
            rerun_resources = {
                row["resource_key"]: row for row in cursor.fetchall()
            }
            case.assertEqual(set(rerun_resources), set(reviewed_resource_keys))
            for resource_key in code_owned_resource_configs:
                case.assertEqual(
                    json.loads(rerun_resources[resource_key]["config_json"]),
                    code_owned_resource_configs[resource_key],
                )
                case.assertEqual(
                    rerun_resources[resource_key]["source"],
                    "migration-018-reviewed-builtin",
                )
            for resource_key, config in required_existing_resource_configs.items():
                case.assertEqual(
                    json.loads(rerun_resources[resource_key]["config_json"]),
                    {
                        **config,
                        "resource_kind": REQUIRED_PROJECT_RESOURCE_KINDS[
                            resource_key
                        ],
                    },
                )
                case.assertEqual(
                    rerun_resources[resource_key]["source"],
                    "integration-required-resource",
                )
            migrated_r7 = select_legacy_r7_departure_task(
                cursor,
                include_identity=True,
            )
            assert_legacy_r7_departure_state(case, migrated_r7)
            case.assertEqual(
                migrated_r7["automation_id"],
                LEGACY_R7_DEPARTURE_TASK_ID,
            )
            case.assertEqual(migrated_r7["automation_generation"], 1)

    with patch.dict(os.environ, case._environment(partial_database), clear=False):
        case.assertEqual(0, case.runner.restore_automation_project_authorization())
    with case._connection(partial_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            restored_r7 = select_legacy_r7_departure_task(
                cursor,
                include_identity=False,
            )
            assert_legacy_r7_departure_state(case, restored_r7)
            cursor.execute(
                """
                SELECT config_json, source, updated_at, created_at
                FROM workflow_resources
                WHERE BINARY resource_key = BINARY %s
                """,
                (legacy_pending_key,),
            )
            restored_pending = cursor.fetchone()
            case.assertIsNotNone(restored_pending)
            case.assertEqual(
                json.loads(restored_pending["config_json"]),
                legacy_pending_config,
            )
            case.assertEqual(restored_pending["source"], legacy_pending_source)
            case.assertEqual(
                restored_pending["updated_at"],
                legacy_pending_updated_at,
            )
            case.assertEqual(
                restored_pending["created_at"],
                legacy_pending_created_at,
            )
            cursor.execute(
                f"""
                SELECT resource_key, config_json, source
                FROM workflow_resources
                WHERE BINARY resource_key IN ({reviewed_resource_placeholders})
                ORDER BY BINARY resource_key
                """,
                reviewed_resource_keys,
            )
            restored_reviewed = {
                row["resource_key"]: row for row in cursor.fetchall()
            }
            case.assertEqual(
                set(restored_reviewed),
                set(required_existing_resource_configs),
            )
            for resource_key, config in required_existing_resource_configs.items():
                case.assertEqual(
                    json.loads(restored_reviewed[resource_key]["config_json"]),
                    config,
                )
                case.assertEqual(
                    restored_reviewed[resource_key]["source"],
                    "integration-required-resource",
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS n
                FROM workflow_resources
                WHERE BINARY resource_key IN (%s, %s)
                """,
                tuple(sorted(DEFERRED_R7_RESOURCE_KEYS)),
            )
            case.assertEqual(cursor.fetchone()["n"], 0)
            cursor.execute(
                """
                SELECT COUNT(*) AS n
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=%s
                  AND TABLE_NAME IN (
                    'automation_project_resource_backup_018',
                    'automation_project_reviewed_resource_map_018'
                  )
                """,
                (partial_database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 0)


    # A pre-existing route remains owned by its source, but it must match the
    # reviewed trusted entrypoint exactly. A valid-looking wrong route is not
    # overwritten and the migration fails closed before recording 018.
    mismatched_route_key = "automation.feishu_route.send_order"
    mismatched_route_config = {
        "resource_kind": "feishu_route",
        "route_key": "builtin.scan_codes",
    }
    with case._connection(partial_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO workflow_resources (
                    resource_key, config_json, source, updated_at, created_at
                ) VALUES (%s, %s, 'integration-route-mismatch', %s, %s)
                """,
                (
                    mismatched_route_key,
                    json.dumps(mismatched_route_config, separators=(",", ":")),
                    legacy_pending_updated_at,
                    legacy_pending_created_at,
                ),
            )
    with case.assertRaises(Exception):
        case._run_migrations(partial_database)
    with case._connection(partial_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT config_json, source, updated_at, created_at
                FROM workflow_resources
                WHERE BINARY resource_key = BINARY %s
                """,
                (mismatched_route_key,),
            )
            rejected_route = cursor.fetchone()
            case.assertEqual(
                json.loads(rejected_route["config_json"]),
                mismatched_route_config,
            )
            case.assertEqual(rejected_route["source"], "integration-route-mismatch")
            case.assertEqual(rejected_route["updated_at"], legacy_pending_updated_at)
            case.assertEqual(rejected_route["created_at"], legacy_pending_created_at)
            cursor.execute(
                "SELECT COUNT(*) AS n FROM schema_migrations WHERE version='018'"
            )
            case.assertEqual(cursor.fetchone()["n"], 0)


def run_test_automation_worker_dispatch_is_durable_exact_device_and_replay_safe(case):
    from shared.automation_plugin_repository import AutomationPluginRepository

    database = case.worker_dispatch_database
    case._run_migrations(database)
    device_public_key = b"k" * 32
    fingerprint = hashlib.sha256(device_public_key).hexdigest()
    job_id = str(uuid4())
    with case._connection(database) as connection:
        repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        repository.register_package_version(
            package={
                "plugin_id": "worker_dispatch_plugin",
                "display_name": "Worker Dispatch",
                "description": "test-only signed Worker package",
            },
            version={
                "version": "1.0.0",
                "package_sha256": "1" * 64,
                "manifest_sha256": "2" * 64,
                "manifest_json": {"runtime": {"kind": "python_subprocess"}},
                "tool_contract_sha256": "3" * 64,
                "config_schema_sha256": "4" * 64,
                "allowed_entrypoints_sha256": "5" * 64,
                "invocation_contracts_sha256": "6" * 64,
                "worker_requirement_sha256": "7" * 64,
                "runtime_sha256": "8" * 64,
                "scheduling_sha256": "9" * 64,
                "project_full_auto_allowed": True,
                "trust_source": "ed25519_first_party",
                "install_root_metadata_json": {},
                "install_root_metadata_sha256": "a" * 64,
                "installed_by_actor_id": "integration-admin",
            },
        )
        repository.install_project_instance(
            {
                "automation_id": "worker_dispatch_instance",
                "plugin_id": "worker_dispatch_plugin",
                "plugin_version": "1.0.0",
                "display_name": "Worker Dispatch Instance",
                "install_request_id": str(uuid4()),
                "install_payload_sha256": "b" * 64,
                "installed_by_actor_id": "integration-admin",
                "migration_authority": False,
            }
        )
        repository.pair_device(
            {
                "device_id": "office-device",
                "display_name": "Office Device",
                "platform": "windows",
                "agent_version": "1.0.0",
                "identity_json": {
                    "device_key_id": "office-device-key",
                    "ed25519_public_key_base64": base64.b64encode(
                        device_public_key
                    ).decode("ascii"),
                    "tls_client_certificate_sha256": "e" * 64,
                },
                "paired_public_key_fingerprint": fingerprint,
                "capabilities_json": {"interactive": True},
            }
        )
        with connection.cursor() as cursor:
            hashes = [character * 64 for character in "cdef012345678"]
            cursor.execute(
                """
                INSERT INTO automation_project_generations(
                    automation_id, generation, request_id, state,
                    plugin_id, plugin_version, package_sha256,
                    manifest_sha256, trust_source, project_config_sha256,
                    account_bindings_sha256, resource_bindings_sha256,
                    device_binding_sha256, schedule_sha256,
                    core_registry_sha256, tool_contract_sha256,
                    invocation_contracts_sha256,
                    compiled_invocations_sha256, runtime_descriptor_sha256,
                    governance_anchor_sha256, policy_contract_sha256,
                    enabled_entrypoints_sha256, snapshot_json,
                    snapshot_sha256, committed_at
                ) VALUES (
                    %s, 1, %s, 'COMMITTED', %s, '1.0.0',
                    %s, %s, 'ed25519_first_party',
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, '{}', %s, NOW(6)
                )
                """,
                (
                    "worker_dispatch_instance",
                    str(uuid4()),
                    "worker_dispatch_plugin",
                    "1" * 64,
                    "2" * 64,
                    *hashes,
                    "0" * 64,
                ),
            )
            cursor.execute(
                """
                UPDATE automation_projects
                SET enabled=TRUE, state='ENABLED', target_generation=1,
                    committed_generation=1, reconcile_state='STABLE'
                WHERE automation_id='worker_dispatch_instance'
                """
            )
            cursor.execute(
                """
                UPDATE automation_worker_devices
                SET service_state='ONLINE',
                    interactive_session_state='AVAILABLE'
                WHERE device_id='office-device'
                """
            )
        repository.enqueue_job(
            {
                "job_id": job_id,
                "automation_id": "worker_dispatch_instance",
                "automation_generation": 1,
                "plugin_id": "worker_dispatch_plugin",
                "plugin_version": "1.0.0",
                "request_id": str(uuid4()),
                "job_type": "INVOKE",
                "payload_json": {"action": "run"},
                "worker_requirement_json": {"required": True},
                "operation_type": "external_write",
                "requires_interactive_session": True,
                "target_device_id": "office-device",
                "max_attempts": 1,
                "deadline_at": datetime.utcnow() + timedelta(minutes=5),
            },
            release_hold=False,
        )
        connection.commit()

    def sign_dispatch(*, device_id, sequence, message_id, body):
        return {
            "schema_version": 1,
            "message_id": message_id,
            "device_id": device_id,
            "sequence": sequence,
            "issued_at": "2026-08-15T08:00:00Z",
            "expires_at": "2026-08-15T08:05:00Z",
            "kind": "COMMAND",
            "body": body,
            "key_id": "server-key",
            "signature": "signed",
        }

    with case._connection(database) as connection:
        repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        claimed = repository.claim_dispatch_envelopes(
            device_id="office-device",
            worker_id="dispatcher-one",
            limit=1,
            lease_seconds=60,
            release_hold=False,
            release_sha="abcdef1",
            envelope_factory=sign_dispatch,
        )
        case.assertEqual(len(claimed), 1)
        first_envelope = claimed[0]["dispatch_envelope_json"]
        connection.commit()

    # An HTTP response loss returns the exact persisted envelope, not a
    # newly signed authorization or a second attempt.
    with case._connection(database) as connection:
        repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        replayed = repository.claim_dispatch_envelopes(
            device_id="office-device",
            worker_id="dispatcher-one",
            limit=1,
            lease_seconds=60,
            release_hold=False,
            release_sha="abcdef1",
            envelope_factory=sign_dispatch,
        )
        case.assertEqual(replayed[0]["dispatch_envelope_json"], first_envelope)
        case.assertEqual(replayed[0]["attempt_count"], 1)

    dispatch = first_envelope["body"]["dispatch"]
    result_envelope = {
        "schema_version": 1,
        "message_id": str(uuid4()),
        "device_id": "office-device",
        "sequence": 0,
        "issued_at": "2026-08-15T08:00:01Z",
        "expires_at": "2026-08-15T08:05:01Z",
        "kind": "JOB_STATUS",
        "body": {
            "job_id": job_id,
            "dispatch_message_id": first_envelope["message_id"],
            "dispatch_authorization_id": dispatch["authorization_id"],
            "status": "FAILED",
            "process_confirmed": False,
            "result": {},
            "error_code": "PROCESS_EXITED",
        },
        "key_id": "device-key",
        "signature": "signed",
    }
    with case._connection(database) as connection:
        repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        unknown = repository.record_worker_job_status(
            result_envelope,
            principal_device_id="office-device",
            paired_public_key_fingerprint=fingerprint,
            signature_verified=True,
        )
        case.assertEqual(unknown["status"], "OUTCOME_UNKNOWN")
        connection.commit()

    with case._connection(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, attempt_count FROM automation_worker_jobs "
                "WHERE job_id=%s",
                (job_id,),
            )
            row = cursor.fetchone()
            case.assertEqual(row["status"], "OUTCOME_UNKNOWN")
            case.assertEqual(row["attempt_count"], 1)
            cursor.execute(
                "SELECT COUNT(*) AS n FROM automation_worker_job_messages "
                "WHERE job_id=%s",
                (job_id,),
            )
            case.assertEqual(cursor.fetchone()["n"], 1)
            cursor.execute(
                "SELECT inbound_sequence, last_inbound_message_id "
                "FROM automation_worker_devices WHERE device_id=%s",
                ("office-device",),
            )
            device = cursor.fetchone()
            case.assertEqual(device["inbound_sequence"], 0)
            case.assertEqual(
                device["last_inbound_message_id"],
                result_envelope["message_id"],
            )


def run_test_automation_project_018_case_drift_fails_before_ddl(case):
    case_database = case.project_authorization_collation_database
    with case._connection(case_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, tool_name, tool_params, cron_expression, enabled) "
                "VALUES ('clockin_ronghui_0830', 'case drift', 'clock_in_dual', "
                "JSON_OBJECT('account_id', 'Price_Default'), '30 8 * * *', TRUE)"
            )
    with case.assertRaises(Exception):
        case._run_migrations(case_database)
    with case._connection(case_database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='scheduled_tasks' "
                "AND COLUMN_NAME='automation_id'",
                (case_database,),
            )
            case.assertEqual(cursor.fetchone()["n"], 0)


def run_test_grouped_approval_second_cas_failure_is_atomic(case):
    """Prove a mid-batch CAS loss rolls back every durable side effect.

    The first member uses the production ApprovalRepository and therefore
    writes both an approval decision and its domain event.  The second member
    runs a real guarded MySQL UPDATE whose deliberately stale plan hash must
    affect zero rows, then raises the same conflict consumed by the service.
    The enclosing production Unit of Work must roll the first member back.
    """

    from agent.orchestration.automation_project_policy_service import (
        AutomationProjectPolicyService,
    )
    from agent.orchestration.models import (
        Actor,
        ActorType,
        OrchestrationError,
    )
    from shared.automation_plugin_repository import AutomationPluginRepository
    from shared.orchestration_repository import (
        ApprovalRepository,
        InvalidStateError,
    )

    database = case.project_approval_atomic_database
    case._run_migrations(database)
    automation_id = "integration_atomic_approval_instance"
    request_id = str(uuid4())
    plugin_id = "integration_atomic_approval_plugin"
    digest_fields = {
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "tool_contract_sha256": "3" * 64,
        "config_schema_sha256": "4" * 64,
        "allowed_entrypoints_sha256": "5" * 64,
        "invocation_contracts_sha256": "6" * 64,
        "worker_requirement_sha256": "7" * 64,
        "runtime_sha256": "8" * 64,
        "scheduling_sha256": "9" * 64,
        "install_root_metadata_sha256": "a" * 64,
    }
    with case._connection(database) as connection:
        plugins = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        plugins.register_package_version(
            package={
                "plugin_id": plugin_id,
                "display_name": "Atomic approval integration",
                "description": "test-only signed package",
            },
            version={
                "version": "1.0.0",
                **digest_fields,
                "manifest_json": {
                    "allowed_entrypoints": ["console"],
                    "runtime": {
                        "kind": "core_tool_ref",
                        "tool_name": "integration_probe",
                    },
                },
                "project_full_auto_allowed": False,
                "trust_source": "ed25519_first_party",
                "install_root_metadata_json": {},
                "installed_by_actor_id": "integration-admin",
            },
        )
        plugins.install_project_instance(
            {
                "automation_id": automation_id,
                "plugin_id": plugin_id,
                "plugin_version": "1.0.0",
                "display_name": "Atomic approval instance",
                "install_request_id": str(uuid4()),
                "install_payload_sha256": "b" * 64,
                "installed_by_actor_id": "integration-admin",
                "migration_authority": False,
            }
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE automation_projects SET enabled=TRUE, state='ENABLED' "
                "WHERE automation_id=%s",
                (automation_id,),
            )
            case.assertEqual(1, cursor.rowcount)
        connection.commit()

    repository = case._repository(database)
    with repository.unit_of_work() as uow:
        uow.automation_projects.ensure_default(
            automation_id,
            mode="REQUIRE_EACH_RUN",
            project_generation=1,
            project_configuration_version=1,
        )
        uow.commit()

    approval_ids: list[str] = []
    run_ids: list[str] = []
    now = datetime.utcnow()
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for index in range(2):
                command_id = str(uuid4())
                work_item_id = str(uuid4())
                run_id = str(uuid4())
                approval_id = str(uuid4())
                correlation_id = str(uuid4())
                plan_hash = str(index + 1) * 64
                approval_ids.append(approval_id)
                run_ids.append(run_id)
                cursor.execute(
                    """
                    INSERT INTO agent_commands (
                        command_id, command_type, automation_id,
                        automation_generation, source, actor_type, actor_id,
                        actor_roles_json, entity_refs_json, parameters_json,
                        automation_invocation_json, idempotency_key,
                        correlation_id, status, requested_at
                    ) VALUES (
                        %s, 'automation.project.invoke', %s, 1,
                        'console', 'console_admin', 'integration-admin',
                        %s, %s, %s, %s, %s, %s, 'ACCEPTED', %s
                    )
                    """,
                    (
                        command_id,
                        automation_id,
                        json.dumps(["super_admin"]),
                        json.dumps([]),
                        json.dumps({"execution_context": {}}),
                        json.dumps({"automation_id": automation_id}),
                        f"atomic-approval:{command_id}",
                        correlation_id,
                        now,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO work_items (
                        work_item_id, command_id, type, title, status,
                        priority, source, dedupe_key
                    ) VALUES (
                        %s, %s, 'automation_project', %s,
                        'WAITING_APPROVAL', 'NORMAL', 'console', %s
                    )
                    """,
                    (
                        work_item_id,
                        command_id,
                        f"Atomic approval member {index + 1}",
                        f"atomic-approval:{work_item_id}",
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO agent_runs (
                        run_id, work_item_id, command_id, run_no, status,
                        mode, planner_kind, plan_schema_version, plan_json,
                        plan_hash, correlation_id, next_attempt_at
                    ) VALUES (
                        %s, %s, %s, 1, 'WAITING_APPROVAL',
                        'deterministic', 'deterministic', 1, %s, %s, %s, %s
                    )
                    """,
                    (
                        run_id,
                        work_item_id,
                        command_id,
                        json.dumps({"schema_version": 1, "steps": []}),
                        plan_hash,
                        correlation_id,
                        now,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO approval_requests (
                        approval_id, work_item_id, run_id, approval_round,
                        plan_hash, impact_json, impact_sha256, risk_level,
                        required_role, required_approvals, status,
                        requested_by_type, requested_by_id, expires_at
                    ) VALUES (
                        %s, %s, %s, 1, %s, %s, %s, 'HIGH',
                        'super_admin', 1, 'PENDING', 'system',
                        'integration-test', %s
                    )
                    """,
                    (
                        approval_id,
                        work_item_id,
                        run_id,
                        plan_hash,
                        json.dumps({"member": index + 1}),
                        str(index + 3) * 64,
                        now + timedelta(minutes=5),
                    ),
                )

    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "integration-admin",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    wake_calls: list[str] = []
    service = AutomationProjectPolicyService(
        repository,
        core_catalog=object(),
        plugin_catalog=object(),
        wake_runner=wake_calls.append,
    )
    pending = service.pending_approvals(automation_id, actor=actor)
    case.assertEqual(2, pending["pending_count"])

    original_record_decision = ApprovalRepository.record_decision
    decision_calls: list[str] = []

    def record_with_second_cas_failure(
        approval_repository,
        row,
        *,
        expected_plan_hash,
    ):
        decision_calls.append(str(row["approval_id"]))
        if len(decision_calls) == 2:
            with approval_repository.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE approval_requests
                    SET status='APPROVED', decided_at=NOW(6)
                    WHERE approval_id=%s
                      AND status='PENDING'
                      AND plan_hash=%s
                    """,
                    (row["approval_id"], "f" * 64),
                )
                case.assertEqual(0, cursor.rowcount)
            raise InvalidStateError("injected second approval CAS conflict")
        return original_record_decision(
            approval_repository,
            row,
            expected_plan_hash=expected_plan_hash,
        )

    with (
        patch.object(service, "_validate_pending_rows", return_value=None),
        patch.object(
            ApprovalRepository,
            "record_decision",
            new=record_with_second_cas_failure,
        ),
    ):
        with case.assertRaises(OrchestrationError) as raised:
            service.decide_pending_approvals(
                automation_id,
                decision="APPROVED",
                expected_pending_set_hash=pending["pending_set_hash"],
                request_id=request_id,
                comment="approve atomically",
                actor=actor,
            )

    case.assertEqual("PENDING_SET_CHANGED", raised.exception.code)
    case.assertEqual(2, raised.exception.details["pending"]["pending_count"])
    case.assertEqual(2, len(decision_calls))
    case.assertEqual([], wake_calls)
    with case._connection(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM approval_decisions "
                "WHERE approval_id IN (%s, %s)",
                tuple(approval_ids),
            )
            case.assertEqual(0, cursor.fetchone()["n"])
            cursor.execute(
                "SELECT COUNT(*) AS n FROM domain_events "
                "WHERE event_type='agent.approval.decided' "
                "AND run_id IN (%s, %s)",
                tuple(run_ids),
            )
            case.assertEqual(0, cursor.fetchone()["n"])
            cursor.execute(
                "SELECT COUNT(*) AS n "
                "FROM automation_project_approval_batches "
                "WHERE automation_id=%s AND request_id=%s",
                (automation_id, request_id),
            )
            case.assertEqual(0, cursor.fetchone()["n"])
            cursor.execute(
                """
                SELECT status, decided_at
                FROM approval_requests
                WHERE approval_id IN (%s, %s)
                ORDER BY approval_id
                """,
                tuple(approval_ids),
            )
            approval_rows = cursor.fetchall()
            case.assertEqual(2, len(approval_rows))
            case.assertTrue(
                all(
                    row["status"] == "PENDING" and row["decided_at"] is None
                    for row in approval_rows
                )
            )
