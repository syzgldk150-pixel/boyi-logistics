"""财务“派件账单”页面在 Chromium 里对合成的 Agent 回复（数据虚构）：汇总、勾选确认、增值费编辑、派送费规则。"""
import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest

from console.tests import test_handbook_console as handbook_console
from shared.finance.delivery_bills import (
    build_bill,
    delivery_fee,
    describe_rule,
    parse_rule_form,
    segment_examples,
    settlement_entries,
)
from shared.finance.errors import FinanceValidationError

CONSOLE_DIR = Path(__file__).resolve().parents[1]
ORIGIN = "https://console.test"
SHOTS = os.environ.get("HANDBOOK_SCREENSHOT_DIR")


def _bill(items=None):
    content = parse_rule_form({"effective_from": "2026-11-01", "rounding": "exact", "segments": [
        {"from": "0", "first_weight": "30", "first_price": "10", "extra_price": "0.5"}]})["content"]
    rows = [
        {"BILL_CODE": "R001", "BALANCE_TYPE": "付派送费", "BALANCE_CUR_MONEY": "40.5", "BALANCE_DATE": "2026-11-05",
         "SETTLEMENT_WEIGHT": "45.5", "PIECE_NUMBER": "2", "BL_DEL": "否"},
        {"BILL_CODE": "R001", "BALANCE_TYPE": "派到付款", "BALANCE_CUR_MONEY": "-300", "BALANCE_DATE": "2026-11-05",
         "SETTLEMENT_WEIGHT": "45.5", "PIECE_NUMBER": "2", "BL_DEL": "否"},
        {"BILL_CODE": "R002", "BALANCE_TYPE": "付派送费", "BALANCE_CUR_MONEY": "1234.5", "BALANCE_DATE": "2026-11-05",
         "SETTLEMENT_WEIGHT": "20", "PIECE_NUMBER": "1", "BL_DEL": "否"},
        {"BILL_CODE": "R009", "BALANCE_TYPE": "付派送费", "BALANCE_CUR_MONEY": "25", "BALANCE_DATE": "2026-11-06",
         "SETTLEMENT_WEIGHT": "20", "PIECE_NUMBER": "1", "BL_DEL": "否"},
    ]
    arrivals = [{"tracking_number": "R001", "arrival_date": "2026-11-04", "goods_name": "家具", "delivery_method": "派送"},
                {"tracking_number": "R002", "arrival_date": "2026-11-04", "goods_name": "百货", "delivery_method": "自提"},
                {"tracking_number": "R003", "arrival_date": "2026-11-05", "goods_name": "床垫", "delivery_method": "派送"}]
    bill = build_bill(start=dt.date(2026, 11, 1), end=dt.date(2026, 11, 24), arrivals=arrivals,
                      settlements=settlement_entries(rows), known_arrivals=["R001", "R002", "R003"],
                      missing_dates=["2026-11-20"], settings={"include_types": [], "exclude_types": []},
                      rules=[{"id": 5, "effective_from": "2026-11-01", "status": "active", "content": content}],
                      items=items or {})
    return {**bill, "snapshot_id": "snap-1", "station": "邵阳大祥S站", "start": "2026-11-01", "end": "2026-11-24",
            "settlement_window": {"start": "2026-11-01", "end": "2026-11-24"}, "fetched_at": "2026-11-24 12:00:00",
            "settings": {"lookahead_days": 10, "include_types": [], "exclude_types": []}}


