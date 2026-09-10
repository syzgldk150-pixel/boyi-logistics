from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.orchestration.models import OrchestrationError
from feishu import message_handler
from feishu.invocation_results import InvocationResultFollower


class _FakeProjectEntrypoints:
    def __init__(
        self,
        *,
        status: str = "COMPLETED",
        results: list[dict | Exception] | None = None,
        accepted_errors: dict[int, Exception] | None = None,
    ) -> None:
        self.status = status
        self.results = list(results or [])
        self.accepted_errors = dict(accepted_errors or {})
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
        accepted_error = self.accepted_errors.get(len(self.calls))
        if accepted_error is not None:
            on_accepted = kwargs.get("on_accepted")
            if on_accepted is not None:
                await on_accepted(SimpleNamespace(run_id="run-accepted"))
            raise accepted_error
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            on_accepted = kwargs.get("on_accepted")
            if on_accepted is not None:
                await on_accepted(SimpleNamespace(run_id=result.get("invocation_id", "")))
            return dict(result)
        on_accepted = kwargs.get("on_accepted")
        if on_accepted is not None:
            await on_accepted(SimpleNamespace(run_id="run-feishu-one"))
        return {
            "success": self.status == "COMPLETED",
            "status": self.status,
            "invocation_id": "run-feishu-one",
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


class _FakeServiceV2FeishuDispatcher:
    def __init__(
        self,
        *,
        result: dict | None = None,
        error: Exception | None = None,
        acceptance_notifications: int = 1,
        error_after_acceptance: bool = False,
    ) -> None:
        self.result = result
        self.error = error
        self.acceptance_notifications = acceptance_notifications
        self.error_after_acceptance = error_after_acceptance
        self.calls: list[dict] = []

    async def dispatch(self, **kwargs):
        on_accepted = kwargs.pop("on_accepted", None)
        self.calls.append(dict(kwargs))
        if self.error is not None and not self.error_after_acceptance:
            raise self.error
        if (self.result is not None or self.error_after_acceptance) and on_accepted is not None:
            for _ in range(self.acceptance_notifications):
                await on_accepted(SimpleNamespace(run_id="run-service-v2"))
        if self.error is not None:
            raise self.error
        return self.result


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
        "contract_version": 3,
        "automation_id": "scan_codes",
        "preview_invocation_id": run_id,
        "target_date": observed_at.date().isoformat(),
        "observed_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(minutes=15)).isoformat(),
        "source_page_count": 2,
        "normalized_record_count": 18,
        "selection_count": 7,
        "batch_count": 1,
        "can_confirm": True,
        "preview_state": "AVAILABLE",
    }


def _selection_preview(
    run_id: str,
    automation_id: str,
    candidates: list[dict],
    summary: dict,
) -> dict:
    observed_at = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "contract_version": 3,
        "automation_id": automation_id,
        "title": automation_id,
        "preview_invocation_id": run_id,
        "observed_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(minutes=15)).isoformat(),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "summary": summary,
        "can_confirm": True,
        "preview_state": "AVAILABLE",
    }


def test_service_v2_feishu_dispatches_verified_exact_context_and_hides_internal_ids():
    dispatcher = _FakeServiceV2FeishuDispatcher(
        result={
            "success": False,
            "status": "FAILED_TERMINAL",
            "automation_id": "private-automation",
            "service": "private.service",
            "operation": "private.operation",
            "contribution_id": "private.contribution",
            "invocation_id": "11111111-1111-4111-8111-111111111111",
            "error_summary": "private.service/private.operation failed",
        }
    )
    agent = _FakeAgent()
    replies = []
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", None),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=None),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("插件只读日报", event_id="event-dynamic-one")

    assert dispatcher.calls == [
        {
            "command_text": "插件只读日报",
            "event_id": "event-dynamic-one",
            "sender_id": "user-one",
            "chat_id": "chat-one",
        }
    ]
    assert agent.chat_calls == []
    assert replies[0][1]["reply_type"] == "service_v2_feishu_started"
    assert "已开始执行" in replies[0][0]
    assert replies[-1][0] == (
        "扩展任务执行失败：执行未完成，请在自动化页面查看处理建议。"
    )
    public_reply = replies[-1][0]
    for private_value in (
        "private-automation",
        "private.service",
        "private.operation",
        "private.contribution",
        "11111111-1111-4111-8111-111111111111",
    ):
        assert private_value not in public_reply


