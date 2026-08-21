from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from agent.orchestration.models import ActorType
from agent.task_templates import (
    GOVERNED_SCHEDULED_TASK_IDS,
    GOVERNED_SCHEDULED_TASK_TEMPLATES,
    PHASE7_SCHEDULED_TASK_TEMPLATES,
)
from shared.finance.sources import (
    FINANCE_SOURCE_SPECS,
    enabled_finance_account_ids,
    enabled_finance_platforms,
)
from shared.automation_project_manifest import FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES

HAS_APSCHEDULER = importlib.util.find_spec("apscheduler") is not None
if HAS_APSCHEDULER:
    from agent.scheduler import (
        ensure_control_plane_schedule_tasks,
        ensure_finance_schedule_task,
        init_scheduler,
        seed_phase7_schedule_tasks,
    )
else:
    ensure_control_plane_schedule_tasks = None
    ensure_finance_schedule_task = None
    init_scheduler = None
    seed_phase7_schedule_tasks = None


class _Memory:
    def __init__(self):
        self.rows = [
            {
                "id": "finance_bills_0010",
                "name": "Finance bills",
                "tool_name": "sync_finance_bills",
                "tool_params": {"mode": "sync", "platform": "ronghui", "rescan_days": 7},
                "cron_expression": "10 0 * * *",
                "enabled": True,
                "configuration_version": 7,
            },
            {
                "id": "finance_startup_catchup",
                "name": "Finance startup catch-up",
                "tool_name": "sync_finance_bills",
                "tool_params": {
                    "mode": "sync",
                    "platform": "ronghui",
                    "rescan_days": 7,
                    "_startup_catchup": True,
                },
                "cron_expression": "@startup",
                "enabled": True,
                "configuration_version": 8,
            },
        ]

    def _conn(self):
        raise RuntimeError("fixture database unavailable")

    def list_scheduled_tasks(self):
        return list(self.rows)

    def list_enabled_scheduled_tasks(self):
        return [dict(row) for row in self.rows if row.get("enabled")]

    def upsert_scheduled_task(self, task):
        self.rows.append(dict(task))

    def update_scheduled_task_runtime(self, *_args, **_kwargs):
        return None


class _AgentCore:
    def __init__(self):
        self.memory = _Memory()
        self.calls = []

    async def execute_tool(self, tool_name, params, **trusted_context):
        self.calls.append((tool_name, params, trusted_context))
        return {"success": True}


class _ProjectInvoker:
    def __init__(self):
        self.calls = []

    async def invoke_trusted_and_wait(self, automation_id, **kwargs):
        self.calls.append((automation_id, kwargs))
        return {"success": True, "status": "COMPLETED", "run_id": "run-project"}


class _SeedMemory:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.upserts = []

    def list_scheduled_tasks(self):
        return list(self.rows)

    def upsert_scheduled_task(self, task):
        self.upserts.append(task)
        self.rows.append(dict(task))


class _SeedCore:
    def __init__(self, rows=None):
        self.memory = _SeedMemory(rows)


class _ReleaseStartupJob:
    def __init__(self):
        self.next_run_time = None
        self.modify_calls = 0

    def modify(self, *, next_run_time):
        self.modify_calls += 1
        self.next_run_time = next_run_time


class _ReleaseScheduler:
    def __init__(self, state, startup_job=None):
        self.state = state
        self.startup_job = startup_job
        self.resume_calls = 0
        self.pause_calls = 0

    def get_jobs(self):
        return [self.startup_job] if self.startup_job is not None else []

    def get_job(self, job_id):
        if job_id == "finance_startup_catchup":
            return self.startup_job
        return None

    def resume(self):
        from apscheduler.schedulers.base import STATE_RUNNING

        self.resume_calls += 1
        self.state = STATE_RUNNING

    def pause(self):
        from apscheduler.schedulers.base import STATE_PAUSED

        self.pause_calls += 1
        self.state = STATE_PAUSED


