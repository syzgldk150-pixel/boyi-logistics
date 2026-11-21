"""融辉/韵达对账单 (frontend-spec ㉓a/㉓b/㉓c) rendered in Chromium against synthetic Agent replies.

Checks the reshaping rules of rules 16: constant columns move to the account bar, 一级/二级 share one cell,
a single record becomes KPIs + fields, totals come from the rows or the platform footer, and the 31-day
platform limit is enforced in the date controls before any query.
"""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from console.tests import test_handbook_console as handbook_console

CONSOLE_DIR = Path(__file__).resolve().parents[1]
ORIGIN = "https://console.test"
SHOTS = os.environ.get("HANDBOOK_SCREENSHOT_DIR")

RH_SUMMARY = {
    "platform": "ronghui", "action": "summary", "fetched_at": "2026-11-19 10:00:00", "site_code": "10001", "site_name": "示例网点",
    "columns": [{"field": "CENTER_NAME", "title": "财务中心"}, {"field": "SITE_NAME", "title": "网点名称"},
                {"field": "BALANCE_TYPE", "title": "结算类型"}, {"field": "BALANCE_CUR_MONEY", "title": "结算金额"}],
    "rows": [{"CENTER_NAME": "示例中心", "SITE_NAME": "示例网点", "CENTER_NAME_CODE": "C1", "SITE_NAME_CODE": "10001", "BALANCE_TYPE": "收甲费", "BALANCE_CUR_MONEY": "-1080"},
             {"CENTER_NAME": "示例中心", "SITE_NAME": "示例网点", "CENTER_NAME_CODE": "C1", "SITE_NAME_CODE": "10001", "BALANCE_TYPE": "收乙费", "BALANCE_CUR_MONEY": "3909.22"},
             {"CENTER_NAME": "示例中心", "SITE_NAME": "示例网点", "CENTER_NAME_CODE": "C1", "SITE_NAME_CODE": "10001", "BALANCE_TYPE": "收甲费", "BALANCE_CUR_MONEY": "-0.5"}],
    "groups": [{"center_code": "C1", "site_code": "10001", "balance_type": "收甲费", "count": 12, "latest_date": "2026-11-19", "latest_note": ""},
               {"center_code": "C1", "site_code": "10001", "balance_type": "收乙费", "count": 8, "latest_date": "2026-11-18", "latest_note": ""}],
    "total": 3, "page": 1, "page_size": 3, "footer": {},
}
RH_BALANCE = {
    "platform": "ronghui", "action": "balance", "fetched_at": "2026-11-19 10:00:00", "site_code": "10001", "site_name": "示例网点",
    "columns": [{"field": "ACCOUNT", "title": "账户编号"}, {"field": "CUR_MONEY", "title": "目前结算余额"},
                {"field": "REAL_MONEY", "title": "真实结算余额"}, {"field": "WARN_MONEY", "title": "警戒结算金额"},
                {"field": "DEPOSIT", "title": "保证金/押金/加盟费"}, {"field": "FLAG", "title": "启用标识"}],
    "rows": [{"ACCOUNT": "A001", "CUR_MONEY": "14578.24", "REAL_MONEY": "14578.24", "WARN_MONEY": "1000", "DEPOSIT": "30000", "FLAG": "启用"}],
    "total": 1, "page": 1, "page_size": 1, "footer": {},
}
YD_DETAIL = {
    "platform": "yunda", "action": "detail", "fetched_at": "2026-11-19 10:00:00", "user_id": "900001",
    "columns": [{"field": "trade_time", "title": "交易时间"}, {"field": "begin", "title": "期初余额"}, {"field": "income", "title": "金额/收入"},
                {"field": "expense", "title": "金额/支出"}, {"field": "end", "title": "期末余额"}, {"field": "lv1", "title": "一级费用项目"},
                {"field": "lv2", "title": "二级费用项目"}, {"field": "dept", "title": "收费部门"}, {"field": "bank", "title": "开户行"},
                {"field": "holder", "title": "开户户名"}, {"field": "account", "title": "账户"}],
    "rows": [{"trade_time": "2026-11-19 11:00:22", "begin": "100", "income": "40", "expense": "", "end": "140", "lv1": "运费", "lv2": "", "dept": "示例部门", "bank": "示例银行", "holder": "示例户名", "account": "10000001"},
             {"trade_time": "2026-11-19 11:05:00", "begin": "140", "income": "", "expense": "12.5", "end": "127.5", "lv1": "服务费", "lv2": "派费", "dept": "示例部门二", "bank": "示例银行", "holder": "示例户名", "account": "10000002"}],
    "total": 2891, "page": 1, "page_size": 200, "footer": ["收入：", "34543.30", "支出：", "27505.27", "收支差：", "7038.03"],
}