def test_service_v2_feishu_unknown_command_continues_to_existing_agent_path():
    dispatcher = _FakeServiceV2FeishuDispatcher(result=None)
    agent = _FakeAgent()
    replies = []
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", None),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=None),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("普通未知消息", event_id="event-unknown-one")

    assert dispatcher.calls[0]["command_text"] == "普通未知消息"
    assert len(agent.chat_calls) == 1
    assert replies[-1][0] == message_handler.UNKNOWN_EXECUTION_REPLY


def test_service_v2_feishu_deduplicates_acceptance_and_sends_one_final_reply():
    dispatcher = _FakeServiceV2FeishuDispatcher(
        result={"success": True, "status": "COMPLETED"},
        acceptance_notifications=2,
    )
    replies = []
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", None),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=None),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("插件只读日报", event_id="event-dynamic-dedup")

    assert [reply[1]["reply_type"] for reply in replies] == [
        "service_v2_feishu_started",
        "automation_project_completed",
    ]


def test_service_v2_post_acceptance_wait_failure_never_claims_not_submitted():
    dispatcher = _FakeServiceV2FeishuDispatcher(
        error=OrchestrationError("RUN_WAIT_TIMEOUT", "synthetic wait timeout"),
        error_after_acceptance=True,
    )
    replies = []
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", None),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=None),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("插件只读日报", event_id="event-dynamic-timeout")

    assert [reply[1]["reply_type"] for reply in replies] == [
        "service_v2_feishu_started",
        "service_v2_feishu_result_pending",
    ]
    assert "已发起" in replies[-1][0]
    assert "未能执行" not in replies[-1][0]
    assert "重试" not in replies[-1][0]


def test_fixed_action_v1_command_wins_without_calling_dynamic_dispatcher():
    service = _FakeProjectEntrypoints()
    dispatcher = _FakeServiceV2FeishuDispatcher(
        error=AssertionError("dynamic dispatcher must not run")
    )
    agent = _FakeAgent()
    request = {
        "tool_name": "sync_arrival_stats",
        "params": {},
        "mode": "automation_project",
        "automation_route_key": "builtin.arrival_stats",
        "dynamic_inputs": {},
    }
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", None),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder([])),
    ):
        _run_verified_text("统计", event_id="event-fixed-one")

    assert len(service.calls) == 1
    assert dispatcher.calls == []
    assert agent.chat_calls == []


def test_service_v2_feishu_match_without_verified_context_stops_before_agent():
    dispatcher = _FakeServiceV2FeishuDispatcher(
        error=OrchestrationError(
            "STABLE_EVENT_ID_REQUIRED",
            "A stable verified Feishu event id is required",
        )
    )
    agent = _FakeAgent()
    replies = []
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", None),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=None),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        asyncio.run(
            message_handler._process_and_reply(
                "插件只读日报",
                "unverified-user",
                "unverified-chat",
            )
        )

    assert dispatcher.calls == [
        {
            "command_text": "插件只读日报",
            "event_id": "",
            "sender_id": "",
            "chat_id": "",
        }
    ]
    assert agent.chat_calls == []
    assert replies[-1][0] == "扩展任务未能执行：消息身份不完整或当前入口不可用。"


def test_service_v2_feishu_verified_event_without_sender_keeps_identity_empty():
    dispatcher = _FakeServiceV2FeishuDispatcher(
        error=OrchestrationError(
            "STABLE_SENDER_ID_REQUIRED",
            "A stable verified Feishu sender id is required",
        )
    )
    agent = _FakeAgent()
    replies = []
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", None),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=None),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        asyncio.run(
            message_handler._handle_im_message_data(
                msg_type="text",
                chat_id="chat-one",
                sender_id="",
                raw_content=json.dumps({"text": "插件只读日报"}),
                event_id="event-without-sender",
            )
        )

    assert dispatcher.calls == [
        {
            "command_text": "插件只读日报",
            "event_id": "event-without-sender",
            "sender_id": "",
            "chat_id": "chat-one",
        }
    ]
    assert agent.chat_calls == []
    assert replies[-1][0] == "扩展任务未能执行：消息身份不完整或当前入口不可用。"


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


