"""Customer share page (spec ⑩a / ⑩b) in a real browser against stubbed endpoints."""
import json
from urllib.parse import parse_qs, urlparse

from console.tests.test_manual_saved_print_flow import CONSOLE_DIR, ORIGIN, app as app, browser as browser

SHARE = {
    "id": 3, "record_version": 2, "customer_id": 1, "customer_name": "勇胜贸易有限公司", "customer_code": "KH00001",
    "url": "https://r.example.test/r/AbCdEfGhIjKlMnOpQrStUv", "naming_template": "{客户简称}-{运单号}-{单据类型}.jpg",
    "naming_label": "默认规则", "image_types": ["sign", "receipt"], "image_types_text": "签收图·回单图", "scope": "all",
    "expires_text": "至 2026-12-31", "view_count": 128, "last_viewed_text": "11-13 14:02", "state": "active",
    "state_label": "生效中", "has_access_code": True, "access_code": "Ys7#mQ2&kP", "item_count": 12, "refresh_state": "ok",
    "refresh_message": "运单 5 张，图片 12 张", "last_refreshed_text": "11-13 18:00",
}
CUSTOMER = {"id": 1, "code": "KH00001", "name": "勇胜贸易有限公司", "short_name": "勇胜", "is_active": True,
            "match": {"mode": "any", "conditions": [{"field": "sender_name", "op": "eq", "value": "勇胜"}]}}
DEFAULTS = {"naming_template": "{客户简称}-{运单号}-{单据类型}.jpg",
            "naming_fields": ["客户简称", "运单号", "单据类型", "开单日期", "发货单号"],
            "image_kinds": [{"value": "sender_label", "label": "寄件单"}, {"value": "sign", "label": "签收图"},
                            {"value": "receipt", "label": "回单图"}],
            "match_fields": [{"value": "sender_name", "label": "发货人"}, {"value": "sender_phone", "label": "发货电话"},
                             {"value": "sender_address", "label": "发货地址"}]}


def _listing(shares):
    return {"configured": True, "shares": shares, "customers": [CUSTOMER], "defaults": DEFAULTS}


