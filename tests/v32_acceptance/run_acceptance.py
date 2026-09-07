"""Actual signed Console invocation to durable MySQL Command/Run acceptance."""
from __future__ import annotations

import asyncio
import json
import os
import time
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import async_playwright

from agent.orchestration.models import Actor, ActorType
from tests.v32_acceptance.browser_performance import TASK_ENV, distribution
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.management_fixture import ManagementFixture, connect
from tests.v32_acceptance.runner_fixture import RunnerFixture


def read_run(run_id):
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT run_id,status,error_code,error_summary FROM agent_runs WHERE run_id=%s", (run_id,))
        return cursor.fetchone()


async def measure(console, automation_ids, runner):
    rows = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ["V32_CHROMIUM_EXECUTABLE"], headless=True)
        contexts = [await browser.new_context() for _ in range(4)]
        for context in contexts:
            administrator = console.new_admin()
            async def local_only(route):
                await route.continue_() if urlparse(route.request.url).hostname == "127.0.0.1" else await route.abort()
            await context.route("**/*", local_only)
            response = await context.request.post(console.url + "/login", form={"username": administrator["username"],
                "password": administrator["password"], "next": "/"}, max_redirects=0)
            if response.status != 303:
                raise RuntimeError("real independent administrator login failed")
        payloads = []
        for index, automation_id in enumerate(automation_ids):
            page = await contexts[index].new_page()
            await page.goto(console.url + "/automations", wait_until="domcontentloaded")
            form = page.locator(f'[data-plugin-instance][data-automation-id="{automation_id}"]').locator("xpath=ancestor::form")
            payloads.append(await form.evaluate("form => Object.fromEntries(new FormData(form))"))
            await page.close()

        async def client(index):
            for attempt in range(25):
                request_id = str(uuid4())
                row = {"sample": attempt * 4 + index, "client": index,
                    "request_id": request_id, "acceptance_ms": None, "error": None}
                started = time.monotonic()
                try:
                    response = await contexts[index].request.post(console.url + "/automations/tasks/run-now",
                        form=payloads[index], headers={"X-Browser-Request-UUID": request_id,
                            "X-Requested-With": "XMLHttpRequest", "Origin": console.url}, timeout=10000)
                    body = await response.json()
                    row["response_ms"] = (time.monotonic() - started) * 1000
                    if response.status != 202 or body.get("ok") is not True or not body.get("run_id"):
                        raise AssertionError(f"Run was not durably accepted: HTTP {response.status}, {body}")
                    row["acceptance_ms"] = row["response_ms"]
                    row["run_id"] = body["run_id"]
                    runner.runner.wake(row["run_id"])
                    # Completion is outside the acceptance stopwatch. The next
                    # same-instance command is submitted only after a real
                    # terminal result, preserving the existing overlap guard.
                    completion_started = time.monotonic()
                    while time.monotonic() - completion_started < 60:
                        persisted = await asyncio.to_thread(read_run, row["run_id"])
                        if persisted and persisted["status"] in {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"}:
                            row["execution_status"] = persisted["status"]
                            row["execution_error_code"] = persisted["error_code"]
                            if persisted["status"] != "COMPLETED":
                                raise AssertionError(f"real plugin execution ended {persisted['status']}: {persisted['error_code']}: {persisted['error_summary']}")
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise AssertionError(f"real Runner did not finish within the separate 60-second execution bound: {persisted}")
                    row["completion_wait_ms"] = (time.monotonic() - completion_started) * 1000
                except Exception as exc:
                    row["error"] = type(exc).__name__ + ": " + str(exc)
                    row["elapsed_ms"] = (time.monotonic() - started) * 1000
                rows.append(row)
                if row["error"]:
                    break
        await asyncio.gather(*(client(index) for index in range(4)))
        version = browser.version
        for context in contexts:
            await context.close()
        await browser.close()
    return rows, version


def main():
    actor = Actor(ActorType.CONSOLE_ADMIN, "v32-run-setup", roles=("super_admin",), authenticated_by="mysql_admin_session")
    with ManagementFixture() as management:
        candidates = management.catalog.list()
        candidates = [item for item in candidates if item.plugin_id == "v32_list_automation" and item.configured]
        if not candidates:
            raise RuntimeError("real configured synthetic compute instance is required")
        entries = sorted(candidates, key=lambda item: item.display_name)[:4]
        if len(entries) != 4:
            raise RuntimeError("four distinct real configured instances are required")
        initially_enabled = {entry.automation_id: entry.enabled for entry in entries}
        for entry in entries:
            if not entry.enabled:
                management.management.set_enabled(entry.automation_id, enabled=True, request_id=str(uuid4()),
                    expected_record_version=entry.record_version, actor=actor)
        try:
            with RunnerFixture(management) as runner, ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                signing_secret=management.signing_secret) as console:
                rows, browser = asyncio.run(measure(console, [entry.automation_id for entry in entries], runner))
                runtime_evidence = runner.snapshot()
            run_ids = [row["run_id"] for row in rows if row.get("run_id")]
            persisted = []
            if run_ids:
                with connect() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT run_id,status FROM agent_runs WHERE run_id IN (" + ",".join(["%s"] * len(run_ids)) + ")", tuple(run_ids))
                    persisted = cursor.fetchall()
            result = distribution(rows, "acceptance_ms", 500)
            if len(rows) != 100 or len(persisted) != len(run_ids) or len(set(run_ids)) != len(run_ids):
                result["status"] = "FAIL"
            report = {"scope": "actual browser form -> authenticated Console -> signed policy -> CommandGateway -> MySQL commit; execution not timed",
                "status": result["status"], "result": result, "raw_samples": rows,
                "persisted_runs": persisted, "browser": browser, "concurrent_clients": 4, "distinct_authenticated_administrators": 4,
                "automation_ids": [entry.automation_id for entry in entries], "runtime": runtime_evidence,
                "business_execution": "actual synthetic compute plugin only; no first-party daily business acceptance claim"}
            (TASK_ENV / "run-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"result": result, "persisted_runs": len(persisted)}, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "PASS" else 1
        finally:
            for entry in entries:
                if not initially_enabled[entry.automation_id]:
                    current = management.catalog.require(entry.automation_id)
                    management.management.set_enabled(entry.automation_id, enabled=False, request_id=str(uuid4()),
                        expected_record_version=current.record_version, actor=actor)


if __name__ == "__main__":
    raise SystemExit(main())
