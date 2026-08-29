import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from feishu import message_handler


class FeishuSelfPickupPendingTests(unittest.TestCase):
    def test_self_pickup_zero_candidate_preview_does_not_offer_confirmation(self):
        replies: list[str] = []
        pending_store: dict[str, dict[str, Any]] = {}
        project_calls: list[dict[str, Any]] = []
        preview_run_id = "33333333-3333-4333-8333-333333333333"
        observed_at = datetime.now(timezone.utc).replace(microsecond=0)

        class FakeAgent:
            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("signed selection preview must not use a legacy tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("preview should not reach the LLM")

        class FakeProjectEntrypoints:
            async def invoke_feishu(self, **kwargs):
                project_calls.append(dict(kwargs))
                return {
                    "success": True,
                    "status": "COMPLETED",
                    "run_id": preview_run_id,
                    "selection_preview": {
                        "contract_version": 1,
                        "automation_id": "self_pickup_problem_upload",
                        "title": "自提到货问题件",
                        "preview_run_id": preview_run_id,
                        "observed_at": observed_at.isoformat(),
                        "expires_at": (
                            observed_at + timedelta(minutes=15)
                        ).isoformat(),
                        "candidate_count": 0,
                        "candidates": [],
                        "summary": {"duplicate_source_rows": 0},
                        "can_confirm": True,
                    },
                }

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
            token = message_handler._COMMAND_CONTEXT.set(
                message_handler.FeishuCommandContext(
                    event_id="event-zero-candidate",
                    actor_id="user-1",
                    chat_id="chat-1",
                )
            )
            try:
                asyncio.run(
                    message_handler._process_and_reply(
                        "自提到货问题件", "user-1", "chat-1"
                    )
                )
            finally:
                message_handler._COMMAND_CONTEXT.reset(token)

            self.assertEqual(1, len(project_calls))
            self.assertEqual({}, project_calls[0]["envelope"]["body"])
            self.assertIsNone(project_calls[0]["preview_run_id"])
            self.assertNotIn("chat-1", pending_store)
            self.assertIn("待上传自提到货问题件候选 0 单", replies[-1])
            self.assertIn("当前没有需要上传的候选数据", replies[-1])
            self.assertNotIn('回复"确认"', replies[-1])

        self.assertNotIn("chat-1", pending_store)
        self.assertNotIn("已取消：自提到货问题件", replies[-1])


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
