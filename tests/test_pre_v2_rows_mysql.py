"""Pre-V2 project rows stay in MySQL until migration 064 and must not stop the Host."""
from datetime import datetime
from uuid import uuid4

from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.production import ProductionRuntimeEffectDriver
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter,
    MySQLAutomationPluginRuntimeAdapter,
    MySQLAutomationProjectConfigurationReadAdapter,
)
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.orchestration_repository import OrchestrationRepository
from shared.orchestration_repository_support import _json_hash
from tests.test_v2_maintenance_mysql import database  # noqa: F401


def _commit_action_v1_project(connection, cursor_factory) -> str:
    """Write a committed, enabled ACTION_V1 project through the repository API."""

    automation_id = f"pre_v2_{uuid4().hex[:20]}"
    plugin_id = f"pre_v2_plugin_{uuid4().hex[:16]}"
    tool_contract = {"name": f"integration.{automation_id}.run", "version": "1.0.0"}
    runtime = {"kind": "python_subprocess", "entrypoint": "payload/main.py"}
    scheduling = {"supported": False}
    governance_anchor = {"schema_version": 1, "authority": "integration"}
    install_metadata = {"install_root": f"/integration/plugins/{plugin_id}/1.0.0", "python_relative": "venv/bin/python"}
    manifest = {
        "allowed_entrypoints": [], "account_roles": [],
        "config_schema": {"type": "object", "additionalProperties": False},
        "governance_anchor": governance_anchor, "invocation_contracts": {}, "resource_roles": [],
        "runtime": runtime, "runtime_permissions": {}, "scheduling": scheduling,
        "tool_contract": tool_contract, "worker_requirement": {"kind": "server"},
    }
    package_sha256 = _json_hash({"plugin_id": plugin_id, "version": "1.0.0"})
    manifest_sha256 = _json_hash(manifest)
    runtime_descriptor = {"runtime": runtime, "runtime_permissions": {}, "account_roles": [],
                          "resource_roles": [], "install_metadata": install_metadata}
    repository = AutomationPluginRepository(connection, cursor_factory=cursor_factory)
    repository.register_package_version(
        package={"plugin_id": plugin_id, "display_name": "pre-v2", "description": "integration only"},
        version={
            "version": "1.0.0", "package_sha256": package_sha256, "manifest_sha256": manifest_sha256,
            "manifest_json": manifest, "tool_contract_sha256": _json_hash(tool_contract),
            "config_schema_sha256": _json_hash(manifest["config_schema"]),
            "allowed_entrypoints_sha256": _json_hash([]), "invocation_contracts_sha256": _json_hash({}),
            "worker_requirement_sha256": _json_hash(manifest["worker_requirement"]),
            "runtime_sha256": _json_hash(runtime), "scheduling_sha256": _json_hash(scheduling),
            "project_full_auto_allowed": True, "trust_source": "ed25519_first_party",
            "install_root_metadata_json": install_metadata,
            "install_root_metadata_sha256": _json_hash(install_metadata),
            "installed_by_actor_id": "integration-admin",
        },
    )
    repository.install_project_instance({
        "automation_id": automation_id, "plugin_id": plugin_id, "plugin_version": "1.0.0",
        "display_name": "pre-v2", "install_request_id": str(uuid4()),
        "install_payload_sha256": _json_hash({"automation_id": automation_id}),
        "installed_by_actor_id": "integration-admin", "migration_authority": False,
    })
    initial = repository.initialize_project_config(automation_id, enabled_entrypoints=())
    config = repository.save_project_config(
        automation_id, config={}, account_bindings={}, resource_bindings={}, enabled_entrypoints=(),
        schedule={"kind": "none", "times": [], "enabled": False}, compiled_invocations={},
        contract_witness={"runtime_model": "ACTION_V1", "allowed_entrypoints": [],
                          "invocation_contracts": {}, "scheduling": scheduling},
        actor_id="integration-admin", actor_role="super_admin", request_id=str(uuid4()),
        expected_project_configuration_version=int(initial["config_version"]),
    )
    snapshot = {
        "automation_id": automation_id, "generation": 1, "plugin_id": plugin_id, "plugin_version": "1.0.0",
        "package_sha256": package_sha256, "manifest_sha256": manifest_sha256,
        "project_config_sha256": config["config_sha256"],
        "account_bindings_sha256": config["account_bindings_sha256"],
        "resource_bindings_sha256": config["resource_bindings_sha256"],
        "device_binding_sha256": config["device_binding_sha256"],
        "schedule_sha256": config["desired_schedule_sha256"],
        "core_registry_sha256": _json_hash({"revision": "integration"}),
        "tool_contract_sha256": _json_hash(tool_contract),
        "invocation_contracts_sha256": _json_hash({}),
        "compiled_invocations_sha256": config["compiled_invocations_sha256"],
        "runtime_descriptor_sha256": _json_hash(runtime_descriptor),
        "governance_anchor_sha256": _json_hash(governance_anchor),
        "policy_contract_sha256": _json_hash({"mode": "PROJECT_FULL_AUTO"}),
        "trust_source": "ed25519_first_party", "enabled_entrypoints": [],
        "execution_metadata": {
            "project_config_version": int(config["config_version"]), "project_config": config["config_json"],
            "account_bindings": config["account_bindings_json"], "resource_bindings": config["resource_bindings_json"],
            "device_binding": None, "schedule": config["desired_schedule_json"],
            "compiled_invocations": config["compiled_invocations_json"], "runtime_descriptor": runtime_descriptor,
            "action_contract": tool_contract, "governance_anchor": governance_anchor,
        },
        "created_at": datetime.now(),
    }
    repository.allocate_target_generation_row(snapshot, expected_committed_generation=None, request_id=str(uuid4()))
    repository.mark_generation_preparing_row(automation_id, 1)
    repository.replace_generation_coeffects_rows(automation_id, 1, ({
        "kind": "CORE_ADAPTER", "key": "integration", "revision": "generation-1", "ready": True,
        "observed_at": datetime.now(), "reason_code": None,
    },))
    repository.mark_generation_prepared_row(automation_id, 1)
    repository.commit_generation_cas_row(automation_id, 1, expected_committed_generation=None)
    generation = repository.get_generation_row(automation_id, 1)
    repository.complete_generation_activation_row(
        automation_id, 1, expected_transition_token=generation["activation_transition_token"])
    with connection.cursor() as cursor:
        cursor.execute("UPDATE automation_projects SET enabled=TRUE, state='ENABLED' WHERE automation_id=%s",
                       (automation_id,))
    connection.commit()
    return automation_id


def test_restart_hides_persisted_pre_v2_projects_and_skips_their_restore(database):
    fixture, name = database

    def connect():
        return fixture.pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, database=name, charset="utf8mb4", autocommit=False,
            cursorclass=fixture.pymysql.cursors.DictCursor)

    with connect() as connection:
        legacy = _commit_action_v1_project(connection, fixture.pymysql.cursors.DictCursor)

    repository = OrchestrationRepository(connect)
    catalog = PluginCatalog(MySQLAutomationPluginCatalogRepositoryAdapter(repository),
                            MySQLAutomationProjectConfigurationReadAdapter(repository))
    projection = catalog.safe_projection()
    assert legacy not in {item["automation_id"] for item in projection["instances"]}
    assert legacy not in projection.get("unavailable_projects", {})
    assert legacy in catalog.excluded_persisted_automation_ids()
    assert catalog.get(legacy) is None

    # The production startup composition, restored from the same database.
    driver = ProductionRuntimeEffectDriver(
        broker_handler_keys=(),
        project_enabled=lambda automation_id: catalog.require(automation_id).enabled,
        excluded_automation_ids=catalog.excluded_persisted_automation_ids,
    )
    driver.restore_from_repository(MySQLAutomationPluginRuntimeAdapter(repository))
