from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.orchestration.models import OrchestrationError
from feishu import message_handler


class _FakeProjectEntrypoints:
    def __init__(
        self,
        *,
        status: str = "COMPLETED",
        results: list[dict | Exception] | None = None,
    ) -> None:
        self.status = status
        self.results = list(results or [])
        self.calls: list[dict] = []
        self.project_config = {
            "plate_numbers": ["ABC123", "XYZ789"],
            "class_name": "route-one",
            "departure_time_fixed": "21:30:00",
        }
        self.account_bindings = {
            "account_id": "business-primary",
            "daxiang_s_account_id": "business-secondary",
        }

    async def invoke_feishu(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return dict(result)
        return {
            "success": self.status == "COMPLETED",
            "status": self.status,
            "run_id": "run-feishu-one",
        }

    def describe_feishu_route(self, route_key):
        if not route_key:
            raise OrchestrationError("PROJECT_ROUTE_NOT_FOUND", "missing route")
        return SimpleNamespace(
            project_config=dict(self.project_config),
            account_bindings=dict(self.account_bindings),
        )

    def require_feishu_account_bindings(self, route_key, *roles):
        self.describe_feishu_route(route_key)
        return {role: self.account_bindings[role] for role in roles}


class _FakeAgent:
    def __init__(self, preview_result: dict | None = None) -> None:
        self.preview_result = preview_result or {"success": True, "data": {}}
        self.tool_calls: list[tuple[str, dict]] = []
        self.chat_calls: list[dict] = []
        self.registry = SimpleNamespace(
            get_capability=lambda _name: {"operation_type": "read"}
        )

    async def execute_tool(self, tool_name, params, **_kwargs):
        self.tool_calls.append((tool_name, dict(params)))
        return self.preview_result

    async def handle_message(self, **kwargs):
        self.chat_calls.append(dict(kwargs))
        return {"reply": message_handler.UNKNOWN_EXECUTION_REPLY}


def _reply_recorder(replies):
    async def record(_receive_id, text, receive_id_type="chat_id", **kwargs):
        replies.append(
            (
                str(text),
                {"receive_id_type": receive_id_type, **dict(kwargs)},
            )
        )

    return record


def _run_verified_text(text: str, *, event_id: str) -> None:
    asyncio.run(
        message_handler._handle_im_message_data(
            msg_type="text",
            chat_id="chat-one",
            sender_id="user-one",
            raw_content=json.dumps({"text": text}),
            event_id=event_id,
        )
    )


def _scan_preview(run_id: str) -> dict:
    observed_at = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "contract_version": 1,
        "preview_run_id": run_id,
        "target_date": observed_at.date().isoformat(),
        "observed_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(minutes=15)).isoformat(),
        "source_page_count": 2,
        "normalized_record_count": 18,
        "selection_count": 7,
        "batch_count": 1,
        "can_confirm": True,
    }


def test_direct_feishu_project_reports_waiting_approval_without_generic_execution():
    service = _FakeProjectEntrypoints(status="WAITING_APPROVAL")
    agent = _FakeAgent()
    replies = []
    request = {
        "tool_name": "sync_scan_codes",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.scan_codes",
        "dynamic_inputs": {},
    }
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("run scan now", event_id="event-waiting")

    assert agent.tool_calls == []
    assert agent.chat_calls == []
    assert service.calls[0]["event_id"] == "event-waiting"
    assert service.calls[0]["route_key"] == "builtin.scan_codes"
    assert "已开始生成扫描预览" in replies[0][0]
    assert "等待审批" in replies[-1][0]


