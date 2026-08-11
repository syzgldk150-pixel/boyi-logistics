"""Shared challenge-login broker for supplier-specific automation profiles."""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.ssl_ import create_urllib3_context
except Exception:  # pragma: no cover - fallback for very old urllib3 builds
    create_urllib3_context = None

from agent.tms_runtime import captcha_ocr, yunda_report
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_models import (
    LoginConfig,
    PendingBrowser,
    format_ts as _format_ts,
    now_ts as _now_ts,
    safe_profile_name as _safe_profile_name,
    status_label as _status_label,
    status_tone as _status_tone,
)
from agent.tms_runtime.session_state import SessionStateStore
from agent.tms_runtime.session_validators import looks_like_ronghui_login


logger = logging.getLogger("agent")

SAVED_PASSWORD_MASK = "********"

BASE_ORIGIN = "https://tms.ronghuiwl.com"
LOGIN_PATH = "/system/login"
HOME_PATH = "/module/index?mv=index"
RONGHUI_SCAN_VALIDATION_PATH = "/dataQuery/findPageByCallId"
RONGHUI_SCAN_VALIDATION_CALL_ID = "FIND_COME_SCAN_RECORD"
RONGHUI_SCAN_VALIDATION_SITE_CODE = "73901"
RONGHUI_SCAN_VALIDATION_SCAN_TYPE = "\u5230\u4ef6"
RONGHUI_MENU_VALIDATION_PATH = "/menuTreeExtend/loadMenu"
YUNDA_BASE_ORIGIN = "https://ky-client.yunda56.com"
YUNDA_LOGIN_PATH = "https://ky-sso.yunda56.com/login"
YUNDA_HOME_PATH = "/client"
YUNDA_CLIENT_HOME_URL = f"{YUNDA_BASE_ORIGIN}/#/"
YUNDA_CLIENT_SYSTEM_HOME_URL = f"{YUNDA_BASE_ORIGIN}/#/systemlink/systemhome"
YUNDA_USER_INFO_URL = f"{YUNDA_BASE_ORIGIN}/client/user/info"
YUNDA_MESSAGE_ORIGIN = "https://ky-message.yunda56.com"
YUNDA_MESSAGE_TYPES_URL = f"{YUNDA_MESSAGE_ORIGIN}/message/api/getTypes"
YUNDA_HEAD_MESSAGE_REFERER = f"{YUNDA_MESSAGE_ORIGIN}/message/view/head_message"
YUNDA_TRACKING_CLIENT_URL = f"{YUNDA_BASE_ORIGIN}/#/ifarme/ifarme/6382/%E5%BF%AB%E4%BB%B6%E8%B7%9F%E8%B8%AA"
YUNDA_INMS_ORIGIN = "https://kyinms.yunda56.com"
YUNDA_INMS_INDEX_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/system/mail/index.html?page=tab&p=nil"
YUNDA_PROBLEM_ORIGIN = "https://kyproblem.yunda56.com"
YUNDA_PROBLEM_QUERY_URL = f"{YUNDA_PROBLEM_ORIGIN}/ky_problem/public/index.php/query/index.html"
YUNDA_PROBLEM_CLIENT_QUERY_URL = f"{YUNDA_BASE_ORIGIN}/#/ifarme/ifarme/4768/%E9%97%AE%E9%A2%98%E4%BB%B6%E6%9F%A5%E8%AF%A2"
YUNDA_PROBLEM_IFRAME_SELECTOR = 'iframe[src*="kyproblem.yunda56.com"]'
PHONE_INPUT = "#phone"
USERNAME_INPUT = "#username"
PASSWORD_INPUT = "#password"
CODE_INPUT = "#validateCode"
SEND_CODE_BUTTON = "#sendCode"
LOGIN_BUTTON = 'a.supplier[onclick="newLogin()"]'
CAPTCHA_IMAGE = "#yzm_img"
YUNDA_LOGIN_FORM = "#login_form"
YUNDA_USERNAME_INPUT = "#username"
YUNDA_PASSWORD_INPUT = "#password"
YUNDA_CAPTCHA_INPUT = "#verify_code"
YUNDA_LOGIN_BUTTON = '#login_form button[type="submit"]'
YUNDA_SMS_PATH = "/public/sms/sms_valid"
YUNDA_SMS_CODE_INPUT = "#sms_code"
YUNDA_SMS_SEND_BUTTON = "#send_code"
YUNDA_SMS_CONFIRM_BUTTON = "button[type='submit']:not(#send_code), button.btn-outline-primary:not(#send_code)"
YUNDA_SMS_PENDING_MESSAGE = "韵达账号触发手机验证，请收到验证码后提交；如未收到，请重新发送或重新登录韵达账号。"
ERROR_BOX = "#showError"
ERROR_TEXT = "#errorSpan"
LOGIN_PAGE_MARKER = "#loinForm"
VALIDATION_TTL_SEC = 60
MAX_AUTO_CAPTCHA_ATTEMPTS = 3
YUNDA_REPORT_VALIDATION_ATTEMPTS = 2
YUNDA_REPORT_VALIDATION_RETRY_DELAY_SEC = 1.0