@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("Chromium is required for bill page regressions")
        with runtime.chromium.launch() as instance:
            yield instance


def _render(platform):
    from console import app_support
    app, _ = handbook_console._ConsoleCase()._app(lambda *a: None)
    token = app_support._CURRENT_ADMIN_USER.set({"id": 1, "role": "super_admin", "is_legacy_basic_auth": False})
    try:
        app._render_platform_bills(handbook_console._handler({}), platform)
    finally:
        app_support._CURRENT_ADMIN_USER.reset(token)
    return app.sent_html


def _open(browser, platform, replies, held=None):
    html = _render(platform)
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5000)
    errors, queries = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != "console.test":
            return route.abort()
        if parsed.path == f"/modules/finance/{platform}-bills":
            return route.fulfill(content_type="text/html", body=html)
        if parsed.path.startswith("/static/"):
            file = CONSOLE_DIR / parsed.path.lstrip("/")
            return route.fulfill(path=str(file)) if file.is_file() else route.fulfill(status=404, body="")
        if parsed.path == f"/finance/platform-bills/{platform}":
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            queries.append(query)
            if held is not None and len(queries) == 1:
                held.append((route, query))  # 首次查询先挂起，模拟平台慢返回
                return None
            data = replies.get(query["action"])
            if data is None:
                return route.fulfill(status=502, content_type="application/json", body=json.dumps({"ok": False, "message": "示例平台失败原因"}, ensure_ascii=False))
            return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": data}, ensure_ascii=False))
        return route.fulfill(status=404, body="")

    page.route("**/*", serve)
    page.goto(f"{ORIGIN}/modules/finance/{platform}-bills")
    return context, page, errors, queries


def _shot(page, name):
    if SHOTS:
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)


def test_ronghui_summary_groups_types_and_drills_down(browser):
    detail = {**RH_SUMMARY, "action": "summary_detail", "columns": [{"field": "BILL_CODE", "title": "运单编号"}], "rows": [{"BILL_CODE": "R1"}], "total": 1, "groups": None}
    context, page, errors, queries = _open(browser, "ronghui", {"summary": RH_SUMMARY, "balance": RH_BALANCE, "summary_detail": detail})
    with context:
        page.wait_for_selector(".pb-table--summary")
        body = page.inner_text(".pb-result")
        assert "财务中心" not in page.inner_text(".pb-table") and "示例中心" in page.inner_text("[data-bill-context]")  # constant columns
        assert "-1,080.50" in body and "3,909.22" in body and "2,828.72" in body  # per-type sums and net
        assert "20 笔" in page.inner_text("[data-bill-strip]") and "+3,909.22" in page.inner_text("[data-bill-strip]")
        assert "2026-11-19" in body and "查看明细 ›" in body and "100.0%" in body
        footer = page.inner_text("[data-bill-pager]")
        assert "个结算类型" in footer and "已全部显示" in footer  # few types: no paging (㉓a)
        # 栅格（rules 7.1.1/7.1.2）：整表左对齐，列边界 1px 竖线，末列不画
        amount = page.locator(".pb-table--summary tbody tr:first-child td.num").first
        assert amount.evaluate("el => getComputedStyle(el).textAlign") in ("left", "start")
        assert amount.evaluate("el => getComputedStyle(el).borderRightWidth") == "1px"
        assert page.locator(".pb-table--summary tbody tr:first-child td").last.evaluate("el => getComputedStyle(el).borderRightWidth") == "0px"
        _shot(page, "23a-ronghui-summary")
        page.click(".pb-table--summary tbody tr:first-child")
        page.wait_for_selector(".pb-trail:not([hidden])")
        assert queries[-1]["action"] == "summary_detail" and queries[-1]["balance_type"] == "收甲费"
        page.click("[data-bill-view='balance']")
        page.wait_for_selector(".pb-kpis")
        assert page.locator(".pb-kpi").count() == 4 and page.locator(".pb-kpi[data-tone='warn']").count() == 1
        assert "14,578.24" in page.inner_text(".pb-kpis") and "启用标识" in page.inner_text(".pb-fields")
        assert page.locator("[data-bill-pager]").is_hidden()  # no fake paging for one record
        _shot(page, "23b-ronghui-balance")
        assert errors == []


