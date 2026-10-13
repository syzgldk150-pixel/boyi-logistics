"""Offline saved-waybill navigation tests; all browser requests are intercepted."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.app import LocalDocFlowApp
from console.app_support import ActionResult, ui_label
from console.database import MANUAL_PAYMENT_METHODS


CONSOLE_DIR = Path(__file__).resolve().parents[1]
ORIGIN = "https://console.test"
FRAME_PATH = "/ocr/boyi/frame"
SAVED_ROW = {
    "id": 41,
    "source": "manual",
    "waybill_no": "BY00041",
    "open_date": "2026-01-02",
    "destination_site": "测试市",
    "receiver_name": "合成收件人",
    "receiver_phone": "00000000000",
    "receiver_address": "测试省测试市合成路1号",
    "sender_name": "合成发件人",
    "sender_phone": "00000000001",
    "sender_address": "测试省测试市合成路2号",
    "goods_name_lines": "合成货物",
    "package_type_lines": "纸箱",
    "quantity_lines": "2",
    "weight_volume": "10kg/0.1m³",
    "delivery_method": "送货",
    "freight_fee": "12.30",
    "payment_method": "寄付",
    "receipt_required": 1,
    "remark": "仅离线测试",
    "internal_test_marker": "must-not-enter-print-payload",
}


@pytest.fixture
def app():
    instance = object.__new__(LocalDocFlowApp)
    instance.repository = Mock()
    instance.repository.count_by_status.return_value = {}
    instance.repository.list_documents_by_status.return_value = []
    instance.repository.list_writers.return_value = []
    instance.repository.peek_next_manual_waybill_no.return_value = "BY00042"
    instance.repository.get_waybill.return_value = dict(SAVED_ROW)
    instance.settings = SimpleNamespace(app_title="离线测试", amap_api_key="", amap_security_code="")
    instance.template_store = Mock()
    instance.template_store.get_active_template_name.return_value = "test"
    instance.template_store.list_templates.return_value = []
    instance._get_template_spec_for_document = Mock(return_value={"fields": []})
    instance._load_waybill_entry_extensions = Mock(return_value=instance._empty_waybill_entry_extensions())
    instance._validate_active_waybill_entry_extensions = Mock(return_value=(True, ""))
    instance.task_queue = Mock()
    instance.task_queue.snapshot.return_value = {}
    instance.template_env = Environment(
        loader=FileSystemLoader(CONSOLE_DIR / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    instance.template_env.globals.update(
        ui_label=ui_label,
        manual_payment_methods=MANUAL_PAYMENT_METHODS,
        current_admin_user=lambda: None,
        console_navigation=(),
    )
    instance._send_html = Mock()
    instance._redirect_with_message = Mock()
    instance.service = SimpleNamespace(apply_manual_waybill=Mock(return_value=ActionResult(
        ok=True, message="合成运单已保存", waybill_id=41, waybill_no="BY00041",
    )))
    return instance


def render_frame(app, query=None):
    app._render_document(object(), None, {**(query or {}), "boyi_frame": ["1"]})
    return app._send_html.call_args.args[1]


@pytest.mark.parametrize("return_to", (FRAME_PATH, FRAME_PATH + "?tab=fixture", "/ocr", ""))
def test_save_and_print_redirects_to_entry_frame(app, return_to):
    values = {"auto_print": "1", "return_to": return_to}
    app._parse_urlencoded_form = Mock(return_value=values)
    handler = object()

    app._handle_manual_waybill(handler)

    app.service.apply_manual_waybill.assert_called_once_with(values)
    args = app._redirect_with_message.call_args.args
    target = urlparse(args[1])
    assert target.path == FRAME_PATH
    assert parse_qs(target.query)["saved_waybill_id"] == ["41"]
    assert args[3] == "success"
    if "tab=" in return_to:
        assert parse_qs(target.query)["tab"] == ["fixture"]


@pytest.mark.parametrize("ok, auto_print", ((True, ""), (False, "1")))
def test_nonprinting_or_failed_save_does_not_schedule_preview(app, ok, auto_print):
    app._parse_urlencoded_form = Mock(return_value={"auto_print": auto_print, "return_to": FRAME_PATH})
    app.service.apply_manual_waybill.return_value = ActionResult(
        ok=ok, message="合成保存结果", waybill_id=41 if ok else None,
    )
    app._handle_manual_waybill(object())
    args = app._redirect_with_message.call_args.args
    assert args[1] == FRAME_PATH
    assert args[3] == ("success" if ok else "warning")


def test_saved_waybill_is_read_with_manual_source_and_safe_payload(app):
    html = render_frame(app, {"saved_waybill_id": ["41"]})
    app.repository.get_waybill.assert_called_once_with(41, source="manual")
    assert "BY00041" in html
    assert "must-not-enter-print-payload" not in html
    assert 'value="BY00042"' in html


PRINT_MOCK = """(() => {
  if (location.origin !== 'https://console.test') return;
  const state = window.top;
  state.__printEvents ||= [];
  state.__printPayloads ||= [];
  state.__printerIndexes ||= [];
  state.__copies ||= [];
  const lodop = {
    GET_PRINTER_COUNT: () => 1,
    GET_PRINTER_NAME: () => 'Synthetic Label Printer',
    PRINT_INIT() {}, SET_PREVIEW_WINDOW() {},
    SET_PRINT_COPIES(value) { state.__copies.push(value); },
    SET_PRINTER_INDEX(value) { state.__printerIndexes.push(value); },
    PREVIEW() { state.__printEvents.push('preview'); },
    PRINT() { state.__printEvents.push('print'); }
  };
  window.BoyiCLodop = {
    getInstance: async () => {
      if (state.__failPrint) throw new Error('C-Lodop service not found');
      return lodop;
    },
    getLoadErrors: () => []
  };
  window.WaybillLabelLodop = {
    applyTemplate: async (_lodop, data) => state.__printPayloads.push(structuredClone(data))
  };
  window.WaybillLabelHtml = {renderPreview: async () => {}, buildHtml: async () => ''};
})();
"""


@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("Chromium is required for saved-print browser regressions")
        with runtime.chromium.launch() as instance:
            yield instance


@pytest.fixture
def browser_page(app, browser):
    with browser.new_context(viewport={"width": 1600, "height": 1100}) as context:
        context.add_init_script(PRINT_MOCK)
        page = context.new_page()
        page.set_default_timeout(5000)
        requests = []
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def serve(route):
            request = route.request
            parsed = urlparse(request.url)
            requests.append((request.method, parsed.path))
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/fixture":
                source = parse_qs(parsed.query).get("src", [FRAME_PATH])[0]
                route.fulfill(content_type="text/html", body=(
                    '<main data-test-shell><iframe name="entry" width="1500" height="1000" '
                    f'src="{source}"></iframe></main>'
                ))
            elif parsed.path == FRAME_PATH:
                route.fulfill(content_type="text/html", body=render_frame(app, parse_qs(parsed.query)))
            elif parsed.path == "/waybills/manual":
                app._parse_urlencoded_form = Mock(return_value={
                    key: values[0] for key, values in parse_qs(request.post_data or "").items()
                })
                app._handle_manual_waybill(object())
                target = app._redirect_with_message.call_args.args[1]
                # Playwright does not re-route an intercepted redirect chain.
                # Exercise the server-selected target without any real network.
                route.fulfill(content_type="text/html", body=(
                    f"<script>location.replace({json.dumps(target)})</script>"
                ))
            elif parsed.path == "/waybills/41/print":
                route.fulfill(content_type="text/html", body=app.template_env.get_template(
                    "waybill_print.html"
                ).render(waybill=SAVED_ROW, autoprint=False, print_preview=False))
            elif parsed.path == "/ocr":
                route.fulfill(content_type="text/html", body='<main data-test-shell>录单工作台</main>')
            elif parsed.path in {
                "/static/js/clodop_loader.js", "/static/js/waybill_label_lodop.js",
                "/static/js/waybill_label_html.js",
            }:
                route.fulfill(content_type="text/javascript", body="")
            elif parsed.path.startswith("/static/"):
                asset = CONSOLE_DIR / parsed.path.lstrip("/")
                if asset.is_file():
                    route.fulfill(path=str(asset))
                else:
                    route.abort()
            else:
                route.abort()

        context.route("**/*", serve)
        yield SimpleNamespace(page=page, requests=requests, errors=page_errors)


def open_frame(page, source=FRAME_PATH):
    page.goto(ORIGIN + "/fixture?" + urlencode({"src": source}))
    frame = page.frame(name="entry")
    assert frame is not None
    return frame


def save_synthetic_waybill(frame):
    form_values = {
        "open_date": "2026-01-02", "destination_site": "测试市",
        "receiver_name": "合成收件人", "receiver_phone": "00000000000",
        "receiver_address": "测试省测试市合成路1号", "goods_name_lines": "合成货物",
        "package_type_lines": "纸箱", "quantity_lines": "2", "freight_fee": "12.30",
    }
    for name, value in form_values.items():
        frame.locator(f"#field_{name}").fill(value)
    frame.locator("#field_delivery_method").select_option("送货")
    frame.locator("#field_payment_method").select_option("寄付")
    frame.locator('[name="auto_print"]').check()
    frame.locator('[data-manual-form] button[type="submit"]').click()


def test_save_opens_native_preview_once_and_refresh_keeps_new_draft(app, browser_page):
    page = browser_page.page
    frame = open_frame(page)
    page.evaluate("""localStorage.setItem('shipnow.manual.printerSettings', JSON.stringify({
        printer_name: 'Synthetic Label Printer', print_copies: '2'
    }))""")
    save_synthetic_waybill(frame)
    page.wait_for_function("window.__printEvents.length === 1")

    assert page.evaluate("window.__printEvents") == ["preview"]
    assert frame.url == ORIGIN + FRAME_PATH
    assert len(page.frames) == 2
    assert frame.locator("[data-boyi-frame-root]").count() == 1
    assert frame.locator("[data-entry-tabs-root]").count() == 0
    assert frame.locator("#field_waybill_no").input_value() == "BY00042"
    assert frame.locator("#field_receiver_name").input_value() == ""
    payload = page.evaluate("window.__printPayloads[0]")
    assert payload["waybill_no"] == "BY00041"
    assert payload["sender_address"] == SAVED_ROW["sender_address"]
    assert payload["receipt_required"] is True
    assert page.evaluate("window.__printerIndexes") == [0]
    assert page.evaluate("window.__copies") == [2]
    assert page.evaluate("JSON.parse(localStorage.getItem('shipnow.manual.printerSettings')).printer_name") == "Synthetic Label Printer"
    assert "internal_test_marker" not in payload
    assert ("GET", "/waybills/41/print") not in browser_page.requests

    frame.goto(frame.url)
    assert frame.locator("[data-saved-manual-preview]").count() == 0
    assert page.evaluate("window.__printEvents") == ["preview"]
    app.service.apply_manual_waybill.assert_called_once()
    assert browser_page.requests.count(("POST", "/waybills/manual")) == 1
    assert not browser_page.errors


def test_failed_preview_can_retry_saved_snapshot_without_saving_again(app, browser_page):
    page = browser_page.page
    frame = open_frame(page)
    page.evaluate("window.__failPrint = true")
    save_synthetic_waybill(frame)
    feedback = frame.locator("[data-manual-feedback]")
    frame.wait_for_function("document.querySelector('[data-manual-feedback]')?.textContent.includes('勿重复保存')")
    assert "已保存" in feedback.inner_text()
    assert page.evaluate("window.__printEvents") == []
    frame.locator("#field_receiver_name").fill("下一单尚未保存")
    page.evaluate("window.__failPrint = false")
    frame.locator("[data-saved-manual-preview]").click()
    page.wait_for_function("window.__printEvents.length === 1")
    assert page.evaluate("window.__printEvents") == ["preview"]
    assert page.evaluate("window.__printPayloads[0].receiver_name") == SAVED_ROW["receiver_name"]
    assert frame.locator("#field_receiver_name").input_value() == "下一单尚未保存"
    app.service.apply_manual_waybill.assert_called_once()
    assert browser_page.requests.count(("POST", "/waybills/manual")) == 1
    assert not browser_page.errors


@pytest.mark.parametrize("failure", ("missing", "exception", "invalid-id"))
def test_failed_saved_read_stops_print_and_retains_entry(app, browser_page, failure):
    if failure == "exception":
        app.repository.get_waybill.side_effect = RuntimeError("private-database-diagnostic")
    else:
        app.repository.get_waybill.return_value = None
    saved_id = "invalid" if failure == "invalid-id" else "41"
    page = browser_page.page
    frame = open_frame(page, FRAME_PATH + "?saved_waybill_id=" + saved_id)
    assert "已停止打印" in frame.locator('[role="alert"]').inner_text()
    assert "private-database-diagnostic" not in frame.content()
    assert frame.locator("[data-saved-manual-preview]").count() == 0
    assert frame.locator("[data-boyi-frame-root]").count() == 1
    assert page.evaluate("window.__printEvents") == []
    app.service.apply_manual_waybill.assert_not_called()
    if failure == "invalid-id":
        app.repository.get_waybill.assert_not_called()
    else:
        app.repository.get_waybill.assert_called_once_with(41, source="manual")
    assert not browser_page.errors


@pytest.mark.parametrize("embedded", (True, False))
def test_old_print_page_return_uses_the_correct_navigation_level(browser_page, embedded):
    page = browser_page.page
    if embedded:
        target = open_frame(page, "/waybills/41/print?source=manual")
        expected_path = FRAME_PATH
    else:
        page.goto(ORIGIN + "/waybills/41/print?source=manual")
        target = page
        expected_path = "/ocr"
    target.get_by_role("link", name="返回录单").click()
    target.wait_for_url(ORIGIN + expected_path)
    if embedded:
        assert len(page.frames) == 2
        assert target.locator("[data-boyi-frame-root]").count() == 1
        assert target.locator("[data-entry-tabs-root]").count() == 0
    else:
        assert page.locator("[data-test-shell]").count() == 1
    assert page.evaluate("window.__printEvents") == []
    assert not browser_page.errors
