"""C09 real financial source/type restoration and partial-source totals."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse

from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.collector_navigation_probe import exercise_run_links, exercise_finance_failure_notification
from tests.v32_acceptance.finance_maintenance import (
    ACCOUNTS, RUNTIME, TARGET, composed, connect, prepare_database, setup_instance, wait_invocation,
)
from tests.v32_acceptance.finance_maintenance_browser import FinanceBrowser
from tests.v32_acceptance.finance_maintenance_drill import business_proof, require_complete


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset-owned-fixture", action="store_true")
    options = parser.parse_args()
    prepare_database(reset=options.reset_owned_fixture)
    report = {"status": "RUNNING"}
    try:
        with composed() as (management, _runner, supplier, artifacts):
            with (ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                    signing_secret=management.signing_secret, runtime_root=management.task_env / "console") as console,
                    FinanceBrowser(console) as browser):
                automation_id = browser.install(artifacts["baseline"])
                setup_instance(management, artifacts["baseline"], automation_id=automation_id)
                browser.save_accounts(automation_id, ACCOUNTS)
                with browser.page.expect_response(lambda response: urlparse(response.url).path == "/finance/summary") as empty_response:
                    browser.page.goto(console.url + f"/modules/finance?start_date={TARGET}&end_date={TARGET}", wait_until="domcontentloaded")
                empty = empty_response.value.json()["data"]
                if empty["validation_status"] != "unavailable" or empty["entry_count"] != 0:
                    raise AssertionError("uncollected source was presented as a published snapshot")
                browser.page.wait_for_function("document.querySelector('[data-finance-metric=total_expense] strong')?.textContent === '尚无数据'")
                report["before_first_collection"] = empty
                baseline = require_complete(browser.run(automation_id))
                proof = business_proof(automation_id, expected_version=artifacts["baseline"]["version"])
                source_ids = [row["source_id"] for row in proof["sources"]]
                report["invocation_links"] = exercise_run_links(browser, automation_id, baseline["invocation_id"], source_ids)
                browser.verify_finance(source_ids, target_date=TARGET,
                    expected_waybills={row["source_id"]: row["waybill_no"] for row in proof["rows"]})
                browser.page.locator("[data-finance-platform]").select_option("ronghui")
                browser.page.locator('[data-finance-tab="entries"]').click()
                form = browser.page.locator("[data-finance-entry-form]")
                form.locator('[name="direction"]').select_option("expense")
                form.locator('[name="fee_level"]').select_option("waybill")
                with browser.page.expect_response(lambda response: urlparse(response.url).path == "/finance/entries"):
                    form.locator('button[type="submit"]').click()
                selected = {"source_id": browser.page.locator("[data-source-selector]").input_value(),
                    "platform": browser.page.locator("[data-finance-platform]").input_value(),
                    "direction": form.locator('[name="direction"]').input_value(),
                    "fee_level": form.locator('[name="fee_level"]').input_value()}
                url = browser.page.url
                browser.page.reload(wait_until="domcontentloaded")
                browser.page.wait_for_function("document.querySelector('[data-source-selector]')?.options.length >= 4")
                form = browser.page.locator("[data-finance-entry-form]")
                restored = {"source_id": browser.page.locator("[data-source-selector]").input_value(),
                    "platform": browser.page.locator("[data-finance-platform]").input_value(),
                    "direction": form.locator('[name="direction"]').input_value(),
                    "fee_level": form.locator('[name="fee_level"]').input_value()}
                report.update(baseline=baseline, selected=selected, restored=restored, selected_url=url)
                if restored != selected:
                    raise AssertionError(f"financial source/type did not survive page refresh: {selected} -> {restored}")
                failed_account = next(iter(ACCOUNTS.values()))
                browser.open_sources()
                calls_before = len(supplier.requests)
                management.account_manager.inactive.add(failed_account)
                try:
                    receipt = browser.submit_current_run(automation_id)
                    assert receipt["http_status"] == 202
                    early = wait_invocation(receipt["body"]["invocation_id"])
                    assert early["status"] == "FAILED"
                    assert len(supplier.requests) == calls_before
                    with connect() as connection, connection.cursor() as cursor:
                        cursor.execute("SELECT COUNT(*) AS n FROM automation_project_generation_leases WHERE invocation_id=%s", (early["invocation_id"],))
                        assert cursor.fetchone()["n"] == 0
                    report["prelease_account_failure"] = {"run": early, "raw_request_count": len(supplier.requests) - calls_before,
                        "lease_count": 0, "notification": exercise_finance_failure_notification(management, early["invocation_id"], expect_sources=False)}
                finally:
                    management.account_manager.inactive.remove(failed_account)
                supplier.failed_accounts.add(failed_account)
                failed_run = wait_invocation(browser.run(automation_id))
                if failed_run["status"] != "FAILED":
                    raise AssertionError(f"supplier failure was not reported: {failed_run}")
                report["failure_notification"] = exercise_finance_failure_notification(management, failed_run["invocation_id"])
                browser.page.goto(console.url + "/modules/finance", wait_until="domcontentloaded")
                browser.page.locator("[data-finance-start-date]").fill(TARGET)
                browser.page.locator("[data-finance-end-date]").fill(TARGET)
                with browser.page.expect_response(lambda response: urlparse(response.url).path == "/finance/summary"
                        and parse_qs(urlparse(response.url).query).get("start_date") == [TARGET]
                        and parse_qs(urlparse(response.url).query).get("end_date") == [TARGET]) as response:
                    browser.page.locator("[data-finance-refresh]").click()
                body = response.value.json()
                summary = body["data"] if "data" in body else body
                if "summary" in summary:
                    summary = summary["summary"]
                if summary["entry_count"] != len(ACCOUNTS) or Decimal(str(summary["total_expense"])) != Decimal("1.25") * len(ACCOUNTS):
                    raise AssertionError(f"failed source history was silently excluded from totals: {summary}")
                if not any(row["account_id"] == failed_account for row in summary["failed_sources"]):
                    raise AssertionError("failed source was not identified separately from retained totals")
                browser.page.locator("[data-finance-partial-warning]").wait_for(state="visible")
                warning = browser.page.locator("[data-finance-partial-warning]").inner_text()
                if "此前已发布的数据" not in warning or "更新时间可能不同" not in warning:
                    raise AssertionError("financial UI did not distinguish retained history and differing source updates")
                report.update(status="PASS", failed_run=failed_run, summary=summary,
                    failure_warning=warning,
                    supplier_requests=supplier.requests, browser_errors=browser.errors)
    except Exception as exc:
        report.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        (RUNTIME / "source-state.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(RUNTIME / "source-state.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