def test_direct_feishu_project_explains_blocked_data_reason():
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": False,
                "status": "BLOCKED_DATA",
                "run_id": "run-blocked-data",
                "error_summary": "每日到货表写后读回不一致",
            }
        ]
    )
    replies = []
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="event-blocked-data",
            actor_id="user-one",
            chat_id="chat-one",
        )
    )
    try:
        with (
            patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
            patch.object(
                message_handler,
                "_reply_text",
                side_effect=_reply_recorder(replies),
            ),
        ):
            asyncio.run(
                message_handler._invoke_automation_project_and_reply(
                    route_key="builtin.arrival_stats",
                    dynamic_inputs={},
                    receive_id="chat-one",
                )
            )
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)

    assert "每日到货表写后读回不一致" in replies[-1][0]
    assert "统计到货数据任务执行失败" in replies[-1][0]
    assert "run-blocked-data" not in replies[-1][0]
    assert "Run" not in replies[-1][0]


def test_direct_feishu_project_explains_terminal_failure_without_internal_status():
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": False,
                "status": "FAILED_TERMINAL",
                "run_id": "run-terminal-failure",
                "error_summary": "分批结果表写后核验未通过",
            }
        ]
    )
    replies = []
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="event-terminal-failure",
            actor_id="user-one",
            chat_id="chat-one",
        )
    )
    try:
        with (
            patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
            patch.object(
                message_handler,
                "_reply_text",
                side_effect=_reply_recorder(replies),
            ),
        ):
            asyncio.run(
                message_handler._invoke_automation_project_and_reply(
                    route_key="builtin.split_pending_problem_upload",
                    dynamic_inputs={},
                    receive_id="chat-one",
                )
            )
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)

    assert "已开始执行：分批差错及问题件任务" in replies[0][0]
    assert "分批差错及问题件任务执行失败" in replies[-1][0]
    assert "分批结果表写后核验未通过" in replies[-1][0]
    assert "FAILED_TERMINAL" not in replies[-1][0]
    assert "run-terminal-failure" not in replies[-1][0]


def test_scan_preview_creates_volatile_pending_and_confirm_uses_new_event():
    preview_run_id = "11111111-1111-4111-8111-111111111111"
    formal_run_id = "22222222-2222-4222-8222-222222222222"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": preview_run_id,
                "scan_preview": _scan_preview(preview_run_id),
            },
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": formal_run_id,
                "error_code": None,
            },
        ]
    )
    agent = _FakeAgent()
    pending = {}
    pending_writes = []
    replies = []

    def set_pending(_chat_id, value, ttl_sec=600, *, persist=True):
        pending_writes.append((dict(value), ttl_sec, persist))
        pending.clear()
        pending.update(value)

    request = {
        "tool_name": "sync_scan_codes",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.scan_codes",
        "dynamic_inputs": {},
    }
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _chat_id: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("扫描", event_id="event-scan-preview")
        assert pending["type"] == "scan_preview_confirmation"
        assert pending["preview_run_id"] == preview_run_id
        assert pending["preview_event_id"] == "event-scan-preview"
        assert pending_writes[-1][2] is False
        assert "待扫描：7" in replies[-1][0]
        assert "确认扫描" in replies[-1][0]
        _run_verified_text("确认扫描", event_id="event-scan-confirm")

    assert service.calls[0]["preview_run_id"] is None
    assert service.calls[1]["preview_run_id"] == preview_run_id
    assert service.calls[1]["event_id"] == "event-scan-confirm"
    assert service.calls[1]["envelope"]["body"] == {}
    assert pending == {}
    assert "正式扫描已完成" in replies[-1][0]
    assert formal_run_id not in replies[-1][0]
    assert "Run" not in replies[-1][0]


def test_scan_preview_requires_explicit_cancel_phrase():
    preview_run_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": preview_run_id,
                "scan_preview": _scan_preview(preview_run_id),
            }
        ]
    )
    pending = {}
    replies = []

    def set_pending(_chat_id, value, ttl_sec=600, *, persist=True):
        del ttl_sec, persist
        pending.clear()
        pending.update(value)

    request = {
        "tool_name": "sync_scan_codes",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.scan_codes",
        "dynamic_inputs": {},
    }
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _chat_id: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("扫描", event_id="event-preview-cancel")
        _run_verified_text("取消", event_id="event-generic-cancel")
        assert pending["type"] == "scan_preview_confirmation"
        assert "确认扫描”或“取消扫描" in replies[-1][0]
        _run_verified_text("取消扫描", event_id="event-explicit-cancel")

    assert pending == {}
    assert "没有提交正式扫描" in replies[-1][0]
    assert len(service.calls) == 1


