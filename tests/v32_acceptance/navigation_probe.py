"""Measure real retained tabs and close/reopen the real default settings page."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from tests.v32_acceptance.browser_performance import TASK_ENV, distribution
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.management_fixture import ManagementFixture


INSTRUMENTATION = """(() => {
  const add = EventTarget.prototype.addEventListener;
  const remove = EventTarget.prototype.removeEventListener;
  const listeners = new Map();
  EventTarget.prototype.addEventListener = function(type, listener, options) {
    if (this === window || this === document) {
      const key = (this === window ? 'window:' : 'document:') + type;
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(listener);
    }
    return add.call(this, type, listener, options);
  };
  EventTarget.prototype.removeEventListener = function(type, listener, options) {
    if (this === window || this === document) {
      const key = (this === window ? 'window:' : 'document:') + type;
      listeners.get(key)?.delete(listener);
    }
    return remove.call(this, type, listener, options);
  };
  const intervals = new Set();
  const set = window.setInterval, clear = window.clearInterval;
  window.setInterval = (...args) => {const id = set(...args); intervals.add(id); return id;};
  window.clearInterval = id => {intervals.delete(id); return clear(id);};
  window.__v32Runtime = () => ({
    listeners: Object.fromEntries(Array.from(listeners, ([key, value]) => [key, value.size])),
    intervals: intervals.size,
  });
})();"""


def probe(page, console, result):
    requests, errors = [], []
    page.on("request", lambda request: requests.append({"path": urlparse(request.url).path, "type": request.resource_type}))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(INSTRUMENTATION)
    console.login(page, next_path="/automations")
    page.wait_for_selector("[data-plugin-instance]")
    page.evaluate("window.__v32AutomationDOM = document.querySelector('[data-management-module]')")
    page.locator('[data-nav-list] a[href="/modules/finance"]').first.click()
    page.wait_for_url(console.url + "/modules/finance")
    page.wait_for_function("document.querySelector('[data-console-tab-key=\"/automations\"]') !== null")
    page.wait_for_load_state("networkidle", timeout=10000)
    initial = page.evaluate("window.__v32Runtime()")
    baseline_requests = len(requests)
    samples = []
    for index in range(20):
        key = "/automations" if index % 2 == 0 else "/modules/finance"
        started = page.evaluate("performance.now()")
        page.locator(f'[data-console-tab][data-console-tab-key="{key}"]:visible [data-console-tab-activate]').first.click()
        page.wait_for_function("key => location.pathname === key && !document.body.classList.contains('content-loading')", arg=key)
        elapsed = page.evaluate("performance.now()") - started
        same = page.evaluate("window.__v32AutomationDOM === document.querySelector('[data-management-module]')") if key == "/automations" else True
        samples.append({"sample": index, "tab": key, "tab_ms": elapsed,
            "error": None if same else "automation DOM was rebuilt",
            "runtime": page.evaluate("window.__v32Runtime()")})
    switch_requests = requests[baseline_requests:]
    result.update({"tab_switch": distribution(samples, "tab_ms", 200), "raw_samples": samples,
        "requests_during_switches": switch_requests,
        "runtime_before": initial, "runtime_after": page.evaluate("window.__v32Runtime()"),
        "settings": {"status": "NOT_RUN"}, "page_errors": errors})
    unexpected = [item for item in switch_requests if item["type"] != "stylesheet"]
    result["unexpected_requests_during_switches"] = unexpected
    if unexpected or result["runtime_before"] != result["runtime_after"]:
        result["tab_switch"]["status"] = "FAIL"
    # Use a real visible settings link and close/reopen its tab. No DOM injection.
    page.locator('[data-console-tab][data-console-tab-key="/automations"]:visible [data-console-tab-activate]').first.click()
    settings_link = page.locator('[data-plugin-instance] a[href$="/settings"]').first
    settings_path = settings_link.get_attribute("href")
    settings_link.click()
    page.wait_for_url(console.url + settings_path)
    page.wait_for_selector('[data-default-plugin-settings] button[type="submit"]:enabled')
    first_dom = page.locator('[data-default-plugin-settings]').get_attribute("data-initialized")
    save_started = page.evaluate("performance.now()")
    with page.expect_response(lambda response: urlparse(response.url).path == settings_path + "/bridge", timeout=60000) as saved:
        page.locator('[data-default-plugin-settings] button[type="submit"]').click()
    if saved.value.status != 200 or not saved.value.json().get("ok"):
        raise AssertionError(f"real settings save failed: HTTP {saved.value.status}: {saved.value.json()}")
    page.wait_for_function("document.querySelector('[data-settings-feedback]')?.textContent.includes('设置已保存')", timeout=10000)
    save_ms = page.evaluate("performance.now()") - save_started
    result["settings"] = {"status": "FAIL", "path": settings_path, "initial_initialized": first_dom,
        "save": "PASS", "save_ms": save_ms, "reopen": "NOT_RUN", "compatible_history_restore": "NOT_RUN"}
    page.locator(f'[data-console-tab][data-console-tab-key="{settings_path}"]:visible [data-console-tab-close]').first.click()
    page.wait_for_function("!document.querySelector('[data-default-plugin-settings]')")
    # Reopening the already loaded page script must initialize the new DOM.
    page.locator('[data-nav-list] a[href="/automations"]').first.click()
    page.wait_for_selector("[data-plugin-instance]")
    page.locator(f'a[href="{settings_path}"]').first.click()
    page.wait_for_selector('[data-default-plugin-settings] button[type="submit"]:enabled')
    reopened = page.locator('[data-default-plugin-settings]').get_attribute("data-initialized")
    page.locator('[data-plugin-settings-history] summary').click()
    page.wait_for_function("document.querySelector('[data-plugin-settings-history] select').options.length > 1", timeout=10000)
    history = page.locator('[data-plugin-settings-history] select')
    generation = history.locator("option").nth(1).get_attribute("value")
    history.select_option(generation)
    page.locator('[data-plugin-settings-history] button').click()
    page.wait_for_function("document.querySelector('[data-plugin-settings-history] [role=status]')?.textContent.includes('设置已恢复')", timeout=60000)
    result["settings"] = {"status": "PASS" if first_dom == reopened == "true" else "FAIL",
        "path": settings_path, "initial_initialized": first_dom, "reopened_initialized": reopened,
        "save": "PASS", "save_ms": save_ms, "compatible_history_restore": "PASS", "generation": generation}
    # Open distinct modules through the actual links. Each must retain its own
    # DOM and source selector when another module is opened afterwards.
    routes = []
    for path, module in (("/modules/finance", "finance"), ("/modules/customer-service", "customer_service")):
        page.locator(f'[data-nav-list] a[href="{path}"]').first.click()
        page.wait_for_url(console.url + path)
        active = page.locator('.main-content:not([hidden])')
        active.locator(f'[data-source-selector="{module}"]').wait_for()
        page.wait_for_function("module => document.querySelector('.main-content:not([hidden]) [data-source-selector=' + module + ']')?.options.length >= 51", arg=module)
        source_path = path + "/data-sources"
        active.locator(f'a[href="{source_path}"]').first.click()
        page.wait_for_url(console.url + source_path)
        page.wait_for_selector(f'.main-content:not([hidden]) [data-management-module="{module}"]')
        page.wait_for_function("document.querySelectorAll('.main-content:not([hidden]) [data-plugin-instance]').length === 50")
        search_query = f'V3.2 合成 {module} ' + ('01' if module=='finance' else '02')
        page.locator('.main-content:not([hidden]) [data-automation-search]').fill(search_query)
        page.wait_for_function("document.querySelectorAll('.main-content:not([hidden]) [data-automation-task-row]:not([hidden])').length === 1")
        routes.append({"query_path": path, "source_path": source_path, "module": module,
            "search_query":search_query, "search_matches":1, "status": "PASS"})
    for item in routes:
        for key in (item["query_path"], item["source_path"]):
            page.locator(f'[data-console-tab][data-console-tab-key="{key}"]:visible [data-console-tab-activate]').click()
            page.wait_for_url(console.url + key)
            expected = f'[data-management-module="{item["module"]}"]' if key == item["source_path"] else f'[data-source-selector="{item["module"]}"]'
            page.locator('.main-content:not([hidden])').locator(expected).wait_for()
            if key == item['source_path']:
                current = page.locator('.main-content:not([hidden])')
                if current.locator('[data-automation-search]').input_value()!=item['search_query']:
                    raise AssertionError('retained module search was overwritten by another page')
                if current.locator('[data-automation-task-row]:not([hidden])').count()!=1:
                    raise AssertionError('retained module search result differs')
    result["module_navigation"] = {"status": "PASS", "routes": routes}
    page.locator('[data-console-tab][data-console-tab-key="/automations"]:visible [data-console-tab-activate]').click()
    other_link = page.locator('.main-content:not([hidden]) [data-plugin-instance] a[href$="/settings"]').nth(1)
    other_path = other_link.get_attribute("href")
    if other_path == settings_path:
        raise AssertionError("second instance settings path must be distinct")
    other_link.click()
    page.wait_for_url(console.url + other_path)
    page.locator('.main-content:not([hidden]) [data-default-plugin-settings] button[type="submit"]:enabled').wait_for()
    for path in (settings_path, other_path):
        page.locator(f'[data-console-tab][data-console-tab-key="{path}"]:visible [data-console-tab-activate]').click()
        page.wait_for_url(console.url + path)
        page.locator('.main-content:not([hidden]) [data-default-plugin-settings] button[type="submit"]:enabled').wait_for()
    result["settings"]["multiple_instance_tabs"] = {"status": "PASS", "paths": [settings_path, other_path]}
    result["page_errors"] = errors
    return result


def main():
    with ManagementFixture() as management, ConsoleFixture(agent_base_url=management.url,
        internal_token=management.internal_token, signing_secret=management.signing_secret) as console:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ["V32_CHROMIUM_EXECUTABLE"], headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1080})
            page.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname == "127.0.0.1" else route.abort())
            result = {"status": "FAIL", "tab_switch": {"status": "NOT_RUN"}, "settings": {"status": "NOT_RUN"}}
            try:
                result = probe(page, console, result)
                result["status"] = "PASS" if not result["page_errors"] and result["tab_switch"]["status"] == result["settings"]["status"] == "PASS" else "FAIL"
            except Exception as exc:
                result["error"] = type(exc).__name__ + ": " + str(exc)
                page.screenshot(path=str(TASK_ENV / "navigation-failure.png"))
            result["browser"] = browser.version
            result["scope"] = "real Console/MySQL login, retained DOM tab clicks, signed settings save/restore"
            (TASK_ENV / "navigation-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({key: value for key, value in result.items() if key not in {"raw_samples"}}, ensure_ascii=False, indent=2))
            browser.close()
            return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
