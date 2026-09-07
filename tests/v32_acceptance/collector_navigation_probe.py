"""C16 actual run output navigation and finance failure event formatting."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

from agent.orchestration.models import RunStatus
from shared.redaction import redact_text


def exercise_run_links(browser, automation_id, run_id, expected_sources):
    response = browser.context.request.get(browser.console.url + "/automations/tasks/output",
        params={"task_id": automation_id, "run_id": run_id})
    assert response.status == 200
    navigation = response.json()["collector_navigation"]
    assert navigation["status"] == "known" and navigation["module"] == browser.module
    assert navigation["module_url"] == browser.sources_path
    assert {row["source_id"] for row in navigation["sources"]} == set(expected_sources)
    form = browser.card(automation_id).locator("xpath=ancestor::form")
    drawer = form.locator("[data-terminal-drawer]")
    if not drawer.is_visible():
        form.locator("[data-terminal-toggle]").click()
    links = drawer.locator("[data-run-source-links]")
    links.wait_for(state="visible", timeout=15000)
    browser.page.wait_for_function("""({id, count}) => {
        const form = document.querySelector(`[data-plugin-instance][data-automation-id="${id}"]`)?.closest('form');
        return form?.querySelectorAll('[data-run-source-links] a').length === count;
    }""", arg={"id": automation_id, "count": len(expected_sources) + 1})
    actual_urls = links.locator("a").evaluate_all("rows => rows.map(row => row.getAttribute('href'))")
    assert set(actual_urls) == {navigation["module_url"], *(row["url"] for row in navigation["sources"])}
    # Click a real source hyperlink, then verify the destination selection.
    source_url = navigation["sources"][0]["url"]
    links.locator(f'a[href="{source_url}"]').click()
    browser.page.wait_for_url("**" + source_url)
    browser.page.wait_for_function("""value => document.querySelector('.main-content:not([hidden]) [data-source-selector]')?.value === value""",
        arg=parse_qs(urlparse(source_url).query)["source_id"][0])
    return {"status": "PASS", "run_id": run_id, "navigation": navigation,
        "dom_links": actual_urls, "clicked_url": browser.page.url}


def exercise_finance_failure_notification(management, run_id, *, expect_sources=True):
    # Use the exact production projector/formatter without importing main startup.
    path = Path(__file__).resolve().parents[2] / "agent/main.py"
    names = {"_run_plan_steps", "_project_finance_failure_event", "_project_run_completed_event",
        "_finance_sync_failure_handler", "FINANCE_FAILURE_RUN_STATUSES"}
    nodes = [node for node in ast.parse(path.read_text(encoding="utf-8")).body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets))]
    assert len(nodes) == len(names)
    from feishu.notify import build_finance_anomaly_message
    alerts = []
    namespace = {"Any": Any, "RunStatus": RunStatus, "datetime": datetime,
        "timezone": timezone, "uuid": uuid, "redact_text": redact_text,
        "publish_finance_alert": lambda payload: alerts.append(build_finance_anomaly_message(payload)) or True}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    status = management.repository.get_run(run_id)["status"]
    with management.repository.unit_of_work() as uow:
        with uow.connection.cursor() as cursor:
            cursor.execute("""SELECT * FROM domain_events WHERE run_id=%s AND event_type='agent.run.status_changed'
                AND JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.to'))=%s ORDER BY occurred_at DESC""", (run_id, status))
            events = cursor.fetchall()
        assert len(events) == 1
        event = events[0]
        event["payload_json"] = json.loads(event["payload_json"])
        projection = namespace["_project_run_completed_event"](event, uow)
        assert projection["projected"] is True, "installed finance wrapper failed to project its real failure notification"
        with uow.connection.cursor() as cursor:
            cursor.execute("SELECT * FROM domain_events WHERE event_id=%s", (projection["finance_failure_event_id"],))
            failure = cursor.fetchone()
        failure["payload_json"] = json.loads(failure["payload_json"])
        uow.commit()
    assert namespace["_finance_sync_failure_handler"](failure, None)["sent"] is True
    navigation = failure["payload_json"]["collector_navigation"]
    assert navigation["module"] == "finance" and bool(navigation["sources"]) == expect_sources
    assert len(alerts) == 1 and "/modules/finance/data-sources" in alerts[0]
    for source in navigation["sources"]:
        assert source["url"] in alerts[0] and source["display_name"] in alerts[0]
    if not expect_sources:
        assert "尚无已核验的来源记录" in alerts[0]
    assert "#sync" not in alerts[0] and "/automations" not in alerts[0]
    return {"status": "PASS", "event_id": failure["event_id"], "navigation": navigation,
        "formatted_message": alerts[0], "external_delivery": "captured formatter callback; no real Feishu send"}
