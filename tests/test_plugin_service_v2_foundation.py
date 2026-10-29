from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from shared.automation_plugin_repository import (
    AutomationPluginRepository,
    _GENERATION_HASH_FIELDS,
    _json_hash,
    _runtime_contract,
    _validated_generation_row,
)
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "agent" / "migrations" / "033_plugin_service_v2_foundation.sql"
RUNNER = ROOT / "agent" / "scripts" / "run_migrations.py"


class _Cursor:
    def __init__(self, actions):
        self.actions = list(actions)
        self.row = None
        self.rowcount = 0
        self.executions = []

    def execute(self, sql, params=None):
        if not self.actions:
            raise AssertionError(f"unexpected SQL: {sql}")
        marker, self.row, self.rowcount = self.actions.pop(0)
        normalized = " ".join(str(sql).split())
        if marker not in normalized:
            raise AssertionError(f"expected {marker!r}: {normalized}")
        self.executions.append((normalized, params))

    def fetchone(self):
        return None if isinstance(self.row, list) else self.row

    def fetchall(self):
        return self.row if isinstance(self.row, list) else []

    def close(self):
        return None


class _Connection:
    def __init__(self, actions):
        self.cursor_instance = _Cursor(actions)

    def cursor(self):
        return self.cursor_instance


def test_migration_adds_runtime_contracts_pair_lock_and_managed_documents():
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
    assert "runtime_model VARCHAR(16) NOT NULL DEFAULT ''ACTION_V1''" in sql
    assert "plugin_api VARCHAR(32) NOT NULL DEFAULT ''1.0.0''" in sql
    assert "BINARY 'super_admin_upload'" in sql
    assert "BINARY 'builtin_bundle'" in sql
    assert "CREATE TABLE IF NOT EXISTS automation_plugin_migration_pairs" in sql
    assert "BINARY 'PREPARING'" in sql
    assert "state VARCHAR(24) NOT NULL DEFAULT 'PREPARING'" in sql
    assert "BINARY 'PREPARING', BINARY 'TESTING'" in sql
    assert "CREATE TABLE IF NOT EXISTS automation_plugin_migration_run_locks" in sql
    assert "PRIMARY KEY (migration_pair_id, business_run_key)" in sql
    assert "idx_automation_plugin_migration_target_state" in sql
    assert "idx_automation_plugin_migration_lock_execution" in sql
    assert "expires_at > acquired_at" in sql
    assert "CREATE TABLE IF NOT EXISTS automation_plugin_documents" in sql
    assert "PRIMARY KEY (automation_id, collection_name, document_key)" in sql
    assert "CREATE TABLE IF NOT EXISTS automation_plugin_document_indexes" in sql
    assert "idx_automation_plugin_document_index_lookup" in sql
    assert "uq_automation_plugin_document_unique_value" in sql
    assert "unique_value_sha256 = value_sha256" in sql


def test_runtime_contract_preserves_v1_and_requires_canonical_v2_api():
    assert _runtime_contract({}) == ("ACTION_V1", "1.0.0")
    assert _runtime_contract(
        {"runtime_model": "SERVICE_V2", "plugin_api": "2.0.0"}
    ) == ("SERVICE_V2", "2.0.0")
    with pytest.raises(ValueError, match="declared together"):
        _runtime_contract({"runtime_model": "SERVICE_V2"})
    with pytest.raises(ValueError, match="canonical"):
        _runtime_contract(
            {"runtime_model": "SERVICE_V2", "plugin_api": "02.0.0"}
        )


