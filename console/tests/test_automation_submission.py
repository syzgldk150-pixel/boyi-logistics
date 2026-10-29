import unittest
import json
from http import HTTPStatus
from types import SimpleNamespace

from console.app import LocalDocFlowApp
from console.services.automation import _automation_blocking_feedback


class AutomationSubmissionTests(unittest.TestCase):
    def _make_app(self, form_values: dict) -> LocalDocFlowApp:
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app._parse_urlencoded_form = lambda handler: form_values
        return app

    def test_automation_feedback_never_promises_old_retry_or_history_block(self):
        expected = {
            "ACTIVE": "本次未启动",
            "RETRY_PENDING": "本次未启动",
            "UNKNOWN_WRITE": "本次未启动",
            "NEEDS_ATTENTION": "本次未启动",
        }
        for blocking_kind, title in expected.items():
            with self.subTest(blocking_kind=blocking_kind):
                actual_title, message = _automation_blocking_feedback(
                    {"blocking_kind": blocking_kind}
                )
                self.assertEqual(title, actual_title)
                self.assertTrue(message)

    def test_account_name_action_proxies_note_without_status_refresh(self):
        app = self._make_app({"name": "  融辉自提专用账号  "})
        captured = {}

        def proxy(handler, method, endpoint, **kwargs):
            captured.update({"method": method, "endpoint": endpoint, **kwargs})
            return True

        app._proxy_automation_account_action = proxy

        handled = app._handle_automation_account_post(
            None,
            "/automation-accounts/ronghui_default/name",
        )

        self.assertTrue(handled)
        self.assertEqual("POST", captured["method"])
        self.assertEqual(
            "/internal/v1/admin/accounts/ronghui_default/name",
            captured["endpoint"],
        )
        self.assertEqual({"name": "融辉自提专用账号"}, captured["payload"])
        self.assertFalse(captured["refresh_status"])

    def test_plugin_submission_discards_browser_tool_params_accounts_and_tool_name(self):
        app = self._make_app(
            {
                "task_id": "finance_action_east",
                "task_mode": "manual",
                "name": "华东财务同步",
                "tool_name": "browser.supplied.tool",
                "cron_expression": "* * * * *",
                "schedule_times_json": '["00:01"]',
                "tool_params_json": '{"token":"browser-secret","account_id":"legacy"}',
                "account_role__account_id": "legacy",
                "plugin_account_role__finance_quote_source": "browser-account",
                "project_plugin_instance": "true",
                "contribution_id": "manual.sync",
                "enabled": "on",
            }
        )

        payload, _override, error_message = app._collect_automation_task_submission(None)

        self.assertEqual("", error_message)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertTrue(payload["project_plugin_instance"])
        self.assertEqual("automation.finance_action_east.run", payload["tool_name"])
        self.assertEqual({}, payload["tool_params"])
        self.assertEqual("{}", payload["tool_params_json"])
        for browser_field in ("schedule_times", "cron_expressions", "account_bindings", "enabled"):
            self.assertNotIn(browser_field, payload)
        self.assertEqual("manual.sync", payload["contribution_id"])

    def test_submission_rejects_invalid_or_non_plugin_contribution_id(self):
        cases = (
            ({"project_plugin_instance": "true", "contribution_id": "Invalid ID"}, "无效"),
            ({"project_plugin_instance": "", "contribution_id": "manual.sync"}, "自动化项目标识无效"),
            (
                {
                    "project_plugin_instance": "true",
                    "contribution_id": ["manual.sync", "manual.retry"],
                },
                "一次只能选择一个",
            ),
        )
        for extra_values, expected_error in cases:
            with self.subTest(extra_values=extra_values):
                app = self._make_app(
                    {
                        "task_id": "finance_action_east",
                        "task_mode": "manual",
                        "name": "华东财务同步",
                        "tool_name": "browser.supplied.tool",
                        "tool_params_json": "{}",
                        **extra_values,
                    }
                )

                payload, _override, error_message = (
                    app._collect_automation_task_submission(None)
                )

                self.assertIsNone(payload)
                self.assertIn(expected_error, error_message)

    def test_service_v2_manual_invoke_forwards_only_contribution_identity(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(agent_timeout_seconds=10)
        app.automation_virtual_task_state = {}
        app._record_virtual_task_runtime = lambda *_args, **_kwargs: None
        captured = {}

        def agent_request(method, endpoint, **kwargs):
            captured.update({"method": method, "endpoint": endpoint, **kwargs})
            return {"ok": True, "data": {"invocation_id": "invocation-1", "status": "STARTING"}}

        app._agent_request = agent_request
        result = app._start_automation_task_run(
            {
                "task_id": "finance_action_east",
                "task_mode": "manual",
                "project_plugin_instance": True,
                "contribution_id": "manual.sync",
                "tool_name": "browser.supplied.tool",
                "tool_params": {"token": "must-not-forward"},
                "account_id": "must-not-forward",
                "service": "must-not-forward",
                "operation": "must-not-forward",
            },
            trusted_context={"_console_principal": {"actor_id": "17"}},
            browser_request_uuid="12345678-1234-4234-8234-123456789abc",
        )

        self.assertTrue(result["ok"])
        self.assertEqual("POST", captured["method"])
        self.assertEqual(
            "/internal/v1/automation-projects/finance_action_east/invoke",
            captured["endpoint"],
        )
        self.assertEqual(
            {
                "request_id": "12345678-1234-4234-8234-123456789abc",
                "contribution_id": "manual.sync",
            },
            captured["payload"],
        )

    def test_fetch_accounts_marks_authenticated_session_without_credentials(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app._agent_request = lambda *args, **kwargs: {
            "ok": True,
            "data": {
                "ok": True,
                "accounts": [
                    {
                        "account_id": "price_default",
                        "name": "大祥报价账号",
                        "system": "ronghui",
                        "system_label": "TMS融辉",
                        "account_purpose": "price",
                        "account_purpose_label": "大祥报价",
                        "is_active": True,
                        "is_default": True,
                        "session_capable": True,
                        "login_kind": "image",
                        "status": {
                            "status": "authenticated",
                            "label": "已登录",
                            "status_tone": "success",
                            "authenticated": True,
                            "has_saved_credentials": False,
                            "has_manual_credentials": False,
                            "has_env_credentials": False,
                            "credential_source": "",
                        },
                        "credentials": {
                            "username": "",
                            "phone": "",
                            "has_saved_credentials": False,
                            "has_manual_credentials": False,
                            "has_env_credentials": False,
                            "credential_source": "",
                        },
                    }
                ],
            },
        }

        accounts, warning = app._fetch_automation_accounts(force=False)

        self.assertEqual("", warning)
        self.assertEqual("登录态有效", accounts[0]["status_label"])
        self.assertEqual("warning", accounts[0]["status_tone"])
        self.assertEqual("未保存账号密码", accounts[0]["credentials_label"])
        self.assertEqual("warning", accounts[0]["credentials_tone"])
        self.assertIn("只检测到浏览器登录态", accounts[0]["status_note"])

    def test_fetch_accounts_marks_auto_login_disabled_and_failure_circuit(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app._agent_request = lambda *args, **kwargs: {
            "ok": True,
            "data": {
                "ok": True,
                "accounts": [
                    {
                        "account_id": "ronghui_manual",
                        "name": "手动登录账号",
                        "system": "ronghui",
                        "is_active": True,
                        "session_capable": True,
                        "auto_login_enabled": False,
                        "status": {
                            "status": "logged_out",
                            "label": "自动登录失败",
                            "last_error_summary": "旧错误",
                        },
                        "credentials": {},
                    },
                    {
                        "account_id": "yunda_blocked",
                        "name": "韵达熔断账号",
                        "system": "yunda",
                        "is_active": True,
                        "session_capable": True,
                        "auto_login_enabled": True,
                        "auto_login_blocked": True,
                        "auto_login_failure_limit": 3,
                        "status": {
                            "status": "error",
                            "label": "自动登录失败",
                            "last_error_summary": "账号或密码错误",
                        },
                        "credentials": {},
                    },
                ],
            },
        }

        accounts, warning = app._fetch_automation_accounts(force=False)

        self.assertEqual("", warning)
        self.assertEqual("已退出", accounts[0]["status_label"])
        self.assertEqual("", accounts[0]["status"]["last_error_summary"])
        self.assertIn("不做定时登录校验", accounts[0]["status_note"])
        self.assertEqual("自动登录已暂停", accounts[1]["status_label"])
        self.assertIn("连续失败达到 3 次", accounts[1]["status_note"])

    def test_run_now_preserves_safe_agent_admission_status_and_error_code(self):
        for upstream_status in (
            HTTPStatus.CONFLICT,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            HTTPStatus.SERVICE_UNAVAILABLE,
            HTTPStatus.INTERNAL_SERVER_ERROR,
        ):
            with self.subTest(upstream_status=upstream_status):
                app = LocalDocFlowApp.__new__(LocalDocFlowApp)
                app._is_ajax_request = lambda _handler: True
                app._control_plane_write_context = lambda _handler: {
                    "_console_principal": {"actor_id": "17"}
                }
                app._collect_automation_task_submission = (
                    lambda _handler: (
                        {
                            "task_id": "arrival_stats",
                            "task_mode": "manual",
                            "project_plugin_instance": True,
                        },
                        {},
                        "",
                    )
                )
                app._start_automation_task_run = (
                    lambda _payload, trusted_context, browser_request_uuid: {
                        "ok": False,
                        "status": upstream_status,
                        "error": "project runtime unavailable",
                        "error_code": "PROJECT_RUNTIME_UNAVAILABLE",
                    }
                )
                captured = {}
                app._send_json = lambda _handler, status, payload: captured.update(
                    status=status,
                    payload=payload,
                )

                app._handle_automation_task_run_now(
                    SimpleNamespace(headers={"X-Browser-Request-UUID": "request-1"})
                )

                expected_status = (
                    upstream_status
                    if upstream_status
                    in {
                        HTTPStatus.CONFLICT,
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    }
                    else HTTPStatus.BAD_GATEWAY
                )
                self.assertEqual(expected_status, captured["status"])
                self.assertEqual(
                    "PROJECT_RUNTIME_UNAVAILABLE",
                    captured["payload"]["error_code"],
                )

    def _run_now_existing_project_response(self, details):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app._is_ajax_request = lambda _handler: True
        app._control_plane_write_context = lambda _handler: {
            "_console_principal": {"actor_id": "17"}
        }
        app._collect_automation_task_submission = (
            lambda _handler: (
                {
                    "task_id": "daily_sign",
                    "task_mode": "scheduled",
                    "project_plugin_instance": True,
                },
                {},
                "",
            )
        )
        app._start_automation_task_run = (
            lambda _payload, trusted_context, browser_request_uuid: {
                "ok": False,
                "status": HTTPStatus.CONFLICT,
                "error": "该脚本已在运行",
                "error_code": "AUTOMATION_ALREADY_RUNNING",
                "data": details,
            }
        )
        captured = {}
        app._send_json = lambda _handler, status, payload: captured.update(
            status=status,
            payload=payload,
        )

        app._handle_automation_task_run_now(
            SimpleNamespace(headers={"X-Browser-Request-UUID": "request-2"})
        )
        return captured

    def test_run_now_reports_existing_project_run_without_resubmitting(self):
        captured = self._run_now_existing_project_response({
            "blocking_kind": "ACTIVE", "active_invocation_id": "invocation-existing", "active_status": "RUNNING",
            "blocking_count": 2, "unrelated": {"private": "not-public"},
        })

        self.assertEqual(HTTPStatus.CONFLICT, captured["status"])
        self.assertEqual("本次未启动", captured["payload"]["title"])
        self.assertEqual(
            "当前所需资源仍在使用，本次已结束，请稍后重新触发。",
            captured["payload"]["message"],
        )
        self.assertEqual(
            "AUTOMATION_ALREADY_RUNNING",
            captured["payload"]["error_code"],
        )
        self.assertFalse(captured["payload"]["ok"])
        self.assertFalse(captured["payload"]["pending"])
        self.assertNotIn("run_id", captured["payload"])
        self.assertEqual({"invocation_id": "invocation-existing", "status": "RUNNING", "blocking_kind": "ACTIVE"},
            captured["payload"]["existing_run"])
        self.assertNotIn("unrelated", captured["payload"])

    def test_existing_run_receipt_is_omitted_when_identity_or_state_is_invalid(self):
        for details in (
            {"blocking_kind": "ACTIVE"},
            {"blocking_kind": "ACTIVE", "active_invocation_id": "../another", "active_status": "RUNNING"},
            {"blocking_kind": "ACTIVE", "active_invocation_id": {"invocation_id": "nested"}, "active_status": "RUNNING"},
            {"blocking_kind": "ACTIVE", "active_invocation_id": "invocation-existing", "active_status": "not-a-call-state"},
            {"blocking_kind": {}, "active_invocation_id": "invocation-existing", "active_status": "RUNNING"},
            {"blocking_kind": "ACTIVE", "active_run_id": "historical-run", "active_status": "RUNNING"},
        ):
            with self.subTest(details=details):
                captured = self._run_now_existing_project_response(details)
                self.assertEqual(HTTPStatus.CONFLICT, captured["status"])
                self.assertNotIn("existing_run", captured["payload"])

    def test_task_output_without_current_identity_does_not_reuse_historical_success(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.repository = SimpleNamespace(
            list_scheduled_task_group=lambda task_id: [
                {
                    "id": "daily_sign_0500",
                    "last_run": "2026-06-20 16:47:43",
                    "last_status": "success",
                    "last_duration_ms": 1263400,
                    "last_message": "",
                }
            ]
        )
        app.automation_virtual_task_state = {}
        console_principal = {
            "actor_id": "17",
            "roles": ["admin"],
            "authenticated_by": "mysql_admin_session",
        }
        app._control_plane_read_context = lambda handler: {
            "_console_principal": console_principal,
        }
        app._agent_request = lambda *args, **kwargs: {"ok": False, "error": "timed out"}
        app._sync_task_runtime_from_latest_tool_log = (
            lambda task_id, tool_name, since=None, console_principal=None: (_ for _ in ()).throw(
                AssertionError("should not call agent logs")
            )
        )
        captured = {}
        app._send_json = lambda handler, status, payload: captured.update({"status": status, "payload": payload})

        app._handle_automation_task_output(
            None,
            {
                "tool_name": ["sync_daily_should_sign"],
                "task_id": ["daily_sign"],
                "offset": ["0"],
                "started_at": ["2026-06-20 16:26:40"],
            },
        )

        self.assertEqual(False, captured["payload"]["running"])
        self.assertEqual(HTTPStatus.BAD_REQUEST, captured["status"])
        self.assertNotIn("runtime", captured["payload"])
        self.assertIn("缺少本次执行标识", captured["payload"]["error"])


if __name__ == "__main__":
    unittest.main()
