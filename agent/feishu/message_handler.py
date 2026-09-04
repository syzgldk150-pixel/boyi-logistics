"""飞书消息处理：解析文本 / 菜单事件 -> 直达工具或交给 Agent。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

import httpx

from agent.direct_tool_router import (
    FIRST_PARTY_FEISHU_ROUTE_KEYS,
    direct_tool_request_from_text,
    format_tool_reply,
    format_tool_reply_messages,
    is_deprecated_split_command,
    is_cancel_text,
    is_confirm_text,
    parse_login_account_choice,
    parse_login_send_code_session,
    parse_verify_code,
)
from agent.feishu_command_contract import (
    cancel_tool_name_from_text as _shared_cancel_tool_name_from_text,
    is_scan_cancel_text as _is_scan_cancel_text,
    is_scan_confirm_text as _is_scan_confirm_text,
)
from agent.orchestration.automation_project_entrypoints import (
    AutomationProjectEntrypoints,
    ServiceV2FeishuDispatcher,
)
from agent.orchestration.scan_preview_binding import (
    normalize_scan_preview_public_projection,
)
from agent.pending_actions import clear_pending, get_pending, set_pending
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.tms_runtime.account_contracts import PRICE_SESSION_PROFILE
from feishu.migration_entrypoint_router import (
    dispatch_migrated_fixed_feishu_entrypoint,
)
from feishu.notify import remember_chat_id
from feishu.selection_preview import (
    FEISHU_SAFE_TEXT_BYTES,
    SCAN_PREVIEW_ERROR_MESSAGES,
    SELF_PICKUP_MAX_SELECTED,
    contains_account_override as _contains_account_override,
    normalize_selection_preview_projection as _normalize_selection_preview_projection,
    parse_split_selection as _parse_split_selection,
    scan_confirmation_ttl as _scan_confirmation_ttl,
    scan_preview_error_message as _scan_preview_error_message,
    scan_preview_reply as _scan_preview_reply,
    scan_preview_ttl as _scan_preview_ttl,
    selection_confirmation_ttl as _selection_confirmation_ttl,
    selection_preview_ttl as _selection_preview_ttl,
    self_pickup_candidate_lines as _self_pickup_candidate_lines,
    split_candidate_lines as _split_candidate_lines,
    split_selected_lines as _split_selected_lines,
    split_text_chunks as _split_text_chunks,
)
from shared.redaction import redact_text
from tools.internal_http import internal_api_headers

logger = logging.getLogger("feishu")

AUTH_REQUIRED_KEYWORDS = (
    "AUTH_REQUIRED",
    "当前未登录",
    "登录态已过期",
    "登录态已失效",
    "登录已过期",
)
AUTH_PENDING_CODE_KEYWORDS = (
    "AUTH_PENDING_CODE",
    "短信验证码已发送",
    "等待人工提交验证码",
)
UNKNOWN_EXECUTION_REPLY = "没有匹配到可执行脚本，我不知道该执行哪个任务。"
LOGIN_PENDING_TTL = 600
LOGIN_ACCOUNT_CHOICE_TTL = 600
SELF_PICKUP_PROBLEM_ACCOUNT_ID = "ronghui_self_pickup_problem"
SELF_PICKUP_PREVIEW_TOOL_NAME = "preview_self_pickup_problems"
SPLIT_TOOL_NAME = "split_pending_problem_upload"
SPLIT_PREVIEW_TOOL_NAME = "preview_split_pending_problems"
SCAN_FEISHU_ROUTE_KEY = "builtin.scan_codes"
ACCOUNT_AUTH_SESSION_PREFIX = "account:"
ADMIN_REQUEST_TIMEOUT = 90.0
_FEISHU_ROUTE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,190}$")


_AUTOMATION_PROJECT_ENTRYPOINTS: AutomationProjectEntrypoints | None = None
_FEISHU_APPROVAL_RUNTIME: Any | None = None
_SERVICE_V2_FEISHU_DISPATCHER: ServiceV2FeishuDispatcher | None = None


def bind_automation_project_entrypoints(
    service: AutomationProjectEntrypoints | None,
) -> None:
    """Bind the sole trusted project invocation adapter for Feishu events."""

    global _AUTOMATION_PROJECT_ENTRYPOINTS
    if service is not None and not isinstance(service, AutomationProjectEntrypoints):
        raise TypeError("service must be AutomationProjectEntrypoints or None")
    _AUTOMATION_PROJECT_ENTRYPOINTS = service


def bind_feishu_approval_runtime(service: Any | None) -> None:
    """Bind the database-backed Feishu administrator and approval runtime."""

    global _FEISHU_APPROVAL_RUNTIME
    _FEISHU_APPROVAL_RUNTIME = service


def bind_service_v2_feishu_dispatcher(service: ServiceV2FeishuDispatcher | None) -> None:
    """Bind the sole managed Service v2 Feishu command dispatcher."""

    global _SERVICE_V2_FEISHU_DISPATCHER
    if service is not None and not isinstance(service, ServiceV2FeishuDispatcher):
        raise TypeError("service must be ServiceV2FeishuDispatcher or None")
    _SERVICE_V2_FEISHU_DISPATCHER = service


@dataclass(frozen=True)
class FeishuCommandContext:
    """Trusted event identity propagated to every command submitted for one event."""

    event_id: str
    actor_id: str
    chat_id: str


_COMMAND_CONTEXT: ContextVar[FeishuCommandContext | None] = ContextVar(
    "feishu_command_context",
    default=None,
)
MENU_KEY_ALIASES = {
    "scan": "扫描",
    "sync_scan": "扫描",
    "scan_sync": "扫描",
    "sync_scan_codes": "扫描",
    "get_and_scan": "扫描",
}
RUNNING_CANCEL_PENDING_TTL = 300
ACTIVE_RUN_PENDING_TTL = 21600
RUN_TERMINAL_STATUSES = {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"}
TOOL_DISPLAY_NAMES = {
    "sync_scan_codes": "扫描任务",
    "sync_arrival_stats": "统计到货数据任务",
    "sync_arrive_list": "到货清单任务",
    "sync_daily_send_orders": "当日寄件数据任务",
    "sync_yunda_dispatch_forecast": "韵达派件预测任务",
    "sync_yunda_send_waybills": "韵达寄件运单任务",
    "self_pickup_problem_upload": "自提到货问题件任务",
    "split_pending_problem_upload": "分批问题件任务",
}


def _automation_task_name(route_key: str) -> str:
    """Return the business-facing name for one fixed Feishu project route."""

    safe_route_key = str(route_key or "").strip()
    matching_names = [
        TOOL_DISPLAY_NAMES[tool_name]
        for tool_name, candidate_route in FIRST_PARTY_FEISHU_ROUTE_KEYS.items()
        if candidate_route == safe_route_key and tool_name in TOOL_DISPLAY_NAMES
    ]
    return matching_names[0] if matching_names else "自动化任务"


def _automation_result_reply(
    *,
    task_name: str,
    result: dict[str, Any],
) -> tuple[str, str]:
    """Render one terminal project result without exposing control-plane jargon."""

    status = str(result.get("status") or "").strip().upper()
    reason = str(result.get("error_summary") or "").strip()
    if status in {"WAITING_APPROVAL", "PENDING_APPROVAL"}:
        return (
            f"{task_name}已提交，正在等待审批。",
            "automation_project_waiting_approval",
        )
    if status == "COMPLETED":
        return f"{task_name}已完成。", "automation_project_completed"
    if status == "BLOCKED_LOGIN":
        return (
            f"{task_name}未完成：绑定的业务账号需要重新登录。",
            "automation_project_blocked_login",
        )
    if status == "CANCELLED":
        return f"{task_name}已取消。", "automation_project_cancelled"
    if status == "PARTIAL":
        detail = reason or "只完成了部分数据，请查看任务详情后重试未完成部分。"
        return f"{task_name}部分完成：{detail[:300]}", "automation_project_partial"
    if status in {"BLOCKED_DATA", "FAILED_RETRYABLE", "FAILED_TERMINAL"}:
        if (
            task_name == TOOL_DISPLAY_NAMES["self_pickup_problem_upload"]
            and "SELECTION_PREVIEW_EXPIRED" in reason
        ):
            return (
                "候选清单已变化，请重新发送“自提到货问题件”；本次未写入。",
                "self_pickup_preview_stale",
            )
        if (
            task_name == TOOL_DISPLAY_NAMES[SPLIT_TOOL_NAME]
            and "ACTION_VALUE_ERROR:FRAME=action.py:642:run_action" in reason
        ):
            return (
                "分批候选清单或执行参数已变化，请重新发送“分批”生成最新清单；本次未执行外部写入。",
                "split_preview_stale",
            )
        detail = reason or "数据读取或写入校验未通过，请查看任务详情。"
        return f"{task_name}执行失败：{detail[:300]}", "automation_project_failed"
    detail = reason or "结果暂时无法确认，请前往事项中心查看任务详情。"
    return f"{task_name}未完成：{detail[:300]}", "automation_project_status"
async def _selection_pending_actor_allowed(
    *,
    chat_id: str,
    sender_id: str,
    pending: dict[str, Any],
) -> bool:
    originator = str(pending.get("originator_actor_id") or "").strip()
    if not originator:
        clear_pending(chat_id)
        await _reply_text(
            chat_id,
            "候选确认状态无效，请重新生成候选清单。",
            reply_type="selection_preview_pending_invalid",
        )
        return False
    if originator != str(sender_id or "").strip():
        await _reply_text(
            chat_id,
            "只有生成本次候选清单的用户可以确认或取消。",
            reply_type="selection_preview_actor_mismatch",
        )
        return False
    return True


def _automation_entrypoints() -> AutomationProjectEntrypoints:
    service = _AUTOMATION_PROJECT_ENTRYPOINTS
    if service is None:
        raise OrchestrationError(
            "PROJECT_INVOKE_UNAVAILABLE",
            "Automation project entrypoints are not initialized",
        )
    return service


def _safe_feishu_route_key(value: Any) -> str:
    route_key = str(value or "").strip()
    if not _FEISHU_ROUTE_KEY_RE.fullmatch(route_key):
        raise OrchestrationError(
            "PROJECT_ROUTE_INVALID",
            "Automation project route key is invalid",
        )
    return route_key


async def _invoke_automation_project(
    *,
    route_key: str,
    dynamic_inputs: dict[str, Any] | None,
    preview_run_id: str | None = None,
    on_accepted: Callable[[Any], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Invoke one exact committed project from a verified Feishu event."""

    context = _COMMAND_CONTEXT.get()
    safe_route_key = _safe_feishu_route_key(route_key)
    if context is None or not context.event_id:
        raise OrchestrationError(
            "STABLE_EVENT_ID_REQUIRED",
            "A stable verified Feishu event id is required",
        )
    inputs = dict(dynamic_inputs or {})
    if _contains_account_override(inputs):
        raise OrchestrationError(
            "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN",
            "Feishu cannot override project account bindings",
        )
    return await _automation_entrypoints().invoke_feishu(
        route_key=safe_route_key,
        event_id=context.event_id,
        sender_id=context.actor_id,
        chat_id=context.chat_id,
        envelope={"body": inputs, "query": {}},
        preview_run_id=preview_run_id,
        on_accepted=on_accepted,
    )


