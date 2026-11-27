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


REPLY_SNAPSHOT = {
    "ok": True, "updated_at": "2026-01-02 10:00:00", "stale": False, "refreshing": False,
    "systems": [{"system": "yunda", "label": "韵达", "status": "ok", "categories": [
        {"category_id": "yunda:problems", "title": "问题件 · 未完结", "count": 1, "items_supported": True}]}],
}
REPLY_ITEM = {
    "id": "Y-PROB-1", "title": "合成催件", "occurred_at": "2026-01-02 08:00:00", "content": "合成问题正文",
    "status_label": "处理中", "waybill_no": "SYNTHETIC-1",
    "reply": {"platform": "yunda", "account_id": "yunda_default", "external_id": "Y-PROB-1",
              "source_direction": "query", "waybill_no": "SYNTHETIC-1", "status": "处理中",
              "registered_at": "2026-01-02 08:00:00",
              # The original reply page's form (shared/problem_reply_rules.yunda_reply_form).
              "form": {"platform": "yunda", "blocked": "", "reply_max": 600, "default_status": "2", "review_entry": False,
                       "statuses": [{"value": "1", "label": "处理中"}, {"value": "2", "label": "已处理"}]}},
}


def test_problem_items_are_replied_in_place(app, browser):
    """用户 2026-11-27：问题件在消息中心直接回复，不跳转；失败保留内容可重试，成功后标为已回复。"""
    replies, errors = [], []
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
                route.fulfill(content_type="application/json", body=json.dumps(REPLY_SNAPSHOT))
            elif parsed.path == "/monitoring/items":
                route.fulfill(content_type="application/json", body=json.dumps({
                    "ok": True, "items": [REPLY_ITEM], "total": 1, "has_more": False, "updated_at": "2026-01-02 10:00:00"}))
            elif parsed.path == "/customer-service/problems/reply":
                replies.append((route.request.headers.get("x-browser-request-uuid"), json.loads(route.request.post_data)))
                if len(replies) == 1:
                    route.fulfill(status=502, content_type="application/json",
                                  body=json.dumps({"ok": False, "message": "合成：原平台未确认"}))
                else:
                    route.fulfill(content_type="application/json", body=json.dumps({"ok": True}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        page.locator(".monitor-reply-toggle").wait_for()
        page.click(".monitor-reply-toggle")
        assert page.locator(".monitor-reply select option").all_inner_texts() == ["处理中", "已处理"]
        assert page.input_value(".monitor-reply select") == "2"
        page.fill(".monitor-reply textarea", "已联系收件人，明天派送")
        page.click(".reply-form-save")
        page.wait_for_function("document.querySelector('.reply-form-message')?.dataset.tone === 'error'")
        assert "原平台未确认" in page.inner_text(".reply-form-message")
        assert page.input_value(".monitor-reply textarea") == "已联系收件人，明天派送"  # kept for a retry

        page.click(".reply-form-save")
        page.locator(".monitor-reply-done").wait_for()
        assert page.inner_text(".monitor-reply-done") == "已回复：已联系收件人，明天派送"
        assert page.locator(".monitor-reply-toggle").count() == 0
        assert page.inner_text(".monitor-item-status") == "已回复"
        (first_uuid, first), (second_uuid, second) = replies
        # The server answered the first try with a failure, so the retry is a new request.
        assert first_uuid and second_uuid and first_uuid != second_uuid
        assert second == {
            "platform": "yunda", "account_id": "yunda_default",
            "item": {"platform": "yunda", "account_id": "yunda_default", "external_id": "Y-PROB-1",
                     "source_direction": "query", "waybill_no": "SYNTHETIC-1", "status": "处理中",
                     "registered_at": "2026-01-02 08:00:00"},
            "payload": {"reply_text": "已联系收件人，明天派送", "prob_status": "2"},
        }
        assert not errors


def test_a_reply_lost_in_the_network_is_retried_with_the_same_request(app, browser):
    replies = []
    body = app.template_env.get_template("portal.html").render(app_title="离线合成测试", message="",
                                                                message_kind="info", settings=app.settings)
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/":
                route.fulfill(content_type="text/html", body=body)
            elif parsed.path == "/monitoring/summary":
                route.fulfill(content_type="application/json", body=json.dumps(REPLY_SNAPSHOT))
            elif parsed.path == "/monitoring/items":
                route.fulfill(content_type="application/json", body=json.dumps({
                    "ok": True, "items": [REPLY_ITEM], "total": 1, "has_more": False, "updated_at": "2026-01-02 10:00:00"}))
            elif parsed.path == "/customer-service/problems/reply":
                replies.append(route.request.headers.get("x-browser-request-uuid"))
                if len(replies) == 1:
                    route.abort("connectionreset")
                else:
                    route.fulfill(content_type="application/json", body=json.dumps({"ok": True}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        page.locator(".monitor-reply-toggle").wait_for()
        page.click(".monitor-reply-toggle")
        page.fill(".monitor-reply textarea", "合成回复")
        page.click(".reply-form-save")
        page.wait_for_function("document.querySelector('.reply-form-message')?.dataset.tone === 'error'")
        assert "不确定是否已提交" in page.inner_text(".reply-form-message")
        page.click(".reply-form-save")
        page.locator(".monitor-reply-done").wait_for()
        assert replies[0] and replies[0] == replies[1]


RONGHUI_SELF_PICK_ITEM = {
    "id": "RH-1", "title": "开单为自提件", "occurred_at": "2026-01-02 08:00:00", "content": "合成问题正文",
    "status_label": "未回复", "waybill_no": "R-SYNTH-1",
    "reply": {"platform": "ronghui", "account_id": "price_default", "external_id": "RH-1", "source_direction": "received",
              "waybill_no": "R-SYNTH-1", "status": "", "registered_at": "2026-01-02 08:00:00",
              "form": {"platform": "ronghui", "blocked": "", "reply_max": 500, "return_opinions": [], "self_pick": True,
                       "pick_types": ["安排司机提货", "收件人自行提货", "其他"], "driver_pick_types": ["安排司机提货", "收件人自行提货"],
                       "pickup_days": ["2026-01-02", "2026-01-03"], "pickup_hours": ["09:00-10:00", "10:00-11:00"],
                       "max_drivers": 5, "piece_number": 3}},
}


def test_ronghui_self_pickup_reply_sends_the_original_form_fields(app, browser):
    replies = []
    snapshot = {"ok": True, "updated_at": "2026-01-02 10:00:00", "systems": [{"system": "ronghui", "label": "融辉", "status": "ok",
                "categories": [{"category_id": "ronghui:problems", "title": "问题件 · 未回复", "count": 1, "items_supported": True}]}]}
    body = app.template_env.get_template("portal.html").render(app_title="离线合成测试", message="",
                                                                message_kind="info", settings=app.settings)
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/":
                route.fulfill(content_type="text/html", body=body)
            elif parsed.path == "/monitoring/summary":
                route.fulfill(content_type="application/json", body=json.dumps(snapshot))
            elif parsed.path == "/monitoring/items":
                route.fulfill(content_type="application/json", body=json.dumps({
                    "ok": True, "items": [RONGHUI_SELF_PICK_ITEM], "total": 1, "has_more": False}))
            elif parsed.path == "/customer-service/problems/reply":
                replies.append(json.loads(route.request.post_data))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        page.click(".monitor-reply-toggle")
        assert page.locator(".monitor-reply .reply-form-driver").count() == 0  # drivers only after a pickup type
        page.get_by_label("安排司机提货").check()
        # 原页：选了提货方式就生成第一行，件数默认开单件数，行末“添加司机”，计数右上。
        assert page.input_value("[aria-label='司机1件数']") == "3"
        assert page.inner_text(".reply-form-counter") == "当前已添加 1 名司机（最多5名）"
        page.fill("[aria-label='司机1司机姓名']", "合成司机")
        page.fill("[aria-label='司机1手机号']", "1380000")
        page.locator("[aria-label='司机1身份证号']").focus()  # leaving a box checks it, message under it
        assert page.locator(".reply-form-driver-input").nth(1).inner_text() == "手机号格式不正确"
        page.fill("[aria-label='司机1手机号']", "13800000000")
        page.fill("[aria-label='司机1车牌号']", "湘E12345")
        page.fill("[aria-label='司机1件数']", "2")
        page.select_option("[aria-label='提货日期']", "2026-01-03")
        page.select_option("[aria-label='提货时间段']", "10:00-11:00")
        page.get_by_text("添加司机", exact=True).click()
        assert page.locator(".monitor-reply .reply-form-driver").count() == 2
        page.click(".reply-form-save")  # an empty second row blocks saving, as on the original page
        page.wait_for_function("document.querySelector('.reply-form-message')?.textContent === '司机信息填完整!'")
        assert not replies
        page.get_by_text("删除司机").click()
        assert page.locator(".monitor-reply .reply-form-driver").count() == 1
        assert page.input_value("[aria-label='司机1司机姓名']") == "合成司机"
        page.fill("[aria-label='回复']", "已安排司机提货")
        page.click(".reply-form-save")
        page.locator(".monitor-reply-done").wait_for()
        assert replies[0]["item"]["registered_at"] == "2026-01-02 08:00:00"
        assert replies[0]["payload"] == {
            "reply_text": "已安排司机提货", "pick_type": "安排司机提货", "pickup_day": "2026-01-03", "pickup_hour": "10:00-11:00",
            "drivers": [{"name": "合成司机", "phone": "13800000000", "idCard": "", "plate": "湘E12345", "count": "2"}],
        }
