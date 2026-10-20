"""A scheduled collection skipped by a release runs once after activation."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from agent import scheduler


class _Cursor:
    def __init__(self, rows):
        self.rows, self.sql, self.args = rows, "", ()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, args):
        self.sql, self.args = sql, args

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.cursor_object, self.closed = _Cursor(rows), False

    def cursor(self):
        return self.cursor_object

    def close(self):
        self.closed = True


def _utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def _missed(rows, now):
    connection = _Connection(rows)
    result = scheduler.release_missed_scheduled_task_ids(lambda: connection, now=_utc(now))
    assert connection.closed
    assert connection.cursor_object.args == ("sync_customer_service_problems_v2",)
    return result


def test_fire_time_inside_the_release_is_caught_up_once_per_project():
    # 22:30 Shanghai run, release spans the 22:45 fire, activation at 22:52.
    rows = [
        {"id": "ap_a", "automation_id": "p1", "cron_expression": "*/15 * * * *",
         "last_started": datetime(2026, 10, 20, 14, 30, 3)},
        {"id": "ap_b", "automation_id": "p1", "cron_expression": "*/15 * * * *",
         "last_started": datetime(2026, 10, 20, 14, 30, 3)},
    ]
    assert _missed(rows, "2026-10-20T14:52:00") == ("ap_a",)


def test_night_release_after_the_last_daytime_run_does_not_collect():
    rows = [{"id": "ap_a", "automation_id": "p1", "cron_expression": "*/15 9-20 * * *",
             "last_started": datetime(2026, 10, 20, 12, 45, 3)}]
    # 20:45 Shanghai was the final fire; activation at 01:10.
    assert _missed(rows, "2026-10-20T17:10:00") == ()


def test_no_missed_fire_and_never_run_projects_are_left_to_the_schedule():
    rows = [
        {"id": "ap_a", "automation_id": "p1", "cron_expression": "*/15 * * * *",
         "last_started": datetime(2026, 10, 20, 14, 45, 3)},
        {"id": "ap_b", "automation_id": "p2", "cron_expression": "*/15 * * * *", "last_started": None},
    ]
    assert _missed(rows, "2026-10-20T14:52:00") == ()


def test_catch_up_fires_existing_jobs_through_the_scheduler(monkeypatch):
    modified = []

    class Job:
        trigger = SimpleNamespace(timezone=timezone.utc)

        def modify(self, **changes):
            modified.append(changes["next_run_time"])

    fake = SimpleNamespace(state=scheduler.STATE_RUNNING,
                           get_job=lambda task_id: Job() if task_id == "ap_a" else None)
    monkeypatch.setattr(scheduler, "_scheduler", fake)
    assert scheduler.run_scheduled_tasks_now(("ap_a", "ap_missing")) == ("ap_a",)
    assert len(modified) == 1
    monkeypatch.setattr(scheduler, "_scheduler", SimpleNamespace(state=scheduler.STATE_PAUSED))
    assert scheduler.run_scheduled_tasks_now(("ap_a",)) == ()
