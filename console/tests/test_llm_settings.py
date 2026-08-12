from __future__ import annotations

import io
import json
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.app import LocalDocFlowApp


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class _Handler:
    def __init__(self, body=None, *, user=None, origin="http://localhost:8765"):
        payload = json.dumps(body or {}).encode("utf-8")
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": "application/json",
            "Host": "localhost:8765",
        }
        if origin:
            self.headers["Origin"] = origin
        self.rfile = io.BytesIO(payload)
        self.wfile = io.BytesIO()
        self.current_admin_user = user


class LLMSettingsConsoleTests(unittest.TestCase):
    def setUp(self):
        self.app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        self.app.settings = SimpleNamespace(app_title="ShipNow")
        self.app.template_env = Environment(
            loader=FileSystemLoader(CONSOLE_DIR / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.sent_status = None
        self.sent_payload = None
        self.agent_calls = []

        def send_json(app, handler, status, payload):
            self.sent_status = status
            self.sent_payload = payload

        def parse_json_body(app, handler):
            return json.loads(handler.rfile.read().decode("utf-8") or "{}")

        def agent_request(app, method, path, *, payload=None, timeout=0):
            self.agent_calls.append((method, path, payload, timeout))
            if path.endswith("/config"):
                return {
                    "ok": True,
                    "data": {
                        "active": {"id": 4, "provider": "deepseek", "model_id": "deepseek-chat", "status": "active"},
                        "runtime": {"configured": True, "provider": "deepseek", "model": "deepseek-chat", "health": "ready"},
                        "providers": [{"provider": "deepseek", "configured": True, "key_hint": "syn…key", "base_url": "https://api.deepseek.com/v1"}],
                        "versions": [{"id": 4}],
                        "models": [{"provider": "deepseek", "model_id": "deepseek-chat"}],
                    },
                }
            return {"ok": True, "data": {"config_id": 9}}

        self.app._send_json = types.MethodType(send_json, self.app)
        self.app._parse_json_body = types.MethodType(parse_json_body, self.app)
        self.app._agent_request = types.MethodType(agent_request, self.app)

    @staticmethod
    def _user(role):
        return {
            "id": 7,
            "username": "admin",
            "role": role,
            "is_legacy_basic_auth": False,
        }

    def test_normal_admin_cannot_call_any_model_write_action(self):
        handler = _Handler(
            {"provider": "deepseek", "model_id": "deepseek-chat", "api_key": "synthetic"},
            user=self._user("admin"),
        )

        self.app._handle_llm_settings_post(handler, "save")

        self.assertEqual(HTTPStatus.FORBIDDEN, self.sent_status)
        self.assertEqual([], self.agent_calls)

    def test_super_admin_write_requires_same_origin(self):
        handler = _Handler(
            {"provider": "deepseek", "model_id": "deepseek-chat"},
            user=self._user("super_admin"),
            origin="https://untrusted.example",
        )

        self.app._handle_llm_settings_post(handler, "save")

        self.assertEqual(HTTPStatus.FORBIDDEN, self.sent_status)
        self.assertEqual("CSRF_ORIGIN_REJECTED", self.sent_payload["error_code"])
        self.assertEqual([], self.agent_calls)

    def test_super_admin_same_origin_write_is_forwarded_with_server_identity(self):
        handler = _Handler(
            {"provider": "deepseek", "model_id": "deepseek-chat"},
            user=self._user("super_admin"),
        )

        self.app._handle_llm_settings_post(handler, "save")

        self.assertEqual(HTTPStatus.OK, self.sent_status)
        self.assertEqual("admin", self.agent_calls[0][2]["actor"])

    def test_normal_admin_status_removes_key_hint_urls_versions_and_catalog(self):
        handler = _Handler(user=self._user("admin"))

        self.app._handle_llm_settings_get(handler, "status")

        data = self.sent_payload["data"]
        self.assertTrue(data["read_only"])
        self.assertEqual([{"provider": "deepseek", "configured": True}], data["providers"])
        self.assertNotIn("versions", data)
        self.assertNotIn("models", data)

    def test_template_never_prefills_key_and_supports_model_selection(self):
        template = (CONSOLE_DIR / "templates" / "llm_settings.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "llm_settings.js").read_text(encoding="utf-8")

        self.assertIn('type="password"', template)
        self.assertIn('autocomplete="new-password"', template)
        self.assertIn('list="llm-model-options"', template)
        self.assertNotIn('name="base_url"', template)
        self.assertIn("renderModelOptions", script)


if __name__ == "__main__":
    unittest.main()
