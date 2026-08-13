import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from feishu import message_handler
from agent import pending_actions


def candidates(count: int) -> list[dict[str, Any]]:
    return [
        {
            "bill_code": f"R{index:04d}",
            "status": "未执行",
            "problem_type": "少货/分批" if index % 2 else "有发未到",
            "arrived_quantity": 1 if index % 2 else 0,
            "expected_quantity": 5,
        }
        for index in range(1, count + 1)
    ]


class FeishuSplitSelectionTests(unittest.TestCase):
    def test_selection_parser_supports_all_formats(self):
        self.assertEqual([2], message_handler._parse_split_selection("2", 5))
        self.assertEqual([1, 3, 5], message_handler._parse_split_selection("1，3、5", 5))
        self.assertEqual([2, 3, 4], message_handler._parse_split_selection("2-4", 5))
        self.assertEqual([1, 2, 3], message_handler._parse_split_selection("全部", 3))

    def test_selection_parser_rejects_duplicate_overlap_illegal_and_out_of_range(self):
        for raw in ("1,1", "1-3,3", "4", "3-1", "1,,2", "abc"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                message_handler._parse_split_selection(raw, 3)

    def test_long_candidate_list_is_chunked_with_global_numbering(self):
        rows = candidates(180)
        chunks = message_handler._split_text_chunks(
            message_handler._split_candidate_lines(rows, hidden_completed=7)
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= message_handler.FEISHU_SAFE_TEXT_BYTES for chunk in chunks))
        combined = "\n".join(chunks)
        self.assertIn("1. R0001", combined)
        self.assertIn("180. R0180", combined)

    def test_preview_select_confirm_executes_only_selected_codes(self):
        replies: list[str] = []
        pending_store: dict[str, dict[str, Any]] = {}
        pending_ttls: list[int] = []
        calls: list[tuple[str, dict[str, Any]]] = []

        class FakeAgent:
            async def execute_tool(self, tool_name, params, **_kwargs):
                calls.append((tool_name, dict(params)))
                if tool_name == "preview_split_pending_problems":
                    return {
                        "success": True,
                        "data": {
                            "ok": True,
                            "stage": "dry_run",
                            "candidate_count": 3,
                            "hidden_completed_count": 2,
                            "preview_fingerprint": "a" * 64,
                            "candidates": candidates(3),
                        },
                    }
                return {
                    "success": True,
                    "data": {
                        "ok": True,
                        "stage": "done",
                        "candidate_count": 3,
                        "selected_count": 2,
                        "saved_bills": 2,
                        "failed_bills": 0,
                        "results": [],
                    },
                }

            async def handle_message(self, *_args, **_kwargs):
                raise AssertionError("split flow must not reach LLM")

        async def fake_reply(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(str(text))

        def get_pending(chat_id):
            return pending_store.get(chat_id)

        def set_pending(chat_id, payload, ttl_sec=600):
            pending_store[chat_id] = dict(payload)
            pending_ttls.append(ttl_sec)

        def clear_pending(chat_id):
            return pending_store.pop(chat_id, None)

        with patch("feishu.bot.get_agent_core", return_value=FakeAgent()), patch.object(
            message_handler, "get_pending", side_effect=get_pending
        ), patch.object(message_handler, "set_pending", side_effect=set_pending), patch.object(
            message_handler, "clear_pending", side_effect=clear_pending
        ), patch.object(message_handler, "_reply_text", side_effect=fake_reply):
            asyncio.run(message_handler._process_and_reply("分批", "user", "chat"))
            self.assertEqual("split_pending_selection", pending_store["chat"]["type"])
            self.assertTrue(any("3. R0003" in reply for reply in replies))

            asyncio.run(message_handler._process_and_reply("1,3", "user", "chat"))
            self.assertEqual("split_pending_confirmation", pending_store["chat"]["type"])
            self.assertEqual(["R0001", "R0003"], pending_store["chat"]["selected_bill_codes"])

            asyncio.run(message_handler._process_and_reply("确认", "user", "chat"))

        self.assertNotIn("chat", pending_store)
        formal_call = calls[-1]
        self.assertFalse(formal_call[1]["dry_run"])
        self.assertEqual(["R0001", "R0003"], formal_call[1]["selected_bill_codes"])
        self.assertEqual("a" * 64, formal_call[1]["preview_fingerprint"])
        self.assertEqual([600, 600], pending_ttls)

    def test_initial_confirmation_executes_all_previewed_codes(self):
        replies: list[str] = []
        pending_store: dict[str, dict[str, Any]] = {}
        calls: list[tuple[str, dict[str, Any]]] = []

        class FakeAgent:
            async def execute_tool(self, tool_name, params, **_kwargs):
                calls.append((tool_name, dict(params)))
                if tool_name == "preview_split_pending_problems":
                    return {
                        "success": True,
                        "data": {
                            "ok": True,
                            "stage": "dry_run",
                            "candidate_count": 3,
                            "hidden_completed_count": 0,
                            "preview_fingerprint": "g" * 64,
                            "candidates": candidates(3),
                        },
                    }
                return {
                    "success": True,
                    "data": {
                        "ok": True,
                        "stage": "done",
                        "candidate_count": 3,
                        "selected_count": 3,
                        "saved_bills": 3,
                        "failed_bills": 0,
                        "results": [],
                    },
                }

            async def handle_message(self, *_args, **_kwargs):
                raise AssertionError("split flow must not reach LLM")

        async def fake_reply(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(str(text))

        def set_pending(chat_id, payload, ttl_sec=600):
            pending_store[chat_id] = dict(payload)

        with patch("feishu.bot.get_agent_core", return_value=FakeAgent()), patch.object(
            message_handler, "get_pending", side_effect=lambda chat_id: pending_store.get(chat_id)
        ), patch.object(message_handler, "set_pending", side_effect=set_pending), patch.object(
            message_handler,
            "clear_pending",
            side_effect=lambda chat_id: pending_store.pop(chat_id, None),
        ), patch.object(message_handler, "_reply_text", side_effect=fake_reply):
            asyncio.run(message_handler._process_and_reply("分批", "user", "chat"))
            self.assertEqual("split_pending_selection", pending_store["chat"]["type"])

            asyncio.run(message_handler._process_and_reply("确认", "user", "chat"))

        self.assertNotIn("chat", pending_store)
        formal_calls = [params for tool_name, params in calls if tool_name == "split_pending_problem_upload"]
        self.assertEqual(1, len(formal_calls))
        self.assertEqual(["R0001", "R0002", "R0003"], formal_calls[0]["selected_bill_codes"])
        self.assertEqual("g" * 64, formal_calls[0]["preview_fingerprint"])
        self.assertEqual("ronghui_default", formal_calls[0]["account_id"])
        self.assertFalse(any(reply.startswith("已选择") for reply in replies))

    def test_initial_confirmation_keeps_full_list_when_tool_is_running(self):
        replies: list[str] = []
        pending_store = {
            "chat": {
                "type": "split_pending_selection",
                "tool_name": message_handler.SPLIT_TOOL_NAME,
                "candidates": candidates(2),
                "preview_fingerprint": "h" * 64,
            }
        }

        class FakeAgent:
            def is_tool_running(self, _tool_name):
                return True

            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("running tool must not execute")

        async def fake_reply(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(str(text))

        with patch("feishu.bot.get_agent_core", return_value=FakeAgent()), patch.object(
            message_handler, "get_pending", side_effect=lambda chat_id: pending_store.get(chat_id)
        ), patch.object(
            message_handler,
            "clear_pending",
            side_effect=lambda chat_id: pending_store.pop(chat_id, None),
        ), patch.object(message_handler, "_reply_text", side_effect=fake_reply):
            asyncio.run(message_handler._process_and_reply("确认", "user", "chat"))

        self.assertEqual("split_pending_selection", pending_store["chat"]["type"])
        self.assertIn("当前列表仍保留", replies[-1])

    def test_candidate_prompt_explains_initial_confirmation_executes_all(self):
        prompt = "\n".join(message_handler._split_candidate_lines(candidates(2), hidden_completed=0))
        self.assertIn("回复“确认”直接执行全部", prompt)
        self.assertIn("部分选择后需再次回复“确认”执行", prompt)

    def test_invalid_selection_keeps_current_preview(self):
        replies: list[str] = []
        pending_store = {
            "chat": {
                "type": "split_pending_selection",
                "tool_name": message_handler.SPLIT_TOOL_NAME,
                "candidates": candidates(2),
                "preview_fingerprint": "b" * 64,
            }
        }

        class FakeAgent:
            async def handle_message(self, *_args, **_kwargs):
                raise AssertionError

        async def fake_reply(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(str(text))

        with patch("feishu.bot.get_agent_core", return_value=FakeAgent()), patch.object(
            message_handler, "get_pending", side_effect=lambda chat_id: pending_store.get(chat_id)
        ), patch.object(
            message_handler,
            "clear_pending",
            side_effect=lambda chat_id: pending_store.pop(chat_id, None),
        ), patch.object(message_handler, "_reply_text", side_effect=fake_reply):
            asyncio.run(message_handler._process_and_reply("1,1", "user", "chat"))

        self.assertEqual("split_pending_selection", pending_store["chat"]["type"])
        self.assertIn("选择无效", replies[-1])

    def test_cancel_clears_split_selection(self):
        replies: list[str] = []
        pending_store = {
            "chat": {
                "type": "split_pending_selection",
                "candidates": candidates(2),
                "preview_fingerprint": "c" * 64,
            }
        }

        class FakeAgent:
            pass

        async def fake_reply(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(str(text))

        with patch("feishu.bot.get_agent_core", return_value=FakeAgent()), patch.object(
            message_handler, "get_pending", side_effect=lambda chat_id: pending_store.get(chat_id)
        ), patch.object(
            message_handler,
            "clear_pending",
            side_effect=lambda chat_id: pending_store.pop(chat_id, None),
        ), patch.object(message_handler, "_reply_text", side_effect=fake_reply):
            asyncio.run(message_handler._process_and_reply("取消", "user", "chat"))
        self.assertNotIn("chat", pending_store)
        self.assertIn("已取消", replies[-1])

    def test_resending_split_replaces_old_confirmation_with_latest_preview(self):
        pending_store: dict[str, dict[str, Any]] = {
            "chat": {
                "type": "split_pending_confirmation",
                "selected_bill_codes": ["OLD"],
                "preview_fingerprint": "d" * 64,
            }
        }

        class FakeAgent:
            async def execute_tool(self, _tool_name, params, **_kwargs):
                self.params = params
                return {
                    "success": True,
                    "data": {
                        "ok": True,
                        "stage": "dry_run",
                        "candidate_count": 1,
                        "hidden_completed_count": 0,
                        "preview_fingerprint": "e" * 64,
                        "candidates": candidates(1),
                    },
                }

        async def fake_reply(*_args, **_kwargs):
            return None

        def clear_pending(chat_id):
            return pending_store.pop(chat_id, None)

        def set_pending(chat_id, payload, ttl_sec=600):
            pending_store[chat_id] = dict(payload)

        with patch("feishu.bot.get_agent_core", return_value=FakeAgent()), patch.object(
            message_handler, "get_pending", side_effect=lambda chat_id: pending_store.get(chat_id)
        ), patch.object(message_handler, "clear_pending", side_effect=clear_pending), patch.object(
            message_handler, "set_pending", side_effect=set_pending
        ), patch.object(message_handler, "_reply_text", side_effect=fake_reply):
            asyncio.run(message_handler._process_and_reply("分批", "user", "chat"))
        self.assertEqual("split_pending_selection", pending_store["chat"]["type"])
        self.assertEqual("e" * 64, pending_store["chat"]["preview_fingerprint"])

    def test_running_tool_keeps_confirmation_selection(self):
        replies: list[str] = []
        pending = {
            "type": "split_pending_confirmation",
            "selected_bill_codes": ["R0001"],
            "preview_fingerprint": "f" * 64,
            "account_id": "ronghui_default",
        }

        class FakeAgent:
            def is_tool_running(self, _tool_name):
                return True

            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("running tool must not execute")

        async def fake_reply(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(str(text))

        with patch("feishu.bot.get_agent_core", return_value=FakeAgent()), patch.object(
            message_handler, "get_pending", return_value=pending
        ), patch.object(message_handler, "clear_pending") as clear_mock, patch.object(
            message_handler, "_reply_text", side_effect=fake_reply
        ):
            asyncio.run(message_handler._process_and_reply("确认", "user", "chat"))
        clear_mock.assert_not_called()
        self.assertIn("当前选择仍保留", replies[-1])

    def test_pending_selection_expires_after_ten_minutes(self):
        project_tmp = Path(__file__).resolve().parents[1] / "tmp"
        project_tmp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=project_tmp) as temp_dir:
            state_file = os.path.join(temp_dir, "pending.json")
            with patch.dict(os.environ, {"AGENT_PENDING_STATE_FILE": state_file}), patch.object(
                pending_actions.time, "time", return_value=100.0
            ):
                pending_actions.set_pending(
                    "split-timeout-chat",
                    {"type": "split_pending_selection"},
                    ttl_sec=600,
                )
            with patch.dict(os.environ, {"AGENT_PENDING_STATE_FILE": state_file}), patch.object(
                pending_actions.time, "time", return_value=701.0
            ):
                self.assertIsNone(pending_actions.get_pending("split-timeout-chat"))


if __name__ == "__main__":
    unittest.main()
