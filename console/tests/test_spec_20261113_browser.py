"""Spec package 2026-11-13: waybill check-and-export, automation quick bar, tracking sign block."""

import json
from urllib.parse import parse_qs, urlparse

from console.tests.test_manual_saved_print_flow import (
    CONSOLE_DIR,
    ORIGIN,
    PRINT_MOCK,
    app as app,
    browser as browser,
)
from console.tests.test_waybill_photos_browser import JPEG, render_signed

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def serve_static(route, parsed):
    path = CONSOLE_DIR / parsed.path.lstrip("/")
    if parsed.path.startswith("/static/") and path.is_file():
        route.fulfill(path=str(path))
        return True
    return False


def test_waybill_rows_are_checked_and_exported(app, browser):
    exports, errors = [], []
    with browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True) as context:
        context.add_init_script(PRINT_MOCK)
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/waybills":
                route.fulfill(content_type="text/html", body=render_signed(app))
            elif parsed.path == "/waybills/photos":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {"sign": [], "receipt": [], "receipt_error": ""}}))
            elif parsed.path == "/waybills/export.xlsx":
                exports.append(parse_qs(parsed.query))
                route.fulfill(content_type=XLSX_TYPE, body=b"PK-offline",
                              headers={"Content-Disposition": "attachment; filename=\"waybills.xlsx\"; filename*=UTF-8''%E5%AF%84%E4%BB%B6.xlsx"})
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/waybills")
        export = page.locator("[data-waybill-export]")
        count = page.locator("[data-waybill-pick-count]")
        header = page.locator("[data-waybill-pick-all]")

        # 操作列只留「轨迹」; the row itself opens the detail.
        assert page.locator(".waybill-row-actions button").count() == 0
        assert page.locator(".waybill-row-actions a").all_inner_texts() == ["轨迹", "轨迹"]
        assert export.is_disabled() and count.is_hidden()

        page.locator('[data-waybill-pick="yunda:41"]').check()
        assert count.inner_text() == "已选 1 条" and export.is_enabled()
        assert page.locator('tr[data-waybill-row="yunda:41"]').evaluate("row => row.classList.contains('is-picked')")
        assert header.evaluate("box => box.indeterminate && !box.checked")
        assert page.locator("[data-waybill-detail-dialog]").evaluate("dialog => !dialog.open")

        header.check()
        assert count.inner_text() == "已选 2 条"
        assert header.evaluate("box => box.checked && !box.indeterminate")

        with page.expect_download() as download:
            export.click()
        assert download.value.suggested_filename == "寄件.xlsx"
        assert exports == [{"keys": ["yunda:41,ronghui:41"]}]

        header.uncheck()
        assert export.is_disabled() and count.is_hidden()
        assert not errors


def render_tracking(app):
    return app.template_env.get_template("tracking.html").render(app_title="离线合成测试", initial_tracking_number="")


SIGNED_RONGHUI = {
    "ok": True,
    "data": {
        "type": "ronghui_tms",
        "tracking_number": "R900000003",
        "summary": {"status": "已签收"},
        "waybill_stub": {"waybill_no": "R900000003", "receipt_number": "hd0000001"},
        "route_rows": [
            {"scan_station": "合成始发站", "scan_type": "发件", "scan_time": "2026-01-01 08:00:00", "description": "快件在【合成始发站】已装车"},
            {"scan_station": "合成签收部", "scan_type": "签收", "scan_time": "2026-01-02 10:16:19", "description": "快件在【合成签收部】完成签收扫描"},
        ],
    },
}


def test_signed_tracking_shows_sign_block_and_viewer(app, browser):
    photo_requests, errors = [], []
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/tracking":
                route.fulfill(content_type="text/html", body=render_tracking(app))
            elif parsed.path == "/tracking/query":
                route.fulfill(content_type="application/json", body=json.dumps(SIGNED_RONGHUI["data"]))
            elif parsed.path == "/waybills/photos":
                photo_requests.append(parse_qs(parsed.query))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {
                    "sign": ["/waybills/photo-image?platform=ronghui&url=s1", "/waybills/photo-image?platform=ronghui&url=s2"],
                    "receipt": ["/waybills/photo-image?platform=ronghui&url=r1"], "receipt_error": ""}}))
            elif parsed.path == "/waybills/photo-image":
                route.fulfill(content_type="image/jpeg", body=JPEG)
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/tracking")
        page.fill("#trackingInput", "R900000003")
        page.click("#searchBtn")
        # 扫描轨迹: newest scan on top.
        assert page.locator("#shipmentRoutes tbody tr td:nth-child(2)").all_inner_texts()[:2] == ["2026-01-02 10:16:19最新", "2026-01-01 08:00:00"]
        page.locator('[data-shipment-tab="details"]').click()
        section = page.locator("[data-sign-section]")
        section.wait_for()
        assert page.locator(".shipment-value-pill").evaluate("pill => pill.classList.contains('shipment-value-pill--signed')")

        fields = section.locator(".shipment-sign-field").evaluate_all(
            "els => els.map(el => [el.querySelector('dt').textContent, el.querySelector('dd').textContent])")
        assert fields == [["签收时间", "2026-01-02 10:16:19"], ["签收网点", "合成签收部"], ["回单编号", "hd0000001"]]
        thumbs = section.locator(".shipment-sign-thumb")
        thumbs.nth(2).wait_for()
        assert thumbs.locator("span").all_inner_texts() == ["签收单 1", "签收单 2", "回单 1"]
        assert photo_requests == [{"platform": ["ronghui"], "waybill_no": ["R900000003"]}]
        page.wait_for_function("""() => [...document.querySelectorAll('.shipment-sign-thumb img')]
            .filter((img) => img.complete && img.naturalWidth > 0).length === 3""")

        thumbs.nth(2).click()
        viewer = page.locator("#signViewer")
        viewer.locator("[data-sign-viewer-view] img").wait_for()
        assert viewer.locator('[data-sign-viewer-tab="receipt"]').get_attribute("aria-selected") == "true"
        assert viewer.locator("[data-sign-viewer-counter]").inner_text() == "第 1 / 1 张 · 2026-01-02"
        assert viewer.locator("[data-sign-viewer-sub]").inner_text() == "R900000003 · 融辉 TMS · 2026-01-02 签收"
        viewer.locator('[data-sign-viewer-tab="sign"]').click()
        page.keyboard.press("ArrowRight")
        assert viewer.locator("[data-sign-viewer-counter]").inner_text().startswith("第 2 / 2 张")
        page.keyboard.press("Escape")
        page.wait_for_function("!document.getElementById('signViewer').open")
        assert not errors


