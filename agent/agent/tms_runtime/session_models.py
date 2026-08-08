"""Pure models and display helpers shared by TMS session adapters."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingBrowser:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    created_at: float


@dataclass(frozen=True)
class LoginConfig:
    base_origin: str
    login_url: str
    home_url: str
    username: str
    password: str
    phone: str


def now_ts() -> float:
    return time.time()


def format_ts(value: float | None) -> str:
    if not value:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def status_label(status: str) -> str:
    return {
        "authenticated": "已登录",
        "pending_code": "待输入验证码",
        "logged_out": "未登录",
        "expired": "已过期",
        "error": "异常",
    }.get(status, "未知")


def status_tone(status: str) -> str:
    return {
        "authenticated": "success",
        "pending_code": "warning",
        "logged_out": "neutral",
        "expired": "error",
        "error": "error",
    }.get(status, "neutral")


def safe_profile_name(profile_name: str) -> str:
    normalized = str(profile_name or "default").strip().lower()
    keep = []
    for char in normalized:
        keep.append(char if char.isalnum() or char in {"_", "-"} else "_")
    return "".join(keep).strip("_") or "default"
