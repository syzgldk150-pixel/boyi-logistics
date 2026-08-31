"""Real-MySQL generation write-lock scenarios, isolated from project-policy cases."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import json
import os
import threading
from unittest.mock import patch
from uuid import uuid4


def run_test_generation_write_lock_order_races(case):
    """Exercise the exact-lease finalization/recovery and record/release races.

    This deliberately uses separate production repository connections.  A CAS
    loss is a valid state-machine result; MySQL deadlocks and a terminal lease
    with an unconverted STARTED receipt are not.
    """
    from shared.automation_plugin_repository import AutomationPluginRepository
    from shared.orchestration_repository_support import (
        ConcurrentUpdateError,
        IdempotencyConflict,
        _json_hash,
    )

    database = case.database
    automation_id = f"write_lock_order_{uuid4().hex[:20]}"
    plugin_id = f"write_lock_plugin_{uuid4().hex[:18]}"
    run_id = str(uuid4())
    command_id = str(uuid4())
    work_item_id = str(uuid4())
    hashes = [character * 64 for character in "cdef012345678"]
    with case._connection(database) as connection:
        repository = AutomationPluginRepository(
            connection, cursor_factory=case.pymysql.cursors.DictCursor,
        )
        repository.register_package_version(
            package={"plugin_id": plugin_id, "display_name": "write lock", "description": "integration only"},
            version={
                "version": "1.0.0", "package_sha256": "1" * 64,
                "manifest_sha256": "2" * 64, "manifest_json": {"runtime": {"kind": "python_subprocess"}},
                "tool_contract_sha256": "3" * 64, "config_schema_sha256": "4" * 64,
                "allowed_entrypoints_sha256": "5" * 64, "invocation_contracts_sha256": "6" * 64,
                "worker_requirement_sha256": "7" * 64, "runtime_sha256": "8" * 64,
                "scheduling_sha256": "9" * 64, "project_full_auto_allowed": False,
                "trust_source": "ed25519_first_party", "install_root_metadata_json": {},
                "install_root_metadata_sha256": "a" * 64, "installed_by_actor_id": "integration-admin",
            },
        )
        repository.install_project_instance({
            "automation_id": automation_id, "plugin_id": plugin_id, "plugin_version": "1.0.0",
            "display_name": "write lock", "install_request_id": str(uuid4()),
            "install_payload_sha256": "b" * 64, "installed_by_actor_id": "integration-admin",
            "migration_authority": False,
        })
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_project_generations(
                    automation_id, generation, request_id, state, plugin_id, plugin_version,
                    package_sha256, manifest_sha256, trust_source, project_config_sha256,
                    account_bindings_sha256, resource_bindings_sha256, device_binding_sha256,
                    schedule_sha256, core_registry_sha256, tool_contract_sha256,
                    invocation_contracts_sha256, compiled_invocations_sha256,
                    runtime_descriptor_sha256, governance_anchor_sha256, policy_contract_sha256,
                    enabled_entrypoints_sha256, snapshot_json, snapshot_sha256, committed_at
                ) VALUES (%s, 1, %s, 'COMMITTED', %s, '1.0.0', %s, %s,
                          'ed25519_first_party', %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, '{}', %s, NOW(6))
                """,
                (automation_id, str(uuid4()), plugin_id, "1" * 64, "2" * 64, *hashes, "0" * 64),
            )
            cursor.execute(
                "UPDATE automation_projects SET enabled=TRUE, state='ENABLED', target_generation=1, committed_generation=1, reconcile_state='STABLE' WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                """INSERT INTO agent_commands (command_id, command_type, automation_id, automation_generation, source, actor_type, actor_id, actor_roles_json, entity_refs_json, parameters_json, automation_invocation_json, idempotency_key, correlation_id, status, requested_at)
                   VALUES (%s, 'automation.project.invoke', %s, 1, 'console', 'console_admin', 'integration-admin', '[]', '[]', '{}', '{}', %s, %s, 'ACCEPTED', NOW(6))""",
                (command_id, automation_id, f"write-lock:{command_id}", str(uuid4())),
            )
            cursor.execute(
                "INSERT INTO work_items (work_item_id, command_id, type, title, status, priority, source, dedupe_key) VALUES (%s, %s, 'automation_project', 'write lock', 'IN_PROGRESS', 'NORMAL', 'console', %s)",
                (work_item_id, command_id, f"write-lock:{work_item_id}"),
            )
            cursor.execute(
                """INSERT INTO agent_runs (run_id, work_item_id, command_id, run_no, status, mode, planner_kind, plan_schema_version, plan_json, plan_hash, correlation_id, next_attempt_at)
                   VALUES (%s, %s, %s, 1, 'BLOCKED_DATA', 'deterministic', 'deterministic', 1, '{\"schema_version\":1,\"steps\":[]}', %s, %s, NOW(6))""",
                (run_id, work_item_id, command_id, "f" * 64, str(uuid4())),
            )
        connection.commit()

    target_ref = {
        "schema": 1, "automation_id": automation_id, "operation": "write", "action": "sync",
        "role_sha256": "1" * 64, "binding_sha256": "2" * 64, "request_sha256": "3" * 64,
        "business_date_sha256": "", "batch_sha256": "", "run_sha256": "", "idempotency_key_sha256": "",
        "record_count": 0, "content_sha256": "4" * 64,
    }
    target_ref_sha256 = _json_hash(target_ref)

    def insert_lease(*, outcome, receipt=False):
        lease_id = str(uuid4())
        with case._connection(database) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO automation_project_generation_leases (lease_id, automation_id, generation, orchestration_run_id, lease_owner, runtime_metadata_json, runtime_metadata_sha256, outcome, expires_at)
                       VALUES (%s, %s, 1, %s, 'integration', '{}', %s, %s, DATE_ADD(NOW(6), INTERVAL 5 MINUTE))""",
                    (lease_id, automation_id, run_id, "5" * 64, outcome),
                )
                if receipt:
                    cursor.execute(
                        """INSERT INTO automation_write_attempt_receipts (receipt_id, automation_id, generation, lease_id, orchestration_run_id, step_id, request_id, operation, action, argument_sha256, target_ref_sha256, target_ref_json, outcome, evidence_sha256, created_at, updated_at)
                           VALUES (%s, %s, 1, %s, %s, %s, %s, 'write', 'sync', %s, %s, %s, 'WRITE_VERIFIED', %s, NOW(6), NOW(6))""",
                        (str(uuid4()), automation_id, lease_id, run_id, str(uuid4()), str(uuid4()), "4" * 64, target_ref_sha256, json.dumps(target_ref), "9" * 64),
                    )
            connection.commit()
        return lease_id

    def race(*, left, right):
        barrier, errors = threading.Barrier(2), []
        def invoke(callback):
            try:
                barrier.wait(timeout=10)
                callback()
            except Exception as exc:  # CAS loss is expected for one contender.
                errors.append((callback.__name__, exc))
        threads = [threading.Thread(target=invoke, args=(callback,)) for callback in (left, right)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            case.assertFalse(thread.is_alive(), "write-lock race did not finish")
        case.assertFalse(
            any("deadlock" in str(error).lower() for _, error in errors),
            errors,
        )
        return errors

    finalization_lease = insert_lease(outcome="VERIFYING", receipt=True)
    def finalize_unknown():
        with case._connection(database) as connection:
            AutomationPluginRepository(connection, cursor_factory=case.pymysql.cursors.DictCursor).finalize_generation_write_row(
                automation_id=automation_id, generation=1, lease_id=finalization_lease,
                outcome="WRITE_OUTCOME_UNKNOWN", evidence_sha256="7" * 64,
            )
            connection.commit()
    def settle_applied():
        with case._connection(database) as connection:
            AutomationPluginRepository(connection, cursor_factory=case.pymysql.cursors.DictCursor).settle_unknown_write_recovery_row(
                automation_id=automation_id, generation=1, lease_id=finalization_lease,
                recovery_status="APPLIED", evidence_sha256="8" * 64,
            )
            connection.commit()
    race(left=finalize_unknown, right=settle_applied)

    record_lease = insert_lease(outcome="RUNNING")
    def record():
        with case._connection(database) as connection:
            AutomationPluginRepository(connection, cursor_factory=case.pymysql.cursors.DictCursor).record_generation_write_attempt_row({
                "automation_id": automation_id, "generation": 1, "lease_id": record_lease,
                "orchestration_run_id": run_id, "step_id": str(uuid4()), "request_id": str(uuid4()),
                "operation": "write", "action": "sync", "argument_sha256": "4" * 64,
                "target_ref_sha256": target_ref_sha256, "target_ref_json": target_ref,
            })
            connection.commit()
    def release_unknown():
        with case._connection(database) as connection:
            AutomationPluginRepository(connection, cursor_factory=case.pymysql.cursors.DictCursor).release_generation_lease_row(
                record_lease, outcome="WRITE_OUTCOME_UNKNOWN",
            )
            connection.commit()
    record_race_errors = race(left=record, right=release_unknown)
    case.assertTrue(
        all(
            name != "record" or isinstance(error, IdempotencyConflict)
            for name, error in record_race_errors
        ),
        record_race_errors,
    )
    with case._connection(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT outcome FROM automation_project_generation_leases WHERE lease_id=%s",
                (record_lease,),
            )
            case.assertEqual("WRITE_OUTCOME_UNKNOWN", cursor.fetchone()["outcome"])
            cursor.execute(
                "SELECT outcome FROM automation_write_attempt_receipts WHERE lease_id=%s",
                (record_lease,),
            )
            recorded_receipts = cursor.fetchall()
    case.assertLessEqual(len(recorded_receipts), 1)
    case.assertTrue(
        all(row["outcome"] == "WRITE_OUTCOME_UNKNOWN" for row in recorded_receipts),
        recorded_receipts,
    )

    # Disposal reconciliation and active write settlement contend on the same
    # project/generation.  Keep one durable unknown lease as the authoritative
    # blocker while a second lease transitions into the same state.
    blocker_lease = insert_lease(outcome="WRITE_OUTCOME_UNKNOWN")
    block_release_lease = insert_lease(outcome="RUNNING")

    def block_unknown_generation():
        with case._connection(database) as connection:
            AutomationPluginRepository(
                connection, cursor_factory=case.pymysql.cursors.DictCursor,
            ).block_generation_unknown_write_row(automation_id, 1)
            connection.commit()

    def release_unknown_for_block_race():
        with case._connection(database) as connection:
            AutomationPluginRepository(
                connection, cursor_factory=case.pymysql.cursors.DictCursor,
            ).release_generation_lease_row(
                block_release_lease, outcome="WRITE_OUTCOME_UNKNOWN",
            )
            connection.commit()

    race(left=block_unknown_generation, right=release_unknown_for_block_race)

    block_finalize_lease = insert_lease(outcome="VERIFYING")

    def finalize_unknown_for_block_race():
        with case._connection(database) as connection:
            AutomationPluginRepository(
                connection, cursor_factory=case.pymysql.cursors.DictCursor,
            ).finalize_generation_write_row(
                automation_id=automation_id, generation=1,
                lease_id=block_finalize_lease, outcome="WRITE_OUTCOME_UNKNOWN",
                evidence_sha256="a" * 64,
            )
            connection.commit()

    race(left=block_unknown_generation, right=finalize_unknown_for_block_race)

    # Recovery contends with the idempotent Command gateway on this exact Run.
    # The gateway holds Command -> Work Item -> Run; recovery must never take
    # Run then Command/Work Item while retaining its generation locks.
    recovery_command, recovery_item, recovery_run, recovery_event, recovery_outbox = (
        case._aggregate_rows("unknown-write-recovery-lock-order")
    )
    recovery_command["automation_id"] = automation_id
    recovery_command["automation_generation"] = 1
    recovery_command["automation_invocation_json"] = {
        "schema_version": 1,
        "automation_id": automation_id,
        "automation_generation": 1,
        "entrypoint": "console",
        "contract_id": "integration.write_lock_order",
        "contract_hash": "d" * 64,
        "policy_version": 1,
        "project_configuration_version": 1,
        "request_id": str(uuid4()),
    }
    recovery_item["status"] = "IN_PROGRESS"
    recovery_run["status"] = "BLOCKED_DATA"
    recovery_repository = case._repository(database)
    with recovery_repository.unit_of_work() as uow:
        recovery_gateway = uow.command_gateway_create(
            recovery_command, recovery_item, recovery_run, recovery_event,
            recovery_outbox,
        )
        uow.commit()
    recovery_lease = str(uuid4())
    recovery_step = str(uuid4())
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_run_steps (step_id, run_id, step_key, step_order,
                       tool_name, tool_version, operation_type, risk_level, status,
                       requires_approval, retry_safe, idempotency_key)
                   VALUES (%s, %s, 'write', 1, 'integration_write', '1.0.0',
                           'EXTERNAL_WRITE', 'HIGH', 'BLOCKED_DATA', TRUE, TRUE, %s)""",
                (recovery_step, recovery_gateway["run_id"], f"recovery:{recovery_step}"),
            )
            cursor.execute(
                """INSERT INTO automation_project_generation_leases (lease_id, automation_id,
                       generation, orchestration_run_id, lease_owner, runtime_metadata_json,
                       runtime_metadata_sha256, outcome, expires_at)
                   VALUES (%s, %s, 1, %s, 'integration', '{}', %s,
                           'WRITE_OUTCOME_UNKNOWN', DATE_ADD(NOW(6), INTERVAL 5 MINUTE))""",
                (recovery_lease, automation_id, recovery_gateway["run_id"], "b" * 64),
            )
            cursor.execute(
                """INSERT INTO automation_write_attempt_receipts (receipt_id, automation_id,
                       generation, lease_id, orchestration_run_id, step_id, request_id,
                       operation, action, argument_sha256, target_ref_sha256, target_ref_json,
                       outcome, evidence_sha256, created_at, updated_at)
                   VALUES (%s, %s, 1, %s, %s, %s, %s, 'write', 'sync', %s, %s, %s,
                           'WRITE_VERIFIED', %s, NOW(6), NOW(6))""",
                (
                    str(uuid4()), automation_id, recovery_lease,
                    recovery_gateway["run_id"], recovery_step, str(uuid4()),
                    "4" * 64, target_ref_sha256, json.dumps(target_ref), "c" * 64,
                ),
            )

    def recover_for_gateway_race():
        repository = case._repository(database)
        with repository.unit_of_work() as uow:
            uow.recover_unknown_automation_write(
                automation_id=automation_id, generation=1, lease_id=recovery_lease,
                request_id=str(uuid4()), actor_id="integration-admin",
                actor_role="super_admin",
            )
            uow.commit()

    def replay_command_for_recovery_race():
        repository = case._repository(database)
        with repository.unit_of_work() as uow:
            replayed = uow.command_gateway_create(
                recovery_command, recovery_item, recovery_run, recovery_event,
                recovery_outbox,
            )
            case.assertFalse(replayed["created"]["command"])
            uow.commit()

    race(left=recover_for_gateway_race, right=replay_command_for_recovery_race)

    # Two sibling reconciliations must serialize through P -> G -> lease-set.
    # Neither may reopen the project while another unknown lease remains.
    def settle_sibling(lease_id):
        with case._connection(database) as connection:
            AutomationPluginRepository(
                connection, cursor_factory=case.pymysql.cursors.DictCursor,
            ).settle_unknown_write_recovery_row(
                automation_id=automation_id, generation=1, lease_id=lease_id,
                recovery_status="APPLIED", evidence_sha256="d" * 64,
            )
            connection.commit()

    race(
        left=lambda: settle_sibling(blocker_lease),
        right=lambda: settle_sibling(block_release_lease),
    )
    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM automation_project_generations "
                "WHERE automation_id=%s AND generation=1",
                (automation_id,),
            )
            case.assertEqual("COMMITTED", cursor.fetchone()["state"])
            cursor.execute(
                "SELECT reconcile_state FROM automation_projects WHERE automation_id=%s",
                (automation_id,),
            )
            case.assertEqual("STABLE", cursor.fetchone()["reconcile_state"])

    with case._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT outcome FROM automation_project_generation_leases "
                "WHERE lease_id IN (%s, %s, %s, %s, %s)",
                (
                    finalization_lease, record_lease, blocker_lease,
                    block_release_lease, block_finalize_lease,
                ),
            )
            outcomes = {row["outcome"] for row in cursor.fetchall()}
            case.assertTrue(outcomes <= {"WRITE_VERIFIED", "WRITE_OUTCOME_UNKNOWN"})
            cursor.execute("SELECT COUNT(*) AS n FROM automation_write_attempt_receipts AS receipt JOIN automation_project_generation_leases AS lease ON lease.lease_id=receipt.lease_id WHERE lease.lease_id IN (%s, %s) AND lease.outcome IN ('WRITE_VERIFIED', 'WRITE_OUTCOME_UNKNOWN') AND receipt.outcome='STARTED'", (finalization_lease, record_lease))
            case.assertEqual(0, cursor.fetchone()["n"])

    # A fully prepared successor may replace an exact archival unknown-write
    # generation without mutating or replaying the predecessor. A late
    # finalizer for a second old lease must remain scoped to the old generation
    # and must not block the newly committed route.
    archival_automation_id = f"archival_unknown_{uuid4().hex[:20]}"
    archival_plugin_id = f"archival_plugin_{uuid4().hex[:18]}"
    tool_contract = {
        "name": f"integration.{archival_automation_id}.run",
        "version": "1.0.0",
    }
    runtime = {"kind": "python_subprocess", "entrypoint": "payload/main.py"}
    runtime_permissions = {}
    account_roles = []
    resource_roles = []
    scheduling = {"supported": False}
    governance_anchor = {"schema_version": 1, "authority": "integration"}
    invocation_contracts = {}
    install_metadata = {
        "install_root": f"/integration/plugins/{archival_plugin_id}/1.0.0",
        "python_relative": "venv/bin/python",
    }
    manifest = {
        "allowed_entrypoints": [],
        "account_roles": account_roles,
        "config_schema": {"type": "object", "additionalProperties": False},
        "governance_anchor": governance_anchor,
        "invocation_contracts": invocation_contracts,
        "resource_roles": resource_roles,
        "runtime": runtime,
        "runtime_permissions": runtime_permissions,
        "scheduling": scheduling,
        "tool_contract": tool_contract,
        "worker_requirement": {"kind": "server"},
    }
    package_sha256 = _json_hash({"plugin_id": archival_plugin_id, "version": "1.0.0"})
    manifest_sha256 = _json_hash(manifest)
    runtime_descriptor = {
        "runtime": runtime,
        "runtime_permissions": runtime_permissions,
        "account_roles": account_roles,
        "resource_roles": resource_roles,
        "install_metadata": install_metadata,
    }

    def generation_snapshot(config, generation, *, instance_id=archival_automation_id):
        return {
            "automation_id": instance_id,
            "generation": generation,
            "plugin_id": archival_plugin_id,
            "plugin_version": "1.0.0",
            "package_sha256": package_sha256,
            "manifest_sha256": manifest_sha256,
            "project_config_sha256": config["config_sha256"],
            "account_bindings_sha256": config["account_bindings_sha256"],
            "resource_bindings_sha256": config["resource_bindings_sha256"],
            "device_binding_sha256": config["device_binding_sha256"],
            "schedule_sha256": config["desired_schedule_sha256"],
            "core_registry_sha256": _json_hash({"revision": "integration"}),
            "tool_contract_sha256": _json_hash(tool_contract),
            "invocation_contracts_sha256": _json_hash(invocation_contracts),
            "compiled_invocations_sha256": config["compiled_invocations_sha256"],
            "runtime_descriptor_sha256": _json_hash(runtime_descriptor),
            "governance_anchor_sha256": _json_hash(governance_anchor),
            "policy_contract_sha256": _json_hash({"mode": "PROJECT_FULL_AUTO"}),
            "trust_source": "ed25519_first_party",
            "enabled_entrypoints": [],
            "execution_metadata": {
                "project_config_version": int(config["config_version"]),
                "project_config": config["config_json"],
                "account_bindings": config["account_bindings_json"],
                "resource_bindings": config["resource_bindings_json"],
                "device_binding": None,
                "schedule": config["desired_schedule_json"],
                "compiled_invocations": config["compiled_invocations_json"],
                "runtime_descriptor": runtime_descriptor,
                "action_contract": tool_contract,
                "governance_anchor": governance_anchor,
            },
            "created_at": datetime.now(),
        }

    def prepare_generation(repository, snapshot, expected_committed):
        instance_id = str(snapshot["automation_id"])
        generation = int(snapshot["generation"])
        repository.allocate_target_generation_row(
            snapshot,
            expected_committed_generation=expected_committed,
            request_id=str(uuid4()),
        )
        repository.mark_generation_preparing_row(
            instance_id,
            generation,
        )
        repository.replace_generation_coeffects_rows(
            instance_id,
            generation,
            (
                {
                    "kind": "CORE_ADAPTER",
                    "key": "integration",
                    "revision": f"generation-{generation}",
                    "ready": True,
                    "observed_at": datetime.now(),
                    "reason_code": None,
                },
            ),
        )
        repository.mark_generation_prepared_row(
            instance_id,
            generation,
        )

    with case._connection(database) as connection:
        archival_repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        archival_repository.register_package_version(
            package={
                "plugin_id": archival_plugin_id,
                "display_name": "archival unknown integration",
                "description": "integration only",
            },
            version={
                "version": "1.0.0",
                "package_sha256": package_sha256,
                "manifest_sha256": manifest_sha256,
                "manifest_json": manifest,
                "tool_contract_sha256": _json_hash(tool_contract),
                "config_schema_sha256": _json_hash(manifest["config_schema"]),
                "allowed_entrypoints_sha256": _json_hash([]),
                "invocation_contracts_sha256": _json_hash(invocation_contracts),
                "worker_requirement_sha256": _json_hash(manifest["worker_requirement"]),
                "runtime_sha256": _json_hash(runtime),
                "scheduling_sha256": _json_hash(scheduling),
                "project_full_auto_allowed": True,
                "trust_source": "ed25519_first_party",
                "install_root_metadata_json": install_metadata,
                "install_root_metadata_sha256": _json_hash(install_metadata),
                "installed_by_actor_id": "integration-admin",
            },
        )
        archival_repository.install_project_instance(
            {
                "automation_id": archival_automation_id,
                "plugin_id": archival_plugin_id,
                "plugin_version": "1.0.0",
                "display_name": "archival unknown integration",
                "install_request_id": str(uuid4()),
                "install_payload_sha256": _json_hash(
                    {"automation_id": archival_automation_id}
                ),
                "installed_by_actor_id": "integration-admin",
                "migration_authority": False,
            }
        )
        initial_archival_config = archival_repository.initialize_project_config(
            archival_automation_id,
            enabled_entrypoints=(),
        )
        archival_config = archival_repository.save_project_config(
            archival_automation_id,
            config={},
            account_bindings={},
            resource_bindings={},
            enabled_entrypoints=(),
            schedule={"kind": "none", "times": [], "enabled": False},
            compiled_invocations={},
            contract_witness={
                "runtime_model": "ACTION_V1",
                "allowed_entrypoints": [],
                "invocation_contracts": invocation_contracts,
                "scheduling": scheduling,
            },
            device_binding=None,
            actor_id="integration-admin",
            actor_role="super_admin",
            request_id=str(uuid4()),
            expected_project_configuration_version=int(
                initial_archival_config["config_version"]
            ),
        )
        first_snapshot = generation_snapshot(archival_config, 1)
        prepare_generation(archival_repository, first_snapshot, None)
        archival_repository.commit_generation_cas_row(
            archival_automation_id,
            1,
            expected_committed_generation=None,
        )
        first_generation = archival_repository.get_generation_row(
            archival_automation_id,
            1,
        )
        case.assertIsNotNone(first_generation)
        case.assertEqual(
            "PENDING_PROJECTION",
            first_generation["activation_phase"],
        )
        archival_repository.complete_generation_activation_row(
            archival_automation_id,
            1,
            expected_transition_token=first_generation[
                "activation_transition_token"
            ],
        )
        first_snapshot_sha256 = first_generation["snapshot_sha256"]
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE automation_projects SET enabled=TRUE, state='ENABLED' "
                "WHERE automation_id=%s",
                (archival_automation_id,),
            )
        connection.commit()

    unknown_lease = str(uuid4())
    late_finalizer_lease = str(uuid4())
    successor_lease = str(uuid4())
    current_unknown_lease = str(uuid4())
    repeat_after_unknown_lease = str(uuid4())
    with case._connection(database) as connection:
        archival_repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        for lease_id, owner in (
            (unknown_lease, "integration-unknown"),
            (late_finalizer_lease, "integration-late-finalizer"),
        ):
            archival_repository.acquire_committed_generation_lease_row(
                archival_automation_id,
                expected_generation=1,
                expected_manifest_sha256=manifest_sha256,
                lease_id=lease_id,
                orchestration_run_id=run_id,
                expires_at=datetime.now() + timedelta(minutes=5),
                lease_owner=owner,
            )
        second_snapshot = generation_snapshot(archival_config, 2)
        prepare_generation(archival_repository, second_snapshot, 1)
        archival_repository.release_generation_lease_row(
            late_finalizer_lease,
            outcome="VERIFYING",
        )
        archival_repository.release_generation_lease_row(
            unknown_lease,
            outcome="WRITE_OUTCOME_UNKNOWN",
        )
        connection.commit()

    with case._connection(database) as connection:
        archival_repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        # An older run that is still finalizing must not block a prepared
        # successor. Its eventual unknown-write evidence is quarantined on the
        # archived generation while the newly committed route remains usable.
        archival_repository.commit_generation_cas_row(
            archival_automation_id,
            2,
            expected_committed_generation=1,
        )
        successor_generation = archival_repository.get_generation_row(
            archival_automation_id,
            2,
        )
        case.assertIsNotNone(successor_generation)
        archival_repository.complete_generation_activation_row(
            archival_automation_id,
            2,
            expected_transition_token=successor_generation[
                "activation_transition_token"
            ],
        )
        archival_repository.finalize_generation_write_row(
            automation_id=archival_automation_id,
            generation=1,
            lease_id=late_finalizer_lease,
            outcome="WRITE_OUTCOME_UNKNOWN",
            evidence_sha256="e" * 64,
        )
        archival_repository.commit_generation_cas_row(
            archival_automation_id,
            2,
            expected_committed_generation=1,
        )
        archival_repository.acquire_committed_generation_lease_row(
            archival_automation_id,
            expected_generation=2,
            expected_manifest_sha256=manifest_sha256,
            lease_id=successor_lease,
            orchestration_run_id=run_id,
            expires_at=datetime.now() + timedelta(minutes=5),
            lease_owner="integration-successor",
        )
        archival_repository.release_generation_lease_row(
            successor_lease,
            outcome="SUCCEEDED",
        )
        connection.commit()

    with case._connection(database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT target_generation, committed_generation, reconcile_state, state "
                "FROM automation_projects WHERE automation_id=%s",
                (archival_automation_id,),
            )
            project = cursor.fetchone()
            case.assertEqual(2, project["target_generation"])
            case.assertEqual(2, project["committed_generation"])
            case.assertEqual("STABLE", project["reconcile_state"])
            case.assertEqual("ENABLED", project["state"])
            cursor.execute(
                "SELECT generation, state, snapshot_sha256 "
                "FROM automation_project_generations WHERE automation_id=%s "
                "ORDER BY generation",
                (archival_automation_id,),
            )
            generations = cursor.fetchall()
            case.assertEqual(
                [(1, "BLOCKED"), (2, "COMMITTED")],
                [(row["generation"], row["state"]) for row in generations],
            )
            case.assertEqual(
                first_snapshot_sha256,
                generations[0]["snapshot_sha256"],
            )
            cursor.execute(
                "SELECT lease_id, outcome FROM automation_project_generation_leases "
                "WHERE lease_id IN (%s, %s, %s)",
                (unknown_lease, late_finalizer_lease, successor_lease),
            )
            lease_outcomes = {
                row["lease_id"]: row["outcome"] for row in cursor.fetchall()
            }
            case.assertEqual("WRITE_OUTCOME_UNKNOWN", lease_outcomes[unknown_lease])
            case.assertEqual(
                "WRITE_OUTCOME_UNKNOWN",
                lease_outcomes[late_finalizer_lease],
            )
            case.assertEqual("SUCCEEDED", lease_outcomes[successor_lease])

        archival_repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        locked_history = archival_repository.lock_project_generation_history(
            archival_automation_id,
            committed_generation=2,
        )
        case.assertEqual(
            [(1, "BLOCKED", True), (2, "COMMITTED", False)],
            [
                (
                    row["generation"],
                    row["state"],
                    row.get("_archival_unknown_write", False),
                )
                for row in locked_history
            ],
        )
        connection.rollback()

        archival_repository.acquire_committed_generation_lease_row(
            archival_automation_id,
            expected_generation=2,
            expected_manifest_sha256=manifest_sha256,
            lease_id=current_unknown_lease,
            orchestration_run_id=run_id,
            expires_at=datetime.now() + timedelta(minutes=5),
            lease_owner="integration-current-unknown",
        )
        archival_repository.release_generation_lease_row(
            current_unknown_lease,
            outcome="WRITE_OUTCOME_UNKNOWN",
        )
        connection.commit()

        locked_current = archival_repository.lock_project_generation_history(
            archival_automation_id,
            committed_generation=2,
        )
        case.assertEqual(
            [(1, "BLOCKED", True), (2, "COMMITTED", False)],
            [
                (
                    row["generation"],
                    row["state"],
                    row.get("_archival_unknown_write", False),
                )
                for row in locked_current
            ],
        )
        connection.rollback()

        repeat_lease = archival_repository.acquire_committed_generation_lease_row(
            archival_automation_id,
            expected_generation=2,
            expected_manifest_sha256=manifest_sha256,
            lease_id=repeat_after_unknown_lease,
            orchestration_run_id=run_id,
            expires_at=datetime.now() + timedelta(minutes=5),
            lease_owner="integration-repeat-after-unknown",
        )
        case.assertEqual("RUNNING", repeat_lease["outcome"])
        completed_repeat = archival_repository.release_generation_lease_row(
            repeat_after_unknown_lease,
            outcome="SUCCEEDED",
        )
        case.assertEqual("SUCCEEDED", completed_repeat["outcome"])
        connection.commit()

    # Durable activation transitions retain an exact scheduler/policy
    # before-image and support token-guarded reverse CAS, including first
    # install and cross-version retries.
    rollback_automation_id = f"reverse_cas_{uuid4().hex[:20]}"

    def version_material(version):
        version_tool_contract = {**tool_contract, "version": version}
        version_install_metadata = {
            **install_metadata,
            "install_root": f"/integration/plugins/{archival_plugin_id}/{version}",
        }
        version_manifest = {
            **manifest,
            "tool_contract": version_tool_contract,
        }
        version_runtime_descriptor = {
            **runtime_descriptor,
            "install_metadata": version_install_metadata,
        }
        return {
            "version": version,
            "tool_contract": version_tool_contract,
            "install_metadata": version_install_metadata,
            "manifest": version_manifest,
            "runtime_descriptor": version_runtime_descriptor,
            "package_sha256": _json_hash(
                {"plugin_id": archival_plugin_id, "version": version}
            ),
            "manifest_sha256": _json_hash(version_manifest),
        }

    version_two = version_material("2.0.0")
    version_three = version_material("3.0.0")

    def register_version(repository, material):
        repository.register_package_version(
            package={
                "plugin_id": archival_plugin_id,
                "display_name": "archival unknown integration",
                "description": "integration only",
            },
            version={
                "version": material["version"],
                "package_sha256": material["package_sha256"],
                "manifest_sha256": material["manifest_sha256"],
                "manifest_json": material["manifest"],
                "tool_contract_sha256": _json_hash(material["tool_contract"]),
                "config_schema_sha256": _json_hash(manifest["config_schema"]),
                "allowed_entrypoints_sha256": _json_hash([]),
                "invocation_contracts_sha256": _json_hash(invocation_contracts),
                "worker_requirement_sha256": _json_hash(
                    manifest["worker_requirement"]
                ),
                "runtime_sha256": _json_hash(runtime),
                "scheduling_sha256": _json_hash(scheduling),
                "project_full_auto_allowed": True,
                "trust_source": "ed25519_first_party",
                "install_root_metadata_json": material["install_metadata"],
                "install_root_metadata_sha256": _json_hash(
                    material["install_metadata"]
                ),
                "installed_by_actor_id": "integration-admin",
            },
        )

    def versioned_snapshot(config, generation, material):
        snapshot = generation_snapshot(
            config,
            generation,
            instance_id=rollback_automation_id,
        )
        snapshot.update(
            {
                "plugin_version": material["version"],
                "package_sha256": material["package_sha256"],
                "manifest_sha256": material["manifest_sha256"],
                "tool_contract_sha256": _json_hash(material["tool_contract"]),
                "runtime_descriptor_sha256": _json_hash(
                    material["runtime_descriptor"]
                ),
            }
        )
        snapshot["execution_metadata"] = {
            **snapshot["execution_metadata"],
            "action_contract": material["tool_contract"],
            "runtime_descriptor": material["runtime_descriptor"],
        }
        return snapshot

    with case._connection(database) as connection:
        rollback_repository = AutomationPluginRepository(
            connection,
            cursor_factory=case.pymysql.cursors.DictCursor,
        )
        register_version(rollback_repository, version_two)
        register_version(rollback_repository, version_three)
        rollback_repository.install_project_instance(
            {
                "automation_id": rollback_automation_id,
                "plugin_id": archival_plugin_id,
                "plugin_version": "1.0.0",
                "display_name": "activation rollback integration",
                "install_request_id": str(uuid4()),
                "install_payload_sha256": _json_hash(
                    {"automation_id": rollback_automation_id}
                ),
                "installed_by_actor_id": "integration-admin",
                "migration_authority": False,
            }
        )
        initial_config = rollback_repository.initialize_project_config(
            rollback_automation_id,
            enabled_entrypoints=(),
        )
        rollback_config = rollback_repository.save_project_config(
            rollback_automation_id,
            config={},
            account_bindings={},
            resource_bindings={},
            enabled_entrypoints=(),
            schedule={"kind": "none", "times": [], "enabled": False},
            compiled_invocations={},
            contract_witness={
                "runtime_model": "ACTION_V1",
                "allowed_entrypoints": [],
                "invocation_contracts": invocation_contracts,
                "scheduling": scheduling,
            },
            device_binding=None,
            actor_id="integration-admin",
            actor_role="super_admin",
            request_id=str(uuid4()),
            expected_project_configuration_version=int(
                initial_config["config_version"]
            ),
        )
        first_snapshot = generation_snapshot(
            rollback_config,
            1,
            instance_id=rollback_automation_id,
        )
        prepare_generation(rollback_repository, first_snapshot, None)
        rollback_repository.commit_generation_cas_row(
            rollback_automation_id,
            1,
            expected_committed_generation=None,
        )
        connection.commit()

        committed_first = rollback_repository.get_generation_row(
            rollback_automation_id,
            1,
        )
        first_token = committed_first["activation_transition_token"]
        rollback_repository.rollback_generation_cas_row(
            rollback_automation_id,
            1,
            expected_base_committed_generation=None,
            expected_transition_token=first_token,
        )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT committed_generation, target_generation, state, "
                "reconcile_state FROM automation_projects WHERE automation_id=%s",
                (rollback_automation_id,),
            )
            project = cursor.fetchone()
            case.assertIsNone(project["committed_generation"])
            case.assertEqual(1, project["target_generation"])
            case.assertEqual("INSTALLED", project["state"])
            case.assertEqual("READY_TO_COMMIT", project["reconcile_state"])
            cursor.execute(
                "SELECT COUNT(*) AS n FROM scheduled_tasks WHERE automation_id=%s",
                (rollback_automation_id,),
            )
            case.assertEqual(0, cursor.fetchone()["n"])
        rollback_repository.rollback_generation_cas_row(
            rollback_automation_id,
            1,
            expected_base_committed_generation=None,
            expected_transition_token=first_token,
        )
        rollback_repository.commit_generation_cas_row(
            rollback_automation_id,
            1,
            expected_committed_generation=None,
        )
        retried_first = rollback_repository.get_generation_row(
            rollback_automation_id,
            1,
        )
        case.assertNotEqual(
            first_token,
            retried_first["activation_transition_token"],
        )
        rollback_repository.complete_generation_activation_row(
            rollback_automation_id,
            1,
            expected_transition_token=retried_first[
                "activation_transition_token"
            ],
        )
        connection.commit()

        old_task_id = uuid4().hex
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, automation_id, automation_generation, name,
                    tool_name, tool_params, cron_expression, enabled,
                    last_status, last_duration_ms, last_message,
                    configuration_version
                ) VALUES (%s, %s, 1, %s, %s, %s, %s, TRUE,
                          'success', 17, 'old result', %s)
                """,
                (
                    old_task_id,
                    rollback_automation_id,
                    "Administrator supplied name",
                    f"automation.{rollback_automation_id}.run",
                    json.dumps({"marker": "old"}),
                    "0 9 * * *",
                    int(rollback_config["config_version"]),
                ),
            )
            cursor.execute(
                """
                INSERT INTO scheduled_task_approval_policies (
                    task_id, mode, contract_hash, contract_snapshot_json,
                    tool_contract_hash, approved_by_actor_id,
                    approved_by_actor_role, approved_by_actor_display_name,
                    approved_at, comment, version
                ) VALUES (
                    %s, 'EXACT_SCHEDULE_EXEMPT', %s, %s, %s,
                    'integration-admin', 'super_admin', 'Integration Admin',
                    NOW(6), 'retain exact approval', 7
                )
                """,
                (
                    old_task_id,
                    "a" * 64,
                    json.dumps({"scope": "exact"}),
                    "b" * 64,
                ),
            )
        connection.commit()

        second_snapshot = generation_snapshot(
            rollback_config,
            2,
            instance_id=rollback_automation_id,
        )
        prepare_generation(rollback_repository, second_snapshot, 1)
        rollback_repository.commit_generation_cas_row(
            rollback_automation_id,
            2,
            expected_committed_generation=1,
        )
        connection.commit()
        committed_second = rollback_repository.get_generation_row(
            rollback_automation_id,
            2,
        )
        second_token = committed_second["activation_transition_token"]
        blocking_lease = str(uuid4())
        with case.assertRaises(ConcurrentUpdateError):
            rollback_repository.acquire_committed_generation_lease_row(
                rollback_automation_id,
                expected_generation=2,
                expected_manifest_sha256=manifest_sha256,
                lease_id=blocking_lease,
                orchestration_run_id=run_id,
                expires_at=datetime.now() + timedelta(minutes=5),
                lease_owner="reverse-cas-blocker",
            )
        connection.rollback()
        # Simulate a persisted lease written outside the supported repository
        # boundary after the transition. Even a completed outcome must make
        # reverse CAS fail closed; outcome filtering would lose that evidence.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_project_generation_leases (
                    lease_id, automation_id, generation,
                    orchestration_run_id, lease_owner,
                    runtime_metadata_json, runtime_metadata_sha256,
                    outcome, acquired_at, expires_at, released_at
                ) VALUES (
                    %s, %s, 2, %s, %s, %s, %s,
                    'SUCCEEDED', NOW(6), %s, NOW(6)
                )
                """,
                (
                    blocking_lease,
                    rollback_automation_id,
                    run_id,
                    "reverse-cas-blocker",
                    json.dumps({}),
                    _json_hash({}),
                    datetime.now() + timedelta(minutes=5),
                ),
            )
        connection.commit()
        with case.assertRaises(ConcurrentUpdateError):
            rollback_repository.rollback_generation_cas_row(
                rollback_automation_id,
                2,
                expected_base_committed_generation=1,
                expected_transition_token=second_token,
            )
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM automation_project_generation_leases
                WHERE lease_id=%s AND automation_id=%s AND generation=2
                """,
                (blocking_lease, rollback_automation_id),
            )
            case.assertEqual(1, cursor.rowcount)
        connection.commit()
        rollback_repository.rollback_generation_cas_row(
            rollback_automation_id,
            2,
            expected_base_committed_generation=1,
            expected_transition_token=second_token,
        )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task.name, task.automation_generation,
                       task.tool_params, task.last_status,
                       task.last_duration_ms, task.last_message,
                       policy.mode, policy.contract_hash,
                       policy.contract_snapshot_json,
                       policy.tool_contract_hash, policy.comment,
                       policy.version
                FROM scheduled_tasks AS task
                JOIN scheduled_task_approval_policies AS policy
                  ON policy.task_id=task.id
                WHERE task.id=%s
                """,
                (old_task_id,),
            )
            restored_task = cursor.fetchone()
            case.assertEqual("Administrator supplied name", restored_task["name"])
            case.assertEqual(1, restored_task["automation_generation"])
            case.assertEqual(
                {"marker": "old"},
                json.loads(restored_task["tool_params"]),
            )
            case.assertEqual("success", restored_task["last_status"])
            case.assertEqual(17, restored_task["last_duration_ms"])
            case.assertEqual("old result", restored_task["last_message"])
            case.assertEqual("EXACT_SCHEDULE_EXEMPT", restored_task["mode"])
            case.assertEqual("a" * 64, restored_task["contract_hash"])
            case.assertEqual(
                {"scope": "exact"},
                json.loads(restored_task["contract_snapshot_json"]),
            )
            case.assertEqual("b" * 64, restored_task["tool_contract_hash"])
            case.assertEqual("retain exact approval", restored_task["comment"])
            case.assertEqual(7, restored_task["version"])
        rollback_repository.rollback_generation_cas_row(
            rollback_automation_id,
            2,
            expected_base_committed_generation=1,
            expected_transition_token=second_token,
        )
        rollback_repository.commit_generation_cas_row(
            rollback_automation_id,
            2,
            expected_committed_generation=1,
        )
        retried_second = rollback_repository.get_generation_row(
            rollback_automation_id,
            2,
        )
        rollback_repository.complete_generation_activation_row(
            rollback_automation_id,
            2,
            expected_transition_token=retried_second[
                "activation_transition_token"
            ],
        )
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_projects
                SET plugin_version='2.0.0', state='UPGRADING',
                    record_version=record_version+1
                WHERE automation_id=%s
                """,
                (rollback_automation_id,),
            )
        connection.commit()
        third_snapshot = versioned_snapshot(
            rollback_config,
            3,
            version_two,
        )
        prepare_generation(rollback_repository, third_snapshot, 2)
        rollback_repository.commit_generation_cas_row(
            rollback_automation_id,
            3,
            expected_committed_generation=2,
        )
        connection.commit()
        committed_third = rollback_repository.get_generation_row(
            rollback_automation_id,
            3,
        )
        third_token = committed_third["activation_transition_token"]
        rollback_repository.rollback_generation_cas_row(
            rollback_automation_id,
            3,
            expected_base_committed_generation=2,
            expected_transition_token=third_token,
        )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT plugin_version, state, committed_generation, "
                "target_generation, reconcile_state FROM automation_projects "
                "WHERE automation_id=%s",
                (rollback_automation_id,),
            )
            rolled_back_upgrade = cursor.fetchone()
            case.assertEqual("1.0.0", rolled_back_upgrade["plugin_version"])
            case.assertEqual("UPGRADING", rolled_back_upgrade["state"])
            case.assertEqual(2, rolled_back_upgrade["committed_generation"])
            case.assertEqual(3, rolled_back_upgrade["target_generation"])
            case.assertEqual(
                "READY_TO_COMMIT",
                rolled_back_upgrade["reconcile_state"],
            )
        rollback_repository.commit_generation_cas_row(
            rollback_automation_id,
            3,
            expected_committed_generation=2,
        )
        retried_third = rollback_repository.get_generation_row(
            rollback_automation_id,
            3,
        )
        connection.commit()
        rollback_repository.rollback_generation_cas_row(
            rollback_automation_id,
            3,
            expected_base_committed_generation=2,
            expected_transition_token=retried_third[
                "activation_transition_token"
            ],
        )
        connection.commit()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_projects
                SET plugin_version='3.0.0', state='UPGRADING',
                    record_version=record_version+1
                WHERE automation_id=%s
                """,
                (rollback_automation_id,),
            )
        connection.commit()
        with case.assertRaises(ConcurrentUpdateError):
            rollback_repository.commit_generation_cas_row(
                rollback_automation_id,
                3,
                expected_committed_generation=2,
            )
        connection.rollback()
