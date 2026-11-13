"""Date-range calendars page months with the mouse wheel; plugin upgrade runs from one dialog."""

import datetime as dt
import json
from urllib.parse import urlparse

from console.tests.test_manual_saved_print_flow import (
    CONSOLE_DIR,
    ORIGIN,
    PRINT_MOCK,
    app as app,
    browser as browser,
)
from console.tests.test_waybill_photos_browser import render_signed


def _serve_static(route, parsed):
    path = CONSOLE_DIR / parsed.path.lstrip("/")
    if parsed.path.startswith("/static/") and path.is_file():
        route.fulfill(path=str(path))
        return True
    return False


def test_waybill_calendar_wheel_crosses_months_and_presets(app, browser):
    errors = []
    with browser.new_context(viewport={"width": 1600, "height": 1100}) as context:
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
            elif not _serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/waybills")
        picker = page.locator("[data-date-range-picker]")
        picker.locator("[data-date-trigger]").click()
        title = picker.locator("[data-calendar-title]")
        assert title.inner_text() == "2026年1月"

        # Click the start, wheel down one notch to the next month, click the end, then 完成.
        picker.locator('[data-date-value="2026-01-05"]').click()
        picker.locator("[data-calendar-grid]").hover()
        page.mouse.wheel(0, 120)
        page.wait_for_function("document.querySelector('[data-calendar-title]').textContent === '2026年2月'")
        picker.locator('[data-date-value="2026-02-15"]').click()
        assert picker.locator('[data-date-value="2026-02-10"]').get_attribute("class").count("is-in-range") == 1
        picker.locator("[data-date-done]").click()
        assert page.locator("#dateFrom").input_value() == "2026-01-05"
        assert page.locator("#dateTo").input_value() == "2026-02-15"
        assert picker.locator("[data-date-label]").inner_text() == "2026/01/05 至 2026/02/15"

        # Wheel up goes back a month.
        picker.locator("[data-date-trigger]").click()
        picker.locator("[data-calendar-grid]").hover()
        page.mouse.wheel(0, -120)
        page.wait_for_function("document.querySelector('[data-calendar-title]').textContent === '2025年12月'")

        labels = picker.locator("[data-date-quick-range]").all_inner_texts()
        assert labels == ["今天", "昨天", "当月"]
        today = dt.date.fromisoformat(page.evaluate(
            "(() => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; })()"))
        picker.locator('[data-date-quick-range="yesterday"]').click()
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        assert (page.locator("#dateFrom").input_value(), page.locator("#dateTo").input_value()) == (yesterday, yesterday)
        picker.locator("[data-date-trigger]").click()
        picker.locator('[data-date-quick-range="month"]').click()
        next_month = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        month_end = (next_month - dt.timedelta(days=1)).isoformat()
        assert (page.locator("#dateFrom").input_value(), page.locator("#dateTo").input_value()) == (
            today.replace(day=1).isoformat(), month_end)
        assert errors == []


def _upgrade_dialog_markup():
    source = (CONSOLE_DIR / "templates" / "automation.html").read_text(encoding="utf-8")
    start = source.index('<dialog class="extension-install-dialog extension-upgrade-dialog"')
    return source[start:source.index("</dialog>", start) + len("</dialog>")]


UPGRADE_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/style.css"></head><body>
<div data-extension-instance data-automation-id="automation.demo" data-record-version="3"
     data-current-version="1.0.0" data-instance-name="示例插件">
  <button class="ghost-btn" type="button" data-extension-action="upgrade">升级</button>
  <p data-extension-feedback hidden></p>
</div>
{dialog}
<script src="/static/extensions.js"></script></body></html>"""


def test_upgrade_button_opens_dialog_and_confirm_posts_package(browser):
    posts, errors = [], []
    with browser.new_context(viewport={"width": 1280, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/automations":
                route.fulfill(content_type="text/html", body=UPGRADE_PAGE.format(dialog=_upgrade_dialog_markup()))
            elif parsed.path == "/automations/plugins/automation.demo/upgrade":
                request = route.request
                posts.append({"uuid": request.headers.get("x-browser-request-uuid"), "body": request.post_data_buffer})
                if len(posts) == 1:
                    route.fulfill(status=409, content_type="application/json",
                                  body=json.dumps({"ok": False, "error": {"message": "版本冲突，请重试"}}))
                else:
                    route.fulfill(content_type="application/json", body=json.dumps(
                        {"ok": True, "message": "已升级到 1.1.0", "data": {"record_version": 4, "version": "1.1.0"}}))
            elif not _serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/automations")
        page.locator('[data-extension-action="upgrade"]').click()
        dialog = page.locator("[data-extension-upgrade-dialog]")
        assert dialog.evaluate("node => node.open")
        assert "示例插件（当前版本 1.0.0）" in dialog.locator("[data-extension-upgrade-target]").inner_text()
        confirm = dialog.locator("[data-extension-upgrade-confirm]")
        assert confirm.is_disabled()

        # Drag-and-drop a package onto the drop zone.
        page.evaluate("""() => {
          const transfer = new DataTransfer();
          transfer.items.add(new File([new Uint8Array([80, 75, 3, 4])], "demo-1.1.0.zip", {type: "application/zip"}));
          document.querySelector("[data-extension-upgrade-dropzone]").dispatchEvent(
            new DragEvent("drop", {dataTransfer: transfer, bubbles: true, cancelable: true}));
        }""")
        assert dialog.locator("[data-extension-upgrade-file-name]").inner_text() == "demo-1.1.0.zip"
        assert confirm.is_enabled()

        confirm.click()
        page.wait_for_function("document.querySelector('[data-extension-upgrade-feedback]').textContent.includes('版本冲突')")
        assert dialog.evaluate("node => node.open")
        confirm.click()
        page.wait_for_function("!document.querySelector('[data-extension-upgrade-dialog]').open")

        assert len(posts) == 2
        assert posts[0]["uuid"] and posts[0]["uuid"] == posts[1]["uuid"]  # same file retries with the same request id
        assert b'name="expected_record_version"' in posts[1]["body"] and b"demo-1.1.0.zip" in posts[1]["body"]
        card = page.locator("[data-extension-instance]")
        assert card.get_attribute("data-record-version") == "4"
        assert card.get_attribute("data-current-version") == "1.1.0"
        assert "已升级到 1.1.0" in card.locator("[data-extension-feedback]").inner_text()
        assert errors == []
