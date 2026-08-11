import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from agent.direct_tool_router import format_price_reply, format_price_reply_messages, parse_price_request
from feishu import message_handler, notify
from feishu.reply_formatter import format_reply
import main
from main import _check_tms_account_session, _feishu_event_type, _tms_session_alert_key, _verify_feishu_event_token
from tools.price_tool import get_price_via_script


def _admin_accounts_payload(*, pending_accounts: set[str] | None = None) -> dict:
    pending_accounts = pending_accounts or set()
    accounts = [
        ("ronghui_default", "TMS融辉默认账号", "ronghui", "TMS融辉", "default", True),
        ("price_default", "大祥报价账号", "price", "大祥报价", "price", True),
        ("yunda_default", "韵达默认账号", "yunda", "韵达", "yunda", True),
    ]
    return {
        "ok": True,
        "accounts": [
            {
                "account_id": account_id,
                "name": name,
                "system": system,
                "system_label": system_label,
                "session_profile": session_profile,
                "session_capable": True,
                "is_active": True,
                "is_default": is_default,
                "status": {
                    "status": "pending_code" if account_id in pending_accounts else "logged_out",
                    "pending_code": account_id in pending_accounts,
                },
            }
            for account_id, name, system, system_label, session_profile, is_default in accounts
        ],
    }