async def _invoke_automation_project_and_reply(
    *,
    route_key: str,
    dynamic_inputs: dict[str, Any] | None,
    receive_id: str,
    receive_id_type: str = "chat_id",
    preview_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Invoke one exact committed project and render its bounded Feishu result."""

    context = _COMMAND_CONTEXT.get()
    safe_route_key = str(route_key or "").strip()
    task_name = _automation_task_name(safe_route_key)
    if safe_route_key == SCAN_FEISHU_ROUTE_KEY:
        start_reply = "已开始生成扫描预览，完成后我会发回待扫描清单。"
    else:
        start_reply = f"已开始执行：{task_name}。完成后我会反馈结果。"
    accepted_notified = False

    async def notify_accepted(_receipt: Any) -> None:
        nonlocal accepted_notified
        if accepted_notified:
            return
        accepted_notified = True
        try:
            await _reply_text(
                receive_id,
                start_reply,
                receive_id_type=receive_id_type,
                reply_type="automation_project_started",
            )
        except Exception:
            logger.exception(
                "trusted Feishu automation acceptance reply failed | route=%s",
                safe_route_key[:191],
            )

    try:
        result = await _invoke_automation_project(
            route_key=safe_route_key,
            dynamic_inputs=dynamic_inputs,
            preview_run_id=preview_run_id,
            on_accepted=notify_accepted,
        )
    except OrchestrationError as exc:
        logger.warning(
            "trusted Feishu automation rejected | route=%s | code=%s",
            str(route_key or "")[:191],
            exc.code,
        )
        if exc.code == "AUTOMATION_ALREADY_RUNNING":
            details = exc.details if isinstance(exc.details, dict) else {}
            active_status = str(details.get("active_status") or "").strip().upper()
            status_label = {
                "RUNNING": "执行中",
                "VERIFYING": "核验中",
                "WAITING_APPROVAL": "待审批",
                "NEEDS_CLARIFICATION": "待补充信息",
                "BLOCKED_LOGIN": "登录阻塞",
                "BLOCKED_DATA": "数据阻塞",
                "FAILED_RETRYABLE": "可重试失败",
            }.get(active_status, "未终结")
            rejected_reply = (
                f"{task_name}未重复提交：已有一条未结束任务（状态：{status_label}）。"
                "请到事项中心处理或取消后再重试。"
            )
        else:
            rejected_reply = (
                _scan_preview_error_message(exc.code)
                if safe_route_key == SCAN_FEISHU_ROUTE_KEY
                and exc.code in SCAN_PREVIEW_ERROR_MESSAGES
                else f"自动化任务未提交（{exc.code}），请检查项目设置后重试。"
            )
        await _reply_text(
            receive_id,
            rejected_reply,
            receive_id_type=receive_id_type,
            reply_type="automation_project_rejected",
        )
        return None

    status = str(result.get("status") or "").strip().upper()
    run_id = str(result.get("run_id") or "").strip()
    if safe_route_key == SCAN_FEISHU_ROUTE_KEY and status == "COMPLETED":
        projection = normalize_scan_preview_public_projection(
            result.get("scan_preview"),
            expected_run_id=run_id,
        )
        if projection is None:
            await _reply_text(
                receive_id,
                "扫描预览返回无效，确认入口已阻断，请重新发送“扫描”。",
                receive_id_type=receive_id_type,
                reply_type="scan_preview_invalid",
            )
            return result
        pending_key = context.actor_id if context is not None else receive_id
        set_pending(
            pending_key,
            {
                "type": "scan_preview_confirmation",
                "automation_route_key": SCAN_FEISHU_ROUTE_KEY,
                "preview_run_id": projection["preview_run_id"],
                "expires_at": projection["expires_at"],
                "originator_actor_id": context.actor_id if context is not None else "",
                "preview_event_id": context.event_id if context is not None else "",
                "confirmation_event_id": "",
                "confirmation_state": "pending",
            },
            ttl_sec=_scan_preview_ttl(projection),
            persist=False,
        )
        await _reply_text(
            receive_id,
            _scan_preview_reply(projection),
            receive_id_type=receive_id_type,
            reply_type="scan_preview_ready",
        )
        return result
    reply, reply_type = _automation_result_reply(
        task_name=task_name,
        result=result,
    )
    await _reply_text(
        receive_id,
        reply,
        receive_id_type=receive_id_type,
        reply_type=reply_type,
    )
    return result


async def _invoke_selection_preview_and_reply(
    *,
    route_key: str,
    tool_name: str,
    receive_id: str,
) -> dict[str, Any] | None:
    expected_automation_id = (
        "self_pickup_problem_upload"
        if tool_name == SELF_PICKUP_PREVIEW_TOOL_NAME
        else "split_pending_problem_upload"
    )
    start_reply = (
        "正在生成自提到货问题件候选清单，完成后我会发回结果。"
        if tool_name == SELF_PICKUP_PREVIEW_TOOL_NAME
        else "正在生成分批问题件候选清单；任务繁忙时可能需要排队，完成后我会反馈结果。"
    )
    await _reply_text(
        receive_id,
        start_reply,
        reply_type="selection_preview_started",
    )
    try:
        result = await _invoke_automation_project(
            route_key=route_key,
            dynamic_inputs={},
        )
    except OrchestrationError as exc:
        logger.warning(
            "trusted Feishu selection preview rejected | route=%s | code=%s",
            str(route_key or "")[:191],
            exc.code,
        )
        await _reply_text(
            receive_id,
            f"候选清单未生成（{exc.code}），请检查项目账号和数据表绑定后重试。",
            reply_type="automation_preview_rejected",
        )
        return None

    status = str(result.get("status") or "").strip().upper()
    run_id = str(result.get("run_id") or "").strip()
    if status != "COMPLETED":
        reply, reply_type = _automation_result_reply(
            task_name=_automation_task_name(route_key),
            result=result,
        )
        await _reply_text(receive_id, reply, reply_type=reply_type)
        return result
    projection = _normalize_selection_preview_projection(
        result.get("selection_preview"),
        expected_automation_id=expected_automation_id,
        expected_run_id=run_id,
    )
    if projection is None:
        await _reply_text(
            receive_id,
            "候选清单返回无效，确认入口已阻断，请重新发起任务。",
            reply_type="selection_preview_invalid",
        )
        return result

    candidates = list(projection["candidates"])
    can_confirm = bool(projection["can_confirm"])
    ttl = _selection_preview_ttl(projection) if can_confirm else 0
    context = _COMMAND_CONTEXT.get()
    originator_actor_id = context.actor_id if context is not None else ""
    if tool_name == SELF_PICKUP_PREVIEW_TOOL_NAME:
        if candidates and len(candidates) <= SELF_PICKUP_MAX_SELECTED and ttl > 0:
            set_pending(
                receive_id,
                {
                    "type": "self_pickup_selection_confirmation",
                    "tool_name": "self_pickup_problem_upload",
                    "automation_route_key": route_key,
                    "preview_run_id": projection["preview_run_id"],
                    "originator_actor_id": originator_actor_id,
                    "selected_bill_codes": [
                        str(item["bill_code"]) for item in candidates
                    ],
                    "expires_at": projection["expires_at"],
                    "description": "自提到货问题件",
                },
                ttl_sec=ttl,
            )
        await _reply_split_lines(
            receive_id,
            _self_pickup_candidate_lines(
                candidates,
                int(projection["summary"].get("duplicate_source_rows") or 0),
            ),
            reply_type="self_pickup_candidate_list",
        )
        return result

    hidden_completed = int(
        projection["summary"].get("hidden_completed_count") or 0
    )
    if not candidates:
        await _reply_text(
            receive_id,
            f"待执行分批运单 0 单（已隐藏完整成功 {hidden_completed} 单）。",
            reply_type="split_candidate_list",
        )
        return result
    if ttl <= 0:
        await _reply_text(
            receive_id,
            "分批候选清单已过期，请重新发送“分批”。",
            reply_type="split_preview_stale",
        )
        return result
    set_pending(
        receive_id,
        {
            "type": "split_pending_selection",
            "tool_name": SPLIT_TOOL_NAME,
            "automation_route_key": route_key,
            "preview_run_id": projection["preview_run_id"],
            "originator_actor_id": originator_actor_id,
            "expires_at": projection["expires_at"],
            "candidates": candidates,
        },
        ttl_sec=ttl,
    )
    await _reply_split_lines(
        receive_id,
        _split_candidate_lines(candidates, hidden_completed),
        reply_type="split_candidate_list",
    )
    return result


def _store_scan_confirmation_pending(
    chat_id: str,
    pending: dict[str, Any],
) -> bool:
    ttl = _scan_confirmation_ttl(pending)
    if ttl <= 0:
        clear_pending(chat_id, volatile_only=True)
        return False
    set_pending(chat_id, pending, ttl_sec=ttl, persist=False)
    return True


async def _confirm_scan_preview_and_reply(
    *,
    chat_id: str,
    pending_key: str,
    sender_id: str,
    pending: dict[str, Any],
) -> None:
    context = _COMMAND_CONTEXT.get()
    expected_fields = {
        "type",
        "automation_route_key",
        "preview_run_id",
        "expires_at",
        "originator_actor_id",
        "preview_event_id",
        "confirmation_event_id",
        "confirmation_state",
        "terminal_error_code",
    }
    if (
        not set(pending) <= expected_fields
        or str(pending.get("type") or "") != "scan_preview_confirmation"
        or str(pending.get("automation_route_key") or "")
        != SCAN_FEISHU_ROUTE_KEY
        or _contains_account_override(pending)
    ):
        clear_pending(pending_key, volatile_only=True)
        await _reply_text(
            chat_id,
            "扫描确认状态无效，请重新发送“扫描”。",
            reply_type="scan_preview_pending_invalid",
        )
        return
    if context is None or not context.event_id:
        await _reply_text(
            chat_id,
            "确认扫描需要稳定的飞书事件标识，本次未提交。",
            reply_type="scan_preview_confirmation_rejected",
        )
        return
    originator = str(pending.get("originator_actor_id") or "").strip()
    if not originator or originator != str(sender_id or "").strip():
        await _reply_text(
            chat_id,
            "只有生成本次扫描预览的用户可以确认执行。",
            reply_type="scan_preview_actor_mismatch",
        )
        return
    ttl = _scan_confirmation_ttl(pending)
    if ttl <= 0:
        clear_pending(pending_key, volatile_only=True)
        await _reply_text(
            chat_id,
            SCAN_PREVIEW_ERROR_MESSAGES["SCAN_PREVIEW_EXPIRED"],
            reply_type="scan_preview_expired",
        )
        return
    try:
        preview_run_id = str(uuid.UUID(str(pending.get("preview_run_id") or "")))
    except (AttributeError, ValueError):
        preview_run_id = ""
    if not preview_run_id or preview_run_id != str(pending.get("preview_run_id") or ""):
        clear_pending(pending_key, volatile_only=True)
        await _reply_text(
            chat_id,
            SCAN_PREVIEW_ERROR_MESSAGES["SCAN_PREVIEW_ID_INVALID"],
            reply_type="scan_preview_pending_invalid",
        )
        return
    preview_event_id = str(pending.get("preview_event_id") or "").strip()
    confirmation_event_id = str(pending.get("confirmation_event_id") or "").strip()
    if context.event_id == preview_event_id:
        await _reply_text(
            chat_id,
            "确认扫描必须使用一条新的飞书消息，请重新发送“确认扫描”。",
            reply_type="scan_preview_new_event_required",
        )
        return
    if str(pending.get("confirmation_state") or "") == "terminal":
        await _reply_text(
            chat_id,
            _scan_preview_error_message(pending.get("terminal_error_code")),
            reply_type="scan_preview_confirmation_terminal",
        )
        return
    if confirmation_event_id and confirmation_event_id != context.event_id:
        await _reply_text(
            chat_id,
            "原确认请求结果仍需核实，请前往事项中心查看原任务；本次没有创建新请求。",
            reply_type="scan_preview_confirmation_locked",
        )
        return

    locked_pending = {
        **pending,
        "confirmation_event_id": context.event_id,
        "confirmation_state": "submitting",
    }
    if not _store_scan_confirmation_pending(pending_key, locked_pending):
        await _reply_text(
            chat_id,
            SCAN_PREVIEW_ERROR_MESSAGES["SCAN_PREVIEW_EXPIRED"],
            reply_type="scan_preview_expired",
        )
        return
    await _reply_text(
        chat_id,
        "已开始执行：正式扫描。完成后我会反馈结果。",
        reply_type="scan_preview_formal_started",
    )
    try:
        result = await _invoke_automation_project(
            route_key=SCAN_FEISHU_ROUTE_KEY,
            dynamic_inputs={},
            preview_run_id=preview_run_id,
        )
    except OrchestrationError as exc:
        code = str(exc.code or "").strip()
        if code == "REQUEST_ID_REUSED":
            unlocked = {
                **pending,
                "confirmation_event_id": "",
                "confirmation_state": "pending",
            }
            _store_scan_confirmation_pending(pending_key, unlocked)
        elif code in {
            "SCAN_PREVIEW_ID_INVALID",
            "SCAN_PREVIEW_NOT_FOUND",
            "SCAN_PREVIEW_INCOMPLETE",
            "SCAN_PREVIEW_INVALID",
            "SCAN_PREVIEW_EXPIRED",
            "SCAN_PREVIEW_STALE",
            "PROJECT_INVOCATION_STALE",
            "SCAN_PREVIEW_CONTEXT_REQUIRED",
            "SCAN_PREVIEW_CONTEXT_INVALID",
        }:
            clear_pending(pending_key, volatile_only=True)
        elif code in {
            "SCAN_PREVIEW_ALREADY_CONSUMED",
            "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED",
        }:
            terminal = {
                **locked_pending,
                "confirmation_state": "terminal",
                "terminal_error_code": code,
            }
            _store_scan_confirmation_pending(pending_key, terminal)
        else:
            unknown = {**locked_pending, "confirmation_state": "unknown"}
            _store_scan_confirmation_pending(pending_key, unknown)
        await _reply_text(
            chat_id,
            _scan_preview_error_message(code),
            reply_type=f"scan_preview_confirmation_error:{code or 'unknown'}",
        )
        return
    except Exception as exc:
        logger.error(
            "Feishu scan confirmation result unknown | error=%s",
            redact_text(exc)[:200],
        )
        unknown = {**locked_pending, "confirmation_state": "unknown"}
        _store_scan_confirmation_pending(pending_key, unknown)
        await _reply_text(
            chat_id,
            _scan_preview_error_message(""),
            reply_type="scan_preview_confirmation_unknown",
        )
        return

    run_id = str(result.get("run_id") or "").strip()
    error_code = str(result.get("error_code") or "").strip()
    if not run_id:
        unknown = {**locked_pending, "confirmation_state": "unknown"}
        _store_scan_confirmation_pending(pending_key, unknown)
        await _reply_text(
            chat_id,
            _scan_preview_error_message(""),
            reply_type="scan_preview_confirmation_unknown",
        )
        return
    if error_code:
        reply = _scan_preview_error_message(error_code)
        reply_type = f"scan_preview_formal_error:{error_code}"
    else:
        reply, reply_type = _automation_result_reply(
            task_name="正式扫描",
            result=result,
        )
    await _reply_text(
        chat_id,
        reply,
        reply_type=reply_type,
    )
    clear_pending(pending_key, volatile_only=True)


async def _execute_split_formal(
    agent: Any,
    chat_id: str,
    pending: dict[str, Any],
    selected_bill_codes: list[str],
    *,
    running_message: str,
) -> None:
    del agent, running_message
    if _selection_confirmation_ttl(pending) <= 0:
        clear_pending(chat_id)
        await _reply_text(
            chat_id,
            "分批候选清单已过期，请重新发送“分批”。",
            reply_type="split_preview_stale",
        )
        return
    if _contains_account_override(pending):
        clear_pending(chat_id)
        await _reply_text(
            chat_id,
            "旧版确认状态已失效，请重新发送“分批”。",
            reply_type="split_confirmation_stale",
        )
        return
    route_key = str(pending.get("automation_route_key") or "").strip()
    preview_run_id = str(pending.get("preview_run_id") or "").strip()
    if not route_key or not preview_run_id or not selected_bill_codes:
        clear_pending(chat_id)
        await _reply_text(
            chat_id,
            "分批候选清单已失效，请重新发送“分批”。",
            reply_type="split_confirmation_stale",
        )
        return
    clear_pending(chat_id)
    await _invoke_automation_project_and_reply(
        route_key=route_key,
        dynamic_inputs={"selected_bill_codes": selected_bill_codes},
        receive_id=chat_id,
        preview_run_id=preview_run_id,
    )


async def _reply_split_lines(chat_id: str, lines: list[str], *, reply_type: str) -> None:
    for chunk in _split_text_chunks(lines):
        await _reply_text(chat_id, chunk, reply_type=reply_type)


def _admin_base_url() -> str:
    raw = (
        os.getenv("AGENT_ADMIN_BASE_URL")
        or os.getenv("DOCFLOW_AGENT_BASE_URL")
        or ""
    ).strip()
    if not raw:
        port = str(os.getenv("AGENT_PORT") or "9000").strip() or "9000"
        raw = f"http://127.0.0.1:{port}"
    raw = raw.rstrip("/")
    if raw.endswith("/tms"):
        raw = raw[:-4]
    return raw


def _post_admin_sync(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_admin_base_url()}{path}"
    try:
        resp = httpx.post(
            url,
            json=body or {},
            headers=internal_api_headers(),
            timeout=ADMIN_REQUEST_TIMEOUT,
        )
    except Exception as exc:
        return {"ok": False, "error": f"调用 {path} 失败: {redact_text(exc)[:200]}"}
    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": f"unexpected payload: {str(data)[:200]}"}
    return data


async def _post_admin(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_post_admin_sync, path, body)


def _get_admin_sync(path: str) -> dict[str, Any]:
    url = f"{_admin_base_url()}{path}"
    try:
        resp = httpx.get(
            url,
            headers=internal_api_headers(),
            timeout=ADMIN_REQUEST_TIMEOUT,
        )
    except Exception as exc:
        return {"ok": False, "error": f"调用 {path} 失败: {redact_text(exc)[:200]}"}
    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": f"unexpected payload: {str(data)[:200]}"}
    return data


async def _get_admin(path: str) -> dict[str, Any]:
    return await asyncio.to_thread(_get_admin_sync, path)


def _auth_session_for_tool(tool_name: str, params: dict[str, Any] | None = None) -> str:
    normalized = str(tool_name or "").strip()
    params = params if isinstance(params, dict) else {}
    account_id = str(params.get("account_id") or params.get("accountId") or "").strip()
    if account_id:
        return f"{ACCOUNT_AUTH_SESSION_PREFIX}{account_id}"
    if normalized == "get_price":
        return "price"
    if normalized == "track_waybill":
        provider = str(params.get("provider") or "").strip().lower()
        tracking_number = str(params.get("tracking_number") or "").strip()
        if provider == "yunda" or (tracking_number.isdigit() and not tracking_number.startswith(("000", "200"))):
            return "yunda"
    if normalized == "sync_yunda_dispatch_forecast" or normalized.startswith("yunda_"):
        return "yunda"
    if normalized == "self_pickup_problem_upload":
        return f"{ACCOUNT_AUTH_SESSION_PREFIX}{SELF_PICKUP_PROBLEM_ACCOUNT_ID}"
    return "default"


def _auth_account_id(auth_session: str) -> str:
    value = str(auth_session or "").strip()
    if value.lower().startswith(ACCOUNT_AUTH_SESSION_PREFIX):
        account_id = value[len(ACCOUNT_AUTH_SESSION_PREFIX):].strip()
        if account_id.replace("_", "").replace("-", "").isalnum():
            return account_id
    return ""


def _auth_session_path(auth_session: str, action: str) -> str:
    account_id = _auth_account_id(auth_session)
    if account_id:
        normalized_action = "login" if action.strip("/") == "send-code" else action.strip("/")
        return f"/admin/accounts/{account_id}/{normalized_action}"
    if auth_session == "price":
        prefix = "/admin/tms/price-session"
    elif auth_session == "yunda":
        prefix = "/admin/tms/yunda-session"
    else:
        prefix = "/admin/tms/session"
    return f"{prefix}/{action.lstrip('/')}"


def _auth_session_label(auth_session: str) -> str:
    account_id = _auth_account_id(auth_session)
    if account_id == SELF_PICKUP_PROBLEM_ACCOUNT_ID:
        return "自提到货问题件账号"
    if account_id:
        return account_id
    if auth_session == "price":
        return "大祥账号"
    if auth_session == "yunda":
        return "韵达账号"
    return "操作场账号"


def _normalize_auth_session(auth_session: str) -> str:
    raw_value = str(auth_session or "").strip()
    account_id = _auth_account_id(raw_value)
    if account_id:
        return f"{ACCOUNT_AUTH_SESSION_PREFIX}{account_id}"
    value = raw_value.lower()
    if value == "price":
        return "price"
    if value == "yunda":
        return "yunda"
    return "default"


def _auth_session_for_result(
    tool_name: str,
    params: dict[str, Any] | None,
    result: dict[str, Any],
) -> str:
    candidates: list[Any] = [result]
    if isinstance(result, dict):
        candidates.append(result.get("data"))
    for item in candidates:
        if not isinstance(item, dict):
            continue
        auth_session = str(item.get("auth_session") or item.get("authSession") or "").strip()
        if auth_session:
            return _normalize_auth_session(auth_session)
        account_id = str(item.get("account_id") or item.get("accountId") or "").strip()
        if account_id and account_id.replace("_", "").replace("-", "").isalnum():
            return f"{ACCOUNT_AUTH_SESSION_PREFIX}{account_id}"
        provider = str(item.get("provider") or "").strip().lower()
        if provider in {"yunda", "韵达"}:
            return "yunda"
        if provider in {"ronghui", "融辉", "price", "大祥"}:
            return "price"
    return _auth_session_for_tool(tool_name, params)


def _account_option_from_payload(account: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(account, dict):
        return None
    if not bool(account.get("is_active", True)):
        return None
    account_id = str(account.get("account_id") or account.get("accountId") or "").strip()
    if not account_id:
        return None
    system = str(account.get("system") or "").strip().lower()
    system_label = str(account.get("system_label") or account.get("systemLabel") or system).strip()
    account_purpose = str(account.get("account_purpose") or account.get("accountPurpose") or "").strip().lower()
    account_purpose_label = str(
        account.get("account_purpose_label") or account.get("accountPurposeLabel") or ""
    ).strip()
    account_name = str(account.get("name") or account.get("account_name") or account_id).strip()
    return {
        "account_id": account_id,
        "account_name": account_name,
        "system": system,
        "system_label": system_label,
        "account_purpose": account_purpose,
        "account_purpose_label": account_purpose_label,
        "is_default": bool(account.get("is_default", False)),
        "session_capable": bool(account.get("session_capable", True)),
        "session_profile": str(account.get("session_profile") or "").strip(),
        "auth_session": f"{ACCOUNT_AUTH_SESSION_PREFIX}{account_id}",
        "status": account.get("status") if isinstance(account.get("status"), dict) else {},
    }


def _account_options_from_accounts_payload(
    payload: dict[str, Any],
    *,
    pending_only: bool = False,
) -> list[dict[str, Any]]:
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return []
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for account in accounts:
        option = _account_option_from_payload(account)
        if not option:
            continue
        status = option.get("status") if isinstance(option.get("status"), dict) else {}
        if pending_only and not (
            bool(status.get("pending_code"))
            or str(status.get("status") or "").strip().lower() == "pending_code"
        ):
            continue
        account_id = option["account_id"]
        if account_id in seen:
            continue
        seen.add(account_id)
        options.append(option)
    return options


async def _fetch_login_account_options(*, pending_only: bool = False) -> list[dict[str, Any]]:
    payload = await _get_admin("/admin/accounts")
    if not payload.get("ok"):
        logger.warning("Failed to fetch automation account choices: %s", str(payload)[:300])
        return []
    return _account_options_from_accounts_payload(payload, pending_only=pending_only)


def _format_login_account_choice(options: list[dict[str, Any]] | None = None) -> str:
    if not options:
        return "\n".join([
            "1. 大祥账号",
            "2. 操作场账号",
            "3. 韵达账号",
        ])
    lines = []
    for index, option in enumerate(options, start=1):
        account_name = str(option.get("account_name") or option.get("account_id") or "").strip()
        account_id = str(option.get("account_id") or "").strip()
        system_label = str(option.get("system_label") or option.get("system") or "").strip()
        purpose = str(option.get("account_purpose") or "").strip().lower()
        purpose_label = str(option.get("account_purpose_label") or "").strip()
        label = account_name or account_id
        if account_id and account_id != label:
            label = f"{label} ({account_id})"
        if system_label:
            if purpose_label and purpose not in {"", "general"}:
                system_label = f"{system_label} / {purpose_label}"
            label = f"{label} · {system_label}"
        lines.append(f"{index}. {label}")
    return "\n".join(lines)


async def _begin_login_account_choice(
    chat_id: str,
    *,
    options: list[dict[str, Any]] | None = None,
) -> None:
    account_options = options if options is not None else await _fetch_login_account_options()
    if not account_options:
        await _reply_text(chat_id, "没有找到可登录的自动化账号，请先到账号管理中启用并保存账号。")
        return
    set_pending(
        chat_id,
        {"type": "login_account_choice", "options": account_options},
        ttl_sec=LOGIN_ACCOUNT_CHOICE_TTL,
    )
    await _reply_text(chat_id, _format_login_account_choice(account_options))


def _resolve_login_account_choice(text: str, options: list[dict[str, Any]] | None) -> str | None:
    normalized = str(text or "").strip()
    if options:
        if normalized.isdigit():
            index = int(normalized)
            if 1 <= index <= len(options):
                return str(options[index - 1].get("auth_session") or "").strip() or None
        lowered = normalized.lower()
        for option in options:
            candidates = {
                str(option.get("account_id") or "").strip().lower(),
                str(option.get("account_name") or "").strip().lower(),
                str(option.get("system_label") or "").strip().lower(),
                str(option.get("system") or "").strip().lower(),
            }
            if lowered in {item for item in candidates if item}:
                return str(option.get("auth_session") or "").strip() or None
    return None


async def _resolve_login_command_session(auth_session: str) -> str:
    normalized = _normalize_auth_session(auth_session)
    if _auth_account_id(normalized):
        return normalized
    system_by_session = {
        "default": "ronghui",
        "price": "ronghui",
        "yunda": "yunda",
    }
    system = system_by_session.get(normalized)
    if not system:
        return normalized
    options = await _fetch_login_account_options()
    preferred = [option for option in options if str(option.get("system") or "") == system]
    if normalized == "price":
        preferred = [
            option
            for option in preferred
            if str(option.get("account_purpose") or "").strip().lower() == "price"
        ]
    default_option = next((option for option in preferred if bool(option.get("is_default"))), None)
    default_option = default_option or next(
        (
            option
            for option in preferred
            if str(option.get("session_profile") or "").strip()
            in {"default", PRICE_SESSION_PROFILE, "yunda", "self_pickup_problem_upload"}
        ),
        None,
    )
    selected = default_option or (preferred[0] if preferred else None)
    return str(selected.get("auth_session")) if selected else normalized


async def _pending_code_auth_session() -> str | None:
    account_options = await _fetch_login_account_options(pending_only=True)
    if len(account_options) == 1:
        return str(account_options[0].get("auth_session") or "")
    if len(account_options) > 1:
        return "choice"

    for auth_session in (
        "default",
        "price",
        "yunda",
        f"{ACCOUNT_AUTH_SESSION_PREFIX}{SELF_PICKUP_PROBLEM_ACCOUNT_ID}",
    ):
        status = await _get_admin(_auth_session_path(auth_session, "status"))
        if not status.get("ok"):
            continue
        if status.get("pending_code") or str(status.get("status") or "").lower() == "pending_code":
            return auth_session
    return None


async def _submit_standalone_code(chat_id: str, code: str, auth_session: str) -> None:
    auth_session = _normalize_auth_session(auth_session)
    await _reply_text(chat_id, f"正在校验验证码（{_auth_session_label(auth_session)}），请稍等...")
    resp = await _post_admin(
        _auth_session_path(auth_session, "submit-code"),
        {"code": code},
    )
    if not resp.get("ok"):
        err = str(resp.get("error") or "验证码错误")[:200]
        await _reply_text(
            chat_id,
            f'登录失败：{err}\n请重新输入验证码，或回复"取消"放弃。',
        )
        return
    broker_status = str(resp.get("status") or "").lower()
    if broker_status != "authenticated":
        err = (
            str(resp.get("last_error_summary") or "").strip()
            or f"登录态校验未通过（broker status={broker_status or 'unknown'}）"
        )
        await _reply_text(
            chat_id,
            f'登录失败：{err}\n请重新输入验证码，或回复"取消"放弃。',
        )
        return
    await _reply_text(chat_id, "登录成功")


def _auth_result_text(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    chunks = [
        str(result.get("error_code") or ""),
        str(result.get("code") or ""),
        str(result.get("error") or ""),
        str(result.get("message") or ""),
        str(result.get("last_error_summary") or ""),
    ]
    data = result.get("data")
    if isinstance(data, dict):
        chunks.extend([
            str(data.get("error_code") or ""),
            str(data.get("code") or ""),
            str(data.get("error") or ""),
            str(data.get("message") or ""),
            str(data.get("last_error_summary") or ""),
        ])
        chunks.append(str(data))
    elif isinstance(data, str):
        chunks.append(data)
    return " ".join(chunks)


def _auth_state(result: dict[str, Any]) -> str:
    blob = _auth_result_text(result)
    if any(keyword in blob for keyword in AUTH_PENDING_CODE_KEYWORDS):
        return "pending_code"
    if any(keyword in blob for keyword in AUTH_REQUIRED_KEYWORDS):
        return "required"
    return ""


async def _auth_session_currently_authenticated(auth_session: str) -> bool:
    status_path = _auth_session_path(auth_session, "status")
    sep = "&" if "?" in status_path else "?"
    try:
        status = await _get_admin(f"{status_path}{sep}force=1")
    except Exception:
        logger.warning("Auth status force check failed: session=%s", auth_session, exc_info=True)
        return False
    status_text = str(status.get("status") or "").strip().lower()
    return bool(status.get("authenticated")) or status_text == "authenticated"


async def _execute_tool_with_stale_auth_retry(
    agent: Any,
    tool_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    result = await _submit_tool_command(agent, tool_name, params)
    if not _auth_state(result):
        return result
    auth_session = _auth_session_for_result(tool_name, params, result)
    if not await _auth_session_currently_authenticated(auth_session):
        return result
    logger.warning(
        "Tool reported auth failure while the forced account status is authenticated; "
        "leaving the governed Run blocked instead of retrying it. tool=%s session=%s",
        tool_name,
        auth_session,
    )
    # Never blind-retry a governed write. AccountManager publishes the real
    # session transition and ControlPlaneService resumes that same blocked Run.
    return result


def _legacy_read_request_id(tool_name: str, params: dict[str, Any]) -> str:
    """Stable identifier only for non-production direct helper/test calls."""

    encoded = json.dumps(
        {"tool_name": tool_name, "params": params},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"legacy-read-{hashlib.sha256(encoded).hexdigest()}"


def _tool_operation_type(agent: Any, tool_name: str) -> str:
    registry = getattr(agent, "registry", None)
    if registry is None or not hasattr(registry, "get_capability"):
        return ""
    try:
        capability = registry.get_capability(tool_name)
    except Exception:
        logger.warning("Failed to resolve governed capability for tool=%s", tool_name, exc_info=True)
        return "unknown"
    if not isinstance(capability, dict):
        return "unknown"
    return str(capability.get("operation_type") or "unknown").strip()


async def _submit_tool_command(
    agent: Any,
    tool_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Submit a Feishu command only through the injected Agent facade."""

    context = _COMMAND_CONTEXT.get()
    operation_type = _tool_operation_type(agent, tool_name)
    is_write = operation_type not in {"", "read", "compute"}
    if context is None and is_write:
        return {
            "success": False,
            "error_code": "FEISHU_EVENT_ID_REQUIRED",
            "error": "Feishu write commands require a stable event id",
        }

    event_id = context.event_id if context is not None else _legacy_read_request_id(tool_name, params)
    actor_id = context.actor_id if context is not None else "feishu-legacy-read"
    actor = Actor(
        ActorType.FEISHU_USER,
        actor_id,
        roles=(),
        authenticated_by="feishu_event",
    )
    keyword_arguments = {
        "actor": actor,
        "source": "feishu",
        "idempotency_key": f"feishu:{event_id}",
        "execution_context": {
            "feishu_chat_id": context.chat_id if context is not None else "",
            "feishu_event_id": event_id,
        },
    }

    submitted_run_id = ""

    def remember_submission(receipt: Any) -> None:
        nonlocal submitted_run_id
        if context is None or not isinstance(receipt, dict):
            return
        submitted_run_id = str(receipt.get("run_id") or "").strip()
        if not submitted_run_id:
            return
        set_pending(
            context.chat_id,
            {
                "type": "active_run",
                "run_id": submitted_run_id,
                "tool_name": tool_name,
                "description": _tool_display_name(tool_name),
                "originator_actor_id": context.actor_id,
                "status": str(receipt.get("status") or "RECEIVED"),
            },
            ttl_sec=ACTIVE_RUN_PENDING_TTL,
        )

    result = await agent.execute_tool(
        tool_name,
        params,
        **keyword_arguments,
        on_submitted=remember_submission,
    )
    if context is not None and isinstance(result, dict):
        run_id = str(result.get("run_id") or submitted_run_id).strip()
        status = str(result.get("status") or "").strip().upper()
        current = get_pending(context.chat_id)
        if (
            run_id
            and status in RUN_TERMINAL_STATUSES
            and isinstance(current, dict)
            and current.get("type") == "active_run"
            and str(current.get("run_id") or "") == run_id
        ):
            clear_pending(context.chat_id)
    return result


def _message_kind(text: str) -> str:
    if parse_verify_code(text):
        return "verify_code"
    if is_confirm_text(text):
        return "confirm"
    if is_cancel_text(text):
        return "cancel"
    if parse_login_send_code_session(text):
        return "login"
    return "text"


def _pending_type(pending: dict[str, Any] | None) -> str:
    if not isinstance(pending, dict):
        return "none"
    return str(pending.get("type") or "confirm_action")


def _tool_display_name(tool_name: str) -> str:
    return TOOL_DISPLAY_NAMES.get(str(tool_name or "").strip(), str(tool_name or "任务").strip() or "任务")


def _cancel_tool_name_from_text(text: str) -> str | None:
    return _shared_cancel_tool_name_from_text(text)


def _running_tool_info(agent: Any, tool_name: str) -> dict[str, Any]:
    try:
        if hasattr(agent, "running_tool_info"):
            info = agent.running_tool_info(tool_name)
            if isinstance(info, dict):
                return info
        if hasattr(agent, "is_tool_running") and agent.is_tool_running(tool_name):
            return {"running": True, "started_at": "", "cancel_requested": False}
    except Exception:
        logger.warning("检查工具运行状态失败: tool=%s", tool_name, exc_info=True)
    return {"running": False, "started_at": "", "cancel_requested": False}


async def _reply_if_tool_running(
    agent: Any,
    receive_id: str,
    tool_name: str,
    *,
    receive_id_type: str = "chat_id",
) -> bool:
    info = _running_tool_info(agent, tool_name)
    if not info.get("running"):
        return False
    label = _tool_display_name(tool_name)
    if receive_id_type == "chat_id":
        suffix = "请由原发起人在绑定该 Run 的会话中取消，或到事项中心处理。"
    else:
        suffix = "请先等待完成，或到控制台取消当前任务。"
    await _reply_text(
        receive_id,
        f"{label}失败：脚本正在执行中，{suffix}",
        receive_id_type=receive_id_type,
        reply_type=f"tool_already_running:{tool_name}",
    )
    return True


async def _cancel_running_tool_and_reply(
    agent: Any,
    chat_id: str,
    tool_name: str,
    *,
    started_at: str = "",
) -> None:
    label = _tool_display_name(tool_name)
    try:
        result = await agent.cancel_tool(tool_name, started_at=started_at)
    except Exception as exc:
        safe_error = redact_text(exc)[:200]
        logger.error("取消工具失败: tool=%s error=%s", tool_name, safe_error)
        await _reply_text(chat_id, f"{label}取消失败：{safe_error}", reply_type=f"tool_cancel:{tool_name}")
        return
    message = str(result.get("message") or ("已发送取消请求，正在停止脚本。" if result.get("ok") else "取消失败")).strip()
    await _reply_text(chat_id, f"{label}：{message}", reply_type=f"tool_cancel:{tool_name}")


async def _cancel_active_run_and_reply(
    agent: Any,
    chat_id: str,
    sender_id: str,
    pending: dict[str, Any],
) -> None:
    run_id = str(pending.get("run_id") or "").strip()
    originator = str(pending.get("originator_actor_id") or "").strip()
    context = _COMMAND_CONTEXT.get()
    if (
        context is None
        or context.actor_id != str(sender_id or "").strip()
        or not run_id
        or not originator
        or originator != context.actor_id
    ):
        await _reply_text(chat_id, "取消失败：无法验证原任务发起人。", reply_type="run_cancel_forbidden")
        return
    result = await agent.cancel_feishu_run(run_id, actor_id=context.actor_id)
    if result.get("ok"):
        clear_pending(chat_id)
        await _reply_text(chat_id, "已发送取消请求，正在停止原事项运行。", reply_type="run_cancel")
        return
    await _reply_text(
        chat_id,
        f"取消失败：{str(result.get('error') or '未知错误')[:200]}",
        reply_type="run_cancel",
    )


def _is_auth_required(result: dict[str, Any]) -> bool:
    return bool(_auth_state(result))


async def _request_relogin(
    chat_id: str,
    *,
    resume_tool: str,
    resume_params: dict[str, Any],
    auth_session: str | None = None,
) -> None:
    auth_session = _normalize_auth_session(auth_session or _auth_session_for_tool(resume_tool, resume_params))
    set_pending(
        chat_id,
        {
            "type": "confirm_login_for_resume",
            "auth_session": auth_session,
            "resume_tool": resume_tool,
            "resume_params": resume_params,
        },
        ttl_sec=LOGIN_PENDING_TTL,
    )
    await _reply_text(
        chat_id,
        "登录过期需要重新登录。\n"
        "是否现在发送登录验证码？\n"
        '回复"是"立即发送，回复"否"取消。',
        reply_type="auth_prompt",
    )


async def _request_pending_code_resume(
    chat_id: str,
    *,
    resume_tool: str,
    resume_params: dict[str, Any],
    auth_session: str | None = None,
) -> None:
    auth_session = _normalize_auth_session(auth_session or _auth_session_for_tool(resume_tool, resume_params))
    set_pending(
        chat_id,
        {
            "type": "waiting_code_for_resume",
            "auth_session": auth_session,
            "resume_tool": resume_tool,
            "resume_params": resume_params,
        },
        ttl_sec=LOGIN_PENDING_TTL,
    )
    await _reply_text(
        chat_id,
        "登录验证码已生成，请直接回复验证码。\n"
        '如需取消，回复"取消"。',
        reply_type="send_code_prompt",
    )


async def _send_code_and_wait_resume(
    chat_id: str,
    *,
    resume_tool: str,
    resume_params: dict[str, Any],
    auth_session: str | None = None,
) -> None:
    auth_session = _normalize_auth_session(auth_session or _auth_session_for_tool(resume_tool, resume_params))
    await _send_code_and_wait(
        chat_id,
        auth_session=auth_session,
        resume_tool=resume_tool,
        resume_params=resume_params,
    )


def _auth_session_uses_auto_image(auth_session: str) -> bool:
    normalized = _normalize_auth_session(auth_session)
    account_id = _auth_account_id(normalized).lower()
    if account_id.startswith("yunda"):
        return False
    if account_id.startswith(("ronghui", "price")):
        return True
    return normalized != "yunda"


def _challenge_prompt(resp: dict[str, Any]) -> str:
    challenge_type = str(resp.get("challenge_type") or "").strip().lower()
    challenge_label = str(resp.get("challenge_label") or "").strip() or "验证码"
    account_name = str(resp.get("account_name") or "").strip()
    prefix = f"{account_name} " if account_name else ""
    if challenge_type == "image":
        fallback_summary = str(resp.get("last_error_summary") or "").strip()
        summary = f"{fallback_summary}\n" if fallback_summary else ""
        return (
            f"{summary}{prefix}{challenge_label}已生成，请到后台账号管理页面查看图片后直接回复验证码。\n"
            '如需取消，回复"取消"。'
        )
    return (
        f"{prefix}{challenge_label}已发送至绑定手机，请直接回复 6 位数字验证码。\n"
        '如需取消，回复"取消"。'
    )


async def _send_code_and_wait(
    chat_id: str,
    *,
    auth_session: str = "default",
    resume_tool: str | None = None,
    resume_params: dict[str, Any] | None = None,
    agent: Any | None = None,
) -> None:
    auth_session = _normalize_auth_session(auth_session)
    auto_image_login = _auth_session_uses_auto_image(auth_session)
    await _reply_text(
        chat_id,
        (
            f"正在自动识别图片验证码并登录（{_auth_session_label(auth_session)}），请稍候..."
            if auto_image_login
            else f"正在处理登录（{_auth_session_label(auth_session)}），请稍候..."
        ),
        reply_type="send_code_start",
    )
    resp = await _post_admin(_auth_session_path(auth_session, "send-code"))
    if not resp.get("ok"):
        default_error = "自动登录失败" if auto_image_login else "登录处理失败"
        err = str(resp.get("error") or default_error)[:200]
        await _reply_text(chat_id, f"{default_error}：{err}", reply_type="send_code_error")
        return

    status = str(resp.get("status") or "").strip().lower()
    authenticated = bool(resp.get("authenticated")) or status == "authenticated"
    pending_code = bool(resp.get("pending_code")) or status == "pending_code"
    if authenticated and not pending_code:
        clear_pending(chat_id)
        if resume_tool:
            await _reply_text(
                chat_id,
                "登录成功，原事项运行已恢复；请在事项中心查看进度。",
                reply_type="login_success",
            )
            return
        await _reply_text(chat_id, "登录成功", reply_type="login_success")
        return

    if not pending_code and str(resp.get("status_tone") or "").strip().lower() == "success":
        clear_pending(chat_id)
        await _reply_text(chat_id, str(resp.get("label") or "账号凭据验证通过"), reply_type="login_success")
        return

    set_pending(
        chat_id,
        {
            "type": "waiting_code_for_resume",
            "auth_session": auth_session,
            "resume_tool": resume_tool,
            "resume_params": resume_params or {},
        },
        ttl_sec=LOGIN_PENDING_TTL,
    )
    await _reply_text(chat_id, _challenge_prompt(resp), reply_type="send_code_prompt")


async def _handle_auth_result(
    chat_id: str,
    *,
    result: dict[str, Any],
    resume_tool: str,
    resume_params: dict[str, Any],
) -> bool:
    state = _auth_state(result)
    auth_session = _auth_session_for_result(resume_tool, resume_params, result)
    if state and await _auth_session_currently_authenticated(auth_session):
        logger.warning(
            "Suppressing auth recovery because forced admin status is authenticated. tool=%s session=%s state=%s",
            resume_tool,
            auth_session,
            state,
        )
        return False
    if state == "pending_code":
        await _send_code_and_wait_resume(
            chat_id,
            resume_tool=resume_tool,
            resume_params=resume_params,
            auth_session=auth_session,
        )
        return True
    if state == "required":
        if auth_session == "price":
            await _send_code_and_wait_resume(
                chat_id,
                resume_tool=resume_tool,
                resume_params=resume_params,
                auth_session=auth_session,
            )
            return True
        await _request_relogin(
            chat_id,
            resume_tool=resume_tool,
            resume_params=resume_params,
            auth_session=auth_session,
        )
        return True
    return False


async def _handle_agent_tool_auth_result(chat_id: str, agent_result: dict[str, Any]) -> bool:
    executed_tools = agent_result.get("executed_tools")
    if not isinstance(executed_tools, list):
        return False
    for item in executed_tools:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "").strip()
        tool_result = item.get("result")
        if not tool_name or not isinstance(tool_result, dict):
            continue
        if await _handle_auth_result(
            chat_id,
            result=tool_result,
            resume_tool=tool_name,
            resume_params=item.get("params") if isinstance(item.get("params"), dict) else {},
        ):
            return True
    return False


