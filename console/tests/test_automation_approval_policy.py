import io
import json
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.app import LocalDocFlowApp
from console.services.automation import (
    build_scheduled_approval_policy_view,
    normalize_scheduled_approval_policy_items,
)


class AutomationApprovalPolicyProjectionTests(unittest.TestCase):
    def test_normalizer_keeps_only_closed_safe_fields(self):
        items = normalize_scheduled_approval_policy_items(
            [
                {
                    "task_id": "clockin_daxiang_1830",
                    "mode": "EXACT_SCHEDULE_EXEMPT",
                    "configured_mode": "EXACT_SCHEDULE_EXEMPT",
                    "effective_mode": "EXACT_SCHEDULE_EXEMPT",
                    "effective_status": "ACTIVE",
                    "can_exempt": True,
                    "version": 7,
                    "configuration_version": 11,
                    "policy_hash_short": "ab12cd34",
                    "approved_by": "管理员 <script>",
                    "approved_at": "2026-08-14 12:00:00",
                    "invalid_reason": "",
                    "policy_hash": "must-not-leak",
                    "arguments": {"token": "must-not-leak"},
                }
            ]
        )

        self.assertEqual(1, len(items))
        self.assertEqual(
            {
                "task_id",
                "mode",
                "configured_mode",
                "effective_mode",
                "effective_status",
                "can_exempt",
                "version",
                "configuration_version",
                "policy_hash_short",
                "approved_by",
                "approved_at",
                "invalid_reason",
            },
            set(items[0]),
        )
        self.assertNotIn("policy_hash", items[0])
        self.assertNotIn("arguments", items[0])

    def test_group_projection_marks_mixed_and_stale_explicitly(self):
        mixed = build_scheduled_approval_policy_view(
            ["daily_sign_0500", "daily_sign_0700"],
            {
                "daily_sign_0500": {
                    "task_id": "daily_sign_0500",
                    "mode": "REQUIRE_EACH_RUN",
                    "configured_mode": "REQUIRE_EACH_RUN",
                    "effective_mode": "REQUIRE_EACH_RUN",
                    "effective_status": "ACTIVE",
                    "can_exempt": True,
                    "version": 2,
                    "configuration_version": 5,
                },
                "daily_sign_0700": {
                    "task_id": "daily_sign_0700",
                    "mode": "EXACT_SCHEDULE_EXEMPT",
                    "configured_mode": "EXACT_SCHEDULE_EXEMPT",
                    "effective_mode": "EXACT_SCHEDULE_EXEMPT",
                    "effective_status": "ACTIVE",
                    "can_exempt": True,
                    "version": 3,
                    "configuration_version": 6,
                },
            },
        )
        stale = build_scheduled_approval_policy_view(
            ["clockin_daxiang_1830"],
            {
                "clockin_daxiang_1830": {
                    "task_id": "clockin_daxiang_1830",
                    "mode": "EXACT_SCHEDULE_EXEMPT",
                    "configured_mode": "EXACT_SCHEDULE_EXEMPT",
                    "effective_mode": "REQUIRE_EACH_RUN",
                    "effective_status": "STALE",
                    "can_exempt": True,
                    "version": 8,
                    "configuration_version": 9,
                    "invalid_reason": "工具版本已变化",
                }
            },
        )

        self.assertTrue(mixed["mixed"])
        self.assertEqual("混合策略", mixed["label"])
        self.assertIn("2 条任务", mixed["summary"])
        self.assertEqual("配置已变更需重新授权", stale["label"])
        self.assertEqual("工具版本已变化", stale["invalid_reason"])
        self.assertEqual("EXACT_SCHEDULE_EXEMPT", stale["configured_mode"])
        self.assertEqual("REQUIRE_EACH_RUN", stale["effective_mode"])
        self.assertEqual(
            {"clockin_daxiang_1830": 9},
            stale["expected_configuration_versions"],
        )

    def test_group_projection_preserves_each_real_schedule_row(self):
        view = build_scheduled_approval_policy_view(
            ["delivery_status_0900", "delivery_status_0930"],
            {
                "delivery_status_0900": {
                    "task_id": "delivery_status_0900",
                    "mode": "REQUIRE_EACH_RUN",
                    "configured_mode": "REQUIRE_EACH_RUN",
                    "effective_mode": "REQUIRE_EACH_RUN",
                    "effective_status": "ACTIVE",
                    "can_exempt": True,
                    "version": 2,
                    "configuration_version": 7,
                },
                "delivery_status_0930": {
                    "task_id": "delivery_status_0930",
                    "mode": "EXACT_SCHEDULE_EXEMPT",
                    "configured_mode": "EXACT_SCHEDULE_EXEMPT",
                    "effective_mode": "REQUIRE_EACH_RUN",
                    "effective_status": "STALE",
                    "can_exempt": True,
                    "version": 4,
                    "configuration_version": 9,
                    "invalid_reason": "任务参数已变化",
                },
            },
            cron_expressions_by_task_id={
                "delivery_status_0900": "0 9 * * *",
                "delivery_status_0930": "30 9 * * *",
            },
        )

        self.assertEqual(
            ["delivery_status_0900", "delivery_status_0930"],
            [item["task_id"] for item in view["items"]],
        )
        self.assertEqual(
            ["每天 09:00", "每天 09:30"],
            [item["schedule_label"] for item in view["items"]],
        )
        self.assertEqual(2, view["items"][0]["version"])
        self.assertEqual(9, view["items"][1]["configuration_version"])
        self.assertEqual("任务参数已变化", view["items"][1]["invalid_reason"])
        self.assertEqual("混合策略", view["label"])

    def test_normalizer_rejects_open_status_and_inconsistent_modes(self):
        base = {
            "task_id": "daily_sign_0500",
            "mode": "REQUIRE_EACH_RUN",
            "configured_mode": "REQUIRE_EACH_RUN",
            "effective_mode": "REQUIRE_EACH_RUN",
            "effective_status": "ACTIVE",
            "can_exempt": True,
            "version": 1,
            "configuration_version": 1,
        }
        unknown_status = {**base, "effective_status": "UNKNOWN"}
        inconsistent_mode = {
            **base,
            "configured_mode": "EXACT_SCHEDULE_EXEMPT",
        }

        self.assertEqual(
            [],
            normalize_scheduled_approval_policy_items(
                [unknown_status, inconsistent_mode]
            ),
        )


