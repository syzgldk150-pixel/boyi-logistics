import unittest
import json
from http import HTTPStatus
from types import SimpleNamespace

from app import (
    AUTOMATION_RUN_TIMEOUTS,
    AUTOMATION_WORKFLOW_BY_ID,
    LocalDocFlowApp,
    automation_task_provider,
    build_automation_resource_bindings,
    build_virtual_task_defaults,
    flatten_automation_fields,
)


class AutomationSubmissionTests(unittest.TestCase):
    def _make_app(self, form_values: dict) -> LocalDocFlowApp:
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app._parse_urlencoded_form = lambda handler: form_values
        return app

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

    def test_run_now_allows_scheduled_task_without_cron(self):
        app = self._make_app(
            {
                "task_id": "arrive_list",
                "task_mode": "scheduled",
                "name": "arrive-list",
                "tool_name": "sync_arrive_list",
                "cron_expression": "",
                "schedule_times_json": "[]",
                "tool_params_json": "{}",
                "enabled": "",
            }
        )

        payload, override, error_message = app._collect_automation_task_submission(
            None,
            allow_missing_schedule=True,
        )

        self.assertEqual("", error_message)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual("manual", payload["task_mode"])
        self.assertEqual("arrive_list", payload["task_id"])
        self.assertEqual("sync_arrive_list", payload["tool_name"])
        self.assertEqual([], payload["cron_expressions"])
        self.assertEqual("[]", payload["schedule_times_json"])
        self.assertIn("arrive_list", override)

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
                "enabled": "on",
            }
        )

        payload, _override, error_message = app._collect_automation_task_submission(
            None,
            allow_missing_schedule=True,
        )

        self.assertEqual("", error_message)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertTrue(payload["project_plugin_instance"])
        self.assertEqual("automation.finance_action_east.run", payload["tool_name"])
        self.assertEqual({}, payload["tool_params"])
        self.assertEqual("{}", payload["tool_params_json"])
        self.assertEqual([], payload["schedule_times"])
        self.assertEqual([], payload["cron_expressions"])

    def test_save_still_requires_schedule_for_scheduled_task(self):
        app = self._make_app(
            {
                "task_id": "arrive_list",
                "task_mode": "scheduled",
                "name": "arrive-list",
                "tool_name": "sync_arrive_list",
                "cron_expression": "",
                "schedule_times_json": "[]",
                "tool_params_json": "{}",
                "enabled": "",
            }
        )

        payload, _override, error_message = app._collect_automation_task_submission(None)

        self.assertIsNone(payload)
        self.assertIn("请至少设置一个执行时间", error_message)

    def test_resource_bindings_report_missing_required_resources(self):
        bindings = build_automation_resource_bindings("arrival_stats", {})

        missing_required = {
            item["resource_key"]
            for item in bindings
            if item["missing"] and item["required"]
        }
        self.assertEqual(
            {
                "phase7.arrive_primary_sheet",
                "phase7.arrive_secondary_sheet",
                "phase7.stats_archive_sheet",
            },
            missing_required,
        )

    def test_daily_sign_no_longer_requires_legacy_r13_credentials_resource(self):
        bindings = build_automation_resource_bindings("daily_sign", {})

        missing_required = {
            item["resource_key"]
            for item in bindings
            if item["missing"] and item["required"]
        }
        self.assertNotIn("phase7.r13_credentials", missing_required)
        self.assertIn("phase7.daily_sign_bitable", missing_required)
        self.assertIn("phase7.daily_sign_sheet", missing_required)

    def test_yunda_send_waybills_workflow_is_visible_without_default_cron(self):
        workflow = AUTOMATION_WORKFLOW_BY_ID["yunda_send_waybills"]
        defaults = build_virtual_task_defaults("yunda_send_waybills")
        bindings = build_automation_resource_bindings("yunda_send_waybills", {})

        self.assertEqual("sync_yunda_send_waybills", workflow["tool_name"])
        self.assertEqual("scheduled", defaults["task_mode"])
        self.assertEqual(False, defaults["enabled"])
        self.assertEqual(
            {"account_id": "yunda_default", "ensure_fields": False},
            defaults["tool_params"],
        )
        self.assertNotIn("session_profile", defaults["tool_params"])
        self.assertNotIn("default_schedule_times", workflow)
        self.assertEqual(1800, AUTOMATION_RUN_TIMEOUTS["sync_yunda_send_waybills"])
        self.assertEqual(
            ["phase7.yunda_send_waybills_bitable", "phase7.yunda_send_waybills_sheet"],
            [item["resource_key"] for item in bindings],
        )
        self.assertEqual(False, bindings[0]["required"])
        self.assertEqual(False, bindings[1]["required"])

    def test_catalog_defaults_match_production_scheduler_contract(self):
        send_order = AUTOMATION_WORKFLOW_BY_ID["send_order"]
        daily_sign = AUTOMATION_WORKFLOW_BY_ID["daily_sign"]

        self.assertEqual(
            {"account_id": "price_default"},
            send_order["default_tool_params"],
        )
        self.assertEqual(
            "price_default",
            send_order["account_roles"][0]["default_account_id"],
        )
        self.assertEqual(
            {
                "r13_account_id": "r13_default",
                "account_id": "ronghui_daxiang_s",
                "days": 7,
            },
            daily_sign["default_tool_params"],
        )
        tms_role = next(
            role
            for role in daily_sign["account_roles"]
            if role["field"] == "account_id"
        )
        self.assertEqual("ronghui_daxiang_s", tms_role["default_account_id"])
        self.assertEqual(2, len(daily_sign["account_roles"]))

    def test_clock_catalog_is_read_only_and_not_tms_query(self):
        for task_id in ("clockin_daxiang", "clockin_daxiang_s"):
            workflow = AUTOMATION_WORKFLOW_BY_ID[task_id]
            self.assertEqual("clock_in_dual", workflow["tool_name"])
            self.assertTrue(workflow["control_plane_only"])
            self.assertEqual("定时任务", workflow["trigger_label"])
            self.assertIn("参数仍只读", workflow["note"])
            self.assertIn("审批策略由超级管理员单独配置", workflow["note"])

    def test_clock_submission_is_rejected_before_save_or_run(self):
        for task_id in ("clockin_daxiang", "clockin_daxiang_s_1833"):
            with self.subTest(task_id=task_id):
                app = self._make_app(
                    {
                        "task_id": task_id,
                        "task_mode": "scheduled",
                        "name": "legacy clock",
                        "tool_name": "tms_query",
                        "cron_expression": "33 18 * * *",
                        "schedule_times_json": "[\"18:33\"]",
                        "tool_params_json": "{}",
                        "enabled": "on",
                    }
                )

                payload, _override, error_message = (
                    app._collect_automation_task_submission(
                        None,
                        allow_missing_schedule=True,
                    )
                )

                self.assertIsNone(payload)
                self.assertIn("时间、账号与参数仍由代码锁定", error_message)
                self.assertIn("不能修改任务配置", error_message)
                self.assertIn("可以单独设置审批策略", error_message)

    def test_direct_clock_persist_is_fail_closed_without_repository_write(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.repository = SimpleNamespace(
            replace_scheduled_task_group=lambda **kwargs: self.fail(
                "control-plane-only task reached persistence"
            )
        )

        result = app._persist_automation_task(
            {
                "task_id": "clockin_daxiang",
                "task_mode": "scheduled",
            }
        )

        self.assertFalse(result["ok"])
        self.assertIn("时间、账号与参数仍由代码锁定", result["error"])
        self.assertIn("可以单独设置审批策略", result["error"])

    def test_delivery_status_workflow_is_scheduled_scan(self):
        workflow = AUTOMATION_WORKFLOW_BY_ID["delivery_status"]
        defaults = build_virtual_task_defaults("delivery_status")
        bindings = build_automation_resource_bindings("delivery_status", {})

        self.assertEqual("sync_delivery_status", workflow["tool_name"])
        self.assertEqual("scheduled", defaults["task_mode"])
        self.assertEqual({"account_id": "ronghui_default"}, defaults["tool_params"])
        self.assertEqual(1800, AUTOMATION_RUN_TIMEOUTS["sync_delivery_status"])
        self.assertIn(
            "phase7.delivery_status_bitable",
            [item["resource_key"] for item in bindings],
        )
        self.assertEqual(True, bindings[0]["required"])

    def test_send_order_uses_closed_default_account_contract(self):
        defaults = build_virtual_task_defaults("send_order")
        self.assertEqual({"account_id": "price_default"}, defaults["tool_params"])

    def test_arrive_list_uses_closed_default_account_contract(self):
        defaults = build_virtual_task_defaults("arrive_list")
        self.assertEqual({"account_id": "ronghui_default"}, defaults["tool_params"])

    def test_scan_codes_keeps_optional_target_date_field(self):
        defaults = build_virtual_task_defaults("scan_codes")
        fields = {item["path"]: item for item in flatten_automation_fields(defaults["tool_params"])}

        self.assertEqual("", defaults["tool_params"]["target_date"])
        self.assertEqual("date", fields["target_date"]["kind"])
        self.assertEqual("指定日期", fields["target_date"]["label"])
        self.assertIn("默认拉取当天", fields["target_date"]["hint"])

    def test_self_pickup_problem_upload_catalog_points_to_feishu_confirmation(self):
        workflow = AUTOMATION_WORKFLOW_BY_ID["self_pickup_problem_upload"]
        defaults = build_virtual_task_defaults("self_pickup_problem_upload")

        self.assertEqual("self_pickup_problem_upload", workflow["tool_name"])
        self.assertEqual("manual", defaults["task_mode"])
        self.assertEqual(False, defaults["enabled"])
        self.assertEqual(True, defaults["tool_params"]["dry_run"])
        self.assertEqual("ronghui_self_pickup_problem", defaults["tool_params"]["account_id"])
        self.assertEqual(
            "飞书自提到货问题件预览 / 确认",
            workflow["trigger_label"],
        )
        self.assertEqual("请到飞书预览并选择运单", workflow["console_disabled_reason"])
        self.assertEqual("ronghui", automation_task_provider("self_pickup_problem_upload", workflow))
        self.assertEqual(7200, AUTOMATION_RUN_TIMEOUTS["self_pickup_problem_upload"])

    def test_split_pending_problem_upload_catalog_points_to_feishu_selection(self):
        workflow = AUTOMATION_WORKFLOW_BY_ID["split_pending_problem_upload"]
        defaults = build_virtual_task_defaults("split_pending_problem_upload")

        self.assertEqual("split_pending_problem_upload", workflow["tool_name"])
        self.assertEqual("manual", defaults["task_mode"])
        self.assertEqual(False, defaults["enabled"])
        self.assertEqual(True, defaults["tool_params"]["dry_run"])
        self.assertEqual("飞书分批预览 / 选择 / 确认", workflow["trigger_label"])
        self.assertEqual("请到飞书预览并选择运单", workflow["console_disabled_reason"])
        self.assertEqual("ronghui", automation_task_provider("split_pending_problem_upload", workflow))
        self.assertEqual(7200, AUTOMATION_RUN_TIMEOUTS["split_pending_problem_upload"])

    def test_daily_sign_account_roles_select_r13_and_one_tms_account(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        tasks = [
            {
                "task_id": "daily_sign",
                "tool_name_value": "sync_daily_should_sign",
                "provider": "",
                "tool_params_json": json.dumps(
                    {
                        "r13_account_id": "r13_ops",
                        "account_id": "ronghui_daxiang_s",
                    }
                ),
            }
        ]
        accounts = [
            {
                "account_id": "ronghui_default",
                "system": "ronghui",
                "name": "TMS融辉默认账号",
                "is_active": True,
                "is_default": True,
            },
            {
                "account_id": "ronghui_daxiang_s",
                "system": "ronghui",
                "name": "TMS邵阳大祥站账号",
                "is_active": True,
                "is_default": False,
            },
            {
                "account_id": "r13_default",
                "system": "r13",
                "name": "R13默认账号",
                "is_active": True,
                "is_default": True,
            },
            {
                "account_id": "r13_ops",
                "system": "r13",
                "name": "R13运营账号",
                "is_active": True,
                "is_default": False,
            },
        ]

        app._enrich_automation_tasks_with_accounts(tasks, accounts)

        roles = {item["field"]: item for item in tasks[0]["account_role_bindings"]}
        self.assertEqual("r13_ops", roles["r13_account_id"]["selected_account_id"])
        self.assertEqual(
            ["r13_default", "r13_ops"],
            [item["account_id"] for item in roles["r13_account_id"]["options"]],
        )
        self.assertEqual("ronghui_daxiang_s", roles["account_id"]["selected_account_id"])
        self.assertEqual(
            ["ronghui_default", "ronghui_daxiang_s"],
            [item["account_id"] for item in roles["account_id"]["options"]],
        )
        self.assertEqual({"r13_account_id", "account_id"}, set(roles))

    def test_uninstalled_legacy_task_exposes_roles_without_implicit_account_defaults(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        tasks = [
            {
                "task_id": "self_pickup_problem_upload",
                "tool_name_value": "self_pickup_problem_upload",
                "provider": "",
                "tool_params_json": "{}",
            }
        ]
        accounts = [
            {
                "account_id": "ronghui_default",
                "system": "ronghui",
                "name": "TMS融辉默认账号",
                "is_active": True,
                "is_default": True,
            },
            {
                "account_id": "ronghui_self_pickup_problem",
                "system": "ronghui",
                "name": "自提部账号",
                "is_active": True,
                "is_default": False,
            },
            {
                "account_id": "ronghui_daxiang_s",
                "system": "ronghui",
                "name": "大祥S站账号",
                "is_active": True,
                "is_default": False,
            },
        ]

        app._enrich_automation_tasks_with_accounts(tasks, accounts)

        roles = {item["field"]: item for item in tasks[0]["account_role_bindings"]}
        self.assertEqual("自提部账号", roles["account_id"]["label"])
        self.assertEqual("", roles["account_id"]["selected_account_id"])
        self.assertEqual("大祥S站账号", roles["daxiang_s_account_id"]["label"])
        self.assertEqual("", roles["daxiang_s_account_id"]["selected_account_id"])
        self.assertEqual(
            {"ronghui_default", "ronghui_self_pickup_problem", "ronghui_daxiang_s"},
            {item["account_id"] for item in roles["account_id"]["options"]},
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

    def test_delivery_status_can_save_daily_schedule_without_webhook_params(self):
        app = self._make_app(
            {
                "task_id": "delivery_status",
                "task_mode": "scheduled",
                "name": "查询并更新签收状态",
                "tool_name": "sync_delivery_status",
                "cron_expression": "",
                "schedule_times_json": "[\"08:30\"]",
                "tool_params_json": "{}",
                "enabled": "on",
            }
        )

        payload, _override, error_message = app._collect_automation_task_submission(None)

        self.assertEqual("", error_message)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual("scheduled", payload["task_mode"])
        self.assertEqual("sync_delivery_status", payload["tool_name"])
        self.assertEqual({}, payload["tool_params"])
        self.assertEqual(["08:30"], payload["schedule_times"])
        self.assertEqual(["30 8 * * *"], payload["cron_expressions"])
        self.assertEqual(True, payload["enabled"])

    def test_account_role_select_overrides_manual_json_account_id(self):
        app = self._make_app(
            {
                "task_id": "send_order",
                "task_mode": "scheduled",
                "name": "获取当日寄件数据",
                "tool_name": "sync_daily_send_orders",
                "cron_expression": "",
                "schedule_times_json": "[\"23:59\"]",
                "tool_params_json": json.dumps(
                    {"account_id": "TMS融辉默认账号", "max_pages": 2},
                    ensure_ascii=False,
                ),
                "account_role__account_id": "ronghui_default",
                "enabled": "on",
            }
        )

        payload, override, error_message = app._collect_automation_task_submission(None)

        self.assertEqual("", error_message)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual("ronghui_default", payload["tool_params"]["account_id"])
        self.assertEqual(2, payload["tool_params"]["max_pages"])
        saved_params = json.loads(payload["tool_params_json"])
        self.assertEqual("ronghui_default", saved_params["account_id"])
        override_params = json.loads(override["send_order"]["tool_params_json"])
        self.assertEqual("ronghui_default", override_params["account_id"])

    def test_yunda_account_role_select_overrides_manual_json_account_id(self):
        app = self._make_app(
            {
                "task_id": "yunda_send_waybills",
                "task_mode": "scheduled",
                "name": "韵达寄件运单同步",
                "tool_name": "sync_yunda_send_waybills",
                "cron_expression": "",
                "schedule_times_json": "[\"21:00\"]",
                "tool_params_json": json.dumps(
                    {"account_id": "韵达默认账号", "ensure_fields": True},
                    ensure_ascii=False,
                ),
                "account_role__account_id": "yunda_default",
                "enabled": "on",
            }
        )

        payload, _override, error_message = app._collect_automation_task_submission(None)

        self.assertEqual("", error_message)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual("yunda_default", payload["tool_params"]["account_id"])
        self.assertEqual(True, payload["tool_params"]["ensure_fields"])

    def test_run_now_returns_friendly_error_when_required_resources_missing(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.repository = SimpleNamespace(list_workflow_resources=lambda: [])
        app._is_ajax_request = lambda handler: True
        app._control_plane_write_context = lambda handler: {
            "actor": {"actor_type": "console_admin", "actor_id": "17", "roles": ["admin"]},
            "actor_roles": ["admin"],
            "source": "console",
        }
        app._collect_automation_task_submission = lambda handler, allow_missing_schedule=False: (
            {
                "task_id": "arrival_stats",
                "name": "统计到货数据",
                "tool_name": "sync_arrival_stats",
                "task_mode": "manual",
                "tool_params": {},
                "tool_params_json": "{}",
            },
            {},
            "",
        )
        captured = {}
        app._send_json = lambda handler, status, payload: captured.update({"status": status, "payload": payload})

        handler = SimpleNamespace(headers={"X-Browser-Request-UUID": "request-1"})
        app._handle_automation_task_run_now(handler)

        self.assertEqual(False, captured["payload"]["ok"])
        self.assertEqual("执行未开始", captured["payload"]["title"])
        self.assertIn("每日到货表写入配置", captured["payload"]["message"])
        self.assertIn("到货统计归档表配置", captured["payload"]["message"])

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
                    lambda _handler, allow_missing_schedule=False: (
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

    def test_batch_resource_save_persists_multiple_resources(self):
        saved = []
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app._is_ajax_request = lambda handler: True
        app._parse_urlencoded_form = lambda handler: {
            "task_id": "arrival_stats",
            "resources_json": json.dumps(
                {
                    "phase7.arrive_primary_sheet": json.dumps(
                        {
                            "spreadsheet_token": "sht_demo",
                            "range": "8fc516!A2:R200",
                            "clear_range": "8fc516!A2:R200",
                        }
                    ),
                    "phase7.stats_archive_sheet": json.dumps(
                        {
                            "spreadsheet_token": "sht_demo",
                            "default_write_range": "A1:S199",
                        }
                    ),
                }
            ),
        }
        app.repository = SimpleNamespace(
            upsert_workflow_resource=lambda key, config, source="backend_console": saved.append(
                (key, config, source)
            )
        )
        captured = {}
        app._send_json = lambda handler, status, payload: captured.update({"status": status, "payload": payload})

        app._handle_automation_resource_save(None)

        self.assertEqual(True, captured["payload"]["ok"])
        self.assertEqual(2, len(saved))
        self.assertEqual("phase7.arrive_primary_sheet", saved[0][0])
        self.assertEqual("backend_console", saved[0][2])
        self.assertIn("phase7.stats_archive_sheet", captured["payload"]["saved"])

    def test_task_output_returns_latest_runtime_when_agent_output_unavailable(self):
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
        self.assertEqual(True, captured["payload"]["runtime"]["ok"])
        self.assertEqual("最近一次立即执行", captured["payload"]["runtime"]["title"])
        self.assertEqual("2026-06-20 16:47:43", captured["payload"]["runtime"]["last_run"])
        self.assertEqual("21 分 3 秒", captured["payload"]["runtime"]["duration_label"])


if __name__ == "__main__":
    unittest.main()
