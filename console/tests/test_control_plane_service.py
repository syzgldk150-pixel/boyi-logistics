"""Security and contract tests for the Console control-plane boundary."""

from __future__ import annotations

import io
import json
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace


CONSOLE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CONSOLE_DIR.parent
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from console.services.auth import AuthServiceMixin  # noqa: E402
from console.services.control_plane import ControlPlaneServiceMixin  # noqa: E402


class _Handler:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        origin: str = "https://console.test",
        user: dict | None = None,
    ) -> None:
        body = json.dumps(payload or {}).encode("utf-8")
        self.headers = {
            "Host": "console.test",
            "Origin": origin,
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "X-Browser-Request-UUID": "f117c816-061c-4ec2-a803-9b3a14ce2a25",
        }
        self.rfile = io.BytesIO(body)
        self.current_admin_user = user or {
            "id": 17,
            "username": "ops-admin",
            "control_plane_role": "admin",
            "display_name": "运营管理员",
            "is_legacy_basic_auth": False,
        }


class _ControlPlaneApp(ControlPlaneServiceMixin, AuthServiceMixin):
    def __init__(self, agent_result: dict) -> None:
        self.settings = SimpleNamespace(agent_timeout_seconds=12)
        self.agent_result = agent_result
        self.agent_calls: list[tuple[str, str, dict | None, int | None]] = []
        self.sent_status = None
        self.sent_payload = None
        self.sent_headers = None

    def _agent_request(
        self,
        method,
        endpoint,
        *,
        payload=None,
        timeout=None,
        console_principal=None,
    ):
        if method == "GET" and console_principal is None:
            raise AssertionError("signed Console principal is required for control-plane reads")
        self.agent_calls.append((method, endpoint, payload, timeout))
        return self.agent_result

    def _send_json(self, _handler, status, payload):
        self.sent_status = status
        self.sent_payload = payload

    def _send_bytes(
        self,
        _handler,
        status,
        payload,
        _content_type,
        cache_control=None,
        extra_headers=None,
    ):
        self.sent_status = status
        self.sent_payload = json.loads(payload.decode("utf-8"))
        self.sent_headers = dict(extra_headers or {})
        self.cache_control = cache_control


