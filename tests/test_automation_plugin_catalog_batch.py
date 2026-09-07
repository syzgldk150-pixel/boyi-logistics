"""Real MySQL display batching keeps bad rows and cached identities isolated."""
from dataclasses import replace
from uuid import uuid4

from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.catalog_read_scope import catalog_row_cache
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.mysql_repository import MySQLAutomationPluginRepositoryAdapter
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter, MySQLAutomationProjectConfigurationReadAdapter,
)
from shared.orchestration_repository import OrchestrationRepository
from tests.test_module_data_sources_mysql import database  # noqa: F401
from tests.test_automation_plugin_upgrade import _synthetic_manifest, _version


def test_real_batch_reads_scope_bad_configuration_and_next_request_refresh(database):
    fixture, name = database
    repository = OrchestrationRepository(lambda: fixture.pymysql.connect(
        host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
        database=name, charset="utf8mb4", cursorclass=fixture.pymysql.cursors.DictCursor,
    ))
    packages = MySQLAutomationPluginRepositoryAdapter(repository, release_hold_provider=lambda: False)
    marker = uuid4().hex

    def install(module, label):
        raw = _synthetic_manifest("1.0.0").to_mapping()
        raw["plugin_id"] = "catalog_" + marker + "_" + module + "_" + uuid4().hex[:8]
        if module == "finance":
            raw["management"] = {"purpose": "collector", "module": "finance",
                "dataset": "finance.transactions", "version": "1"}
        manifest = AutomationPluginManifest.from_mapping(raw)
        version = replace(_version(manifest, "1"), installed_at=None)
        return packages.install_instance(version, instance_name=label,
            actor_id="synthetic-catalog-admin", actor_role="super_admin", request_id=str(uuid4()))

    valid = install("automation", "synthetic old name")
    invalid = install("automation", "synthetic invalid config")
    unknown_owner = install("automation", "synthetic invalid ownership")
    other = install("finance", "synthetic other module")
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE automation_project_configs SET desired_schedule_json='[]' WHERE automation_id=%s",
            (invalid.automation_id,))
        cursor.execute("UPDATE automation_plugin_versions SET manifest_json=JSON_SET(manifest_json, '$.management', JSON_OBJECT('purpose', 'action', 'module', 'invalid_module', 'dataset', '', 'version', '')) WHERE plugin_id=%s AND version=%s",
            (unknown_owner.plugin_id, unknown_owner.active_version.version))
        connection.commit()
    catalog = PluginCatalog(MySQLAutomationPluginCatalogRepositoryAdapter(repository),
        MySQLAutomationProjectConfigurationReadAdapter(repository))
    with catalog.read_scope():
        view = catalog.safe_projection(module="automation")
        assert valid.automation_id in {item["automation_id"] for item in view["instances"]}
        assert invalid.automation_id in view["unavailable_projects"]
        assert unknown_owner.automation_id in view["unavailable_projects"]
        assert unknown_owner.automation_id not in {item["automation_id"] for item in view["instances"]}
        assert other.automation_id in view["hidden_automation_ids"]
        rows = catalog_row_cache(repository)
        assert valid.automation_id in rows and invalid.automation_id in rows
        assert other.automation_id not in rows
        with fixture._connection(name) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE automation_projects SET display_name=%s WHERE automation_id=%s",
                ("synthetic new name", valid.automation_id))
            connection.commit()
        assert catalog.require(valid.automation_id).display_name == "synthetic old name"
    assert catalog_row_cache(repository) is None
    assert catalog.require(valid.automation_id).display_name == "synthetic new name"
