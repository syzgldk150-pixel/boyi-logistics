"""Daily times are the host timer's: a times-only save never stages a generation.

Kind and on/off changes still switch the scheduler entrypoint through a
generation. The test uses the real repository, MySQL, signed V2 ZIP install
and the management service; no plugin subprocess runs.
"""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.automation_plugins.errors import PluginConflictError
from tests.test_v2_maintenance_mysql import ACTOR, database  # noqa: F401
from shared.connection_reuse import reuse_transaction_connections
from shared.orchestration_repository_support import _json_hash, OrchestrationPersistenceError
from tests.v32_acceptance.management_fixture import ManagementFixture

PLUGIN_ID = "v32_schedule_times"


def _install_scheduled_plugin(host, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    main = source / "payload" / "main.py"
    payload = main.read_text(encoding="utf-8").replace(
        'request.get("entrypoint") not in {"console", "harness"}',
        'request.get("entrypoint") not in {"console", "harness", "scheduler"}',
    ).replace(
        '"run" if request.get("entrypoint") == "console" else "assistant_run"',
        '{"console": "run", "harness": "assistant_run", "scheduler": "daily_run"}[request["entrypoint"]]',
    )
    main.write_text(payload, encoding="utf-8")
    archive = tmp_path / "plugin.zip"
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    installed = host.management.install_service_v2(
        package, request_id=str(uuid4()), transport_package_sha256=hashlib.sha256(package).hexdigest(),
        raw_intent=json.dumps({"instance_name": "定时合成插件", "permissions_confirmed": True}), actor=ACTOR,
    )
    automation_id = installed["automation_id"]
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
        expected_schedule_sha256=_json_hash(expected) if expected is not None else None,
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

    class CountingConnection(fixture.pymysql.connections.Connection):
        begins = 0
        commits = 0

        def begin(self):
            self.begins += 1
            return super().begin()

        def commit(self):
            self.commits += 1
            return super().commit()

    def connect():
        connection = CountingConnection(
            host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
            database=name, charset="utf8mb4", autocommit=False, cursorclass=fixture.pymysql.cursors.DictCursor,
        )
        connections.append(connection)
        return connection

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
        assert enabled["schedule_sha256"] == _json_hash(enabled["schedule"])
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
        committed_connections = [connection for connection in connections if connection.commits]
        assert len(committed_connections) == 1
        assert committed_connections[0].begins == committed_connections[0].commits == 1
        assert saved["schedule_times_only"] is True
        assert saved["schedule"] == {"kind": "daily_times", "times": ["08:00", "09:30"], "enabled": True}
        assert saved["schedule_sha256"] == _json_hash(saved["schedule"])
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
        projected = next(item for item in host.catalog.safe_projection()["instances"] if item["automation_id"] == project)
        assert projected["schedule_sha256"] == saved["schedule_sha256"]

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
        # A matching browser hash cannot legitimize corrupt persisted schedule content.
        with pytest.raises(OrchestrationPersistenceError, match="digest does not match"):
            with host.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
                cursor.execute("UPDATE automation_project_configs SET desired_schedule_sha256=%s WHERE automation_id=%s",
                               ("0" * 64, project))
                uow.automation_plugins.save_project_schedule_times(
                    project, schedule={"kind": "daily_times", "times": ["11:00"], "enabled": True},
                    expected_schedule_sha256="0" * 64, scheduling=entry.scheduling,
                    actor_id=ACTOR.actor_id, actor_role="super_admin", request_id=str(uuid4()),
                    expected_project_configuration_version=version,
                )
        assert [row["cron_expression"] for row in _rows(host, project)] == ["0 8 * * *", "30 9 * * *"]
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


def test_host_saved_times_load_and_execute_after_runtime_restart(database, tmp_path, monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from agent import scheduler as scheduler_module
    from tests.direct_invocation_fixture import DirectFixture

    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    connect = fixture._repository(name)._connection_factory
    runtime_root = tmp_path / "host"
    ipc_root = Path(__file__).resolve().parents[1] / ".task_tmp" / ("schedule-" + uuid4().hex[:6])
    occurrence = datetime.now(timezone.utc).replace(microsecond=0)

    def run_saved_task(host, row, directory, at):
        with DirectFixture(host, directory=directory) as runtime:
            future = asyncio.run_coroutine_threadsafe(
                scheduler_module._execute_scheduled_tool(
                    None, task_id=row["id"], tool_name=f"automation.{project}.run",
                    arguments=json.loads(row["tool_params"]), scheduled_for=at,
                    cron_expression=row["cron_expression"], configuration_version=row["configuration_version"],
                    automation_id=project, automation_generation=row["automation_generation"],
                    automation_project_invoker=host.policy,
                ),
                runtime.loop,
            )
            result = future.result(timeout=15)
            assert result["status"] == "COMPLETED", json.dumps(result)
            return result["invocation_id"]

    with ManagementFixture(connection_factory=connect, runtime_root=runtime_root,
                           enable_directory_faults=False) as host:
        project = _install_scheduled_plugin(host, tmp_path)
        _save(host, project, ["08:00"])
        previous = host.catalog.require(project)
        saved = _save(host, project, ["09:30"],
                      expected={"kind": "daily_times", "times": ["08:00"], "enabled": True})
        assert saved["schedule_times_only"] is True
        rows = _rows(host, project)
        first_invocation = run_saved_task(host, rows[0], ipc_root / "first", occurrence)

    # Rebuild the actual catalog, contribution driver, target reconciler and
    # Invocation service from MySQL and the same installed package directory.
    with ManagementFixture(connection_factory=connect, runtime_root=runtime_root,
                           enable_directory_faults=False) as restarted:
        restarted.targets.reconcile_project(project)
        current = restarted.catalog.require(project)
        assert current.committed_generation == previous.committed_generation
        assert current.project_schedule["times"] == ["09:30"]
        assert _rows(restarted, project) == rows

        def persisted_tasks():
            with restarted.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
                cursor.execute("SELECT * FROM scheduled_tasks WHERE automation_id=%s AND enabled=1", (project,))
                tasks = list(cursor.fetchall())
            for task in tasks:
                task["tool_params"] = json.loads(task["tool_params"])
            return tasks

        scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        monkeypatch.setattr(scheduler_module, "_scheduler", scheduler)
        monkeypatch.setattr(scheduler_module, "_automation_project_job_tombstones", set())
        monkeypatch.setattr(scheduler_module, "_include_startup_catchup_for_process", False)
        core = SimpleNamespace(memory=SimpleNamespace(list_enabled_scheduled_tasks=persisted_tasks), registry=None)
        reloaded = scheduler_module.reload_scheduler(core, automation_project_invoker=restarted.policy, strict=True)
        assert reloaded["invalid_tasks"] == []
        assert reloaded["job_ids"] == [rows[0]["id"]]
        assert str(scheduler.get_job(rows[0]["id"]).trigger) == "cron[month='*', day='*', day_of_week='*', hour='9', minute='30']"
        second_invocation = run_saved_task(restarted, rows[0], ipc_root / "second", occurrence + timedelta(days=1))
        assert second_invocation != first_invocation


@pytest.mark.parametrize("gate_open,has_consumer", [(True, False), (False, False), (True, True)])
def test_dependency_retry_skips_only_an_open_gate_without_consumers(
    database, tmp_path, monkeypatch, gate_open, has_consumer,
):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    with ManagementFixture(connection_factory=fixture._repository(name)._connection_factory,
                           runtime_root=tmp_path / "host", enable_directory_faults=False) as host:
        project = _install_scheduled_plugin(host, tmp_path / "provider")
        _save(host, project, ["08:00"])
        consumers = ()
        if has_consumer:
            consumer = _install_scheduled_plugin(host, tmp_path / "consumer")
            _save(host, consumer, ["12:00"])
            consumers = (consumer,)
        if not gate_open:
            host.runtime_repository.set_project_dependency_scheduler_gate(project, dependency_ready=False)
        calls = []
        original = host.targets.reconcile_project

        def reconcile(automation_id, **kwargs):
            calls.append(automation_id)
            return original(automation_id, **kwargs)

        monkeypatch.setattr(host.targets, "reconcile_project", reconcile)
        host.targets.reconcile_provider_dependency_tree(
            project, provider_services=(), enabled=True,
            consumer_automation_ids=consumers, skip_without_consumers=True,
        )
        assert calls == ([] if gate_open and not has_consumer else [project, *consumers])
        assert all(row["enabled"] for identity in (project, *consumers) for row in _rows(host, identity))
