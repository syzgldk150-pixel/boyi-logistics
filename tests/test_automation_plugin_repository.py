from __future__ import annotations

import base64
import hashlib
from datetime import datetime
import uuid
from unittest import TestCase

from shared.automation_plugin_repository import (
    AutomationPluginRepository,
    _normalized_project_schedule,
    _normalized_worker_identity,
    _schedule_expressions,
    _schedule_from_rows,
    _stable_schedule_task_id,
    _validated_generation_row,
    _validated_worker_inbound_envelope,
    _worker_status_body,
)
from shared.automation_project_policy_repository import AutomationProjectPolicyRepository
from shared.orchestration_repository_support import (
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