def test_unknown_scan_confirmation_locks_out_new_event_identity():
    preview_run_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": preview_run_id,
                "scan_preview": _scan_preview(preview_run_id),
            },
            RuntimeError("response lost"),
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": "22222222-2222-4222-8222-222222222222",
                "error_code": None,
            },
        ]
    )
    pending = {}
    replies = []

    def set_pending(_chat_id, value, ttl_sec=600, *, persist=True):
        del ttl_sec, persist
        pending.clear()
        pending.update(value)

    request = {
        "tool_name": "sync_scan_codes",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.scan_codes",
        "dynamic_inputs": {},
    }
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _chat_id: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("扫描", event_id="event-preview-unknown")
        _run_verified_text("确认扫描", event_id="event-confirm-unknown")
        assert pending["confirmation_state"] == "unknown"
        assert pending["confirmation_event_id"] == "event-confirm-unknown"
        asyncio.run(
            message_handler._handle_menu_action(
                event_key="scan",
                receive_id="user-one",
                receive_id_type="open_id",
                event_id="event-menu-after-unknown",
            )
        )
        assert len(service.calls) == 2
        assert "结果暂时无法确定" in replies[-1][0]
        _run_verified_text("登录", event_id="event-login-blocked")
        assert pending["confirmation_state"] == "unknown"
        assert "事项中心查看原任务" in replies[-1][0]
        _run_verified_text("确认扫描", event_id="event-confirm-new")
        assert len(service.calls) == 2
        assert "本次没有创建新请求" in replies[-1][0]
        _run_verified_text("确认扫描", event_id="event-confirm-unknown")

    assert len(service.calls) == 3
    assert service.calls[1]["event_id"] == service.calls[2]["event_id"]
    assert pending == {}
    assert "正式扫描已完成" in replies[-1][0]


def test_consumed_scan_preview_blocks_new_preview_in_same_pending_state():
    preview_run_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": preview_run_id,
                "scan_preview": _scan_preview(preview_run_id),
            },
            OrchestrationError(
                "SCAN_PREVIEW_ALREADY_CONSUMED",
                "already consumed",
            ),
        ]
    )
    pending = {}
    replies = []

    def set_pending(_chat_id, value, ttl_sec=600, *, persist=True):
        del ttl_sec, persist
        pending.clear()
        pending.update(value)

    request = {
        "tool_name": "sync_scan_codes",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.scan_codes",
        "dynamic_inputs": {},
    }
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _chat_id: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("扫描", event_id="event-preview-consumed")
        _run_verified_text("确认扫描", event_id="event-confirm-consumed")
        assert pending["confirmation_state"] == "terminal"
        assert pending["terminal_error_code"] == "SCAN_PREVIEW_ALREADY_CONSUMED"
        asyncio.run(
            message_handler._handle_menu_action(
                event_key="scan",
                receive_id="user-one",
                receive_id_type="open_id",
                event_id="event-menu-after-terminal",
            )
        )
        assert len(service.calls) == 2
        assert "事项中心查看原任务" in replies[-1][0]
        _run_verified_text("扫描", event_id="event-new-preview-blocked")

    assert len(service.calls) == 2
    assert "事项中心查看原任务" in replies[-1][0]