def test_service_generation_duplicates_runtime_contract_in_indexed_columns():
    hashes = {
        field: hashlib.sha256(field.encode("utf-8")).hexdigest()
        for field in _GENERATION_HASH_FIELDS
    }
    snapshot = {
        "automation_id": "service-v2",
        "generation": 1,
        "plugin_id": "service-plugin",
        "plugin_version": "2.0.0",
        "runtime_model": "SERVICE_V2",
        "plugin_api": "2.0.0",
        "trust_source": "super_admin_upload",
        "enabled_entrypoints": [],
        "execution_metadata": {
            "runtime_model": "SERVICE_V2", "plugin_api": "2.0.0",
            "service_contracts": {"provides": [], "requires": []},
            "contributions": {}, "storage_contract": {},
            "project_config_version": 1,
            "project_config": {},
            "account_bindings": {},
            "resource_bindings": {},
            "device_binding": None,
            "schedule": {"kind": "none", "times": [], "enabled": False},
            "compiled_invocations": {},
            "runtime_descriptor": {},
            "action_contract": {},
            "governance_anchor": {},
        },
        "created_at": datetime(2026, 8, 30),
        **hashes,
    }
    row = {
        **snapshot,
        "enabled_entrypoints_sha256": _json_hash([]),
        "snapshot_json": snapshot,
        "snapshot_sha256": _json_hash(snapshot),
    }
    assert _validated_generation_row(row)["runtime_model"] == "SERVICE_V2"
    from shared.automation_plugin_repository import AutomationPluginRepository
    from tests.test_automation_plugin_repository import _ScriptedConnection

    for include_details in (True, False):
        actions = [("FROM automation_project_generations", row, 1)]
        if include_details:
            actions += [("FROM automation_project_generation_coeffects", [], 0), ("FROM automation_project_generation_effects", [], 0)]
        actions.append(("FROM automation_project_generation_transitions", {"phase": "STABLE", "transition_token": "isolated-transition"}, 1))
        connection = _ScriptedConnection(actions)
        read = AutomationPluginRepository(connection).get_generation_row("service-v2", 1, include_execution_details=include_details)
        assert read["snapshot_sha256"] == row["snapshot_sha256"]
        assert read["activation_phase"] == "STABLE"
        assert read["activation_transition_token"] == "isolated-transition"
        assert ("effects" in read) is include_details
        assert ("coeffects" in read) is include_details
        assert not connection.cursor_instance._actions
    denied = AutomationPluginRepository(_ScriptedConnection([]))
    with pytest.raises(ValueError, match="complete execution details"):
        denied.get_generation_row("service-v2", 1, for_update=True, include_execution_details=False)
    invalid = _ScriptedConnection([("FROM automation_project_generations", {**row, "plugin_api": "2.1.0"}, 1)])
    with pytest.raises(Exception, match="snapshot integrity"):
        AutomationPluginRepository(invalid).get_generation_row("service-v2", 1, include_execution_details=False)
    with pytest.raises(Exception, match="snapshot integrity"):
        _validated_generation_row({**row, "plugin_api": "2.1.0"})


def test_dependency_loss_disables_physical_scheduler_without_mutating_intent():
    connection = _Connection(
        [("UPDATE scheduled_tasks SET enabled=FALSE", None, 3)]
    )
    repository = AutomationPluginRepository(connection)

    result = repository.set_project_dependency_scheduler_gate(
        "service-v2", dependency_ready=False
    )

    assert result == {
        "automation_id": "service-v2",
        "scheduler_enabled": False,
        "task_count": 3,
    }
    sql, params = connection.cursor_instance.executions[0]
    assert "automation_project_configs" not in sql
    assert params == ("service-v2",)


def test_first_package_registration_writes_closed_immutable_audit_event() -> None:
    digest = "a" * 64
    version = {
        "version": "2.0.0",
        "runtime_model": "SERVICE_V2",
        "plugin_api": "2.0.0",
        "package_sha256": digest,
        "manifest_sha256": "b" * 64,
        "manifest_json": {
            "capabilities": [], "provides": [], "requires": [],
            "contributes": {}, "config_schema": {}, "storage": {},
        },
        "tool_contract_sha256": "c" * 64,
        "config_schema_sha256": "d" * 64,
        "allowed_entrypoints_sha256": "e" * 64,
        "invocation_contracts_sha256": "f" * 64,
        "worker_requirement_sha256": "1" * 64,
        "runtime_sha256": "2" * 64,
        "scheduling_sha256": "3" * 64,
        "project_full_auto_allowed": True,
        "trust_source": "super_admin_upload",
        "install_root_metadata_json": {},
        "install_root_metadata_sha256": "4" * 64,
        "installed_by_actor_id": "admin-one",
    }
    persisted = {"plugin_id": "service_v2", **version}
    request_id = str(uuid.uuid4())
    connection = _Connection(
        [
            ("SELECT * FROM automation_plugin_versions", None, 0),
            ("INSERT INTO automation_plugin_packages", None, 1),
            ("SELECT * FROM automation_plugin_package_events", None, 0),
            ("INSERT INTO automation_plugin_package_events", None, 1),
            ("INSERT INTO automation_plugin_versions", None, 1),
            ("SELECT * FROM automation_plugin_versions", persisted, 0),
        ]
    )
    repository = AutomationPluginRepository(connection)

    repository.register_package_version(
        package={"plugin_id": "service_v2", "display_name": "Service", "description": "x"},
        version=version,
        request_id=request_id,
        actor_id="admin-one",
        actor_role="super_admin",
    )

    event_sql, event_params = next(
        item for item in connection.cursor_instance.executions
        if "INSERT INTO automation_plugin_package_events" in item[0]
    )
    assert "PACKAGE_VERSION_REGISTERED" in event_sql
    metadata = json.loads(event_params[3])
    assert metadata["runtime_model"] == "SERVICE_V2"
    assert metadata["manifest_component_sha256"]["storage"]
    assert "manifest_json" not in metadata


