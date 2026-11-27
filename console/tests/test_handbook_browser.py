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
               {"key": "elevator", "label": "有电梯", "type": "bool", "default": True}],
    "formula": {"type": "when", "cases": [{"if": {"left": "elevator", "op": "eq", "value": True}, "then": {
        "type": "sum", "label": "有电梯", "source": SOURCE, "items": [{"type": "fixed", "amount": "5"}, {"type": "rate", "rate": "0.06", "of": "weight"}]}}],
        "else": {"type": "tiers", "label": "楼层加收", "by": "floor", "tiers": [
            {"over": None, "up_to": "2", "then": {"type": "fixed", "amount": "0"}},
            {"over": "2", "up_to": None, "then": {"type": "rate", "rate": "0.1", "of": "weight", "unit": "元/kg"}}]}},
    "reminders": ["示例提醒"],
}
RULE = {
    "rule_id": RULE_ID, "fee_kind": "upstairs", "fee_name": "上楼费", "version_label": "示例手册 V1 草稿", "status": "DRAFT",
    "effective_from": None, "row_version": 1, "updated_at": "2026-11-15T10:12:00", "updated_by": "6", "content": RULE_CONTENT,
    "params": RULE_CONTENT["params"], "content_problems": [],
    "checks": [{"kind": "question", "text": "混装时怎么收？", "options": ["需人工判断（现在的做法）", "其他（写明做法）"], "answer": None, "note": ""}],
    "lines": [
        {"sig": "38914bb1e4808a64", "text": "有电梯 = 是 › 有电梯：5 元 + 0.06 × 结算重量", "condition": "有电梯 = 是 › 有电梯",
         "amount": "5 元 + 0.06 × 结算重量", "path": "formula.cases.0.then", "kind": "sum",
         "document": "示例物流手册 V1", "location": "第一章 第 1 页", "pages": [1], "done": False},
        {"sig": "42ef3975026d82f5", "text": "其他情况 › 楼层加收 › 送到几楼 ≤ 2：0 元", "condition": "其他情况 › 楼层加收 › 送到几楼 ≤ 2",
         "amount": "0 元", "path": "formula.else.tiers.0.then", "kind": "fixed",
         "document": "", "location": "", "pages": [], "done": False},
        {"sig": "a6b3908a945bcfac", "text": "其他情况 › 楼层加收 › 送到几楼 > 2：0.1 元/kg × 结算重量", "condition": "其他情况 › 楼层加收 › 送到几楼 > 2",
         "amount": "0.1 元/kg × 结算重量", "path": "formula.else.tiers.1.then",
         "kind": "rate", "document": "", "location": "", "pages": [], "done": False, "changed": True}],
    "compare": {"rule_id": "r-live", "version_label": "示例手册 V0", "status": "APPROVED", "effective_from": "2026-01-01",
                "same": 2, "changed": 1, "removed": ["旧的一行：3 元"]},
    "events": [{"action": "SAVE", "actor_id": "6", "created_at": "2026-11-15T10:12:00"}],
    "trial_count": 0, "people": {"6": "示例编辑"}, "can_delete": True,
    "progress": {"lines": 3, "ticked": 0, "questions": 1, "answered": 0},
    "submit_blockers": ["还没有填写执行日期（以总部通知为准）。", "保存后还没有试算过，请至少试算一个场景。"],
}
JOB_ID = "33333333-4444-4555-8666-777777777777"
DRAFT_JOB = {"job_id": JOB_ID, "status": "DONE", "stage": "完成", "chapter": "第一章 上楼", "start": 2, "end": 3, "elapsed": 64,
             "title": "示例物流手册 V1.pdf", "document_title": "示例物流手册 V1", "node_token": "n1", "rule_id": RULE_ID,
             "stages": ["读取手册", "生成页面图片", "AI 起草", "检查结果"], "result": {"filled": True, "lines": 3, "questions": 1, "skipped": []}}