def test_direct_feishu_project_rejection_does_not_claim_execution_started():
    service = _FakeProjectEntrypoints(
        results=[
            OrchestrationError(
                "AUTOMATION_ALREADY_RUNNING",
                "该脚本已在运行",
                details={
                    "blocking_kind": "UNKNOWN_WRITE",
                    "active_run_id": "private-run",
                    "active_status": "BLOCKED_DATA",
                },
            )
        ]
    )
    replies = []
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="event-already-running",
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

    assert len(replies) == 1
    assert "本次执行未开始" in replies[0][0]
    assert "已开始" not in replies[0][0]
    assert "private-run" not in replies[0][0]


@pytest.mark.parametrize("wait_error", [
    OrchestrationError("RUN_WAIT_TIMEOUT", "synthetic wait timeout"),
    TimeoutError("synthetic result read timeout"),
    RuntimeError("synthetic result storage unavailable"),
])
def test_direct_project_post_acceptance_wait_failure_reports_background_run(wait_error):
    service = _FakeProjectEntrypoints(
        accepted_errors={
            1: wait_error
        }
    )
    replies = []
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="event-accepted-timeout",
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

    assert [reply[1]["reply_type"] for reply in replies] == [
        "automation_project_started",
        "automation_project_result_pending",
    ]
    assert "已发起" in replies[-1][0]
    assert "未提交" not in replies[-1][0]
    assert "重试" not in replies[-1][0]


def test_direct_feishu_project_rejection_distinguishes_blocking_kinds():
    expected = {
        "ACTIVE": "本次执行未开始",
        "RETRY_PENDING": "本次执行未开始",
        "UNKNOWN_WRITE": "本次执行未开始",
        "NEEDS_ATTENTION": "本次执行未开始",
    }
    for blocking_kind, phrase in expected.items():
        service = _FakeProjectEntrypoints(
            results=[
                OrchestrationError(
                    "AUTOMATION_ALREADY_RUNNING",
                    "该脚本存在未结束任务",
                    details={
                        "blocking_kind": blocking_kind,
                        "active_run_id": "private-run",
                    },
                )
            ]
        )
        replies = []
        token = message_handler._COMMAND_CONTEXT.set(
            message_handler.FeishuCommandContext(
                event_id=f"event-{blocking_kind.lower()}",
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

        assert phrase in replies[0][0]
        assert "已开始" not in replies[0][0]
        assert "private-run" not in replies[0][0]


def test_selection_preview_fails_once_without_queuing_or_retry() -> None:
    preview_invocation_id = "22222222-2222-4222-8222-222222222222"
    service = _FakeProjectEntrypoints(
        results=[
            OrchestrationError(
                "AUTOMATION_ALREADY_RUNNING",
                "该脚本仍在运行",
                details={"blocking_kind": "ACTIVE"},
            ),
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "selection_preview": _selection_preview(
                    preview_invocation_id,
                    "split_pending_problem_upload",
                    [],
                    {
                        "complete_count": 0,
                        "hidden_completed_count": 0,
                        "split_count": 0,
                        "pending_count": 0,
                    },
                ),
            },
        ]
    )
    replies = []
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="event-queued-preview",
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
            patch.object(message_handler.asyncio, "sleep", return_value=None) as sleep,
        ):
            result = asyncio.run(
                message_handler._invoke_selection_preview_and_reply(
                    route_key="builtin.split_pending_problem_upload",
                    tool_name="preview_split_pending_problems",
                    receive_id="chat-one",
                )
            )
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)

    assert result is None
    assert len(service.calls) == 1
    sleep.assert_not_awaited()
    assert [reply[1]["reply_type"] for reply in replies] == ["automation_preview_rejected"]
    assert "已排队" not in replies[-1][0]
    assert "本次候选读取未完成" in replies[-1][0]


