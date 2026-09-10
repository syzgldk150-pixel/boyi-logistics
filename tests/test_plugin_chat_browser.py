"""Real browser UI recovery test; HTTP responses are explicit UI fixtures."""
import json
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import sync_playwright


def test_chat_confirmation_recovery_keeps_original_request_and_selection():
    root = Path(__file__).resolve().parents[1]
    template = (root / "console/templates/harness.html").read_text(encoding="utf-8")
    body = template.split('{% block content %}', 1)[1].split('{% endblock %}', 1)[0]
    session, preview_id, formal_id = [str(uuid4()) for _ in range(3)]
    confirmations = []
    errors = []
    def route(request):
        path = request.request.url.rsplit("/", 1)[-1]
        if path == "harness":
            request.fulfill(status=200, content_type="text/html; charset=utf-8", body='<meta charset="utf-8">' + body + '<script src="/harness.js"></script>')
            return
        if path == "harness.js":
            request.fulfill(status=200, content_type="application/javascript", body=(root / "console/static/harness.js").read_text(encoding="utf-8"))
            return
        payload = request.request.post_data_json
        if path == "sessions":
            data = {"session_id": session, "status": "READY", "tools": []}
        elif path == "messages":
            data = {"session_id": session, "result": "候选正在读取。", "plugin_invocations": [{"invocation_id": preview_id, "title": "自提测试", "status": "RUNNING"}]}
        elif payload["action"] == "confirm":
            confirmations.append(payload)
            if len(confirmations) == 1:
                # The plugin is accepted, but the browser loses that response.
                request.fulfill(status=503, content_type="application/json", body=json.dumps({"ok": False, "error": {"code": "UNREACHABLE", "message": "response lost"}}))
                return
            assert payload["request_uuid"] == confirmations[0]["request_uuid"]
            assert payload["selected_indices"] == [1]
            data = {"session_id": session, "plugin_invocations": [{"invocation_id": formal_id, "status": "COMPLETED", "summary": "处理记录：1 条", "result_text": '{"verified":true}'}]}
        else:
            data = {"session_id": session, "plugin_invocations": [{"invocation_id": preview_id, "status": "COMPLETED", "summary": "本次候选", "preview": {"state": "AVAILABLE", "can_confirm": True, "kind": "selection", "candidates": [{"index": 0, "label": "R-ONE"}, {"index": 1, "label": "R-TWO"}]}}]}
        request.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(error))
        page.route("http://localhost:19419/**", route)
        page.goto("http://localhost:19419/harness")
        page.locator("[data-harness-message]").fill("执行自提测试")
        page.locator("[data-harness-submit]").click()
        page.get_by_label("R-TWO", exact=True).check()
        assert not errors
        page.get_by_role("button", name="确认执行", exact=True).click()
        page.get_by_role("button", name="读取本次操作结果", exact=True).click()
        page.get_by_text("处理记录：1 条", exact=True).wait_for()
        assert len(confirmations) == 2
        assert confirmations[0]["selected_indices"] == [1]
        assert not errors
        browser.close()
