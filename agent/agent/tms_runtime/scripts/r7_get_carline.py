import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

import requests

API_URL = "https://r7.ronghuiwl.com/gateway/tms/public/lineTask/pageGet"
REFERER_URL = "https://r7.ronghuiwl.com/operateManage/vehicleSchedule/vehicleRegular"

# "运输状态" 下拉框对应的 taskStatus 取值（从页面 Vue 组件 props 中读取到）
TASK_STATUS_CODE_TO_NAME: Dict[int, str] = {
    30: "待调度",
    40: "已调度",
    45: "装车待发",
    50: "在途",
    51: "经停点-车辆到达",
    52: "经停点-到达待卸",
    53: "经停点-装车待发",
    55: "车辆到达",
    58: "到达待卸",
    60: "完成",
    90: "取消",
}

DONE_STATUS_CODE = 60


def _validate_token_ascii(token: str, *, source: str) -> str:
    token = str(token or "").strip()
    if not token:
        raise RuntimeError(f"{source}: token is empty")
    if any(ch.isspace() for ch in token):
        raise RuntimeError(f"{source}: token contains whitespace; please copy the raw JWT only")
    # requests/urllib3 encodes headers as latin-1; aurora-token should be ASCII (JWT).
    if any(ord(ch) > 127 for ch in token):
        raise RuntimeError(f"{source}: token contains non-ASCII characters; please copy the raw accessToken (JWT)")
    return token


