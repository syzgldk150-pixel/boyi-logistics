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
         "validation": {"ok": True}, "acknowledged": [0],
         "compiled": {"restatement": "“示例店”指示例区网点；报个价指三平台报价。", "ambiguities": ["“示例店”是否只指示例区？", "“报个价”是否包含单独报价？"],
                      "unsupported": ["希望机器人自动改回单状态"]},
         "described": ["示例店 = 示例区网点", "报个价 = 三平台报价", "先问再答"],
         "trial": {"cases": 2, "passed": 2, "improved": 1, "rows": [
             {"utterance": "示例店在哪", "expected_title": "直接回答", "before": "none", "after": "none", "pass": True},
             {"utterance": "帮我报个价", "expected_title": "三平台报价", "before": "ask_user", "after": "plugin:q", "after_title": "三平台报价", "pass": True}]},
         "blockers": ["还有未处理的歧义"]},
        {"version": 1, "status": "ACTIVE", "body": "旧说明", "updated_at": "2026-11-10T08:00:00"},
    ],
}
RULE = {
    "rule_id": RULE_ID, "fee_kind": "upstairs", "fee_name": "上楼费", "version_label": "示例手册 V1 草稿", "status": "DRAFT",
    "effective_from": None, "row_version": 1, "updated_at": "2026-11-15T10:12:00",
    "content": {"base": {"fixed": "10", "per_kg": "0.12", "source": SOURCE}, "elevator": {"fixed": "5", "per_kg": "0.06", "source": SOURCE},
                "floor_tiers": [{"floor_from": 2, "floor_to": 7, "per_kg": "0.1", "source": SOURCE}], "max_floor_without_elevator": 7,
                "max_floor_source": SOURCE, "first_floor": "MANUAL", "piece_tiers": [{"over_kg": "50", "up_to_kg": "120", "per_piece": "10", "source": {}}],
                "piece_limit_kg": "120", "piece_limit_source": SOURCE, "reminders": ["示例提醒"]},
    "checks": [{"text": "逐格核对第 1 页", "done": False}], "events": [{"action": "SAVE", "created_at": "2026-11-15T10:12:00"}],
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
       "steps": [{"action": "按手册计算费用", "decision": "执行"}]}


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
            reply = {"entries": [{k: v for k, v in ENTRY.items() if k != "versions"}]}
        elif path == f"/handbook/entries/{ENTRY_ID}":
            reply = ENTRY
        elif path == "/handbook/fee-rules":
            reply = {"versions": [{k: v for k, v in RULE.items() if k not in {"content", "checks", "events"}}]}
        elif path == f"/handbook/fee-rules/{RULE_ID}":
            reply = RULE
        elif path == f"/handbook/fee-rules/{RULE_ID}/trial":
            reply = TRIAL
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
        assert "需要你确认 1 项歧义" in result and "不会生效" in result and "还差 1 项" in result
        assert page.locator("[data-handbook-approve]").count() == 0
        assert page.locator("[data-handbook-tab='entries'] [data-tab-count]").inner_text() == "1"
        _shot(page, "52-entries")
        page.click("[data-handbook-tab='fees']")
        page.click(f"[data-fee-open='{RULE_ID}']")
        page.wait_for_selector(".hb-fold")
        assert "缺出处" in page.inner_text("[data-fee-editor]")
        assert "1 处缺出处" in page.inner_text(".hb-side")
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
        _shot(page, "56-sources")
        page.click("[data-handbook-tab='try']")
        page.fill("[data-try-question]", "上楼费怎么收？")
        page.press("[data-try-question]", "Enter")
        page.wait_for_selector(".hb-timeline")
        answer = page.inner_text("[data-try-answer]")
        assert "按手册计算费用" in answer and "示例物流手册" in answer
        _shot(page, "57-try")
        assert errors == []


def test_super_admin_review_needs_confirmation(browser):
    context, page, errors, posts = _open(browser, {"id": 1, "role": "super_admin", "is_legacy_basic_auth": False})
    with context:
        ready = json.loads(json.dumps(ENTRY))
        ready["versions"][0]["acknowledged"] = [0, 1]
        ready["versions"][0]["blockers"] = []
        page.route(f"**/handbook/entries/{ENTRY_ID}", lambda route: route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "data": ready}, ensure_ascii=False)))
        page.click(f"[data-entry='{ENTRY_ID}']")
        page.wait_for_selector("[data-handbook-approve]")
        page.click("[data-handbook-approve]")
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
