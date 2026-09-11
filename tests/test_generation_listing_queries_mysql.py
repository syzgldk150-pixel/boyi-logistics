"""Generation history stays complete without per-generation SQL round trips."""
from uuid import uuid4

from tests.test_v2_maintenance_mysql import ACTOR, database  # noqa: F401
from tests.v32_acceptance.management_fixture import ManagementFixture


def test_batch_generation_listing_matches_individual_rows(database, tmp_path, monkeypatch):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    statements = []

    class CountingCursor(fixture.pymysql.cursors.DictCursor):
        def execute(self, query, args=None):
            statements.append(query)
            return super().execute(query, args)

    def connect():
        return fixture.pymysql.connect(
            host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, database=name, charset="utf8mb4",
            autocommit=False, cursorclass=CountingCursor,
        )

    with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "host",
                           enable_directory_faults=False) as host:
        host.seed_instances(per_module=1)
        entries = host.catalog.list()
        project = next(entry.automation_id for entry in entries
                       if entry.plugin_id == "v32_list_automation")
        for enabled in (True, False, True, False, True, False):
            entry = host.catalog.require(project)
            host.management.save_configuration(
                project, config={}, account_bindings={}, resource_bindings={},
                enabled_entrypoints=("run",) if enabled else (),
                schedule={"kind": "none", "times": [], "enabled": False}, device_id=None,
                request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version,
                actor=ACTOR,
            )
            host.targets.reconcile_project(project)
        with host.repository.unit_of_work() as uow:
            statements.clear()
            actual = uow.automation_plugins.list_generation_rows(project)
            # Three batch reads plus the public repository's activation journal.
            assert len(statements) == 4
            assert len(actual) >= 6
            expected = [uow.automation_plugins.get_generation_row(project, row["generation"])
                        for row in actual]
            assert actual == expected
            assert any(row["effects"] for row in actual)
            assert any(row["coeffects"] for row in actual)
            assert any(row["state"] == "DISPOSED" for row in actual)
        statements.clear()
        for row in actual:
            assert host.runtime_repository.list_active_generation_leases(project, row["generation"]) == ()
        assert not any("FROM automation_project_generations" in query for query in statements)
