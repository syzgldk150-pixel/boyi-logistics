"""Daily times are the host timer's: a times-only save never stages a generation.

Kind and on/off changes still switch the scheduler entrypoint through a
generation. The test uses the real repository, MySQL, signed V2 ZIP install
and the management service; no plugin subprocess runs.
"""
import hashlib
import json
from uuid import uuid4

import pytest

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.automation_plugins.errors import PluginConflictError
from tests.test_v2_maintenance_mysql import ACTOR, database  # noqa: F401
from shared.connection_reuse import reuse_transaction_connections
from tests.v32_acceptance.management_fixture import ManagementFixture

PLUGIN_ID = "v32_schedule_times"


def _install_scheduled_plugin(host, tmp_path):
    source = tmp_path / "source"
    init_service_v2_source(source, plugin_id=PLUGIN_ID, name="定时合成插件", version="1.0.0")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    console = manifest["contributes"]["console"][0]
    manifest["contributes"]["scheduler"] = [{
        "id": "daily_run", "title": "定时合成插件", "service": console["service"],
        "operation": console["operation"], "default_enabled": False,
    }]
    manifest["contributes"]["harness"] = []
    manifest["management"] = {"purpose": "action", "module": "automation", "dataset": "", "version": ""}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = tmp_path / "plugin.zip"
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    host.management.install_service_v2(
        package, request_id=str(uuid4()), transport_package_sha256=hashlib.sha256(package).hexdigest(),
        raw_intent=json.dumps({"instance_name": "定时合成插件", "permissions_confirmed": True}), actor=ACTOR,
    )
    automation_id = next(entry.automation_id for entry in host.catalog.list() if entry.plugin_id == PLUGIN_ID)
    entry = host.catalog.require(automation_id)
    host.management.save_plugin_settings(
        automation_id, config={}, account_bindings={}, resource_bindings={}, request_id=str(uuid4()),
        expected_project_configuration_version=entry.project_config_version, actor=ACTOR,
    )
    host.targets.reconcile_project(automation_id)
    entry = host.catalog.require(automation_id)
    host.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
                                expected_record_version=entry.record_version, actor=ACTOR)
    return automation_id


def _save(host, automation_id, times, *, expected=None, enabled=True, request_id=None):
    entry = host.catalog.require(automation_id)
    return host.management.save_console_schedule(
        automation_id, schedule={"kind": "daily_times", "times": times, "enabled": enabled},
        request_id=request_id or str(uuid4()),
        expected_project_configuration_version=entry.project_config_version, actor=ACTOR,
        expected_schedule=expected,
    )


def _rows(host, automation_id):
    with host.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
        cursor.execute(
            """SELECT task.id, task.cron_expression, task.automation_generation, task.configuration_version,
                      task.enabled, task.tool_params, policy.mode
               FROM scheduled_tasks AS task
               LEFT JOIN scheduled_task_approval_policies AS policy ON policy.task_id=task.id
               WHERE task.automation_id=%s ORDER BY task.cron_expression""",
            (automation_id,),
        )
        return list(cursor.fetchall())


def _generation_count(host, automation_id):
    with host.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM automation_project_generations WHERE automation_id=%s",
                       (automation_id,))
        return int(cursor.fetchone()["n"])