def test_list_create_and_disable(app, browser):
    state = {"shares": [dict(SHARE)], "posts": [], "errors": []}
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=ORIGIN)
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: state["errors"].append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        def serve(route):
            parsed, request = urlparse(route.request.url), route.request
            if parsed.netloc != "console.test":
                return route.abort()
            if parsed.path == "/receipts/shares" and request.method == "GET":
                body = app.template_env.get_template("receipt_shares.html").render(app_title="测试", listing=_listing(state["shares"]))
                return route.fulfill(content_type="text/html", body=body)
            if parsed.path == "/receipts/shares/data":
                return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": _listing(state["shares"])}))
            if parsed.path == "/receipts/shares/access-code":
                return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {"access_code": "Kp4$wz8Hn!"}}))
            if parsed.path == "/receipts/shares/naming-example":
                template = parse_qs(parsed.query)["template"][0]
                if "{运单号}" not in template:
                    return route.fulfill(status=400, content_type="application/json",
                                         body=json.dumps({"ok": False, "message": "命名规则必须包含 {运单号} 和 {单据类型}"}))
                return route.fulfill(content_type="application/json", body=json.dumps(
                    {"ok": True, "data": {"example": "勇胜-R00024388425-签收图.jpg"}}))
            if request.method == "POST":
                payload = request.post_data_json
                state["posts"].append((parsed.path, payload, request.headers.get("x-browser-request-uuid")))
                if parsed.path == "/receipts/shares":
                    created = {**SHARE, "id": 4, "url": "https://r.example.test/r/NewLinkCodeNewLinkCode1", "has_access_code": True}
                    state["shares"].insert(0, created)
                    return route.fulfill(content_type="application/json", body=json.dumps(
                        {"ok": True, "data": {"share": created, "access_code": payload["access_code"]}}))
                if parsed.path.endswith("/renew"):
                    return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {"share": state["shares"][-1]}}))
                if parsed.path.endswith("/disable"):
                    state["shares"][-1].update(state="disabled", state_label="已停用", record_version=3)
                    return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {"share": state["shares"][-1]}}))
            path = CONSOLE_DIR / parsed.path.lstrip("/")
            if parsed.path.startswith("/static/") and path.is_file():
                return route.fulfill(path=str(path))
            return route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/receipts/shares")
        row = page.locator("[data-share-rows] tr").first
        cells = row.locator("td").all_inner_texts()
        assert cells[0].split("\n") == ["勇胜贸易有限公司", "KH00001"] and cells[1] == "r.example.test/r/AbCdEfGhIjKlMnOpQrStUv"
        assert cells[3:8] == ["签收图·回单图", "至 2026-12-31", "128 次", "生效中", "11-13 14:02"]
        assert page.locator("[data-share-count]").inner_text() == "共 1 条"
        widths = page.locator(".share-table col").evaluate_all("cols => cols.map(c => parseInt(c.style.width))")
        assert widths == [168, 168, 156, 152, 104, 64, 76, 92, 82] and sum(widths) == 1062

        row.get_by_role("button", name="复制").click()
        assert page.evaluate("navigator.clipboard.readText()") == SHARE["url"] + "\n访问码：Ys7#mQ2&kP"

        page.locator(".share-card-head [data-share-open-create]").click()
        dialog = page.locator("[data-share-create-dialog]")
        box = dialog.bounding_box()
        assert abs(box["x"] + box["width"] / 2 - 720) < 4 and abs(box["y"] + box["height"] / 2 - 450) < 4 and box["width"] == 720
        page.wait_for_function("document.querySelector('[data-share-example]').textContent === '勇胜-R00024388425-签收图.jpg'")
        kinds = dialog.locator("[data-share-kind]")
        # The boxes themselves must be visible and clickable (style.css resets inputs to appearance:none).
        for box in kinds.all() + dialog.locator('input[name="scope"]').all():
            assert box.evaluate("node => getComputedStyle(node).appearance") != "none"
            size = box.bounding_box()
            assert size and size["width"] >= 14 and size["height"] >= 14
        # A previous link of this customer pre-fills its image types.
        assert kinds.evaluate_all("boxes => boxes.map(b => [b.value, b.checked])") == [["sender_label", False], ["sign", True], ["receipt", True]]
        submit = dialog.locator("[data-share-create-submit]")
        kinds.nth(1).click(); kinds.nth(2).click()  # click the boxes directly
        assert not kinds.nth(1).is_checked() and not kinds.nth(2).is_checked()
        assert submit.is_disabled()  # at least one image type
        kinds.nth(2).click()
        dialog.locator("[data-share-template]").fill("{客户简称}.jpg")
        page.wait_for_function("document.querySelector('[data-share-create-feedback]').dataset.kind === 'error'")
        dialog.locator("[data-share-template]").fill(DEFAULTS["naming_template"])
        dialog.locator("[data-share-code-random]").click()
        page.wait_for_function("document.querySelector('[data-share-code]').value === 'Kp4$wz8Hn!'")
        code = dialog.locator("[data-share-code]").input_value()
        assert dialog.locator("[data-share-custom-days]").is_hidden() and dialog.locator("[data-share-dates]").is_hidden()
        dialog.get_by_text("按发货日期").click()
        assert dialog.locator("[data-share-dates]").is_visible()
        dialog.get_by_text("全部回单").click()
        dialog.get_by_text("自定义").click()
        dialog.locator("[data-share-custom-days-input]").fill("45")
        submit.click()
        page.wait_for_function("!document.querySelector('[data-share-create-dialog]').open")
        path, body, uuid = state["posts"][-1]
        assert path == "/receipts/shares" and uuid == body["request_id"]
        assert {key: body[key] for key in ("customer_id", "scope", "image_types", "expires_days", "access_code", "naming_template")} == {
            "customer_id": 1, "scope": "all", "image_types": ["receipt"], "expires_days": 45, "access_code": code,
            "naming_template": DEFAULTS["naming_template"]}
        assert page.evaluate("navigator.clipboard.readText()") == f"https://r.example.test/r/NewLinkCodeNewLinkCode1\n访问码：{code}"
        assert page.locator("[data-share-rows] tr").count() == 2

        page.locator("[data-share-rows] tr").nth(1).get_by_role("button", name="停用").click()
        page.wait_for_function("document.querySelectorAll('.share-state--disabled').length === 1")
        assert state["posts"][-1][0] == "/receipts/shares/3/disable" and state["posts"][-1][1]["record_version"] == 2
        assert page.locator("[data-share-rows] tr").nth(1).get_by_role("button", name="启用").count() == 1

        state["shares"][-1].update(state="expired", state_label="已过期", record_version=4)
        page.evaluate("1")  # keep the page alive while the stub changes
        page.locator("[data-share-search]").fill("")
        page.locator("[data-share-rows] tr").first.evaluate("n => n")
        page.reload()
        expired = page.locator("[data-share-rows] tr").nth(1)
        expired.get_by_role("button", name="续期").click()
        renew = page.locator("[data-share-renew-dialog]")
        renew.locator("[data-share-renew-days]").fill("120")
        renew.get_by_role("button", name="确定续期").click()
        page.wait_for_function("document.querySelector('[data-share-toast]').textContent === '已续期 120 天'")
        assert state["posts"][-1][0] == "/receipts/shares/3/renew" and state["posts"][-1][1]["expires_days"] == 120
        expired.get_by_role("button", name="续期").click()
        renew.get_by_text("长期有效").click()
        renew.get_by_role("button", name="确定续期").click()
        page.wait_for_function("document.querySelector('[data-share-toast]').textContent === '已改为长期有效'")
        assert state["posts"][-1][1]["expires_days"] == 0
        state["shares"][-1].update(state="disabled", state_label="已停用")
        page.reload()
        page.locator("[data-share-state-filter]").select_option("disabled")
        assert page.locator("[data-share-rows] tr").count() == 1
        assert state["errors"] == []


