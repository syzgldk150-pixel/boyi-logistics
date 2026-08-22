from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from agent.orchestration.models import OrchestrationError
from feishu import message_handler


class _FakeProjectEntrypoints:
    def __init__(self, *, status: str = "COMPLETED") -> None:
        self.status = status
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
    assert "等待审批" in replies[-1][0]


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
                "candidate_count": 0,
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
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id: pending.clear()),
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
    assert service.calls[-1]["envelope"]["body"] == {"dry_run": False}


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
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id: pending.clear()),
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
        patch.object(message_handler, "clear_pending", side_effect=lambda _chat_id: pending.clear()),
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
