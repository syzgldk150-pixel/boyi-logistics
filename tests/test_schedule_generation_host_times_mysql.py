"""A compiled generation must not overwrite newer host-owned daily times."""
from uuid import uuid4

from agent.automation_plugins.production_snapshot import build_runtime_generation_snapshot
from tests.test_v2_maintenance_mysql import database  # noqa: F401
from tests.test_schedule_times_host_save_mysql import _install_scheduled_plugin, _rows, _save
from tests.v32_acceptance.management_fixture import ManagementFixture


def test_compiled_generation_commits_latest_host_times(database, tmp_path, monkeypatch):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)

    def connect():
        return fixture.pymysql.connect(
            host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
            database=name, charset="utf8mb4", autocommit=False,
            cursorclass=fixture.pymysql.cursors.DictCursor,
        )

    with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "runtime",
                           enable_directory_faults=False) as host:
        project = _install_scheduled_plugin(host, tmp_path)
        _save(host, project, ["08:00"])
        entry = host.catalog.require(project)
        config, policy = host.targets._desired_rows(project)
        compiled = build_runtime_generation_snapshot(
            entry, desired_config_row=config,
            policy_row=dict(policy, project_generation=entry.committed_generation + 1),
            generation=entry.committed_generation + 1, core_catalog=host.core_catalog,
        )
        _save(host, project, ["09:30", "18:00"], expected=entry.project_schedule)
        assert compiled.execution_metadata["schedule"]["times"] == ["08:00"]

        # Allocation and commit independently compare kind/on-off, and the
        # physical projection must take the latest locked configuration times.
        result = host.reconciler.reconcile(
            compiled, expected_committed_generation=entry.committed_generation,
            request_id=str(uuid4()),
        )
        assert result.committed_generation == compiled.generation
        rows = _rows(host, project)
        assert {row["cron_expression"] for row in rows} == {"30 9 * * *", "0 18 * * *"}
        assert {row["automation_generation"] for row in rows} == {compiled.generation}
        assert {row["configuration_version"] for row in rows} == {entry.project_config_version}
        assert host.catalog.require(project).project_schedule["times"] == ["09:30", "18:00"]
