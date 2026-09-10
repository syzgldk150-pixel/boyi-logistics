"""Real MySQL target-contract upgrades; no process or business execution."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from uuid import uuid4

import pytest

from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.models import FirstPartyInstanceSeed, PluginTrustSource
from agent.automation_plugins.mysql_repository import MySQLAutomationPluginRepositoryAdapter
from shared.orchestration_repository_support import OrchestrationPersistenceError
from tests.test_automation_plugin_upgrade import (
    _generation_row, _sha, _snapshot, _synthetic_manifest, _version,
)
from tests.test_legacy_unknown_scope_migration_mysql import database  # noqa: F401


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8",
)
PROJECT = "upgrade-instance"
RELEASE = "a" * 40
RELEASE_ACTOR = "system:release:first-party-upgrade"


def _witness(manifest):
    payload = manifest.to_mapping()
    return {"runtime_model": "ACTION_V1", **{
        key: payload[key] for key in ("allowed_entrypoints", "invocation_contracts", "scheduling")
    }}


def _rows(database):
    result = {}
    with database.helper._connection() as connection, connection.cursor() as cursor:
        for table in (
            "automation_projects", "automation_project_configs", "automation_project_generations",
            "automation_project_events", "automation_project_policies", "scheduled_tasks",
        ):
            cursor.execute(f"SELECT * FROM {table} WHERE automation_id=%s", (PROJECT,))
            result[table] = cursor.fetchall()
        cursor.execute("SELECT * FROM domain_events WHERE entity_id=%s", (PROJECT,))
        result["domain_events"] = cursor.fetchall()
        cursor.execute("""SELECT o.* FROM outbox_events o JOIN domain_events d ON d.event_id=o.event_id
            WHERE d.entity_id=%s""", (PROJECT,))
        result["outbox_events"] = cursor.fetchall()
    return result


@pytest.fixture
def upgrade(database):  # noqa: F811
    old_manifest = _synthetic_manifest("1.0.0")
    target_payload = _synthetic_manifest("1.0.1").to_mapping()
    # Real contract change: the new signed code receives an additional literal
    # input, while the administrator's saved settings stay unchanged.
    for schema in (target_payload["tool_contract"]["input_schema"],
                   target_payload["invocation_contracts"]["console"]["input_schema"]):
        schema["properties"]["include_details"] = {"type": "boolean"}
        schema["required"].append("include_details")
    target_payload["invocation_contracts"]["console"]["argument_template"]["include_details"] = {
        "source": "literal", "value": True,
    }
    target_manifest = AutomationPluginManifest.from_mapping(target_payload)
    old = replace(_version(old_manifest, "1"), trust_source=PluginTrustSource.ED25519_FIRST_PARTY)
    target = replace(_version(target_manifest, "2"), trust_source=PluginTrustSource.ED25519_FIRST_PARTY)
    snapshot = replace(_snapshot(old_manifest, old), trust_source=PluginTrustSource.ED25519_FIRST_PARTY)
    adapter = MySQLAutomationPluginRepositoryAdapter(database.repository)
    with database.repository.unit_of_work() as uow:
        for version in (old, target):
            adapter._register(uow.automation_plugins, version, actor_id="isolated-admin")
        uow.automation_plugins.install_project_instance({
            "automation_id": PROJECT, "plugin_id": old.plugin_id, "plugin_version": old.version,
            "display_name": "Isolated contract upgrade", "install_request_id": str(uuid4()),
            "install_payload_sha256": "3" * 64, "installed_by_actor_id": "isolated-admin",
            "migration_authority": False,
        })
        uow.automation_plugins.initialize_project_config(PROJECT, enabled_entrypoints=("console",))
        uow.automation_projects.ensure_default(
            PROJECT, mode="PROJECT_FULL_AUTO", project_generation=1, project_configuration_version=1,
        )
        uow.commit()
    # Persist a closed historical generation, as an existing installation has
    # before release bootstrap. Only the uniquely owned test database is used.
    with database.helper._connection() as connection, connection.cursor() as cursor:
        compiled = {"console": {"arguments": {"marker": "A"}, "dynamic_resolvers": {}}}
        cursor.execute("""UPDATE automation_project_configs SET config_json=%s,config_sha256=%s,
            compiled_invocations_json=%s,compiled_invocations_sha256=%s,configured=TRUE
            WHERE automation_id=%s""", (json.dumps({"marker": "A"}), _sha({"marker": "A"}),
                json.dumps(compiled), _sha(compiled), PROJECT))
        cursor.execute("SHOW COLUMNS FROM automation_project_generations")
        columns = {row["Field"] for row in cursor.fetchall()}
        row = {key: value for key, value in _generation_row(snapshot).items() if key in columns}
        row["request_id"] = str(uuid4())
        row["snapshot_json"] = json.dumps(row["snapshot_json"], default=str)
        cursor.execute("INSERT INTO automation_project_generations (" + ",".join(row) + ") VALUES ("
            + ",".join(["%s"] * len(row)) + ")", tuple(row.values()))
        cursor.execute("""UPDATE automation_projects SET target_generation=1,committed_generation=1,
            reconcile_state='STABLE',state='ENABLED',enabled=TRUE WHERE automation_id=%s""", (PROJECT,))
        connection.commit()
    seed = FirstPartyInstanceSeed(automation_id=PROJECT, plugin_id=target.plugin_id,
        version=target.version, display_name="Isolated contract upgrade", allowed_entrypoints=("console",))
    return database, adapter, old, target, target_manifest, seed


def test_changed_contract_bootstrap_preserves_committed_history_and_replays_without_change(upgrade):
    database, adapter, old, target, manifest, seed = upgrade
    before = _rows(database)
    assert _witness(_synthetic_manifest(old.version)) != _witness(manifest)
    adapter.bootstrap_missing((target,), (seed,), release_sha=RELEASE)
    after = _rows(database)
    project = after["automation_projects"][0]
    assert project["plugin_version"] == target.version
    assert (project["target_generation"], project["committed_generation"]) == (2, 1)
    assert project["reconcile_state"] == "PREPARING"
    config = after["automation_project_configs"][0]
    assert json.loads(config["config_json"]) == {"marker": "A"}
    assert config["config_version"] == 2
    assert json.loads(config["compiled_invocations_json"])["console"]["arguments"] == {
        "marker": "A", "include_details": True,
    }
    assert after["automation_project_generations"] == before["automation_project_generations"]
    assert after["automation_project_policies"][0]["mode"] == "PROJECT_FULL_AUTO"
    assert after["scheduled_tasks"] == before["scheduled_tasks"]
    events = [json.loads(row["metadata_json"]) for row in after["automation_project_events"]
        if row["event_type"] == "CONFIGURATION_UPDATED"]
    assert events[0]["first_party_upgrade_target"] == {
        "from_version": old.version, "to_version": target.version, "package_sha256": target.package_sha256,
    }
    # A new adapter models the next service startup without replaying business.
    MySQLAutomationPluginRepositoryAdapter(database.repository).bootstrap_missing(
        (target,), (seed,), release_sha=RELEASE,
    )
    assert _rows(database) == after


def test_stage_failure_rolls_back_prepared_contract_and_complete_bootstrap_can_retry(upgrade, monkeypatch):
    database, adapter, _old, target, _manifest, seed = upgrade
    before = _rows(database)
    observed = []
    stage = adapter._stage_instance_upgrade
    def fail_before_commit(uow, automation_id, version, **kwargs):
        stage(uow, automation_id, version, **kwargs)
        prepared = uow.automation_plugins.get_project_config(automation_id, for_update=True)
        observed.append(prepared["compiled_invocations_json"]["console"]["arguments"])
        assert prepared["config_version"] == 2
        assert uow.automation_plugins.get_project(automation_id, for_update=True)["plugin_version"] == target.version
        raise RuntimeError("isolated process failure before stage commit")
    with monkeypatch.context() as patch:
        patch.setattr(adapter, "_stage_instance_upgrade", fail_before_commit)
        with pytest.raises(RuntimeError, match="before stage commit"):
            adapter.bootstrap_missing((target,), (seed,), release_sha=RELEASE)
    assert observed == [{"marker": "A", "include_details": True}]
    assert _rows(database) == before, "configuration, desired version, policy and events must roll back together"
    MySQLAutomationPluginRepositoryAdapter(database.repository).bootstrap_missing(
        (target,), (seed,), release_sha=RELEASE,
    )
    assert _rows(database)["automation_projects"][0]["plugin_version"] == target.version


@pytest.mark.parametrize("invalid", ["ordinary_save", "actor", "digest", "witness", "from_version"])
def test_wrong_upgrade_identity_or_witness_never_modifies_project(upgrade, invalid):
    database, _adapter, old, target, manifest, _seed = upgrade
    before = _rows(database)
    identity = {"from_version": old.version, "to_version": target.version, "package_sha256": target.package_sha256}
    witness = copy.deepcopy(_witness(manifest))
    kwargs = {"first_party_upgrade_target": identity}
    actor = RELEASE_ACTOR
    if invalid == "ordinary_save":
        kwargs = {}
        actor = "isolated-admin"
    elif invalid == "actor":
        actor = "isolated-admin"
    elif invalid == "digest":
        identity["package_sha256"] = "f" * 64
    elif invalid == "from_version":
        identity["from_version"] = "0.9.0"
    else:
        witness["invocation_contracts"]["console"]["input_schema"]["description"] = "not the registered contract"
    with pytest.raises((ValueError, OrchestrationPersistenceError)):
        with database.repository.unit_of_work() as uow:
            uow.automation_plugins.save_project_config(
                PROJECT, config={"marker": "A"}, account_bindings={}, resource_bindings={},
                enabled_entrypoints=("console",), schedule={"kind": "none", "times": [], "enabled": False},
                compiled_invocations={"console": {"arguments": {"marker": "A", "include_details": True},
                    "dynamic_resolvers": {}}}, contract_witness=witness, device_binding=None,
                actor_id=actor, actor_role="super_admin", request_id=str(uuid4()),
                expected_project_configuration_version=1, **kwargs,
            )
            uow.commit()
    assert _rows(database) == before
