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


def test_yunda_reply_attachments_upload_at_once_and_go_with_the_reply(app, browser):
    replies, uploads = [], []
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
                    "ok": True, "items": [REPLY_ITEM], "total": 1, "has_more": False}))
            elif parsed.path == "/customer-service/problems/attachments/upload":
                data = route.request.post_data_buffer or b""
                uploads.append((route.request.headers.get("x-browser-request-uuid"), b'name="purpose"' in data and b"problem_reply" in data,
                                b'name="account_id"' in data and b"yunda_default" in data))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "attachment": {
                    "platform": "yunda", "file_name": "a.txt", "old_name": "a.txt", "new_name": "x1.txt", "file_path": "/up/x1.txt"}}))
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
        assert page.inner_text(".attach-zone") == "附件选择（最多3个，最大5M，支持拖拽）"
        page.set_input_files(".attach-zone input", files=[{"name": "a&b.txt", "mimeType": "text/plain", "buffer": b"x"}])
        page.wait_for_function("document.querySelector('.attach-message')?.textContent === '不支持文件名包含符号的文件'")
        assert not uploads
        page.set_input_files(".attach-zone input", files=[{"name": "a.txt", "mimeType": "text/plain", "buffer": b"x"}])
        page.locator(".attach-item").wait_for()
        assert page.inner_text(".attach-name") == "a.txt"
        assert uploads and uploads[0][0] and uploads[0][1] and uploads[0][2]
        page.fill("[aria-label='回复正文']", "已处理，附件为签收照片")
        page.click(".reply-form-save")
        page.locator(".monitor-reply-done").wait_for()
        assert replies[0]["payload"]["attachments"] == [
            {"platform": "yunda", "file_name": "a.txt", "old_name": "a.txt", "new_name": "x1.txt", "file_path": "/up/x1.txt"}]


WORK_SNAPSHOT = {
    "ok": True, "updated_at": "2026-01-02 10:00:00", "stale": False, "refreshing": False,
    "systems": [{"system": "ronghui", "label": "融辉", "status": "ok", "categories": [
        {"category_id": "ronghui:work_response", "title": "收到工单 · 待响应", "count": 1, "items_supported": True}]}],
}


def _work_item():
    from shared.original_page_actions import work_order_specs

    row = {"WORK_ORDER_CODE": "W-SYN-1", "BILL_CODE": "SYNTHETIC-2", "WORK_TYPE": "咨询单", "WORK_NAME": "合成类别",
           "WORK_CONTENT": "合成工单正文", "WORK_STATE": "待责任方响应", "URGE_COUNT": 0, "APPEAL_STATUS": None}
    return {"id": "W-SYN-1", "title": "合成类别", "occurred_at": "2026-01-02 08:00:00", "content": "合成工单正文",
            "status_label": "待责任方响应", "waybill_no": "SYNTHETIC-2",
            "actions": {"platform": "ronghui", "account_id": "ronghui_daxiang_s",
                        "item": {"external_id": "W-SYN-1", "BILL_CODE": "SYNTHETIC-2", "WORK_TYPE": "咨询单", "WORK_NAME": "合成类别"},
                        "specs": work_order_specs(row, {"loginEmpName": "合成员工", "loginSiteName": "合成网点"})}}


def test_work_order_buttons_open_the_original_dialog_and_save(app, browser):
    """用户 2026-11-27：工单按原页工具栏处理；原页拦下的按钮显示原页提示，对话框字段与原页一致。"""
    posts = []
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
                route.fulfill(content_type="application/json", body=json.dumps(WORK_SNAPSHOT))
            elif parsed.path == "/monitoring/items":
                route.fulfill(content_type="application/json", body=json.dumps({
                    "ok": True, "items": [_work_item()], "total": 1, "has_more": False}))
            elif parsed.path == "/customer-service/process":
                posts.append((route.request.headers.get("x-browser-request-uuid"), json.loads(route.request.post_data)))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "verified": True}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        page.locator(".action-button[data-operation='respond']").wait_for()
        assert page.locator(".action-button").all_inner_texts() == ["响应", "完结", "催工单", "申诉"]
        page.click(".action-button[data-operation='finish']")
        assert page.inner_text(".action-bar-note") == "工单状态暂不支持完结"
        assert page.locator(".action-dialog").count() == 0
        page.click(".action-button[data-operation='respond']")
        dialog = page.locator(".action-dialog")
        assert dialog.locator(".action-label").all_inner_texts() == ["处理人", "处理站点", "工单类型", "工单类别", "工单内容", "*处理内容"]
        assert page.input_value("[aria-label='处理人']") == "合成员工"
        dialog.locator(".action-save").click()
        assert dialog.locator(".action-message").inner_text() == "处理内容不能为空"
        assert not posts
        page.fill("[aria-label='处理内容']", "已联系客户，今天派送")
        dialog.locator(".action-save").click()
        page.wait_for_function("document.querySelector('.monitor-item-status')?.textContent === '已响应'")
        uuid, sent = posts[0]
        assert uuid
        assert sent == {"platform": "ronghui", "account_id": "ronghui_daxiang_s",
                        "item": {"external_id": "W-SYN-1", "BILL_CODE": "SYNTHETIC-2", "WORK_TYPE": "咨询单", "WORK_NAME": "合成类别"},
                        "kind": "work_order", "operation": "respond",
                        "payload": {"HANDLE_CONTENT": "已联系客户，今天派送", "attachments": []}}


