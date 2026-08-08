"""
Fetch arrive-list source rows from the R7 "预到达清单" page.

The login step uses the dedicated R7 browser login flow in headless mode, then
the script calls the page's backend POST endpoint directly.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote

import requests

from agent.tms_runtime.scripts.browser_manager import launch_browser
from agent.tms_runtime.scripts.r7_login import HOME_URL, build_auth, ensure_logged_in
from agent.tms_runtime.scripts.r7_login_manager import R7SSOAuth


PRE_ARRIVE_PAGE_URL = "https://r7.ronghuiwl.com/operateManage/preArriveList"
PRE_ARRIVE_API_URL = "https://r7.ronghuiwl.com/gateway/bicenter/scanSendData/preArrivalPageList"
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
DEFAULT_SITE_CODE = "7390004"

USER_INFO_JS = """
() => {
  const storages = [window.localStorage, window.sessionStorage];
  const preferred = ["userInfo", "USER_INFO", "user_info", "loginInfo", "loginUserInfo"];
  const parseValue = (value) => {
    if (!value) return null;
    try { return JSON.parse(value); } catch (e) { return null; }
  };
  const unwrap = (value) => {
    if (!value || typeof value !== "object") return null;
    if (value.siteCode) return value;
    if (value.result && value.result.siteCode) return value.result;
    if (value.data && value.data.siteCode) return value.data;
    return null;
  };
  for (const storage of storages) {
    if (!storage) continue;
    for (const key of preferred) {
      const resolved = unwrap(parseValue(storage.getItem(key)));
      if (resolved) return resolved;
    }
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      const resolved = unwrap(parseValue(storage.getItem(key)));
      if (resolved) return resolved;
    }
  }
  return {};
}
"""


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


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _extract_input_value(html: str, input_id: str) -> Optional[str]:
    pattern = rf"id=[\"']{re.escape(input_id)}[\"'][^>]*value=[\"']([^\"']*)[\"']"
    match = re.search(pattern, html)
    if not match:
        return None
    return match.group(1)


def _js_unescape(value: str) -> str:
    if not value:
        return ""

    def _replace_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    text = re.sub(r"%u([0-9A-Fa-f]{4})", _replace_unicode, value)
    return unquote(text)


def _read_user_info_cookie(session: requests.Session) -> dict[str, Any]:
    raw = session.cookies.get("userInfo") or session.cookies.get("USER_INFO")
    if not raw:
        return {}
    try:
        decoded = _js_unescape(raw)
        data = json.loads(decoded)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_site_code_from_user_info(user_info: dict[str, Any]) -> str:
    queue: list[dict[str, Any]] = [user_info] if isinstance(user_info, dict) else []
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        for key in ("siteCode", "loginSiteCode", "site_code", "login_site_code"):
            value = _clean_str(current.get(key))
            if value:
                return value
        for nested_key in ("result", "data", "userInfo", "loginInfo", "loginUserInfo", "user"):
            nested = current.get(nested_key)
            if isinstance(nested, dict):
                queue.append(nested)
    return ""


def _extract_site_code_from_html(html: str) -> str:
    candidates = (
        _extract_input_value(html, "loginSiteCode"),
        _extract_input_value(html, "siteCode"),
    )
    for candidate in candidates:
        value = _clean_str(candidate)
        if value:
            return value

    patterns = (
        r'"loginSiteCode"\s*:\s*"([^"]+)"',
        r'"siteCode"\s*:\s*"([^"]+)"',
        r"loginSiteCode\s*[:=]\s*['\"]([^'\"]+)['\"]",
        r"siteCode\s*[:=]\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if not match:
            continue
        value = _clean_str(match.group(1))
        if value:
            return value
    return ""


def _resolve_site_code_http(
    session: requests.Session,
    params: dict[str, Any],
    *,
    timeout_sec: int,
) -> tuple[str, str]:
    explicit = _clean_str(
        params.get("site_code")
        or params.get("siteCode")
        or params.get("login_site_code")
        or params.get("loginSiteCode")
    )
    if explicit:
        return explicit, "param"

    cookie_user_info = _read_user_info_cookie(session)
    cookie_site_code = _resolve_site_code_from_user_info(cookie_user_info)
    if cookie_site_code:
        return cookie_site_code, "userInfo_cookie"

    response = session.get(PRE_ARRIVE_PAGE_URL, timeout=timeout_sec)
    response.raise_for_status()
    html = response.text or ""

    cookie_user_info = _read_user_info_cookie(session)
    cookie_site_code = _resolve_site_code_from_user_info(cookie_user_info)
    if cookie_site_code:
        return cookie_site_code, "userInfo_cookie_after_page"

    html_site_code = _extract_site_code_from_html(html)
    if html_site_code:
        return html_site_code, "pre_arrive_page_html"

    home_response = session.get(HOME_URL, timeout=timeout_sec)
    home_response.raise_for_status()
    home_html_site_code = _extract_site_code_from_html(home_response.text or "")
    if home_html_site_code:
        return home_html_site_code, "home_page_html"

    env_site_code = _clean_str(
        os.environ.get("R7_CAOZUOCHANG_SITE_CODE")
        or os.environ.get("R7_LOGIN_SITE_CODE")
        or os.environ.get("R7_SITE_CODE")
    )
    if env_site_code:
        return env_site_code, "env"

    default_site_code = _clean_str(params.get("default_site_code") or DEFAULT_SITE_CODE)
    if default_site_code:
        return default_site_code, "default"

    raise RuntimeError("R7 siteCode is empty after HTTP login.")


def _resolve_credentials(params: dict) -> Tuple[str, str]:
    username = (
        params.get("username")
        or os.environ.get("R7_CAOZUOCHANG_USER")
        or os.environ.get("R7_USERNAME")
        or os.environ.get("R7_USER")
        or ""
    )
    password = (
        params.get("password")
        or os.environ.get("R7_CAOZUOCHANG_PASS")
        or os.environ.get("R7_PASSWORD")
        or ""
    )
    username = str(username).strip()
    password = str(password).strip()
    if not username or not password:
        raise RuntimeError("Missing R7 credentials. Provide username/password or set R7_CAOZUOCHANG_USER/R7_CAOZUOCHANG_PASS.")
    return username, password


def _resolve_scan_range(params: dict) -> Tuple[str, str]:
    start_value = str(params.get("scan_time_start") or "").strip()
    end_value = str(params.get("scan_time_end") or "").strip()
    if start_value and end_value:
        return start_value, end_value

    target_date_raw = str(params.get("target_date") or "").strip()
    if target_date_raw:
        target_date = datetime.date.fromisoformat(target_date_raw)
    else:
        target_date = datetime.date.today()
    return (
        f"{target_date.strftime('%Y-%m-%d')} 00:00:00",
        f"{target_date.strftime('%Y-%m-%d')} 23:59:59",
    )


def _build_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "aurora-token": token,
        "x-appId": "tms",
        "aurora-back": PRE_ARRIVE_PAGE_URL,
        "Referer": PRE_ARRIVE_PAGE_URL,
    }


def _build_payload(
    *,
    site_code: str,
    start: str,
    end: str,
    page: int,
    page_size: int,
    params: dict,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "currentPage": page,
        "pageSize": page_size,
        "queryCount": True,
        "siteCode": site_code,
        "scanTimeStart": start,
        "scanTimeEnd": end,
        "scanTime": [start, end],
        "showSub": str(params.get("show_sub", "20")),
        "rn": str(params.get("rn", "10")),
    }

    optional_map = {
        "scan_site_code": "scanSiteCode",
        "waybill_no_list": "waybillNoList",
        "handover_number_list": "handoverNumberList",
        "main_waybill_no_list": "mainWaybillNoList",
    }
    for source_key, target_key in optional_map.items():
        value = params.get(source_key)
        if value not in (None, "", []):
            payload[target_key] = value

    if params.get("is_input") not in (None, "", "30"):
        payload["isInput"] = str(params.get("is_input"))
    if params.get("is_sign") not in (None, "", "30"):
        payload["isSign"] = str(params.get("is_sign"))
    if params.get("waybill_type_code_list") not in (None, "", []):
        payload["waybillTypeCodeList"] = params.get("waybill_type_code_list")

    return payload


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    for key in ("data", "rows", "records", "items", "list"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _extract_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("total", "count"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _sanitize_debug_headers(headers: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in headers.items():
        lower_key = str(key).lower()
        if lower_key in {"aurora-token", "authorization", "cookie"}:
            sanitized[str(key)] = "***"
        else:
            sanitized[str(key)] = value
    return sanitized


def _resolve_request_timeout_sec(params: dict[str, Any]) -> int:
    timeout_sec = params.get("request_timeout_sec")
    if timeout_sec in (None, "", 0):
        timeout_ms = params.get("request_timeout_ms")
        if timeout_ms not in (None, "", 0):
            try:
                timeout_sec = max(10, int(timeout_ms) // 1000)
            except (TypeError, ValueError):
                timeout_sec = None
    if timeout_sec in (None, "", 0):
        timeout_sec = 20
    try:
        return max(10, int(timeout_sec))
    except (TypeError, ValueError):
        return 20


def _page_request_json(
    page,
    *,
    url: str,
    method: str,
    payload: dict[str, Any] | None,
    request_headers: dict[str, Any] | None,
    request_timeout_ms: int,
) -> dict[str, Any]:
    script = """
    async ({ url, method, payload, requestHeaders, timeoutMs }) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort("timeout"), timeoutMs);
      try {
        const headers = {
          "Accept": "application/json, text/plain, */*",
        };
        if (requestHeaders && typeof requestHeaders === "object") {
          for (const [key, value] of Object.entries(requestHeaders)) {
            if (!key || value == null) continue;
            const lowerKey = key.toLowerCase();
            if (["content-length", "host", "origin", "referer", "cookie"].includes(lowerKey)) {
              continue;
            }
            if (lowerKey === "content-type") {
              headers["Content-Type"] = value;
              continue;
            }
            if (lowerKey === "accept") {
              headers["Accept"] = value;
              continue;
            }
            headers[key] = value;
          }
        }
        if (payload !== null && payload !== undefined) {
          headers["Content-Type"] = headers["Content-Type"] || "application/json";
        }
        const response = await fetch(url, {
          method,
          credentials: "include",
          headers,
          body: payload === null || payload === undefined ? undefined : JSON.stringify(payload),
          signal: controller.signal,
        });
        const text = await response.text();
        return { ok: response.ok, status: response.status, text };
      } catch (error) {
        return { ok: false, status: 0, text: String(error) };
      } finally {
        clearTimeout(timer);
      }
    }
    """
    return page.evaluate(
        script,
        {
            "url": url,
            "method": method,
            "payload": payload,
            "requestHeaders": request_headers,
            "timeoutMs": request_timeout_ms,
        },
    )


def _click_search_button(page) -> None:
    click_errors: list[str] = []
    strategies = (
        lambda: page.get_by_role("button", name="搜索").click(timeout=5_000),
        lambda: page.locator("button:has-text('搜索')").first.click(timeout=5_000),
        lambda: page.locator("text=搜索").first.click(timeout=5_000),
    )
    for action in strategies:
        try:
            action()
            return
        except Exception as exc:  # pragma: no cover - browser fallback chain
            click_errors.append(str(exc))
    raise RuntimeError("Unable to trigger R7 预到达清单查询: " + " | ".join(click_errors))


def _discover_pre_arrive_request(page, request_timeout_ms: int) -> dict[str, Any]:
    matcher = lambda response: "scanSendData" in response.url and response.request.method.upper() in {"POST", "GET"}
    with page.expect_response(matcher, timeout=request_timeout_ms) as response_info:
        _click_search_button(page)
    response = response_info.value
    request = response.request
    text = response.text()
    try:
        request_headers = request.headers
    except Exception:
        request_headers = {}
    try:
        request_post_data = request.post_data
    except Exception:
        request_post_data = None
    request_post_data_json = None
    if request_post_data:
        try:
            request_post_data_json = json.loads(request_post_data)
        except Exception:
            request_post_data_json = None
    payload_json: dict[str, Any] | None
    try:
        payload_json = json.loads(text)
    except Exception:
        payload_json = None
    return {
        "url": response.url,
        "method": request.method.upper(),
        "status": response.status,
        "raw": text,
        "json": payload_json,
        "request_headers": request_headers,
        "request_post_data": request_post_data,
        "request_post_data_json": request_post_data_json,
    }


def _run_once_http(params: Dict[str, Any]) -> Any:
    params = params or {}
    username, password = _resolve_credentials(params)
    scan_start, scan_end = _resolve_scan_range(params)
    page_size = max(1, min(int(params.get("page_size", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    max_pages = max(1, int(params.get("max_pages", 20) or 20))
    fetch_all = _coerce_bool(params.get("fetch_all"), default=True)
    disable_proxy = _coerce_bool(params.get("disable_proxy"), default=False)
    request_timeout_sec = _resolve_request_timeout_sec(params)

    auth = R7SSOAuth(
        config_path=_clean_str(params.get("config_path")) or None,
        disable_proxy=disable_proxy,
    )
    session = auth.login_and_get_session(
        username=username,
        password=password,
        max_attempts=max(1, int(params.get("max_attempts", 6) or 6)),
        attach_bearer=False,
        exchange=True,
        verify=False,
    )

    token = _clean_str(auth.last_token)
    if not token:
        raise RuntimeError("Missing aurora-token after HTTP login.")

    if not _clean_str(params.get("site_code") or params.get("siteCode")):
        config_site_code = _clean_str(
            auth.config.get("r7_site_code")
            or auth.config.get("site_code")
            or auth.config.get("login_site_code")
        )
        if config_site_code:
            params = {**params, "site_code": config_site_code}

    site_code, site_code_source = _resolve_site_code_http(session, params, timeout_sec=request_timeout_sec)
    current_page = max(1, int(params.get("page", 1) or 1))
    fetched_pages = 0
    rows: list[dict[str, Any]] = []
    total: Optional[int] = None
    headers = _build_headers(token)

    if _coerce_bool(params.get("debug_request"), default=False):
        payload = _build_payload(
            site_code=site_code,
            start=scan_start,
            end=scan_end,
            page=current_page,
            page_size=page_size,
            params=params,
        )
        response = session.post(
            PRE_ARRIVE_API_URL,
            json=payload,
            headers=headers,
            timeout=request_timeout_sec,
        )
        preview = response.text or ""
        preview_json = None
        try:
            preview_json = response.json()
        except Exception:
            preview_json = None
        return {
            "ok": True,
            "debug_request": {
                "mode": "http",
                "url": PRE_ARRIVE_API_URL,
                "method": "POST",
                "site_code": site_code,
                "site_code_source": site_code_source,
                "request_headers": _sanitize_debug_headers(headers),
                "request_post_data_json": payload,
                "response_status": response.status_code,
                "response_preview": preview[:2000],
                "response_rows": len(_extract_rows(preview_json)),
            },
        }

    while True:
        payload = _build_payload(
            site_code=site_code,
            start=scan_start,
            end=scan_end,
            page=current_page,
            page_size=page_size,
            params=params,
        )
        response = session.post(
            PRE_ARRIVE_API_URL,
            json=payload,
            headers=headers,
            timeout=request_timeout_sec,
        )
        if response.status_code != 200:
            return {
                "http_status": response.status_code,
                "error": f"R7 预到达清单接口返回状态码 {response.status_code}",
                "raw": (response.text or "")[:4000],
                "mode": "http",
            }

        try:
            payload_json = response.json()
        except Exception as exc:
            return {
                "error": f"R7 预到达清单返回非 JSON: {exc}",
                "raw": (response.text or "")[:4000],
                "mode": "http",
            }

        page_rows = _extract_rows(payload_json)
        page_total = _extract_total(payload_json)
        if total is None and page_total is not None:
            total = page_total

        rows.extend(page_rows)
        fetched_pages += 1

        if not fetch_all:
            break
        if not page_rows:
            break
        if total is not None and len(rows) >= total:
            break
        if len(page_rows) < page_size:
            break
        if fetched_pages >= max_pages:
            break
        current_page += 1

    return rows


def _run_once_browser(params: Dict[str, Any]) -> Any:
    params = params or {}
    username, password = _resolve_credentials(params)
    scan_start, scan_end = _resolve_scan_range(params)
    page_size = max(1, min(int(params.get("page_size", DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    max_pages = max(1, int(params.get("max_pages", 20) or 20))
    fetch_all = _coerce_bool(params.get("fetch_all"), default=True)
    disable_proxy = _coerce_bool(params.get("disable_proxy"), default=False)

    p = browser = context = page = None
    try:
        p, browser, context, page = launch_browser(
            headless=_coerce_bool(params.get("browser_headless"), default=True),
            slow_mo_ms=max(0, int(params.get("browser_slow_mo_ms", 0) or 0)),
            channel=(str(params.get("browser_channel") or "").strip() or None),
            use_tms_storage_state=False,
        )
        auth = build_auth(max_attempts=max(1, int(params.get("max_attempts", 6) or 6)))
        ensure_logged_in(page, auth, username=username, password=password)

        page.goto(PRE_ARRIVE_PAGE_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

        user_info = page.evaluate(USER_INFO_JS) or {}
        if not isinstance(user_info, dict):
            user_info = {}

        site_code = str(params.get("site_code") or user_info.get("siteCode") or "").strip()
        if not site_code:
            raise RuntimeError("R7 siteCode is empty after login.")

        rows: list[dict[str, Any]] = []
        current_page = max(1, int(params.get("page", 1) or 1))
        fetched_pages = 0
        total: Optional[int] = None
        request_timeout_ms = max(10_000, int(params.get("request_timeout_ms", 60_000) or 60_000))
        discovered = _discover_pre_arrive_request(page, request_timeout_ms)
        actual_url = str(discovered.get("url") or PRE_ARRIVE_API_URL).strip() or PRE_ARRIVE_API_URL
        actual_method = str(discovered.get("method") or "POST").strip().upper() or "POST"
        observed_headers = discovered.get("request_headers") if isinstance(discovered.get("request_headers"), dict) else {}
        observed_payload = (
            discovered.get("request_post_data_json")
            if isinstance(discovered.get("request_post_data_json"), dict)
            else {}
        )
        if int(discovered.get("status") or 0) >= 400:
            return {
                "http_status": int(discovered.get("status") or 0),
                "error": f"R7 预到达清单探测请求失败，状态码 {int(discovered.get('status') or 0)}",
                "raw": discovered.get("raw"),
            }

        if _coerce_bool(params.get("debug_request"), default=False):
            replay_payload = dict(observed_payload) if observed_payload else _build_payload(
                site_code=site_code,
                start=scan_start,
                end=scan_end,
                page=current_page,
                page_size=page_size,
                params=params,
            )
            replay_payload["currentPage"] = current_page
            replay_payload["pageSize"] = page_size
            replay_payload["queryCount"] = True
            replay_payload["scanTimeStart"] = scan_start
            replay_payload["scanTimeEnd"] = scan_end
            replay_payload["scanTime"] = [scan_start, scan_end]
            replay_response = _page_request_json(
                page,
                url=actual_url,
                method=actual_method,
                payload=replay_payload if actual_method != "GET" else None,
                request_headers=observed_headers,
                request_timeout_ms=request_timeout_ms,
            )
            replay_preview = str(replay_response.get("text") or "")
            replay_json = None
            try:
                replay_json = json.loads(replay_preview) if replay_preview else None
            except Exception:
                replay_json = None
            return {
                "ok": True,
                "debug_request": {
                    "url": actual_url,
                    "method": actual_method,
                    "request_headers": _sanitize_debug_headers(observed_headers),
                    "request_post_data": discovered.get("request_post_data"),
                    "request_post_data_json": observed_payload,
                    "response_status": int(discovered.get("status") or 0),
                    "response_preview": str(discovered.get("raw") or "")[:2000],
                    "replay_status": int(replay_response.get("status") or 0),
                    "replay_preview": replay_preview[:2000],
                    "replay_rows": len(_extract_rows(replay_json)),
                },
            }

        while True:
            if observed_payload:
                merged_payload = dict(observed_payload)
                merged_payload["currentPage"] = current_page
                merged_payload["pageSize"] = page_size
                merged_payload["queryCount"] = True
                merged_payload["scanTimeStart"] = scan_start
                merged_payload["scanTimeEnd"] = scan_end
                merged_payload["scanTime"] = [scan_start, scan_end]
                payload = merged_payload
            else:
                payload = _build_payload(
                    site_code=site_code,
                    start=scan_start,
                    end=scan_end,
                    page=current_page,
                    page_size=page_size,
                    params=params,
                )
            response = _page_request_json(
                page,
                url=actual_url,
                method=actual_method,
                payload=payload if actual_method != "GET" else None,
                request_headers=observed_headers,
                request_timeout_ms=request_timeout_ms,
            )
            status = int(response.get("status") or 0)
            if status and status != 200:
                return {
                    "http_status": status,
                    "error": f"R7 预到达清单接口返回状态码 {status}",
                    "raw": response.get("text"),
                }

            raw_text = str(response.get("text") or "")
            try:
                payload_json = json.loads(raw_text)
            except Exception as exc:
                return {"error": f"R7 预到达清单返回非 JSON: {exc}", "raw": raw_text}

            page_rows = _extract_rows(payload_json)
            page_total = _extract_total(payload_json)
            if total is None and page_total is not None:
                total = page_total

            rows.extend(page_rows)
            fetched_pages += 1

            if not fetch_all:
                break
            if not page_rows:
                break
            if total is not None and len(rows) >= total:
                break
            if len(page_rows) < page_size:
                break
            if fetched_pages >= max_pages:
                break
            current_page += 1

        return rows
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


def run_once(params: Dict[str, Any]) -> Any:
    params = params or {}
    browser_only = _coerce_bool(params.get("browser_only"), default=False)
    disable_browser_fallback = _coerce_bool(params.get("disable_browser_fallback"), default=False)

    if browser_only:
        return _run_once_browser(params)

    try:
        return _run_once_http(params)
    except Exception as exc:
        if disable_browser_fallback:
            raise
        try:
            browser_result = _run_once_browser(params)
        except Exception as browser_exc:
            raise RuntimeError(f"{exc}; browser fallback failed: {browser_exc}") from browser_exc
        if isinstance(browser_result, dict) and "http_status" not in browser_result:
            browser_result.setdefault("fallback_reason", str(exc))
            browser_result.setdefault("mode", "browser_fallback")
        return browser_result


if __name__ == "__main__":
    import sys

    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    print(json.dumps(run_once(payload), ensure_ascii=False))
