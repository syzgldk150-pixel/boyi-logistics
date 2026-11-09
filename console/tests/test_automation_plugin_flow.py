"""Console side of plugin flows (AI phase 5): super-admin only, forwards to the Agent, saves through plugin settings."""
import json
import types
import unittest
from http import HTTPStatus

import test_customer_service_module as base_module
from test_customer_service_module import BROWSER_REQUEST_UUID, CONSOLE_DIR, _Handler

AUTOMATION_ID = "11111111-2222-4333-8444-555555555555"
FLOW = {"steps": [{"block": "candidates"}, {"block": "draft"}, {"block": "reply"}]}


def _handler(body):
    return _Handler(json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    {"Content-Type": "application/json", "X-Browser-Request-UUID": BROWSER_REQUEST_UUID})


class AutomationPluginFlowConsoleTests(unittest.TestCase):
    def _app(self, responses, roles=("super_admin",)):
        app = base_module.CustomerServiceModuleTests()._build_app()
        calls = []
        context = {"actor_roles": list(roles), "_console_principal": {"actor_id": "9"}}

        def agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
            calls.append({"method": method, "endpoint": endpoint, "payload": payload})
            return responses(method, endpoint)

        app._agent_request = types.MethodType(agent_request, app)
        app._control_plane_read_context = lambda handler: context
        app._control_plane_write_context = lambda handler: context
        app._clear_automation_plugin_catalog_cache = lambda: None
        return app, calls

    def test_only_super_admins_reach_the_agent(self):
        app, calls = self._app(lambda *a: self.fail("must not call the Agent"), roles=("admin",))
        app._handle_automation_plugin_flow_action(_handler({"instruction": "只处理韵达"}), AUTOMATION_ID, "ai-edit")
        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent_status)
        self.assertEqual([], calls)

    def test_ai_edit_forwards_only_the_sentence(self):
        app, calls = self._app(lambda *a: {"ok": True, "data": {"status": "ok"}})
        app._handle_automation_plugin_flow_action(_handler({"instruction": " 只处理韵达 ", "flow": FLOW}), AUTOMATION_ID, "ai-edit")
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual(f"/internal/v1/automation/instances/{AUTOMATION_ID}/flow/ai-edit", calls[0]["endpoint"])
        self.assertEqual({"request_id": BROWSER_REQUEST_UUID, "instruction": "只处理韵达"}, calls[0]["payload"])

    def test_save_keeps_other_settings_and_uses_config_cas(self):
        context = {"project_configuration_version": 4, "config": {"max_per_run": 3},
                   "account_bindings": {"customer_service_source": ["a"]}, "resource_bindings": {}}

        def responses(method, endpoint):
            return {"ok": True, "data": context} if method == "GET" else {"ok": True, "data": {"version": 5}}
        app, calls = self._app(responses)
        app._handle_automation_plugin_flow_action(
            _handler({"flow": FLOW, "expected_project_configuration_version": 4}), AUTOMATION_ID, "save")
        self.assertEqual(HTTPStatus.OK, app.sent_status)
        saved = calls[1]
        self.assertEqual(("PUT", f"/internal/v1/automation/instances/{AUTOMATION_ID}/plugin-settings"),
                         (saved["method"], saved["endpoint"]))
        self.assertEqual({"max_per_run": 3, "flow": FLOW}, saved["payload"]["config"])
        self.assertEqual({"customer_service_source": ["a"]}, saved["payload"]["account_bindings"])
        self.assertEqual(4, saved["payload"]["expected_project_configuration_version"])

    def test_save_refuses_a_stale_version(self):
        app, calls = self._app(lambda *a: {"ok": True, "data": {"project_configuration_version": 5, "config": {}}})
        app._handle_automation_plugin_flow_action(
            _handler({"flow": FLOW, "expected_project_configuration_version": 4}), AUTOMATION_ID, "save")
        self.assertEqual(HTTPStatus.CONFLICT, app.sent_status)
        self.assertEqual(1, len(calls))

    def test_agent_rule_failures_are_shown(self):
        app, _ = self._app(lambda *a: {"ok": False, "status": 422, "error": {"code": "FLOW_INVALID", "message": "不能删除"}})
        app._handle_automation_plugin_flow_action(_handler({"flow": FLOW}), AUTOMATION_ID, "dry-run")
        self.assertEqual(422, app.sent_status)
        self.assertEqual("FLOW_INVALID", app.sent_payload["error_code"])

    def test_page_renders_for_super_admins(self):
        app, _ = self._app(lambda *a: self.fail("page render must not call the Agent"))
        app._render_automation_plugin_flow(_handler({}), AUTOMATION_ID)
        self.assertEqual(HTTPStatus.OK, app.sent_status)

    def test_page_wiring(self):
        template = (CONSOLE_DIR / "templates" / "automation_plugin_flow.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "plugin_flow.js").read_text(encoding="utf-8")
        self.assertIn("data-plugin-flow-instruction", template)
        self.assertIn("/static/plugin_flow.js", template)
        for route in ("/data", "/ai-edit", "/dry-run", "/save"):
            self.assertIn(route, script)
        self.assertIn("plugin.flow_editable", (CONSOLE_DIR / "templates" / "automation.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