def rule(**changes):
    """A copy of RULE with changes; ``ticked=True`` ticks every line and gives each a source."""
    data = json.loads(json.dumps(RULE, ensure_ascii=False))
    if changes.pop("ticked", False):
        for line in data["lines"]:
            line.update(done=True, document="示例物流手册 V1", location="第一章 第 1 页", pages=[1])
    data.update(changes)
    return data


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
        elif path == "/handbook/fee-rules" and request.method == "GET":
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
        elif path == "/handbook/fee-drafts/jobs" and request.method == "GET":
            reply = {"job": None}
        elif path == "/handbook/fee-drafts/jobs":
            reply = {"job_id": JOB_ID}
        elif path == f"/handbook/fee-drafts/jobs/{JOB_ID}":
            polls.append(1)
            reply = DRAFT_JOB if len(polls) > 1 else {**DRAFT_JOB, "status": "RUNNING", "stage": "AI 起草", "result": None}
        elif path == f"/handbook/fee-drafts/jobs/{JOB_ID}/cancel":
            reply = {**DRAFT_JOB, "status": "CANCELLED", "error": "已取消起草。"}
        elif path == "/handbook/fee-rules" and request.method == "POST":
            reply = {"rule_id": RULE_ID}
        elif path == f"/handbook/fee-rules/{RULE_ID}/checks":
            reply = {"saved": True, "row_version": 2}
        elif path == f"/handbook/fee-rules/{RULE_ID}":
            reply = RULE
        elif path == f"/handbook/fee-rules/{RULE_ID}/trial":
            reply = {**TRIAL, "trial_count": 1}
        elif path == f"/handbook/fee-rules/{RULE_ID}/save":
            reply = {"saved": True, "row_version": 2}
        elif re.fullmatch(f"/handbook/fee-rules/{RULE_ID}/(submit|withdraw|approve|retire|delete|copy)", path):
            reply = {"action": path.rsplit("/", 1)[-1], "rule_id": RULE_ID}
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


EDITOR = {"id": 6, "role": "admin", "access_permissions": ("handbook:view", "handbook:operate")}


def _entries(page):
    """说明条目 sits under “更多” since 2026-11-22."""
    page.click("[data-handbook-more] summary")
    page.click("[data-handbook-tab='entries']")


def _serve_rule(page, data, versions=None):
    body = json.dumps({"ok": True, "data": data}, ensure_ascii=False)
    page.route(f"**/handbook/fee-rules/{RULE_ID}", lambda route: route.fulfill(content_type="application/json", body=body))
    listed = versions if versions is not None else [{k: v for k, v in data.items() if k not in {"content", "checks", "events", "lines"}}]
    page.route("**/handbook/fee-rules", lambda route: route.fallback() if route.request.method != "GET" else route.fulfill(
        content_type="application/json", body=json.dumps({"ok": True, "data": {"versions": listed}}, ensure_ascii=False)))


def _status(page, text):
    page.wait_for_function(f"document.querySelector('[data-fee-status]').innerText.includes({json.dumps(text)})")


def test_editor_sees_workflow_and_no_approval(browser):
    context, page, errors, _posts = _open(browser, EDITOR)
    with context:
        # 计算规则 is the first tab now; 说明条目 moved into “更多”.
        assert page.locator("[data-handbook-tab='fees']").get_attribute("aria-selected") == "true"
        _entries(page)
        page.click(f"[data-entry='{ENTRY_ID}']")
        page.wait_for_selector(".hb-steps")
        assert page.locator("[data-handbook-more]").get_attribute("data-selected") == "true"
        assert "这条说明正在生效中" in page.inner_text("[data-handbook-editor]")
        result = page.inner_text("[data-handbook-result]")
        assert "需要你确认 1 项歧义" in result and "不会生效" in result and "还差 1 项歧义确认" in result and "32" in result
        assert page.locator("[data-handbook-submit], [data-handbook-review], [data-review-approve]").count() == 0
        assert page.locator(".hb-submit button.primary-btn[disabled]").count() == 1
        assert page.locator("[data-handbook-tab='entries'] [data-tab-count]").text_content() == "1"
        assert "写作提示" in page.inner_text("[data-handbook-editor]")
        assert "全部 1" in page.inner_text("[data-entries-filters]")
        # 2026-11-20: a clashing .hb-list__tools rule wrapped these filters one character per line.
        assert page.locator("[data-entries-filters]").bounding_box()["height"] < 40
        _shot(page, "52-entries")
        page.click("[data-handbook-answer]")
        page.check("input[name='hb-answer-1'][value='不包含']")
        page.click("[data-handbook-answer-save]")
        page.wait_for_function("document.querySelector('[data-handbook-status]').innerText.includes('已保存确认')")
        page.click("[data-handbook-tab='quote']")
        page.wait_for_selector("[data-quote-fee='upstairs']")
        assert "1 种费用在这个日期不能报价" in page.inner_text("[data-quote-fees]")
        page.wait_for_function("document.querySelector('[data-quote-recent]').innerText.includes('示例编辑')")
        page.check("[data-quote-fee='upstairs']")
        page.fill("[data-quote-form] [data-param='weight']", "200")
        page.fill("[data-quote-form] [data-param='floor']", "5")
        # 2026-11-27: an unset yes/no shows the rule's default (有电梯 = 是) instead of “请选择”.
        assert page.input_value("[data-quote-form] [data-param='elevator']") == "true"
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


