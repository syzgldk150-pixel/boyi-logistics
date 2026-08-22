import io
import json
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.app import LocalDocFlowApp
from console.services.automation import (
    apply_automation_project_execution_gate,
    build_automation_project_policy_view,
    normalize_automation_approval_batch_result,
    normalize_hidden_automation_ids,
    normalize_automation_pending_approvals,
    normalize_automation_project_policy_items,
)


POLICY_ITEM = {
    "automation_id": "clockin_daxiang",
    "configured_mode": "PROJECT_FULL_AUTO",
    "effective_mode": "PROJECT_FULL_AUTO",
    "effective_status": "ACTIVE",
    "can_full_auto": True,
    "runnable": True,
    "runtime_status": "READY",
    "runtime_reason": "",
    "summary": "项目清单允许的入口完全自动。",
    "updated_by": "系统管理员",
    "updated_at": "2026-08-15 09:00:00",
    "policy_version": 7,
    "project_configuration_version": 11,
}


class AutomationProjectPolicyProjectionTests(unittest.TestCase):
    def test_policy_projection_is_project_level_and_drops_technical_contract(self):
        items = normalize_automation_project_policy_items(
            [
                {
                    **POLICY_ITEM,
                    "task_ids": ["clockin_daxiang_1830"],
                    "policy_hash": "must-not-leak",
                    "manifest_hash": "must-not-leak",
                    "arguments": {"token": "must-not-leak"},
                }
            ]
        )

        self.assertEqual(1, len(items))
        self.assertEqual(
            {
                "automation_id",
                "configured_mode",
                "effective_mode",
                "effective_status",
                "can_full_auto",
                "runnable",
                "runtime_status",
                "runtime_reason",
                "summary",
                "updated_by",
                "updated_at",
                "policy_version",
                "project_configuration_version",
            },
            set(items[0]),
        )
        self.assertNotIn("task_ids", items[0])
        self.assertNotIn("policy_hash", items[0])
        self.assertNotIn("manifest_hash", items[0])

    def test_legacy_per_cron_mode_is_never_a_new_configured_mode(self):
        old_mode = {
            **POLICY_ITEM,
            "configured_mode": "EXACT_SCHEDULE_EXEMPT",
            "effective_mode": "EXACT_SCHEDULE_EXEMPT",
        }
        legacy_effective = {
            **POLICY_ITEM,
            "configured_mode": "REQUIRE_EACH_RUN",
            "effective_mode": "LEGACY_SCHEDULE_ONLY",
            "effective_status": "LEGACY_SCHEDULE_ONLY",
        }

        self.assertEqual([], normalize_automation_project_policy_items([old_mode]))
        normalized = normalize_automation_project_policy_items([legacy_effective])
        self.assertEqual("LEGACY_SCHEDULE_ONLY", normalized[0]["effective_mode"])
        view = build_automation_project_policy_view("clockin_daxiang", normalized[0])
        self.assertEqual("旧版计划权限", view["label"])

    def test_missing_dynamic_project_is_fail_closed_without_policy_downgrade(self):
        view = build_automation_project_policy_view("finance_startup_catchup", None)

        self.assertFalse(view["available"])
        self.assertFalse(view["runnable"])
        self.assertEqual("UNAVAILABLE", view["runtime_status"])
        self.assertEqual("权限状态不可用", view["label"])
        self.assertNotEqual("PROJECT_FULL_AUTO", view["effective_mode"])

    def test_full_auto_runtime_transitions_preserve_policy_intent(self):
        for status, label in (
            ("RECONCILING", "完全自动，运行环境同步中"),
            ("UNAVAILABLE", "完全自动，运行环境不可用"),
        ):
            with self.subTest(runtime_status=status):
                normalized = normalize_automation_project_policy_items(
                    [
                        {
                            **POLICY_ITEM,
                            "effective_status": status,
                            "runnable": False,
                            "runtime_status": status,
                            "runtime_reason": f"RECONCILE_{status}",
                        }
                    ]
                )

                self.assertEqual(1, len(normalized))
                self.assertEqual("PROJECT_FULL_AUTO", normalized[0]["effective_mode"])
                self.assertFalse(normalized[0]["runnable"])
                self.assertEqual(status, normalized[0]["runtime_status"])
                view = build_automation_project_policy_view(
                    "clockin_daxiang", normalized[0]
                )
                self.assertEqual(label, view["label"])
                self.assertEqual("PROJECT_FULL_AUTO", view["configured_mode"])


