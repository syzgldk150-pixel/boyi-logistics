"""Real scheduled entry repeats a Console-collected source through one publisher."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from uuid import uuid4

from agent.scheduler import _execute_scheduled_tool
from shared.customer_service_repository import CustomerServiceRepository


def exercise_scheduler_entry(*, management, connection_factory, automation_id, source_id, supplier, actor):
    original = management.catalog.require(automation_id)

    def save(entry, schedule, entrypoints):
        return management.management.save_configuration(automation_id,
            config=entry.project_config, account_bindings=entry.account_bindings,
            resource_bindings=entry.resource_bindings, enabled_entrypoints=entrypoints,
            schedule=schedule, device_id=None, request_id=str(uuid4()),
            expected_project_configuration_version=entry.project_config_version, actor=actor)

    def business_rows():
        with connection_factory() as connection:
            result = CustomerServiceRepository(connection).query(source_ids=[source_id])
        keys = ("source_id", "external_id", "source_direction", "waybill_no", "problem_text", "resolved", "note")
        return {"stats": result["stats"], "rows": [{key: row[key] for key in keys} for row in result["rows"]]}

    before = business_rows()
    save(original, {"kind": "daily_times", "times": ["01:23"], "enabled": True},
        tuple(sorted(set(original.current_enabled_entrypoints) | {"scheduler"})))
    try:
        entry = management.catalog.require(automation_id)
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,tool_name,cron_expression,configuration_version,automation_generation FROM scheduled_tasks WHERE automation_id=%s", (automation_id,))
            tasks = cursor.fetchall()
        if len(tasks) != 1:
            raise AssertionError("the actual saved collector schedule did not yield exactly one job")
        task = tasks[0]
        occurrence = datetime(2026, 9, 7, 1, 23, tzinfo=timezone.utc)

        async def submit():
            return await _execute_scheduled_tool(None, task_id=task["id"], tool_name=task["tool_name"],
                arguments={}, scheduled_for=occurrence, cron_expression=task["cron_expression"],
                configuration_version=task["configuration_version"], automation_id=automation_id,
                automation_generation=task["automation_generation"], automation_project_invoker=management.policy)

        def scheduled_occurrence():
            # Playwright's sync API owns an event loop on the browser thread.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(lambda: asyncio.run(submit())).result(timeout=60)

        first = scheduled_occurrence()
        if first["status"] != "COMPLETED":
            raise AssertionError(f"actual scheduled collector failed: {first}")
        after = business_rows()
        if before != after:
            raise AssertionError("Console and Scheduler drifted in source business keys, state or manual fields")
        calls = len(supplier.requests)
        replay = scheduled_occurrence()
        if replay["run_id"] != first["run_id"] or len(supplier.requests) != calls:
            raise AssertionError("replaying the same actual scheduler occurrence repeated collection")
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT source,automation_invocation_json FROM agent_commands WHERE command_id=%s", (first["command_id"],))
            command = cursor.fetchone()
        invocation = json.loads(command["automation_invocation_json"])
        if command["source"] != "scheduler" or invocation["entrypoint"] != "scheduler":
            raise AssertionError("scheduled adapter did not persist its actual entrypoint")
        return {"status": "PASS", "scheduled_task": task, "run": first, "replay_run_id": replay["run_id"],
            "before": before, "after": after, "entrypoint": invocation["entrypoint"],
            "committed_generation": entry.committed_generation}
    finally:
        save(management.catalog.require(automation_id), original.project_schedule, original.current_enabled_entrypoints)