def test_fee_rule_opens_on_check_step_and_ticks_lines(browser):
    context, page, errors, posts = _open(browser, EDITOR)
    with context:
        # The draft opens on its first open step: ① has a draft, so ② 核对原文.
        page.wait_for_selector(".fs-line")
        steps = page.inner_text(".fs-steps")
        assert "核对原文" in steps and "还差 3 行" in steps
        assert page.locator(".fs-step[data-state='done']").count() == 1  # ① 起草; ③ still has an unanswered question
        body = page.inner_text("[data-fee-body]")
        assert "还差 3 行" in body and "有电梯 = 是 › 有电梯" in body and "5 元 + 0.06 × 结算重量" in body
        assert "1 行有变化" in body and "旧的一行：3 元" in body and "缺出处" in body
        # Lines without a source cannot be ticked; the cited page shows on the left.
        assert page.locator("[data-line-tick='42ef3975026d82f5']").is_disabled()
        page.wait_for_selector(".fs-viewer img")
        assert page.locator("[data-fee-next]").is_disabled()
        assert "下一步" in page.inner_text(".fs-foot") and "保存草稿" not in page.inner_text("[data-fee-editor]")
        list_text = page.inner_text("[data-fee-list]")
        assert "上楼费" in list_text and "已核对 0 / 3 行" in list_text
        _shot(page, "62-check")
        # 2026-11-23: a tick re-renders the list but keeps its scroll position (it used to jump to the top).
        page.add_style_tag(content=".fs-lines { max-height: 90px !important; }")
        scrolled = page.evaluate("""() => { const box = document.querySelector('.fs-lines__body');
            box.scrollTop = box.scrollHeight; return box.scrollTop; }""")
        assert scrolled > 0
        page.evaluate("document.querySelector(\"[data-line-tick='38914bb1e4808a64']\").click()")
        assert page.evaluate("document.querySelector('.fs-lines__body').scrollTop") > 0
        page.wait_for_timeout(700)
        ticked = [body for path, body in posts if path.endswith("/checks")][-1]
        assert ticked == {"row_version": 1, "checks": [
            {"kind": "question", "text": "混装时怎么收？", "options": ["需人工判断（现在的做法）", "其他（写明做法）"], "answer": None, "note": ""},
            {"kind": "line", "sig": "38914bb1e4808a64", "done": True}]}
        # 改 opens the line's own fields in place; edits save by themselves about a second later.
        page.click("[data-line-edit='formula.else.tiers.1.then']")
        page.fill("[data-f='formula.else.tiers.1.then.rate']", "0.2")
        page.wait_for_timeout(1500)
        saved = [body for path, body in posts if path.endswith("/save")][-1]
        assert saved["content"]["formula"]["else"]["tiers"][1]["then"]["rate"] == "0.2" and saved["fee_name"] == "上楼费"
        assert "已自动保存" in page.inner_text(".fs-foot")
        # A later step stays closed until the open one is done.
        page.click("[data-fee-step='3']")
        assert "先完成第 ② 步" in page.inner_text("[data-fee-body]")
        page.click("[data-fee-step='0']")
        assert "规则初稿已经有了" in page.inner_text("[data-fee-body]")
        page.click("[data-fee-structure]")
        page.wait_for_selector(".hb-fold")
        assert errors == []


