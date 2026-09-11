"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403
from agent.orchestration.models import Actor, ActorType
from agent.tool_registry import ToolRegistry
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tests.chat_runtime_support import configure_chat


def direct_admin():
    return Actor(ActorType.CONSOLE_ADMIN, "test-admin", roles=("admin",), authenticated_by="mysql_admin_session")


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
    core.configure_direct_readers({name: (lambda _arguments: data)
                                  for name in ("track_waybill", "get_price")
                                  if name not in core._direct_tool_runners})
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
        core = AgentCore()
        core.llm = SimpleNamespace(chat=AsyncMock(return_value={"content": "同步已完成，已写入MySQL和飞书表格。"}))
        _, calls = configure_chat(core)
        result = asyncio.run(core.handle_message("执行一次未知脚本", actor=direct_admin(), source="console"))
        self.assertIn("本次未发起插件执行", result["reply"])
        self.assertNotIn("已写入", result["reply"])
        self.assertEqual([], calls)

    def test_agent_blocks_freeform_execution_answer_without_tool_call(self):
        core = AgentCore()
        core.llm = SimpleNamespace(chat=AsyncMock(return_value={"content": "我来处理这个任务。"}))
        _, calls = configure_chat(core)
        result = asyncio.run(core.handle_message("执行一个不存在的脚本", actor=direct_admin(), source="console"))
        self.assertIn("没有匹配到可执行脚本", result["reply"])
        self.assertEqual([], calls)

    def test_agent_allows_normal_chat_without_claiming_execution(self):
        core = AgentCore()
        core.llm = SimpleNamespace(chat=AsyncMock(return_value={"content": "你好，请告诉我需要查询或执行的业务。"}))
        _, calls = configure_chat(core)
        result = asyncio.run(core.handle_message("你好", actor=direct_admin(), source="console"))
        self.assertEqual("你好，请告诉我需要查询或执行的业务。", result["reply"])
        self.assertEqual([], calls)

    def test_agent_formats_real_tool_result_instead_of_llm_summary(self):
        core = AgentCore(direct_tool_runners={"track_waybill": Mock(return_value={"tracking_number": "R00014513348", "route_rows": []})})
        core.llm = SimpleNamespace(chat=AsyncMock(return_value={"content": "假的 LLM 总结：已经处理好了。"}))
        _, calls = configure_chat(core)
        result = asyncio.run(core.handle_message("R00014513348", actor=direct_admin(), source="console"))
        self.assertIn("R00014513348", result["reply"])
        self.assertNotIn("假的 LLM 总结", result["reply"])
        self.assertEqual("track_waybill", calls[0]["tool_name"])
        self.assertTrue(calls[0]["result"]["success"])
        core.llm.chat.assert_not_awaited()

    def test_agent_login_message_does_not_reach_llm(self):
        core = AgentCore()
        core.llm = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("login must not call LLM")))
        _, calls = configure_chat(core)
        result = asyncio.run(core.handle_message("登陆", actor=direct_admin(), source="console"))
        self.assertIn("业务账号页面", result["reply"])
        self.assertEqual([], calls)
        core.llm.chat.assert_not_awaited()

    def test_agent_queries_tracking_without_command_or_run(self):
        class _FakeRegistry:
            def get_capability(self, name):
                return ToolRegistry().get_capability(name)

            def validate_input(self, name, params):
                return ToolRegistry().validate_input(name, params)

        run_track = Mock(return_value={"tracking_number": "R00014513348", "route_rows": []})
        core = AgentCore(direct_tool_runners={"track_waybill": run_track})
        core.registry = _FakeRegistry()
        gateway = _configure_completed_control_plane(
            core,
            data={"tracking_number": "R00014513348", "route_rows": []},
        )

        result = asyncio.run(core.execute_tool("track_waybill", {"tracking_number": "R00014513348"}, actor=direct_admin(), source="console"))

        run_track.assert_called_once_with({"tracking_number": "R00014513348"})
        self.assertEqual([], gateway.commands)
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

    def test_agent_queries_price_without_command_or_run(self):
        class _FakeRegistry:
            def get_capability(self, name):
                return ToolRegistry().get_capability(name)

            def validate_input(self, name, params):
                return ToolRegistry().validate_input(name, params)

        run_price = Mock(return_value={"mode": "agent_tms_combined", "ronghui": {}, "yunda": {}})
        core = AgentCore(direct_tool_runners={"get_price": run_price})
        core.registry = _FakeRegistry()
        gateway = _configure_completed_control_plane(
            core,
            data={"mode": "agent_tms_combined", "ronghui": {}, "yunda": {}},
        )

        params = {"address": "河北省邢台市隆尧县莲子镇中学", "weight": 199, "volume": 2.727}
        result = asyncio.run(core.execute_tool("get_price", params, actor=direct_admin(), source="console"))

        run_price.assert_called_once_with(params)
        self.assertEqual([], gateway.commands)
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
                "detail": {
                    "status_text": params.get("status_text"),
                    "verify_status_text": params.get("verify_status_text"),
                    "task_number": "R7-TASK-1",
                    "observed_status": params.get("verify_status_text"),
                    "url": "https://r7.example/task?token=must-not-leak",
                    "diagnostic": "password=must-not-leak",
                },
                "ts": "2026-08-14T09:00:01+08:00",
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
        self.assertNotIn(
            "_scheduled_task",
            r7_arrival_checkin_tool._sanitize_for_log(
                {"_scheduled_task": {"id": "browser-controlled"}}
            ),
        )
        self.assertEqual("success", insert_log.call_args.kwargs["status"])
        self.assertEqual(0, insert_log.call_args.kwargs["success_count_before"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_after"])
        self.assertEqual({"0": True}, result["postconditions"])
        proof = result["postcondition_evidence"]["0"]
        self.assertTrue(proof["verified"])
        self.assertEqual(
            "third_party_r7_arrival_state_confirmed",
            proof["condition"],
        )
        self.assertEqual("R7-TASK-1", proof["details"]["external_task_id"])
        self.assertEqual("已到达", proof["details"]["observed_status"])
        self.assertNotIn("url", result["detail"])
        self.assertNotIn("must-not-leak", result["detail"]["diagnostic"])
        logged_result = insert_log.call_args.kwargs["result"]
        self.assertNotIn("url", logged_result["detail"])

    def test_r7_log_task_identity_uses_only_trusted_scheduler_side_channel(self):
        with patch(
            "tools.r7_arrival_checkin_tool.trusted_scheduler_context",
            return_value=None,
        ):
            self.assertEqual("", r7_arrival_checkin_tool._scheduled_task_id())

        with patch(
            "tools.r7_arrival_checkin_tool.trusted_scheduler_context",
            return_value={"task_id": "r7_arrival_checkin_0900"},
        ):
            self.assertEqual(
                "r7_arrival_checkin_0900",
                r7_arrival_checkin_tool._scheduled_task_id(),
            )

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
            patch(
                "tools.r7_arrival_checkin_tool._latest_success_observation",
                return_value={
                    "log_id": 42,
                    "task_number": "R7-TASK-1",
                    "observed_status": "已到达",
                    "verified_at": "2026-08-14T09:00:01+08:00",
                },
            ),
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
        self.assertEqual({"0": True}, result["postconditions"])
        self.assertEqual(
            {"external_task_id", "observed_status", "source_verified_at"},
            set(result["postcondition_evidence"]["0"]["details"]),
        )

    def test_r7_arrival_daily_limit_fails_closed_without_prior_exact_proof(self):
        with (
            patch(
                "tools.r7_arrival_checkin_tool.resolve_account_params",
                side_effect=_resolved_r7_test_params,
            ),
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=1),
            patch(
                "tools.r7_arrival_checkin_tool._latest_success_observation",
                return_value=None,
            ),
            patch("tools.r7_arrival_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once") as run_once,
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin(
                {"daily_success_limit": 1}
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIn("prior exact-task proof", result["error"])
        run_once.assert_not_called()
        self.assertFalse(insert_log.call_args.kwargs["ok"])

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
