"""Console side of the 业务手册 module and the 复核请求 page: access rules and forwarding to the Agent."""
import json
import types
import unittest
from http import HTTPStatus

import test_customer_service_module as base_module
from test_customer_service_module import BROWSER_REQUEST_UUID, _Handler

ENTRY_ID = "11111111-2222-4333-8444-555555555555"


def _handler(body):
    return _Handler(json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    {"Content-Type": "application/json", "X-Browser-Request-UUID": BROWSER_REQUEST_UUID})


class _ConsoleCase(unittest.TestCase):
    def _app(self, responses, roles=("super_admin",)):
        app = base_module.CustomerServiceModuleTests()._build_app()
        calls = []
        context = {"actor_roles": list(roles), "_console_principal": {"actor_id": "9"}}

        def agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
            calls.append({"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout})
            return responses(method, endpoint)

        app._agent_request = types.MethodType(agent_request, app)
        app._control_plane_read_context = lambda handler: context
        app._control_plane_write_context = lambda handler: context
        return app, calls


class HandbookConsoleTests(_ConsoleCase):
    def test_module_permission_decides_access(self):
        from shared.identity_routes import agent_permission, console_permission
        self.assertEqual("handbook:view", console_permission("GET", "/handbook"))
        self.assertEqual("handbook:view", console_permission("GET", "/settings/llm/handbook"))
        self.assertEqual("handbook:view", console_permission("GET", f"/handbook/entries/{ENTRY_ID}"))
        for action in ("save", "compile", "trial", "acknowledge", "pause"):
            self.assertEqual("handbook:operate", console_permission("POST", f"/handbook/entries/{ENTRY_ID}/{action}"))
            self.assertEqual("handbook:operate", agent_permission("POST", f"/internal/v1/admin/handbook/{ENTRY_ID}/{action}"))
        self.assertEqual("handbook:operate", console_permission("POST", "/handbook/entries"))
        for action in ("approve", "resume", "retire"):
            self.assertEqual("super_admin", console_permission("POST", f"/handbook/entries/{ENTRY_ID}/{action}"))
            self.assertEqual("super_admin", agent_permission("POST", f"/internal/v1/admin/handbook/{ENTRY_ID}/{action}"))
        self.assertEqual("handbook:view", agent_permission("GET", "/internal/v1/admin/handbook"))
        # 复核请求 stays under 智能模型 for super administrators only.
        self.assertEqual("super_admin", console_permission("GET", "/settings/llm/reviews"))

    def test_module_editors_reach_the_agent(self):
        app, calls = self._app(lambda *a: {"ok": True, "data": {"entry_id": ENTRY_ID}}, roles=("admin",))
        app._handle_handbook_post(_handler({"title": "t", "body": "b"}), None, None)
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual("/internal/v1/admin/handbook", calls[0]["endpoint"])

    def test_create_and_actions_forward_only_their_fields(self):
        app, calls = self._app(lambda *a: {"ok": True, "data": {"entry_id": ENTRY_ID}})
        app._handle_handbook_post(_handler({"title": "简称", "body": "大家说岳麓店", "actor": "x"}), None, None)
        self.assertEqual(("POST", "/internal/v1/admin/handbook"), (calls[0]["method"], calls[0]["endpoint"]))
        self.assertEqual({"title": "简称", "body": "大家说岳麓店"}, calls[0]["payload"])
        app._handle_handbook_post(_handler({"version": 2, "extra": 1}), ENTRY_ID, "approve")
        self.assertEqual(f"/internal/v1/admin/handbook/{ENTRY_ID}/approve", calls[1]["endpoint"])
        self.assertEqual({"version": 2}, calls[1]["payload"])
        app._handle_handbook_post(_handler({}), ENTRY_ID, "compile")
        self.assertEqual({}, calls[2]["payload"])
        self.assertGreaterEqual(calls[2]["timeout"], 120)

    def test_unknown_action_or_entry_is_refused_locally(self):
        app, calls = self._app(lambda *a: self.fail("must not call the Agent"))
        app._handle_handbook_post(_handler({}), ENTRY_ID, "delete")
        self.assertEqual(HTTPStatus.NOT_FOUND, app.sent_status)
        app._handle_handbook_get(_handler({}), "../etc")
        self.assertEqual(HTTPStatus.NOT_FOUND, app.sent_status)
        self.assertEqual([], calls)

    def test_agent_errors_keep_their_status_and_message(self):
        app, _ = self._app(lambda *a: {"ok": False, "status": 409, "error": {"code": "HANDBOOK_NOT_APPROVABLE", "message": "还有未处理的歧义"}})
        app._handle_handbook_post(_handler({"version": 1}), ENTRY_ID, "approve")
        self.assertEqual(409, app.sent_status)
        self.assertEqual("还有未处理的歧义", app.sent_payload["message"])

    def test_routes_dispatch(self):
        from routes import handbook
        seen = []
        app = types.SimpleNamespace(_render_handbook=lambda h: seen.append("page"),
                                    _redirect=lambda h, location: seen.append(("redirect", location)),
                                    _handle_handbook_get=lambda h, e: seen.append(("get", e)),
                                    _handle_handbook_post=lambda h, e, a: seen.append(("post", e, a)))
        self.assertTrue(handbook.handle_get(app, None, "/handbook", "", {}))
        self.assertTrue(handbook.handle_get(app, None, "/settings/llm/handbook", "", {}))
        self.assertTrue(handbook.handle_get(app, None, f"/handbook/entries/{ENTRY_ID}", "", {}))
        self.assertTrue(handbook.handle_post(app, None, f"/handbook/entries/{ENTRY_ID}/trial", "", {}))
        self.assertFalse(handbook.handle_get(app, None, "/settings/llm", "", {}))
        self.assertFalse(handbook.handle_get(app, None, f"/settings/llm/handbook/entries/{ENTRY_ID}", "", {}))
        self.assertEqual(["page", ("redirect", "/handbook"), ("get", ENTRY_ID), ("post", ENTRY_ID, "trial")], seen)


class ReviewsConsoleTests(_ConsoleCase):
    def test_review_actions_forward_only_the_note(self):
        app, calls = self._app(lambda *a: {"ok": True, "data": {"status": "EXECUTED"}})
        app._handle_review_action(_handler({"note": "x" * 300, "actor": "y"}), ENTRY_ID, "reject")
        self.assertEqual(f"/internal/v1/admin/reviews/{ENTRY_ID}/reject", calls[0]["endpoint"])
        self.assertEqual({"note": "x" * 200}, calls[0]["payload"])
        app._handle_review_action(_handler({}), ENTRY_ID, "approve")
        self.assertEqual({}, calls[1]["payload"])
        app._handle_review_action(_handler({}), "bad", "approve")
        self.assertEqual(HTTPStatus.NOT_FOUND, app.sent_status)

    def test_admins_cannot_list(self):
        app, calls = self._app(lambda *a: self.fail("must not call"), roles=("admin",))
        app._handle_reviews_list(_handler({}))
        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent_status)
        self.assertEqual([], calls)

    def test_admins_cannot_decide(self):
        app, calls = self._app(lambda *a: self.fail("must not call"), roles=("admin",))
        app._handle_review_action(_handler({}), ENTRY_ID, "approve")
        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent_status)