class ControlPlaneServiceTests(unittest.TestCase):
    def test_command_rebuilds_trusted_context_and_returns_202_with_run_location(self):
        app = _ControlPlaneApp(
            {
                "ok": True,
                "status": 202,
                "data": {
                    "command_id": "cmd-1",
                    "work_item_id": "wi-1",
                    "run_id": "run-1",
                    "status": "RECEIVED",
                    "reused": False,
                    "next_poll_after_ms": 1000,
                },
            }
        )
        handler = _Handler(
            {
                "command_type": "compare_waybills",
                "parameters": {"date": "2026-08-13"},
                "entity_refs": [],
                "idempotency_key": "console-test-1",
                "actor": {"actor_id": "attacker"},
                "actor_roles": ["system"],
                "source": "untrusted",
            }
        )

        app._handle_control_plane_command_post(handler)

        self.assertEqual(HTTPStatus.ACCEPTED, app.sent_status)
        self.assertEqual("run-1", app.sent_payload["data"]["run_id"])
        self.assertEqual("/control-plane/runs/run-1", app.sent_headers["Location"])
        method, endpoint, payload, timeout = app.agent_calls[0]
        self.assertEqual(("POST", "/internal/v1/commands", 12), (method, endpoint, timeout))
        self.assertEqual("console", payload["source"])
        self.assertEqual(["admin"], payload["actor_roles"])
        self.assertEqual("17", payload["actor"]["actor_id"])
        self.assertEqual("mysql_admin_session", payload["actor"]["authenticated_by"])
        self.assertEqual(
            "console:17:compare_waybills:f117c816-061c-4ec2-a803-9b3a14ce2a25",
            payload["idempotency_key"],
        )
        self.assertNotEqual("attacker", payload["actor"]["actor_id"])

    def test_command_requires_agent_run_id(self):
        app = _ControlPlaneApp({"ok": True, "status": 202, "data": {"command_id": "cmd-1"}})
        handler = _Handler(
            {
                "command_type": "compare_waybills",
                "parameters": {},
                "entity_refs": [],
                "idempotency_key": "console-test-2",
            }
        )

        app._handle_control_plane_command_post(handler)

        self.assertEqual(HTTPStatus.BAD_GATEWAY, app.sent_status)
        self.assertEqual("INVALID_AGENT_RUN_CONTRACT", app.sent_payload["error"]["code"])

    def test_basic_auth_is_rejected_before_any_agent_write(self):
        app = _ControlPlaneApp({"ok": True, "status": 202, "data": {"run_id": "run-1"}})
        handler = _Handler(
            {"command_type": "x", "idempotency_key": "y"},
            user={
                "id": 0,
                "username": "emergency",
                "display_name": "emergency",
                "is_legacy_basic_auth": True,
            },
        )

        app._handle_control_plane_command_post(handler)

        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent_status)
        self.assertEqual("MYSQL_ADMIN_SESSION_REQUIRED", app.sent_payload["error"]["code"])
        self.assertEqual([], app.agent_calls)

    def test_cross_origin_write_is_rejected_before_body_or_agent_access(self):
        app = _ControlPlaneApp({"ok": True, "status": 202, "data": {"run_id": "run-1"}})
        handler = _Handler(
            {"command_type": "x", "idempotency_key": "y"},
            origin="https://attacker.test",
        )

        app._handle_control_plane_command_post(handler)

        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent_status)
        self.assertEqual("CSRF_ORIGIN_REJECTED", app.sent_payload["error"]["code"])
        self.assertEqual([], app.agent_calls)

    def test_approval_forwards_only_bound_plan_fields_and_trusted_context(self):
        app = _ControlPlaneApp(
            {"ok": True, "status": 200, "data": {"run": {"run_id": "run-1"}}}
        )
        handler = _Handler(
            {
                "approval_id": "forged-approval",
                "plan_hash": "sha256-plan",
                "comment": "已核验",
                "plan": {"steps": [{"tool": "untrusted"}]},
                "actor": {"actor_id": "attacker"},
                "source": "untrusted",
            }
        )

        app._handle_control_plane_approval_post(handler, "approval-1", "approve")

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        payload = app.agent_calls[0][2]
        self.assertEqual(
            {
                "approval_id",
                "plan_hash",
                "comment",
                "actor",
                "actor_roles",
                "source",
            },
            set(payload),
        )
        self.assertEqual("approval-1", payload["approval_id"])
        self.assertEqual("sha256-plan", payload["plan_hash"])
        self.assertNotIn("plan", payload)

    def test_run_actions_allowlist_their_distinct_business_field(self):
        cases = (
            ("cancel", {"comment": "停止", "reason": "ignored"}, "comment", "停止"),
            ("retry", {"reason": "数据已恢复", "comment": "ignored"}, "reason", "数据已恢复"),
            (
                "clarify",
                {"clarification": "使用账号 A", "reason": "ignored"},
                "clarification",
                "使用账号 A",
            ),
        )
        for action, body, field, value in cases:
            with self.subTest(action=action):
                app = _ControlPlaneApp(
                    {"ok": True, "status": 200, "data": {"run": {"run_id": "run-1"}}}
                )
                app._handle_control_plane_run_action_post(_Handler(body), "run-1", action)
                payload = app.agent_calls[0][2]
                self.assertEqual(value, payload[field])
                self.assertEqual(
                    {field, "actor", "actor_roles", "source"},
                    set(payload),
                )

    def test_structured_clarification_forwards_only_closed_business_fields(self):
        app = _ControlPlaneApp(
            {"ok": True, "status": 200, "data": {"run": {"run_id": "run-1"}}}
        )
        handler = _Handler(
            {
                "clarification": {
                    "note": "账号已核对",
                    "account_id": "ronghui-ops-1",
                    "argument_updates": {"direction": "my_published"},
                },
                "actor": {"actor_id": "attacker"},
            }
        )

        app._handle_control_plane_run_action_post(handler, "run-1", "clarify")

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual(
            {
                "note": "账号已核对",
                "account_id": "ronghui-ops-1",
                "argument_updates": {"direction": "my_published"},
            },
            app.agent_calls[0][2]["clarification"],
        )

    def test_structured_clarification_rejects_unknown_fields_before_agent_call(self):
        app = _ControlPlaneApp(
            {"ok": True, "status": 200, "data": {"run": {"run_id": "run-1"}}}
        )

        app._handle_control_plane_run_action_post(
            _Handler({"clarification": {"account_id": "account-1", "guess": True}}),
            "run-1",
            "clarify",
        )

        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent_status)
        self.assertEqual("CLARIFICATION_REQUIRED", app.sent_payload["error"]["code"])
        self.assertEqual([], app.agent_calls)

    def test_list_query_forwards_only_bounded_allowlisted_fields(self):
        app = _ControlPlaneApp(
            {"ok": True, "status": 200, "data": {"items": [], "page": 2}}
        )
        handler = _Handler()

        app._handle_control_plane_work_items_get(
            handler,
            {
                "q": ["运单 123"],
                "page": ["2"],
                "actor": ["attacker"],
                "internal_token": ["must-not-forward"],
            },
        )

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        endpoint = app.agent_calls[0][1]
        self.assertIn("q=%E8%BF%90%E5%8D%95+123", endpoint)
        self.assertIn("page=2", endpoint)
        self.assertNotIn("actor", endpoint)
        self.assertNotIn("internal_token", endpoint)


if __name__ == "__main__":
    unittest.main()
