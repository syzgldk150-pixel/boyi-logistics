"""Real scheduled entry repeats a Console-collected source through one publisher."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from agent.scheduler import _execute_scheduled_tool
from shared.customer_service_repository import CustomerServiceRepository
from tests.v32_acceptance.finance_maintenance import wait_invocation


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
        return {"stats": {key: result['stats'][key] for key in ('row_count', 'open_count', 'resolved_count')},
            "rows": [{key: row[key] for key in keys} for row in result["rows"]]}

    before = business_rows()
    scheduler_entries = [key for key, value in original.invocation_contracts.items()
                         if value.get('contribution_kind') == 'scheduler']
    if len(scheduler_entries) != 1:
        raise AssertionError('collector must declare exactly one tested schedule entry')
    scheduler_entry = scheduler_entries[0]
    save(original, {"kind": "daily_times", "times": ["01:23"], "enabled": True},
        tuple(sorted(set(original.current_enabled_entrypoints) | {scheduler_entry})))
    try:
        entry = management.catalog.require(automation_id)
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,tool_name,cron_expression,configuration_version,automation_generation FROM scheduled_tasks WHERE automation_id=%s", (automation_id,))
            tasks = cursor.fetchall()
        if len(tasks) != 1:
            raise AssertionError("the actual saved collector schedule did not yield exactly one job")
        task = tasks[0]
        occurrence = datetime(2026, 9, 7, 1, 23, tzinfo=timezone.utc)

        async def submit(when):
            return await _execute_scheduled_tool(None, task_id=task["id"], tool_name=task["tool_name"],
                arguments={}, scheduled_for=when, cron_expression=task["cron_expression"],
                configuration_version=task["configuration_version"], automation_id=automation_id,
                automation_generation=task["automation_generation"], automation_project_invoker=management.policy)

        def scheduled_occurrence(when=occurrence):
            # Playwright's sync API owns an event loop on the browser thread.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(lambda: asyncio.run(submit(when))).result(timeout=60)

        accepted = scheduled_occurrence()
        first = wait_invocation(accepted["invocation_id"], connection_factory=connection_factory)
        if first["status"] != "COMPLETED":
            raise AssertionError(f"actual scheduled collector failed: {first}")
        after = business_rows()
        if before != after:
            raise AssertionError("Console and Scheduler drifted in source business keys, state or manual fields")
        calls = len(supplier.requests)
        replay = scheduled_occurrence()
        if replay["invocation_id"] != first["invocation_id"] or len(supplier.requests) != calls:
            raise AssertionError("replaying the same actual scheduler occurrence repeated collection")
        supplier.fail = True
        failed_time = occurrence + timedelta(days=1)
        failed_receipt = scheduled_occurrence(failed_time)
        failed = wait_invocation(failed_receipt['invocation_id'], connection_factory=connection_factory)
        if failed['status'] != 'FAILED' or business_rows() != before:
            raise AssertionError('failed scheduled collection did not preserve business history: ' + json.dumps(failed, default=str))
        failed_calls = len(supplier.requests)
        failed_replay = scheduled_occurrence(failed_time)
        if failed_replay['invocation_id'] != failed['invocation_id'] or len(supplier.requests) != failed_calls:
            raise AssertionError('failed occurrence was automatically re-executed')
        supplier.fail = False
        future_receipt = scheduled_occurrence(occurrence + timedelta(days=2))
        future = wait_invocation(future_receipt['invocation_id'], connection_factory=connection_factory)
        if future['status'] != 'COMPLETED' or business_rows() != before:
            raise AssertionError('future scheduled occurrence did not preserve the normal collection path')
        if management.catalog.require(automation_id).project_schedule != entry.project_schedule:
            raise AssertionError('failed occurrence changed the future schedule')
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT source,invocation_json FROM automation_plugin_invocations WHERE invocation_id=%s", (first["invocation_id"],))
            call = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) AS n FROM agent_commands")
            assert cursor.fetchone()["n"] == 0, "direct scheduler must not create a Command"
            cursor.execute("SELECT COUNT(*) AS n FROM agent_runs")
            assert cursor.fetchone()["n"] == 0, "direct scheduler must not create a Run"
        invocation = json.loads(call["invocation_json"])
        if call["source"] != "scheduler" or invocation["entrypoint"] != "scheduler":
            raise AssertionError("scheduled adapter did not persist its actual entrypoint")
        return {"status": "PASS", "scheduled_task": task, "invocation": first, "replay_invocation_id": replay["invocation_id"],
            "before": before, "after": after, "entrypoint": invocation["entrypoint"],
            "failed_occurrence": failed, "failed_replay": failed_replay, "future_occurrence": future,
            "contribution_id": scheduler_entry, "committed_generation": entry.committed_generation}
    finally:
        supplier.fail = False
        save(management.catalog.require(automation_id), original.project_schedule, original.current_enabled_entrypoints)
