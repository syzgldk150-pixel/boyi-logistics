"""Release windows check current host times against the committed V2 task set."""
import copy
import hashlib
import json

import pytest

from scripts import scheduled_write_window as window
from tests.test_v2_maintenance_mysql import database  # noqa: F401


@pytest.fixture
def preflight():
    return window


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _v2_rows(preflight):
    desired = {"kind": "daily_times", "times": ["22:17"], "enabled": True}
    snapshot = {
        "automation_id": "v2-clockin", "generation": 3, "runtime_model": "SERVICE_V2", "plugin_api": "2.0.0",
        "enabled_entrypoints": ["daily_clockin"],
        "execution_metadata": {
            "project_config_version": 7,
            "schedule": {"kind": "daily_times", "times": ["18:30"], "enabled": True},
            "contributions": {"scheduler": [{"id": "daily_clockin", "default_enabled": True,
                                               "schedule": {"timezone": "Asia/Shanghai"}}]},
            "compiled_invocations": {"daily_clockin": {"arguments": {}}},
            "governance_anchor": {"operation_type": "external_write"},
        },
    }
    return [{
        "task_id": "v2-clockin-2217", "automation_id": "v2-clockin",
        "automation_generation": 3, "committed_generation": 3, "target_generation": 3,
        "tool_name": "automation.v2-clockin.run", "cron_expression": "17 22 * * *", "enabled": 1,
        "configuration_version": 7, "config_version": 7,
        "desired_schedule_json": desired, "desired_schedule_sha256": _canonical_sha256(desired),
        "snapshot_json": snapshot, "project_enabled": 1, "project_state": "ENABLED",
        "reconcile_state": "STABLE", "generation_state": "COMMITTED", "policy_mode": "PROJECT_FULL_AUTO",
    }]


def _v2_validate(preflight, rows):
    class Cursor:
        def execute(self, sql):
            assert "LEFT JOIN scheduled_tasks" in sql
            assert "config.desired_schedule_json" in sql
            assert "generation.runtime_model='SERVICE_V2'" in sql

        def fetchall(self):
            return rows

    return preflight.project_scheduled_write_crons(Cursor())


def test_v2_release_uses_current_host_times_and_keeps_generation_snapshot(preflight):
    rows = _v2_rows(preflight)
    original = copy.deepcopy(rows)
    assert _v2_validate(preflight, rows) == ("17 22 * * *",)
    assert rows == original


@pytest.mark.parametrize("drift", ["times", "generation", "version", "missing", "extra", "duplicate", "digest", "shape"])
def test_v2_release_rejects_projection_drift(preflight, drift):
    rows = _v2_rows(preflight)
    if drift == "times":
        rows[0]["cron_expression"] = "30 18 * * *"
    elif drift == "generation":
        rows[0]["automation_generation"] = 2
    elif drift == "version":
        rows[0]["configuration_version"] = 6
    elif drift == "missing":
        rows[0].update(task_id=None, cron_expression=None, enabled=None)
    elif drift in {"extra", "duplicate"}:
        rows.append(dict(rows[0], task_id="extra", cron_expression="0 23 * * *" if drift == "extra" else "17 22 * * *"))
    elif drift == "digest":
        rows[0]["desired_schedule_sha256"] = "f" * 64
    else:
        rows[0]["desired_schedule_json"]["enabled"] = False
        rows[0]["desired_schedule_sha256"] = _canonical_sha256(rows[0]["desired_schedule_json"])
    with pytest.raises(preflight.ScheduledWriteWindowError):
        _v2_validate(preflight, rows)


@pytest.mark.parametrize("kind,times,cron", [("none", [], None), ("startup", [], "@startup"), ("daily_times", ["22:17"], "17 22 * * *")])
def test_v2_disabled_schedules_validate_their_complete_physical_set(preflight, kind, times, cron):
    rows = _v2_rows(preflight)
    desired = {"kind": kind, "times": times, "enabled": False}
    rows[0].update(desired_schedule_json=desired, desired_schedule_sha256=_canonical_sha256(desired),
                   enabled=0, cron_expression=cron, task_id=None if cron is None else "disabled-task")
    rows[0]["snapshot_json"]["execution_metadata"]["schedule"] = copy.deepcopy(desired)
    assert _v2_validate(preflight, rows) == ()
    if cron is not None:
        rows[0].update(task_id=None, cron_expression=None)
        with pytest.raises(preflight.ScheduledWriteWindowError, match="PROJECT_SCHEDULE_ROWS_MISMATCH"):
            _v2_validate(preflight, rows)


def test_v2_staged_configuration_keeps_existing_isolated_projection_semantics(preflight):
    rows = _v2_rows(preflight)
    rows[0].update(project_state="UPGRADING", reconcile_state="PREPARING", target_generation=4,
                   policy_project_generation=4, target_generation_state="TARGET", target_base_generation=3,
                   max_generation=4, generation_error_code=None, unsafe_non_disposed_other_count=0,
                   active_current_lease_count=0, unknown_write_count=0)
    rows[0]["desired_schedule_json"] = {"kind": "startup", "times": [], "enabled": True}
    assert _v2_validate(preflight, rows) == ()


def test_v2_projection_query_detects_entirely_missing_tasks_mysql(preflight, database, tmp_path, monkeypatch):
    from tests.test_schedule_times_host_save_mysql import _install_scheduled_plugin, _save
    from tests.v32_acceptance.management_fixture import ManagementFixture

    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)

    def connect():
        return fixture.pymysql.connect(
            host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
            database=name, charset="utf8mb4", autocommit=False,
            cursorclass=fixture.pymysql.cursors.DictCursor,
        )

    def validate(host):
        with host.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
            return preflight.project_scheduled_write_crons(cursor)

    with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "runtime",
                           enable_directory_faults=False) as host:
        project = _install_scheduled_plugin(host, tmp_path)
        _save(host, project, ["08:00"])
        entry = host.catalog.require(project)
        _save(host, project, ["09:30", "18:00"], expected=entry.project_schedule)
        validate(host)
        with host.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
            cursor.execute("DELETE FROM scheduled_tasks WHERE automation_id=%s", (project,))
            uow.commit()
        with pytest.raises(preflight.ScheduledWriteWindowError, match="PROJECT_SCHEDULE_ROWS_MISMATCH"):
            validate(host)
