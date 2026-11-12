"""Signed rows show sign-for and receipt thumbnails that open the large images."""

import json
from urllib.parse import parse_qs, urlparse

from console.tests.test_manual_saved_print_flow import (
    CONSOLE_DIR,
    ORIGIN,
    PRINT_MOCK,
    SAVED_ROW,
    app as app,
    browser as browser,
)

JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c"
    "20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100"
    "000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114"
    "328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a63646566"
    "6768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3"
    "d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9")


def render_signed(app):
    rows = [{
        **SAVED_ROW, "id": 41, "source": source, "waybill_no": number, "source_label": "韵达寄件",
        "status": status, "status_label": "已签收" if status == "signed" else "运输中", "status_tone": "success",
        "opening_cost": "12.30", "print_data_url": "/waybills/41/print-data?source=yunda",
        "tracking_url": f"/tracking?tracking_number={number}",
    } for source, number, status in (("yunda", "900000001", "signed"), ("ronghui", "R900000002", "in_transit"))]
    return app.template_env.get_template("waybills.html").render(
        app_title="离线合成测试",
        filters={"q": "", "date_from": "2026-01-02", "date_to": "2026-01-02", "status": "all", "source": "all",
                 "payment_method": "", "delivery_method": "", "sort": "open_date_desc"},
        rows=rows, summary={"total": 2, "invalid_money_count": 0, "opening_cost_total": "24.60"},
        pagination={"page": 1, "page_size": 50, "total": 2, "total_pages": 10, "offset": 0,
                    "has_prev": False, "has_next": True},
        source_options=[{"value": "all", "label": "全部来源"}], status_options=[{"value": "all", "label": "全部状态"}],
        payment_options=[""], delivery_options=[""], sort_options=[{"value": "open_date_desc", "label": "倒序"}],
        current_url="/waybills", prev_url="", next_url="/waybills?page=2", db_error="", message="",
        page_links=[1, 2, 3, None, 10], page_url_base="/waybills?page=",
        sync_status={"messages": [], "warnings": []},
        source_refresh={"source": "all", "date_from": "2026-01-02", "date_to": "2026-01-02"},
    )


def test_signed_row_thumbnails_open_the_photo_viewer(app, browser):
    photo_requests, errors = [], []
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
            elif parsed.path == "/waybills/photos":
                photo_requests.append(parse_qs(parsed.query))
                route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {
                    "sign": ["/waybills/photo-image?platform=yunda&url=s1", "/waybills/photo-image?platform=yunda&url=s2"],
                    "receipt": ["/waybills/photo-image?platform=yunda&url=r1"], "receipt_error": ""}}))
            elif parsed.path == "/waybills/photo-image":
                route.fulfill(content_type="image/jpeg", body=JPEG)
            elif parsed.path.startswith("/static/") and (CONSOLE_DIR / parsed.path.lstrip("/")).is_file():
                route.fulfill(path=str(CONSOLE_DIR / parsed.path.lstrip("/")))
            else:
                route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/waybills")
        holder = page.locator('[data-waybill-photos][data-waybill-no="900000001"]')
        holder.locator("button.waybill-photo-slot").first.wait_for()

        assert [{"platform": ["yunda"], "waybill_no": ["900000001"]}] == photo_requests
        assert holder.locator("button.waybill-photo-slot").evaluate_all("els => els.map(e => e.dataset.kind)") == ["sign", "receipt"]
        assert holder.locator(".waybill-photo-count").all_inner_texts() == ["2"]
        unsigned = page.locator('tr[data-waybill-row="ronghui:41"] .waybill-photo-slot')
        assert unsigned.count() == 2 and page.locator('tr[data-waybill-row="ronghui:41"] button.waybill-photo-slot').count() == 0
        assert page.locator(".waybill-page-link[aria-current=page]").inner_text() == "1"
        assert page.locator(".waybill-page-gap").count() == 1

        holder.locator('button[data-kind="sign"]').click()
        dialog = page.locator("[data-waybill-photo-dialog]")
        dialog.locator("[data-waybill-photo-view] img").wait_for()
        assert dialog.locator("[data-waybill-photo-counter]").inner_text() == "第 1 / 2 张"
        assert dialog.locator("[data-waybill-photo-sub]").inner_text() == "900000001 · 韵达寄件 · " + SAVED_ROW["open_date"]
        page.keyboard.press("ArrowRight")
        assert dialog.locator("[data-waybill-photo-counter]").inner_text() == "第 2 / 2 张"
        assert dialog.locator('.waybill-photo-nav[data-step="1"]').is_disabled()
        dialog.locator('[data-waybill-photo-tab="receipt"]').click()
        assert dialog.locator("[data-waybill-photo-counter]").inner_text() == "第 1 / 1 张"
        assert dialog.locator('[data-waybill-photo-tab="receipt"] [data-count]').inner_text() == "1"
        assert "r1" in dialog.locator("[data-waybill-photo-download]").get_attribute("href")
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('[data-waybill-photo-dialog]').open")
        assert not errors