@pytest.mark.parametrize("wait_error", [
    OrchestrationError("RUN_WAIT_TIMEOUT", "synthetic wait timeout"),
    TimeoutError("synthetic result read timeout"),
    RuntimeError("synthetic result storage unavailable"),
])
def test_selection_preview_post_acceptance_wait_failure_does_not_invite_replay(wait_error):
    service = _FakeProjectEntrypoints(
        accepted_errors={
            1: wait_error
        }
    )
    replies = []
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="event-selection-timeout",
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
            result = asyncio.run(
                message_handler._invoke_selection_preview_and_reply(
                    route_key="builtin.split_pending_problem_upload",
                    tool_name="split_pending_problem_upload",
                    receive_id="chat-one",
                )
            )
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)

    assert result is None
    assert [reply[1]["reply_type"] for reply in replies] == [
        "selection_preview_started",
        "automation_preview_result_pending",
    ]
    assert "已发起" in replies[-1][0]
    assert "重试" not in replies[-1][0]


def test_selection_preview_unknown_write_is_not_queued_or_hidden() -> None:
    service = _FakeProjectEntrypoints(
        results=[
            OrchestrationError(
                "AUTOMATION_ALREADY_RUNNING",
                "旧写入结果未知",
                details={"blocking_kind": "UNKNOWN_WRITE"},
            )
        ]
    )
    replies = []
    token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="event-unknown-preview",
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
            patch.object(message_handler.asyncio, "sleep", return_value=None) as sleep,
        ):
            result = asyncio.run(
                message_handler._invoke_selection_preview_and_reply(
                    route_key="builtin.split_pending_problem_upload",
                    tool_name="preview_split_pending_problems",
                    receive_id="chat-one",
                )
            )
    finally:
        message_handler._COMMAND_CONTEXT.reset(token)

    assert result is None
    assert len(service.calls) == 1
    sleep.assert_not_awaited()
    assert all(
        reply[1]["reply_type"] != "selection_preview_started"
        for reply in replies
    )
    assert "本次候选读取未完成" in replies[-1][0]
    assert replies[-1][1]["reply_type"] == "automation_preview_rejected"


def test_direct_feishu_project_explains_blocked_data_reason():
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": False,
                "status": "BLOCKED_DATA",
                "invocation_id": "run-blocked-data",
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
                "invocation_id": "run-terminal-failure",
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

    assert "已开始执行：分批问题件任务" in replies[0][0]
    assert "分批问题件任务执行失败" in replies[-1][0]
    assert "分批结果表写后核验未通过" in replies[-1][0]
    assert "FAILED_TERMINAL" not in replies[-1][0]
    assert "run-terminal-failure" not in replies[-1][0]


def test_scan_preview_creates_volatile_pending_and_confirm_uses_new_event():
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    formal_run_id = "22222222-2222-4222-8222-222222222222"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": _scan_preview(preview_invocation_id),
            },
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": formal_run_id,
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
        assert pending["preview_invocation_id"] == preview_invocation_id
        assert pending["preview_event_id"] == "event-scan-preview"
        assert pending_writes[-1][2] is False
        assert "待扫描：7" in replies[-1][0]
        assert "确认扫描" in replies[-1][0]
        _run_verified_text("确认扫描", event_id="event-scan-confirm")

    assert service.calls[0]["preview_invocation_id"] is None
    assert service.calls[1]["preview_invocation_id"] == preview_invocation_id
    assert service.calls[1]["event_id"] == "event-scan-confirm"
    assert service.calls[1]["envelope"]["body"] == {}
    assert pending == {}
    assert "正式扫描已完成" in replies[-1][0]
    assert formal_run_id not in replies[-1][0]
    assert "Run" not in replies[-1][0]


