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
                                  for name in ("track_waybill",)
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
        self.internal_token_patch.start()
        self.addCleanup(self.internal_token_patch.stop)

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

            def validate_arguments(self, name, params):
                return ToolRegistry().validate_arguments(name, params)

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

    def test_system_price_tool_is_retired_from_registry(self):
        self.assertIsNone(ToolRegistry().get_capability("get_price"))
        with self.assertRaises(KeyError):
            ToolRegistry().validate_arguments("get_price", {"address": "示例地址", "weight": 199, "volume": 2.727})

    def test_phase7_resource_import_has_no_n8n_dependency(self):
        target = Path(__file__).resolve().parents[1] / "agent" / "phase7_resource_import.py"
        text = target.read_text(encoding="utf-8")
        self.assertNotIn("n8n", text.lower())
        self.assertNotIn("sqlite", text.lower())

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