def test_yunda_work_order_and_transfer_buttons(app, browser):
    """韵达工单“回复处理”外呼人身份切换带出姓名电话；调拨件“接单”按原页确认后直接提交。"""
    from shared.original_page_actions import cssp_specs, transfer_specs

    detail = {"complaintId": "C-SYN", "compStatus": 3, "compStatusName": "已首回，待处理", "comper": "合成投诉人", "comperPhone": "13800000000",
              "sender": "合成寄件人", "senderPhone": "13900000000", "receiver": "", "receiverPhone": ""}
    items = [
        {"id": "C-SYN", "title": "合成工单", "occurred_at": "2026-01-02 08:00:00", "content": "合成工单正文", "status_label": "已首回，待处理",
         "actions": {"platform": "yunda", "account_id": "yunda_default", "item": {"external_id": "C-SYN"}, "specs": cssp_specs(detail)}},
        {"id": "R-SYN", "title": "调拨件待接单", "occurred_at": "2026-01-02 07:00:00", "content": "合成调拨件", "status_label": "待接单",
         "actions": {"platform": "yunda", "account_id": "yunda_default", "item": {"external_id": "R-SYN"},
                     "specs": transfer_specs({"Replace_Id": "R-SYN", "Replace_Status_Code": "0"})}},
    ]
    snapshot = {"ok": True, "updated_at": "2026-01-02 10:00:00", "stale": False, "refreshing": False,
                "systems": [{"system": "yunda", "label": "韵达", "status": "ok", "categories": [
                    {"category_id": "yunda:work_orders", "title": "收到的待处理工单", "count": 2, "items_supported": True}]}]}
    posts = []
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
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "items": items, "total": 2, "has_more": False}))
            elif parsed.path == "/customer-service/process":
                posts.append(json.loads(route.request.post_data))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "verified": True}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        page.locator(".action-button[data-operation='reply_handling']").wait_for()
        page.click(".action-button[data-operation='prompt_resp']")
        assert "不能立即响应" in page.inner_text(".action-bar-note")
        page.click(".action-button[data-operation='reply_handling']")
        assert page.input_value("[aria-label='姓名']") == "合成投诉人"
        page.check(".action-radio:has-text('发件人') input")
        assert page.input_value("[aria-label='姓名']") == "合成寄件人" and page.input_value("[aria-label='外呼电话']") == "13900000000"
        page.check(".action-radio:has-text('第三方') input")
        assert page.input_value("[aria-label='姓名']") == ""
        page.fill("[aria-label='姓名']", "合成第三方")
        page.fill("[aria-label='外呼电话']", "13700000000")
        page.fill("[aria-label='沟通记录']", "已沟通")
        page.locator(".action-dialog .action-save").click()
        page.wait_for_function("document.querySelectorAll('.monitor-item-status')[0]?.textContent === '已回复处理'")
        assert posts[0]["payload"] == {"putboundIdentity": "第三方", "customerName": "合成第三方", "callNumber": "13700000000",
                                       "dealContent": "已沟通", "attachments": []}
        page.once("dialog", lambda dialog: (posts.append({"confirm": dialog.message}), dialog.accept()))
        page.click(".action-button[data-operation='accept']")
        page.wait_for_function("[...document.querySelectorAll('.action-bar-note')].some((n) => n.textContent === '已接单')")
        assert posts[1] == {"confirm": "是否确认接单？"}
        assert posts[2]["kind"] == "transfer" and posts[2]["operation"] == "accept" and posts[2]["item"] == {"external_id": "R-SYN"}


