from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from agent.tms_runtime.scripts.mysql_sink import MySQLSink, load_mysql_config

from agent.tms_runtime.scripts import auto_checkin_r7


API_URL = "https://r7.ronghuiwl.com/gateway/tms/public/lineTask/pageGet"
REFERER_URL = "https://r7.ronghuiwl.com/operateManage/vehicleSchedule/vehicleRegular"


TASK_STATUS_CODE_TO_NAME: Dict[int, str] = {
    30: "待调度",
    40: "已调度",
    45: "装车待发",
    50: "在途",  # 用户也会称为“途中”
    51: "经停点-车辆到达",
    52: "经停点-到达待卸",
    53: "经停点-装车待发",
    55: "车辆到达",
    58: "到达待卸",
    60: "完成",
    90: "取消",
}


DONE_STATUS_CODE = 60
CAR_ARRIVED_CODE = 55
ARRIVE_WAIT_UNLOAD_CODE = 58


def _validate_token_ascii(token: str, *, source: str) -> str:
    token = str(token or "").strip()
    if not token:
        raise RuntimeError(f"{source}: token is empty")
    if any(ch.isspace() for ch in token):
        raise RuntimeError(f"{source}: token contains whitespace; please copy the raw JWT only")
    if any(ord(ch) > 127 for ch in token):
        raise RuntimeError(f"{source}: token contains non-ASCII characters; please copy the raw accessToken (JWT)")
    return token


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _default_range(days: int) -> Tuple[str, str]:
    if days <= 0:
        days = 3
    now = datetime.now()
    end_dt = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start_dt = (end_dt - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return _format_dt(start_dt), _format_dt(end_dt)


def _sleep(seconds: float, *, label: str = "") -> None:
    sec = max(0.0, float(seconds))
    if not label:
        time.sleep(sec)
        return
    # Print a heartbeat every ~60s so it's visible in logs.
    end = time.time() + sec
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        if remaining <= 10 or int(remaining) % 60 == 0:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {label} sleep {int(remaining)}s", flush=True)
        time.sleep(min(1.0, remaining))


def _in_window(now: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = [int(x) for x in start_hhmm.split(":", 1)]
    eh, em = [int(x) for x in end_hhmm.split(":", 1)]
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now < end


def _seconds_until_window_start(now: datetime, start_hhmm: str, end_hhmm: str) -> int:
    sh, sm = [int(x) for x in start_hhmm.split(":", 1)]
    eh, em = [int(x) for x in end_hhmm.split(":", 1)]
    start_today = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_today = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if now < start_today:
        return int((start_today - now).total_seconds())
    if now >= end_today:
        start_next = start_today + timedelta(days=1)
        return int((start_next - now).total_seconds())
    return 0


def _resolve_token_via_env_or_browser(
    *,
    token: Optional[str],
    username: Optional[str],
    password: Optional[str],
    max_login_attempts: int,
    browser_headless: bool,
    browser_slow_mo_ms: int,
    browser_channel: Optional[str],
) -> str:
    if token:
        return _validate_token_ascii(str(token), source="--token")

    env_token = (
        os.environ.get("R7_TOKEN")
        or os.environ.get("R7_ACCESS_TOKEN")
        or os.environ.get("AURORA_TOKEN")
        or os.environ.get("ACCESS_TOKEN")
    )
    if env_token:
        try:
            return _validate_token_ascii(str(env_token), source="env R7_TOKEN")
        except Exception as exc:
            print(f"Warning: ignore env token ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)

    # Use r7_login.py (Playwright) to login and read localStorage['accessToken'].
    from agent.tms_runtime.scripts.browser_manager import launch_browser
    from agent.tms_runtime.scripts.r7_login import (
        DEFAULT_PASSWORD,
        DEFAULT_USERNAME,
        HOME_URL,
        build_auth,
        ensure_logged_in,
    )

    u = (username or DEFAULT_USERNAME or "").strip()
    p = (password or DEFAULT_PASSWORD or "").strip()
    if not u or not p:
        raise RuntimeError("Missing token and username/password. Provide --token or --username/--password.")

    pw = br = ctx = page = None
    try:
        pw, br, ctx, page = launch_browser(
            headless=bool(browser_headless),
            slow_mo_ms=int(browser_slow_mo_ms),
            channel=browser_channel or None,
            use_tms_storage_state=False,
        )
        auth = build_auth(max_attempts=max(1, int(max_login_attempts)))
        ensure_logged_in(page, auth, username=u, password=p)
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        token_value = page.evaluate(
            "() => localStorage.getItem('accessToken') || sessionStorage.getItem('accessToken')"
        )
        token_text = str(token_value).strip() if token_value else ""
        if not token_text:
            keys = page.evaluate("() => Object.keys(localStorage)")
            raise RuntimeError(f"accessToken not found in localStorage after login. keys={keys}")
        return _validate_token_ascii(token_text, source="browser localStorage accessToken")
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
        try:
            if br is not None:
                br.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass


def _build_payload(*, start: str, end: str, page_size: int, page: int) -> Dict[str, Any]:
    return {
        "queryType": 1,
        "pageSize": int(page_size),
        "currentPage": int(page),
        "queryCount": True,
        "headPlanGoTime_CondStart": start,
        "headPlanGoTime_CondEnd": end,
        "publishStatus_CondList": ["20"],
    }


def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("data")
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def fetch_tasks(
    *,
    session: requests.Session,
    token: str,
    start: str,
    end: str,
    page_size: int = 200,
    page: int = 1,
) -> List[Dict[str, Any]]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "aurora-token": token,
        "x-appId": "tms",
        "aurora-back": REFERER_URL,
        "Referer": REFERER_URL,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
    }
    payload = _build_payload(start=start, end=end, page_size=page_size, page=page)
    resp = session.post(API_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return _extract_rows(data)


def _task_status_code(row: Dict[str, Any]) -> Optional[int]:
    value = row.get("taskStatus")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_status_name(row: Dict[str, Any]) -> Optional[str]:
    name = row.get("taskStatusName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    code = _task_status_code(row)
    if code is None:
        return None
    return TASK_STATUS_CODE_TO_NAME.get(code)


@dataclass(frozen=True)
class IntervalPolicy:
    min_sec: int
    max_sec: int

    def pick(self) -> int:
        lo = max(1, int(self.min_sec))
        hi = max(lo, int(self.max_sec))
        return random.randint(lo, hi)


def compute_next_interval_sec(
    *,
    task_status: Optional[int],
    task_status_name: Optional[str],
    plan_arrive_time: Optional[str],
    pending_policy: IntervalPolicy,
    default_policy: IntervalPolicy,
    loading_policy: IntervalPolicy,
    intransit_policy: IntervalPolicy,
    near_arrival_policy: IntervalPolicy,
    near_arrival_window_sec: int,
    jitter_sec: int,
    min_interval_sec: int,
    max_interval_sec: int,
) -> int:
    status = task_status
    status_name = (task_status_name or "").strip()
    policy = default_policy

    if status in (30,) or status_name == "待调度":
        policy = pending_policy
    elif status in (45,) or status_name == "装车待发":
        policy = loading_policy
    elif status in (CAR_ARRIVED_CODE,) or status_name == "车辆到达":
        # When vehicle already arrived, poll faster to perform check-in quickly / retry on failures.
        policy = near_arrival_policy
    elif status in (50,) or status_name in {"在途", "途中"}:
        policy = intransit_policy
        plan_dt = _parse_dt(plan_arrive_time)
        if plan_dt is not None:
            delta = (plan_dt - datetime.now()).total_seconds()
            if delta <= float(near_arrival_window_sec):
                policy = near_arrival_policy
    elif status in (ARRIVE_WAIT_UNLOAD_CODE,) or status_name == "到达待卸":
        # When already at "到达待卸", poll faster in case we need a retry.
        policy = near_arrival_policy

    value = policy.pick()
    value = min(max_interval_sec, max(min_interval_sec, int(value)))
    if jitter_sec > 0:
        value += random.randint(0, int(jitter_sec))
    return max(1, int(value))


def _trim_row_for_db(row: Dict[str, Any]) -> Dict[str, Any]:
    want = [
        "taskNumber",
        "className",
        "taskStatus",
        "taskStatusName",
        "headPlanGoTime",
        "headPlanArriveTime",
        "lineName",
        "headCarNumber",
        "trunkCarNumber",
    ]
    out: Dict[str, Any] = {}
    for k in want:
        if k in row:
            out[k] = row.get(k)
    return out


def watch_loop(
    *,
    class_name: str,
    station_name: str,
    token: Optional[str],
    username: Optional[str],
    password: Optional[str],
    days: int,
    start_hhmm: str,
    end_hhmm: str,
    disable_proxy: bool,
    max_login_attempts: int,
    browser_headless: bool,
    browser_slow_mo_ms: int,
    browser_channel: Optional[str],
    stop_after_checkin: bool,
    stop_after_done: bool,
    pending_policy: IntervalPolicy,
    default_policy: IntervalPolicy,
    loading_policy: IntervalPolicy,
    intransit_policy: IntervalPolicy,
    near_arrival_policy: IntervalPolicy,
    near_arrival_window_sec: int,
    jitter_sec: int,
    min_interval_sec: int,
    max_interval_sec: int,
    mysql_config_path: Optional[str],
) -> int:
    class_name = (class_name or "").strip()
    station_name = (station_name or "").strip()
    if not class_name:
        raise ValueError("class_name is empty")
    if not station_name:
        raise ValueError("station_name is empty")

    mysql_cfg = load_mysql_config(config_path=mysql_config_path)
    sink = MySQLSink(mysql_cfg) if mysql_cfg else None

    def db_event(event_type: str, row: Optional[Dict[str, Any]], *, ok: Optional[bool], message: str, detail: Dict[str, Any]):
        if sink is None:
            return
        try:
            base = row or {}
            sink.insert_event(
                event_type=event_type,
                task_number=str(base.get("taskNumber") or "") or None,
                class_name=str(base.get("className") or "") or None,
                task_status=_task_status_code(base) if isinstance(base, dict) else None,
                task_status_name=_task_status_name(base) if isinstance(base, dict) else None,
                plan_go_time=str(base.get("headPlanGoTime") or "") or None,
                plan_arrive_time=str(base.get("headPlanArriveTime") or "") or None,
                ok=ok,
                manual_arrive_time=detail.get("manual_arrive_time"),
                message=message,
                detail=detail,
            )
        except Exception as exc:
            print(f"[db] insert_event failed: {type(exc).__name__}: {exc}", flush=True)

    def db_status(row: Dict[str, Any], *, checkin_success: bool, manual_arrive_time: Optional[str]):
        if sink is None:
            return
        try:
            sink.upsert_status(
                task_number=str(row.get("taskNumber") or ""),
                class_name=str(row.get("className") or "") or None,
                task_status=_task_status_code(row),
                task_status_name=_task_status_name(row),
                plan_go_time=str(row.get("headPlanGoTime") or "") or None,
                plan_arrive_time=str(row.get("headPlanArriveTime") or "") or None,
                checkin_success=bool(checkin_success),
                manual_arrive_time=manual_arrive_time,
                detail=_trim_row_for_db(row),
            )
        except Exception as exc:
            print(f"[db] upsert_status failed: {type(exc).__name__}: {exc}", flush=True)

    checkin_done_tasks: Dict[str, str] = {}  # taskNumber -> manual_arrive_time
    last_error_backoff_sec = 0

    # Build session once to reuse connections.
    session = requests.Session()
    if disable_proxy:
        session.trust_env = False

    start_value, end_value = _default_range(days)

    while True:
        now = datetime.now()
        if not _in_window(now, start_hhmm, end_hhmm):
            wait_sec = _seconds_until_window_start(now, start_hhmm, end_hhmm)
            if wait_sec > 0:
                print(f"[{_format_dt(now)}] outside window; sleep {wait_sec}s", flush=True)
                _sleep(wait_sec, label="outside-window")
            continue

        # Refresh token lazily only when needed or on auth failures.
        try:
            token_value = _resolve_token_via_env_or_browser(
                token=token,
                username=username,
                password=password,
                max_login_attempts=max_login_attempts,
                browser_headless=browser_headless,
                browser_slow_mo_ms=browser_slow_mo_ms,
                browser_channel=browser_channel,
            )
        except Exception as exc:
            msg = f"token_resolve_failed: {type(exc).__name__}: {exc}"
            print(f"[{_format_dt(now)}] {msg}", flush=True)
            db_event("token_error", None, ok=False, message=msg, detail={"error": str(exc)})
            _sleep(300, label="token-error")
            continue

        try:
            rows = fetch_tasks(session=session, token=token_value, start=start_value, end=end_value)
            last_error_backoff_sec = 0
        except Exception as exc:
            # Backoff on network/API errors to avoid hammering and reduce anti-bot risk.
            backoff = 60 if last_error_backoff_sec <= 0 else min(900, int(last_error_backoff_sec * 2))
            last_error_backoff_sec = backoff
            msg = f"fetch_failed: {type(exc).__name__}: {exc}"
            print(f"[{_format_dt(now)}] {msg}; backoff={backoff}s", flush=True)
            db_event("fetch_error", None, ok=False, message=msg, detail={"error": str(exc), "backoff_sec": backoff})
            _sleep(backoff, label="fetch-error")
            continue

        targets = [r for r in rows if str(r.get("className") or "").strip() == class_name]
        if not targets:
            msg = f"no task for className={class_name}"
            print(f"[{_format_dt(now)}] {msg}", flush=True)
            db_event("no_task", None, ok=True, message=msg, detail={"className": class_name})
            interval = pending_policy.pick()
            interval = min(max_interval_sec, max(min_interval_sec, interval))
            interval += random.randint(0, max(0, int(jitter_sec)))
            _sleep(interval, label="no-task")
            continue

        def _is_done_like(r: Dict[str, Any]) -> bool:
            status = _task_status_code(r)
            name = (_task_status_name(r) or "").strip()
            return status in (DONE_STATUS_CODE, 90) or name in {"完成", "取消"}

        # Prefer non-done tasks; among them, prefer the one with latest plan go time.
        def _sort_key(r: Dict[str, Any]):
            status = _task_status_code(r) or 0
            done_flag = 1 if status in (DONE_STATUS_CODE, 90) else 0
            go = _parse_dt(r.get("headPlanGoTime")) or datetime(1970, 1, 1)
            return (done_flag, -int(go.timestamp()))

        targets.sort(key=_sort_key)
        required = [r for r in targets if not _is_done_like(r)]
        if not required:
            msg = f"no active task (non-done) for className={class_name}; targets={len(targets)}"
            print(f"[{_format_dt(now)}] {msg}", flush=True)
            db_event(
                "no_active_task",
                None,
                ok=True,
                message=msg,
                detail={"className": class_name, "targets": int(len(targets))},
            )
            if stop_after_done:
                msg2 = "stop_after_done: no active task (all done/canceled)"
                db_event("stop_done", targets[0], ok=True, message=msg2, detail={"className": class_name})
                print(f"[{_format_dt(now)}] stop: {msg2}", flush=True)
                return 0
            interval = pending_policy.pick()
            interval = min(max_interval_sec, max(min_interval_sec, interval))
            interval += random.randint(0, max(0, int(jitter_sec)))
            _sleep(interval, label="no-active-task")
            continue

        # Track/update status for all required tasks; any one task near-arrival should drive a shorter poll interval.
        print(f"[{_format_dt(now)}] className={class_name} tasks={len(required)}", flush=True)
        tasks_need_checkin: List[Dict[str, Any]] = []
        required_task_nos: List[str] = []

        for r in required:
            task_no = str(r.get("taskNumber") or "").strip()
            if not task_no:
                continue
            required_task_nos.append(task_no)

            status_code = _task_status_code(r)
            status_name = _task_status_name(r)
            plan_go = str(r.get("headPlanGoTime") or "").strip() or None
            plan_arrive = str(r.get("headPlanArriveTime") or "").strip() or None

            manual_time = checkin_done_tasks.get(task_no)
            print(
                f"  task={task_no} status={status_name or status_code} planGo={plan_go} "
                f"planArrive={plan_arrive} checked={'Y' if manual_time else 'N'}",
                flush=True,
            )

            db_status(r, checkin_success=bool(manual_time), manual_arrive_time=manual_time)
            db_event(
                "poll",
                r,
                ok=True,
                message="poll",
                detail={
                    "taskNumber": task_no,
                    "className": class_name,
                    "taskStatus": status_code,
                    "taskStatusName": status_name,
                    "planGo": plan_go,
                    "planArrive": plan_arrive,
                    "checkin_success": bool(manual_time),
                    "manual_arrive_time": manual_time,
                },
            )

            if status_code == CAR_ARRIVED_CODE and not manual_time:
                tasks_need_checkin.append(r)

        # Check-in all tasks that are currently at "车辆到达" and not yet marked success.
        for r in tasks_need_checkin:
            task_no = str(r.get("taskNumber") or "").strip()
            if not task_no:
                continue
            if checkin_done_tasks.get(task_no):
                continue

            print(f"[{_format_dt(now)}] start checkin flow task={task_no}", flush=True)
            db_event("checkin_start", r, ok=None, message="checkin_start", detail={"taskNumber": task_no})
            try:
                result = auto_checkin_r7.checkin_task_and_verify_manual_arrival(
                    {
                        "username": username,
                        "password": password,
                        "task_number": task_no,
                        "station_name": station_name,
                        "headless": browser_headless,
                        "slow_mo_ms": browser_slow_mo_ms,
                        "max_login_attempts": max_login_attempts,
                        # keep same search range as polling
                        "apply_last_3_days": True,
                    }
                )
            except Exception as exc:
                msg = f"checkin_exception: {type(exc).__name__}: {exc}"
                print(f"[{_format_dt(now)}] {msg}", flush=True)
                db_event("checkin_error", r, ok=False, message=msg, detail={"error": str(exc), "taskNumber": task_no})
                continue

            ok = bool(result.get("ok"))
            manual_arrive_time = (
                (result.get("detail") or {}).get("manual_arrive_time")
                if isinstance(result.get("detail"), dict)
                else None
            )
            if isinstance(manual_arrive_time, str):
                manual_arrive_time = manual_arrive_time.strip() or None
            if ok and manual_arrive_time:
                checkin_done_tasks[task_no] = str(manual_arrive_time)

            db_event(
                "checkin_result",
                r,
                ok=ok,
                message=str(result.get("message") or ""),
                detail={
                    "taskNumber": task_no,
                    "ok": ok,
                    "stage": result.get("stage"),
                    "manual_arrive_time": manual_arrive_time,
                    "raw": result,
                },
            )
            db_status(r, checkin_success=bool(ok and manual_arrive_time), manual_arrive_time=manual_arrive_time)

            # Add a small random pause between check-ins to reduce anti-bot risk.
            _sleep(random.randint(2, 6), label="between-checkin")

        # Stop rule: by default, stop when ALL required tasks for this className have check-in success.
        if stop_after_checkin and required_task_nos:
            all_checked = all(bool(checkin_done_tasks.get(tn)) for tn in required_task_nos)
            if all_checked:
                msg = f"stop_after_checkin: all tasks checked ({len(required_task_nos)}/{len(required_task_nos)})"
                db_event(
                    "stop_checkin_all",
                    required[0],
                    ok=True,
                    message=msg,
                    detail={"className": class_name, "tasks": required_task_nos},
                )
                print(f"[{_format_dt(now)}] stop: {msg}", flush=True)
                return 0

        # Compute next interval based on the MOST urgent task among the required tasks.
        intervals: List[int] = []
        for r in required:
            status_code = _task_status_code(r)
            status_name = _task_status_name(r)
            plan_arrive = str(r.get("headPlanArriveTime") or "").strip() or None
            intervals.append(
                compute_next_interval_sec(
                    task_status=status_code,
                    task_status_name=status_name,
                    plan_arrive_time=plan_arrive,
                    pending_policy=pending_policy,
                    default_policy=default_policy,
                    loading_policy=loading_policy,
                    intransit_policy=intransit_policy,
                    near_arrival_policy=near_arrival_policy,
                    near_arrival_window_sec=near_arrival_window_sec,
                    jitter_sec=jitter_sec,
                    min_interval_sec=min_interval_sec,
                    max_interval_sec=max_interval_sec,
                )
            )
        interval = min(intervals) if intervals else pending_policy.pick()
        _sleep(interval, label="poll-interval")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Watch R7 运输任务管理列表: 轮询接口筛选指定班次(className)，当任务到达“车辆到达”时自动打卡并验证；"
            "默认在该班次的所有未完成任务都打卡成功后退出。"
        )
    )
    parser.add_argument("--class-name", default="长沙-邵阳操作场")
    parser.add_argument("--station-name", default="邵阳操作场")
    parser.add_argument("--token", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--start", default="09:00", help="work window start HH:MM")
    parser.add_argument("--end", default="21:00", help="work window end HH:MM")
    parser.add_argument("--disable-proxy", action="store_true", help="Disable system proxy for requests.")
    parser.add_argument("--max-login-attempts", type=int, default=6)
    parser.add_argument("--headed", action="store_true", help="Run browser with UI for login/checkin.")
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    parser.add_argument("--browser-channel", default=None)
    parser.add_argument(
        "--stop-after-checkin",
        action="store_true",
        default=True,
        help="Stop when ALL matched non-done tasks are checked-in successfully (default: true).",
    )
    parser.add_argument("--no-stop-after-checkin", dest="stop_after_checkin", action="store_false")
    parser.add_argument("--stop-after-done", action="store_true", default=False)
    parser.add_argument("--pending-min", type=int, default=600)
    parser.add_argument("--pending-max", type=int, default=1200)
    parser.add_argument("--default-min", type=int, default=600)
    parser.add_argument("--default-max", type=int, default=900)
    parser.add_argument("--loading-min", type=int, default=300)
    parser.add_argument("--loading-max", type=int, default=600)
    parser.add_argument("--intransit-min", type=int, default=600)
    parser.add_argument("--intransit-max", type=int, default=900)
    parser.add_argument("--near-arrival-min", type=int, default=120)
    parser.add_argument("--near-arrival-max", type=int, default=240)
    parser.add_argument("--near-arrival-window-sec", type=int, default=3600)
    parser.add_argument("--jitter-sec", type=int, default=30)
    parser.add_argument("--min-interval-sec", type=int, default=120)
    parser.add_argument("--max-interval-sec", type=int, default=1200)
    parser.add_argument("--mysql-config-path", default=None, help="Optional JSON config containing {\"mysql\":{...}}.")
    args = parser.parse_args(argv)

    try:
        return watch_loop(
            class_name=str(args.class_name),
            station_name=str(args.station_name),
            token=args.token,
            username=args.username,
            password=args.password,
            days=int(args.days),
            start_hhmm=str(args.start),
            end_hhmm=str(args.end),
            disable_proxy=bool(args.disable_proxy),
            max_login_attempts=int(args.max_login_attempts),
            browser_headless=(not bool(args.headed)),
            browser_slow_mo_ms=int(args.slow_mo_ms),
            browser_channel=(str(args.browser_channel).strip() if args.browser_channel else None),
            stop_after_checkin=bool(args.stop_after_checkin),
            stop_after_done=bool(args.stop_after_done),
            pending_policy=IntervalPolicy(int(args.pending_min), int(args.pending_max)),
            default_policy=IntervalPolicy(int(args.default_min), int(args.default_max)),
            loading_policy=IntervalPolicy(int(args.loading_min), int(args.loading_max)),
            intransit_policy=IntervalPolicy(int(args.intransit_min), int(args.intransit_max)),
            near_arrival_policy=IntervalPolicy(int(args.near_arrival_min), int(args.near_arrival_max)),
            near_arrival_window_sec=int(args.near_arrival_window_sec),
            jitter_sec=int(args.jitter_sec),
            min_interval_sec=int(args.min_interval_sec),
            max_interval_sec=int(args.max_interval_sec),
            mysql_config_path=args.mysql_config_path,
        )
    except KeyboardInterrupt:
        print("Interrupted.", flush=True)
        return 130
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
