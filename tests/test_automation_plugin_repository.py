from __future__ import annotations

import base64
import hashlib
from datetime import datetime
import uuid
from typing import Any
from unittest import TestCase

from shared.automation_plugin_repository import (
    AutomationPluginRepository,
    _configuration_target_generation,
    _normalized_project_schedule,
    _normalized_worker_identity,
    _prepared_configuration_upgrade_stage,
    _schedule_expressions,
    _schedule_from_rows,
    _stable_schedule_task_id,
    _validated_generation_row,
    _validated_worker_inbound_envelope,
    _worker_status_body,
)
from shared.automation_plugin_generation_unknown_write_repository import (
    lock_archival_unknown_predecessor,
)
from shared.automation_project_policy_repository import AutomationProjectPolicyRepository
from shared.feishu_approval_repository import FeishuApprovalRepository
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
    OrchestrationPersistenceError,
    _json_hash,
)


class _Cursor:
    rowcount = 0

    def execute(self, _sql, _params=None):
        return None

    def close(self):
        return None


class _Connection:
    def cursor(self):
        return _Cursor()


class _ScriptedCursor:
    def __init__(self, actions):
        self._actions = list(actions)
        self._row = None
        self.rowcount = 0
        self.executions = []

    def execute(self, sql, params=None):
        if not self._actions:
            raise AssertionError(f"unexpected SQL: {sql}")
        marker, row, rowcount = self._actions.pop(0)
        if marker not in " ".join(str(sql).split()):
            raise AssertionError(f"expected SQL containing {marker!r}: {sql}")
        self._row = row
        self.rowcount = rowcount
        self.executions.append((sql, params))

    def fetchone(self):
        return None if isinstance(self._row, list) else self._row

    def fetchall(self):
        return self._row if isinstance(self._row, list) else []

    def close(self):
        return None


class _ScriptedConnection:
    def __init__(self, actions):
        self.cursor_instance = _ScriptedCursor(actions)

    def cursor(self):
        return self.cursor_instance


class _BlockedUpgradeCursor:
    def __init__(self) -> None:
        self.rowcount = 0
        self._row = None
        self.executions = []

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.executions.append((normalized, params))
        self.rowcount = 0
        if normalized.startswith(("INSERT", "UPDATE", "DELETE")):
            raise AssertionError(
                f"blocked upgrade mutated persistence before rejection: {normalized}"
            )
        if "FROM automation_project_events" in normalized:
            self._row = None
        elif "FROM automation_projects" in normalized:
            self._row = {
                "automation_id": "instance-one",
                "plugin_id": "plugin-one",
                "plugin_version": "1.0.0",
                "display_name": "Instance one",
                "state": "ENABLED",
                "enabled": 1,
                "record_version": 7,
                "target_generation": 1,
                "committed_generation": 1,
                "reconcile_state": "BLOCKED_UNKNOWN_WRITE",
            }
        elif "FROM automation_project_configs" in normalized:
            _project, config, _policy = _configuration_save_rows()
            self._row = config
        elif "FROM scheduled_tasks" in normalized:
            self._row = []
        elif "FROM automation_plugin_versions" in normalized:
            self._row = {"package_sha256": "b" * 64}
        elif "COUNT(*) AS max_generation" in normalized:
            self._row = {"max_generation": 1}
        elif "FROM automation_project_generation_leases" in normalized:
            self._row = {"unknown_count": 1}
        elif "FROM automation_project_generations" in normalized:
            if "ORDER BY generation" in normalized:
                self._row = [{"generation": 1, "state": "BLOCKED"}]
            else:
                self._row = {"generation": 1, "state": "BLOCKED"}
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return None if isinstance(self._row, list) else self._row

    def fetchall(self):
        return self._row if isinstance(self._row, list) else []

    def close(self):
        return None


class _BlockedUpgradeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _BlockedUpgradeCursor()

    def cursor(self):
        return self.cursor_instance


def _configuration_save_rows():
    schedule = {"kind": "none", "times": [], "enabled": False}
    compiled = {
        "console": {
            "arguments": {"marker": "A"},
            "dynamic_resolvers": {},
        }
    }
    project = {
        "automation_id": "instance-one",
        "plugin_id": "plugin-one",
        "plugin_version": "1.0.0",
        "display_name": "Instance one",
        "state": "ENABLED",
        "record_version": 7,
        "target_generation": 1,
        "committed_generation": 1,
        "reconcile_state": "STABLE",
        "manifest_json": {
            "allowed_entrypoints": ["console"],
            "scheduling": {
                "supported": False,
                "allowed_kinds": [],
                "max_daily_times": 0,
            },
            "tool_contract": {"name": "automation.instance-one.run"},
            "runtime": {"kind": "python_subprocess"},
        },
    }
    config = {
        "automation_id": "instance-one",
        "config_json": {"marker": "A"},
        "config_sha256": _json_hash({"marker": "A"}),
        "account_bindings_json": {},
        "account_bindings_sha256": _json_hash({}),
        "resource_bindings_json": {},
        "resource_bindings_sha256": _json_hash({}),
        "enabled_entrypoints_json": ["console"],
        "enabled_entrypoints_sha256": _json_hash(["console"]),
        "desired_schedule_json": schedule,
        "desired_schedule_sha256": _json_hash(schedule),
        "compiled_invocations_json": compiled,
        "compiled_invocations_sha256": _json_hash(compiled),
        "device_id": None,
        "device_binding_sha256": _json_hash(None),
        "configured": 1,
        "config_version": 2,
    }
    policy = {
        "automation_id": "instance-one",
        "mode": "PROJECT_FULL_AUTO",
        "project_generation": 1,
        "project_configuration_version": 2,
        "version": 4,
        "contract_snapshot_json": {"schema_version": 1},
    }
    return project, config, policy


def _save_configuration(
    repository,
    *,
    request_id: str | None = None,
    marker: str = "B",
):
    return repository.save_project_config(
        "instance-one",
        config={"marker": marker},
        account_bindings={},
        resource_bindings={},
        enabled_entrypoints=("console",),
        schedule={"kind": "none", "times": [], "enabled": False},
        compiled_invocations={
            "console": {
                "arguments": {"marker": marker},
                "dynamic_resolvers": {},
            }
        },
        device_binding=None,
        actor_id="admin-one",
        actor_role="super_admin",
        request_id=request_id or str(uuid.uuid4()),
        expected_project_configuration_version=2,
    )


def _worker_envelope(*, kind, body, sequence=0, message_id=None):
    return {
        "schema_version": 1,
        "message_id": message_id or str(uuid.uuid4()),
        "device_id": "office-device",
        "sequence": sequence,
        "issued_at": "2026-08-15T08:00:00Z",
        "expires_at": "2026-08-15T08:05:00Z",
        "kind": kind,
        "body": body,
        "key_id": "device-key",
        "signature": "signed",
    }


