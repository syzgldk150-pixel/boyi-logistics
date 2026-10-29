"""Optional Console inputs preserve administrator settings during a V2 upgrade."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import PluginRuntimeModel, PluginTrustSource, PluginVersionRecord
from agent.automation_plugins.mysql_repository import (
    MySQLAutomationPluginRepositoryAdapter, _compile_upgrade_invocations,
    _configuration_contract_witness, _version_contract,
)
from agent.automation_plugins.storage import VERIFIED_ARCHIVE_RELATIVE
from shared.automation_plugin_configuration_contract import (
    optional_console_input_additions, validate_optional_console_upgrade_configuration,
)
from shared.orchestration_repository_support import (
    ConcurrentUpdateError, IdempotencyConflict, OrchestrationPersistenceError,
)
from shared.plugin_json import plugin_json_digest
from tests.test_legacy_unknown_scope_migration_mysql import database  # noqa: F401

PROJECT = "upgrade-instance"
ACTOR = "isolated-console-admin"
MYSQL = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")
CONSOLE = "monthly_capture"
SCHEDULER = "monthly_schedule"
_MANIFEST = Path(__file__).resolve().parents[1] / "agent/service_v2_plugins/sync_ronghui_finance_statement_v2/manifest.json"


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


def _version(payload, marker):
    manifest = AutomationPluginManifestV2.from_mapping(payload)
    install_root = f"/plugins/synthetic/{manifest.version}"
    return PluginVersionRecord(
        plugin_id=manifest.plugin_id, version=manifest.version, package_sha256=marker * 64,
        manifest_sha256=manifest.manifest_sha256, manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD, install_root=install_root,
        install_metadata={"install_root": install_root, "python_relative": "venv/bin/python",
                          "archive_relative": VERIFIED_ARCHIVE_RELATIVE, "archive_sha256": marker * 64},
        runtime_model=PluginRuntimeModel.SERVICE_V2, plugin_api="2.0.0",
    )


def _versions():
    """The current statement package with a saved setting, a scheduler and an optional month input."""
    target_payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    target_payload["version"] = "1.0.1"
    target_payload["config_schema"]["properties"]["page_size"] = {"type": "integer", "minimum": 1, "maximum": 500}
    console = target_payload["contributes"]["console"][0]
    console["input_schema"]["required"] = []
    target_payload["contributes"]["scheduler"] = [{
        "id": SCHEDULER, "title": "Monthly schedule", "service": console["service"],
        "operation": console["operation"], "default_enabled": False}]
    old_payload = copy.deepcopy(target_payload)
    old_payload["version"] = "1.0.0"
    del old_payload["contributes"]["console"][0]["input_schema"]
    return _version(old_payload, "1"), _version(target_payload, "2")


def _witness(version):
    return _configuration_contract_witness(_version_contract(version)[1], runtime_model=version.runtime_model)


def _compiled(version, config, accounts, sources):
    return _compile_upgrade_invocations(PROJECT, _version_contract(version)[1], config,
        accounts, {}, sources, runtime_model=version.runtime_model)


def _seed(database, *, startup=False):
    old, target = _versions()
    adapter = MySQLAutomationPluginRepositoryAdapter(database.repository)
    sources = [CONSOLE, SCHEDULER] if startup else [CONSOLE]
    schedule = ({"kind": "startup", "times": [], "enabled": True} if startup else
        {"kind": "daily_times", "times": ["00:10"], "enabled": False})
    config = {"page_size": 100}
    accounts = {role["role"]: "isolated-" + role["role"] for role in old.manifest["account_roles"]}
    compiled = _compiled(old, config, accounts, sources)
    with database.repository.unit_of_work() as uow:
        adapter._register(uow.automation_plugins, old, actor_id=ACTOR)
        uow.automation_plugins.install_project_instance({
            "automation_id": PROJECT, "plugin_id": old.plugin_id, "plugin_version": old.version,
            "display_name": "Isolated optional Console upgrade", "install_request_id": str(uuid4()),
            "install_payload_sha256": "3" * 64, "installed_by_actor_id": ACTOR, "migration_authority": False,
        })
        uow.automation_plugins.initialize_project_config(PROJECT, enabled_entrypoints=sources)
        uow.automation_projects.ensure_default(PROJECT, mode="PROJECT_FULL_AUTO",
            project_generation=1, project_configuration_version=1)
        uow.automation_plugins.save_project_config(PROJECT, config=config, account_bindings=accounts,
            resource_bindings={}, enabled_entrypoints=sources, schedule=schedule,
            compiled_invocations=compiled, contract_witness=_witness(old), 
            actor_id=ACTOR, actor_role="super_admin", request_id=str(uuid4()),
            expected_project_configuration_version=1)
        uow.commit()
    # A historical committed lineage suffices for this persistence test. No
    # provider or business execution is constructed from this synthetic row.
    with database.helper._connection() as connection, connection.cursor() as cursor:
        hashes = ["package_sha256", "manifest_sha256", "project_config_sha256", "account_bindings_sha256",
            "resource_bindings_sha256", "device_binding_sha256", "schedule_sha256", "core_registry_sha256",
            "tool_contract_sha256", "invocation_contracts_sha256", "compiled_invocations_sha256",
            "runtime_descriptor_sha256", "governance_anchor_sha256", "policy_contract_sha256",
            "enabled_entrypoints_sha256", "snapshot_sha256"]
        cursor.execute("INSERT INTO automation_project_generations(automation_id,generation,request_id,state,"
            "plugin_id,plugin_version,trust_source,runtime_model,plugin_api,snapshot_json," + ",".join(hashes) + ") "
            "VALUES(%s,1,%s,'COMMITTED',%s,%s,'super_admin_upload','SERVICE_V2','2.0.0','{}'," +
            ",".join(["%s"] * len(hashes)) + ")", (PROJECT, str(uuid4()), old.plugin_id, old.version,
                *(["a" * 64] * len(hashes))))
        cursor.execute("UPDATE automation_projects SET target_generation=1,committed_generation=1,"
            "reconcile_state='STABLE',state='DISABLED',enabled=FALSE WHERE automation_id=%s", (PROJECT,))
        connection.commit()
    return adapter, old, target


def _stage(database, adapter, old, target, request_id, record_version):
    with database.repository.unit_of_work() as uow:
        adapter._stage_instance_upgrade(uow, PROJECT, target, actor_id=ACTOR, actor_role="super_admin",
            request_id=request_id, expected_current_version=old.version, expected_record_version=record_version)
        uow.commit()


@MYSQL
@pytest.mark.parametrize("startup", [False, True])
def test_optional_console_upgrade_preserves_settings_and_replays(database, startup):  # noqa: F811
    adapter, old, target = _seed(database, startup=startup)
    before = _rows(database)
    request = str(uuid4())
    record_version = before["automation_projects"][0]["record_version"]
    _stage(database, adapter, old, target, request, record_version)
    after = _rows(database)
    saved_before, saved_after = (rows["automation_project_configs"][0] for rows in (before, after))
    for field in ("config_json", "config_sha256", "account_bindings_json", "account_bindings_sha256",
                  "resource_bindings_json", "resource_bindings_sha256", "enabled_entrypoints_json",
                  "desired_schedule_json", "desired_schedule_sha256", "device_id"):
        assert saved_after[field] == saved_before[field], field
    assert saved_after["config_version"] == saved_before["config_version"] + 1
    compiled = json.loads(saved_after["compiled_invocations_json"])
    assert compiled[CONSOLE]["dynamic_resolvers"] == {"month": "verified_optional_console_month"}
    assert saved_after["compiled_invocations_sha256"] == plugin_json_digest(compiled)
    assert after["scheduled_tasks"] == before["scheduled_tasks"]
    assert after["automation_project_generations"] == before["automation_project_generations"]
    project = after["automation_projects"][0]
    assert project["plugin_version"] == target.version
    assert (project["target_generation"], project["committed_generation"]) == (2, 1)
    assert not project["enabled"]
    policy = after["automation_project_policies"][0]
    assert policy["project_configuration_version"] == saved_after["config_version"]
    assert policy["project_generation"] == 2
    events = [row for row in after["automation_project_events"] if row["event_type"] == "CONFIGURATION_UPDATED"]
    prepared = next(row for row in events if "optional_console_upgrade_target" in json.loads(row["metadata_json"]))
    assert prepared["actor_id"] == ACTOR
    _stage(database, adapter, old, target, request, record_version)
    assert _rows(database) == after
    with pytest.raises((PluginConflictError, ConcurrentUpdateError)):
        _stage(database, adapter, old, target, str(uuid4()), record_version)
    assert _rows(database) == after
    with pytest.raises(IdempotencyConflict):
        _stage(database, adapter, old, target, request, record_version + 1)
    assert _rows(database) == after


@MYSQL
@pytest.mark.parametrize("failure", ["cas", "saved_template", "required_input", "stage"])
def test_optional_console_upgrade_failure_rolls_back_every_write(database, monkeypatch, failure):  # noqa: F811
    adapter, old, target = _seed(database)
    if failure == "saved_template":
        with database.helper._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT compiled_invocations_json FROM automation_project_configs WHERE automation_id=%s",
                (PROJECT,))
            compiled = json.loads(cursor.fetchone()["compiled_invocations_json"])
            compiled[CONSOLE]["arguments"]["page_size"] = 30
            cursor.execute("UPDATE automation_project_configs SET compiled_invocations_json=%s,"
                "compiled_invocations_sha256=%s WHERE automation_id=%s",
                (json.dumps(compiled), plugin_json_digest(compiled), PROJECT))
            connection.commit()
    if failure == "required_input":
        payload = copy.deepcopy(target.manifest)
        payload["contributes"]["console"][0]["input_schema"]["required"] = ["month"]
        manifest = AutomationPluginManifestV2.from_mapping(payload)
        target = replace(target, manifest=manifest.to_mapping(), manifest_sha256=manifest.manifest_sha256)
    before = _rows(database)
    if failure == "stage":
        from shared.automation_plugin_repository import AutomationPluginRepository
        original = AutomationPluginRepository.stage_project_upgrade
        def fail_after_stage(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            assert result["plugin_version"] == target.version
            raise RuntimeError("isolated failure after configuration and stage")
        monkeypatch.setattr(AutomationPluginRepository, "stage_project_upgrade", fail_after_stage)
    record_version = before["automation_projects"][0]["record_version"] + (failure == "cas")
    with pytest.raises((PluginConflictError, RuntimeError)):
        _stage(database, adapter, old, target, str(uuid4()), record_version)
    assert _rows(database) == before


@pytest.mark.parametrize("drift", ["required", "old_schema", "old_resolver", "target", "governance", "schedule"])
def test_optional_console_contract_rejects_non_additive_change(drift):
    old, target = _versions()
    before, after = _witness(old), _witness(target)
    invocation = after["invocation_contracts"][CONSOLE]
    if drift == "required":
        invocation["input_schema"]["required"].append("month")
    elif drift == "old_schema":
        invocation["input_schema"]["properties"]["page_size"]["type"] = "string"
    elif drift == "old_resolver":
        invocation["dynamic_resolvers"]["page_size"] = "verified_optional_console_page_size"
    elif drift == "target":
        invocation["operation"] = "another_operation"
    elif drift == "governance":
        invocation["governance"]["risk_level"] = "high"
    else:
        after["scheduling"]["max_daily_times"] += 1
    with pytest.raises(ValueError):
        optional_console_input_additions(before, after)


@pytest.mark.parametrize("drift", ["config", "bindings", "arguments", "resolver", "target", "entrypoints", "bool", "float"])
def test_optional_console_preparation_rejects_saved_or_compiled_change(drift):
    old, target = _versions()
    config, sources = {"page_size": 1}, [CONSOLE]
    accounts = {role["role"]: "isolated-" + role["role"] for role in old.manifest["account_roles"]}
    current = {"config_json": config, "account_bindings_json": accounts, "resource_bindings_json": {},
        "enabled_entrypoints_json": sources, "desired_schedule_json": {"kind": "none", "times": [], "enabled": False},
        "device_id": None, "compiled_invocations_json": _compiled(old, config, accounts, sources)}
    request = {"config": copy.deepcopy(config), "account_bindings": copy.deepcopy(accounts), "resource_bindings": {},
        "enabled_entrypoints": list(sources), "schedule": copy.deepcopy(current["desired_schedule_json"]),
        "device_id": None, "compiled_invocations": _compiled(target, config, accounts, sources)}
    if drift in {"bool", "float"}:
        request["config"]["page_size"] = True if drift == "bool" else 1.0
    elif drift == "config":
        request["config"]["page_size"] = 30
    elif drift == "bindings":
        request["account_bindings"]["extra"] = "another-account"
    elif drift == "entrypoints":
        request["enabled_entrypoints"].append(SCHEDULER)
    else:
        compiled = request["compiled_invocations"][CONSOLE]
        if drift == "arguments":
            compiled["arguments"]["page_size"] = 7
        elif drift == "resolver":
            compiled["dynamic_resolvers"]["month"] = "verified_console_month"
        else:
            compiled["target"]["operation"] = "another_operation"
    with pytest.raises(ValueError):
        validate_optional_console_upgrade_configuration(_witness(old), _witness(target), current, request)


@MYSQL
@pytest.mark.parametrize("tamper", ["previous_witness", "target_witness", "target_digest", "actor"])
def test_shared_preparation_rejects_forged_upgrade_proof(database, tamper):  # noqa: F811
    adapter, old, target = _seed(database)
    before = _rows(database)
    previous, witness = _witness(old), _witness(target)
    identity = {"from_version": old.version, "to_version": target.version, "package_sha256": target.package_sha256}
    if tamper == "previous_witness":
        previous["invocation_contracts"][CONSOLE]["operation"] = "forged"
    elif tamper == "target_witness":
        witness["invocation_contracts"][CONSOLE]["dynamic_resolvers"]["month"] = "forged"
    elif tamper == "target_digest":
        identity["package_sha256"] = "f" * 64
    with pytest.raises((ValueError, OrchestrationPersistenceError)):
        with database.repository.unit_of_work() as uow:
            adapter._register(uow.automation_plugins, target, actor_id=ACTOR)
            current = uow.automation_plugins.get_project_config(PROJECT, for_update=True)
            uow.automation_plugins.save_project_config(PROJECT, config=current["config_json"],
                account_bindings=current["account_bindings_json"], resource_bindings=current["resource_bindings_json"],
                enabled_entrypoints=current["enabled_entrypoints_json"], schedule=current["desired_schedule_json"],
                compiled_invocations=_compiled(target, current["config_json"], current["account_bindings_json"],
                    current["enabled_entrypoints_json"]), contract_witness=witness, 
                actor_id=ACTOR, actor_role="viewer" if tamper == "actor" else "super_admin",
                request_id=str(uuid4()), expected_project_configuration_version=current["config_version"],
                optional_console_upgrade={"target": identity, "previous_contract_witness": previous})
            uow.commit()
    assert _rows(database) == before
    with database.repository.unit_of_work() as uow:
        assert uow.automation_plugins.get_version(target.plugin_id, target.version) is None
