"""Focused tests extracted from the former TMS runtime aggregate."""

from types import SimpleNamespace

from _tms_runtime_test_support import *  # noqa: F403
from agent.orchestration.models import Actor, ActorType


# Commands the current plugin packages declare for their fixed Feishu routes.
_DECLARED_FEISHU_COMMANDS = frozenset({"扫描", "自提到货问题件", "分批", "统计到货数据", "arrive-list"})


class _FakeAutomationProjectEntrypoints:
    def __init__(self) -> None:
        self.status = "COMPLETED"
        self.calls: list[dict[str, Any]] = []
        self.project_config = {
            "class_name": "route-one",
            "departure_time_fixed": "21:30:00",
            "plate_numbers": ["湘AK6980", "湘B12345"],
        }

    def describe_feishu_route(self, _route_key):
        return SimpleNamespace(
            project_config=dict(self.project_config),
            account_bindings={"account_id": "business-primary"},
        )

    async def dispatch(self, **kwargs):
        """Stand in for the Service V2 dispatcher: only declared commands match."""
        if kwargs.get("command_text") not in _DECLARED_FEISHU_COMMANDS:
            return None
        self.calls.append(dict(kwargs))
        return {
            "success": self.status == "COMPLETED",
            "status": self.status,
            "run_id": "run-feishu",
        }


