import json
import asyncio
import base64
import importlib
import inspect
import os
import sys
import tempfile
import types
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "agent" / "tms_runtime" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_pre_arrive_list
import fetch_dispatch
import yunda_dispatch_forecast
import yunda_price
import yunda_waybill_entry
import yunda_waybill_tracking
import yunda_send_waybills
import Send_order
import auto_departure_r7
import browser_manager
import query_waybill_detail as tms_query_waybill_detail
import tracking_query
import r7_login
import waybill_tracking
import get_qianshou

from agent import automation_profile, direct_tool_router, pending_actions
from agent.core import AgentCore
from agent.tms_runtime import account_manager as account_manager_module
from agent.tms_runtime import dispatch as dispatch_module
from agent.tms_runtime import routes as routes_module
from agent.tms_runtime.routes import router
from agent.tms_runtime.session_broker import (
    CAPTCHA_IMAGE,
    CODE_INPUT,
    HOME_PATH,
    LOGIN_BUTTON,
    LOGIN_PATH,
    LoginConfig,
    SAVED_PASSWORD_MASK,
    SessionBroker,
    get_session_broker,
)
from agent.tms_runtime import session_broker as session_broker_module
from feishu import bot as feishu_bot
from feishu import message_handler
from tools import (
    arrive_list_sync_tool,
    arrival_stats_sync_tool,
    automation_profile_tool,
    daily_sign_sync_tool,
    delivery_status_sync_tool,
    phase7_mysql_store,
    phase7_sync_common,
    init_waybills_sql_from_feishu_tool,
    price_tool,
    query_tool,
    r7_arrival_checkin_tool,
    r7_departure_checkin_tool,
    scan_sync_tool,
    send_order_sync_tool,
    feishu_cli_tool,
    site_send_list_sync_tool,
    tms_tool,
    track_waybill_tool,
    yunda_dispatch_forecast_sync_tool,
    yunda_send_waybills_sync_tool,
)


def _admin_accounts_payload(*, pending_accounts: set[str] | None = None) -> dict[str, Any]:
    pending_accounts = pending_accounts or set()
    accounts = [
        ("ronghui_default", "TMS融辉默认账号", "ronghui", "TMS融辉", "general", "普通TMS账号", "default", True),
        ("price_default", "大祥报价账号", "ronghui", "TMS融辉", "price", "大祥报价", "price", True),
        ("yunda_default", "韵达默认账号", "yunda", "韵达", "general", "普通账号", "yunda", True),
    ]
    return {
        "ok": True,
        "accounts": [
            {
                "account_id": account_id,
                "name": name,
                "system": system,
                "system_label": system_label,
                "account_purpose": account_purpose,
                "account_purpose_label": account_purpose_label,
                "session_profile": session_profile,
                "session_capable": True,
                "is_active": True,
                "is_default": is_default,
                "status": {
                    "status": "pending_code" if account_id in pending_accounts else "logged_out",
                    "pending_code": account_id in pending_accounts,
                },
            }
            for (
                account_id,
                name,
                system,
                system_label,
                account_purpose,
                account_purpose_label,
                session_profile,
                is_default,
            ) in accounts
        ],
    }


class _FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self, timeout=None):
        return True

    def wait_for(self, state=None, timeout=None):
        return None

    def screenshot(self, timeout=None):
        return b"captcha"

    def fill(self, value):
        self.page.filled[self.selector] = value

    def click(self):
        self.page.clicked.append(self.selector)
        if self.selector == LOGIN_BUTTON:
            self.page.url = self.page.after_login_url


class _FakePage:
    def __init__(self, login_url, after_login_url):
        self.url = login_url
        self.after_login_url = after_login_url
        self.filled = {}
        self.clicked = []

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def wait_for_timeout(self, ms):
        return None

    def wait_for_load_state(self, state, timeout=None):
        return None

    def evaluate(self, js_code):
        return ""


class _FakeConditionalLocator(_FakeLocator):
    def count(self):
        return 1 if self.selector in self.page.visible_selectors else 0

    def is_visible(self, timeout=None):
        return self.count() > 0

    def wait_for(self, state=None, timeout=None):
        if self.count() <= 0:
            raise RuntimeError(f"missing selector: {self.selector}")
        return None


class _FakeImageCaptchaPage(_FakePage):
    visible_selectors = {"#username", "#password", CODE_INPUT, CAPTCHA_IMAGE, LOGIN_BUTTON}

    def locator(self, selector):
        return _FakeConditionalLocator(self, selector)


class _FakeYundaLocator(_FakeLocator):
    def click(self):
        self.page.clicked.append(self.selector)


