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
RULE = {
    "rule_id": RULE_ID, "fee_kind": "upstairs", "fee_name": "上楼费", "version_label": "示例手册 V1 草稿", "status": "DRAFT",
    "effective_from": None, "row_version": 1, "updated_at": "2026-11-15T10:12:00",
    "content": {"base": {"fixed": "10", "per_kg": "0.12", "source": SOURCE}, "elevator": {"fixed": "5", "per_kg": "0.06", "source": SOURCE},
                "floor_tiers": [{"floor_from": 2, "floor_to": 7, "per_kg": "0.1", "source": SOURCE}], "max_floor_without_elevator": 7,
                "max_floor_source": SOURCE, "first_floor": "MANUAL", "piece_tiers": [{"over_kg": "50", "up_to_kg": "120", "per_piece": "10", "source": {}}],
                "piece_limit_kg": "120", "piece_limit_source": SOURCE, "reminders": ["示例提醒"]},
    "checks": [{"text": "逐格核对第 1 页", "done": False}], "events": [{"action": "SAVE", "actor_id": "6", "created_at": "2026-11-15T10:12:00"}],
    "trial_count": 0, "people": {"6": "示例编辑"},
    "submit_blockers": ["还没有填写执行日期（以总部通知为准）。", "保存后还没有试算过，请至少试算一个场景。"],
}
SOURCES = {
    "last_scan": {"at": "2026-11-15T08:30:00+08:00", "documents": 2, "reminders": 1, "baseline": False},
    "documents": [{"node_token": "n1", "title": "示例物流手册", "space": "示例空间", "edit_time": "1763170000", "cited_by": ["上楼费 示例手册 V1"]},
                  {"node_token": "n2", "title": "示例收费调整通知", "space": "示例空间", "edit_time": "1763170000", "cited_by": []}],
    "reminders": [{"reminder_id": 7, "node_token": "n2", "kind": "NEW", "title": "示例收费调整通知", "detail": "标题像收费通知", "status": "OPEN", "detected_at": "2026-11-15T00:30:00"}],
}
TRIAL = {"result": {"outcome": "AMOUNT", "amount": "54.00", "steps": ["基础 10 + 0.12×200 = 34", "楼层加收（5 楼）0.1×200 = 20", "合计 54.00"],
                    "citations": ["示例物流手册 V1 第一章 第 1 页"], "reminders": ["示例提醒"], "rule_version": "示例手册 V1 草稿", "reportable": False},
         "boundary": [{"elevator": False, "floor": 5, "piece_counts": [0], "pieces_over_limit": 0, "outcome": "AMOUNT", "amount": "54.00"},
                      {"elevator": False, "floor": 8, "piece_counts": [0], "pieces_over_limit": 0, "outcome": "NEGOTIATE", "amount": None}]}
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
    errors, posts = [], []
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
            reply = {"versions": [{k: v for k, v in RULE.items() if k not in {"content", "checks", "events"}}]}
        elif path == f"/handbook/fee-rules/{RULE_ID}":
            reply = RULE
        elif path == f"/handbook/fee-rules/{RULE_ID}/trial":
            reply = {**TRIAL, "trial_count": 1}
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
        _shot(page, "52-entries")
        page.click("[data-handbook-answer]")
        page.check("input[name='hb-answer-1'][value='不包含']")
        page.click("[data-handbook-answer-save]")
        page.wait_for_function("document.querySelector('[data-handbook-status]').innerText.includes('已保存确认')")
        page.click("[data-handbook-tab='fees']")
        page.click(f"[data-fee-open='{RULE_ID}']")
        page.wait_for_selector(".hb-fold")
        assert "缺出处" in page.inner_text("[data-fee-editor]")
        side = page.inner_text(".hb-side")
        assert "生效检查" in side and "还差 4 项" in side and "0 / 4 项通过" in side and "保存后还没有试算" in side
        assert page.locator("[data-fee-action='submit']").is_disabled()
        page.fill("[data-meta='version_label']", "示例手册 V1 草稿二")
        assert page.locator("[data-fee-action='submit']").is_disabled()
        _shot(page, "54-fees")
        page.click("[data-fee-tab='trial']")
        page.click("[data-fee-trial='boundary']")
        page.wait_for_selector(".hb-trial__amount")
        assert "未生效 · 只供核对" in page.inner_text("[data-fee-editor]")
        _shot(page, "55-trial")
        page.fill("[data-t='weight']", "0")
        page.click("[data-fee-trial]")
        assert "输入不合理" in page.inner_text("[data-fee-status]")
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