def test_scan_preview_requires_explicit_cancel_phrase():
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": _scan_preview(preview_invocation_id),
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
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": _scan_preview(preview_invocation_id),
            },
            RuntimeError("response lost"),
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": "22222222-2222-4222-8222-222222222222",
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
        _run_verified_text("确认扫描", event_id="event-confirm-new")
        assert len(service.calls) == 2
        assert "本次没有创建新请求" in replies[-1][0]
        _run_verified_text("确认扫描", event_id="event-confirm-unknown")

    assert len(service.calls) == 3
    assert service.calls[1]["event_id"] == service.calls[2]["event_id"]
    assert pending == {}
    assert "正式扫描已完成" in replies[-1][0]


def test_scan_confirmation_post_acceptance_timeout_reports_background_run():
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": _scan_preview(preview_invocation_id),
            }
        ],
        accepted_errors={
            2: OrchestrationError("RUN_WAIT_TIMEOUT", "synthetic wait timeout")
        },
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
        patch.object(
            message_handler,
            "clear_pending",
            side_effect=lambda _chat_id, **_kwargs: pending.clear(),
        ),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("扫描", event_id="event-preview-accepted-timeout")
        _run_verified_text("确认扫描", event_id="event-confirm-accepted-timeout")

    assert pending["confirmation_state"] == "unknown"
    assert pending["confirmation_event_id"] == "event-confirm-accepted-timeout"
    assert [reply[1]["reply_type"] for reply in replies][-2:] == [
        "scan_preview_formal_started",
        "scan_preview_confirmation_result_pending",
    ]
    assert "正式扫描已发起" in replies[-1][0]
    assert "重试" not in replies[-1][0]


@pytest.mark.parametrize("initial", ["active", "read_error"])
@pytest.mark.parametrize("status, final_text", [("COMPLETED", "正式扫描已完成"), ("FAILED", "正式扫描执行失败"), ("CANCELLED", "正式扫描已取消")])
def test_formal_scan_waits_past_initial_window_and_replies_once_with_actual_terminal_result(initial, status, final_text):
    preview_id = "11111111-1111-4111-8111-111111111111"
    formal_id = "22222222-2222-4222-8222-222222222222"
    receipt = {"invocation_id": formal_id, "automation_id": "scan_codes", "status": "RUNNING"}
    pending = {}
    replies = []

    class DelayedResultEntrypoints(_FakeProjectEntrypoints):
        async def invoke_feishu(self, **kwargs):
            self.calls.append(kwargs)
            if not kwargs["preview_invocation_id"]:
                return {"status": "COMPLETED", "invocation_id": preview_id, "scan_preview": _scan_preview(preview_id)}
            await kwargs["on_accepted"](receipt)
            assert replies[-1][1]["reply_type"] == "scan_preview_formal_started"
            if initial == "read_error":
                raise RuntimeError("temporary storage failure")
            return receipt

    service = DelayedResultEntrypoints()

    async def late_result(**kwargs):
        assert kwargs["invocation_id"] == formal_id
        assert kwargs["automation_id"] == "scan_codes"
        assert kwargs["event_id"] == "event-late-confirm"
        assert pending["confirmation_state"] == "submitting"
        assert replies[-1][1]["reply_type"] == "scan_preview_confirmation_waiting_result"
        return {**receipt, "status": status, "success": status == "COMPLETED"}

    service.wait_feishu_invocation = AsyncMock(side_effect=late_result)

    def set_pending(_chat_id, value, ttl_sec=600, *, persist=True):
        del ttl_sec, persist
        pending.clear()
        pending.update(value)

    request = {"tool_name": "sync_scan_codes", "params": {}, "mode": "automation_project", "automation_route_key": "builtin.scan_codes", "dynamic_inputs": {}}
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "_INVOCATION_RESULTS", InvocationResultFollower(retry_delay=0)),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", side_effect=lambda _key: pending or None),
        patch.object(message_handler, "set_pending", side_effect=set_pending),
        patch.object(message_handler, "clear_pending", side_effect=lambda _key, **_kwargs: pending.clear()),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("扫描", event_id="event-late-preview")
        _run_verified_text("确认扫描", event_id="event-late-confirm")

    assert len(service.calls) == 2  # One preview, one formal submission; reads never invoke again.
    assert service.wait_feishu_invocation.await_count == 1
    assert pending == {}
    assert final_text in replies[-1][0]
    assert sum(final_text in text for text, _kwargs in replies) == 1
    assert formal_id not in str(replies)