def test_scan_confirmation_reply_failure_keeps_event_lock_for_exact_replay():
    preview_run_id = "11111111-1111-4111-8111-111111111111"
    formal_run_id = "22222222-2222-4222-8222-222222222222"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": preview_run_id,
                "scan_preview": _scan_preview(preview_run_id),
            },
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": formal_run_id,
                "error_code": None,
            },
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": formal_run_id,
                "error_code": None,
            },
        ]
    )
    pending = {}
    replies = []
    fail_formal_reply_once = True

    def set_pending(_chat_id, value, ttl_sec=600, *, persist=True):
        del ttl_sec, persist
        pending.clear()
        pending.update(value)

    async def reply_text(receive_id, text, **kwargs):
        nonlocal fail_formal_reply_once
        if text == "正式扫描已完成。" and fail_formal_reply_once:
            fail_formal_reply_once = False
            raise RuntimeError("reply lost")
        replies.append((text, kwargs))
        return {"ok": True}

    request = {
        "tool_name": "sync_scan_codes",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.scan_codes",
        "dynamic_inputs": {},
    }
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _key: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _key, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=reply_text),
    ):
        _run_verified_text("扫描", event_id="event-preview-reply-loss")
        with pytest.raises(RuntimeError, match="reply lost"):
            _run_verified_text("确认扫描", event_id="event-confirm-reply-loss")
        assert pending["confirmation_state"] == "submitting"
        assert pending["confirmation_event_id"] == "event-confirm-reply-loss"
        _run_verified_text("确认扫描", event_id="event-confirm-different")
        assert len(service.calls) == 2
        _run_verified_text("确认扫描", event_id="event-confirm-reply-loss")

    assert len(service.calls) == 3
    assert service.calls[1]["event_id"] == service.calls[2]["event_id"]
    assert pending == {}
    assert "正式扫描已完成" in replies[-1][0]
    assert formal_run_id not in replies[-1][0]


def test_scan_projection_with_private_field_is_rejected_without_pending():
    preview_run_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": preview_run_id,
                "scan_preview": {
                    **_scan_preview(preview_run_id),
                    "selection_sha256": "a" * 64,
                },
            }
        ]
    )
    pending_writes = []
    replies = []
    request = {
        "tool_name": "sync_scan_codes",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.scan_codes",
        "dynamic_inputs": {},
    }
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "set_pending", side_effect=lambda *args, **kwargs: pending_writes.append((args, kwargs))),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("扫描", event_id="event-private-preview")

    assert pending_writes == []
    assert "预览返回无效" in replies[-1][0]


def test_scan_menu_pending_is_confirmed_from_the_users_next_chat_message():
    preview_run_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": preview_run_id,
                "scan_preview": _scan_preview(preview_run_id),
            },
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": "22222222-2222-4222-8222-222222222222",
                "error_code": None,
            },
        ]
    )
    pending_store = {}
    replies = []

    def set_pending(key, value, ttl_sec=600, *, persist=True):
        del ttl_sec, persist
        pending_store[key] = dict(value)

    def clear_pending(key, **_kwargs):
        return pending_store.pop(key, None)

    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "get_pending", side_effect=lambda key: pending_store.get(key)),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=clear_pending),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        asyncio.run(
            message_handler._handle_menu_action(
                event_key="scan",
                receive_id="user-one",
                receive_id_type="open_id",
                event_id="event-menu-preview",
            )
        )
        assert pending_store["user-one"]["preview_run_id"] == preview_run_id
        asyncio.run(
            message_handler._handle_menu_action(
                event_key="scan",
                receive_id="user-one",
                receive_id_type="open_id",
                event_id="event-menu-reentry-blocked",
            )
        )
        assert len(service.calls) == 1
        assert "没有创建新预览" in replies[-1][0]
        _run_verified_text("确认扫描", event_id="event-menu-confirm")

    assert service.calls[1]["event_id"] == "event-menu-confirm"
    assert service.calls[1]["preview_run_id"] == preview_run_id
    assert pending_store == {}


