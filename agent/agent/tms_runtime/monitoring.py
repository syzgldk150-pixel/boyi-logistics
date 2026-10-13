"""Lightweight monitoring adapters for supplier message dashboards."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker


POLL_INTERVAL_SEC = 60
CACHE_TTL_SEC = 55

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
_feishu_operation = None


def configure_feishu_operation(callback) -> None:
    """Inject the Feishu port from the service composition root."""
    global _feishu_operation
    _feishu_operation = callback


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
    """Call the configured Feishu port without importing the tools package."""
    if _feishu_operation is None:
        raise RuntimeError("Feishu monitoring operation is not configured")
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
    range_match = re.fullmatch(
        r"(?P<sheet>[^!]+)![A-Z]+\d+:[A-Z]+(?P<end_row>\d+)",
        value_range,
    )
    if range_match:
        value_range = f"{range_match.group('sheet')}!A1:I{range_match.group('end_row')}"
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


def _sheet_sign_date(value: Any) -> str:
    raw = _clean_text(value).replace("/", "-")
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if not match:
        return ""
    year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _count_unsigned_daily_sign_sheet_rows(values: list[list[Any]], target_date: str) -> int:
    header_index = -1
    headers: dict[str, int] = {}
    required_headers = {"运单编号", "R13应签收时间", "问题件后应签时间"}
    for index, row in enumerate(values):
        row_headers = {_clean_text(value): column for column, value in enumerate(row)}
        if required_headers.issubset(row_headers):
            header_index = index
            headers = row_headers
            break
    if header_index < 0:
        raise RuntimeError("应签明细缺少运单编号、R13应签收时间或问题件后应签时间表头")

    try:
        selected_date = date.fromisoformat(target_date)
    except ValueError:
        return 0
    total = 0
    for row in values[header_index + 1 :]:
        if not row:
            continue
        bill_index = headers["运单编号"]
        if bill_index >= len(row) or not _clean_text(row[bill_index]):
            continue
        system_index = headers["问题件后应签时间"]
        r13_index = headers["R13应签收时间"]
        system_value = row[system_index] if system_index < len(row) else ""
        r13_value = row[r13_index] if r13_index < len(row) else ""
        due_date_text = _sheet_sign_date(system_value) or _sheet_sign_date(r13_value)
        if due_date_text and date.fromisoformat(due_date_text) <= selected_date:
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
    return {
        "system": system,
        "label": label,
        "status": status,
        "status_label": status_label,
        "message": message,
        "total_count": None,
        "exception_count": None,
        "updated_at": updated_at,
        "categories": categories,
    }


def _source_snapshot(system: str, *, updated_at: str) -> dict[str, Any]:
    from agent.tms_runtime import monitoring_ronghui, monitoring_yunda

    adapter = monitoring_yunda if system == "yunda" else monitoring_ronghui
    label = "韵达" if system == "yunda" else "融辉 TMS"
    try:
        session = get_session_broker("yunda" if system == "yunda" else "default").build_requests_session(validate=False)
        try:
            categories = adapter.collect_categories(session)
        finally:
            session.close()
    except Exception as exc:
        return {
            **_system_payload(system=system, label=label, updated_at=updated_at,
                              status="auth_required" if isinstance(exc, TMSAuthStateError) else "error",
                              status_label="登录失效" if isinstance(exc, TMSAuthStateError) else "读取失败",
                              message="请检查该平台账号登录态和消息权限后刷新。"),
            "total_count": None, "exception_count": None,
        }
    failures = [item for item in categories if item.get("count") is None]
    included = [item for item in categories if item.get("include_in_total")]
    total = sum(item["count"] for item in included) if included and all(item.get("count") is not None for item in included) else None
    exceptions = [item for item in included if item.get("is_exception")]
    exception_count = sum(item["count"] for item in exceptions) if included and all(item.get("count") is not None for item in exceptions) else None
    return {
        "system": system, "label": label, "status": "partial" if failures else "ok",
        "status_label": "部分来源不可用" if failures else "已连接",
        "message": "各分类口径不同，数量分别展示，不重复合计。" if system == "yunda" else "",
        "total_count": total, "exception_count": exception_count,
        "updated_at": updated_at, "categories": categories,
    }


def _collect_yunda_snapshot(*, force: bool, updated_at: str) -> dict[str, Any]:
    return _source_snapshot("yunda", updated_at=updated_at)


def _collect_ronghui_snapshot(*, force: bool, updated_at: str) -> dict[str, Any]:
    return _source_snapshot("ronghui", updated_at=updated_at)


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


def _build_totals(systems: list[dict[str, Any]]) -> dict[str, Any]:
    by_system = {item.get("system"): item for item in systems}
    counts = [item.get("total_count") for item in systems]
    exceptions = [item.get("exception_count") for item in systems]
    return {
        "total_pending": sum(counts) if counts and all(isinstance(value, int) for value in counts) else None,
        "yunda_pending": by_system.get("yunda", {}).get("total_count"),
        "ronghui_pending": by_system.get("ronghui", {}).get("total_count"),
        "exception_pending": sum(exceptions) if exceptions and all(isinstance(value, int) for value in exceptions) else None,
    }


def _collect_system_snapshot(system: str, *, force: bool, updated_at: str) -> dict[str, Any]:
    if system == "yunda":
        payload = _collect_yunda_snapshot(force=force, updated_at=updated_at)
    elif system == "ronghui":
        payload = _collect_ronghui_snapshot(force=force, updated_at=updated_at)
    else:
        payload = _system_payload(
            system=system,
            label=system,
            status="error",
            status_label="未支持",
            updated_at=updated_at,
            message="不支持的监控系统。",
        )
    return payload


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
            except Exception:
                rows_by_system[system] = _system_payload(
                    system=system,
                    label={"yunda": "韵达", "ronghui": "融辉 TMS"}.get(system, system),
                    status="error",
                    status_label="接口异常",
                    updated_at=updated_at,
                    message="消息来源读取失败，请稍后刷新。",
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


_ITEM_CACHE: dict[tuple[str, str, str, int], tuple[float, dict[str, Any]]] = {}
_ITEM_LOCKS: dict[tuple[str, str, str, int], tuple[threading.Lock, int]] = {}


def build_monitoring_items(*, system: str, category_id: str, page: int = 1, force: bool = False) -> dict[str, Any]:
    """Read a fixed account's bounded page; never execute original-page actions."""
    from agent.tms_runtime import monitoring_ronghui, monitoring_yunda

    if system not in {"ronghui", "yunda"} or not category_id.startswith(system + ":") or not 1 <= page <= 10000:
        return {"ok": False, "error_code": "INVALID_MONITORING_QUERY", "message": "消息来源或页码无效。", "items": [], "total": None}
    profile = "yunda" if system == "yunda" else "default"
    adapter = monitoring_yunda if system == "yunda" else monitoring_ronghui
    allowed = monitoring_yunda.ITEM_CATEGORY_IDS if system == "yunda" else monitoring_ronghui.SOURCES
    if category_id not in allowed:
        return {"ok": False, "error_code": "INVALID_MONITORING_CATEGORY", "message": "该分类仅支持打开原系统。", "items": [], "total": None}
    key = (system, profile, category_id, page)
    with _CACHE_LOCK:
        now = time.time()
        expired = [item_key for item_key, (saved, _) in _ITEM_CACHE.items() if now - saved > CACHE_TTL_SEC]
        for item_key in expired:
            _ITEM_CACHE.pop(item_key, None)
        cached = _ITEM_CACHE.get(key)
        if not force and cached:
            return _with_cache_meta(cached[1], cached=True, stale=False, refreshing=False, cache_age_sec=now-cached[0])
        lock, users = _ITEM_LOCKS.get(key, (threading.Lock(), 0))
        _ITEM_LOCKS[key] = (lock, users + 1)
    try:
        with lock:
            # Waiters share the just-completed read, including forced refreshes.
            with _CACHE_LOCK:
                cached = _ITEM_CACHE.get(key)
                if cached and cached[0] >= now:
                    return _with_cache_meta(cached[1], cached=True, stale=False, refreshing=False)
            try:
                session = get_session_broker(profile).build_requests_session(validate=False)
                try:
                    result = adapter.collect_items(session, category_id, page, 20)
                finally:
                    session.close()
                if result.get("status") not in {"ok", "partial"}:
                    with _CACHE_LOCK:
                        _ITEM_CACHE.pop(key, None)
                    return {**result, "ok": False, "system": system, "category_id": category_id,
                            "error_code": "AUTH_REQUIRED" if result.get("status") == "auth_required" else "MONITORING_SOURCE_FAILED"}
                payload = {**result, "ok": True, "system": system, "category_id": category_id, "updated_at": _now_label()}
            except Exception as exc:
                with _CACHE_LOCK:
                    _ITEM_CACHE.pop(key, None)
                known = isinstance(exc, (monitoring_yunda.SourceError, monitoring_ronghui.RonghuiMonitoringError, TMSAuthStateError))
                auth = isinstance(exc, TMSAuthStateError)
                return {
                    "ok": False, "system": system, "category_id": category_id,
                    "status": "auth_required" if auth else "error", "status_label": "登录失效" if auth else "读取失败",
                    "error_code": "AUTH_REQUIRED" if auth else "MONITORING_SOURCE_FAILED",
                    "message": str(exc) if known else "消息来源读取失败，请稍后刷新或打开原系统核对。",
                    "items": [], "total": None, "page": page, "page_size": 20, "has_more": False,
                }
            with _CACHE_LOCK:
                # Bound retained pages without persisting business message bodies.
                if len(_ITEM_CACHE) >= 64:
                    _ITEM_CACHE.pop(next(iter(_ITEM_CACHE)))
                _ITEM_CACHE[key] = (time.time(), payload)
            return _with_cache_meta(payload, cached=False, stale=False, refreshing=False)
    finally:
        with _CACHE_LOCK:
            _, users = _ITEM_LOCKS[key]
            if users == 1:
                _ITEM_LOCKS.pop(key)
            else:
                _ITEM_LOCKS[key] = (lock, users - 1)


def build_monitoring_detail_link(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    system = _clean_text(payload.get("system")).lower()
    category_id = _clean_text(payload.get("category_id"))
    if system not in {"ronghui", "yunda"} or not category_id.startswith(system + ":"):
        return {"ok": False, "error_code": "INVALID_MONITORING_SYSTEM", "message": "不支持的监控分类。"}
    snapshot = build_monitoring_snapshot(systems=[system])
    matches = [category for source in snapshot.get("systems", []) for category in source.get("categories", [])
               if category.get("category_id") == category_id]
    if len(matches) != 1 or not matches[0].get("open_url"):
        return {"ok": False, "error_code": "MONITORING_CATEGORY_UNAVAILABLE", "message": "该消息分类暂不可用，请刷新后重试。"}
    category = matches[0]
    return {"ok": True, "system": system, "category_id": category_id, "title": category["title"],
            "mode": "external", "open_url": category["open_url"], "embed_url": "", "frame_allowed": False}