class FeishuMessageHandlerTests(unittest.TestCase):
    def test_price_request_without_volume_uses_weight_only(self):
        request = parse_price_request("武汉市黄陂区横店街天阳路1号惠强科技园三号楼5层巴思川食品有限公司，1055")
        self.assertEqual(
            request,
            {
                "address": "武汉市黄陂区横店街天阳路1号惠强科技园三号楼5层巴思川食品有限公司",
                "weight": 1055.0,
            },
        )

    def test_price_request_with_volume_supports_prefix(self):
        request = parse_price_request(
            "报价：武汉市黄陂区横店街天阳路1号惠强科技园三号楼5层巴思川食品有限公司，1055，0.3"
        )
        self.assertEqual(
            request,
            {
                "address": "武汉市黄陂区横店街天阳路1号惠强科技园三号楼5层巴思川食品有限公司",
                "weight": 1055.0,
                "volume": 0.3,
            },
        )

    def test_price_request_with_plain_volume_without_prefix(self):
        request = parse_price_request(
            "四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司，1000，0.35"
        )
        self.assertEqual(
            request,
            {
                "address": "四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
                "weight": 1000.0,
                "volume": 0.35,
            },
        )

    def test_price_request_with_volume_expression_keeps_three_decimals(self):
        request = parse_price_request(
            "贵州省贵阳市白云区蓬家寨街道杨柳街柳雅居小区兔喜快递超市，217，125*112*82*1"
        )
        self.assertEqual(
            request,
            {
                "address": "贵州省贵阳市白云区蓬家寨街道杨柳街柳雅居小区兔喜快递超市",
                "weight": 217.0,
                "volume": 1.148,
            },
        )

    def test_price_request_with_volume_expression(self):
        request = parse_price_request(
            "四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司，1000，30*23*103*1+97*23*31*4"
        )
        self.assertEqual(
            request,
            {
                "address": "四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
                "weight": 1000.0,
                "volume": 0.348,
            },
        )

    def test_price_request_rejects_extra_declared_value_field(self):
        request = parse_price_request(
            "云南省曲靖市麒麟区麒麟南路186号，100，0.1，2000"
        )
        self.assertIsNone(request)

    def test_price_request_rejects_invalid_volume_expression(self):
        request = parse_price_request(
            "四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司，1000，30*23*103"
        )
        self.assertIsNone(request)

    def test_reply_formatter_strips_common_markdown(self):
        rendered = format_reply("**建议：**\n- 第一项\n- 第二项\n`code`")
        self.assertEqual(rendered, "建议：\n- 第一项\n- 第二项\ncode")

    def test_price_reply_uses_stable_field_order(self):
        rendered = format_price_reply({
            "success": True,
            "data": {
                "目的网点": "武汉融信站",
                "融安达": "327.4元",
                "精准零担": "273.92元",
                "融速达": "344.5元",
                "融安达(派送)": "380.15元",
                "精准零担(派送)": "326.67元",
                "融速达(派送)": "397.25元",
                "经理电话": "15717138101",
                "查询电话": "15717138101",
                "门店地址": "武汉市东西湖区走马岭走新路601号",
            },
        })
        self.assertEqual(
            rendered,
            "\n".join([
                "目的网点：武汉融信站",
                "精准零担：273.92元",
                "融速达：344.5元",
                "融安达：327.4元",
                "精准零担(派送)：326.67元",
                "融速达(派送)：397.25元",
                "融安达(派送)：380.15元",
                "查询电话：15717138101",
                "经理电话：15717138101",
                "门店地址：武汉市东西湖区走马岭走新路601号",
            ]),
        )

    def test_combined_price_reply_splits_ronghui_and_yunda(self):
        messages = format_price_reply_messages({
            "success": True,
            "data": {
                "ronghui": {
                    "目的网点": "武汉融信站",
                    "精准零担": "273.92元",
                },
                "yunda": {
                    "韵达自提": "120.00元",
                    "韵达派送": "138.50元",
                },
            },
        })
        self.assertEqual(
            messages,
            [
                "融辉价格\n目的网点：武汉融信站\n精准零担：273.92元",
                "韵达价格\n韵达自提：120.00元\n韵达派送：138.50元",
            ],
        )

    def test_combined_price_reply_formats_yunda_details(self):
        messages = format_price_reply_messages({
            "success": True,
            "data": {
                "ronghui": {"目的网点": "武汉融信站", "精准零担": "273.92元"},
                "yunda": {
                    "目的网点": "贵州毕节赫章县公司",
                    "韵达自提": "573.70元",
                    "韵达派送": "765.13元",
                    "是否派送": "镇上自提*",
                    "查询电话": "08578147200",
                    "经理电话": "18785574344",
                    "门店地址": "贵州省毕节市赫章县汉阳街道卸旗社区育才路",
                    "派送范围": "无",
                    "特殊区域": "无",
                    "特殊区域加收": "加收30元/票",
                    "特殊区域提醒": "该地址涉及特殊区域【后海塘工业区】【加收30元/票】，请核实！",
                    "线路": "邵阳-长沙-贵阳",
                    "到件时效": "一频次:2D2359",
                    "韵达明细": {
                        "自提": {"费用明细": {"特惠一口价": "50.00元", "合计": "205.70元"}},
                        "派送": {"费用明细": {"特惠一口价": "50.00元", "合计": "178.74元"}},
                    },
                },
            },
        })

        self.assertTrue(messages[0].startswith("融辉价格\n"))
        self.assertTrue(messages[1].startswith("韵达价格\n"))
        self.assertIn("目的网点：贵州毕节赫章县公司", messages[1])
        self.assertIn("韵达派送：765.13元", messages[1])
        self.assertIn("查询电话：08578147200", messages[1])
        self.assertIn("门店地址：贵州省毕节市赫章县汉阳街道卸旗社区育才路", messages[1])
        self.assertIn("特殊区域加收：加收30元/票", messages[1])
        self.assertIn("特殊区域提醒：该地址涉及特殊区域【后海塘工业区】【加收30元/票】，请核实！", messages[1])
        self.assertNotIn("线路：", messages[1])
        self.assertNotIn("到件时效：", messages[1])
        self.assertNotIn("费用明细(自提)：", messages[1])
        self.assertNotIn("费用明细(派送)：", messages[1])

    def test_combined_price_reply_marks_failed_yunda_unreachable(self):
        messages = format_price_reply_messages({
            "success": True,
            "data": {
                "ronghui": {"目的网点": "武汉融信站", "精准零担": "273.92元"},
                "yunda": {"provider": "韵达", "unavailable": True, "error": "网点不可达"},
            },
        })

        self.assertEqual(
            messages,
            [
                "融辉价格\n目的网点：武汉融信站\n精准零担：273.92元",
                "韵达价格\n韵达不可到达",
            ],
        )

    def test_combined_price_reply_marks_failed_ronghui_unreachable(self):
        messages = format_price_reply_messages({
            "success": True,
            "data": {
                "ronghui": {"provider": "融辉", "unavailable": True, "error": "网点不可达"},
                "yunda": {"目的网点": "贵州毕节赫章县公司", "韵达自提": "573.70元"},
            },
        })

        self.assertEqual(
            messages,
            [
                "融辉价格\n融辉不可到达",
                "韵达价格\n目的网点：贵州毕节赫章县公司\n韵达自提：573.70元",
            ],
        )

    def test_combined_price_reply_preserves_ronghui_resolver_failure_reason(self):
        messages = format_price_reply_messages({
            "success": True,
            "data": {
                "ronghui": {
                    "provider": "融辉",
                    "error": "融辉地址解析失败：browser address resolve failed: Timeout 30000ms exceeded",
                    "error_code": "RONGHUI_ADDRESS_RESOLVE_FAILED",
                },
                "yunda": {"目的网点": "贵州贵阳开阳公司", "韵达自提": "1399.05元"},
            },
        })

        self.assertEqual(
            messages,
            [
                "融辉价格\n融辉报价失败：融辉地址解析失败：browser address resolve failed: Timeout 30000ms exceeded",
                "韵达价格\n目的网点：贵州贵阳开阳公司\n韵达自提：1399.05元",
            ],
        )
        self.assertNotIn("不可到达", messages[0])

    def test_price_tool_captures_progress_stdout(self):
        class DummyModule:
            @staticmethod
            def run_once(params):
                print("[Login] 第 1 次尝试成功")
                return {"目的网点": "测试站"}

        with patch("tools.price_tool._load_local_price_module", return_value=DummyModule()):
            result = get_price_via_script("武汉", 10)

        self.assertEqual(result["目的网点"], "测试站")
        self.assertEqual(result["mode"], "local_script")

    def test_get_price_yunda_auth_result_selects_yunda_session(self):
        auth_session = message_handler._auth_session_for_result(
            "get_price",
            {"address": "武汉", "weight": 12},
            {
                "success": False,
                "error_code": "AUTH_REQUIRED",
                "data": {"auth_session": "yunda", "error": "韵达登录态已失效"},
            },
        )
        self.assertEqual("yunda", auth_session)


class FeishuSendCodeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_command_when_running_does_not_start_second_script(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict, int]] = []

        class FakeAgent:
            def is_tool_running(self, tool_name):
                return tool_name == "sync_scan_codes"

            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("running scan command must not start another script")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("direct scan command should not use LLM")

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append(text)

        def fake_set_pending(chat_id, payload, ttl_sec=600):
            pending_calls.append((chat_id, payload, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch.object(message_handler, "get_pending", return_value=None),
            patch.object(message_handler, "set_pending", side_effect=fake_set_pending),
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
        ):
            await message_handler._process_and_reply("扫描", "user-1", "chat-1")

        self.assertEqual(1, len(replies))
        self.assertNotIn("程序正在执行", replies[0])
        self.assertIn("扫描任务失败", replies[0])
        self.assertIn("脚本正在执行中", replies[0])
        self.assertEqual("cancel_running_tool", pending_calls[0][1]["type"])
        self.assertEqual("sync_scan_codes", pending_calls[0][1]["tool_name"])

    async def test_cancel_running_tool_pending_calls_cancel_tool(self):
        replies: list[str] = []
        cancel_calls: list[tuple[str, str]] = []
        pending = {
            "type": "cancel_running_tool",
            "tool_name": "sync_scan_codes",
            "description": "扫描任务",
            "started_at": "2026-06-08 15:44:00",
        }

        class FakeAgent:
            async def cancel_tool(self, tool_name, started_at=""):
                cancel_calls.append((tool_name, started_at))
                return {"ok": True, "message": "已发送取消请求，正在停止脚本。"}

            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("cancel pending should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("cancel pending should not use LLM")

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch.object(message_handler, "get_pending", return_value=pending),
            patch.object(message_handler, "clear_pending") as clear_pending_mock,
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
        ):
            await message_handler._process_and_reply("取消", "user-1", "chat-1")

        clear_pending_mock.assert_called_once_with("chat-1")
        self.assertEqual([("sync_scan_codes", "2026-06-08 15:44:00")], cancel_calls)
        self.assertEqual(["扫描任务：已发送取消请求，正在停止脚本。"], replies)

    async def test_explicit_cancel_scan_command_calls_cancel_tool(self):
        replies: list[str] = []
        cancel_calls: list[tuple[str, str]] = []

        class FakeAgent:
            async def cancel_tool(self, tool_name, started_at=""):
                cancel_calls.append((tool_name, started_at))
                return {"ok": True, "message": "已发送取消请求，正在停止脚本。"}

            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("explicit cancel should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("explicit cancel should not use LLM")

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch.object(message_handler, "get_pending", return_value=None),
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
        ):
            await message_handler._process_and_reply("取消扫描", "user-1", "chat-1")

        self.assertEqual([("sync_scan_codes", "")], cancel_calls)
        self.assertEqual(["扫描任务：已发送取消请求，正在停止脚本。"], replies)

    async def test_price_reply_sends_ronghui_and_yunda_messages(self):
        replies: list[tuple[str, str]] = []
        case = self

        class FakeAgent:
            async def execute_tool(self, tool_name, params):
                case.assertEqual("get_price", tool_name)
                return {
                    "success": True,
                    "data": {
                        "ronghui": {"目的网点": "武汉融信站", "精准零担": "273.92元"},
                        "yunda": {"韵达自提": "120.00元", "韵达派送": "138.50元"},
                    },
                }

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append((reply_type, text))

        with patch.object(message_handler, "_reply_text", side_effect=fake_reply):
            await message_handler._execute_and_reply(
                FakeAgent(),
                "oc_test",
                "get_price",
                {"address": "武汉", "weight": 12},
            )

        self.assertEqual(
            replies,
            [
                ("tool_reply:get_price", "融辉价格\n目的网点：武汉融信站\n精准零担：273.92元"),
                ("tool_reply:get_price:2", "韵达价格\n韵达自提：120.00元\n韵达派送：138.50元"),
            ],
        )

    async def test_price_yunda_auth_required_pending_uses_yunda_session(self):
        replies: list[str] = []

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append(text)

        with (
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
            patch.object(message_handler, "_auth_session_currently_authenticated", return_value=False),
            patch.object(message_handler, "set_pending") as set_pending_mock,
        ):
            handled = await message_handler._handle_auth_result(
                "oc_test",
                result={
                    "success": False,
                    "error_code": "AUTH_REQUIRED",
                    "data": {"auth_session": "yunda", "error": "韵达登录态已失效"},
                },
                resume_tool="get_price",
                resume_params={"address": "武汉", "weight": 12},
            )

        self.assertTrue(handled)
        pending_payload = set_pending_mock.call_args.args[1]
        self.assertEqual("confirm_login_for_resume", pending_payload["type"])
        self.assertEqual("yunda", pending_payload["auth_session"])
        self.assertIn("登录过期需要重新登录", replies[0])

    async def test_authenticated_send_code_response_resumes_original_tool(self):
        replies: list[tuple[str, str]] = []
        executed: list[tuple[str, dict]] = []

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append((reply_type, text))

        async def fake_post_admin(path, body=None):
            self.assertEqual(path, "/admin/tms/yunda-session/send-code")
            return {
                "ok": True,
                "profile": "yunda",
                "status": "authenticated",
                "authenticated": True,
                "pending_code": False,
            }

        async def fake_execute_and_reply(agent, chat_id, tool_name, params):
            executed.append((tool_name, params))

        with (
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
            patch.object(message_handler, "_post_admin", side_effect=fake_post_admin),
            patch.object(message_handler, "_execute_and_reply", side_effect=fake_execute_and_reply),
            patch.object(message_handler, "set_pending") as set_pending_mock,
            patch.object(message_handler, "clear_pending") as clear_pending_mock,
        ):
            await message_handler._send_code_and_wait(
                "oc_test",
                auth_session="yunda",
                resume_tool="track_waybill",
                resume_params={"tracking_number": "292084494", "provider": "yunda"},
                agent=Mock(),
            )

        set_pending_mock.assert_not_called()
        clear_pending_mock.assert_called_once_with("oc_test")
        self.assertEqual(executed, [("track_waybill", {"tracking_number": "292084494", "provider": "yunda"})])
        self.assertEqual(replies[0][0], "send_code_start")
        self.assertEqual(replies[1][0], "login_success")

    async def test_ronghui_authenticated_send_code_uses_auto_image_login_flow(self):
        replies: list[tuple[str, str]] = []
        executed: list[tuple[str, dict]] = []

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append((reply_type, text))

        async def fake_post_admin(path, body=None):
            self.assertEqual(path, "/admin/tms/session/send-code")
            return {
                "ok": True,
                "profile": "default",
                "status": "authenticated",
                "authenticated": True,
                "pending_code": False,
            }

        async def fake_execute_and_reply(agent, chat_id, tool_name, params):
            executed.append((tool_name, params))

        with (
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
            patch.object(message_handler, "_post_admin", side_effect=fake_post_admin),
            patch.object(message_handler, "_execute_and_reply", side_effect=fake_execute_and_reply),
            patch.object(message_handler, "set_pending") as set_pending_mock,
            patch.object(message_handler, "clear_pending") as clear_pending_mock,
        ):
            await message_handler._send_code_and_wait(
                "oc_test",
                auth_session="default",
                resume_tool="get_price",
                resume_params={"address": "武汉", "weight": 12},
                agent=Mock(),
            )

        set_pending_mock.assert_not_called()
        clear_pending_mock.assert_called_once_with("oc_test")
        self.assertEqual(executed, [("get_price", {"address": "武汉", "weight": 12})])
        self.assertEqual(replies[0][0], "send_code_start")
        self.assertIn("自动识别图片验证码并登录", replies[0][1])
        self.assertEqual(replies[1][0], "login_success")

    async def test_ronghui_pending_image_send_code_keeps_manual_fallback_prompt(self):
        replies: list[tuple[str, str]] = []

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append((reply_type, text))

        async def fake_post_admin(path, body=None):
            self.assertEqual(path, "/admin/tms/session/send-code")
            return {
                "ok": True,
                "profile": "default",
                "status": "pending_code",
                "authenticated": False,
                "pending_code": True,
                "challenge_type": "image",
                "challenge_label": "图片验证码",
                "last_error_summary": "自动识别失败 4 次，请人工输入或刷新验证码后重试。",
            }

        with (
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
            patch.object(message_handler, "_post_admin", side_effect=fake_post_admin),
            patch.object(message_handler, "set_pending") as set_pending_mock,
            patch.object(message_handler, "clear_pending") as clear_pending_mock,
        ):
            await message_handler._send_code_and_wait(
                "oc_test",
                auth_session="default",
                resume_tool="get_price",
                resume_params={"address": "武汉", "weight": 12},
                agent=Mock(),
            )

        clear_pending_mock.assert_not_called()
        set_pending_mock.assert_called_once()
        pending_payload = set_pending_mock.call_args.args[1]
        self.assertEqual(pending_payload["type"], "waiting_code_for_resume")
        self.assertEqual(pending_payload["auth_session"], "default")
        self.assertEqual(replies[0][0], "send_code_start")
        self.assertEqual(replies[1][0], "send_code_prompt")
        self.assertIn("自动识别失败 4 次", replies[1][1])
        self.assertIn("后台账号管理页面", replies[1][1])

    async def test_login_command_lists_dynamic_account_choices(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict, int]] = []

        class FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login command should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not use LLM")

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append(text)

        def fake_set_pending(chat_id, payload, ttl_sec=600):
            pending_calls.append((chat_id, payload, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch.object(message_handler, "get_pending", return_value=None),
            patch.object(message_handler, "_get_admin", return_value=_admin_accounts_payload()),
            patch.object(message_handler, "set_pending", side_effect=fake_set_pending),
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
        ):
            await message_handler._process_and_reply("登录", "user-1", "chat-1")

        self.assertEqual("login_account_choice", pending_calls[0][1]["type"])
        self.assertEqual("account:ronghui_default", pending_calls[0][1]["options"][0]["auth_session"])
        self.assertIn("1. TMS融辉默认账号 (ronghui_default) · TMS融辉", replies[-1])
        self.assertIn("2. 大祥报价账号 (price_default) · 大祥报价", replies[-1])

    async def test_login_choice_reply_uses_account_login_endpoint(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict | None]] = []
        pending_calls: list[tuple[str, dict, int]] = []
        options = message_handler._account_options_from_accounts_payload(_admin_accounts_payload())
        pending = {"type": "login_account_choice", "options": options}

        class FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login choice should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login choice should not use LLM")

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append(text)

        async def fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code", "account_id": "ronghui_default"}

        def fake_set_pending(chat_id, payload, ttl_sec=600):
            pending_calls.append((chat_id, payload, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch.object(message_handler, "get_pending", return_value=pending),
            patch.object(message_handler, "clear_pending") as clear_pending,
            patch.object(message_handler, "_post_admin", side_effect=fake_post_admin),
            patch.object(message_handler, "set_pending", side_effect=fake_set_pending),
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
        ):
            await message_handler._process_and_reply("1", "user-1", "chat-1")

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual([("/admin/accounts/ronghui_default/login", None)], admin_calls)
        self.assertEqual("account:ronghui_default", pending_calls[0][1]["auth_session"])
        self.assertIn("正在自动识别图片验证码并登录", replies[0])

    async def test_single_pending_account_code_submits_directly(self):
        replies: list[str] = []
        get_calls: list[str] = []
        post_calls: list[tuple[str, dict | None]] = []

        class FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("standalone sms code should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("standalone sms code should not use LLM")

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append(text)

        async def fake_get_admin(path):
            get_calls.append(path)
            return _admin_accounts_payload(pending_accounts={"yunda_default"})

        async def fake_post_admin(path, body=None):
            post_calls.append((path, body))
            return {"ok": True, "status": "authenticated", "account_id": "yunda_default"}

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch.object(message_handler, "get_pending", return_value=None),
            patch.object(message_handler, "_get_admin", side_effect=fake_get_admin),
            patch.object(message_handler, "_post_admin", side_effect=fake_post_admin),
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
        ):
            await message_handler._process_and_reply("123456", "user-1", "chat-1")

        self.assertEqual(["/admin/accounts"], get_calls)
        self.assertEqual([("/admin/accounts/yunda_default/submit-code", {"code": "123456"})], post_calls)
        self.assertIn("正在校验验证码", replies[0])
        self.assertEqual("登录成功", replies[-1])

    async def test_multiple_pending_accounts_require_account_choice_before_code_submit(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict, int]] = []

        class FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("standalone sms code should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("standalone sms code should not use LLM")

        async def fake_reply(chat_id, text, receive_id_type="chat_id", reply_type="text"):
            replies.append(text)

        def fake_set_pending(chat_id, payload, ttl_sec=600):
            pending_calls.append((chat_id, payload, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch.object(message_handler, "get_pending", return_value=None),
            patch.object(
                message_handler,
                "_get_admin",
                return_value=_admin_accounts_payload(pending_accounts={"ronghui_default", "yunda_default"}),
            ),
            patch.object(message_handler, "_post_admin", side_effect=AssertionError("code should not submit before account choice")),
            patch.object(message_handler, "set_pending", side_effect=fake_set_pending),
            patch.object(message_handler, "_reply_text", side_effect=fake_reply),
        ):
            await message_handler._process_and_reply("123456", "user-1", "chat-1")

        self.assertEqual("login_account_choice", pending_calls[0][1]["type"])
        option_ids = [item["account_id"] for item in pending_calls[0][1]["options"]]
        self.assertEqual(["ronghui_default", "yunda_default"], option_ids)
        self.assertIn("1. TMS融辉默认账号", replies[-1])


class FeishuWebhookHelperTests(unittest.TestCase):
    def test_event_type_prefers_header_value(self):
        self.assertEqual(
            _feishu_event_type(
                {
                    "type": "event_callback",
                    "header": {"event_type": "im.message.receive_v1"},
                }
            ),
            "im.message.receive_v1",
        )

    def test_verify_token_accepts_match(self):
        previous = os.environ.get("FEISHU_EVENT_VERIFICATION_TOKEN")
        os.environ["FEISHU_EVENT_VERIFICATION_TOKEN"] = "expected-token"
        try:
            _verify_feishu_event_token({"token": "expected-token"})
        finally:
            self._restore_env("FEISHU_EVENT_VERIFICATION_TOKEN", previous)

    def test_verify_token_rejects_mismatch(self):
        previous = os.environ.get("FEISHU_EVENT_VERIFICATION_TOKEN")
        os.environ["FEISHU_EVENT_VERIFICATION_TOKEN"] = "expected-token"
        try:
            with self.assertRaises(HTTPException):
                _verify_feishu_event_token({"token": "wrong-token"})
        finally:
            self._restore_env("FEISHU_EVENT_VERIFICATION_TOKEN", previous)

    @staticmethod
    def _restore_env(name: str, previous: str | None) -> None:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


class FeishuNotifyTests(unittest.TestCase):
    def test_notify_target_uses_recent_chat_when_env_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                patch.object(notify, "_STATE_DIR", state_dir),
                patch.object(notify, "_LAST_CHAT_PATH", state_dir / "last_chat.json"),
                patch.dict(
                    os.environ,
                    {
                        "FEISHU_TMS_ALERT_CHAT_ID": "",
                        "FEISHU_NOTIFY_CHAT_ID": "",
                        "FEISHU_ALERT_CHAT_ID": "",
                        "FEISHU_DEFAULT_CHAT_ID": "",
                        "FEISHU_TMS_ALERT_OPEN_ID": "",
                        "FEISHU_NOTIFY_OPEN_ID": "",
                        "FEISHU_ALERT_OPEN_ID": "",
                        "FEISHU_TMS_ALERT_USER_ID": "",
                        "FEISHU_NOTIFY_USER_ID": "",
                        "FEISHU_ALERT_USER_ID": "",
                    },
                    clear=False,
                ),
            ):
                notify.remember_chat_id("oc_test_chat")

                self.assertEqual(notify.resolve_notify_target(), ("chat_id", "oc_test_chat"))

    def test_notify_target_env_chat_id_wins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                patch.object(notify, "_STATE_DIR", state_dir),
                patch.object(notify, "_LAST_CHAT_PATH", state_dir / "last_chat.json"),
                patch.dict(os.environ, {"FEISHU_TMS_ALERT_CHAT_ID": "oc_env_chat"}, clear=False),
            ):
                notify.remember_chat_id("oc_test_chat")

                self.assertEqual(notify.resolve_notify_target(), ("chat_id", "oc_env_chat"))

    def test_tms_disconnect_message_mentions_verify_code_login(self):
        text = notify.build_tms_session_disconnected_message("expired", "need login")

        self.assertIn("TMS", text)
        self.assertIn("need login", text)
        self.assertIn("\u9a8c\u8bc1\u7801", text)

    def test_tms_pending_code_message_mentions_waiting_code(self):
        text = notify.build_tms_session_disconnected_message("pending_code", "")

        self.assertIn("TMS", text)
        self.assertIn("\u5f85\u8f93\u5165\u9a8c\u8bc1\u7801", text)
        self.assertIn("\u98de\u4e66", text)

    def test_tms_alert_key_ignores_revalidation_timestamp(self):
        first = _tms_session_alert_key(
            {
                "status": "expired",
                "last_error_summary": "session expired",
                "last_validation_at": "2026-04-29 10:00:00",
            }
        )
        second = _tms_session_alert_key(
            {
                "status": "expired",
                "last_error_summary": "session expired",
                "last_validation_at": "2026-04-29 10:01:00",
            }
        )

        self.assertEqual(first, second)


