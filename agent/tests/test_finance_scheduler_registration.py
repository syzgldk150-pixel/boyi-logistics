from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path

import yaml

from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES
from shared.finance.sources import (
    FINANCE_SOURCE_SPECS,
    enabled_finance_account_ids,
    enabled_finance_platforms,
)

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
                    {
                        "mode": "sync",
                        "rescan_days": 7,
                        "_startup_catchup": True,
                        "platform": "ronghui",
                    },
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
        properties = finance_tool["parameters"]["properties"]
        self.assertNotIn("enum", properties["platform"])
        self.assertNotIn("enum", properties["account_id"])
        self.assertIn("共享财务来源注册表", properties["platform"]["description"])
        self.assertIn("共享财务来源注册表", properties["account_id"]["description"])

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

    def test_legacy_excel_finance_pipeline_is_fully_removed(self):
        agent_root = Path(__file__).resolve().parents[1]
        repository_root = agent_root.parent
        registry = (agent_root / "tools" / "registry.yaml").read_text(encoding="utf-8")
        publish_script = (agent_root / "deploy" / "publish_to_ecs.ps1").read_text(encoding="utf-8")
        boundary_check = (agent_root / "scripts" / "check_runtime_import_boundaries.py").read_text(encoding="utf-8")
        tool_prompt = (agent_root / "prompts" / "tool_selection.md").read_text(encoding="utf-8")

        self.assertFalse(
            any(path.is_file() for path in (agent_root / "finance_reconciliation").rglob("*"))
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
        self.assertIn('scheduled_params["target_date"] = target_date', source)
        self.assertNotIn('scheduled_params.setdefault("target_date", target_date)', source)

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