class _RonghuiTLSAdapter(HTTPAdapter):
    """Relax OpenSSL security level for Ronghui TMS legacy TLS negotiation."""

    def _ssl_context(self):
        if create_urllib3_context is None:
            return None
        return create_urllib3_context(ciphers="DEFAULT:@SECLEVEL=1")

    def init_poolmanager(self, *args, **kwargs):
        ssl_context = self._ssl_context()
        if ssl_context is not None:
            kwargs["ssl_context"] = ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        ssl_context = self._ssl_context()
        if ssl_context is not None:
            kwargs["ssl_context"] = ssl_context
        return super().proxy_manager_for(*args, **kwargs)


DEFAULT_USERNAME_ENVS = ("TMS_LOGIN_USERNAME", "TMS_OPERATOR_UID", "TMS_CAOZUOUSERNAME")
DEFAULT_PASSWORD_ENVS = ("TMS_LOGIN_PASSWORD", "TMS_OPERATOR_PASSWORD", "TMS_CAOZUOPASSWORD")
DEFAULT_PHONE_ENVS = ("TMS_LOGIN_PHONE", "TMS_OPERATOR_PHONE", "TMS_SMS_PHONE")
PRICE_USERNAME_ENVS = ("TMS_PRICE_USERNAME", "TMS_DAXIANGUSERNAME")
PRICE_PASSWORD_ENVS = ("TMS_PRICE_PASSWORD", "TMS_DAXIANGPASSWORD")
PRICE_PHONE_ENVS = ("TMS_PRICE_PHONE", "TMS_DAXIANGPHONE", "TMS_DAXIANGMOBILE")
YUNDA_USERNAME_ENVS = ("YUNDA_REPORT_USERNAME", "YUNDA_USERNAME", "KY_CLIENT_USERNAME")
YUNDA_PASSWORD_ENVS = ("YUNDA_REPORT_PASSWORD", "YUNDA_PASSWORD", "KY_CLIENT_PASSWORD")
YUNDA_PHONE_ENVS = ("YUNDA_REPORT_PHONE", "YUNDA_PHONE", "KY_CLIENT_PHONE")
YUNDA_BASE_ORIGIN_ENVS = ("YUNDA_REPORT_BASE_ORIGIN", "YUNDA_BASE_ORIGIN", "KY_CLIENT_BASE_ORIGIN")
YUNDA_LOGIN_PATH_ENVS = ("YUNDA_REPORT_LOGIN_PATH", "YUNDA_LOGIN_PATH", "KY_CLIENT_LOGIN_PATH")
YUNDA_HOME_PATH_ENVS = ("YUNDA_REPORT_HOME_PATH", "YUNDA_HOME_PATH", "KY_CLIENT_HOME_PATH")


def _resolve_chromium_executable() -> str:
    for env_name in ("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "CHROMIUM_EXECUTABLE", "CHROME_BIN"):
        value = str(os.getenv(env_name) or "").strip()
        if value and os.path.exists(value):
            return value

    for candidate in (
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
    ):
        if os.path.exists(candidate):
            return candidate

    for command in ("chromium-browser", "chromium", "google-chrome-stable", "google-chrome"):
        resolved = shutil.which(command)
        if resolved:
            return resolved

    return ""


def _chromium_launch_kwargs() -> dict[str, Any]:
    options: dict[str, Any] = {"headless": True, "args": ["--no-sandbox"]}
    executable = _resolve_chromium_executable()
    if executable:
        options["executable_path"] = executable
    return options


def _env_first(names: tuple[str, ...]) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _join_origin_path(base_origin: str, path_or_url: str) -> str:
    value = str(path_or_url or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    return urljoin(base_origin.rstrip("/") + "/", value.lstrip("/"))



__all__ = [name for name in globals() if not name.startswith("__")]