def test_sender_scan_pending_takes_priority_over_existing_chat_pending():
    preview_run_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": preview_run_id,
                "scan_preview": _scan_preview(preview_run_id),
            },
            {
                "success": True,
                "status": "COMPLETED",
                "run_id": "22222222-2222-4222-8222-222222222222",
                "error_code": None,
            },
        ]
    )
    existing_chat_pending = {"type": "active_run", "tool_name": "legacy-task"}
    pending_store = {"chat-one": dict(existing_chat_pending)}
    replies = []

    def set_pending(key, value, ttl_sec=600, *, persist=True):
        del ttl_sec, persist
        pending_store[key] = dict(value)

    def clear_pending(key, **_kwargs):
        return pending_store.pop(key, None)

    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "get_pending", side_effect=lambda key: pending_store.get(key)),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=clear_pending),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        asyncio.run(
            message_handler._handle_menu_action(
                event_key="scan",
                receive_id="user-one",
                receive_id_type="open_id",
                event_id="event-menu-with-chat-pending",
            )
        )
        assert pending_store["chat-one"] == existing_chat_pending
        assert pending_store["user-one"]["preview_run_id"] == preview_run_id
        _run_verified_text("确认扫描", event_id="event-confirm-with-chat-pending")

    assert service.calls[1]["preview_run_id"] == preview_run_id
    assert pending_store == {"chat-one": existing_chat_pending}
    assert "正式扫描已完成" in replies[-1][0]


def test_feishu_webhook_and_websocket_keep_the_same_event_identity():
    service = _FakeProjectEntrypoints()
    agent = _FakeAgent()
    replies = []
    scheduled = []
    submitted = []
    request = {
        "tool_name": "sync_scan_codes",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.scan_codes",
        "dynamic_inputs": {},
    }
    event_body = {
        "message": {
            "message_type": "text",
            "chat_id": "chat-one",
            "content": json.dumps({"text": "run scan now"}),
        },
        "sender": {"sender_id": {"open_id": "user-one"}},
    }
    sdk_event = SimpleNamespace(
        header=SimpleNamespace(event_id="event-duplicate"),
        event=SimpleNamespace(
            message=SimpleNamespace(**event_body["message"]),
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="user-one")
            ),
        ),
    )
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
        patch.object(message_handler, "_schedule_local_task", side_effect=scheduled.append),
        patch.object(message_handler, "_submit_with_future_callback", side_effect=submitted.append),
    ):
        assert message_handler.queue_im_message_payload(
            event_body,
            event_id="event-duplicate",
        )
        message_handler.handle_im_message(sdk_event)
        asyncio.run(scheduled.pop())
        asyncio.run(submitted.pop())

    assert [call["event_id"] for call in service.calls] == [
        "event-duplicate",
        "event-duplicate",
    ]
    assert all(call["sender_id"] == "user-one" for call in service.calls)


def test_self_pickup_preview_uses_committed_accounts_and_confirmation_uses_route():
    service = _FakeProjectEntrypoints()
    agent = _FakeAgent(
        {
            "success": True,
            "data": {
                "stage": "dry_run",
                "candidate_count": 2,
                "candidates": [
                    {"bill_code": "R_SELF"},
                    {"bill_code": "R_DX_PICK"},
                ],
                "preview_fingerprint": "f" * 64,
                "source_summaries": [],
            },
        }
    )
    pending = {}
    replies = []
    request = {
        "tool_name": "preview_self_pickup_problems",
        "params": {},
        "mode": "automation_preview",
        "automation_route_key": "builtin.self_pickup_problem_upload",
        "dynamic_inputs": {},
        "confirm_intent": {
            "dynamic_inputs": {"dry_run": False},
            "description": "self pickup",
        },
    }

    def set_pending(_chat_id, value, ttl_sec=600):
        del ttl_sec
        pending.clear()
        pending.update(value)

    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _chat_id: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("preview-command", event_id="event-preview")
        assert agent.tool_calls == [
            (
                "preview_self_pickup_problems",
                {
                    "account_id": "business-primary",
                    "daxiang_s_account_id": "business-secondary",
                },
            )
        ]
        assert pending["automation_route_key"] == "builtin.self_pickup_problem_upload"
        assert not message_handler._contains_account_override(pending)
        _run_verified_text("yes", event_id="event-confirm")

    assert service.calls[-1]["event_id"] == "event-confirm"
    assert service.calls[-1]["envelope"]["body"] == {
        "dry_run": False,
        "selected_bill_codes": ["R_SELF", "R_DX_PICK"],
        "preview_fingerprint": "f" * 64,
    }