def test_project_install_event_uses_immutable_package_identity_and_initial_hashes() -> None:
    version = {
        "plugin_id": "service_v2",
        "version": "2.0.0",
        "runtime_model": "SERVICE_V2",
        "plugin_api": "2.0.0",
        "package_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "manifest_json": {
            "capabilities": [], "provides": [], "requires": [],
            "contributes": {}, "config_schema": {}, "storage": {},
        },
        "tool_contract_sha256": "c" * 64,
        "invocation_contracts_sha256": "d" * 64,
        "runtime_sha256": "e" * 64,
        "scheduling_sha256": "f" * 64,
        "trust_source": "super_admin_upload",
    }
    request_id = str(uuid.uuid4())
    connection = _Connection(
        [
            ("SELECT * FROM automation_projects", version, 0),
            ("SELECT * FROM automation_plugin_versions", version, 0),
            ("SELECT * FROM automation_project_events", None, 0),
            ("INSERT INTO automation_project_events", None, 1),
        ]
    )
    repository = AutomationPluginRepository(connection)

    repository.record_project_install_lifecycle_event(
        "service-v2",
        request_id=request_id,
        actor_id="admin-one",
        actor_role="super_admin",
        enabled_entrypoints=("manual_run",),
        project_configuration_version=1,
        policy_mode="FULL_AUTO",
    )

    event_sql, event_params = connection.cursor_instance.executions[-1]
    assert "PLUGIN_INSTANCE_INSTALLED" in event_sql
    metadata = json.loads(event_params[2])
    assert metadata["package"]["manifest_sha256"] == "b" * 64
    assert metadata["initial"]["enabled_entrypoints_sha256"] == _json_hash(["manual_run"])
    assert "config_json" not in metadata
    assert connection.cursor_instance.actions == []


def test_uninstall_retains_document_bodies_without_recording_them() -> None:
    connection = _Connection(
        [
            ("SELECT retention_state, last_request_id", [{"retention_state": "ACTIVE"}], 0),
            ("UPDATE automation_plugin_documents", None, 1),
        ]
    )
    repository = AutomationPluginRepository(connection)
    result = repository.retain_plugin_documents_for_uninstall(
        "service-v2", request_id=str(uuid.uuid4()), actor_id="admin-one", actor_role="super_admin"
    )
    assert result["retained_count"] == 1
    sql, _params = connection.cursor_instance.executions[1]
    assert "document_json=NULL" not in sql
    assert "retention_state='RETAINED'" in sql


def test_managed_document_uses_cas_and_rejects_credential_fields():
    connection = _Connection(
        [
            ("SELECT automation_id FROM automation_projects", {"automation_id": "service-v2"}, 0),
            ("SELECT * FROM automation_plugin_documents", None, 0),
            ("INSERT INTO automation_plugin_documents", None, 1),
        ]
    )
    repository = AutomationPluginRepository(connection)
    result = repository.put_plugin_document(
        "service-v2",
        "checkpoints",
        "2026-08-30",
        {"cursor": "page-7"},
        expected_document_version=0,
        request_id=str(uuid.uuid4()),
        actor_id="service-v2",
        actor_role="plugin_service",
    )
    assert result["document_version"] == 1
    assert result["retention_state"] == "ACTIVE"

    with pytest.raises(ValueError, match="credential material"):
        repository.put_plugin_document(
            "service-v2",
            "checkpoints",
            "unsafe",
            {"api_key": "forbidden"},
            expected_document_version=0,
            request_id=str(uuid.uuid4()),
            actor_id="service-v2",
            actor_role="plugin_service",
        )


def test_managed_document_write_atomically_refreshes_digest_only_index_rows():
    index_digest = "a" * 64
    unique_digest = "b" * 64
    connection = _Connection(
        [
            ("SELECT automation_id FROM automation_projects", {"automation_id": "service-v2"}, 0),
            ("SELECT * FROM automation_plugin_documents", None, 0),
            ("FROM automation_plugin_document_indexes", [], 0),
            ("unique_value_sha256=%s", None, 0),
            ("INSERT INTO automation_plugin_documents", None, 1),
            ("DELETE FROM automation_plugin_document_indexes", None, 0),
            ("INSERT INTO automation_plugin_document_indexes", None, 1),
            ("INSERT INTO automation_plugin_document_indexes", None, 1),
        ]
    )
    repository = AutomationPluginRepository(connection)

    result = repository.put_plugin_document(
        "service-v2",
        "items",
        "doc-1",
        {"external_id": "business-value"},
        expected_document_version=0,
        request_id=str(uuid.uuid4()),
        actor_id="service-v2",
        actor_role="plugin_service",
        index_values_sha256={"by_external_id": index_digest},
        unique_values_sha256={"one_external_id": unique_digest},
    )

    assert result["document_version"] == 1
    index_inserts = [
        params
        for sql, params in connection.cursor_instance.executions
        if "INSERT INTO automation_plugin_document_indexes" in sql
    ]
    assert index_inserts == [
        (
            "service-v2",
            "items",
            "INDEX",
            "by_external_id",
            index_digest,
            None,
            "doc-1",
            1,
        ),
        (
            "service-v2",
            "items",
            "UNIQUE",
            "one_external_id",
            unique_digest,
            unique_digest,
            "doc-1",
            1,
        ),
    ]
    assert "business-value" not in repr(index_inserts)


