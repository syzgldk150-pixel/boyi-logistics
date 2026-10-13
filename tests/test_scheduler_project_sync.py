"""A times-only schedule save changes only one project's daily jobs."""
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from agent import scheduler as scheduler_module

PROJECT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def _task(task_id, automation_id, cron, *, generation=3, version=5):
    return {
        "id": task_id,
        "automation_id": automation_id,
        "automation_generation": generation,
        "name": "合成 schedule",
        "tool_name": f"automation.{automation_id}.run",
        "tool_params": {},
        "cron_expression": cron,
        "enabled": True,
        "configuration_version": version,
    }


class Memory:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def list_enabled_scheduled_tasks(self, *, automation_id=None):
        self.filters.append(automation_id)
        return [dict(row) for row in self.rows
                if automation_id is None or row["automation_id"] == automation_id]


@pytest.fixture
def scheduler(monkeypatch):
    instance = AsyncIOScheduler(timezone="Asia/Shanghai")
    monkeypatch.setattr(scheduler_module, "_scheduler", instance)
    monkeypatch.setattr(scheduler_module, "_automation_project_job_tombstones", set())
    return instance


def _core(rows):
    return SimpleNamespace(memory=Memory(rows), registry=None)


def _register(core, task):
    scheduler_module._register_scheduled_task(
        task, is_startup=False, agent_core=core, automation_project_invoker=None,
    )


def test_sync_keeps_unchanged_jobs_adds_new_and_removes_deleted_times(scheduler):
    before = [_task("ap_a", PROJECT, "0 8 * * *"), _task("ap_b", PROJECT, "0 12 * * *"),
              _task("ap_other", OTHER, "0 8 * * *")]
    core = _core(before)
    for task in before:
        _register(core, task)
    kept = scheduler.get_job("ap_a")
    other = scheduler.get_job("ap_other")

    core.memory.rows = [_task("ap_a", PROJECT, "0 8 * * *"), _task("ap_c", PROJECT, "30 9 * * *"),
                        _task("ap_other", OTHER, "0 8 * * *")]
    evidence = scheduler_module.sync_project_jobs(core, PROJECT)

    assert core.memory.filters == [PROJECT]
    assert evidence["initialized"] is True and evidence["invalid_tasks"] == []
    assert evidence["added_job_ids"] == ["ap_c"]
    assert evidence["removed_job_ids"] == ["ap_b"]
    assert evidence["job_ids"] == ["ap_a", "ap_c"]
    # Unchanged times keep their live job; other projects are untouched.
    assert scheduler.get_job("ap_a") is kept
    assert scheduler.get_job("ap_other") is other
    assert scheduler.get_job("ap_b") is None
    assert scheduler.get_job("ap_c").kwargs == {scheduler_module.AUTOMATION_JOB_OWNER_MARKER: PROJECT}


def test_sync_rejects_an_invalid_row_before_changing_any_job(scheduler):
    core = _core([_task("ap_a", PROJECT, "0 8 * * *")])
    _register(core, core.memory.rows[0])
    core.memory.rows = [_task("ap_a", PROJECT, "0 8 * * *"), _task("ap_bad", PROJECT, "not a cron")]

    with pytest.raises(scheduler_module.ScheduledTaskReloadError):
        scheduler_module.sync_project_jobs(core, PROJECT)
    assert [job.id for job in scheduler.get_jobs()] == ["ap_a"]


def test_sync_restores_previous_jobs_when_a_registration_fails(scheduler, monkeypatch):
    core = _core([_task("ap_a", PROJECT, "0 8 * * *"), _task("ap_b", PROJECT, "0 12 * * *")])
    for task in core.memory.rows:
        _register(core, task)
    core.memory.rows = [_task("ap_a", PROJECT, "0 8 * * *"), _task("ap_c", PROJECT, "30 9 * * *"),
                        _task("ap_d", PROJECT, "45 10 * * *")]
    original = scheduler_module._register_scheduled_task

    def fail_second(task, **kwargs):
        if task["id"] == "ap_d":
            raise RuntimeError("registration failed")
        return original(task, **kwargs)

    monkeypatch.setattr(scheduler_module, "_register_scheduled_task", fail_second)
    with pytest.raises(RuntimeError):
        scheduler_module.sync_project_jobs(core, PROJECT)
    assert sorted(job.id for job in scheduler.get_jobs()) == ["ap_a", "ap_b"]


def test_sync_leaves_startup_one_shots_and_reports_a_withdrawn_project(scheduler, monkeypatch):
    core = _core([_task("ap_a", PROJECT, "0 8 * * *")])
    scheduler.add_job(lambda **_owner: None, DateTrigger(), id="ap_startup",
                      kwargs={scheduler_module.AUTOMATION_JOB_OWNER_MARKER: PROJECT})
    evidence = scheduler_module.sync_project_jobs(core, PROJECT)
    assert evidence["job_ids"] == ["ap_a", "ap_startup"]

    monkeypatch.setattr(scheduler_module, "_automation_project_job_tombstones", {PROJECT})
    withdrawn = scheduler_module.sync_project_jobs(core, PROJECT)
    assert withdrawn["invalid_tasks"][0]["error_code"] == "PROJECT_SCHEDULER_WITHDRAWN"


def test_sync_without_a_live_scheduler_reports_uninitialized(monkeypatch):
    monkeypatch.setattr(scheduler_module, "_scheduler", None)
    assert scheduler_module.sync_project_jobs(_core([]), PROJECT)["initialized"] is False


def test_driver_uses_project_sync_unless_a_projection_switch_is_pending():
    from agent.automation_plugins.production import ProductionRuntimeEffectDriver

    driver = ProductionRuntimeEffectDriver(broker_handler_keys=())
    synced, refreshed = [], []
    driver.bind_scheduler_project_syncer(lambda automation_id: synced.append(automation_id) or {"initialized": True})
    driver.bind_scheduler_projection_refresher(
        lambda: refreshed.append(True) or {"initialized": True, "invalid_tasks": []}
    )
    assert driver.refresh_project_schedule(PROJECT) == {"initialized": True}
    assert synced == [PROJECT] and refreshed == []

    full = []
    driver.refresh_contribution_projection = lambda: full.append(True) or {"initialized": True}
    driver._pending_projection_transitions[(OTHER, 2)] = ("activate", ())
    driver.refresh_project_schedule(PROJECT)
    assert synced == [PROJECT] and full == [True], "a pending switch must take the full refresh"