async def _reply_tool_result(
    chat_id: str,
    tool_name: str,
    result: dict[str, Any],
    *,
    receive_id_type: str = "chat_id",
) -> None:
    messages = format_tool_reply_messages(tool_name, result)
    for index, reply_text in enumerate(message for message in messages if str(message).strip()):
        reply_type = f"tool_reply:{tool_name}" if index == 0 else f"tool_reply:{tool_name}:{index + 1}"
        await _reply_text(chat_id, reply_text, receive_id_type=receive_id_type, reply_type=reply_type)


async def _send_after_delay(chat_id: str, text: str, delay_sec: float) -> None:
    """延迟发送一条临时提示；在 sleep 期间被取消则不发。"""
    try:
        await asyncio.sleep(delay_sec)
    except asyncio.CancelledError:
        return
    await _reply_text(chat_id, text)


async def _execute_and_reply(
    agent: Any,
    chat_id: str,
    tool_name: str,
    params: dict[str, Any],
) -> None:
    if tool_name in FIRST_PARTY_FEISHU_ROUTE_KEYS:
        await _reply_text(
            chat_id,
            "旧版自动化确认已失效，请重新发起该任务。",
            reply_type="automation_legacy_execution_rejected",
        )
        return
    if await _reply_if_tool_running(agent, chat_id, tool_name):
        return
    try:
        result = await _execute_tool_with_stale_auth_retry(agent, tool_name, params)
    except Exception as exc:
        safe_error = redact_text(exc)
        logger.error("飞书工具执行异常: tool=%s error=%s", tool_name, safe_error[:200])
        await _reply_text(
            chat_id,
            format_tool_reply(tool_name, {"success": False, "error": safe_error}),
        )
        return

    if not result.get("success", False):
        logger.error(
            "飞书工具执行失败: tool=%s error=%s",
            tool_name,
            str(result.get("error") or result)[:200],
        )

    if await _handle_auth_result(chat_id, result=result, resume_tool=tool_name, resume_params=params):
        return

    await _reply_tool_result(chat_id, tool_name, result)


