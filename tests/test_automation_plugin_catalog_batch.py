"""Real MySQL display batching keeps bad rows and cached identities isolated."""
from dataclasses import replace
from uuid import uuid4
import pytest

from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.catalog_read_scope import catalog_read_transaction, catalog_row_cache
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.mysql_repository import MySQLAutomationPluginRepositoryAdapter
from agent.orchestration.automation_project_policy_support import scoped_policy_projection
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter, MySQLAutomationProjectConfigurationReadAdapter,
)
from shared.orchestration_repository import OrchestrationRepository
from shared.orchestration_repository_support import OrchestrationPersistenceError
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


def test_batch_schedule_and_migration_rows_match_direct_reads_and_keep_conflicts_local(database):
    fixture, name = database
    repository = OrchestrationRepository(lambda: fixture.pymysql.connect(
        host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
        database=name, charset="utf8mb4", cursorclass=fixture.pymysql.cursors.DictCursor,
    ))
    source, target, unrelated = ("batch_" + uuid4().hex for _ in range(3))
    pair_id = str(uuid4())
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        for identity in (source, target, unrelated):
            cursor.execute(
                "INSERT INTO scheduled_tasks (id, automation_id, name, tool_name, tool_params, "
                "cron_expression, enabled, automation_generation, configuration_version) "
                "VALUES (%s,%s,'isolated batch schedule','synthetic_action',%s,'55 23 * * *',0,1,2)",
                (identity, identity, '{"target_date":"2026-09-11"}'),
            )
        def insert_pair(identifier, destination):
            cursor.execute(
                "INSERT INTO automation_plugin_migration_pairs "
                "(migration_pair_id,source_automation_id,target_automation_id,state,entrypoint_snapshot_json,"
                "entrypoint_snapshot_sha256,create_request_id,created_by_actor_id,created_by_actor_role,"
                "last_transition_request_id,last_transition_actor_id,last_transition_actor_role,last_transition_reason) "
                "VALUES (%s,%s,%s,'TESTING','{}',%s,%s,'isolated-admin','super_admin',%s,"
                "'isolated-admin','super_admin','isolated catalog test')",
                (identifier, source, destination, '0' * 64, identifier, identifier),
            )
        insert_pair(pair_id, target)
        connection.commit()
    with repository.unit_of_work() as uow:
        display = uow.automation_plugins.read_catalog_rows((source, target))
        for identity in (source, target):
            assert display.schedules[identity] == uow.automation_projects.list_configuration_rows(identity)
            assert display.migration_pair(identity) == uow.automation_plugins.get_active_plugin_migration_pair_for_automation(identity, for_update=False)
        assert unrelated not in display.schedules
        assert display.migration_pair(source)['migration_pair_id'] == pair_id
        changed = display.migration_pair(source)
        changed['state'] = 'COMPLETED'
        assert display.migration_pair(source)['state'] == 'TESTING'
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        insert_pair(str(uuid4()), unrelated)
        connection.commit()
    with repository.unit_of_work() as uow:
        display = uow.automation_plugins.read_catalog_rows((source, target, unrelated))
        with pytest.raises(OrchestrationPersistenceError, match='multiple active migration pairs'):
            display.migration_pair(source)
        with pytest.raises(OrchestrationPersistenceError, match='multiple active migration pairs'):
            uow.automation_plugins.get_active_plugin_migration_pair_for_automation(source, for_update=False)
        assert display.migration_pair(target)['migration_pair_id'] == pair_id
        assert display.migration_pair(unrelated)['target_automation_id'] == unrelated


