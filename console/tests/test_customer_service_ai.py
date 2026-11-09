"""Console side of customer-service AI phase 1: proxies, case recording after a reply, and page wiring."""
import json
import types
import unittest
from http import HTTPStatus

import test_customer_service_module as base_module
from test_customer_service_module import BROWSER_REQUEST_UUID, CONSOLE_DIR, _Handler

ITEM = {"source_id": "11111111-2222-4333-8444-555555555555", "external_id": "problem-1",
        "source_direction": "received", "waybill_no": "YD4312891203", "status": "待处理"}


def _handler(body):
    return _Handler(json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    {"Content-Type": "application/json", "X-Browser-Request-UUID": BROWSER_REQUEST_UUID})


class CustomerServiceAIConsoleTests(unittest.TestCase):
    def _app(self, responses):
        app = base_module.CustomerServiceModuleTests()._build_app()
        calls = []

        def agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
            calls.append({"endpoint": endpoint, "payload": payload, "principal": console_principal})
            return responses(endpoint)

        app._agent_request = types.MethodType(agent_request, app)
        return app, calls

    def test_suggest_forwards_only_the_problem_identity_with_the_admin_principal(self):
        app, calls = self._app(lambda endpoint: {"ok": True, "data": {"suggestion_id": "s"}})
        app._handle_customer_service_ai_suggest(_handler({"item": {**ITEM, "raw": {"token": "x"}}}))
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual("/internal/v1/admin/customer-service/ai/suggest", calls[0]["endpoint"])
        self.assertEqual({"request_id", "source_id", "external_id", "source_direction"}, set(calls[0]["payload"]))
        self.assertEqual(BROWSER_REQUEST_UUID, calls[0]["payload"]["request_id"])
        self.assertEqual("9", calls[0]["principal"]["actor_id"])

    def test_suggest_without_a_local_record_is_refused_before_the_agent(self):
        app, calls = self._app(lambda endpoint: self.fail("must not call the Agent"))
        app._handle_customer_service_ai_suggest(_handler({"item": {"external_id": "problem-1"}}))
        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent_status)
        self.assertEqual([], calls)

    def test_suggest_maps_agent_failures(self):
        app, _ = self._app(lambda endpoint: {"ok": False, "status": 503,
                                             "error": {"code": "CS_AI_UNAVAILABLE", "message": "尚未启用 AI 模型"}})
        app._handle_customer_service_ai_suggest(_handler({"item": ITEM}))
        self.assertEqual(503, app.sent_status)
        self.assertEqual("CS_AI_UNAVAILABLE", app.sent_payload["error_code"])

    def test_status_skips_rows_without_identity(self):
        app, calls = self._app(lambda endpoint: {"ok": True, "data": {"problems": []}})
        app._handle_customer_service_ai_status(_handler({"problems": [{"external_id": "x"}]}))
        self.assertEqual([], calls)
        app._handle_customer_service_ai_status(_handler({"problems": [ITEM, {"external_id": "x"}]}))
        self.assertEqual([{k: ITEM[k] for k in ("source_id", "external_id", "source_direction")}],
                         calls[0]["payload"]["problems"])

    def _reply(self, app, suggestion_id="22222222-3333-4444-8555-666666666666"):
        app._handle_customer_service_problem_agent_action(_handler({
            "platform": "ronghui", "account_id": "ronghui-a", "item": ITEM, "ai_suggestion_id": suggestion_id,
            "payload": {"reply_text": "您好，已安排派送。", "prob_status": "已处理", "old_prob_status": "待处理"},
        }), "reply")

    def test_successful_reply_is_recorded_as_a_case(self):
        app, calls = self._app(lambda endpoint: {"ok": True, "data": {"case_id": 3, "suggestion_use": "edited"}}
                               if "ai/cases" in endpoint else {"ok": True, "data": {"ok": True}})
        self._reply(app)
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual(["/internal/v1/business/customer-service-reply", "/internal/v1/admin/customer-service/ai/cases"],
                         [call["endpoint"] for call in calls])
        case = calls[1]["payload"]
        self.assertEqual(BROWSER_REQUEST_UUID, case["request_id"])
        self.assertEqual("您好，已安排派送。", case["reply_text"])
        self.assertEqual("22222222-3333-4444-8555-666666666666", case["suggestion_id"])
        self.assertEqual({"recorded": True, "case_id": 3, "suggestion_use": "edited"}, app.sent_payload["ai_case"])

    def test_case_failure_never_undoes_a_sent_reply(self):
        app, calls = self._app(lambda endpoint: {"ok": False, "status": 503, "error": {"message": "数据库忙"}}
                               if "ai/cases" in endpoint else {"ok": True, "data": {"ok": True}})
        self._reply(app, suggestion_id="not-a-uuid")
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertTrue(app.sent_payload["ok"])
        self.assertFalse(app.sent_payload["ai_case"]["recorded"])
        self.assertIsNone(calls[1]["payload"]["suggestion_id"])

    def test_failed_reply_records_no_case(self):
        app, calls = self._app(lambda endpoint: {"ok": False, "status": 502, "error": "原平台拒绝"})
        self._reply(app)
        self.assertEqual(1, len(calls))
        self.assertFalse(app.sent_payload["ok"])

    def test_page_wires_the_ai_block_column_and_help(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        for marker in ("data-cs-ai-block", "data-cs-ai-tier", "data-cs-ai-hint", "<th>AI 处理</th>", "data-cs-ai-help"):
            self.assertIn(marker, template)
        self.assertIn('colspan="9"', template)
        self.assertIn("/customer-service/problems/ai-suggest", script)
        self.assertIn("/customer-service/problems/ai-status", script)
        self.assertIn("ai_suggestion_id: state.ai.suggestionId", script)
        # The draft only fills an empty reply box; the person still presses the reply button.
        self.assertIn("!nodes.reply.value.trim()", script)


if __name__ == "__main__":
    unittest.main()
