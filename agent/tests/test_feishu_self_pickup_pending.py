import asyncio
import unittest
from typing import Any
from unittest.mock import patch

from feishu import message_handler


class FeishuSelfPickupPendingTests(unittest.TestCase):
    def test_self_pickup_zero_candidate_preview_still_allows_cancel(self):
        replies: list[str] = []
        pending_store: dict[str, dict[str, Any]] = {}
        calls: list[tuple[str, dict[str, Any]]] = []

        class FakeAgent:
            async def execute_tool(self, tool_name, params, **_kwargs):
                calls.append((tool_name, params))
                return {
                    "success": True,
                    "data": {
                        "stage": "dry_run",
                        "candidate_count": 0,
                        "table_token": "tbl_test",
                        "sheet_name": "每日到货表",
                        "upload_images": False,
                        "source_summaries": [
                            {"source_name": "邵阳自提部", "candidate_count": 0},
                            {"source_name": "邵阳大祥S站自提", "candidate_count": 0},
                        ],
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("cancel after preview should be handled by pending state")

        class FakeProjectEntrypoints:
            @staticmethod
            def require_feishu_account_bindings(_route_key, *roles):
                bindings = {
                    "account_id": "self-pickup-primary",
                    "daxiang_s_account_id": "self-pickup-secondary",
                }
                return {role: bindings[role] for role in roles}

        async def fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def fake_get_pending(chat_id):
            return pending_store.get(chat_id)

        def fake_set_pending(chat_id, payload, ttl_sec=600):
            pending_store[chat_id] = dict(payload)

        def fake_clear_pending(chat_id):
            return pending_store.pop(chat_id, None)

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch("feishu.message_handler.get_pending", side_effect=fake_get_pending),
            patch("feishu.message_handler.set_pending", side_effect=fake_set_pending),
            patch("feishu.message_handler.clear_pending", side_effect=fake_clear_pending),
            patch("feishu.message_handler._reply_text", side_effect=fake_reply_text),
            patch.object(
                message_handler,
                "_AUTOMATION_PROJECT_ENTRYPOINTS",
                FakeProjectEntrypoints(),
            ),
        ):
            asyncio.run(message_handler._process_and_reply("自提到货问题件", "user-1", "chat-1"))

            self.assertEqual(
                [
                    (
                        "preview_self_pickup_problems",
                        {
                            "account_id": "self-pickup-primary",
                            "daxiang_s_account_id": "self-pickup-secondary",
                        },
                    )
                ],
                calls,
            )
            self.assertEqual("confirm_action", pending_store["chat-1"]["type"])
            self.assertEqual(
                {"dry_run": False},
                pending_store["chat-1"]["dynamic_inputs"],
            )
            self.assertEqual(
                "builtin.self_pickup_problem_upload",
                pending_store["chat-1"]["automation_route_key"],
            )
            self.assertFalse(message_handler._contains_account_override(pending_store["chat-1"]))
            self.assertIn("待上传自提到货问题件候选 0 单", replies[-1])
            self.assertIn('确认上传请回复"确认"', replies[-1])

            asyncio.run(message_handler._process_and_reply("取消", "user-1", "chat-1"))

        self.assertNotIn("chat-1", pending_store)
        self.assertIn("已取消：自提到货问题件", replies[-1])


    def test_deprecated_split_commands_are_blocked_before_agent(self):
        replies: list[str] = []

        class FakeAgent:
            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("deprecated split command must not execute a tool")

            async def handle_message(self, *_args, **_kwargs):
                raise AssertionError("deprecated split command must not reach the LLM")

        async def fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=fake_reply_text),
        ):
            for command in ("分批问题件", "分批差错", "上传分批/未到问题件"):
                asyncio.run(
                    message_handler._process_and_reply(command, "user-1", "chat-1")
                )

        self.assertEqual(
            [
                "该指令已停用，请只发送“分批”。",
                "该指令已停用，请只发送“分批”。",
                "该指令已停用，请只发送“分批”。",
            ],
            replies,
        )
if __name__ == "__main__":
    unittest.main()