PREPARE = {
    "tracking_number": "R001", "billing_date": "2026-11-03",
    "facts": {"settlement_weight": "45.5", "pieces": 2, "actual_weight": "44"},
    "fees": [{"fee_kind": "upstairs", "name": "上楼费", "version": "V6.1（执行日期 2026-01-01）", "group": "上楼卸货",
              "params": ["floors", "settlement_weight"], "reminders": []}],
    "params": [{"key": "floors", "label": "楼层", "type": "number", "required": True, "integer": True, "decimals": 0},
               {"key": "settlement_weight", "label": "结算重量", "type": "number", "unit": "kg", "required": True,
                "integer": False, "decimals": 4, "prefill": "settlement_weight"}],
    "prefill": {"settlement_weight": "45.5"}, "prefill_note": "", "unavailable": [], "conflict": "", "row_status": "pending",
    "saved": {"fee_kinds": [], "values": {}, "manual": {}, "lines": [], "revision": None, "billing_date": None},
}
# 规则与试算的回复用真实的 shared 规则函数生成（数字虚构），页面上的“算出来”与 Agent 同一算法。
RULE_SEGMENTS = [{"from": "0", "first_weight": "1", "first_price": "5", "extra_price": "1"},
                 {"from": "50", "first_weight": "50", "first_price": "30", "extra_price": "0.5"}]


def _saved_rule():
    parsed = parse_rule_form({"effective_from": "2026-11-01", "segments": RULE_SEGMENTS, "rounding": "exact"})
    rule = {"id": 5, "effective_from": "2026-11-01", "status": "active", "segments": RULE_SEGMENTS, "rounding": "exact",
            "content": parsed["content"], "confirmed_count": 0, "created_by": "超管",
            "created_at": "2026-11-24 10:00:00", "revision": 1, "note": None}
    rule["description"], rule["examples"] = describe_rule(rule), segment_examples(rule)
    del rule["content"]
    return rule


def _trial(body):
    parsed = parse_rule_form({"effective_from": "2026-11-26", **body["rule"]})
    result = {"examples": segment_examples(parsed)}
    if body.get("settlement_weight"):
        amount, steps = delivery_fee(parsed, Decimal(body["settlement_weight"]))
        result.update(amount=format(amount, "f"), steps=steps)
    return result


LINE = {"position": 0, "fee_kind": "upstairs", "fee_name": "上楼费", "rule_id": "r-up", "version": "V6.1", "group": "上楼卸货",
        "outcome": "AMOUNT", "outcome_text": "金额", "system_amount": "14.5500", "amount": "14.5500", "counted": True,
        "note": "", "reason": "", "steps": ["10 + 0.1 × 45.5 = 14.55"], "reminders": []}


@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("Chromium is required for bill page regressions")
        with runtime.chromium.launch() as instance:
            yield instance


def _render(role="super_admin"):
    from console import app_support
    app, _ = handbook_console._ConsoleCase()._app(lambda *a: None)
    token = app_support._CURRENT_ADMIN_USER.set({"id": 1, "role": role, "is_legacy_basic_auth": False,
                                                 "access_permissions": ["finance:view", "finance:operate"]})
    try:
        app._render_delivery_bills(handbook_console._handler({}))
    finally:
        app_support._CURRENT_ADMIN_USER.reset(token)
    return app.sent_html


def _open(browser, posts):
    html = _render()
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())

    def serve(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != "console.test":
            return route.abort()
        if parsed.path == "/modules/finance/delivery-bills":
            return route.fulfill(content_type="text/html", body=html)
        if parsed.path.startswith("/static/"):
            file = CONSOLE_DIR / parsed.path.lstrip("/")
            return route.fulfill(path=str(file)) if file.is_file() else route.fulfill(status=404, body="")
        if parsed.path == "/finance/delivery-bills":
            return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": _bill()}, ensure_ascii=False))
        if parsed.path == "/finance/delivery-bills/rules":
            rules = [_saved_rule()]
            if route.request.method == "GET":
                return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {"rules": rules}}, ensure_ascii=False))
        if parsed.path.startswith("/finance/delivery-bills/"):
            action = parsed.path.removeprefix("/finance/delivery-bills/")
            body = json.loads(route.request.post_data or "{}")
            posts.append((action, body, route.request.headers.get("x-browser-request-uuid")))
            if action == "rules/trial":
                try:
                    data = _trial(body)
                except FinanceValidationError as exc:
                    return route.fulfill(status=400, content_type="application/json",
                                         body=json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False))
                return route.fulfill(content_type="application/json",
                                     body=json.dumps({"ok": True, "data": data}, ensure_ascii=False))
            reply = {"fees/prepare": PREPARE,
                     "fees/compute": {"tracking_number": "R001", "billing_date": "2026-11-03", "lines": [LINE], "total": "14.5500",
                                      "complete": True, "missing": []},
                     "fees/save": {"tracking_number": "R001", "revision": 1, "value_added_total": "14.5500",
                                   "bill": _bill({"R001": {"status": "open", "value_added_total": "14.5500", "revision": 1, "lines": [LINE]}})},
                     "confirm": {"results": [{"tracking_number": "R001", "ok": True}, {"tracking_number": "R002", "ok": True}],
                                 "succeeded": 2, "failed": 0, "bill": _bill()},
                     "rules": {"rule_id": 6, **{"rules": [_saved_rule()]}}}.get(action)
            if reply is None:
                return route.fulfill(status=404, body="")
            return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": reply}, ensure_ascii=False))
        return route.fulfill(status=404, body="")

    page.route("**/*", serve)
    page.goto(f"{ORIGIN}/modules/finance/delivery-bills")
    return context, page, errors