class AutomationApprovalPolicyHandlerTests(unittest.TestCase):
    @staticmethod
    def _handler(payload, *, role="super_admin"):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return SimpleNamespace(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(raw)),
                "Host": "console.example",
                "Origin": "https://console.example",
            },
            rfile=io.BytesIO(raw),
            current_admin_user={
                "id": 17,
                "username": "operator",
                "display_name": "Operator",
                "role": role,
                "control_plane_role": role,
                "is_legacy_basic_auth": False,
            },
        )

    @staticmethod
    def _payload():
        return {
            "task_ids": ["clockin_daxiang_1830"],
            "mode": "EXACT_SCHEDULE_EXEMPT",
            "comment": "固定晚间打卡计划",
            "request_id": "12345678-1234-4234-8234-123456789abc",
            "expected_versions": {"clockin_daxiang_1830": 4},
            "expected_configuration_versions": {"clockin_daxiang_1830": 9},
            "actor": {"actor_id": "forged"},
            "roles": ["super_admin"],
        }

    def _app(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        captured = {}
        app._send_json = lambda handler, status, payload: captured.update(
            status=status, payload=payload
        )
        app._control_plane_error = lambda handler, status, code, message: captured.update(
            status=status,
            payload={"ok": False, "error_code": code, "message": message},
        )
        return app, captured

    def test_super_admin_forwards_closed_payload_with_signed_principal(self):
        app, captured = self._app()
        handler = self._handler(self._payload())
        forwarded = {}

        def agent_request(method, endpoint, **kwargs):
            forwarded.update(method=method, endpoint=endpoint, **kwargs)
            return {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "task_id": "clockin_daxiang_1830",
                            "mode": "EXACT_SCHEDULE_EXEMPT",
                            "configured_mode": "EXACT_SCHEDULE_EXEMPT",
                            "effective_mode": "EXACT_SCHEDULE_EXEMPT",
                            "effective_status": "ACTIVE",
                            "can_exempt": True,
                            "version": 5,
                            "configuration_version": 9,
                            "policy_hash_short": "abc12345",
                            "approved_by": "Operator",
                            "approved_at": "2026-08-14 12:00:00",
                            "invalid_reason": "",
                        }
                    ]
                },
            }

        app._agent_request = agent_request
        app._handle_automation_task_approval_policy(handler)

        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual("POST", forwarded["method"])
        self.assertEqual(
            "/internal/v1/scheduled-task-approval-policies",
            forwarded["endpoint"],
        )
        self.assertEqual(
            (set(self._payload()) - {"actor", "roles"}) | {"source"},
            set(forwarded["payload"]),
        )
        self.assertEqual("console", forwarded["payload"]["source"])
        self.assertEqual("17", forwarded["console_principal"]["actor_id"])
        self.assertNotIn("actor", forwarded["payload"])

    def test_admin_and_basic_auth_are_rejected_before_agent_call(self):
        for handler in (
            self._handler(self._payload(), role="admin"),
            SimpleNamespace(
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                    "Host": "console.example",
                    "Origin": "https://console.example",
                },
                rfile=io.BytesIO(b"{}"),
                current_admin_user={
                    "id": 0,
                    "username": "legacy",
                    "role": "legacy_admin",
                    "control_plane_role": "legacy_admin",
                    "is_legacy_basic_auth": True,
                },
            ),
        ):
            with self.subTest(role=handler.current_admin_user["role"]):
                app, captured = self._app()
                app._agent_request = lambda *args, **kwargs: self.fail("must not call Agent")
                app._handle_automation_task_approval_policy(handler)
                self.assertEqual(HTTPStatus.FORBIDDEN, captured["status"])

    def test_cross_origin_and_invalid_versions_are_rejected(self):
        cross_origin = self._handler(self._payload())
        cross_origin.headers["Origin"] = "https://attacker.example"
        app, captured = self._app()
        app._agent_request = lambda *args, **kwargs: self.fail("must not call Agent")
        app._handle_automation_task_approval_policy(cross_origin)
        self.assertEqual(HTTPStatus.FORBIDDEN, captured["status"])
        self.assertEqual("CSRF_ORIGIN_REJECTED", captured["payload"]["error_code"])

        invalid = self._payload()
        invalid["expected_versions"] = {}
        app, captured = self._app()
        app._agent_request = lambda *args, **kwargs: self.fail("must not call Agent")
        app._handle_automation_task_approval_policy(self._handler(invalid))
        self.assertEqual(HTTPStatus.BAD_REQUEST, captured["status"])
        self.assertEqual("EXPECTED_VERSIONS_REQUIRED", captured["payload"]["error_code"])

        invalid = self._payload()
        invalid["expected_configuration_versions"] = {}
        app, captured = self._app()
        app._agent_request = lambda *args, **kwargs: self.fail("must not call Agent")
        app._handle_automation_task_approval_policy(self._handler(invalid))
        self.assertEqual(HTTPStatus.BAD_REQUEST, captured["status"])
        self.assertEqual(
            "EXPECTED_CONFIGURATION_VERSIONS_REQUIRED",
            captured["payload"]["error_code"],
        )


class AutomationApprovalPolicyTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        cls.template = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        ).get_template("automation.html")

    def _render(self, *, can_manage):
        policy_item = {
            "task_id": "clockin_daxiang_s_1833",
            "schedule_label": "每天 18:33",
            "mode": "EXACT_SCHEDULE_EXEMPT",
            "configured_mode": "EXACT_SCHEDULE_EXEMPT",
            "effective_mode": "EXACT_SCHEDULE_EXEMPT",
            "effective_status": "ACTIVE",
            "can_exempt": True,
            "version": 3,
            "configuration_version": 7,
            "policy_hash_short": "ab12cd34",
            "approved_by": "系统管理员",
            "approved_at": "2026-08-14 12:00:00",
            "invalid_reason": "",
        }
        policy = {
            "available": True,
            "task_ids": ["clockin_daxiang_s_1833"],
            "item_count": 1,
            "items": [policy_item],
            "mode": "EXACT_SCHEDULE_EXEMPT",
            "configured_mode": "EXACT_SCHEDULE_EXEMPT",
            "effective_mode": "EXACT_SCHEDULE_EXEMPT",
            "effective_status": "ACTIVE",
            "label": "固定计划自动执行",
            "summary": "仅 Scheduler 定时触发可免审；手工运行仍需审批。",
            "can_exempt": True,
            "mixed": False,
            "expected_versions": {"clockin_daxiang_s_1833": 3},
            "expected_configuration_versions": {"clockin_daxiang_s_1833": 7},
            "policy_hash_short": "ab12cd34",
            "approved_by": "系统管理员",
            "approved_at": "2026-08-14 12:00:00",
            "invalid_reason": "",
        }
        task = {
            "task_id": "clockin_daxiang_s",
            "task_ids": ["clockin_daxiang_s_1833"],
            "task_mode": "scheduled",
            "name_value": "网点打卡-大祥S站",
            "tool_name_value": "clock_in_dual",
            "cron_expression_value": "33 18 * * *",
            "schedule_time_values": ["18:33"],
            "tool_params_json": "{}",
            "tool_param_fields": [],
            "search_text": "clockin",
            "last_activity_value": "",
            "is_schedulable": True,
            "schedule_supported": True,
            "schedule_editable": False,
            "has_webhook": False,
            "enabled_value": True,
            "can_save": False,
            "can_run_now": False,
            "control_plane_only": True,
            "control_plane_notice": "任务配置只读，审批策略单独设置。",
            "approval_policy": policy,
        }
        return self.template.render(
            app_title="Console",
            scheduled_tasks=[task],
            enabled_task_count=1,
            automation_db_warning="",
            automation_account_warning="",
            automation_approval_policy_warning="",
            can_manage_approval_policies=can_manage,
            tms_session_status={},
            tms_session_credentials={},
        )

    def _render_group(self, *, can_manage=True):
        items = [
            {
                "task_id": "delivery_status_0900",
                "schedule_label": "每天 09:00",
                "mode": "REQUIRE_EACH_RUN",
                "configured_mode": "REQUIRE_EACH_RUN",
                "effective_mode": "REQUIRE_EACH_RUN",
                "effective_status": "ACTIVE",
                "can_exempt": True,
                "version": 2,
                "configuration_version": 7,
                "policy_hash_short": "",
                "approved_by": "",
                "approved_at": "",
                "invalid_reason": "",
            },
            {
                "task_id": "delivery_status_0930",
                "schedule_label": "每天 09:30",
                "mode": "EXACT_SCHEDULE_EXEMPT",
                "configured_mode": "EXACT_SCHEDULE_EXEMPT",
                "effective_mode": "REQUIRE_EACH_RUN",
                "effective_status": "STALE",
                "can_exempt": True,
                "version": 4,
                "configuration_version": 9,
                "policy_hash_short": "cd34ef56",
                "approved_by": "系统管理员",
                "approved_at": "2026-08-14 12:00:00",
                "invalid_reason": "任务参数已变化",
            },
        ]
        policy = {
            "available": True,
            "task_ids": [item["task_id"] for item in items],
            "item_count": 2,
            "items": items,
            "mode": "",
            "configured_mode": "",
            "effective_mode": "",
            "effective_status": "MIXED",
            "label": "混合策略",
            "summary": "2 条任务，当前审批策略不一致，可按执行时间分别设置。",
            "can_exempt": True,
            "mixed": True,
            "policy_hash_short": "多项",
            "approved_by": "",
            "approved_at": "",
            "invalid_reason": "任务参数已变化",
        }
        task = {
            "task_id": "delivery_status",
            "task_ids": [item["task_id"] for item in items],
            "task_mode": "scheduled",
            "name_value": "签收状态同步",
            "tool_name_value": "sync_delivery_status",
            "cron_expression_value": "0 9,9 * * *",
            "schedule_time_values": ["09:00", "09:30"],
            "tool_params_json": "{}",
            "tool_param_fields": [],
            "search_text": "delivery",
            "last_activity_value": "",
            "is_schedulable": True,
            "schedule_supported": True,
            "schedule_editable": False,
            "has_webhook": False,
            "enabled_value": True,
            "can_save": False,
            "can_run_now": False,
            "control_plane_only": False,
            "approval_policy": policy,
        }
        return self.template.render(
            app_title="Console",
            scheduled_tasks=[task],
            enabled_task_count=2,
            automation_db_warning="",
            automation_account_warning="",
            automation_approval_policy_warning="",
            can_manage_approval_policies=can_manage,
            tms_session_status={},
            tms_session_credentials={},
        )

    def test_super_admin_sees_policy_editor_without_sensitive_contract(self):
        html = self._render(can_manage=True)
        task_html = html.split("<article", 1)[1].split("</article>", 1)[0]
        self.assertIn("每次运行审批", task_html)
        self.assertIn("固定计划自动执行", task_html)
        self.assertIn("保存审批策略", task_html)
        self.assertIn("ab12cd34", task_html)
        self.assertIn("手工运行仍需审批", task_html)
        self.assertNotIn("完整参数", task_html)
        self.assertNotIn('data-settings-toggle', task_html)

    def test_admin_sees_policy_read_only(self):
        html = self._render(can_manage=False)
        self.assertIn("当前账号只读，需超级管理员修改", html)
        self.assertNotIn("data-approval-policy-save", html)
        self.assertNotIn("data-approval-policy-mode", html)

    def test_group_renders_an_independent_editor_for_each_real_task(self):
        html = self._render_group()

        self.assertIn("每天 09:00", html)
        self.assertIn("每天 09:30", html)
        self.assertIn("delivery_status_0900", html)
        self.assertIn("delivery_status_0930", html)
        self.assertIn("任务参数已变化", html)
        self.assertEqual(2, html.count("data-approval-policy-item\n"))
        self.assertEqual(2, html.count("data-approval-policy-save\n"))
        self.assertEqual(2, html.count("data-approval-policy-mode>"))
        self.assertIn("按执行时间设置审批策略", html)

    def test_group_is_read_only_per_row_for_non_super_admin(self):
        html = self._render_group(can_manage=False)

        self.assertIn("每天 09:00", html)
        self.assertIn("每天 09:30", html)
        self.assertIn("当前账号只读，需超级管理员修改", html)
        self.assertNotIn("data-approval-policy-save", html)
        self.assertNotIn("data-approval-policy-mode", html)

    def test_javascript_has_explicit_confirmation_and_replay_uuid(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "automation_approval_policy.js"
        ).read_text(encoding="utf-8")
        self.assertIn("window.crypto.randomUUID()", source)
        self.assertIn("X-Browser-Request-UUID", source)
        self.assertIn("仅 Scheduler 定时触发可免审", source)
        self.assertIn("任务名称改动不影响授权", source)
        self.assertIn("时间、账号、参数或工具版本变化会立即失效", source)
        self.assertIn('credentials: "same-origin"', source)
        body_start = source.index("body: JSON.stringify({")
        body_end = source.index("}),", body_start)
        request_body = source[body_start:body_end]
        self.assertIn("expected_configuration_versions", request_body)
        self.assertNotIn("contract_hash", request_body)
        self.assertIn("TASK_CONFIGURATION_VERSION_CONFLICT", source)

    def test_javascript_updates_only_the_selected_task_row(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "automation_approval_policy.js"
        ).read_text(encoding="utf-8")

        self.assertIn("const taskId = item.task_id;", source)
        self.assertIn("task_ids: [taskId]", source)
        self.assertIn("expected_versions: { [taskId]: item.version }", source)
        self.assertIn(
            "expected_configuration_versions: { [taskId]: item.configuration_version }",
            source,
        )
        self.assertIn("items.length !== 1", source)
        self.assertIn("items[0]?.task_id !== taskId", source)
        self.assertIn("renderPolicyItem(row, items[0])", source)
        self.assertNotIn("task_ids: policy.task_ids", source)

    def test_hidden_policy_badges_are_not_revealed_by_flex_styles(self):
        static_dir = Path(__file__).resolve().parents[1] / "static"
        style = (static_dir / "style.css").read_text(encoding="utf-8")
        base = (
            Path(__file__).resolve().parents[1] / "templates" / "base.html"
        ).read_text(encoding="utf-8")

        self.assertIn(".auto-approval-policy-restriction[hidden]", style)
        self.assertIn(".auto-approval-policy-meta [hidden]", style)
        self.assertIn("display: none !important", style)
        self.assertIn("style.css?v=cal-console-20260814-policy1", base)


if __name__ == "__main__":
    unittest.main()
