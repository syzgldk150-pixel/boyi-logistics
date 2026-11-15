"""Console side of the business handbook (agent layer P2): super-admin only, forwards to the Agent."""
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


class HandbookConsoleTests(unittest.TestCase):
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

    def test_only_super_admins_reach_the_agent(self):
        app, calls = self._app(lambda *a: self.fail("must not call the Agent"), roles=("admin",))
        app._handle_handbook_post(_handler({"title": "t", "body": "b"}), None, None)
        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent_status)
        self.assertEqual([], calls)

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
                                    _handle_handbook_get=lambda h, e: seen.append(("get", e)),
                                    _handle_handbook_post=lambda h, e, a: seen.append(("post", e, a)))
        self.assertTrue(handbook.handle_get(app, None, "/settings/llm/handbook", "", {}))
        self.assertTrue(handbook.handle_get(app, None, f"/settings/llm/handbook/entries/{ENTRY_ID}", "", {}))
        self.assertTrue(handbook.handle_post(app, None, f"/settings/llm/handbook/entries/{ENTRY_ID}/trial", "", {}))
        self.assertFalse(handbook.handle_get(app, None, "/settings/llm", "", {}))
        self.assertEqual(["page", ("get", ENTRY_ID), ("post", ENTRY_ID, "trial")], seen)


class ReviewsConsoleTests(HandbookConsoleTests):
    def test_review_actions_forward_only_the_note(self):
        app, calls = self._app(lambda *a: {"ok": True, "data": {"status": "EXECUTED"}})
        app._handle_review_action(_handler({"note": "x" * 300, "actor": "y"}), ENTRY_ID, "reject")
        self.assertEqual(f"/internal/v1/admin/reviews/{ENTRY_ID}/reject", calls[0]["endpoint"])
        self.assertEqual({"note": "x" * 200}, calls[0]["payload"])
        app._handle_review_action(_handler({}), ENTRY_ID, "approve")
        self.assertEqual({}, calls[1]["payload"])
        app._handle_review_action(_handler({}), "bad", "approve")
        self.assertEqual(HTTPStatus.NOT_FOUND, app.sent_status)

    def test_admins_cannot_decide(self):
        app, calls = self._app(lambda *a: self.fail("must not call"), roles=("admin",))
        app._handle_review_action(_handler({}), ENTRY_ID, "approve")
        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent_status)
