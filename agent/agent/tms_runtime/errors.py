"""Shared error types for the embedded TMS runtime."""

from __future__ import annotations

import subprocess

import requests


class TMSAuthStateError(RuntimeError):
    """Raised when the shared TMS login state is not ready for use."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code or "AUTH_REQUIRED").strip() or "AUTH_REQUIRED"


_LOGIN_RUNTIME_ERRORS = {
    "LOGIN_TIMEOUT": "登录操作超时。",
    "LOGIN_PAGE_UNAVAILABLE": "登录页面暂时不可用。",
    "LOGIN_WORKER_UNAVAILABLE": "登录进程异常。",
    "LOGIN_NETWORK_UNAVAILABLE": "登录服务暂时无法连接。",
    "AUTH_UNAVAILABLE": "登录运行环境不可用，请检查浏览器依赖或服务状态。",
}


def login_runtime_error(exc: BaseException) -> TMSAuthStateError | None:
    """Recognize infrastructure failures before provider wrappers hide their cause.

    Only exception types/codes are inspected. Browser error text can contain
    filled values or session URLs and must not be copied into public status.
    """
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        code = getattr(cause, "code", "")
        if code in _LOGIN_RUNTIME_ERRORS:
            return TMSAuthStateError(code, _LOGIN_RUNTIME_ERRORS[code])
        if isinstance(cause, (TimeoutError, subprocess.TimeoutExpired)):
            return TMSAuthStateError("LOGIN_TIMEOUT", _LOGIN_RUNTIME_ERRORS["LOGIN_TIMEOUT"])
        if isinstance(cause, (requests.Timeout, requests.ConnectionError)):
            return TMSAuthStateError("LOGIN_NETWORK_UNAVAILABLE", _LOGIN_RUNTIME_ERRORS["LOGIN_NETWORK_UNAVAILABLE"])
        if isinstance(cause, requests.HTTPError):
            response = cause.response
            if response is not None and (response.status_code == 429 or response.status_code >= 500):
                return TMSAuthStateError("LOGIN_NETWORK_UNAVAILABLE", _LOGIN_RUNTIME_ERRORS["LOGIN_NETWORK_UNAVAILABLE"])
        if type(cause).__module__.startswith("playwright."):
            return TMSAuthStateError("LOGIN_PAGE_UNAVAILABLE", _LOGIN_RUNTIME_ERRORS["LOGIN_PAGE_UNAVAILABLE"])
        cause = cause.__cause__
    return None


def auth_error_payload(exc: TMSAuthStateError) -> dict:
    return {
        "ok": False,
        "error_code": exc.code,
        "error": str(exc),
    }
