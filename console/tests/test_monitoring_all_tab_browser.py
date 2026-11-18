"""Message center: “全部” is the first tab and merges every category with messages; zero-count tabs are hidden."""

import json
from urllib.parse import parse_qs, urlparse

from console.tests.test_manual_saved_print_flow import ORIGIN, app as app, browser as browser
from console.tests.test_spec_20261113_browser import serve_static

SNAPSHOT = {
    "ok": True, "updated_at": "2026-01-02 10:00:00", "stale": False, "refreshing": False,
    "systems": [
        {"system": "yunda", "label": "韵达", "status": "ok", "categories": [
            {"category_id": "yunda:problems", "title": "问题件 · 未完结", "count": 2, "items_supported": True},
            {"category_id": "yunda:transfers", "title": "调拨件 · 待接单", "count": 0, "items_supported": True},
        ]},
        {"system": "ronghui", "label": "融辉", "status": "ok", "categories": [
            {"category_id": "ronghui:work_response", "title": "收到工单 · 待响应", "count": 1, "items_supported": True},
            {"category_id": "ronghui:notices", "title": "未读公告与消息", "count": 0, "items_supported": False},
        ]},
    ],
}
ITEMS = {
    "yunda:problems": [
        {"id": "y1", "title": "合成问题甲", "occurred_at": "2026-01-02 08:00:00", "content": "合成问题甲"},
        {"id": "y2", "title": "合成问题乙", "occurred_at": "2026-01-02 09:30:00", "content": "合成问题乙"},
    ],
    "ronghui:work_response": [
        {"id": "r1", "title": "合成工单丙", "occurred_at": "2026-01-02 09:00:00", "content": "合成工单丙"},
    ],
}


def test_all_tab_merges_categories_and_hides_zero_tabs(app, browser):
    item_requests, errors = [], []
    body = app.template_env.get_template("portal.html").render(app_title="离线合成测试", message="",
                                                                message_kind="info", settings=app.settings)
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/":
                route.fulfill(content_type="text/html", body=body)
            elif parsed.path == "/monitoring/summary":
                route.fulfill(content_type="application/json", body=json.dumps(SNAPSHOT))
            elif parsed.path == "/monitoring/items":
                category = parse_qs(parsed.query)["category_id"][0]
                item_requests.append(category)
                route.fulfill(content_type="application/json", body=json.dumps({
                    "ok": True, "items": ITEMS[category], "total": len(ITEMS[category]), "has_more": False,
                    "updated_at": "2026-01-02 10:00:00"}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        tabs = page.locator("[data-monitoring-systems] .monitor-category")
        page.locator("[data-monitoring-items] li").nth(2).wait_for()

        # 全部 first and selected; zero-count categories and the empty 待确认 are not shown.
        titles = tabs.locator(".monitor-category-title").all_inner_texts()
        assert titles == ["全部", "问题件 · 未完结", "收到工单 · 待响应"]
        assert tabs.first.get_attribute("aria-selected") == "true"
        assert tabs.first.locator(".monitor-category-count").inner_text() == "3"

        # Rows from both categories, newest first; zero-count categories are never read.
        rows = page.locator("[data-monitoring-items] .monitor-item-title").all_inner_texts()
        assert rows == ["合成问题乙", "合成工单丙", "合成问题甲"]
        assert set(item_requests) == {"ronghui:work_response", "yunda:problems"}

        # A category still opens on its own, and 全部 comes back.
        tabs.nth(2).click()
        page.wait_for_function("document.querySelectorAll('[data-monitoring-items] li').length === 1")
        assert tabs.first.get_attribute("aria-selected") == "false"
        tabs.first.click()
        page.wait_for_function("document.querySelectorAll('[data-monitoring-items] li').length === 3")

        # The source switch narrows 全部 to that platform.
        page.locator('[data-monitoring-source-switch] [data-source="ronghui"]').click()
        page.wait_for_function("document.querySelectorAll('[data-monitoring-items] li').length === 1")
        assert page.locator("[data-monitoring-items] .monitor-item-title").all_inner_texts() == ["合成工单丙"]
        assert not errors