def test_ronghui_new_appeal_dialog_links_process_and_appeal_types(app, browser):
    """融辉“新增申诉”：处理类型决定申诉类型下拉，选中申诉类型带出提示文字；偷重偷方时显示实际重量。"""
    from shared.original_page_actions import complaint_against_specs

    row = {"EXCEPTION_ID": "E-SYN", "BILL_CODE": "SYNTHETIC-3", "APPEAL_TYPE": "差错处理", "EXCEPTION_VOLUME": "20"}
    item = {"id": "E-SYN", "title": "合成被投诉", "occurred_at": "2026-01-02 08:00:00", "content": "合成投诉", "status_label": "投诉待审核",
            "actions": {"platform": "ronghui", "account_id": "ronghui_daxiang_s", "item": {"external_id": "E-SYN", "BILL_CODE": "SYNTHETIC-3"},
                        "specs": complaint_against_specs(row, {"loginEmpName": "合成员工", "loginSiteName": "合成网点"})}}
    snapshot = {"ok": True, "updated_at": "2026-01-02 10:00:00", "stale": False, "refreshing": False,
                "systems": [{"system": "ronghui", "label": "融辉", "status": "ok", "categories": [
                    {"category_id": "ronghui:complaints_against", "title": "被投诉 · 未完结", "count": 1, "items_supported": True}]}]}
    lookups, posts = [], []
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
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "items": [item], "total": 1, "has_more": False}))
            elif parsed.path == "/customer-service/lookup":
                sent = json.loads(route.request.post_data)
                lookups.append((sent["operation"], sent["payload"]))
                if sent["operation"] == "process_types":
                    options = [{"value": "差错处理", "label": "差错处理"}, {"value": "仲裁处理", "label": "仲裁处理"}]
                else:
                    options = ([{"value": "偷重偷方", "label": "偷重偷方", "meta": {"TIP": "请上传称重照片"}},
                                {"value": "延误", "label": "延误", "meta": {"TIP": "说明延误原因"}}]
                               if sent["payload"].get("PROCESS_TYPE") == "差错处理" else [{"value": "仲裁类", "label": "仲裁类", "meta": {}}])
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "options": options}))
            elif parsed.path == "/customer-service/process":
                posts.append(json.loads(route.request.post_data))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "verified": True}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        page.click(".action-button[data-operation='appeal']")
        page.wait_for_function("[...document.querySelectorAll('[aria-label=\"申诉类型\"] option')].length === 3")
        assert page.input_value("[aria-label='处理类型']") == "差错处理"
        assert page.locator("[data-action-row='PRACTICAL_WEIGHT']").is_hidden()
        page.select_option("[aria-label='申诉类型']", "偷重偷方")
        assert page.get_attribute("[aria-label='申诉内容']", "placeholder") == "请上传称重照片"
        assert page.locator("[data-action-row='PRACTICAL_WEIGHT']").is_visible()
        page.select_option("[aria-label='处理类型']", "仲裁处理")
        page.wait_for_function("[...document.querySelectorAll('[aria-label=\"申诉类型\"] option')].map((o) => o.value).includes('仲裁类')")
        assert ("appeal_types", {"PROCESS_TYPE": "仲裁处理"}) in lookups
        page.select_option("[aria-label='申诉类型']", "仲裁类")
        page.fill("[aria-label='联系方式']", "13800000000")
        page.locator(".action-dialog .action-save").click()
        page.wait_for_function("document.querySelector('.monitor-item-status')?.textContent === '已新增申诉'")
        sent = posts[0]["payload"]
        assert sent["PROCESS_TYPE"] == "仲裁处理" and sent["APPEAL_TYPE"] == "仲裁类" and sent["CLAIM_AMOUNT"] == "20"
        assert sent["PHONE"] == "13800000000" and len([key for key in sent if key.startswith("appeal_file")]) == 10