def test_times_only_save_rewrites_rows_on_the_committed_generation(database, tmp_path, monkeypatch):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    connections = []

    def connect():
        connections.append(True)
        return fixture.pymysql.connect(
            host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
            database=name, charset="utf8mb4", autocommit=False, cursorclass=fixture.pymysql.cursors.DictCursor,
        )

    with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "host",
                           enable_directory_faults=False) as host:
        project = _install_scheduled_plugin(host, tmp_path)

        # Turning the schedule on is a kind/on change: it switches the
        # scheduler entrypoint through a new generation.
        before_enable = _generation_count(host, project)
        connections.clear()
        enabled = _save(host, project, ["08:00"])
        full_save_connections = len(connections)
        assert "schedule_times_only" not in enabled
        assert _generation_count(host, project) == before_enable + 1
        entry = host.catalog.require(project)
        committed, version = entry.committed_generation, entry.project_config_version
        first_rows = _rows(host, project)
        assert [row["cron_expression"] for row in first_rows] == ["0 8 * * *"]

        # Adding one time is the host timer's alone.
        generations = _generation_count(host, project)
        connections.clear()
        request_id = str(uuid4())
        saved = _save(host, project, ["09:30", "08:00"],
                      expected={"kind": "daily_times", "times": ["08:00"], "enabled": True},
                      request_id=request_id)
        times_only_connections = len(connections)
        assert saved["schedule_times_only"] is True
        assert saved["schedule"] == {"kind": "daily_times", "times": ["08:00", "09:30"], "enabled": True}
        assert saved["project_configuration_version"] == version
        assert saved["generation_ready"] is True
        assert _generation_count(host, project) == generations
        rows = _rows(host, project)
        assert [row["cron_expression"] for row in rows] == ["0 8 * * *", "30 9 * * *"]
        assert rows[0]["id"] == first_rows[0]["id"]
        assert {(row["automation_generation"], row["configuration_version"], bool(row["enabled"]),
                 row["mode"]) for row in rows} == {(committed, version, True, "REQUIRE_EACH_RUN")}
        assert len({row["tool_params"] for row in rows}) == 1
        # Catalog/readiness reads and one save transaction, never the
        # dozens of per-step connections of a generation switch.
        assert times_only_connections <= 6, times_only_connections
        assert times_only_connections * 4 < full_save_connections, (times_only_connections, full_save_connections)
        after = host.catalog.require(project)
        assert after.committed_generation == committed
        assert after.project_schedule == {"kind": "daily_times", "times": ["08:00", "09:30"], "enabled": True}

        # The same request replays its recorded result; changed input cannot reuse it.
        replay = _save(host, project, ["09:30", "08:00"],
                       expected={"kind": "daily_times", "times": ["08:00"], "enabled": True},
                       request_id=request_id)
        assert replay["schedule"] == saved["schedule"]
        with pytest.raises(PluginConflictError) as reused:
            _save(host, project, ["10:00"],
                  expected={"kind": "daily_times", "times": ["08:00"], "enabled": True},
                  request_id=request_id)
        assert reused.value.code == "PLUGIN_SCHEDULE_REQUEST_REUSED"
        # A stale editing base is rejected instead of overwriting.
        with pytest.raises(PluginConflictError) as stale:
            _save(host, project, ["11:00"], expected={"kind": "daily_times", "times": ["08:00"], "enabled": True})
        assert stale.value.code == "PLUGIN_SCHEDULE_CHANGED"
        with host.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM automation_project_schedule_changes WHERE automation_id=%s",
                           (project,))
            assert int(cursor.fetchone()["n"]) == 1

        # A restart-style reconcile sees no generation material change.
        host.targets.reconcile_project(project)
        assert _generation_count(host, project) == generations
        assert host.catalog.require(project).committed_generation == committed

        # Removing a time deletes only its row (and its policy row).
        _save(host, project, ["09:30"], expected={"kind": "daily_times", "times": ["08:00", "09:30"], "enabled": True})
        assert [row["cron_expression"] for row in _rows(host, project)] == ["30 9 * * *"]

        # A later full settings save commits a generation that keeps the
        # host-saved times rather than an older snapshot's.
        entry = host.catalog.require(project)
        host.management.save_plugin_settings(
            project, config={}, account_bindings={}, resource_bindings={}, request_id=str(uuid4()),
            expected_project_configuration_version=entry.project_config_version, actor=ACTOR,
        )
        host.targets.reconcile_project(project)
        latest = host.catalog.require(project)
        rows = _rows(host, project)
        assert [row["cron_expression"] for row in rows] == ["30 9 * * *"]
        assert {row["automation_generation"] for row in rows} == {latest.committed_generation}

        # Turning the schedule off remains a generation switch.
        generations = _generation_count(host, project)
        disabled = _save(host, project, ["09:30"], enabled=False,
                         expected={"kind": "daily_times", "times": ["09:30"], "enabled": True})
        assert "schedule_times_only" not in disabled
        assert _generation_count(host, project) == generations + 1
        assert {bool(row["enabled"]) for row in _rows(host, project)} == {False}


def test_full_schedule_switch_reuses_one_connection_per_save_request(database, tmp_path, monkeypatch):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    connections = []

    def connect():
        connections.append(True)
        return fixture.pymysql.connect(
            host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
            database=name, charset="utf8mb4", autocommit=False, cursorclass=fixture.pymysql.cursors.DictCursor,
        )

    with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "host",
                           enable_directory_faults=False) as host:
        project = _install_scheduled_plugin(host, tmp_path)
        generations = _generation_count(host, project)
        connections.clear()
        # The management API runs each save inside this scope.
        with reuse_transaction_connections():
            enabled = _save(host, project, ["08:00"])
        assert "schedule_times_only" not in enabled
        assert enabled["generation_ready"] is True
        assert _generation_count(host, project) == generations + 1
        assert [row["cron_expression"] for row in _rows(host, project)] == ["0 8 * * *"]
        # Dozens of sequential transactions; only nested reads open extras.
        assert len(connections) <= 12, len(connections)