def test_managed_document_unique_conflict_fails_before_document_write():
    connection = _Connection(
        [
            ("SELECT automation_id FROM automation_projects", {"automation_id": "service-v2"}, 0),
            ("SELECT * FROM automation_plugin_documents", None, 0),
            ("FROM automation_plugin_document_indexes", [], 0),
            ("unique_value_sha256=%s", {"document_key": "existing-doc"}, 0),
        ]
    )
    repository = AutomationPluginRepository(connection)

    with pytest.raises(ConcurrentUpdateError, match="unique constraint conflict"):
        repository.put_plugin_document(
            "service-v2",
            "items",
            "new-doc",
            {"external_id": "same-value"},
            expected_document_version=0,
            request_id=str(uuid.uuid4()),
            actor_id="service-v2",
            actor_role="plugin_service",
            index_values_sha256={},
            unique_values_sha256={"one_external_id": "c" * 64},
        )
    assert connection.cursor_instance.actions == []


def test_managed_document_index_replay_requires_the_same_digest_projection():
    request_id = str(uuid.uuid4())
    document = {"external_id": "business-value"}
    persisted = {
        "automation_id": "service-v2",
        "collection_name": "items",
        "document_key": "doc-1",
        "document_json": document,
        "document_sha256": _json_hash(document),
        "document_version": 1,
        "retention_state": "ACTIVE",
        "retention_until": None,
        "last_request_id": request_id,
    }
    connection = _Connection(
        [
            ("SELECT automation_id FROM automation_projects", {"automation_id": "service-v2"}, 0),
            ("SELECT * FROM automation_plugin_documents", persisted, 0),
            (
                "FROM automation_plugin_document_indexes",
                [
                    {
                        "index_kind": "INDEX",
                        "index_name": "by_external_id",
                        "value_sha256": "a" * 64,
                    },
                    {
                        "index_kind": "UNIQUE",
                        "index_name": "one_external_id",
                        "value_sha256": "b" * 64,
                    },
                ],
                0,
            ),
        ]
    )
    repository = AutomationPluginRepository(connection)

    replay = repository.put_plugin_document(
        "service-v2",
        "items",
        "doc-1",
        document,
        expected_document_version=0,
        request_id=request_id,
        actor_id="service-v2",
        actor_role="plugin_service",
        index_values_sha256={"by_external_id": "a" * 64},
        unique_values_sha256={"one_external_id": "b" * 64},
    )

    assert replay == persisted
    assert connection.cursor_instance.actions == []


def test_managed_document_index_query_is_project_scoped_and_version_joined():
    persisted = {
        "automation_id": "service-v2",
        "collection_name": "items",
        "document_key": "doc-1",
        "document_json": {"external_id": "external-1"},
        "document_version": 3,
        "retention_state": "ACTIVE",
    }
    connection = _Connection(
        [("FROM automation_plugin_document_indexes AS document_index", [persisted], 0)]
    )
    repository = AutomationPluginRepository(connection)

    rows = repository.query_plugin_documents_by_index(
        "service-v2",
        "items",
        "by_external_id",
        "d" * 64,
        limit=25,
    )

    assert rows == [persisted]
    sql, params = connection.cursor_instance.executions[0]
    assert "document.document_version=document_index.document_version" in sql
    assert "document_index.index_kind='INDEX'" in sql
    assert "document.retention_state IN ('ACTIVE', 'RETAINED')" in sql
    assert params == ("service-v2", "items", "by_external_id", "d" * 64, 25)


def test_permanent_document_clear_removes_all_index_metadata():
    connection = _Connection(
        [
            ("FROM automation_plugin_documents WHERE automation_id=%s", [{"retention_state": "RETAINED"}], 0),
            ("DELETE FROM automation_plugin_document_indexes", None, 2),
            ("UPDATE automation_plugin_documents", None, 1),
        ]
    )
    repository = AutomationPluginRepository(connection)

    result = repository.permanently_clear_plugin_documents(
        "service-v2",
        request_id=str(uuid.uuid4()),
        actor_id="admin-one",
        actor_role="super_admin",
        reason="explicit permanent purge",
    )

    assert result["cleared_count"] == 1
    assert connection.cursor_instance.actions == []
