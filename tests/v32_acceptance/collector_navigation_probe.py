"""C16 actual run output navigation and finance failure event formatting."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse



def exercise_run_links(browser, automation_id, invocation_id, expected_sources):
    response = browser.context.request.get(browser.console.url + "/automations/tasks/output",
        params={"task_id": automation_id, "invocation_id": invocation_id})
    assert response.status == 200
    assert response.json()["invocation_id"] == invocation_id
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
    return {"status": "PASS", "invocation_id": invocation_id, "navigation": navigation,
        "dom_links": actual_urls, "clicked_url": browser.page.url}


def exercise_finance_failure_notification(management, invocation_id, *, expect_sources=True):
    # The current Feishu entrypoint waits on this exact invocation and formats
    # the returned result directly. No historical Run/event projection is used.
    from feishu.automation_messages import automation_result_reply
    result = management.policy.direct_invocations.get(invocation_id)
    assert result['invocation_id'] == invocation_id
    assert result['status'] in {'FAILED', 'WRITE_OUTCOME_UNKNOWN'}
    message, reply_type = automation_result_reply(task_name='财务采集', result=result)
    navigation = result['collector_navigation']
    assert navigation['module'] == 'finance' and bool(navigation['sources']) == expect_sources
    assert '/modules/finance/data-sources' in message
    for source in navigation['sources']:
        assert source['url'] in message and source['display_name'] in message
    if not expect_sources:
        assert '尚无已核验的来源记录' in message
    assert '#sync' not in message
    return {'status': 'PASS', 'invocation_id': invocation_id, 'navigation': navigation,
        'formatted_message': message, 'reply_type': reply_type,
        'external_delivery': 'actual invocation result and production formatter; no real Feishu send'}