class HandbookPageTests(_ConsoleCase):
    def _render(self, user):
        from console import app_support
        app, _ = self._app(lambda *a: self.fail("must not call"))
        token = app_support._CURRENT_ADMIN_USER.set(user)
        try:
            app._render_handbook(_handler({}))
        finally:
            app_support._CURRENT_ADMIN_USER.reset(token)
        return app.sent_html

    def test_page_shows_editing_only_to_module_editors(self):
        viewer = self._render({"id": 5, "role": "admin", "access_permissions": ("handbook:view",)})
        self.assertIn('data-can-edit="false"', viewer)
        self.assertNotIn("data-handbook-new", viewer)
        self.assertIn("当前身份只能查看", viewer)
        editor = self._render({"id": 6, "role": "admin", "access_permissions": ("handbook:view", "handbook:operate")})
        self.assertIn('data-can-edit="true"', editor)
        self.assertIn('data-super-admin="false"', editor)
        self.assertIn("data-handbook-new", editor)
        owner = self._render({"id": 1, "role": "super_admin", "is_legacy_basic_auth": False})
        self.assertIn('data-super-admin="true"', owner)
        self.assertIn("说明条目", owner)
        self.assertNotIn("返回智能模型", owner)


RULE_ID = "0f6a1c2e-5b7d-4e3a-9c41-61f2e8d0a087"


