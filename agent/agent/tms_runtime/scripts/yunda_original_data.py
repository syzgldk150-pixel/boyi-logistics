"""Shared Yunda original contact-detail fetcher."""

from __future__ import annotations

import re
from typing import Any

from agent.tms_runtime.errors import TMSAuthStateError


YUNDA_INMS_ORIGIN = "https://kyinms.yunda56.com"
MAIL_INDEX_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/system/mail/index.html"
ORIGINAL_DATA_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/system/mail/getOriginalData.html"


def _coerce_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _auth_if_login_response(response: Any, body: str) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {301, 302, 401, 403}:
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录韵达账号。")
    content_type = str(getattr(response, "headers", {}).get("content-type") or "").lower()
    lower_body = body.lower()
    response_url = str(getattr(response, "url", "") or "").lower()
    location = str(getattr(response, "headers", {}).get("location") or "").lower()
    login_url = any(marker in response_url or marker in location for marker in ("ky-sso", "/login", "login.html"))
    password_form = any(
        marker in lower_body
        for marker in ('type="password"', "type='password'", 'name="password"', "name='password'")
    )
    sso_redirect = re.search(
        r"(?:window\.)?location(?:\.href)?\s*=\s*['\"][^'\"]*(?:ky-sso|sso\.yunda56\.com)[^'\"]*(?:login|passport)",
        lower_body,
    ) is not None
    explicit_login_page = "login-form" in lower_body or "loginform" in lower_body
    if "text/html" in content_type and (login_url or password_form or sso_redirect or explicit_login_page):
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录韵达账号。")
    if status_code == 200 and not body.strip():
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达接口返回空响应，请重新登录韵达账号。")


def _decode_original_response(response: Any) -> dict[str, Any]:
    body = str(getattr(response, "text", "") or "")
    _auth_if_login_response(response, body)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def fetch_yunda_original_data(session: Any, bill_code: str, params: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": YUNDA_INMS_ORIGIN,
        "Referer": f"{MAIL_INDEX_URL}?state={bill_code}&all=1",
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        ORIGINAL_DATA_URL,
        data={"Logistics_Id": bill_code},
        headers=headers,
        allow_redirects=False,
        timeout=_coerce_int(params.get("request_timeout_sec"), 30),
    )
    return _decode_original_response(response)
