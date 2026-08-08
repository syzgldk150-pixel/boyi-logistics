from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path

from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES

HAS_APSCHEDULER = importlib.util.find_spec("apscheduler") is not None
if HAS_APSCHEDULER:
    from agent.scheduler import ensure_finance_schedule_task, init_scheduler
else:
    ensure_finance_schedule_task = None
    init_scheduler = None


class _Memory:
    def _conn(self):
        raise RuntimeError("fixture database unavailable")


class _AgentCore:
    def __init__(self):
        self.memory = _Memory()
        self.calls = []

    async def execute_tool(self, tool_name, params):
        self.calls.append((tool_name, params))
        return {"success": True}


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


class FinanceSchedulerRegistrationTests(unittest.TestCase):
    def test_enabled_daily_template_runs_at_0010(self):
        templates = {
            item["id"]: item for item in PHASE7_SCHEDULED_TASK_TEMPLATES
        }
        task = templates["finance_bills_0010"]
        self.assertTrue(task["enabled"])
        self.assertEqual("sync_finance_bills", task["tool_name"])
        self.assertEqual("10 0 * * *", task["cron_expression"])
        self.assertEqual({"mode": "sync", "rescan_days": 7}, task["tool_params"])

    def test_startup_scheduler_registers_gap_only_catchup(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        core = _AgentCore()
        scheduler = init_scheduler(core)
        job = scheduler.get_job("finance_startup_catchup")
        self.assertIsNotNone(job)
        asyncio.run(job.func())
        self.assertEqual(
            [
                (
                    "sync_finance_bills",
                    {"mode": "sync", "rescan_days": 7, "_startup_catchup": True},
                )
            ],
            core.calls,
        )

    def test_startup_seeds_only_missing_finance_schedule(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        core = _SeedCore()

        self.assertTrue(ensure_finance_schedule_task(core))
        self.assertEqual(1, len(core.memory.upserts))
        self.assertEqual("finance_bills_0010", core.memory.upserts[0]["id"])
        self.assertEqual("10 0 * * *", core.memory.upserts[0]["cron_expression"])
        self.assertTrue(core.memory.upserts[0]["enabled"])

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

    def test_registry_exposes_fixed_finance_tool_parameters(self):
        registry = (
            Path(__file__).resolve().parents[1] / "tools" / "registry.yaml"
        ).read_text(encoding="utf-8")
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

    def test_daily_job_overrides_stale_configured_target_with_frozen_fire_date(self):
        source = (
            Path(__file__).resolve().parents[1] / "agent" / "scheduler.py"
        ).read_text(encoding="utf-8")
        self.assertIn('scheduled_params["target_date"] = target_date', source)
        self.assertNotIn('scheduled_params.setdefault("target_date", target_date)', source)

    def test_ecs_publish_scope_includes_console_finance_service(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "publish_to_ecs.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '@{ Type = "file"; Local = "finance_service.py"; Remote = "finance_service.py" }',
            script,
        )
        self.assertIn('@{ Type = "dir"; Local = "../shared"; Remote = "../shared" }', script)


if __name__ == "__main__":
    unittest.main()