def _event_id_from_sdk_event(event: Any, *, fallback: str = "") -> str:
    header = getattr(event, "header", None)
    if header is None:
        header = getattr(getattr(event, "event", None), "header", None)
    event_id = str(getattr(header, "event_id", "") or "").strip()
    return event_id or str(fallback or "").strip()


def handle_im_message(event):
    """飞书文本/图片消息回调。"""
    try:
        msg = event.event.message
        msg_type = msg.message_type
        chat_id = msg.chat_id
        sender_id = event.event.sender.sender_id.open_id
        event_id = _event_id_from_sdk_event(event)
        remember_chat_id(chat_id)

        logger.info("收到消息: type=%s, chat=%s, sender=%s", msg_type, chat_id, sender_id)
        _submit_with_future_callback(_handle_im_message_data(
            msg_type=msg_type,
            chat_id=chat_id,
            sender_id=sender_id,
            raw_content=msg.content,
            event_id=event_id,
        ))

    except Exception as e:
        logger.error("消息处理异常: %s", str(e)[:200])


def queue_im_message_payload(event_payload: dict[str, Any], *, event_id: str = "") -> bool:
    """将 Webhook 文本/图片消息投递到当前事件循环。"""
    message = event_payload.get("message") or {}
    sender = event_payload.get("sender") or {}

    msg_type = str(message.get("message_type", "") or "").strip()
    chat_id = str(message.get("chat_id", "") or "").strip()
    sender_id = _extract_open_or_user_id(sender.get("sender_id"))
    stable_event_id = str(event_id or "").strip()

    if not msg_type or not chat_id:
        logger.warning("飞书 Webhook 消息缺少 message_type/chat_id: %s", event_payload)
        return False

    logger.info("收到 Webhook 消息: type=%s, chat=%s, sender=%s", msg_type, chat_id, sender_id)
    remember_chat_id(chat_id)
    _schedule_local_task(_handle_im_message_data(
        msg_type=msg_type,
        chat_id=chat_id,
        sender_id=sender_id,
        raw_content=message.get("content"),
        event_id=stable_event_id,
    ))
    return True


