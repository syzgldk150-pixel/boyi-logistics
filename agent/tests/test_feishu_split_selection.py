import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from feishu import message_handler
from agent import pending_actions


class FakeProjectEntrypoints:
    def __init__(
        self,
        *,
        status: str = "COMPLETED",
        results: list[dict[str, Any]] | None = None,
    ) -> None:
        self.status = status
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def require_feishu_account_bindings(_route_key, *roles):
        del roles
        raise AssertionError("selection preview must not resolve accounts in Feishu")

    async def invoke_feishu(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.results:
            return dict(self.results.pop(0))
        return {
            "success": self.status == "COMPLETED",
            "status": self.status,
            "run_id": "run-split",
        }


def run_verified_message(text: str, *, event_id: str) -> None:
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id=event_id,
            actor_id="user",
            chat_id="chat",
        )
    )
    try:
        asyncio.run(message_handler._process_and_reply(text, "user", "chat"))
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)


def candidates(count: int) -> list[dict[str, Any]]:
    return [
        {
            "bill_code": f"R{index:04d}",
            "complaint_status": "未投诉",
            "problem_type": "少货/分批" if index % 2 else "有发未到",
            "arrived_quantity": 1 if index % 2 else 0,
            "expected_quantity": 5,
            "pending_quantity": 4 if index % 2 else 5,
            "problem_item_status": "未执行",
            "source_row_no": index + 1,
        }
        for index in range(1, count + 1)
    ]


def selection_preview(
    run_id: str,
    rows: list[dict[str, Any]],
    *,
    hidden_completed: int = 0,
) -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "success": True,
        "status": "COMPLETED",
        "run_id": run_id,
        "selection_preview": {
            "contract_version": 1,
            "automation_id": "split_pending_problem_upload",
            "title": "分批/未到问题件",
            "preview_run_id": run_id,
            "observed_at": observed_at.isoformat(),
            "expires_at": (observed_at + timedelta(minutes=15)).isoformat(),
            "candidate_count": len(rows),
            "candidates": rows,
            "summary": {
                "complete_count": hidden_completed,
                "hidden_completed_count": hidden_completed,
                "split_count": sum(
                    item["problem_type"] == "少货/分批" for item in rows
                ),
                "pending_count": sum(
                    item["problem_type"] == "有发未到" for item in rows
                ),
            },
            "can_confirm": True,
        },
    }


def selection_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()


class FeishuSplitSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_entrypoints = FakeProjectEntrypoints()
        service_patch = patch.object(
            message_handler,
            "_AUTOMATION_PROJECT_ENTRYPOINTS",
            self.project_entrypoints,
        )
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.command_context_token = message_handler._COMMAND_CONTEXT.set(
            message_handler.FeishuCommandContext(
                event_id="legacy-flow-event",
                actor_id="user",
                chat_id="chat",
            )
        )
        self.addCleanup(
            message_handler._COMMAND_CONTEXT.reset,
            self.command_context_token,
        )

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
        preview_run_id = "44444444-4444-4444-8444-444444444444"
        self.project_entrypoints.results = [
            selection_preview(preview_run_id, candidates(3), hidden_completed=2),
            {"success": True, "status": "COMPLETED", "run_id": "formal-run"},
        ]

        class FakeAgent:
            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("signed preview must not execute a legacy tool")

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
            self.assertEqual(preview_run_id, pending_store["chat"]["preview_run_id"])
            self.assertEqual("user", pending_store["chat"]["originator_actor_id"])
            self.assertTrue(any("3. R0003" in reply for reply in replies))

            asyncio.run(message_handler._process_and_reply("1,3", "user", "chat"))
            self.assertEqual("split_pending_confirmation", pending_store["chat"]["type"])
            self.assertEqual(preview_run_id, pending_store["chat"]["preview_run_id"])
            self.assertEqual("user", pending_store["chat"]["originator_actor_id"])
            self.assertEqual(["R0001", "R0003"], pending_store["chat"]["selected_bill_codes"])

            asyncio.run(message_handler._process_and_reply("确认", "user", "chat"))

        self.assertNotIn("chat", pending_store)
        self.assertEqual({}, self.project_entrypoints.calls[0]["envelope"]["body"])
        self.assertIsNone(self.project_entrypoints.calls[0]["preview_run_id"])
        formal_body = self.project_entrypoints.calls[-1]["envelope"]["body"]
        self.assertEqual(
            {"selected_bill_codes": ["R0001", "R0003"]},
            formal_body,
        )
        self.assertEqual(
            preview_run_id,
            self.project_entrypoints.calls[-1]["preview_run_id"],
        )
        self.assertFalse(message_handler._contains_account_override(formal_body))
        self.assertEqual(2, len(pending_ttls))
        self.assertTrue(all(1 <= ttl <= 900 for ttl in pending_ttls))

    def test_signed_preview_ignores_legacy_tool_running_flag(self):
        replies: list[str] = []
        pending_store: dict[str, dict[str, Any]] = {}
        preview_run_id = "55555555-5555-4555-8555-555555555555"
        self.project_entrypoints.results = [
            selection_preview(preview_run_id, candidates(1)),
        ]

        class FakeAgent:
            def is_tool_running(self, tool_name):
                return tool_name == message_handler.SPLIT_PREVIEW_TOOL_NAME

            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("signed preview must not execute a legacy tool")

            async def handle_message(self, *_args, **_kwargs):
                raise AssertionError("split flow must not reach LLM")

        async def fake_reply(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            del receive_id_type, reply_type
            replies.append(str(text))

        with patch("feishu.bot.get_agent_core", return_value=FakeAgent()), patch.object(
            message_handler, "get_pending", return_value=None
        ), patch.object(
            message_handler,
            "set_pending",
            side_effect=lambda chat_id, payload, ttl_sec=900: pending_store.update(
                {chat_id: dict(payload)}
            ),
        ), patch.object(message_handler, "_reply_text", side_effect=fake_reply):
            asyncio.run(message_handler._process_and_reply("分批", "user", "chat"))

        self.assertTrue(any("正在生成" in reply for reply in replies))
        self.assertFalse(any("脚本正在执行中" in reply for reply in replies))
        self.assertEqual(preview_run_id, pending_store["chat"]["preview_run_id"])
        self.assertEqual(1, len(self.project_entrypoints.calls))

    def test_initial_confirmation_executes_all_previewed_codes(self):
        replies: list[str] = []
        pending_store: dict[str, dict[str, Any]] = {}
        preview_run_id = "66666666-6666-4666-8666-666666666666"
        self.project_entrypoints.results = [
            selection_preview(preview_run_id, candidates(3)),
            {"success": True, "status": "COMPLETED", "run_id": "formal-run"},
        ]

        class FakeAgent:
            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("signed preview must not execute a legacy tool")

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
        self.assertEqual({}, self.project_entrypoints.calls[0]["envelope"]["body"])
        formal_body = self.project_entrypoints.calls[-1]["envelope"]["body"]
        self.assertEqual(
            {"selected_bill_codes": ["R0001", "R0002", "R0003"]},
            formal_body,
        )
        self.assertEqual(
            preview_run_id,
            self.project_entrypoints.calls[-1]["preview_run_id"],
        )
        self.assertFalse(message_handler._contains_account_override(formal_body))
        self.assertFalse(any(reply.startswith("已选择") for reply in replies))

    def test_initial_confirmation_uses_control_plane_not_legacy_tool_running_flag(self):
        replies: list[str] = []
        preview_run_id = "77777777-7777-4777-8777-777777777777"
        pending_store = {
            "chat": {
                "type": "split_pending_selection",
                "tool_name": message_handler.SPLIT_TOOL_NAME,
                "automation_route_key": "builtin.split_pending_problem_upload",
                "originator_actor_id": "user",
                "candidates": candidates(2),
                "preview_run_id": preview_run_id,
                "expires_at": selection_expires_at(),
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

        self.assertNotIn("chat", pending_store)
        self.assertEqual(1, len(self.project_entrypoints.calls))
        self.assertEqual(
            ["R0001", "R0002"],
            self.project_entrypoints.calls[0]["envelope"]["body"]["selected_bill_codes"],
        )
        self.assertEqual(
            preview_run_id,
            self.project_entrypoints.calls[0]["preview_run_id"],
        )
        self.assertTrue(replies)

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
                "automation_route_key": "builtin.split_pending_problem_upload",
                "originator_actor_id": "user",
                "candidates": candidates(2),
                "preview_run_id": "88888888-8888-4888-8888-888888888888",
                "expires_at": selection_expires_at(),
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
                "originator_actor_id": "user",
                "candidates": candidates(2),
                "preview_run_id": "99999999-9999-4999-8999-999999999999",
                "expires_at": selection_expires_at(),
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
        old_preview_run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        new_preview_run_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        pending_store: dict[str, dict[str, Any]] = {
            "chat": {
                "type": "split_pending_confirmation",
                "originator_actor_id": "user",
                "selected_bill_codes": ["OLD"],
                "preview_run_id": old_preview_run_id,
                "expires_at": selection_expires_at(),
            }
        }
        self.project_entrypoints.results = [
            selection_preview(new_preview_run_id, candidates(1)),
        ]

        class FakeAgent:
            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("signed preview must not execute a legacy tool")

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
        self.assertEqual(new_preview_run_id, pending_store["chat"]["preview_run_id"])
        self.assertNotIn("preview_fingerprint", pending_store["chat"])

    def test_confirmed_selection_submits_typed_command_not_legacy_tool(self):
        replies: list[str] = []
        preview_run_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        pending = {
            "type": "split_pending_confirmation",
            "automation_route_key": "builtin.split_pending_problem_upload",
            "originator_actor_id": "user",
            "selected_bill_codes": ["R0001"],
            "preview_run_id": preview_run_id,
            "expires_at": selection_expires_at(),
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
        clear_mock.assert_called_once_with("chat")
        self.assertEqual(1, len(self.project_entrypoints.calls))
        self.assertEqual(
            ["R0001"],
            self.project_entrypoints.calls[0]["envelope"]["body"]["selected_bill_codes"],
        )
        self.assertEqual(
            {"selected_bill_codes": ["R0001"]},
            self.project_entrypoints.calls[0]["envelope"]["body"],
        )
        self.assertEqual(preview_run_id, self.project_entrypoints.calls[0]["preview_run_id"])
        self.assertTrue(replies)

    def test_only_preview_originator_can_confirm_or_cancel_selection(self):
        replies: list[str] = []
        preview_run_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

        class FakeAgent:
            async def execute_tool(self, *_args, **_kwargs):
                raise AssertionError("another group member must not execute a selection")

        async def fake_reply(
            _chat_id,
            text,
            receive_id_type="chat_id",
            *,
            reply_type="text",
        ):
            del receive_id_type, reply_type
            replies.append(str(text))

        pending_shapes = (
            {
                "type": "split_pending_selection",
                "automation_route_key": "builtin.split_pending_problem_upload",
                "originator_actor_id": "originator",
                "candidates": candidates(1),
                "preview_run_id": preview_run_id,
                "expires_at": selection_expires_at(),
            },
            {
                "type": "split_pending_confirmation",
                "automation_route_key": "builtin.split_pending_problem_upload",
                "originator_actor_id": "originator",
                "selected_bill_codes": ["R0001"],
                "preview_run_id": preview_run_id,
                "expires_at": selection_expires_at(),
            },
            {
                "type": "self_pickup_selection_confirmation",
                "automation_route_key": "builtin.self_pickup_problem_upload",
                "originator_actor_id": "originator",
                "selected_bill_codes": ["R_SELF"],
                "preview_run_id": preview_run_id,
                "expires_at": selection_expires_at(),
            },
        )
        for pending_shape in pending_shapes:
            for text in ("确认", "取消"):
                with self.subTest(pending_type=pending_shape["type"], text=text):
                    pending_store = {"chat": dict(pending_shape)}
                    calls_before = len(self.project_entrypoints.calls)
                    with patch(
                        "feishu.bot.get_agent_core", return_value=FakeAgent()
                    ), patch.object(
                        message_handler,
                        "get_pending",
                        side_effect=lambda chat_id: pending_store.get(chat_id),
                    ), patch.object(
                        message_handler,
                        "clear_pending",
                        side_effect=lambda chat_id: pending_store.pop(chat_id, None),
                    ), patch.object(
                        message_handler,
                        "_reply_text",
                        side_effect=fake_reply,
                    ):
                        asyncio.run(
                            message_handler._process_and_reply(text, "other-user", "chat")
                        )
                    self.assertEqual(pending_shape, pending_store["chat"])
                    self.assertEqual(calls_before, len(self.project_entrypoints.calls))
                    self.assertIn("只有生成本次候选清单的用户", replies[-1])

    def test_pending_selection_expires_after_fifteen_minutes(self):
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
                    ttl_sec=900,
                )
            with patch.dict(os.environ, {"AGENT_PENDING_STATE_FILE": state_file}), patch.object(
                pending_actions.time, "time", return_value=1001.0
            ):
                self.assertIsNone(pending_actions.get_pending("split-timeout-chat"))


if __name__ == "__main__":
    unittest.main()