def test_yunda_receive_toolbar_and_appoint(app, browser):
    """韵达工单分类上方的“接单”读本站待接单量后提交本次接单量；工单“转人员”从本机构人员里查找选择。"""
    from shared.original_page_actions import cssp_receive_specs, cssp_specs

    permissions = frozenset({"comp:sysCompDeal:receive", "comp:sysCompDeal:receiveAppointUser"})
    detail = {"complaintId": "C-SYN", "compStatus": 2, "compStatusName": "已响应，待首回"}
    snapshot = {"ok": True, "updated_at": "2026-01-02 10:00:00", "stale": False, "refreshing": False,
                "systems": [{"system": "yunda", "label": "韵达", "status": "ok", "categories": [
                    {"category_id": "yunda:work_orders", "title": "收到的待处理工单", "count": 1, "items_supported": True,
                     "actions": {"platform": "yunda", "account_id": "yunda_default", "label": "韵达工单",
                                 "item": {"external_id": "receive"}, "specs": cssp_receive_specs(permissions)}}]}]}
    items = [{"id": "C-SYN", "title": "合成工单", "occurred_at": "2026-01-02 08:00:00", "content": "合成工单正文", "status_label": "已响应，待首回",
              "actions": {"platform": "yunda", "account_id": "yunda_default", "item": {"external_id": "C-SYN", "waybill_no": "W1"},
                          "specs": cssp_specs(detail, permissions)}}]
    posts, lookups = [], []
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
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "items": items, "total": 1, "has_more": False}))
            elif parsed.path == "/customer-service/lookup":
                sent = json.loads(route.request.post_data)
                lookups.append((sent["operation"], sent["payload"]))
                data = {"ok": True, "value": "7"} if sent["operation"] == "receive_count" else {
                    "ok": True, "sites": [{"SITE_CODE": "1001", "SITE_NAME": "合成员工(1001)"}]}
                route.fulfill(content_type="application/json", body=json.dumps(data))
            elif parsed.path == "/customer-service/process":
                posts.append(json.loads(route.request.post_data))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "verified": True}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        toolbar = page.locator(".monitor-category-actions")
        toolbar.wait_for()
        assert toolbar.locator(".action-button").all_inner_texts() == ["接单"]
        # 只有“结单…”等有权限的按钮显示；本账号有转人员权限、没有加急接单权限。
        assert page.locator(".action-button[data-operation='appoint']").count() == 1
        assert page.locator(".action-button[data-operation='urgent_receive']").count() == 0
        toolbar.locator(".action-button").click()
        page.wait_for_function("document.querySelector('[aria-label=\"本站待接单量\"]')?.value === '7'")
        page.locator(".action-dialog .action-save").click()
        assert page.inner_text(".action-dialog .action-message") == "本次接单量不能为空"
        page.fill("[aria-label='本次接单量']", "2")
        page.locator(".action-dialog .action-save").click()
        page.wait_for_function("document.querySelectorAll('.action-dialog').length === 0")
        assert posts[0]["operation"] == "receive" and posts[0]["payload"] == {"receiveNum": "2"}
        page.click(".action-button[data-operation='appoint']")
        page.fill("[aria-label='转入人员']", "合成")
        page.click(".action-dialog .action-site-search")
        page.click(".action-site-option")
        page.locator(".action-dialog .action-save").click()
        page.wait_for_function("document.querySelectorAll('.action-dialog').length === 0")
        assert posts[1]["operation"] == "appoint" and posts[1]["payload"] == {"user_code": "1001"}
        assert ("people", {"key": "合成"}) in lookups


def test_ronghui_appeal_create_picks_business_account(app, browser):
    """融辉申诉登记“新增”挂在申诉分类上方；对话框先选业务账号，联动查询和提交都用所选账号。"""
    from shared.original_page_actions import appeal_create_spec

    snapshot = {"ok": True, "updated_at": "2026-01-02 10:00:00", "stale": False, "refreshing": False,
                "systems": [{"system": "ronghui", "label": "融辉", "status": "ok", "categories": [
                    {"category_id": "ronghui:appeals", "title": "申诉 · 未完结", "count": 0, "items_supported": True,
                     "actions": {"platform": "ronghui", "account_id": "ronghui_default", "label": "融辉申诉登记", "item": {"external_id": "new"},
                                 "specs": [appeal_create_spec([("ronghui_default", "操作场账号"), ("price_default", "大祥站账号")])]}}]}]}
    lookups, posts = [], []
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
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "items": [], "total": 0, "has_more": False}))
            elif parsed.path == "/customer-service/lookup":
                sent = json.loads(route.request.post_data)
                lookups.append((sent["account_id"], sent["operation"]))
                options = [{"value": "差错处理", "label": "差错处理"}] if sent["operation"] == "process_types" else [
                    {"value": "延误", "label": "延误", "meta": {}}]
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "options": options}))
            elif parsed.path == "/customer-service/process":
                posts.append(json.loads(route.request.post_data))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "verified": True}))
            elif parsed.path == "/customer-service/pending-summary":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "total": 0, "items": []}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        context.add_init_script("window.EventSource = undefined;")
        page.goto(f"{ORIGIN}/")
        page.locator(".monitor-category-actions .action-button").click()
        page.select_option("[aria-label='业务账号']", "price_default")
        page.wait_for_function("document.querySelector('[aria-label=\"处理类型\"]').options.length === 2")
        assert ("price_default", "process_types") in lookups
        page.fill("[aria-label='运单号']", "SYNTHETIC-9")
        page.select_option("[aria-label='处理类型']", "差错处理")
        page.wait_for_function("document.querySelector('[aria-label=\"申诉类型\"]').options.length === 2")
        page.select_option("[aria-label='申诉类型']", "延误")
        page.fill("[aria-label='联系方式']", "13800000000")
        page.fill("[aria-label='索赔金额']", "10")
        page.locator(".action-dialog .action-save").click()
        page.wait_for_function("document.querySelectorAll('.action-dialog').length === 0")
        assert posts[0]["account_id"] == "price_default" and posts[0]["operation"] == "create"
        assert posts[0]["payload"]["BILL_CODE"] == "SYNTHETIC-9"
