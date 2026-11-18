"""寄件运单查询“打印”: platform waybills open their original-site label and record a real print (synthetic data)."""

import json
from urllib.parse import urlparse

from console.tests.test_manual_saved_print_flow import ORIGIN, app as app, browser as browser
from console.tests.test_spec_20261113_browser import serve_static
from console.tests.test_waybill_photos_browser import render_signed

LABEL = {"platform": "yunda", "platform_name": "韵达", "waybill_no": "900000001", "calls": [
    ["PRINT_INITA", [1, -4, 280, 351, "主单打印"]],
    ["SET_PRINT_PAGESIZE", [1, 740, 930, ""]],
    ["ADD_PRINT_TEXT", [14, 63, 137, 20, "900000001"]],
    ["SET_PRINT_STYLEA", [0, "FontSize", 16]],
]}

LODOP_MOCK = """(() => {
  const state = window;
  state.__lodopCalls = [];
  const lodop = new Proxy({ GET_PRINTER_COUNT: () => 0 }, {
    get(target, name) {
      if (name in target || typeof name !== 'string' || name === 'then') return target[name];
      if (name === 'PREVIEW') return () => {
        state.__lodopCalls.push(['PREVIEW', []]);
        setTimeout(() => target.On_Return && target.On_Return('task', state.__printedCount), 50);
      };
      return (...args) => state.__lodopCalls.push([name, args]);
    },
    set(target, name, value) { target[name] = value; return true; },
  });
  // The page's own loader script runs later; keep this offline C-Lodop.
  const mock = { getInstance: async () => lodop, getLoadErrors: () => [] };
  Object.defineProperty(window, 'BoyiCLodop', { get: () => mock, set: () => {}, configurable: false });
})();
"""


def _run(app, browser, printed_count, check, label=LABEL):
    logs, errors = [], []
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        context.add_init_script(LODOP_MOCK)
        context.add_init_script(f"window.__printedCount = {printed_count};")
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
                route.fulfill(content_type="application/json", body=json.dumps(
                    {"ok": True, "data": {"sign": [], "receipt": [], "receipt_error": ""}}))
            elif parsed.path == "/waybills/41/print-data":
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "platform_label": label}))
            elif parsed.path == "/waybills/41/platform-print-log":
                logs.append({"query": parsed.query, "body": json.loads(route.request.post_data),
                             "uuid": route.request.headers.get("x-browser-request-uuid")})
                route.fulfill(content_type="application/json", body=json.dumps(
                    {"ok": False, "submitted": True, "message": "已向韵达提交打印记录；韵达原站不提供打印记录回读，无法核实。"}))
            elif not serve_static(route, parsed):
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/waybills")
        page.locator('tr[data-waybill-row="yunda:41"]').click()
        page.locator("[data-waybill-detail-dialog] [data-waybill-print]").click()
        check(page, logs)
        assert not errors


STATUS = "document.querySelector('[data-waybill-detail-dialog] [data-waybill-print-status]').textContent"


def test_printed_original_label_records_the_platform_print(app, browser):
    _run(app, browser, 1, _check_printed)


def _check_printed(page, logs):
    page.wait_for_function(f"{STATUS}.includes('无法核实')")
    calls = page.evaluate("window.__lodopCalls")
    assert [call[0] for call in calls] == ["PRINT_INITA", "SET_PRINT_PAGESIZE", "ADD_PRINT_TEXT", "SET_PRINT_STYLEA", "PREVIEW"]
    assert calls[2] == ["ADD_PRINT_TEXT", [14, 63, 137, 20, "900000001"]]
    assert len(logs) == 1 and logs[0]["query"] == "source=yunda"
    assert logs[0]["body"] == {"request_id": logs[0]["uuid"]}


def test_closed_preview_without_printing_records_nothing(app, browser):
    def check(page, logs):
        page.wait_for_function(f"{STATUS}.includes('未打印')")
        assert logs == []
    _run(app, browser, 0, check)


def test_unreviewed_lodop_call_is_never_applied(app, browser):
    bad = {**LABEL, "calls": [*LABEL["calls"], ["PRINT", []]]}

    def check(page, logs):
        page.wait_for_function(f"{STATUS}.includes('未核实的打印指令')")
        assert page.evaluate("window.__lodopCalls") == [] and logs == []
    _run(app, browser, 1, check, bad)


def test_linked_boyi_waybill_offers_both_labels(app):
    import re
    from html import unescape

    from console.tests.test_manual_saved_print_flow import SAVED_ROW

    def render(row):
        html = app.template_env.get_template("waybills.html").render(
            app_title="离线合成测试",
            filters={"q": "", "date_from": "2026-01-02", "date_to": "2026-01-02", "status": "all", "source": "all",
                     "payment_method": "", "delivery_method": "", "sort": "open_date_desc"},
            rows=[row], summary={"total": 1, "invalid_money_count": 0, "opening_cost_total": "0"},
            pagination={"page": 1, "page_size": 50, "total": 1, "total_pages": 1, "offset": 0,
                        "has_prev": False, "has_next": False},
            source_options=[], status_options=[], payment_options=[""], delivery_options=[""], sort_options=[],
            current_url="/waybills", prev_url="", next_url="", db_error="", message="", page_links=[1],
            page_url_base="/waybills?page=", sync_status={"messages": [], "warnings": []},
            source_refresh={"source": "all", "date_from": "2026-01-02", "date_to": "2026-01-02"})
        found = re.findall(r'data-print-data-url="([^"]+)"><i data-feather="printer"></i> ([^<]+)</button>', html)
        return [(unescape(url), text) for url, text in found]

    base = {**SAVED_ROW, "print_data_url": "/waybills/41/print-data?source=manual", "tracking_url": "/tracking"}
    assert render({**base, "linked_platform": "yunda", "linked_waybill_no": "900000001"}) == [
        ("/waybills/41/print-data?source=manual&label=boyi", "打印博益面单"),
        ("/waybills/41/print-data?source=manual", "打印韵达面单")]
    assert render({**base, "linked_platform": "best", "linked_waybill_no": "B1"}) == [
        ("/waybills/41/print-data?source=manual", "打印")]
    assert render(base) == [("/waybills/41/print-data?source=manual", "打印")]