class AutomationProjectExecutionGateTests(unittest.TestCase):
    @staticmethod
    def _service(response):
        service = LocalDocFlowApp.__new__(LocalDocFlowApp)
        service._mysql_console_principal = lambda _user: {
            "actor_id": "17",
            "roles": ["super_admin"],
        }
        service._agent_request = lambda *args, **kwargs: response
        return service

    @staticmethod
    def _task():
        return {
            "task_id": "clockin_daxiang",
            "plugin": {"automation_id": "clockin_daxiang"},
            "can_run_now": True,
            "run_disabled_reason": "",
        }

    @staticmethod
    def _handler():
        return SimpleNamespace(current_admin_user={"id": 17})

    def test_only_ready_runnable_policy_keeps_console_execution_enabled(self):
        task = self._task()
        service = self._service(
            {"ok": True, "data": {"items": [{**POLICY_ITEM}]}}
        )

        warning, can_manage = service._load_automation_project_policies(
            self._handler(), [task]
        )

        self.assertEqual("", warning)
        self.assertTrue(can_manage)
        self.assertTrue(task["can_run_now"])
        self.assertEqual("READY", task["approval_policy"]["runtime_status"])

    def test_reconciling_and_unavailable_runtime_disable_console_execution(self):
        for runtime_status, reason in (
            ("RECONCILING", "运行环境同步中"),
            ("UNAVAILABLE", "运行环境不可用/待修复"),
        ):
            with self.subTest(runtime_status=runtime_status):
                task = self._task()
                service = self._service(
                    {
                        "ok": True,
                        "data": {
                            "items": [
                                {
                                    **POLICY_ITEM,
                                    "effective_status": runtime_status,
                                    "runnable": False,
                                    "runtime_status": runtime_status,
                                    "runtime_reason": "RECONCILE_READY_TO_COMMIT",
                                }
                            ]
                        },
                    }
                )

                service._load_automation_project_policies(self._handler(), [task])

                self.assertFalse(task["can_run_now"])
                self.assertEqual(reason, task["run_disabled_reason"])
                self.assertEqual(
                    "PROJECT_FULL_AUTO",
                    task["approval_policy"]["configured_mode"],
                )

    def test_ready_but_non_runnable_policy_disables_console_execution(self):
        task = self._task()
        service = self._service(
            {
                "ok": True,
                "data": {
                    "items": [
                        {
                            **POLICY_ITEM,
                            "runnable": False,
                            "runtime_reason": "ENTRYPOINTS_DISABLED",
                        }
                    ]
                },
            }
        )

        service._load_automation_project_policies(self._handler(), [task])

        self.assertFalse(task["can_run_now"])
        self.assertEqual("所有运行入口均已关闭", task["run_disabled_reason"])

    def test_closed_runtime_reasons_are_projected_without_collapsing_to_generic_error(self):
        expected = {
            "PROJECT_DISABLED": "项目已停用",
            "PROJECT_CONFIGURATION_INCOMPLETE": (
                "项目配置尚未完整；运行、启用和完全自动均已阻断。"
            ),
            "ENTRYPOINTS_DISABLED": "所有运行入口均已关闭",
            "PROJECT_CONTRACT_UNAVAILABLE": (
                "项目签名合同错误；运行、启用和完全自动均已阻断。"
            ),
            "RECONCILE_PREPARING": "运行环境同步中",
            "PROJECT_RUNTIME_UNAVAILABLE": "运行环境不可用/待修复",
        }
        for reason, label in expected.items():
            with self.subTest(reason=reason):
                task = self._task()
                apply_automation_project_execution_gate(
                    task,
                    {
                        "available": True,
                        "runnable": False,
                        "runtime_status": (
                            "RECONCILING"
                            if reason.startswith("RECONCILE_")
                            else "UNAVAILABLE"
                            if reason == "PROJECT_RUNTIME_UNAVAILABLE"
                            else "READY"
                        ),
                        "runtime_reason": reason,
                    },
                )
                self.assertEqual(label, task["run_disabled_reason"])

    def test_console_only_disabled_keeps_entrypoint_specific_reason(self):
        task = self._task()
        task["can_run_now"] = False
        task["run_disabled_reason"] = "后台入口已关闭"
        apply_automation_project_execution_gate(
            task,
            {
                "available": True,
                "runnable": True,
                "runtime_status": "READY",
                "runtime_reason": "",
            },
        )
        self.assertEqual("后台入口已关闭", task["run_disabled_reason"])

    def test_hidden_catalog_ids_accept_only_closed_identity_list(self):
        self.assertEqual(
            frozenset({"r7_arrival_checkin", "historic-project"}),
            normalize_hidden_automation_ids(
                {
                    "hidden_automation_ids": [
                        "r7_arrival_checkin",
                        "historic-project",
                        "not valid",
                        123,
                    ]
                }
            ),
        )

    def test_missing_or_failed_policy_load_disables_console_execution(self):
        responses = (
            {"ok": True, "data": {"items": []}},
            {
                "ok": False,
                "error_code": "PROJECT_POLICY_SERVICE_UNAVAILABLE",
            },
        )
        for response in responses:
            with self.subTest(response=response):
                task = self._task()
                service = self._service(response)

                service._load_automation_project_policies(self._handler(), [task])

                self.assertFalse(task["can_run_now"])
                self.assertEqual("项目权限不可用", task["run_disabled_reason"])
                self.assertFalse(task["approval_policy"]["available"])

    def test_pending_projection_is_aggregate_only(self):
        pending = normalize_automation_pending_approvals(
            {
                "automation_id": "clockin_daxiang",
                "pending_count": 3,
                "highest_risk": "HIGH",
                "source_summary": "Scheduler 2、飞书 1",
                "pending_set_hash": "a" * 64,
                "can_approve": True,
                "can_reject": True,
                "approval_ids": ["approval-secret"],
                "plan_hashes": ["plan-secret"],
            },
            expected_automation_id="clockin_daxiang",
        )

        self.assertEqual(
            {
                "automation_id",
                "pending_count",
                "highest_risk",
                "highest_risk_label",
                "source_summary",
                "expected_pending_set_hash",
                "can_approve",
                "can_reject",
            },
            set(pending or {}),
        )
        self.assertNotIn("approval_ids", pending or {})
        self.assertNotIn("plan_hashes", pending or {})

    def test_batch_receipt_projection_is_closed_and_project_bound(self):
        result = normalize_automation_approval_batch_result(
            {
                "decision": "APPROVED",
                "decided_count": 1,
                "run_receipts": [
                    {
                        "automation_id": "clockin_daxiang",
                        "work_item_id": "work-1",
                        "run_id": "run-1",
                        "status": "WAITING_APPROVAL",
                    }
                ],
                "approval_ids": ["hidden"],
                "plan_hashes": ["hidden"],
            },
            expected_automation_id="clockin_daxiang",
            expected_decision="APPROVED",
        )

        self.assertEqual(1, result["decided_count"])
        self.assertEqual(
            {
                "automation_id",
                "work_item_id",
                "run_id",
                "status",
                "next_poll_after_ms",
            },
            set(result["run_receipts"][0]),
        )
        self.assertNotIn("approval_ids", result)
        self.assertNotIn("plan_hashes", result)
        self.assertIsNone(
            normalize_automation_approval_batch_result(
                {
                    "decision": "APPROVED",
                    "decided_count": 1,
                    "run_receipts": [
                        {
                            "automation_id": "another-project",
                            "work_item_id": "work-1",
                            "run_id": "run-1",
                            "status": "WAITING_APPROVAL",
                        }
                    ],
                },
                expected_automation_id="clockin_daxiang",
                expected_decision="APPROVED",
            )
        )