def test_split_preview_selection_and_confirmation_preserve_signed_dynamic_fields():
    service = _FakeProjectEntrypoints()
    agent = _FakeAgent(
        {
            "success": True,
            "data": {
                "stage": "dry_run",
                "candidate_count": 1,
                "hidden_completed_count": 0,
                "preview_fingerprint": "f" * 64,
                "candidates": [{"bill_code": "R001", "problem_type": "split"}],
            },
        }
    )
    pending = {}
    replies = []
    request = {
        "tool_name": "preview_split_pending_problems",
        "params": {},
        "mode": "automation_preview",
        "automation_route_key": "builtin.split_pending_problem_upload",
        "dynamic_inputs": {},
        "selection_intent": {"description": "split"},
    }

    def set_pending(_chat_id, value, ttl_sec=600):
        del ttl_sec
        pending.clear()
        pending.update(value)

    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _chat_id: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("split-command", event_id="event-split-preview")
        assert pending["type"] == "split_pending_selection"
        assert not message_handler._contains_account_override(pending)
        _run_verified_text("yes", event_id="event-split-confirm")

    assert service.calls[-1]["envelope"]["body"] == {
        "dry_run": False,
        "selected_bill_codes": ["R001"],
        "preview_fingerprint": "f" * 64,
    }


def test_r7_plate_choice_rechecks_committed_config_before_typed_invocation():
    service = _FakeProjectEntrypoints()
    agent = _FakeAgent()
    pending = {}
    replies = []
    request = {
        "tool_name": "r7_departure_checkin",
        "params": {},
        "mode": "r7_departure_choice",
        "automation_route_key": "builtin.r7_departure_checkin",
        "dynamic_inputs": {},
    }

    def set_pending(_chat_id, value, ttl_sec=600):
        del ttl_sec
        pending.clear()
        pending.update(value)

    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _chat_id: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("departure-command", event_id="event-plate-choice")
        assert pending["plate_numbers"] == ["ABC123", "XYZ789"]
        assert "params" not in pending
        _run_verified_text("ABC123", event_id="event-plate-selected")

    assert service.calls[-1]["envelope"]["body"] == {
        "plate_numbers": ["ABC123"]
    }


def test_account_override_and_unverified_generic_text_cannot_forge_project_context():
    service = _FakeProjectEntrypoints()
    replies = []
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="event-spoof",
            actor_id="user-one",
            chat_id="chat-one",
        )
    )
    try:
        with (
            patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
            patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
        ):
            asyncio.run(
                message_handler._invoke_automation_project_and_reply(
                    route_key="builtin.scan_codes",
                    dynamic_inputs={"account_id": "attacker-account"},
                    receive_id="chat-one",
                )
            )
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)

    assert service.calls == []
    assert "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN" in replies[-1][0]


def test_signed_custom_menu_route_is_typed_and_never_resolved_by_tool_id():
    service = _FakeProjectEntrypoints()
    replies = []
    with (
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        asyncio.run(
            message_handler._handle_menu_action(
                event_key="automation:tenant.scan.second",
                receive_id="user-one",
                receive_id_type="open_id",
                event_id="event-menu",
            )
        )

    assert service.calls[0]["route_key"] == "tenant.scan.second"
    assert service.calls[0]["event_id"] == "event-menu"