def test_fee_rule_questions_trial_and_submit(browser):
    context, page, errors, posts = _open(browser, EDITOR)
    with context:
        _serve_rule(page, rule(ticked=True))
        page.reload()
        page.wait_for_selector(".fs-q")
        assert "已回答 0 / 1" in page.inner_text("[data-fee-body]") and "没有「保存」按钮" in page.inner_text("[data-fee-body]")
        page.check("input[data-answer='0'][value='其他（写明做法）']")
        page.wait_for_selector("[data-answer-note='0']")
        assert "回第 ① 步修改规则结构" in page.inner_text("[data-fee-body]")
        page.fill("[data-answer-note='0']", "混装按重的一件收")
        page.wait_for_timeout(800)
        answered = [body for path, body in posts if path.endswith("/checks")][-1]
        assert answered["checks"][0]["answer"] == "其他（写明做法）" and answered["checks"][0]["note"] == "混装按重的一件收"
        _shot(page, "63-decide")
        answered_rule = rule(ticked=True, checks=[{**RULE["checks"][0], "answer": "需人工判断（现在的做法）"}])
        _serve_rule(page, answered_rule)
        page.reload()
        page.wait_for_selector("[data-fee-trial-form]")
        page.fill("[data-param='weight']", "200")
        page.fill("[data-param='floor']", "5")
        page.select_option("[data-param='elevator']", "false")
        page.press("[data-param='floor']", "Enter")
        page.wait_for_selector(".fs-result")
        trial = [body for path, body in posts if path.endswith("/trial")][-1]
        assert trial == {"values": {"weight": "200", "floor": 5, "elevator": False}, "boundary": False}
        result = page.inner_text("[data-fee-body]")
        assert "¥20.00" in result and "未生效 · 只供核对" in result and "合计 20.00" in result
        page.click("[data-fee-trial='boundary']")
        page.wait_for_selector(".fs-table")
        assert "¥ 20.00" in page.inner_text(".fs-table")
        _shot(page, "64-trial")
        ready = {**answered_rule, "trial_count": 1, "effective_from": "2026-12-01", "submit_blockers": [],
                 "events": [{"action": "TRIAL", "actor_id": "6", "created_at": "2026-11-15T10:20:00", "detail": {"values": {"weight": "200"}, "amount": "20.00", "outcome": "AMOUNT"}},
                            *RULE["events"]]}
        _serve_rule(page, ready)
        page.reload()
        page.wait_for_selector("[data-fee-submit]")
        body = page.inner_text("[data-fee-body]")
        assert "可以提交：核对、拍板、试算都已完成" in body and "2026-12-01（含）之后" in body and "2026-11-30 及之前" in body
        assert "仍按 示例手册 V0" in body and "1 个场景" in body and "3 / 3 行全部核对" in body
        assert page.locator("[data-fee-body] [data-fee-submit], [data-fee-body] [data-fee-action='submit']").count() == 0  # only in the footer
        _shot(page, "65-submit")
        page.click("[data-fee-submit]")
        dialog = page.locator("dialog.hb-dialog")
        assert "期间不可修改" in dialog.inner_text()
        dialog.locator("button[value='ok']").click()
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('已提交审核')")
        assert [body for path, body in posts if path.endswith("/submit")][-1] == {"row_version": 1}
        assert errors == []


def test_editor_drafts_one_fee_from_a_manual_inside_a_draft(browser):
    context, page, errors, posts = _open(browser, EDITOR)
    with context:
        empty = rule(empty=True, checks=[], compare=None, content={"schema": 1, "params": [], "formula": {"type": "none", "outcome": "MANUAL", "reason": "规则还没有填写。"}, "reminders": []},
                     params=[], progress={"lines": 0, "ticked": 0, "questions": 0, "answered": 0})
        _serve_rule(page, empty)
        page.reload()
        page.wait_for_selector(".fs-choice")
        body = page.inner_text("[data-fee-body]")
        assert "从手册起草" in body and "手动填写" in body and "推荐" in body and "起草完成后，这一步会变成摘要" in body
        assert "先完成起草" in page.inner_text(".fs-foot") and page.locator("[data-fee-next]").is_disabled()
        assert "卡在第 ① 步" in page.inner_text("[data-fee-list]")
        _shot(page, "59-draft-empty")
        page.click("[data-fee-from-manual]")
        page.click("[data-draft-doc='n1']")
        page.wait_for_selector("[data-draft-chapter='1']")
        assert page.locator("[data-draft-chapter='0']").is_disabled()  # 30 pages is too long for one draft
        page.check("[data-draft-chapter='1']")
        page.click("[data-draft-start]")
        page.wait_for_selector(".fs-progress")
        progress = page.inner_text("[data-fee-body]")
        assert "进行中" in progress and "读取手册" in progress and "AI 起草" in progress and "取消起草" in progress
        assert "AI 起草中，先等它完成" in page.inner_text(".fs-foot")
        start = [body for path, body in posts if path == "/handbook/fee-drafts/jobs"][-1]
        assert start == {"node_token": "n1", "start": 2, "end": 3, "chapter": "第一章 上楼", "rule_id": RULE_ID}
        _shot(page, "60-drafting")
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('起草完成')", timeout=12000)
        assert errors == []