@pytest.mark.parametrize("dynamic", [False, True])
def test_fixed_and_dynamic_commands_follow_results_after_initial_wait(dynamic):
    receipt = {"invocation_id": "33333333-3333-4333-8333-333333333333", "automation_id": "arrival_stats", "status": "RUNNING"}
    replies = []
    calls = []

    async def accept(**kwargs):
        calls.append(kwargs)
        await kwargs["on_accepted"](receipt)
        return receipt

    service = _FakeProjectEntrypoints()
    service.invoke_feishu = accept
    service.wait_feishu_invocation = AsyncMock(return_value={**receipt, "status": "COMPLETED"})
    dispatcher = SimpleNamespace(dispatch=accept)
    request = None if dynamic else {"tool_name": "sync_arrival_stats", "params": {}, "mode": "automation_project", "automation_route_key": "builtin.arrival_stats", "dynamic_inputs": {}}
    with (
        patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher if dynamic else None),
        patch.object(message_handler, "_INVOCATION_RESULTS", InvocationResultFollower(retry_delay=0)),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("扩展统计" if dynamic else "统计", event_id="event-late-statistics")

    assert len(calls) == service.wait_feishu_invocation.await_count == 1
    assert "已完成" in replies[-1][0]
    assert sum("已完成" in text for text, _kwargs in replies) == 1
    assert receipt["invocation_id"] not in str(replies)


def test_consumed_scan_preview_does_not_block_new_explicit_trigger():
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": _scan_preview(preview_invocation_id),
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
        _run_verified_text("扫描", event_id="event-new-preview")

    assert len(service.calls) == 3
    assert service.calls[-1]["event_id"] == "event-new-preview"
    assert service.calls[-1]["preview_invocation_id"] is None


def test_scan_confirmation_reply_failure_keeps_event_lock_for_exact_replay():
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    formal_run_id = "22222222-2222-4222-8222-222222222222"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": _scan_preview(preview_invocation_id),
            },
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": formal_run_id,
                "error_code": None,
            },
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": formal_run_id,
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
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": {
                    **_scan_preview(preview_invocation_id),
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
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": _scan_preview(preview_invocation_id),
            },
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": "22222222-2222-4222-8222-222222222222",
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
        assert pending_store["user-one"]["preview_invocation_id"] == preview_invocation_id
        _run_verified_text("确认扫描", event_id="event-menu-confirm")

    assert service.calls[1]["event_id"] == "event-menu-confirm"
    assert service.calls[1]["preview_invocation_id"] == preview_invocation_id
    assert pending_store == {}


def test_sender_scan_pending_takes_priority_over_existing_chat_pending():
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "scan_preview": _scan_preview(preview_invocation_id),
            },
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": "22222222-2222-4222-8222-222222222222",
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
        assert pending_store["user-one"]["preview_invocation_id"] == preview_invocation_id
        _run_verified_text("确认扫描", event_id="event-confirm-with-chat-pending")

    assert service.calls[1]["preview_invocation_id"] == preview_invocation_id
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


def test_dynamic_feishu_webhook_and_websocket_keep_the_same_verified_identity():
    dispatcher = _FakeServiceV2FeishuDispatcher(
        result={"success": True, "status": "COMPLETED"}
    )
    agent = _FakeAgent()
    replies = []
    scheduled = []
    submitted = []
    event_body = {
        "message": {
            "message_type": "text",
            "chat_id": "chat-one",
            "content": json.dumps({"text": "插件只读日报"}),
        },
        "sender": {"sender_id": {"open_id": "user-one"}},
    }
    sdk_event = SimpleNamespace(
        header=SimpleNamespace(event_id="event-dynamic-duplicate"),
        event=SimpleNamespace(
            message=SimpleNamespace(**event_body["message"]),
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="user-one")
            ),
        ),
    )
    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_FEISHU_APPROVAL_RUNTIME", None),
        patch.object(message_handler, "_SERVICE_V2_FEISHU_DISPATCHER", dispatcher),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=None),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
        patch.object(message_handler, "_schedule_local_task", side_effect=scheduled.append),
        patch.object(message_handler, "_submit_with_future_callback", side_effect=submitted.append),
    ):
        assert message_handler.queue_im_message_payload(
            event_body,
            event_id="event-dynamic-duplicate",
        )
        message_handler.handle_im_message(sdk_event)
        asyncio.run(scheduled.pop())
        asyncio.run(submitted.pop())

    assert dispatcher.calls == [
        {
            "command_text": "插件只读日报",
            "event_id": "event-dynamic-duplicate",
            "sender_id": "user-one",
            "chat_id": "chat-one",
        },
        {
            "command_text": "插件只读日报",
            "event_id": "event-dynamic-duplicate",
            "sender_id": "user-one",
            "chat_id": "chat-one",
        },
    ]
    assert agent.chat_calls == []


