"""Actual Console immediate invocation admission and isolated execution timings."""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import platform
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import async_playwright

from agent.orchestration.models import Actor, ActorType
from tests.v32_acceptance.browser_performance import TASK_ENV, distribution
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.management_fixture import ManagementFixture, connect
from tests.direct_invocation_fixture import DirectFixture

sys.stdout.reconfigure(encoding="utf-8")


async def measure(console, automation_ids, runner, *, expect_busy=False, prime=None):
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

        if prime is not None:
            await asyncio.to_thread(prime)

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
                    if (response.status != 202 or body.get("ok") is not (not expect_busy)
                            or not body.get("invocation_id") or (expect_busy and body.get("status") != "FAILED")):
                        raise AssertionError(f"Invocation was not durably accepted: HTTP {response.status}, {body}")
                    row["acceptance_ms"] = row["response_ms"]
                    row["invocation_id"] = body["invocation_id"]
                    if expect_busy:
                        persisted = await asyncio.to_thread(runner.service.get, row["invocation_id"])
                        row["execution_status"] = persisted["status"]
                        row["execution_error_code"] = persisted["error_code"]
                        if (persisted["status"] != "FAILED"
                                or persisted["error_code"] != "EXECUTION_RESOURCE_BUSY"
                                or not persisted["finished_at"]):
                            raise AssertionError("full-capacity request was not a terminal busy rejection")
                        rows.append(row)
                        continue
                    # Completion has its own bound, separate from immediate
                    # admission. The next call uses a new request identity.
                    completion_started = time.monotonic()
                    persisted = await asyncio.to_thread(runner.service.wait_sync, row["invocation_id"], timeout_seconds=60)
                    row["execution_status"] = persisted["status"]
                    row["execution_error_code"] = persisted["error_code"]
                    if persisted["status"] != "COMPLETED":
                        raise AssertionError(f"actual plugin did not complete within 60 seconds: {persisted}")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--busy", action="store_true", help="Measure bounded-capacity rejection receipts separately")
    args = parser.parse_args()
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
            primed = []
            busy_evidence = {}
            with DirectFixture(management) as runner, ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                signing_secret=management.signing_secret) as console:
                def prime():
                    from tests.test_direct_plugin_invocation_mysql import _install, _service_package
                    identity = _install(management, _service_package(management.task_env,
                        plugin_id="busy_receipt_" + uuid4().hex[:8], sleep_seconds=29))
                    capacity = runner.service.max_concurrency + runner.service.max_waiting
                    for _ in range(capacity):
                        receipt = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=actor)
                        primed.append(receipt["invocation_id"])
                        if receipt["status"] != "STARTING":
                            raise AssertionError("could not fill the unchanged admission capacity")
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        processes = [item["proc"] for item in runner.router._running.values()
                            if item.get("automation_id") == identity and item.get("proc") is not None]
                        if processes and all(proc.returncode is None for proc in processes):
                            break
                        time.sleep(.01)
                    else:
                        raise AssertionError("real blocking Service V2 subprocess did not start")
                    busy_evidence.update(live_capacity=capacity, max_concurrency=runner.service.max_concurrency,
                        max_waiting=runner.service.max_waiting, resource_wait_seconds=runner.service.resource_wait_seconds,
                        active_before=len(runner.service.active_invocations()), subprocess_count=len(processes))

                try:
                    rows, browser = asyncio.run(measure(console, [entry.automation_id for entry in entries], runner,
                        expect_busy=args.busy, prime=prime if args.busy else None))
                    if args.busy:
                        busy_evidence["active_after"] = len(runner.service.active_invocations())
                        if busy_evidence["active_before"] != busy_evidence["live_capacity"] or busy_evidence["active_after"] != busy_evidence["live_capacity"]:
                            raise AssertionError("capacity was not held throughout rejection measurement")
                finally:
                    # Cancel waiting calls first so releasing the real child cannot
                    # cause another deliberately slow fixture to start.
                    for invocation_id in reversed(primed):
                        asyncio.run_coroutine_threadsafe(runner.service.cancel(invocation_id), runner.loop).result(10)
                runtime_evidence = runner.snapshot()
            invocation_ids = [row["invocation_id"] for row in rows if row.get("invocation_id")]
            persisted = []
            if invocation_ids:
                with connect() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT invocation_id,status,result_json FROM automation_plugin_invocations WHERE invocation_id IN (" + ",".join(["%s"] * len(invocation_ids)) + ")", tuple(invocation_ids))
                    persisted = cursor.fetchall()
                    for row in persisted:
                        raw = row.pop('result_json')
                        row['result'] = json.loads(raw) if isinstance(raw, str) else raw
            result = distribution(rows, "acceptance_ms", 500)
            result["busy_rejections"] = sum(row.get("execution_error_code") == "EXECUTION_RESOURCE_BUSY" for row in rows)
            result["timeouts"] = sum("Timeout" in (row.get("error") or "") for row in rows)
            if args.busy and invocation_ids:
                with connect() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) AS n FROM automation_project_generation_leases WHERE invocation_id IN (" + ",".join(["%s"] * len(invocation_ids)) + ")", tuple(invocation_ids))
                    busy_evidence["rejected_execution_leases"] = cursor.fetchone()["n"]
                if busy_evidence["rejected_execution_leases"]:
                    result["status"] = "FAIL"
            if len(rows) != 100 or len(persisted) != len(invocation_ids) or len(set(invocation_ids)) != len(invocation_ids):
                result["status"] = "FAIL"
            report = {"scope": "actual browser form -> authenticated Console -> signed policy -> immediate process admission -> MySQL Invocation fact; execution separately timed",
                "status": result["status"], "runtime_model": "SERVICE_V2", "result": result, "raw_samples": rows,
                "persisted_invocations": persisted, "browser": browser, "concurrent_clients": 4, "distinct_authenticated_administrators": 4,
                "automation_ids": [entry.automation_id for entry in entries], "runtime": runtime_evidence,
                "scenario": "busy_rejection" if args.busy else "accepted", "busy_evidence": busy_evidence,
                "environment": {"python": platform.python_version(), "platform": platform.platform(), "cpu_count": os.cpu_count(),
                    "memory_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
                    "cache": "same-process warm application; fresh browser contexts and real authenticated sessions"},
                "business_execution": "actual synthetic compute plugin only; no first-party daily business acceptance claim"}
            filename = "run-busy-acceptance.json" if args.busy else "run-acceptance.json"
            (TASK_ENV / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"result": result, "persisted_invocations": len(persisted)}, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "PASS" else 1
        finally:
            for entry in entries:
                if not initially_enabled[entry.automation_id]:
                    current = management.catalog.require(entry.automation_id)
                    management.management.set_enabled(entry.automation_id, enabled=False, request_id=str(uuid4()),
                        expected_record_version=current.record_version, actor=actor)


if __name__ == "__main__":
    raise SystemExit(main())