class ToolFeishuFlowTests(unittest.TestCase):
    def setUp(self):
        self.project_entrypoints = _FakeAutomationProjectEntrypoints()
        self.project_entrypoints_patch = patch.object(
            message_handler,
            "_AUTOMATION_PROJECT_ENTRYPOINTS",
            self.project_entrypoints,
        )
        self.project_entrypoints_patch.start()
        self.addCleanup(self.project_entrypoints_patch.stop)
        self.dispatcher_patch = patch.object(
            message_handler,
            "_SERVICE_V2_FEISHU_DISPATCHER",
            self.project_entrypoints,
        )
        self.dispatcher_patch.start()
        self.addCleanup(self.dispatcher_patch.stop)
        self.command_context_token = message_handler._COMMAND_CONTEXT.set(
            message_handler.FeishuCommandContext(
                event_id="legacy-flow-event",
                actor_id="user-1",
                chat_id="chat-1",
            )
        )
        self.addCleanup(
            message_handler._COMMAND_CONTEXT.reset,
            self.command_context_token,
        )
        self.internal_token_patch = patch.dict(
            os.environ,
            {"AGENT_INTERNAL_API_TOKEN": "test-internal-token"},
            clear=False,
        )
        self.internal_token_patch.start()
        self.addCleanup(self.internal_token_patch.stop)

    def test_direct_router_does_not_map_scan_query_to_scan_sync_tool(self):
        request = direct_tool_router.direct_tool_request_from_text("查扫描记录")

        self.assertIsNone(request)

    def test_fixed_feishu_route_fails_closed_when_v2_projection_is_missing(self):
        replies: list[tuple[str, str]] = []

        class _FakeAgent:
            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("a fixed route must not fall back to a core tool")

            async def handle_message(self, *_args, **_kwargs):
                raise AssertionError("a fixed route must not fall back to the LLM")

        class _MissingTargetDispatcher:
            @staticmethod
            async def dispatch(**_kwargs):
                return None

        async def _fake_reply_text(
            _chat_id,
            text,
            receive_id_type="chat_id",
            *,
            reply_type="text",
        ):
            del receive_id_type
            replies.append((text, reply_type))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch.object(
                message_handler,
                "_SERVICE_V2_FEISHU_DISPATCHER",
                _MissingTargetDispatcher(),
            ),
            patch(
                "feishu.message_handler._reply_text",
                side_effect=_fake_reply_text,
            ),
        ):
            asyncio.run(
                message_handler._process_and_reply(
                    "统计到货数据",
                    "user-1",
                    "chat-1",
                )
            )

        self.assertEqual([], self.project_entrypoints.calls)
        self.assertEqual("service_v2_feishu_failed", replies[-1][1])

    def test_feishu_finance_text_resolves_bound_admin_before_agent_core(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _ApprovalRuntime:
            def handle_binding_text(self, *_args):
                return None

            def resolve_actor(self, open_id):
                calls["resolved_open_id"] = open_id
                return Actor(
                    ActorType.FEISHU_USER,
                    open_id,
                    roles=("admin", "super_admin"),
                    authenticated_by="feishu_admin_binding",
                )

        class _FakeAgent:
            async def handle_message(self, **kwargs):
                calls["handle_message"] = kwargs
                return {"reply": "财务查询已由 AgentCore 处理"}

        async def _fake_reply_text(
            _chat_id,
            text,
            receive_id_type="chat_id",
            *,
            reply_type="text",
        ):
            del receive_id_type, reply_type
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", _ApprovalRuntime()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(
                message_handler._process_and_reply(
                    "今天财务收入",
                    "bound-open-id",
                    "chat-1",
                )
            )

        self.assertEqual(calls["resolved_open_id"], "bound-open-id")
        submitted = calls["handle_message"]
        self.assertEqual(submitted["source"], "feishu")
        self.assertEqual(submitted["request_id"], "legacy-flow-event")
        self.assertEqual(submitted["actor"].authenticated_by, "feishu_admin_binding")
        self.assertIn("财务查询已由 AgentCore 处理", replies[-1])

    def test_feishu_menu_scan_action_runs_scan_sync_tool(self):
        for event_key in ("扫描", "scan", "sync_scan_codes"):
            with self.subTest(event_key=event_key):
                request = message_handler._menu_action_from_key(event_key)

                self.assertIsNotNone(request)
                self.assertEqual("sync_scan_codes", request["tool_name"])
                self.assertEqual({}, request["params"])

    def test_feishu_scan_message_bypasses_llm_and_executes_scan_tool(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {
                        "fetched": 1,
                        "normalized": 1,
                        "child_items": 1,
                        "batches": 1,
                        "batch_results": [{"batch": 1, "items": 1, "ok": True, "raw": {"ok": True}}],
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("扫描 should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("扫描", "user-1", "chat-1"))

        self.assertNotIn("execute_tool", calls)
        self.assertEqual("扫描", self.project_entrypoints.calls[-1]["command_text"])
        self.assertIsNone(self.project_entrypoints.calls[-1]["preview_invocation_id"])
        self.assertTrue(replies)

    def test_feishu_invalid_tracking_number_replies_without_tool_execution(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                calls["execute_tool"] = (tool_name, params)
                raise AssertionError("invalid tracking number should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("invalid tracking number should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("R000016211453", "user-1", "chat-1"))

        self.assertEqual({}, calls)
        self.assertEqual(
            ["单号查询失败：单号格式错误：R 开头融辉单号应为 R+11位主单或 R+15位子单，请检查是否多输/少输数字。"],
            replies,
        )

    def test_feishu_tracking_number_replies_with_query_ack_before_result(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {
                        "type": "ronghui",
                        "tracking_number": params["tracking_number"],
                        "route_rows": [],
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("tracking number should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("R00014513348", "user-1", "chat-1"))

        self.assertEqual(
            ("track_waybill", {"tracking_number": "R00014513348", "provider": "ronghui"}),
            calls["execute_tool"],
        )
        self.assertEqual("正在查询单号：R00014513348", replies[0])
        self.assertIn("查询单号：R00014513348", replies[-1])

    def test_feishu_arrive_list_message_bypasses_llm_and_executes_tool(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {
                        "fetched": 2,
                        "bill_codes": 2,
                        "detail_records": 2,
                        "mysql_result": {"ok": True, "replaced": 2},
                        "primary_result": {"ok": True, "rows": 2},
                        "secondary_result": {"ok": True, "rows": 2},
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("arrivelist should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("arrive-list", "user-1", "chat-1"))

        self.assertNotIn("execute_tool", calls)
        self.assertEqual("arrive-list", self.project_entrypoints.calls[-1]["command_text"])
        self.assertNotIn("runtime_inputs", self.project_entrypoints.calls[-1])
        self.assertTrue(replies)

    def _assert_quote_auth_failure_stays_in_plugin(self, error_code):
        calls, replies, admin_calls, pending_calls = [], [], [], []
        message = "报价账号登录态不可用，请在账号管理恢复后重新查询。"

        class FakeAgent:
            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("quote must not call the retired get_price tool")

            async def handle_message(self, *_args, **_kwargs):
                raise AssertionError("explicit quote must not reach the LLM")

        async def dispatch(**kwargs):
            calls.append(kwargs)
            return {
                "success": False, "status": "FAILED", "invocation_id": "quote-auth-test",
                "error_code": error_code, "error_summary": message,
                "error": {"code": error_code, "message": message},
                "result": {
                    "status": "FAILED",
                    "data": {
                        provider: {"status": "failed", "quotes": [], "error": message}
                        for provider in ("ronghui", "yunda", "best")
                    },
                },
            }

        async def reply(_chat_id, text, **_kwargs):
            replies.append(text)

        async def post_admin(path, body=None):
            admin_calls.append((path, body))
            raise AssertionError("plugin auth failure must not start a legacy login")

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", SimpleNamespace(dispatch=dispatch)),
            patch.object(message_handler, "get_pending", return_value=None),
            patch.object(message_handler, "set_pending", side_effect=lambda *args, **kwargs: pending_calls.append((args, kwargs))),
            patch.object(message_handler, "_post_admin", side_effect=post_admin),
            patch.object(message_handler, "_reply_text", side_effect=reply),
        ):
            asyncio.run(message_handler._process_and_reply("示例市测试路1号，800，5", "user-1", "chat-1"))

        self.assertEqual(1, len(calls))
        self.assertEqual("三平台报价", calls[0]["command_text"])
        self.assertEqual("legacy-flow-event", calls[0]["event_id"])
        self.assertEqual("user-1", calls[0]["sender_id"])
        self.assertEqual({"address": "示例市测试路1号", "weight": 800.0, "volume": 5.0}, calls[0]["runtime_inputs"])
        self.assertEqual([], admin_calls)
        self.assertEqual([], pending_calls)
        self.assertEqual(3, len(replies))
        self.assertTrue(all(message in text for text in replies))

    def test_feishu_quote_auth_pending_does_not_start_legacy_sms_resume(self):
        self._assert_quote_auth_failure_stays_in_plugin("AUTH_PENDING_CODE")

    def test_feishu_quote_auth_required_does_not_guess_a_legacy_account(self):
        self._assert_quote_auth_failure_stays_in_plugin("AUTH_REQUIRED")

    def test_feishu_project_blocked_login_does_not_start_legacy_sms_flow(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        self.project_entrypoints.status = "BLOCKED_LOGIN"

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": False,
                    "error": "工具执行失败(exit 1): ValueError: get_scan 返回格式异常: {'ok': False, 'error_code': 'AUTH_PENDING_CODE', 'error': '短信验证码已发送，等待人工提交验证码。'}",
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("统计 should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._get_admin", return_value={"ok": True, "status": "expired", "authenticated": False}),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("统计", "user-1", "chat-1"))

        self.assertNotIn("execute_tool", calls)
        self.assertEqual([], admin_calls)
        self.assertEqual([], pending_calls)
        self.assertEqual(
            "统计到货数据",
            self.project_entrypoints.calls[-1]["command_text"],
        )
        self.assertTrue(replies)

    def test_feishu_deferred_tool_does_not_blind_retry_stale_auth(self):
        calls: list[tuple[str, dict[str, Any]]] = []
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        self.project_entrypoints.status = "BLOCKED_LOGIN"

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                calls.append((tool_name, params))
                if len(calls) == 1:
                    return {"success": False, "error_code": "AUTH_REQUIRED", "error": "AUTH_REQUIRED"}
                return {
                    "success": True,
                    "data": {
                        "main_trackings": 1,
                        "records": 1,
                        "count_result": {"arrived_nonzero": 1},
                        "primary_result": {"ok": True},
                        "secondary_result": {"ok": True},
                        "pending_result": {"skipped": True, "reason": "missing_resource"},
                        "archive_result": {"ok": True},
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("stats should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch(
                "feishu.message_handler.direct_tool_request_from_text",
                return_value={
                    "tool_name": "sync_arrival_stats",
                    "params": {},
                    "mode": "automation_project",
                    "automation_route_key": "builtin.arrival_stats",
                    "dynamic_inputs": {},
                },
            ),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._get_admin", return_value={"ok": True, "status": "authenticated", "authenticated": True}),
            patch("feishu.message_handler._post_admin", side_effect=AssertionError("stale auth retry should not send code")),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("统计", "user-1", "chat-1"))

        self.assertEqual([], calls)
        self.assertEqual([], pending_calls)
        self.assertEqual(1, len(self.project_entrypoints.calls))
        self.assertTrue(replies)

    def test_feishu_direct_tool_clears_stale_login_pending(self):
        calls: list[tuple[str, dict[str, Any]]] = []
        replies: list[str] = []
        pending = {
            "type": "confirm_login_for_resume",
            "auth_session": "default",
            "resume_tool": "sync_arrival_stats",
            "resume_params": {},
        }

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                calls.append((tool_name, params))
                return {
                    "success": True,
                    "data": {
                        "main_trackings": 1,
                        "records": 1,
                        "count_result": {"arrived_nonzero": 1},
                        "primary_result": {"ok": True},
                        "secondary_result": {"ok": True},
                        "pending_result": {"skipped": True, "reason": "missing_resource"},
                        "archive_result": {"ok": True},
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("direct request should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch(
                "feishu.message_handler.direct_tool_request_from_text",
                return_value={
                    "tool_name": "sync_arrival_stats",
                    "params": {},
                    "mode": "automation_project",
                    "automation_route_key": "builtin.arrival_stats",
                    "dynamic_inputs": {},
                },
            ),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("统计", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual([], calls)
        self.assertEqual(1, len(self.project_entrypoints.calls))
        self.assertEqual(
            "统计到货数据",
            self.project_entrypoints.calls[-1]["command_text"],
        )
        self.assertTrue(replies)

    def test_feishu_llm_selected_tool_auth_required_uses_login_resume_flow(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("semantic request should be handled by handle_message in this test")

            async def handle_message(self, *args, **kwargs):
                return {
                    "reply": "到货清单同步失败：当前未登录或登录态已过期。",
                    "executed_tools": [
                        {
                            "tool_name": "sync_arrive_list",
                            "params": {},
                            "result": {
                                "success": False,
                                "error": "工具执行失败(exit 1): AUTH_REQUIRED 当前未登录或登录态已过期。",
                            },
                        }
                    ],
                }

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._get_admin", return_value={"ok": True, "status": "expired", "authenticated": False}),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("帮我处理今天预到达数据", "user-1", "chat-1"))

        self.assertEqual(1, len(pending_calls))
        self.assertEqual("confirm_login_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("sync_arrive_list", pending_calls[0][1]["resume_tool"])
        self.assertIn("登录过期需要重新登录", replies[-1])
        self.assertNotIn("到货清单同步失败", replies[-1])

    def test_pending_actions_persist_across_memory_clear(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = os.path.join(tmp_dir, "pending_actions.json")
            with patch.dict(os.environ, {"AGENT_PENDING_STATE_FILE": state_file}):
                pending_actions._pending.clear()
                pending_actions.set_pending(
                    "chat-1",
                    {
                        "type": "confirm_login_for_resume",
                        "auth_session": "default",
                        "resume_tool": "sync_arrive_list",
                        "resume_params": {},
                    },
                    ttl_sec=60,
                )

                pending_actions._pending.clear()
                restored = pending_actions.get_pending("chat-1")
                self.assertIsNotNone(restored)
                self.assertEqual("confirm_login_for_resume", restored["type"])
                self.assertEqual("sync_arrive_list", restored["resume_tool"])

                pending_actions.clear_pending("chat-1")
                pending_actions._pending.clear()
                self.assertIsNone(pending_actions.get_pending("chat-1"))

    def test_volatile_pending_is_available_in_process_but_not_restored(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = os.path.join(tmp_dir, "pending_actions.json")
            with patch.dict(os.environ, {"AGENT_PENDING_STATE_FILE": state_file}):
                pending_actions._pending.clear()
                pending_actions._volatile_pending.clear()
                pending_actions.set_pending(
                    "chat-scan",
                    {"type": "existing_persistent_action"},
                    ttl_sec=60,
                )
                pending_actions.set_pending(
                    "chat-scan",
                    {
                        "type": "scan_preview_confirmation",
                        "preview_run_id": "11111111-1111-4111-8111-111111111111",
                    },
                    ttl_sec=60,
                    persist=False,
                )

                self.assertEqual(
                    "scan_preview_confirmation",
                    pending_actions.get_pending("chat-scan")["type"],
                )
                pending_actions.clear_pending("chat-scan", volatile_only=True)
                self.assertEqual(
                    "existing_persistent_action",
                    pending_actions.get_pending("chat-scan")["type"],
                )
                pending_actions.set_pending(
                    "chat-scan",
                    {
                        "type": "scan_preview_confirmation",
                        "preview_run_id": "11111111-1111-4111-8111-111111111111",
                    },
                    ttl_sec=60,
                    persist=False,
                )
                pending_actions._volatile_pending.clear()
                self.assertEqual(
                    "existing_persistent_action",
                    pending_actions.get_pending("chat-scan")["type"],
                )
                self.assertIn("chat-scan", pending_actions._pending)

    def test_feishu_persisted_login_confirm_sends_code_without_llm(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login confirmation should send code before executing tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("persisted login confirmation should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = os.path.join(tmp_dir, "pending_actions.json")
            with patch.dict(os.environ, {"AGENT_PENDING_STATE_FILE": state_file}):
                pending_actions._pending.clear()
                pending_actions.set_pending(
                    "chat-1",
                    {
                        "type": "confirm_login_for_resume",
                        "auth_session": "default",
                        "resume_tool": "sync_arrive_list",
                        "resume_params": {},
                    },
                    ttl_sec=600,
                )
                pending_actions._pending.clear()

                with (
                    patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
                    patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
                    patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
                ):
                    asyncio.run(message_handler._process_and_reply("是", "user-1", "chat-1"))

                self.assertEqual([("/admin/tms/session/send-code", None)], admin_calls)
                self.assertIn("正在自动识别图片验证码并登录（操作场账号）", replies[0])
                self.assertIn("验证码已发送", replies[-1])
                restored = pending_actions.get_pending("chat-1")
                self.assertEqual("waiting_code_for_resume", restored["type"])
                self.assertEqual("sync_arrive_list", restored["resume_tool"])

    def test_feishu_confirm_without_pending_does_not_execute_tool(self):
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("confirm text without pending must not execute a tool")

            async def handle_message(self, *args, **kwargs):
                return {"reply": message_handler.UNKNOWN_EXECUTION_REPLY, "executed_tools": []}

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("是", "user-1", "chat-1"))

        self.assertEqual([message_handler.UNKNOWN_EXECUTION_REPLY], replies)

    def test_feishu_ws_mysql_lease_blocks_duplicate_consumer(self):
        class _FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args):
                return None

            def fetchone(self):
                return (0,)

        class _FakeConn:
            def cursor(self):
                return _FakeCursor()

            def close(self):
                return None

        with patch("feishu.bot._db_connect_for_lease", return_value=_FakeConn()):
            feishu_bot._lease_conn = None
            self.assertFalse(feishu_bot._acquire_ws_lease())

    def test_feishu_login_message_asks_account_choice(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login command should call admin send-code, not a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler._get_admin", return_value=_admin_accounts_payload()),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("登陆", "user-1", "chat-1"))

        self.assertEqual([], admin_calls)
        self.assertEqual(1, len(pending_calls))
        self.assertEqual("login_account_choice", pending_calls[0][1]["type"])
        self.assertIn("1. TMS融辉默认账号 (ronghui_default) · TMS融辉", replies[-1])
        self.assertIn("2. 大祥报价账号 (price_default) · TMS融辉 / 大祥报价", replies[-1])

    def test_feishu_operator_login_message_sends_default_tms_code(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("operator login command should call admin send-code, not a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("operator login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("操作场登陆", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/session/send-code", None)], admin_calls)
        self.assertEqual(1, len(pending_calls))
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("default", pending_calls[0][1]["auth_session"])
        self.assertIsNone(pending_calls[0][1]["resume_tool"])
        self.assertIn("正在自动识别图片验证码并登录（操作场账号）", replies[0])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_price_login_message_sends_price_code(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("price login command should call price send-code, not a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("price login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("报价发验证码", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/price-session/send-code", None)], admin_calls)
        self.assertEqual("price", pending_calls[0][1]["auth_session"])
        self.assertIsNone(pending_calls[0][1]["resume_tool"])
        self.assertIn("正在自动识别图片验证码并登录（大祥账号）", replies[0])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_login_account_choice_sends_selected_code(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {"type": "login_account_choice"}

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login account choice should call admin send-code, not a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login account choice should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("1", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual([("/admin/tms/price-session/send-code", None)], admin_calls)
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("price", pending_calls[0][1]["auth_session"])
        self.assertIn("正在自动识别图片验证码并登录（大祥账号）", replies[0])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_login_message_overrides_non_login_pending(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {
            "type": "r7_departure_plate_choice",
            "tool_name": "r7_departure_checkin",
            "params": {"class_name": "邵阳操作场-长沙"},
            "plate_numbers": ["湘AK6980"],
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login command should override R7 pending and not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._get_admin", return_value=_admin_accounts_payload()),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("登陆", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual("login_account_choice", pending_calls[0][1]["type"])
        self.assertIn("1. TMS融辉默认账号 (ronghui_default) · TMS融辉", replies[-1])

    def test_feishu_login_message_overrides_resume_pending_without_executing_old_task(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {
            "type": "confirm_login_for_resume",
            "auth_session": "default",
            "resume_tool": "r7_arrival_checkin",
            "resume_params": {"_feishu": True},
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("generic login command should not resume the old R7 task")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(*args, **kwargs):
            raise AssertionError("generic login command should ask account choice before sending code")

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._get_admin", return_value=_admin_accounts_payload()),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("登陆", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual("login_account_choice", pending_calls[0][1]["type"])
        self.assertIn("1. TMS融辉默认账号 (ronghui_default) · TMS融辉", replies[-1])

    def test_feishu_explicit_login_overrides_resume_pending_without_resume_tool(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {
            "type": "confirm_login_for_resume",
            "auth_session": "default",
            "resume_tool": "r7_arrival_checkin",
            "resume_params": {"_feishu": True},
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("explicit login command should not resume the old R7 task")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("操作场登录", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual([("/admin/tms/session/send-code", None)], admin_calls)
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("default", pending_calls[0][1]["auth_session"])
        self.assertIsNone(pending_calls[0][1]["resume_tool"])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_standalone_sms_code_logs_in_without_resume(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending = {
            "type": "waiting_code_for_resume",
            "auth_session": "default",
            "resume_tool": None,
            "resume_params": {},
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("standalone login should not resume a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("sms code should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "authenticated"}

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("123456", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/session/submit-code", {"code": "123456"})], admin_calls)
        clear_pending.assert_called_once_with("chat-1")
        self.assertIn("正在校验验证码", replies[0])
        self.assertEqual("登录成功", replies[-1])

    def test_feishu_sms_code_without_pending_uses_broker_pending_code_state(self):
        replies: list[str] = []
        get_calls: list[str] = []
        post_calls: list[tuple[str, dict[str, Any] | None]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("standalone sms code should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("standalone sms code should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_get_admin(path):
            get_calls.append(path)
            if path == "/admin/tms/session/status":
                return {"ok": True, "status": "pending_code", "pending_code": True}
            return {"ok": True, "status": "logged_out", "pending_code": False}

        async def _fake_post_admin(path, body=None):
            post_calls.append((path, body))
            return {"ok": True, "status": "authenticated"}

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._get_admin", side_effect=_fake_get_admin),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("123456", "user-1", "chat-1"))

        self.assertEqual(["/admin/accounts", "/admin/tms/session/status"], get_calls)
        self.assertEqual([("/admin/tms/session/submit-code", {"code": "123456"})], post_calls)
        self.assertIn("正在校验验证码（操作场账号）", replies[0])
        self.assertEqual("登录成功", replies[-1])

    def test_feishu_price_login_confirmation_uses_price_session_endpoint(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {
            "type": "confirm_login_for_resume",
            "auth_session": "price",
            "resume_tool": "get_price",
            "resume_params": {"address": "长沙", "weight": 800.0},
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("confirmation should send code before executing tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("confirmation should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("是", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/price-session/send-code", None)], admin_calls)
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("price", pending_calls[0][1]["auth_session"])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_price_sms_code_does_not_resume_previous_call(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        execute_calls: list[tuple[str, dict[str, Any]]] = []
        pending = {
            "type": "waiting_code_for_resume",
            "auth_session": "price",
            "resume_tool": "get_price",
            "resume_params": {"address": "长沙", "weight": 800.0},
        }

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                execute_calls.append((tool_name, params))
                return {"success": True, "data": {"目的网点": "测试站"}}

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("sms code should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "authenticated"}

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("123456", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/price-session/submit-code", {"code": "123456"})], admin_calls)
        self.assertEqual([], execute_calls)
        self.assertIn("登录成功", replies[-1])
        self.assertIn("先前调用已经结束", replies[-1])

    def test_removed_r7_departure_message_is_not_routed_to_automation(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []
        self.project_entrypoints.project_config["plate_numbers"] = ["湘AK6980"]

        class _FakeMemory:
            def list_scheduled_tasks(self):
                return [
                    {
                        "id": "r7_departure_checkin",
                        "tool_name": "r7_departure_checkin",
                        "tool_params": {
                            "class_name": "邵阳操作场-长沙",
                            "departure_time_fixed": "21:30:00",
                            "plate_numbers": "湘AK6980",
                        },
                    }
                ]

        class _FakeAgent:
            memory = _FakeMemory()

            async def execute_tool(self, tool_name, params, **kwargs):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {
                        "ok": True,
                        "detail": {
                            "class_name": params.get("class_name"),
                            "departure_time": "2026-04-29 21:30:00",
                            "plate_numbers": params.get("plate_numbers"),
                        },
                    },
                }

            async def handle_message(self, *args, **kwargs):
                calls["handle_message"] = kwargs.get("message")
                return {"reply": "未匹配到现有自动化。"}

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("发车", "user-1", "chat-1"))

        self.assertNotIn("execute_tool", calls)
        self.assertEqual("发车", calls["handle_message"])
        self.assertEqual([], self.project_entrypoints.calls)
        self.assertEqual("未匹配到现有自动化。", replies[-1])

    def test_removed_r7_departure_message_does_not_create_plate_pending(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeMemory:
            def list_scheduled_tasks(self):
                return [
                    {
                        "id": "r7_departure_checkin",
                        "tool_name": "r7_departure_checkin",
                        "tool_params": {
                            "class_name": "邵阳操作场-长沙",
                            "departure_time_fixed": "21:30:00",
                            "plate_numbers": "湘AK6980,湘B12345",
                        },
                    }
                ]

        calls: dict[str, Any] = {}

        class _FakeAgent:
            memory = _FakeMemory()

            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("multi-plate 发车 should wait for user choice")

            async def handle_message(self, *args, **kwargs):
                calls["handle_message"] = kwargs.get("message")
                return {"reply": "未匹配到现有自动化。"}

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("发车", "user-1", "chat-1"))

        self.assertEqual([], pending_calls)
        self.assertEqual("发车", calls["handle_message"])
        self.assertEqual([], self.project_entrypoints.calls)
        self.assertEqual("未匹配到现有自动化。", replies[-1])

    def test_removed_r7_departure_pending_is_cleared_without_execution(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []
        pending = {
            "type": "r7_departure_plate_choice",
            "tool_name": "r7_departure_checkin",
            "automation_route_key": "builtin.r7_departure_checkin",
            "plate_numbers": ["湘AK6980", "湘B12345"],
        }

        class _FakeAgent:
            async def execute_tool(self, tool_name, params, **kwargs):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {"ok": True, "detail": {"plate_numbers": params.get("plate_numbers")}},
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("pending choice should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("湘B12345", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertNotIn("execute_tool", calls)
        self.assertEqual([], self.project_entrypoints.calls)
        self.assertIn("已移除", replies[-1])

    def test_removed_r7_departure_pending_clears_numeric_reply_too(self):
        replies: list[str] = []
        pending = {
            "type": "r7_departure_plate_choice",
            "tool_name": "r7_departure_checkin",
            "automation_route_key": "builtin.r7_departure_checkin",
            "plate_numbers": ["湘AK6980", "湘B12345"],
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("bare numeric choice must not execute R7 departure")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("pending choice should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("2", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertIn("已移除", replies[-1])

    def test_scan_sync_reply_never_labels_failed_batches_as_complete(self):
        reply = direct_tool_router.format_tool_reply(
            "sync_scan_codes",
            {
                "success": True,
                "data": {
                    "fetched": 10,
                    "normalized": 9,
                    "child_items": 8,
                    "batches": 2,
                    "scan_index_result": {"replaced": 9},
                    "batch_results": [
                        {"batch": 1, "items": 5, "ok": True, "raw": {"ok": True}},
                        {"batch": 2, "items": 3, "ok": False, "raw": {"error": "scan_next failed"}},
                    ],
                    "skipped_signed_count": 1,
                    "flow_result": {"skipped": True},
                },
            },
        )

        self.assertIn("扫描任务失败", reply)
        self.assertNotIn("扫描任务已完成", reply)
        self.assertIn("第 2 批", reply)
        self.assertIn("scan_next failed", reply)

    def test_scan_sync_reply_uses_nested_scan_next_error(self):
        reply = direct_tool_router.format_tool_reply(
            "sync_scan_codes",
            {
                "success": True,
                "data": {
                    "fetched": 1,
                    "normalized": 1,
                    "child_items": 1,
                    "batches": 1,
                    "batch_results": [
                        {
                            "batch": 1,
                            "items": 1,
                            "ok": False,
                            "raw": {"ok": False, "data": {"message": "select_station timeout"}},
                        }
                    ],
                },
            },
        )

        self.assertIn("select_station timeout", reply)

    def test_arrive_list_sync_reply_summarizes_real_counts(self):
        reply = direct_tool_router.format_tool_reply(
            "sync_arrive_list",
            {
                "success": True,
                "data": {
                    "fetched": 5,
                    "bill_codes": 4,
                    "skipped_receipt_like": 2,
                    "detail_records": 4,
                    "mysql_result": {"ok": True, "replaced": 4},
                    "primary_result": {"ok": True, "rows": 4},
                    "secondary_result": {"ok": True, "rows": 4},
                },
            },
        )

        self.assertIn("到货清单同步已完成", reply)
        self.assertIn("派件预报：5", reply)
        self.assertIn("主单数：4", reply)
        self.assertIn("跳过回单号：2", reply)
        self.assertIn("MySQL：覆盖 4", reply)
        self.assertIn("主飞书表：写入 4 行", reply)