def test_self_pickup_preview_and_confirmation_use_persisted_signed_selection():
    preview_invocation_id = "11111111-1111-4111-8111-111111111111"
    rows = [
        {
            "arrival_count": "1",
            "bill_code": bill_code,
            "delivery_method": "自提",
            "destination_site": "邵阳自提部",
            "goods_count": "1",
            "row_number": index,
            "source_id": "source-one",
            "source_name": "每日到货表",
        }
        for index, bill_code in enumerate(("R_SELF", "R_DX_PICK"), start=2)
    ]
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "selection_preview": _selection_preview(
                    preview_invocation_id,
                    "self_pickup_problem_upload",
                    rows,
                    {"duplicate_source_rows": 0},
                ),
            },
            {"success": True, "status": "COMPLETED", "invocation_id": "run-formal"},
        ]
    )
    agent = _FakeAgent()
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
        assert agent.tool_calls == []
        assert service.calls[0]["preview_invocation_id"] is None
        assert service.calls[0]["envelope"]["body"] == {}
        assert pending["type"] == "self_pickup_selection_confirmation"
        assert pending["automation_route_key"] == "builtin.self_pickup_problem_upload"
        assert pending["preview_invocation_id"] == preview_invocation_id
        assert pending["originator_actor_id"] == "user-one"
        assert pending["selected_bill_codes"] == ["R_SELF", "R_DX_PICK"]
        assert "preview_fingerprint" not in pending
        assert not message_handler._contains_account_override(pending)
        _run_verified_text("yes", event_id="event-confirm")

    assert service.calls[-1]["event_id"] == "event-confirm"
    assert service.calls[-1]["preview_invocation_id"] == preview_invocation_id
    assert service.calls[-1]["envelope"]["body"] == {
        "selected_bill_codes": ["R_SELF", "R_DX_PICK"],
    }


def test_split_preview_selection_and_confirmation_use_persisted_signed_selection():
    preview_invocation_id = "22222222-2222-4222-8222-222222222222"
    rows = [
        {
            "arrived_quantity": 1,
            "bill_code": "R001",
            "complaint_status": "未投诉",
            "expected_quantity": 2,
            "pending_quantity": 1,
            "problem_item_status": "未执行",
            "problem_type": "少货/分批",
            "source_row_no": 2,
        }
    ]
    service = _FakeProjectEntrypoints(
        results=[
            {
                "success": True,
                "status": "COMPLETED",
                "invocation_id": preview_invocation_id,
                "selection_preview": _selection_preview(
                    preview_invocation_id,
                    "split_pending_problem_upload",
                    rows,
                    {
                        "complete_count": 0,
                        "hidden_completed_count": 0,
                        "split_count": 1,
                        "pending_count": 0,
                    },
                ),
            },
            {"success": True, "status": "COMPLETED", "invocation_id": "run-formal"},
        ]
    )
    agent = _FakeAgent()
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
        assert agent.tool_calls == []
        assert service.calls[0]["preview_invocation_id"] is None
        assert service.calls[0]["envelope"]["body"] == {}
        assert pending["type"] == "split_pending_selection"
        assert pending["preview_invocation_id"] == preview_invocation_id
        assert pending["originator_actor_id"] == "user-one"
        assert "preview_fingerprint" not in pending
        assert not message_handler._contains_account_override(pending)
        _run_verified_text("yes", event_id="event-split-confirm")

    assert service.calls[-1]["preview_invocation_id"] == preview_invocation_id
    assert service.calls[-1]["envelope"]["body"] == {
        "selected_bill_codes": ["R001"],
    }