def test_bill_summary_rows_and_notices(browser):
    posts = []
    context, page, errors = _open(browser, posts)
    with context:
        page.click("[data-db-query]")
        page.wait_for_selector(".db-table")
        strip = page.inner_text("[data-db-strip]")
        assert "2 单" in strip and "到货表 3 单" in strip and "1,275.00" in strip and "27.75" in strip
        table = page.inner_text(".db-table")
        assert "R001" in table and "R002" in table and "R003" not in table and "17.75" in table and "1,234.50" in table
        notices = page.inner_text("[data-db-notices]")
        assert "2026-11-20" in notices and "1 单融辉未结算" in notices and "1 单派送费" in notices
        page.click("[data-db-show=missing]")
        assert "R009" in page.inner_text("[data-db-list-body]")
        page.click("[data-db-list] [data-db-close]")
        page.click("[data-db-filter=confirmed]")
        assert "没有符合条件的单" in page.inner_text("[data-db-body]")
        if SHOTS:
            page.screenshot(path=str(Path(SHOTS) / "delivery_bills.png"), full_page=True)
        assert errors == []


def test_pick_and_confirm_sends_snapshot_and_fingerprints(browser):
    posts = []
    context, page, errors = _open(browser, posts)
    with context:
        page.click("[data-db-query]")
        page.wait_for_selector(".db-table")
        page.check("[data-db-pick-all]")
        assert page.inner_text("[data-db-picked]") == "已选 2 单"
        page.click("[data-db-confirm-picked]")
        page.wait_for_function("document.querySelector('[data-db-status]').textContent.includes('已确认 2 单')")
        action, body, request_id = posts[-1]
        assert action == "confirm" and body["snapshot_id"] == "snap-1" and body["request_id"] == request_id
        assert {item["tracking_number"] for item in body["items"]} == {"R001", "R002"}
        assert all(len(item["fingerprint"]) == 64 for item in body["items"]) and set(body["items"][0]) == {"tracking_number", "fingerprint"}
        assert errors == []


def test_value_added_fees_are_chosen_filled_computed_and_saved(browser):
    posts = []
    context, page, errors = _open(browser, posts)
    with context:
        page.click("[data-db-query]")
        page.wait_for_selector(".db-table")
        page.click("[data-db-row=R001] td:nth-child(3)")
        page.click("[data-db-edit-fees]")
        page.wait_for_selector("[data-db-fee=upstairs]")
        assert "2026-11-03" in page.inner_text("[data-db-fees-body]")
        page.check("[data-db-fee=upstairs]")
        page.fill("[data-param=floors]", "3")
        page.click("[data-db-fee-compute]")
        page.wait_for_selector("text=计算结果：合计 14.55")
        action, body, _ = posts[-1]
        assert action == "fees/compute" and body["values"] == {"floors": 3, "settlement_weight": "45.5"}
        page.click("[data-db-fee-save]")
        page.wait_for_selector("[data-db-detail][open]")
        action, body, request_id = posts[-1]
        assert action == "fees/save" and body["fee_kinds"] == ["upstairs"] and body["expected_revision"] is None
        assert body["request_id"] == request_id
        assert "14.55" in page.inner_text("[data-db-detail-body]")
        assert errors == []