def handle_bot_menu(event):
    """飞书机器人菜单点击事件：直接触发工具，不走消息模拟。"""
    try:
        event_key = str(getattr(event.event, "event_key", "") or "").strip()
        operator = getattr(event.event, "operator", None)
        operator_id = getattr(operator, "operator_id", None)
        open_id = str(getattr(operator_id, "open_id", "") or "").strip()
        user_id = str(getattr(operator_id, "user_id", "") or "").strip()
        receive_id = open_id or user_id
        receive_id_type = "open_id" if open_id else "user_id"
        event_id = _event_id_from_sdk_event(event)
        _submit_with_future_callback(_handle_menu_action(
            event_key=event_key,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            event_id=event_id,
        ))
    except Exception as e:
        logger.error("菜单事件处理异常: %s", str(e)[:200])


def queue_bot_menu_payload(event_payload: dict[str, Any], *, event_id: str = "") -> bool:
    """将 Webhook 菜单事件投递到当前事件循环。"""
    event_key = str(event_payload.get("event_key", "") or "").strip()
    operator = event_payload.get("operator") or {}
    operator_id = operator.get("operator_id") or event_payload.get("operator_id") or {}
    open_id = str(operator_id.get("open_id", "") or "").strip()
    user_id = str(operator_id.get("user_id", "") or "").strip()
    receive_id = open_id or user_id
    receive_id_type = "open_id" if open_id else "user_id"

    if not event_key:
        logger.warning("飞书 Webhook 菜单事件缺少 event_key: %s", event_payload)
        return False

    logger.info("收到 Webhook 菜单事件: event_key=%s, receive_id=%s", event_key, receive_id)
    _schedule_local_task(_handle_menu_action(
        event_key=event_key,
        receive_id=receive_id,
        receive_id_type=receive_id_type,
        event_id=str(event_id or "").strip(),
    ))
    return True