def test_date_limit_is_enforced_before_querying(browser):
    context, page, errors, queries = _open(browser, "ronghui", {"summary": RH_SUMMARY})
    with context:
        page.wait_for_selector(".pb-table--summary")
        count = len(queries)
        page.fill("[data-bill-start]", "2026-09-01")
        page.fill("[data-bill-end]", "2026-11-19")
        page.dispatch_event("[data-bill-end]", "change")
        assert "最多查询 31 天" in page.inner_text("[data-bill-date-error]")
        assert page.locator("[data-bill-query]").is_disabled() and len(queries) == count
        page.click("[data-bill-range='7']")
        assert page.locator("[data-bill-date-error]").is_hidden() and page.locator("[data-bill-query]").is_enabled()
        assert errors == []


def test_yunda_detail_moves_constants_and_pairs_fee_items(browser):
    context, page, errors, queries = _open(browser, "yunda", {"summary": None, "detail": YD_DETAIL})
    with context:
        page.wait_for_selector(".pb-empty[data-kind='error']")
        assert "示例平台失败原因" in page.inner_text(".pb-result") and page.locator("[data-bill-retry]").is_visible()
        page.click("[data-bill-view='detail']")
        page.wait_for_selector(".pb-table:not([aria-busy])")
        headers = page.locator(".pb-table thead th").all_inner_texts()
        assert headers == ["交易时间", "期初余额", "金额/收入", "金额/支出", "期末余额", "费用项目", "收费部门", "账户"]
        # account numbers are digits but not money: as written, aligned like text
        account = page.locator(".pb-table tbody tr:first-child td").last
        assert account.inner_text() == "10000001" and "num" not in (account.get_attribute("class") or "")
        assert "num" not in (page.locator(".pb-table thead th").last.get_attribute("class") or "")
        assert "示例银行" in page.inner_text("[data-bill-context]") and "示例户名" in page.inner_text("[data-bill-context]")
        assert "服务费\n派费" in page.inner_text(".pb-table tbody tr:nth-child(2)")
        strip = page.inner_text("[data-bill-strip]")
        assert "34,543.30" in strip and "收支差 = 收入 − 支出" in strip and "2,891 条" in strip
        assert page.locator(".pb-table tfoot").count() == 0  # balance snapshots are not summed
        footer = page.inner_text("[data-bill-pager]")
        assert "已加载 2 / 共 2,891 条" in footer and "向下滚动加载更多" in footer  # rules 7.7: no page numbers
        assert page.locator("[data-bill-page]").count() == 0
        _shot(page, "23c-yunda-detail")
        # scrolling to the bottom appends the next batch instead of replacing the rows
        page.locator(".pb-table-wrap").dispatch_event("scroll")
        page.wait_for_function("document.querySelectorAll('.pb-table tbody tr').length === 4")
        assert queries[-1]["page"] == "2" and queries[-1]["page_size"] == "200"
        assert "已加载 4 / 共 2,891 条" in page.inner_text("[data-bill-pager]")
        assert errors == []


def test_changing_site_while_loading_drops_the_stale_reply(browser):
    # 2026-11-21：首次查询未返回时切换网点，旧响应回来后读 current.action 报 null 错误。
    held = []
    context, page, errors, queries = _open(browser, "ronghui", {"summary": RH_SUMMARY}, held=held)
    with context:
        page.wait_for_function("document.querySelector('.pb-skel') !== null")
        assert len(held) == 1
        page.dispatch_event("[data-bill-account]", "change")
        page.wait_for_selector(".pb-table--summary")
        assert len(queries) == 2
        stale, _ = held[0]
        stale.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {**RH_SUMMARY, "groups": [], "rows": []}}, ensure_ascii=False))
        page.wait_for_timeout(300)
        assert page.locator(".pb-table--summary").count() == 1 and "3,909.22" in page.inner_text(".pb-result")
        assert page.locator("[data-bill-query]").is_enabled()
        assert errors == []