def _format_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _default_range(days: int) -> Tuple[str, str]:
    if days <= 0:
        days = 3
    now = datetime.now()
    end_dt = now.replace(hour=23, minute=59, second=59, microsecond=0)
    start_dt = (end_dt - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return _format_dt(start_dt), _format_dt(end_dt)


def _resolve_range(start: Optional[str], end: Optional[str], days: int) -> Tuple[str, str]:
    if start and end:
        return start, end
    return _default_range(days)


def _coerce_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


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


def _extract_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    total = data.get("total")
    if total is None:
        return None
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def _build_payload(*, start: str, end: str, page_size: int, page: int) -> Dict[str, Any]:
    return {
        "queryType": 1,
        "pageSize": page_size,
        "currentPage": page,
        "queryCount": True,
        "headPlanGoTime_CondStart": start,
        "headPlanGoTime_CondEnd": end,
        # 与前端“搜索”请求保持一致（页面默认会带上该条件）
        "publishStatus_CondList": ["20"],
    }


def _resolve_token(
    *,
    token: Optional[str],
    config_path: Optional[str],
    username: Optional[str],
    password: Optional[str],
    disable_proxy: bool,
    max_attempts: int,
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
            return _validate_token_ascii(env_token, source="env R7_TOKEN")
        except Exception as exc:
            # Common pitfall: accidentally set env token to a Chinese note like "拷贝当前token".
            # Keep JSON output clean: print warning to stderr and fallback to browser login.
            print(f"Warning: ignore env token ({type(exc).__name__}: {exc})", file=sys.stderr)

    # Prefer headless SSO login (requests) to obtain tokenValue, avoiding Playwright in server/sandbox envs.
    try:
        from r7_login_manager import R7SSOAuth  # type: ignore

        auth = R7SSOAuth(config_path=config_path, disable_proxy=bool(disable_proxy))
        auth.login_and_get_session(
            username=username,
            password=password,
            max_attempts=max(1, int(max_attempts)),
            attach_bearer=False,
            exchange=False,
            verify=False,
        )
        if auth.last_token:
            return _validate_token_ascii(str(auth.last_token), source="sso tokenValue")
        raise RuntimeError("SSO login returned empty tokenValue")
    except Exception as exc:
        # Keep JSON output clean: print warning to stderr and fallback to browser login.
        print(
            f"Warning: headless SSO login failed ({type(exc).__name__}: {exc}); fallback to browser login.",
            file=sys.stderr,
        )

    # 沿用 scripts/r7_login.py：用 Playwright 登录后，从 localStorage 读取 accessToken 作为 aurora-token
    try:
        from browser_manager import launch_browser  # type: ignore
        from r7_login import (  # type: ignore
            DEFAULT_PASSWORD,
            DEFAULT_USERNAME,
            HOME_URL,
            build_auth,
            ensure_logged_in,
        )
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Missing token. Provide --token / env R7_TOKEN, or install Playwright+deps to login via r7_login.py."
        ) from exc

    _ = config_path  # keep for backward-compatible signature; r7_login.py currently doesn't read it
    username_value = (username or DEFAULT_USERNAME or "").strip()
    password_value = (password or DEFAULT_PASSWORD or "").strip()
    if not username_value or not password_value:
        raise RuntimeError("Missing username/password for browser login. Provide --username/--password.")

    p = browser = context = page = None
    try:
        p, browser, context, page = launch_browser(
            headless=bool(browser_headless),
            slow_mo_ms=int(browser_slow_mo_ms),
            channel=browser_channel or None,
            use_tms_storage_state=False,
        )
        auth = build_auth(max_attempts=max(1, int(max_attempts)))
        ensure_logged_in(page, auth, username=username_value, password=password_value)

        # Ensure we're in r7 origin so we can read r7 localStorage.
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
            raise RuntimeError(f"accessToken not found in localStorage after login. localStorage keys={keys}")
        return _validate_token_ascii(token_text, source="browser localStorage accessToken")
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass


def _build_session(*, disable_proxy: bool) -> requests.Session:
    sess = requests.Session()
    if disable_proxy:
        sess.trust_env = False
    return sess


def fetch_carline(
    *,
    config_path: Optional[str],
    token: Optional[str],
    username: Optional[str],
    password: Optional[str],
    start: Optional[str],
    end: Optional[str],
    days: int,
    page_size: int,
    page: int,
    fetch_all: bool,
    max_pages: int,
    max_login_attempts: int,
    disable_proxy: bool,
    browser_headless: bool,
    browser_slow_mo_ms: int,
    browser_channel: Optional[str],
) -> List[Dict[str, Any]]:
    token_value = _resolve_token(
        token=token,
        config_path=config_path,
        username=username,
        password=password,
        disable_proxy=disable_proxy,
        max_attempts=max_login_attempts,
        browser_headless=browser_headless,
        browser_slow_mo_ms=browser_slow_mo_ms,
        browser_channel=browser_channel,
    )
    session = _build_session(disable_proxy=disable_proxy)

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "aurora-token": token_value,
        "x-appId": "tms",
        "aurora-back": REFERER_URL,
        "Referer": REFERER_URL,
    }

    start_value, end_value = _resolve_range(start, end, days)
    current_page = max(1, page)
    page_size_value = max(1, page_size)
    fetched_pages = 0

    result: List[Dict[str, Any]] = []
    while True:
        payload = _build_payload(
            start=start_value,
            end=end_value,
            page_size=page_size_value,
            page=current_page,
        )
        resp = session.post(API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rows = _extract_rows(data)

        for row in rows:
            task_status = row.get("taskStatus")
            try:
                task_status_code = int(task_status) if task_status is not None else None
            except (TypeError, ValueError):
                task_status_code = None
            status_name = row.get("taskStatusName")
            if isinstance(status_name, str):
                status_name = status_name.strip()
            if not status_name and task_status_code is not None:
                status_name = TASK_STATUS_CODE_TO_NAME.get(task_status_code)

            # Exclude "完成"
            if task_status_code == DONE_STATUS_CODE or status_name == "完成":
                continue

            result.append(
                {
                    "taskNumber": row.get("taskNumber"),
                    "taskStatus": task_status_code,
                    "taskStatusName": status_name,
                    "headPlanGoTime": row.get("headPlanGoTime"),
                    "headPlanArriveTime": row.get("headPlanArriveTime"),
                    "className": row.get("className"),
                }
            )

        fetched_pages += 1
        if not fetch_all:
            break
        if not rows:
            break
        if len(rows) < page_size_value:
            break

        total = _extract_total(data)
        if total is not None and total >= 0:
            total_pages = max(1, ceil(total / page_size_value))
            if current_page >= total_pages:
                break

        if fetched_pages >= max_pages:
            break

        current_page += 1

    return result


def run_once(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    return fetch_carline(
        config_path=params.get("config_path") or params.get("configPath"),
        token=params.get("token"),
        username=params.get("username") or params.get("user"),
        password=params.get("password") or params.get("pass"),
        start=params.get("start"),
        end=params.get("end"),
        days=_coerce_int(params.get("days"), default=3),
        page_size=_coerce_int(params.get("page_size") or params.get("pageSize"), default=200),
        page=_coerce_int(params.get("page") or params.get("currentPage"), default=1),
        fetch_all=_coerce_bool(params.get("fetch_all") or params.get("fetchAll"), default=True),
        max_pages=_coerce_int(params.get("max_pages") or params.get("maxPages"), default=200),
        max_login_attempts=_coerce_int(params.get("max_login_attempts") or params.get("maxLoginAttempts"), default=3),
        disable_proxy=_coerce_bool(params.get("disable_proxy") or params.get("disableProxy"), default=False),
        browser_headless=_coerce_bool(params.get("browser_headless") or params.get("browserHeadless"), default=True),
        browser_slow_mo_ms=_coerce_int(params.get("browser_slow_mo_ms") or params.get("browserSlowMoMs"), default=0),
        browser_channel=(params.get("browser_channel") or params.get("browserChannel") or None),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch R7 运输任务管理列表（过去N天），过滤掉运输状态=完成，输出任务号/状态/计划发车/计划到达/班次名称。",
    )
    parser.add_argument("--config-path", default=None)
    parser.add_argument(
        "--token",
        default=None,
        help="aurora-token (JWT). If omitted, will try env R7_TOKEN; otherwise login via scripts/r7_login.py (Playwright) and read localStorage accessToken.",
    )
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--start", default=None, help="YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument(
        "--fetch-all",
        action="store_true",
        default=True,
        help="Fetch all pages (default: true). Use --no-fetch-all to only fetch one page.",
    )
    parser.add_argument("--no-fetch-all", dest="fetch_all", action="store_false")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--max-login-attempts", type=int, default=3)
    parser.add_argument("--disable-proxy", action="store_true", help="Disable reading proxy from environment.")
    parser.add_argument("--headed", action="store_true", help="Run browser with UI when auto-login to get token.")
    parser.add_argument("--slow-mo-ms", type=int, default=0, help="Playwright slow_mo in ms (only for browser login).")
    parser.add_argument("--browser-channel", default=None, help="Playwright chromium channel, e.g. chrome.")
    args = parser.parse_args()

    result = fetch_carline(
        config_path=args.config_path,
        token=args.token,
        username=args.username,
        password=args.password,
        start=args.start,
        end=args.end,
        days=args.days,
        page_size=args.page_size,
        page=args.page,
        fetch_all=bool(args.fetch_all),
        max_pages=args.max_pages,
        max_login_attempts=args.max_login_attempts,
        disable_proxy=args.disable_proxy,
        browser_headless=(not bool(args.headed)),
        browser_slow_mo_ms=int(args.slow_mo_ms),
        browser_channel=(str(args.browser_channel).strip() if args.browser_channel else None),
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