class FinanceSchedulerRegistrationTests(unittest.TestCase):
    def test_release_activation_never_reschedules_startup_catchup(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module
        from apscheduler.schedulers.base import STATE_PAUSED

        release_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temp_root:
            marker = Path(temp_root) / "scheduler-release.pause"
            marker.write_text(release_sha + "\n", encoding="utf-8")
            startup_job = _ReleaseStartupJob()
            scheduler = _ReleaseScheduler(STATE_PAUSED, startup_job)
            previous_scheduler = scheduler_module._scheduler
            scheduler_module._scheduler = scheduler
            try:
                with patch.dict(
                    os.environ,
                    {scheduler_module.SCHEDULER_RELEASE_HOLD_ENV: str(marker)},
                ):
                    self.assertTrue(scheduler_module.scheduler_release_hold_requested())
                    resumed = scheduler_module.begin_scheduler_release_activation(release_sha)
                    self.assertTrue(marker.exists())
                    self.assertEqual("running", resumed["state"])
                    self.assertTrue(resumed["release_hold"])
                    status = scheduler_module.consume_scheduler_release_hold(release_sha)
                    self.assertFalse(marker.exists())
            finally:
                scheduler_module._scheduler = previous_scheduler

        self.assertEqual("running", status["state"])
        self.assertFalse(status["release_hold"])
        self.assertEqual(1, scheduler.resume_calls)
        self.assertEqual(0, startup_job.modify_calls)
        self.assertIsNone(startup_job.next_run_time)

    def test_release_activation_rejects_a_marker_for_another_release(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module
        from apscheduler.schedulers.base import STATE_PAUSED

        with tempfile.TemporaryDirectory() as temp_root:
            marker = Path(temp_root) / "scheduler-release.pause"
            marker.write_text("b" * 40 + "\n", encoding="utf-8")
            scheduler = _ReleaseScheduler(STATE_PAUSED)
            previous_scheduler = scheduler_module._scheduler
            scheduler_module._scheduler = scheduler
            try:
                with patch.dict(
                    os.environ,
                    {scheduler_module.SCHEDULER_RELEASE_HOLD_ENV: str(marker)},
                ):
                    with self.assertRaisesRegex(RuntimeError, "does not match"):
                        scheduler_module.begin_scheduler_release_activation("a" * 40)
            finally:
                scheduler_module._scheduler = previous_scheduler

            self.assertTrue(marker.exists())
            self.assertEqual(0, scheduler.resume_calls)

    def test_release_activation_rejects_paused_scheduler_without_hold(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module
        from apscheduler.schedulers.base import STATE_PAUSED

        with tempfile.TemporaryDirectory() as temp_root:
            marker = Path(temp_root) / "scheduler-release.pause"
            scheduler = _ReleaseScheduler(STATE_PAUSED)
            previous_scheduler = scheduler_module._scheduler
            scheduler_module._scheduler = scheduler
            try:
                with patch.dict(
                    os.environ,
                    {scheduler_module.SCHEDULER_RELEASE_HOLD_ENV: str(marker)},
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "paused without the active release hold",
                    ):
                        scheduler_module.begin_scheduler_release_activation("a" * 40)
            finally:
                scheduler_module._scheduler = previous_scheduler

            self.assertEqual(0, scheduler.resume_calls)

    def test_release_activation_retry_finishes_after_scheduler_resumed(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module
        from apscheduler.schedulers.base import STATE_RUNNING

        release_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temp_root:
            marker = Path(temp_root) / "scheduler-release.pause"
            marker.write_text(release_sha + "\n", encoding="utf-8")
            scheduler = _ReleaseScheduler(STATE_RUNNING)
            previous_scheduler = scheduler_module._scheduler
            scheduler_module._scheduler = scheduler
            try:
                with patch.dict(
                    os.environ,
                    {scheduler_module.SCHEDULER_RELEASE_HOLD_ENV: str(marker)},
                ):
                    resumed = scheduler_module.begin_scheduler_release_activation(release_sha)
                    self.assertEqual("running", resumed["state"])
                    self.assertTrue(resumed["release_hold"])
                    status = scheduler_module.consume_scheduler_release_hold(release_sha)
            finally:
                scheduler_module._scheduler = previous_scheduler

        self.assertFalse(marker.exists())
        self.assertEqual("running", status["state"])
        self.assertFalse(status["release_hold"])
        self.assertEqual(0, scheduler.resume_calls)

    def test_release_hold_remains_when_final_marker_consumption_fails(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module
        from apscheduler.schedulers.base import STATE_RUNNING

        release_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temp_root:
            marker = Path(temp_root) / "scheduler-release.pause"
            marker.write_text(release_sha + "\n", encoding="utf-8")
            scheduler = _ReleaseScheduler(STATE_RUNNING)
            previous_scheduler = scheduler_module._scheduler
            scheduler_module._scheduler = scheduler
            try:
                with patch.dict(
                    os.environ,
                    {scheduler_module.SCHEDULER_RELEASE_HOLD_ENV: str(marker)},
                ), patch.object(Path, "unlink", side_effect=OSError("busy")):
                    with self.assertRaisesRegex(RuntimeError, "could not be consumed"):
                        scheduler_module.consume_scheduler_release_hold(release_sha)
            finally:
                scheduler_module._scheduler = previous_scheduler

            self.assertTrue(marker.exists())

    def test_new_finance_template_is_disabled_at_0010(self):
        templates = {
            item["id"]: item for item in PHASE7_SCHEDULED_TASK_TEMPLATES
        }
        task = templates["finance_bills_0010"]
        self.assertFalse(task["enabled"])
        self.assertEqual("sync_finance_bills", task["tool_name"])
        self.assertEqual("10 0 * * *", task["cron_expression"])
        self.assertEqual(
            {"mode": "sync", "platform": "ronghui", "rescan_days": 7},
            task["tool_params"],
        )

    def test_held_scheduler_never_registers_or_runs_startup_catchup(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        async def exercise_held_startup(marker: Path, release_sha: str) -> None:
            core = _AgentCore()
            scheduler = init_scheduler(core, include_startup_catchup=False)
            self.assertIsNone(scheduler.get_job("finance_startup_catchup"))

            reloaded = scheduler_module.reload_scheduler(core)
            self.assertNotIn("finance_startup_catchup", reloaded["job_ids"])
            self.assertIsNone(scheduler.get_job("finance_startup_catchup"))

            scheduler.start(paused=True)
            try:
                resumed = scheduler_module.begin_scheduler_release_activation(release_sha)
                self.assertEqual("running", resumed["state"])
                self.assertTrue(marker.exists())
                await asyncio.sleep(0)
                self.assertIsNone(scheduler.get_job("finance_startup_catchup"))
                self.assertEqual([], core.calls)

                scheduler_module.consume_scheduler_release_hold(release_sha)
                await asyncio.sleep(0)
                self.assertFalse(marker.exists())
                self.assertIsNone(scheduler.get_job("finance_startup_catchup"))
                self.assertEqual([], core.calls)
            finally:
                scheduler.shutdown(wait=False)

        release_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temp_root:
            marker = Path(temp_root) / "scheduler-release.pause"
            marker.write_text(release_sha + "\n", encoding="utf-8")
            previous_scheduler = scheduler_module._scheduler
            previous_include = scheduler_module._include_startup_catchup_for_process
            try:
                with patch.dict(
                    os.environ,
                    {scheduler_module.SCHEDULER_RELEASE_HOLD_ENV: str(marker)},
                ):
                    asyncio.run(exercise_held_startup(marker, release_sha))
            finally:
                scheduler_module._scheduler = previous_scheduler
                scheduler_module._include_startup_catchup_for_process = previous_include

    def test_unheld_startup_scheduler_registers_gap_only_catchup(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        core = _AgentCore()
        scheduler = init_scheduler(core, include_startup_catchup=True)
        job = scheduler.get_job("finance_startup_catchup")
        self.assertIsNotNone(job)
        asyncio.run(job.func())
        self.assertEqual(1, len(core.calls))
        tool_name, arguments, trusted = core.calls[0]
        self.assertEqual("sync_finance_bills", tool_name)
        self.assertEqual(
            {
                "mode": "sync",
                "rescan_days": 7,
                "_startup_catchup": True,
                "platform": "ronghui",
            },
            arguments,
        )
        self.assertEqual(ActorType.SCHEDULER, trusted["actor"].actor_type)
        self.assertEqual("finance_startup_catchup", trusted["actor"].actor_id)
        self.assertEqual("scheduler", trusted["source"])
        scheduled_for = trusted["execution_context"]["scheduled_for"]
        self.assertEqual(
            f"scheduler:finance_startup_catchup:v8:{scheduled_for}",
            trusted["idempotency_key"],
        )
        self.assertEqual("@startup", trusted["execution_context"]["cron_expression"])
        self.assertEqual(8, trusted["execution_context"]["configuration_version"])

        # A second service start on the same business day must submit the same
        # logical occurrence so CommandGateway reuses the original Run.
        asyncio.run(job.func())
        self.assertEqual(2, len(core.calls))
        self.assertEqual(
            core.calls[0][2]["idempotency_key"],
            core.calls[1][2]["idempotency_key"],
        )

        # A contract revision is a distinct governed occurrence and must not
        # collide with the immutable Command created by an older task version.
        asyncio.run(
            scheduler_module._execute_scheduled_tool(
                core,
                task_id="finance_startup_catchup",
                tool_name="sync_finance_bills",
                arguments={
                    "mode": "sync",
                    "platform": "ronghui",
                    "rescan_days": 7,
                    "_startup_catchup": True,
                },
                scheduled_for=datetime.fromisoformat(scheduled_for),
                cron_expression="@startup",
                configuration_version=9,
            )
        )
        self.assertEqual(
            f"scheduler:finance_startup_catchup:v9:{scheduled_for}",
            core.calls[2][2]["idempotency_key"],
        )

        for invalid_version in (None, 0, True):
            with self.subTest(configuration_version=invalid_version):
                with self.assertRaisesRegex(
                    ValueError,
                    "@startup tasks require a positive configuration_version",
                ):
                    asyncio.run(
                        scheduler_module._execute_scheduled_tool(
                            core,
                            task_id="finance_startup_catchup",
                            tool_name="sync_finance_bills",
                            arguments={"mode": "sync"},
                            scheduled_for=datetime.fromisoformat(scheduled_for),
                            cron_expression="@startup",
                            configuration_version=invalid_version,
                        )
                    )

    def test_daily_finance_job_freezes_scope_and_submits_through_gateway(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        core = _AgentCore()
        previous_scheduler = scheduler_module._scheduler
        scheduler_module._scheduler = scheduler_module.AsyncIOScheduler(
            timezone="Asia/Shanghai"
        )
        scheduled_for = datetime.fromisoformat("2026-08-13T00:10:00+08:00")
        try:
            with patch(
                "agent.scheduler._latest_scheduled_fire_time",
                return_value=scheduled_for,
            ):
                scheduler_module._add_job(
                    task_id="finance_bills_0010",
                    cron_expr="10 0 * * *",
                    tool_name="sync_finance_bills",
                    tool_params={"mode": "sync", "rescan_days": 7},
                    agent_core=core,
                )
                job = scheduler_module._scheduler.get_job("finance_bills_0010")
                self.assertIsNotNone(job)
                self.assertEqual(1, job.max_instances)
                self.assertTrue(job.coalesce)
                self.assertEqual(3600, job.misfire_grace_time)
                asyncio.run(job.func())
        finally:
            scheduler_module._scheduler = previous_scheduler

        tool_name, arguments, trusted = core.calls[0]
        self.assertEqual("sync_finance_bills", tool_name)
        self.assertEqual(
            {
                "mode": "sync",
                "rescan_days": 7,
                "platform": "ronghui",
                "target_date": "2026-08-12",
            },
            arguments,
        )
        self.assertEqual(
            "scheduler:finance_bills_0010:2026-08-13T00:10:00+08:00",
            trusted["idempotency_key"],
        )

    def test_plugin_schedule_uses_typed_instance_route_without_legacy_arguments(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        core = _AgentCore()
        invoker = _ProjectInvoker()
        scheduled_for = datetime.fromisoformat("2026-08-15T07:00:00+08:00")
        result = asyncio.run(
            scheduler_module._execute_scheduled_tool(
                core,
                task_id="scan_0700",
                tool_name="automation.scan_project.run",
                arguments={"account_id": "must-not-cross-boundary"},
                scheduled_for=scheduled_for,
                cron_expression="0 7 * * *",
                configuration_version=3,
                automation_id="scan_project",
                automation_generation=4,
                automation_project_invoker=invoker,
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual([], core.calls)
        self.assertEqual(1, len(invoker.calls))
        automation_id, trusted = invoker.calls[0]
        self.assertEqual("scan_project", automation_id)
        self.assertNotIn("arguments", trusted)
        self.assertEqual("scheduler", trusted["entrypoint"])
        self.assertEqual(4, trusted["expected_automation_generation"])
        self.assertEqual(3, trusted["expected_project_configuration_version"])
        self.assertEqual("apscheduler", trusted["actor"].authenticated_by)
        self.assertEqual(
            "scheduler:scan_0700:2026-08-15T07:00:00+08:00",
            trusted["idempotency_key"],
        )

    def test_enabled_deferred_r7_migration_rows_register_no_jobs(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module
        from agent.automation_plugins.release_scope import (
            DEFERRED_R7_LEGACY_SCHEDULE_GENERATION,
            DEFERRED_R7_PLUGIN_IDS,
        )

        reviewed_rows = []
        for automation_id in sorted(DEFERRED_R7_PLUGIN_IDS):
            definition = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[automation_id]
            reviewed_rows.extend(
                (
                    task_id,
                    automation_id,
                    definition.tool_name,
                    dict(definition.legacy_arguments),
                )
                for task_id in sorted(definition.scheduled_task_ids)
            )
        self.assertEqual(14, len(reviewed_rows))
        self.assertEqual(
            {task_id for task_id, *_rest in reviewed_rows},
            set(scheduler_module.DEFERRED_R7_LEGACY_SCHEDULE_TASK_IDS),
        )
        for task_id, automation_id, tool_name, _legacy_arguments in reviewed_rows:
            with self.subTest(reviewed_identity=task_id):
                self.assertTrue(
                    scheduler_module._deferred_r7_schedule_must_not_register(
                        {
                            "id": task_id,
                            "tool_name": tool_name,
                            "automation_id": automation_id,
                            "automation_generation": (
                                DEFERRED_R7_LEGACY_SCHEDULE_GENERATION
                            ),
                        }
                    )
                )

        core = _AgentCore()
        core.memory.rows = [
            {
                "id": task_id,
                "name": task_id,
                "tool_name": tool_name,
                "tool_params": legacy_arguments,
                "cron_expression": "0 9 * * *",
                "enabled": automation_id == "r7_arrival_checkin",
                "configuration_version": 1,
                "automation_id": automation_id,
                "automation_generation": DEFERRED_R7_LEGACY_SCHEDULE_GENERATION,
            }
            for task_id, automation_id, tool_name, legacy_arguments in reviewed_rows
        ]
        self.assertEqual(
            13,
            sum(bool(row["enabled"]) for row in core.memory.rows),
        )
        invoker = _ProjectInvoker()
        previous_scheduler = scheduler_module._scheduler
        scheduler_module._scheduler = scheduler_module.AsyncIOScheduler(
            timezone="Asia/Shanghai"
        )
        try:
            with self.assertLogs("agent", level="WARNING") as captured:
                scheduler_module._load_tasks_from_db(
                    core,
                    automation_project_invoker=invoker,
                )
            self.assertEqual(0, len(scheduler_module._scheduler.get_jobs()))
        finally:
            scheduler_module._scheduler = previous_scheduler

        self.assertIn(
            "Deferred R7 scheduled tasks were not registered",
            "\n".join(captured.output),
        )
        self.assertEqual([], core.calls)
        self.assertEqual([], invoker.calls)

    def test_deferred_r7_schedule_drift_blocks_loading_and_startup(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        valid = {
            "id": "r7_arrival_checkin_0900",
            "name": "R7 arrival",
            "tool_name": "r7_arrival_checkin",
            "tool_params": {},
            "cron_expression": "0 9 * * *",
            "enabled": True,
            "configuration_version": 1,
            "automation_id": "r7_arrival_checkin",
            "automation_generation": (
                scheduler_module.DEFERRED_R7_LEGACY_SCHEDULE_GENERATION
            ),
        }
        invalid_overrides = (
            {"id": "r7_arrival_checkin_0915"},
            {"tool_name": "r7_departure_checkin"},
            {"tool_name": "automation.r7_arrival_checkin.run"},
            {
                "tool_name": "automation.scan_codes.run",
                "automation_id": "scan_codes",
                "automation_generation": 4,
            },
            {"automation_id": "r7_departure_checkin"},
            {"automation_id": " r7_arrival_checkin "},
            {"automation_id": None},
            {"automation_generation": 2},
            {"automation_generation": None},
            {"automation_generation": True},
        )
        previous_scheduler = scheduler_module._scheduler
        try:
            for override in invalid_overrides:
                core = _AgentCore()
                core.memory.rows = [{**valid, **override}]
                invoker = _ProjectInvoker()
                scheduler_module._scheduler = scheduler_module.AsyncIOScheduler(
                    timezone="Asia/Shanghai"
                )
                with self.subTest(override=override), self.assertRaisesRegex(
                    scheduler_module.DeferredR7ScheduleIdentityError,
                    "reviewed migration identity",
                ):
                    scheduler_module._load_tasks_from_db(
                        core,
                        automation_project_invoker=invoker,
                    )
                self.assertEqual(0, len(scheduler_module._scheduler.get_jobs()))
                self.assertEqual([], core.calls)
                self.assertEqual([], invoker.calls)

            startup_core = _AgentCore()
            startup_core.memory.rows = [
                {**valid, "automation_generation": 2}
            ]
            with patch(
                "agent.scheduler.ensure_control_plane_schedule_tasks",
                return_value=(),
            ), self.assertRaises(scheduler_module.DeferredR7ScheduleIdentityError):
                scheduler_module.init_scheduler(
                    startup_core,
                    include_startup_catchup=False,
                )
        finally:
            scheduler_module._scheduler = previous_scheduler

    def test_reviewed_unknown_write_quarantines_skip_only_exact_reviewed_rows(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        definition = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES["delivery_status"]
        self.assertEqual(
            set(definition.scheduled_task_ids),
            set(scheduler_module.DELIVERY_STATUS_QUARANTINE_SCHEDULE_TASK_IDS),
        )
        core = _AgentCore()
        core.registry = SimpleNamespace(
            reviewed_unknown_write_quarantine_status=(
                lambda _automation_id: "QUARANTINED_UNKNOWN_WRITE"
            )
        )
        core.memory.rows = []
        for automation_id, task_ids in sorted(
            scheduler_module.REVIEWED_UNKNOWN_WRITE_QUARANTINE_SCHEDULE_TASK_IDS.items()
        ):
            core.memory.rows.extend(
                {
                    "id": task_id,
                    "name": task_id,
                    "tool_name": f"automation.{automation_id}.run",
                    "tool_params": {},
                    "cron_expression": "0 9 * * *",
                    "enabled": True,
                    "configuration_version": 2,
                    "automation_id": automation_id,
                    "automation_generation": 1,
                }
                for task_id in sorted(task_ids)
            )
        previous_scheduler = scheduler_module._scheduler
        scheduler_module._scheduler = scheduler_module.AsyncIOScheduler(
            timezone="Asia/Shanghai"
        )
        try:
            with self.assertLogs("agent", level="WARNING") as captured:
                scheduler_module._load_tasks_from_db(core)
            self.assertEqual(0, len(scheduler_module._scheduler.get_jobs()))
        finally:
            scheduler_module._scheduler = previous_scheduler

        self.assertIn(
            "Audited unknown-write quarantine scheduled tasks were not registered",
            "\n".join(captured.output),
        )

        normal_core = _AgentCore()
        normal_core.registry = SimpleNamespace(
            reviewed_unknown_write_quarantine_status=lambda _automation_id: None
        )
        normal_core.memory.rows = [core.memory.rows[0]]
        previous_scheduler = scheduler_module._scheduler
        scheduler_module._scheduler = scheduler_module.AsyncIOScheduler(
            timezone="Asia/Shanghai"
        )
        try:
            scheduler_module._load_tasks_from_db(normal_core)
            self.assertEqual(1, len(scheduler_module._scheduler.get_jobs()))
        finally:
            scheduler_module._scheduler = previous_scheduler

    def test_delivery_unknown_write_quarantine_drift_blocks_scheduler_loading(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        core = _AgentCore()
        core.registry = SimpleNamespace(
            reviewed_unknown_write_quarantine_status=(
                lambda _automation_id: "QUARANTINED_UNKNOWN_WRITE"
            )
        )
        core.memory.rows = [
            {
                "id": "delivery_status_0900",
                "name": "Delivery status",
                "tool_name": "automation.delivery_status.run",
                "tool_params": {},
                "cron_expression": "0 9 * * *",
                "enabled": True,
                "configuration_version": 2,
                "automation_id": "delivery_status",
                "automation_generation": 2,
            }
        ]
        previous_scheduler = scheduler_module._scheduler
        scheduler_module._scheduler = scheduler_module.AsyncIOScheduler(
            timezone="Asia/Shanghai"
        )
        try:
            with self.assertRaisesRegex(
                scheduler_module.DeliveryStatusQuarantineIdentityError,
                "audited quarantine identity",
            ):
                scheduler_module._load_tasks_from_db(core)
            self.assertEqual(0, len(scheduler_module._scheduler.get_jobs()))
        finally:
            scheduler_module._scheduler = previous_scheduler

    def test_delivery_unknown_write_topology_failure_never_skips_as_quarantined(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        valid = {
            "id": "delivery_status_0900",
            "name": "Delivery status",
            "tool_name": "automation.delivery_status.run",
            "tool_params": {},
            "cron_expression": "0 9 * * *",
            "enabled": True,
            "configuration_version": 2,
            "automation_id": "delivery_status",
            "automation_generation": 1,
        }
        for drift in ("extra generation", "active lease"):
            core = _AgentCore()

            def reject_topology(_automation_id, current_drift=drift):
                raise RuntimeError(f"delivery quarantine {current_drift} mismatch")

            core.registry = SimpleNamespace(
                reviewed_unknown_write_quarantine_status=reject_topology
            )
            core.memory.rows = [valid]
            with self.assertRaisesRegex(RuntimeError, drift):
                scheduler_module._load_tasks_from_db(core)

    def test_plugin_schedule_fails_closed_without_explicit_project_identity(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        core = _AgentCore()
        with self.assertRaisesRegex(RuntimeError, "explicit project identity"):
            asyncio.run(
                scheduler_module._execute_scheduled_tool(
                    core,
                    task_id="scan_0700",
                    tool_name="automation.scan_project.run",
                    arguments={},
                    scheduled_for=datetime.fromisoformat(
                        "2026-08-15T07:00:00+08:00"
                    ),
                    cron_expression="0 7 * * *",
                    configuration_version=3,
                )
            )
        self.assertEqual([], core.calls)

    def test_startup_seeds_only_missing_finance_schedule(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        core = _SeedCore()

        self.assertTrue(ensure_finance_schedule_task(core))
        self.assertEqual(1, len(core.memory.upserts))
        self.assertEqual("finance_bills_0010", core.memory.upserts[0]["id"])
        self.assertEqual("10 0 * * *", core.memory.upserts[0]["cron_expression"])
        self.assertFalse(core.memory.upserts[0]["enabled"])

    def test_startup_preserves_existing_finance_schedule_override(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        existing = {
            "id": "finance_bills_0010",
            "cron_expression": "10 0 * * *",
            "enabled": False,
        }
        core = _SeedCore([existing])

        self.assertFalse(ensure_finance_schedule_task(core))
        self.assertEqual([], core.memory.upserts)
        self.assertFalse(core.memory.rows[0]["enabled"])

    def test_startup_seeds_customer_shadow_but_preserves_disabled_override(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        empty = _SeedCore()

        seeded = ensure_control_plane_schedule_tasks(empty)

        self.assertIn("customer_problems_shadow", seeded)
        customer = next(
            row for row in empty.memory.rows if row["id"] == "customer_problems_shadow"
        )
        self.assertFalse(customer["enabled"])
        self.assertEqual("sync_customer_service_problems", customer["tool_name"])
        self.assertEqual({"direction": "both"}, customer["tool_params"])

        disabled = _SeedCore(
            [
                {
                    "id": "customer_problems_shadow",
                    "cron_expression": "*/30 * * * *",
                    "enabled": False,
                },
                {
                    "id": "finance_bills_0010",
                    "cron_expression": "10 0 * * *",
                    "enabled": False,
                },
            ]
        )
        seeded = ensure_control_plane_schedule_tasks(disabled)
        self.assertNotIn("customer_problems_shadow", seeded)
        self.assertNotIn("finance_bills_0010", seeded)
        self.assertEqual(
            len(PHASE7_SCHEDULED_TASK_TEMPLATES) - 2,
            len(disabled.memory.upserts),
        )
        self.assertFalse(disabled.memory.rows[0]["enabled"])
        self.assertFalse(disabled.memory.rows[1]["enabled"])
        self.assertTrue(
            all(row["enabled"] is False for row in disabled.memory.upserts)
        )

    def test_governed_seed_templates_exactly_match_policy_ids_and_arguments(self):
        from shared.scheduled_task_contracts import APPROVED_SCHEDULED_TASK_PROFILES

        all_approved_ids = frozenset(
            task_id
            for profile in APPROVED_SCHEDULED_TASK_PROFILES.values()
            for task_id in profile.approved_task_ids
        )
        internal_approved_ids = frozenset(
            task_id
            for profile in APPROVED_SCHEDULED_TASK_PROFILES.values()
            if profile.operation_type == "internal_projection_write"
            for task_id in profile.approved_task_ids
        )
        self.assertEqual(69, len(all_approved_ids))
        self.assertEqual(54, len(internal_approved_ids))
        self.assertEqual(
            internal_approved_ids
            - {
                "finance_bills_0010",
                "finance_startup_catchup",
                "yunda_dispatch_forecast_1700",
            },
            GOVERNED_SCHEDULED_TASK_IDS,
        )
        self.assertEqual(51, len(GOVERNED_SCHEDULED_TASK_TEMPLATES))
        self.assertIn("clockin_daxiang_1830", all_approved_ids)
        self.assertIn("clockin_daxiang_s_1833", all_approved_ids)

        templates = {
            task["id"]: task for task in GOVERNED_SCHEDULED_TASK_TEMPLATES
        }
        self.assertEqual(
            {"account_id": "price_default"},
            templates["send_order_2359"]["tool_params"],
        )
        self.assertEqual(
            "59 23 * * *", templates["send_order_2359"]["cron_expression"]
        )
        self.assertEqual(
            {
                "r13_account_id": "r13_default",
                "account_id": "ronghui_daxiang_s",
                "days": 7,
            },
            templates["daily_sign_0500"]["tool_params"],
        )
        self.assertNotIn("send_order_2150", templates)
        self.assertNotIn("clockin_daxiang_1830", templates)
        self.assertNotIn("clockin_daxiang_s_1833", templates)
        self.assertFalse(any(task_id.startswith("r7_arrival_checkin_") for task_id in templates))

    def test_every_new_seed_row_is_disabled_and_external_writes_are_absent(self):
        templates = {
            task["id"]: task for task in PHASE7_SCHEDULED_TASK_TEMPLATES
        }
        self.assertEqual(len(templates), len(PHASE7_SCHEDULED_TASK_TEMPLATES))
        self.assertTrue(all(task["enabled"] is False for task in templates.values()))
        self.assertFalse(any(task_id.startswith("clockin_") for task_id in templates))
        self.assertNotIn("r7_arrival_checkin", templates)
        self.assertNotIn("r7_departure_checkin", templates)
        self.assertFalse(templates["finance_bills_0010"]["enabled"])
        self.assertFalse(templates["yunda_dispatch_forecast_1700"]["enabled"])

    def test_manual_seed_inserts_only_missing_rows_and_preserves_admin_switch(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        existing = {
            "id": "send_order_2359",
            "cron_expression": "59 23 * * *",
            "enabled": True,
            "tool_params": {"administrator_owned": True},
        }
        core = _SeedCore([existing])

        seeded = seed_phase7_schedule_tasks(core)

        self.assertNotIn("send_order_2359", seeded)
        self.assertEqual(len(PHASE7_SCHEDULED_TASK_TEMPLATES) - 1, len(seeded))
        self.assertTrue(core.memory.rows[0]["enabled"])
        self.assertEqual(
            {"administrator_owned": True},
            core.memory.rows[0]["tool_params"],
        )
        self.assertEqual((), seed_phase7_schedule_tasks(core))

    def test_disabled_daily_finance_does_not_suppress_enabled_startup_catchup(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        core = _SeedCore(
            [
                {
                    "id": "finance_bills_0010",
                    "enabled": False,
                },
                {
                    "id": "finance_startup_catchup",
                    "tool_name": "sync_finance_bills",
                    "tool_params": {
                        "mode": "sync",
                        "platform": "ronghui",
                        "rescan_days": 7,
                        "_startup_catchup": True,
                    },
                    "cron_expression": "@startup",
                    "enabled": True,
                },
            ]
        )
        previous_scheduler = scheduler_module._scheduler
        scheduler_module._scheduler = scheduler_module.AsyncIOScheduler(
            timezone="Asia/Shanghai"
        )
        try:
            scheduler_module._add_finance_startup_catchup_job(core)
            self.assertIsNotNone(
                scheduler_module._scheduler.get_job("finance_startup_catchup")
            )
        finally:
            scheduler_module._scheduler = previous_scheduler

    def test_disabled_startup_task_suppresses_catchup_even_when_daily_is_enabled(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        import agent.scheduler as scheduler_module

        core = _SeedCore(
            [
                {"id": "finance_bills_0010", "enabled": True},
                {
                    "id": "finance_startup_catchup",
                    "tool_name": "sync_finance_bills",
                    "cron_expression": "@startup",
                    "enabled": False,
                },
            ]
        )
        previous_scheduler = scheduler_module._scheduler
        scheduler_module._scheduler = scheduler_module.AsyncIOScheduler(
            timezone="Asia/Shanghai"
        )
        try:
            scheduler_module._add_finance_startup_catchup_job(core)
            self.assertIsNone(
                scheduler_module._scheduler.get_job("finance_startup_catchup")
            )
        finally:
            scheduler_module._scheduler = previous_scheduler

    def test_registry_exposes_runtime_validated_finance_tool_parameters(self):
        registry_path = (
            Path(__file__).resolve().parents[1] / "tools" / "registry.yaml"
        )
        registry = registry_path.read_text(encoding="utf-8")
        self.assertIn("name: sync_finance_bills", registry)
        for field in (
            "mode:",
            "target_date:",
            "start_date:",
            "end_date:",
            "platform:",
            "account_id:",
            "batch_id:",
            "rescan_days:",
        ):
            self.assertIn(field, registry)

        manifest = yaml.safe_load(registry)
        finance_tool = next(
            tool for tool in manifest["tools"] if tool["name"] == "sync_finance_bills"
        )
        properties = finance_tool["input_schema"]["properties"]
        self.assertEqual(list(enabled_finance_platforms()), properties["platform"]["enum"])
        self.assertEqual("string", properties["account_id"]["type"])
        self.assertNotIn("enum", properties["account_id"])
        self.assertEqual([], finance_tool["input_schema"]["required"])
        self.assertEqual("boolean", properties["_startup_catchup"]["type"])

    def test_finance_source_contract_keeps_yunda_declared_but_not_enabled(self):
        self.assertEqual(("ronghui",), enabled_finance_platforms())
        self.assertEqual(
            (
                "price_default",
                "ronghui_daxiang_s",
                "ronghui_self_pickup_problem",
            ),
            enabled_finance_account_ids(),
        )
        yunda = next(spec for spec in FINANCE_SOURCE_SPECS if spec.platform == "yunda")
        self.assertFalse(yunda.production_ready)
        self.assertEqual("not_launched", yunda.status)

    def test_main_context_builder_excludes_not_launched_finance_sources(self):
        from agent.orchestration.models import Actor, ActorType, Command
        from main import _resolve_command_accounts

        rows = [
            {
                "system": "ronghui",
                "account_id": "price_default",
                "account_purpose": "finance",
                "is_active": True,
            },
            {
                "system": "yunda",
                "account_id": "yunda_default",
                "account_purpose": "finance",
                "is_active": True,
            },
        ]
        command = Command(
            command_type="tool.execute",
            source="scheduler",
            actor=Actor(ActorType.SCHEDULER, "finance_bills_0010", roles=("system",)),
            parameters={
                "tool_name": "sync_finance_bills",
                "arguments": {"mode": "sync"},
            },
            idempotency_key="scheduler:finance_bills_0010:fixture",
        )
        with patch("main.get_account_manager") as account_manager_factory:
            account_manager_factory.return_value.list_accounts.return_value = rows
            resolved = _resolve_command_accounts(command)

        self.assertEqual(["price_default"], [row["account_id"] for row in resolved])

    def test_completed_finance_run_projects_durable_brain_event(self):
        from main import _project_run_completed_event

        captured = {}

        class _Runs:
            @staticmethod
            def get(_run_id, *, for_update=False):
                self.assertFalse(for_update)
                return {
                    "run_id": "00000000-0000-0000-0000-000000000001",
                    "work_item_id": "00000000-0000-0000-0000-000000000002",
                    "correlation_id": "00000000-0000-0000-0000-000000000003",
                    "status": "COMPLETED",
                    "plan_json": {
                        "steps": [{"tool_name": "sync_finance_bills"}],
                    },
                }

        class _Events:
            @staticmethod
            def append_with_outbox(event, outbox):
                captured["event"] = event
                captured["outbox"] = tuple(outbox)
                return {"event": {"event_id": event["event_id"]}}

        delivery = {
            "event_id": "00000000-0000-0000-0000-000000000004",
            "event_type": "agent.run.status_changed",
            "run_id": "00000000-0000-0000-0000-000000000001",
            "correlation_id": "00000000-0000-0000-0000-000000000003",
            "payload_json": {"from": "VERIFYING", "to": "COMPLETED"},
        }

        result = _project_run_completed_event(
            delivery,
            SimpleNamespace(runs=_Runs(), events=_Events()),
        )

        self.assertTrue(result["projected"])
        self.assertEqual("agent.run.completed", captured["event"]["event_type"])
        self.assertEqual(
            ["sync_finance_bills"],
            captured["event"]["payload"]["tool_names"],
        )
        self.assertEqual("finance.brain", captured["outbox"][0]["consumer_name"])

    def test_finance_brain_consumer_ignores_non_finance_run(self):
        from main import _finance_brain_completed_handler

        result = _finance_brain_completed_handler(
            SimpleNamespace(finance_brain=None),
            object(),
            {
                "event_id": "00000000-0000-0000-0000-000000000005",
                "payload_json": {"tool_names": ["sync_daily_sign"]},
            },
            object(),
        )

        self.assertFalse(result["processed"])

    def test_legacy_excel_finance_pipeline_is_fully_removed(self):
        agent_root = Path(__file__).resolve().parents[1]
        repository_root = agent_root.parent
        registry = (agent_root / "tools" / "registry.yaml").read_text(encoding="utf-8")
        publish_script = (agent_root / "deploy" / "publish_to_ecs.ps1").read_text(encoding="utf-8")
        boundary_check = (agent_root / "scripts" / "check_runtime_import_boundaries.py").read_text(encoding="utf-8")
        tool_prompt = (agent_root / "prompts" / "tool_selection.md").read_text(encoding="utf-8")

        self.assertFalse(
            any(
                path.is_file() and path.suffix != ".pyc"
                for path in (agent_root / "finance_reconciliation").rglob("*")
            )
        )
        self.assertFalse(
            any(path.is_file() for path in (agent_root / "docs" / "finance_reconciliation").rglob("*"))
        )
        self.assertFalse((agent_root / "tools" / "finance_tool.py").exists())
        self.assertNotIn("name: finance_etl", registry)
        self.assertNotIn("tools/finance_tool.py", registry)
        self.assertNotIn('"finance_reconciliation"', publish_script)
        self.assertNotIn('"finance_tool.py"', boundary_check)
        self.assertNotIn("finance_etl", tool_prompt)
        self.assertTrue((repository_root / "shared" / "finance").is_dir())

    def test_daily_job_overrides_stale_configured_target_with_frozen_fire_date(self):
        source = (
            Path(__file__).resolve().parents[1] / "agent" / "scheduler.py"
        ).read_text(encoding="utf-8")
        self.assertIn('arguments["target_date"] = (', source)
        self.assertIn("scheduled_for.date() - timedelta(days=1)", source)
        self.assertNotIn('arguments.setdefault("target_date"', source)
        self.assertIn('idempotency_key = f"scheduler:{task_id}:{scheduled_iso}"', source)
        self.assertIn("idempotency_key=idempotency_key", source)

    def test_ecs_publish_scope_includes_console_finance_service(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "publish_to_ecs.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('"finance_service.py"', script)
        self.assertIn('foreach ($scope in @("agent", "console", "shared", "_manifests"))', script)


if __name__ == "__main__":
    unittest.main()
