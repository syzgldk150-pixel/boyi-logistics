"""Regression tests for Console automation's durable command boundary."""

from __future__ import annotations

import unittest
from http import HTTPStatus
from types import SimpleNamespace

from console.services.agent_api import AgentApiServiceMixin
from console.services.automation import (
    AutomationServiceMixin,
    normalize_scan_preview_projection,
    normalize_selection_preview_projection,
)


class _Repository:
    def __init__(self) -> None:
        self.runtime_updates: list[dict] = []

    def update_scheduled_task_runtime(self, **values):
        self.runtime_updates.append(values)


class _App(AutomationServiceMixin, AgentApiServiceMixin):
    def __init__(self, result: dict | list[dict]) -> None:
        self.settings = SimpleNamespace(agent_timeout_seconds=12)
        self.repository = _Repository()
        self.automation_virtual_task_state: dict[str, dict] = {}
        self.results = list(result) if isinstance(result, list) else [result]
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
        if not self.results:
            raise AssertionError("unexpected Agent request")
        return self.results.pop(0)

    def _send_json(self, _handler, status, payload):
        self.sent = (status, payload)


class AutomationControlPlaneCutoverTests(unittest.TestCase):
    @staticmethod
    def _selection_projection(run_id):
        return {
            "contract_version": 1,
            "automation_id": "self_pickup_problem_upload",
            "title": "自提到货问题件",
            "preview_run_id": run_id,
            "observed_at": "2026-08-27T07:00:00Z",
            "expires_at": "2026-08-27T07:15:00Z",
            "candidate_count": 1,
            "candidates": [
                {
                    "arrival_count": 2,
                    "bill_code": "R0001",
                    "delivery_method": "自提",
                    "destination_site": "邵阳大祥S站",
                    "goods_count": 2,
                    "row_number": 12,
                    "source_id": "source-one",
                    "source_name": "每日到货表",
                }
            ],
            "summary": {"duplicate_source_rows": 0},
            "can_confirm": True,
        }

    def test_scan_preview_projection_is_closed_and_bound_to_run(self):
        run_id = "11111111-1111-4111-8111-111111111111"
        projection = {
            "contract_version": 1,
            "preview_run_id": run_id,
            "target_date": "2026-08-24",
            "observed_at": "2026-08-24T03:58:00Z",
            "expires_at": "2026-08-24T04:13:00Z",
            "source_page_count": 1,
            "normalized_record_count": 3,
            "selection_count": 2,
            "batch_count": 1,
            "can_confirm": True,
        }

        self.assertEqual(
            projection,
            normalize_scan_preview_projection(
                projection,
                expected_run_id=run_id,
            ),
        )
        self.assertIsNone(
            normalize_scan_preview_projection(
                {**projection, "selection_sha256": "1" * 64},
                expected_run_id=run_id,
            )
        )
        self.assertIsNone(
            normalize_scan_preview_projection(
                projection,
                expected_run_id="22222222-2222-4222-8222-222222222222",
            )
        )

    def test_selection_preview_projection_is_closed_and_bound_to_project(self):
        run_id = "11111111-1111-4111-8111-111111111111"
        projection = self._selection_projection(run_id)

        self.assertEqual(
            projection,
            normalize_selection_preview_projection(
                projection,
                expected_automation_id="self_pickup_problem_upload",
                expected_run_id=run_id,
            ),
        )
        self.assertIsNone(
            normalize_selection_preview_projection(
                {**projection, "preview_fingerprint": "f" * 64},
                expected_automation_id="self_pickup_problem_upload",
                expected_run_id=run_id,
            )
        )
        self.assertIsNone(
            normalize_selection_preview_projection(
                projection,
                expected_automation_id="split_pending_problem_upload",
                expected_run_id=run_id,
            )
        )

    def test_selection_preview_start_submits_only_project_and_request_id(self):
        request_id = "22222222-2222-4222-8222-222222222222"
        app = _App(
            {
                "ok": True,
                "status": 202,
                "data": {
                    "command_id": "command-selection",
                    "run_id": "11111111-1111-4111-8111-111111111111",
                },
            }
        )
        app._control_plane_write_context = lambda _handler: {
            "_console_principal": {"actor_id": "17"}
        }
        app._parse_urlencoded_form = lambda _handler: {
            "task_id": "self_pickup_problem_upload"
        }

        app._handle_selection_preview_start(
            SimpleNamespace(headers={"X-Browser-Request-UUID": request_id})
        )

        self.assertEqual(
            (
                "POST",
                "/internal/v1/automation-projects/self_pickup_problem_upload/"
                "selection-previews",
                {"request_id": request_id},
            ),
            app.calls[0][:3],
        )
        self.assertEqual(HTTPStatus.ACCEPTED, app.sent[0])

    def test_selection_confirmation_forwards_only_selected_bills(self):
        preview_run_id = "11111111-1111-4111-8111-111111111111"
        request_id = "22222222-2222-4222-8222-222222222222"
        app = _App(
            {
                "ok": True,
                "status": 202,
                "data": {
                    "command_id": "command-formal",
                    "run_id": "33333333-3333-4333-8333-333333333333",
                },
            }
        )
        app._control_plane_write_context = lambda _handler: {
            "_console_principal": {"actor_id": "17"}
        }
        app._parse_urlencoded_form = lambda _handler: {
            "task_id": "self_pickup_problem_upload",
            "preview_run_id": preview_run_id,
            "selected_bill_codes_json": '["R0002"]',
        }

        app._handle_selection_preview_confirmation(
            SimpleNamespace(headers={"X-Browser-Request-UUID": request_id})
        )

        _method, endpoint, payload, _timeout, _principal = app.calls[0]
        self.assertEqual(
            "/internal/v1/automation-projects/self_pickup_problem_upload/"
            f"selection-previews/{preview_run_id}/confirm",
            endpoint,
        )
        self.assertEqual(
            {
                "request_id": request_id,
                "selected_bill_codes": ["R0002"],
            },
            payload,
        )
        self.assertNotIn("preview_fingerprint", payload)
        self.assertEqual(HTTPStatus.ACCEPTED, app.sent[0])

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

    def test_preview_run_id_cannot_be_forwarded_to_another_project(self):
        app = _App({"ok": True, "status": 202, "data": {}})

        result = app._start_automation_task_run(
            {
                "task_id": "daily_sign",
                "task_mode": "manual",
                "tool_name": "sync_daily_should_sign",
                "tool_params": {},
                "tool_params_json": "{}",
                "name": "每日应签",
            },
            trusted_context={"_console_principal": {"actor_id": "17"}},
            browser_request_uuid="22222222-2222-4222-8222-222222222222",
            preview_run_id="11111111-1111-4111-8111-111111111111",
        )

        self.assertFalse(result["ok"])
        self.assertEqual("SCAN_PREVIEW_ID_INVALID", result["error_code"])
        self.assertEqual([], app.calls)

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

    def test_scan_confirmation_forwards_only_new_request_and_preview_ids(self):
        preview_run_id = "11111111-1111-4111-8111-111111111111"
        request_id = "22222222-2222-4222-8222-222222222222"
        app = _App(
            {
                "ok": True,
                "status": 202,
                "data": {
                    "command_id": "cmd-formal",
                    "work_item_id": "wi-formal",
                    "run_id": "33333333-3333-4333-8333-333333333333",
                },
            }
        )
        app._control_plane_write_context = lambda _handler: {
            "_console_principal": {"actor_id": "17"}
        }
        app._parse_urlencoded_form = lambda _handler: {
            "task_id": "scan_codes",
            "preview_run_id": preview_run_id,
        }

        app._handle_scan_preview_confirmation(
            SimpleNamespace(headers={"X-Browser-Request-UUID": request_id})
        )

        method, endpoint, payload, _timeout, _principal = app.calls[0]
        self.assertEqual("POST", method)
        self.assertEqual(
            "/internal/v1/automation-projects/scan_codes/invoke",
            endpoint,
        )
        self.assertEqual(
            {"request_id": request_id, "preview_run_id": preview_run_id},
            payload,
        )
        self.assertEqual(HTTPStatus.ACCEPTED, app.sent[0])
        self.assertTrue(app.sent[1]["pending"])

    def test_scan_confirmation_preserves_formal_governance_block(self):
        app = _App(
            {
                "ok": False,
                "status": 422,
                "error_code": "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED",
                "error": "must remain closed",
            }
        )
        app._control_plane_write_context = lambda _handler: {
            "_console_principal": {"actor_id": "17"}
        }
        app._parse_urlencoded_form = lambda _handler: {
            "task_id": "scan_codes",
            "preview_run_id": "11111111-1111-4111-8111-111111111111",
        }

        app._handle_scan_preview_confirmation(
            SimpleNamespace(
                headers={
                    "X-Browser-Request-UUID": (
                        "22222222-2222-4222-8222-222222222222"
                    )
                }
            )
        )

        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, app.sent[0])
        self.assertEqual(
            "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED",
            app.sent[1]["error_code"],
        )
        self.assertIn("正式扫描尚未开放", app.sent[1]["message"])

    def test_scan_confirmation_rejects_caller_owned_action_fields(self):
        app = _App({"ok": True, "status": 202, "data": {}})
        app._control_plane_write_context = lambda _handler: {
            "_console_principal": {"actor_id": "17"}
        }
        app._parse_urlencoded_form = lambda _handler: {
            "task_id": "scan_codes",
            "preview_run_id": "11111111-1111-4111-8111-111111111111",
            "dry_run": "false",
        }

        app._handle_scan_preview_confirmation(
            SimpleNamespace(
                headers={
                    "X-Browser-Request-UUID": (
                        "22222222-2222-4222-8222-222222222222"
                    )
                }
            )
        )

        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent[0])
        self.assertEqual([], app.calls)

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

    def test_completed_scan_preview_returns_only_bounded_projection(self):
        run_id = "11111111-1111-4111-8111-111111111111"
        projection = {
            "contract_version": 1,
            "preview_run_id": run_id,
            "target_date": "2026-08-24",
            "observed_at": "2026-08-24T03:58:00Z",
            "expires_at": "2026-08-24T04:13:00Z",
            "source_page_count": 1,
            "normalized_record_count": 3,
            "selection_count": 2,
            "batch_count": 1,
            "can_confirm": True,
        }
        app = _App(
            [
                {
                    "ok": True,
                    "status": 200,
                    "data": {
                        "run": {
                            "run_id": run_id,
                            "status": "COMPLETED",
                            "finished_at": "2026-08-24 12:00:00",
                        },
                        "next_poll_after_ms": 0,
                    },
                },
                {"ok": True, "status": 200, "data": projection},
            ]
        )

        app._handle_automation_task_output(
            object(),
            {
                "run_id": [run_id],
                "task_id": ["scan_codes"],
                "scan_phase": ["preview"],
                "offset": ["0"],
            },
        )

        self.assertEqual(
            (
                "/internal/v1/automation-projects/scan_codes/scan-previews/"
                + run_id
            ),
            app.calls[1][1],
        )
        self.assertEqual(projection, app.sent[1]["scan_preview"])
        self.assertNotIn("selection_sha256", app.sent[1]["scan_preview"])

    def test_completed_formal_scan_does_not_request_preview_projection(self):
        run_id = "11111111-1111-4111-8111-111111111111"
        app = _App(
            {
                "ok": True,
                "status": 200,
                "data": {
                    "run": {
                        "run_id": run_id,
                        "status": "COMPLETED",
                        "finished_at": "2026-08-24 12:00:00",
                    },
                    "next_poll_after_ms": 0,
                },
            }
        )

        app._handle_automation_task_output(
            object(),
            {
                "run_id": [run_id],
                "task_id": ["scan_codes"],
                "scan_phase": ["formal"],
                "offset": ["0"],
            },
        )

        self.assertEqual(1, len(app.calls))
        self.assertNotIn("scan_preview", app.sent[1])

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

    def test_output_poll_projects_blocked_runs_as_attention_without_polling(self):
        for run_status, expected_title in (
            ("BLOCKED_DATA", "数据阻塞"),
            ("BLOCKED_LOGIN", "登录已失效"),
        ):
            with self.subTest(run_status=run_status):
                app = _App(
                    {
                        "ok": True,
                        "status": 200,
                        "data": {
                            "run": {
                                "run_id": "run-blocked-1",
                                "status": run_status,
                                "created_at": "2026-08-24 01:00:00",
                                "error_summary": "需要处理后继续",
                            },
                            "next_poll_after_ms": 3000,
                        },
                    }
                )

                app._handle_automation_task_output(
                    object(),
                    {
                        "run_id": ["run-blocked-1"],
                        "task_id": ["arrive_list"],
                        "offset": ["0"],
                    },
                )

                payload = app.sent[1]
                self.assertFalse(payload["running"])
                self.assertTrue(payload["pending"])
                self.assertTrue(payload["attention"])
                self.assertEqual(expected_title, payload["attention_title"])
                self.assertEqual("需要处理后继续", payload["attention_message"])
                self.assertEqual(0, payload["next_poll_after_ms"])


if __name__ == "__main__":
    unittest.main()
