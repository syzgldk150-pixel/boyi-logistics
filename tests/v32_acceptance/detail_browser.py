"""Source-filtered real financial BI and customer detail query-to-DOM timings."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from decimal import Decimal
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from tests.v32_acceptance.browser_performance import TASK_ENV, distribution
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.management_fixture import ManagementFixture


async def measure(console, seeded):
    rows, errors = [], []
    finance_proofs = {row['source_id']: row for row in seeded['finance']['proof_by_source']}
    if len(finance_proofs) != len(seeded['finance']['proof_by_source']) or set(finance_proofs) != set(seeded['finance']['source_ids']):
        raise AssertionError('financial SQL proof must identify every published source exactly once')
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ["V32_CHROMIUM_EXECUTABLE"], headless=True)
        contexts = [await browser.new_context(viewport={"width": 1440, "height": 1080}) for _ in range(4)]
        for context in contexts:
            administrator = console.new_admin()
            async def local_only(route):
                await route.continue_() if urlparse(route.request.url).hostname == "127.0.0.1" else await route.abort()
            await context.route("**/*", local_only)
            response = await context.request.post(console.url + "/login", form={"username": administrator["username"],
                "password": administrator["password"], "next": "/"}, max_redirects=0)
            if response.status != 303:
                raise RuntimeError("real independent administrator login failed")

        async def client(module, client_id):
            page = await contexts[client_id].new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                await page.goto(console.url + "/modules/" + ("finance" if module == "finance" else "customer-service"), wait_until="domcontentloaded")
                selector = "[data-source-selector]"
                await page.wait_for_function("document.querySelector('[data-source-selector]')?.options.length >= 51", timeout=10000)
                await page.wait_for_load_state("networkidle", timeout=10000)
                source_ids = seeded["finance"]["source_ids"] if module == "finance" else [item["source_id"] for item in seeded["customer_service"]["sources"]]
                for iteration in range(25):
                    source_id = source_ids[(iteration * 4 + client_id) % len(source_ids)]
                    row = {"module": module, "client": client_id, "sample": iteration * 4 + client_id,
                        "source_id": source_id, "query_dom_ms": None, "error": None}
                    started = await page.evaluate("performance.now()")
                    try:
                        endpoint = "/finance/summary" if module == "finance" else "/customer-service/problems/query"
                        async with page.expect_response(lambda response: urlparse(response.url).path == endpoint, timeout=10000) as pending:
                            await page.locator(selector).select_option(source_id)
                        response = await pending.value
                        body = await response.json()
                        if response.status != 200:
                            raise AssertionError(f"local query HTTP {response.status}: {body}")
                        if module == "finance":
                            if "source_ids=" + source_id not in response.url:
                                raise AssertionError("financial query omitted selected source")
                            await page.wait_for_function("document.querySelector('[data-finance-status]')?.textContent === '财务总览已更新。' && !document.querySelector('[data-finance-refresh]').disabled", timeout=10000)
                            income = await page.locator('[data-finance-metric="total_income"] strong').inner_text()
                            expense = await page.locator('[data-finance-metric="total_expense"] strong').inner_text()
                            proof = finance_proofs[source_id]
                            if Decimal(income.replace("元", "").replace(",", "").strip()) != Decimal(proof["income"]) or Decimal(expense.replace("元", "").replace(",", "").strip()) != Decimal(proof["expense"]):
                                raise AssertionError(f"selected synthetic source totals differ: income={income}, expense={expense}")
                            row.update(income=income, expense=expense)
                        else:
                            if body.get("ok") is not True or body.get("stats", {}).get("row_count") != 200:
                                raise AssertionError("customer selected source count is not the published 200 rows")
                            if any(item.get("source_id") != source_id for item in body["rows"]):
                                raise AssertionError("customer response crossed selected source")
                            await page.wait_for_function("count => document.querySelector('[data-cs-status]')?.textContent.startsWith('查询完成：已返回 ' + count + ' 条')", arg=len(body["rows"]), timeout=10000)
                            row["rendered_rows"] = await page.locator("[data-cs-row]").count()
                            if row["rendered_rows"] != len(body["rows"]):
                                raise AssertionError("customer visible table lost published rows")
                        row["query_dom_ms"] = await page.evaluate("new Promise(resolve => requestAnimationFrame(() => resolve(performance.now())))") - started
                        row["response_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    except Exception as exc:
                        row["error"] = type(exc).__name__ + ": " + str(exc)
                        row["elapsed_ms"] = await page.evaluate("performance.now()") - started
                        await page.screenshot(path=str(TASK_ENV / f"detail-failure-{module}-{client_id}.png"))
                    rows.append(row)
                    if row["error"]:
                        break
            except Exception as exc:
                rows.append({"module": module, "client": client_id, "sample": "initialization",
                    "query_dom_ms": None, "error": type(exc).__name__ + ": " + str(exc)})
                await page.screenshot(path=str(TASK_ENV / f"detail-failure-{module}-{client_id}.png"))
            finally:
                await page.close()

        for module in ("finance", "customer_service"):
            await asyncio.gather(*(client(module, index) for index in range(4)))
        version = browser.version
        for context in contexts:
            await context.close()
        await browser.close()
    return rows, errors, version


def main():
    seeded = json.loads((TASK_ENV / "synthetic-business-data.json").read_text(encoding="utf-8"))
    with ManagementFixture() as management, ConsoleFixture(agent_base_url=management.url,
        internal_token=management.internal_token, signing_secret=management.signing_secret) as console:
        rows, errors, browser = asyncio.run(measure(console, seeded))
        results = {module: distribution([row for row in rows if row["module"] == module], "query_dom_ms", 1500)
            for module in ("finance", "customer_service")}
        for result in results.values():
            if result["samples"] != 100:
                result["status"] = "FAIL"
                result["remaining_samples"] = "NOT_RUN"
        report = {"scope": "real source selection to actual local SQL response and visible financial BI/customer table",
            "status": "PASS" if not errors and all(item["status"] == "PASS" for item in results.values()) else "FAIL",
            "data": seeded, "concurrent_clients": 4, "distinct_authenticated_administrators": 4, "browser": browser,
            "results": results, "raw_samples": rows, "page_errors": errors,
            "agent_requests": management.requests,
            "cache_policy": "retained real page; normal local queries; browser routing disables HTTP cache; no response substitution"}
        (TASK_ENV / "detail-browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "results": results, "page_errors": errors}, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
