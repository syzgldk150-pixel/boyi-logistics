"""飞书消息处理：解析文本 / 菜单事件 -> 直达工具或交给 Agent。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from concurrent.futures import Future
from typing import Any

import httpx

from agent.direct_tool_router import (
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
from agent.pending_actions import clear_pending, get_pending, set_pending
from agent.tms_runtime.account_contracts import PRICE_SESSION_PROFILE
from feishu.notify import remember_chat_id
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
R7_DEPARTURE_PENDING_TTL = 600
R7_DEPARTURE_TASK_ID = "r7_departure_checkin"
R7_DEPARTURE_DEFAULT_PLATE = "湘AK6980"
SELF_PICKUP_PROBLEM_ACCOUNT_ID = "ronghui_self_pickup_problem"
SPLIT_SELECTION_TTL = 600
SPLIT_TOOL_NAME = "split_pending_problem_upload"
FEISHU_SAFE_TEXT_BYTES = 3500
ACCOUNT_AUTH_SESSION_PREFIX = "account:"
ADMIN_REQUEST_TIMEOUT = 90.0
MENU_KEY_ALIASES = {
    "scan": "扫描",
    "sync_scan": "扫描",
    "scan_sync": "扫描",
    "sync_scan_codes": "扫描",
    "get_and_scan": "扫描",
}
RUNNING_CANCEL_PENDING_TTL = 300
TOOL_DISPLAY_NAMES = {
    "sync_scan_codes": "扫描任务",
    "sync_arrival_stats": "统计到货数据任务",
    "sync_arrive_list": "到货清单任务",
    "sync_daily_send_orders": "当日寄件数据任务",
    "sync_yunda_dispatch_forecast": "韵达派件预测任务",
    "sync_yunda_send_waybills": "韵达寄件运单任务",
    "self_pickup_problem_upload": "自提到货问题件任务",
    "split_pending_problem_upload": "分批差错及问题件任务",
    "r7_arrival_checkin": "R7 到达打卡任务",
    "r7_departure_checkin": "R7 发车打卡任务",
}
TOOL_CANCEL_COMMANDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sync_scan_codes", re.compile(r"^\s*取消\s*(?:扫描|扫描数据|扫描任务|sync_scan_codes)\s*$", re.IGNORECASE)),
    ("sync_arrival_stats", re.compile(r"^\s*取消\s*(?:统计|到货统计|统计到货数据|sync_arrival_stats)\s*$", re.IGNORECASE)),
    ("sync_arrive_list", re.compile(r"^\s*取消\s*(?:arrive[-_\s]*list|到货清单|预到达清单|sync_arrive_list)\s*$", re.IGNORECASE)),
    ("sync_daily_send_orders", re.compile(r"^\s*取消\s*(?:当日寄件数据|寄件数据|融辉寄件数据|sync_daily_send_orders)\s*$", re.IGNORECASE)),
    ("sync_yunda_dispatch_forecast", re.compile(r"^\s*取消\s*(?:韵达派件预测|派件预测|sync_yunda_dispatch_forecast)\s*$", re.IGNORECASE)),
    ("sync_yunda_send_waybills", re.compile(r"^\s*取消\s*(?:韵达寄件运单|韵达寄件|sync_yunda_send_waybills)\s*$", re.IGNORECASE)),
    ("self_pickup_problem_upload", re.compile(r"^\s*取消\s*(?:自提到货问题件|自提问题件|self_pickup_problem_upload)\s*$", re.IGNORECASE)),
    ("split_pending_problem_upload", re.compile(r"^\s*取消\s*(?:分批|split_pending_problem_upload)\s*$", re.IGNORECASE)),
    ("r7_arrival_checkin", re.compile(r"^\s*取消\s*(?:R7\s*)?到达\s*打卡\s*$", re.IGNORECASE)),
    ("r7_departure_checkin", re.compile(r"^\s*取消\s*(?:R7\s*)?(?:发车|发车打卡)\s*$", re.IGNORECASE)),
)


def _split_text_chunks(lines: list[str], *, max_bytes: int = FEISHU_SAFE_TEXT_BYTES) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for raw_line in lines:
        line = str(raw_line)
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > max_bytes:
            raise ValueError("单行分批消息超过飞书安全长度")
        separator_bytes = 1 if current else 0
        if current and current_bytes + separator_bytes + line_bytes > max_bytes:
            chunks.append("\n".join(current))
            current = []
            current_bytes = 0
            separator_bytes = 0
        current.append(line)
        current_bytes += separator_bytes + line_bytes
    if current:
        chunks.append("\n".join(current))
    return chunks


def _parse_split_selection(text: str, candidate_count: int) -> list[int]:
    normalized = str(text or "").strip()
    if candidate_count <= 0:
        raise ValueError("当前没有可选择的运单")
    if normalized == "全部":
        return list(range(1, candidate_count + 1))
    for separator in ("，", "、"):
        normalized = normalized.replace(separator, ",")
    if not normalized or normalized.startswith(",") or normalized.endswith(",") or ",," in normalized:
        raise ValueError("请输入数字、逗号分隔数字、区间或“全部”")
    selected: list[int] = []
    seen: set[int] = set()
    for raw_token in normalized.split(","):
        token = raw_token.strip()
        if re.fullmatch(r"\d+", token):
            values = [int(token)]
        else:
            match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
            if not match:
                raise ValueError(f"非法序号：{token or '空'}")
            start = int(match.group(1))
            end = int(match.group(2))
            if start > end:
                raise ValueError(f"区间起始序号不能大于结束序号：{token}")
            values = list(range(start, end + 1))
        for value in values:
            if value < 1 or value > candidate_count:
                raise ValueError(f"序号越界：{value}（可选 1-{candidate_count}）")
            if value in seen:
                raise ValueError(f"序号重复或区间重叠：{value}")
            seen.add(value)
            selected.append(value)
    if not selected:
        raise ValueError("至少选择一个序号")
    return selected


def _split_candidate_lines(candidates: list[dict[str, Any]], hidden_completed: int) -> list[str]:
    lines = [
        f"待执行分批运单 {len(candidates)} 单（已隐藏完整成功 {hidden_completed} 单）：",
    ]
    for index, item in enumerate(candidates, start=1):
        lines.append(
            f"{index}. {item.get('bill_code')} [{item.get('status') or '未执行'}] "
            f"{item.get('problem_type')}，已到{item.get('arrived_quantity')}/"
            f"应到{item.get('expected_quantity')}件"
        )
    lines.extend(
        [
            "",
            "回复“确认”直接执行全部；如需部分上传，请输入序号：2 / 1,3,5 / 2-4。",
            "回复“取消”放弃；部分选择后需再次回复“确认”执行；10 分钟内有效。",
        ]
    )
    return lines


def _split_selected_lines(selected: list[dict[str, Any]]) -> list[str]:
    lines = [f"已选择 {len(selected)} 单："]
    for item in selected:
        lines.append(
            f"- {item.get('bill_code')} [{item.get('status') or '未执行'}] {item.get('problem_type')}"
        )
    lines.extend(["", "回复“确认”正式执行，回复“取消”放弃。10 分钟内有效。"])
    return lines


def _split_execution_params(pending: dict[str, Any], selected_bill_codes: list[str]) -> dict[str, Any]:
    return {
        "dry_run": False,
        "account_id": pending.get("account_id") or "ronghui_default",
        "selected_bill_codes": selected_bill_codes,
        "preview_fingerprint": str(pending.get("preview_fingerprint") or ""),
    }


async def _execute_split_formal(
    agent: Any,
    chat_id: str,
    pending: dict[str, Any],
    selected_bill_codes: list[str],
    *,
    running_message: str,
) -> None:
    if _running_tool_info(agent, SPLIT_TOOL_NAME).get("running"):
        await _reply_text(chat_id, running_message, reply_type="split_confirmation_running")
        return
    clear_pending(chat_id)
    await _reply_text(chat_id, "程序正在执行：分批差错及问题件")
    await _execute_and_reply(
        agent,
        chat_id,
        SPLIT_TOOL_NAME,
        _split_execution_params(pending, selected_bill_codes),
    )


async def _reply_split_lines(chat_id: str, lines: list[str], *, reply_type: str) -> None:
    for chunk in _split_text_chunks(lines):
        await _reply_text(chat_id, chunk, reply_type=reply_type)


def _normalize_plate_numbers(value: Any, *, default: str = R7_DEPARTURE_DEFAULT_PLATE) -> list[str]:
    if value in (None, ""):
        raw_items: list[Any] = [default]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        text = str(value)
        for sep in ("，", "、", ";", "；", "\n", "\r", "\t"):
            text = text.replace(sep, ",")
        raw_items = text.split(",")

    plates: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        plate = str(item or "").strip()
        if not plate or plate in seen:
            continue
        seen.add(plate)
        plates.append(plate)
    return plates or [default]


def _r7_departure_saved_params(agent: Any) -> dict[str, Any]:
    try:
        rows = agent.memory.list_scheduled_tasks()
    except Exception as exc:
        logger.warning("读取 R7 发车打卡后台配置失败: %s", redact_text(exc)[:200])
        return {}

    fallback: dict[str, Any] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        params = row.get("tool_params")
        if not isinstance(params, dict):
            params = {}
        if str(row.get("id") or "").strip() == R7_DEPARTURE_TASK_ID:
            return dict(params)
        if str(row.get("tool_name") or "").strip() == R7_DEPARTURE_TASK_ID and fallback is None:
            fallback = dict(params)
    return fallback or {}


def _format_r7_departure_plate_choice(plate_numbers: list[str], params: dict[str, Any]) -> str:
    class_name = str(params.get("class_name") or "邵阳操作场-长沙").strip()
    fixed_time = str(params.get("departure_time_fixed") or "21:30:00").strip()
    lines = [
        "请选择本次 R7 发车打卡车牌，回复完整车牌号，不要只回复序号：",
        f"班次：{class_name}",
        f"计划发车：今天 {fixed_time}",
    ]
    for index, plate in enumerate(plate_numbers, start=1):
        lines.append(f"{index}. {plate}")
    lines.append('如需取消，回复"取消"。')
    return "\n".join(lines)


def _resolve_r7_departure_plate_choice(text: str, plate_numbers: list[str]) -> str | None:
    normalized = str(text or "").strip()
    if not normalized:
        return None
    compact = "".join(normalized.split()).upper()
    for plate in plate_numbers:
        if "".join(str(plate).split()).upper() == compact:
            return plate
    return None


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
    result = await agent.execute_tool(tool_name, params)
    if not _auth_state(result):
        return result
    auth_session = _auth_session_for_result(tool_name, params, result)
    if not await _auth_session_currently_authenticated(auth_session):
        return result
    logger.warning(
        "Tool reported auth failure but forced admin status is authenticated; retrying once. tool=%s session=%s",
        tool_name,
        auth_session,
    )
    return await agent.execute_tool(tool_name, params)


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
    normalized = str(text or "").strip()
    if not normalized:
        return None
    for tool_name, pattern in TOOL_CANCEL_COMMANDS:
        if pattern.match(normalized):
            return tool_name
    return None


def _running_tool_info(agent: Any, tool_name: str) -> dict[str, Any]:
    try:
        if hasattr(agent, "running_tool_info"):
            info = agent.running_tool_info(tool_name)
            if isinstance(info, dict):
                return info
        executor = getattr(agent, "executor", None)
        if executor is not None and hasattr(executor, "running_tool_info"):
            info = executor.running_tool_info(tool_name)
            if isinstance(info, dict):
                return info
        if hasattr(agent, "is_tool_running") and agent.is_tool_running(tool_name):
            return {"running": True, "started_at": "", "cancel_requested": False}
        if executor is not None and hasattr(executor, "is_tool_running") and executor.is_tool_running(tool_name):
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
    started_at = str(info.get("started_at") or "")
    if receive_id_type == "chat_id":
        set_pending(
            receive_id,
            {
                "type": "cancel_running_tool",
                "tool_name": tool_name,
                "description": label,
                "started_at": started_at,
            },
            ttl_sec=RUNNING_CANCEL_PENDING_TTL,
        )
        suffix = '请先等待完成，或回复"取消"取消当前任务。'
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
            if agent is None:
                from feishu.bot import get_agent_core

                agent = get_agent_core()
            if not agent:
                await _reply_text(chat_id, "登录成功，但 Agent 尚未初始化，请重新发送任务。", reply_type="login_success")
                return
            await _reply_text(chat_id, "登录成功，继续执行原任务...", reply_type="login_success")
            await _execute_and_reply(agent, chat_id, resume_tool, resume_params or {})
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


async def _handle_r7_departure_direct(agent: Any, chat_id: str, tool_name: str) -> None:
    params = _r7_departure_saved_params(agent)
    plate_numbers = _normalize_plate_numbers(
        params.get("plate_numbers") if params.get("plate_numbers") not in (None, "") else params.get("plate_number")
    )
    params["_feishu"] = True

    if len(plate_numbers) > 1:
        clear_pending(chat_id)
        set_pending(
            chat_id,
            {
                "type": "r7_departure_plate_choice",
                "tool_name": tool_name,
                "params": params,
                "plate_numbers": plate_numbers,
            },
            ttl_sec=R7_DEPARTURE_PENDING_TTL,
        )
        await _reply_text(chat_id, _format_r7_departure_plate_choice(plate_numbers, params))
        return

    params["plate_numbers"] = plate_numbers
    if await _reply_if_tool_running(agent, chat_id, tool_name):
        return
    await _reply_text(chat_id, "程序正在执行")
    await _execute_and_reply(agent, chat_id, tool_name, params)


def handle_im_message(event):
    """飞书文本/图片消息回调。"""
    try:
        msg = event.event.message
        msg_type = msg.message_type
        chat_id = msg.chat_id
        sender_id = event.event.sender.sender_id.open_id
        remember_chat_id(chat_id)

        logger.info("收到消息: type=%s, chat=%s, sender=%s", msg_type, chat_id, sender_id)
        _submit_with_future_callback(_handle_im_message_data(
            msg_type=msg_type,
            chat_id=chat_id,
            sender_id=sender_id,
            raw_content=msg.content,
        ))

    except Exception as e:
        logger.error("消息处理异常: %s", str(e)[:200])


def queue_im_message_payload(event_payload: dict[str, Any]) -> bool:
    """将 Webhook 文本/图片消息投递到当前事件循环。"""
    message = event_payload.get("message") or {}
    sender = event_payload.get("sender") or {}

    msg_type = str(message.get("message_type", "") or "").strip()
    chat_id = str(message.get("chat_id", "") or "").strip()
    sender_id = _extract_open_or_user_id(sender.get("sender_id"))

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
        _submit_with_future_callback(_handle_menu_action(
            event_key=event_key,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        ))
    except Exception as e:
        logger.error("菜单事件处理异常: %s", str(e)[:200])


def queue_bot_menu_payload(event_payload: dict[str, Any]) -> bool:
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
    ))
    return True


async def _handle_im_message_data(
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
):
    menu_action = _menu_action_from_key(event_key)
    if not menu_action:
        logger.warning("未识别的机器人菜单 event_key: %s", event_key)
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

    agent = get_agent_core()
    if not agent:
        await _reply_text(chat_id, "Agent 尚未初始化，请稍后再试")
        return

    pending = get_pending(chat_id)
    logger.info(
        "feishu inbound | chat=%s | sender=%s | message_kind=%s | pending=%s",
        chat_id,
        sender_id,
        _message_kind(text),
        _pending_type(pending),
    )
    login_session = parse_login_send_code_session(text)
    if login_session:
        logger.info("feishu route | chat=%s | route=login_command | session=%s", chat_id, login_session)
        clear_pending(chat_id)
        if login_session == "choice":
            await _begin_login_account_choice(chat_id)
            return
        resolved_session = await _resolve_login_command_session(login_session)
        await _send_code_and_wait(chat_id, auth_session=resolved_session)
        return

    cancel_tool_name = _cancel_tool_name_from_text(text)
    if cancel_tool_name:
        logger.info("feishu route | chat=%s | route=cancel_running_tool | tool=%s", chat_id, cancel_tool_name)
        await _cancel_running_tool_and_reply(agent, chat_id, cancel_tool_name)
        return

    if str(text or "").strip() == "分批" and pending is not None:
        if str(pending.get("type") or "") in {"split_pending_selection", "split_pending_confirmation"}:
            clear_pending(chat_id)
            pending = None
            logger.info("feishu route | chat=%s | route=split_restart_preview", chat_id)

    if pending is not None:
        ptype = str(pending.get("type") or "confirm_action")
        logger.info("feishu route | chat=%s | route=pending | pending=%s", chat_id, ptype)

        if ptype == "split_pending_selection":
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消：分批差错及问题件")
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
                        "分批差错及问题件任务正在执行中；当前列表仍保留，请稍后再次回复“确认”。"
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
            set_pending(
                chat_id,
                {
                    "type": "split_pending_confirmation",
                    "tool_name": SPLIT_TOOL_NAME,
                    "selected_bill_codes": selected_codes,
                    "preview_fingerprint": pending.get("preview_fingerprint"),
                    "account_id": pending.get("account_id") or "ronghui_default",
                    "selected": selected,
                },
                ttl_sec=SPLIT_SELECTION_TTL,
            )
            await _reply_split_lines(
                chat_id,
                _split_selected_lines(selected),
                reply_type="split_selection_echo",
            )
            return

        if ptype == "split_pending_confirmation":
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消：分批差错及问题件")
                return
            if is_confirm_text(text):
                await _execute_split_formal(
                    agent,
                    chat_id,
                    pending,
                    list(pending.get("selected_bill_codes") or []),
                    running_message=(
                        "分批差错及问题件任务正在执行中；当前选择仍保留，请稍后再次回复“确认”。"
                    ),
                )
                return
            await _reply_text(chat_id, "当前选择已保留，请回复“确认”执行或回复“取消”放弃。")
            return

        if ptype == "confirm_action":
            if is_confirm_text(text):
                clear_pending(chat_id)
                if await _reply_if_tool_running(agent, chat_id, pending["tool_name"]):
                    return
                await _reply_text(chat_id, "程序正在执行")
                await _execute_and_reply(
                    agent,
                    chat_id,
                    pending["tool_name"],
                    pending.get("params") or {},
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
                await _cancel_running_tool_and_reply(
                    agent,
                    chat_id,
                    str(pending.get("tool_name") or ""),
                    started_at=str(pending.get("started_at") or ""),
                )
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
            if is_cancel_text(text):
                clear_pending(chat_id)
                await _reply_text(chat_id, "已取消：R7 发车打卡")
                return
            plate_numbers = _normalize_plate_numbers(pending.get("plate_numbers"))
            selected_plate = _resolve_r7_departure_plate_choice(text, plate_numbers)
            if selected_plate:
                clear_pending(chat_id)
                params = dict(pending.get("params") or {})
                params["_feishu"] = True
                params["plate_numbers"] = [selected_plate]
                tool_name = pending.get("tool_name") or R7_DEPARTURE_TASK_ID
                if await _reply_if_tool_running(agent, chat_id, tool_name):
                    return
                await _reply_text(chat_id, f"程序正在执行：{selected_plate}")
                await _execute_and_reply(
                    agent,
                    chat_id,
                    tool_name,
                    params,
                )
                return
            await _reply_text(chat_id, _format_r7_departure_plate_choice(plate_numbers, pending.get("params") or {}))
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
                resume_params = pending.get("resume_params") or {}
                clear_pending(chat_id)
                if resume_tool:
                    await _reply_text(chat_id, "登录成功，继续执行原任务...")
                    await _execute_and_reply(agent, chat_id, resume_tool, resume_params)
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
        confirm_intent = direct_request.get("confirm_intent")
        selection_intent = direct_request.get("selection_intent")
        local_result = direct_request.get("local_result")
        logger.info("feishu route | chat=%s | route=direct_tool | tool=%s | mode=%s", chat_id, tool_name, mode)

        if isinstance(local_result, dict):
            await _reply_tool_result(chat_id, tool_name, local_result)
            return

        if mode == "r7_departure_choice":
            await _handle_r7_departure_direct(agent, chat_id, tool_name)
            return

        if mode == "deferred":
            if await _reply_if_tool_running(agent, chat_id, tool_name):
                return
            await _reply_text(chat_id, "程序正在执行")
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
            await _reply_text(chat_id, "程序正在执行", reply_type="tool_start:get_price")

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

        if confirm_intent and result.get("success"):
            set_pending(
                chat_id,
                {
                    "type": "confirm_action",
                    "tool_name": tool_name,
                    "params": confirm_intent.get("execute_params") or {},
                    "description": confirm_intent.get("description") or tool_name,
                },
            )
        if selection_intent and tool_name == SPLIT_TOOL_NAME and result.get("success"):
            payload = result.get("data") if isinstance(result.get("data"), dict) else result
            candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
            fingerprint = str(payload.get("preview_fingerprint") or "")
            if candidates and len(fingerprint) == 64:
                set_pending(
                    chat_id,
                    {
                        "type": "split_pending_selection",
                        "tool_name": tool_name,
                        "candidates": candidates,
                        "preview_fingerprint": fingerprint,
                        "account_id": params.get("account_id") or "ronghui_default",
                    },
                    ttl_sec=SPLIT_SELECTION_TTL,
                )
                await _reply_split_lines(
                    chat_id,
                    _split_candidate_lines(
                        candidates,
                        int(payload.get("hidden_completed_count") or 0),
                    ),
                    reply_type="split_candidate_list",
                )
                return
        await _reply_tool_result(chat_id, tool_name, result)
        return

    logger.info("feishu route | chat=%s | route=agent_llm", chat_id)
    notice_task = asyncio.create_task(_send_after_delay(chat_id, "正在处理...", 1.5))
    try:
        result = await agent.handle_message(
            message=text,
            user_id=sender_id,
            conversation_id=f"feishu_{chat_id}",
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


async def _run_deferred_tool(
    *,
    tool_name: str,
    params: dict[str, Any],
    receive_id: str = "",
    receive_id_type: str = "open_id",
):
    from feishu.bot import get_agent_core

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
        await _reply_text(receive_id, "程序正在执行", receive_id_type=receive_id_type)

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
    aliased_text = MENU_KEY_ALIASES.get(key.lower(), key)
    request = direct_tool_request_from_text(aliased_text)
    if not request:
        return None
    return {
        "tool_name": request["tool_name"],
        "params": request.get("params") or {},
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
