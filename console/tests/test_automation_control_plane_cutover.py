"""Regression tests for Console automation's durable command boundary."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from console.services.agent_api import AgentApiServiceMixin
from console.services.automation import AutomationServiceMixin


class _Repository:
    def __init__(self) -> None:
        self.runtime_updates: list[dict] = []

    def update_scheduled_task_runtime(self, **values):
        self.runtime_updates.append(values)


class _App(AutomationServiceMixin, AgentApiServiceMixin):
    def __init__(self, result: dict) -> None:
        self.settings = SimpleNamespace(agent_timeout_seconds=12)
        self.repository = _Repository()
        self.automation_virtual_task_state: dict[str, dict] = {}
        self.result = result
        self.calls: list[tuple[str, str, dict | None, int | None, dict | None]] = []
        self.sent = None
        self._control_plane_read_context = lambda _handler: {
            "_console_principal": {
                "actor_type": "console_admin",
                "actor_id": "17",
                "roles": ["admin"],
                "authenticated_by": "mysql_admin_session",
            }
        }

    def _agent_request(
        self,
        method,
        endpoint,
        *,
        payload=None,
        timeout=None,
        console_principal=None,
    ):
        self.calls.append((method, endpoint, payload, timeout, console_principal))
        return self.result

    def _send_json(self, _handler, status, payload):
        self.sent = (status, payload)


class AutomationControlPlaneCutoverTests(unittest.TestCase):
    def test_manual_run_invokes_saved_project_and_keeps_run_reference(self):
        app = _App(
            {
                "ok": True,
                "status": 202,
                "data": {
                    "command_id": "cmd-1",
                    "work_item_id": "wi-1",
                    "run_id": "run-1",
                    "status": "RECEIVED",
                },
            }
        )
        console_principal = {
            "actor_type": "console_admin",
            "actor_id": "17",
            "roles": ["admin"],
            "authenticated_by": "mysql_admin_session",
        }
        request_uuid = "11111111-1111-4111-8111-111111111111"
        trusted = {
            "actor": {"actor_type": "console_admin", "actor_id": "17", "roles": ["admin"]},
            "actor_roles": ["admin"],
            "source": "console",
            "_console_principal": console_principal,
        }

        result = app._start_automation_task_run(
            {
                "task_id": "daily_sign",
                "task_mode": "manual",
                "tool_name": "sync_daily_should_sign",
                "tool_params": {"target_date": "2026-08-13"},
                "tool_params_json": '{"target_date":"2026-08-13"}',
                "name": "每日应签",
            },
            trusted_context=trusted,
            browser_request_uuid=request_uuid,
        )

        self.assertTrue(result["ok"])
        method, endpoint, payload, _timeout, signed_principal = app.calls[0]
        self.assertEqual(
            ("POST", "/internal/v1/automation-projects/daily_sign/invoke"),
            (method, endpoint),
        )
        self.assertEqual({"request_id": request_uuid}, payload)
        self.assertNotIn("tool_name", payload)
        self.assertNotIn("parameters", payload)
        self.assertEqual(console_principal, signed_principal)
        self.assertEqual("run-1", app.automation_virtual_task_state["daily_sign"]["run_id"])

    def test_cancel_targets_the_exact_run(self):
        app = _App({"ok": True, "status": 200, "data": {"run": {"run_id": "run-1"}}})
        app._parse_urlencoded_form = lambda _handler: {"task_id": "daily_sign", "run_id": "run-1"}
        console_principal = {
            "actor_type": "console_admin",
            "actor_id": "17",
            "roles": ["admin"],
            "authenticated_by": "mysql_admin_session",
        }
        app._control_plane_write_context = lambda _handler: {
            "actor": {"actor_type": "console_admin", "actor_id": "17", "roles": ["admin"]},
            "actor_roles": ["admin"],
            "source": "console",
            "_console_principal": console_principal,
        }

        app._handle_automation_task_cancel(object())

        self.assertEqual("/internal/v1/runs/run-1/cancel", app.calls[0][1])
        self.assertNotIn("_console_principal", app.calls[0][2])
        self.assertEqual(console_principal, app.calls[0][4])
        self.assertTrue(app.sent[1]["cancel_requested"])

    def test_output_poll_uses_run_state_not_tool_process_guessing(self):
        app = _App(
            {
                "ok": True,
                "status": 200,
                "data": {
                    "run": {
                        "run_id": "run-1",
                        "status": "COMPLETED",
                        "finished_at": "2026-08-13 12:00:00",
                    },
                    "next_poll_after_ms": 0,
                },
            }
        )

        app._handle_automation_task_output(
            object(),
            {"run_id": ["run-1"], "task_id": ["daily_sign"], "offset": ["0"]},
        )

        self.assertEqual("/internal/v1/runs/run-1", app.calls[0][1])
        self.assertFalse(app.sent[1]["running"])
        self.assertTrue(app.sent[1]["runtime"]["ok"])

    def test_output_poll_does_not_treat_waiting_approval_as_running(self):
        app = _App(
            {
                "ok": True,
                "status": 200,
                "data": {
                    "run": {
                        "run_id": "run-approval-1",
                        "status": "WAITING_APPROVAL",
                        "created_at": "2026-08-15 00:01:33",
                    },
                    "next_poll_after_ms": 3000,
                },
            }
        )

        app._handle_automation_task_output(
            object(),
            {
                "run_id": ["run-approval-1"],
                "task_id": ["daily_sign"],
                "offset": ["0"],
            },
        )

        payload = app.sent[1]
        self.assertFalse(payload["running"])
        self.assertTrue(payload["pending"])
        self.assertTrue(payload["awaiting_approval"])
        self.assertEqual("WAITING_APPROVAL", payload["status"])


if __name__ == "__main__":
    unittest.main()