class TmsAccountMonitorTests(unittest.TestCase):
    def test_alert_monitor_waits_for_websocket_lease_in_websocket_mode(self):
        with (
            patch.object(main, "websocket_enabled", return_value=True),
            patch.object(main, "websocket_lease_active", return_value=False),
        ):
            self.assertFalse(main._should_start_tms_session_alert_monitor())

    def test_alert_monitor_runs_without_websocket_lease_in_webhook_mode(self):
        with (
            patch.object(main, "websocket_enabled", return_value=False),
            patch.object(main, "websocket_lease_active", return_value=False),
        ):
            self.assertTrue(main._should_start_tms_session_alert_monitor())

    def test_session_monitor_skips_accounts_with_auto_login_disabled_or_blocked(self):
        class FakeManager:
            def describe_status(self, account_id, *, validate=True, force=False):
                raise AssertionError("paused accounts must not be validated")

            def check_status_with_auto_login(self, account_id, *, force=False):
                raise AssertionError("paused accounts must not attempt auto-login")

        base_account = {
            "account_id": "ronghui_default",
            "system": "ronghui",
            "session_capable": True,
            "is_active": True,
        }

        disabled = _check_tms_account_session(
            FakeManager(),
            {**base_account, "auto_login_enabled": False},
        )
        blocked = _check_tms_account_session(
            FakeManager(),
            {
                **base_account,
                "auto_login_enabled": True,
                "auto_login_blocked": True,
            },
        )

        self.assertFalse(disabled["monitored"])
        self.assertFalse(disabled["should_alert"])
        self.assertFalse(blocked["monitored"])
        self.assertFalse(blocked["should_alert"])
        self.assertFalse(
            main._should_alert_tms_session_status(
                {"status": "error", "auto_login_enabled": False}
            )
        )

    def test_disconnected_account_auto_login_success_does_not_alert(self):
        account = {
            "account_id": "ronghui_default",
            "name": "TMS融辉默认账号",
            "system": "ronghui",
            "system_label": "TMS融辉",
            "session_profile": "default",
            "session_capable": True,
            "is_active": True,
            "auto_login_enabled": True,
        }

        class FakeManager:
            def __init__(self):
                self.calls = []

            def describe_status(self, account_id, *, validate=True, force=False):
                self.calls.append(("status", account_id, validate, force))
                return {
                    "account_id": account_id,
                    "status": "authenticated",
                    "authenticated": True,
                    "pending_code": False,
                    "last_error_summary": "",
                }

            def check_status_with_auto_login(self, account_id, *, force=False):
                self.calls.append(("check", account_id, force))
                return {
                    "account_id": account_id,
                    "status": "authenticated",
                    "authenticated": True,
                    "pending_code": False,
                }

        manager = FakeManager()
        result = _check_tms_account_session(manager, account)

        self.assertTrue(result["monitored"])
        self.assertFalse(result["should_alert"])
        self.assertEqual("authenticated", result["status_payload"]["status"])
        self.assertEqual(
            manager.calls,
            [
                ("status", "ronghui_default", False, False),
                ("check", "ronghui_default", True),
            ],
        )

    def test_disconnected_account_pending_code_alert_keeps_account_context(self):
        account = {
            "account_id": "yunda_default",
            "name": "韵达默认账号",
            "system": "yunda",
            "system_label": "韵达",
            "session_profile": "yunda",
            "session_capable": True,
            "is_active": True,
            "auto_login_enabled": True,
        }

        class FakeManager:
            def describe_status(self, account_id, *, validate=True, force=False):
                return {
                    "account_id": account_id,
                    "status": "logged_out",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "logged out",
                }

            def check_status_with_auto_login(self, account_id, *, force=False):
                return {
                    "account_id": account_id,
                    "account_name": "韵达默认账号",
                    "status": "pending_code",
                    "authenticated": False,
                    "pending_code": True,
                    "challenge_type": "sms",
                    "last_error_summary": "短信验证码已发送",
                }

        result = _check_tms_account_session(FakeManager(), account)

        self.assertTrue(result["monitored"])
        self.assertTrue(result["should_alert"])
        self.assertEqual("pending_code", result["status_payload"]["status"])
        self.assertEqual("sms", result["status_payload"]["challenge_type"])
        self.assertEqual("yunda_default", result["status_payload"]["account_id"])
        self.assertEqual("韵达默认账号", result["status_payload"]["account_name"])

    def test_session_monitor_updates_account_list_cache_with_checked_status(self):
        account = {
            "account_id": "ronghui_default",
            "name": "TMS融辉默认账号",
            "system": "ronghui",
            "system_label": "TMS融辉",
            "session_profile": "default",
            "session_capable": True,
            "is_active": True,
            "auto_login_enabled": True,
        }

        class FakeManager:
            def describe_status(self, account_id, *, validate=True, force=False):
                return {
                    "account_id": account_id,
                    "status": "authenticated",
                    "last_error_summary": "",
                }

            def check_status_with_auto_login(self, account_id, *, force=False):
                return {
                    "account_id": account_id,
                    "status": "error",
                    "last_error_summary": "缺少登录配置",
                }

        with patch.object(main, "update_account_list_cache_status") as update_cache:
            result = _check_tms_account_session(FakeManager(), account)

        update_cache.assert_called_once()
        self.assertEqual(update_cache.call_args.args[0]["account_id"], "ronghui_default")
        self.assertEqual(update_cache.call_args.args[0]["status"], "error")
        self.assertTrue(result["should_alert"])

    def test_session_monitor_suppresses_transient_yunda_timeout_alert(self):
        account = {
            "account_id": "yunda_default",
            "name": "韵达默认账号",
            "system": "yunda",
            "system_label": "韵达",
            "session_profile": "yunda",
            "session_capable": True,
            "is_active": True,
            "auto_login_enabled": True,
        }
        timeout_message = (
            "HTTPSConnectionPool(host='rpts-kyprts.yunda56.com', port=8081): "
            "Read timed out. (read timeout=30)"
        )

        class FakeManager:
            def describe_status(self, account_id, *, validate=True, force=False):
                return {
                    "account_id": account_id,
                    "status": "authenticated",
                    "last_error_summary": "",
                }

            def check_status_with_auto_login(self, account_id, *, force=False):
                raise TimeoutError(timeout_message)

        with patch.object(main, "update_account_list_cache_status") as update_cache:
            result = _check_tms_account_session(FakeManager(), account)

        update_cache.assert_called_once()
        self.assertEqual(update_cache.call_args.args[0]["account_id"], "yunda_default")
        self.assertEqual(update_cache.call_args.args[0]["status"], "error")
        self.assertEqual(timeout_message, update_cache.call_args.args[0]["last_error_summary"])
        self.assertTrue(result["monitored"])
        self.assertFalse(result["should_alert"])
        self.assertEqual("error", result["status_payload"]["status"])


if __name__ == "__main__":
    unittest.main()
