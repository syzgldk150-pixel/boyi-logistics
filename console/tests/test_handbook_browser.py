"""业务手册页 (frontend-spec 52–58) rendered in Chromium against synthetic Agent replies."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from console.tests import test_handbook_console as handbook_console

CONSOLE_DIR = Path(__file__).resolve().parents[1]
ORIGIN = "https://console.test"
ENTRY_ID = "11111111-2222-4333-8444-555555555555"
RULE_ID = "22222222-3333-4444-8555-666666666666"
SHOTS = os.environ.get("HANDBOOK_SCREENSHOT_DIR")

SOURCE = {"document": "示例物流手册 V1", "location": "第一章 第 1 页", "link": ""}
ENTRY = {
    "entry_id": ENTRY_ID, "title": "网点简称与报价说法", "status": "ACTIVE", "latest_version": 2, "active_version": 1,
    "updated_at": "2026-11-15T10:02:00", "row_version": 3,
    "versions": [
        {"version": 2, "status": "COMPILED", "body": "大家说“示例店”就是示例区网点。", "updated_at": "2026-11-15T10:02:00",
         "validation": {"ok": True}, "acknowledged": [{"index": 0, "answer": "是，只指示例区", "by": "6", "at": "2026-11-15T10:03:00+00:00"}],
         "compiled": {"restatement": "“示例店”指示例区网点；报个价指三平台报价。",
                      "ambiguities": [{"question": "“示例店”是否只指示例区？", "options": ["是，只指示例区", "也包括其他区"]},
                                      {"question": "“报个价”是否包含单独报价？", "options": ["包含", "不包含"]}],
                      "examples": [{"utterance": "帮我报个价", "action": "plugin:q"}],
                      "unsupported": ["希望机器人自动改回单状态"]},
         "described": ["示例店 = 示例区网点", "报个价 = 三平台报价", "先问再答"],
         "trial": {"cases": 2, "passed": 2, "improved": 1, "rows": [
             {"utterance": "示例店在哪", "expected": "none", "expected_title": "直接回答", "before": "none", "after": "none", "pass": True},
             {"utterance": "帮我报个价", "expected": "plugin:q", "expected_title": "三平台报价", "before": "ask_user", "after": "plugin:q", "after_title": "三平台报价", "pass": True}]},
         "submit_blockers": ["还差 1 项歧义确认"], "blockers": ["这个版本还没有提交审核", "还差 1 项歧义确认"]},
        {"version": 1, "status": "ACTIVE", "body": "旧说明", "updated_at": "2026-11-10T08:00:00"},
    ],
    "events": [{"version": 2, "action": "COMPILE", "actor_id": "6", "detail": {"ok": True, "seconds": 32.0}, "created_at": "2026-11-15T10:02:30+00:00"},
               {"version": 2, "action": "SAVE", "actor_id": "6", "detail": None, "created_at": "2026-11-15T10:02:00+00:00"}],
    "people": {"6": "示例编辑"},
}
RULE_CONTENT = {
    "schema": 1,
    "params": [{"key": "weight", "label": "结算重量", "type": "number", "unit": "kg", "min": "0.01"},
               {"key": "floor", "label": "送到几楼", "type": "number", "integer": True, "min": "1"},
               {"key": "elevator", "label": "有电梯", "type": "bool"}],
    "formula": {"type": "when", "cases": [{"if": {"left": "elevator", "op": "eq", "value": True}, "then": {
        "type": "sum", "label": "有电梯", "source": SOURCE, "items": [{"type": "fixed", "amount": "5"}, {"type": "rate", "rate": "0.06", "of": "weight"}]}}],
        "else": {"type": "tiers", "label": "楼层加收", "by": "floor", "tiers": [
            {"over": None, "up_to": "2", "then": {"type": "fixed", "amount": "0"}},
            {"over": "2", "up_to": None, "then": {"type": "rate", "rate": "0.1", "of": "weight", "unit": "元/kg"}}]}},
    "reminders": ["示例提醒"],
}
RULE = {
    "rule_id": RULE_ID, "fee_kind": "upstairs", "fee_name": "上楼费", "version_label": "示例手册 V1 草稿", "status": "DRAFT",
    "effective_from": None, "row_version": 1, "updated_at": "2026-11-15T10:12:00", "content": RULE_CONTENT,
    "params": RULE_CONTENT["params"], "content_problems": [],
    "checks": [{"kind": "question", "text": "混装时怎么收？", "options": ["需人工判断（现在的做法）", "其他（写明做法）"], "answer": None, "note": ""}],
    "lines": [
        {"sig": "38914bb1e4808a64", "text": "有电梯 = 是 › 有电梯：5 元 + 0.06 × 结算重量", "path": "formula.cases.0.then", "kind": "sum",
         "document": "示例物流手册 V1", "location": "第一章 第 1 页", "pages": [1], "done": False},
        {"sig": "42ef3975026d82f5", "text": "其他情况 › 楼层加收 › 送到几楼 ≤ 2：0 元", "path": "formula.else.tiers.0.then", "kind": "fixed",
         "document": "", "location": "", "pages": [], "done": False},
        {"sig": "a6b3908a945bcfac", "text": "其他情况 › 楼层加收 › 送到几楼 > 2：0.1 元/kg × 结算重量", "path": "formula.else.tiers.1.then",
         "kind": "rate", "document": "", "location": "", "pages": [], "done": False, "changed": True}],
    "compare": {"rule_id": "r-live", "version_label": "示例手册 V0", "status": "APPROVED", "effective_from": "2026-01-01",
                "same": 2, "changed": 1, "removed": ["旧的一行：3 元"]},
    "events": [{"action": "SAVE", "actor_id": "6", "created_at": "2026-11-15T10:12:00"}],
    "trial_count": 0, "people": {"6": "示例编辑"},
    "submit_blockers": ["还没有填写执行日期（以总部通知为准）。", "保存后还没有试算过，请至少试算一个场景。"],
}
JOB_ID = "33333333-4444-4555-8666-777777777777"
DRAFT_JOB = {"job_id": JOB_ID, "status": "DONE", "stage": "完成", "chapter": "第一章 上楼", "start": 2, "end": 3, "elapsed": 64,
             "document_title": "示例物流手册 V1", "saved": {}, "stages": ["读取手册", "生成页面图片", "AI 起草", "检查结果"],
             "result": {"skipped": ["第 3 页的图示没有整理"], "fees": [
                 {"index": 0, "name": "上楼费", "existing": True, "params": ["结算重量", "送到几楼"], "questions": [
                     {"kind": "question", "text": "混装时怎么收？", "options": ["需人工判断（现在的做法）", "其他（写明做法）"], "answer": None, "note": ""}],
                  "lines": [{"text": "有电梯：5 元 + 0.06 元/kg × 结算重量", "location": "第一章 第 2 页"}]},
                 {"index": 1, "name": "示例搬运费", "existing": False, "params": [], "questions": [],
                  "lines": [{"text": "示例搬运费：20 元", "location": "第一章 第 3 页"}]}]}}
# A 1×1 PNG standing in for a manual page picture.
PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABc3UBGAAAAABJRU5ErkJggg=="
SOURCES = {
    "last_scan": {"at": "2026-11-15T08:30:00+08:00", "documents": 2, "reminders": 1, "baseline": False},
    "documents": [{"node_token": "n1", "title": "示例物流手册", "space": "示例空间", "edit_time": "1763170000", "cited_by": ["上楼费 示例手册 V1"]},
                  {"node_token": "n2", "title": "示例收费调整通知.pdf", "space": "示例空间", "edit_time": "1763170000", "cited_by": []}],
    "reminders": [{"reminder_id": 7, "node_token": "n2", "kind": "NEW", "title": "示例收费调整通知.pdf", "detail": "标题像收费通知", "status": "OPEN", "detected_at": "2026-11-15T00:30:00"}],
}
TRIAL = {"result": {"outcome": "AMOUNT", "outcome_text": "金额", "amount": "20.00", "reason": "", "missing": [],
                    "steps": ["楼层加收：送到几楼 5，落在 > 2 这一档", "0.1 元/kg × 结算重量 200 = 20", "合计 20.00"],
                    "citations": [], "reminders": ["示例提醒"], "rule_version": "示例手册 V1 草稿", "reportable": False},
         "boundary": [{"param": "送到几楼", "value": "2", "outcome": "AMOUNT", "amount": "0.00", "note": ""},
                      {"param": "送到几楼", "value": "3", "outcome": "AMOUNT", "amount": "20.00", "note": ""}]}
CATALOG = {"billing_date": "2026-11-19", "conflict": "", "params": RULE_CONTENT["params"],
           "fees": [{"fee_kind": "upstairs", "name": "上楼费", "version": "示例手册 V1（执行日期 2026-10-01）", "group": "示例组",
                     "params": ["weight", "floor", "elevator"], "reminders": []}],
           "unavailable": [{"fee_kind": "other", "name": "示例费", "reason": "这个日期没有已审核生效的收费规则，不能给出金额。"}]}
QUOTE = {"billing_date": "2026-11-19", "total": "20.00", "complete": True, "missing": [], "unavailable": [], "quote_id": "q-1",
         "items": [{"fee_kind": "upstairs", "name": "上楼费", "version": "示例手册 V1（执行日期 2026-10-01）", "counted": True, "note": "",
                    **TRIAL["result"]}]}
TRY = {"reply": "以下是《示例物流手册》第一章的原文……", "quotes": [{"title": "示例物流手册", "location": "第一章", "pages": [1], "url": "https://example.invalid/doc"}],
       "steps": [{"title": "理解问题", "state": "done", "result": "选择了：按手册计算费用"},
                 {"title": "信息检查", "state": "done", "result": "信息足够，未触发反问"},
                 {"title": "检索知识库", "state": "done", "result": "检索知识库并引用原文 1 段"},
                 {"title": "匹配规则", "state": "skipped", "result": "本次不涉及计算规则"},
                 {"title": "计算", "state": "skipped", "result": "本次没有计算"},
                 {"title": "权限检查", "state": "done", "result": "未执行插件 / 未写入 / 未发送"}],
       "seconds": 2.4, "handbook_entries": 3}


def _render(user):
    case = handbook_console.HandbookPageTests()
    return case._render(user)


@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("Chromium is required for handbook browser regressions")
        with runtime.chromium.launch() as instance:
            yield instance


def _open(browser, user):
    html = _render(user)
    context = browser.new_context(viewport={"width": 1440, "height": 1080})
    page = context.new_page()
    page.set_default_timeout(5000)
    errors, posts, polls = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        reply = None
        if parsed.netloc != "console.test":
            return route.abort()
        if path == "/handbook":
            return route.fulfill(content_type="text/html", body=html)
        if path.startswith("/static/"):
            file = CONSOLE_DIR / path.lstrip("/")
            return route.fulfill(path=str(file)) if file.is_file() else route.fulfill(status=404, body="")
        if request.method == "POST":
            posts.append((path, json.loads(request.post_data or "{}")))
        if path == "/handbook/entries":
            reply = {"entries": [{**{k: v for k, v in ENTRY.items() if k not in {"versions", "events", "people"}},
                                  "latest_status": ENTRY["versions"][0]["status"], "summary": "大家说“示例店”就是示例区网点。"}]}
        elif path == f"/handbook/entries/{ENTRY_ID}":
            reply = ENTRY
        elif path == "/handbook/fee-rules":
            reply = {"versions": [{k: v for k, v in RULE.items() if k not in {"content", "checks", "events", "lines"}}]}
        elif path == "/handbook/fee-rules/source-page":
            reply = {"document": "示例物流手册 V1", "page": 1, "png_base64": PIXEL}
        elif path == "/handbook/fee-rules/effective-dates":
            reply = {"updated": 1}
        elif path == "/handbook/fee-drafts/documents":
            reply = {"documents": [{"node_token": "n1", "title": "示例物流手册 V1.pdf", "space": "示例空间"}]}
        elif path == "/handbook/fee-drafts/outline":
            reply = {"node_token": "n1", "title": "示例物流手册 V1.pdf", "page_count": 30, "chapters": [
                {"title": "第一篇 收费", "level": 0, "start": 1, "end": 30},
                {"title": "第一章 上楼", "level": 1, "start": 2, "end": 3}]}
        elif path == "/handbook/fee-drafts/jobs":
            reply = {"job_id": JOB_ID}
        elif path == f"/handbook/fee-drafts/jobs/{JOB_ID}":
            polls.append(1)
            reply = DRAFT_JOB if len(polls) > 1 else {**DRAFT_JOB, "status": "RUNNING", "stage": "AI 起草", "result": None}
        elif path == f"/handbook/fee-drafts/jobs/{JOB_ID}/save":
            reply = {"created": [{"rule_id": RULE_ID, "fee_name": "上楼费", "new_fee": False}]}
        elif path == f"/handbook/fee-rules/{RULE_ID}/checks":
            reply = {"saved": True, "row_version": 2}
        elif path == f"/handbook/fee-rules/{RULE_ID}":
            reply = RULE
        elif path == f"/handbook/fee-rules/{RULE_ID}/trial":
            reply = {**TRIAL, "trial_count": 1}
        elif path == f"/handbook/fee-rules/{RULE_ID}/save":
            reply = {"saved": True}
        elif path == "/handbook/quote/catalog":
            reply = CATALOG
        elif path == "/handbook/quote/recent":
            reply = {"quotes": [{"quote_id": "q-0", "created_at": "2026-11-19T09:00:00", "total": "34.00", "complete": True,
                                 "actor": "示例编辑", "channel": "console", "tracking_number": None, "result": {"items": [{"name": "上楼费"}]}}]}
        elif path == "/handbook/quote":
            reply = QUOTE
        elif path == f"/handbook/entries/{ENTRY_ID}/acknowledge":
            reply = {"acknowledged": [1]}
        elif path == "/handbook/sources":
            reply = SOURCES
        elif path == "/handbook/try":
            reply = TRY
        if reply is None:
            return route.fulfill(status=404, content_type="application/json", body="{}")
        return route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": reply}, ensure_ascii=False))

    page.route("**/*", serve)
    page.add_init_script("try { localStorage.removeItem('handbook.tab'); } catch (e) {}")
    page.goto(f"{ORIGIN}/handbook")
    return context, page, errors, posts


def _shot(page, name):
    if SHOTS:
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)


def test_editor_sees_workflow_and_no_approval(browser):
    context, page, errors, _posts = _open(browser, {"id": 6, "role": "admin", "access_permissions": ("handbook:view", "handbook:operate")})
    with context:
        page.click(f"[data-entry='{ENTRY_ID}']")
        page.wait_for_selector(".hb-steps")
        assert "这条说明正在生效中" in page.inner_text("[data-handbook-editor]")
        result = page.inner_text("[data-handbook-result]")
        assert "需要你确认 1 项歧义" in result and "不会生效" in result and "还差 1 项歧义确认" in result and "32" in result
        assert page.locator("[data-handbook-submit], [data-handbook-review], [data-review-approve]").count() == 0
        assert page.locator(".hb-submit button.primary-btn[disabled]").count() == 1
        assert page.locator("[data-handbook-tab='entries'] [data-tab-count]").inner_text() == "1"
        assert "写作提示" in page.inner_text("[data-handbook-editor]")
        assert "全部 1" in page.inner_text("[data-entries-filters]")
        # 2026-11-20: a clashing .hb-list__tools rule wrapped these filters one character per line.
        assert page.locator("[data-entries-filters]").bounding_box()["height"] < 40
        assert page.evaluate("getComputedStyle(document.querySelector('.hb-tab[aria-selected=\"true\"]')).boxShadow") == "none"
        _shot(page, "52-entries")
        page.click("[data-handbook-answer]")
        page.check("input[name='hb-answer-1'][value='不包含']")
        page.click("[data-handbook-answer-save]")
        page.wait_for_function("document.querySelector('[data-handbook-status]').innerText.includes('已保存确认')")
        page.click("[data-handbook-tab='fees']")
        page.click(f"[data-fee-open='{RULE_ID}']")
        # 核对 is the first view: the saved rule as 收费表 lines, ticked against the manual page.
        page.wait_for_selector("[data-line]")
        review = page.inner_text("[data-fee-editor]")
        assert "已核对 0 / 3 行" in review and "有电梯 = 是 › 有电梯：5 元 + 0.06 × 结算重量" in review and "缺出处" in review
        assert "混装时怎么收？" in review and page.locator("[data-question-text]").count() == 0
        page.click("[data-edit-questions]")  # editors may reword a draft's question
        assert page.locator("[data-question-text='0']").input_value() == "混装时怎么收？"
        page.click("[data-edit-questions]")
        assert page.locator("[data-meta='version_label']").count() == 0
        side = page.inner_text(".hb-side")
        assert "生效检查" in side and "还差 4 项" in side and "0 / 4 项通过" in side and "保存后还没有试算" in side
        assert "2 处缺出处" in side and "1 项未回答" in side
        assert "1 行有变化或新增" in review and "旧的一行：3 元" in review and page.locator("[data-changed='true']").count() == 1
        page.check("[data-line='38914bb1e4808a64']")
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('核对已保存')")
        ticked = [body for path, body in _posts if path.endswith("/checks")][-1]
        assert ticked == {"row_version": 1, "checks": [
            {"kind": "question", "text": "混装时怎么收？", "options": ["需人工判断（现在的做法）", "其他（写明做法）"], "answer": None, "note": ""},
            {"kind": "line", "sig": "38914bb1e4808a64", "done": True}]}
        page.check("input[data-answer='0'][value='需人工判断（现在的做法）']")
        page.wait_for_timeout(800)
        answered = [body for path, body in _posts if path.endswith("/checks")][-1]
        assert answered["checks"][0]["answer"] == "需人工判断（现在的做法）"
        page.click("[data-source-page='1']")
        page.wait_for_selector(".hb-page img")
        assert "示例物流手册 V1 · 第 1 页" in page.inner_text(".hb-dialog--page")
        page.click("[data-page-close]")
        _shot(page, "54-review")
        page.click("[data-fee-batch-dates]")
        page.fill("[data-batch-date]", "2026-12-01")
        page.click(".hb-dialog .primary-btn")
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('设执行日期')")
        dates = [body for path, body in _posts if path.endswith("/effective-dates")][-1]
        assert dates == {"effective_from": "2026-12-01", "versions": [{"rule_id": RULE_ID, "row_version": 1}]}
        page.click("[data-fee-tab='content']")
        page.wait_for_selector(".hb-fold")
        assert "缺出处" in page.inner_text("[data-fee-editor]") and "标识" not in page.inner_text("[data-fee-editor]")
        page.fill("[data-meta='fee_name']", "上楼费")
        assert page.locator("[data-fee-action='submit']").is_disabled()
        _shot(page, "54-fees")
        page.click("[data-fee-tab='trial']")
        page.click("[data-fee-trial='boundary']")
        page.wait_for_selector(".hb-trial__amount")
        editor = page.inner_text("[data-fee-editor]")
        assert "未生效 · 只供核对" in editor and "¥ 20.00" in editor and "其余参数按本次填写" in editor
        trial = [body for path, body in _posts if path.endswith("/trial")][-1]
        assert trial == {"values": {}, "boundary": True}
        page.fill("[data-param='weight']", "200")
        page.fill("[data-param='floor']", "5")
        page.select_option("[data-param='elevator']", "false")
        page.press("[data-param='floor']", "Enter")
        page.wait_for_function("document.querySelectorAll('[data-fee-trial-result] .hb-trial').length === 1")
        page.wait_for_timeout(200)
        trial = [body for path, body in _posts if path.endswith("/trial")][-1]
        assert trial == {"values": {"weight": "200", "floor": 5, "elevator": False}, "boundary": False}
        _shot(page, "55-trial")
        page.click("[data-fee-tab='content']")
        page.click("[data-fold-all]")
        page.fill("[data-f='formula.else.tiers.1.then.rate']", "0.2")
        page.click("[data-fee-action='save']")
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('已保存')")
        saved = [body for path, body in _posts if path.endswith("/save")][-1]
        assert saved["content"]["formula"]["else"]["tiers"][1]["then"]["rate"] == "0.2" and saved["fee_name"] == "上楼费"
        page.click("[data-handbook-tab='quote']")
        page.wait_for_selector("[data-quote-fee='upstairs']")
        assert "1 种费用在这个日期不能报价" in page.inner_text("[data-quote-fees]")
        page.wait_for_function("document.querySelector('[data-quote-recent]').innerText.includes('示例编辑')")
        page.check("[data-quote-fee='upstairs']")
        page.fill("[data-quote-form] [data-param='weight']", "200")
        page.fill("[data-quote-form] [data-param='floor']", "5")
        page.select_option("[data-quote-form] [data-param='elevator']", "false")
        page.click("[data-quote-run]")
        page.wait_for_selector(".hb-quote__item")
        assert "¥ 20.00" in page.inner_text("[data-quote-result]")
        quote = [body for path, body in _posts if path == "/handbook/quote"][-1]
        assert quote["fee_kinds"] == ["upstairs"] and quote["values"] == {"weight": "200", "floor": 5, "elevator": False}
        _shot(page, "quote")
        page.click("[data-handbook-tab='sources']")
        page.wait_for_selector(".hb-reminder")
        assert "标记已检查" in page.inner_text("[data-sources-reminders]")
        assert page.locator("[data-sources-check-all]").is_visible()
        _shot(page, "56-sources")
        page.click("[data-handbook-tab='try']")
        page.fill("[data-try-question]", "上楼费怎么收？")
        page.press("[data-try-question]", "Enter")
        page.wait_for_selector(".hb-timeline")
        answer = page.inner_text("[data-try-answer]")
        assert "按手册计算费用" in answer and "示例物流手册" in answer and "用时 2.4 秒" in answer and "未触发" in answer
        assert "未写入任何数据" in answer and page.locator("[data-try-recent] button").count() == 1
        _shot(page, "57-try")
        assert errors == []


def test_editor_drafts_fees_from_a_manual_chapter(browser):
    context, page, errors, posts = _open(browser, {"id": 6, "role": "admin", "access_permissions": ("handbook:view", "handbook:operate")})
    with context:
        page.click("[data-handbook-tab='fees']")
        page.wait_for_selector("[data-line]")  # the first draft opens by itself
        page.click("[data-fee-draft]")
        page.click("[data-draft-doc='n1']")
        page.wait_for_selector("[data-draft-chapter='1']")
        assert page.locator("[data-draft-chapter='0']").is_disabled()  # 30 pages is too long for one draft
        assert page.locator("[data-draft-start]").is_disabled()
        page.check("[data-draft-chapter='1']")
        page.click("[data-draft-start]")
        page.wait_for_selector(".hb-draft__stages")
        assert "正在起草 第一章 上楼" in page.inner_text("[data-fee-editor]")
        page.wait_for_selector(".hb-draft__fee", timeout=12000)
        result = page.inner_text("[data-fee-editor]")
        assert "起草了 2 项费用" in result and "已有同名 · 存为新版本" in result and "新费用" in result
        assert "第 3 页的图示没有整理" in result and "混装时怎么收？" in result
        page.uncheck("[data-draft-pick='1']")
        _shot(page, "54-draft")
        page.click("[data-draft-save]")
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('已保存 1 个草稿')")
        start = [body for path, body in posts if path == "/handbook/fee-drafts/jobs"][-1]
        assert start == {"node_token": "n1", "start": 2, "end": 3, "chapter": "第一章 上楼"}
        assert [body for path, body in posts if path.endswith("/save")][-1] == {"indices": [0]}
        page.wait_for_selector("[data-line]")  # the kept draft opens in 核对
        assert not errors


def test_a_knowledge_reminder_opens_drafting_for_that_document(browser):
    context, page, errors, _posts = _open(browser, {"id": 6, "role": "admin", "access_permissions": ("handbook:view", "handbook:operate")})
    with context:
        page.click("[data-handbook-tab='sources']")
        page.click("[data-draft-from='n2']")
        page.wait_for_selector("[data-draft-chapter='1']")
        assert page.locator("[data-handbook-tab='fees']").get_attribute("aria-selected") == "true"
        assert "示例物流手册 V1.pdf" in page.inner_text("[data-fee-editor]")
        assert not errors


def test_super_admin_review_needs_confirmation(browser):
    context, page, errors, posts = _open(browser, {"id": 1, "role": "super_admin", "is_legacy_basic_auth": False})
    with context:
        ready = json.loads(json.dumps(ENTRY))
        ready["status"] = "PAUSED"
        ready["versions"][0]["status"] = "PENDING"
        ready["versions"][0]["acknowledged"].append({"index": 1, "answer": "不包含", "by": "6", "at": "2026-11-15T10:04:00+00:00"})
        ready["versions"][0]["blockers"] = ready["versions"][0]["submit_blockers"] = []
        ready["events"].insert(0, {"version": 2, "action": "SUBMIT", "actor_id": "6", "detail": None, "created_at": "2026-11-15T10:07:00+00:00"})
        page.route(f"**/handbook/entries/{ENTRY_ID}", lambda route: route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": ready}, ensure_ascii=False)))
        page.click(f"[data-entry='{ENTRY_ID}']")
        page.wait_for_selector("button[data-handbook-review]")
        page.click("button[data-handbook-review]")
        page.wait_for_selector("[data-review-approve]")
        review = page.inner_text("[data-handbook-review-page]")
        assert "已满足生效条件" in review and "已选：不包含" in review and "示例编辑" in review and "提交审核" in review
        page.click("[data-review-compare]")
        assert "旧说明" in page.inner_text(".hb-diff")
        _shot(page, "53-review")
        page.click("[data-review-approve]")
        dialog = page.locator("dialog.hb-dialog")
        assert re.search("V2", dialog.inner_text())
        _shot(page, "53-approve")
        dialog.locator("button[value='cancel']").click()
        assert not [path for path, _body in posts if path.endswith("/approve")]
        assert errors == []


def test_viewer_sees_no_actions(browser):
    context, page, errors, _posts = _open(browser, {"id": 5, "role": "admin", "access_permissions": ("handbook:view",)})
    with context:
        page.click(f"[data-entry='{ENTRY_ID}']")
        page.wait_for_selector(".hb-steps")
        assert page.locator("[data-handbook-save], [data-handbook-compile], [data-handbook-new]").count() == 0
        assert "只读 · 仅查看" in page.inner_text(".hb-role")
        page.click("[data-handbook-tab='sources']")
        page.wait_for_selector(".hb-reminder")
        assert page.locator("[data-check], [data-sources-scan]").count() == 0
        assert errors == []