async def _handle_im_message_data(
    *,
    msg_type: str,
    chat_id: str,
    sender_id: str,
    raw_content: Any,
    event_id: str = "",
):
    stable_event_id = str(event_id or "").strip()
    token: Token[FeishuCommandContext | None] = _COMMAND_CONTEXT.set(
        FeishuCommandContext(
            event_id=stable_event_id,
            actor_id=str(sender_id or "").strip(),
            chat_id=str(chat_id or "").strip(),
        )
        if stable_event_id
        else None
    )
    try:
        await _handle_im_message_data_inner(
            msg_type=msg_type,
            chat_id=chat_id,
            sender_id=sender_id,
            raw_content=raw_content,
        )
    finally:
        _COMMAND_CONTEXT.reset(token)


async def _handle_im_message_data_inner(
    *,
    msg_type: str,
    chat_id: str,
    sender_id: str,
    raw_content: Any,
):
    normalized_type = str(msg_type or "").strip().lower()

    if normalized_type == "text":
        text = _extract_text(raw_content)
        if not text:
            return
        await _process_and_reply(text, sender_id, chat_id)
        return

    if normalized_type == "image":
        await _reply_text(chat_id, "图片识别功能开发中，敬请期待")


async def _handle_menu_action(
    *,
    event_key: str,
    receive_id: str,
    receive_id_type: str,
    event_id: str = "",
):
    stable_event_id = str(event_id or "").strip()
    token: Token[FeishuCommandContext | None] = _COMMAND_CONTEXT.set(
        FeishuCommandContext(
            event_id=stable_event_id,
            actor_id=str(receive_id or "").strip(),
            chat_id=str(receive_id or "").strip(),
        )
        if stable_event_id
        else None
    )
    try:
        await _handle_menu_action_inner(
            event_key=event_key,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        )
    finally:
        _COMMAND_CONTEXT.reset(token)


async def _handle_menu_action_inner(
    *,
    event_key: str,
    receive_id: str,
    receive_id_type: str,
):
    menu_action = _menu_action_from_key(event_key)
    if not menu_action:
        logger.warning("未识别的机器人菜单 event_key: %s", event_key)
        return

    route_key = str(menu_action.get("automation_route_key") or "").strip()
    if route_key:
        if route_key == SCAN_FEISHU_ROUTE_KEY:
            scan_pending = get_pending(receive_id)
            if (
                isinstance(scan_pending, dict)
                and scan_pending.get("type") == "scan_preview_confirmation"
            ):
                state = str(
                    scan_pending.get("confirmation_state") or "pending"
                )
                if state in {"submitting", "unknown", "terminal"}:
                    reply = _scan_preview_error_message(
                        scan_pending.get("terminal_error_code")
                    )
                    reply_type = "scan_preview_confirmation_locked"
                else:
                    reply = (
                        "已有扫描预览正在等待确认，请在聊天中明确回复“确认扫描”"
                        "或“取消扫描”；本次没有创建新预览。"
                    )
                    reply_type = "scan_preview_confirmation_required"
                await _reply_text(
                    receive_id,
                    reply,
                    receive_id_type=receive_id_type,
                    reply_type=reply_type,
                )
                return
        await _invoke_automation_project_and_reply(
            route_key=route_key,
            dynamic_inputs=dict(menu_action.get("dynamic_inputs") or {}),
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        )
        return

    await _run_deferred_tool(
        tool_name=menu_action["tool_name"],
        params=menu_action["params"],
        receive_id=receive_id,
        receive_id_type=receive_id_type,
    )