class FeeRulesConsoleTests(_ConsoleCase):
    def test_permissions_follow_the_handbook_module(self):
        from shared.identity_routes import agent_permission, console_permission
        self.assertEqual("handbook:view", console_permission("GET", "/handbook/fee-rules"))
        self.assertEqual("handbook:view", agent_permission("GET", f"/internal/v1/admin/fee-rules/{RULE_ID}"))
        for action in ("copy", "save", "checks", "trial", "submit", "withdraw"):
            self.assertEqual("handbook:operate", console_permission("POST", f"/handbook/fee-rules/{RULE_ID}/{action}"))
            self.assertEqual("handbook:operate", agent_permission("POST", f"/internal/v1/admin/fee-rules/{RULE_ID}/{action}"))
        for action in ("approve", "retire"):
            self.assertEqual("super_admin", console_permission("POST", f"/handbook/fee-rules/{RULE_ID}/{action}"))
            self.assertEqual("super_admin", agent_permission("POST", f"/internal/v1/admin/fee-rules/{RULE_ID}/{action}"))

    def test_routes_dispatch(self):
        from routes import handbook
        seen = []
        app = types.SimpleNamespace(_handle_fee_rules_get=lambda h, r: seen.append(("get", r)),
                                    _handle_fee_rules_post=lambda h, r, a: seen.append(("post", r, a)))
        self.assertTrue(handbook.handle_get(app, None, "/handbook/fee-rules", "", {}))
        self.assertTrue(handbook.handle_get(app, None, f"/handbook/fee-rules/{RULE_ID}", "", {}))
        self.assertTrue(handbook.handle_post(app, None, f"/handbook/fee-rules/{RULE_ID}/save", "", {}))
        self.assertEqual([("get", None), ("get", RULE_ID), ("post", RULE_ID, "save")], seen)

    def test_each_action_forwards_only_its_fields(self):
        app, calls = self._app(lambda *a: {"ok": True, "data": {}}, roles=("admin",))
        body = {"version_label": "V6.1", "effective_from": "2026-10-01", "content": {"base": {}}, "checks": [],
                "row_version": 3, "status": "APPROVED", "approved_by": "x"}
        app._handle_fee_rules_post(_handler(body), RULE_ID, "save")
        self.assertEqual(f"/internal/v1/admin/fee-rules/{RULE_ID}/save", calls[0]["endpoint"])
        self.assertEqual({"version_label", "effective_from", "content", "checks", "row_version"}, set(calls[0]["payload"]))
        app._handle_fee_rules_post(_handler({"row_version": 4, "status": "APPROVED"}), RULE_ID, "submit")
        self.assertEqual({"row_version": 4}, calls[1]["payload"])
        app._handle_fee_rules_post(_handler({"settlement_weight_kg": "200", "elevator": True, "floor": 3, "x": 1}), RULE_ID, "trial")
        self.assertEqual({"settlement_weight_kg": "200", "elevator": True, "floor": 3}, calls[2]["payload"])

    def test_unknown_action_or_id_is_refused_locally(self):
        app, calls = self._app(lambda *a: self.fail("must not call the Agent"))
        app._handle_fee_rules_post(_handler({}), RULE_ID, "delete")
        self.assertEqual(HTTPStatus.NOT_FOUND, app.sent_status)
        app._handle_fee_rules_get(_handler({}), "../x")
        self.assertEqual(HTTPStatus.NOT_FOUND, app.sent_status)
        self.assertEqual([], calls)

    def test_page_has_the_fee_rules_tab(self):
        html = HandbookPageTests()._render({"id": 1, "role": "super_admin", "is_legacy_basic_auth": False})
        self.assertIn('data-handbook-tab="fees"', html)
        self.assertIn("data-fee-rules", html)
        self.assertIn("/static/handbook_fee_rules.js", html)


class HandbookTryConsoleTests(_ConsoleCase):
    def test_question_is_forwarded_alone(self):
        app, calls = self._app(lambda *a: {"ok": True, "data": {"reply": "以下是……"}}, roles=("admin",))
        app._handle_handbook_try(_handler({"question": " 破损件怎么处理 ", "actor": "x"}))
        self.assertEqual(("POST", "/internal/v1/admin/handbook-try"), (calls[0]["method"], calls[0]["endpoint"]))
        self.assertEqual({"question": "破损件怎么处理"}, calls[0]["payload"])
        self.assertGreaterEqual(calls[0]["timeout"], 60)

    def test_empty_question_is_refused_locally(self):
        app, calls = self._app(lambda *a: self.fail("must not call the Agent"))
        app._handle_handbook_try(_handler({"question": "  "}))
        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent_status)
        self.assertEqual([], calls)

    def test_route_and_tab(self):
        from routes import handbook
        seen = []
        app = types.SimpleNamespace(_handle_handbook_try=lambda h: seen.append("try"))
        self.assertTrue(handbook.handle_post(app, None, "/handbook/try", "", {}))
        self.assertEqual(["try"], seen)
        html = HandbookPageTests()._render({"id": 1, "role": "super_admin", "is_legacy_basic_auth": False})
        self.assertIn('data-handbook-tab="try"', html)
        self.assertIn("/static/handbook_try.js", html)


class KnowledgeSourcesConsoleTests(_ConsoleCase):
    def test_routes_and_forwarding(self):
        from routes import handbook
        seen = []
        app = types.SimpleNamespace(_handle_knowledge_sources=lambda h, action, rid: seen.append((action, rid)))
        self.assertTrue(handbook.handle_get(app, None, "/handbook/sources", "", {}))
        self.assertTrue(handbook.handle_post(app, None, "/handbook/sources/scan", "", {}))
        self.assertTrue(handbook.handle_post(app, None, "/handbook/sources/reminders/12/check", "", {}))
        self.assertFalse(handbook.handle_post(app, None, "/handbook/sources/reminders/x/check", "", {}))
        self.assertEqual([("view", None), ("scan", None), ("check", 12)], seen)
        app, calls = self._app(lambda *a: {"ok": True, "data": {}}, roles=("admin",))
        app._handle_knowledge_sources(_handler({}), "check", 12)
        self.assertEqual("/internal/v1/admin/knowledge-sources/reminders/12/check", calls[0]["endpoint"])
        self.assertEqual({}, calls[0]["payload"])

    def test_page_has_the_sources_tab(self):
        html = HandbookPageTests()._render({"id": 1, "role": "super_admin", "is_legacy_basic_auth": False})
        self.assertIn('data-handbook-tab="sources"', html)
        self.assertIn("/static/handbook_sources.js", html)
