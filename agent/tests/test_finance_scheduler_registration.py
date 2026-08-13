from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path

from agent.orchestration.models import ActorType
from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES

HAS_APSCHEDULER = importlib.util.find_spec("apscheduler") is not None
if HAS_APSCHEDULER:
    from agent.scheduler import (
        ensure_control_plane_schedule_tasks,
        ensure_finance_schedule_task,
        init_scheduler,
    )
else:
    ensure_control_plane_schedule_tasks = None
    ensure_finance_schedule_task = None
    init_scheduler = None


class _Memory:
    def _conn(self):
        raise RuntimeError("fixture database unavailable")


class _AgentCore:
    def __init__(self):
        self.memory = _Memory()
        self.calls = []

    async def execute_tool(self, tool_name, params, **trusted_context):
        self.calls.append((tool_name, params, trusted_context))
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
        self.assertEqual(1, len(core.calls))
        tool_name, arguments, trusted = core.calls[0]
        self.assertEqual("sync_finance_bills", tool_name)
        self.assertEqual({"mode": "sync", "rescan_days": 7}, arguments)
        self.assertEqual(ActorType.SCHEDULER, trusted["actor"].actor_type)
        self.assertEqual("finance_startup_catchup", trusted["actor"].actor_id)
        self.assertEqual("scheduler", trusted["source"])
        scheduled_for = trusted["execution_context"]["scheduled_for"]
        self.assertEqual(
            f"scheduler:finance_startup_catchup:{scheduled_for}",
            trusted["idempotency_key"],
        )
        self.assertEqual("@startup", trusted["execution_context"]["cron_expression"])

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

    def test_startup_seeds_customer_shadow_but_preserves_disabled_override(self):
        if not HAS_APSCHEDULER:
            self.skipTest("apscheduler is not installed in the unit-test interpreter")
        empty = _SeedCore()

        seeded = ensure_control_plane_schedule_tasks(empty)

        self.assertIn("customer_problems_shadow", seeded)
        customer = next(
            row for row in empty.memory.rows if row["id"] == "customer_problems_shadow"
        )
        self.assertTrue(customer["enabled"])
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
        self.assertEqual((), ensure_control_plane_schedule_tasks(disabled))
        self.assertEqual([], disabled.memory.upserts)
        self.assertFalse(disabled.memory.rows[0]["enabled"])

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
        self.assertIn('arguments["target_date"] = (', source)
        self.assertIn("scheduled_for.date() - timedelta(days=1)", source)
        self.assertNotIn('arguments.setdefault("target_date"', source)
        self.assertIn('idempotency_key=f"scheduler:{task_id}:{scheduled_iso}"', source)

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