async def _process_and_reply(text: str, sender_id: str, chat_id: str):
    """处理文本消息并回复。优先处理待确认动作 / 登录恢复，其次确定性命令，最后交给 Agent。"""
    from feishu.bot import get_agent_core
    from feishu.reply_formatter import format_reply

    if _FEISHU_APPROVAL_RUNTIME is not None:
        try:
            approval_reply = await asyncio.to_thread(
                _FEISHU_APPROVAL_RUNTIME.handle_text,
                str(sender_id or ""),
                str(chat_id or ""),
                str(text or ""),
            )
        except OrchestrationError as exc:
            await _reply_text(chat_id, f"审批处理失败（{exc.code}）。")
            return
        if approval_reply is not None:
            await _reply_text(chat_id, approval_reply, reply_type="feishu_approval")
            return

    agent = get_agent_core()
    if not agent:
        await _reply_text(chat_id, "Agent 尚未初始化，请稍后再试")
        return

    pending_key = chat_id
    pending = get_pending(pending_key)
    if sender_id:
        sender_pending = pending if sender_id == chat_id else get_pending(sender_id)
        if (
            isinstance(sender_pending, dict)
            and sender_pending.get("type") == "scan_preview_confirmation"
        ):
            pending_key = sender_id
            pending = sender_pending
    logger.info(
        "feishu inbound | chat=%s | sender=%s | message_kind=%s | pending=%s",
        chat_id,
        sender_id,
        _message_kind(text),
        _pending_type(pending),
    )
    login_session = parse_login_send_code_session(text)
    if login_session:
        if (
            isinstance(pending, dict)
            and pending.get("type") == "scan_preview_confirmation"
            and str(pending.get("confirmation_state") or "pending")
            in {"submitting", "unknown", "terminal"}
        ):
            await _reply_text(
                chat_id,
                "正式扫描请求状态仍需核实，请先前往事项中心查看原任务。",
                reply_type="scan_preview_confirmation_locked",
            )
            return
        logger.info("feishu route | chat=%s | route=login_command | session=%s", chat_id, login_session)
        if (
            isinstance(pending, dict)
            and pending.get("type") == "scan_preview_confirmation"
        ):
            clear_pending(pending_key, volatile_only=True)
        else:
            clear_pending(pending_key)
        if login_session == "choice":
            await _begin_login_account_choice(chat_id)
            return
        resolved_session = await _resolve_login_command_session(login_session)
        await _send_code_and_wait(chat_id, auth_session=resolved_session)
        return

    if isinstance(pending, dict) and pending.get("type") == "scan_preview_confirmation":
        state = str(pending.get("confirmation_state") or "pending")
        originator = str(pending.get("originator_actor_id") or "").strip()
        if _is_scan_cancel_text(text):
            if originator != str(sender_id or "").strip():
                await _reply_text(
                    chat_id,
                    "只有生成本次扫描预览的用户可以取消确认。",
                    reply_type="scan_preview_actor_mismatch",
                )
                return
            if state in {"submitting", "unknown", "terminal"}:
                await _reply_text(
                    chat_id,
                    "正式请求状态仍需核实，请前往事项中心查看原任务；当前确认状态不会被清除。",
                    reply_type="scan_preview_confirmation_locked",
                )
                return
            clear_pending(pending_key, volatile_only=True)
            await _reply_text(
                chat_id,
                "已取消本次扫描预览；没有提交正式扫描。",
                reply_type="scan_preview_cancelled",
            )
            return
        if _is_scan_confirm_text(text):
            await _confirm_scan_preview_and_reply(
                chat_id=chat_id,
                pending_key=pending_key,
                sender_id=sender_id,
                pending=pending,
            )
            return
        if state in {"submitting", "unknown", "terminal"}:
            await _reply_text(
                chat_id,
                _scan_preview_error_message(pending.get("terminal_error_code")),
                reply_type="scan_preview_confirmation_locked",
            )
            return
        await _reply_text(
            chat_id,
            "扫描预览仍在等待确认，请明确回复“确认扫描”或“取消扫描”。",
            reply_type="scan_preview_confirmation_required",
        )
        return

    cancel_tool_name = _cancel_tool_name_from_text(text)
    if cancel_tool_name:
        logger.info("feishu route | chat=%s | route=cancel_running_tool | tool=%s", chat_id, cancel_tool_name)
        if isinstance(pending, dict) and pending.get("type") == "active_run":
            if str(pending.get("tool_name") or "") == cancel_tool_name:
                await _cancel_active_run_and_reply(agent, chat_id, sender_id, pending)
                return
        await _reply_text(chat_id, "取消失败：当前消息没有绑定可取消的事项运行。")
        return

    if _is_scan_confirm_text(text):
        await _reply_text(
            chat_id,
            "当前没有可确认的扫描预览，请先发送“扫描”。",
            reply_type="scan_preview_missing",
        )
        return

    if str(text or "").strip() == "分批" and pending is not None:
        if str(pending.get("type") or "") in {"split_pending_selection", "split_pending_confirmation"}:
            clear_pending(chat_id)
            pending = None
            logger.info("feishu route | chat=%s | route=split_restart_preview", chat_id)

    if pending is not None:
        ptype = str(pending.get("type") or "confirm_action")
        logger.info("feishu route | chat=%s | route=pending | pending=%s", chat_id, ptype)

        if ptype == "active_run":
            if is_cancel_text(text):
                await _cancel_active_run_and_reply(agent, chat_id, sender_id, pending)
                return
            await _reply_text(
                chat_id,
                f"原事项仍在处理中（Run {str(pending.get('run_id') or '')}）。如需停止，请回复“取消”。",
            )
            return

        if ptype == "split_pending_selection":
            if not await _selection_pending_actor_allowed(
                chat_id=chat_id,
                sender_id=sender_id,
                pending=pending,
            ):
                return
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消：分批问题件")
                return
            if _contains_account_override(pending) or not str(
                pending.get("automation_route_key") or ""
            ).strip() or not str(pending.get("preview_run_id") or "").strip():
                clear_pending(chat_id)
                await _reply_text(
                    chat_id,
                    "旧版分批预览已失效，请重新发送“分批”。",
                    reply_type="split_preview_stale",
                )
                return
            candidates = pending.get("candidates") if isinstance(pending.get("candidates"), list) else []
            if is_confirm_text(text):
                selected_codes = [str(item.get("bill_code") or "").strip() for item in candidates]
                if not selected_codes or not all(selected_codes):
                    await _reply_text(chat_id, "候选列表数据无效，请重新发送“分批”。")
                    clear_pending(chat_id)
                    return
                await _execute_split_formal(
                    agent,
                    chat_id,
                    pending,
                    selected_codes,
                    running_message=(
                        "分批问题件任务正在执行中；当前列表仍保留，请稍后再次回复“确认”。"
                    ),
                )
                return
            try:
                selected_numbers = _parse_split_selection(text, len(candidates))
            except ValueError as exc:
                await _reply_text(
                    chat_id,
                    f"选择无效：{exc}\n当前列表仍保留，请重新输入序号或回复“取消”。",
                    reply_type="split_selection_error",
                )
                return
            selected = [candidates[index - 1] for index in selected_numbers]
            selected_codes = [str(item.get("bill_code") or "").strip() for item in selected]
            if not all(selected_codes):
                await _reply_text(chat_id, "候选列表数据无效，请重新发送“分批”。")
                clear_pending(chat_id)
                return
            ttl = _selection_confirmation_ttl(pending)
            if ttl <= 0:
                clear_pending(chat_id)
                await _reply_text(
                    chat_id,
                    "分批候选清单已过期，请重新发送“分批”。",
                    reply_type="split_preview_stale",
                )
                return
            set_pending(
                chat_id,
                {
                    "type": "split_pending_confirmation",
                    "tool_name": SPLIT_TOOL_NAME,
                    "automation_route_key": pending.get("automation_route_key"),
                    "originator_actor_id": pending.get("originator_actor_id"),
                    "selected_bill_codes": selected_codes,
                    "preview_run_id": pending.get("preview_run_id"),
                    "expires_at": pending.get("expires_at"),
                    "selected": selected,
                },
                ttl_sec=ttl,
            )
            await _reply_split_lines(
                chat_id,
                _split_selected_lines(selected),
                reply_type="split_selection_echo",
            )
            return

        if ptype == "split_pending_confirmation":
            if not await _selection_pending_actor_allowed(
                chat_id=chat_id,
                sender_id=sender_id,
                pending=pending,
            ):
                return
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消：分批问题件")
                return
            if is_confirm_text(text):
                await _execute_split_formal(
                    agent,
                    chat_id,
                    pending,
                    list(pending.get("selected_bill_codes") or []),
                    running_message=(
                        "分批问题件任务正在执行中；当前选择仍保留，请稍后再次回复“确认”。"
                    ),
                )
                return
            await _reply_text(chat_id, "当前选择已保留，请回复“确认”执行或回复“取消”放弃。")
            return

        if ptype == "self_pickup_selection_confirmation":
            if not await _selection_pending_actor_allowed(
                chat_id=chat_id,
                sender_id=sender_id,
                pending=pending,
            ):
                return
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消：自提到货问题件")
                return
            if is_confirm_text(text):
                if _selection_confirmation_ttl(pending) <= 0:
                    clear_pending(chat_id)
                    await _reply_text(
                        chat_id,
                        "自提到货问题件候选清单已过期，请重新发起任务。",
                        reply_type="self_pickup_preview_stale",
                    )
                    return
                route_key = str(pending.get("automation_route_key") or "").strip()
                preview_run_id = str(pending.get("preview_run_id") or "").strip()
                selected_bill_codes = list(pending.get("selected_bill_codes") or [])
                if (
                    _contains_account_override(pending)
                    or not route_key
                    or not preview_run_id
                    or not selected_bill_codes
                ):
                    clear_pending(chat_id)
                    await _reply_text(
                        chat_id,
                        "自提到货问题件候选清单已失效，请重新发起任务。",
                        reply_type="automation_confirmation_stale",
                    )
                    return
                clear_pending(chat_id)
                await _invoke_automation_project_and_reply(
                    route_key=route_key,
                    dynamic_inputs={
                        "selected_bill_codes": selected_bill_codes,
                    },
                    receive_id=chat_id,
                    preview_run_id=preview_run_id,
                )
                return
            await _reply_text(
                chat_id,
                "自提到货问题件候选已保留，请回复“确认”上传全部，或回复“取消”放弃。",
            )
            return

        if ptype == "confirm_action":
            if is_confirm_text(text):
                route_key = str(pending.get("automation_route_key") or "").strip()
                dynamic_inputs = dict(pending.get("dynamic_inputs") or {})
                legacy_tool_name = str(pending.get("tool_name") or "").strip()
                legacy_params = dict(pending.get("params") or {})
                has_account_override = _contains_account_override(pending)
                clear_pending(chat_id)
                if route_key:
                    if has_account_override:
                        await _reply_text(
                            chat_id,
                            "旧版确认状态已失效，请重新发起自动化任务。",
                            reply_type="automation_confirmation_stale",
                        )
                        return
                    await _invoke_automation_project_and_reply(
                        route_key=route_key,
                        dynamic_inputs=dynamic_inputs,
                        receive_id=chat_id,
                    )
                    return
                if await _reply_if_tool_running(agent, chat_id, legacy_tool_name):
                    return
                await _reply_text(
                    chat_id,
                    f"已开始执行：{TOOL_DISPLAY_NAMES.get(legacy_tool_name, legacy_tool_name or '自动化任务')}。完成后我会反馈结果。",
                    reply_type=f"tool_start:{legacy_tool_name or 'unknown'}",
                )
                await _execute_and_reply(
                    agent,
                    chat_id,
                    legacy_tool_name,
                    legacy_params,
                )
                return
            if is_cancel_text(text):
                clear_pending(chat_id)
                description = str(pending.get("description") or "操作")
                await _reply_text(chat_id, f"已取消：{description}")
                return

        elif ptype == "cancel_running_tool":
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "旧取消状态已失效，请从事项中心按 Run 取消。")
                return
            if is_confirm_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, f"继续等待：{pending.get('description') or '当前任务'}")
                return

        elif ptype == "login_account_choice":
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消登录")
                return
            options = pending.get("options") if isinstance(pending.get("options"), list) else []
            selected_session = _resolve_login_account_choice(text, options)
            legacy_session = parse_login_account_choice(text) or parse_login_send_code_session(text)
            if not selected_session and legacy_session and legacy_session != "choice":
                selected_session = await _resolve_login_command_session(legacy_session)
            if not selected_session and not options:
                options = await _fetch_login_account_options()
                selected_session = _resolve_login_account_choice(text, options)
            if selected_session == "choice":
                await _begin_login_account_choice(chat_id, options=options)
                return
            if selected_session:
                clear_pending(chat_id)
                await _send_code_and_wait(chat_id, auth_session=selected_session)
                return
            await _reply_text(chat_id, _format_login_account_choice(options))
            return

        elif ptype == "r7_departure_plate_choice":
            clear_pending(chat_id)
            await _reply_text(
                chat_id,
                "R7 发车打卡自动化已移除，本次旧操作已清理。",
                reply_type="removed_automation_pending_cleared",
            )
            return

        elif ptype == "confirm_login_for_resume":
            if is_confirm_text(text):
                clear_pending(chat_id)
                auth_session = str(pending.get("auth_session") or _auth_session_for_tool(pending.get("resume_tool"))).strip()
                await _send_code_and_wait(
                    chat_id,
                    auth_session=auth_session,
                    resume_tool=pending.get("resume_tool"),
                    resume_params=pending.get("resume_params") or {},
                    agent=agent,
                )
                return
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消重新登录")
                return

        elif ptype == "waiting_code_for_resume":
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消重新登录")
                return
            code = parse_verify_code(text)
            if code:
                await _reply_text(chat_id, "正在校验验证码，请稍候...")
                auth_session = str(pending.get("auth_session") or _auth_session_for_tool(pending.get("resume_tool"))).strip()
                resp = await _post_admin(
                    _auth_session_path(auth_session, "submit-code"),
                    {"code": code},
                )
                if not resp.get("ok"):
                    err = str(resp.get("error") or "验证码错误")[:200]
                    await _reply_text(
                        chat_id,
                        f'登录失败：{err}\n请重新输入验证码，或回复"取消"放弃。',
                    )
                    return
                # 即使 ok=True，broker 内部 _validate_locked 可能把状态校验成
                # expired/error/logged_out。这里严格按 status 字段判断，不能误报登录成功。
                broker_status = str(resp.get("status") or "").lower()
                if broker_status != "authenticated":
                    err = (
                        str(resp.get("last_error_summary") or "").strip()
                        or f"登录态校验未通过（broker status={broker_status or 'unknown'}）"
                    )
                    logger.warning(
                        "submit-code 接口 ok=True 但 broker 状态非 authenticated: status=%s payload=%s",
                        broker_status, str(resp)[:300],
                    )
                    await _reply_text(
                        chat_id,
                        f'登录失败：{err}\n请重新输入验证码，或回复"取消"放弃。',
                    )
                    return
                resume_tool = pending.get("resume_tool")
                clear_pending(chat_id)
                if resume_tool:
                    await _reply_text(
                        chat_id,
                        "登录成功，原事项运行已恢复；请在事项中心查看进度。",
                    )
                else:
                    await _reply_text(chat_id, "登录成功")
                return

    if pending is None:
        code = parse_verify_code(text)
        if code:
            auth_session = await _pending_code_auth_session()
            if auth_session:
                if auth_session == "choice":
                    options = await _fetch_login_account_options(pending_only=True)
                    await _begin_login_account_choice(chat_id, options=options)
                    return
                await _submit_standalone_code(chat_id, code, auth_session)
                return

    if is_deprecated_split_command(text):
        logger.info("feishu route | chat=%s | route=deprecated_split_command", chat_id)
        await _reply_text(chat_id, "该指令已停用，请只发送“分批”。")
        return

    direct_request = direct_tool_request_from_text(text)
    if direct_request and pending is not None:
        ptype = str(pending.get("type") or "")
        if ptype in {"confirm_login_for_resume", "waiting_code_for_resume"}:
            logger.info("feishu route | chat=%s | route=direct_tool_clear_stale_login_pending | pending=%s", chat_id, ptype)
            clear_pending(chat_id)
            pending = None
    if direct_request:
        tool_name = direct_request["tool_name"]
        params = direct_request["params"]
        mode = direct_request["mode"]
        automation_route_key = str(
            direct_request.get("automation_route_key") or ""
        ).strip()
        dynamic_inputs = direct_request.get("dynamic_inputs")
        if not isinstance(dynamic_inputs, dict):
            dynamic_inputs = {}
        local_result = direct_request.get("local_result")
        logger.info("feishu route | chat=%s | route=direct_tool | tool=%s | mode=%s", chat_id, tool_name, mode)

        if await dispatch_migrated_fixed_feishu_entrypoint(
            mode=mode,
            automation_route_key=automation_route_key,
            tool_name=tool_name,
            command_text=text,
            receive_id=chat_id,
            dispatcher=_SERVICE_V2_FEISHU_DISPATCHER,
            dispatch_service_v2=_dispatch_service_v2_feishu_command,
            reply_text=_reply_text,
        ):
            return

        if isinstance(local_result, dict):
            await _reply_tool_result(chat_id, tool_name, local_result)
            return

        if mode == "automation_project":
            await _invoke_automation_project_and_reply(
                route_key=automation_route_key,
                dynamic_inputs=dynamic_inputs,
                receive_id=chat_id,
            )
            return

        if mode == "automation_preview":
            await _invoke_selection_preview_and_reply(
                route_key=automation_route_key,
                tool_name=tool_name,
                receive_id=chat_id,
            )
            return

        if mode == "deferred":
            if await _reply_if_tool_running(agent, chat_id, tool_name):
                return
            await _reply_text(
                chat_id,
                f"已开始执行：{TOOL_DISPLAY_NAMES.get(tool_name, tool_name)}。完成后我会反馈结果。",
                reply_type=f"tool_start:{tool_name}",
            )
            await _execute_and_reply(agent, chat_id, tool_name, params)
            return

        if await _reply_if_tool_running(agent, chat_id, tool_name):
            return

        if tool_name == "track_waybill":
            tracking_number = str(params.get("tracking_number") or "").strip()
            await _reply_text(
                chat_id,
                f"正在查询单号：{tracking_number}" if tracking_number else "正在查询单号",
                reply_type="tool_start:track_waybill",
            )
        elif tool_name == "get_price":
            await _reply_text(
                chat_id,
                "正在查询融辉和韵达价格，完成后我会反馈结果。",
                reply_type="tool_start:get_price",
            )

        try:
            result = await _execute_tool_with_stale_auth_retry(agent, tool_name, params)
        except Exception as e:
            logger.error("飞书指令执行异常: tool=%s error=%s", tool_name, str(e)[:200])
            await _reply_text(chat_id, format_tool_reply(tool_name, {"success": False, "error": str(e)}))
            return

        if not result.get("success", False):
            logger.error("飞书指令执行失败: tool=%s error=%s", tool_name, str(result.get("error") or result)[:200])

        if await _handle_auth_result(chat_id, result=result, resume_tool=tool_name, resume_params=params):
            return

        await _reply_tool_result(chat_id, tool_name, result)
        return

    if await _dispatch_service_v2_feishu_command(text=text, receive_id=chat_id):
        return

    logger.info("feishu route | chat=%s | route=agent_llm", chat_id)
    notice_task = asyncio.create_task(_send_after_delay(chat_id, "正在处理...", 1.5))
    try:
        feishu_actor = (
            await asyncio.to_thread(
                _FEISHU_APPROVAL_RUNTIME.resolve_actor,
                str(sender_id or ""),
            )
            if _FEISHU_APPROVAL_RUNTIME is not None
            else Actor(
                ActorType.FEISHU_USER,
                (
                    _COMMAND_CONTEXT.get().actor_id
                    if _COMMAND_CONTEXT.get() is not None
                    else str(sender_id or "feishu-legacy-read")
                ),
                roles=(),
                authenticated_by="feishu_event",
            )
        )
        result = await agent.handle_message(
            message=text,
            user_id=sender_id,
            conversation_id=f"feishu_{chat_id}",
            actor=feishu_actor,
            source="feishu",
            request_id=(
                _COMMAND_CONTEXT.get().event_id
                if _COMMAND_CONTEXT.get() is not None
                else _legacy_read_request_id("chat", {"message": text})
            ),
        )
    except Exception as e:
        notice_task.cancel()
        logger.error("Agent 处理失败: %s", str(e)[:200])
        await _reply_text(chat_id, f"处理失败: {str(e)[:100]}")
        return

    notice_task.cancel()
    if await _handle_agent_tool_auth_result(chat_id, result):
        return
    reply_text = format_reply(result.get("reply") or UNKNOWN_EXECUTION_REPLY)
    reply_type = "unknown_no_tool" if reply_text == UNKNOWN_EXECUTION_REPLY else "agent_reply"
    await _reply_text(chat_id, reply_text, reply_type=reply_type)


