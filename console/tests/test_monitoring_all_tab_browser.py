"""Message center (2026-11-19): no category tabs. The list merges every category with messages, newest
first, narrowed only by the 全部/韵达/融辉 switch; “加载更多” reads the next page of each category that has
more; a metric card can still narrow the list and shows a removable chip."""

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
PAGE_2 = [{"id": "y3", "title": "合成问题丁", "occurred_at": "2026-01-01 08:00:00", "content": "合成问题丁"}]
ITEMS = {
    "yunda:problems": [
        {"id": "y1", "title": "合成问题甲", "occurred_at": "2026-01-02 08:00:00", "content": "合成问题甲"},
        {"id": "y2", "title": "合成问题乙", "occurred_at": "2026-01-02 09:30:00", "content": "合成问题乙"},
    ],
    "ronghui:work_response": [
        {"id": "r1", "title": "合成工单丙", "occurred_at": "2026-01-02 09:00:00",
         "content": "合成长正文" + "虚构网点回复内容，" * 40},
    ],
}


def test_message_center_merges_everything_without_category_tabs(app, browser):
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
                query = parse_qs(parsed.query)
                category, page_no = query["category_id"][0], query["page"][0]
                item_requests.append((category, page_no))
                items = PAGE_2 if page_no == "2" else ITEMS[category]
                more = category == "yunda:problems" and page_no == "1"
                route.fulfill(content_type="application/json", body=json.dumps({
                    "ok": True, "items": items, "total": 3 if category == "yunda:problems" else 1, "has_more": more,
                    "updated_at": "2026-01-02 10:00:00"}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        page.locator("[data-monitoring-items] li").nth(2).wait_for()

        # No category tabs, no “其余 N 类” note: just the total and the platform switch.
        assert page.locator("[data-monitoring-systems] .monitor-category").count() == 0
        assert page.inner_text("[data-monitoring-systems]") == "共 3 条待办"

        # Rows from both categories, newest first; zero-count categories are never read.
        rows = page.locator("[data-monitoring-items] .monitor-item-title").all_inner_texts()
        assert rows == ["合成问题乙", "合成工单丙", "合成问题甲"]
        assert {category for category, _ in item_requests} == {"ronghui:work_response", "yunda:problems"}

        # A long one-line body collapses to an ellipsis instead of pushing the row past the list edge.
        widths = page.evaluate("""() => {
            const list = document.querySelector('[data-monitoring-items]');
            return [list.clientWidth, Math.max(...[...list.querySelectorAll('li, li *')].map((n) =>
                n.getBoundingClientRect().right - list.getBoundingClientRect().left))];
        }""")
        assert widths[1] <= widths[0] + 1, widths

        import os
        if os.environ.get("MONITOR_SHOT"):
            page.screenshot(path=os.environ["MONITOR_SHOT"], full_page=True)

        # 加载更多 reads page 2 only of the category that has more.
        page.click("[data-monitoring-more]")
        page.wait_for_function("document.querySelectorAll('[data-monitoring-items] li').length === 4")
        assert ("yunda:problems", "2") in item_requests and ("ronghui:work_response", "2") not in item_requests
        assert page.locator("[data-monitoring-more]").is_hidden()

        # A metric card narrows the merged list to all of its categories; the chip brings everything back.
        page.click('[data-monitoring-metric="work"]')
        page.wait_for_function("document.querySelectorAll('[data-monitoring-items] li').length === 1")
        assert "工单" in page.inner_text("[data-monitoring-clear-filter]")
        assert page.get_attribute('[data-monitoring-metric="work"]', "aria-pressed") == "true"
        page.click('[data-monitoring-metric="problems"]')
        page.wait_for_function("document.querySelectorAll('[data-monitoring-items] li').length === 2")
        assert page.locator("[data-monitoring-items] .monitor-item-title").all_inner_texts() == ["合成问题乙", "合成问题甲"]
        assert page.locator("[data-monitoring-more]").is_visible()  # its page 2 is read on demand
        page.click("[data-monitoring-clear-filter]")
        page.wait_for_function("document.querySelectorAll('[data-monitoring-items] li').length === 3")

        # The source switch narrows the merged list to that platform.
        page.locator('[data-monitoring-source-switch] [data-source="ronghui"]').click()
        page.wait_for_function("document.querySelectorAll('[data-monitoring-items] li').length === 1")
        assert page.locator("[data-monitoring-items] .monitor-item-title").all_inner_texts() == ["合成工单丙"]
        assert not errors