class AutomationProjectPolicyHandlerTests(unittest.TestCase):
    @staticmethod
    def _handler(payload=None, *, role="super_admin"):
        raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
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
    def _policy_payload():
        return {
            "mode": "PROJECT_FULL_AUTO",
            "request_id": "12345678-1234-4234-8234-123456789abc",
            "comment": "固定项目完全自动",
            "expected_policy_version": 7,
            "expected_project_configuration_version": 11,
        }

    @staticmethod
    def _app():
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        captured = {}
        app._send_json = lambda handler, status, payload: captured.update(
            status=status,
            payload=payload,
        )
        return app, captured

    def test_policy_post_forwards_only_locked_dto_with_signed_principal(self):
        app, captured = self._app()
        handler = self._handler(self._policy_payload())
        forwarded = {}

        def agent_request(method, endpoint, **kwargs):
            forwarded.update(method=method, endpoint=endpoint, **kwargs)
            return {"ok": True, "data": {"policy": POLICY_ITEM}}

        app._agent_request = agent_request
        app._handle_automation_project_approval_policy(handler, "clockin_daxiang")

        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual("POST", forwarded["method"])
        self.assertEqual(
            "/internal/v1/automation-projects/clockin_daxiang/approval-policy",
            forwarded["endpoint"],
        )
        self.assertEqual(self._policy_payload(), forwarded["payload"])
        self.assertEqual("17", forwarded["console_principal"]["actor_id"])
        self.assertNotIn("actor", forwarded["payload"])
        self.assertNotIn("task_ids", forwarded["payload"])

    def test_old_mode_extra_ids_and_non_super_admin_are_rejected(self):
        invalid_payloads = [
            {**self._policy_payload(), "mode": "EXACT_SCHEDULE_EXEMPT"},
            {**self._policy_payload(), "task_ids": ["clockin_daxiang_1830"]},
            {**self._policy_payload(), "expected_policy_version": 0},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                app, captured = self._app()
                app._agent_request = lambda *args, **kwargs: self.fail("must not call Agent")
                app._handle_automation_project_approval_policy(
                    self._handler(payload),
                    "clockin_daxiang",
                )
                self.assertEqual(HTTPStatus.BAD_REQUEST, captured["status"])

        app, captured = self._app()
        app._agent_request = lambda *args, **kwargs: self.fail("must not call Agent")
        app._handle_automation_project_approval_policy(
            self._handler(self._policy_payload(), role="admin"),
            "clockin_daxiang",
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, captured["status"])

    def test_pending_get_returns_only_safe_project_summary(self):
        app, captured = self._app()
        handler = self._handler()
        app._agent_request = lambda *args, **kwargs: {
            "ok": True,
            "data": {
                "pending": {
                    "automation_id": "clockin_daxiang",
                    "pending_count": 2,
                    "highest_risk": "HIGH",
                    "source_summary": "Scheduler 2",
                    "pending_set_hash": "b" * 64,
                    "can_approve": True,
                    "can_reject": True,
                    "approval_ids": ["hidden"],
                    "plan_hash": "hidden",
                }
            },
        }

        app._handle_automation_project_pending_approvals_get(handler, "clockin_daxiang")

        pending = captured["payload"]["data"]["pending"]
        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual("b" * 64, pending["expected_pending_set_hash"])
        self.assertNotIn("approval_ids", pending)
        self.assertNotIn("plan_hash", pending)

    def test_batch_action_forwards_only_expected_set_request_and_comment(self):
        body = {
            "expected_pending_set_hash": "c" * 64,
            "request_id": "87654321-4321-4321-8321-cba987654321",
            "comment": "同一项目批量通过",
        }
        app, captured = self._app()
        forwarded = {}

        def agent_request(method, endpoint, **kwargs):
            forwarded.update(method=method, endpoint=endpoint, **kwargs)
            return {
                "ok": True,
                "data": {
                    "decision": "APPROVED",
                    "decided_count": 1,
                    "run_receipts": [
                        {
                            "automation_id": "clockin_daxiang",
                            "work_item_id": "work-1",
                            "run_id": "run-1",
                            "status": "WAITING_APPROVAL",
                        }
                    ],
                    "pending": {
                        "automation_id": "clockin_daxiang",
                        "pending_count": 0,
                        "highest_risk": "",
                        "source_summary": "",
                        "pending_set_hash": "",
                    }
                },
            }

        app._agent_request = agent_request
        app._handle_automation_project_pending_approvals_action(
            self._handler(body),
            "clockin_daxiang",
            "approve",
        )

        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual(body, forwarded["payload"])
        self.assertEqual(
            "/internal/v1/automation-projects/clockin_daxiang/pending-approvals/approve",
            forwarded["endpoint"],
        )
        self.assertNotIn("approval_ids", forwarded["payload"])
        self.assertNotIn("plan_hash", forwarded["payload"])
        response_data = captured["payload"]["data"]
        self.assertEqual(1, response_data["decided_count"])
        self.assertEqual("run-1", response_data["run_receipts"][0]["run_id"])
        self.assertNotIn("approval_id", response_data["run_receipts"][0])
        self.assertNotIn("plan_hash", response_data["run_receipts"][0])

    def test_pending_set_conflict_is_safely_projected_for_inline_refresh(self):
        body = {
            "expected_pending_set_hash": "d" * 64,
            "request_id": "87654321-4321-4321-8321-cba987654321",
            "comment": "",
        }
        app, captured = self._app()
        app._agent_request = lambda *args, **kwargs: {
            "ok": False,
            "status": 409,
            "error_code": "PENDING_SET_CHANGED",
            "error": "集合已变化",
            "data": {
                "pending": {
                    "automation_id": "clockin_daxiang",
                    "pending_count": 1,
                    "highest_risk": "MEDIUM",
                    "source_summary": "Console 1",
                    "pending_set_hash": "e" * 64,
                    "approval_ids": ["hidden"],
                }
            },
        }

        app._handle_automation_project_pending_approvals_action(
            self._handler(body),
            "clockin_daxiang",
            "reject",
        )

        self.assertEqual(HTTPStatus.CONFLICT, captured["status"])
        projected = captured["payload"]["data"]["pending"]
        self.assertEqual(1, projected["pending_count"])
        self.assertNotIn("approval_ids", projected)

    def test_batch_action_rejects_incomplete_or_overbroad_agent_receipts(self):
        body = {
            "expected_pending_set_hash": "f" * 64,
            "request_id": "87654321-4321-4321-8321-cba987654321",
            "comment": "",
        }
        app, captured = self._app()
        app._agent_request = lambda *args, **kwargs: {
            "ok": True,
            "data": {
                "decision": "APPROVED",
                "decided_count": 1,
                "run_receipts": [
                    {
                        "automation_id": "clockin_daxiang",
                        "work_item_id": "work-1",
                        "run_id": "run-1",
                        "status": "WAITING_APPROVAL",
                        "plan_hash": "must-not-cross-console-boundary",
                    }
                ],
                "pending": {
                    "automation_id": "clockin_daxiang",
                    "pending_count": 0,
                    "highest_risk": "",
                    "source_summary": "",
                    "pending_set_hash": "",
                },
            },
        }

        app._handle_automation_project_pending_approvals_action(
            self._handler(body),
            "clockin_daxiang",
            "approve",
        )

        self.assertEqual(HTTPStatus.BAD_GATEWAY, captured["status"])
        self.assertEqual(
            "INVALID_PENDING_APPROVAL_ACTION_RESPONSE",
            captured["payload"]["error"]["code"],
        )
        self.assertIsNone(captured["payload"].get("data"))


class AutomationProjectPolicyTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        cls.template = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        ).get_template("automation.html")

    def _render(self, *, can_manage=True):
        task = {
            "task_id": "clockin_daxiang",
            "task_ids": ["clockin_daxiang_1830", "clockin_daxiang_1900"],
            "task_mode": "scheduled",
            "name_value": "网点打卡-大祥",
            "tool_name_value": "clock_in_dual",
            "cron_expression_value": "0 18,19 * * *",
            "schedule_time_values": ["18:00", "19:00"],
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
            "control_plane_notice": "任务配置只读。",
            "approval_policy": build_automation_project_policy_view(
                "clockin_daxiang",
                POLICY_ITEM,
            ),
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

    def test_one_project_permission_entry_has_exactly_two_product_options(self):
        html = self._render()
        card = html.split("<article", 1)[1].split("</article>", 1)[0]

        self.assertEqual(1, card.count("data-project-policy-toggle"))
        self.assertEqual(2, card.count("data-project-policy-mode"))
        self.assertIn("每次运行审批", card)
        self.assertIn("完全自动", card)
        self.assertNotIn("data-project-policy-comment", card)
        self.assertNotIn("EXACT_SCHEDULE_EXEMPT", card)
        self.assertNotIn("clockin_daxiang_1830", card)
        self.assertNotIn("策略标识", card)
        self.assertNotIn("policy_hash", card)

    def test_pending_strip_has_aggregate_summary_and_card_actions(self):
        html = self._render()

        self.assertIn("项待审批", html)
        self.assertIn("最高风险", html)
        self.assertIn("来源", html)
        self.assertIn("全部审批通过", html)
        self.assertIn("全部驳回", html)
        self.assertIn("data-project-policy-cancel", html)

    def test_non_super_admin_sees_read_only_project_policy(self):
        html = self._render(can_manage=False)

        self.assertIn("当前账号只读，需超级管理员修改", html)
        self.assertNotIn("data-project-policy-save", html)
        self.assertNotIn("data-pending-action", html)

    def test_javascript_uses_project_contract_and_closed_batch_body(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "automation_approval_policy.js"
        ).read_text(encoding="utf-8")

        self.assertIn("PROJECT_FULL_AUTO", source)
        self.assertIn(
            'const RUNTIME_STATUSES = new Set(["READY", "RECONCILING", "UNAVAILABLE"]);',
            source,
        )
        self.assertIn('"active", "reconciling", "unsupported"', source)
        self.assertIn("完全自动权限会持续保留", source)
        self.assertNotIn("系统会恢复为需要审批", source)
        self.assertNotIn("EXACT_SCHEDULE_EXEMPT", source)
        self.assertIn("expected_policy_version", source)
        self.assertIn("expected_project_configuration_version", source)
        self.assertIn("PENDING_SET_CHANGED", source)
        self.assertNotIn("data-project-policy-comment", source)
        batch_start = source.index("body: JSON.stringify({", source.index("async function actOnPending"))
        batch_end = source.index("}),", batch_start)
        batch_body = source[batch_start:batch_end]
        self.assertIn("expected_pending_set_hash", batch_body)
        self.assertIn("request_id", batch_body)
        self.assertIn("comment", batch_body)
        self.assertNotIn("approval_ids", batch_body)
        self.assertNotIn("plan_hash", batch_body)
        self.assertNotIn("automation_id", batch_body)
        self.assertIn("validApprovedRunReceipts", source)
        self.assertIn('new CustomEvent("automation:approved-runs"', source)
        self.assertLess(
            source.index("if (changed)"),
            source.index('new CustomEvent("automation:approved-runs"'),
        )

        template_source = (
            Path(__file__).resolve().parents[1] / "templates" / "automation.html"
        ).read_text(encoding="utf-8")
        self.assertIn('form.addEventListener("automation:approved-runs"', template_source)
        self.assertIn("async function pollApprovedBatch", template_source)
        self.assertIn("batchTerminalStatuses", template_source)
        self.assertIn("已批准，等待执行", template_source)
        self.assertIn("plugin-manager3", template_source)

    def test_assets_are_cache_busted_with_project_governance_styles(self):
        static_dir = Path(__file__).resolve().parents[1] / "static"
        style = (static_dir / "style.css").read_text(encoding="utf-8")
        base = (
            Path(__file__).resolve().parents[1] / "templates" / "base.html"
        ).read_text(encoding="utf-8")

        self.assertIn(".auto-project-governance", style)
        self.assertIn(".auto-pending-approvals[hidden]", style)
        self.assertIn("style.css?v=cal-console-20260822-plugin-manager3", base)
        self.assertNotIn(".automation-plugin-install-panel", style)
        self.assertNotIn(
            ".automation-plugin-install-form { display: grid; grid-template-columns:",
            style,
        )


if __name__ == "__main__":
    unittest.main()