class _FakeYundaCaptchaPage(_FakePage):
    def locator(self, selector):
        return _FakeYundaLocator(self, selector)


class _FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def storage_state(self, path=None):
        payload = {"cookies": [{"name": "sid", "value": "cookie", "domain": "tms.ronghuiwl.com", "path": "/"}], "origins": []}
        if path:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

    def close(self):
        return None


class _FakeBrowser:
    def __init__(self, context):
        self._context = context

    def new_context(self, viewport=None, storage_state=None):
        return self._context

    def close(self):
        return None


class _FakePlaywrightManager:
    def __init__(self, browser):
        self.chromium = types.SimpleNamespace(launch=lambda **kwargs: browser)

    def stop(self):
        return None


class _FakeSyncPlaywright:
    def __init__(self, manager):
        self.manager = manager

    def start(self):
        return self.manager


class _DummyResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text or "{}")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")


class _DummySession:
    def __init__(self, response, post_response=None, menu_response=None):
        self.response = response
        self.post_response = post_response or _DummyResponse(status_code=200, text='{"data":[]}', headers={"Content-Type": "application/json"})
        self.menu_response = menu_response or _DummyResponse(
            status_code=200,
            text='{"success":true,"result":{"data":[]}}',
            headers={"Content-Type": "application/json"},
        )
        self.cookies = {}
        self.get_calls = []
        self.post_calls = []

    def get(self, url, allow_redirects=False, timeout=15, headers=None):
        self.get_calls.append({"url": url, "allow_redirects": allow_redirects, "timeout": timeout, "headers": headers})
        if "/menuTreeExtend/loadMenu" in str(url):
            return self.menu_response
        return self.response

    def post(self, url, params=None, data=None, headers=None, allow_redirects=False, timeout=15):
        self.post_calls.append(
            {
                "url": url,
                "params": params,
                "data": data,
                "headers": headers,
                "allow_redirects": allow_redirects,
                "timeout": timeout,
            }
        )
        return self.post_response


class _SimpleCookie:
    def __init__(
        self,
        name="sid",
        value="cookie",
        domain="tms.ronghuiwl.com",
        path="/",
        secure=False,
        expires=None,
        rest=None,
    ):
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.secure = secure
        self.expires = expires
        self._rest = rest or {}


class _CaptchaPostSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.post_calls = []
        self.cookies = [_SimpleCookie()]

    def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
        self.post_calls.append(
            {
                "url": url,
                "data": data,
                "headers": headers,
                "allow_redirects": allow_redirects,
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError("unexpected captcha login post")
        return self._responses.pop(0)


class _FakeLoginLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def fill(self, value):
        self.page.filled[self.selector] = value

    def click(self):
        self.page.clicked.append(self.selector)
        self.page.url = self.page.home_url


class _FakeIndependentLoginPage:
    def __init__(self, login_url, home_url):
        self.login_url = login_url
        self.home_url = home_url
        self.url = ""
        self.filled = {}
        self.clicked = []

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def wait_for_load_state(self, state, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        return None

    def wait_for_selector(self, selector, state=None, timeout=None):
        return _FakeLoginLocator(self, selector)

    def content(self):
        return "<html></html>"

    def is_visible(self, selector):
        return self.url == self.login_url


class _FakeR7Cookie:
    name = "r7-session"
    value = "cookie-value"
    domain = "r7.ronghuiwl.com"
    path = "/"
    secure = True
    expires = None
    _rest = {"HttpOnly": True, "SameSite": "Lax"}


class _FakeR7Session:
    def __init__(self):
        self.cookies = [_FakeR7Cookie()]


class _FakeR7HttpLoginContext:
    def __init__(self):
        self.cookies = []

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)


class _FakeR7HttpLoginPage(_FakeIndependentLoginPage):
    def __init__(self, login_url, home_url):
        super().__init__(login_url, home_url)
        self.context = _FakeR7HttpLoginContext()
        self.evaluated = []

    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        return None


class _FakeLaunchContext:
    def __init__(self):
        self.kwargs = None

    def new_page(self):
        return object()


class _FakeLaunchBrowser:
    def __init__(self, context):
        self.context = context
        self.launch_kwargs = None

    def new_context(self, **kwargs):
        self.context.kwargs = kwargs
        return self.context


class _FakeLaunchChromium:
    def __init__(self, browser):
        self.browser = browser

    def launch(self, **kwargs):
        self.browser.launch_kwargs = kwargs
        return self.browser


class _FakeLaunchPlaywright:
    def __init__(self, browser):
        self.chromium = _FakeLaunchChromium(browser)

    def start(self):
        return self



__all__ = [name for name in globals() if not name.startswith("__")]