async def _dispatch_service_v2_feishu_command(*, text: str, receive_id: str) -> bool:
    """Dispatch one exact managed command after all fixed routes miss."""

    dispatcher = _SERVICE_V2_FEISHU_DISPATCHER
    if dispatcher is None:
        return False
    context = _COMMAND_CONTEXT.get() or FeishuCommandContext("", "", "")
    try:
        result = await dispatcher.dispatch(
            command_text=text, event_id=context.event_id,
            sender_id=context.actor_id, chat_id=context.chat_id,
        )
        if result is not None and not isinstance(result, dict):
            raise TypeError("managed Feishu command result must be a dict or None")
    except OrchestrationError as exc:
        logger.warning("managed Feishu command rejected | code=%s", exc.code)
        await _reply_text(
            receive_id,
            "扩展任务未能执行：消息身份不完整或当前入口不可用。",
            reply_type="service_v2_feishu_rejected",
        )
        return True
    except Exception as exc:
        logger.error(
            "managed Feishu command failed | error_type=%s", type(exc).__name__,
        )
        await _reply_text(
            receive_id,
            "扩展任务暂时无法执行，请稍后重试。",
            reply_type="service_v2_feishu_failed",
        )
        return True
    if result is None:
        return False
    reply, reply_type = _automation_result_reply(
        task_name="扩展任务",
        result={"status": result.get("status")},
    )
    await _reply_text(receive_id, reply, reply_type=reply_type)
    return True


async def _run_deferred_tool(
    *,
    tool_name: str,
    params: dict[str, Any],
    receive_id: str = "",
    receive_id_type: str = "open_id",
):
    from feishu.bot import get_agent_core

    if tool_name in FIRST_PARTY_FEISHU_ROUTE_KEYS:
        if receive_id:
            await _reply_text(
                receive_id,
                "自动化菜单入口缺少可信项目路由，任务未提交。",
                receive_id_type=receive_id_type,
                reply_type="automation_menu_route_required",
            )
        return

    agent = get_agent_core()
    if not agent:
        if receive_id:
            await _reply_text(receive_id, "Agent 尚未初始化，请稍后再试", receive_id_type=receive_id_type)
        return

    if receive_id and await _reply_if_tool_running(
        agent,
        receive_id,
        tool_name,
        receive_id_type=receive_id_type,
    ):
        return

    if receive_id:
        await _reply_text(
            receive_id,
            f"已开始执行：{TOOL_DISPLAY_NAMES.get(tool_name, tool_name)}。完成后我会反馈结果。",
            receive_id_type=receive_id_type,
            reply_type=f"tool_start:{tool_name}",
        )

    try:
        result = await _execute_tool_with_stale_auth_retry(agent, tool_name, params)
    except Exception as e:
        logger.error(
            "飞书异步工具执行异常: tool=%s error=%s",
            tool_name,
            str(e)[:200],
        )
        if receive_id:
            await _reply_text(
                receive_id,
                format_tool_reply(tool_name, {"success": False, "error": str(e)}),
                receive_id_type=receive_id_type,
            )
        return

    if not result.get("success", False):
        logger.error(
            "飞书异步工具执行失败: tool=%s error=%s",
            tool_name,
            str(result.get("error") or result)[:200],
        )
    if receive_id:
        if receive_id_type == "chat_id" and await _handle_auth_result(
            receive_id,
            result=result,
            resume_tool=tool_name,
            resume_params=params,
        ):
            return
        await _reply_tool_result(receive_id, tool_name, result, receive_id_type=receive_id_type)


def _menu_action_from_key(event_key: str) -> dict[str, Any] | None:
    key = str(event_key or "").strip()
    if not key:
        return None
    if key.startswith("automation:"):
        route_key = key.removeprefix("automation:").strip()
        if not _FEISHU_ROUTE_KEY_RE.fullmatch(route_key):
            return None
        return {
            "tool_name": "automation_project",
            "params": {},
            "mode": "automation_project",
            "automation_route_key": route_key,
            "dynamic_inputs": {},
        }
    aliased_text = MENU_KEY_ALIASES.get(key.lower(), key)
    request = direct_tool_request_from_text(aliased_text)
    if not request:
        return None
    return {
        "tool_name": request["tool_name"],
        "params": request.get("params") or {},
        "mode": request.get("mode") or "reply",
        "automation_route_key": request.get("automation_route_key") or "",
        "dynamic_inputs": request.get("dynamic_inputs") or {},
    }


async def _reply_text(
    receive_id: str,
    text: str,
    receive_id_type: str = "chat_id",
    *,
    reply_type: str = "text",
):
    """发送文本回复到飞书。"""
    logger.info(
        "feishu outbound | receive_id=%s | receive_id_type=%s | reply_type=%s | chars=%d",
        receive_id,
        receive_id_type,
        reply_type,
        len(str(text or "")),
    )
    await asyncio.to_thread(_reply_text_sync, receive_id, text, receive_id_type)


def _extract_text(raw_content: Any) -> str:
    if isinstance(raw_content, dict):
        content = raw_content
    else:
        try:
            content = json.loads(str(raw_content or "{}"))
        except json.JSONDecodeError:
            return str(raw_content or "").strip()

    text = str(content.get("text", "") or "").strip()
    if text.startswith("@"):
        text = text.split(" ", 1)[-1] if " " in text else ""
    return text.strip()


def _extract_open_or_user_id(raw_identity: Any) -> str:
    if isinstance(raw_identity, dict):
        return str(raw_identity.get("open_id") or raw_identity.get("user_id") or "").strip()
    return str(getattr(raw_identity, "open_id", "") or getattr(raw_identity, "user_id", "") or "").strip()


def _reply_text_sync(receive_id: str, text: str, receive_id_type: str = "chat_id"):
    """同步发送文本，供主事件循环通过 to_thread 调用。"""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    body = CreateMessageRequestBody.builder() \
        .receive_id(receive_id) \
        .msg_type("text") \
        .content(json.dumps({"text": text}, ensure_ascii=False)) \
        .build()

    req = CreateMessageRequest.builder() \
        .receive_id_type(receive_id_type) \
        .request_body(body) \
        .build()

    resp = client.im.v1.message.create(req)
    if not resp.success():
        logger.error("发送消息失败: code=%s, msg=%s", resp.code, resp.msg)


def _submit_to_agent_loop(coro) -> Future:
    from feishu.bot import get_agent_loop

    agent_loop = get_agent_loop()
    if not agent_loop or not agent_loop.is_running():
        raise RuntimeError("Agent 事件循环未就绪")
    return asyncio.run_coroutine_threadsafe(coro, agent_loop)


def _submit_with_future_callback(coro) -> None:
    future = _submit_to_agent_loop(coro)
    future.add_done_callback(_log_future_exception)


def _schedule_local_task(coro) -> None:
    task = asyncio.create_task(coro)
    task.add_done_callback(_log_task_exception)


def _log_future_exception(future: Future):
    try:
        future.result()
    except Exception as e:
        logger.error("飞书异步任务失败: %s", str(e)[:200])


def _log_task_exception(task: asyncio.Task):
    try:
        task.result()
    except Exception as e:
        logger.error("飞书本地异步任务失败: %s", str(e)[:200])