def test_customer_rules_dialog_and_empty_state(app, browser):
    posts, errors = [], []
    with browser.new_context(viewport={"width": 1280, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed, request = urlparse(route.request.url), route.request
            if parsed.netloc != "console.test":
                return route.abort()
            if parsed.path == "/receipts/shares" and request.method == "GET":
                return route.fulfill(content_type="text/html", body=app.template_env.get_template("receipt_shares.html").render(
                    app_title="测试", listing={**_listing([]), "customers": []}))
            if parsed.path == "/receipts/shares/data":
                return route.fulfill(content_type="application/json", body=json.dumps(
                    {"ok": True, "data": _listing([]) if posts else {**_listing([]), "customers": []}}))
            if parsed.path == "/receipts/shares/senders":
                return route.fulfill(content_type="application/json", body=json.dumps(
                    {"ok": True, "data": [{"sender_name": "勇胜", "sender_phone": "07315186128", "waybills": 26}]}))
            if request.method == "POST":
                posts.append((parsed.path, request.post_data_json))
                data = {"matching_waybills": 26, "capped": False, "sample": ["R00024852187"]} if parsed.path.endswith("/preview") \
                    else {"customer_id": 1, "matching_waybills": 26}
                return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": data}))
            path = CONSOLE_DIR / parsed.path.lstrip("/")
            if parsed.path.startswith("/static/") and path.is_file():
                return route.fulfill(path=str(path))
            return route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/receipts/shares")
        assert page.locator("[data-share-empty]").is_visible() and "还没有共享链接" in page.locator("[data-share-empty]").inner_text()
        page.locator(".share-card-head [data-share-open-customers]").click()
        dialog = page.locator("[data-share-customer-dialog]")
        assert dialog.locator("[data-share-customer-switch]").is_hidden()  # nothing to choose yet
        assert dialog.locator("[data-share-customer-mode]").inner_text() == "新建客户"
        mode_any = dialog.locator('input[name="match_mode"][value="any"]')
        assert mode_any.evaluate("node => getComputedStyle(node).appearance") != "none" and mode_any.is_checked()
        name_label = dialog.locator(".share-labelled").first
        label_box = name_label.locator("span").bounding_box()
        input_box = name_label.locator("input").bounding_box()
        assert input_box["y"] > label_box["y"] and abs(input_box["x"] - label_box["x"]) < 2  # label above its input
        dialog.locator("[data-share-customer-name]").fill("勇胜贸易有限公司")
        dialog.locator("[data-share-customer-short]").fill("勇胜")
        dialog.locator("[data-share-sender-keyword]").fill("勇胜")
        dialog.locator("[data-share-sender-search]").click()
        dialog.get_by_role("button", name="按名称加规则").click()
        dialog.locator(".share-rule").first.get_by_role("button", name="删除这条规则").click()  # the empty starter rule
        dialog.locator("[data-share-rule-preview]").click()
        page.wait_for_function("document.querySelector('[data-share-customer-feedback]').textContent.includes('26 张运单')")
        dialog.get_by_role("button", name="保存客户").click()
        page.wait_for_function("document.querySelector('[data-share-customer-feedback]').textContent.includes('已保存')")
        # After saving, the picker appears with the customer selected, plus a button to start a new one.
        assert dialog.locator("[data-share-customer-switch]").is_visible()
        assert dialog.locator("[data-share-customer-mode]").inner_text() == "正在修改：勇胜贸易有限公司"
        dialog.locator("[data-share-customer-new]").click()
        assert dialog.locator("[data-share-customer-mode]").inner_text() == "新建客户"
        assert dialog.locator("[data-share-customer-name]").input_value() == ""
        path, body = posts[-1]
        assert path == "/receipts/shares/customers"
        assert {key: body[key] for key in ("customer_id", "name", "short_name", "match")} == {
            "customer_id": None, "name": "勇胜贸易有限公司", "short_name": "勇胜",
            "match": {"mode": "any", "conditions": [{"field": "sender_name", "op": "eq", "value": "勇胜"}]}}
        assert errors == []


def test_share_button_on_the_receipts_page_opens_its_own_tab(app, browser):
    """Regression: /receipts/shares shared the receipts tab key, so the click only re-activated the receipts tab."""
    from console.tests.test_receipts_management import _Handler, _ReceiptRepo, _build_app

    receipts_app = _build_app(_ReceiptRepo())
    handler = _Handler()
    handler.current_admin_user = {"id": 1, "username": "admin", "role": "super_admin"}
    receipts_app._render_receipts(handler, {})
    receipts_html = handler.wfile.getvalue().decode("utf-8")
    assert 'href="/receipts/shares" data-console-navigate' in receipts_html
    share_html = app.template_env.get_template("receipt_shares.html").render(app_title="测试", listing=_listing([dict(SHARE)]))
    errors = []
    with browser.new_context(viewport={"width": 1440, "height": 900}) as context:
        page = context.new_page()
        page.set_default_timeout(5000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed = urlparse(route.request.url)
            if parsed.netloc != "console.test":
                return route.abort()
            if parsed.path == "/receipts":
                return route.fulfill(content_type="text/html", body=receipts_html)
            if parsed.path == "/receipts/shares":
                return route.fulfill(content_type="text/html", body=share_html)
            path = CONSOLE_DIR / parsed.path.lstrip("/")
            if parsed.path.startswith("/static/") and path.is_file():
                return route.fulfill(path=str(path))
            return route.fulfill(status=404, body="")

        context.route("**/*", serve)
        page.goto(f"{ORIGIN}/receipts")
        page.locator('a[href="/receipts/shares"]').click()
        page.wait_for_function("location.pathname === '/receipts/shares'")
        # The page script runs after the tab is inserted; wait for its first render.
        page.locator(".main-content:not([hidden]) [data-share-rows] tr").first.wait_for()
        assert page.locator(".main-content:not([hidden]) [data-share-rows] tr").count() == 1
        # The receipts page stays open in its own tab beside it.
        assert page.locator(".main-content [data-receipts-page]").count() == 1
        assert errors == []