def test_draft_failure_offers_retry_and_manual(browser):
    context, page, errors, posts = _open(browser, EDITOR)
    with context:
        empty = rule(empty=True, checks=[], compare=None, progress={"lines": 0, "ticked": 0, "questions": 0, "answered": 0})
        _serve_rule(page, empty)
        failed = {**DRAFT_JOB, "status": "FAILED", "result": None, "error": "第 2–3 页没有找到“上楼费”的收费，没有生成初稿。"}
        page.route("**/handbook/fee-drafts/jobs?rule_id=*", lambda route: route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": {"job": failed}}, ensure_ascii=False)))
        page.reload()
        page.wait_for_selector(".fs-fail")
        body = page.inner_text("[data-fee-body]")
        assert "需要处理" in body and "第 2–3 页没有找到" in body and "上次尝试" in body and "重新起草" in body
        assert "起草失败，先重试或换范围" in page.inner_text(".fs-foot")
        _shot(page, "61-draft-failed")
        page.click("[data-draft-manual]")
        page.wait_for_selector("[data-json]")
        assert errors == []


def test_menu_deletes_a_never_submitted_draft(browser):
    context, page, errors, posts = _open(browser, EDITOR)
    with context:
        page.wait_for_selector(".fs-line")
        page.click("[data-fee-menu]")
        menu = page.inner_text(".fs-menu")
        assert "删除草稿" in menu and "重命名" in menu and "作废" not in menu
        page.click("[data-fee-menu-item='delete']")
        dialog = page.locator("dialog.hb-dialog")
        assert "删除后无法恢复" in dialog.inner_text()
        dialog.locator("button[value='ok']").click()
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('已删除草稿')")
        assert [body for path, body in posts if path.endswith("/delete")][-1] == {"row_version": 1}
        assert errors == []


def test_a_conflict_offers_reload_instead_of_overwriting(browser):
    context, page, errors, posts = _open(browser, EDITOR)
    with context:
        page.wait_for_selector(".fs-line")
        page.route(f"**/handbook/fee-rules/{RULE_ID}/save", lambda route: route.fulfill(status=409, content_type="application/json",
                   body=json.dumps({"ok": False, "message": "这个版本刚被别人修改过，请刷新后再操作。"}, ensure_ascii=False)))
        page.click("[data-fee-step='0']")
        page.fill("[data-meta='fee_name']", "上楼费（改）")
        page.wait_for_selector(".fs-conflict", timeout=4000)
        assert "已被 示例编辑 修改" in page.inner_text(".fs-conflict") and "载入最新" in page.inner_text(".fs-conflict")
        _shot(page, "69-conflict")
        page.click("[data-fee-reload]")
        page.wait_for_function("!document.querySelector('.fs-conflict')")
        assert errors == []


def test_a_knowledge_reminder_opens_drafting_for_that_document(browser):
    context, page, errors, posts = _open(browser, EDITOR)
    with context:
        page.wait_for_selector(".fs-line")
        page.click("[data-handbook-tab='sources']")
        page.click("[data-draft-from='n2']")
        dialog = page.locator("dialog.hb-dialog")
        dialog.locator("[data-dialog-input]").fill("示例调整费")
        dialog.locator("button[value='ok']").click()
        page.wait_for_selector("[data-draft-chapter='1']")
        assert page.locator("[data-handbook-tab='fees']").get_attribute("aria-selected") == "true"
        assert [body for path, body in posts if path == "/handbook/fee-rules"][-1] == {"fee_name": "示例调整费"}
        assert "示例物流手册 V1.pdf" in page.inner_text("[data-fee-editor]")
        assert not errors


def test_narrow_screen_keeps_the_list_in_a_drawer(browser):
    context, page, errors, _posts = _open(browser, EDITOR)
    with context:
        page.set_viewport_size({"width": 1180, "height": 900})
        page.wait_for_selector(".fs-line")
        assert page.locator(".fs-list").bounding_box()["x"] < 0  # off screen
        page.click(".fs-ws [data-fee-drawer]")
        page.wait_for_timeout(300)
        assert page.locator(".fs-list").bounding_box()["x"] >= 0
        page.click(f"[data-fee-open='{RULE_ID}']")
        page.wait_for_timeout(300)
        assert page.locator(".fs-list").bounding_box()["x"] < 0
        assert errors == []


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
        _entries(page)
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
        page.wait_for_selector(".fs-line")
        assert page.locator("[data-fee-create], [data-fee-draft], [data-line-tick], [data-fee-menu]").count() == 0
        assert page.locator("[data-meta='fee_name']").count() == 0 or page.locator("[data-meta='fee_name']").is_disabled()
        _entries(page)
        page.click(f"[data-entry='{ENTRY_ID}']")
        page.wait_for_selector(".hb-steps")
        assert page.locator("[data-handbook-save], [data-handbook-compile], [data-handbook-new]").count() == 0
        assert "只读 · 仅查看" in page.inner_text(".hb-role")
        page.click("[data-handbook-tab='sources']")
        page.wait_for_selector(".hb-reminder")
        assert page.locator("[data-check], [data-sources-scan]").count() == 0
        assert errors == []


def test_super_admin_reviews_and_sees_the_overview(browser):
    context, page, errors, posts = _open(browser, {"id": 1, "role": "super_admin", "is_legacy_basic_auth": False})
    with context:
        pending = rule(ticked=True, status="PENDING", effective_from="2026-12-01", submitted_by="6", submitted_at="2026-11-22T08:40:00",
                       checks=[{**RULE["checks"][0], "answer": "需人工判断（现在的做法）"}], trial_count=1, blockers=[], submit_blockers=[], can_delete=False)
        _serve_rule(page, pending)
        page.reload()
        page.wait_for_selector(".fs-await")
        assert page.locator(".fs-step__lock").count() == 5
        body = page.inner_text("[data-fee-body]")
        assert "已提交，等待你审核" in body and "提交人 示例编辑" in body and "收费表（供核对，不可改）" in body
        assert "等待你审核" in page.inner_text(".fs-foot")
        _shot(page, "66-review-admin")
        page.click("[data-fee-action='approve']")
        dialog = page.locator("dialog.hb-dialog")
        assert "2026-12-01" in dialog.inner_text()
        dialog.locator("button[value='cancel']").click()
        assert not [path for path, _body in posts if path.endswith("/approve")]
        page.click("[data-fee-menu]")
        assert "作废此版本" in page.inner_text(".fs-menu") and "删除草稿" not in page.inner_text(".fs-menu")
        page.keyboard.press("Escape")
        approved = {**pending, "status": "APPROVED", "approved_by": "1", "approved_at": "2026-11-22T09:05:00"}
        _serve_rule(page, approved)
        page.reload()
        page.wait_for_selector(".fs-tiles")
        assert page.locator(".fs-steps").count() == 0
        body = page.inner_text("[data-fee-body]")
        assert "生效概览" in body and "长期有效" in body and "全部有手册出处" in body and "复制为新草稿以修改" in body
        assert "只读 · 已生效" in page.inner_text(".fs-foot")
        _shot(page, "68-approved")
        page.click("[data-fee-action='copy']")
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('已复制为新草稿')")
        assert errors == []


def test_editor_waits_while_a_version_is_in_review(browser):
    context, page, errors, posts = _open(browser, EDITOR)
    with context:
        pending = rule(ticked=True, status="PENDING", effective_from="2026-12-01", submitted_by="6", submitted_at="2026-11-22T08:40:00",
                       checks=[{**RULE["checks"][0], "answer": "需人工判断（现在的做法）"}], trial_count=1, blockers=[], submit_blockers=[], can_delete=False)
        _serve_rule(page, pending)
        page.reload()
        page.wait_for_selector(".fs-wait")
        body = page.inner_text("[data-fee-body]")
        assert "等待超级管理员审核" in body and "撤回为草稿" in body and "前面的步骤是锁住的" in body
        assert page.locator("[data-fee-action='approve']").count() == 0
        page.click("[data-fee-step='2']")
        assert page.locator("input[data-answer]").first.is_disabled()
        _shot(page, "67-review-editor")
        page.click("[data-fee-step='5']")
        page.click("[data-fee-action='withdraw']")
        page.locator("dialog.hb-dialog button[value='ok']").click()
        page.wait_for_function("document.querySelector('[data-fee-status]').innerText.includes('已撤回为草稿')")
        assert errors == []
