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


    def _pending_app(self, rows, status_rows=()):
        from contextlib import contextmanager
        from unittest import mock

        app, calls = self._app(lambda endpoint: {"ok": True, "data": {"problems": list(status_rows)}})

        @contextmanager
        def connect():
            yield object()

        app.repository.connect = connect
        fake = mock.Mock()
        fake.return_value.query.return_value = {"rows": rows, "errors": []}
        return app, calls, mock.patch("shared.customer_service_repository.CustomerServiceRepository", fake)

    def test_pending_summary_lists_unanswered_problems_oldest_first_with_ai_tier(self):
        rows = [
            {**ITEM, "external_id": "new", "status": "待处理", "reply_text": "", "resolved": False, "updated_at": "2026-11-09 10:00"},
            {**ITEM, "external_id": "old", "status": "未回复", "reply_text": "", "resolved": False, "updated_at": "2026-11-08 09:00"},
            {**ITEM, "external_id": "done", "status": "已处理", "reply_text": "好的", "resolved": False, "updated_at": "2026-11-07"},
        ]
        scenario = {"id": "urge", "name": "催件", "tier": "L1", "locked": False}
        app, calls, patched = self._pending_app(rows, [{**{k: ITEM[k] for k in ("source_id", "source_direction")},
                                                         "external_id": "old", "scenario": scenario}])
        with patched:
            app._handle_customer_service_pending_summary(_Handler())
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual(2, app.sent_payload["total"])
        self.assertEqual(["old", "new"], [item["external_id"] for item in app.sent_payload["items"]])
        self.assertEqual(scenario, app.sent_payload["items"][0]["ai_scenario"])
        self.assertIsNone(app.sent_payload["items"][1].get("ai_scenario"))
        self.assertEqual("/internal/v1/admin/customer-service/ai/status", calls[0]["endpoint"])

    def test_pending_summary_survives_an_unavailable_ai_status(self):
        rows = [{**ITEM, "status": "待处理", "reply_text": "", "resolved": False, "updated_at": "2026-11-09"}]
        app, calls, patched = self._pending_app(rows)
        app._agent_request = lambda *args, **kwargs: {"ok": False, "status": 503}
        with patched:
            app._handle_customer_service_pending_summary(_Handler())
        self.assertEqual(1, app.sent_payload["total"])
        self.assertNotIn("ai_scenario", app.sent_payload["items"][0])

    def test_stats_proxy_reads_the_agent(self):
        app, calls = self._app(lambda endpoint: {"ok": True, "data": {"scenarios": [], "totals": {}}})
        app._handle_customer_service_ai_stats(_Handler())
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual("/internal/v1/admin/customer-service/ai/stats", calls[0]["endpoint"])

    def test_deep_link_prefills_the_page_and_names_the_problem(self):
        app = base_module.CustomerServiceModuleTests()._build_app()
        key = "|".join(ITEM[k] for k in ("source_id", "external_id", "source_direction"))
        app._render_customer_service(_Handler(), {"open": [key], "q": ["YD4312891203"]})
        self.assertIn(f'data-open-problem="{key}"', app.sent_html)
        self.assertIn('value="YD4312891203"', app.sent_html)
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        self.assertIn("runQuery().then(openRequestedProblem)", script)
        self.assertIn("console:querychange", script)

    def test_overview_block_is_wired_and_hidden_without_permission(self):
        portal = (CONSOLE_DIR / "templates" / "portal.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "overview_pending.js").read_text(encoding="utf-8")
        self.assertIn("data-overview-pending hidden", portal)
        self.assertIn("/static/overview_pending.js", portal)
        self.assertIn("/customer-service/pending-summary", script)
        self.assertIn("response.status === 403", script)
        self.assertIn("data-console-navigate", script)

    TEMPLATE_ID = "33333333-4444-4555-8666-777777777777"

    def test_template_review_forwards_edits_only_on_approve(self):
        app, calls = self._app(lambda endpoint: {"ok": True, "data": {"template_id": self.TEMPLATE_ID}})
        app._handle_customer_service_ai_template_review(_handler({
            "action": "approve", "reply_template": "您好，{网点}已安排。", "conditions": ["已到站"], "extra": 1}), self.TEMPLATE_ID)
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual(f"/internal/v1/admin/customer-service/ai/templates/{self.TEMPLATE_ID}/review", calls[0]["endpoint"])
        self.assertEqual({"request_id", "action", "reason", "reply_template", "conditions"}, set(calls[0]["payload"]))
        app._handle_customer_service_ai_template_review(_handler({"action": "retire", "reply_template": "x"}), self.TEMPLATE_ID)
        self.assertNotIn("reply_template", calls[1]["payload"])

    def test_template_review_refuses_unknown_actions_and_ids(self):
        app, calls = self._app(lambda endpoint: self.fail("must not call the Agent"))
        app._handle_customer_service_ai_template_review(_handler({"action": "delete"}), self.TEMPLATE_ID)
        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent_status)
        app._handle_customer_service_ai_template_review(_handler({"action": "approve"}), "not-an-id")
        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent_status)

    def test_template_review_maps_state_conflicts(self):
        app, _ = self._app(lambda endpoint: {"ok": False, "status": 409,
                                             "error": {"code": "TEMPLATE_STATE_CHANGED", "message": "模板状态已变化"}})
        app._handle_customer_service_ai_template_review(_handler({"action": "approve"}), self.TEMPLATE_ID)
        self.assertEqual(409, app.sent_status)
        self.assertEqual("TEMPLATE_STATE_CHANGED", app.sent_payload["error_code"])

    def test_template_routes_and_page_wiring(self):
        import routes.customer_service as customer_routes

        seen = []
        app = types.SimpleNamespace(
            _handle_customer_service_ai_templates=lambda handler: seen.append("list"),
            _handle_customer_service_ai_template_review=lambda handler, template_id: seen.append(template_id))
        self.assertTrue(customer_routes.handle_get(app, None, "/customer-service/ai-templates", "", {}))
        self.assertTrue(customer_routes.handle_post(app, None, f"/customer-service/ai-templates/{self.TEMPLATE_ID}/review", "", {}))
        self.assertFalse(customer_routes.handle_post(app, None, "/customer-service/ai-templates/x/review", "", {}))
        self.assertEqual(["list", self.TEMPLATE_ID], seen)
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        for marker in ("data-cs-templates-open", "data-cs-templates-body", "data-cs-tpl-count"):
            self.assertIn(marker, template)
        for marker in ("/customer-service/ai-templates", "data-cs-tpl-action", "已按模板", "template_not_followed"):
            self.assertIn(marker, script)

    def test_authorize_and_revoke_are_forwarded_without_edits(self):
        app, calls = self._app(lambda endpoint: {"ok": True, "data": {}})
        for action in ("authorize", "revoke"):
            app._handle_customer_service_ai_template_review(_handler({"action": action, "reply_template": "x"}), self.TEMPLATE_ID)
            self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual(["authorize", "revoke"], [call["payload"]["action"] for call in calls])
        self.assertTrue(all("reply_template" not in call["payload"] for call in calls))

    def test_flagging_an_automatic_reply_forwards_the_problem_identity(self):
        app, calls = self._app(lambda endpoint: {"ok": True, "data": {"auto_revoked": True}})
        app._handle_customer_service_ai_flag_auto_reply(_handler({"item": {**ITEM, "raw": 1}, "reason": "网点错了"}))
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual("/internal/v1/admin/customer-service/ai/auto-replies/flag", calls[0]["endpoint"])
        self.assertEqual({"request_id", "source_id", "external_id", "source_direction", "reason"}, set(calls[0]["payload"]))
        app._handle_customer_service_ai_flag_auto_reply(_handler({"item": {"external_id": "x"}}))
        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent_status)
        import routes.customer_service as customer_routes
        seen = []
        stub = types.SimpleNamespace(_handle_customer_service_ai_flag_auto_reply=lambda handler: seen.append(True))
        self.assertTrue(customer_routes.handle_post(stub, None, "/customer-service/ai-auto-replies/flag", "", {}))
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        for marker in ('data-cs-tpl-action="authorize"', "data-cs-auto-flag", "AI 已自动回复"):
            self.assertIn(marker, script)

if __name__ == "__main__":
    unittest.main()
