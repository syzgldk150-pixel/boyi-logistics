"""Query-detail printing stays on the page and only opens native preview."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from console.tests.test_manual_saved_print_flow import (
    CONSOLE_DIR,
    ORIGIN,
    PRINT_MOCK,
    SAVED_ROW,
    app as app,
    browser as browser,
)


def render_query(app):
    rows = [
        {
            **SAVED_ROW,
            "source": source,
            "waybill_no": "BY00041" if source == "manual" else "SYNTHETIC00041",
            "source_label": "合成来源",
            "status": "pending",
            "status_label": "待发货",
            "status_tone": "muted",
            "opening_cost": "12.30",
            "pickup_payment_amount": "0.00",
            "print_data_url": f"/waybills/41/print-data?source={source}",
            "tracking_url": "/tracking?tracking_number=SYNTHETIC00041",
        }
        for source in ("manual", "yunda")
    ]
    return app.template_env.get_template("waybills.html").render(
        app_title="离线合成测试",
        filters={
            "q": "synthetic", "date_from": "", "date_to": "", "status": "all",
            "source": "all", "payment_method": "", "delivery_method": "",
            "sort": "open_date_desc",
        },
        rows=rows,
        summary={"total": 2, "invalid_money_count": 0, "opening_cost_total": "24.60"},
        pagination={
            "page": 1, "page_size": 50, "total": 2, "total_pages": 1,
            "offset": 0, "has_prev": False, "has_next": False,
        },
        source_options=[], status_options=[], payment_options=[], delivery_options=[], sort_options=[],
        current_url="/waybills?q=synthetic", prev_url="", next_url="", db_error="", message="",
    )


@pytest.fixture
def query_page(app, browser):
    state = SimpleNamespace(status=200, payload=None, data_requests=[], errors=[])
    with browser.new_context(viewport={"width": 1600, "height": 1100}) as context:
        context.add_init_script(PRINT_MOCK)
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: state.errors.append(str(error)))

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                route.abort()
            elif parsed.path == "/waybills":
                route.fulfill(content_type="text/html", body=render_query(app))
            elif parsed.path == "/waybills/41/print-data":
                state.data_requests.append((route.request.method, parsed.path, parse_qs(parsed.query)))
                source = parse_qs(parsed.query).get("source", [""])[0]
                payload = state.payload if state.payload is not None else {
                    "ok": True,
                    "waybill": {
                        **SAVED_ROW,
                        "source": source,
                        "waybill_no": "BY00041" if source == "manual" else "SYNTHETIC00041",
                        "receipt_required": True,
                    },
                }
                route.fulfill(status=state.status, content_type="application/json", body=json.dumps(payload))
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
        page.goto(ORIGIN + "/waybills?q=synthetic")
        page.evaluate("""localStorage.setItem('shipnow.manual.printerSettings', JSON.stringify({
            printer_name: 'Synthetic Label Printer', print_copies: '2'
        }))""")
        state.page = page
        yield state


def open_detail(page, source="manual"):
    page.locator(f'[data-waybill-select="{source}:41"]').click()
    detail = page.locator("[data-waybill-detail-dialog]")
    detail.wait_for(state="visible")
    return detail


@pytest.mark.parametrize("source", ("manual", "yunda"))
def test_query_print_fetches_exact_source_and_opens_preview_without_navigation(query_page, source):
    page = query_page.page
    original_url = page.url
    detail = open_detail(page, source)
    detail.locator("[data-waybill-print]").click()
    page.wait_for_function("window.__printEvents.length === 1")

    assert query_page.data_requests == [("GET", "/waybills/41/print-data", {"source": [source]})]
    assert page.evaluate("window.__printEvents") == ["preview"]
    assert page.evaluate("window.__printerIndexes") == [0]
    assert page.evaluate("window.__copies") == [2]
    assert page.evaluate("JSON.parse(localStorage.getItem('shipnow.manual.printerSettings')).printer_name") == "Synthetic Label Printer"
    assert page.evaluate("window.__printPayloads[0].waybill_no") == (
        "BY00041" if source == "manual" else "SYNTHETIC00041"
    )
    assert page.url == original_url
    assert len(page.context.pages) == 1
    assert detail.is_visible()
    assert not query_page.errors


@pytest.mark.parametrize("failure", ("data-error", "missing-data", "clodop"))
def test_query_preview_failure_stays_in_details_and_can_retry(query_page, failure):
    page = query_page.page
    original_url = page.url
    if failure == "data-error":
        query_page.status = 503
        query_page.payload = {"ok": False, "error": "合成打印数据读取失败"}
    elif failure == "missing-data":
        query_page.payload = {"ok": True}
    else:
        page.evaluate("window.__failPrint = true")
    detail = open_detail(page)
    button = detail.locator("[data-waybill-print]")
    button.click()
    page.wait_for_function("""() => {
        const button = document.querySelector('[data-waybill-detail-dialog] [data-waybill-print]');
        const status = document.querySelector('[data-waybill-detail-dialog] [data-waybill-print-status]');
        return button && !button.disabled && status && status.textContent.trim();
    }""")
    assert page.evaluate("window.__printEvents") == []
    assert detail.is_visible()
    assert page.url == original_url

    query_page.status = 200
    query_page.payload = None
    page.evaluate("window.__failPrint = false")
    button.click()
    page.wait_for_function("window.__printEvents.length === 1")
    assert page.evaluate("window.__printEvents") == ["preview"]
    assert len(query_page.data_requests) == 2
    assert page.url == original_url
    assert len(page.context.pages) == 1
    assert not query_page.errors
