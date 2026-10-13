"""Query-detail printing stays on the page and only opens native preview."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from console.database import MANUAL_WAYBILL_EDIT_FIELDS

from console.tests.test_manual_saved_print_flow import (
    CONSOLE_DIR,
    ORIGIN,
    PRINT_MOCK,
    SAVED_ROW,
    app as app,
    browser as browser,
)


def render_query(app, state=None):
    rows = [
        {
            **SAVED_ROW,
            "source": source,
            "waybill_no": "BY00041" if source == "manual" else "SYNTHETIC00041",
            "source_label": "博益开单" if source == "manual" else "韵达寄件",
            "created_at": "2026-01-02 10:00:00",
            "updated_at": "2026-01-02 11:00:00",
            **(state.saved_fields if state and source == "manual" else {}),
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
            "q": "synthetic", "date_from": "2026-01-02", "date_to": "2026-01-02", "status": "all",
            "source": "all", "payment_method": "", "delivery_method": "",
            "sort": "open_date_desc",
        },
        rows=rows,
        summary={"total": 2, "invalid_money_count": 0, "opening_cost_total": "24.60"},
        pagination={
            "page": 1, "page_size": 50, "total": 2, "total_pages": 2,
            "offset": 0, "has_prev": False, "has_next": False,
        },
        source_options=[{"value": "all", "label": "全部来源"}, {"value": "manual", "label": "博益开单"}],
        status_options=[{"value": "all", "label": "全部状态"}], payment_options=["", "寄付", "到付"],
        delivery_options=["", "送货", "自提"], sort_options=[{"value": "open_date_desc", "label": "按开单日期倒序"}, {"value": "open_date_asc", "label": "按开单日期正序"}],
        current_url="/waybills?q=synthetic", prev_url="", next_url="/waybills?q=synthetic&page=2", db_error="", message="",
        sync_status={"messages": [], "warnings": []},
        source_refresh={"source": "all", "date_from": "2026-01-02", "date_to": "2026-01-02"},
    )


@pytest.fixture
def query_page(app, browser):
    state = SimpleNamespace(
        status=200, payload=None, data_requests=[], errors=[], saved_fields={},
        list_requests=[], edit_requests=[], history_requests=[], source_requests=[],
        save_status=200, save_message="", source_complete=True,
        defer_refresh=False, source_pending=[],
    )
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
                state.list_requests.append(route.request.url)
                route.fulfill(content_type="text/html", body=render_query(app, state))
            elif parsed.path == "/waybills/41/edit-data":
                state.edit_requests.append((route.request.method, parsed.path, parse_qs(parsed.query)))
                values = {f"field_{key}": str(SAVED_ROW.get(key, "")) for key in MANUAL_WAYBILL_EDIT_FIELDS if key != "weight_volume"}
                values.update({f"field_{key}": value for key, value in state.saved_fields.items()})
                values.update(field_weight_kg="10", field_volume_m3="0.1")
                route.fulfill(content_type="application/json", body=json.dumps({
                    "ok": True, "waybill_no": "BY00041", "values": values,
                    "fingerprint": "a" * 64, "editable": True, "reason": "",
                    "payment_methods": ["寄付", "到付", "月结"], "delivery_methods": ["送货", "自提"],
                }))
            elif parsed.path == "/waybills/41/manual-update":
                payload = route.request.post_data_json
                state.edit_requests.append((route.request.method, parsed.path, payload, route.request.headers.get("x-browser-request-uuid")))
                if state.save_status == 200:
                    state.saved_fields = {key.removeprefix("field_"): value for key, value in payload["fields"].items()}
                route.fulfill(status=state.save_status, content_type="application/json", body=json.dumps({
                    "ok": state.save_status == 200, "outcome": "SAVED", "message": state.save_message,
                }))
            elif parsed.path == "/waybills/41/edit-history":
                state.history_requests.append(parse_qs(parsed.query))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "edits": [{
                    "actor": "合成操作人", "created_at": "2026-01-02 12:00", "changes": [{
                        "field": "remark", "label": "备注", "before": "原备注", "after": "<img src=x onerror=alert(1)>合成新备注",
                    }],
                }]}))
            elif parsed.path == "/waybills/source-refresh":
                state.source_requests.append(route.request.post_data_json)
                result = json.dumps({"ok": True, "data": {
                    "refreshed": True, "complete": state.source_complete, "messages": [],
                    "warnings": [] if state.source_complete else ["原平台数据未完整更新，当前为服务器快照。"],
                    "refreshed_at": "12:30", "reused": len(state.source_requests) > 1,
                }})
                if state.defer_refresh:
                    state.source_pending.append((route, result))
                else:
                    route.fulfill(content_type="application/json", body=result)
            elif parsed.path == "/waybills/41/print-data":
                state.data_requests.append((route.request.method, parsed.path, parse_qs(parsed.query)))
                source = parse_qs(parsed.query).get("source", [""])[0]
                payload = state.payload if state.payload is not None else {
                    "ok": True,
                    "waybill": {
                        **SAVED_ROW,
                        **(state.saved_fields if source == "manual" else {}),
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
    page.locator(f'[data-waybill-select="{source}:41"]').first.click()
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


def test_edit_save_rereads_list_reopens_details_and_prints_current_data(query_page):
    page = query_page.page
    detail = open_detail(page)
    assert query_page.history_requests == []
    detail.locator("[data-waybill-edit]").click()
    form = detail.locator("[data-waybill-edit-form]")
    form.wait_for(state="visible")
    assert form.locator('[name="field_weight_kg"]').input_value() == "10"
    assert form.locator('[name="field_volume_m3"]').input_value() == "0.1"
    assert not form.locator('[name="field_waybill_no"]').count()
    form.locator('[name="field_receiver_name"]').fill("修改后的合成收件人")
    form.get_by_role("button", name="保存修改", exact=True).click()
    page.wait_for_function("document.querySelector('[data-waybill-detail-dialog] [data-waybill-detail-message]')?.textContent.includes('已修改')")
    detail = page.locator("[data-waybill-detail-dialog]")
    assert detail.is_visible()
    assert "修改后的合成收件人" in detail.inner_text()
    assert len(query_page.list_requests) == 2
    saved = query_page.edit_requests[-1]
    assert saved[2]["request_id"] == saved[3]
    assert saved[2]["fingerprint"] == "a" * 64
    assert saved[2]["fields"]["field_receipt_required"] == "1"
    assert all(key.startswith("field_") for key in saved[2]["fields"])
    assert query_page.source_requests == []
    detail.locator("[data-waybill-print]").click()
    page.wait_for_function("window.__printEvents.length === 1")
    assert query_page.data_requests[-1][2] == {"source": ["manual"]}
    assert page.evaluate("window.__printPayloads[0].receiver_name") == "修改后的合成收件人"
    assert not query_page.errors


def test_edit_failure_keeps_values_and_retries_the_same_request(query_page):
    page = query_page.page
    detail = open_detail(page)
    detail.locator("[data-waybill-edit]").click()
    form = detail.locator("[data-waybill-edit-form]")
    form.locator('[name="field_remark"]').fill("保留的合成备注")
    query_page.save_status = 503
    query_page.save_message = "合成临时保存失败，请重试。"
    form.get_by_role("button", name="保存修改", exact=True).click()
    page.wait_for_function("document.querySelector('[data-waybill-detail-dialog] [data-waybill-detail-message]')?.textContent.includes('合成临时保存失败')")
    first = query_page.edit_requests[-1][2]
    assert form.locator('[name="field_remark"]').input_value() == "保留的合成备注"
    query_page.save_status = 200
    form.get_by_role("button", name="保存修改", exact=True).click()
    page.wait_for_function("document.querySelector('[data-waybill-detail-dialog] [data-waybill-detail-message]')?.textContent.includes('已修改')")
    assert query_page.edit_requests[-1][2] == first
    assert not query_page.errors


def test_history_loads_once_on_demand_and_escapes_saved_changes(query_page):
    page = query_page.page
    detail = open_detail(page)
    assert query_page.history_requests == []
    detail.locator("[data-waybill-history] summary").click()
    page.wait_for_function("document.querySelector('[data-waybill-history-body]')?.textContent.includes('合成新备注')")
    assert query_page.history_requests == [{"source": ["manual"]}]
    assert detail.locator("[data-waybill-history-body] img").count() == 0
    detail.locator("[data-waybill-history] summary").click()
    detail.locator("[data-waybill-history] summary").click()
    assert len(query_page.history_requests) == 1
    detail.locator("[data-waybill-detail-close]").click()
    platform = open_detail(page, "yunda")
    assert platform.locator("[data-waybill-edit]").count() == 0
    assert platform.locator("[data-waybill-history]").count() == 0
    assert "付款状态" not in platform.inner_text()
    assert "内部备注" not in platform.inner_text()
    assert "费用合计" not in platform.inner_text()
    assert not query_page.errors


def test_only_query_refreshes_sources_and_reuse_status_survives_navigation(query_page):
    page = query_page.page
    assert query_page.source_requests == []
    page.locator("[data-waybill-query]").click()
    page.wait_for_function("document.querySelector('[data-waybill-refresh-status]')?.textContent.includes('已按原平台更新')")
    assert query_page.source_requests == [{"source": "all", "date_from": "2026-01-02", "date_to": "2026-01-02"}]
    assert len(query_page.list_requests) == 3
    page.locator("[data-waybill-sort]").select_option("open_date_asc")
    page.wait_for_function("location.search.includes('sort=open_date_asc')")
    page.locator("[data-waybill-page-size]").select_option("20")
    page.wait_for_function("location.search.includes('page_size=20')")
    page.get_by_role("link", name="下一页", exact=True).click()
    page.wait_for_function("location.search.includes('page=2')")
    assert len(query_page.source_requests) == 1
    assert "已按原平台更新" in page.locator("[data-waybill-refresh-status]").inner_text()
    page.locator("[data-waybill-query]").click()
    page.wait_for_function("document.querySelector('[data-waybill-refresh-status]')?.textContent.includes('复用两分钟内')")
    assert len(query_page.source_requests) == 2
    assert not query_page.errors


def test_incomplete_source_refresh_shows_warning_after_list_reload(query_page):
    query_page.source_complete = False
    page = query_page.page
    page.locator("[data-waybill-query]").click()
    page.wait_for_function("document.querySelector('[data-waybill-refresh-status]')?.classList.contains('notice--warning')")
    assert "原平台数据未完整更新" in page.locator("[data-waybill-refresh-status]").inner_text()
    assert len(query_page.list_requests) == 3
    assert not query_page.errors


def test_completed_backfill_preserves_an_unsaved_edit(query_page):
    query_page.defer_refresh = True
    page = query_page.page
    page.locator("[data-waybill-query]").click()
    page.wait_for_function("document.querySelector('[data-waybill-refresh-status]')?.textContent.includes('正在补查')")
    detail = open_detail(page)
    detail.locator("[data-waybill-edit]").click()
    form = detail.locator("[data-waybill-edit-form]")
    form.locator('[name="field_remark"]').fill("不能丢失的合成备注")
    route, result = query_page.source_pending.pop()
    route.fulfill(content_type="application/json", body=result)
    page.wait_for_function("document.querySelector('[data-waybill-refresh-status]')?.textContent.includes('已按原平台更新')")
    assert form.locator('[name="field_remark"]').input_value() == "不能丢失的合成备注"
    assert len(query_page.list_requests) == 2
    detail.locator("[data-waybill-detail-close]").click()
    page.wait_for_function("!document.querySelector('[data-waybill-detail-dialog]')?.open")
    assert not query_page.errors


@pytest.mark.parametrize("width,height", [(1600, 1100), (1024, 900), (375, 812), (320, 740), (667, 375)])
def test_query_and_edit_are_usable_without_page_overflow(query_page, width, height):
    page = query_page.page
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("document.fonts.ready")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    output = CONSOLE_DIR.parent / ".task_tmp" / "codex-waybill-ui-20261013"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"query-{width}.png"), full_page=True)
    detail = open_detail(page)
    assert detail.evaluate("node => node.scrollWidth <= node.clientWidth")
    page.screenshot(path=str(output / f"detail-{width}.png"))
    detail.locator("[data-waybill-edit]").click()
    form = detail.locator("[data-waybill-edit-form]")
    form.wait_for(state="visible")
    assert form.evaluate("node => node.scrollWidth <= node.clientWidth")
    assert detail.evaluate("node => node.scrollWidth <= node.clientWidth")
    assert form.get_by_role("button", name="保存修改", exact=True).is_visible()
    page.screenshot(path=str(output / f"edit-{width}.png"))
    assert not query_page.errors