class AutomationPluginRepositoryTests(TestCase):
    def test_plugin_state_change_is_audited_with_the_same_cas(self):
        request_id = str(uuid.uuid4())
        project = {
            "automation_id": "instance-one",
            "state": "ENABLED",
            "enabled": 1,
            "record_version": 7,
        }
        connection = _ScriptedConnection(
            [
                ("FROM automation_projects", project, 0),
                ("FROM automation_project_events", None, 0),
                ("UPDATE automation_projects", None, 1),
                ("INSERT INTO automation_project_events", None, 1),
            ]
        )

        changed = AutomationPluginRepository(
            connection
        ).set_project_enabled_with_audit(
            "instance-one",
            enabled=False,
            expected_record_version=7,
            actor_id="admin-one",
            actor_role="super_admin",
            request_id=request_id,
        )

        self.assertEqual("DISABLED", changed["state"])
        self.assertFalse(changed["enabled"])
        self.assertEqual(8, changed["record_version"])
        insert_sql, insert_params = connection.cursor_instance.executions[3]
        self.assertIn("PLUGIN_STATE_CHANGED", insert_sql)
        self.assertEqual("instance-one", insert_params[0])
        self.assertEqual(request_id, insert_params[1])
        self.assertEqual("ENABLED", insert_params[2])
        self.assertEqual("DISABLED", insert_params[3])
        self.assertEqual("admin-one", insert_params[6])
        self.assertEqual("super_admin", insert_params[7])

    def test_plugin_state_response_loss_replays_without_second_update(self):
        request_id = str(uuid.uuid4())
        payload = {
            "enabled": False,
            "expected_record_version": 7,
        }
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_projects",
                    {
                        "automation_id": "instance-one",
                        "state": "DISABLED",
                        "enabled": 0,
                        "record_version": 8,
                    },
                    0,
                ),
                (
                    "FROM automation_project_events",
                    {
                        "event_type": "PLUGIN_STATE_CHANGED",
                        "metadata_json": {
                            "request_payload_sha256": _json_hash(payload),
                        },
                        "actor_id": "admin-one",
                        "actor_role": "super_admin",
                    },
                    0,
                ),
            ]
        )

        replayed = AutomationPluginRepository(
            connection
        ).set_project_enabled_with_audit(
            "instance-one",
            enabled=False,
            expected_record_version=7,
            actor_id="admin-one",
            actor_role="super_admin",
            request_id=request_id,
        )

        self.assertEqual("DISABLED", replayed["state"])
        self.assertEqual(2, len(connection.cursor_instance.executions))

    def test_plugin_state_request_id_cannot_be_reused_with_new_input(self):
        request_id = str(uuid.uuid4())
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_projects",
                    {
                        "automation_id": "instance-one",
                        "state": "DISABLED",
                        "enabled": 0,
                        "record_version": 8,
                    },
                    0,
                ),
                (
                    "FROM automation_project_events",
                    {
                        "event_type": "PLUGIN_STATE_CHANGED",
                        "metadata_json": {
                            "request_payload_sha256": _json_hash(
                                {
                                    "enabled": False,
                                    "expected_record_version": 7,
                                }
                            ),
                        },
                        "actor_id": "admin-one",
                        "actor_role": "super_admin",
                    },
                    0,
                ),
            ]
        )

        with self.assertRaisesRegex(IdempotencyConflict, "different input"):
            AutomationPluginRepository(
                connection
            ).set_project_enabled_with_audit(
                "instance-one",
                enabled=True,
                expected_record_version=7,
                actor_id="admin-one",
                actor_role="super_admin",
                request_id=request_id,
            )

    def test_plugin_state_request_id_cannot_be_replayed_by_another_actor(self):
        request_id = str(uuid.uuid4())
        payload = {
            "enabled": False,
            "expected_record_version": 7,
        }
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_projects",
                    {
                        "automation_id": "instance-one",
                        "state": "DISABLED",
                        "enabled": 0,
                        "record_version": 8,
                    },
                    0,
                ),
                (
                    "FROM automation_project_events",
                    {
                        "event_type": "PLUGIN_STATE_CHANGED",
                        "metadata_json": {
                            "request_payload_sha256": _json_hash(payload),
                        },
                        "actor_id": "admin-one",
                        "actor_role": "super_admin",
                    },
                    0,
                ),
            ]
        )

        with self.assertRaisesRegex(IdempotencyConflict, "different input"):
            AutomationPluginRepository(
                connection
            ).set_project_enabled_with_audit(
                "instance-one",
                enabled=False,
                expected_record_version=7,
                actor_id="admin-two",
                actor_role="super_admin",
                request_id=request_id,
            )

    def test_plugin_state_replay_rejects_later_project_drift(self):
        request_id = str(uuid.uuid4())
        payload = {
            "enabled": False,
            "expected_record_version": 7,
        }
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_projects",
                    {
                        "automation_id": "instance-one",
                        "state": "DISABLED",
                        "enabled": 0,
                        "record_version": 9,
                    },
                    0,
                ),
                (
                    "FROM automation_project_events",
                    {
                        "event_type": "PLUGIN_STATE_CHANGED",
                        "metadata_json": {
                            "request_payload_sha256": _json_hash(payload),
                        },
                        "actor_id": "admin-one",
                        "actor_role": "super_admin",
                    },
                    0,
                ),
            ]
        )

        with self.assertRaisesRegex(IdempotencyConflict, "changed after"):
            AutomationPluginRepository(
                connection
            ).set_project_enabled_with_audit(
                "instance-one",
                enabled=False,
                expected_record_version=7,
                actor_id="admin-one",
                actor_role="super_admin",
                request_id=request_id,
            )

    def test_archival_commit_rejects_active_predecessor_lease(self):
        cursor = _ScriptedCursor(
            [
                (
                    "FROM automation_project_generations",
                    {"state": "BLOCKED", "error_code": "WRITE_OUTCOME_UNKNOWN"},
                    0,
                ),
                (
                    "FROM automation_project_generation_leases",
                    [
                        {"lease_id": "unknown", "outcome": "WRITE_OUTCOME_UNKNOWN"},
                        {"lease_id": "active", "outcome": "VERIFYING"},
                    ],
                    0,
                ),
            ]
        )

        with self.assertRaisesRegex(ConcurrentUpdateError, "active runtime leases"):
            lock_archival_unknown_predecessor(
                cursor,
                automation_id="instance-one",
                expected_committed=1,
            )
        self.assertIn(
            "outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')",
            " ".join(cursor.executions[1][0].split()),
        )

    def test_archival_unknown_predecessor_closes_completed_drain(self):
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_projects",
                    {"committed_generation": 2},
                    0,
                ),
                (
                    "FROM automation_project_generations",
                    {"state": "DRAINING"},
                    0,
                ),
                (
                    "FROM automation_project_generation_leases",
                    [{"lease_id": "unknown"}],
                    0,
                ),
                ("UPDATE automation_project_generations", None, 1),
                ("UPDATE automation_projects AS project", None, 1),
            ]
        )

        AutomationPluginRepository(connection).block_generation_unknown_write_row(
            "instance-one",
            1,
        )

        stable_sql = " ".join(
            connection.cursor_instance.executions[-1][0].split()
        )
        self.assertIn("project.reconcile_state='STABLE'", stable_sql)
        self.assertIn(
            "project.target_generation=project.committed_generation",
            stable_sql,
        )
        self.assertIn("archival_lease.outcome='WRITE_OUTCOME_UNKNOWN'", stable_sql)
        self.assertIn(
            "active_lease.outcome IN ('RUNNING', 'VERIFYING')",
            stable_sql,
        )

    def test_generic_upgrade_replays_event_from_before_prepared_target_support(self):
        request_id = str(uuid.uuid4())
        payload = {
            "plugin_id": "plugin-one",
            "to_version": "2.0.0",
            "package_sha256": "b" * 64,
            "expected_record_version": 7,
        }
        project = {
            "automation_id": "instance-one",
            "plugin_id": "plugin-one",
            "plugin_version": "2.0.0",
            "record_version": 8,
        }
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_project_events",
                    {
                        "event_type": "PLUGIN_UPGRADE_STAGED",
                        "metadata_json": {
                            "request_payload_sha256": _json_hash(payload),
                        },
                        "actor_id": "admin-one",
                        "actor_role": "super_admin",
                    },
                    0,
                ),
                ("FROM automation_projects", project, 0),
            ]
        )
        repository = AutomationPluginRepository(connection)

        replayed = repository.stage_project_upgrade(
            "instance-one",
            plugin_id="plugin-one",
            from_version="1.0.0",
            to_version="2.0.0",
            package_sha256="b" * 64,
            request_id=request_id,
            actor_id="admin-one",
            actor_role="super_admin",
            expected_record_version=7,
        )

        self.assertFalse(replayed["_upgrade_staged_created"])

    def test_upgrade_reuses_a_matching_audited_empty_configuration_target(self):
        request_id = str(uuid.uuid4())
        prepared_request_id = str(uuid.uuid4())
        project, config, _policy = _configuration_save_rows()
        project["target_generation"] = 2
        project["reconcile_state"] = "PREPARING"
        persisted = {
            **project,
            "plugin_version": "2.0.0",
            "state": "UPGRADING",
            "record_version": 8,
        }
        connection = _ScriptedConnection(
            [
                ("FROM automation_project_events", None, 0),
                ("FROM automation_projects", project, 0),
                ("FROM automation_plugin_versions", {"package_sha256": "b" * 64}, 0),
                ("FROM automation_project_configs", config, 0),
                ("FROM scheduled_tasks", [], 0),
                (
                    "FROM automation_project_events",
                    {
                        "event_type": "CONFIGURATION_UPDATED",
                        "actor_id": "admin-one",
                        "actor_role": "super_admin",
                        "metadata_json": {
                            "request_payload_sha256": "a" * 64,
                            "to_project_configuration_version": 2,
                        },
                    },
                    0,
                ),
                (
                    "FROM automation_project_policies",
                    {
                        "project_generation": 2,
                        "project_configuration_version": 2,
                    },
                    0,
                ),
                (
                    "FROM automation_project_generations",
                    [{"generation": 1, "state": "COMMITTED"}],
                    0,
                ),
                ("INSERT INTO automation_project_events", None, 1),
                ("UPDATE automation_projects", None, 1),
                ("FROM automation_projects", persisted, 0),
            ]
        )
        repository = AutomationPluginRepository(connection)

        staged = repository.stage_project_upgrade(
            "instance-one",
            plugin_id="plugin-one",
            from_version="1.0.0",
            to_version="2.0.0",
            package_sha256="b" * 64,
            request_id=request_id,
            actor_id="admin-one",
            actor_role="super_admin",
            expected_record_version=7,
            prepared_configuration_request_id=prepared_request_id,
        )

        self.assertTrue(staged["_upgrade_staged_created"])
        self.assertEqual("2.0.0", staged["plugin_version"])

    def test_plugin_upgrade_rejects_blocked_committed_topology_before_mutation(self):
        connection = _BlockedUpgradeConnection()
        repository = AutomationPluginRepository(connection)

        with self.assertRaises(ConcurrentUpdateError):
            repository.stage_project_upgrade(
                "instance-one",
                plugin_id="plugin-one",
                from_version="1.0.0",
                to_version="2.0.0",
                package_sha256="b" * 64,
                request_id=str(uuid.uuid4()),
                actor_id="admin-one",
                actor_role="super_admin",
                expected_record_version=7,
            )

        self.assertFalse(
            any(
                sql.startswith(("INSERT", "UPDATE", "DELETE"))
                for sql, _params in connection.cursor_instance.executions
            )
        )

    def test_configuration_generation_advances_only_one_closed_stable_lineage(self):
        project, config, _policy = _configuration_save_rows()
        self.assertEqual(
            2,
            _configuration_target_generation(
                project,
                config,
                ({"generation": 1, "state": "COMMITTED"},),
            ),
        )

        self.assertEqual(
            3,
            _configuration_target_generation(
                project,
                config,
                (
                    {"generation": 1, "state": "COMMITTED"},
                    {"generation": 2, "state": "DISPOSED"},
                ),
            ),
        )
        self.assertEqual(
            1,
            _configuration_target_generation(
                {
                    **project,
                    "target_generation": 1,
                    "committed_generation": None,
                    "reconcile_state": "WAITING_COEFFECTS",
                },
                {**config, "configured": 0},
                (),
            ),
        )

        invalid_states = (
            ({**project, "reconcile_state": "PREPARING"}, config),
            ({**project, "target_generation": 2}, config),
            (
                {
                    **project,
                    "committed_generation": None,
                    "reconcile_state": "PREPARING",
                },
                {**config, "configured": 0},
            ),
        )
        for changed_project, changed_config in invalid_states:
            with self.subTest(project=changed_project):
                with self.assertRaises(ConcurrentUpdateError):
                    _configuration_target_generation(
                        changed_project,
                        changed_config,
                        ({"generation": 1, "state": "COMMITTED"},),
                    )
        with self.assertRaises(OrchestrationPersistenceError):
            _configuration_target_generation(
                project,
                config,
                ({"generation": 1, "state": "DISPOSED"},),
            )
        invalid_history = (
            (
                {"generation": 1, "state": "COMMITTED"},
                {"generation": 3, "state": "DISPOSED"},
            ),
            (
                {"generation": 1, "state": "COMMITTED"},
                {"generation": 2, "state": "FAILED"},
            ),
        )
        for rows in invalid_history:
            with self.subTest(rows=rows):
                with self.assertRaises(ConcurrentUpdateError):
                    _configuration_target_generation(project, config, rows)

    def test_configuration_generation_advances_past_proven_unknown_write_archive(self):
        project, config, _policy = _configuration_save_rows()
        project.update({"target_generation": 2, "committed_generation": 2})
        archived_history = (
            {
                "generation": 1,
                "state": "BLOCKED",
                "_archival_unknown_write": True,
            },
            {"generation": 2, "state": "COMMITTED"},
        )

        self.assertEqual(
            3,
            _configuration_target_generation(project, config, archived_history),
        )
        with self.assertRaises(ConcurrentUpdateError):
            _configuration_target_generation(
                project,
                config,
                (
                    {"generation": 1, "state": "BLOCKED"},
                    {"generation": 2, "state": "COMMITTED"},
                ),
            )

    def test_configuration_generation_rejects_current_unknown_write(self):
        project, config, _policy = _configuration_save_rows()
        project["reconcile_state"] = "BLOCKED_UNKNOWN_WRITE"
        blocked_current = {
            "generation": 1,
            "state": "BLOCKED",
            "_archival_unknown_write": True,
        }

        with self.assertRaises(ConcurrentUpdateError):
            _configuration_target_generation(
                project,
                config,
                (blocked_current,),
            )
        with self.assertRaises(ConcurrentUpdateError):
            _configuration_target_generation(
                project,
                config,
                ({"generation": 1, "state": "BLOCKED"},),
            )

    def test_configuration_unknown_write_archive_authority_is_release_only(self):
        repository = AutomationPluginRepository(_Connection())

        with self.assertRaisesRegex(ValueError, "belongs to first-party release"):
            repository.save_project_config(
                "instance-one",
                config={},
                account_bindings={},
                resource_bindings={},
                enabled_entrypoints=(),
                schedule={"kind": "none", "times": [], "enabled": False},
                compiled_invocations={},
                device_binding=None,
                actor_id="console-admin",
                actor_role="super_admin",
                request_id=str(uuid.uuid4()),
                expected_project_configuration_version=1,
                allow_blocked_unknown_write_archive=True,
            )

    def test_generation_stage_locks_current_unknown_write_evidence(self):
        project, config, _policy = _configuration_save_rows()
        project["reconcile_state"] = "BLOCKED_UNKNOWN_WRITE"
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_project_generations",
                    [
                        {
                            "generation": 1,
                            "state": "BLOCKED",
                            "error_code": "WRITE_OUTCOME_UNKNOWN",
                        }
                    ],
                    0,
                ),
                (
                    "FROM automation_project_generation_leases",
                    [
                        {
                            "generation": 1,
                            "lease_id": "lease-current",
                            "outcome": "WRITE_OUTCOME_UNKNOWN",
                        },
                        {
                            "generation": 1,
                            "lease_id": "lease-current-second",
                            "outcome": "WRITE_OUTCOME_UNKNOWN",
                        },
                    ],
                    0,
                ),
            ]
        )
        repository = AutomationPluginRepository(connection)

        stage = repository.lock_project_generation_stage(
            "instance-one",
            project=project,
            current_config=config,
            allow_current_unknown_write=True,
        )

        self.assertEqual(2, stage.target_generation)
        self.assertEqual(1, stage.committed_generation)
        self.assertEqual(
            ("instance-one", 1),
            connection.cursor_instance.executions[1][1],
        )

    def test_generation_stage_rejects_active_current_unknown_write_lease(self):
        project, config, _policy = _configuration_save_rows()
        project["reconcile_state"] = "BLOCKED_UNKNOWN_WRITE"
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_project_generations",
                    [
                        {
                            "generation": 1,
                            "state": "BLOCKED",
                            "error_code": "WRITE_OUTCOME_UNKNOWN",
                        }
                    ],
                    0,
                ),
                (
                    "FROM automation_project_generation_leases",
                    [
                        {
                            "generation": 1,
                            "lease_id": "lease-unknown",
                            "outcome": "WRITE_OUTCOME_UNKNOWN",
                        },
                        {
                            "generation": 1,
                            "lease_id": "lease-active",
                            "outcome": "VERIFYING",
                        },
                    ],
                    0,
                ),
            ]
        )

        with self.assertRaises(ConcurrentUpdateError):
            AutomationPluginRepository(connection).lock_project_generation_stage(
                "instance-one",
                project=project,
                current_config=config,
                allow_current_unknown_write=True,
            )

    def test_generation_stage_locks_unknown_write_archive_evidence(self):
        project, config, _policy = _configuration_save_rows()
        project.update({"target_generation": 2, "committed_generation": 2})
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_project_generations",
                    [
                        {
                            "generation": 1,
                            "state": "BLOCKED",
                            "error_code": "WRITE_OUTCOME_UNKNOWN",
                        },
                        {"generation": 2, "state": "COMMITTED"},
                    ],
                    0,
                ),
                (
                    "FROM automation_project_generation_leases",
                    [
                        {
                            "generation": 1,
                            "lease_id": "lease-one",
                            "outcome": "WRITE_OUTCOME_UNKNOWN",
                        }
                    ],
                    0,
                ),
            ]
        )
        repository = AutomationPluginRepository(connection)

        stage = repository.lock_project_generation_stage(
            "instance-one",
            project=project,
            current_config=config,
        )

        self.assertEqual(3, stage.target_generation)
        self.assertEqual(
            ("instance-one", 1),
            connection.cursor_instance.executions[1][1],
        )

    def test_plugin_upgrade_reuses_only_exact_empty_configuration_target(self):
        project, config, _policy = _configuration_save_rows()
        project["target_generation"] = 2
        project["reconcile_state"] = "PREPARING"

        stage = _prepared_configuration_upgrade_stage(
            project,
            config,
            ({"generation": 1, "state": "COMMITTED"},),
        )

        self.assertEqual(2, stage.target_generation)
        self.assertEqual(2, stage.prior_target_generation)
        self.assertEqual("PREPARING", stage.prior_reconcile_state)
        with self.assertRaises(ConcurrentUpdateError):
            _prepared_configuration_upgrade_stage(
                project,
                config,
                (
                    {"generation": 1, "state": "COMMITTED"},
                    {"generation": 2, "state": "PREPARED"},
                ),
            )

    def test_plugin_upgrade_reuses_configuration_target_from_current_unknown_write(self):
        project, config, _policy = _configuration_save_rows()
        project["target_generation"] = 2
        project["reconcile_state"] = "PREPARING"

        stage = _prepared_configuration_upgrade_stage(
            project,
            config,
            (
                {
                    "generation": 1,
                    "state": "BLOCKED",
                    "error_code": "WRITE_OUTCOME_UNKNOWN",
                    "_archival_unknown_write": True,
                },
            ),
            allow_current_unknown_write=True,
        )

        self.assertEqual(2, stage.target_generation)
        self.assertEqual(1, stage.committed_generation)

    def test_initial_configuration_stages_generation_one_and_binds_policy(self):
        project, config, _policy = _configuration_save_rows()
        initial_project = {
            **project,
            "record_version": 1,
            "target_generation": 1,
            "committed_generation": None,
            "reconcile_state": "WAITING_COEFFECTS",
        }
        initial_config = {**config, "configured": 0, "config_version": 1}
        inserted_policy = {
            "automation_id": "instance-one",
            "mode": "REQUIRE_EACH_RUN",
            "project_generation": 1,
            "project_configuration_version": 2,
            "version": 1,
            "contract_snapshot_json": None,
        }
        persisted = {
            **initial_config,
            "configured": 1,
            "config_version": 2,
            "config_json": {"marker": "B"},
        }
        connection = _ScriptedConnection(
            [
                ("FROM automation_projects AS project", initial_project, 0),
                ("FROM automation_project_configs", initial_config, 0),
                ("FROM automation_project_events", None, 0),
                ("FROM automation_project_generations", [], 0),
                ("FROM automation_project_policies", None, 0),
                ("FROM scheduled_tasks", [], 0),
                ("FROM scheduled_tasks AS task", [], 0),
                ("UPDATE automation_project_configs", None, 1),
                ("UPDATE automation_projects", None, 1),
                ("INSERT INTO automation_project_policies", None, 1),
                ("FROM automation_project_policies", inserted_policy, 0),
                ("INSERT INTO automation_project_policy_events", None, 1),
                ("UPDATE automation_project_policies", None, 1),
                ("INSERT INTO automation_project_events", None, 1),
                ("FROM automation_project_configs", persisted, 0),
                ("FROM scheduled_tasks", [], 0),
            ]
        )

        saved = AutomationPluginRepository(connection).save_project_config(
            "instance-one",
            config={"marker": "B"},
            account_bindings={},
            resource_bindings={},
            enabled_entrypoints=("console",),
            schedule={"kind": "none", "times": [], "enabled": False},
            compiled_invocations={
                "console": {
                    "arguments": {"marker": "B"},
                    "dynamic_resolvers": {},
                }
            },
            device_binding=None,
            actor_id="admin-one",
            actor_role="super_admin",
            request_id=str(uuid.uuid4()),
            expected_project_configuration_version=1,
        )

        self.assertEqual(2, saved["config_version"])
        project_sql, project_params = connection.cursor_instance.executions[8]
        self.assertIn("target_generation=%s", project_sql)
        self.assertEqual(
            (1, "instance-one", 1, 1, None, "WAITING_COEFFECTS", "ENABLED"),
            project_params,
        )
        insert_sql, insert_params = connection.cursor_instance.executions[9]
        self.assertIn("project_generation", insert_sql)
        self.assertEqual(("instance-one", 1, 2), insert_params)
        policy_update_params = connection.cursor_instance.executions[12][1]
        self.assertEqual((1, 2, "instance-one", 1), policy_update_params)

    def test_save_configuration_atomically_stages_next_generation_and_policy(self):
        project, config, policy = _configuration_save_rows()
        persisted = {
            **config,
            "config_json": {"marker": "B"},
            "config_sha256": _json_hash({"marker": "B"}),
            "compiled_invocations_json": {
                "console": {
                    "arguments": {"marker": "B"},
                    "dynamic_resolvers": {},
                }
            },
            "config_version": 3,
        }
        connection = _ScriptedConnection(
            [
                ("FROM automation_projects AS project", project, 0),
                ("FROM automation_project_configs", config, 0),
                ("FROM automation_project_events", None, 0),
                (
                    "FROM automation_project_generations",
                    [{"generation": 1, "state": "COMMITTED"}],
                    0,
                ),
                ("FROM automation_project_policies", policy, 0),
                ("FROM scheduled_tasks", [], 0),
                ("FROM scheduled_tasks AS task", [], 0),
                ("UPDATE automation_project_configs", None, 1),
                ("UPDATE automation_projects", None, 1),
                ("INSERT INTO automation_project_policy_events", None, 1),
                ("UPDATE automation_project_policies", None, 1),
                ("INSERT INTO automation_project_events", None, 1),
                ("FROM automation_project_configs", persisted, 0),
                ("FROM scheduled_tasks", [], 0),
            ]
        )

        saved = _save_configuration(AutomationPluginRepository(connection))

        self.assertEqual(3, saved["config_version"])
        executions = connection.cursor_instance.executions
        generation_sql, generation_params = executions[3]
        self.assertIn("ORDER BY generation FOR UPDATE", generation_sql)
        self.assertEqual(("instance-one",), generation_params)
        project_sql, project_params = executions[8]
        self.assertIn("target_generation=%s", project_sql)
        self.assertIn("committed_generation <=> %s", project_sql)
        self.assertEqual(
            (2, "instance-one", 7, 1, 1, "STABLE", "ENABLED"),
            project_params,
        )
        policy_event_sql, policy_event_params = executions[9]
        self.assertIn("project_generation", policy_event_sql)
        self.assertEqual("PROJECT_FULL_AUTO", policy_event_params[3])
        self.assertEqual(2, policy_event_params[4])
        policy_sql, policy_params = executions[10]
        self.assertIn("project_generation=%s", policy_sql)
        self.assertIn("version=%s", policy_sql)
        self.assertEqual((2, 3, "instance-one", 4), policy_params)

    def test_configuration_idempotency_does_not_advance_generation_twice(self):
        request_id = str(uuid.uuid4())
        project, config, _policy = _configuration_save_rows()
        persisted = {
            **config,
            "config_json": {"marker": "B"},
            "config_sha256": _json_hash({"marker": "B"}),
            "config_version": 3,
        }
        request_payload = {
            "config": {"marker": "B"},
            "account_bindings": {},
            "resource_bindings": {},
            "enabled_entrypoints": ["console"],
            "schedule": {"kind": "none", "times": [], "enabled": False},
            "compiled_invocations": {
                "console": {
                    "arguments": {"marker": "B"},
                    "dynamic_resolvers": {},
                }
            },
            "device_id": None,
            "expected_project_configuration_version": 2,
        }
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_projects AS project",
                    {
                        **project,
                        "target_generation": 2,
                        "reconcile_state": "PREPARING",
                    },
                    0,
                ),
                ("FROM automation_project_configs", persisted, 0),
                (
                    "FROM automation_project_events",
                    {
                        "event_type": "CONFIGURATION_UPDATED",
                        "metadata_json": {
                            "request_payload_sha256": _json_hash(request_payload),
                            "to_project_configuration_version": 3,
                        },
                        "actor_id": "admin-one",
                        "actor_role": "super_admin",
                    },
                    0,
                ),
                ("FROM automation_project_configs", persisted, 0),
                ("FROM scheduled_tasks", [], 0),
            ]
        )

        saved = _save_configuration(
            AutomationPluginRepository(connection),
            request_id=request_id,
        )

        self.assertEqual(3, saved["config_version"])
        sql_statements = [" ".join(sql.split()) for sql, _ in connection.cursor_instance.executions]
        self.assertFalse(any("automation_project_generations" in sql for sql in sql_statements))
        self.assertFalse(any(sql.startswith("UPDATE") for sql in sql_statements))

    def test_configuration_replay_rejects_actor_target_and_later_version_drift(self):
        request_id = str(uuid.uuid4())
        project, config, _policy = _configuration_save_rows()
        request_payload = {
            "config": {"marker": "B"},
            "account_bindings": {},
            "resource_bindings": {},
            "enabled_entrypoints": ["console"],
            "schedule": {"kind": "none", "times": [], "enabled": False},
            "compiled_invocations": {
                "console": {
                    "arguments": {"marker": "B"},
                    "dynamic_resolvers": {},
                }
            },
            "device_id": None,
            "expected_project_configuration_version": 2,
        }
        base_event = {
            "event_type": "CONFIGURATION_UPDATED",
            "metadata_json": {
                "request_payload_sha256": _json_hash(request_payload),
                "to_project_configuration_version": 3,
            },
            "actor_id": "admin-one",
            "actor_role": "super_admin",
        }
        scenarios = (
            (
                {**config, "config_version": 3},
                {**base_event, "actor_id": "admin-two"},
                "different input",
            ),
            (
                {**config, "config_version": 3},
                {
                    **base_event,
                    "metadata_json": {
                        **base_event["metadata_json"],
                        "to_project_configuration_version": 4,
                    },
                },
                "different input",
            ),
            (
                {**config, "config_version": 4},
                base_event,
                "changed after",
            ),
        )

        for current_config, event, message in scenarios:
            with self.subTest(message=message):
                connection = _ScriptedConnection(
                    [
                        ("FROM automation_projects AS project", project, 0),
                        ("FROM automation_project_configs", current_config, 0),
                        ("FROM automation_project_events", event, 0),
                    ]
                )
                with self.assertRaisesRegex(IdempotencyConflict, message):
                    _save_configuration(
                        AutomationPluginRepository(connection),
                        request_id=request_id,
                    )

    def test_repository_enforces_signed_schedule_kinds_and_daily_limit(self):
        project, _config, _policy = _configuration_save_rows()
        project = {
            **project,
            "manifest_json": {
                **project["manifest_json"],
                "allowed_entrypoints": ["console", "scheduler"],
                "scheduling": {
                    "supported": True,
                    "allowed_kinds": ["startup"],
                    "max_daily_times": 1,
                },
            },
        }

        def save(schedule: dict[str, Any]) -> None:
            AutomationPluginRepository(
                _ScriptedConnection(
                    [("FROM automation_projects AS project", project, 0)]
                )
            ).save_project_config(
                "instance-one",
                config={"marker": "B"},
                account_bindings={},
                resource_bindings={},
                enabled_entrypoints=("console", "scheduler"),
                schedule=schedule,
                compiled_invocations={
                    "console": {
                        "arguments": {"marker": "B"},
                        "dynamic_resolvers": {},
                    },
                    "scheduler": {
                        "arguments": {"marker": "B"},
                        "dynamic_resolvers": {},
                    },
                },
                device_binding=None,
                actor_id="admin-one",
                actor_role="super_admin",
                request_id=str(uuid.uuid4()),
                expected_project_configuration_version=2,
            )

        with self.assertRaisesRegex(
            OrchestrationPersistenceError,
            "signed plugin kinds",
        ):
            save(
                {
                    "kind": "daily_times",
                    "times": ["08:00"],
                    "enabled": True,
                }
            )

        daily_project = {
            **project,
            "manifest_json": {
                **project["manifest_json"],
                "scheduling": {
                    "supported": True,
                    "allowed_kinds": ["daily_times"],
                    "max_daily_times": 1,
                },
            },
        }
        project = daily_project
        with self.assertRaisesRegex(
            OrchestrationPersistenceError,
            "signed plugin daily limit",
        ):
            save(
                {
                    "kind": "daily_times",
                    "times": ["08:00", "09:00"],
                    "enabled": True,
                }
            )

    def test_configuration_change_preserves_the_selected_project_policy_mode(self):
        for mode in ("LEGACY_SCHEDULE_ONLY", "PROJECT_FULL_AUTO"):
            with self.subTest(mode=mode):
                project, config, policy = _configuration_save_rows()
                policy = {**policy, "mode": mode}
                persisted = {**config, "config_version": 3}
                connection = _ScriptedConnection(
                    [
                        ("FROM automation_projects AS project", project, 0),
                        ("FROM automation_project_configs", config, 0),
                        ("FROM automation_project_events", None, 0),
                        (
                            "FROM automation_project_generations",
                            [{"generation": 1, "state": "COMMITTED"}],
                            0,
                        ),
                        ("FROM automation_project_policies", policy, 0),
                        ("FROM scheduled_tasks", [], 0),
                        ("FROM scheduled_tasks AS task", [], 0),
                        ("UPDATE automation_project_configs", None, 1),
                        ("UPDATE automation_projects", None, 1),
                        ("INSERT INTO automation_project_policy_events", None, 1),
                        ("UPDATE automation_project_policies", None, 1),
                        ("INSERT INTO automation_project_events", None, 1),
                        ("FROM automation_project_configs", persisted, 0),
                        ("FROM scheduled_tasks", [], 0),
                    ]
                )

                _save_configuration(AutomationPluginRepository(connection))

                event_params = connection.cursor_instance.executions[9][1]
                self.assertEqual(mode, event_params[2])
                update_sql, update_params = connection.cursor_instance.executions[10]
                self.assertNotIn("SET mode=", update_sql)
                self.assertIn("contract_hash=NULL", update_sql)
                self.assertEqual((2, 3, "instance-one", 4), update_params)

    def test_save_configuration_rejects_nonstable_or_lost_project_cas(self):
        project, config, policy = _configuration_save_rows()
        nonstable = _ScriptedConnection(
            [
                (
                    "FROM automation_projects AS project",
                    {**project, "reconcile_state": "PREPARING"},
                    0,
                ),
                ("FROM automation_project_configs", config, 0),
                ("FROM automation_project_events", None, 0),
                (
                    "FROM automation_project_generations",
                    [{"generation": 1, "state": "COMMITTED"}],
                    0,
                ),
            ]
        )
        with self.assertRaises(ConcurrentUpdateError):
            _save_configuration(AutomationPluginRepository(nonstable))
        self.assertTrue(
            all(
                not " ".join(sql.split()).startswith("UPDATE")
                for sql, _params in nonstable.cursor_instance.executions
            )
        )

        lost_cas = _ScriptedConnection(
            [
                ("FROM automation_projects AS project", project, 0),
                ("FROM automation_project_configs", config, 0),
                ("FROM automation_project_events", None, 0),
                (
                    "FROM automation_project_generations",
                    [{"generation": 1, "state": "COMMITTED"}],
                    0,
                ),
                ("FROM automation_project_policies", policy, 0),
                ("FROM scheduled_tasks", [], 0),
                ("FROM scheduled_tasks AS task", [], 0),
                ("UPDATE automation_project_configs", None, 1),
                ("UPDATE automation_projects", None, 0),
            ]
        )
        with self.assertRaises(ConcurrentUpdateError):
            _save_configuration(AutomationPluginRepository(lost_cas))
        self.assertFalse(
            any(
                "automation_project_policy_events" in sql
                for sql, _params in lost_cas.cursor_instance.executions
            )
        )

        upgrading = _ScriptedConnection(
            [
                (
                    "FROM automation_projects AS project",
                    {**project, "state": "UPGRADING"},
                    0,
                )
            ]
        )
        with self.assertRaises(OrchestrationPersistenceError):
            _save_configuration(AutomationPluginRepository(upgrading))
        self.assertEqual(1, len(upgrading.cursor_instance.executions))

    def test_project_policy_repository_reads_exact_018_identity_backups(self):
        connection = _ScriptedConnection(
            [
                (
                    "FROM scheduled_task_automation_identity_backup_018",
                    [
                        {
                            "id": "task-a",
                            "name": "Task A",
                            "tool_name": "legacy-tool",
                            "tool_params": '{"account_id":"account-a"}',
                            "cron_expression": "0 9 * * *",
                            "enabled": 1,
                            "configuration_version": 1,
                            "updated_at": datetime(2026, 8, 15),
                        }
                    ],
                    0,
                )
            ]
        )
        repository = AutomationProjectPolicyRepository(connection)

        rows = repository.list_automation_identity_backup_rows_018(
            ("task-a",),
            for_update=True,
        )

        self.assertEqual({"account_id": "account-a"}, rows[0]["tool_params"])
        sql, params = connection.cursor_instance.executions[0]
        self.assertIn("WHERE BINARY id IN (%s)", " ".join(sql.split()))
        self.assertIn("FOR UPDATE", sql)
        self.assertEqual(("task-a",), params)
        with self.assertRaises(OrchestrationPersistenceError):
            repository.list_automation_identity_backup_rows_018(
                ("task-a", "task-a")
            )

    def test_project_change_invalidates_pending_approvals_and_wakes_runs(self):
        connection = _ScriptedConnection(
            [
                (
                    "FROM agent_runs AS run",
                    [{"run_id": "run-a"}, {"run_id": "run-b"}],
                    0,
                ),
                ("UPDATE approval_requests", None, 2),
                ("UPDATE agent_runs", None, 2),
            ]
        )
        repository = AutomationProjectPolicyRepository(connection)

        run_ids = repository.invalidate_pending_approvals_and_wake_runs(
            "instance-one"
        )

        self.assertEqual(("run-a", "run-b"), run_ids)
        select_sql, _ = connection.cursor_instance.executions[0]
        self.assertIn("ORDER BY run.run_id", " ".join(select_sql.split()))
        self.assertIn("FOR UPDATE", select_sql)
        approval_sql, approval_params = connection.cursor_instance.executions[1]
        self.assertIn("status='INVALIDATED'", approval_sql)
        self.assertIn("status IN ('PENDING', 'APPROVED')", approval_sql)
        self.assertEqual(("run-a", "run-b"), approval_params)
        run_sql, run_params = connection.cursor_instance.executions[2]
        self.assertIn("next_attempt_at=NOW(6)", run_sql)
        self.assertEqual(("run-a", "run-b"), run_params)

    def test_project_change_emits_feishu_queue_completion_for_invalidated_approval(self):
        connection = _ScriptedConnection(
            [
                (
                    "FROM agent_runs AS run",
                    [{"run_id": "run-a"}],
                    0,
                ),
                (
                    "FROM approval_requests AS approval",
                    [
                        {
                            "approval_id": "approval-a",
                            "work_item_id": "work-a",
                            "run_id": "run-a",
                            "plan_hash": "a" * 64,
                            "correlation_id": "correlation-a",
                            "causation_id": None,
                        }
                    ],
                    0,
                ),
                ("UPDATE approval_requests", None, 1),
                ("UPDATE agent_runs", None, 1),
            ]
        )
        repository = AutomationProjectPolicyRepository(connection)

        class _Events:
            def __init__(self):
                self.rows = []

            def append_with_outbox(self, event, deliveries):
                self.rows.append((dict(event), tuple(deliveries)))

        events = _Events()

        run_ids = repository.invalidate_pending_approvals_and_wake_runs(
            "instance-one",
            event_repository=events,
        )

        self.assertEqual(("run-a",), run_ids)
        self.assertEqual(1, len(events.rows))
        event, deliveries = events.rows[0]
        self.assertEqual("agent.approval.invalidated", event["event_type"])
        self.assertEqual("approval-a", event["entity_id"])
        self.assertEqual(
            {"orchestration.audit", "feishu.approval"},
            {row["consumer_name"] for row in deliveries},
        )

    def test_feishu_active_lookup_locks_binding_then_delivery_before_projection(self):
        connection = _ScriptedConnection(
            [
                ("FROM feishu_admin_bindings", {"binding_id": "binding-a"}, 0),
                ("FROM feishu_approval_deliveries", {"delivery_id": "d-1"}, 0),
                (
                    "JOIN approval_requests AS approval",
                    {"delivery_id": "d-1", "approval_id": "approval-a"},
                    0,
                ),
            ]
        )
        repository = FeishuApprovalRepository(connection)

        active = repository.active_for_binding("binding-a", for_update=True)

        self.assertEqual("approval-a", active["approval_id"])
        binding_lock_sql, _ = connection.cursor_instance.executions[0]
        delivery_lock_sql, _ = connection.cursor_instance.executions[1]
        projection_sql, _ = connection.cursor_instance.executions[2]
        self.assertIn("FOR UPDATE", binding_lock_sql)
        self.assertIn("FROM feishu_admin_bindings", binding_lock_sql)
        self.assertIn("FOR UPDATE", delivery_lock_sql)
        self.assertNotIn("approval_requests", delivery_lock_sql)
        self.assertNotIn("agent_runs", delivery_lock_sql)
        self.assertNotIn("FOR UPDATE", projection_sql)
        self.assertIn("command.automation_id AS automation_id", projection_sql)
        self.assertNotIn("run.automation_id", projection_sql)
        self.assertIn("FROM agent_run_steps AS step", projection_sql)
        self.assertIn("AS tool_names", projection_sql)
        self.assertNotIn("plan_json", projection_sql)

    def test_feishu_single_binding_finish_never_expands_to_other_bindings(self):
        connection = _ScriptedConnection(
            [
                ("FROM feishu_admin_bindings", {"binding_id": "binding-a"}, 0),
                ("UPDATE feishu_approval_deliveries", None, 1),
                ("FROM feishu_admin_bindings", {"binding_id": "binding-a"}, 0),
                ("FROM feishu_approval_deliveries", {"delivery_id": "d-next"}, 0),
                ("FROM feishu_admin_bindings", {"binding_id": "binding-a"}, 0),
                ("FROM feishu_approval_deliveries", {"delivery_id": "d-next"}, 0),
                (
                    "JOIN approval_requests AS approval",
                    {"delivery_id": "d-next", "approval_id": "approval-next"},
                    0,
                ),
            ]
        )
        repository = FeishuApprovalRepository(connection)

        binding_ids = repository.finish_active_for_binding(
            "binding-a",
            "approval-stale",
            status="SKIPPED",
        )

        self.assertEqual(["binding-a"], binding_ids)
        statements = [
            " ".join(str(sql).split())
            for sql, _params in connection.cursor_instance.executions
        ]
        self.assertFalse(
            any("SELECT DISTINCT binding_id" in sql for sql in statements)
        )
        update_sql, update_params = connection.cursor_instance.executions[1]
        self.assertIn("WHERE binding_id=%s AND approval_id=%s", update_sql)
        self.assertEqual(
            ("SKIPPED", "binding-a", "approval-stale"),
            update_params,
        )

    def test_worker_pairing_is_request_audited_and_identity_immutable(self):
        request_id = str(uuid.uuid4())
        public_key = b"p" * 32
        identity = {
            "device_key_id": "paired-key",
            "ed25519_public_key_base64": base64.b64encode(public_key).decode(
                "ascii"
            ),
            "tls_client_certificate_sha256": "c" * 64,
        }
        capabilities = {"interactive": True}
        row = {
            "device_id": "paired-device",
            "display_name": "Paired device",
            "platform": "windows",
            "agent_version": "1.0.0",
            "identity_json": identity,
            "identity_sha256": _json_hash(identity),
            "paired_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
            "capabilities_json": capabilities,
            "capabilities_sha256": _json_hash(capabilities),
            "service_state": "OFFLINE",
            "interactive_session_state": "LOGGED_OUT",
            "record_version": 1,
        }
        request = {
            field: row[field]
            for field in (
                "device_id",
                "display_name",
                "platform",
                "agent_version",
                "identity_json",
                "paired_public_key_fingerprint",
                "capabilities_json",
            )
        }
        connection = _ScriptedConnection(
            [
                ("FROM automation_worker_pairing_events", None, 0),
                ("FROM automation_worker_devices", None, 0),
                ("INSERT INTO automation_worker_devices", None, 1),
                ("FROM automation_worker_devices", row, 0),
                ("INSERT INTO automation_worker_pairing_events", None, 1),
            ]
        )
        persisted = AutomationPluginRepository(
            connection
        ).pair_device_with_audit(
            request,
            request_id=request_id,
            actor_id="admin-one",
            actor_role="super_admin",
        )
        self.assertEqual("paired-device", persisted["device_id"])
        event_sql, event_params = connection.cursor_instance.executions[-1]
        self.assertIn("automation_worker_pairing_events", event_sql)
        self.assertEqual(request_id, event_params[2])
        self.assertNotIn(identity["ed25519_public_key_base64"], event_params[3])

        audit_payload = {
            "device_id": "paired-device",
            "display_name": "Paired device",
            "platform": "windows",
            "agent_version": "1.0.0",
            "identity_sha256": _json_hash(identity),
            "paired_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
            "capabilities_sha256": _json_hash(capabilities),
        }
        existing_event = {
            "event_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"boyi:automation-worker-pairing:{request_id}",
                )
            ),
            "device_id": "paired-device",
            "request_id": request_id,
            "payload_sha256": _json_hash(audit_payload),
            "actor_id": "admin-one",
            "actor_role": "super_admin",
            "metadata_json": audit_payload,
        }
        replayed = AutomationPluginRepository(
            _ScriptedConnection(
                [
                    ("FROM automation_worker_pairing_events", existing_event, 0),
                    ("FROM automation_worker_devices", row, 0),
                ]
            )
        ).pair_device_with_audit(
            request,
            request_id=request_id,
            actor_id="admin-one",
            actor_role="super_admin",
        )
        self.assertEqual("paired-device", replayed["device_id"])

        with self.assertRaises(IdempotencyConflict):
            AutomationPluginRepository(
                _ScriptedConnection(
                    [("FROM automation_worker_pairing_events", existing_event, 0)]
                )
            ).pair_device_with_audit(
                {**request, "display_name": "Drifted device"},
                request_id=request_id,
                actor_id="admin-one",
                actor_role="super_admin",
            )

    def test_worker_pairing_identity_is_closed_and_device_reads_are_explicit(self):
        public_key = b"k" * 32
        identity = {
            "device_key_id": "office-key",
            "ed25519_public_key_base64": base64.b64encode(public_key).decode("ascii"),
            "tls_client_certificate_sha256": "e" * 64,
        }
        self.assertEqual(identity, _normalized_worker_identity(identity))
        for invalid in (
            {**identity, "extra": "forged"},
            {**identity, "ed25519_public_key_base64": "not-base64"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _normalized_worker_identity(invalid)

        row = {
            "device_id": "office-device",
            "paired_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
            "identity_json": identity,
            "capabilities_json": {"interactive": True},
            "service_state": "ONLINE",
            "interactive_session_state": "AVAILABLE",
            "record_version": 1,
        }
        one = AutomationPluginRepository(
            _ScriptedConnection(
                [("FROM automation_worker_devices WHERE device_id", row, 0)]
            )
        ).get_worker_device("office-device")
        self.assertEqual(identity, one["identity_json"])
        listed = AutomationPluginRepository(
            _ScriptedConnection(
                [("FROM automation_worker_devices ORDER BY device_id", [row], 0)]
            )
        ).list_worker_devices()
        self.assertEqual(["office-device"], [item["device_id"] for item in listed])

    def test_worker_package_download_requires_exact_live_dispatch_authorization(self):
        authorization_id = str(uuid.uuid4())
        metadata = {
            "install_root": "/srv/automation-plugins/plugin-one/1.0.0",
            "archive_relative": ".verified-package.zip",
            "archive_sha256": "a" * 64,
        }
        row = {
            "plugin_id": "plugin-one",
            "version": "1.0.0",
            "package_sha256": "a" * 64,
            "manifest_json": {"plugin_id": "plugin-one"},
            "install_root_metadata_json": metadata,
            "install_root_metadata_sha256": _json_hash(metadata),
            "job_id": str(uuid.uuid4()),
            "automation_id": "instance-one",
            "automation_generation": 2,
            "dispatch_authorization_id": authorization_id,
            "assigned_device_id": "office-device",
        }
        connection = _ScriptedConnection(
            [("FROM automation_worker_jobs AS job", row, 0)]
        )
        authorized = AutomationPluginRepository(
            connection
        ).authorize_worker_package_download(
            device_id="office-device",
            plugin_id="plugin-one",
            plugin_version="1.0.0",
            package_sha256="a" * 64,
            dispatch_authorization_id=authorization_id,
        )
        self.assertEqual(row["job_id"], authorized["job_id"])
        sql, params = connection.cursor_instance.executions[0]
        self.assertIn("job.job_type IN ('INSTALL', 'UPGRADE')", sql)
        self.assertIn("job.status='CLAIMED'", sql)
        self.assertIn("job.lease_expires_at > NOW(6)", sql)
        self.assertIn("JSON_EXTRACT(job.payload_json", sql)
        self.assertEqual(
            params,
            (
                "office-device",
                "office-device",
                "plugin-one",
                "1.0.0",
                "a" * 64,
                authorization_id,
            ),
        )

        drifted = {
            **row,
            "install_root_metadata_json": {**metadata, "archive_sha256": "b" * 64},
        }
        with self.assertRaisesRegex(
            OrchestrationPersistenceError,
            "package metadata failed integrity",
        ):
            AutomationPluginRepository(
                _ScriptedConnection(
                    [("FROM automation_worker_jobs AS job", drifted, 0)]
                )
            ).authorize_worker_package_download(
                device_id="office-device",
                plugin_id="plugin-one",
                plugin_version="1.0.0",
                package_sha256="a" * 64,
                dispatch_authorization_id=authorization_id,
            )

    def test_worker_install_job_requires_closed_package_identity(self):
        common = {
            "job_id": str(uuid.uuid4()),
            "automation_id": "instance-one",
            "automation_generation": 2,
            "plugin_id": "plugin-one",
            "plugin_version": "1.0.0",
            "request_id": str(uuid.uuid4()),
            "job_type": "INSTALL",
            "worker_requirement_json": {"required": True},
            "operation_type": "compute",
            "requires_interactive_session": False,
            "target_device_id": "office-device",
            "deadline_at": datetime(2026, 8, 15, 8, 5),
        }
        for payload in (
            {},
            {
                "package": {
                    "plugin_id": "plugin-one",
                    "version": "1.0.0",
                    "package_sha256": "a" * 64,
                    "url": "/forged/browser/url",
                }
            },
            {
                "package": {
                    "plugin_id": "other-plugin",
                    "version": "1.0.0",
                    "package_sha256": "a" * 64,
                }
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    AutomationPluginRepository(_Connection()).enqueue_job(
                        {**common, "payload_json": payload},
                        release_hold=False,
                    )

    def test_worker_inbound_envelope_and_status_are_closed(self):
        dispatch_message_id = str(uuid.uuid4())
        dispatch_authorization_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        body = {
            "job_id": job_id,
            "dispatch_message_id": dispatch_message_id,
            "dispatch_authorization_id": dispatch_authorization_id,
            "status": "SUCCEEDED",
            "process_confirmed": True,
            "result": {"ok": True},
            "error_code": None,
        }
        envelope = _worker_envelope(kind="JOB_STATUS", body=body)
        validated = _validated_worker_inbound_envelope(
            envelope,
            principal_device_id="office-device",
            expected_kind="JOB_STATUS",
        )
        self.assertEqual(0, validated["sequence"])
        self.assertEqual(body, _worker_status_body(body))

        for changed in (
            {**envelope, "device_id": "forged-device"},
            {**envelope, "extra": "forged"},
            {**envelope, "sequence": True},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(OrchestrationPersistenceError):
                    _validated_worker_inbound_envelope(
                        changed,
                        principal_device_id="office-device",
                        expected_kind="JOB_STATUS",
                    )
        with self.assertRaisesRegex(
            OrchestrationPersistenceError,
            "success requires confirmed execution",
        ):
            _worker_status_body({**body, "process_confirmed": False})

    def test_signed_worker_status_atomically_binds_exact_dispatch_and_lease(self):
        dispatch_message_id = str(uuid.uuid4())
        dispatch_authorization_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        result_message_id = str(uuid.uuid4())
        body = {
            "job_id": job_id,
            "dispatch_message_id": dispatch_message_id,
            "dispatch_authorization_id": dispatch_authorization_id,
            "status": "SUCCEEDED",
            "process_confirmed": True,
            "result": {"ok": True},
            "error_code": None,
        }
        envelope = _worker_envelope(
            kind="JOB_STATUS",
            body=body,
            sequence=0,
            message_id=result_message_id,
        )
        claimed_job = {
            "job_id": job_id,
            "status": "CLAIMED",
            "assigned_device_id": "office-device",
            "lease_owner": "dispatcher-one",
            "lease_expires_at": datetime(2026, 8, 15, 8, 5),
            "dispatch_message_id": dispatch_message_id,
            "dispatch_authorization_id": dispatch_authorization_id,
            "operation_type": "external_write",
            "payload_json": {},
            "worker_requirement_json": {"required": True},
        }
        finished_job = {**claimed_job, "status": "SUCCEEDED", "result_json": {"ok": True}}
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_worker_devices",
                    {
                        "paired_public_key_fingerprint": "f" * 64,
                        "inbound_sequence": None,
                        "last_inbound_message_id": None,
                        "last_inbound_envelope_sha256": None,
                    },
                    0,
                ),
                ("FROM automation_worker_jobs WHERE job_id", claimed_job, 0),
                ("UPDATE automation_worker_jobs", None, 1),
                ("INSERT INTO automation_worker_job_messages", None, 1),
                ("UPDATE automation_worker_devices", None, 1),
                ("FROM automation_worker_jobs WHERE job_id", finished_job, 0),
            ]
        )
        repository = AutomationPluginRepository(connection)
        persisted = repository.record_worker_job_status(
            envelope,
            principal_device_id="office-device",
            paired_public_key_fingerprint="f" * 64,
            signature_verified=True,
        )
        self.assertFalse(persisted["duplicate"])
        self.assertEqual("SUCCEEDED", persisted["status"])
        update_sql, update_params = connection.cursor_instance.executions[2]
        self.assertIn("dispatch_authorization_id=%s", update_sql)
        self.assertEqual("SUCCEEDED", update_params[0])
        self.assertEqual(dispatch_message_id, update_params[-2])
        self.assertEqual(dispatch_authorization_id, update_params[-1])

    def test_unconfirmed_worker_write_becomes_unknown_and_cannot_be_success(self):
        dispatch_message_id = str(uuid.uuid4())
        dispatch_authorization_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        body = {
            "job_id": job_id,
            "dispatch_message_id": dispatch_message_id,
            "dispatch_authorization_id": dispatch_authorization_id,
            "status": "FAILED",
            "process_confirmed": False,
            "result": {},
            "error_code": "PROCESS_EXITED",
        }
        envelope = _worker_envelope(kind="JOB_STATUS", body=body)
        claimed_job = {
            "job_id": job_id,
            "status": "CLAIMED",
            "assigned_device_id": "office-device",
            "lease_owner": "dispatcher-one",
            "lease_expires_at": datetime(2026, 8, 15, 8, 5),
            "dispatch_message_id": dispatch_message_id,
            "dispatch_authorization_id": dispatch_authorization_id,
            "operation_type": "financial_write",
            "payload_json": {},
            "worker_requirement_json": {"required": True},
        }
        finished_job = {**claimed_job, "status": "OUTCOME_UNKNOWN", "result_json": {}}
        connection = _ScriptedConnection(
            [
                (
                    "FROM automation_worker_devices",
                    {
                        "paired_public_key_fingerprint": "f" * 64,
                        "inbound_sequence": None,
                        "last_inbound_message_id": None,
                        "last_inbound_envelope_sha256": None,
                    },
                    0,
                ),
                ("FROM automation_worker_jobs WHERE job_id", claimed_job, 0),
                ("UPDATE automation_worker_jobs", None, 1),
                ("INSERT INTO automation_worker_job_messages", None, 1),
                ("UPDATE automation_worker_devices", None, 1),
                ("FROM automation_worker_jobs WHERE job_id", finished_job, 0),
            ]
        )
        result = AutomationPluginRepository(connection).record_worker_job_status(
            envelope,
            principal_device_id="office-device",
            paired_public_key_fingerprint="f" * 64,
            signature_verified=True,
        )
        update_params = connection.cursor_instance.executions[2][1]
        self.assertEqual("OUTCOME_UNKNOWN", update_params[0])
        self.assertEqual("WRITE_OUTCOME_UNKNOWN", update_params[3])
        self.assertEqual("OUTCOME_UNKNOWN", result["status"])

    def test_generation_row_closes_snapshot_and_every_material_hash(self):
        digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
        metadata = {
            "project_config_version": 1,
            "project_config": {},
            "account_bindings": {},
            "resource_bindings": {},
            "device_binding": None,
            "schedule": {"kind": "none", "times": [], "enabled": False},
            "compiled_invocations": {"console": {"arguments": {}}},
            "runtime_descriptor": {"runtime": {"kind": "python_subprocess"}},
            "action_contract": {"name": "automation.instance-one.run"},
            "governance_anchor": {"name": "governed-action"},
        }
        material_hashes = {
            field: digest(field)
            for field in (
                "package_sha256",
                "manifest_sha256",
                "project_config_sha256",
                "account_bindings_sha256",
                "resource_bindings_sha256",
                "device_binding_sha256",
                "schedule_sha256",
                "core_registry_sha256",
                "tool_contract_sha256",
                "invocation_contracts_sha256",
                "compiled_invocations_sha256",
                "runtime_descriptor_sha256",
                "governance_anchor_sha256",
                "policy_contract_sha256",
            )
        }
        snapshot = {
            "automation_id": "instance-one",
            "generation": 1,
            "plugin_id": "plugin-one",
            "plugin_version": "1.0.0",
            "trust_source": "ed25519_first_party",
            "enabled_entrypoints": ["console"],
            "execution_metadata": metadata,
            "created_at": "2026-08-15 08:00:00",
            **material_hashes,
        }
        row = {
            **material_hashes,
            "automation_id": "instance-one",
            "generation": 1,
            "plugin_id": "plugin-one",
            "plugin_version": "1.0.0",
            "trust_source": "ed25519_first_party",
            "enabled_entrypoints_sha256": _json_hash(["console"]),
            "snapshot_json": snapshot,
            "snapshot_sha256": _json_hash(
                {**snapshot, "created_at": datetime(2026, 8, 15, 8, 0)}
            ),
        }
        validated = _validated_generation_row(row)
        self.assertEqual(metadata, validated["snapshot_json"]["execution_metadata"])

        drifted = dict(row)
        drifted["runtime_descriptor_sha256"] = digest("drifted")
        with self.assertRaisesRegex(Exception, "snapshot integrity"):
            _validated_generation_row(drifted)

    def test_system_schedule_is_closed_and_reversible_without_browser_cron_or_ids(self):
        schedule = _normalized_project_schedule(
            {
                "kind": "daily_times",
                "times": ["18:30", "08:05"],
                "enabled": True,
            }
        )
        expressions = _schedule_expressions(schedule)
        self.assertEqual(expressions, ("5 8 * * *", "30 18 * * *"))
        self.assertEqual(
            _schedule_from_rows(
                [
                    {"cron_expression": expression, "enabled": True}
                    for expression in expressions
                ]
            ),
            schedule,
        )
        first_id = _stable_schedule_task_id("instance_one", expressions[0])
        self.assertEqual(first_id, _stable_schedule_task_id("instance_one", expressions[0]))
        self.assertLessEqual(len(first_id), 64)
        self.assertNotEqual(
            first_id,
            _stable_schedule_task_id("instance_two", expressions[0]),
        )

        for invalid in (
            {
                "kind": "daily_times",
                "times": ["08:05"],
                "enabled": True,
                "task_id": "forged",
            },
            {
                "kind": "daily_times",
                "times": ["08:05"],
                "enabled": True,
                "cron": "* * * * *",
            },
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _normalized_project_schedule(invalid)

    def test_none_and_startup_schedules_round_trip(self):
        self.assertEqual(
            _schedule_from_rows([]),
            {"kind": "none", "times": [], "enabled": False},
        )
        startup = {"kind": "startup", "times": [], "enabled": False}
        self.assertEqual(_schedule_expressions(startup), ("@startup",))
        self.assertEqual(
            _schedule_from_rows([{"cron_expression": "@startup", "enabled": False}]),
            startup,
        )

    def test_reviewed_customer_shadow_interval_expands_to_closed_daily_times(self):
        schedule = _schedule_from_rows(
            [{"cron_expression": "*/15 * * * *", "enabled": True}]
        )
        self.assertEqual(schedule["kind"], "daily_times")
        self.assertTrue(schedule["enabled"])
        self.assertEqual(len(schedule["times"]), 96)
        self.assertEqual(schedule["times"][:4], ["00:00", "00:15", "00:30", "00:45"])
        self.assertEqual(schedule["times"][-1], "23:45")
        self.assertEqual(_schedule_expressions(schedule), ("*/15 * * * *",))
        self.assertEqual(
            _schedule_from_rows(
                [
                    {"cron_expression": expression, "enabled": True}
                    for expression in _schedule_expressions(schedule)
                ]
            ),
            schedule,
        )

        with self.assertRaisesRegex(
            OrchestrationPersistenceError,
            "non-canonical system cron",
        ):
            _schedule_from_rows(
                [{"cron_expression": "*/10 * * * *", "enabled": True}]
            )
        with self.assertRaisesRegex(
            OrchestrationPersistenceError,
            "cannot be mixed",
        ):
            _schedule_from_rows(
                [
                    {"cron_expression": "*/15 * * * *", "enabled": True},
                    {"cron_expression": "7 0 * * *", "enabled": True},
                ]
            )

    def test_grouped_approval_idempotency_binds_actor_comment_and_decided_set(self):
        persisted = {
            "decision": "APPROVED",
            "expected_pending_set_hash": "a" * 64,
            "decided_pending_set_hash": "a" * 64,
            "decided_count": 2,
            "actor_id": "admin-one",
            "actor_role": "super_admin",
            "comment": "approve both",
        }
        repository = AutomationProjectPolicyRepository(_Connection())
        repository.get_batch_by_request = lambda *_args, **_kwargs: dict(persisted)  # type: ignore[method-assign]
        base = {
            "batch_id": "batch-one",
            "automation_id": "instance-one",
            "request_id": "request-one",
            "decision": "APPROVED",
            "expected_pending_set_hash": "a" * 64,
            "decided_pending_set_hash": "a" * 64,
            "decided_count": 2,
            "actor_id": "admin-one",
            "actor_role": "super_admin",
            "comment": "approve both",
            "result_json": {},
        }
        self.assertEqual(
            repository.create_batch(base)["actor_id"],
            "admin-one",
        )
        for field, value in (
            ("actor_id", "admin-two"),
            ("actor_role", "admin"),
            ("comment", "different"),
            ("decided_pending_set_hash", "b" * 64),
            ("decided_count", 1),
        ):
            changed = dict(base)
            changed[field] = value
            with self.subTest(field=field):
                with self.assertRaises(IdempotencyConflict):
                    repository.create_batch(changed)


if __name__ == "__main__":
    import unittest

    unittest.main()