def test_rule_segments_editor_and_trial(browser):
    posts = []
    context, page, errors = _open(browser, posts)
    with context:
        page.click("[data-db-open-rules]")
        # 在用的一版按段列成表，每段旁边是算出来的钱（起点、到下一段前 1 公斤）。
        card = page.locator("[data-db-rule-card]").first
        card.wait_for()
        assert "现在用的" in card.inner_text()
        assert "0–50 kg" in card.inner_text() and "50 kg 以上" in card.inner_text()
        assert "0 kg 5.00 → 49 kg 53.00" in card.inner_text() and "50 kg 30.00 元起" in card.inner_text()
        # 编辑表按在用的一版填好：每段只填“到”，起点自动接上一段，最后一段“以上”。
        first, second = "[data-db-segment]:nth-child(1)", "[data-db-segment]:nth-child(2)"
        assert page.input_value(f"{first} [data-field=to]") == "50"
        assert page.locator(f"{second} [data-field=to]").count() == 0 and "50 kg 以上" in page.inner_text(second)
        page.wait_for_function("document.querySelector('[data-db-segment] [data-db-seg-example]').textContent.includes('53.00')")
        # 加一段：原来的最后一段要填“到”，新的一段从它开始。
        page.click("[data-db-segment-add]")
        third = "[data-db-segment]:nth-child(3)"
        page.fill(f"{second} [data-field=to]", "200")
        assert "200 kg 以上" in page.inner_text(third)
        assert page.get_attribute(f"{third} [data-field=first_weight]", "placeholder") == "200"
        page.fill(f"{third} [data-field=first_price]", "100")
        page.fill(f"{third} [data-field=extra_price]", "0.3")  # “起步价含”空着＝从 200 kg 算
        page.wait_for_function("document.querySelectorAll('[data-db-seg-example]')[2].textContent.includes('200 kg 100.00 元起')")
        assert "50 kg 30.00 → 199 kg 104.50" in page.inner_text(second)
        _action, body, _ = posts[-1]
        assert body["rule"]["segments"][2] == {"from": "200", "first_weight": "200", "first_price": "100", "extra_price": "0.3"}
        # “到”不比起点大时，表下直接说哪一段错了。
        page.fill(f"{second} [data-field=to]", "40")
        page.wait_for_selector("text=第 2 段的“到”必须大于这一段的起点 50 公斤")
        page.fill(f"{second} [data-field=to]", "200")
        page.wait_for_function("!document.querySelector('[data-db-seg-check]').textContent")
        # 删掉中间一段：后面一段接到上一段的终点。
        page.click(f"{second} [data-db-segment-remove]")
        assert page.locator("[data-db-segment]").count() == 2 and "50 kg 以上" in page.inner_text(second)
        page.select_option("[data-db-rule-form] [name=rounding]", "ceil_kg")
        page.fill("[data-db-rule-form] [name=trial_weight]", "120.2")
        page.click("[data-db-rule-trial]")
        page.wait_for_selector("text=派送费 121.30 元")  # 第 2 段从 50 起、起步价含空着＝50：100 + 71 × 0.3
        action, body, _ = posts[-1]
        assert action == "rules/trial" and body["rule"]["rounding"] == "ceil_kg" and body["settlement_weight"] == "120.2"
        assert body["rule"]["segments"] == [{"from": "0", "first_weight": "1", "first_price": "5", "extra_price": "1"},
                                            {"from": "50", "first_weight": "50", "first_price": "100", "extra_price": "0.3"}]
        assert "怎么算的" in page.inner_text("[data-db-rule-trial-result]")
        page.wait_for_function("document.querySelectorAll('[data-db-seg-example]')[1].textContent.includes('50 kg 100.00 元起')")
        if SHOTS:
            page.locator("[data-db-rules]").screenshot(path=str(Path(SHOTS) / "delivery_rule_segments.png"))
        assert errors == []