def test_catalog_projection_batches_versions_and_migration_history(database):
    fixture, name = database
    statements = []
    connections = []

    def connect():
        connections.append(1)
        connection = fixture.pymysql.connect(
            host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
            database=name, charset="utf8mb4", cursorclass=fixture.pymysql.cursors.DictCursor,
        )
        original_cursor = connection.cursor

        def counting_cursor(*args, **kwargs):
            cursor = original_cursor(*args, **kwargs)
            original_execute = cursor.execute

            def execute(query, args=None):
                statements.append(" ".join(str(query).split()))
                return original_execute(query, args)

            cursor.execute = execute
            return cursor

        connection.cursor = counting_cursor
        return connection

    repository = OrchestrationRepository(connect)
    packages = MySQLAutomationPluginRepositoryAdapter(repository, release_hold_provider=lambda: False)
    marker = uuid4().hex
    installed = []
    for index in range(3):
        raw = _synthetic_manifest("1.0.0").to_mapping()
        raw["plugin_id"] = f"batch_{marker}_{index}"
        manifest = AutomationPluginManifest.from_mapping(raw)
        installed.append(packages.install_instance(
            replace(_version(manifest, "1"), installed_at=None), instance_name=f"synthetic batch {index}",
            actor_id="synthetic-catalog-admin", actor_role="super_admin", request_id=str(uuid4())))
    catalog = PluginCatalog(MySQLAutomationPluginCatalogRepositoryAdapter(repository),
        MySQLAutomationProjectConfigurationReadAdapter(repository),
        migration_pair_provider=packages.get_catalog_migration_pair)
    statements.clear()
    with catalog.read_scope():
        view = catalog.safe_projection(module="automation")
        assert {item.automation_id for item in installed} <= {item["automation_id"] for item in view["instances"]}
        per_key_reads = [sql for sql in statements if "FROM automation_plugin_versions WHERE plugin_id=%s" in sql]
        assert per_key_reads == []
        rows = catalog_row_cache(repository)
        with catalog_read_transaction(repository) as uow:
            low = uow.automation_plugins
            for project in installed:
                display = rows[project.automation_id]
                key = (project.plugin_id, project.active_version.version)
                assert key in display.versions
                assert display.version(low, *key) == low.get_version(*key)
                assert display.authoritative_migration_pair(project.automation_id) == (
                    low.get_authoritative_plugin_migration_pair_for_automation(project.automation_id, for_update=False))
            # Keys outside the batch keep the direct read and its result.
            assert display.version(low, installed[0].plugin_id, "9.9.9") is None
        # The policy projection served with the catalog reuses its snapshot
        # connection instead of opening a second cross-region connection.
        opened = len(connections)
        identities = {item.automation_id for item in installed}
        policies = scoped_policy_projection(repository, catalog, identities,
            lambda entry, policy: {"automation_id": entry.automation_id})
        assert {item["automation_id"] for item in policies["items"]} == identities
        assert len(connections) == opened


def test_batched_authoritative_migration_history_matches_direct_reads(database):
    fixture, name = database
    repository = OrchestrationRepository(lambda: fixture.pymysql.connect(
        host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
        database=name, charset="utf8mb4", cursorclass=fixture.pymysql.cursors.DictCursor,
    ))
    source, target, other_target, untouched = ("history_" + uuid4().hex for _ in range(4))
    def insert_pair(cursor, destination, state):
        identifier = str(uuid4())
        cursor.execute(
            "INSERT INTO automation_plugin_migration_pairs "
            "(migration_pair_id,source_automation_id,target_automation_id,state,entrypoint_snapshot_json,"
            "entrypoint_snapshot_sha256,create_request_id,created_by_actor_id,created_by_actor_role,"
            "last_transition_request_id,last_transition_actor_id,last_transition_actor_role,last_transition_reason) "
            "VALUES (%s,%s,%s,%s,'{}',%s,%s,'isolated-admin','super_admin',%s,"
            "'isolated-admin','super_admin','isolated history test')",
            (identifier, source, destination, state, '0' * 64, identifier, identifier),
        )

    with fixture._connection(name) as connection, connection.cursor() as cursor:
        insert_pair(cursor, target, "TESTING")
        connection.commit()
    identities = (source, target, other_target, untouched)
    with repository.unit_of_work() as uow:
        display = uow.automation_plugins.read_catalog_rows(identities)
        for identity in identities:
            assert display.authoritative_migration_pair(identity) == (
                uow.automation_plugins.get_authoritative_plugin_migration_pair_for_automation(identity, for_update=False))
        assert display.authoritative_migration_pair(untouched) is None
        copied = display.authoritative_migration_pair(source)
        copied["state"] = "COMPLETED"
        assert display.authoritative_migration_pair(source)["state"] == "TESTING"
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        insert_pair(cursor, other_target, "READY")
        connection.commit()
    with repository.unit_of_work() as uow:
        display = uow.automation_plugins.read_catalog_rows(identities)
        for reader in (lambda: display.authoritative_migration_pair(source),
                       lambda: uow.automation_plugins.get_authoritative_plugin_migration_pair_for_automation(source, for_update=False)):
            with pytest.raises(OrchestrationPersistenceError, match="ambiguous unfinished migration ownership"):
                reader()
        assert display.authoritative_migration_pair(other_target)["target_automation_id"] == other_target