def test_split_action_value_error_has_safe_repreview_reply():
    reply, reply_type = message_handler._automation_result_reply(
        task_name=message_handler.TOOL_DISPLAY_NAMES[message_handler.SPLIT_TOOL_NAME],
        result={
            "status": "FAILED_TERMINAL",
            "error_summary": "FIRST_PARTY_ACTION_FAILED:ACTION_VALUE_ERROR:FRAME=action.py:642:run_action",
        },
    )

    assert reply_type == "split_preview_stale"
    assert reply == (
        "分批候选清单或执行参数已变化，请重新发送“分批”生成最新清单；本次未执行外部写入。"
    )
    assert "ACTION_VALUE_ERROR" not in reply

    other_reply, other_reply_type = message_handler._automation_result_reply(
        task_name=message_handler.TOOL_DISPLAY_NAMES[message_handler.SPLIT_TOOL_NAME],
        result={
            "status": "FAILED_TERMINAL",
            "error_summary": "FIRST_PARTY_ACTION_FAILED:ACTION_VALUE_ERROR:FRAME=action.py:700:run_action",
        },
    )
    assert other_reply_type == "automation_project_failed"
    assert "本次未执行外部写入" not in other_reply
    assert "ACTION_VALUE_ERROR" not in other_reply
    assert "FRAME=" not in other_reply


def test_self_pickup_selection_preview_expired_has_stable_repreview_reply():
    reply, reply_type = message_handler._automation_result_reply(
        task_name=message_handler.TOOL_DISPLAY_NAMES["self_pickup_problem_upload"],
        result={
            "status": "FAILED_TERMINAL",
            "error_summary": (
                "FIRST_PARTY_ACTION_FAILED:RUNTIME_ERROR:"
                "SELECTION_PREVIEW_EXPIRED"
            ),
        },
    )

    assert reply_type == "self_pickup_preview_stale"
    assert reply == "候选清单已变化，请重新发送“自提到货问题件”；本次未写入。"
    assert "SELECTION_PREVIEW_EXPIRED" not in reply


def test_unknown_write_reply_does_not_expose_internal_failure_or_invite_replay():
    reply, reply_type = message_handler._automation_result_reply(
        task_name="统计到货数据",
        result={
            "status": "BLOCKED_DATA",
            "error_summary": (
                "FIRST_PARTY_ACTION_FAILED:WRITE_OUTCOME_UNKNOWN:"
                "FRAME=action.py:660:run_action"
            ),
        },
    )

    assert reply_type == "automation_project_write_outcome_unknown"
    assert reply == (
        "统计到货数据的目标表可能已更新，但最终核验暂未确认。"
        "系统已保留核验记录，新任务不会因此被阻塞。"
    )
    assert "FIRST_PARTY_ACTION_FAILED" not in reply
    assert "WRITE_OUTCOME_UNKNOWN" not in reply


def test_removed_r7_plate_choice_mode_cannot_invoke_project():
    service = _FakeProjectEntrypoints()
    agent = _FakeAgent()
    replies = []
    request = {
        "tool_name": "r7_departure_checkin",
        "params": {},
        "mode": "r7_departure_choice",
        "automation_route_key": "builtin.r7_departure_checkin",
        "dynamic_inputs": {},
    }

    with (
        patch("feishu.bot.get_agent_core", return_value=agent),
        patch.object(message_handler, "_AUTOMATION_PROJECT_ENTRYPOINTS", service),
        patch.object(message_handler, "direct_tool_request_from_text", return_value=request),
        patch.object(message_handler, "get_pending", return_value=None),
        patch.object(message_handler, "set_pending") as set_pending,
        patch.object(message_handler, "_reply_text", side_effect=_reply_recorder(replies)),
    ):
        _run_verified_text("departure-command", event_id="event-plate-choice")

    set_pending.assert_not_called()
    assert service.calls == []


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
