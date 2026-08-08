"""Lightweight monitoring adapters for supplier message dashboards."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from urllib.parse import quote

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker


POLL_INTERVAL_SEC = 60
CACHE_TTL_SEC = 55

YUNDA_CLIENT_ORIGIN = "https://ky-client.yunda56.com"
YUNDA_CLIENT_HOME_URL = f"{YUNDA_CLIENT_ORIGIN}/#/"
YUNDA_USER_INFO_URL = f"{YUNDA_CLIENT_ORIGIN}/client/user/info"
YUNDA_MESSAGE_ORIGIN = "https://ky-message.yunda56.com"
YUNDA_MESSAGE_TYPES_URL = f"{YUNDA_MESSAGE_ORIGIN}/message/api/getTypes"
YUNDA_HEAD_MESSAGE_REFERER = f"{YUNDA_MESSAGE_ORIGIN}/message/view/head_message"
RONGHUI_HOME_URL = "https://tms.ronghuiwl.com/module/index?mv=index"
DAILY_SIGN_SHEET_RESOURCE_KEY = "phase7.daily_sign_sheet"

_SNAPSHOT_CACHE: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
_DAILY_SIGN_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.RLock()
_SNAPSHOT_LOCKS: dict[tuple[str, ...], threading.Lock] = {}
_DAILY_SIGN_LOCKS: dict[str, threading.Lock] = {}
_SNAPSHOT_REFRESHING: set[tuple[str, ...]] = set()
_DAILY_SIGN_REFRESHING: set[str] = set()
_CACHE_META_KEYS = {"cached", "stale", "refreshing", "cache_age_sec"}
_DAILY_CRON_RE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$")


def _now_label() -> str:
    return datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S")


def _strip_cache_meta(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _CACHE_META_KEYS}


def _with_cache_meta(
    payload: dict[str, Any],
    *,
    cached: bool,
    stale: bool,
    refreshing: bool,
    cache_age_sec: float | int = 0,
) -> dict[str, Any]:
    clean = _strip_cache_meta(payload)
    return {
        **clean,
        "cached": cached,
        "stale": stale,
        "refreshing": refreshing,
        "cache_age_sec": max(int(cache_age_sec), 0),
    }


def _snapshot_lock(cache_key: tuple[str, ...]) -> threading.Lock:
    with _CACHE_LOCK:
        lock = _SNAPSHOT_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _SNAPSHOT_LOCKS[cache_key] = lock
        return lock


def _daily_sign_lock(target_date: str) -> threading.Lock:
    with _CACHE_LOCK:
        lock = _DAILY_SIGN_LOCKS.get(target_date)
        if lock is None:
            lock = threading.Lock()
            _DAILY_SIGN_LOCKS[target_date] = lock
        return lock


def get_workflow_resource(resource_key: str) -> dict | None:
    """Lazily read runtime resource config so tests can patch the boundary."""
    from agent.workflow_resource_store import get_workflow_resource as _get_workflow_resource

    return _get_workflow_resource(resource_key)


def feishu_operation(action: str, params: dict) -> dict:
    """Lazily call Feishu CLI helpers so monitoring does not expose tokens."""
    from tools.feishu_cli_tool import feishu_operation as _feishu_operation

    return _feishu_operation(action, params)


def list_scheduled_tasks() -> list[dict[str, Any]]:
    """Read the same scheduled_tasks table used by the Agent scheduler."""
    from agent.memory import Memory

    memory = Memory()
    memory.init()
    return memory.list_scheduled_tasks()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    text = re.sub(r"[^\d-]", "", str(value))
    if not text:
        return default
    try:
        return max(int(text), 0)
    except ValueError:
        return default


def _safe_slug(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return "unknown"
    keep = []
    for char in text.lower():
        keep.append(char if char.isalnum() else "_")
    return re.sub(r"_+", "_", "".join(keep)).strip("_") or "unknown"


def _parse_target_date(value: str | None) -> str:
    text = _clean_text(value)
    if not text:
        return date.today().isoformat()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return date.today().isoformat()


def _daily_sign_error_payload(
    *,
    target_date: str,
    updated_at: str,
    status: str,
    message: str,
    poll_interval_sec: int = POLL_INTERVAL_SEC,
    refresh_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "target_date": target_date,
        "updated_at": updated_at,
        "poll_interval_sec": poll_interval_sec,
        "counts": {"unsigned_today": 0},
        "message": message,
        "refresh_schedule": refresh_schedule or {
            "time_values": [],
            "last_refresh_at": "",
            "next_refresh_at": "",
            "source": "scheduled_tasks",
        },
    }


def _redact_known_values(message: Any, values: list[Any]) -> str:
    text = _clean_text(message)
    for value in values:
        secret = _clean_text(value)
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _extract_sheet_values(payload: Any) -> list[list[Any]] | None:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend(
            [
                payload.get("data", {}).get("valueRange", {}).get("values")
                if isinstance(payload.get("data"), dict)
                else None,
                payload.get("valueRange", {}).get("values")
                if isinstance(payload.get("valueRange"), dict)
                else None,
                payload.get("data", {}).get("values") if isinstance(payload.get("data"), dict) else None,
                payload.get("values"),
            ]
        )
    for candidate in candidates:
        if isinstance(candidate, list):
            values: list[list[Any]] = []
            for row in candidate:
                if isinstance(row, list):
                    values.append(row)
                elif row not in (None, ""):
                    values.append([row])
            return values
    return None


def _daily_sign_sheet_read_params() -> tuple[dict[str, Any], list[str]]:
    resource = get_workflow_resource(DAILY_SIGN_SHEET_RESOURCE_KEY) or {}
    spreadsheet_token = _clean_text(resource.get("spreadsheet_token"))
    value_range = _clean_text(resource.get("read_range") or resource.get("clear_range") or resource.get("range"))
    sensitive_values = [spreadsheet_token]
    if not spreadsheet_token or not value_range:
        raise ValueError(f"未找到 {DAILY_SIGN_SHEET_RESOURCE_KEY} 的 spreadsheet_token 或 range 配置")
    return {
        "spreadsheet_token": spreadsheet_token,
        "range": value_range,
        "as": "bot",
    }, sensitive_values


def _read_daily_sign_sheet_values() -> list[list[Any]]:
    params, sensitive_values = _daily_sign_sheet_read_params()
    result = feishu_operation("read_sheet", params)
    if not isinstance(result, dict):
        raise RuntimeError("飞书应签明细读取返回格式异常")
    if result.get("error"):
        raise RuntimeError(_redact_known_values(result.get("error") or "飞书应签明细读取失败", sensitive_values))
    values = _extract_sheet_values(result)
    if values is None:
        raise RuntimeError("飞书应签明细读取返回格式异常")
    return values


def _sheet_plan_sign_date(row: list[Any]) -> str:
    if len(row) < 2:
        return ""
    raw = _clean_text(row[1]).replace("/", "-")
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if not match:
        return ""
    year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _count_unsigned_daily_sign_sheet_rows(values: list[list[Any]], target_date: str) -> int:
    total = 0
    for row in values:
        if not row:
            continue
        bill_number = _clean_text(row[0])
        if not bill_number or bill_number in {"运单编号", "waybill_no", "billNumberMain"}:
            continue
        if _sheet_plan_sign_date(row) == target_date:
            total += 1
    return total


def _daily_cron_time_value(cron_expression: Any) -> str:
    match = _DAILY_CRON_RE.fullmatch(_clean_text(cron_expression))
    if not match:
        return ""
    minute = int(match.group(1))
    hour = int(match.group(2))
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _task_enabled(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _clean_text(value).lower() not in {"0", "false", "no", "off", "disabled"}


def _is_daily_sign_task(row: dict[str, Any]) -> bool:
    task_id = _clean_text(row.get("id"))
    tool_name = _clean_text(row.get("tool_name"))
    return tool_name == "sync_daily_should_sign" or task_id.startswith("daily_sign")


def _daily_sign_schedule_time_values() -> tuple[list[str], str]:
    try:
        rows = list_scheduled_tasks()
    except Exception:
        return [], "unavailable"
    values = sorted(
        {
            value
            for row in rows
            if isinstance(row, dict) and _is_daily_sign_task(row) and _task_enabled(row.get("enabled"))
            for value in [_daily_cron_time_value(row.get("cron_expression"))]
            if value
        }
    )
    return values, "scheduled_tasks"


def _combine_time(day: date, time_value: str) -> datetime:
    hour_text, minute_text = time_value.split(":", 1)
    return datetime.combine(day, dt_time(hour=int(hour_text), minute=int(minute_text)))


def _daily_sign_refresh_schedule(target_date: str, now_ts: float | None = None) -> tuple[int, str, dict[str, Any]]:
    now_dt = datetime.fromtimestamp(time.time() if now_ts is None else now_ts)
    target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
    time_values, source = _daily_sign_schedule_time_values()
    if not time_values:
        updated_at = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        return POLL_INTERVAL_SEC, updated_at, {
            "time_values": [],
            "last_refresh_at": "",
            "next_refresh_at": "",
            "source": source,
        }

    today = now_dt.date()
    today_slots = [_combine_time(today, item) for item in time_values]
    future_slots = [item for item in today_slots if item > now_dt]
    if future_slots:
        next_refresh = future_slots[0]
    else:
        next_refresh = _combine_time(today + timedelta(days=1), time_values[0])

    if target_day == today:
        past_slots = [item for item in today_slots if item <= now_dt]
        if past_slots:
            last_refresh = past_slots[-1]
        else:
            last_refresh = _combine_time(today - timedelta(days=1), time_values[-1])
    elif target_day < today:
        last_refresh = _combine_time(target_day, time_values[-1])
    else:
        last_refresh = _combine_time(target_day, time_values[0])

    poll_interval = max(int((next_refresh - now_dt).total_seconds()), 1)
    return poll_interval, last_refresh.strftime("%Y-%m-%d %H:%M:%S"), {
        "time_values": time_values,
        "last_refresh_at": last_refresh.strftime("%Y-%m-%d %H:%M:%S"),
        "next_refresh_at": next_refresh.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
    }


def _is_exception_category(title: str) -> bool:
    return any(marker in title for marker in ("问题", "差错", "投诉", "仲裁", "异常", "申诉"))


def _category_tone(title: str, count: int) -> str:
    if count <= 0:
        return "muted"
    if any(marker in title for marker in ("投诉", "仲裁", "异常", "差错")):
        return "danger"
    if "问题" in title or "回复" in title:
        return "warning"
    return "info"


def _system_payload(
    *,
    system: str,
    label: str,
    status: str,
    status_label: str,
    updated_at: str,
    categories: list[dict[str, Any]] | None = None,
    message: str = "",
) -> dict[str, Any]:
    categories = categories or []
    total_count = sum(_to_int(item.get("count")) for item in categories)
    exception_count = sum(
        _to_int(item.get("count"))
        for item in categories
        if item.get("is_exception") or _is_exception_category(str(item.get("title") or ""))
    )
    return {
        "system": system,
        "label": label,
        "status": status,
        "status_label": status_label,
        "message": message,
        "total_count": total_count,
        "exception_count": exception_count,
        "updated_at": updated_at,
        "categories": categories,
    }


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "rows", "list", "details", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_rows(value)
            if nested:
                return nested
    return []


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return ""


def _yunda_category_from_row(row: dict[str, Any]) -> dict[str, Any]:
    title = _clean_text(
        _first_value(
            row,
            (
                "noticeTypeName",
                "typeName",
                "noticeName",
                "name",
                "title",
                "label",
            ),
        )
    ) or "未命名消息"
    type_code = _clean_text(_first_value(row, ("typeCode", "code", "noticeTypeCode")))
    resource_id = _clean_text(
        _first_value(
            row,
            (
                "newClientResourceId",
                "redirectUrl",
                "resourceId",
                "resource_id",
                "menuId",
            ),
        )
    )
    count = _to_int(_first_value(row, ("sumValue", "count", "num", "total", "value")))
    target_title = _clean_text(_first_value(row, ("noticeSystemName", "targetTitle", "systemName"))) or title
    category_key = type_code or _safe_slug(title)
    return {
        "system": "yunda",
        "category_id": f"yunda:{category_key}",
        "title": title,
        "count": count,
        "status": "pending" if count > 0 else "empty",
        "status_label": "未处理" if count > 0 else "无数据",
        "tone": _category_tone(title, count),
        "is_exception": _is_exception_category(title),
        "type_code": type_code,
        "resource_id": resource_id,
        "target_title": target_title,
        "detail_supported": bool(resource_id),
    }


def parse_yunda_types_payload(payload: dict[str, Any], *, updated_at: str | None = None) -> dict[str, Any]:
    updated_at = updated_at or _now_label()
    rows = _extract_rows(payload)
    categories = [_yunda_category_from_row(row) for row in rows]
    row_total = sum(_to_int(item.get("count")) for item in categories)
    declared_total = _to_int(
        _first_value(
            payload if isinstance(payload, dict) else {},
            ("pendingTotal", "totalPending", "total_count", "total", "num", "sumValue"),
        ),
        default=row_total,
    )
    total_count = max(declared_total, row_total)
    exception_count = sum(_to_int(item.get("count")) for item in categories if item.get("is_exception"))
    return {
        "system": "yunda",
        "label": "韵达",
        "status": "ok",
        "status_label": "已连接",
        "message": "",
        "total_count": total_count,
        "exception_count": exception_count,
        "updated_at": updated_at,
        "categories": categories,
    }


def _extract_yunda_user_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates: list[dict[str, Any]] = []
    for key in ("details", "data", "user", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    candidates.append(payload)
    for item in candidates:
        for key in ("username", "userName", "user_id", "userId", "uid", "id", "account"):
            value = _clean_text(item.get(key))
            if value:
                return value
    return ""


def _response_requires_login(response: Any, body_text: str = "") -> bool:
    location = _clean_text(getattr(response, "headers", {}).get("Location") if getattr(response, "headers", None) else "")
    if "login" in location.lower():
        return True
    lowered = body_text.lower()
    return any(
        marker in lowered
        for marker in (
            "auth_required",
            "session error",
            '"code":1001',
            "system/login",
            "/login",
            "validatecode",
            "<title>登录",
            "用户登录",
        )
    )


def _collect_yunda_snapshot(*, force: bool, updated_at: str) -> dict[str, Any]:
    try:
        session = get_session_broker("yunda").build_requests_session(validate=force)
        user_response = session.get(
            YUNDA_USER_INFO_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": YUNDA_CLIENT_HOME_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False,
            timeout=15,
        )
        user_body = getattr(user_response, "text", "") or ""
        if getattr(user_response, "status_code", 0) in {301, 302, 303, 307, 308} or _response_requires_login(user_response, user_body):
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录。")
        user_payload = user_response.json()
        user_id = _extract_yunda_user_id(user_payload)
        if not user_id:
            raise RuntimeError("韵达用户信息中未找到消息用户标识。")

        message_response = session.post(
            YUNDA_MESSAGE_TYPES_URL,
            data={"userId": user_id, "sourceFlag": "up"},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": YUNDA_MESSAGE_ORIGIN,
                "Referer": YUNDA_HEAD_MESSAGE_REFERER,
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False,
            timeout=15,
        )
        message_body = getattr(message_response, "text", "") or ""
        if getattr(message_response, "status_code", 0) in {301, 302, 303, 307, 308} or _response_requires_login(message_response, message_body):
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达消息接口登录态已失效，请重新登录。")
        if getattr(message_response, "status_code", 0) != 200:
            raise RuntimeError(f"韵达消息接口返回 HTTP {getattr(message_response, 'status_code', '')}")
        payload = message_response.json()
        return parse_yunda_types_payload(payload, updated_at=updated_at)
    except TMSAuthStateError as exc:
        return _system_payload(
            system="yunda",
            label="韵达",
            status="auth_required",
            status_label="登录失效",
            updated_at=updated_at,
            message=str(exc),
        )
    except Exception as exc:
        return _system_payload(
            system="yunda",
            label="韵达",
            status="error",
            status_label="接口异常",
            updated_at=updated_at,
            message=str(exc),
        )


def _collect_ronghui_snapshot(*, force: bool, updated_at: str) -> dict[str, Any]:
    try:
        status = get_session_broker("default").describe_status(validate=True, force=force)
        if not status.get("authenticated"):
            return _system_payload(
                system="ronghui",
                label="融辉 TMS",
                status="auth_required",
                status_label="登录失效",
                updated_at=updated_at,
                message=_clean_text(status.get("last_error_summary")) or "融辉 TMS 当前未登录或登录态已过期。",
            )
        return _system_payload(
            system="ronghui",
            label="融辉 TMS",
            status="no_source",
            status_label="待配置",
            updated_at=updated_at,
            categories=[
                {
                    "system": "ronghui",
                    "category_id": "ronghui:home",
                    "title": "融辉消息中心",
                    "count": 0,
                    "status": "empty",
                    "status_label": "未发现消息源",
                    "tone": "muted",
                    "is_exception": False,
                    "type_code": "",
                    "resource_id": "",
                    "target_title": "融辉 TMS",
                    "detail_supported": True,
                }
            ],
            message="未定位到融辉 TMS 的消息分类接口，先保留原系统入口。",
        )
    except TMSAuthStateError as exc:
        return _system_payload(
            system="ronghui",
            label="融辉 TMS",
            status="auth_required",
            status_label="登录失效",
            updated_at=updated_at,
            message=str(exc),
        )
    except Exception as exc:
        return _system_payload(
            system="ronghui",
            label="融辉 TMS",
            status="error",
            status_label="接口异常",
            updated_at=updated_at,
            message=str(exc),
        )


def _normalize_systems(systems: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if systems is None:
        raw_items = ["yunda", "ronghui"]
    elif isinstance(systems, str):
        raw_items = systems.split(",")
    else:
        raw_items = list(systems)
    normalized: list[str] = []
    for item in raw_items:
        value = _clean_text(item).lower()
        if value in {"yunda", "ronghui"} and value not in normalized:
            normalized.append(value)
    return normalized or ["yunda", "ronghui"]


def _build_totals(systems: list[dict[str, Any]]) -> dict[str, int]:
    by_system = {item.get("system"): item for item in systems}
    yunda_pending = _to_int(by_system.get("yunda", {}).get("total_count"))
    ronghui_pending = _to_int(by_system.get("ronghui", {}).get("total_count"))
    exception_pending = sum(_to_int(item.get("exception_count")) for item in systems)
    return {
        "total_pending": yunda_pending + ronghui_pending,
        "yunda_pending": yunda_pending,
        "ronghui_pending": ronghui_pending,
        "exception_pending": exception_pending,
    }


def _collect_system_snapshot(system: str, *, force: bool, updated_at: str) -> dict[str, Any]:
    if system == "yunda":
        return _collect_yunda_snapshot(force=force, updated_at=updated_at)
    if system == "ronghui":
        return _collect_ronghui_snapshot(force=force, updated_at=updated_at)
    return _system_payload(
        system=system,
        label=system,
        status="error",
        status_label="未支持",
        updated_at=updated_at,
        message="不支持的监控系统。",
    )


def _collect_systems_parallel(normalized: list[str], *, force: bool, updated_at: str) -> list[dict[str, Any]]:
    if len(normalized) <= 1:
        return [_collect_system_snapshot(normalized[0], force=force, updated_at=updated_at)]

    rows_by_system: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(normalized), thread_name_prefix="monitoring") as executor:
        futures = {
            executor.submit(_collect_system_snapshot, system, force=force, updated_at=updated_at): system
            for system in normalized
        }
        for future in as_completed(futures):
            system = futures[future]
            try:
                rows_by_system[system] = future.result()
            except Exception as exc:
                rows_by_system[system] = _system_payload(
                    system=system,
                    label={"yunda": "韵达", "ronghui": "融辉 TMS"}.get(system, system),
                    status="error",
                    status_label="接口异常",
                    updated_at=updated_at,
                    message=str(exc),
                )
    return [rows_by_system[system] for system in normalized]


def _schedule_snapshot_refresh(normalized: list[str], *, force: bool = True) -> None:
    cache_key = tuple(normalized)
    with _CACHE_LOCK:
        if cache_key in _SNAPSHOT_REFRESHING:
            return
        _SNAPSHOT_REFRESHING.add(cache_key)

    def _worker() -> None:
        try:
            build_monitoring_snapshot(systems=normalized, force=force, prefer_cached=False)
        finally:
            with _CACHE_LOCK:
                _SNAPSHOT_REFRESHING.discard(cache_key)

    threading.Thread(target=_worker, name=f"monitoring-refresh-{'-'.join(normalized)}", daemon=True).start()


def _schedule_daily_sign_refresh(target_date: str, *, force: bool = True) -> None:
    with _CACHE_LOCK:
        if target_date in _DAILY_SIGN_REFRESHING:
            return
        _DAILY_SIGN_REFRESHING.add(target_date)

    def _worker() -> None:
        try:
            build_daily_sign_monitoring_snapshot(force=force, target_date=target_date, prefer_cached=False)
        finally:
            with _CACHE_LOCK:
                _DAILY_SIGN_REFRESHING.discard(target_date)

    threading.Thread(target=_worker, name=f"monitoring-daily-sign-{target_date}", daemon=True).start()


def build_monitoring_snapshot(
    *,
    systems: list[str] | tuple[str, ...] | str | None = None,
    force: bool = False,
    prefer_cached: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_systems(systems)
    cache_key = tuple(normalized)
    now = time.time()
    with _CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(cache_key)
        refreshing = cache_key in _SNAPSHOT_REFRESHING
    if cached:
        cache_age = now - cached[0]
        if prefer_cached:
            _schedule_snapshot_refresh(normalized, force=True)
            return _with_cache_meta(cached[1], cached=True, stale=True, refreshing=True, cache_age_sec=cache_age)
        if not force and cache_age <= CACHE_TTL_SEC:
            return _with_cache_meta(cached[1], cached=True, stale=False, refreshing=refreshing, cache_age_sec=cache_age)

    lock = _snapshot_lock(cache_key)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        lock.acquire()
        lock.release()
        with _CACHE_LOCK:
            refreshed = _SNAPSHOT_CACHE.get(cache_key)
        if refreshed:
            age = time.time() - refreshed[0]
            return _with_cache_meta(refreshed[1], cached=True, stale=False, refreshing=False, cache_age_sec=age)
        lock.acquire()
        acquired = True
    try:
        updated_at = _now_label()
        rows = _collect_systems_parallel(normalized, force=force, updated_at=updated_at)

        snapshot = {
            "ok": True,
            "updated_at": updated_at,
            "poll_interval_sec": POLL_INTERVAL_SEC,
            "totals": _build_totals(rows),
            "systems": rows,
        }
        stored_at = time.time()
        with _CACHE_LOCK:
            _SNAPSHOT_CACHE[cache_key] = (stored_at, _strip_cache_meta(snapshot))
        return _with_cache_meta(snapshot, cached=False, stale=False, refreshing=False, cache_age_sec=0)
    finally:
        if acquired:
            lock.release()


def build_daily_sign_monitoring_snapshot(
    *,
    force: bool = False,
    target_date: str | None = None,
    prefer_cached: bool = False,
) -> dict[str, Any]:
    selected_date = _parse_target_date(target_date)
    now = time.time()
    poll_interval_sec, scheduled_updated_at, refresh_schedule = _daily_sign_refresh_schedule(selected_date, now)
    with _CACHE_LOCK:
        cached = _DAILY_SIGN_CACHE.get(selected_date)
        refreshing = selected_date in _DAILY_SIGN_REFRESHING
    if cached:
        cache_age = now - cached[0]
        if prefer_cached:
            _schedule_daily_sign_refresh(selected_date, force=True)
            return _with_cache_meta(cached[1], cached=True, stale=True, refreshing=True, cache_age_sec=cache_age)
        if not force and cache_age <= CACHE_TTL_SEC:
            return _with_cache_meta(cached[1], cached=True, stale=False, refreshing=refreshing, cache_age_sec=cache_age)

    lock = _daily_sign_lock(selected_date)
    acquired = lock.acquire(blocking=False)
    if not acquired:
        lock.acquire()
        lock.release()
        with _CACHE_LOCK:
            refreshed = _DAILY_SIGN_CACHE.get(selected_date)
        if refreshed:
            age = time.time() - refreshed[0]
            return _with_cache_meta(refreshed[1], cached=True, stale=False, refreshing=False, cache_age_sec=age)
        lock.acquire()
        acquired = True

    try:
        updated_at = scheduled_updated_at
        try:
            values = _read_daily_sign_sheet_values()
            unsigned_today = _count_unsigned_daily_sign_sheet_rows(values, selected_date)
            snapshot = {
                "ok": True,
                "status": "ok",
                "target_date": selected_date,
                "updated_at": updated_at,
                "poll_interval_sec": poll_interval_sec,
                "counts": {"unsigned_today": unsigned_today},
                "message": "飞书应签明细",
                "refresh_schedule": refresh_schedule,
            }
        except Exception as exc:
            snapshot = _daily_sign_error_payload(
                target_date=selected_date,
                updated_at=updated_at,
                status="error",
                message=str(exc),
                poll_interval_sec=poll_interval_sec,
                refresh_schedule=refresh_schedule,
            )

        stored_at = time.time()
        with _CACHE_LOCK:
            _DAILY_SIGN_CACHE[selected_date] = (stored_at, _strip_cache_meta(snapshot))
        return _with_cache_meta(snapshot, cached=False, stale=False, refreshing=False, cache_age_sec=0)
    finally:
        if acquired:
            lock.release()


def build_monitoring_detail_link(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    system = _clean_text(payload.get("system")).lower()
    category_id = _clean_text(payload.get("category_id"))
    title = _clean_text(payload.get("title")) or "消息详情"
    if system == "yunda":
        resource_id = _clean_text(payload.get("resource_id"))
        type_code = _clean_text(payload.get("type_code"))
        target_title = _clean_text(payload.get("target_title")) or title
        if resource_id:
            suffix = "?q=1" if type_code in {"001", "222"} or "问题件" in title else ""
            encoded_title = quote(target_title, safe="")
            url = f"{YUNDA_CLIENT_ORIGIN}/#/ifarme/ifarme/{quote(resource_id, safe='')}/{encoded_title}{suffix}"
        else:
            url = f"{YUNDA_CLIENT_ORIGIN}/#/systemlink/systemhome"
        return {
            "ok": True,
            "system": "yunda",
            "category_id": category_id,
            "title": title,
            "mode": "iframe",
            "embed_url": url,
            "open_url": url,
            "frame_allowed": "unknown",
        }
    if system == "ronghui":
        return {
            "ok": True,
            "system": "ronghui",
            "category_id": category_id or "ronghui:home",
            "title": title or "融辉 TMS",
            "mode": "iframe",
            "embed_url": RONGHUI_HOME_URL,
            "open_url": RONGHUI_HOME_URL,
            "frame_allowed": "unknown",
        }
    return {
        "ok": False,
        "error_code": "INVALID_MONITORING_SYSTEM",
        "message": "不支持的监控系统。",
    }
