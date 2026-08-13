from __future__ import annotations

import asyncio
import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from agent.orchestration.models import ActorType
from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES
from shared.finance.sources import (
    FINANCE_SOURCE_SPECS,
    enabled_finance_account_ids,
    enabled_finance_platforms,
)

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
        self.assertEqual(
            {"mode": "sync", "platform": "ronghui", "rescan_days": 7},
            task["tool_params"],
        )

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
            f"scheduler:finance_startup_catchup:{scheduled_for}",
            trusted["idempotency_key"],
        )
        self.assertEqual("@startup", trusted["execution_context"]["cron_expression"])

        # A second service start on the same business day must submit the same
        # logical occurrence so CommandGateway reuses the original Run.
        asyncio.run(job.func())
        self.assertEqual(2, len(core.calls))
        self.assertEqual(
            core.calls[0][2]["idempotency_key"],
            core.calls[1][2]["idempotency_key"],
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
        self.assertEqual(list(enabled_finance_account_ids()), properties["account_id"]["enum"])
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
