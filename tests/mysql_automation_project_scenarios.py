"""Real-MySQL scenarios for project approvals, invocation serialization and generation write locks."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import threading
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


def run_test_project_invocation_serializes_and_replays_on_real_mysql(case):
    """Actual signed processes: immediate atomic admission and immutable replay."""
    import concurrent.futures
    from pathlib import Path
    from tests.direct_invocation_fixture import DirectFixture
    from tests.v32_acceptance.management_fixture import ManagementFixture
    from tests.test_direct_plugin_invocation_mysql import ACTOR, _install, _service_package, _legacy_counts
    # The approval migration scenarios intentionally seed incomplete historical
    # packages. A real runtime startup must use its own valid installed state.
    database = case.project_invocation_database
    case._run_migrations(database)
    directory = Path(__file__).resolve().parents[1] / ".t" / ("race-" + uuid4().hex[:6])
    directory.mkdir(parents=True)
    with ManagementFixture(connection_factory=case._repository(database)._connection_factory, runtime_root=directory / "runtime", enable_directory_faults=False) as management:
        identity = _install(management, _service_package(directory, plugin_id="race_" + uuid4().hex[:8], sleep_seconds=.5))
        with DirectFixture(management, directory=directory / "ipc") as runtime:
            before = _legacy_counts(management.repository)
            barrier = threading.Barrier(2)
            request_ids = [str(uuid4()), str(uuid4())]
            def submit(index):
                barrier.wait(timeout=5)
                return management.policy.invoke_console(identity, request_id=request_ids[index], actor=ACTOR)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(submit, range(2)))
            case.assertEqual(["STARTING", "STARTING"], sorted(row["status"] for row in receipts))
            accepted, waiting = receipts
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                replays = list(pool.map(lambda _: management.policy.invoke_console(identity, request_id=accepted["request_id"], actor=ACTOR), range(8)))
            case.assertEqual({accepted["invocation_id"]}, {row["invocation_id"] for row in replays})
            result = runtime.service.wait_sync(accepted["invocation_id"])
            case.assertEqual("COMPLETED", result["status"])
            case.assertEqual("COMPLETED", runtime.service.wait_sync(waiting["invocation_id"])["status"])
            fresh = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
            case.assertEqual("COMPLETED", runtime.service.wait_sync(fresh["invocation_id"])["status"])
            replay = management.policy.invoke_console(identity, request_id=accepted["request_id"], actor=ACTOR)
            case.assertEqual(accepted["invocation_id"], replay["invocation_id"])
            case.assertEqual(result["output"], replay["output"])
            case.assertEqual(before, _legacy_counts(management.repository))
            with management.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
                cursor.execute("SELECT invocation_id,orchestration_run_id FROM automation_project_generation_leases WHERE invocation_id IN (%s,%s,%s)", tuple(row["invocation_id"] for row in (accepted, waiting, fresh)))
                leases = cursor.fetchall()
            case.assertEqual({accepted["invocation_id"], waiting["invocation_id"], fresh["invocation_id"]}, {row["invocation_id"] for row in leases})
            case.assertTrue(all(row["orchestration_run_id"] is None for row in leases))


def run_test_automation_project_024_original_plugin_full_auto(case):
    """Exercise the exact original six-key plugin recovery on real MySQL."""

    database = case.legacy_plugin_full_auto_database
    plugin_id = "migration_024_legacy_plugin"
    actor_id = "legacy-plugin-admin"
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
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS n
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA=DATABASE()
                  AND TABLE_NAME='automation_plugin_versions'
                  AND COLUMN_NAME IN ('runtime_model', 'plugin_api')
                """
            )
            case.assertEqual(0, cursor.fetchone()["n"])
            cursor.execute(
                """
                INSERT INTO automation_plugin_packages (
                    plugin_id, display_name, description, latest_version,
                    state, record_version
                ) VALUES (%s, %s, %s, '1.0.0', 'REGISTERED', 1)
                """,
                (
                    plugin_id,
                    "Migration 024 integration plugin",
                    "test-only original plugin writer fixture",
                ),
            )
            cursor.execute(
                """
                INSERT INTO automation_plugin_versions (
                    plugin_id, version, package_sha256, manifest_sha256,
                    manifest_json, tool_contract_sha256, config_schema_sha256,
                    allowed_entrypoints_sha256, invocation_contracts_sha256,
                    worker_requirement_sha256, runtime_sha256, scheduling_sha256,
                    project_full_auto_allowed, trust_source,
                    install_root_metadata_json, install_root_metadata_sha256,
                    installed_by_actor_id, state
                ) VALUES (
                    %s, '1.0.0', %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, TRUE, 'ed25519_first_party', %s, %s, %s, 'INSTALLED'
                )
                """,
                (
                    plugin_id,
                    digest_fields["package_sha256"],
                    digest_fields["manifest_sha256"],
                    json.dumps(
                        {
                            "allowed_entrypoints": ["console"],
                            "runtime": {
                                "kind": "core_tool_ref",
                                "tool_name": "migration_024_probe",
                            },
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    digest_fields["tool_contract_sha256"],
                    digest_fields["config_schema_sha256"],
                    digest_fields["allowed_entrypoints_sha256"],
                    digest_fields["invocation_contracts_sha256"],
                    digest_fields["worker_requirement_sha256"],
                    digest_fields["runtime_sha256"],
                    digest_fields["scheduling_sha256"],
                    json.dumps({}),
                    digest_fields["install_root_metadata_sha256"],
                    actor_id,
                ),
            )
            for automation_id in (
                "migration_024_legal",
                "migration_024_seven_key",
                "migration_024_extra_key",
                "migration_024_bad_hash",
                "migration_024_later_admin",
            ):
                cursor.execute(
                    """
                    INSERT INTO automation_projects (
                        automation_id, plugin_id, plugin_version, display_name,
                        enabled, state, install_request_id, install_payload_sha256,
                        installed_by_actor_id, migration_authority, record_version
                    ) VALUES (
                        %s, %s, '1.0.0', %s, FALSE, 'INSTALLED', %s, %s, %s,
                        FALSE, 1
                    )
                    """,
                    (
                        automation_id,
                        plugin_id,
                        automation_id,
                        str(uuid4()),
                        hashlib.sha256(automation_id.encode("utf-8")).hexdigest(),
                        actor_id,
                    ),
                )
        connection.commit()

    def canonical_hash(payload: dict) -> str:
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def original_metadata() -> dict:
        return {
            "from_version": "1.0.0",
            "package_sha256": "b" * 64,
            "previous_state": "ENABLED",
            "request_payload_sha256": "c" * 64,
            "target_generation": 1,
            "to_version": "2.0.0",
        }

    def seed_downgrade(
        automation_id: str,
        metadata: dict,
        metadata_sha256: str,
        *,
        later_admin_event: bool = False,
    ) -> None:
        request_id = str(uuid4())
        with case._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO automation_project_policies (
                        automation_id, project_generation, mode,
                        project_configuration_version, approved_by_actor_id,
                        approved_by_actor_role, approved_by_actor_display_name,
                        approved_at, comment, version
                    ) VALUES (%s, 1, 'REQUIRE_EACH_RUN', 1, %s, 'super_admin',
                              NULL, NOW(6), NULL, 1)
                    """,
                    (automation_id, actor_id),
                )
                cursor.execute(
                    """
                    INSERT INTO automation_project_events (
                        automation_id, request_id, event_type, from_state,
                        to_state, metadata_json, metadata_sha256, actor_id,
                        actor_role
                    ) VALUES (%s, %s, 'PLUGIN_UPGRADE_STAGED', 'ENABLED',
                              'UPGRADING', %s, %s, %s, 'super_admin')
                    """,
                    (
                        automation_id,
                        request_id,
                        json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                        metadata_sha256,
                        actor_id,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO automation_project_policy_events (
                        automation_id, request_id, from_mode, to_mode,
                        contract_hash, contract_snapshot_json,
                        tool_contract_hash, plugin_contract_hash,
                        project_generation, project_configuration_version,
                        actor_id, actor_role, actor_display_name, reason,
                        comment, correlation_id
                    ) VALUES (%s, %s, 'PROJECT_FULL_AUTO', 'REQUIRE_EACH_RUN',
                              NULL, NULL, NULL, NULL, 1, 1, %s, 'super_admin',
                              NULL, 'PLUGIN_VERSION_CHANGED', NULL, %s)
                    """,
                    (automation_id, request_id, actor_id, request_id),
                )
                if later_admin_event:
                    later_request_id = str(uuid4())
                    cursor.execute(
                        """
                        INSERT INTO automation_project_policy_events (
                            automation_id, request_id, from_mode, to_mode,
                            contract_hash, contract_snapshot_json,
                            tool_contract_hash, plugin_contract_hash,
                            project_generation, project_configuration_version,
                            actor_id, actor_role, actor_display_name, reason,
                            comment, correlation_id
                        ) VALUES (%s, %s, 'REQUIRE_EACH_RUN', 'REQUIRE_EACH_RUN',
                                  NULL, NULL, NULL, NULL, 1, 1,
                                  'later-super-admin', 'super_admin',
                                  'Later administrator',
                                  'SUPER_ADMIN_PROJECT_POLICY_CHANGED',
                                  'Keep approval', %s)
                        """,
                        (automation_id, later_request_id, later_request_id),
                    )
                    cursor.execute(
                        """
                        UPDATE automation_project_policies
                        SET approved_by_actor_id='later-super-admin',
                            approved_by_actor_role='super_admin',
                            approved_by_actor_display_name='Later administrator',
                            comment='Keep approval',
                            version=version+1
                        WHERE automation_id=%s
                        """,
                        (automation_id,),
                    )
    legal_id = "migration_024_legal"
    seed_downgrade(legal_id, original_metadata(), canonical_hash(original_metadata()))
    seven_key_metadata = {
        **original_metadata(),
        "prepared_configuration_request_id": str(uuid4()),
    }
    seed_downgrade(
        "migration_024_seven_key",
        seven_key_metadata,
        canonical_hash(seven_key_metadata),
    )
    extra_key_metadata = {**original_metadata(), "unexpected": "reject"}
    seed_downgrade(
        "migration_024_extra_key",
        extra_key_metadata,
        canonical_hash(extra_key_metadata),
    )
    seed_downgrade("migration_024_bad_hash", original_metadata(), "0" * 64)
    seed_downgrade(
        "migration_024_later_admin",
        original_metadata(),
        canonical_hash(original_metadata()),
        later_admin_event=True,
    )

    command_id = str(uuid4())
    work_item_id = str(uuid4())
    run_id = str(uuid4())
    approval_id = str(uuid4())
    correlation_id = str(uuid4())
    plan_hash = "d" * 64
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_commands (
                    command_id, command_type, automation_id,
                    automation_generation, source, actor_type, actor_id,
                    actor_roles_json, entity_refs_json, parameters_json,
                    automation_invocation_json, idempotency_key,
                    correlation_id, status, requested_at
                ) VALUES (%s, 'automation.project.invoke', %s, 1,
                          'console', 'console_admin', %s, %s, %s, %s, %s,
                          %s, %s, 'ACCEPTED', NOW(6))
                """,
                (
                    command_id,
                    legal_id,
                    actor_id,
                    json.dumps(["super_admin"]),
                    json.dumps([]),
                    json.dumps({}),
                    json.dumps({"automation_id": legal_id}),
                    f"migration-024:{command_id}",
                    correlation_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO work_items (
                    work_item_id, command_id, type, title, status, priority,
                    source, dedupe_key
                ) VALUES (%s, %s, 'automation_project', 'Migration 024 run',
                          'WAITING_APPROVAL', 'NORMAL', 'console', %s)
                """,
                (work_item_id, command_id, f"migration-024:{work_item_id}"),
            )
            cursor.execute(
                """
                INSERT INTO agent_runs (
                    run_id, work_item_id, command_id, run_no, status, mode,
                    planner_kind, plan_schema_version, plan_json, plan_hash,
                    correlation_id, next_attempt_at, worker_id,
                    lease_expires_at
                ) VALUES (%s, %s, %s, 1, 'WAITING_APPROVAL', 'deterministic',
                          'deterministic', 1, %s, %s, %s,
                          DATE_ADD(NOW(6), INTERVAL 1 DAY), 'stale-worker',
                          DATE_ADD(NOW(6), INTERVAL 1 DAY))
                """,
                (
                    run_id,
                    work_item_id,
                    command_id,
                    json.dumps({"schema_version": 1, "steps": []}),
                    plan_hash,
                    correlation_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, work_item_id, run_id, approval_round,
                    plan_hash, impact_json, impact_sha256, risk_level,
                    required_role, required_approvals, status,
                    requested_by_type, requested_by_id, expires_at
                ) VALUES (%s, %s, %s, 1, %s, %s, %s, 'HIGH', 'super_admin',
                          1, 'PENDING', 'system', 'migration-024',
                          DATE_ADD(NOW(6), INTERVAL 5 MINUTE))
                """,
                (
                    approval_id,
                    work_item_id,
                    run_id,
                    plan_hash,
                    json.dumps({"migration": 24}),
                    "e" * 64,
                ),
            )

    case._apply_one(database, "024")

    invalid_evidence_ids = (
        "migration_024_seven_key",
        "migration_024_extra_key",
        "migration_024_bad_hash",
    )
    with case._connection(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mode, version, approved_by_actor_id, comment
                FROM automation_project_policies WHERE automation_id=%s
                """,
                (legal_id,),
            )
            legal_policy = cursor.fetchone()
            case.assertEqual("PROJECT_FULL_AUTO", legal_policy["mode"])
            case.assertEqual(2, legal_policy["version"])
            case.assertEqual(
                "system:migration:automation-plugin-full-auto-v2",
                legal_policy["approved_by_actor_id"],
            )
            case.assertEqual(
                "Restored durable full-auto after original plugin downgrade",
                legal_policy["comment"],
            )
            cursor.execute(
                """
                SELECT COUNT(*) AS n FROM automation_project_policy_events
                WHERE automation_id=%s
                  AND reason='MIGRATION_024_PLUGIN_FULL_AUTO'
                  AND request_id=%s
                """,
                (legal_id, f"migration-024-plugin-full-auto:{legal_id}"),
            )
            case.assertEqual(1, cursor.fetchone()["n"])
            cursor.execute(
                """
                SELECT approval.status AS approval_status,
                       approval.decided_at IS NOT NULL AS approval_decided,
                       run.status AS run_status, run.version AS run_version,
                       run.next_attempt_at<=NOW(6) AS awake,
                       run.worker_id, run.lease_expires_at
                FROM approval_requests AS approval
                JOIN agent_runs AS run ON run.run_id=approval.run_id
                WHERE approval.approval_id=%s
                """,
                (approval_id,),
            )
            woken = cursor.fetchone()
            case.assertEqual("INVALIDATED", woken["approval_status"])
            case.assertEqual(1, woken["approval_decided"])
            case.assertEqual("WAITING_APPROVAL", woken["run_status"])
            case.assertEqual(2, woken["run_version"])
            case.assertEqual(1, woken["awake"])
            case.assertIsNone(woken["worker_id"])
            case.assertIsNone(woken["lease_expires_at"])
            cursor.execute(
                """
                SELECT automation_id, mode, version
                FROM automation_project_policies
                WHERE automation_id IN (%s, %s, %s)
                ORDER BY automation_id
                """,
                invalid_evidence_ids,
            )
            rejected = cursor.fetchall()
            case.assertEqual(3, len(rejected))
            case.assertTrue(
                all(row["mode"] == "REQUIRE_EACH_RUN" for row in rejected)
            )
            case.assertTrue(all(row["version"] == 1 for row in rejected))
            cursor.execute(
                """
                SELECT mode, version, approved_by_actor_id,
                       approved_by_actor_display_name, comment
                FROM automation_project_policies
                WHERE automation_id='migration_024_later_admin'
                """
            )
            later_admin_policy = cursor.fetchone()
            case.assertEqual("REQUIRE_EACH_RUN", later_admin_policy["mode"])
            case.assertEqual(2, later_admin_policy["version"])
            case.assertEqual(
                "later-super-admin", later_admin_policy["approved_by_actor_id"]
            )
            case.assertEqual(
                "Later administrator",
                later_admin_policy["approved_by_actor_display_name"],
            )
            case.assertEqual("Keep approval", later_admin_policy["comment"])
            cursor.execute(
                """
                SELECT COUNT(*) AS n FROM automation_project_policy_events
                WHERE automation_id IN (%s, %s, %s, %s)
                  AND reason='MIGRATION_024_PLUGIN_FULL_AUTO'
                """,
                (*invalid_evidence_ids, "migration_024_later_admin"),
            )
            case.assertEqual(0, cursor.fetchone()["n"])

    migration = next(
        path
        for version, path in case.runner.discover_migrations()
        if version == "024"
    )
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for statement in case.runner.split_sql_statements(
                migration.read_text(encoding="utf-8")
            ):
                cursor.execute(statement)
            cursor.execute(
                """
                SELECT version FROM automation_project_policies
                WHERE automation_id=%s
                """,
                (legal_id,),
            )
            case.assertEqual(2, cursor.fetchone()["version"])
            cursor.execute(
                """
                SELECT COUNT(*) AS n FROM automation_project_policy_events
                WHERE automation_id=%s
                  AND reason='MIGRATION_024_PLUGIN_FULL_AUTO'
                """,
                (legal_id,),
            )
            case.assertEqual(1, cursor.fetchone()["n"])
            cursor.execute(
                """
                SELECT approval.status AS approval_status,
                       approval.decided_at IS NOT NULL AS approval_decided,
                       run.version AS run_version, run.worker_id,
                       run.lease_expires_at
                FROM approval_requests AS approval
                JOIN agent_runs AS run ON run.run_id=approval.run_id
                WHERE approval.approval_id=%s
                """,
                (approval_id,),
            )
            replayed = cursor.fetchone()
            case.assertEqual("INVALIDATED", replayed["approval_status"])
            case.assertEqual(1, replayed["approval_decided"])
            case.assertEqual(2, replayed["run_version"])
            case.assertIsNone(replayed["worker_id"])
            case.assertIsNone(replayed["lease_expires_at"])


from tests.mysql_scheduler_supersession_scenarios import run_test_scheduler_supersession_selector_is_exact_and_terminal_retry_observes_cancellation as run_test_scheduler_supersession_selector_is_exact_and_terminal_retry_observes_cancellation


def _load_generation_write_scenarios():
    path = Path(__file__).with_name("mysql_generation_write_scenarios.py")
    spec = importlib.util.spec_from_file_location("mysql_generation_write_scenarios", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("generation write scenario module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_test_generation_write_lock_order_races(case):
    """Forward the isolated generation-write lock-order suite."""

    return _load_generation_write_scenarios().run_test_generation_write_lock_order_races(case)
