import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


class AutomationRunControlsTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        cls.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )
        cls.template = cls.env.get_template("automation.html")

    def _render(self, task: dict) -> str:
        return self.template.render(
            app_title="Test Console",
            message="",
            message_kind="info",
            settings={},
            scheduled_tasks=[task],
            scheduled_task_count=1,
            enabled_task_count=0,
            automation_db_warning="",
            automation_account_warning="",
            automation_approval_policy_warning="",
            automation_plugin_warning="",
            automation_plugin_packages=[],
            unsupported_automation_ids=[],
            can_manage_plugins=True,
            can_manage_approval_policies=True,
        )

    def test_template_renders_single_run_toggle_control(self):
        html = self._render(
            {
                "task_id": "demo_task",
                "task_mode": "manual",
                "name_value": "测试任务",
                "tool_name_value": "demo_tool",
                "cron_expression_value": "",
                "schedule_time_values": [],
                "tool_params_json": "{}",
                "tool_param_fields": [],
                "search_text": "测试任务 demo_task demo_tool",
                "last_activity_value": "",
                "sort_order": 1,
                "is_schedulable": False,
                "schedule_supported": False,
                "schedule_editable": False,
                "has_webhook": False,
                "enabled_value": False,
                "is_open": False,
                "feedback": None,
                "last_error_summary": "",
                "last_run_value": "",
                "webhook_masked_url": "",
                "webhook_full_url": "",
                "webhook_path": "",
                "webhook_token_enabled": False,
                "webhook_header_name": "X-Agent-Webhook-Token",
                "webhook_body_json": "{}",
            }
        )

        self.assertIn('data-run-now', html)
        self.assertIn('data-run-icon', html)
        self.assertIn('data-run-label', html)
        self.assertIn('data-run-mode="start"', html)
        self.assertNotIn('data-run-cancel', html)
        self.assertIn("/automations/tasks/cancel", html)

    def test_scheduled_task_without_schedule_still_renders_toggle_and_settings(self):
        html = self._render(
            {
                "task_id": "arrive_list",
                "task_mode": "scheduled",
                "name_value": "arrive-list",
                "tool_name_value": "sync_arrive_list",
                "cron_expression_value": "",
                "schedule_time_values": [],
                "tool_params_json": "{}",
                "tool_param_fields": [],
                "search_text": "arrive-list sync_arrive_list",
                "last_activity_value": "",
                "sort_order": 1,
                "is_schedulable": True,
                "schedule_supported": True,
                "schedule_editable": True,
                "has_webhook": False,
                "enabled_value": False,
                "is_open": False,
                "feedback": None,
                "last_error_summary": "",
                "last_run_value": "",
                "webhook_masked_url": "",
                "webhook_full_url": "",
                "webhook_path": "",
                "webhook_token_enabled": False,
                "webhook_header_name": "X-Agent-Webhook-Token",
                "webhook_body_json": "{}",
            }
        )

        self.assertIn('data-automation-toggle', html)
        self.assertIn('data-settings-toggle', html)
        self.assertIn('data-schedule-stack', html)
        self.assertNotIn('data-code-toggle', html)

    def test_control_plane_only_clock_is_read_only(self):
        html = self._render(
            {
                "task_id": "clockin_daxiang_s",
                "task_mode": "scheduled",
                "name_value": "网点打卡-大祥S站",
                "tool_name_value": "clock_in_dual",
                "cron_expression_value": "33 18 * * *",
                "schedule_time_values": ["18:33"],
                "tool_params_json": "{}",
                "tool_param_fields": [],
                "search_text": "网点打卡 大祥S站 clock_in_dual",
                "last_activity_value": "",
                "sort_order": 1,
                "is_schedulable": True,
                "schedule_supported": True,
                "schedule_editable": False,
                "has_webhook": False,
                "enabled_value": True,
                "is_open": False,
                "feedback": None,
                "last_error_summary": "",
                "last_run_value": "",
                "webhook_masked_url": "",
                "webhook_full_url": "",
                "webhook_path": "",
                "webhook_token_enabled": False,
                "webhook_header_name": "X-Agent-Webhook-Token",
                "webhook_body_json": "{}",
                "can_save": False,
                "can_run_now": False,
                "plugin_blocked": True,
                "control_plane_only": True,
                "control_plane_notice": (
                    "代码锁定的既有自动打卡任务；"
                    "任务配置只读，但审批策略可以单独设置。"
                ),
                "approval_policy": {
                    "available": True,
                    "configured_mode": "REQUIRE_EACH_RUN",
                    "effective_mode": "REQUIRE_EACH_RUN",
                    "effective_status": "ACTIVE",
                    "label": "每次运行审批",
                    "summary": "项目已启用的全部可信入口每次运行都需要审批。",
                    "can_full_auto": True,
                    "policy_version": 3,
                    "project_configuration_version": 7,
                },
            }
        )
        task_html = html.split("<article", 1)[1].split("</article>", 1)[0]

        self.assertNotIn("data-automation-toggle", task_html)
        self.assertNotIn("data-run-now", task_html)
        self.assertNotIn("data-settings-toggle", task_html)
        self.assertNotIn("data-schedule-stack", task_html)
        self.assertIn("任务配置只读，但审批策略可以单独设置", task_html)
        self.assertIn("当前任务：已启用", task_html)
        self.assertIn("每次运行审批", task_html)
        self.assertIn("保存权限", task_html)
        self.assertIn("全部可信入口每次运行都需要审批", task_html)
        self.assertNotIn("EXACT_SCHEDULE_EXEMPT", task_html)
        self.assertNotIn("policy_hash", task_html)

    def test_missing_required_resources_render_resource_editor(self):
        html = self._render(
            {
                "task_id": "arrival_stats",
                "task_mode": "manual",
                "name_value": "统计到货数据",
                "tool_name_value": "sync_arrival_stats",
                "cron_expression_value": "",
                "schedule_time_values": [],
                "tool_params_json": "{}",
                "tool_param_fields": [],
                "search_text": "统计到货数据 sync_arrival_stats",
                "last_activity_value": "",
                "sort_order": 1,
                "is_schedulable": False,
                "schedule_supported": False,
                "schedule_editable": False,
                "has_webhook": False,
                "enabled_value": False,
                "is_open": True,
                "feedback": None,
                "last_error_summary": "",
                "last_run_value": "",
                "webhook_masked_url": "",
                "webhook_full_url": "",
                "webhook_path": "",
                "webhook_token_enabled": False,
                "webhook_header_name": "X-Agent-Webhook-Token",
                "webhook_body_json": "{}",
                "resource_blocked": True,
                "missing_required_resources": [
                    "phase7.arrive_primary_sheet",
                    "phase7.arrive_secondary_sheet",
                    "phase7.stats_archive_sheet",
                ],
                "resource_bindings": [
                    {
                        "resource_key": "phase7.arrive_primary_sheet",
                        "required": True,
                        "configured": False,
                        "missing": True,
                        "display_name": "到货清单主表写入配置",
                        "note": "到货清单主表写入配置。",
                        "source": "",
                        "updated_at": "",
                        "editor_json": "{\n  \"spreadsheet_token\": \"shtxxxxxxxx\"\n}",
                        "visual_fields": [
                            {
                                "path": "spreadsheet_token",
                                "label": "电子表格 Token",
                                "value": "shtxxxxxxxx",
                                "kind": "text",
                                "secret": True,
                                "hint": "从飞书电子表格地址中获取。",
                                "empty_null": False,
                            }
                        ],
                    }
                ],
            }
        )

        self.assertIn("缺少运行资源", html)
        self.assertIn("到货清单主表写入配置", html)
        self.assertIn('data-resource-path="spreadsheet_token"', html)
        self.assertIn('type="password"', html)
        self.assertIn("/automations/resources/save", html)
        self.assertIn('data-resource-save', html)
        self.assertIn('data-resource-save-all', html)
        self.assertIn('data-resources-json-hidden', html)
        self.assertNotIn("下面是可直接修改的 JSON 模板", html)
        self.assertNotIn("auto-resource-editor", html)

    def test_configured_resource_renders_saved_json_inside_reconfigure(self):
        html = self._render(
            {
                "task_id": "arrival_stats",
                "task_mode": "manual",
                "name_value": "缁熻鍒拌揣鏁版嵁",
                "tool_name_value": "sync_arrival_stats",
                "cron_expression_value": "",
                "schedule_time_values": [],
                "tool_params_json": "{}",
                "tool_param_fields": [],
                "search_text": "缁熻鍒拌揣鏁版嵁 sync_arrival_stats",
                "last_activity_value": "",
                "sort_order": 1,
                "is_schedulable": False,
                "schedule_supported": False,
                "schedule_editable": False,
                "has_webhook": False,
                "enabled_value": False,
                "is_open": True,
                "feedback": None,
                "last_error_summary": "",
                "last_run_value": "",
                "webhook_masked_url": "",
                "webhook_full_url": "",
                "webhook_path": "",
                "webhook_token_enabled": False,
                "webhook_header_name": "X-Agent-Webhook-Token",
                "webhook_body_json": "{}",
                "resource_blocked": False,
                "missing_required_resources": [],
                "resource_bindings": [
                    {
                        "resource_key": "phase7.arrive_primary_sheet",
                        "required": True,
                        "configured": True,
                        "missing": False,
                        "display_name": "primary sheet resource",
                        "note": "primary sheet resource",
                        "source": "backend_console",
                        "updated_at": "2026-04-24 17:00:28",
                        "editor_json": "{\n  \"spreadsheet_token\": \"sht_real_saved\",\n  \"range\": \"8fc516!A2:R200\"\n}",
                        "visual_fields": [
                            {
                                "path": "spreadsheet_token",
                                "label": "电子表格 Token",
                                "value": "sht_real_saved",
                                "kind": "text",
                                "secret": True,
                                "hint": "从飞书电子表格地址中获取。",
                                "empty_null": False,
                            },
                            {
                                "path": "range",
                                "label": "读取范围",
                                "value": "8fc516!A2:R200",
                                "kind": "text",
                                "secret": False,
                                "hint": "例如 Sheet1!A2:R200。",
                                "empty_null": False,
                            },
                        ],
                    }
                ],
            }
        )

        self.assertIn("已配置", html)
        self.assertIn("重新配置", html)
        self.assertIn('data-resource-path="range"', html)
        self.assertIn("sht_real_saved", html)
        self.assertNotIn("shtxxxxxxxx", html)
        self.assertNotIn("参数 JSON", html)
        self.assertNotIn("代码设置", html)
        self.assertNotIn("auto-json-editor", html)

    def test_settings_save_submit_uses_ajax_and_closes_drawers(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('form.addEventListener("submit", async event =>', source)
        self.assertIn('event.preventDefault();', source)
        self.assertIn('fetch("/automations/tasks/save"', source)
        self.assertIn("function closeAutomationTaskPanels(form)", source)
        self.assertIn("closeAutomationTaskPanels(form);", source)
        self.assertNotIn("data-code-toggle", source)
        self.assertNotIn("data-json-editor", source)

    def test_terminal_output_polling_uses_ajax_json_headers(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        terminal_fetch_index = source.index("fetch(`/automations/tasks/output?")
        terminal_fetch_block = source[terminal_fetch_index : terminal_fetch_index + 400]
        self.assertIn('"X-Requested-With": "XMLHttpRequest"', terminal_fetch_block)
        self.assertIn('"Accept": "application/json"', terminal_fetch_block)

    def test_terminal_output_polling_retries_after_transient_failure(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        catch_index = source.index('termStatus.textContent = "连接失败";')
        catch_block = source[catch_index : catch_index + 500]
        self.assertIn("setTimeout(pollOutput, 3000)", catch_block)
        self.assertIn("runUiState.running", catch_block)

    def test_terminal_output_polling_has_browser_side_timeout(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        terminal_fetch_index = source.index("fetch(`/automations/tasks/output?")
        terminal_fetch_block = source[terminal_fetch_index - 300 : terminal_fetch_index + 500]
        self.assertIn("AbortController", terminal_fetch_block)
        self.assertIn("signal: controller.signal", terminal_fetch_block)
        self.assertIn("clearTimeout(timeoutId)", terminal_fetch_block)

    def test_blocked_runs_render_attention_and_do_not_keep_polling(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("function renderAttentionRun(data)", source)
        self.assertIn('"数据阻塞"', source)
        self.assertIn('"登录已失效"', source)
        self.assertIn("if (data.attention)", source)
        self.assertIn("pendingRun && !data.attention", source)

    def test_approved_batch_attention_states_stop_tracking_when_settled(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("const settledCount = terminalCount + attentionCount;", source)
        self.assertIn("if (settledCount < total)", source)
        self.assertIn("const activeStates = states.filter", source)
        self.assertIn("if (attentionCount > 0)", source)
        self.assertIn("let approvedBatchStates = new Map();", source)
        self.assertIn("const fetchReceipts = receipts.filter", source)
        self.assertIn("approvedBatchStates.set(state.run_id, state)", source)
        self.assertNotIn("Number(state.next_poll_after_ms) ||", source)
        self.assertNotIn("卡片会继续跟踪", source)

    def test_attention_latch_survives_poll_and_request_error_recovery(self):
        source = (Path(__file__).resolve().parents[1] / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("let termAttentionLatched = false;", source)
        self.assertIn("termAttentionLatched = true;", source)
        self.assertIn("if (outputState?.attention)", source)
        self.assertIn(
            "if (!termAttentionLatched && (runUiState.running || termDrawer.dataset.runStartedAt))",
            source,
        )


if __name__ == "__main__":
    unittest.main()
