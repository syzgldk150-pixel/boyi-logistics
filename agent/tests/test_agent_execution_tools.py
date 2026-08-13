"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403


def _resolved_r7_test_params(params, **_kwargs):
    resolved = dict(params or {})
    resolved.setdefault("account_id", "r7_default")
    resolved.setdefault("session_profile", "r7_default")
    resolved.setdefault("username", "synthetic-r7-user")
    resolved.setdefault("password", "synthetic-r7-password")
    return resolved


class _CompletedRunSteps:
    def __init__(self, data):
        self._data = data

    def list_for_run(self, _run_id):
        return [
            {
                "result_summary_json": {
                    "status": "SUCCESS",
                    "data": self._data,
                    "meta": {},
                    "warnings": [],
                    "error": None,
                }
            }
        ]


class _NoApprovals:
    def get_latest_for_run(self, _run_id, *, for_update=False):
        return None


class _CompletedRunUow:
    def __init__(self, data):
        self.steps = _CompletedRunSteps(data)
        self.approvals = _NoApprovals()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _CompletedRunRepository:
    def __init__(self, data):
        self._data = data

    def unit_of_work(self):
        return _CompletedRunUow(self._data)


class _CompletedRunGateway:
    def __init__(self):
        self.commands = []

    async def submit_and_wait(self, command, *, timeout_seconds):
        self.commands.append(command)
        return {
            "status": "COMPLETED",
            "command_id": command.command_id,
            "work_item_id": "work-item-1",
            "run_id": "run-1",
            "correlation_id": command.correlation_id,
        }


def _configure_completed_control_plane(core, *, data):
    gateway = _CompletedRunGateway()
    core.configure_orchestration(
        command_gateway=gateway,
        repository=_CompletedRunRepository(data),
        workflow_runner=object(),
        execution_runtime=object(),
    )
    return gateway