def test_unsigned_tracking_has_no_sign_block(app, browser):
    # A Yunda waybill that has only arrived: its "最晚应签收时间" notice is not a sign-for scan.
    unsigned = {"data": {
        "type": "yunda", "tracking_number": "900000004", "summary": {"status": "到达"},
        "waybill_stub": {"waybill_no": "900000004"},
        "route_rows": [
            {"scan_time": "", "status": "", "description": "快件最晚应签收时间:2026-01-04 23:59:59"},
            {"scan_time": "2026-01-02 10:00:00", "status": "到达", "description": "快件到达【合成分拨中心】"},
        ],
    }}
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/tracking":
                route.fulfill(content_type="text/html", body=render_tracking(app))
            elif parsed.path == "/tracking/query":
                route.fulfill(content_type="application/json", body=json.dumps(unsigned["data"]))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/tracking")
        page.fill("#trackingInput", "900000004")
        page.click("#searchBtn")
        assert page.locator("#shipmentRoutes tbody tr td:first-child").all_inner_texts() == ["2026-01-02 10:00:00最新", "无数据"]
        page.locator('[data-shipment-tab="details"]').click()
        page.locator(".shipment-details").wait_for()
        assert page.locator("[data-sign-section]").count() == 0
        assert not page.locator(".shipment-value-pill").evaluate("pill => pill.classList.contains('shipment-value-pill--signed')")


QUICK_TASKS = [
    {"task_id": task_id, "task_mode": mode, "name_value": name, "tool_name_value": task_id, "cron_expression_value": "",
     "schedule_time_values": times, "tool_params_json": "{}", "tool_param_fields": [], "search_text": name,
     "last_activity_value": "", "sort_order": index, "is_schedulable": False, "schedule_supported": False,
     "schedule_editable": False, "has_webhook": False, "enabled_value": False, "is_open": False, "feedback": None,
     "last_error_summary": "", "last_run_value": "", "provider": "ronghui"}
    for index, (task_id, mode, name, times) in enumerate([
        ("quote_demo", "manual", "合成报价", []),
        ("receipt_demo", "scheduled", "合成回单归集", ["08:00"]),
    ])
]


def render_automation(app, quick_ids):
    return app.template_env.get_template("automation.html").render(
        app_title="离线合成测试", message="", message_kind="info", settings={}, scheduled_tasks=QUICK_TASKS,
        scheduled_task_count=len(QUICK_TASKS), enabled_task_count=0, management_module="automation",
        management_url="/automations", automation_plugin_packages=[], unsupported_automation_ids=[],
        can_manage_plugins=False, can_manage_approval_policies=False, automation_quick_ids=quick_ids,
    )


def test_quick_bar_pins_and_runs_plugins(app, browser):
    saves, runs, errors = [], [], []
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/automations":
                route.fulfill(content_type="text/html", body=render_automation(app, ["receipt_demo", "gone_demo"]))
            elif parsed.path == "/settings/profile/automation-quick-plugins":
                saves.append(json.loads(route.request.post_data))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": saves[-1], "error": None}))
            elif parsed.path.startswith("/automations/tasks/"):
                runs.append(parsed.path)
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"ok": False, "message": "离线测试"}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/automations")
        pills = page.locator("[data-quick-run]")
        # Only pins that are still on the page are shown, in the saved order.
        assert pills.all_inner_texts() == ["合成回单归集"]

        page.locator("[data-quick-add]").click()
        dialog = page.locator("[data-quick-dialog]")
        assert dialog.locator("[data-quick-count]").inner_text() == "已选 1 / 8"
        assert dialog.locator(".auto-quick-mode").all_inner_texts() == ["手动执行", "定时 每天 08:00"]
        assert dialog.locator("[data-quick-pinned]:not([hidden])").count() == 1
        dialog.locator('[data-quick-option][value="quote_demo"]').check()
        assert dialog.locator("[data-quick-count]").inner_text() == "已选 2 / 8"
        dialog.locator("[data-quick-save]").click()
        page.wait_for_function("!document.querySelector('[data-quick-dialog]').open")
        assert saves == [{"automation_ids": ["receipt_demo", "quote_demo"]}]
        assert pills.all_inner_texts() == ["合成回单归集", "合成报价"]

        # The pill runs the card in place: no scrolling, the icon animates, then the pill shows the outcome.
        page.evaluate("window.scrollTo(0, 0)")
        pills.nth(1).click()
        page.wait_for_function("document.querySelector('[data-quick-run=\"quote_demo\"]').textContent.trim() !== '执行中'", timeout=20000)
        assert runs == ["/automations/tasks/run-now"]
        assert pills.nth(1).inner_text().strip() == "失败"
        assert page.evaluate("window.scrollY") == 0
        assert not errors
