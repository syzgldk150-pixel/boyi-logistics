"""Actual cold Console navigation and signed catalog HTTP timings.

Reports only these measured surfaces, not business execution or maintenance.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.management_fixture import ManagementFixture

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_ENV = PROJECT_ROOT / ".task_tmp" / "v32" / "environment"
PATHS = {
    "automation": "/automations",
    "finance": "/modules/finance/data-sources",
    "customer_service": "/modules/customer-service/data-sources",
}


def distribution(rows, field, limit):
    values = sorted(row[field] for row in rows if row.get(field) is not None)
    errors = sum(row.get("error") is not None for row in rows)
    result = {"samples": len(rows), "successful_samples": len(values), "errors": errors,
        "threshold_ms": limit, "quantile_method": "nearest rank ceil(p*n)"}
    if values:
        result.update(p50_ms=values[math.ceil(len(values) * 0.5) - 1],
            p95_ms=values[math.ceil(len(values) * 0.95) - 1], max_ms=max(values))
        result["status"] = "PASS" if errors == 0 and len(values) == len(rows) and result["p95_ms"] <= limit else "FAIL"
    else:
        result["status"] = "FAIL"
    return result


async def run_browser(management, console, *, samples_per_module=30, verify_search=False):
    rows = []
    with_errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ["V32_CHROMIUM_EXECUTABLE"], headless=True)
        contexts = [await browser.new_context(viewport={"width": 1440, "height": 1080}) for _ in range(4)]
        for context in contexts:
            administrator = console.new_admin()
            async def local_only(route):
                if urlparse(route.request.url).hostname != "127.0.0.1":
                    await route.abort()
                else:
                    await route.continue_()
            await context.route("**/*", local_only)
            response = await context.request.post(console.url + "/login", form={
                "username": administrator["username"], "password": administrator["password"], "next": "/"}, max_redirects=0)
            if response.status != 303:
                raise RuntimeError("real Console session login failed")

        async def navigate(module, index):
            context = contexts[index % len(contexts)]
            page = await context.new_page()
            page.on("pageerror", lambda error: with_errors.append(str(error)))
            await page.add_init_script("""(() => {
              window.__v32 = {shell_ms: null, list_ms: null};
              const check = () => {
                const root = document.querySelector('[data-management-module]');
                if (root && root.getBoundingClientRect().width > 0 && window.__v32.shell_ms === null) window.__v32.shell_ms = performance.now();
                const instances = document.querySelectorAll('[data-plugin-instance]').length;
                const buttons = document.querySelectorAll('[data-extension-action="uninstall"]:not([disabled])').length;
                if (instances >= 50 && buttons >= 50 && document.documentElement.dataset.pluginConfigurationDelegated === 'true' && window.__v32.list_ms === null) window.__v32.list_ms = performance.now();
                requestAnimationFrame(check);
              };
              requestAnimationFrame(check);
            })();""")
            row = {"module": module, "sample": index, "client": index % len(contexts),
                "shell_ms": None, "list_ms": None, "error": None}
            started = time.monotonic()
            try:
                response = await page.goto(console.url + PATHS[module], wait_until="domcontentloaded", timeout=20000)
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                await page.wait_for_function("window.__v32 && window.__v32.list_ms !== null", timeout=15000)
                row.update(await page.evaluate("window.__v32"))
                row["instance_count"] = await page.locator('[data-plugin-instance]').count()
                row["plugin_ids"] = await page.locator('[data-automation-task-row]').evaluate_all("rows => [...new Set(rows.map(row => row.dataset.pluginId))]")
                row["automation_ids"] = await page.locator('[data-plugin-instance]').evaluate_all("rows => rows.map(row => row.dataset.automationId)")
                row["module_seen"] = await page.locator('[data-management-module]').get_attribute("data-management-module")
                if row["module_seen"] != module:
                    raise RuntimeError("page module identity mismatch")
                if verify_search:
                    query = f"V3.2 合成 {module} 01"
                    search = page.locator('[data-automation-search]')
                    await search.fill(query)
                    await page.wait_for_function("document.querySelectorAll('[data-automation-task-row]:not([hidden])').length === 1")
                    match = await page.locator('[data-automation-task-row]:not([hidden]) [data-plugin-instance]').get_attribute('aria-label')
                    if query not in match:
                        raise AssertionError("module search returned another instance")
                    await search.fill('V32-explicit-no-such-instance')
                    await page.wait_for_function("document.querySelectorAll('[data-automation-task-row]:not([hidden])').length === 0")
                    await search.fill('')
                    await page.wait_for_function("document.querySelectorAll('[data-automation-task-row]:not([hidden])').length === 50")
                    row['search'] = {'query':query, 'unique_matches':1, 'no_match_count':0, 'cleared_count':50, 'status':'PASS'}
                    if module == 'finance':
                        await page.screenshot(path=str(TASK_ENV/'finance-data-sources.png'), full_page=False)
            except Exception as exc:
                row["observed_dom"] = await page.evaluate("({marks: window.__v32, instances: document.querySelectorAll('[data-plugin-instance]').length, enabled_uninstall: document.querySelectorAll('[data-extension-action=uninstall]:not([disabled])').length, delegated: document.documentElement.dataset.pluginConfigurationDelegated})")
                row["error"] = type(exc).__name__ + ": " + str(exc).splitlines()[0]
                row["elapsed_ms"] = (time.monotonic() - started) * 1000
                await page.screenshot(path=str(TASK_ENV / f"browser-failure-{module}-{index}.png"), full_page=False)
            finally:
                rows.append(row)
                await page.close()

        for module in PATHS:
            for offset in range(0, samples_per_module, len(contexts)):
                console.app._clear_automation_plugin_catalog_cache()
                await asyncio.gather(*(navigate(module, index) for index in range(offset, min(offset + len(contexts), samples_per_module))))
                if any(row["error"] for row in rows):
                    # Preserve the failed samples and fail explicitly; don't spend
                    # the complete performance window repeating a broken page.
                    break
            if any(row["error"] for row in rows):
                break
        version = browser.version
        for context in contexts:
            await context.close()
        await browser.close()
    return rows, with_errors, version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-only", action="store_true", help="Independently verify module ownership through real HTTP/DOM; does not evaluate performance")
    options = parser.parse_args()
    with ManagementFixture() as management:
        seeded = management.seed_instances()
        fault_canaries = [] if options.scope_only else asyncio.run(management.verify_fault_injection())
        with ConsoleFixture(agent_base_url=management.url,
            internal_token=management.internal_token, signing_secret=management.signing_secret) as console:
            rows, errors, version = asyncio.run(run_browser(management, console, samples_per_module=1 if options.scope_only else 30, verify_search=options.scope_only))
            if options.scope_only:
                valid = len(rows) == len(PATHS) and not errors
                identities = []
                for row in rows:
                    valid = valid and not row["error"] and row.get("instance_count") == 50
                    valid = valid and row.get("plugin_ids") == ["v32_list_" + row["module"]]
                    valid = valid and len(set(row.get("automation_ids", []))) == 50
                    valid = valid and row.get('search', {}).get('status') == 'PASS'
                    identities.extend(row.get("automation_ids", []))
                valid = valid and len(set(identities)) == 150
                result = {"status": "PASS" if valid else "FAIL", "scope": "Actual signed Agent module filter and three Console DOM pages; excludes performance, which has a separate unchanged threshold gate",
                    "raw_samples": rows, "page_errors": errors, "agent_requests": management.requests,
                    "synthetic_instances": seeded, "browser": version}
                (TASK_ENV / "module-scope.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps({"status": result["status"], "modules": len(rows), "distinct_instances": len(set(identities))}))
                return 0 if valid else 1
            results = {}
            for module in PATHS:
                selected = [row for row in rows if row["module"] == module]
                results[module] = {
                    "shell": distribution(selected, "shell_ms", 1000),
                    "list_and_controls": distribution(selected, "list_ms", 1500),
                }
                if len(selected) < 30:
                    results[module]["status"] = "NOT_RUN" if not selected else "FAIL"
                else:
                    results[module]["status"] = "PASS" if all(item["status"] == "PASS" for item in results[module].values()) else "FAIL"
            result = {
                "scope": "real cold Console navigation; MySQL plugin lifecycle/catalog; signed Agent HTTP",
                "synthetic_instances": seeded, "browser": version, "concurrent_clients": 4,
                "distinct_authenticated_administrators": 4,
                "cache_policy": "new page/DOM per sample; four client contexts; server catalog display cache cleared per concurrent batch; persisted packages and database retained; loopback-only routing disables browser HTTP cache",
                "external_directory_delay_seconds": management.external_delay_seconds,
                "external_directory_calls": management.resource_calls,
                "fault_injection_canaries": fault_canaries,
                "fault_boundary_calls": management.fault_calls,
                "agent_requests": management.requests,
                "page_errors": errors, "results": results, "raw_samples": rows,
                "detail_queries": "NOT_RUN", "tab_switch": "NOT_RUN", "run_acceptance": "NOT_RUN",
            }
            (TASK_ENV / "browser-performance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"results": results, "external_directory_calls": management.resource_calls, "page_errors": errors}, ensure_ascii=False, indent=2))
            unexpected_fault_calls = [item for item in management.fault_calls if item["phase"] == "measurement"]
            result["status"] = "PASS" if not errors and not unexpected_fault_calls and all(item["status"] == "PASS" for item in results.values()) else "FAIL"
            (TASK_ENV / "browser-performance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