class AgentExecutionToolTests(unittest.TestCase):
    def setUp(self):
        self.internal_token_patch = patch.dict(
            os.environ,
            {"AGENT_INTERNAL_API_TOKEN": "test-internal-token"},
            clear=False,
        )
        self.send_order_sql_patch = patch(
            "tools.send_order_sync_tool.sync_console_waybills",
            return_value={"ok": True, "upserted": 0, "updates": 0, "creates": 0, "deleted_stale": 0},
        )
        self.yunda_send_sql_patch = patch(
            "tools.yunda_send_waybills_sync_tool.sync_console_waybills",
            return_value={"ok": True, "upserted": 0, "updates": 0, "creates": 0, "deleted_stale": 0},
        )
        self.delivery_status_sql_patch = patch(
            "tools.delivery_status_sync_tool.update_console_waybill_statuses",
            return_value={"ok": True, "updated": 0, "status": "signed"},
        )
        self.internal_token_patch.start()
        self.send_order_sql_mock = self.send_order_sql_patch.start()
        self.addCleanup(self.internal_token_patch.stop)
        self.yunda_send_sql_mock = self.yunda_send_sql_patch.start()
        self.delivery_status_sql_mock = self.delivery_status_sql_patch.start()
        self.addCleanup(self.send_order_sql_patch.stop)
        self.addCleanup(self.yunda_send_sql_patch.stop)
        self.addCleanup(self.delivery_status_sql_patch.stop)

    def test_agent_blocks_unverified_execution_completion_claim(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                return []

        class _FakeLLM:
            async def chat(self, messages, tools=None):
                return {"content": "同步已完成，已写入MySQL和飞书表格。"}

        run_track = Mock(return_value={"tracking_number": "R00014513348", "route_rows": []})
        core = AgentCore(direct_tool_runners={"track_waybill": run_track})
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "执行一次未知脚本",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertEqual("没有匹配到可执行脚本，我不知道该执行哪个任务。", result["reply"])

    def test_agent_blocks_freeform_execution_answer_without_tool_call(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                return []

        class _FakeLLM:
            async def chat(self, messages, tools=None):
                return {"content": "我来处理这个任务。"}

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "执行一个不存在的脚本",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertEqual("没有匹配到可执行脚本，我不知道该执行哪个任务。", result["reply"])

    def test_agent_blocks_plain_freeform_answer_without_tool_call(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                return []

        class _FakeLLM:
            async def chat(self, messages, tools=None):
                return {"content": "这是一个普通聊天回复。"}

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "你好",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertEqual("没有匹配到可执行脚本，我不知道该执行哪个任务。", result["reply"])

    def test_agent_formats_real_tool_result_instead_of_llm_summary(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

            def save_tool_log(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                return [{"type": "function", "function": {"name": "track_waybill"}}]

            def get_capability(self, name):
                if name != "track_waybill":
                    return None
                return {
                    "name": name,
                    "version": "1.0.0",
                    "operation_type": "read",
                }

        class _FakeLLM:
            def __init__(self):
                self.calls = 0

            async def chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "track_waybill",
                                    "arguments": '{"tracking_number":"R00014513348"}',
                                },
                            }
                        ],
                    }
                return {"content": "假的 LLM 总结：已经处理好了。"}

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()
        _configure_completed_control_plane(
            core,
            data={"tracking_number": "R00014513348", "route_rows": []},
        )

        result = asyncio.run(
            core.handle_message(
                "帮我发车",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertIn("R00014513348", result["reply"])
        self.assertNotIn("假的 LLM 总结", result["reply"])
        self.assertEqual("track_waybill", result["executed_tools"][0]["tool_name"])
        self.assertTrue(result["executed_tools"][0]["result"]["success"])

    def test_agent_login_message_does_not_reach_llm(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                raise AssertionError("login message should return before tool schema lookup")

        class _FakeLLM:
            async def chat(self, *args, **kwargs):
                raise AssertionError("login message should not be routed to LLM")

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "登陆",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertEqual("1. 大祥账号\n2. 操作场账号\n3. 韵达账号", result["reply"])

    def test_agent_submits_track_waybill_to_control_plane_adapter_path(self):
        class _FakeRegistry:
            def get_capability(self, name):
                return {"name": name, "version": "1.0.0", "operation_type": "read"}

        run_track = Mock(return_value={"tracking_number": "R00014513348", "route_rows": []})
        core = AgentCore(direct_tool_runners={"track_waybill": run_track})
        core.registry = _FakeRegistry()
        gateway = _configure_completed_control_plane(
            core,
            data={"tracking_number": "R00014513348", "route_rows": []},
        )

        result = asyncio.run(core.execute_tool("track_waybill", {"tracking_number": "R00014513348"}))

        run_track.assert_not_called()
        self.assertEqual("track_waybill", gateway.commands[0].parameters["tool_name"])
        self.assertTrue(result["success"])
        self.assertEqual("R00014513348", result["data"]["tracking_number"])

    def test_price_tool_queries_ronghui_and_yunda_concurrently(self):
        threading = __import__("threading")
        yunda_started = threading.Event()

        def _fake_ronghui(**kwargs):
            return {
                "目的网点": "隆尧莲子镇S站",
                "saw_yunda_started": yunda_started.wait(timeout=0.1),
            }

        def _fake_yunda(**kwargs):
            yunda_started.set()
            return {"目的网点": "隆尧莲子镇分部"}

        with (
            patch("tools.price_tool.PRICE_TOOL_PREFER_HTTP", True),
            patch("tools.price_tool.get_price_via_http", side_effect=_fake_ronghui),
            patch("tools.price_tool.get_yunda_price_via_http", side_effect=_fake_yunda),
        ):
            result = price_tool.get_combined_price(
                address="河北省邢台市隆尧县莲子镇中学",
                weight=199,
                volume=2.727,
            )

        self.assertTrue(result["ronghui"]["saw_yunda_started"])
        self.assertEqual("隆尧莲子镇分部", result["yunda"]["目的网点"])

    def test_agent_submits_get_price_to_control_plane_adapter_path(self):
        class _FakeRegistry:
            def get_capability(self, name):
                return {"name": name, "version": "1.0.0", "operation_type": "read"}

        run_price = Mock(return_value={"mode": "agent_tms_combined", "ronghui": {}, "yunda": {}})
        core = AgentCore(direct_tool_runners={"get_price": run_price})
        core.registry = _FakeRegistry()
        gateway = _configure_completed_control_plane(
            core,
            data={"mode": "agent_tms_combined", "ronghui": {}, "yunda": {}},
        )

        params = {"address": "河北省邢台市隆尧县莲子镇中学", "weight": 199, "volume": 2.727}
        result = asyncio.run(core.execute_tool("get_price", params))

        run_price.assert_not_called()
        self.assertEqual("get_price", gateway.commands[0].parameters["tool_name"])
        self.assertTrue(result["success"])
        self.assertEqual("agent_tms_combined", result["data"]["mode"])

    def test_r7_arrival_checkin_tool_passes_managed_credentials_only_to_script_runtime(self):
        captured: dict[str, Any] = {}

        def _fake_run_once(params):
            captured.update(params)
            return {
                "ok": True,
                "stage": "done",
                "message": "success",
                "detail": {"status_text": params.get("status_text")},
                "cost_sec": 1.2,
            }

        with (
            patch(
                "tools.r7_arrival_checkin_tool.resolve_account_params",
                side_effect=_resolved_r7_test_params,
            ),
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_arrival_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once", side_effect=_fake_run_once),
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin(
                {
                    "username": "should-not-be-stored",
                    "password": "should-not-be-stored",
                    "status_text": "已调度",
                    "timeout_sec": 900,
                    "daily_success_limit": 2,
                    "_scheduled_task": {"id": "r7_arrival_checkin"},
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["detail"]["success_count_today"])
        self.assertEqual(2, result["detail"]["daily_success_limit"])
        self.assertEqual("车辆到达", captured["status_text"])
        self.assertTrue(captured["headless"])
        self.assertTrue(captured["do_arrive_wait_unload"])
        self.assertNotIn("daily_success_limit", captured)
        self.assertEqual("should-not-be-stored", captured["username"])
        self.assertEqual("should-not-be-stored", captured["password"])
        self.assertEqual(
            "***",
            r7_arrival_checkin_tool._sanitize_for_log(captured)["password"],
        )
        self.assertNotIn("timeout_sec", captured)
        self.assertNotIn("_scheduled_task", captured)
        self.assertEqual("success", insert_log.call_args.kwargs["status"])
        self.assertEqual(0, insert_log.call_args.kwargs["success_count_before"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_after"])

    def test_r7_arrival_checkin_tool_keeps_dispatched_status_for_dry_run(self):
        captured: dict[str, Any] = {}

        def _fake_run_once(params):
            captured.update(params)
            return {"ok": True, "stage": "checkbox_clicked", "message": "checkbox clicked"}

        with (
            patch(
                "tools.r7_arrival_checkin_tool.resolve_account_params",
                side_effect=_resolved_r7_test_params,
            ),
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_arrival_checkin_tool._insert_log"),
            patch("tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once", side_effect=_fake_run_once),
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin(
                {"status_text": "已调度", "do_arrive_wait_unload": False}
            )

        self.assertTrue(result["ok"])
        self.assertEqual("已调度", captured["status_text"])
        self.assertFalse(captured["do_arrive_wait_unload"])

    def test_r7_arrival_checkin_tool_skips_when_daily_limit_reached(self):
        with (
            patch(
                "tools.r7_arrival_checkin_tool.resolve_account_params",
                side_effect=_resolved_r7_test_params,
            ),
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=1),
            patch("tools.r7_arrival_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once") as run_once,
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin({"daily_success_limit": 1})

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual("daily_limit_reached", result["stage"])
        self.assertEqual(1, result["detail"]["success_count_today"])
        run_once.assert_not_called()
        self.assertEqual("skipped", insert_log.call_args.kwargs["status"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_before"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_after"])

    def test_r7_arrival_checkin_tool_marks_script_not_ok_as_error(self):
        with (
            patch(
                "tools.r7_arrival_checkin_tool.resolve_account_params",
                side_effect=_resolved_r7_test_params,
            ),
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_arrival_checkin_tool._insert_log") as insert_log,
            patch(
                "tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once",
                return_value={"ok": False, "stage": "not_found", "message": "未找到满足条件的行"},
            ),
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin({})

        self.assertFalse(result["ok"])
        self.assertEqual("not_found", result["stage"])
        self.assertIn("未找到满足条件的行", result["error"])
        self.assertEqual("failure", insert_log.call_args.kwargs["status"])

    def test_r7_departure_checkin_tool_passes_managed_credentials_only_to_script_runtime(self):
        captured: dict[str, Any] = {}

        def _fake_run_once(params):
            captured.update(params)
            return {
                "ok": True,
                "stage": "done",
                "message": "success",
                "detail": {
                    "status_text": params.get("status_text"),
                    "verify_status_text": params.get("verify_status_text"),
                    "class_name": params.get("class_name"),
                    "departure_time": "2026-04-29 21:30:00",
                    "plate_numbers": params.get("plate_numbers"),
                },
                "cost_sec": 1.2,
            }

        with (
            patch(
                "tools.r7_departure_checkin_tool.resolve_account_params",
                side_effect=_resolved_r7_test_params,
            ),
            patch("tools.r7_departure_checkin_tool._prepare_log_storage"),
            patch("tools.r7_departure_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_departure_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_departure_checkin_tool.auto_departure_r7.run_once", side_effect=_fake_run_once),
        ):
            result = r7_departure_checkin_tool.run_r7_departure_checkin(
                {
                    "username": "should-not-be-stored",
                    "password": "should-not-be-stored",
                    "status_text": "已调度",
                    "verify_status_text": "装车待发",
                    "class_name": "邵阳操作场-长沙",
                    "plate_numbers": "湘AK6980,湘B12345",
                    "timeout_sec": 900,
                    "daily_success_limit": 2,
                    "_scheduled_task": {"id": "r7_departure_checkin"},
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["detail"]["success_count_today"])
        self.assertEqual(2, result["detail"]["daily_success_limit"])
        self.assertEqual("已调度", captured["status_text"])
        self.assertEqual("装车待发", captured["verify_status_text"])
        self.assertEqual("邵阳操作场-长沙", captured["class_name"])
        self.assertEqual("湘AK6980,湘B12345", captured["plate_numbers"])
        self.assertTrue(captured["headless"])
        self.assertNotIn("daily_success_limit", captured)
        self.assertEqual("should-not-be-stored", captured["username"])
        self.assertEqual("should-not-be-stored", captured["password"])
        self.assertEqual(
            "***",
            r7_departure_checkin_tool._sanitize_for_log(captured)["password"],
        )
        self.assertNotIn("timeout_sec", captured)
        self.assertNotIn("_scheduled_task", captured)
        self.assertEqual("success", insert_log.call_args.kwargs["status"])
        self.assertEqual(0, insert_log.call_args.kwargs["success_count_before"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_after"])

    def test_r7_departure_checkin_tool_skips_when_daily_limit_reached(self):
        with (
            patch(
                "tools.r7_departure_checkin_tool.resolve_account_params",
                side_effect=_resolved_r7_test_params,
            ),
            patch("tools.r7_departure_checkin_tool._prepare_log_storage"),
            patch("tools.r7_departure_checkin_tool._count_successes_today", return_value=1),
            patch("tools.r7_departure_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_departure_checkin_tool.auto_departure_r7.run_once") as run_once,
        ):
            result = r7_departure_checkin_tool.run_r7_departure_checkin({"daily_success_limit": 1})

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual("daily_limit_reached", result["stage"])
        self.assertEqual(1, result["detail"]["success_count_today"])
        run_once.assert_not_called()
        self.assertEqual("skipped", insert_log.call_args.kwargs["status"])

    def test_r7_departure_checkin_tool_marks_script_not_ok_as_error(self):
        with (
            patch(
                "tools.r7_departure_checkin_tool.resolve_account_params",
                side_effect=_resolved_r7_test_params,
            ),
            patch("tools.r7_departure_checkin_tool._prepare_log_storage"),
            patch("tools.r7_departure_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_departure_checkin_tool._insert_log") as insert_log,
            patch(
                "tools.r7_departure_checkin_tool.auto_departure_r7.run_once",
                return_value={"ok": False, "stage": "target_match_failed", "message": "目标车牌未唯一命中"},
            ),
        ):
            result = r7_departure_checkin_tool.run_r7_departure_checkin({})

        self.assertFalse(result["ok"])
        self.assertEqual("target_match_failed", result["stage"])
        self.assertIn("目标车牌未唯一命中", result["error"])
        self.assertEqual("failure", insert_log.call_args.kwargs["status"])

    def test_phase7_resource_import_has_no_n8n_dependency(self):
        target = Path(__file__).resolve().parents[1] / "agent" / "phase7_resource_import.py"
        text = target.read_text(encoding="utf-8")
        self.assertNotIn("n8n", text.lower())
        self.assertNotIn("sqlite", text.lower())

    def test_arrive_list_sync_handles_malformed_fetch_response(self):
        with patch("tools.arrive_list_sync_tool.call_http_service", return_value={"unexpected": True}):
            result = arrive_list_sync_tool.run_arrive_list_sync(
                {"account_id": "ronghui-test"}
            )
        self.assertIn("fetch_dispatch 返回格式异常", result["error"])

    def test_yunda_dispatch_forecast_fetch_maps_required_fields(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "total": 1,
                    "rows": [
                        {
                            "ship_id": "YD001",
                            "unit_cnt": "3",
                            "scan_cnt": "2",
                            "frgt_wgt": "12.5",
                            "frgt_vol": "0.3",
                            "pkg_lod_typ": "纸箱",
                            "fld_tm": "2026-05-10 18:00:00",
                            "plan_tlns": "24",
                            "rcv_cust_addr": "湖南省邵阳市测试地址",
                            "est_arv_tm": "2026-05-11 12:00:00",
                            "due_delv_dt": "2026-05-11",
                        }
                    ],
                }

        class Session:
            def get(self, *args, **kwargs):
                self.kwargs = kwargs
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("yunda_dispatch_forecast.get_session_broker", return_value=broker):
            result = yunda_dispatch_forecast.run_once({"target_date": "2026-05-11", "page_size": 200})

        self.assertTrue(result["ok"])
        self.assertEqual("2026-05-11", result["target_date"])
        self.assertEqual(1, result["total"])
        self.assertEqual(1, result["fetched"])
        self.assertEqual("YD001", result["records"][0]["主单号"])
        self.assertEqual("湖南省邵阳市测试地址", result["records"][0]["开单目的地址"])
        self.assertEqual("2026-05-11 00:00:00", session.kwargs["params"]["bgn_dt"])

    def test_yunda_dispatch_forecast_fetch_auth_redirect_raises_auth_required(self):
        class Response:
            status_code = 302
            headers = {"Location": "/login"}
            text = ""

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        with self.assertRaises(Exception) as ctx:
            yunda_dispatch_forecast.fetch_page(
                Session(),
                {},
                target_date=date(2026, 5, 11),
                limit=200,
                offset=0,
            )

        self.assertEqual("AUTH_REQUIRED", getattr(ctx.exception, "code", ""))

    def test_yunda_dispatch_forecast_fetch_empty_body_raises_auth_required(self):
        class Response:
            status_code = 200
            headers = {"content-type": "text/plain"}
            text = ""

            def raise_for_status(self):
                return None

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        with self.assertRaises(Exception) as ctx:
            yunda_dispatch_forecast.fetch_page(
                Session(),
                {},
                target_date=date(2026, 5, 11),
                limit=200,
                offset=0,
            )

        self.assertEqual("AUTH_REQUIRED", getattr(ctx.exception, "code", ""))
