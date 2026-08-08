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
from unittest.mock import patch

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
    PRICE_PASSWORD_ENVS,
    PRICE_PHONE_ENVS,
    PRICE_USERNAME_ENVS,
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


class SessionBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.broker = SessionBroker()
        self._configure_broker_state(self.broker)
        self.login_config = LoginConfig(
            base_origin="https://tms.ronghuiwl.com",
            login_url="https://tms.ronghuiwl.com/system/login",
            home_url="https://tms.ronghuiwl.com/module/index?mv=index",
            username="demo-user",
            password="demo-pass",
            phone="13800000000",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _configure_broker_state(self, broker, subdir: str = ""):
        state_dir = Path(self.tempdir.name) / subdir if subdir else Path(self.tempdir.name)
        state_dir.mkdir(parents=True, exist_ok=True)
        broker._state_dir = state_dir
        broker._meta_path = state_dir / "session_meta.json"
        broker._storage_state_path = state_dir / "storage_state.json"
        broker._cookies_path = state_dir / "cookies.json"
        broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        broker._pending_login_state_path = state_dir / "pending_login_state.json"
        broker._login_profile_path = state_dir / "login_profile.json"
        return broker

    @staticmethod
    def _authenticated_meta():
        return {
            "status": "authenticated",
            "last_validation_at": "",
            "last_error_summary": "",
            "authenticated_at": "2026-04-22 12:00:00",
            "pending_since": "",
            "expires_at": "",
        }

    def _playwright_patch(self, page=None):
        page = page or _FakePage(self.login_config.login_url, self.login_config.home_url)
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        manager = _FakePlaywrightManager(browser)
        sync_api_module = types.ModuleType("playwright.sync_api")
        sync_api_module.sync_playwright = lambda: _FakeSyncPlaywright(manager)
        playwright_module = types.ModuleType("playwright")
        playwright_module.sync_api = sync_api_module
        return patch.dict(
            sys.modules,
            {
                "playwright": playwright_module,
                "playwright.sync_api": sync_api_module,
            },
        )

    def test_send_code_transitions_to_pending_code(self):
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_read_login_error", return_value=""),
            self._playwright_patch(),
        ):
            result = self.broker.send_code()

        self.assertEqual(result["status"], "pending_code")
        self.assertTrue(self.broker._pending_storage_state_path.exists())
        self.assertTrue(self.broker.describe_status(validate=False)["pending_code"])

    def test_send_code_handles_ronghui_image_captcha_page(self):
        page = _FakeImageCaptchaPage(self.login_config.login_url, self.login_config.home_url)
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            self._playwright_patch(page),
        ):
            result = self.broker.send_code()

        self.assertEqual(result["status"], "pending_code")
        self.assertEqual(result["challenge_type"], "image")
        self.assertEqual(result["challenge_label"], "图片验证码")
        self.assertTrue(result["captcha_image"].startswith("data:image/png;base64,"))
        self.assertTrue(self.broker._pending_storage_state_path.exists())
        self.assertTrue(self.broker._pending_login_state_path.exists())
        status = self.broker.describe_status(validate=False)
        self.assertEqual(status["challenge_type"], "image")
        self.assertTrue(status["captcha_image"].startswith("data:image/png;base64,"))

    def test_send_code_returns_auth_unavailable_when_playwright_missing(self):
        with patch.object(self.broker, "resolve_login_config", return_value=self.login_config):
            with patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
                with self.assertRaises(Exception) as ctx:
                    self.broker.send_code()

        self.assertEqual(getattr(ctx.exception, "code", ""), "AUTH_UNAVAILABLE")
        self.assertIn("playwright install chromium", str(ctx.exception))

    def test_submit_code_runs_sync_playwright_outside_async_loop(self):
        page = _FakePage(self.login_config.login_url, self.login_config.home_url)
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        manager = _FakePlaywrightManager(browser)

        class _LoopGuardSyncPlaywright:
            def start(self):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    return manager
                raise RuntimeError("sync playwright started inside async loop")

        sync_api_module = types.ModuleType("playwright.sync_api")
        sync_api_module.sync_playwright = lambda: _LoopGuardSyncPlaywright()
        playwright_module = types.ModuleType("playwright")
        playwright_module.sync_api = sync_api_module
        self.broker._pending_storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}),
            encoding="utf-8",
        )

        with (
            patch.dict(sys.modules, {"playwright": playwright_module, "playwright.sync_api": sync_api_module}),
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_persist_storage_state_locked", return_value=self._authenticated_meta()),
        ):
            result = asyncio.run(self._submit_code_from_async_loop("123456"))

        self.assertEqual(result["status"], "authenticated")

    async def _submit_code_from_async_loop(self, code: str):
        return self.broker.submit_code(code)

    def test_send_code_handles_ronghui_image_captcha_page(self):
        page = _FakeImageCaptchaPage(self.login_config.login_url, self.login_config.home_url)
        session = _CaptchaPostSession([
            _DummyResponse(status_code=302, headers={"Location": self.login_config.home_url}),
        ])
        authenticated_meta = self._authenticated_meta()
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_requests_session_from_storage_state_payload", return_value=session),
            patch.object(
                self.broker,
                "_fetch_ronghui_captcha_challenge",
                return_value=(b"captcha-1", "data:image/png;base64,Y2FwdGNoYS0x", "image/png"),
            ),
            patch.object(session_broker_module.captcha_ocr, "classify_captcha_image", return_value="ab12"),
            patch.object(self.broker, "_persist_requests_session_locked", return_value=authenticated_meta) as persist_mock,
            self._playwright_patch(page),
        ):
            result = self.broker.send_code()

        self.assertEqual(result["status"], "authenticated")
        persist_mock.assert_called_once_with(session, self.login_config)
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.post_calls[0]["data"]["validateCode"], "ab12")
        self.assertTrue(self.broker._pending_storage_state_path.exists())
        self.assertFalse(self.broker._pending_login_state_path.exists())

    def test_auto_login_ronghui_retries_after_empty_ocr_and_then_succeeds(self):
        session = _CaptchaPostSession([
            _DummyResponse(status_code=302, headers={"Location": self.login_config.home_url}),
        ])
        authenticated_meta = self._authenticated_meta()
        fetch_side_effect = [
            (b"captcha-1", "data:image/png;base64,Y2FwdGNoYS0x", "image/png"),
            (b"captcha-2", "data:image/png;base64,Y2FwdGNoYS0y", "image/png"),
        ]
        with (
            patch.object(self.broker, "_fetch_ronghui_captcha_challenge", side_effect=fetch_side_effect),
            patch.object(session_broker_module.captcha_ocr, "classify_captcha_image", side_effect=["", "ab12"]),
            patch.object(self.broker, "_persist_requests_session_locked", return_value=authenticated_meta) as persist_mock,
        ):
            result = self.broker._auto_login_ronghui_image_captcha(session, self.login_config)

        self.assertEqual(result["status"], "authenticated")
        persist_mock.assert_called_once_with(session, self.login_config)
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.post_calls[0]["data"]["validateCode"], "ab12")

    def test_auto_login_ronghui_falls_back_to_manual_after_four_failed_attempts(self):
        session = _CaptchaPostSession([
            _DummyResponse(status_code=200, text="validateCode system/login", headers={"Content-Type": "text/html"})
            for _ in range(4)
        ])
        fetch_side_effect = [
            (f"captcha-{idx}".encode("utf-8"), f"data:image/png;base64,Y2FwdGNoYS0{idx}", "image/png")
            for idx in range(1, 5)
        ]
        with (
            patch.object(self.broker, "_fetch_ronghui_captcha_challenge", side_effect=fetch_side_effect),
            patch.object(session_broker_module.captcha_ocr, "classify_captcha_image", return_value="ab12"),
        ):
            result = self.broker._auto_login_ronghui_image_captcha(session, self.login_config)

        self.assertEqual(result["status"], "pending_code")
        self.assertEqual(result["challenge_type"], "image")
        self.assertTrue(result["captcha_image"].startswith("data:image/png;base64,"))
        self.assertTrue(result["last_error_summary"])
        self.assertIn("4", result["last_error_summary"])
        self.assertTrue(self.broker._pending_storage_state_path.exists())
        self.assertTrue(self.broker._pending_login_state_path.exists())
        self.assertEqual(len(session.post_calls), 4)

    def test_price_profile_send_code_reuses_auto_image_login_flow(self):
        price_broker = self._configure_broker_state(
            SessionBroker(
                profile_name="price",
                username_envs=PRICE_USERNAME_ENVS,
                password_envs=PRICE_PASSWORD_ENVS,
                phone_envs=PRICE_PHONE_ENVS,
            ),
            "price",
        )
        price_config = LoginConfig(
            base_origin="https://tms.ronghuiwl.com",
            login_url="https://tms.ronghuiwl.com/system/login",
            home_url="https://tms.ronghuiwl.com/module/index?mv=index",
            username="price-user",
            password="price-pass",
            phone="",
        )
        page = _FakeImageCaptchaPage(price_config.login_url, price_config.home_url)
        session = _CaptchaPostSession([
            _DummyResponse(status_code=302, headers={"Location": price_config.home_url}),
        ])
        authenticated_meta = self._authenticated_meta()
        with (
            patch.object(price_broker, "resolve_login_config", return_value=price_config),
            patch.object(price_broker, "_requests_session_from_storage_state_payload", return_value=session),
            patch.object(
                price_broker,
                "_fetch_ronghui_captcha_challenge",
                return_value=(b"captcha-price", "data:image/png;base64,Y2FwdGNoYS1wcmljZQ==", "image/png"),
            ),
            patch.object(session_broker_module.captcha_ocr, "classify_captcha_image", return_value="pq12"),
            patch.object(price_broker, "_persist_requests_session_locked", return_value=authenticated_meta),
            self._playwright_patch(page),
        ):
            result = price_broker.send_code()

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.post_calls[0]["data"]["validateCode"], "pq12")

    def test_yunda_image_captcha_auto_login_succeeds_with_ocr(self):
        yunda_config = LoginConfig(
            base_origin="https://ky-sso.yunda56.com",
            login_url="https://ky-sso.yunda56.com/login",
            home_url="https://ky-sso.yunda56.com/home",
            username="yunda-user",
            password="yunda-pass",
            phone="13800000000",
        )
        page = _FakeYundaCaptchaPage(yunda_config.login_url, yunda_config.home_url)
        context = _FakeContext(page)
        authenticated_meta = self._authenticated_meta()

        with (
            patch.object(
                self.broker,
                "_capture_yunda_captcha_image_payload",
                return_value=(b"captcha-yunda", "data:image/png;base64,eXVuZGE=", "image/png"),
            ),
            patch.object(session_broker_module.captcha_ocr, "classify_captcha_image", return_value="yd12"),
            patch.object(self.broker, "_read_yunda_login_error", return_value=""),
            patch.object(self.broker, "_is_yunda_sms_page", return_value=False),
            patch.object(self.broker, "_is_yunda_captcha_visible", return_value=False),
            patch.object(self.broker, "_is_yunda_login_page", return_value=False),
            patch.object(self.broker, "_persist_storage_state_locked", return_value=authenticated_meta) as persist_mock,
            patch.object(self.broker, "_close_pending_locked") as close_pending,
        ):
            result = self.broker._auto_login_yunda_image_captcha(context, page, config=yunda_config)

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(page.filled[session_broker_module.YUNDA_USERNAME_INPUT], "yunda-user")
        self.assertEqual(page.filled[session_broker_module.YUNDA_PASSWORD_INPUT], "yunda-pass")
        self.assertEqual(page.filled[session_broker_module.YUNDA_CAPTCHA_INPUT], "yd12")
        self.assertEqual([session_broker_module.YUNDA_LOGIN_BUTTON], page.clicked)
        persist_mock.assert_called_once_with(context, page)
        close_pending.assert_called_once()

    def test_yunda_image_captcha_auto_login_retries_empty_ocr_and_then_succeeds(self):
        yunda_config = LoginConfig(
            base_origin="https://ky-sso.yunda56.com",
            login_url="https://ky-sso.yunda56.com/login",
            home_url="https://ky-sso.yunda56.com/home",
            username="yunda-user",
            password="yunda-pass",
            phone="13800000000",
        )
        page = _FakeYundaCaptchaPage(yunda_config.login_url, yunda_config.home_url)
        context = _FakeContext(page)
        authenticated_meta = self._authenticated_meta()

        with (
            patch.object(
                self.broker,
                "_capture_yunda_captcha_image_payload",
                return_value=(b"captcha-yunda", "data:image/png;base64,eXVuZGE=", "image/png"),
            ) as capture_mock,
            patch.object(session_broker_module.captcha_ocr, "classify_captcha_image", side_effect=["", "yd12"]),
            patch.object(self.broker, "_read_yunda_login_error", return_value=""),
            patch.object(self.broker, "_is_yunda_sms_page", return_value=False),
            patch.object(self.broker, "_is_yunda_captcha_visible", return_value=False),
            patch.object(self.broker, "_is_yunda_login_page", return_value=False),
            patch.object(self.broker, "_persist_storage_state_locked", return_value=authenticated_meta),
            patch.object(self.broker, "_close_pending_locked"),
        ):
            result = self.broker._auto_login_yunda_image_captcha(context, page, config=yunda_config)

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(2, capture_mock.call_count)
        self.assertEqual(1, len(page.clicked))
        self.assertEqual(page.filled[session_broker_module.YUNDA_CAPTCHA_INPUT], "yd12")

    def test_yunda_image_captcha_auto_login_falls_back_after_four_failures(self):
        yunda_config = LoginConfig(
            base_origin="https://ky-sso.yunda56.com",
            login_url="https://ky-sso.yunda56.com/login",
            home_url="https://ky-sso.yunda56.com/home",
            username="yunda-user",
            password="yunda-pass",
            phone="13800000000",
        )
        page = _FakeYundaCaptchaPage(yunda_config.login_url, yunda_config.home_url)
        context = _FakeContext(page)
        image_payloads = [
            (f"captcha-{idx}".encode("utf-8"), f"data:image/png;base64,eXVuZGE{idx}", "image/png")
            for idx in range(1, session_broker_module.MAX_AUTO_CAPTCHA_ATTEMPTS + 2)
        ]

        with (
            patch.object(self.broker, "_capture_yunda_captcha_image_payload", side_effect=image_payloads),
            patch.object(session_broker_module.captcha_ocr, "classify_captcha_image", return_value="yd12"),
            patch.object(self.broker, "_read_yunda_login_error", return_value=""),
            patch.object(self.broker, "_is_yunda_sms_page", return_value=False),
            patch.object(self.broker, "_is_yunda_captcha_visible", return_value=True),
            patch.object(self.broker, "_is_yunda_login_page", return_value=True),
        ):
            result = self.broker._auto_login_yunda_image_captcha(context, page, config=yunda_config)

        self.assertEqual(result["status"], "pending_code")
        self.assertEqual(result["challenge_type"], "image")
        self.assertIn("4", result["last_error_summary"])
        self.assertTrue(self.broker._pending_storage_state_path.exists())
        self.assertTrue(self.broker._pending_login_state_path.exists())
        self.assertEqual(session_broker_module.MAX_AUTO_CAPTCHA_ATTEMPTS, len(page.clicked))

    def test_yunda_image_captcha_auto_login_returns_sms_pending_when_sms_page_appears(self):
        yunda_config = LoginConfig(
            base_origin="https://ky-sso.yunda56.com",
            login_url="https://ky-sso.yunda56.com/login",
            home_url="https://ky-sso.yunda56.com/home",
            username="yunda-user",
            password="yunda-pass",
            phone="13800000000",
        )
        page = _FakeYundaCaptchaPage(yunda_config.login_url, yunda_config.home_url)
        context = _FakeContext(page)

        with (
            patch.object(
                self.broker,
                "_capture_yunda_captcha_image_payload",
                return_value=(b"captcha-yunda", "data:image/png;base64,eXVuZGE=", "image/png"),
            ),
            patch.object(session_broker_module.captcha_ocr, "classify_captcha_image", return_value="yd12"),
            patch.object(self.broker, "_read_yunda_login_error", return_value=""),
            patch.object(self.broker, "_is_yunda_sms_page", return_value=True),
            patch.object(self.broker, "_read_yunda_sms_error", return_value=""),
            patch.object(
                self.broker,
                "_save_yunda_sms_pending_state_locked",
                return_value={"status": "pending_code", "challenge_type": "sms"},
            ) as save_sms,
        ):
            result = self.broker._auto_login_yunda_image_captcha(context, page, config=yunda_config)

        self.assertEqual(result["status"], "pending_code")
        self.assertEqual(result["challenge_type"], "sms")
        save_sms.assert_called_once()

    def test_submit_code_transitions_to_authenticated(self):
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_read_login_error", return_value=""),
            self._playwright_patch(),
        ):
            self.broker.send_code()

        authenticated_meta = self.broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-04-22 12:00:00",
                "pending_since": "",
                "expires_at": "2026-04-23 12:00:00",
            }
        )
        def persist_and_assert(context, page):
            self.assertFalse(self.broker._pending_storage_state_path.exists())
            return authenticated_meta

        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_read_login_error", return_value=""),
            patch.object(self.broker, "_persist_storage_state_locked", side_effect=persist_and_assert),
            self._playwright_patch(),
        ):
            result = self.broker.submit_code("123456")

        self.assertEqual(result["status"], "authenticated")
        self.assertFalse(self.broker._pending_storage_state_path.exists())
        self.assertTrue(self.broker.describe_status(validate=False)["authenticated"])

    def test_submit_code_posts_ronghui_image_captcha_without_phone(self):
        page = _FakeImageCaptchaPage(self.login_config.login_url, self.login_config.home_url)
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            self._playwright_patch(page),
        ):
            self.broker.send_code()

        captured = {}

        home_url = self.login_config.home_url

        class CaptchaSession:
            cookies = []

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                captured["url"] = url
                captured["data"] = data
                captured["headers"] = headers
                captured["allow_redirects"] = allow_redirects
                captured["timeout"] = timeout
                return _DummyResponse(status_code=302, headers={"Location": home_url})

        authenticated_meta = {
            "status": "authenticated",
            "last_validation_at": "",
            "last_error_summary": "",
            "authenticated_at": "2026-04-22 12:00:00",
            "pending_since": "",
            "expires_at": "",
        }
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_requests_session_from_storage_state_payload", return_value=CaptchaSession()),
            patch.object(self.broker, "_persist_requests_session_locked", return_value=authenticated_meta),
        ):
            result = self.broker.submit_code("abcd")

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(captured["url"], "https://tms.ronghuiwl.com/system/login")
        self.assertEqual(captured["data"], {
            "username": "demo-user",
            "password": "demo-pass",
            "validateCode": "abcd",
        })
        self.assertNotIn("phone", captured["data"])

    def test_submit_code_posts_ronghui_image_captcha_without_phone(self):
        self.broker._pending_storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.broker._pending_login_state_path.write_text(
            json.dumps(
                {
                    "challenge_type": "image",
                    "challenge_label": "å›¾ç‰‡éªŒè¯ç ",
                    "captcha_image": "data:image/png;base64,abc",
                    "captcha_image_mime": "image/png",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.broker._save_meta(
            {
                "status": "pending_code",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "",
                "pending_since": "2026-04-22 11:58:00",
                "expires_at": "",
                "challenge_type": "image",
                "challenge_label": "å›¾ç‰‡éªŒè¯ç ",
                "captcha_image": "data:image/png;base64,abc",
                "captcha_image_mime": "image/png",
            }
        )

        captured = {}
        home_url = self.login_config.home_url
        authenticated_meta = self._authenticated_meta()

        class CaptchaSession:
            cookies = [_SimpleCookie()]

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                captured["url"] = url
                captured["data"] = data
                captured["headers"] = headers
                captured["allow_redirects"] = allow_redirects
                captured["timeout"] = timeout
                return _DummyResponse(status_code=302, headers={"Location": home_url})

        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_requests_session_from_storage_state_payload", return_value=CaptchaSession()),
            patch.object(self.broker, "_persist_requests_session_locked", return_value=authenticated_meta),
        ):
            result = self.broker.submit_code("abcd")

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(captured["url"], "https://tms.ronghuiwl.com/system/login")
        self.assertEqual(captured["data"], {
            "username": "demo-user",
            "password": "demo-pass",
            "validateCode": "abcd",
        })
        self.assertNotIn("phone", captured["data"])

    def test_submit_code_persists_authenticated_before_validation(self):
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_read_login_error", return_value=""),
            self._playwright_patch(),
        ):
            self.broker.send_code()

        response = _DummyResponse(status_code=200, text="dashboard", headers={})
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_read_login_error", return_value=""),
            patch.object(self.broker, "_session_from_saved_state_locked", return_value=_DummySession(response)),
            self._playwright_patch(),
        ):
            result = self.broker.submit_code("123456")

        self.assertEqual(result["status"], "authenticated")
        self.assertFalse(self.broker._pending_storage_state_path.exists())
        self.assertTrue(self.broker.describe_status(validate=False)["authenticated"])

    def test_validate_repairs_logged_out_meta_with_saved_authenticated_state(self):
        self.broker._save_meta(
            {
                "status": "logged_out",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-04-22 12:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        self.broker._storage_state_path.write_text(
            json.dumps({"cookies": [{"name": "sid", "value": "cookie", "domain": "tms.ronghuiwl.com", "path": "/"}], "origins": []}),
            encoding="utf-8",
        )
        response = _DummyResponse(status_code=200, text="dashboard", headers={})

        with patch.object(self.broker, "_session_from_saved_state_locked", return_value=_DummySession(response)):
            result = self.broker.describe_status(validate=True)

        self.assertEqual(result["status"], "authenticated")

    def test_validate_falls_back_to_browser_when_requests_validation_fails(self):
        self.broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-04-22 12:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        self.broker._storage_state_path.write_text(
            json.dumps({"cookies": [{"name": "sid", "value": "cookie", "domain": "tms.ronghuiwl.com", "path": "/"}], "origins": []}),
            encoding="utf-8",
        )

        with (
            patch.object(self.broker, "_session_from_saved_state_locked", side_effect=RuntimeError("ssl handshake failed")),
            patch.object(self.broker, "_validate_storage_state_with_browser_locked", return_value=("authenticated", "")),
        ):
            result = self.broker.describe_status(validate=True)

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(result["last_error_summary"], "")

    def test_clear_transitions_to_logged_out(self):
        self.broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-04-22 12:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        self.broker._storage_state_path.write_text("{}", encoding="utf-8")
        self.broker._cookies_path.write_text("[]", encoding="utf-8")

        with patch.object(self.broker, "_start_status_alert"):
            result = self.broker.clear()

        self.assertEqual(result["status"], "logged_out")
        self.assertFalse(self.broker._storage_state_path.exists())
        self.assertFalse(self.broker._cookies_path.exists())

    def test_save_credentials_are_returned_and_marked_saved(self):
        result = self.broker.save_credentials(username="demo-user", password="demo-pass", phone="13800000000")

        self.assertTrue(result["has_saved_credentials"])
        self.assertEqual(result["username"], "demo-user")
        self.assertEqual(result["password"], "")
        self.assertEqual(result["phone"], "13800000000")
        self.assertTrue(self.broker._login_profile_path.exists())
        self.assertTrue(self.broker.describe_status(validate=False)["has_saved_credentials"])

    def test_default_ronghui_credentials_do_not_require_phone(self):
        result = self.broker.save_credentials(username="demo-user", password="demo-pass", phone="")

        self.assertTrue(result["has_saved_credentials"])
        self.assertEqual(result["phone"], "")
        self.assertFalse(self.broker._require_phone)

    def test_save_credentials_preserves_existing_password_when_masked(self):
        self.broker.save_credentials(username="demo-user", password="demo-pass", phone="13800000000")

        result = self.broker.save_credentials(
            username="demo-user-updated",
            password=SAVED_PASSWORD_MASK,
            phone="13800001111",
        )

        saved = self.broker._load_saved_credentials_locked()
        self.assertTrue(result["has_saved_credentials"])
        self.assertEqual(result["password"], "")
        self.assertEqual(saved["username"], "demo-user-updated")
        self.assertEqual(saved["password"], "demo-pass")
        self.assertEqual(saved["phone"], "13800001111")

    def test_clear_saved_credentials_resets_defaults(self):
        self.broker.save_credentials(username="demo-user", password="demo-pass", phone="13800000000")

        result = self.broker.clear_saved_credentials()

        self.assertFalse(result["has_saved_credentials"])
        self.assertEqual(result["username"], "")
        self.assertFalse(self.broker._login_profile_path.exists())

    def test_resolve_login_config_prefers_saved_credentials(self):
        self.broker.save_credentials(username="saved-user", password="saved-pass", phone="13800001111")
        with patch.dict(
            "os.environ",
            {
                "TMS_LOGIN_USERNAME": "env-user",
                "TMS_LOGIN_PASSWORD": "env-pass",
                "TMS_LOGIN_PHONE": "13900002222",
            },
            clear=False,
        ):
            config = self.broker.resolve_login_config()

        self.assertEqual(config.username, "saved-user")
        self.assertEqual(config.password, "saved-pass")
        self.assertEqual(config.phone, "13800001111")

    def test_resolve_login_config_falls_back_to_env_without_saved_credentials(self):
        with patch.dict(
            "os.environ",
            {
                "TMS_LOGIN_USERNAME": "env-user",
                "TMS_LOGIN_PASSWORD": "env-pass",
                "TMS_LOGIN_PHONE": "13900002222",
            },
            clear=False,
        ):
            config = self.broker.resolve_login_config()

        self.assertEqual(config.username, "env-user")
        self.assertEqual(config.password, "env-pass")
        self.assertEqual(config.phone, "13900002222")

    def test_price_profile_uses_price_envs_and_separate_state_dir(self):
        price_broker = SessionBroker(
            profile_name="price",
            username_envs=PRICE_USERNAME_ENVS,
            password_envs=PRICE_PASSWORD_ENVS,
            phone_envs=PRICE_PHONE_ENVS,
        )
        state_dir = Path(self.tempdir.name) / "price"
        price_broker._state_dir = state_dir
        price_broker._meta_path = state_dir / "session_meta.json"
        price_broker._storage_state_path = state_dir / "storage_state.json"
        price_broker._cookies_path = state_dir / "cookies.json"
        price_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        price_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        price_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)

        with patch.dict(
            "os.environ",
            {
                "TMS_DAXIANGUSERNAME": "price-user",
                "TMS_DAXIANGPASSWORD": "price-pass",
                "TMS_DAXIANGPHONE": "13800003333",
            },
            clear=False,
        ):
            config = price_broker.resolve_login_config()

        self.assertEqual(price_broker.profile_name, "price")
        self.assertEqual(config.username, "price-user")
        self.assertEqual(config.password, "price-pass")
        self.assertEqual(config.phone, "13800003333")
        self.assertIn("price", str(price_broker._state_dir))

    def test_yunda_profile_uses_independent_state_and_ronghui_alias(self):
        session_broker_module._SESSION_BROKERS.clear()
        try:
            with patch.dict(
                "os.environ",
                {
                    "YUNDA_REPORT_USERNAME": "yunda-user",
                    "YUNDA_REPORT_PASSWORD": "yunda-pass",
                    "YUNDA_REPORT_PHONE": "13800004444",
                    "YUNDA_REPORT_BASE_ORIGIN": "https://ky-client.yunda56.com",
                },
                clear=False,
            ):
                default_broker = get_session_broker("default")
                ronghui_broker = get_session_broker("ronghui")
                yunda_broker = get_session_broker("yunda")
                config = yunda_broker.resolve_login_config()

            self.assertIs(default_broker, ronghui_broker)
            self.assertNotEqual(default_broker._state_dir, yunda_broker._state_dir)
            self.assertIn("yunda", str(yunda_broker._state_dir))
            self.assertEqual(config.base_origin, "https://ky-client.yunda56.com")
            self.assertEqual(config.login_url, "https://ky-sso.yunda56.com/login")
            self.assertEqual(config.home_url, "https://ky-client.yunda56.com/client")
            self.assertEqual(config.username, "yunda-user")
            self.assertEqual(config.password, "yunda-pass")
            self.assertEqual(config.phone, "13800004444")
            self.assertEqual(yunda_broker._login_mode, "yunda_password")
            self.assertFalse(yunda_broker._require_phone)
        finally:
            session_broker_module._SESSION_BROKERS.clear()

    def test_yunda_credentials_do_not_require_phone(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        state_dir = Path(self.tempdir.name) / "yunda-creds"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)

        result = yunda_broker.save_credentials(username="yunda-user", password="yunda-pass", phone="")

        self.assertTrue(result["has_saved_credentials"])
        self.assertTrue(result["has_manual_credentials"])
        self.assertEqual(result["credential_source"], "saved")
        self.assertEqual(result["phone"], "")
        self.assertTrue(yunda_broker.describe_status(validate=False)["has_saved_credentials"])

    def test_yunda_env_credentials_enable_backend_login_without_exposing_values(self):
        yunda_broker = SessionBroker(
            profile_name="yunda",
            username_envs=("YUNDA_USERNAME",),
            password_envs=("YUNDA_PASSWORD",),
            phone_envs=("YUNDA_PHONE",),
            login_mode="yunda_password",
            require_phone=False,
        )
        state_dir = Path(self.tempdir.name) / "yunda-env-creds"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)

        with patch.dict(
            "os.environ",
            {"YUNDA_USERNAME": "env-user", "YUNDA_PASSWORD": "env-pass"},
            clear=False,
        ):
            credentials = yunda_broker.get_saved_credentials()
            config = yunda_broker.resolve_login_config()
            status = yunda_broker.describe_status(validate=False)

        self.assertTrue(credentials["has_saved_credentials"])
        self.assertFalse(credentials["has_manual_credentials"])
        self.assertTrue(credentials["has_env_credentials"])
        self.assertEqual(credentials["credential_source"], "env")
        self.assertEqual(credentials["username"], "")
        self.assertEqual(credentials["password"], "")
        self.assertEqual(config.username, "env-user")
        self.assertEqual(config.password, "env-pass")
        self.assertTrue(status["has_saved_credentials"])
        self.assertEqual(status["credential_source"], "env")

    def test_pending_meta_preserves_yunda_captcha_image(self):
        meta = self.broker._save_meta(
            {
                "status": "pending_code",
                "last_validation_at": "",
                "last_error_summary": "请输入图片验证码",
                "authenticated_at": "",
                "pending_since": "2026-05-12 17:00:00",
                "expires_at": "",
                "challenge_type": "image",
                "challenge_label": "图片验证码",
                "captcha_image": "data:image/png;base64,abc",
                "captcha_image_mime": "image/png",
                "captcha_captured_at": "2026-05-12 17:00:01",
            }
        )

        self.assertEqual(meta["captcha_image"], "data:image/png;base64,abc")
        status = self.broker.describe_status(validate=False)
        self.assertEqual(status["status_tone"], "warning")
        self.assertEqual(status["challenge_type"], "image")
        self.assertEqual(status["challenge_label"], "图片验证码")
        self.assertEqual(status["captcha_image"], "data:image/png;base64,abc")

    def test_yunda_submit_code_uses_sms_pending_challenge(self):
        self.broker._pending_storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}),
            encoding="utf-8",
        )
        self.broker._pending_login_state_path.write_text(
            json.dumps({"challenge_type": "sms", "login_url": "https://ky-sso.yunda56.com/public/sms/sms_valid"}),
            encoding="utf-8",
        )
        self.broker._save_meta(
            {
                "status": "pending_code",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "",
                "pending_since": "2026-05-12 17:00:00",
                "expires_at": "",
                "challenge_type": "sms",
                "challenge_label": "短信验证码",
            }
        )
        expected = {"status": "authenticated"}
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_run_in_isolated_thread", side_effect=lambda func: func()),
            patch.object(self.broker, "_submit_yunda_sms_login", return_value=expected) as submit_sms,
            patch.object(self.broker, "_submit_yunda_captcha_login") as submit_captcha,
        ):
            result = self.broker._submit_code_yunda("123456")

        self.assertIs(result, expected)
        submit_sms.assert_called_once()
        self.assertEqual(submit_sms.call_args.kwargs["sms_code"], "123456")
        submit_captcha.assert_not_called()

    def test_yunda_sms_submit_clicks_confirm_without_resending_code(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        self._configure_broker_state(yunda_broker, "yunda-sms-submit")
        yunda_broker._pending_storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}),
            encoding="utf-8",
        )
        yunda_broker._pending_login_state_path.write_text(
            json.dumps({"challenge_type": "sms", "login_url": "https://ky-sso.yunda56.com/public/sms/sms_valid"}),
            encoding="utf-8",
        )
        yunda_config = LoginConfig(
            base_origin="https://ky-sso.yunda56.com",
            login_url="https://ky-sso.yunda56.com/login",
            home_url="https://ky-client.yunda56.com/#/",
            username="yunda-user",
            password="yunda-pass",
            phone="",
        )
        authenticated_meta = self._authenticated_meta()

        class SmsLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector

            @property
            def first(self):
                return self

            def fill(self, value):
                self.page.filled[self.selector] = value

            def click(self, timeout=None):
                self.page.clicked.append(self.selector)
                if ":not(#send_code)" in self.selector:
                    self.page.submitted = True
                    self.page.url = "https://ky-client.yunda56.com/#/"

        class SmsPage(_FakePage):
            def __init__(self):
                super().__init__("https://ky-sso.yunda56.com/public/sms/sms_valid", yunda_config.home_url)
                self.submitted = False

            def locator(self, selector):
                return SmsLocator(self, selector)

        page = SmsPage()
        with (
            patch.object(yunda_broker, "resolve_login_config", return_value=yunda_config),
            patch.object(yunda_broker, "_is_yunda_sms_page", side_effect=lambda target: not target.submitted),
            patch.object(yunda_broker, "_persist_storage_state_locked", return_value=authenticated_meta) as persist_mock,
            patch.object(yunda_broker, "_close_pending_locked") as close_pending,
            self._playwright_patch(page),
        ):
            result = yunda_broker._submit_yunda_sms_login(
                yunda_config,
                sms_code="123456",
                pending_since="2026-05-30 18:14:00",
            )

        self.assertEqual("authenticated", result["status"])
        self.assertEqual("123456", page.filled[session_broker_module.YUNDA_SMS_CODE_INPUT])
        self.assertTrue(page.clicked)
        self.assertNotEqual(session_broker_module.YUNDA_SMS_SEND_BUTTON, page.clicked[-1])
        self.assertIn(":not(#send_code)", page.clicked[-1])
        persist_mock.assert_called_once()
        close_pending.assert_called_once()

    def test_yunda_sms_submit_waits_for_delayed_redirect(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        self._configure_broker_state(yunda_broker, "yunda-sms-delayed-redirect")
        yunda_broker._pending_storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}),
            encoding="utf-8",
        )
        yunda_broker._pending_login_state_path.write_text(
            json.dumps({"challenge_type": "sms", "login_url": "https://ky-sso.yunda56.com/public/sms/sms_valid"}),
            encoding="utf-8",
        )
        yunda_config = LoginConfig(
            base_origin="https://ky-sso.yunda56.com",
            login_url="https://ky-sso.yunda56.com/login",
            home_url="https://ky-client.yunda56.com/#/",
            username="yunda-user",
            password="yunda-pass",
            phone="",
        )
        authenticated_meta = self._authenticated_meta()

        class DelayedSmsLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector

            @property
            def first(self):
                return self

            def fill(self, value):
                self.page.filled[self.selector] = value

            def click(self, timeout=None):
                self.page.clicked.append(self.selector)
                self.page.submitted = True

        class DelayedSmsPage(_FakePage):
            def __init__(self):
                super().__init__("https://ky-sso.yunda56.com/public/sms/sms_valid", yunda_config.home_url)
                self.submitted = False
                self.settle_ticks = 0

            def locator(self, selector):
                return DelayedSmsLocator(self, selector)

            def wait_for_timeout(self, ms):
                if self.submitted:
                    self.settle_ticks += 1
                    if self.settle_ticks >= 2:
                        self.url = "https://ky-client.yunda56.com/#/"

        page = DelayedSmsPage()
        with (
            patch.object(yunda_broker, "resolve_login_config", return_value=yunda_config),
            patch.object(
                yunda_broker,
                "_is_yunda_sms_page",
                side_effect=lambda target: not target.submitted or target.settle_ticks < 2,
            ),
            patch.object(yunda_broker, "_read_yunda_sms_error", return_value="") as read_error,
            patch.object(yunda_broker, "_persist_storage_state_locked", return_value=authenticated_meta) as persist_mock,
            patch.object(yunda_broker, "_close_pending_locked") as close_pending,
            self._playwright_patch(page),
        ):
            result = yunda_broker._submit_yunda_sms_login(
                yunda_config,
                sms_code="123456",
                pending_since="2026-05-30 18:30:00",
            )

        self.assertEqual("authenticated", result["status"])
        self.assertGreaterEqual(page.settle_ticks, 2)
        read_error.assert_called()
        persist_mock.assert_called_once()
        close_pending.assert_called_once()

    def test_yunda_status_validates_report_api(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        state_dir = Path(self.tempdir.name) / "yunda-report-ok"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        yunda_broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-05-12 22:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        yunda_broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        class Response:
            status_code = 200

            def __init__(self, url, text, headers=None, json_payload=None):
                self.url = url
                self.text = text
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                return self._json_payload

        class Session:
            cookies = []

            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "searchData" in url:
                    return Response(url, '{"total":0,"rows":[]}', {"content-type": "application/json"}, {"total": 0, "rows": []})
                if "kyinms.yunda56.com" in url:
                    return Response(url, "<html>用户登录 快件跟踪 mail app</html>", {"content-type": "text/html"})
                if "client/user/info" in url:
                    return Response(url, '{"data":{"userId":"u-1"}}', {"content-type": "application/json"}, {"data": {"userId": "u-1"}})
                if "kyproblem.yunda56.com" in url:
                    return Response(url, "<html>韵达问题件查询</html>", {"content-type": "text/html"})
                return Response(url, "<html>report</html>", {"content-type": "text/html"})

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "message/api/getTypes" in url:
                    return Response(url, '{"data":[]}', {"content-type": "application/json"}, {"data": []})
                return Response(url, "{}", {"content-type": "application/json"}, {})

        session = Session()
        with patch.object(yunda_broker, "_session_from_saved_state_locked", return_value=session):
            status = yunda_broker.describe_status(validate=True)

        self.assertEqual("authenticated", status["status"])
        self.assertEqual("success", status["status_tone"])
        self.assertTrue(any("searchData" in url for url, _kwargs in session.calls))
        self.assertTrue(any("kyinms.yunda56.com" in url for url, _kwargs in session.calls))
        self.assertTrue(any("client/user/info" in url for url, _kwargs in session.calls))
        self.assertTrue(any("message/api/getTypes" in url for url, _kwargs in session.calls))
        self.assertTrue(any("kyproblem.yunda56.com" in url for url, _kwargs in session.calls))

    def test_yunda_status_expires_when_problem_center_redirects_to_client_shell(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        state_dir = Path(self.tempdir.name) / "yunda-problem-expired"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        yunda_broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-05-12 22:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        yunda_broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        class Response:
            status_code = 200

            def __init__(self, url, text, headers=None, json_payload=None):
                self.url = url
                self.text = text
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                return self._json_payload

        class Session:
            cookies = []

            def get(self, url, **kwargs):
                if "searchData" in url:
                    return Response(url, '{"total":0,"rows":[]}', {"content-type": "application/json"}, {"total": 0, "rows": []})
                if "kyinms.yunda56.com" in url:
                    return Response(url, "<html>快件跟踪 mail app</html>", {"content-type": "text/html"})
                if "client/user/info" in url:
                    return Response(url, '{"data":{"userId":"u-1"}}', {"content-type": "application/json"}, {"data": {"userId": "u-1"}})
                if "kyproblem.yunda56.com" in url:
                    return Response(
                        "https://ky-client.yunda56.com/#/",
                        "<html>韵达快运客户端</html>",
                        {"content-type": "text/html"},
                    )
                return Response(url, "<html>report</html>", {"content-type": "text/html"})

            def post(self, url, **kwargs):
                if "message/api/getTypes" in url:
                    return Response(url, '{"data":[]}', {"content-type": "application/json"}, {"data": []})
                return Response(url, "{}", {"content-type": "application/json"}, {})

        with patch.object(yunda_broker, "_session_from_saved_state_locked", return_value=Session()):
            status = yunda_broker.describe_status(validate=True)

        self.assertEqual("expired", status["status"])
        self.assertIn("问题件", status["last_error_summary"])

    def test_yunda_problem_browser_session_uses_client_menu_route(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        self._configure_broker_state(yunda_broker, "yunda-problem-client-route")

        class ProblemPage(_FakePage):
            def __init__(self):
                super().__init__(
                    "https://ky-client.yunda56.com/#/",
                    "https://ky-client.yunda56.com/#/",
                )
                self.goto_urls = []
                self.waited_selectors = []
                self.evaluated = []
                self.frames = []

            def goto(self, url, wait_until=None, timeout=None):
                self.goto_urls.append(url)
                self.url = url

            def evaluate(self, js_code):
                self.evaluated.append(js_code)
                self.frames = [
                    types.SimpleNamespace(
                        url="https://kyproblem.yunda56.com/ky_problem/public/index.php/query/index.html?kyflag=redacted"
                    )
                ]
                return {
                    "clicked": True,
                    "text": "问题件查询",
                    "href": "https://ky-client.yunda56.com/#/ifarme/ifarme/4768/问题件查询",
                }

            def wait_for_selector(self, selector, timeout=None):
                self.waited_selectors.append(selector)
                return None

        page = ProblemPage()
        with (
            patch.object(yunda_broker, "resolve_login_config", return_value=LoginConfig(
                base_origin="https://ky-sso.yunda56.com",
                login_url="https://ky-sso.yunda56.com/login",
                home_url="https://ky-client.yunda56.com/#/",
                username="yunda-user",
                password="yunda-pass",
                phone="",
            )),
            patch.object(yunda_broker, "_is_yunda_sms_page", return_value=False),
            patch.object(yunda_broker, "_is_yunda_login_page", return_value=False),
        ):
            yunda_broker._ensure_yunda_problem_session_in_browser_locked(_FakeContext(page), page)

        self.assertEqual(
            session_broker_module.YUNDA_CLIENT_SYSTEM_HOME_URL,
            page.goto_urls[0],
        )
        self.assertNotEqual(session_broker_module.YUNDA_PROBLEM_QUERY_URL, page.goto_urls[0])
        self.assertTrue(any("问题件查询" in script for script in page.evaluated))
        self.assertIn(session_broker_module.YUNDA_PROBLEM_IFRAME_SELECTOR, page.waited_selectors)

    def test_yunda_status_expires_when_inms_requires_login(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        state_dir = Path(self.tempdir.name) / "yunda-inms-expired"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        yunda_broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-05-12 22:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        yunda_broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        class Response:
            status_code = 200

            def __init__(self, url, text, headers=None, json_payload=None):
                self.url = url
                self.text = text
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                return self._json_payload

        class Session:
            cookies = []

            def get(self, url, **kwargs):
                if "searchData" in url:
                    return Response(url, '{"total":0,"rows":[]}', {"content-type": "application/json"}, {"total": 0, "rows": []})
                if "kyinms.yunda56.com" in url:
                    return Response(url, '<form id="login_form"></form>', {"content-type": "text/html"})
                return Response(url, "<html>report</html>", {"content-type": "text/html"})

        with patch.object(yunda_broker, "_session_from_saved_state_locked", return_value=Session()):
            status = yunda_broker.describe_status(validate=True)

        self.assertEqual("expired", status["status"])
        self.assertIn("快件跟踪", status["last_error_summary"])

    def test_yunda_status_expires_when_message_center_has_no_user_id(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        state_dir = Path(self.tempdir.name) / "yunda-message-expired"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        yunda_broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-05-12 22:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        yunda_broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        class Response:
            status_code = 200

            def __init__(self, url, text, headers=None, json_payload=None):
                self.url = url
                self.text = text
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                if self._json_payload is None:
                    raise ValueError("empty")
                return self._json_payload

        class Session:
            cookies = []

            def get(self, url, **kwargs):
                if "searchData" in url:
                    return Response(url, '{"total":0,"rows":[]}', {"content-type": "application/json"}, {"total": 0, "rows": []})
                if "kyinms.yunda56.com" in url:
                    return Response(url, "<html>快件跟踪 mail app</html>", {"content-type": "text/html"})
                if "client/user/info" in url:
                    return Response(url, '{"data":{}}', {"content-type": "application/json"}, {"data": {}})
                return Response(url, "<html>report</html>", {"content-type": "text/html"})

        with patch.object(yunda_broker, "_session_from_saved_state_locked", return_value=Session()):
            status = yunda_broker.describe_status(validate=True)

        self.assertEqual("expired", status["status"])
        self.assertIn("消息中心", status["last_error_summary"])

    def test_yunda_status_ignores_message_center_database_error(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        state_dir = Path(self.tempdir.name) / "yunda-message-db-error"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        yunda_broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-05-12 22:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        yunda_broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        class Response:
            status_code = 200

            def __init__(self, url, text, headers=None, json_payload=None):
                self.url = url
                self.text = text
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                return self._json_payload

        class Session:
            cookies = []

            def get(self, url, **kwargs):
                if "searchData" in url:
                    return Response(url, '{"total":0,"rows":[]}', {"content-type": "application/json"}, {"total": 0, "rows": []})
                if "kyinms.yunda56.com" in url:
                    return Response(url, "<html>快件跟踪 mail app</html>", {"content-type": "text/html"})
                if "client/user/info" in url:
                    return Response(url, '{"data":{"userId":"u-1"}}', {"content-type": "application/json"}, {"data": {"userId": "u-1"}})
                return Response(url, "<html>report</html>", {"content-type": "text/html"})

            def post(self, url, **kwargs):
                if "message/api/getTypes" in url:
                    payload = {"errorCode": "80001", "message": "数据库操作异常"}
                    return Response(url, json.dumps(payload, ensure_ascii=False), {"content-type": "application/json"}, payload)
                return Response(url, "{}", {"content-type": "application/json"}, {})

        with patch.object(yunda_broker, "_session_from_saved_state_locked", return_value=Session()):
            status = yunda_broker.describe_status(validate=True)

        self.assertEqual("authenticated", status["status"])
        self.assertEqual("", status["last_error_summary"])

    def test_yunda_status_expires_when_report_api_empty(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        state_dir = Path(self.tempdir.name) / "yunda-report-empty"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        yunda_broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-05-12 22:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        yunda_broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        class Response:
            status_code = 200

            def __init__(self, url, text, headers=None):
                self.url = url
                self.text = text
                self.headers = headers or {}

            def json(self):
                raise ValueError("empty")

        class Session:
            cookies = []

            def get(self, url, **kwargs):
                if "searchData" in url:
                    return Response(url, "", {"content-type": "application/json"})
                return Response(url, "<html>report</html>", {"content-type": "text/html"})

        with patch.object(yunda_broker, "_session_from_saved_state_locked", return_value=Session()):
            status = yunda_broker.describe_status(validate=True)

        self.assertEqual("expired", status["status"])
        self.assertIn("报表接口返回空响应", status["last_error_summary"])

    def test_yunda_status_retries_transient_report_non_json(self):
        yunda_broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        state_dir = Path(self.tempdir.name) / "yunda-report-transient-html"
        yunda_broker._state_dir = state_dir
        yunda_broker._meta_path = state_dir / "session_meta.json"
        yunda_broker._storage_state_path = state_dir / "storage_state.json"
        yunda_broker._cookies_path = state_dir / "cookies.json"
        yunda_broker._pending_storage_state_path = state_dir / "pending_storage_state.json"
        yunda_broker._pending_login_state_path = state_dir / "pending_login_state.json"
        yunda_broker._login_profile_path = state_dir / "login_profile.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        yunda_broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-05-12 22:00:00",
                "pending_since": "",
                "expires_at": "",
            }
        )
        yunda_broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        class Response:
            status_code = 200

            def __init__(self, url, text, headers=None, json_payload=None):
                self.url = url
                self.text = text
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                if self._json_payload is None:
                    raise ValueError("not json")
                return self._json_payload

        class Session:
            cookies = []

            def __init__(self):
                self.calls = []
                self.search_calls = 0

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "searchData" in url:
                    self.search_calls += 1
                    if self.search_calls == 1:
                        return Response(url, "<html>temporary upstream page</html>", {"content-type": "text/html"})
                    return Response(url, '{"total":0,"rows":[]}', {"content-type": "application/json"}, {"total": 0, "rows": []})
                if "kyinms.yunda56.com" in url:
                    return Response(url, "<html>快件跟踪 mail app</html>", {"content-type": "text/html"})
                if "client/user/info" in url:
                    return Response(url, '{"data":{"userId":"u-1"}}', {"content-type": "application/json"}, {"data": {"userId": "u-1"}})
                return Response(url, "<html>report</html>", {"content-type": "text/html"})

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "message/api/getTypes" in url:
                    return Response(url, '{"data":[]}', {"content-type": "application/json"}, {"data": []})
                return Response(url, "{}", {"content-type": "application/json"}, {})

        session = Session()
        with (
            patch.object(yunda_broker, "_session_from_saved_state_locked", return_value=session),
            patch.object(session_broker_module.time, "sleep") as sleep_mock,
        ):
            status = yunda_broker.describe_status(validate=True)

        self.assertEqual("authenticated", status["status"])
        self.assertEqual(2, session.search_calls)
        sleep_mock.assert_called_once()

    def test_ronghui_status_validates_scan_api(self):
        self.broker._save_meta(self._authenticated_meta())
        self.broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        home_response = _DummyResponse(status_code=200, text="dashboard", headers={})
        scan_response = _DummyResponse(status_code=200, text='{"data":[]}', headers={"Content-Type": "application/json"})
        session = _DummySession(home_response, post_response=scan_response)

        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_session_from_saved_state_locked", return_value=session),
        ):
            status = self.broker.describe_status(validate=True)

        self.assertEqual("authenticated", status["status"])
        self.assertEqual(1, len(session.post_calls))
        self.assertIn("/dataQuery/findPageByCallId", session.post_calls[0]["url"])
        self.assertEqual({"id": "FIND_COME_SCAN_RECORD"}, session.post_calls[0]["params"])
        self.assertEqual("1", session.post_calls[0]["data"]["pageSize"])

    def test_ronghui_status_expires_when_scan_api_returns_login_page(self):
        self.broker._save_meta(self._authenticated_meta())
        self.broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        home_response = _DummyResponse(status_code=200, text="dashboard", headers={})
        scan_response = _DummyResponse(
            status_code=200,
            text="<html>system/login validateCode</html>",
            headers={"Content-Type": "text/html"},
        )
        session = _DummySession(home_response, post_response=scan_response)

        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_session_from_saved_state_locked", return_value=session),
        ):
            status = self.broker.describe_status(validate=True)

        self.assertEqual("expired", status["status"])
        self.assertIn("scan API", status["last_error_summary"])

    def test_ronghui_status_expires_when_menu_api_returns_failure(self):
        self.broker._save_meta(self._authenticated_meta())
        self.broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        home_response = _DummyResponse(status_code=200, text="dashboard", headers={})
        scan_response = _DummyResponse(status_code=200, text='{"data":[]}', headers={"Content-Type": "application/json"})
        menu_response = _DummyResponse(
            status_code=200,
            text='{"success":false,"message":"服务器内部错误\\r\\n","result":null}',
            headers={"Content-Type": "application/json"},
        )
        session = _DummySession(home_response, post_response=scan_response, menu_response=menu_response)

        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(self.broker, "_session_from_saved_state_locked", return_value=session),
        ):
            status = self.broker.describe_status(validate=True)

        self.assertEqual("expired", status["status"])
        self.assertIn("menu validation failed", status["last_error_summary"])

    def test_ensure_authenticated_raises_auth_required_after_expiry(self):
        self.broker._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "2026-04-22 12:00:00",
                "pending_since": "",
                "expires_at": "2026-04-23 12:00:00",
            }
        )
        self.broker._storage_state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        response = _DummyResponse(
            status_code=200,
            text="用户名 验证码 system/login",
            headers={},
        )
        with (
            patch.object(self.broker, "_session_from_saved_state_locked", return_value=_DummySession(response)),
            patch.object(self.broker, "_start_status_alert"),
        ):
            with self.assertRaises(Exception) as ctx:
                self.broker.ensure_authenticated(validate=True)

        self.assertEqual(getattr(ctx.exception, "code", ""), "AUTH_REQUIRED")
        self.assertEqual(self.broker.describe_status(validate=False)["status"], "expired")

    def test_describe_status_force_bypasses_validation_ttl(self):
        self.broker._storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.broker._save_meta(
            {
                **self._authenticated_meta(),
                "last_validation_at": session_broker_module._format_ts(session_broker_module._now_ts()),
            }
        )
        response = _DummyResponse(status_code=200, text="<html>dashboard</html>", headers={})
        with (
            patch.object(self.broker, "resolve_login_config", return_value=self.login_config),
            patch.object(
                self.broker,
                "_session_from_saved_state_locked",
                return_value=_DummySession(response),
            ) as session_factory,
        ):
            cached = self.broker.describe_status(validate=True)
            session_factory.assert_not_called()

            forced = self.broker.describe_status(validate=True, force=True)

        self.assertEqual(cached["status"], "authenticated")
        self.assertEqual(forced["status"], "authenticated")
        session_factory.assert_called_once()

    def test_session_meta_save_does_not_send_proactive_alert(self):
        with patch.object(self.broker, "_start_status_alert") as start_alert:
            self.broker._save_meta(
                {
                    "status": "authenticated",
                    "last_validation_at": "",
                    "last_error_summary": "",
                    "authenticated_at": "2026-04-22 12:00:00",
                    "pending_since": "",
                    "expires_at": "",
                }
            )
            start_alert.assert_not_called()

            expired = self.broker._save_meta(
                {
                    "status": "expired",
                    "last_validation_at": "2026-04-29 10:00:00",
                    "last_error_summary": "session expired",
                    "authenticated_at": "2026-04-22 12:00:00",
                    "pending_since": "",
                    "expires_at": "",
                }
            )
            start_alert.assert_not_called()

            self.broker._save_meta(
                {
                    **expired,
                    "last_validation_at": "2026-04-29 10:01:00",
                }
            )
            start_alert.assert_not_called()

    def test_session_disconnect_alert_not_sent_without_previous_auth(self):
        with patch.object(self.broker, "_start_status_alert") as start_alert:
            self.broker._save_meta(
                {
                    "status": "logged_out",
                    "last_validation_at": "2026-04-29 10:00:00",
                    "last_error_summary": "",
                    "authenticated_at": "",
                    "pending_since": "",
                    "expires_at": "",
                }
            )
            start_alert.assert_not_called()


class AutomationAccountManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        state_dir = Path(self.tempdir.name) / "state"
        self.patches = [
            patch.object(account_manager_module, "STATE_DIR", state_dir),
            patch.object(account_manager_module, "ACCOUNTS_PATH", state_dir / "automation_accounts.json"),
            patch.object(account_manager_module, "LOCAL_ACCOUNT_DIR", state_dir / "automation_account_credentials"),
        ]
        for item in self.patches:
            item.start()
        self.manager = account_manager_module.AutomationAccountManager()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tempdir.cleanup()

    def test_local_credentials_preserve_password_and_show_success_status(self):
        result = self.manager.save_credentials(
            "r7_default",
            username="73901001",
            password="r7-secret-pass",
        )
        self.assertTrue(result["has_saved_credentials"])
        self.assertEqual(result["password"], "")

        masked_result = self.manager.save_credentials(
            "r7_default",
            username="73901001",
            password=SAVED_PASSWORD_MASK,
        )
        private = self.manager.private_credentials("r7_default")
        status = self.manager.describe_status("r7_default", validate=False)

        self.assertTrue(masked_result["has_saved_credentials"])
        self.assertEqual(masked_result["password"], "")
        self.assertEqual(private["password"], "r7-secret-pass")
        self.assertEqual(status["status_tone"], "success")
        self.assertEqual(status["label"], "凭据已配置")

    def test_default_ronghui_account_status_maps_to_default_profile(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker:
            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "profile": "default",
                    "status": "authenticated",
                    "label": "已登录",
                    "status_tone": "success",
                    "authenticated": True,
                    "pending_code": False,
                    "last_validation_at": "2026-05-22 11:46:00",
                    "last_error_summary": "",
                    "authenticated_at": "2026-05-22 11:45:00",
                    "pending_since": "",
                    "expires_at": "",
                    "has_saved_credentials": True,
                }

        def fake_get_session_broker(profile):
            calls.append(("profile", profile))
            return FakeBroker()

        with patch.object(account_manager_module, "get_session_broker", side_effect=fake_get_session_broker):
            status = self.manager.describe_status("ronghui_default", validate=True, force=True)

        self.assertEqual(calls[0], ("profile", "default"))
        self.assertEqual(calls[1], ("describe", True, True))
        self.assertEqual(status["profile"], "default")
        self.assertEqual(status["account_id"], "ronghui_default")
        self.assertEqual(status["system"], "ronghui")

    def test_force_status_check_auto_logs_in_expired_session(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker:
            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "profile": "default",
                    "status": "expired" if validate else "authenticated",
                    "label": "已过期" if validate else "已登录",
                    "status_tone": "error" if validate else "success",
                    "authenticated": False if validate else True,
                    "pending_code": False,
                    "last_validation_at": "2026-06-01 13:53:51" if validate else "2026-06-01 13:52:51",
                    "last_error_summary": "session expired" if validate else "",
                    "authenticated_at": "2026-06-01 12:00:00",
                    "pending_since": "",
                    "expires_at": "",
                    "has_saved_credentials": True,
                }

            def send_code(self):
                calls.append(("send_code",))
                return {
                    "profile": "default",
                    "status": "authenticated",
                    "label": "已登录",
                    "status_tone": "success",
                    "authenticated": True,
                    "pending_code": False,
                    "last_validation_at": "2026-06-01 13:53:52",
                    "last_error_summary": "",
                    "authenticated_at": "2026-06-01 13:53:52",
                    "pending_since": "",
                    "expires_at": "",
                    "has_saved_credentials": True,
                }

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            status = self.manager.check_status_with_auto_login("ronghui_default", force=True)

        self.assertEqual(status["status"], "authenticated")
        self.assertEqual(status["account_id"], "ronghui_default")
        self.assertEqual(status["system"], "ronghui")
        self.assertEqual(
            calls,
            [
                ("describe", True, True),
                ("send_code",),
            ],
        )

    def test_force_status_check_keeps_pending_code_without_resending(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker:
            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "profile": "yunda",
                    "status": "pending_code",
                    "label": "待输入验证码",
                    "status_tone": "warning",
                    "authenticated": False,
                    "pending_code": True,
                    "last_validation_at": "",
                    "last_error_summary": "短信验证码已发送",
                    "authenticated_at": "",
                    "pending_since": "2026-06-01 13:53:51",
                    "expires_at": "",
                    "challenge_type": "sms",
                    "has_saved_credentials": True,
                }

            def send_code(self):
                calls.append(("send_code",))
                raise AssertionError("pending_code accounts must not resend code")

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            status = self.manager.check_status_with_auto_login("yunda_default", force=True)

        self.assertEqual(status["status"], "pending_code")
        self.assertEqual(status["account_id"], "yunda_default")
        self.assertEqual(calls, [("describe", True, True)])

    def test_default_accounts_include_daxiang_s_independent_profile(self):
        accounts = {
            item["account_id"]: item
            for item in self.manager.list_accounts(include_status=False)
        }

        self.assertIn("ronghui_daxiang_s", accounts)
        self.assertEqual("TMS大祥S站账号", accounts["ronghui_daxiang_s"]["name"])
        self.assertEqual("daxiang_s", accounts["ronghui_daxiang_s"]["account_purpose"])
        self.assertEqual("大祥S站", accounts["ronghui_daxiang_s"]["account_purpose_label"])
        self.assertEqual("daxiang_s", accounts["ronghui_daxiang_s"]["session_profile"])
        self.assertTrue(accounts["ronghui_daxiang_s"]["is_default"])

    def test_legacy_price_account_migrates_to_ronghui_price_purpose(self):
        account_manager_module.ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        account_manager_module.ACCOUNTS_PATH.write_text(
            json.dumps(
                [
                    {
                        "account_id": "price_default",
                        "system": "price",
                        "name": "大祥报价账号",
                        "is_active": True,
                        "is_default": True,
                        "session_profile": "price",
                        "created_at": "2026-05-01 08:00:00",
                        "updated_at": "2026-05-01 08:00:00",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        accounts = {
            item["account_id"]: item
            for item in self.manager.list_accounts(include_status=False)
        }

        self.assertEqual("ronghui", accounts["price_default"]["system"])
        self.assertEqual("TMS融辉", accounts["price_default"]["system_label"])
        self.assertEqual("price", accounts["price_default"]["account_purpose"])
        self.assertEqual("大祥报价", accounts["price_default"]["account_purpose_label"])
        self.assertEqual("price", accounts["price_default"]["session_profile"])
        self.assertTrue(accounts["price_default"]["is_default"])
        self.assertNotIn("price", {item["system"] for item in accounts.values()})

    def test_defaults_are_scoped_by_system_and_purpose(self):
        self.manager.create_account(
            account_id="price_backup",
            system="ronghui",
            account_purpose="price",
            name="报价备用账号",
        )

        self.manager.set_default("price_backup")
        accounts = {
            item["account_id"]: item
            for item in self.manager.list_accounts(include_status=False)
        }

        self.assertTrue(accounts["ronghui_default"]["is_default"])
        self.assertTrue(accounts["price_backup"]["is_default"])
        self.assertFalse(accounts["price_default"]["is_default"])
        self.assertEqual("price", accounts["price_backup"]["account_purpose"])
        self.assertEqual("price_price_backup", accounts["price_backup"]["session_profile"])

    def test_resolve_execution_params_uses_price_default_for_ronghui_price_purpose(self):
        params = self.manager.resolve_execution_params(
            {},
            default_system="ronghui",
            default_purpose="price",
        )

        self.assertEqual("price_default", params["account_id"])
        self.assertEqual("price", params["session_profile"])

    def test_resolve_role_account_params_injects_r13_credentials(self):
        self.manager.save_credentials(
            "r13_default",
            username="r13-user",
            password="r13-pass",
        )

        params = self.manager.resolve_role_account_params(
            {"r13_account_id": "r13_default", "days": 1},
            account_field="r13_account_id",
            output_account_field="",
            output_session_profile_field="",
        )

        self.assertEqual("r13-user", params["username"])
        self.assertEqual("r13-pass", params["password"])
        self.assertEqual("r13_default", params["r13_account_id"])
        self.assertEqual(1, params["days"])

    def test_account_login_response_includes_context_and_hides_password(self):
        class FakeBroker:
            def send_code(self):
                return {
                    "status": "authenticated",
                    "authenticated": True,
                    "pending_code": False,
                    "profile": "default",
                    "password": "secret-value",
                }

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            result = self.manager.login("ronghui_default")

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(result["account_id"], "ronghui_default")
        self.assertEqual(result["account_name"], "TMS融辉默认账号")
        self.assertEqual(result["system"], "ronghui")
        self.assertEqual(result["system_label"], "TMS融辉")
        self.assertEqual(result["account_purpose"], "general")
        self.assertEqual(result["session_profile"], "default")
        self.assertTrue(result["session_capable"])
        self.assertNotIn("password", result)

    def test_account_submit_code_response_includes_context_and_hides_password(self):
        class FakeBroker:
            def submit_code(self, code):
                return {
                    "status": "authenticated",
                    "authenticated": True,
                    "submitted_length": len(code),
                    "password": "secret-value",
                }

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            result = self.manager.submit_code("ronghui_default", "123456")

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(result["submitted_length"], 6)
        self.assertEqual(result["account_id"], "ronghui_default")
        self.assertEqual(result["session_profile"], "default")
        self.assertNotIn("password", result)


class TMSRoutesTests(unittest.TestCase):
    def setUp(self):
        if hasattr(routes_module, "_ACCOUNT_LIST_CACHE"):
            routes_module._ACCOUNT_LIST_CACHE.clear()
        if hasattr(routes_module, "_ACCOUNT_LIST_REFRESHING"):
            routes_module._ACCOUNT_LIST_REFRESHING = False
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_admin_status_route_uses_default_account_mapping(self):
        calls = []

        class FakeAccountManager:
            def describe_status(self, account_id, *, validate=True, force=False):
                calls.append((account_id, validate, force))
                return {
                    "profile": "default",
                    "account_id": account_id,
                    "account_name": "TMS融辉默认账号",
                    "system": "ronghui",
                    "system_label": "TMS融辉",
                    "status": "authenticated",
                    "label": "已登录",
                    "authenticated": True,
                    "pending_code": False,
                    "last_validation_at": "2026-04-22 12:00:00",
                    "last_error_summary": "",
                    "authenticated_at": "2026-04-22 11:59:00",
                    "pending_since": "",
                    "expires_at": "2026-04-23 11:59:00",
                    "has_saved_credentials": True,
                }

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            response = self.client.get("/admin/tms/session/status?force=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "authenticated")
        self.assertEqual(payload["profile"], "default")
        self.assertEqual(payload["account_id"], "ronghui_default")
        self.assertTrue(payload["has_saved_credentials"])
        self.assertEqual(calls, [("ronghui_default", True, True)])

    def test_admin_account_status_route_passes_force_to_manager(self):
        calls = []

        class FakeAccountManager:
            def check_status_with_auto_login(self, account_id, *, force=False):
                calls.append((account_id, force))
                return {
                    "profile": "default",
                    "account_id": account_id,
                    "account_name": "TMS融辉默认账号",
                    "system": "ronghui",
                    "status": "authenticated",
                    "label": "已登录",
                    "authenticated": True,
                    "pending_code": False,
                    "last_validation_at": "2026-04-22 12:00:00",
                    "last_error_summary": "",
                    "authenticated_at": "2026-04-22 11:59:00",
                    "pending_since": "",
                    "expires_at": "",
                    "has_saved_credentials": True,
                }

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            response = self.client.get("/admin/accounts/ronghui_default/status?force=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile"], "default")
        self.assertEqual(calls, [("ronghui_default", True)])

    def test_admin_accounts_route_passes_force_to_manager(self):
        calls = []

        class FakeAccountManager:
            def list_accounts(self, *, include_status=True, validate=True, force=False):
                calls.append((include_status, validate, force))
                return [
                    {
                        "account_id": "ronghui_default",
                        "system": "ronghui",
                        "status": {
                            "profile": "default",
                            "account_id": "ronghui_default",
                            "status": "authenticated",
                            "last_validation_at": "2026-04-22 12:00:00",
                        },
                    }
                ]

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            response = self.client.get("/admin/accounts?force=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["accounts"][0]["status"]["profile"], "default")
        self.assertEqual(calls, [(True, True, True)])

    def test_admin_accounts_prefer_cached_uses_existing_payload_without_rechecking(self):
        calls = []

        class FakeAccountManager:
            def list_accounts(self, *, include_status=True, validate=True, force=False):
                calls.append((include_status, validate, force))
                return [
                    {
                        "account_id": "ronghui_default",
                        "system": "ronghui",
                        "status": {
                            "profile": "default",
                            "account_id": "ronghui_default",
                            "status": "authenticated",
                        },
                    }
                ]

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            first = self.client.get("/admin/accounts?force=1")
            second = self.client.get("/admin/accounts?prefer_cached=1")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        payload = second.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["cached"])
        self.assertFalse(payload["stale"])
        self.assertFalse(payload["refreshing"])
        self.assertGreaterEqual(payload["cache_age_sec"], 0)
        self.assertEqual("ronghui_default", payload["accounts"][0]["account_id"])
        self.assertEqual(calls, [(True, True, True)])

    def test_admin_accounts_prefer_cached_force_schedules_background_refresh(self):
        calls = []

        class FakeAccountManager:
            def list_accounts(self, *, include_status=True, validate=True, force=False):
                calls.append((include_status, validate, force))
                return [
                    {
                        "account_id": "ronghui_default",
                        "system": "ronghui",
                        "status": {"status": "authenticated"},
                    }
                ]

        with (
            patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()),
            patch.object(routes_module, "_schedule_account_list_refresh", return_value=True) as schedule_refresh,
        ):
            first = self.client.get("/admin/accounts?force=1")
            second = self.client.get("/admin/accounts?force=1&prefer_cached=1")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        payload = second.json()
        self.assertTrue(payload["cached"])
        self.assertTrue(payload["stale"])
        self.assertTrue(payload["refreshing"])
        schedule_refresh.assert_called_once_with(force=True)
        self.assertEqual(calls, [(True, True, True)])

    def test_monitor_status_update_rewrites_cached_account_status(self):
        class FakeAccountManager:
            def list_accounts(self, *, include_status=True, validate=True, force=False):
                return [
                    {
                        "account_id": "ronghui_default",
                        "system": "ronghui",
                        "status": {
                            "profile": "default",
                            "account_id": "ronghui_default",
                            "status": "authenticated",
                            "last_error_summary": "",
                        },
                    },
                    {
                        "account_id": "yunda_default",
                        "system": "yunda",
                        "status": {
                            "profile": "yunda",
                            "account_id": "yunda_default",
                            "status": "authenticated",
                            "last_error_summary": "",
                        },
                    },
                ]

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            first = self.client.get("/admin/accounts?force=1")

        self.assertEqual(first.status_code, 200)

        routes_module.update_account_list_cache_status(
            {
                "profile": "default",
                "account_id": "ronghui_default",
                "status": "error",
                "last_error_summary": "缺少登录配置",
            }
        )

        second = self.client.get("/admin/accounts?prefer_cached=1")

        self.assertEqual(second.status_code, 200)
        payload = second.json()
        self.assertTrue(payload["cached"])
        self.assertEqual(payload["accounts"][0]["status"]["status"], "error")
        self.assertEqual(payload["accounts"][0]["status"]["last_error_summary"], "缺少登录配置")
        self.assertEqual(payload["accounts"][1]["status"]["status"], "authenticated")

    def test_credentials_routes_use_broker(self):
        fake_broker = types.SimpleNamespace(
            get_saved_credentials=lambda: {
                "username": "demo-user",
                "password": "demo-pass",
                "phone": "13800000000",
                "updated_at": "2026-04-22 12:00:00",
                "has_saved_credentials": True,
            },
            save_credentials=lambda username, password, phone: {
                "username": username,
                "password": password,
                "phone": phone,
                "updated_at": "2026-04-22 12:00:00",
                "has_saved_credentials": True,
            },
            clear_saved_credentials=lambda: {
                "username": "",
                "password": "",
                "phone": "",
                "updated_at": "",
                "has_saved_credentials": False,
            },
        )
        with patch("agent.tms_runtime.routes.get_session_broker", return_value=fake_broker):
            get_response = self.client.get("/admin/tms/session/credentials")
            save_response = self.client.post(
                "/admin/tms/session/credentials",
                json={"username": "saved-user", "password": "saved-pass", "phone": "13800001111"},
            )
            clear_response = self.client.post("/admin/tms/session/credentials/clear")

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["username"], "demo-user")
        self.assertEqual(get_response.json()["password"], "")
        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(save_response.json()["username"], "saved-user")
        self.assertEqual(save_response.json()["password"], "")
        self.assertEqual(clear_response.status_code, 200)
        self.assertFalse(clear_response.json()["has_saved_credentials"])

    def test_send_code_route_returns_auth_unavailable_payload(self):
        def _raise():
            from agent.tms_runtime.errors import TMSAuthStateError

            raise TMSAuthStateError("AUTH_UNAVAILABLE", "Playwright Python 依赖未安装。")

        fake_broker = types.SimpleNamespace(send_code=_raise)
        with patch("agent.tms_runtime.routes.get_session_broker", return_value=fake_broker):
            response = self.client.post("/admin/tms/session/send-code")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "AUTH_UNAVAILABLE")

    def test_price_session_routes_use_price_broker(self):
        calls: list[str] = []
        fake_broker = types.SimpleNamespace(
            send_code=lambda: {"status": "pending_code", "profile": "price"},
            submit_code=lambda code: {"status": "authenticated", "submitted": code, "profile": "price"},
        )

        def _fake_get_session_broker(profile_name="default"):
            calls.append(profile_name)
            return fake_broker

        with patch("agent.tms_runtime.routes.get_session_broker", side_effect=_fake_get_session_broker):
            send_response = self.client.post("/admin/tms/price-session/send-code")
            submit_response = self.client.post("/admin/tms/price-session/submit-code", json={"code": "123456"})

        self.assertEqual(send_response.status_code, 200)
        self.assertEqual(submit_response.status_code, 200)
        self.assertEqual(send_response.json()["profile"], "price")
        self.assertEqual(submit_response.json()["submitted"], "123456")
        self.assertEqual(["price", "price"], calls)

    def test_yunda_session_routes_use_yunda_broker(self):
        calls: list[str] = []
        fake_broker = types.SimpleNamespace(
            send_code=lambda: {"status": "pending_code", "profile": "yunda"},
            submit_code=lambda code: {"status": "authenticated", "submitted": code, "profile": "yunda"},
        )

        def _fake_get_session_broker(profile_name="default"):
            calls.append(profile_name)
            return fake_broker

        with patch("agent.tms_runtime.routes.get_session_broker", side_effect=_fake_get_session_broker):
            send_response = self.client.post("/admin/tms/yunda-session/send-code")
            submit_response = self.client.post("/admin/tms/yunda-session/submit-code", json={"code": "123456"})

        self.assertEqual(send_response.status_code, 200)
        self.assertEqual(submit_response.status_code, 200)
        self.assertEqual(send_response.json()["profile"], "yunda")
        self.assertEqual(submit_response.json()["submitted"], "123456")
        self.assertEqual(["yunda", "yunda"], calls)

    def test_get_price_route_uses_dispatch_layer(self):
        async def fake_execute_target(name, req):
            self.assertEqual(name, "get_price")
            self.assertEqual(req.params["address"], "长沙")
            return 200, {"ok": True, "data": {"目的网点": "测试站"}}

        with patch("agent.tms_runtime.routes.execute_target", side_effect=fake_execute_target):
            response = self.client.post("/tms/get_price", json={"params": {"address": "长沙"}, "timeout_sec": 30})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["目的网点"], "测试站")

    def test_post_route_returns_auth_required_payload(self):
        async def fake_execute_target(name, req):
            return 200, {"ok": False, "error_code": "AUTH_REQUIRED", "message": "当前未登录或登录态已过期。"}

        with patch("agent.tms_runtime.routes.execute_target", side_effect=fake_execute_target):
            response = self.client.post("/tms/scan_next", json={"params": {"items": []}, "timeout_sec": 30})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "AUTH_REQUIRED")

    def test_tms_route_accepts_legacy_raw_payload(self):
        async def fake_execute_target(name, req):
            self.assertEqual(name, "get_qianshou")
            self.assertEqual(req.params["disp_site_code"], "7390004")
            self.assertEqual(req.params["page_size"], 1)
            return 200, {"ok": True, "data": []}

        with patch("agent.tms_runtime.routes.execute_target", side_effect=fake_execute_target):
            response = self.client.post(
                "/tms/get_qianshou",
                json={"disp_site_code": "7390004", "page_size": 1},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_dispatch_runs_sync_target_outside_async_loop(self):
        module_name = "_test_tms_loop_guard_target"
        target_name = "_loop_guard"
        fake_module = types.ModuleType(module_name)
        fake_module.__file__ = str(SCRIPTS_DIR / f"{module_name}.py")

        def run_once(params):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return {"ok": True, "value": params.get("value")}
            return {"ok": False, "error": "running loop leaked into sync target"}

        fake_module.run_once = run_once
        sys.modules[module_name] = fake_module
        dispatch_module.TARGETS[target_name] = dispatch_module.Target(module=module_name, func="run_once")

        async def _run_target():
            dispatch_module._SEMAPHORES[target_name] = asyncio.Semaphore(1)
            return await dispatch_module.execute_target(
                target_name,
                dispatch_module.TaskRequest(params={"value": "ok"}, timeout_sec=30),
            )

        try:
            status_code, payload = asyncio.run(_run_target())
        finally:
            dispatch_module.TARGETS.pop(target_name, None)
            dispatch_module._SEMAPHORES.pop(target_name, None)
            sys.modules.pop(module_name, None)

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["data"]["ok"])
        self.assertEqual(payload["data"]["value"], "ok")

    def test_scan_next_run_once_moves_flow_out_of_running_async_loop(self):
        import scan_next

        calls = []

        def fake_run_flow_impl(**kwargs):
            try:
                asyncio.get_running_loop()
                loop_running = True
            except RuntimeError:
                loop_running = False
            calls.append({"loop_running": loop_running, "items": kwargs.get("items")})
            return {"ok": not loop_running, "items": kwargs.get("items")}

        async def _run_in_loop():
            with patch.object(scan_next, "_run_flow_impl", side_effect=fake_run_flow_impl):
                return scan_next.run_once(
                    {
                        "items": [
                            {
                                "station_name": "测试站",
                                "bill_code": "TEST001",
                            }
                        ]
                    }
                )

        result = asyncio.run(_run_in_loop())

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["loop_running"])
        self.assertEqual(calls[0]["items"][0]["bill_code"], "TEST001")


class ToolRegressionTests(unittest.TestCase):
    def setUp(self):
        self.internal_token_patch = patch.dict(
            os.environ,
            {"AGENT_INTERNAL_API_TOKEN": "test-internal-token"},
            clear=False,
        )
        self.send_order_sql_patch = patch(
            "tools.send_order_sync_tool.sync_console_waybills",
            return_value={"ok": True, "upserted": 0, "updates": 0, "creates": 0, "deleted_stale": 0},
        )
        self.yunda_send_sql_patch = patch(
            "tools.yunda_send_waybills_sync_tool.sync_console_waybills",
            return_value={"ok": True, "upserted": 0, "updates": 0, "creates": 0, "deleted_stale": 0},
        )
        self.delivery_status_sql_patch = patch(
            "tools.delivery_status_sync_tool.update_console_waybill_statuses",
            return_value={"ok": True, "updated": 0, "status": "signed"},
        )
        self.internal_token_patch.start()
        self.send_order_sql_mock = self.send_order_sql_patch.start()
        self.addCleanup(self.internal_token_patch.stop)
        self.yunda_send_sql_mock = self.yunda_send_sql_patch.start()
        self.delivery_status_sql_mock = self.delivery_status_sql_patch.start()
        self.addCleanup(self.send_order_sql_patch.stop)
        self.addCleanup(self.yunda_send_sql_patch.stop)
        self.addCleanup(self.delivery_status_sql_patch.stop)

    def test_fetch_dispatch_collects_all_pages(self):
        calls = []

        def fake_fetch(session, login_site_code, date_range=None, page_index=0, page_size=100):
            calls.append((login_site_code, page_index, page_size))
            pages = {
                0: {"data": [{"BILL_CODE": "R0001", "GOODS_NAME": "货物1"}]},
                1: {"data": [{"BILL_CODE": "R0002", "GOODS_NAME": "货物2"}]},
                2: {"data": []},
            }
            return pages[page_index]

        with patch("fetch_dispatch.fetch_dispatch_records", side_effect=fake_fetch):
            rows = fetch_dispatch.collect_dispatch_records(
                object(),
                login_site_code="73901",
                page_size=1,
                max_pages=5,
            )

        self.assertEqual(["R0001", "R0002"], [row[0] for row in rows])
        self.assertEqual([("73901", 0, 1), ("73901", 1, 1), ("73901", 2, 1)], calls)

    def test_default_http_service_urls_point_to_agent_tms(self):
        self.assertEqual(tms_tool.HTTP_SERVICE_URL, "http://127.0.0.1:9000/tms")
        self.assertEqual(query_tool.HTTP_SERVICE_URL, "http://127.0.0.1:9000/tms")
        self.assertEqual(price_tool.HTTP_SERVICE_URL, "http://127.0.0.1:9000/tms")

    def test_local_price_module_load_does_not_pollute_legacy_helper_modules(self):
        price_module_dir = str(Path(price_tool.PRICE_GET_MODULE).resolve().parent)
        price_script_root = str(Path(price_tool.PRICE_SCRIPT_ROOT).resolve())
        legacy_login_manager = Path(price_tool.PRICE_GET_MODULE).with_name("login_manager.py").resolve()
        helper_module_names = (
            "login_manager",
            "browser_address_resolver",
            "shared",
            "shared.address_utils",
            "shared.price_utils",
        )
        original_modules = {name: sys.modules.get(name) for name in helper_module_names}
        original_path = list(sys.path)

        price_tool._load_local_price_module.cache_clear()
        for name in helper_module_names:
            sys.modules.pop(name, None)

        try:
            price_tool._load_local_price_module()
            loaded_login_manager = sys.modules.get("login_manager")
            loaded_file = Path(getattr(loaded_login_manager, "__file__", "") or "").resolve()

            self.assertNotEqual(legacy_login_manager, loaded_file)
            self.assertEqual(original_path, sys.path)
            self.assertNotIn(price_module_dir, sys.path)
            self.assertNotIn(price_script_root, sys.path)
        finally:
            price_tool._load_local_price_module.cache_clear()
            sys.path[:] = original_path
            for name, module in original_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_dispatch_runtime_scripts_ignore_cached_legacy_login_manager(self):
        from agent.tms_runtime import dispatch

        class WrongAuth:
            pass

        fake_login_manager = types.ModuleType("login_manager")
        fake_login_manager.TMSAuth = WrongAuth
        fake_login_manager.__file__ = str(Path(price_tool.PRICE_GET_MODULE).with_name("login_manager.py"))

        original_login_manager = sys.modules.get("login_manager")
        original_fetch_dispatch = sys.modules.get("fetch_dispatch")
        sys.modules["login_manager"] = fake_login_manager
        sys.modules.pop("fetch_dispatch", None)

        try:
            fn = dispatch._load_callable(dispatch.TARGETS["fetch_dispatch"])
            loaded_module = sys.modules[fn.__module__]
            auth_module = sys.modules[loaded_module.TMSAuth.__module__]
            auth_file = Path(auth_module.__file__).resolve()

            self.assertIsNot(WrongAuth, loaded_module.TMSAuth)
            self.assertTrue(auth_file.is_relative_to(SCRIPTS_DIR))
        finally:
            if original_login_manager is None:
                sys.modules.pop("login_manager", None)
            else:
                sys.modules["login_manager"] = original_login_manager
            if original_fetch_dispatch is None:
                sys.modules.pop("fetch_dispatch", None)
            else:
                sys.modules["fetch_dispatch"] = original_fetch_dispatch

    def test_price_tool_unwraps_agent_tms_response_data(self):
        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "ok": True,
                    "cost_sec": 0.2,
                    "data": {"目的网点": "泸州泸县站"},
                }

        with patch("tools.price_tool.httpx.post", return_value=_Response()):
            result = price_tool.get_price_via_http(
                address="四川省泸州市泸县241乡道东南侧",
                weight=800,
                volume=5,
            )

        self.assertEqual("泸州泸县站", result["目的网点"])
        self.assertEqual("agent_tms", result["mode"])
        self.assertNotIn("data", result)

    def test_price_tool_combines_ronghui_and_yunda_address_quotes(self):
        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        def fake_post(url, json=None, timeout=None, headers=None):
            if url.endswith("/get_price"):
                return _Response({"ok": True, "data": {"目的网点": "武汉融信站", "精准零担": "273.92元"}})
            if url.endswith("/yunda_price"):
                return _Response({"ok": True, "data": {"韵达自提": "120.00元", "韵达派送": "138.50元"}})
            raise AssertionError(url)

        with (
            patch("tools.price_tool.httpx.post", side_effect=fake_post),
            patch.object(price_tool, "PRICE_TOOL_PREFER_HTTP", True),
        ):
            result = price_tool.get_combined_price(
                address="武汉市黄陂区横店街天阳路1号",
                weight=1055,
                volume=0.3,
            )

        self.assertEqual("agent_tms_combined", result["mode"])
        self.assertEqual("武汉融信站", result["ronghui"]["目的网点"])
        self.assertEqual("120.00元", result["yunda"]["韵达自提"])
        self.assertEqual("138.50元", result["yunda"]["韵达派送"])

    def test_price_tool_keeps_ronghui_when_yunda_quote_fails(self):
        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        def fake_post(url, json=None, timeout=None, headers=None):
            if url.endswith("/get_price"):
                return _Response({"ok": True, "data": {"目的网点": "武汉融信站", "精准零担": "273.92元"}})
            if url.endswith("/yunda_price"):
                return _Response({"ok": False, "error": "韵达报价无结果"})
            raise AssertionError(url)

        with (
            patch("tools.price_tool.httpx.post", side_effect=fake_post),
            patch.object(price_tool, "PRICE_TOOL_PREFER_HTTP", True),
        ):
            result = price_tool.get_combined_price(
                address="武汉市黄陂区横店街天阳路1号",
                weight=1055,
                volume=0.3,
            )

        self.assertEqual("agent_tms_combined", result["mode"])
        self.assertNotIn("error", result)
        self.assertEqual("武汉融信站", result["ronghui"]["目的网点"])
        self.assertTrue(result["yunda"]["failed"])
        self.assertNotIn("unavailable", result["yunda"])
        self.assertEqual("韵达", result["yunda"]["provider"])
        self.assertIn("韵达报价无结果", result["yunda"]["error"])

    def test_price_tool_still_calls_yunda_when_ronghui_quote_fails(self):
        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        called_urls: list[str] = []

        def fake_post(url, json=None, timeout=None, headers=None):
            called_urls.append(url)
            if url.endswith("/get_price"):
                return _Response({"ok": False, "error": "融辉报价无结果"})
            if url.endswith("/yunda_price"):
                return _Response({"ok": True, "data": {"韵达自提": "120.00元", "韵达派送": "138.50元"}})
            raise AssertionError(url)

        with (
            patch("tools.price_tool.httpx.post", side_effect=fake_post),
            patch.object(price_tool, "PRICE_TOOL_PREFER_HTTP", True),
        ):
            result = price_tool.get_combined_price(
                address="武汉市黄陂区横店街天阳路1号",
                weight=1055,
                volume=0.3,
            )

        self.assertEqual(2, len(called_urls))
        self.assertEqual("agent_tms_combined", result["mode"])
        self.assertNotIn("error", result)
        self.assertTrue(result["ronghui"]["failed"])
        self.assertNotIn("unavailable", result["ronghui"])
        self.assertEqual("融辉", result["ronghui"]["provider"])
        self.assertIn("融辉报价无结果", result["ronghui"]["error"])
        self.assertEqual("120.00元", result["yunda"]["韵达自提"])

    def test_price_tool_keeps_yunda_when_ronghui_returns_unreachable_marker(self):
        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        def fake_post(url, json=None, timeout=None, headers=None):
            if url.endswith("/get_price"):
                return _Response({"ok": True, "data": {"网点不可达": "网点不可达"}})
            if url.endswith("/yunda_price"):
                return _Response({"ok": True, "data": {"韵达自提": "120.00元", "韵达派送": "138.50元"}})
            raise AssertionError(url)

        with (
            patch("tools.price_tool.httpx.post", side_effect=fake_post),
            patch.object(price_tool, "PRICE_TOOL_PREFER_HTTP", True),
        ):
            result = price_tool.get_combined_price(
                address="武汉市黄陂区横店街天阳路1号",
                weight=1055,
                volume=0.3,
            )

        self.assertEqual("agent_tms_combined", result["mode"])
        self.assertTrue(result["ronghui"]["unavailable"])
        self.assertEqual("网点不可达", result["ronghui"]["error"])
        self.assertNotIn("网点不可达", result)
        self.assertEqual("120.00元", result["yunda"]["韵达自提"])

    def test_price_tool_yunda_auth_error_marks_yunda_session(self):
        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        def fake_post(url, json=None, timeout=None, headers=None):
            if url.endswith("/get_price"):
                return _Response({"ok": True, "data": {"目的网点": "武汉融信站"}})
            if url.endswith("/yunda_price"):
                return _Response({
                    "ok": False,
                    "error_code": "AUTH_REQUIRED",
                    "error": "韵达登录态已失效，请重新登录韵达账号。",
                    "data": {},
                })
            raise AssertionError(url)

        with (
            patch("tools.price_tool.httpx.post", side_effect=fake_post),
            patch.object(price_tool, "PRICE_TOOL_PREFER_HTTP", True),
        ):
            result = price_tool.get_combined_price(
                address="武汉市黄陂区横店街天阳路1号",
                weight=1055,
                volume=0.3,
            )

        self.assertEqual("AUTH_REQUIRED", result["error_code"])
        self.assertEqual("yunda", result["auth_session"])
        self.assertEqual("韵达", result["provider"])
        self.assertEqual("武汉融信站", result["ronghui"]["目的网点"])

    def test_ronghui_address_resolution_prefers_entry_page_resolver(self):
        from agent.tms_runtime.scripts import get_price as ronghui_get_price

        browser_resolved = {
            "used_address": "浙江省宁波市镇海区招宝山街道威海路1188号2楼A库康特恩仓库",
            "addr_info": {
                "province": "浙江省",
                "city": "宁波市",
                "county": "镇海区",
            },
            "search_name": "浙江宁波镇海招宝山公司",
            "destination": {"DESTINATION_CODE": "3302001"},
            "dispatch": {"dispatch_site_code": "3302001", "dispatch_site_name": "浙江宁波镇海招宝山公司"},
            "temp_dest_code": "3302001",
        }

        with (
            patch.object(ronghui_get_price, "_resolve_destination_via_browser", return_value=browser_resolved) as browser_resolve,
            patch.object(ronghui_get_price, "_parse_address", side_effect=AssertionError("API resolver should not run first")) as parse_address,
        ):
            result = ronghui_get_price.resolve_address_destination(
                object(),
                "浙江省宁波市镇海区招宝山街道威海路1188号2楼A库康特恩仓库",
                {},
                "",
                "",
            )

        self.assertEqual("浙江宁波镇海招宝山公司", result["dispatch"]["dispatch_site_name"])
        browser_resolve.assert_called_once()
        parse_address.assert_not_called()

    def test_ronghui_address_resolution_does_not_fallback_when_entry_page_fails(self):
        from agent.tms_runtime.scripts import get_price as ronghui_get_price

        with (
            patch.object(
                ronghui_get_price,
                "_resolve_destination_via_browser",
                side_effect=ronghui_get_price.PriceCalcError("browser resolver destination code missing"),
            ),
            patch.object(ronghui_get_price, "_parse_address", side_effect=AssertionError("fallback resolver should not run")),
        ):
            with self.assertRaisesRegex(ronghui_get_price.PriceCalcError, "browser resolver destination code missing"):
                ronghui_get_price.resolve_address_destination(
                    object(),
                    "浙江省宁波市镇海区招宝山街道威海路1188号2楼A库康特恩仓库",
                    {},
                    "",
                    "",
                )

    def test_ronghui_address_resolution_uses_runtime_resolver_when_cached_legacy_module_exists(self):
        from agent.tms_runtime.scripts import browser_address_resolver as runtime_resolver
        from agent.tms_runtime.scripts import get_price as ronghui_get_price

        class WrongCachedResolver:
            def __init__(self, **_kwargs):
                pass

            def resolve(self, _address):
                raise RuntimeError("wrong cached resolver used")

            def close(self):
                pass

        class RuntimeResolver:
            def __init__(self, **_kwargs):
                self.closed = False

            def resolve(self, address):
                return {
                    "address": address,
                    "province": "浙江省",
                    "city": "宁波市",
                    "county": "象山县",
                    "town": "丹东街道",
                    "destination_name": "浙江宁波象山丹西公司一分部",
                    "destination_code": "5740252",
                    "destination_center_name": "宁波分拨",
                    "destination_center_code": "57401",
                    "dispatch_site_name": "浙江宁波象山丹西公司一分部",
                    "dispatch_site_code": "5740252",
                }

            def close(self):
                self.closed = True

        cached_module = types.ModuleType("browser_address_resolver")
        cached_module.BrowserAddressResolver = WrongCachedResolver
        original_module = sys.modules.get("browser_address_resolver")
        old_resolver = ronghui_get_price._BROWSER_RESOLVER
        old_key = ronghui_get_price._BROWSER_RESOLVER_KEY
        ronghui_get_price._BROWSER_RESOLVER = None
        ronghui_get_price._BROWSER_RESOLVER_KEY = ""
        sys.modules["browser_address_resolver"] = cached_module

        try:
            with (
                patch.object(runtime_resolver, "BrowserAddressResolver", RuntimeResolver),
                patch.object(
                    ronghui_get_price,
                    "_post_json_list",
                    return_value=[
                        {
                            "DESTINATION_CODE": "5740252",
                            "DESTINATION_NAME": "浙江宁波象山丹西公司一分部",
                            "DISPATCH_UNDERLING_SITE_CODE": "5740252",
                            "DISPATCH_UNDERLING_SITE": "浙江宁波象山丹西公司一分部",
                        }
                    ],
                ),
            ):
                result = ronghui_get_price.resolve_address_destination(
                    object(),
                    "宁波市象山县丹东街道丹峰东路63号三楼",
                    {},
                    "",
                    "",
                )
        finally:
            resolver = ronghui_get_price._BROWSER_RESOLVER
            if resolver is not None:
                try:
                    resolver.close()
                except Exception:
                    pass
            ronghui_get_price._BROWSER_RESOLVER = old_resolver
            ronghui_get_price._BROWSER_RESOLVER_KEY = old_key
            if original_module is None:
                sys.modules.pop("browser_address_resolver", None)
            else:
                sys.modules["browser_address_resolver"] = original_module

        self.assertEqual("5740252", result["temp_dest_code"])
        self.assertEqual("浙江宁波象山丹西公司一分部", result["dispatch"]["dispatch_site_name"])

    def test_ronghui_fetch_prices_reports_entry_page_resolver_failure(self):
        from agent.tms_runtime.scripts import get_price as ronghui_get_price

        class Auth:
            config = {"test_user_data": {}}

            def login_and_get_session(self):
                return object()

        with (
            patch.object(ronghui_get_price, "TMSAuth", return_value=Auth()),
            patch.object(
                ronghui_get_price,
                "_fetch_login_context",
                return_value={
                    "site_code": "7390004",
                    "site_name": "邵阳大祥站",
                    "emp_code": "73900040001",
                    "emp_name": "邵阳大祥站(管理员)",
                },
            ),
            patch.object(
                ronghui_get_price,
                "resolve_address_destination",
                side_effect=ronghui_get_price.PriceCalcError(
                    "browser address resolve failed: Timeout 30000ms exceeded"
                ),
            ),
        ):
            result = ronghui_get_price.fetch_prices(
                "贵州省贵阳市开阳县双流镇贵州胜泽威化工有限公司",
                3000,
                0.1,
            )

        self.assertEqual("RONGHUI_ADDRESS_RESOLVE_FAILED", result.get("error_code"))
        self.assertIn("融辉地址解析失败", result.get("error", ""))
        self.assertIn("Timeout 30000ms exceeded", result.get("address_resolution_error", ""))
        self.assertNotIn("网点不可达", result)

    def test_ronghui_price_payload_uses_entry_page_insurance_defaults(self):
        from agent.tms_runtime.scripts import get_price as ronghui_get_price

        payload = ronghui_get_price._build_base_payload(
            ctx={"site_code": "7390004", "site_name": "邵阳大祥站"},
            addr_info={
                "province": "内蒙古自治区",
                "city": "呼伦贝尔市",
                "county": "满洲里市",
                "town": "",
            },
            destination={
                "DESTINATION_CODE": "1507811",
                "DESTINATION_NAME": "满洲里站",
                "DESTINATION_CENTER_CODE": "151",
                "DESTINATION_CENTER": "齐市新操作场",
            },
            dispatch={
                "dispatch_site_code": "1507811",
                "dispatch_site_name": "满洲里站",
                "dispatch_finance_center": "",
                "dispatch_finance_center_code": "",
            },
            address="内蒙古满洲里市富豪城小区6号楼6号门市",
            weight=33,
            volume=0.1,
            volume_weight=20,
            settlement_weight=33,
            emp_code="73900040001",
            emp_name="邵阳大祥站(管理员)",
        )

        self.assertEqual("3000", payload["INSURANCE"])
        self.assertEqual("3", payload["INSURANCE_FEE"])

    def test_ronghui_price_sum_matches_entry_page_total_with_insurance_fee(self):
        from agent.tms_runtime.scripts import get_price as ronghui_get_price

        total = ronghui_get_price._calc_sum_fee(
            {
                "TRANSPORT_FEE": "47",
                "TRANSPORT_FEE_DIS": "7.05",
                "REC_DISPATCH_FEE": "30",
                "REC_DISPATCH_FEE_DIS": "15",
                "OPERATE_FEE": "1.98",
                "PERIOD_FEE": "3",
                "TARIFF_FEE": "5",
                "INSURANCE": "3000",
                "INSURANCE_FEE": "3",
                "TRANSFER_FEE": "42.51",
                "REC_SHORTHAUL_FEE": "40.5",
            }
        )

        self.assertEqual(Decimal("150.94"), total)

    def test_browser_address_resolver_triggers_miniui_blur_with_page_context(self):
        from agent.tms_runtime.scripts import browser_address_resolver

        class FakeLocator:
            def __init__(self):
                self.fills = []
                self.blurs = 0

            def fill(self, value):
                self.fills.append(value)

            def blur(self):
                self.blurs += 1

        class FakePage:
            def wait_for_function(self, script, timeout=None):
                return None

        class FakeFrame:
            def __init__(self):
                self.evaluations = []
                self.waits = []
                self.locators = {}

            def evaluate(self, script, arg=None):
                self.evaluations.append((script, arg))
                if "$Z.user.getUserInfo" in script:
                    return {"has_user_info": True, "has_site_levels": True}
                return None

            def wait_for_function(self, script, arg=None, timeout=None):
                self.waits.append((script, arg, timeout))

            def locator(self, selector):
                locator = self.locators.get(selector)
                if locator is None:
                    locator = FakeLocator()
                    self.locators[selector] = locator
                return locator

        resolver = browser_address_resolver.BrowserAddressResolver()
        resolver._page = FakePage()
        resolver._frame = FakeFrame()
        resolver._read_values = lambda: {
            "address": "浙江省宁波市镇海区招宝山街道威海路1188号2楼A库康特恩仓库",
            "province": "浙江省",
            "city": "宁波市",
            "county": "镇海区",
            "town": "招宝山街道",
            "destination_name": "蟹浦后海塘站",
            "destination_code": "5740252",
            "destination_center_name": "宁波分拨",
            "destination_center_code": "57401",
            "dispatch_site_name": "蟹浦后海塘站",
            "dispatch_site_code": "5740252",
        }

        with patch.object(
            resolver,
            "_load_page_user_info",
            return_value={
                "loginEmpName": "邵阳大祥站(管理员)",
                "loginEmpCode": "73900040001",
                "loginSiteName": "邵阳大祥站",
                "loginSiteCode": "7390004",
                "token": "must-not-be-injected",
            },
            create=True,
        ):
            result = resolver._resolve_once("浙江省宁波市镇海区招宝山街道威海路1188号2楼A库康特恩仓库")

        combined_js = "\n".join(script for script, _arg in resolver._frame.evaluations)
        self.assertEqual("蟹浦后海塘站", result["destination_name"])
        self.assertIn("$Z.user.getUserInfo", combined_js)
        self.assertIn("loginEmpName", combined_js)
        self.assertIn("mergedUserInfo", combined_js)
        self.assertIn("SITE_LEVELS", combined_js)
        self.assertIn("LEVELS", combined_js)
        self.assertIn("L.icon", combined_js)
        self.assertIn("editableLayers", combined_js)
        self.assertIn("mini.get('ACCEPT_MAN_ADDRESS')", combined_js)
        self.assertIn("fire('blur'", combined_js)
        self.assertNotIn("must-not-be-injected", combined_js)
        self.assertTrue(any(arg == "浙江省宁波市镇海区招宝山街道威海路1188号2楼A库康特恩仓库" for _script, arg in resolver._frame.evaluations))
        wait_js = "\n".join(script for script, _arg, _timeout in resolver._frame.waits)
        self.assertIn("DESTINATION_CODE$value", wait_js)
        self.assertIn("DISPATCH_UNDERLING_SITE_CODE$value", wait_js)

    def test_browser_address_resolver_fails_when_site_levels_selection_missing(self):
        from agent.tms_runtime.scripts import browser_address_resolver

        class FakeFrame:
            def evaluate(self, script, arg=None):
                return {
                    "has_user_info": True,
                    "has_site_levels": False,
                    "site_levels_error": "SITE_LEVELS selection missing",
                }

        resolver = browser_address_resolver.BrowserAddressResolver()
        resolver._frame = FakeFrame()

        with (
            patch.object(
                resolver,
                "_load_page_user_info",
                return_value={
                    "loginEmpName": "邵阳大祥站(管理员)",
                    "loginEmpCode": "73900040001",
                    "loginSiteType": "一级网点",
                },
                create=True,
            ),
            self.assertRaisesRegex(RuntimeError, "SITE_LEVELS selection missing"),
        ):
            resolver._prepare_entry_page_context()

    def test_browser_address_resolver_injects_missing_site_levels_control(self):
        from agent.tms_runtime.scripts import browser_address_resolver

        class FakeFrame:
            def __init__(self):
                self.evaluations = []

            def evaluate(self, script, arg=None):
                self.evaluations.append((script, arg))
                if "createSyntheticSiteLevelsControl" in script:
                    return {"has_user_info": True, "has_site_levels": True}
                return {
                    "has_user_info": True,
                    "has_site_levels": False,
                    "site_levels_error": "SITE_LEVELS control missing",
                }

        resolver = browser_address_resolver.BrowserAddressResolver()
        resolver._frame = FakeFrame()

        with patch.object(
            resolver,
            "_load_page_user_info",
            return_value={
                "loginEmpName": "邵阳大祥站(管理员)",
                "loginEmpCode": "73900040001",
                "loginSiteType": "一级网点",
            },
            create=True,
        ):
            resolver._prepare_entry_page_context()

        combined_js = "\n".join(script for script, _arg in resolver._frame.evaluations)
        self.assertIn("createSyntheticSiteLevelsControl", combined_js)

    def test_browser_address_resolver_accepts_site_levels_label_value_rows(self):
        from agent.tms_runtime.scripts import browser_address_resolver

        resolver = browser_address_resolver.BrowserAddressResolver()
        source = inspect.getsource(resolver._prepare_entry_page_context)

        self.assertIn("normalizeSiteLevelsRow", source)
        self.assertIn("row.label", source)
        self.assertIn("row = normalizeSiteLevelsRow(row);", source)

    def test_browser_address_resolver_resolve_runs_outside_async_loop(self):
        import threading

        from agent.tms_runtime.scripts import browser_address_resolver

        calls = []
        resolver = browser_address_resolver.BrowserAddressResolver()

        def fake_ensure_ready():
            try:
                asyncio.get_running_loop()
                loop_running = True
            except RuntimeError:
                loop_running = False
            calls.append(("ensure", threading.get_ident(), loop_running))

        def fake_resolve_once(address):
            try:
                asyncio.get_running_loop()
                loop_running = True
            except RuntimeError:
                loop_running = False
            calls.append(("resolve", threading.get_ident(), loop_running))
            return {
                "address": address,
                "province": "浙江省",
                "city": "宁波市",
                "county": "象山县",
                "town": "丹东街道",
                "destination_name": "浙江宁波象山丹西公司一分部",
                "destination_code": "5740252",
                "destination_center_name": "宁波分拨",
                "destination_center_code": "57401",
                "dispatch_site_name": "浙江宁波象山丹西公司一分部",
                "dispatch_site_code": "5740252",
            }

        async def run_in_loop():
            return resolver.resolve("宁波市象山县丹东街道丹峰东路63号三楼")

        try:
            with (
                patch.object(resolver, "_ensure_ready", side_effect=fake_ensure_ready),
                patch.object(resolver, "_resolve_once", side_effect=fake_resolve_once),
            ):
                result = asyncio.run(run_in_loop())
        finally:
            resolver.close()

        self.assertEqual(result["destination_code"], "5740252")
        self.assertTrue(calls)
        self.assertEqual({call[1] for call in calls}, {calls[0][1]})
        self.assertFalse(any(call[2] for call in calls))

    def test_legacy_browser_address_resolver_resolve_runs_outside_async_loop(self):
        import threading

        module_path = (
            Path(__file__).resolve().parents[1]
            / "price_scripts"
            / "scripts"
            / "02_tms_price_fetch"
            / "browser_address_resolver.py"
        )
        spec = importlib.util.spec_from_file_location("_legacy_browser_address_resolver_for_test", module_path)
        legacy_resolver_module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(legacy_resolver_module)

        calls = []
        resolver = legacy_resolver_module.BrowserAddressResolver()

        def fake_ensure_ready():
            try:
                asyncio.get_running_loop()
                loop_running = True
            except RuntimeError:
                loop_running = False
            calls.append(("ensure", threading.get_ident(), loop_running))

        def fake_resolve_once(address):
            try:
                asyncio.get_running_loop()
                loop_running = True
            except RuntimeError:
                loop_running = False
            calls.append(("resolve", threading.get_ident(), loop_running))
            return {
                "address": address,
                "province": "浙江省",
                "city": "宁波市",
                "county": "象山县",
                "town": "丹东街道",
                "destination_name": "浙江宁波象山丹西公司一分部",
                "destination_code": "5740252",
                "destination_center_name": "宁波分拨",
                "destination_center_code": "57401",
                "dispatch_site_name": "浙江宁波象山丹西公司一分部",
                "dispatch_site_code": "5740252",
            }

        async def run_in_loop():
            return resolver.resolve("宁波市象山县丹东街道丹峰东路63号三楼")

        try:
            with (
                patch.object(resolver, "_ensure_ready", side_effect=fake_ensure_ready),
                patch.object(resolver, "_resolve_once", side_effect=fake_resolve_once),
            ):
                result = asyncio.run(run_in_loop())
        finally:
            resolver.close()

        self.assertEqual(result["destination_code"], "5740252")
        self.assertTrue(calls)
        self.assertEqual({call[1] for call in calls}, {calls[0][1]})
        self.assertFalse(any(call[2] for call in calls))

    def test_tms_browser_auth_uses_configured_shared_session_profile(self):
        page = _FakePage(
            "https://tms.ronghuiwl.com/system/login",
            "https://tms.ronghuiwl.com/module/index?mv=index",
        )
        auth = browser_manager.TMSBrowserAuth(
            home_url="https://tms.ronghuiwl.com/module/index?mv=index",
            profile="price",
        )
        calls: list[str] = []

        class Broker:
            def ensure_authenticated(self, validate=True):
                calls.append(f"validate={validate}")

        with patch("browser_manager.get_session_broker", return_value=Broker()) as get_broker:
            auth.login(page, username="", password="")

        get_broker.assert_called_once_with("price")
        self.assertEqual(["validate=True"], calls)

    def test_dispatch_load_callable_prefers_runtime_scripts_dir(self):
        from agent.tms_runtime import dispatch

        fake = types.ModuleType("get_price")
        fake.__file__ = str(Path(tempfile.gettempdir()) / "get_price.py")
        fake.run_once = lambda params: {"wrong": True}
        original = sys.modules.get("get_price")
        sys.modules["get_price"] = fake
        try:
            fn = dispatch._load_callable(dispatch.TARGETS["get_price"])
            loaded_file = Path(sys.modules[fn.__module__].__file__).resolve()
            self.assertTrue(loaded_file.is_relative_to(SCRIPTS_DIR))
            self.assertEqual("agent.tms_runtime.scripts.get_price", fn.__module__)
            self.assertIsNot(fn, fake.run_once)
        finally:
            if original is None:
                sys.modules.pop("get_price", None)
            else:
                sys.modules["get_price"] = original

    def test_feishu_admin_base_ignores_http_service_url(self):
        with patch.dict(
            "os.environ",
            {
                "HTTP_SERVICE_URL": "http://legacy-service:8000/tms",
                "AGENT_PORT": "9100",
            },
            clear=False,
        ):
            self.assertEqual(message_handler._admin_base_url(), "http://127.0.0.1:9100")

    def test_feishu_admin_base_allows_explicit_agent_admin_url(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_ADMIN_BASE_URL": "http://agent.internal:9000/tms",
                "HTTP_SERVICE_URL": "http://legacy-service:8000/tms",
            },
            clear=False,
        ):
            self.assertEqual(message_handler._admin_base_url(), "http://agent.internal:9000")

    def test_r7_login_uses_independent_browser_auth(self):
        auth = r7_login.build_auth(max_attempts=2)
        page = _FakeIndependentLoginPage(r7_login.LOGIN_URL, r7_login.HOME_URL)

        with patch("browser_manager.get_session_broker", side_effect=AssertionError("TMS session should not be used")):
            auth.login(page, username="r7-user", password="r7-pass")

        self.assertFalse(auth.use_shared_session)
        self.assertEqual(r7_login.HOME_URL, page.url)
        self.assertIn("r7-user", page.filled.values())
        self.assertIn("r7-pass", page.filled.values())
        self.assertTrue(page.clicked)

    def test_r7_ensure_logged_in_prefers_http_sso_browser_state(self):
        class _FakeR7SSOAuth:
            last_instance = None

            def __init__(self, *args, **kwargs):
                self.calls = []
                self.last_token = ""
                _FakeR7SSOAuth.last_instance = self

            def login_and_get_session(self, **kwargs):
                self.calls.append(kwargs)
                self.last_token = "header.payload.signature"
                return _FakeR7Session()

        auth = r7_login.build_auth(max_attempts=2)
        page = _FakeR7HttpLoginPage(r7_login.LOGIN_URL, r7_login.HOME_URL)

        with (
            patch("r7_login.R7SSOAuth", _FakeR7SSOAuth),
            patch.object(auth, "login", side_effect=AssertionError("browser fallback should not be used")),
        ):
            r7_login.ensure_logged_in(page, auth, username="r7-user", password="r7-pass")

        self.assertEqual(r7_login.HOME_URL, page.url)
        self.assertEqual("header.payload.signature", page.evaluated[0][1])
        self.assertEqual("r7-session", page.context.cookies[0]["name"])
        self.assertEqual("r7.ronghuiwl.com", page.context.cookies[0]["domain"])
        self.assertEqual("Lax", page.context.cookies[0]["sameSite"])
        self.assertEqual("r7-user", _FakeR7SSOAuth.last_instance.calls[0]["username"])
        self.assertEqual("r7-pass", _FakeR7SSOAuth.last_instance.calls[0]["password"])
        self.assertFalse(auth.use_shared_session)

    def test_r7_ensure_logged_in_falls_back_to_browser_login(self):
        class _FailingR7SSOAuth:
            last_token = ""

            def login_and_get_session(self, **kwargs):
                raise RuntimeError("http login unavailable")

        auth = r7_login.build_auth(max_attempts=2)
        page = _FakeIndependentLoginPage(r7_login.LOGIN_URL, r7_login.HOME_URL)

        with patch("r7_login.R7SSOAuth", _FailingR7SSOAuth):
            r7_login.ensure_logged_in(page, auth, username="r7-user", password="r7-pass")

        self.assertEqual(r7_login.HOME_URL, page.url)
        self.assertIn("r7-user", page.filled.values())
        self.assertIn("r7-pass", page.filled.values())
        self.assertTrue(page.clicked)

    def test_r7_ensure_logged_in_reports_http_and_browser_failures(self):
        class _FailingR7SSOAuth:
            last_token = ""

            def login_and_get_session(self, **kwargs):
                raise RuntimeError("http login unavailable")

        auth = r7_login.build_auth(max_attempts=2)
        page = _FakeIndependentLoginPage(r7_login.LOGIN_URL, r7_login.HOME_URL)

        with (
            patch("r7_login.R7SSOAuth", _FailingR7SSOAuth),
            patch.object(auth, "login", side_effect=RuntimeError("browser login unavailable")),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                r7_login.ensure_logged_in(page, auth, username="r7-user", password="r7-pass")

        message = str(ctx.exception)
        self.assertIn("HTTP SSO failed", message)
        self.assertIn("http login unavailable", message)
        self.assertIn("browser fallback failed", message)
        self.assertIn("browser login unavailable", message)

    def test_launch_browser_can_skip_tms_storage_state_for_r7(self):
        context = _FakeLaunchContext()
        browser = _FakeLaunchBrowser(context)
        sync_api_module = types.ModuleType("playwright.sync_api")
        sync_api_module.sync_playwright = lambda: _FakeLaunchPlaywright(browser)
        playwright_module = types.ModuleType("playwright")
        playwright_module.sync_api = sync_api_module

        with (
            patch.dict(sys.modules, {"playwright": playwright_module, "playwright.sync_api": sync_api_module}),
            patch("browser_manager.get_session_broker", side_effect=AssertionError("TMS storage should not be used")),
        ):
            browser_manager.launch_browser(use_tms_storage_state=False)

        self.assertIsInstance(context.kwargs, dict)
        self.assertNotIn("storage_state", context.kwargs)
        self.assertEqual({"width": 1440, "height": 900}, context.kwargs["viewport"])

    def test_launch_browser_uses_configured_storage_state_profile(self):
        context = _FakeLaunchContext()
        browser = _FakeLaunchBrowser(context)
        sync_api_module = types.ModuleType("playwright.sync_api")
        sync_api_module.sync_playwright = lambda: _FakeLaunchPlaywright(browser)
        playwright_module = types.ModuleType("playwright")
        playwright_module.sync_api = sync_api_module

        class Broker:
            def get_storage_state_path(self, validate=True):
                return "/tmp/price-storage.json"

        with (
            patch.dict(sys.modules, {"playwright": playwright_module, "playwright.sync_api": sync_api_module}),
            patch("browser_manager.get_session_broker", return_value=Broker()) as get_broker,
        ):
            browser_manager.launch_browser(profile="price")

        get_broker.assert_called_once_with("price")
        self.assertEqual("/tmp/price-storage.json", context.kwargs["storage_state"])

    def test_tms_tool_does_not_double_wrap_task_request_payload(self):
        captured: dict[str, Any] = {}

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        def _fake_post(url, json, timeout, headers):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            captured["headers"] = headers
            return _Response()

        with patch("tools.tms_tool.httpx.post", side_effect=_fake_post):
            result = tms_tool.call_http_service(
                "/query_waybill_detail",
                {
                    "params": {"bill_codes": ["R0001"]},
                    "timeout_sec": 900,
                    "client_timeout_sec": 960,
                },
            )

        self.assertEqual({"ok": True}, result)
        self.assertIn("X-Agent-Internal-Token", captured["headers"])
        self.assertEqual({"bill_codes": ["R0001"]}, captured["json"]["params"])
        self.assertNotIn("params", captured["json"]["params"])
        self.assertEqual(960, captured["json"]["timeout_sec"])

    def test_tms_tool_preserves_json_http_error_payload(self):
        request = tms_tool.httpx.Request("POST", "http://127.0.0.1:9000/tms/get_qianshou")
        response = tms_tool.httpx.Response(
            500,
            json={"ok": False, "error_type": "RuntimeError", "error": "R13 SSO login failed"},
            request=request,
        )

        class _Response:
            def raise_for_status(self):
                raise tms_tool.httpx.HTTPStatusError("server error", request=request, response=response)

        with patch("tools.tms_tool.httpx.post", return_value=_Response()):
            result = tms_tool.call_http_service("/get_qianshou", {})

        self.assertFalse(result["ok"])
        self.assertEqual(500, result["http_status"])
        self.assertEqual("R13 SSO login failed", result["error"])

    def test_get_qianshou_forwards_account_key_to_r13_auth(self):
        captured: dict[str, Any] = {}

        def _fake_fetch(**kwargs):
            captured.update(kwargs)
            return []

        with patch("get_qianshou.fetch_qianshou", side_effect=_fake_fetch):
            result = get_qianshou.run_once({"accountKey": "r13", "dispSiteCode": "7390004"})

        self.assertEqual([], result)
        self.assertEqual("r13", captured["account_key"])

    def test_registry_removed_trigger_n8n_and_contains_sync_tools(self):
        registry_path = Path(__file__).resolve().parents[1] / "tools" / "registry.yaml"
        registry_text = registry_path.read_text(encoding="utf-8")
        self.assertNotIn("trigger_n8n", registry_text)
        self.assertIn("sync_arrive_list", registry_text)
        self.assertIn("sync_scan_codes", registry_text)
        self.assertIn("sync_arrival_stats", registry_text)
        self.assertIn("sync_yunda_dispatch_forecast", registry_text)
        self.assertIn("sync_yunda_send_waybills", registry_text)
        self.assertIn("init_waybills_sql_from_feishu", registry_text)
        self.assertIn("track_waybill", registry_text)
        self.assertIn("automation_profile", registry_text)
        self.assertIn("r7_arrival_checkin", registry_text)
        self.assertIn("r7_departure_checkin", registry_text)
        self.assertIn("sql_only", registry_text)
        self.assertIn("sync_sql", registry_text)

    def test_direct_router_maps_arrival_checkin_command_to_r7_tool(self):
        request = direct_tool_router.direct_tool_request_from_text("到达打卡")

        self.assertIsNotNone(request)
        self.assertEqual("r7_arrival_checkin", request["tool_name"])
        self.assertEqual({}, request["params"])
        self.assertEqual("deferred", request["mode"])

    def test_direct_router_maps_arrive_list_command_to_sync_tool(self):
        for text in ("执行一次arrivelist脚本", "同步到货清单", "拉取预到达清单", "arrive-list"):
            with self.subTest(text=text):
                request = direct_tool_router.direct_tool_request_from_text(text)

                self.assertIsNotNone(request)
                self.assertEqual("sync_arrive_list", request["tool_name"])
                self.assertEqual({}, request["params"])
                self.assertEqual("deferred", request["mode"])

    def test_direct_router_maps_self_pickup_problem_command_to_preview(self):
        for text in (
            "自提到货问题件",
            "自提部到货问题件",
            "自提部到货问题件上传",
            "开单为自提件问题件",
            "大祥S站自提问题件上传",
        ):
            with self.subTest(text=text):
                request = direct_tool_router.direct_tool_request_from_text(text)

                self.assertIsNotNone(request)
                self.assertEqual("self_pickup_problem_upload", request["tool_name"])
                self.assertEqual(
                    {"dry_run": True, "account_id": "ronghui_self_pickup_problem"},
                    request["params"],
                )
                self.assertEqual("reply", request["mode"])
                self.assertEqual(
                    {"dry_run": False, "account_id": "ronghui_self_pickup_problem"},
                    request["confirm_intent"]["execute_params"],
                )

    def test_self_pickup_problem_preview_reply_hides_account_and_session_names(self):
        reply = direct_tool_router.format_tool_reply(
            "self_pickup_problem_upload",
            {
                "success": True,
                "data": {
                    "stage": "dry_run",
                    "candidate_count": 2,
                    "source": {"sheet_title": "每日到货表"},
                    "screenshot_enabled": False,
                    "source_summaries": [
                        {
                            "source_name": "邵阳自提部",
                            "candidate_count": 1,
                            "account_id": "ronghui_self_pickup_problem",
                            "session_profile": "self_pickup_problem_upload",
                            "candidates": [{"bill_code": "R0001"}],
                        },
                        {
                            "source_name": "邵阳大祥S站自提",
                            "candidate_count": 1,
                            "account_id": "ronghui_daxiang_s",
                            "session_profile": "daxiang_s",
                            "candidates": [{"bill_code": "R0002"}],
                        },
                    ],
                },
            },
        )

        self.assertIn("邵阳自提部：1 单", reply)
        self.assertIn("邵阳大祥S站自提：1 单", reply)
        self.assertIn("R0001", reply)
        self.assertIn("R0002", reply)
        self.assertNotIn("账号", reply)
        self.assertNotIn("登录态", reply)
        self.assertNotIn("ronghui_self_pickup_problem", reply)
        self.assertNotIn("self_pickup_problem_upload", reply)
        self.assertNotIn("ronghui_daxiang_s", reply)
        self.assertNotIn("daxiang_s", reply)

    def test_direct_router_recognizes_login_send_code_intent(self):
        for text in ("登录", "登陆", "发验证码", "重新登录", "登录态验证", "TMS发验证码"):
            with self.subTest(text=text):
                self.assertEqual("choice", direct_tool_router.parse_login_send_code_session(text))

        for text in ("大祥登录", "报价登录", "价格发验证码", "price验证码"):
            with self.subTest(text=text):
                self.assertEqual("price", direct_tool_router.parse_login_send_code_session(text))

        for text in ("操作场登录", "后台发验证码", "后台保存账号登录"):
            with self.subTest(text=text):
                self.assertEqual("default", direct_tool_router.parse_login_send_code_session(text))

        for text in ("韵达登录", "韵达发验证码", "yunda验证码"):
            with self.subTest(text=text):
                self.assertEqual("yunda", direct_tool_router.parse_login_send_code_session(text))

        for text in ("1", "大祥账号", "报价账号"):
            with self.subTest(text=text):
                self.assertEqual("price", direct_tool_router.parse_login_account_choice(text))

        for text in ("2", "操作场账号", "后台保存账号"):
            with self.subTest(text=text):
                self.assertEqual("default", direct_tool_router.parse_login_account_choice(text))

        for text in ("3", "韵达账号", "yunda"):
            with self.subTest(text=text):
                self.assertEqual("yunda", direct_tool_router.parse_login_account_choice(text))

        self.assertIsNone(direct_tool_router.parse_login_send_code_session("执行一次arrivelist脚本"))

    def test_direct_router_accepts_alphanumeric_image_codes(self):
        self.assertEqual("a1B2", direct_tool_router.parse_verify_code(" a1B2 "))
        self.assertEqual("123456", direct_tool_router.parse_verify_code("123456"))
        self.assertIsNone(direct_tool_router.parse_verify_code("验证码 a1B2"))

    def test_direct_router_maps_automation_profile_commands(self):
        switch_request = direct_tool_router.direct_tool_request_from_text("切换到韵达自动化")
        self.assertIsNotNone(switch_request)
        self.assertEqual("automation_profile", switch_request["tool_name"])
        self.assertEqual({"action": "set", "profile": "yunda"}, switch_request["params"])

        status_request = direct_tool_router.direct_tool_request_from_text("当前自动化状态")
        self.assertIsNotNone(status_request)
        self.assertEqual("automation_profile", status_request["tool_name"])
        self.assertEqual({"action": "get"}, status_request["params"])

    def test_direct_router_maps_yunda_dispatch_forecast_command(self):
        request = direct_tool_router.direct_tool_request_from_text("韵达网点派件量预测主单表")

        self.assertIsNotNone(request)
        self.assertEqual("sync_yunda_dispatch_forecast", request["tool_name"])
        self.assertEqual({"session_profile": "yunda"}, request["params"])
        self.assertEqual("deferred", request["mode"])

    def test_direct_router_maps_yunda_send_waybills_command(self):
        request = direct_tool_router.direct_tool_request_from_text("韵达寄件运单管理")

        self.assertIsNotNone(request)
        self.assertEqual("sync_yunda_send_waybills", request["tool_name"])
        self.assertEqual({"session_profile": "yunda"}, request["params"])
        self.assertEqual("deferred", request["mode"])

        range_request = direct_tool_router.direct_tool_request_from_text(
            "韵达寄件运单同步从2026年5月6日到2026年5月16日"
        )
        self.assertIsNotNone(range_request)
        self.assertEqual("sync_yunda_send_waybills", range_request["tool_name"])
        self.assertEqual(
            {"session_profile": "yunda", "start_date": "2026-05-06", "end_date": "2026-05-16"},
            range_request["params"],
        )

    def test_direct_router_maps_send_order_range_command(self):
        request = direct_tool_router.direct_tool_request_from_text(
            "获取当日寄件数据从2026年5月6日到2026年5月16日"
        )

        self.assertIsNotNone(request)
        self.assertEqual("sync_daily_send_orders", request["tool_name"])
        self.assertEqual({"start_date": "2026-05-06", "end_date": "2026-05-16"}, request["params"])
        self.assertEqual("deferred", request["mode"])

    def test_direct_router_maps_tracking_commands(self):
        checks = [
            ("977808459", {"tracking_number": "977808459", "provider": "yunda"}),
            ("查物流 977808459", {"tracking_number": "977808459", "provider": "yunda"}),
            ("查单号 R00014513348", {"tracking_number": "R00014513348", "provider": "ronghui"}),
            ("查运单 000123456", {"tracking_number": "000123456", "provider": "zhuanxian"}),
        ]
        for text, params in checks:
            with self.subTest(text=text):
                request = direct_tool_router.direct_tool_request_from_text(text)
                self.assertIsNotNone(request)
                self.assertEqual("track_waybill", request["tool_name"])
                self.assertEqual(params, request["params"])
                self.assertEqual("reply", request["mode"])

    def test_direct_router_returns_local_error_for_invalid_r_tracking_number(self):
        request = direct_tool_router.direct_tool_request_from_text("R000016211453")

        self.assertIsNotNone(request)
        self.assertEqual("track_waybill", request["tool_name"])
        self.assertEqual("reply", request["mode"])
        self.assertEqual({"tracking_number": "R000016211453", "provider": "ronghui"}, request["params"])
        self.assertEqual(
            {
                "success": False,
                "error": "单号格式错误：R 开头融辉单号应为 R+11位主单或 R+15位子单，请检查是否多输/少输数字。",
                "error_code": "INVALID_TRACKING_NUMBER",
            },
            request["local_result"],
        )

    def test_track_waybill_reply_formats_routes_newest_first(self):
        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "yunda",
                    "tracking_number": "977808459",
                    "route_rows": [
                        {
                            "scan_time": "2026-05-10 17:28:54",
                            "status": "揽收",
                            "description": "快件在【湖南邵阳双清滨江公司】已揽件开单",
                            "scan_station": "湖南邵阳双清滨江公司",
                            "contact": "湖南邵阳双清滨江公司：0739-1111111",
                        },
                        {
                            "scan_time": "2026-05-12 13:11:45",
                            "status": "签收",
                            "description": "快件已被客户【指定位置】签收",
                            "scan_station": "客户指定位置",
                            "contact": "客户指定位置：无",
                        },
                    ],
                    "waybill_stub": {
                        "pieces": "2 件",
                        "disp_site": "湖南邵阳双清滨江公司",
                        "delivery_method": "自提",
                        "recipient_name": "张三",
                        "recipient_phone": "13800000000",
                    },
                },
            },
        )

        self.assertIn("查询单号：977808459", reply)
        self.assertLess(reply.index("最新路由："), reply.index("最初开单路由："))
        self.assertIn("网点信息：客户指定位置", reply)
        self.assertIn("扫描时间：2026-05-12 13:11:45", reply)
        self.assertIn("路由信息：快件已被客户【指定位置】签收", reply)
        self.assertIn("网点信息：湖南邵阳双清滨江公司", reply)
        self.assertIn("扫描时间：2026-05-10 17:28:54", reply)
        self.assertIn("货物件数：2 件", reply)
        self.assertIn("目的站点：湖南邵阳双清滨江公司", reply)
        self.assertIn("派送方式：自提", reply)
        self.assertIn("收货人：张三 13800000000", reply)
        self.assertNotIn("开单件数：", reply)

    def test_track_waybill_reply_replaces_yunda_voucher_segment_with_contact(self):
        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "yunda",
                    "tracking_number": "978810106",
                    "route_rows": [
                        {
                            "scan_time": "2026-05-22 01:35:14",
                            "status": "到达",
                            "description": (
                                "快件在【辽宁沈阳分拨中心】正发往【吉林长春分拨中心】扫描员是【沈建】  "
                                "凭证号:56011489523;线路名称:沈阳ZZ-长春ZZ;预计发车:2026-05-22 09:00:00;"
                                "预计到达:2026-05-22 14:10:00;实际发车:2026-05-22 07:00:43;实际到达: "
                            ),
                            "contact": "辽宁沈阳分拨中心：分拨经理【李东伟】；分拨客服电话【024-89512469】",
                        }
                    ],
                },
            },
        )

        self.assertIn(
            "路由信息：快件在【辽宁沈阳分拨中心】正发往【吉林长春分拨中心】扫描员是【沈建】",
            reply,
        )
        self.assertIn("货物跟踪查询电话：辽宁沈阳分拨中心：分拨经理【李东伟】；分拨客服电话【024-89512469】", reply)
        self.assertNotIn("凭证号", reply)
        self.assertNotIn("线路名称", reply)

    def test_track_waybill_reply_expands_yunda_problem_routes_until_network_handoff(self):
        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "yunda",
                    "tracking_number": "980392474",
                    "route_rows": [
                        {
                            "scan_time": "2026-05-30 18:23:01",
                            "status": "揽收",
                            "description": "快件在【湖南邵阳双清滨江公司】已揽件开单",
                            "scan_station": "湖南邵阳双清滨江公司",
                        },
                        {
                            "scan_time": "2026-06-02 03:03:41",
                            "status": "发件扫描",
                            "description": "快件在【江西南昌分拨中心】正发往【江西九江修水公司】扫描员是【邹循峰】",
                            "scan_station": "江西南昌分拨中心",
                        },
                        {
                            "scan_time": "2026-06-03 07:59:11",
                            "status": "问题",
                            "description": "【江西九江修水公司】已进行【问题】扫描【问题】原因【分拨/网点/乡镇自提】备注【无标签，请问是贵司货物不？】",
                            "scan_station": "江西九江修水公司",
                        },
                        {
                            "scan_time": "2026-06-03 14:19:30",
                            "status": "问题",
                            "description": "【湖南邵阳双清滨江公司】已进行【问题】扫描【问题】原因【运单调整审核】备注【目的地址信息变更】",
                            "scan_station": "湖南邵阳双清滨江公司",
                        },
                    ],
                },
            },
        )

        latest_index = reply.index("路由信息：【湖南邵阳双清滨江公司】已进行【问题】扫描")
        previous_problem_index = reply.index("路由信息：【江西九江修水公司】已进行【问题】扫描")
        handoff_index = reply.index("路由信息：快件在【江西南昌分拨中心】正发往【江西九江修水公司】")
        opening_index = reply.index("最初开单路由：")
        self.assertLess(latest_index, previous_problem_index)
        self.assertLess(previous_problem_index, handoff_index)
        self.assertLess(handoff_index, opening_index)
        self.assertIn("前序路由1：", reply)
        self.assertIn("前序路由2：", reply)

    def test_track_waybill_reply_keeps_single_yunda_latest_route_when_handoff_is_latest(self):
        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "yunda",
                    "tracking_number": "980392474",
                    "route_rows": [
                        {
                            "scan_time": "2026-05-30 18:23:01",
                            "status": "揽收",
                            "description": "快件在【湖南邵阳双清滨江公司】已揽件开单",
                            "scan_station": "湖南邵阳双清滨江公司",
                        },
                        {
                            "scan_time": "2026-06-03 07:59:11",
                            "status": "问题",
                            "description": "【江西九江修水公司】已进行【问题】扫描【问题】原因【分拨/网点/乡镇自提】",
                            "scan_station": "江西九江修水公司",
                        },
                        {
                            "scan_time": "2026-06-03 14:19:30",
                            "status": "发件扫描",
                            "description": "快件在【江西南昌分拨中心】正发往【江西九江修水公司】扫描员是【邹循峰】",
                            "scan_station": "江西南昌分拨中心",
                        },
                    ],
                },
            },
        )

        self.assertIn("路由信息：快件在【江西南昌分拨中心】正发往【江西九江修水公司】", reply)
        self.assertNotIn("前序路由1：", reply)
        self.assertNotIn("路由信息：【江西九江修水公司】已进行【问题】扫描", reply)

    def test_track_waybill_reply_formats_ronghui_tms_route_rows(self):
        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "ronghui_tms",
                    "tracking_number": "R00014513348",
                    "route_rows": [
                        {
                            "scan_time": "2026-05-10 19:20:58",
                            "type": "网点开单",
                            "description": "快件在【泉州德化站】完成收件扫描",
                            "scan_station": "泉州德化站",
                            "contact": "泉州德化站: 0595-1111111",
                        },
                        {
                            "scan_time": "2026-05-12 19:20:58",
                            "type": "到达",
                            "description": "快件到达【湖南邵阳集配站】",
                            "scan_station": "湖南邵阳集配站",
                            "contact": "湖南邵阳集配站: 0739-5455259",
                        }
                    ],
                    "waybill_stub": {
                        "pieces": "3 件",
                        "goods_name": "吨袋",
                        "disp_site": "萧山分拨",
                        "recipient_name": "李四",
                        "recipient_phone": "13900000000",
                        "recipient_phone_extension": "1097",
                        "recipient_address": "湖南省邵阳市双清区建设南路1号",
                    },
                    "arrival_progress": {"arrived_quantity": 2},
                },
            },
        )

        self.assertIn("查询单号：R00014513348", reply)
        self.assertIn("网点信息：湖南邵阳集配站", reply)
        self.assertIn("扫描时间：2026-05-12 19:20:58", reply)
        self.assertIn("路由信息：快件到达【湖南邵阳集配站】", reply)
        self.assertIn("货物跟踪查询电话：湖南邵阳集配站: 0739-5455259", reply)
        self.assertIn("网点信息：泉州德化站", reply)
        self.assertIn("扫描时间：2026-05-10 19:20:58", reply)
        self.assertIn("货物件数：3 件", reply)
        self.assertIn("目的站点：萧山分拨", reply)
        self.assertIn("货物名称：吨袋", reply)
        self.assertIn("收货人：李四 13900000000 分机号：1097", reply)
        self.assertIn("收货地址：湖南省邵阳市双清区建设南路1号", reply)
        self.assertIn("开单/到达：3 件 / 2 件", reply)

    def test_track_waybill_reply_uses_arrival_progress_for_ronghui_tms_arrived_count(self):
        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "ronghui_tms",
                    "tracking_number": "R00014513348",
                    "route_rows": [
                        {
                            "scan_time": "2026-05-12 19:20:58",
                            "type": "到达",
                            "description": "快件到达【湖南邵阳集配站】",
                            "scan_station": "湖南邵阳集配站",
                        }
                    ],
                    "waybill_stub": {
                        "pieces": "3 件",
                        "recipient_name": "李四",
                        "recipient_phone": "13900000000",
                    },
                    "arrival_progress": {
                        "expected_quantity": 3,
                        "arrived_quantity": 1,
                    },
                },
            },
        )

        self.assertIn("开单/到达：3 件 / 1 件", reply)

    def test_track_waybill_reply_shows_no_data_when_arrival_progress_is_missing(self):
        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "ronghui_tms",
                    "tracking_number": "R00014513348",
                    "route_rows": [
                        {
                            "scan_time": "2026-06-02 19:20:58",
                            "type": "到达",
                            "description": "快件到达【湖南邵阳集配站】",
                            "scan_station": "湖南邵阳集配站",
                        }
                    ],
                    "waybill_stub": {
                        "pieces": "3 件",
                        "recipient_name": "李四",
                        "recipient_phone": "13900000000",
                    },
                    "child_detail_rows": [{}, {}],
                },
            },
        )

        self.assertIn("开单/到达：3 件 / 无数据", reply)

    def test_track_waybill_reply_keeps_explicit_zero_arrival_count(self):
        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "ronghui_tms",
                    "tracking_number": "R00014513348",
                    "route_rows": [
                        {
                            "scan_time": "2026-06-02 19:20:58",
                            "type": "发件",
                            "description": "快件在【长沙分拨】完成发件扫描",
                            "scan_station": "长沙分拨",
                        }
                    ],
                    "waybill_stub": {"pieces": "3 件"},
                    "arrival_progress": {"arrived_quantity": 0},
                },
            },
        )

        self.assertIn("开单/到达：3 件 / 0 件", reply)

    def test_track_waybill_reply_hides_arrival_line_for_daxiang_opening_station(self):
        for opening_station in ("邵阳大祥站", "邵阳大祥S站"):
            with self.subTest(opening_station=opening_station):
                reply = direct_tool_router.format_tool_reply(
                    "track_waybill",
                    {
                        "success": True,
                        "data": {
                            "type": "ronghui_tms",
                            "tracking_number": "2003441423",
                            "route_rows": [
                                {
                                    "scan_time": "2026-05-30 12:49:31",
                                    "type": "网点开单",
                                    "description": f"快件在【{opening_station}】完成收件扫描",
                                    "scan_station": opening_station,
                                    "contact": f"{opening_station}: 0739-5186128",
                                },
                                {
                                    "scan_time": "2026-06-01 14:08:24",
                                    "type": "发件扫描",
                                    "description": "快件在【沈阳分拨】完成发件扫描",
                                    "scan_station": "沈阳分拨",
                                    "contact": "沈阳分拨: 024-31729337",
                                },
                            ],
                            "waybill_stub": {
                                "pieces": "6件",
                                "goods_name": "泵",
                                "recipient_name": "陈浩",
                                "recipient_phone": "18602419426",
                                "recipient_address": "辽宁省沈阳市皇姑区观音路20-6号",
                            },
                            "arrival_progress": {"arrived_quantity": 0},
                        },
                    },
                )

                self.assertIn(f"网点信息：{opening_station}", reply)
                self.assertIn("货物名称：泵", reply)
                self.assertIn("货物件数：6件", reply)
                self.assertNotIn("开单/到达：", reply)
                self.assertNotIn("开单件数：", reply)

    def test_track_waybill_tool_merges_waybill_cache_for_ronghui_tms_summary(self):
        with patch(
            "tools.track_waybill_tool.call_http_service",
            return_value={
                "ok": True,
                "data": {
                    "ok": True,
                    "type": "ronghui_tms",
                    "tracking_number": "R00014513348",
                    "route_rows": [
                        {
                            "scan_time": "2026-05-12 19:20:58",
                            "type": "到达",
                            "description": "快件到达【湖南邵阳集配站】",
                            "scan_station": "湖南邵阳集配站",
                        }
                    ],
                    "waybill_stub": {
                        "pieces": "3 件",
                        "recipient_name": "李**",
                    },
                },
            },
        ), patch(
            "tools.track_waybill_tool.get_waybill_tracking_cache",
            return_value={
                "tracking_number": "R00014513348",
                "goods_name": "吨袋",
                "recipient_name": "李四",
                "recipient_phone": "13900000000",
                "expected_quantity": 3,
                "arrived_quantity": 1,
            },
            create=True,
        ):
            result = track_waybill_tool.run_track_waybill({"tracking_number": "R00014513348"})

        self.assertEqual("吨袋", result["waybill_stub"]["goods_name"])
        self.assertEqual("李四", result["waybill_stub"]["recipient_name"])
        self.assertEqual("13900000000", result["waybill_stub"]["recipient_phone"])
        self.assertEqual(1, result["arrival_progress"]["arrived_quantity"])

    def test_track_waybill_tool_keeps_live_tms_arrival_over_stale_zero_cache(self):
        with patch(
            "tools.track_waybill_tool.call_http_service",
            return_value={
                "ok": True,
                "data": {
                    "ok": True,
                    "type": "ronghui_tms",
                    "tracking_number": "R00018097100",
                    "route_rows": [
                        {
                            "scan_time": "2026-07-16 12:59:10",
                            "scan_type": "卸车",
                            "description": "快件在【邵阳操作场】完成卸车",
                            "scan_station": "邵阳操作场",
                        }
                    ],
                    "waybill_stub": {"pieces": "100 件"},
                    "arrival_progress": {
                        "expected_quantity": 100,
                        "arrived_quantity": 100,
                        "pending_quantity": 0,
                        "source": "ronghui_tms_child_distribution",
                    },
                },
            },
        ), patch(
            "tools.track_waybill_tool.get_waybill_tracking_cache",
            return_value={
                "tracking_number": "R00018097100",
                "expected_quantity": 100,
                "arrived_quantity": 0,
                "pending_quantity": 100,
            },
        ):
            result = track_waybill_tool.run_track_waybill({"tracking_number": "R00018097100"})

        self.assertEqual(100, result["arrival_progress"]["arrived_quantity"])
        self.assertEqual(0, result["arrival_progress"]["pending_quantity"])
        self.assertEqual("ronghui_tms_child_distribution", result["arrival_progress"]["source"])

    def test_track_waybill_tool_recomputes_cached_derived_fields_for_live_arrival_count(self):
        with patch(
            "tools.track_waybill_tool.call_http_service",
            return_value={
                "ok": True,
                "data": {
                    "ok": True,
                    "type": "ronghui_tms",
                    "tracking_number": "R00018097100",
                    "route_rows": [],
                    "arrival_progress": {
                        "arrived_quantity": 100,
                        "source": "ronghui_tms_child_distribution",
                    },
                },
            },
        ), patch(
            "tools.track_waybill_tool.get_waybill_tracking_cache",
            return_value={
                "tracking_number": "R00018097100",
                "expected_quantity": 100,
                "arrived_quantity": 0,
                "pending_quantity": 100,
                "arrival_status": "pending",
                "first_arrival_at": "2026-07-16 12:48:00",
            },
        ):
            result = track_waybill_tool.run_track_waybill({"tracking_number": "R00018097100"})

        self.assertEqual(100, result["arrival_progress"]["arrived_quantity"])
        self.assertEqual(0, result["arrival_progress"]["pending_quantity"])
        self.assertEqual("completed", result["arrival_progress"]["arrival_status"])
        self.assertEqual("2026-07-16 12:48:00", result["arrival_progress"]["first_arrival_at"])

    def test_track_waybill_tool_fetches_detail_when_stub_recipient_is_masked(self):
        calls: list[str] = []

        def _fake_call_http_service(endpoint, params):
            calls.append(endpoint)
            if endpoint == "/tms/tracking_query":
                return {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "type": "ronghui_tms",
                        "tracking_number": "R00014513348",
                        "route_rows": [],
                        "waybill_stub": {
                            "pieces": "3 件",
                            "recipient_name": "李**",
                        },
                    },
                }
            if endpoint == "/query_waybill_detail":
                return {
                    "ok": True,
                    "items": [
                        {
                            "tracking_number": "R00014513348",
                            "recipient_name": "李四",
                            "recipient_phone": "13900000000",
                            "recipient_address": "湖南省邵阳市双清区建设南路1号",
                            "destination_station": "邵阳集配站",
                            "goods_name": "吨袋",
                            "quantity": 3,
                        }
                    ],
                }
            raise AssertionError(endpoint)

        with (
            patch("tools.track_waybill_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.track_waybill_tool.get_waybill_tracking_cache", return_value={}),
        ):
            result = track_waybill_tool.run_track_waybill({"tracking_number": "R00014513348"})

        self.assertEqual(["/tms/tracking_query", "/query_waybill_detail"], calls)
        self.assertEqual("李四", result["waybill_stub"]["recipient_name"])
        self.assertEqual("13900000000", result["waybill_stub"]["recipient_phone"])
        self.assertEqual("湖南省邵阳市双清区建设南路1号", result["waybill_stub"]["recipient_address"])
        self.assertEqual("吨袋", result["waybill_stub"]["goods_name"])
        self.assertEqual("邵阳集配站", result["waybill_stub"]["disp_site"])

    def test_track_waybill_tool_uses_feishu_arrival_sheet_when_db_cache_missing(self):
        sheet_values = [
            [
                "运单编号",
                "货物名称",
                "包装类型",
                "派送方式",
                "件数",
                "回单号",
                "实际重量",
                "体积",
                "备注",
                "目的站点",
                "收件人",
                "收件电话",
                "收件地址",
                "结算重量",
                "体积重",
                "运费",
                "支付类型",
                "到付款",
                "累计到货件数",
            ],
            ["R00014513348", "", "", "", "3", "", "", "", "", "", "李四", "13900000000", "", "", "", "", "", "", "1"],
        ]

        with (
            patch(
                "tools.track_waybill_tool.call_http_service",
                return_value={
                    "ok": True,
                    "data": {
                        "ok": True,
                        "type": "ronghui_tms",
                        "tracking_number": "R00014513348",
                        "route_rows": [
                            {
                                "scan_time": "2026-06-02 19:20:58",
                                "type": "到达",
                                "description": "快件到达【湖南邵阳集配站】",
                                "scan_station": "湖南邵阳集配站",
                            }
                        ],
                        "waybill_stub": {
                            "pieces": "3 件",
                            "recipient_name": "李四",
                            "recipient_phone": "13900000000",
                        },
                        "child_detail_rows": [{}, {}],
                    },
                },
            ),
            patch("tools.track_waybill_tool.get_waybill_tracking_cache", return_value={}),
            patch(
                "tools.track_waybill_tool.get_workflow_resource",
                side_effect=lambda key: {
                    "spreadsheet_token": "sheet-token",
                    "range": "sheet-id!A2:S200",
                }
                if key == "phase7.arrive_primary_sheet"
                else None,
            ),
            patch(
                "tools.track_waybill_tool.feishu_operation",
                return_value={"ok": True, "data": {"valueRange": {"values": sheet_values}}},
            ),
        ):
            result = track_waybill_tool.run_track_waybill({"tracking_number": "R00014513348"})

        self.assertEqual("1", result["arrival_progress"]["arrived_quantity"])
        self.assertEqual(3, result["arrival_progress"]["expected_quantity"])

    def test_track_waybill_reply_truncates_when_feishu_text_is_too_long(self):
        rows = [
            {
                "scan_time": f"2026-05-12 13:{index:02d}:45",
                "status": "装车",
                "description": "快件在【沈阳分拨】已装车，站点客服电话【02431729337】" * 240,
            }
            for index in range(80)
        ]

        reply = direct_tool_router.format_tool_reply(
            "track_waybill",
            {
                "success": True,
                "data": {
                    "type": "yunda",
                    "tracking_number": "977808459",
                    "route_rows": rows,
                },
            },
        )

        self.assertLessEqual(len(reply.encode("utf-8")), 4000)
        self.assertIn("已截断", reply)

    def test_automation_profile_tool_sets_and_reads_profile(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "automation_profile.json"
            with patch.object(automation_profile, "STATE_PATH", state_path):
                set_result = automation_profile_tool.run_automation_profile_tool(
                    {"action": "set", "profile": "yunda"}
                )
                get_result = automation_profile_tool.run_automation_profile_tool({"action": "get"})

        self.assertTrue(set_result["ok"])
        self.assertEqual("yunda", set_result["profile"])
        self.assertEqual("韵达自动化", get_result["label"])

    def test_direct_router_maps_departure_checkin_command_to_r7_tool(self):
        for text in ("发车", "R7发车", "发车打卡"):
            with self.subTest(text=text):
                request = direct_tool_router.direct_tool_request_from_text(text)

                self.assertIsNotNone(request)
                self.assertEqual("r7_departure_checkin", request["tool_name"])
                self.assertEqual({}, request["params"])
                self.assertEqual("r7_departure_choice", request["mode"])

    def test_r7_departure_expected_time_and_plate_normalization(self):
        self.assertEqual(
            "2026-04-29 21:30:00",
            auto_departure_r7.expected_departure_time(
                None,
                fixed_time="21:30:00",
                today=date(2026, 4, 29),
            ),
        )
        self.assertEqual(
            ["湘AK6980", "湘B12345", "湘C99999"],
            auto_departure_r7.normalize_plate_numbers("湘AK6980，湘B12345\n湘C99999"),
        )
        self.assertEqual(
            ["湘AK6980", "湘B12345"],
            auto_departure_r7.normalize_plate_numbers(["湘AK6980", "湘AK6980", "湘B12345"]),
        )

    def test_r7_departure_select_targets_requires_unique_plate_match(self):
        rows = [
            {
                "task_no": "RH1",
                "status": "已调度",
                "departure_time": "2026-04-29 21:30:00",
                "class_name": "邵阳操作场-长沙",
                "plate_number": "湘AK6980",
            },
            {
                "task_no": "RH2",
                "status": "已调度",
                "departure_time": "2026-04-29 21:30:00",
                "class_name": "邵阳操作场-长沙",
                "plate_number": "湘AK6980",
            },
        ]

        result = auto_departure_r7.select_departure_targets(
            rows,
            status_text="已调度",
            departure_time_text="2026-04-29 21:30:00",
            class_name="邵阳操作场-长沙",
            plate_numbers=["湘AK6980"],
        )

        self.assertFalse(result["ok"])
        self.assertEqual("target_match_failed", result["stage"])
        self.assertEqual(2, result["errors"][0]["match_count"])

    def test_r7_departure_select_targets_accepts_minute_precision_time(self):
        rows = [
            {
                "task_no": "RH1",
                "status": "已调度",
                "departure_time": "2026-04-29 21:30",
                "class_name": "邵阳操作场-长沙",
                "plate_number": "湘AK6980",
            }
        ]

        result = auto_departure_r7.select_departure_targets(
            rows,
            status_text="已调度",
            departure_time_text="2026-04-29 21:30:00",
            class_name="邵阳操作场-长沙",
            plate_numbers=["湘AK6980"],
        )

        self.assertTrue(result["ok"])
        self.assertEqual("RH1", result["targets"][0]["task_no"])

    def test_r7_departure_row_cell_text_reads_input_value(self):
        class _FakeInput:
            @property
            def first(self):
                return self

            def count(self):
                return 1

            def nth(self, index):
                return self

            def input_value(self, timeout=None):
                return "湘AK6980"

            def get_attribute(self, name):
                return "湘AK6980" if name == "value" else None

        class _FakeCell:
            @property
            def first(self):
                return self

            def count(self):
                return 1

            def inner_text(self):
                return ""

            def text_content(self):
                return ""

            def locator(self, selector):
                return _FakeInput()

        class _FakeRow:
            def locator(self, selector):
                return _FakeCell()

        self.assertEqual(
            "湘AK6980",
            auto_departure_r7._row_cell_text(_FakeRow(), column_index=8),
        )

    def test_direct_router_maps_scan_command_to_scan_sync_tool(self):
        for text in ("扫描", "获取并扫描数据", "同步扫描", "“扫描”", "\u200b扫描\u200b"):
            with self.subTest(text=text):
                request = direct_tool_router.direct_tool_request_from_text(text)

                self.assertIsNotNone(request)
                self.assertEqual("sync_scan_codes", request["tool_name"])
                self.assertEqual({}, request["params"])
                self.assertEqual("deferred", request["mode"])

    def test_direct_router_does_not_map_scan_query_to_scan_sync_tool(self):
        request = direct_tool_router.direct_tool_request_from_text("查扫描记录")

        self.assertIsNone(request)

    def test_feishu_menu_scan_action_runs_scan_sync_tool(self):
        for event_key in ("扫描", "scan", "sync_scan_codes"):
            with self.subTest(event_key=event_key):
                request = message_handler._menu_action_from_key(event_key)

                self.assertIsNotNone(request)
                self.assertEqual("sync_scan_codes", request["tool_name"])
                self.assertEqual({}, request["params"])

    def test_feishu_scan_message_bypasses_llm_and_executes_scan_tool(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {
                        "fetched": 1,
                        "normalized": 1,
                        "child_items": 1,
                        "batches": 1,
                        "batch_results": [{"batch": 1, "items": 1, "ok": True, "raw": {"ok": True}}],
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("扫描 should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("扫描", "user-1", "chat-1"))

        self.assertEqual(("sync_scan_codes", {}), calls["execute_tool"])
        self.assertEqual("程序正在执行", replies[0])
        self.assertIn("扫描任务已完成", replies[-1])

    def test_feishu_invalid_tracking_number_replies_without_tool_execution(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                raise AssertionError("invalid tracking number should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("invalid tracking number should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("R000016211453", "user-1", "chat-1"))

        self.assertEqual({}, calls)
        self.assertEqual(
            ["单号查询失败：单号格式错误：R 开头融辉单号应为 R+11位主单或 R+15位子单，请检查是否多输/少输数字。"],
            replies,
        )

    def test_feishu_tracking_number_replies_with_query_ack_before_result(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {
                        "type": "ronghui",
                        "tracking_number": params["tracking_number"],
                        "route_rows": [],
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("tracking number should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("R00014513348", "user-1", "chat-1"))

        self.assertEqual(
            ("track_waybill", {"tracking_number": "R00014513348", "provider": "ronghui"}),
            calls["execute_tool"],
        )
        self.assertEqual("正在查询单号：R00014513348", replies[0])
        self.assertIn("查询单号：R00014513348", replies[-1])

    def test_feishu_arrive_list_message_bypasses_llm_and_executes_tool(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {
                        "fetched": 2,
                        "bill_codes": 2,
                        "detail_records": 2,
                        "mysql_result": {"ok": True, "replaced": 2},
                        "primary_result": {"ok": True, "rows": 2},
                        "secondary_result": {"ok": True, "rows": 2},
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("arrivelist should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("执行一次arrivelist脚本", "user-1", "chat-1"))

        self.assertEqual(("sync_arrive_list", {}), calls["execute_tool"])
        self.assertEqual("程序正在执行", replies[0])
        self.assertIn("到货清单同步已完成", replies[-1])
        self.assertIn("派件预报：2", replies[-1])

    def test_feishu_price_auth_pending_waits_for_sms_code(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": False,
                    "error": "短信验证码已发送，等待人工提交验证码。",
                    "data": {
                        "ok": False,
                        "error_code": "AUTH_PENDING_CODE",
                        "error": "短信验证码已发送，等待人工提交验证码。",
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("报价 should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._get_admin", return_value={"ok": True, "status": "expired", "authenticated": False}),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("四川省泸州市泸县241乡道东南侧，800，5", "user-1", "chat-1"))

        self.assertEqual("get_price", calls["execute_tool"][0])
        self.assertEqual([("/admin/tms/price-session/send-code", None)], admin_calls)
        self.assertEqual(1, len(pending_calls))
        self.assertEqual("chat-1", pending_calls[0][0])
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("price", pending_calls[0][1]["auth_session"])
        self.assertEqual("get_price", pending_calls[0][1]["resume_tool"])
        self.assertIn("验证码已发送", replies[-1])
        self.assertNotIn("报价失败", replies[-1])

    def test_feishu_price_auth_required_sends_code_without_confirmation(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": False,
                    "error": "当前未登录或登录态已过期。",
                    "data": {"ok": False, "error_code": "AUTH_REQUIRED", "error": "当前未登录或登录态已过期。"},
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("报价登录恢复 should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._get_admin", return_value={"ok": True, "status": "expired", "authenticated": False}),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("四川省泸州市泸县241乡道东南侧，800，5", "user-1", "chat-1"))

        self.assertEqual("get_price", calls["execute_tool"][0])
        self.assertEqual([("/admin/tms/price-session/send-code", None)], admin_calls)
        self.assertEqual(1, len(pending_calls))
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("price", pending_calls[0][1]["auth_session"])
        self.assertEqual("get_price", pending_calls[0][1]["resume_tool"])
        self.assertIn("正在自动识别图片验证码并登录", replies[-2])
        self.assertIn("验证码已发送", replies[-1])
        self.assertNotIn("是否现在发送", "\n".join(replies))

    def test_feishu_deferred_tool_auth_pending_waits_for_sms_code(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": False,
                    "error": "工具执行失败(exit 1): ValueError: get_scan 返回格式异常: {'ok': False, 'error_code': 'AUTH_PENDING_CODE', 'error': '短信验证码已发送，等待人工提交验证码。'}",
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("统计 should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._get_admin", return_value={"ok": True, "status": "expired", "authenticated": False}),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("统计", "user-1", "chat-1"))

        self.assertEqual(("sync_arrival_stats", {}), calls["execute_tool"])
        self.assertEqual("程序正在执行", replies[0])
        self.assertEqual([("/admin/tms/session/send-code", None)], admin_calls)
        self.assertEqual(1, len(pending_calls))
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("default", pending_calls[0][1]["auth_session"])
        self.assertEqual("sync_arrival_stats", pending_calls[0][1]["resume_tool"])
        self.assertIn("验证码已发送", replies[-1])
        self.assertNotIn("工具执行失败", replies[-1])

    def test_feishu_deferred_tool_retries_stale_auth_when_status_is_authenticated(self):
        calls: list[tuple[str, dict[str, Any]]] = []
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls.append((tool_name, params))
                if len(calls) == 1:
                    return {"success": False, "error_code": "AUTH_REQUIRED", "error": "AUTH_REQUIRED"}
                return {
                    "success": True,
                    "data": {
                        "main_trackings": 1,
                        "records": 1,
                        "count_result": {"arrived_nonzero": 1},
                        "primary_result": {"ok": True},
                        "secondary_result": {"ok": True},
                        "pending_result": {"skipped": True, "reason": "missing_resource"},
                        "archive_result": {"ok": True},
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("stats should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch(
                "feishu.message_handler.direct_tool_request_from_text",
                return_value={"tool_name": "sync_arrival_stats", "params": {}, "mode": "deferred"},
            ),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._get_admin", return_value={"ok": True, "status": "authenticated", "authenticated": True}),
            patch("feishu.message_handler._post_admin", side_effect=AssertionError("stale auth retry should not send code")),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("ç»Ÿè®¡", "user-1", "chat-1"))

        self.assertEqual(2, len(calls))
        self.assertEqual(("sync_arrival_stats", {}), calls[-1])
        self.assertEqual([], pending_calls)
        self.assertIn("missing_resource", replies[-1])
        self.assertNotIn("AUTH_REQUIRED", replies[-1])

    def test_feishu_direct_tool_clears_stale_login_pending(self):
        calls: list[tuple[str, dict[str, Any]]] = []
        replies: list[str] = []
        pending = {
            "type": "confirm_login_for_resume",
            "auth_session": "default",
            "resume_tool": "sync_arrival_stats",
            "resume_params": {},
        }

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls.append((tool_name, params))
                return {
                    "success": True,
                    "data": {
                        "main_trackings": 1,
                        "records": 1,
                        "count_result": {"arrived_nonzero": 1},
                        "primary_result": {"ok": True},
                        "secondary_result": {"ok": True},
                        "pending_result": {"skipped": True, "reason": "missing_resource"},
                        "archive_result": {"ok": True},
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("direct request should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch(
                "feishu.message_handler.direct_tool_request_from_text",
                return_value={"tool_name": "sync_arrival_stats", "params": {}, "mode": "deferred"},
            ),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("ç»Ÿè®¡", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual([("sync_arrival_stats", {})], calls)
        self.assertIn("missing_resource", replies[-1])

    def test_feishu_llm_selected_tool_auth_required_uses_login_resume_flow(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("semantic request should be handled by handle_message in this test")

            async def handle_message(self, *args, **kwargs):
                return {
                    "reply": "到货清单同步失败：当前未登录或登录态已过期。",
                    "executed_tools": [
                        {
                            "tool_name": "sync_arrive_list",
                            "params": {},
                            "result": {
                                "success": False,
                                "error": "工具执行失败(exit 1): AUTH_REQUIRED 当前未登录或登录态已过期。",
                            },
                        }
                    ],
                }

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._get_admin", return_value={"ok": True, "status": "expired", "authenticated": False}),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("帮我处理今天预到达数据", "user-1", "chat-1"))

        self.assertEqual(1, len(pending_calls))
        self.assertEqual("confirm_login_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("sync_arrive_list", pending_calls[0][1]["resume_tool"])
        self.assertIn("登录过期需要重新登录", replies[-1])
        self.assertNotIn("到货清单同步失败", replies[-1])

    def test_pending_actions_persist_across_memory_clear(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = os.path.join(tmp_dir, "pending_actions.json")
            with patch.dict(os.environ, {"AGENT_PENDING_STATE_FILE": state_file}):
                pending_actions._pending.clear()
                pending_actions.set_pending(
                    "chat-1",
                    {
                        "type": "confirm_login_for_resume",
                        "auth_session": "default",
                        "resume_tool": "sync_arrive_list",
                        "resume_params": {},
                    },
                    ttl_sec=60,
                )

                pending_actions._pending.clear()
                restored = pending_actions.get_pending("chat-1")
                self.assertIsNotNone(restored)
                self.assertEqual("confirm_login_for_resume", restored["type"])
                self.assertEqual("sync_arrive_list", restored["resume_tool"])

                pending_actions.clear_pending("chat-1")
                pending_actions._pending.clear()
                self.assertIsNone(pending_actions.get_pending("chat-1"))

    def test_feishu_persisted_login_confirm_sends_code_without_llm(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login confirmation should send code before executing tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("persisted login confirmation should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = os.path.join(tmp_dir, "pending_actions.json")
            with patch.dict(os.environ, {"AGENT_PENDING_STATE_FILE": state_file}):
                pending_actions._pending.clear()
                pending_actions.set_pending(
                    "chat-1",
                    {
                        "type": "confirm_login_for_resume",
                        "auth_session": "default",
                        "resume_tool": "sync_arrive_list",
                        "resume_params": {},
                    },
                    ttl_sec=600,
                )
                pending_actions._pending.clear()

                with (
                    patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
                    patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
                    patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
                ):
                    asyncio.run(message_handler._process_and_reply("是", "user-1", "chat-1"))

                self.assertEqual([("/admin/tms/session/send-code", None)], admin_calls)
                self.assertIn("正在自动识别图片验证码并登录（操作场账号）", replies[0])
                self.assertIn("验证码已发送", replies[-1])
                restored = pending_actions.get_pending("chat-1")
                self.assertEqual("waiting_code_for_resume", restored["type"])
                self.assertEqual("sync_arrive_list", restored["resume_tool"])

    def test_feishu_confirm_without_pending_does_not_execute_tool(self):
        replies: list[str] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("confirm text without pending must not execute a tool")

            async def handle_message(self, *args, **kwargs):
                return {"reply": message_handler.UNKNOWN_EXECUTION_REPLY, "executed_tools": []}

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("是", "user-1", "chat-1"))

        self.assertEqual([message_handler.UNKNOWN_EXECUTION_REPLY], replies)

    def test_feishu_ws_mysql_lease_blocks_duplicate_consumer(self):
        class _FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args):
                return None

            def fetchone(self):
                return (0,)

        class _FakeConn:
            def cursor(self):
                return _FakeCursor()

            def close(self):
                return None

        with patch("feishu.bot._db_connect_for_lease", return_value=_FakeConn()):
            feishu_bot._lease_conn = None
            self.assertFalse(feishu_bot._acquire_ws_lease())

    def test_feishu_login_message_asks_account_choice(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login command should call admin send-code, not a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler._get_admin", return_value=_admin_accounts_payload()),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("登陆", "user-1", "chat-1"))

        self.assertEqual([], admin_calls)
        self.assertEqual(1, len(pending_calls))
        self.assertEqual("login_account_choice", pending_calls[0][1]["type"])
        self.assertIn("1. TMS融辉默认账号 (ronghui_default) · TMS融辉", replies[-1])
        self.assertIn("2. 大祥报价账号 (price_default) · TMS融辉 / 大祥报价", replies[-1])

    def test_feishu_operator_login_message_sends_default_tms_code(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("operator login command should call admin send-code, not a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("operator login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("操作场登陆", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/session/send-code", None)], admin_calls)
        self.assertEqual(1, len(pending_calls))
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("default", pending_calls[0][1]["auth_session"])
        self.assertIsNone(pending_calls[0][1]["resume_tool"])
        self.assertIn("正在自动识别图片验证码并登录（操作场账号）", replies[0])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_price_login_message_sends_price_code(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("price login command should call price send-code, not a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("price login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("报价发验证码", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/price-session/send-code", None)], admin_calls)
        self.assertEqual("price", pending_calls[0][1]["auth_session"])
        self.assertIsNone(pending_calls[0][1]["resume_tool"])
        self.assertIn("正在自动识别图片验证码并登录（大祥账号）", replies[0])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_login_account_choice_sends_selected_code(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {"type": "login_account_choice"}

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login account choice should call admin send-code, not a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login account choice should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("1", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual([("/admin/tms/price-session/send-code", None)], admin_calls)
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("price", pending_calls[0][1]["auth_session"])
        self.assertIn("正在自动识别图片验证码并登录（大祥账号）", replies[0])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_login_message_overrides_non_login_pending(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {
            "type": "r7_departure_plate_choice",
            "tool_name": "r7_departure_checkin",
            "params": {"class_name": "邵阳操作场-长沙"},
            "plate_numbers": ["湘AK6980"],
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("login command should override R7 pending and not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._get_admin", return_value=_admin_accounts_payload()),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("登陆", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual("login_account_choice", pending_calls[0][1]["type"])
        self.assertIn("1. TMS融辉默认账号 (ronghui_default) · TMS融辉", replies[-1])

    def test_feishu_login_message_overrides_resume_pending_without_executing_old_task(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {
            "type": "confirm_login_for_resume",
            "auth_session": "default",
            "resume_tool": "r7_arrival_checkin",
            "resume_params": {"_feishu": True},
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("generic login command should not resume the old R7 task")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(*args, **kwargs):
            raise AssertionError("generic login command should ask account choice before sending code")

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._get_admin", return_value=_admin_accounts_payload()),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("登陆", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual("login_account_choice", pending_calls[0][1]["type"])
        self.assertIn("1. TMS融辉默认账号 (ronghui_default) · TMS融辉", replies[-1])

    def test_feishu_explicit_login_overrides_resume_pending_without_resume_tool(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {
            "type": "confirm_login_for_resume",
            "auth_session": "default",
            "resume_tool": "r7_arrival_checkin",
            "resume_params": {"_feishu": True},
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("explicit login command should not resume the old R7 task")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("login command should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("操作场登录", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        self.assertEqual([("/admin/tms/session/send-code", None)], admin_calls)
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("default", pending_calls[0][1]["auth_session"])
        self.assertIsNone(pending_calls[0][1]["resume_tool"])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_standalone_sms_code_logs_in_without_resume(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending = {
            "type": "waiting_code_for_resume",
            "auth_session": "default",
            "resume_tool": None,
            "resume_params": {},
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("standalone login should not resume a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("sms code should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "authenticated"}

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("123456", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/session/submit-code", {"code": "123456"})], admin_calls)
        clear_pending.assert_called_once_with("chat-1")
        self.assertIn("正在校验验证码", replies[0])
        self.assertEqual("登录成功", replies[-1])

    def test_feishu_sms_code_without_pending_uses_broker_pending_code_state(self):
        replies: list[str] = []
        get_calls: list[str] = []
        post_calls: list[tuple[str, dict[str, Any] | None]] = []

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("standalone sms code should not execute a tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("standalone sms code should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_get_admin(path):
            get_calls.append(path)
            if path == "/admin/tms/session/status":
                return {"ok": True, "status": "pending_code", "pending_code": True}
            return {"ok": True, "status": "logged_out", "pending_code": False}

        async def _fake_post_admin(path, body=None):
            post_calls.append((path, body))
            return {"ok": True, "status": "authenticated"}

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._get_admin", side_effect=_fake_get_admin),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("123456", "user-1", "chat-1"))

        self.assertEqual(["/admin/accounts", "/admin/tms/session/status"], get_calls)
        self.assertEqual([("/admin/tms/session/submit-code", {"code": "123456"})], post_calls)
        self.assertIn("正在校验验证码（操作场账号）", replies[0])
        self.assertEqual("登录成功", replies[-1])

    def test_feishu_price_login_confirmation_uses_price_session_endpoint(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []
        pending = {
            "type": "confirm_login_for_resume",
            "auth_session": "price",
            "resume_tool": "get_price",
            "resume_params": {"address": "长沙", "weight": 800.0},
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("confirmation should send code before executing tool")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("confirmation should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "pending_code"}

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("是", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/price-session/send-code", None)], admin_calls)
        self.assertEqual("waiting_code_for_resume", pending_calls[0][1]["type"])
        self.assertEqual("price", pending_calls[0][1]["auth_session"])
        self.assertIn("验证码已发送", replies[-1])

    def test_feishu_price_sms_code_uses_price_session_endpoint_and_resumes(self):
        replies: list[str] = []
        admin_calls: list[tuple[str, dict[str, Any] | None]] = []
        execute_calls: list[tuple[str, dict[str, Any]]] = []
        pending = {
            "type": "waiting_code_for_resume",
            "auth_session": "price",
            "resume_tool": "get_price",
            "resume_params": {"address": "长沙", "weight": 800.0},
        }

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                execute_calls.append((tool_name, params))
                return {"success": True, "data": {"目的网点": "测试站"}}

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("sms code should not be routed to LLM")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        async def _fake_post_admin(path, body=None):
            admin_calls.append((path, body))
            return {"ok": True, "status": "authenticated"}

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending"),
            patch("feishu.message_handler._post_admin", side_effect=_fake_post_admin),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("123456", "user-1", "chat-1"))

        self.assertEqual([("/admin/tms/price-session/submit-code", {"code": "123456"})], admin_calls)
        self.assertEqual([("get_price", {"address": "长沙", "weight": 800.0})], execute_calls)
        self.assertIn("登录成功", replies[-2])
        self.assertIn("目的网点：测试站", replies[-1])

    def test_feishu_departure_message_executes_single_configured_plate(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []

        class _FakeMemory:
            def list_scheduled_tasks(self):
                return [
                    {
                        "id": "r7_departure_checkin",
                        "tool_name": "r7_departure_checkin",
                        "tool_params": {
                            "class_name": "邵阳操作场-长沙",
                            "departure_time_fixed": "21:30:00",
                            "plate_numbers": "湘AK6980",
                        },
                    }
                ]

        class _FakeAgent:
            memory = _FakeMemory()

            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {
                        "ok": True,
                        "detail": {
                            "class_name": params.get("class_name"),
                            "departure_time": "2026-04-29 21:30:00",
                            "plate_numbers": params.get("plate_numbers"),
                        },
                    },
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("发车 should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("发车", "user-1", "chat-1"))

        tool_name, params = calls["execute_tool"]
        self.assertEqual("r7_departure_checkin", tool_name)
        self.assertTrue(params["_feishu"])
        self.assertEqual(["湘AK6980"], params["plate_numbers"])
        self.assertEqual("程序正在执行", replies[0])
        self.assertIn("R7 发车打卡已完成", replies[-1])

    def test_feishu_departure_message_sets_pending_for_multiple_plates(self):
        replies: list[str] = []
        pending_calls: list[tuple[str, dict[str, Any], int]] = []

        class _FakeMemory:
            def list_scheduled_tasks(self):
                return [
                    {
                        "id": "r7_departure_checkin",
                        "tool_name": "r7_departure_checkin",
                        "tool_params": {
                            "class_name": "邵阳操作场-长沙",
                            "departure_time_fixed": "21:30:00",
                            "plate_numbers": "湘AK6980,湘B12345",
                        },
                    }
                ]

        class _FakeAgent:
            memory = _FakeMemory()

            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("multi-plate 发车 should wait for user choice")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("发车 should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        def _fake_set_pending(chat_id, action, ttl_sec=600):
            pending_calls.append((chat_id, action, ttl_sec))

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=None),
            patch("feishu.message_handler.set_pending", side_effect=_fake_set_pending),
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("发车", "user-1", "chat-1"))

        self.assertEqual(1, len(pending_calls))
        self.assertEqual("chat-1", pending_calls[0][0])
        self.assertEqual("r7_departure_plate_choice", pending_calls[0][1]["type"])
        self.assertEqual(["湘AK6980", "湘B12345"], pending_calls[0][1]["plate_numbers"])
        self.assertIn("1. 湘AK6980", replies[-1])
        self.assertIn("2. 湘B12345", replies[-1])

    def test_feishu_departure_pending_choice_executes_selected_plate(self):
        calls: dict[str, Any] = {}
        replies: list[str] = []
        pending = {
            "type": "r7_departure_plate_choice",
            "tool_name": "r7_departure_checkin",
            "params": {"class_name": "邵阳操作场-长沙"},
            "plate_numbers": ["湘AK6980", "湘B12345"],
        }

        class _FakeAgent:
            async def execute_tool(self, tool_name, params):
                calls["execute_tool"] = (tool_name, params)
                return {
                    "success": True,
                    "data": {"ok": True, "detail": {"plate_numbers": params.get("plate_numbers")}},
                }

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("pending choice should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("湘B12345", "user-1", "chat-1"))

        clear_pending.assert_called_once_with("chat-1")
        tool_name, params = calls["execute_tool"]
        self.assertEqual("r7_departure_checkin", tool_name)
        self.assertTrue(params["_feishu"])
        self.assertEqual(["湘B12345"], params["plate_numbers"])
        self.assertIn("湘B12345", replies[0])

    def test_feishu_departure_pending_rejects_bare_numeric_choice(self):
        replies: list[str] = []
        pending = {
            "type": "r7_departure_plate_choice",
            "tool_name": "r7_departure_checkin",
            "params": {"class_name": "邵阳操作场-长沙"},
            "plate_numbers": ["湘AK6980", "湘B12345"],
        }

        class _FakeAgent:
            async def execute_tool(self, *args, **kwargs):
                raise AssertionError("bare numeric choice must not execute R7 departure")

            async def handle_message(self, *args, **kwargs):
                raise AssertionError("pending choice should not be routed to LLM handle_message")

        async def _fake_reply_text(_chat_id, text, receive_id_type="chat_id", *, reply_type="text"):
            replies.append(text)

        with (
            patch("feishu.bot.get_agent_core", return_value=_FakeAgent()),
            patch("feishu.message_handler.get_pending", return_value=pending),
            patch("feishu.message_handler.clear_pending") as clear_pending,
            patch("feishu.message_handler._reply_text", side_effect=_fake_reply_text),
        ):
            asyncio.run(message_handler._process_and_reply("2", "user-1", "chat-1"))

        clear_pending.assert_not_called()
        self.assertIn("回复完整车牌号", replies[-1])
        self.assertIn("2. 湘B12345", replies[-1])

    def test_scan_sync_reply_summarizes_counts_and_failed_batches(self):
        reply = direct_tool_router.format_tool_reply(
            "sync_scan_codes",
            {
                "success": True,
                "data": {
                    "fetched": 10,
                    "normalized": 9,
                    "child_items": 8,
                    "batches": 2,
                    "scan_index_result": {"replaced": 9},
                    "batch_results": [
                        {"batch": 1, "items": 5, "ok": True, "raw": {"ok": True}},
                        {"batch": 2, "items": 3, "ok": False, "raw": {"error": "scan_next failed"}},
                    ],
                    "skipped_signed_count": 1,
                    "flow_result": {"skipped": True},
                },
            },
        )

        self.assertIn("扫描任务已完成", reply)
        self.assertIn("拉取扫描记录：10", reply)
        self.assertIn("失败批次：1/2", reply)
        self.assertIn("已签收跳过：1", reply)

    def test_scan_sync_reply_uses_nested_scan_next_error(self):
        reply = direct_tool_router.format_tool_reply(
            "sync_scan_codes",
            {
                "success": True,
                "data": {
                    "fetched": 1,
                    "normalized": 1,
                    "child_items": 1,
                    "batches": 1,
                    "batch_results": [
                        {
                            "batch": 1,
                            "items": 1,
                            "ok": False,
                            "raw": {"ok": False, "data": {"message": "select_station timeout"}},
                        }
                    ],
                },
            },
        )

        self.assertIn("select_station timeout", reply)

    def test_arrive_list_sync_reply_summarizes_real_counts(self):
        reply = direct_tool_router.format_tool_reply(
            "sync_arrive_list",
            {
                "success": True,
                "data": {
                    "fetched": 5,
                    "bill_codes": 4,
                    "skipped_receipt_like": 2,
                    "detail_records": 4,
                    "mysql_result": {"ok": True, "replaced": 4},
                    "primary_result": {"ok": True, "rows": 4},
                    "secondary_result": {"ok": True, "rows": 4},
                },
            },
        )

        self.assertIn("到货清单同步已完成", reply)
        self.assertIn("派件预报：5", reply)
        self.assertIn("主单数：4", reply)
        self.assertIn("跳过回单号：2", reply)
        self.assertIn("MySQL：覆盖 4", reply)
        self.assertIn("主飞书表：写入 4 行", reply)

    def test_agent_blocks_unverified_execution_completion_claim(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                return []

        class _FakeLLM:
            async def chat(self, messages, tools=None):
                return {"content": "同步已完成，已写入MySQL和飞书表格。"}

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "执行一次未知脚本",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertEqual("没有匹配到可执行脚本，我不知道该执行哪个任务。", result["reply"])

    def test_agent_blocks_freeform_execution_answer_without_tool_call(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                return []

        class _FakeLLM:
            async def chat(self, messages, tools=None):
                return {"content": "我来处理这个任务。"}

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "执行一个不存在的脚本",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertEqual("没有匹配到可执行脚本，我不知道该执行哪个任务。", result["reply"])

    def test_agent_blocks_plain_freeform_answer_without_tool_call(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                return []

        class _FakeLLM:
            async def chat(self, messages, tools=None):
                return {"content": "这是一个普通聊天回复。"}

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "你好",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertEqual("没有匹配到可执行脚本，我不知道该执行哪个任务。", result["reply"])

    def test_agent_formats_real_tool_result_instead_of_llm_summary(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

            def save_tool_log(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                return [{"type": "function", "function": {"name": "r7_departure_checkin"}}]

            def get_tool(self, name):
                return {"name": name}

        class _FakeExecutor:
            async def execute(self, tool_config, params):
                return {
                    "success": True,
                    "data": {
                        "ok": True,
                        "stage": "done",
                        "message": "success",
                        "detail": {
                            "class_name": "邵阳操作场-长沙",
                            "plate_numbers": ["湘AK6980"],
                            "status_text": "装车待发",
                        },
                    },
                }

        class _FakeLLM:
            def __init__(self):
                self.calls = 0

            async def chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "r7_departure_checkin",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                return {"content": "假的 LLM 总结：已经处理好了。"}

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.executor = _FakeExecutor()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "帮我发车",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertIn("R7 发车打卡已完成", result["reply"])
        self.assertIn("湘AK6980", result["reply"])
        self.assertNotIn("假的 LLM 总结", result["reply"])
        self.assertEqual("r7_departure_checkin", result["executed_tools"][0]["tool_name"])
        self.assertTrue(result["executed_tools"][0]["result"]["success"])

    def test_agent_login_message_does_not_reach_llm(self):
        class _FakeMemory:
            def get_or_create_conversation(self, user_id, conversation_id):
                return conversation_id or "conv-1"

            def get_recent_messages(self, conv_id, limit=10):
                return []

            def search_knowledge(self, message, limit=3):
                return []

            def save_message(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_openai_tools(self):
                raise AssertionError("login message should return before tool schema lookup")

        class _FakeLLM:
            async def chat(self, *args, **kwargs):
                raise AssertionError("login message should not be routed to LLM")

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.llm = _FakeLLM()

        result = asyncio.run(
            core.handle_message(
                "登陆",
                user_id="user-1",
                conversation_id="conv-1",
            )
        )

        self.assertEqual("1. 大祥账号\n2. 操作场账号\n3. 韵达账号", result["reply"])

    def test_agent_executes_track_waybill_in_process_without_tool_executor_lock(self):
        class _FakeMemory:
            def save_tool_log(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_tool(self, name):
                return {"name": name, "executor": "tools/track_waybill_tool.py"}

        class _FailingExecutor:
            async def execute(self, tool_config, params):
                raise AssertionError("track_waybill should bypass generic subprocess executor")

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.executor = _FailingExecutor()

        with patch(
            "agent.core._run_track_waybill_in_process",
            return_value={"tracking_number": "R00014513348", "route_rows": []},
        ) as run_track:
            result = asyncio.run(core.execute_tool("track_waybill", {"tracking_number": "R00014513348"}))

        run_track.assert_called_once_with({"tracking_number": "R00014513348"})
        self.assertTrue(result["success"])
        self.assertEqual("R00014513348", result["data"]["tracking_number"])

    def test_price_tool_queries_ronghui_and_yunda_concurrently(self):
        threading = __import__("threading")
        yunda_started = threading.Event()

        def _fake_ronghui(**kwargs):
            return {
                "目的网点": "隆尧莲子镇S站",
                "saw_yunda_started": yunda_started.wait(timeout=0.1),
            }

        def _fake_yunda(**kwargs):
            yunda_started.set()
            return {"目的网点": "隆尧莲子镇分部"}

        with (
            patch("tools.price_tool.PRICE_TOOL_PREFER_HTTP", True),
            patch("tools.price_tool.get_price_via_http", side_effect=_fake_ronghui),
            patch("tools.price_tool.get_yunda_price_via_http", side_effect=_fake_yunda),
        ):
            result = price_tool.get_combined_price(
                address="河北省邢台市隆尧县莲子镇中学",
                weight=199,
                volume=2.727,
            )

        self.assertTrue(result["ronghui"]["saw_yunda_started"])
        self.assertEqual("隆尧莲子镇分部", result["yunda"]["目的网点"])

    def test_agent_executes_get_price_in_process_without_tool_executor_lock(self):
        class _FakeMemory:
            def save_tool_log(self, *args, **kwargs):
                return 1

        class _FakeRegistry:
            def get_tool(self, name):
                return {"name": name, "executor": "tools/price_tool.py"}

        class _FailingExecutor:
            async def execute(self, tool_config, params):
                raise AssertionError("get_price should bypass generic subprocess executor")

        core = AgentCore()
        core.memory = _FakeMemory()
        core.registry = _FakeRegistry()
        core.executor = _FailingExecutor()

        params = {"address": "河北省邢台市隆尧县莲子镇中学", "weight": 199, "volume": 2.727}
        with patch(
            "agent.core._run_get_price_in_process",
            return_value={"mode": "agent_tms_combined", "ronghui": {}, "yunda": {}},
        ) as run_price:
            result = asyncio.run(core.execute_tool("get_price", params))

        run_price.assert_called_once_with(params)
        self.assertTrue(result["success"])
        self.assertEqual("agent_tms_combined", result["data"]["mode"])

    def test_r7_arrival_checkin_tool_uses_r7_script_without_secret_params(self):
        captured: dict[str, Any] = {}

        def _fake_run_once(params):
            captured.update(params)
            return {
                "ok": True,
                "stage": "done",
                "message": "success",
                "detail": {"status_text": params.get("status_text")},
                "cost_sec": 1.2,
            }

        with (
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_arrival_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once", side_effect=_fake_run_once),
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin(
                {
                    "username": "should-not-be-stored",
                    "password": "should-not-be-stored",
                    "status_text": "已调度",
                    "timeout_sec": 900,
                    "daily_success_limit": 2,
                    "_scheduled_task": {"id": "r7_arrival_checkin"},
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["detail"]["success_count_today"])
        self.assertEqual(2, result["detail"]["daily_success_limit"])
        self.assertEqual("车辆到达", captured["status_text"])
        self.assertTrue(captured["headless"])
        self.assertTrue(captured["do_arrive_wait_unload"])
        self.assertNotIn("daily_success_limit", captured)
        self.assertNotIn("username", captured)
        self.assertNotIn("password", captured)
        self.assertNotIn("timeout_sec", captured)
        self.assertNotIn("_scheduled_task", captured)
        self.assertEqual("success", insert_log.call_args.kwargs["status"])
        self.assertEqual(0, insert_log.call_args.kwargs["success_count_before"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_after"])

    def test_r7_arrival_checkin_tool_keeps_dispatched_status_for_dry_run(self):
        captured: dict[str, Any] = {}

        def _fake_run_once(params):
            captured.update(params)
            return {"ok": True, "stage": "checkbox_clicked", "message": "checkbox clicked"}

        with (
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_arrival_checkin_tool._insert_log"),
            patch("tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once", side_effect=_fake_run_once),
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin(
                {"status_text": "已调度", "do_arrive_wait_unload": False}
            )

        self.assertTrue(result["ok"])
        self.assertEqual("已调度", captured["status_text"])
        self.assertFalse(captured["do_arrive_wait_unload"])

    def test_r7_arrival_checkin_tool_skips_when_daily_limit_reached(self):
        with (
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=1),
            patch("tools.r7_arrival_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once") as run_once,
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin({"daily_success_limit": 1})

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual("daily_limit_reached", result["stage"])
        self.assertEqual(1, result["detail"]["success_count_today"])
        run_once.assert_not_called()
        self.assertEqual("skipped", insert_log.call_args.kwargs["status"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_before"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_after"])

    def test_r7_arrival_checkin_tool_marks_script_not_ok_as_error(self):
        with (
            patch("tools.r7_arrival_checkin_tool._prepare_log_storage"),
            patch("tools.r7_arrival_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_arrival_checkin_tool._insert_log") as insert_log,
            patch(
                "tools.r7_arrival_checkin_tool.auto_checkin_r7.run_once",
                return_value={"ok": False, "stage": "not_found", "message": "未找到满足条件的行"},
            ),
        ):
            result = r7_arrival_checkin_tool.run_r7_arrival_checkin({})

        self.assertFalse(result["ok"])
        self.assertEqual("not_found", result["stage"])
        self.assertIn("未找到满足条件的行", result["error"])
        self.assertEqual("failure", insert_log.call_args.kwargs["status"])

    def test_r7_departure_checkin_tool_uses_script_without_secret_params(self):
        captured: dict[str, Any] = {}

        def _fake_run_once(params):
            captured.update(params)
            return {
                "ok": True,
                "stage": "done",
                "message": "success",
                "detail": {
                    "status_text": params.get("status_text"),
                    "verify_status_text": params.get("verify_status_text"),
                    "class_name": params.get("class_name"),
                    "departure_time": "2026-04-29 21:30:00",
                    "plate_numbers": params.get("plate_numbers"),
                },
                "cost_sec": 1.2,
            }

        with (
            patch("tools.r7_departure_checkin_tool._prepare_log_storage"),
            patch("tools.r7_departure_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_departure_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_departure_checkin_tool.auto_departure_r7.run_once", side_effect=_fake_run_once),
        ):
            result = r7_departure_checkin_tool.run_r7_departure_checkin(
                {
                    "username": "should-not-be-stored",
                    "password": "should-not-be-stored",
                    "status_text": "已调度",
                    "verify_status_text": "装车待发",
                    "class_name": "邵阳操作场-长沙",
                    "plate_numbers": "湘AK6980,湘B12345",
                    "timeout_sec": 900,
                    "daily_success_limit": 2,
                    "_scheduled_task": {"id": "r7_departure_checkin"},
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["detail"]["success_count_today"])
        self.assertEqual(2, result["detail"]["daily_success_limit"])
        self.assertEqual("已调度", captured["status_text"])
        self.assertEqual("装车待发", captured["verify_status_text"])
        self.assertEqual("邵阳操作场-长沙", captured["class_name"])
        self.assertEqual("湘AK6980,湘B12345", captured["plate_numbers"])
        self.assertTrue(captured["headless"])
        self.assertNotIn("daily_success_limit", captured)
        self.assertNotIn("username", captured)
        self.assertNotIn("password", captured)
        self.assertNotIn("timeout_sec", captured)
        self.assertNotIn("_scheduled_task", captured)
        self.assertEqual("success", insert_log.call_args.kwargs["status"])
        self.assertEqual(0, insert_log.call_args.kwargs["success_count_before"])
        self.assertEqual(1, insert_log.call_args.kwargs["success_count_after"])

    def test_r7_departure_checkin_tool_skips_when_daily_limit_reached(self):
        with (
            patch("tools.r7_departure_checkin_tool._prepare_log_storage"),
            patch("tools.r7_departure_checkin_tool._count_successes_today", return_value=1),
            patch("tools.r7_departure_checkin_tool._insert_log") as insert_log,
            patch("tools.r7_departure_checkin_tool.auto_departure_r7.run_once") as run_once,
        ):
            result = r7_departure_checkin_tool.run_r7_departure_checkin({"daily_success_limit": 1})

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual("daily_limit_reached", result["stage"])
        self.assertEqual(1, result["detail"]["success_count_today"])
        run_once.assert_not_called()
        self.assertEqual("skipped", insert_log.call_args.kwargs["status"])

    def test_r7_departure_checkin_tool_marks_script_not_ok_as_error(self):
        with (
            patch("tools.r7_departure_checkin_tool._prepare_log_storage"),
            patch("tools.r7_departure_checkin_tool._count_successes_today", return_value=0),
            patch("tools.r7_departure_checkin_tool._insert_log") as insert_log,
            patch(
                "tools.r7_departure_checkin_tool.auto_departure_r7.run_once",
                return_value={"ok": False, "stage": "target_match_failed", "message": "目标车牌未唯一命中"},
            ),
        ):
            result = r7_departure_checkin_tool.run_r7_departure_checkin({})

        self.assertFalse(result["ok"])
        self.assertEqual("target_match_failed", result["stage"])
        self.assertIn("目标车牌未唯一命中", result["error"])
        self.assertEqual("failure", insert_log.call_args.kwargs["status"])

    def test_phase7_resource_import_has_no_n8n_dependency(self):
        target = Path(__file__).resolve().parents[1] / "agent" / "phase7_resource_import.py"
        text = target.read_text(encoding="utf-8")
        self.assertNotIn("n8n", text.lower())
        self.assertNotIn("sqlite", text.lower())

    def test_arrive_list_sync_handles_malformed_fetch_response(self):
        with patch("tools.arrive_list_sync_tool.call_http_service", return_value={"unexpected": True}):
            result = arrive_list_sync_tool.run_arrive_list_sync({})
        self.assertIn("fetch_dispatch 返回格式异常", result["error"])

    def test_yunda_dispatch_forecast_fetch_maps_required_fields(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "total": 1,
                    "rows": [
                        {
                            "ship_id": "YD001",
                            "unit_cnt": "3",
                            "scan_cnt": "2",
                            "frgt_wgt": "12.5",
                            "frgt_vol": "0.3",
                            "pkg_lod_typ": "纸箱",
                            "fld_tm": "2026-05-10 18:00:00",
                            "plan_tlns": "24",
                            "rcv_cust_addr": "湖南省邵阳市测试地址",
                            "est_arv_tm": "2026-05-11 12:00:00",
                            "due_delv_dt": "2026-05-11",
                        }
                    ],
                }

        class Session:
            def get(self, *args, **kwargs):
                self.kwargs = kwargs
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("yunda_dispatch_forecast.get_session_broker", return_value=broker):
            result = yunda_dispatch_forecast.run_once({"target_date": "2026-05-11", "page_size": 200})

        self.assertTrue(result["ok"])
        self.assertEqual("2026-05-11", result["target_date"])
        self.assertEqual(1, result["total"])
        self.assertEqual(1, result["fetched"])
        self.assertEqual("YD001", result["records"][0]["主单号"])
        self.assertEqual("湖南省邵阳市测试地址", result["records"][0]["开单目的地址"])
        self.assertEqual("2026-05-11 00:00:00", session.kwargs["params"]["bgn_dt"])

    def test_yunda_dispatch_forecast_fetch_auth_redirect_raises_auth_required(self):
        class Response:
            status_code = 302
            headers = {"Location": "/login"}
            text = ""

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        with self.assertRaises(Exception) as ctx:
            yunda_dispatch_forecast.fetch_page(
                Session(),
                {},
                target_date=date(2026, 5, 11),
                limit=200,
                offset=0,
            )

        self.assertEqual("AUTH_REQUIRED", getattr(ctx.exception, "code", ""))

    def test_yunda_dispatch_forecast_fetch_empty_body_raises_auth_required(self):
        class Response:
            status_code = 200
            headers = {"content-type": "text/plain"}
            text = ""

            def raise_for_status(self):
                return None

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        with self.assertRaises(Exception) as ctx:
            yunda_dispatch_forecast.fetch_page(
                Session(),
                {},
                target_date=date(2026, 5, 11),
                limit=200,
                offset=0,
            )

        self.assertEqual("AUTH_REQUIRED", getattr(ctx.exception, "code", ""))

    def test_yunda_dispatch_forecast_fetch_accepts_top_level_list(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def raise_for_status(self):
                return None

            def json(self):
                return [
                    {
                        "ship_id": "YD001",
                        "unit_cnt": "3",
                        "due_delv_dt": "2026-05-11",
                    }
                ]

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("yunda_dispatch_forecast.get_session_broker", return_value=broker):
            result = yunda_dispatch_forecast.run_once({"target_date": "2026-05-11", "page_size": 200})

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["total"])
        self.assertEqual("YD001", result["records"][0]["主单号"])

    def test_yunda_waybill_entry_bootstrap_parses_html_fields(self):
        html = """
        <html><body>
          <input name="LogisticsId" value="YD001">
          <select name="ProductType">
            <option value="">请选择</option>
            <option value="standard" selected>标准</option>
          </select>
          <textarea name="BuyerAddress">湖南省长沙市岳麓区测试路1号</textarea>
          <input type="checkbox" name="BuyerSms" checked>
        </body></html>
        """

        class Response:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            url = yunda_waybill_entry.ENTRY_INDEX_URL
            text = html

            def raise_for_status(self):
                return None

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("yunda_waybill_entry.get_session_broker", return_value=broker):
            result = yunda_waybill_entry.run_once({"action": "bootstrap"})

        self.assertTrue(result["ok"])
        self.assertEqual("YD001", result["data"]["default_form"]["LogisticsId"])
        self.assertEqual("standard", result["data"]["default_form"]["ProductType"])
        self.assertEqual("standard", result["data"]["defaults"]["ProductType"])
        self.assertIn("ProductType", result["data"]["ui_options"])
        self.assertIn("remote_context", result["data"])
        self.assertIn("BuyerAddress", result["data"]["fields"])
        self.assertTrue(any(section["fields"] for section in result["data"]["sections"]))

    def test_yunda_waybill_entry_bootstrap_allows_business_login_text(self):
        html = """
        <html><body>
          <div>\u6700\u540e\u767b\u5f55\u65f6\u95f4</div>
          <script>var loginName = "operator";</script>
          <script>window.location.href = "https://sso.yunda56.com/logout";</script>
          <input name="LogisticsId" value="YD001">
        </body></html>
        """

        class Response:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            url = yunda_waybill_entry.ENTRY_INDEX_URL
            text = html

            def raise_for_status(self):
                return None

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("yunda_waybill_entry.get_session_broker", return_value=broker):
            result = yunda_waybill_entry.run_once({"action": "bootstrap"})

        self.assertTrue(result["ok"])
        self.assertEqual("YD001", result["data"]["default_form"]["LogisticsId"])

    def test_yunda_waybill_entry_save_runs_checks_and_normalizes_success(self):
        html = '<html><body><input name="LogisticsId" value=""><input name="BuyerName" value=""></body></html>'

        class HtmlResponse:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            url = yunda_waybill_entry.ENTRY_INDEX_URL
            text = html

            def raise_for_status(self):
                return None

        class JsonResponse:
            status_code = 200
            headers = {"content-type": "application/json"}

            def __init__(self, payload):
                self._payload = payload
                self.text = json.dumps(payload, ensure_ascii=False)

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, *args, **kwargs):
                return HtmlResponse()

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"url": url, "data": dict(data or {})})
                if url.endswith("/save.html"):
                    return JsonResponse({"info": "1", "LogisticsId": "YD001", "message": "saved"})
                return JsonResponse({"ok": True, "message": "checked"})

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("yunda_waybill_entry.get_session_broker", return_value=broker):
            result = yunda_waybill_entry.run_once({"action": "save", "form": {"LogisticsId": "YD001", "BuyerName": "张三"}})

        self.assertTrue(result["ok"])
        self.assertEqual("save", result["action"])
        self.assertEqual("YD001", result["data"]["waybill_no"])
        self.assertIn("close_route", result["data"]["checks"])
        self.assertIn("patch_form", result["data"])
        self.assertIn("panels", result["data"])
        self.assertEqual(yunda_waybill_entry.SAVE_URL, session.calls[-1]["url"])

    def test_yunda_waybill_entry_service_scope_url_matches_entry_page_config(self):
        self.assertEqual(
            "https://kyinms.yunda56.com/ky_inms/public/index.php/checkServiceScope.html",
            yunda_waybill_entry.CHECK_SERVICE_SCOPE_URL,
        )

    def test_yunda_waybill_entry_bootstrap_auth_redirect_raises_auth_required(self):
        class Response:
            status_code = 302
            headers = {"Location": "/login"}
            url = yunda_waybill_entry.ENTRY_INDEX_URL
            text = ""

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("yunda_waybill_entry.get_session_broker", return_value=broker):
            with self.assertRaises(Exception) as ctx:
                yunda_waybill_entry.run_once({"action": "bootstrap"})

        self.assertEqual("AUTH_REQUIRED", getattr(ctx.exception, "code", ""))

    def test_yunda_waybill_entry_draft_list_extracts_rows(self):
        html = '<html><body><input name="LogisticsId" value="YD001"></body></html>'

        class HtmlResponse:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            url = yunda_waybill_entry.ENTRY_INDEX_URL
            text = html

            def raise_for_status(self):
                return None

        class JsonResponse:
            status_code = 200
            headers = {"content-type": "application/json"}

            def __init__(self, payload):
                self._payload = payload
                self.text = json.dumps(payload, ensure_ascii=False)

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def get(self, *args, **kwargs):
                return HtmlResponse()

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.last_post = {"url": url, "data": dict(data or {})}
                return JsonResponse({"data": {"rows": [{"id": "1", "Name": "Draft A"}]}})

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("yunda_waybill_entry.get_session_broker", return_value=broker):
            result = yunda_waybill_entry.run_once({"action": "drafts/list", "form": {"LogisticsId": "YD001"}})

        self.assertTrue(result["ok"])
        self.assertEqual("1", result["data"]["items"][0]["id"])

    def test_yunda_waybill_entry_print_returns_preview_html(self):
        html = '<html><body><input name="LogisticsId" value="YD001"><input name="BuyerName" value="张三"></body></html>'

        class Response:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            url = yunda_waybill_entry.ENTRY_INDEX_URL
            text = html

            def raise_for_status(self):
                return None

        class Session:
            def __init__(self):
                self.posts = []
                self.gets = []

            def get(self, url, **kwargs):
                self.gets.append({"url": url})
                return Response()

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.posts.append({"url": url, "data": dict(data or {})})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("yunda_waybill_entry.get_session_broker", return_value=broker):
            result = yunda_waybill_entry.run_once({"action": "print/master", "form": {"LogisticsId": "YD001", "BuyerName": "张三"}})

        self.assertTrue(result["ok"])
        self.assertIn("preview_html", result["data"])
        self.assertEqual(
            yunda_waybill_entry._build_print_url("master", {"LogisticsId": "YD001"}),
            session.gets[-1]["url"],
        )
        self.assertIn("<base href=", result["data"]["preview_html"])
        self.assertEqual("printer_main_index", result["data"]["panels"]["print"]["remote_endpoint_name"])
        self.assertIn("YD001", result["data"]["preview_html"])

    def test_yunda_waybill_entry_extracts_child_waybills_for_side_panel(self):
        rows = yunda_waybill_entry._extract_child_waybills(
            {
                "childList": [
                    {"LogisticsId": "YDCHILD001", "dotName": "长沙站", "remark": "一件"},
                    {"mailno": "YDCHILD002", "siteName": "岳麓站"},
                ]
            }
        )

        self.assertEqual(["YDCHILD001", "YDCHILD002"], [row["waybill_no"] for row in rows])
        self.assertEqual("长沙站", rows[0]["destination"])

    def test_yunda_waybill_entry_parser_prefers_non_empty_duplicate_value(self):
        parsed = yunda_waybill_entry._parse_entry_page(
            """
            <select id="SenderDistributionCode" name=""></select>
            <input type="hidden" name="SenderDistributionCode" value="56731000">
            """
        )

        self.assertEqual("56731000", parsed["default_form"]["SenderDistributionCode"])

    def test_yunda_waybill_proxy_rewrites_html_and_filters_sensitive_headers(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.yunda_waybill_proxy")
        html = """
        <html><head>
          <link href="/ky_inms/public/static/app.css">
          <script src="https://kyinms.yunda56.com/ky_inms/public/static/app.js"></script>
          <script>
            var saveUrl = "/ky_inms/public/index.php/business/waybill/entry/save.html";
            var templateUrl = 'https://kyinms.yunda56.com/ky_inms/public/index.php/business/waybill/template/list.html?type=entry';
            var previewUrl = "/ky_inms/public/index.php/index/waybill._entry/indexNew.html";
            var previewHtml = `<img src="${previewUrl}" alt="">`;
            var batchHtml = '<iframe src = /ky_inms/public/index.php/business/waybill/uploadEntry/index.html ></iframe>';
          </script>
        </head><body>
          <form action="/ky_inms/public/index.php/business/waybill/entry/save.html"></form>
          <img src="../images/logo.png">
        </body></html>
        """

        class Response:
            status_code = 200
            headers = {
                "content-type": "text/html; charset=utf-8",
                "set-cookie": "SESSION=secret",
                "x-frame-options": "DENY",
            }
            url = proxy.YUNDA_INMS_ORIGIN + "/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&p=nil"
            content = html.encode("utf-8")
            text = html

            def raise_for_status(self):
                return None

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.yunda_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/ky_inms/public/index.php/business/waybill/entry/indexNew.html",
                    "query": "page=tab&p=nil",
                    "proxy_prefix": "/ocr/yunda/live",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(200, result["status_code"])
        self.assertEqual(
            proxy.YUNDA_INMS_ORIGIN + "/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&p=nil",
            session.calls[0]["url"],
        )
        self.assertNotIn("set-cookie", {key.lower() for key in result["headers"]})
        self.assertNotIn("x-frame-options", {key.lower() for key in result["headers"]})
        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn('href="/ocr/yunda/live/ky_inms/public/static/app.css"', body)
        self.assertIn('src="/ocr/yunda/live/ky_inms/public/static/app.js"', body)
        self.assertIn('action="/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/save.html"', body)
        self.assertIn('src="/ocr/yunda/live/ky_inms/public/index.php/business/waybill/images/logo.png"', body)
        self.assertIn('var saveUrl = "/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/save.html";', body)
        self.assertIn(
            "var templateUrl = '/ocr/yunda/live/ky_inms/public/index.php/business/waybill/template/list.html?type=entry';",
            body,
        )
        self.assertIn('var previewUrl = "/ocr/yunda/live/ky_inms/public/index.php/index/waybill._entry/indexNew.html";', body)
        self.assertIn('var previewHtml = `<img src="${previewUrl}" alt="">`;', body)
        self.assertIn(
            "var batchHtml = '<iframe src = /ocr/yunda/live/ky_inms/public/index.php/business/waybill/uploadEntry/index.html ></iframe>';",
            body,
        )

    def test_yunda_waybill_proxy_rejects_non_yunda_public_path(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.yunda_waybill_proxy")

        result = proxy.run_once({"method": "GET", "path": "https://example.com/evil.html"})

        self.assertFalse(result["ok"])
        self.assertEqual("INVALID_PROXY_PATH", result["error_code"])

    def test_yunda_waybill_proxy_rewrites_javascript_public_urls(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.yunda_waybill_proxy")
        javascript = """
        const saveUrl = "/ky_inms/public/index.php/business/waybill/entry/save.html";
        const cssIcon = "url('/ky_inms/public/static/inms/images/icon.png')";
        """

        class Response:
            status_code = 200
            headers = {"Content-Type": "application/javascript; charset=utf-8"}
            url = proxy.YUNDA_INMS_ORIGIN + "/ky_inms/public/static/inms/js/entry.js"
            content = javascript.encode("utf-8")
            text = javascript

            def raise_for_status(self):
                return None

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.yunda_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/ky_inms/public/static/inms/js/entry.js",
                    "proxy_prefix": "/ocr/yunda/live",
                }
            )

        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn('"/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/save.html"', body)
        self.assertIn("url('/ocr/yunda/live/ky_inms/public/static/inms/images/icon.png')", body)

    def test_yunda_waybill_proxy_injects_cost_visibility_helper(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.yunda_waybill_proxy")
        html = """
        <html><body>
          <div class="costInformation">
            <p class="content-title cost-title bg_ui_ls">成本信息</p>
            <div style="display:none">
              <form class="layui-form hi search_forms_dot flex">
                <div id="isNewCost" style="display:none">
                  <div id="classify_show_box"></div>
                </div>
              </form>
            </div>
          </div>
        </body></html>
        """

        class Response:
            status_code = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}
            url = proxy.YUNDA_INMS_ORIGIN + "/ky_inms/public/index.php/business/waybill/entry/indexNew.html"
            content = html.encode("utf-8")
            text = html

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("agent.tms_runtime.scripts.yunda_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/ky_inms/public/index.php/business/waybill/entry/indexNew.html",
                    "proxy_prefix": "/ocr/yunda/live",
                }
            )

        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn("codex-yunda-cost-style", body)
        self.assertIn(".costInformation > div:has(.search_forms_dot)", body)
        self.assertIn('holder.style.setProperty("display", "block", "important")', body)

    def test_yunda_waybill_proxy_passes_through_remote_error_status(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.yunda_waybill_proxy")

        class Response:
            status_code = 404
            headers = {"Content-Type": "text/plain; charset=utf-8"}
            url = proxy.YUNDA_INMS_ORIGIN + "/ky_inms/public/index.php/missing.html"
            content = b"not found"
            text = "not found"

            def raise_for_status(self):
                raise RuntimeError("raise_for_status should not be called by raw proxy")

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("agent.tms_runtime.scripts.yunda_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once({"method": "GET", "path": "/ky_inms/public/index.php/missing.html"})

        self.assertTrue(result["ok"])
        self.assertEqual(404, result["status_code"])
        self.assertEqual(b"not found", base64.b64decode(result["body_base64"]))

    def test_yunda_waybill_proxy_rewrites_proxied_origin_and_referer(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.yunda_waybill_proxy")

        class Response:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            url = f"{proxy.YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/price.html"
            content = b'{"ok":true}'
            text = '{"ok":true}'

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.yunda_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "POST",
                    "path": "/ky_inms/public/index.php/price.html",
                    "headers": {
                        "Origin": "http://123.57.106.70:8765",
                        "Referer": "http://123.57.106.70:8765/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&p=nil",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    "content_type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "body": "GrossWeight=10&Volume=1",
                }
            )

        self.assertTrue(result["ok"])
        headers = session.calls[0]["kwargs"]["headers"]
        self.assertEqual(proxy.YUNDA_INMS_ORIGIN, headers["Origin"])
        self.assertEqual(proxy.ENTRY_INDEX_URL, headers["Referer"])
        self.assertEqual("XMLHttpRequest", headers["X-Requested-With"])

    def test_yunda_waybill_proxy_is_registered_as_yunda_target(self):
        from agent.tms_runtime.dispatch import TARGET_ACCOUNT_SYSTEMS, TARGETS

        self.assertIn("yunda_waybill_proxy", TARGETS)
        self.assertEqual("yunda", TARGET_ACCOUNT_SYSTEMS["yunda_waybill_proxy"])

    def test_ronghui_waybill_proxy_resolves_entry_and_rewrites_same_origin_urls(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")
        menu_payload = {
            "result": {
                "data": [
                    {
                        "id": "1622",
                        "text": "运单录入",
                        "url": "/widget/home?authenticationKey=auth-token&pageId=page-token",
                    }
                ]
            }
        }
        html = """
        <html><head>
          <base href="https://tms.ronghuiwl.com/">
          <base href=/>
          <link href="/static/miniui2/themes/default/miniui.css">
          <meta http-equiv="refresh" content="0;url=/widget/home?page=meta">
          <meta http-equiv="Content-Security-Policy" content="default-src 'self'; frame-ancestors 'none'">
          <meta http-equiv=content-security-policy content="script-src 'self'">
          <link href="https:\\/\\/example.com\\/static\\/external.css">
          <script src="https://tms.ronghuiwl.com/static/miniui2/miniui.js"></script>
          <style>.icon{background:url(//tms.ronghuiwl.com/static/imgs/icon.png)}.rel{background:url(static/imgs/relative.png)}@font-face{font-family:Mini;src:url(/static/index/fonts/fontawesome-webfont.woff2?v=4.7.0)}</style>
          <script>
            var saveUrl = "/dataOperation/saveTables";
            var queryUrl = "https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FIND_PRODUCT_TYPE";
            var protoUrl = "//tms.ronghuiwl.com/static/js/protocol-relative.js";
            var relativeQueryUrl = "dataQuery/findAllByCallId?id=FIND_RELATIVE";
            var quoteUrl = "/fhdquote/getFhdQuote";
            var commonUrl = "/commonOption/queryDispInfoByAddress";
            var refundUrl = "/advancePayment/getRefundPayquery";
            var uploadUrl = "/file/upload";
            var downloadUrl = "/unauth/download/group1/M00/00/01/demo.png";
            var templateUrl = `/dataQuery/findAllByCallId?id=FIND_TEMPLATE`;
            var mapFrame = "<iframe id='mapContainer' src='http://sutong.api.htkj56.com/view/showFenDan?sn=abc&amp;appId=H00018'></iframe>";
            location.assign("widget/home?page=next");
            window.location.replace("/module/index?mv=index");
          </script>
        </head><body>
          <form action="/dataOperation/saveTables"></form>
          <button formaction="/dataOperation/saveTables"></button>
          <button formaction=/dataOperation/saveTables></button>
          <img src="/static/imgs/default/menu-bar-16x16.png">
          <img srcset="/static/imgs/small.png 1x, https://tms.ronghuiwl.com/static/imgs/large.png 2x">
          <div class="mini-datagrid" url=dataQuery/findGridRows></div>
          <button data-url=widget/home></button>
          <img data-src=/static/imgs/lazy.png>
          <a data-href=widget/home?page=lazy></a>
          <video poster=/file/video/poster.png></video>
          <table background=/static/imgs/table-bg.png></table>
          <object data=/file/object.bin></object>
          <iframe srcdoc="&lt;script src=&quot;/static/inline-frame.js&quot;&gt;&lt;/script&gt;&lt;form action=&quot;/dataOperation/saveTables&quot;&gt;&lt;/form&gt;"></iframe>
        </body></html>
        """

        class Response:
            def __init__(self, url, text, headers=None, json_payload=None):
                self.status_code = 200
                self.url = url
                self.text = text
                self.content = text.encode("utf-8")
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                return self._json_payload if self._json_payload is not None else json.loads(self.text or "{}")

        class Session:
            def __init__(self):
                self.calls = []
                self.cookies = [
                    types.SimpleNamespace(
                        name="userInfo",
                        value=json.dumps(
                            {
                                "loginEmpCode": "E001",
                                "loginEmpName": "勇胜",
                                "loginSiteCode": "S001",
                                "loginSiteName": "大祥",
                                "token": "secret-token",
                                "password": "secret-password",
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                ]

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                if url.endswith("/menuTreeExtend/loadMenu"):
                    return Response(url, json.dumps(menu_payload), {"Content-Type": "application/json"}, menu_payload)
                return Response(
                    url,
                    html,
                    {
                        "Content-Type": "text/html; charset=utf-8",
                        "Set-Cookie": "SESSION=secret",
                        "X-Frame-Options": "DENY",
                    },
                )

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once({"method": "GET", "path": "", "proxy_prefix": "/ocr/ronghui/live"})

        self.assertTrue(result["ok"])
        self.assertEqual(
            "https://tms.ronghuiwl.com/widget/home?authenticationKey=auth-token&pageId=page-token",
            session.calls[-1]["url"],
        )
        self.assertNotIn("set-cookie", {key.lower() for key in result["headers"]})
        self.assertNotIn("x-frame-options", {key.lower() for key in result["headers"]})
        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn('base href="/ocr/ronghui/live/"', body)
        self.assertIn("base href=/ocr/ronghui/live/", body)
        self.assertNotIn("Content-Security-Policy", body)
        self.assertNotIn("frame-ancestors", body)
        self.assertNotIn("script-src 'self'", body)
        self.assertIn('href="/ocr/ronghui/live/static/miniui2/themes/default/miniui.css"', body)
        self.assertIn('content="0;url=/ocr/ronghui/live/widget/home?page=meta"', body)
        self.assertIn('href="https:\\/\\/example.com\\/static\\/external.css"', body)
        self.assertIn('src="https://tms.ronghuiwl.com/static/miniui2/miniui.js"', body)
        self.assertIn('action="/ocr/ronghui/live/dataOperation/saveTables"', body)
        self.assertIn('formaction="/ocr/ronghui/live/dataOperation/saveTables"', body)
        self.assertIn('formaction=/ocr/ronghui/live/dataOperation/saveTables', body)
        self.assertIn('src="https://tms.ronghuiwl.com/static/imgs/default/menu-bar-16x16.png"', body)
        self.assertIn(
            'srcset="https://tms.ronghuiwl.com/static/imgs/small.png 1x, https://tms.ronghuiwl.com/static/imgs/large.png 2x"',
            body,
        )
        self.assertIn('var saveUrl = "/ocr/ronghui/live/dataOperation/saveTables";', body)
        self.assertIn(
            'var queryUrl = "/ocr/ronghui/live/dataQuery/findAllByCallId?id=FIND_PRODUCT_TYPE";',
            body,
        )
        self.assertIn("codex-ronghui-proxy-script", body)
        self.assertIn("ronghuiUserInfoCookie", body)
        self.assertIn('document.cookie = "userInfo=" + ronghuiUserInfoCookie', body)
        self.assertIn("loginEmpName", body)
        self.assertIn("loginSiteCode", body)
        self.assertNotIn("secret-token", body)
        self.assertNotIn("secret-password", body)
        self.assertIn("XMLHttpRequest.prototype.open", body)
        self.assertIn("window.fetch", body)
        self.assertIn('"https://tms.ronghuiwl.com"', body)
        self.assertIn('"/ocr/ronghui/live"', body)
        self.assertIn('background:url(https://tms.ronghuiwl.com/static/imgs/icon.png)', body)
        self.assertIn('background:url(https://tms.ronghuiwl.com/static/imgs/relative.png)', body)
        self.assertIn(
            "src:url(/ocr/ronghui/live/static/index/fonts/fontawesome-webfont.woff2?v=4.7.0)",
            body,
        )
        self.assertIn('var protoUrl = "https://tms.ronghuiwl.com/static/js/protocol-relative.js";', body)
        self.assertIn(
            'var relativeQueryUrl = "/ocr/ronghui/live/dataQuery/findAllByCallId?id=FIND_RELATIVE";',
            body,
        )
        self.assertIn('var quoteUrl = "/ocr/ronghui/live/fhdquote/getFhdQuote";', body)
        self.assertIn('var commonUrl = "/ocr/ronghui/live/commonOption/queryDispInfoByAddress";', body)
        self.assertIn('var refundUrl = "/ocr/ronghui/live/advancePayment/getRefundPayquery";', body)
        self.assertIn('var uploadUrl = "/ocr/ronghui/live/file/upload";', body)
        self.assertIn('var downloadUrl = "/ocr/ronghui/live/unauth/download/group1/M00/00/01/demo.png";', body)
        self.assertIn("var templateUrl = `/ocr/ronghui/live/dataQuery/findAllByCallId?id=FIND_TEMPLATE`;", body)
        self.assertIn('url=/ocr/ronghui/live/dataQuery/findGridRows', body)
        self.assertIn('data-url=/ocr/ronghui/live/widget/home', body)
        self.assertIn('data-src=https://tms.ronghuiwl.com/static/imgs/lazy.png', body)
        self.assertIn('data-href=/ocr/ronghui/live/widget/home?page=lazy', body)
        self.assertIn('poster=/ocr/ronghui/live/file/video/poster.png', body)
        self.assertIn('background=https://tms.ronghuiwl.com/static/imgs/table-bg.png', body)
        self.assertIn('data=/ocr/ronghui/live/file/object.bin', body)
        self.assertIn('https://tms.ronghuiwl.com/static/inline-frame.js', body)
        self.assertIn('/ocr/ronghui/live/dataOperation/saveTables', body)
        self.assertIn(
            "id='mapContainer' src='about:blank' data-codex-deferred-src='http://sutong.api.htkj56.com/view/showFenDan?sn=abc&amp;appId=H00018'",
            body,
        )
        self.assertNotIn("id='mapContainer' src='http://sutong.api.htkj56.com/view/showFenDan", body)
        self.assertIn('location.assign("/ocr/ronghui/live/widget/home?page=next");', body)
        self.assertIn('window.location.replace("/ocr/ronghui/live/module/index?mv=index");', body)

    def test_ronghui_waybill_proxy_resolves_entry_with_browser_xhr_headers(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")
        menu_payload = {
            "result": {
                "data": [
                    {
                        "id": "1622",
                        "text": "运单录入",
                        "url": "/widget/home?authenticationKey=auth-token&pageId=page-token",
                    }
                ]
            }
        }

        class Response:
            def __init__(self, url, text, headers=None, json_payload=None):
                self.status_code = 200
                self.url = url
                self.text = text
                self.content = text.encode("utf-8")
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                return self._json_payload if self._json_payload is not None else json.loads(self.text or "{}")

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                if url.endswith("/menuTreeExtend/loadMenu"):
                    return Response(url, json.dumps(menu_payload), {"Content-Type": "application/json"}, menu_payload)
                return Response(url, "<html><head></head><body></body></html>", {"Content-Type": "text/html; charset=utf-8"})

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once({"method": "GET", "path": "", "proxy_prefix": "/ocr/ronghui/live"})

        self.assertTrue(result["ok"])
        self.assertEqual("POST", session.calls[0]["method"])
        self.assertEqual(f"{proxy.RONGHUI_ORIGIN}{proxy.MENU_PATH}", session.calls[0]["url"])
        headers = session.calls[0]["kwargs"]["headers"]
        self.assertEqual(proxy.RONGHUI_ORIGIN, headers["Origin"])
        self.assertEqual(proxy.RONGHUI_ENTRY_REFERER, headers["Referer"])
        self.assertEqual("XMLHttpRequest", headers["X-Requested-With"])
        self.assertIn("application/json", headers["Accept"])

    def test_ronghui_waybill_proxy_resolves_custom_entry_menu_text(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")
        menu_payload = {
            "result": {
                "data": [
                    {
                        "id": "1622",
                        "text": "运单录入",
                        "url": "/widget/home?authenticationKey=waybill-auth&pageId=waybill-page",
                    },
                    {
                        "id": "receipt-send",
                        "text": "寄方回单跟踪",
                        "url": "/widget/home?authenticationKey=receipt-auth&pageId=receipt-page",
                    },
                ]
            }
        }

        class Response:
            def __init__(self, url, text, headers=None, json_payload=None):
                self.status_code = 200
                self.url = url
                self.text = text
                self.content = text.encode("utf-8")
                self.headers = headers or {}
                self._json_payload = json_payload

            def json(self):
                return self._json_payload if self._json_payload is not None else json.loads(self.text or "{}")

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                if url.endswith("/menuTreeExtend/loadMenu"):
                    return Response(url, json.dumps(menu_payload), {"Content-Type": "application/json"}, menu_payload)
                return Response(url, "<html><head></head><body>receipt page</body></html>", {"Content-Type": "text/html; charset=utf-8"})

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "",
                    "proxy_prefix": "/receipts/ronghui/live",
                    "entry_menu_text": "寄方回单跟踪",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            "https://tms.ronghuiwl.com/widget/home?authenticationKey=receipt-auth&pageId=receipt-page",
            session.calls[-1]["url"],
        )
        self.assertEqual("/widget/home", result["remote_path"])
        self.assertEqual("authenticationKey=receipt-auth&pageId=receipt-page", result["remote_query"])

    def test_ronghui_waybill_proxy_caches_static_lookup_gets_without_cache_buster(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")
        proxy._RONGHUI_PROXY_LOOKUP_CACHE.clear()

        class Response:
            status_code = 200
            headers = {"Content-Type": "application/json; charset=utf-8"}

            def __init__(self, url):
                self.url = url
                self.content = b'{"ok":true,"rows":[{"name":"cached"}]}'
                self.text = self.content.decode("utf-8")

        class Session:
            cookies = {}

            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return Response(url)

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        try:
            with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
                first = proxy.run_once(
                    {
                        "method": "GET",
                        "path": "/minic/combobox",
                        "query": "optionCode=CARD_TYPE&_=1",
                        "proxy_prefix": "/ocr/ronghui/live",
                    }
                )
                second = proxy.run_once(
                    {
                        "method": "GET",
                        "path": "/minic/combobox",
                        "query": "optionCode=CARD_TYPE&_=2",
                        "proxy_prefix": "/ocr/ronghui/live",
                    }
                )
        finally:
            proxy._RONGHUI_PROXY_LOOKUP_CACHE.clear()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(1, len(session.calls))
        self.assertEqual(first["body_base64"], second["body_base64"])
        self.assertEqual("private, max-age=300", first["headers"]["Cache-Control"])
        self.assertEqual("hit", second["headers"]["X-Codex-Proxy-Cache"])
        self.assertEqual("optionCode=CARD_TYPE&_=2", second["remote_query"])

    def test_ronghui_waybill_proxy_sanitizes_user_info_cookie_for_client(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")
        raw_payload = {
            "loginEmpCode": "E001",
            "loginEmpName": "勇胜",
            "loginSiteCode": "S001",
            "loginSiteName": "大祥",
            "token": "secret-token",
            "password": "secret-password",
        }
        encoded_cookie = proxy._js_escape_cookie_value(
            json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":"))
        )
        session = types.SimpleNamespace(
            cookies=[types.SimpleNamespace(name="userInfo", value=encoded_cookie)]
        )

        client_cookie = proxy._client_user_info_cookie_from_session(session)
        decoded = proxy._parse_user_info_cookie(client_cookie)

        self.assertEqual("勇胜", decoded["loginEmpName"])
        self.assertEqual("S001", decoded["loginSiteCode"])
        self.assertNotIn("token", decoded)
        self.assertNotIn("password", decoded)
        self.assertNotIn("secret-token", client_cookie)
        self.assertNotIn("secret-password", client_cookie)

    def test_ronghui_waybill_proxy_preserves_explicit_widget_home_query(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}
            text = "<html><head></head><body>next</body></html>"
            content = text.encode("utf-8")

            def __init__(self, url):
                self.url = url

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response(url)

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker) as get_broker:
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/widget/home",
                    "query": "page=next&_winid=abc",
                    "proxy_prefix": "/ocr/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        get_broker.assert_called_once_with("price")
        self.assertEqual(1, len(session.calls))
        self.assertEqual("https://tms.ronghuiwl.com/widget/home?page=next&_winid=abc", session.calls[0]["url"])
        self.assertEqual("/widget/home", result["remote_path"])
        self.assertEqual("page=next&_winid=abc", result["remote_query"])

    def test_ronghui_waybill_proxy_menu_login_page_raises_auth_required(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")
        from agent.tms_runtime.errors import TMSAuthStateError

        class Response:
            status_code = 200
            url = "https://tms.ronghuiwl.com/system/login"
            text = '<html><form id="loinform"><input name="validateCode"></form></html>'
            content = text.encode("utf-8")
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def json(self):
                raise ValueError("not json")

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        with self.assertRaises(TMSAuthStateError) as ctx:
            proxy._resolve_entry_url(Session())

        self.assertEqual("AUTH_REQUIRED", getattr(ctx.exception, "code", ""))

    def test_ronghui_waybill_proxy_login_redirect_raises_auth_required(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")
        from agent.tms_runtime.errors import TMSAuthStateError

        class Response:
            status_code = 302
            url = "https://tms.ronghuiwl.com/widget/home"
            headers = {"Location": "/system/login"}
            content = b""
            text = ""

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            with self.assertRaises(TMSAuthStateError) as ctx:
                proxy.run_once({"method": "GET", "path": "/widget/home", "proxy_prefix": "/ocr/ronghui/live"})

        self.assertEqual("AUTH_REQUIRED", getattr(ctx.exception, "code", ""))

    def test_ronghui_runtime_helper_rewrites_dynamic_element_and_window_urls(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        helper = proxy._runtime_proxy_helper(proxy_prefix="/ocr/ronghui/live")

        self.assertIn("Element.prototype.setAttribute", helper)
        self.assertIn("HTMLIFrameElement", helper)
        self.assertIn("srcdoc", helper)
        self.assertIn("HTMLScriptElement", helper)
        self.assertIn("HTMLFormElement", helper)
        self.assertIn("HTMLFormElement.prototype.submit", helper)
        self.assertIn("HTMLButtonElement", helper)
        self.assertIn("HTMLInputElement", helper)
        self.assertIn('patchUrlProperty(window.HTMLInputElement && window.HTMLInputElement.prototype, "src")', helper)
        self.assertIn("HTMLAreaElement", helper)
        self.assertIn("HTMLSourceElement", helper)
        self.assertIn("HTMLVideoElement", helper)
        self.assertIn("HTMLAudioElement", helper)
        self.assertIn("HTMLTrackElement", helper)
        self.assertIn("HTMLEmbedElement", helper)
        self.assertIn("HTMLObjectElement", helper)
        self.assertIn("HTMLMetaElement", helper)
        self.assertIn("isMetaContentSecurityPolicy", helper)
        self.assertIn("removeMetaContentSecurityPolicy", helper)
        self.assertIn("content-security-policy", helper)
        self.assertIn("HTMLBaseElement", helper)
        self.assertIn("window.open", helper)
        self.assertIn("sendBeacon", helper)
        self.assertIn("EventSource", helper)
        self.assertIn("Worker", helper)
        self.assertIn("SharedWorker", helper)
        self.assertIn("patchUrlConstructor", helper)
        self.assertIn('"src"', helper)
        self.assertIn('"srcset"', helper)
        self.assertIn('"href"', helper)
        self.assertIn('"action"', helper)
        self.assertIn('"formaction"', helper)
        self.assertIn('"poster"', helper)
        self.assertIn('"data"', helper)
        self.assertIn('patchHistoryMethod("pushState")', helper)
        self.assertIn('patchHistoryMethod("replaceState")', helper)
        self.assertIn('"url"', helper)
        self.assertIn('"data-url"', helper)
        self.assertIn('"data-src"', helper)
        self.assertIn('"data-href"', helper)
        self.assertIn('"poster"', helper)
        self.assertIn('"background"', helper)
        self.assertIn('key === "data"', helper)
        self.assertIn('"object[data]"', helper)
        self.assertIn("rewriteHtmlText", helper)
        self.assertIn("mayContainRonghuiReference", helper)
        self.assertIn("if (!mayContainRonghuiReference(value)) return value;", helper)
        self.assertIn("rewritingHtmlText", helper)
        self.assertIn("if (rewritingHtmlText) return value;", helper)
        self.assertIn("insertAdjacentHTML", helper)
        self.assertIn("innerHTML", helper)
        self.assertIn("outerHTML", helper)
        self.assertIn("document.write", helper)
        self.assertIn("document.writeln", helper)
        self.assertIn("rewriteMetaRefreshContent", helper)
        self.assertIn("rewriteMetaRefreshElement", helper)
        self.assertIn("rewriteRonghuiBaseUrl", helper)
        self.assertIn("shouldKeepStaticSameOrigin", helper)
        self.assertIn("rewriteBaseHrefElement", helper)
        self.assertIn("MutationObserver", helper)
        self.assertIn("observeAddedNodes", helper)
        self.assertIn("mutation.addedNodes", helper)
        self.assertIn('mutation.type === "attributes"', helper)
        self.assertIn("mutation.target", helper)
        self.assertIn("attributeFilter", helper)
        self.assertIn('"srcdoc"', helper)
        self.assertIn("rewriteStyleText", helper)
        self.assertIn("rewriteCssImportText", helper)
        self.assertIn("@import", helper)
        self.assertIn("CSSStyleDeclaration", helper)
        self.assertIn("CSSStyleSheet", helper)
        self.assertIn("insertRule", helper)
        self.assertIn('"style"', helper)
        self.assertIn("[style]", helper)
        self.assertIn("rewrite.call(this, value)", helper)
        self.assertIn("rewriteAjaxOptions", helper)
        self.assertIn("patchAjaxLibrary", helper)
        self.assertIn("jQuery.ajax", helper)
        self.assertIn("$.ajax", helper)
        self.assertIn("patchMiniLibrary", helper)
        self.assertIn("mini.open", helper)
        self.assertIn("mini.ajax", helper)
        self.assertIn("__codexRonghuiMiniOpenPatched", helper)
        self.assertIn("__codexRonghuiMiniAjaxPatched", helper)
        self.assertIn('patchDeferredMiniGlobal("mini")', helper)
        self.assertIn("loadDeferredRonghuiMapFrame", helper)
        self.assertIn("patchDeferredRonghuiMapFrame", helper)
        self.assertIn("getDispInfoByAddress", helper)
        self.assertIn("data-codex-deferred-src", helper)
        self.assertIn("normalizeLookupCacheBuster", helper)
        self.assertIn("cacheableDataQueryCallIds", helper)
        self.assertIn("FIND_CREATE_BILL_DESTINATION", helper)
        self.assertIn("CARD_TYPE", helper)
        self.assertEqual(1, helper.count("function patchFormSubmit()"))
        self.assertIn('typeof input.href === "string"', helper)
        self.assertNotIn("window.location.assign =", helper)
        self.assertNotIn("window.location.replace =", helper)

    def test_ronghui_waybill_proxy_rewrites_json_response_urls(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 200
            url = "https://tms.ronghuiwl.com/dataQuery/findAllByCallId?id=FILES"
            headers = {"Content-Type": "application/json; charset=utf-8"}
            text = json.dumps(
                {
                    "download": "/unauth/download/group1/M00/00/01/pod.jpg",
                    "next": "dataQuery/findAllByCallId?id=NEXT",
                    "script": "https://tms.ronghuiwl.com/static/miniui2/miniui.js",
                    "escaped_next": "\\/dataQuery\\/findAllByCallId?id=ESCAPED",
                    "escaped_script": "https:\\/\\/tms.ronghuiwl.com\\/static\\/escaped.js",
                    "external": "https://example.com/static/app.js",
                }
            )
            content = text.encode("utf-8")

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/dataQuery/findAllByCallId",
                    "query": "id=FILES",
                    "proxy_prefix": "/ocr/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn('"/ocr/ronghui/live/unauth/download/group1/M00/00/01/pod.jpg"', body)
        self.assertIn('"/ocr/ronghui/live/dataQuery/findAllByCallId?id=NEXT"', body)
        self.assertIn('"https://tms.ronghuiwl.com/static/miniui2/miniui.js"', body)
        self.assertIn('"/ocr/ronghui/live/dataQuery/findAllByCallId?id=ESCAPED"', body)
        self.assertIn('"https://tms.ronghuiwl.com/static/escaped.js"', body)
        self.assertIn('"https://example.com/static/app.js"', body)

    def test_ronghui_waybill_proxy_rewrites_text_plain_response_urls(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 200
            url = "https://tms.ronghuiwl.com/commonOption/commonHttpGet"
            headers = {"Content-Type": "text/plain; charset=utf-8"}
            text = '{"download":"/unauth/download/group1/M00/00/01/plain.jpg","next":"dataQuery/findAllByCallId?id=PLAIN"}'
            content = text.encode("utf-8")

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/commonOption/commonHttpGet",
                    "proxy_prefix": "/ocr/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn('"/ocr/ronghui/live/unauth/download/group1/M00/00/01/plain.jpg"', body)
        self.assertIn('"/ocr/ronghui/live/dataQuery/findAllByCallId?id=PLAIN"', body)

    def test_ronghui_waybill_proxy_does_not_corrupt_javascript_boot_fragments(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")
        boot_js = "\n".join(
            [
                'var bootPATH = __CreateJSPath("boot.js");',
                'document.write(\'<script src="\' + bootPATH + \'jquery.min.js" type="text/javascript"></sc\' + \'ript>\');',
                'document.write(\'<link href="\' + bootPATH + \'themes/default/miniui.css" rel="stylesheet" type="text/css" />\');',
            ]
        )

        class Response:
            status_code = 200
            url = "https://tms.ronghuiwl.com/static/miniui2/boot.js"
            headers = {"Content-Type": "application/javascript", "Last-Modified": "Fri, 29 May 2026 06:46:35 GMT"}
            text = boot_js
            content = boot_js.encode("utf-8")

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/static/miniui2/boot.js",
                    "headers": {
                        "If-Modified-Since": "Fri, 29 May 2026 06:46:35 GMT",
                        "If-None-Match": '"cached"',
                    },
                    "proxy_prefix": "/ocr/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        forwarded_headers = session.calls[0]["kwargs"]["headers"]
        self.assertNotIn("If-Modified-Since", forwarded_headers)
        self.assertNotIn("If-None-Match", forwarded_headers)
        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertEqual(boot_js, body)
        self.assertEqual("no-store", result["headers"]["Cache-Control"])

    def test_ronghui_waybill_proxy_caches_same_origin_css_and_fonts(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 200
            url = "https://tms.ronghuiwl.com/static/miniui2/themes/default/miniui.css"
            headers = {"Content-Type": "text/css; charset=utf-8"}
            text = (
                "@font-face{font-family:FontAwesome;"
                "src:url(/static/index/fonts/fontawesome-webfont.woff2?v=4.7.0)}"
                ".icon{background:url(/static/imgs/icon.png)}"
            )
            content = text.encode("utf-8")

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/static/miniui2/themes/default/miniui.css",
                    "proxy_prefix": "/ocr/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual("public, max-age=86400", result["headers"]["Cache-Control"])
        self.assertNotIn("Pragma", result["headers"])
        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn(
            "src:url(/ocr/ronghui/live/static/index/fonts/fontawesome-webfont.woff2?v=4.7.0)",
            body,
        )
        self.assertIn("background:url(https://tms.ronghuiwl.com/static/imgs/icon.png)", body)

    def test_ronghui_waybill_proxy_rewrites_xml_response_urls(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 200
            url = "https://tms.ronghuiwl.com/module/config.xml"
            headers = {"Content-Type": "application/xml; charset=utf-8"}
            text = '<root icon="/static/imgs/icon.svg" data="dataQuery/findAllByCallId?id=XML"></root>'
            content = text.encode("utf-8")

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/module/config.xml",
                    "proxy_prefix": "/ocr/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn('icon="https://tms.ronghuiwl.com/static/imgs/icon.svg"', body)
        self.assertIn('data="/ocr/ronghui/live/dataQuery/findAllByCallId?id=XML"', body)

    def test_ronghui_waybill_proxy_rewrites_xhtml_response_and_injects_helper(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 200
            url = "https://tms.ronghuiwl.com/widget/home"
            headers = {"Content-Type": "application/xhtml+xml; charset=utf-8"}
            text = '<html><head><link href="/static/miniui2/miniui.css"/></head><body></body></html>'
            content = text.encode("utf-8")

        class Session:
            def request(self, method, url, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/widget/home",
                    "proxy_prefix": "/ocr/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        body = base64.b64decode(result["body_base64"]).decode("utf-8")
        self.assertIn('href="/ocr/ronghui/live/static/miniui2/miniui.css"', body)
        self.assertIn("codex-ronghui-proxy-script", body)

    def test_ronghui_waybill_proxy_allows_entry_auxiliary_paths_seen_in_live_page(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            content = b'{"success":true}'
            text = '{"success":true}'

            def __init__(self, url):
                self.url = url

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response(url)

        auxiliary_paths = [
            "/advancePayment/getRefundPayquery",
            "/commonOption/commonHttpGet",
            "/commonOption/queryDispInfoByAddress",
            "/fhdquote/getFhdQuote",
            "/file/upload",
            "/unauth/download/group1/M00/00/01/demo.png",
        ]
        for auxiliary_path in auxiliary_paths:
            with self.subTest(path=auxiliary_path):
                session = Session()
                broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
                with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
                    result = proxy.run_once({"method": "GET", "path": auxiliary_path, "query": "id=1"})

                self.assertTrue(result["ok"])
                self.assertEqual(f"https://tms.ronghuiwl.com{auxiliary_path}?id=1", session.calls[0]["url"])

    def test_ronghui_waybill_proxy_rewrites_headers_for_post(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 200
            url = "https://tms.ronghuiwl.com/dataOperation/saveTables"
            headers = {"Content-Type": "application/json"}
            content = b'{"success":true}'
            text = '{"success":true}'

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "POST",
                    "path": "/dataOperation/saveTables",
                    "headers": {
                        "Origin": "http://127.0.0.1:8765",
                        "Referer": "http://127.0.0.1:8765/ocr/ronghui/live",
                        "Cookie": "secret=1",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    "content_type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "body": "data=1",
                }
            )

        self.assertTrue(result["ok"])
        headers = session.calls[0]["kwargs"]["headers"]
        self.assertEqual(proxy.RONGHUI_ORIGIN, headers["Origin"])
        self.assertEqual(proxy.RONGHUI_ENTRY_REFERER, headers["Referer"])
        self.assertEqual("XMLHttpRequest", headers["X-Requested-With"])
        self.assertNotIn("Cookie", headers)

    def test_ronghui_waybill_proxy_follows_safe_download_redirects(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            def __init__(self, status_code, url, headers=None, content=b""):
                self.status_code = status_code
                self.url = url
                self.headers = headers or {}
                self.content = content
                self.text = content.decode("utf-8", errors="ignore")

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                if len(self.calls) == 1:
                    return Response(
                        302,
                        "https://tms.ronghuiwl.com/unauth/download/group1/M00/00/01/demo.jpg",
                        {"Location": "https://img.ronghuiwl.com/group1/M00/00/01/demo-real.jpg"},
                    )
                return Response(
                    200,
                    "https://img.ronghuiwl.com/group1/M00/00/01/demo-real.jpg",
                    {"Content-Type": "image/jpeg"},
                    b"\xff\xd8image",
                )

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/unauth/download/group1/M00/00/01/demo.jpg",
                    "proxy_prefix": "/receipts/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(200, result["status_code"])
        self.assertEqual(
            "https://img.ronghuiwl.com/group1/M00/00/01/demo-real.jpg",
            session.calls[1]["url"],
        )
        self.assertEqual(b"\xff\xd8image", base64.b64decode(result["body_base64"]))

    def test_ronghui_waybill_proxy_does_not_follow_external_download_redirects(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 302
            url = "https://tms.ronghuiwl.com/unauth/download/group1/M00/00/01/demo.jpg"
            headers = {"Location": "https://example.com/evil.jpg", "Content-Type": "text/plain; charset=utf-8"}
            content = b""
            text = ""

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "GET",
                    "path": "/unauth/download/group1/M00/00/01/demo.jpg",
                    "proxy_prefix": "/receipts/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(302, result["status_code"])
        self.assertEqual(1, len(session.calls))

    def test_ronghui_waybill_proxy_rewrites_redirect_response_headers(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        class Response:
            status_code = 302
            url = "https://tms.ronghuiwl.com/dataOperation/saveTables"
            headers = {
                "Location": "/widget/home?page=next",
                "Refresh": "0; url=https://tms.ronghuiwl.com/module/index?mv=index",
                "X-External-Location": "https://example.com/static/app.js",
                "Content-Type": "text/plain; charset=utf-8",
                "X-Frame-Options": "DENY",
            }
            content = b""
            text = ""

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append({"method": method, "url": url, "kwargs": kwargs})
                return Response()

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("agent.tms_runtime.scripts.ronghui_waybill_proxy.get_session_broker", return_value=broker):
            result = proxy.run_once(
                {
                    "method": "POST",
                    "path": "/dataOperation/saveTables",
                    "proxy_prefix": "/ocr/ronghui/live",
                }
            )

        self.assertTrue(result["ok"])
        self.assertIs(session.calls[0]["kwargs"]["allow_redirects"], False)
        self.assertEqual("/ocr/ronghui/live/widget/home?page=next", result["headers"]["Location"])
        self.assertEqual("0; url=/ocr/ronghui/live/module/index?mv=index", result["headers"]["Refresh"])
        self.assertEqual("https://example.com/static/app.js", result["headers"]["X-External-Location"])
        self.assertNotIn("X-Frame-Options", result["headers"])

    def test_ronghui_waybill_proxy_rejects_non_ronghui_url(self):
        proxy = importlib.import_module("agent.tms_runtime.scripts.ronghui_waybill_proxy")

        result = proxy.run_once({"method": "GET", "path": "https://example.com/static/app.js"})

        self.assertFalse(result["ok"])
        self.assertEqual("INVALID_PROXY_PATH", result["error_code"])

    def test_ronghui_waybill_proxy_is_registered_as_ronghui_target(self):
        from agent.tms_runtime.dispatch import TARGET_ACCOUNT_PURPOSES, TARGET_ACCOUNT_SYSTEMS, TARGETS

        self.assertIn("ronghui_waybill_proxy", TARGETS)
        self.assertEqual("ronghui", TARGET_ACCOUNT_SYSTEMS["ronghui_waybill_proxy"])
        self.assertEqual("price", TARGET_ACCOUNT_PURPOSES["ronghui_waybill_proxy"])
        self.assertGreaterEqual(TARGETS["ronghui_waybill_proxy"].max_concurrency, 12)

    def test_yunda_price_entry_base_form_reads_script_defaults(self):
        form = yunda_price._entry_base_form(
            {
                "default_form": {
                    "CreatedDotCode": "56739382",
                    "SenderDistributionCode": "",
                    "SenderDistributionName": "湖南长沙分拨中心",
                    "PackageByCode": "",
                },
                "fields": {},
                "html": "var SenderDistributionCode = '56731000'; var CreatedByCode = \"56739382003\";",
            },
            {"current_time": "2026-05-25 18:04:37"},
        )

        self.assertEqual("56731000", form["SenderDistributionCode"])
        self.assertEqual("56739382003", form["CreatedByCode"])

    def test_yunda_price_builds_heavy_and_fixed_cost_tasks(self):
        task = yunda_price.build_trial_task(
            address="湖南省长沙市岳麓区梅溪湖",
            weight="120.50",
            volume="0.30",
            service_mode="自提",
            uuid_value="UUID1",
            sort=2,
        )

        self.assertEqual("湖南省长沙市岳麓区梅溪湖", task["Buyer_Address"])
        self.assertEqual("120.5", task["Gross_Weight"])
        self.assertEqual("0.3", task["Volume"])
        self.assertEqual("自提", task["Service_Type"])
        self.assertEqual("是", task["Check_Heavy_Weight"])
        self.assertEqual("是", task["Check_Fixed_Cost"])

        dispatch_task = yunda_price.build_trial_task(
            address="湖南省长沙市岳麓区梅溪湖",
            weight=120,
            volume=0.3,
            service_mode="派送",
            uuid_value="UUID1",
            sort=1,
        )
        self.assertEqual("", dispatch_task["Service_Type"])

    def test_yunda_price_extracts_total_cost_only(self):
        tasks = [
            {"Remark": "YD_PRICE_PS", "Service_Type": ""},
            {"Remark": "YD_PRICE_ZT", "Service_Type": "自提"},
        ]
        prices = yunda_price._extract_prices(
            [
                {"Remark": "YD_PRICE_ZT", "Trial_Status": "1", "Total_Cost": "120"},
                {"Remark": "YD_PRICE_PS", "Trial_Status": "1", "Total_Cost": "138.5", "1Kg_Cost": "1.23"},
            ],
            tasks,
        )

        self.assertEqual({"韵达派送": "138.50元", "韵达自提": "120.00元"}, prices)

    def test_yunda_price_extracts_row_detail_and_cost_summary(self):
        details = yunda_price._extract_row_details(
            [
                {
                    "Remark": "YD_PRICE_PS",
                    "Trial_Status": "1",
                    "Buyer_Destination_Dot_Name": "贵州毕节赫章县公司",
                    "Buyer_Destination_Dot_Code": "56858947",
                    "Send_Msg": "镇上自提*",
                    "Sender_Distribution_Name": "湖南长沙分拨中心",
                    "Tfr_Weight": "1000.00",
                    "Cost_Detail": json.dumps(
                        {
                            "FixedCost": "390",
                            "SendCost": "256.24",
                            "TownSendCost": "105.19",
                            "CostTotal": "765.13",
                        }
                    ),
                }
            ],
            [{"Remark": "YD_PRICE_PS", "Service_Type": ""}],
        )

        self.assertEqual("贵州毕节赫章县公司", details["派送"]["目的网点"])
        self.assertEqual("镇上自提*", details["派送"]["是否派送"])
        self.assertEqual("390.00元", details["派送"]["费用明细"]["特惠一口价"])
        self.assertEqual("765.13元", details["派送"]["费用明细"]["合计"])

    def test_yunda_price_failed_trial_row_raises_clear_error(self):
        tasks = [{"Remark": "YD_PRICE_PS", "Service_Type": ""}]
        with self.assertRaises(yunda_price.YundaPriceError) as ctx:
            yunda_price._extract_prices(
                [{"Remark": "YD_PRICE_PS", "Trial_Status": "2", "Trial_Description": "匹配不到"}],
                tasks,
            )

        self.assertIn("韵达派送试算失败", str(ctx.exception))

    def test_yunda_price_entry_message_fee_matches_checked_sms_flags(self):
        self.assertEqual(Decimal("0.05"), yunda_price._entry_message_fee({"DispatchSms": "1"}))
        self.assertEqual(
            Decimal("0.10"),
            yunda_price._entry_message_fee({"DeliversSms1": "1", "DispatchSms": "1", "IsSendMsg": "0"}),
        )
        self.assertEqual(Decimal("0.00"), yunda_price._entry_message_fee({"DispatchSms": "0", "IsCod": "0"}))

    def test_yunda_price_entry_total_adds_message_fee(self):
        total = yunda_price._entry_total_text(
            {"info": "1", "data": {"CostTotal": "563.70"}},
            service_mode="自提",
            form={"DispatchSms": "1"},
        )

        self.assertEqual("563.75元", total)

    def test_yunda_price_disables_heavy_weight_when_volume_is_too_large(self):
        page_context = {
            "default_form": {
                "CreatedDotCode": "56739382",
                "SenderDistributionCode": "56731000",
                "PackageByCode": "56739382001",
                "ProductType": "24",
                "PaymentType": "102",
                "GoodsType": "184",
                "Freight": "0.00",
                "InsuredAmount": "11000",
            },
            "fields": {},
            "html": "var $BubbleRatio = '3000'; var $HeavyMinWeight = '50'; var CreatedByCode = '56739382003';",
        }
        address_detail = {
            "省": "四川省",
            "市": "绵阳市",
            "区县": "涪城区",
            "详细地址": "石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
            "地址解析明细": {"Buyer_Province": "510000", "Buyer_City": "510700", "Buyer_Area": "510703"},
            "raw": {
                "target_center_code": "56816191",
                "target_center": "四川绵阳涪城石塘公司",
                "business_center_code": "56280000",
                "business_center": "四川成都分拨中心",
                "BuyerTownCode": "510703011",
            },
        }

        large_volume_form = yunda_price._build_entry_price_form(
            page_context=page_context,
            remote_context={"current_time": "2026-05-25 18:04:37"},
            address_detail=address_detail,
            address="四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
            weight=1000,
            volume=30,
            service_mode="派送",
        )
        normal_volume_form = yunda_price._build_entry_price_form(
            page_context=page_context,
            remote_context={"current_time": "2026-05-25 18:04:37"},
            address_detail=address_detail,
            address="四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
            weight=1000,
            volume=0.1,
            service_mode="派送",
        )

        self.assertEqual("0", large_volume_form["CheckHeavyWeight"])
        self.assertEqual("1", large_volume_form["CheckFixedCost"])
        self.assertEqual("1", normal_volume_form["CheckHeavyWeight"])
        self.assertEqual("1", normal_volume_form["CheckFixedCost"])

    def test_yunda_price_preserves_entry_page_declared_value(self):
        page_context = {
            "default_form": {
                "CreatedDotCode": "56739382",
                "SenderDistributionCode": "56731000",
                "PackageByCode": "56739382001",
                "ProductType": "24",
                "PaymentType": "102",
                "GoodsType": "184",
                "InGoodsType": "184",
                "Freight": "0.00",
                "InsuredAmount": "2000",
            },
            "fields": {},
            "html": "var $BubbleRatio = '3000'; var $HeavyMinWeight = '50'; var CreatedByCode = '56739382003';",
        }
        address_detail = {
            "省": "云南省",
            "市": "曲靖市",
            "区县": "麒麟区",
            "详细地址": "麒麟南路186号",
            "地址解析明细": {"Buyer_Province": "530000", "Buyer_City": "530300", "Buyer_Area": "530302"},
            "raw": {
                "target_center_code": "56789901",
                "target_center": "云南曲靖市麒麟区公司",
                "business_center_code": "56730000",
                "business_center": "云南昆明分拨中心",
                "BuyerTownCode": "530302002",
            },
        }

        form = yunda_price._build_entry_price_form(
            page_context=page_context,
            remote_context={"current_time": "2026-05-28 11:31:22"},
            address_detail=address_detail,
            address="云南省曲靖市麒麟区麒麟南路186号",
            weight=100,
            volume=0.1,
            service_mode="派送",
            weight_payload={"info": 1, "data": 100, "Tfr": 100, "Del": 100},
        )

        self.assertEqual("2000", form["InsuredAmount"])

    def test_yunda_price_uses_entry_weight_api_for_large_volume_chargeable_weight(self):
        html = """
        <html><body>
          <input name="CreatedDotCode" value="56739382">
          <input name="SenderDistributionCode" value="56731000">
          <input name="PackageByCode" value="56739382001">
          <input name="ProductType" value="24">
          <input name="PaymentType" value="102">
          <input name="GoodsType" value="184">
          <input name="InGoodsType" value="184">
          <input name="OrderSource" value="65">
          <input name="ItemTotalNumber" value="1">
          <input name="Freight" value="0.00">
          <input name="InsuredAmount" value="15000">
          <input type="checkbox" name="DispatchSms" value="1" checked disabled>
          <input name="IsSendMsg" value="0">
          <input name="IsCod" value="0">
          <input name="IsDiscount" value="2">
          <script>var $BubbleRatio = '3000'; var $HeavyMinWeight = '5';</script>
        </body></html>
        """

        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}

            def __init__(self, payload, *, text=None, url=""):
                self._payload = payload
                self.text = "{}" if text is None else text
                self.url = url

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, headers=None, allow_redirects=None, timeout=None):
                if url == yunda_price.ENTRY_INDEX_URL:
                    return Response({}, text=html, url=url)
                if url == yunda_waybill_entry.ELEC_STOCK_URL:
                    return Response({"info": "1", "data": {"num": 835}}, url=url)
                raise AssertionError(url)

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                stored_data = dict(data or {}) if isinstance(data, dict) else list(data or [])
                self.calls.append({"url": url, "data": stored_data})
                if url == yunda_waybill_entry.CURRENT_TIME_URL:
                    return Response({"info": "1", "data": "2026-05-26 11:31:22"})
                if url == yunda_price.ADDRESS_ANALYSIS_URL:
                    return Response({
                        "info": "1",
                        "data": {
                            "Buyer_Province": "510000",
                            "Buyer_City": "510700",
                            "Buyer_Area": "510703",
                            "Buyer_Province_Name": "四川省",
                            "Buyer_City_Name": "绵阳市",
                            "Buyer_Area_Name": "涪城区",
                            "Buyer_Address": "石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
                        },
                    })
                if url == yunda_price.ADDRESS_SITE_URL:
                    return Response({
                        "info": "1",
                        "data": {
                            "51070301": {
                                "target_center_code": "56816191",
                                "target_center": "四川绵阳涪城石塘公司",
                                "business_center_code": "56280000",
                                "business_center": "四川成都分拨中心",
                                "BuyerTownCode": "510703011",
                                "BuyerTown": "石塘街道",
                                "SendMsg": "派送",
                            }
                        },
                    })
                if url == "https://kyinms.yunda56.com/ky_inms/public/index.php/weight.html":
                    return Response({"info": 1, "data": 6000, "volRate": 5, "Tfr": 6000, "Del": 6000})
                if url == yunda_price.ENTRY_INSURED_AMOUNT_URL:
                    return Response({"info": 1, "data": {"MIN": 0, "MAX": 200000}})
                if url == yunda_waybill_entry.CHECK_SERVICE_SCOPE_URL:
                    return Response({"info": "1", "data": {}})
                if url == yunda_waybill_entry.PRICE_URL:
                    return Response({"info": "1", "data": {"CostTotal": "3814.30"}})
                raise AssertionError(url)

        session = Session()
        yunda_price.fetch_yunda_prices(
            session,
            address="四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
            weight=1000,
            volume=30,
        )

        weight_call = next(
            call
            for call in session.calls
            if call["url"] == "https://kyinms.yunda56.com/ky_inms/public/index.php/weight.html"
        )
        self.assertEqual("30", weight_call["data"]["vol"])
        self.assertEqual("1000", weight_call["data"]["GrossWeight"])
        self.assertEqual("56816191", weight_call["data"]["BuyerDestinationDotCode"])
        price_calls = [call for call in session.calls if call["url"] == yunda_waybill_entry.PRICE_URL]
        self.assertEqual(2, len(price_calls))
        for call in price_calls:
            self.assertEqual("1000", call["data"]["GrossWeight"])
            self.assertEqual("30", call["data"]["Volume"])
            self.assertEqual("0", call["data"]["ItemTotalNumber"])
            self.assertEqual("6000", call["data"]["SettlementTotalNumber"])
            self.assertEqual("6000", call["data"]["Tfr"])
            self.assertEqual("6000", call["data"]["Del"])
            self.assertEqual("6000", call["data"]["VolWeight"])
            self.assertEqual("0", call["data"]["CheckHeavyWeight"])
            self.assertEqual("1", call["data"]["CheckFixedCost"])

    def test_yunda_price_fetch_uses_waybill_entry_price_endpoint(self):
        html = """
        <html><body>
          <input name="CreatedDotCode" value="56739382">
          <input name="CreatedDotname" value="湖南邵阳双清滨江公司">
          <input name="SenderDistributionCode" value="56731000">
          <input name="SenderDistributionName" value="湖南长沙分拨中心">
          <input name="PackageByCode" value="56739382001">
          <input name="ProductType" value="24">
          <input name="PaymentType" value="102">
          <input name="GoodsType" value="184">
          <input name="ItemTotalNumber" value="1">
          <input name="Freight" value="0.00">
          <input name="InsuredAmount" value="11000">
          <input type="checkbox" name="DispatchSms" value="1" checked disabled>
          <input name="IsSendMsg" value="0">
          <input name="IsCod" value="0">
          <input name="IsDiscount" value="2">
          <script>var $BubbleRatio = '3000'; var $HeavyMinWeight = '50';</script>
        </body></html>
        """

        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}

            def __init__(self, payload, *, text=None, url=""):
                self._payload = payload
                self.text = "{}" if text is None else text
                self.url = url

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []
                self.gets = []

            def get(self, url, headers=None, allow_redirects=None, timeout=None):
                self.gets.append({"url": url, "headers": headers or {}})
                if url == yunda_price.ENTRY_INDEX_URL:
                    return Response({}, text=html, url=url)
                if url == yunda_waybill_entry.ELEC_STOCK_URL:
                    return Response({"info": "1", "data": {"num": 837}}, url=url)
                raise AssertionError(url)

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                stored_data = dict(data or {}) if isinstance(data, dict) else list(data or [])
                self.calls.append({"url": url, "data": stored_data})
                if url == yunda_waybill_entry.CURRENT_TIME_URL:
                    return Response({"info": "1", "data": "2026-05-25 18:04:37"})
                if url == yunda_price.ADDRESS_ANALYSIS_URL:
                    return Response({
                        "info": "1",
                        "data": {
                            "Buyer_Province": "510000",
                            "Buyer_City": "510700",
                            "Buyer_Area": "510703",
                            "Buyer_Province_Name": "四川省",
                            "Buyer_City_Name": "绵阳市",
                            "Buyer_Area_Name": "涪城区",
                            "Buyer_Address": "石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
                        },
                    })
                if url == yunda_price.ADDRESS_SITE_URL:
                    return Response({
                        "info": "1",
                        "data": {
                            "51070301": {
                                "target_center_code": "51070301",
                                "target_center": "四川绵阳涪城石塘公司",
                                "business_center_code": "51000000",
                                "business_center": "四川成都分拨中心",
                                "BuyerTownCode": "510703101",
                                "BuyerTown": "石塘镇",
                                "SendMsg": "派送",
                                "qry_phone": "0816-7221174",
                                "site_manager_phone": "18009078488",
                                "SiteAddress": "四川省绵阳市涪城区毅锦街靠近毅德商贸城",
                            }
                        },
                    })
                if url == yunda_price.ENTRY_WEIGHT_URL:
                    return Response({"info": 1, "data": 1000, "volRate": 5, "Tfr": 1000, "Del": 1000})
                if url == yunda_price.ENTRY_INSURED_AMOUNT_URL:
                    return Response({"info": 1, "data": {"MIN": 0, "MAX": 200000}})
                if url == yunda_waybill_entry.CHECK_SERVICE_SCOPE_URL:
                    return Response({"info": "1", "data": {}})
                if url == yunda_waybill_entry.PRICE_URL:
                    if stored_data.get("ServiceType") == "112":
                        return Response({"info": "1", "data": {"CostTotal": "563.70"}})
                    if stored_data.get("ServiceType") == "111":
                        return Response({"info": "1", "data": {"CostTotal": "613.54"}})
                    raise AssertionError(stored_data)
                raise AssertionError(url)

        session = Session()
        result = yunda_price.fetch_yunda_prices(
            session,
            address="四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
            weight=1000,
            volume=0.1,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("613.58元", result["韵达派送"])
        self.assertEqual("563.75元", result["韵达自提"])
        self.assertEqual("四川绵阳涪城石塘公司", result["目的网点"])
        self.assertEqual("0816-7221174", result["查询电话"])
        self.assertEqual("派送", result["是否派送"])
        self.assertNotIn(yunda_price.BATCH_TRIAL_CHECK_URL, [call["url"] for call in session.calls])
        price_calls = [call for call in session.calls if call["url"] == yunda_waybill_entry.PRICE_URL]
        self.assertEqual(2, len(price_calls))
        by_service = {call["data"]["ServiceType"]: call["data"] for call in price_calls}
        self.assertEqual("1", by_service["112"]["CheckHeavyWeight"])
        self.assertEqual("1", by_service["112"]["CheckFixedCost"])
        self.assertEqual("1", by_service["112"]["DispatchSms"])
        self.assertEqual("", by_service["112"]["ShippingMethods"])
        self.assertEqual("180", by_service["111"]["ShippingMethods"])
        self.assertEqual("510000", by_service["112"]["BuyerProvince"])
        self.assertEqual("510700", by_service["112"]["BuyerCity"])
        self.assertEqual("510703", by_service["112"]["BuyerArea"])
        self.assertEqual("0", by_service["112"]["ItemTotalNumber"])
        self.assertEqual("11000", by_service["112"]["InsuredAmount"])
        self.assertEqual("1000", by_service["112"]["GrossWeight"])
        self.assertEqual("1000", by_service["112"]["SettlementTotalNumber"])
        self.assertEqual("0.1", by_service["112"]["Volume"])
        analysis_call = next(call for call in session.calls if call["url"] == yunda_price.ADDRESS_ANALYSIS_URL)
        self.assertEqual(
            "四川省绵阳市涪城区石塘镇瓦店村七组东岳汽修厂内金源冷挤压有限公司",
            analysis_call["data"]["AddressInfo"],
        )

    def test_yunda_price_applies_special_area_scope_to_dispatch_quote(self):
        case = self
        html = """
        <html><body>
          <input name="CreatedDotCode" value="56739382">
          <input name="CreatedDotname" value="湖南邵阳双清滨江公司">
          <input name="SenderDistributionCode" value="56731000">
          <input name="SenderDistributionName" value="湖南长沙分拨中心">
          <input name="PackageByCode" value="56739382001">
          <input name="ProductType" value="24">
          <input name="PaymentType" value="102">
          <input name="GoodsType" value="184">
          <input name="ItemTotalNumber" value="1">
          <input name="Freight" value="0.00">
          <input name="InsuredAmount" value="11000">
          <input type="checkbox" name="DispatchSms" value="1" checked disabled>
          <input name="IsSendMsg" value="0">
          <input name="IsCod" value="0">
          <input name="IsDiscount" value="2">
          <script>var $BubbleRatio = '3000'; var $HeavyMinWeight = '50';</script>
        </body></html>
        """

        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}

            def __init__(self, payload, *, text=None, url=""):
                self._payload = payload
                self.text = "{}" if text is None else text
                self.url = url

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, headers=None, allow_redirects=None, timeout=None):
                if url == yunda_price.ENTRY_INDEX_URL:
                    return Response({}, text=html, url=url)
                if url == yunda_waybill_entry.ELEC_STOCK_URL:
                    return Response({"info": "1", "data": {"num": 837}}, url=url)
                raise AssertionError(url)

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                stored_data = dict(data or {}) if isinstance(data, dict) else list(data or [])
                self.calls.append({"url": url, "data": stored_data})
                if url == yunda_waybill_entry.CURRENT_TIME_URL:
                    return Response({"info": "1", "data": "2026-06-04 00:28:07"})
                if url == yunda_price.ADDRESS_ANALYSIS_URL:
                    return Response({
                        "info": "1",
                        "data": {
                            "Buyer_Province": "330000",
                            "Buyer_City": "330200",
                            "Buyer_Area": "330211",
                            "Buyer_Province_Name": "浙江省",
                            "Buyer_City_Name": "宁波市",
                            "Buyer_Area_Name": "镇海区",
                            "Buyer_Address": "招宝山街道威海路1188号2楼A库康特恩仓库",
                        },
                    })
                if url == yunda_price.ADDRESS_SITE_URL:
                    return Response({
                        "info": "1",
                        "data": {
                            "33021101": {
                                "target_center_code": "57114536",
                                "target_center": "浙江宁波镇海招宝山公司",
                                "business_center_code": "57100000",
                                "business_center": "浙江宁波分拨中心",
                                "BuyerTownCode": "330211001",
                                "BuyerTown": "招宝山街道",
                                "SendMsg": "派送",
                                "special_range": "-",
                                "SpecialArea": {
                                    "浙江省宁波市镇海区后海塘工业区": {
                                        "Similarity": "0.00%",
                                        "remark": "加收30元/票",
                                        "charge_type": 1,
                                    }
                                },
                                "IsIncludeSpecialArea": "1",
                                "SpecialAreaCode": "60123776",
                                "SpecialAreaMsg": "该地址涉及特殊区域【后海塘工业区】【加收30元/票】，请核实！",
                                "SpecialAreaInfo": {
                                    "site_code": 56574962,
                                    "short_address": "后海塘工业区",
                                    "remark": "加收30元/票",
                                    "charge_type": 1,
                                },
                            }
                        },
                    })
                if url == yunda_price.ENTRY_WEIGHT_URL:
                    return Response({"info": 1, "data": 1000, "volRate": 5, "Tfr": 1000, "Del": 1000})
                if url == yunda_price.ENTRY_INSURED_AMOUNT_URL:
                    return Response({"info": 1, "data": {"MIN": 0, "MAX": 200000}})
                if url == yunda_waybill_entry.CHECK_SERVICE_SCOPE_URL:
                    return Response({"info": "1", "data": {}})
                if url == yunda_waybill_entry.PRICE_URL:
                    if stored_data.get("ServiceType") == "111":
                        case.assertEqual("60123776", stored_data.get("SpecialAreaCode"))
                        case.assertEqual("浙江省宁波市镇海区后海塘工业区", stored_data.get("SpecialAreaName"))
                        return Response({"info": "1", "data": {"CostTotal": "700.62"}})
                    if stored_data.get("ServiceType") == "112":
                        case.assertNotEqual("SA-NB-ZH-HT", stored_data.get("SpecialAreaCode", ""))
                        return Response({"info": "1", "data": {"CostTotal": "608.94"}})
                    raise AssertionError(stored_data)
                raise AssertionError(url)

        session = Session()
        result = yunda_price.fetch_yunda_prices(
            session,
            address="浙江省宁波市镇海区招宝山街道威海路1188号2楼A库康特恩仓库",
            weight=1000,
            volume=0.1,
        )

        self.assertEqual("浙江省宁波市镇海区后海塘工业区", result["特殊区域"])
        self.assertEqual("加收30元/票", result["特殊区域加收"])
        self.assertEqual("该地址涉及特殊区域【后海塘工业区】【加收30元/票】，请核实！", result["特殊区域提醒"])
        self.assertIn(yunda_waybill_entry.CHECK_SERVICE_SCOPE_URL, [call["url"] for call in session.calls])

    def test_yunda_send_waybills_fetch_paginates_and_merges_details(self):
        case = self

        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = "{}"

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"url": url, "data": data})
                if url.endswith("/business/waybill/sendwaybill/list.html"):
                    if data["page"] == 1:
                        return Response(
                            {
                                "total": 2,
                                "rows": [
                                    {
                                        "Logistics_Id": "978284775",
                                        "Created_Dot_Code": "56739382",
                                        "Buyer_Destination_Dot_Name": "湖南长沙岳麓区梅溪湖公司",
                                        "Buyer_Area_Name": "岳麓区",
                                        "Buyer_Address": "梅溪湖*栋*",
                                        "Sender_Name": "勇*",
                                        "Sender_Phone": "073*****128",
                                        "Buyer_Name": "廖*",
                                        "Buyer_Mobile": "188****4321",
                                        "Item_Name": "透析液",
                                        "Packing_Type": "纸箱:16",
                                        "Shipping_Methods": "180",
                                        "Pickup_Method": "不上楼",
                                        "Item_Total_Number": 16,
                                        "Gross_Weight": "250.00",
                                        "Freight": "115.00",
                                        "Payment_Type": "到付",
                                        "Transfer_Cost": "*",
                                        "Total_Cost_Money": "*",
                                        "Total_Money": "999.99",
                                        "Return_Logistics_Id": "",
                                        "Remarks": "",
                                        "Settlement_Total_Number": "250.00",
                                        "Volume": "1.0000",
                                    }
                                ],
                            }
                        )
                    return Response(
                        {
                            "total": 2,
                            "rows": [
                                {
                                    "Logistics_Id": "978281237",
                                    "Created_Dot_Code": "56739382",
                                    "Buyer_Destination_Dot_Name": "安徽铜陵公司三分部",
                                    "Buyer_Area_Name": "郊区",
                                    "Buyer_Address": "铜都大道*",
                                    "Sender_Name": "勇*",
                                    "Sender_Phone": "073*****128",
                                    "Buyer_Name": "洪*",
                                    "Buyer_Mobile": "158****9716",
                                    "Item_Name": "吨袋",
                                    "Packing_Type": "编织袋:12",
                                    "Shipping_Methods": "231",
                                    "Item_Total_Number": 12,
                                    "Gross_Weight": "522.00",
                                    "Freight": "12.00",
                                    "Payment_Type": "现金",
                                    "Transfer_Cost": "8.50",
                                    "Total_Cost_Money": "106.50",
                                    "Total_Money": "999.99",
                                    "Return_Logistics_Id": "HD001",
                                    "Remarks": "测试备注",
                                    "Settlement_Total_Number": "557.90",
                                    "Volume": "2.7895",
                                }
                            ],
                        }
                    )
                if url.endswith("/business/specialLine/specialLineManage/getList.html"):
                    return Response({"total": 0, "rows": []})
                if url.endswith("/system/mail/list.html"):
                    bill_code = data["Ids[]"]
                    return Response(
                        {
                            "rows": [
                                {
                                    bill_code: {
                                        "logistics": {
                                            "Logistics_Id": bill_code,
                                            "Extend_Field1": "200" if bill_code == "978284775" else "557.90",
                                            "COD": "115.00" if bill_code == "978284775" else "0.00",
                                        }
                                    }
                                }
                            ]
                        }
                    )
                if url.endswith("/business/waybill/sendwaybill/renderer.html"):
                    bill_code = data["LogisticsId"]
                    case.assertEqual("56739382", data["createDotCode"])
                    return Response(
                        {
                            "Logistics_Id": bill_code,
                            "price": {
                                "Total": "81.85" if bill_code == "978284775" else "257.69",
                            },
                        }
                    )
                if url.endswith("/system/mail/getOriginalData.html"):
                    bill_code = data["Logistics_Id"]
                    if bill_code == "978284775":
                        return Response(
                            {
                                "Sender_Name": "勇胜",
                                "Sender_Mobile": "",
                                "Sender_Phone": "07315186128",
                                "Buyer_Name": "廖芬姣",
                                "Buyer_Mobile": "18874714321",
                                "Buyer_Address": "湖南省长沙市岳麓区梅溪湖街道金茂梅溪湖29栋3902",
                            }
                        )
                    return Response(
                        {
                            "Sender_Name": "勇胜",
                            "Sender_Phone": "07315186128",
                            "Buyer_Name": "洪师傅",
                            "Buyer_Mobile": "15800009716",
                            "Buyer_Address": "安徽省铜陵市郊区铜都大道中段铜南小区",
                        }
                    )
                raise AssertionError(url)

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("yunda_send_waybills.get_session_broker", return_value=broker):
            result = yunda_send_waybills.run_once({"target_date": "2026-05-15", "page_size": 1, "max_pages": 5})

        self.assertTrue(result["ok"])
        self.assertEqual(2, result["total"])
        self.assertEqual(2, result["fetched"])
        self.assertEqual({"send_waybill": 2, "special_line": 0}, result["source_counts"])
        self.assertEqual([1, 2], [call["data"]["page"] for call in session.calls if "sendwaybill/list" in call["url"]])
        self.assertEqual([1], [call["data"]["page"] for call in session.calls if "specialLine/specialLineManage/getList" in call["url"]])
        first = result["records"][0]
        self.assertEqual("978284775", first["5.14编号"])
        self.assertEqual("湖南省长沙市岳麓区梅溪湖街道金茂梅溪湖29栋3902", first["收件地址"])
        self.assertEqual("勇胜", first["寄件人"])
        self.assertEqual("07315186128", first["寄件手机"])
        self.assertEqual("廖芬姣", first["收货人"])
        self.assertEqual("18874714321", first["收货电话"])
        self.assertEqual("", first["现付"])
        self.assertEqual("", first["月结"])
        self.assertEqual("115.00", first["提付"])
        self.assertEqual("81.85", first["中转运费"])
        self.assertEqual("200", first["体积重"])
        self.assertEqual("115.00", first["到付款"])
        self.assertEqual("2026-05-15", first["日期"])
        second = result["records"][1]
        self.assertEqual("送货进仓", second["派送方式"])
        self.assertEqual("12.00", second["现付"])
        self.assertEqual("", second["月结"])
        self.assertEqual("", second["提付"])
        self.assertEqual("257.69", second["中转运费"])

    def test_yunda_send_waybills_fetch_includes_special_line_rows(self):
        case = self

        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = "{}"

            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"url": url, "data": data})
                if url.endswith("/business/waybill/sendwaybill/list.html"):
                    return Response({"total": 0, "rows": []})
                if url.endswith("/business/specialLine/specialLineManage/getList.html"):
                    case.assertEqual("1", data["SendType"])
                    case.assertEqual("ALL", data["SpecialType"])
                    return Response(
                        {
                            "total": 1,
                            "rows": [
                                {
                                    "Source_Page": "send_special_line",
                                    "Logistics_Id": "978288946",
                                    "Sender_Name": "勇胜",
                                    "Sender_Phone": "07315186128",
                                    "Shipping_Methods": "不上楼",
                                    "Buyer_Destination_Dot_Name": "云南昆明官渡六甲公司昌宏分部",
                                    "Buyer_Area": "官渡区",
                                    "Buyer_Address": "国雅陶瓷市场B区3栋19号",
                                    "Item_Total_Number": 110,
                                    "Gross_Weight": "2000.00",
                                    "Settlement_Total_Number": "2800.00",
                                    "Volume": "14.0000",
                                    "Special_Freight": "349.00",
                                    "Payment_Type": "现金",
                                    "Return_Logistics_Id": "",
                                    "Remarks": "",
                                    "Total_Cost_Money": "1037.25",
                                    "Created_Dot_Code": "56739382",
                                }
                            ],
                        }
                    )
                if url.endswith("/system/mail/list.html"):
                    bill_code = data["Ids[]"]
                    return Response(
                        {
                            "rows": [
                                {
                                    bill_code: {
                                        "logistics": {
                                            "Logistics_Id": bill_code,
                                            "Item_Name": "安全帽",
                                            "Packing_Type": "纸箱:110",
                                            "Extend_Field1": "2800",
                                            "COD": "0.00",
                                        }
                                    }
                                }
                            ]
                        }
                    )
                if url.endswith("/system/mail/getOriginalData.html"):
                    return Response(
                        {
                            "Sender_Name": "勇胜",
                            "Sender_Phone": "07315186128",
                            "Buyer_Name": "柳松林",
                            "Buyer_Mobile": "15877967657",
                            "Buyer_Address": "云南省昆明市官渡区国雅陶瓷市场B区3栋19号",
                        }
                    )
                if url.endswith("/business/waybill/sendwaybill/renderer.html"):
                    raise AssertionError("special-line rows should not call sendwaybill renderer")
                raise AssertionError(url)

        session = Session()
        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: session)
        with patch("yunda_send_waybills.get_session_broker", return_value=broker):
            result = yunda_send_waybills.run_once({"target_date": "2026-05-15", "page_size": 10, "max_pages": 3})

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["total"])
        self.assertEqual(1, result["fetched"])
        self.assertEqual({"send_waybill": 0, "special_line": 1}, result["source_counts"])
        record = result["records"][0]
        self.assertEqual("978288946", record["5.14编号"])
        self.assertEqual("云南昆明官渡六甲公司昌宏分部", record["目的网点"])
        self.assertEqual("官渡区", record["收件区/县"])
        self.assertEqual("云南省昆明市官渡区国雅陶瓷市场B区3栋19号", record["收件地址"])
        self.assertEqual("柳松林", record["收货人"])
        self.assertEqual("15877967657", record["收货电话"])
        self.assertEqual("安全帽", record["货物名称"])
        self.assertEqual("纸箱:110", record["包装类型"])
        self.assertEqual("不上楼", record["派送方式"])
        self.assertEqual("349.00", record["现付"])
        self.assertEqual("", record["月结"])
        self.assertEqual("", record["提付"])
        self.assertEqual("1037.25", record["中转运费"])
        self.assertEqual("2800", record["体积重"])
        self.assertEqual("0.00", record["到付款"])

    def test_yunda_send_waybills_fetch_auth_redirect_raises_auth_required(self):
        class Response:
            status_code = 302
            headers = {"Location": "/login"}
            text = ""

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        with self.assertRaises(Exception) as ctx:
            yunda_send_waybills.fetch_send_page(
                Session(),
                {},
                target_date=date(2026, 5, 15),
                page=1,
                page_size=20,
            )

        self.assertEqual("AUTH_REQUIRED", getattr(ctx.exception, "code", ""))

    def test_phase7_tms_tools_propagate_auth_required_error_code(self):
        auth_payload = {
            "ok": False,
            "error_code": "AUTH_REQUIRED",
            "error": "当前未登录或登录态已过期。",
        }

        checks = [
            (arrive_list_sync_tool.run_arrive_list_sync, "tools.arrive_list_sync_tool.call_http_service", {}),
            (scan_sync_tool.run_scan_sync, "tools.scan_sync_tool.call_http_service", {}),
            (daily_sign_sync_tool.run_daily_sign_sync, "tools.daily_sign_sync_tool.call_http_service", {}),
            (site_send_list_sync_tool.run_site_send_list_sync, "tools.site_send_list_sync_tool.call_http_service", {}),
            (
                yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync,
                "tools.yunda_dispatch_forecast_sync_tool.call_http_service",
                {},
            ),
            (
                yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync,
                "tools.yunda_send_waybills_sync_tool.call_http_service",
                {},
            ),
            (
                delivery_status_sync_tool.run_delivery_status_sync,
                "tools.delivery_status_sync_tool.call_http_service",
                {"bill_codes": "R0001", "record_ids": "rec-1"},
            ),
            (send_order_sync_tool.run_send_order_sync, "tools.send_order_sync_tool.call_http_service", {}),
        ]

        for runner, patch_target, params in checks:
            with self.subTest(runner=runner.__module__):
                with patch(patch_target, return_value=auth_payload):
                    result = runner(params)
                self.assertEqual("AUTH_REQUIRED", result.get("error_code"))
                self.assertIn("登录", result.get("error", ""))

    def test_console_waybill_records_map_delivery_status(self):
        rows = [
            {"运单编号": "R001", "发件日期": "2026-05-12", "签收状态": "已签收", "当前扫描状态": "签收扫描"},
            {"运单编号": "R002", "发件日期": "2026-05-12", "签收状态": "未签收", "当前扫描状态": "发件扫描"},
        ]

        records = send_order_sync_tool._console_waybill_records(rows, target_date=date(2026, 5, 12))

        self.assertEqual(["signed", "in_transit"], [record["status"] for record in records])
        self.assertEqual(["签收扫描", "发件扫描"], [record["scan_status"] for record in records])

    def test_yunda_console_waybill_records_default_to_in_transit(self):
        records = yunda_send_waybills_sync_tool._console_waybill_records(
            [{"5.14编号": "978284775", "日期": "2026-05-12", "scan_type": "派件扫描"}],
            target_date=date(2026, 5, 12),
        )

        self.assertEqual("in_transit", records[0]["status"])
        self.assertEqual("派件扫描", records[0]["scan_status"])

    def test_normalize_console_waybill_record_keeps_scan_status(self):
        record = phase7_mysql_store.normalize_console_waybill_record(
            {"waybill_no": "R003", "status": "未签收", "scan_status": "到件扫描"}
        )

        self.assertIsNotNone(record)
        self.assertEqual("到件扫描", record["scan_status"])

    def test_sync_console_waybills_preserves_cancelled_status_on_update_and_stale_delete(self):
        calls: list[tuple[str, list[Any] | tuple[Any, ...] | None]] = []

        class Cursor:
            rowcount = 0
            _next_row = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "SELECT id" in sql:
                    self._next_row = {"id": 9}
                elif "UPDATE waybills" in sql:
                    self.rowcount = 1
                elif "DELETE FROM waybills" in sql:
                    self.rowcount = 0

            def fetchone(self):
                return self._next_row

            def close(self):
                return None

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def cursor(self):
                return self.cursor_obj

            def close(self):
                return None

        with (
            patch("tools.phase7_mysql_store.ensure_console_waybill_table", return_value=None),
            patch("tools.phase7_mysql_store._connect", return_value=Connection()),
        ):
            result = phase7_mysql_store.sync_console_waybills(
                [{"waybill_no": "R001", "open_date": "2026-05-12", "status": "signed"}],
                source="ronghui",
                target_date=date(2026, 5, 12),
                replace_date=True,
            )

        update_sql = next(sql for sql, _params in calls if "UPDATE waybills" in sql)
        delete_sql = next(sql for sql, _params in calls if "DELETE FROM waybills" in sql)
        update_params = next(params for sql, params in calls if "UPDATE waybills" in sql)
        self.assertIn("status = CASE WHEN status = 'cancelled' THEN status ELSE %s END", update_sql)
        self.assertIn("status <> 'cancelled'", delete_sql)
        self.assertIn("signed", update_params)
        self.assertEqual(1, result["updates"])

    def test_get_waybill_tracking_cache_merges_console_waybill_and_scan_rows(self):
        calls: list[tuple[str, list[Any] | tuple[Any, ...] | None]] = []

        class Cursor:
            _next_row = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "FROM waybill_data wd" in sql:
                    self._next_row = None
                elif "FROM scan_codes" in sql:
                    self._next_row = {
                        "arrived_quantity": 1,
                        "first_arrival_at": "2026-06-03 10:00:00",
                        "last_arrival_at": "2026-06-03 10:00:00",
                    }
                elif "FROM waybills" in sql:
                    self._next_row = {
                        "waybill_no": "R0001",
                        "quantity_lines": "3件",
                        "receiver_name": "李四",
                        "receiver_phone": "13900000000",
                        "receiver_address": "湖南邵阳",
                        "destination_site": "邵阳自提部",
                    }

            def fetchone(self):
                return self._next_row

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def cursor(self):
                return self.cursor_obj

            def close(self):
                return None

        with (
            patch("tools.phase7_mysql_store.ensure_phase7_tables", return_value=None),
            patch("tools.phase7_mysql_store.ensure_console_waybill_table", return_value=None),
            patch("tools.phase7_mysql_store._connect", return_value=Connection()),
        ):
            cache = phase7_mysql_store.get_waybill_tracking_cache("R0001")

        self.assertEqual("R0001", cache["tracking_number"])
        self.assertEqual("李四", cache["recipient_name"])
        self.assertEqual("13900000000", cache["recipient_phone"])
        self.assertEqual("3件", cache["quantity"])
        self.assertEqual(1, cache["arrived_quantity"])
        self.assertTrue(any(params == ("R0001",) for _sql, params in calls))
        scan_sql = next(sql for sql, _params in calls if "FROM scan_codes" in sql)
        self.assertIn("COUNT(DISTINCT raw_code)", scan_sql)
        self.assertIn("MIN(last_seen_at)", scan_sql)
        self.assertIn("code_type = 'child'", scan_sql)
        self.assertNotIn("first_seen_at", scan_sql)

    def test_delivery_status_sync_scans_bitable_and_updates_signed_records_only(self):
        self.delivery_status_sql_mock.reset_mock()
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_views":
                return {
                    "ok": True,
                    "items": [
                        {"view_id": "vewPending", "view_name": "未签收明细"},
                        {"view_id": "veweDmbdIS", "view_name": "寄件数据(总表)"},
                    ],
                }
            if action == "list_records":
                self.assertEqual("Fcm8b2H7wayK1UsYLjlcFmWhnMh", params["base_token"])
                self.assertEqual("tblX96gGAuBfJrtW", params["table_id"])
                self.assertEqual("vewPending", params["view_id"])
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-signed", "fields": {"运单编号": "R001", "签收状态": "未签收"}},
                        {"record_id": "rec-still", "fields": {"运单编号": "R002", "签收状态": "未签收"}},
                        {"record_id": "rec-done", "fields": {"运单编号": "R003", "签收状态": "已签收"}},
                        {"record_id": "rec-empty", "fields": {"运单编号": "", "签收状态": "未签收"}},
                    ],
                }
            if action == "write_records":
                records = params["records"]
                self.assertEqual(
                    [{"record_id": "rec-signed", "fields": {"签收状态": "已签收"}}],
                    records,
                )
                return {"ok": True, "written": len(records)}
            raise AssertionError(action)

        with (
            patch("tools.delivery_status_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.delivery_status_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch(
                "tools.delivery_status_sync_tool.call_http_service",
                return_value={
                    "ok": True,
                    "data": [
                        {"运单编号": "R001", "签收状态": "签收"},
                        {"运单编号": "R002", "签收状态": "未签收"},
                    ],
                },
            ) as call_http,
        ):
            result = delivery_status_sync_tool.run_delivery_status_sync({})

        self.assertTrue(result["ok"])
        self.assertEqual(4, result["scanned"])
        self.assertEqual(2, result["pending"])
        self.assertEqual(2, result["queried"])
        self.assertEqual(1, result["updated"])
        self.assertEqual(1, result["unchanged"])
        self.assertEqual(0, result["unmatched"])
        self.assertEqual(1, result["skipped_empty_waybill"])
        self.assertEqual("vewPending", result["list_result"]["view_id"])
        self.assertEqual("未签收明细", result["list_result"]["view_name"])
        self.assertEqual("/delivery_status", call_http.call_args.args[0])
        self.assertEqual("R001,R002", call_http.call_args.args[1]["params"]["bill_codes"])
        self.assertIn("write_records", [action for action, _params in calls])
        self.delivery_status_sql_mock.assert_called_once_with(["R001"], "signed")

    def test_delivery_status_sync_reads_records_beyond_first_feishu_page(self):
        self.delivery_status_sql_mock.reset_mock()
        list_offsets: list[int] = []

        def _fake_feishu_operation(action, params):
            if action == "list_views":
                return {"ok": True, "items": [{"view_id": "vewPending", "view_name": "未签收明细"}]}
            if action == "list_records":
                list_offsets.append(params["offset"])
                if params["offset"] == 0:
                    return {
                        "ok": True,
                        "items": [
                            {
                                "record_id": f"rec-old-{index}",
                                "fields": {"运单编号": f"R{index:03d}", "签收状态": "已签收"},
                            }
                            for index in range(200)
                        ],
                    }
                if params["offset"] == 200:
                    return {
                        "ok": True,
                        "items": [
                            {"record_id": "rec-late", "fields": {"运单编号": "R201", "签收状态": "未签收"}},
                        ],
                    }
                return {"ok": True, "items": []}
            if action == "write_records":
                self.assertEqual(
                    [{"record_id": "rec-late", "fields": {"签收状态": "已签收"}}],
                    params["records"],
                )
                return {"ok": True, "written": len(params["records"])}
            raise AssertionError(action)

        with (
            patch("tools.delivery_status_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.delivery_status_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch(
                "tools.delivery_status_sync_tool.call_http_service",
                return_value={"ok": True, "data": [{"运单编号": "R201", "签收状态": "已签收"}]},
            ) as call_http,
        ):
            result = delivery_status_sync_tool.run_delivery_status_sync({})

        self.assertTrue(result["ok"])
        self.assertEqual([0, 200], list_offsets)
        self.assertEqual(201, result["scanned"])
        self.assertEqual(1, result["pending"])
        self.assertEqual(1, result["queried"])
        self.assertEqual(1, result["updated"])
        self.assertEqual("R201", call_http.call_args.args[1]["params"]["bill_codes"])
        self.delivery_status_sql_mock.assert_called_once_with(["R201"], "signed")

    def test_delivery_status_sync_dry_run_does_not_write_bitable(self):
        self.delivery_status_sql_mock.reset_mock()
        def _fake_feishu_operation(action, params):
            if action == "list_views":
                return {"ok": True, "items": [{"view_id": "vewPending", "view_name": "未签收明细"}]}
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-signed", "fields": {"运单编号": "R001", "签收状态": "未签收"}},
                    ],
                }
            if action == "write_records":
                raise AssertionError("dry_run should not write records")
            raise AssertionError(action)

        with (
            patch("tools.delivery_status_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.delivery_status_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch(
                "tools.delivery_status_sync_tool.call_http_service",
                return_value={"ok": True, "data": [{"运单编号": "R001", "签收状态": "已签收"}]},
            ),
        ):
            result = delivery_status_sync_tool.run_delivery_status_sync({"dry_run": True})

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(1, result["updated"])
        self.assertEqual(
            [{"record_id": "rec-signed", "fields": {"签收状态": "已签收"}}],
            result["planned_records"],
        )
        self.delivery_status_sql_mock.assert_not_called()

    def test_delivery_status_sync_keeps_explicit_webhook_mode_compatible(self):
        self.delivery_status_sql_mock.reset_mock()
        def _fake_feishu_operation(action, params):
            if action == "write_records":
                self.assertEqual(
                    [{"record_id": "rec-1", "fields": {"签收状态": "未签收"}}],
                    params["records"],
                )
                return {"ok": True, "written": 1}
            raise AssertionError(action)

        with (
            patch(
                "tools.delivery_status_sync_tool.get_workflow_resource",
                return_value={"base_token": "base", "table_id": "table"},
            ),
            patch("tools.delivery_status_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch(
                "tools.delivery_status_sync_tool.call_http_service",
                return_value={"ok": True, "data": [{"运单编号": "R001", "签收状态": "未签收"}]},
            ),
        ):
            result = delivery_status_sync_tool.run_delivery_status_sync(
                {"bill_codes": ["R001"], "record_ids": ["rec-1"]}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["matched"])
        self.assertEqual(1, result["updated"])
        self.delivery_status_sql_mock.assert_not_called()

    def test_send_order_runtime_fetches_all_pages_by_default(self):
        class Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, params=None, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"url": url, "params": params, "data": data})
                page_index = int(data["pageIndex"])
                rows = [
                    {"BILL_CODE": "R001", "INSERT_DATE": "2026-05-12 08:00:00", "PIECE_NUMBER": "1"},
                    {"BILL_CODE": "R002", "INSERT_DATE": "2026-05-12 09:00:00", "PIECE_NUMBER": "2"},
                    {"BILL_CODE": "R003", "INSERT_DATE": "2026-05-12 10:00:00", "PIECE_NUMBER": "3"},
                ]
                start = page_index * 2
                return Response({"total": 3, "data": rows[start:start + 2]})

        session = Session()
        with patch("Send_order.login_as_daxiang", return_value=session):
            rows = Send_order.run_once({"target_date": "2026-05-12", "page_size": 2})

        self.assertEqual(["R001", "R002", "R003"], [row["运单编号"] for row in rows])
        self.assertEqual(["0", "1"], [call["data"]["pageIndex"] for call in session.calls])
        self.assertIn("2026/05/12", session.calls[0]["data"]["REGISTER_DATE"])

    def test_send_order_runtime_keeps_explicit_single_page_behavior(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "total": 3,
                    "data": [{"BILL_CODE": "R003", "INSERT_DATE": "2026-05-12 10:00:00"}],
                }

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, params=None, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"data": data})
                return Response()

        session = Session()
        with patch("Send_order.login_as_daxiang", return_value=session):
            rows = Send_order.run_once({"target_date": "2026-05-12", "page_index": 1, "page_size": 2})

        self.assertEqual(["R003"], [row["运单编号"] for row in rows])
        self.assertEqual(1, len(session.calls))
        self.assertEqual("1", session.calls[0]["data"]["pageIndex"])

    def test_send_order_runtime_uses_bound_session_profile(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"total": 0, "data": []}

        class Session:
            def post(self, url, params=None, data=None, headers=None, allow_redirects=None, timeout=None):
                return Response()

        captured: dict[str, Any] = {}

        def fake_login(config_path=None, *, profile="default"):
            captured["profile"] = profile
            return Session()

        with patch("Send_order.login_as_daxiang", side_effect=fake_login):
            Send_order.run_once({"target_date": "2026-05-12", "session_profile": "ronghui_ops"})

        self.assertEqual("ronghui_ops", captured["profile"])

    def test_send_order_sync_replaces_target_date_with_new_bitable_snapshot(self):
        tms_payload = {
            "ok": True,
            "data": [
                {
                    "运单编号": "R001",
                    "发件日期": "2026-05-12 08:00:00",
                    "件数": "1",
                    "录单金额": "12.50",
                }
            ],
        }
        actions: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            actions.append((action, params))
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "rec-keep",
                            "fields": {
                                "运单编号": "R001",
                                "发件日期": send_order_sync_tool._date_to_timestamp_ms(date(2026, 5, 12)),
                            },
                        },
                        {
                            "record_id": "rec-stale",
                            "fields": {"运单编号": "R002", "发件日期": "2026-05-12 09:00:00"},
                        },
                        {
                            "record_id": "rec-other-day",
                            "fields": {"运单编号": "R003", "发件日期": "2026-05-11 09:00:00"},
                        },
                    ],
                }
            if action == "write_records":
                records = params["records"]
                self.assertEqual(1, len(records))
                self.assertNotIn("record_id", records[0])
                self.assertEqual("R001", records[0]["fields"]["运单编号"])
                self.assertEqual(12.5, records[0]["fields"]["录单金额"])
                return {"ok": True, "written": 1}
            if action == "delete_records":
                self.assertEqual(["rec-keep", "rec-stale"], params["record_ids"])
                return {"ok": True, "deleted": 2}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload) as call_http,
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            self.send_order_sql_mock.return_value = {"ok": True, "upserted": 1, "updates": 1, "creates": 0, "deleted_stale": 1}
            result = send_order_sync_tool.run_send_order_sync({"target_date": "2026-05-12"})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(2, result["deleted"])
        self.assertEqual(1, result["sql_upserted"])
        self.assertEqual(1, result["sql_deleted_stale"])
        self.send_order_sql_mock.assert_called_once()
        sql_records = self.send_order_sql_mock.call_args.args[0]
        self.assertEqual("R001", sql_records[0]["waybill_no"])
        self.assertEqual("2026-05-12", sql_records[0]["open_date"])
        self.assertEqual("12.50", sql_records[0]["freight_fee"])
        self.assertEqual("ronghui", self.send_order_sql_mock.call_args.kwargs["source"])
        self.assertTrue(self.send_order_sql_mock.call_args.kwargs["replace_date"])
        self.assertEqual("2026-05-12", call_http.call_args.args[1]["params"]["target_date"])
        delete_actions = [params for action, params in actions if action == "delete_records"]
        self.assertTrue(delete_actions)
        self.assertNotIn("rec-other-day", delete_actions[0]["record_ids"])
        self.assertLess(
            [action for action, _params in actions].index("delete_records"),
            [action for action, _params in actions].index("write_records"),
        )

    def test_send_order_sync_filters_receipt_like_h_and_hr_rows(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R00015275708", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
                {"运单编号": "H001", "发件日期": "2026-05-12 09:00:00", "件数": "1"},
                {"运单编号": "HR002", "发件日期": "2026-05-12 10:00:00", "件数": "1"},
            ],
        }

        def _fake_feishu_operation(action, params):
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-h", "fields": {"运单编号": "H001", "发件日期": "2026-05-12"}},
                    ],
                }
            if action == "write_records":
                records = params["records"]
                self.assertEqual(1, len(records))
                self.assertEqual("R00015275708", records[0]["fields"]["运单编号"])
                return {"ok": True, "written": 1}
            if action == "delete_records":
                self.assertEqual(["rec-h"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync({"target_date": "2026-05-12"})

        self.assertTrue(result["ok"])
        self.assertEqual(3, result["raw_fetched"])
        self.assertEqual(1, result["fetched"])
        self.assertEqual(2, result["skipped_receipt_like"])
        self.assertEqual(1, result["deleted"])

    def test_send_order_sync_reads_existing_records_after_feishu_200_row_page(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R250", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
            ],
        }
        list_offsets: list[int] = []

        def _fake_feishu_operation(action, params):
            if action == "list_records":
                list_offsets.append(params["offset"])
                self.assertEqual(200, params["limit"])
                if params["offset"] == 0:
                    return {
                        "ok": True,
                        "items": [
                            {
                                "record_id": f"rec-other-day-{index}",
                                "fields": {
                                    "运单编号": f"R{index:03d}",
                                    "发件日期": "2026-05-11",
                                },
                            }
                            for index in range(200)
                        ],
                    }
                if params["offset"] == 200:
                    return {
                        "ok": True,
                        "items": [
                            {
                                "record_id": "rec-keep",
                                "fields": {"运单编号": "R250", "发件日期": "2026-05-12"},
                            }
                        ],
                    }
                return {"ok": True, "items": []}
            if action == "write_records":
                self.assertNotIn("record_id", params["records"][0])
                return {"ok": True, "written": len(params["records"])}
            if action == "delete_records":
                self.assertEqual(["rec-keep"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync({"target_date": "2026-05-12"})

        self.assertTrue(result["ok"])
        self.assertEqual([0, 200, 0, 200], list_offsets)
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(1, result["deleted"])
        self.assertEqual(200, result["list_result"]["list_limit"])

    def test_send_order_sync_deletes_duplicate_existing_waybill_records(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R001", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
            ],
        }
        list_call_count = 0

        def _fake_feishu_operation(action, params):
            nonlocal list_call_count
            if action == "list_records":
                list_call_count += 1
                if list_call_count > 1:
                    return {
                        "ok": True,
                        "items": [
                            {"record_id": "rec-new", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        ],
                    }
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-main", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        {"record_id": "rec-dup-1", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        {"record_id": "rec-dup-2", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                    ],
                }
            if action == "write_records":
                self.assertNotIn("record_id", params["records"][0])
                return {"ok": True, "written": len(params["records"])}
            if action == "delete_records":
                self.assertEqual(["rec-main", "rec-dup-1", "rec-dup-2"], params["record_ids"])
                return {"ok": True, "deleted": len(params["record_ids"])}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync({"target_date": "2026-05-12"})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(3, result["deleted"])

    def test_send_order_sync_cleans_duplicates_created_during_write_race(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R001", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
            ],
        }
        list_call_count = 0

        def _fake_feishu_operation(action, params):
            nonlocal list_call_count
            if action == "list_records":
                list_call_count += 1
                if list_call_count == 1:
                    return {"ok": True, "items": []}
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-new-1", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        {"record_id": "rec-new-2", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                    ],
                }
            if action == "write_records":
                self.assertNotIn("record_id", params["records"][0])
                return {"ok": True, "written": len(params["records"])}
            if action == "delete_records":
                self.assertEqual(["rec-new-2"], params["record_ids"])
                return {"ok": True, "deleted": len(params["record_ids"])}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync({"target_date": "2026-05-12"})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(1, result["dedup_deleted"])

    def test_send_order_sync_sql_only_skips_feishu(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R00015275708", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
            ],
        }
        self.send_order_sql_mock.return_value = {
            "ok": True,
            "upserted": 1,
            "updates": 0,
            "creates": 1,
            "deleted_stale": 0,
        }

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.feishu_operation") as feishu_op,
        ):
            result = send_order_sync_tool.run_send_order_sync({"target_date": "2026-05-12", "sql_only": True})

        self.assertTrue(result["ok"])
        self.assertTrue(result["sql_only"])
        self.assertEqual(1, result["sql_upserted"])
        self.assertEqual(0, result["written"])
        feishu_op.assert_not_called()
        self.send_order_sql_mock.assert_called_once()

    def test_init_waybills_sql_from_feishu_reads_ronghui_and_yunda(self):
        captured: list[tuple[str, list[dict[str, Any]]]] = []

        def _fake_resource(key):
            if key == "phase7.send_order_bitable":
                return {"base_token": "base-rh", "table_id": "table-rh"}
            if key == "phase7.yunda_send_waybills_bitable":
                return {"base_token": "base-yd", "table_id": "table-yd"}
            return None

        def _fake_feishu_operation(action, params):
            self.assertEqual("list_records", action)
            self.assertEqual(200, params["limit"])
            table_id = params["table_id"]
            if table_id == "table-rh":
                return {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "rh-1",
                            "fields": {
                                "运单编号": "R00015275708",
                                "发件日期": "2026-05-12 08:00:00",
                                "目的网点": "长沙",
                                "收货人": "张三",
                            },
                        },
                        {
                            "record_id": "rh-2",
                            "fields": {
                                "运单编号": "HR0001",
                                "发件日期": "2026-05-12 09:00:00",
                            },
                        }
                    ],
                }
            if table_id == "table-yd":
                return {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "yd-1",
                            "fields": {
                                "运单编号": "978288946",
                                "日期": "2026-05-15",
                                "目的网点": "韵达站点",
                                "收货人": "李四",
                            },
                        }
                    ],
                }
            raise AssertionError(table_id)

        def _fake_sql(records, *, source, **kwargs):
            captured.append((source, records))
            return {"ok": True, "upserted": len(records), "updates": 0, "creates": len(records), "deleted_stale": 0}

        with (
            patch("tools.init_waybills_sql_from_feishu_tool.get_workflow_resource", side_effect=_fake_resource),
            patch("tools.init_waybills_sql_from_feishu_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.init_waybills_sql_from_feishu_tool.sync_console_waybills", side_effect=_fake_sql),
            patch("tools.init_waybills_sql_from_feishu_tool.delete_receipt_like_console_waybills", return_value={"ok": True, "deleted": 0}) as cleanup,
        ):
            result = init_waybills_sql_from_feishu_tool.run_init_waybills_sql_from_feishu({"list_limit": 500})

        self.assertTrue(result["ok"])
        self.assertEqual(3, result["feishu_records"])
        self.assertEqual(1, result["skipped_receipt_like"])
        self.assertEqual(2, result["sql_upserted"])
        self.assertEqual("ronghui", captured[0][0])
        self.assertEqual("R00015275708", captured[0][1][0]["waybill_no"])
        self.assertEqual("yunda", captured[1][0])
        self.assertEqual("978288946", captured[1][1][0]["waybill_no"])
        cleanup.assert_called_once_with(source="ronghui")

    def test_send_order_sync_zero_rows_clears_target_date(self):
        actions: list[str] = []

        def _fake_feishu_operation(action, params):
            actions.append(action)
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-1", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        {"record_id": "rec-2", "fields": {"运单编号": "R002", "发件日期": "2026-05-12"}},
                    ],
                }
            if action == "delete_records":
                self.assertEqual(["rec-1", "rec-2"], params["record_ids"])
                return {"ok": True, "deleted": 2}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value={"ok": True, "data": []}),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync({"target_date": "2026-05-12"})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["fetched"])
        self.assertEqual(2, result["deleted"])
        self.assertNotIn("write_records", actions)

    def test_send_order_sync_supports_date_range(self):
        request_dates: list[str] = []

        def _fake_call_http_service(endpoint, request_body):
            self.assertEqual("/send_order", endpoint)
            target_date = request_body["params"]["target_date"]
            request_dates.append(target_date)
            return {
                "ok": True,
                "data": [
                    {
                        "运单编号": f"R{target_date[-2:]}",
                        "发件日期": f"{target_date} 08:00:00",
                        "件数": "1",
                    }
                ],
            }

        def _fake_feishu_operation(action, params):
            if action == "list_records":
                return {"ok": True, "items": []}
            if action == "write_records":
                return {"ok": True, "written": len(params["records"])}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync(
                {"start_date": "2026-05-06", "end_date": "2026-05-08"}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["2026-05-06", "2026-05-07", "2026-05-08"], request_dates)
        self.assertEqual(3, result["days"])
        self.assertEqual(3, result["fetched"])
        self.assertEqual(3, result["creates"])
        self.assertEqual(3, result["written"])

    def test_send_order_sync_dry_run_does_not_write_or_delete(self):
        actions: list[str] = []

        def _fake_feishu_operation(action, params):
            actions.append(action)
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [{"record_id": "rec-stale", "fields": {"运单编号": "R002", "发件日期": "2026-05-12"}}],
                }
            raise AssertionError(action)

        with (
            patch(
                "tools.send_order_sync_tool.call_http_service",
                return_value={"ok": True, "data": [{"运单编号": "R001", "发件日期": "2026-05-12 08:00:00"}]},
            ),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync({"target_date": "2026-05-12", "dry_run": True})

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(1, result["planned"])
        self.assertEqual(1, result["planned_creates"])
        self.assertEqual(1, result["planned_deletes"])
        self.assertEqual(1, result["planned_sql_upserts"])
        self.assertEqual(["list_records"], actions)
        self.send_order_sql_mock.assert_not_called()

    def test_feishu_record_list_normalizes_lark_cli_record_id_list(self):
        payload = {
            "ok": True,
            "data": {
                "record_id_list": ["rec-1"],
                "data": [["YD001", "2026-05-11"]],
                "fields": ["主单号", "应派时间"],
            },
        }

        result = feishu_cli_tool._normalize_bitable_record_list(payload)

        self.assertEqual("rec-1", result["items"][0]["record_id"])
        self.assertEqual("YD001", result["items"][0]["fields"]["主单号"])
        self.assertEqual("2026-05-11", result["items"][0]["fields"]["应派时间"])

    def test_feishu_list_views_uses_open_api_and_normalizes_items(self):
        calls: list[tuple[str, str]] = []

        def _fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path))
            return {
                "ok": True,
                "data": {
                    "items": [{"view_id": "vewPending", "view_name": "未签收明细"}],
                    "has_more": False,
                },
            }

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=_fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "list_views",
                {"base_token": "base", "table_id": "table"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual("vewPending", result["items"][0]["view_id"])
        self.assertEqual("GET", calls[0][0])
        self.assertIn("/open-apis/bitable/v1/apps/base/tables/table/views?", calls[0][1])

    def test_yunda_dispatch_forecast_sync_appends_by_default(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [
                    {
                        "主单号": "YD001",
                        "开单件数": "3",
                        "扫描件数": "2",
                        "重量/kg": "12.5",
                        "体积/m3": "0.3",
                        "包装类型": "纸箱",
                        "清场时间": "2026-05-10 18:00:00",
                        "规划时效": "24",
                        "开单目的地址": "湖南省邵阳市测试地址",
                        "预计到达时间": "2026-05-11 12:00:00",
                        "应派时间": "2026-05-11",
                    }
                ],
            },
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": "文本", "is_primary": True}]
                    + [
                        {"field_name": name}
                        for name in yunda_dispatch_forecast_sync_tool.FIELD_NAMES
                        if name != "应派时间"
                    ],
                }
            if action == "create_field":
                return {"ok": True, "field": params["field_name"]}
            if action == "write_records":
                record = params["records"][0]["fields"]
                self.assertEqual("YD001", record["文本"])
                self.assertEqual("YD001", record["主单号"])
                self.assertEqual(3, record["开单件数"])
                self.assertEqual(12.5, record["重量/kg"])
                return {"ok": True, "written": 1}
            raise AssertionError(action)

        with (
            patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_dispatch_forecast_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_dispatch_forecast_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                {"target_date": "2026-05-11"}
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["append_only"])
        self.assertEqual(0, result["deleted"])
        self.assertEqual(1, result["written"])
        self.assertNotIn("list_records", [action for action, _params in calls])
        self.assertNotIn("delete_records", [action for action, _params in calls])
        create_calls = [params for action, params in calls if action == "create_field"]
        self.assertEqual(["应派时间"], [params["field_name"] for params in create_calls])

    def test_yunda_dispatch_forecast_sync_uses_primary_field_as_main_index(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [
                    {
                        "主单号": "YD001",
                        "开单件数": "3",
                        "扫描件数": "2",
                        "重量/kg": "12.5",
                        "体积/m3": "0.3",
                        "包装类型": "纸箱",
                        "清场时间": "2026-05-10 18:00:00",
                        "规划时效": "24",
                        "开单目的地址": "湖南省邵阳市测试地址",
                        "预计到达时间": "2026-05-11 12:00:00",
                        "应派时间": "2026-05-11",
                    }
                ],
            },
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": "文本", "is_primary": True}]
                    + [
                        {"field_name": name}
                        for name in yunda_dispatch_forecast_sync_tool.FIELD_NAMES
                        if name not in {"主单号", "应派时间"}
                    ],
                }
            if action == "create_field":
                return {"ok": True, "field": params["field_name"]}
            if action == "write_records":
                record = params["records"][0]["fields"]
                self.assertEqual("YD001", record["文本"])
                self.assertNotIn("主单号", record)
                self.assertEqual("2026-05-11", record["应派时间"])
                return {"ok": True, "written": 1}
            raise AssertionError(action)

        with (
            patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_dispatch_forecast_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_dispatch_forecast_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                {"target_date": "2026-05-11"}
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["append_only"])
        self.assertEqual(0, result["deleted"])
        create_calls = [params for action, params in calls if action == "create_field"]
        self.assertEqual(["应派时间"], [params["field_name"] for params in create_calls])

    def test_yunda_dispatch_forecast_sync_surfaces_tms_runtime_error(self):
        tms_payload = {
            "ok": False,
            "error_type": "RuntimeError",
            "error": "韵达派件预测接口返回格式异常: list",
            "http_status": 500,
        }

        with patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                {"target_date": "2026-05-11"}
            )

        self.assertIn("韵达派件预测接口返回格式异常: list", result["error"])
        self.assertNotEqual("yunda_dispatch_forecast 返回格式异常", result["error"])
        self.assertEqual("RuntimeError", result["error_type"])

    def test_yunda_dispatch_forecast_sync_clears_target_date_when_no_rows(self):
        tms_payload = {"ok": True, "data": {"ok": True, "total": 0, "records": []}}
        actions: list[str] = []

        def _fake_feishu_operation(action, params):
            actions.append(action)
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": name} for name in yunda_dispatch_forecast_sync_tool.FIELD_NAMES],
                }
            if action == "list_records":
                raise AssertionError("append mode should not list old records")
            if action == "delete_records":
                raise AssertionError("append mode should not delete old records")
            raise AssertionError(action)

        with (
            patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_dispatch_forecast_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_dispatch_forecast_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                {"target_date": "2026-05-11"}
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["append_only"])
        self.assertEqual(0, result["deleted"])
        self.assertEqual(0, result["written"])
        self.assertNotIn("write_records", actions)

    def test_yunda_dispatch_forecast_sync_can_replace_target_date_when_requested(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [{"主单号": "YD001", "开单件数": "3", "应派时间": "2026-05-11"}],
            },
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {"ok": True, "items": [{"field_name": name} for name in yunda_dispatch_forecast_sync_tool.FIELD_NAMES]}
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-target", "fields": {"应派时间": "2026-05-11"}},
                        {"record_id": "rec-other", "fields": {"应派时间": "2026-05-12"}},
                    ],
                }
            if action == "delete_records":
                self.assertEqual(["rec-target"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            if action == "write_records":
                return {"ok": True, "written": 1}
            raise AssertionError(action)

        with (
            patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_dispatch_forecast_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_dispatch_forecast_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                {"target_date": "2026-05-11", "append_only": False}
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["append_only"])
        self.assertEqual(1, result["deleted"])

    def test_yunda_send_waybills_sql_only_updates_console_without_feishu(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [
                    {
                        "5.14编号": "978SQL001",
                        "日期": "2026-05-15",
                        "目的网点": "测试网点",
                        "收件地址": "测试地址",
                        "寄件人": "勇胜",
                        "收货人": "测试收件人",
                    }
                ],
            },
        }
        http_calls: list[tuple[str, dict[str, Any]]] = []
        sql_calls: list[dict[str, Any]] = []

        def _fake_call_http_service(endpoint, payload):
            http_calls.append((endpoint, payload))
            return tms_payload

        def _fake_sync_console_waybills(records, *, source, target_date, replace_date):
            sql_calls.append(
                {
                    "records": records,
                    "source": source,
                    "target_date": target_date,
                    "replace_date": replace_date,
                }
            )
            return {"ok": True, "upserted": len(records), "updates": 1, "creates": 0, "deleted_stale": 2}

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", side_effect=AssertionError("Feishu resource should not be read")),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=AssertionError("Feishu should not be called")),
            patch("tools.yunda_send_waybills_sync_tool.sync_console_waybills", side_effect=_fake_sync_console_waybills),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                {"target_date": "2026-05-15", "sql_only": True}
            )

        self.assertEqual("/yunda_send_waybills", http_calls[0][0])
        self.assertEqual("2026-05-15", http_calls[0][1]["params"]["target_date"])
        self.assertEqual(1, len(sql_calls))
        self.assertEqual("yunda", sql_calls[0]["source"])
        self.assertEqual(date(2026, 5, 15), sql_calls[0]["target_date"])
        self.assertTrue(sql_calls[0]["replace_date"])
        self.assertEqual("978SQL001", sql_calls[0]["records"][0]["waybill_no"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["sql_only"])
        self.assertEqual(1, result["sql_upserted"])
        self.assertEqual(2, result["sql_deleted_stale"])

    def test_yunda_send_waybills_sql_only_range_aggregates_sql_counts(self):
        def _fake_call_http_service(endpoint, payload):
            target_date = payload["params"]["target_date"]
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "total": 1,
                    "records": [{"5.14编号": f"978{target_date[-2:]}", "日期": target_date}],
                },
            }

        def _fake_sync_console_waybills(records, *, source, target_date, replace_date):
            return {"ok": True, "upserted": 1, "updates": 0, "creates": 1, "deleted_stale": int(target_date.day)}

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", side_effect=AssertionError("Feishu resource should not be read")),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=AssertionError("Feishu should not be called")),
            patch("tools.yunda_send_waybills_sync_tool.sync_console_waybills", side_effect=_fake_sync_console_waybills),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                {"start_date": "2026-05-15", "end_date": "2026-05-16", "sql_only": True}
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["sql_only"])
        self.assertEqual(2, result["days"])
        self.assertEqual(2, result["sql_upserted"])
        self.assertEqual(31, result["sql_deleted_stale"])

    def test_yunda_send_waybills_sync_replaces_target_date_with_new_bitable_snapshot(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 2,
                "records": [
                    {
                        "5.14编号": "978284775",
                        "目的网点": "湖南长沙岳麓区梅溪湖公司",
                        "收件区/县": "岳麓区",
                        "收件地址": "湖南省长沙市岳麓区梅溪湖街道金茂梅溪湖29栋3902",
                        "寄件人": "勇胜",
                        "寄件手机": "07315186128",
                        "收货人": "廖芬姣",
                        "收货电话": "18874714321",
                        "货物名称": "透析液",
                        "包装类型": "纸箱:16",
                        "派送方式": "不上楼",
                        "件数": "16",
                        "实际重量": "250.00",
                        "现付": "",
                        "月结": "",
                        "提付": "115.00",
                        "中转运费": "81.85",
                        "回单号": "",
                        "备注": "",
                        "结算重量": "250.00",
                        "体积": "1.0000",
                        "支付类型": "到付",
                        "体积重": "200",
                        "到付款": "115.00",
                    },
                    {
                        "5.14编号": "978281237",
                        "目的网点": "安徽铜陵公司三分部",
                        "收件区/县": "郊区",
                        "收件地址": "安徽省铜陵市郊区铜都大道中段铜南小区",
                        "寄件人": "勇胜",
                        "寄件手机": "07315186128",
                        "收货人": "洪师傅",
                        "收货电话": "15800009716",
                        "货物名称": "吨袋",
                        "包装类型": "编织袋:12",
                        "派送方式": "送货进仓",
                        "件数": "12",
                        "实际重量": "522.00",
                        "现付": "12.00",
                        "月结": "",
                        "提付": "",
                        "中转运费": "257.69",
                        "回单号": "HD001",
                        "备注": "测试备注",
                        "结算重量": "557.90",
                        "体积": "2.7895",
                        "支付类型": "现金",
                        "体积重": "557.90",
                        "到付款": "0.00",
                    },
                ],
            },
        }
        calls: list[tuple[str, dict[str, Any]]] = []
        sheet_calls: list[dict[str, Any]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": "5.14编号", "is_primary": True}]
                    + [{"field_name": name} for name in yunda_send_waybills_sync_tool.FIELD_NAMES if name != "5.14编号"],
                }
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "rec-old",
                            "fields": {"5.14编号": "978284775", "日期": "2026-05-15"},
                        },
                        {
                            "record_id": "rec-other-day",
                            "fields": {"5.14编号": "978284700", "日期": "2026-05-14"},
                        },
                    ],
                }
            if action == "delete_records":
                self.assertEqual(["rec-old"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            if action == "write_records":
                records = params["records"]
                self.assertNotIn("record_id", records[0])
                self.assertNotIn("record_id", records[1])
                first_fields = records[0]["fields"]
                self.assertEqual(16, first_fields["件数"])
                self.assertEqual(115, first_fields["提付"])
                self.assertIsNone(first_fields["现付"])
                self.assertIsNone(first_fields["月结"])
                self.assertEqual(81.85, first_fields["中转运费"])
                self.assertEqual(
                    yunda_send_waybills_sync_tool._to_date_timestamp_ms("2026-05-15"),
                    first_fields["日期"],
                )
                second_fields = records[1]["fields"]
                self.assertEqual(12, second_fields["现付"])
                self.assertEqual(257.69, second_fields["中转运费"])
                return {"ok": True, "written": 2}
            if action == "clear_sheet":
                sheet_calls.append(params)
                return {"ok": True}
            if action == "write_sheet":
                sheet_calls.append(params)
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.phase7_sync_common.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            self.yunda_send_sql_mock.return_value = {"ok": True, "upserted": 2, "updates": 1, "creates": 1, "deleted_stale": 0}
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync({"target_date": "2026-05-15"})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(2, result["creates"])
        self.assertEqual(1, result["deleted"])
        self.assertEqual(2, result["written"])
        self.assertEqual(2, result["sql_upserted"])
        self.yunda_send_sql_mock.assert_called_once()
        sql_records = self.yunda_send_sql_mock.call_args.args[0]
        self.assertEqual("978284775", sql_records[0]["waybill_no"])
        self.assertEqual("2026-05-15", sql_records[0]["open_date"])
        self.assertEqual("115.00", sql_records[0]["freight_fee"])
        self.assertEqual("81.85", sql_records[0]["transfer_fee"])
        self.assertEqual("yunda", self.yunda_send_sql_mock.call_args.kwargs["source"])
        self.assertTrue(self.yunda_send_sql_mock.call_args.kwargs["replace_date"])
        self.assertLess(
            [action for action, _params in calls].index("delete_records"),
            [action for action, _params in calls].index("write_records"),
        )
        self.assertEqual(2, result["sheet_rows"])
        self.assertEqual(2, len(sheet_calls))
        self.assertEqual(yunda_send_waybills_sync_tool.DEFAULT_SPREADSHEET_TOKEN, sheet_calls[0]["spreadsheet_token"])
        self.assertEqual("Sheet1!A2:Y5000", sheet_calls[0]["range"])
        self.assertEqual("Sheet1!A2:Y3", sheet_calls[1]["range"])
        self.assertEqual("978284775", sheet_calls[1]["values"][0][0])
        self.assertEqual("2026-05-15", sheet_calls[1]["values"][0][-1])

    def test_yunda_send_waybills_sync_zero_rows_clears_target_date(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 0,
                "records": [],
            },
        }

        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": name} for name in yunda_send_waybills_sync_tool.FIELD_NAMES],
                }
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-target", "fields": {"日期": "2026-05-21"}},
                        {"record_id": "rec-other", "fields": {"日期": "2026-05-20"}},
                    ],
                }
            if action == "delete_records":
                self.assertEqual(["rec-target"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            raise AssertionError(action)

        self.yunda_send_sql_mock.return_value = {
            "ok": True,
            "upserted": 0,
            "updates": 0,
            "creates": 0,
            "deleted_stale": 1,
        }

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.phase7_sync_common.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync({"target_date": "2026-05-21"})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["fetched"])
        self.assertEqual(0, result["written"])
        self.assertEqual(0, result["sql_upserted"])
        self.assertEqual(1, result["deleted"])
        self.assertEqual(1, result["sql_deleted_stale"])
        self.assertEqual(0, result["sheet_rows"])
        self.assertTrue(result["sheet_result"]["skipped"])
        self.assertNotIn("write_records", [action for action, _params in calls])
        self.yunda_send_sql_mock.assert_called_once()

    def test_yunda_send_waybills_sync_supports_date_range(self):
        request_dates: list[str] = []
        write_batches: list[list[dict[str, Any]]] = []

        def _fake_call_http_service(endpoint, request_body):
            self.assertEqual("/yunda_send_waybills", endpoint)
            target_date = request_body["params"]["target_date"]
            request_dates.append(target_date)
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "target_date": target_date,
                    "total": 1,
                    "records": [
                        {
                            yunda_send_waybills_sync_tool.INDEX_FIELD_NAME: f"978{target_date[-2:]}",
                            yunda_send_waybills_sync_tool.DATE_FIELD_NAME: target_date,
                            "件数": "1",
                        }
                    ],
                },
            }

        def _fake_feishu_operation(action, params):
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": yunda_send_waybills_sync_tool.INDEX_FIELD_NAME, "is_primary": True}]
                    + [
                        {"field_name": name}
                        for name in yunda_send_waybills_sync_tool.FIELD_NAMES
                        if name != yunda_send_waybills_sync_tool.INDEX_FIELD_NAME
                    ],
                }
            if action == "list_records":
                return {"ok": True, "items": []}
            if action == "write_records":
                write_batches.append(params["records"])
                return {"ok": True, "written": len(params["records"])}
            raise AssertionError(action)

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                {"start_date": "2026-05-06", "end_date": "2026-05-08"}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["2026-05-06", "2026-05-07", "2026-05-08"], request_dates)
        self.assertEqual("2026-05-06", result["start_date"])
        self.assertEqual("2026-05-08", result["end_date"])
        self.assertEqual(3, result["days"])
        self.assertEqual(3, result["fetched"])
        self.assertEqual(3, result["creates"])
        self.assertEqual(3, result["written"])
        self.assertEqual(3, len(write_batches))

    def test_yunda_send_waybills_sync_uses_primary_field_when_index_field_missing(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [{"5.14编号": "978284775", "件数": "16", "支付类型": "到付", "提付": "115.00"}],
            },
        }

        def _fake_feishu_operation(action, params):
            if action == "list_fields":
                return {"ok": True, "items": [{"field_name": "编号", "is_primary": True}, {"field_name": "件数"}, {"field_name": "支付类型"}, {"field_name": "提付"}]}
            if action == "create_field":
                self.assertNotEqual("5.14编号", params["field_name"])
                return {"ok": True, "field": params["field_name"]}
            if action == "list_records":
                return {"ok": True, "items": [{"record_id": "rec-old", "fields": {"编号": "978284775", "日期": "2026-05-15"}}]}
            if action == "delete_records":
                self.assertEqual(["rec-old"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            if action == "write_records":
                record = params["records"][0]
                self.assertNotIn("record_id", record)
                self.assertEqual("978284775", record["fields"]["编号"])
                self.assertNotIn("5.14编号", record["fields"])
                return {"ok": True, "written": 1}
            if action == "clear_sheet":
                return {"ok": True}
            if action == "write_sheet":
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.phase7_sync_common.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync({"target_date": "2026-05-15"})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(1, result["deleted"])

    def test_yunda_send_waybills_sync_maps_waybill_number_primary_when_ensure_fields_disabled(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [
                    {
                        "5.14编号": "978284775",
                        "件数": "16",
                        "支付类型": "到付",
                        "提付": "115.00",
                    }
                ],
            },
        }
        actions: list[str] = []

        def _fake_feishu_operation(action, params):
            actions.append(action)
            if action == "list_fields":
                return {"ok": True, "items": [{"field_name": "运单编号", "is_primary": True}, {"field_name": "件数"}]}
            if action == "list_records":
                return {"ok": True, "items": [{"record_id": "rec-old", "fields": {"运单编号": "978284775", "日期": "2026-05-15"}}]}
            if action == "delete_records":
                self.assertEqual(["rec-old"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            if action == "write_records":
                record = params["records"][0]
                self.assertNotIn("record_id", record)
                self.assertEqual({"运单编号": "978284775", "件数": 16}, record["fields"])
                return {"ok": True, "written": 1}
            if action == "clear_sheet":
                return {"ok": True}
            if action == "write_sheet":
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.phase7_sync_common.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                {"target_date": "2026-05-15", "ensure_fields": False}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(1, result["deleted"])
        self.assertNotIn("create_field", actions)

    def test_yunda_send_waybills_sync_dry_run_does_not_call_feishu(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [{"5.14编号": "978284775", "件数": "16", "支付类型": "到付", "提付": "115.00"}],
            },
        }

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation") as feishu_operation_mock,
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                {"target_date": "2026-05-15", "dry_run": True}
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(1, result["planned"])
        self.assertEqual(1, result["planned_sql_upserts"])
        self.assertEqual(1, result["planned_sheet_rows"])
        feishu_operation_mock.assert_not_called()
        self.yunda_send_sql_mock.assert_not_called()

    def test_arrival_stats_sync_propagates_auth_required_error_code(self):
        auth_payload = {
            "ok": False,
            "error_code": "AUTH_REQUIRED",
            "error": "当前未登录或登录态已过期。",
        }
        with patch("tools.arrival_stats_sync_tool.call_http_service", return_value=auth_payload):
            result = arrival_stats_sync_tool.run_arrival_stats_sync({})
        self.assertEqual("AUTH_REQUIRED", result.get("error_code"))

    def test_arrive_list_sync_uses_dispatch_forecast_rows_without_detail_query(self):
        def fake_call_http_service(endpoint, request_body):
            if endpoint == "/fetch_dispatch":
                return {
                    "data": [
                        {"BILL_CODE": "H2003441275"},
                        {
                            "BILL_CODE": "R00014652502",
                            "GOODS_NAME": "测试货物",
                            "R_BILLCODE": "H2003441275",
                            "PIECE_NUMBER": 2,
                        }
                    ]
                }
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        with (
            patch("tools.arrive_list_sync_tool.call_http_service", side_effect=fake_call_http_service),
            patch("tools.arrive_list_sync_tool.replace_waybill_records", return_value={"ok": True, "replaced": 1}) as replace_records,
            patch("tools.arrive_list_sync_tool._write_sheet_resource", return_value={"ok": True, "rows": 1}) as write_sheet,
        ):
            result = arrive_list_sync_tool.run_arrive_list_sync({})

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["skipped_receipt_like"])
        self.assertEqual(1, result["bill_codes"])
        self.assertEqual(1, result["detail_records"])
        self.assertEqual("fetch_dispatch", result["source"])
        records = replace_records.call_args.args[0]
        self.assertEqual(["R00014652502"], [record["tracking_number"] for record in records])
        self.assertEqual("测试货物", records[0]["goods_name"])
        self.assertEqual(2, records[0]["quantity"])
        sheet_rows = write_sheet.call_args_list[0].args[1]
        self.assertEqual("R00014652502", sheet_rows[0][0])
        self.assertEqual("H2003441275", sheet_rows[0][5])

    def test_arrive_list_sync_passes_target_date_to_dispatch(self):
        captured_request = {}

        def fake_call_http_service(endpoint, request_body):
            self.assertEqual("/fetch_dispatch", endpoint)
            captured_request.update(request_body)
            return {"data": []}

        with (
            patch("tools.arrive_list_sync_tool.call_http_service", side_effect=fake_call_http_service),
            patch("tools.arrive_list_sync_tool._write_sheet_resource", return_value={"ok": True, "rows": 0}),
        ):
            result = arrive_list_sync_tool.run_arrive_list_sync(
                {"target_date": "2026-05-04", "dry_run": True}
            )

        self.assertTrue(result["ok"])
        self.assertEqual("2026-05-04", captured_request["params"]["target_date"])
        self.assertEqual("05.04运单编号", arrive_list_sync_tool._build_title({"target_date": "2026-05-04"})[0])

    def test_scan_sync_handles_malformed_fetch_response(self):
        with patch("tools.scan_sync_tool.call_http_service", return_value={"unexpected": True}):
            result = scan_sync_tool.run_scan_sync({})
        self.assertIn("get_scan 返回格式异常", result["error"])

    def test_arrival_stats_sync_handles_malformed_fetch_response(self):
        with patch("tools.arrival_stats_sync_tool.call_http_service", return_value={"unexpected": True}):
            with self.assertRaises(ValueError):
                arrival_stats_sync_tool.run_arrival_stats_sync({})

    def test_daily_sign_sync_merges_r13_resource_into_qianshou_request(self):
        with (
            patch(
                "tools.daily_sign_sync_tool.get_workflow_resource",
                return_value={
                    "username": "r13-user",
                    "password": "r13-pass",
                    "disp_site_code": "7390004",
                    "days": 7,
                    "_meta": {"source": "backend_console"},
                },
            ),
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                return_value=[
                    {
                        "billNumberMain": "YS1",
                        "planSignTime": "2026-04-24 10:00:00",
                        "goodsName": "demo",
                        "pcs": 1,
                    }
                ],
            ) as call_tms,
            patch("tools.daily_sign_sync_tool.sync_bitable_snapshot", return_value={"ok": True}),
            patch("tools.daily_sign_sync_tool.sync_sheet_snapshot", return_value={"ok": True}),
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(
                {"request_body": {"days": 1}, "enrich_addresses": False, "enrich_arrival_counts": False}
            )

        self.assertTrue(result["ok"])
        request_body = call_tms.call_args.args[1]
        self.assertEqual("r13-user", request_body["username"])
        self.assertEqual("r13-pass", request_body["password"])
        self.assertEqual("7390004", request_body["disp_site_code"])
        self.assertEqual(1, request_body["days"])
        self.assertNotIn("_meta", request_body)

    def test_daily_sign_sync_prefers_r13_account_manager_credentials(self):
        class FakeAccountManager:
            def resolve_role_account_params(self, params, **kwargs):
                self.kwargs = kwargs
                result = dict(params)
                result["username"] = "r13-account-user"
                result["password"] = "r13-account-pass"
                return result

        fake_manager = FakeAccountManager()
        with (
            patch("tools.daily_sign_sync_tool.get_account_manager", return_value=fake_manager),
            patch(
                "tools.daily_sign_sync_tool.get_workflow_resource",
                return_value={
                    "username": "legacy-resource-user",
                    "password": "legacy-resource-pass",
                    "disp_site_code": "7390004",
                },
            ),
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                return_value=[
                    {
                        "billNumberMain": "YS1",
                        "planSignTime": "2026-04-24 10:00:00",
                        "goodsName": "demo",
                        "pcs": 1,
                    }
                ],
            ) as call_tms,
            patch("tools.daily_sign_sync_tool.sync_bitable_snapshot", return_value={"ok": True}),
            patch("tools.daily_sign_sync_tool.sync_sheet_snapshot", return_value={"ok": True}),
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(
                {
                    "r13_account_id": "r13_default",
                    "request_body": {"days": 1},
                    "enrich_addresses": False,
                    "enrich_arrival_counts": False,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual("r13_account_id", fake_manager.kwargs["account_field"])
        self.assertEqual("", fake_manager.kwargs["output_account_field"])
        self.assertEqual("", fake_manager.kwargs["output_session_profile_field"])
        request_body = call_tms.call_args.args[1]
        self.assertEqual("r13-account-user", request_body["username"])
        self.assertEqual("r13-account-pass", request_body["password"])

    def test_daily_sign_request_body_helper_merges_r13_resource_without_meta(self):
        with patch(
            "tools.daily_sign_sync_tool.get_workflow_resource",
            return_value={
                "username": "r13-user",
                "password": "r13-pass",
                "disp_site_code": "7390004",
                "days": 7,
                "_meta": {"source": "backend_console"},
            },
        ):
            request_body = daily_sign_sync_tool.build_daily_sign_request_body(
                {"request_body": {"start": "2026-05-31 00:00:00", "end": "2026-05-31 23:59:59", "days": 1}}
            )

        self.assertEqual("r13-user", request_body["username"])
        self.assertEqual("r13-pass", request_body["password"])
        self.assertEqual("7390004", request_body["disp_site_code"])
        self.assertEqual("2026-05-31 00:00:00", request_body["start"])
        self.assertEqual("2026-05-31 23:59:59", request_body["end"])
        self.assertEqual(1, request_body["days"])
        self.assertNotIn("_meta", request_body)

    def test_daily_sign_sync_surfaces_get_qianshou_error(self):
        with patch(
            "tools.daily_sign_sync_tool.call_http_service",
            return_value={"ok": False, "error": "R13 SSO login failed", "http_status": 500},
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync({})

        self.assertIn("get_qianshou 执行失败", result["error"])
        self.assertNotIn("返回格式异常", result["error"])

    def test_daily_sign_sync_zero_rows_preserves_targets(self):
        with (
            patch("tools.daily_sign_sync_tool.call_http_service", return_value=[]),
            patch("tools.daily_sign_sync_tool.sync_bitable_snapshot") as bitable_mock,
            patch("tools.daily_sign_sync_tool.sync_sheet_snapshot") as sheet_mock,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync({})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["fetched"])
        self.assertEqual("no_fetched_rows", result["skip_reason"])
        self.assertTrue(result["bitable_result"]["skipped"])
        self.assertTrue(result["sheet_result"]["skipped"])
        bitable_mock.assert_not_called()
        sheet_mock.assert_not_called()

    def test_site_send_list_sync_zero_rows_clears_targets(self):
        bitable_result = {"ok": True, "deleted": 3, "written": 0}
        sheet_result = {
            "ok": True,
            "rows": 0,
            "clear_result": {"ok": True},
            "write_result": {"ok": True, "skipped": True, "rows": 0},
        }
        with (
            patch("tools.site_send_list_sync_tool.call_http_service", return_value={"ok": True, "data": []}),
            patch("tools.site_send_list_sync_tool.sync_bitable_snapshot", return_value=bitable_result) as bitable_mock,
            patch("tools.site_send_list_sync_tool.sync_sheet_snapshot", return_value=sheet_result) as sheet_mock,
        ):
            result = site_send_list_sync_tool.run_site_send_list_sync({})

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["fetched"])
        self.assertNotIn("skip_reason", result)
        self.assertEqual(bitable_result, result["bitable_result"])
        self.assertEqual(sheet_result, result["sheet_result"])
        bitable_mock.assert_called_once_with("phase7.site_send_bitable", [], {})
        sheet_mock.assert_called_once_with("phase7.site_send_sheet", [], {})

    def test_daily_sign_sheet_values_match_header_columns(self):
        values = daily_sign_sync_tool._build_sheet_values(
            [
                {
                    "billNumberMain": "YS1",
                    "planSignTime": "2026-04-25 23:59:59",
                    "goodsName": "固化剂",
                    "packTypeDesc": "编织袋+桶",
                    "pcs": 30,
                    "dispAddress": "湖南省邵阳市大祥区雨溪镇",
                    "dispatchMode": "送货（不含上楼）",
                }
            ]
        )

        self.assertEqual(8, len(values[0]))
        self.assertEqual("湖南省邵阳市大祥区雨溪镇", values[0][5])
        self.assertEqual("送货（不含上楼）", values[0][6])
        self.assertEqual("", values[0][7])

    def test_daily_sign_sync_sorts_feishu_output_by_plan_sign_time(self):
        written_records = []
        written_values = []

        def capture_bitable(_resource_key, records, _params):
            written_records.extend(records)
            return {"ok": True, "written": len(records)}

        def capture_sheet(_resource_key, values, _params):
            written_values.extend(values)
            return {"ok": True, "rows": len(values)}

        with (
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                return_value=[
                    {
                        "billNumberMain": "LATE",
                        "planSignTime": "2026-06-21 23:59:59",
                        "goodsName": "配件",
                        "packTypeDesc": "托盘",
                        "pcs": 2,
                    },
                    {
                        "billNumberMain": "EARLY",
                        "planSignTime": "2026-06-19 23:59:59",
                        "goodsName": "配件",
                        "packTypeDesc": "纸箱",
                        "pcs": 1,
                    },
                    {
                        "billNumberMain": "MIDDLE",
                        "planSignTime": "2026-06-20 23:59:59",
                        "goodsName": "配件",
                        "packTypeDesc": "编织袋",
                        "pcs": 5,
                    },
                ],
            ),
            patch("tools.daily_sign_sync_tool.sync_bitable_snapshot", side_effect=capture_bitable),
            patch("tools.daily_sign_sync_tool.sync_sheet_snapshot", side_effect=capture_sheet),
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(
                {"enrich_addresses": False, "enrich_arrival_counts": False}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["EARLY", "MIDDLE", "LATE"], [row[0] for row in written_values])
        self.assertEqual(
            ["EARLY", "MIDDLE", "LATE"],
            [record["fields"]["运单编号"] for record in written_records],
        )

    def test_daily_sign_sync_writes_arrived_quantity_to_sheet_column_h(self):
        written_values = []
        captured_sheet_params = {}

        def capture_sheet(_resource_key, values, _params):
            written_values.extend(values)
            captured_sheet_params.update(_params)
            return {"ok": True, "rows": len(values)}

        with (
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                return_value=[
                    {
                        "billNumberMain": "R0001",
                        "planSignTime": "2026-06-04 23:59:59",
                        "goodsName": "配件",
                        "packTypeDesc": "编织袋",
                        "pcs": 6,
                        "dispAddress": "湖南省邵阳市大祥区",
                        "dispatchMode": "送货（不含上楼）",
                    }
                ],
            ),
            patch(
                "tools.daily_sign_sync_tool.get_waybill_tracking_cache",
                create=True,
                return_value={"arrived_quantity": 4},
            ),
            patch("tools.daily_sign_sync_tool.sync_bitable_snapshot", return_value={"ok": True, "written": 1}),
            patch("tools.daily_sign_sync_tool.sync_sheet_snapshot", side_effect=capture_sheet),
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(
                {
                    "enrich_addresses": False,
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:G100",
                    "clear_range": "Sheet1!A2:G100",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(8, len(written_values[0]))
        self.assertEqual(4, written_values[0][7])
        self.assertEqual("Sheet1!A2:H2", captured_sheet_params["range"])
        self.assertEqual("Sheet1!A2:H100", captured_sheet_params["clear_range"])

    def test_daily_sign_sync_enriches_masked_addresses_before_writing(self):
        written_records = []
        written_values = []

        def capture_bitable(_resource_key, records, _params):
            written_records.extend(records)
            return {"ok": True, "written": len(records)}

        def capture_sheet(_resource_key, values, _params):
            written_values.extend(values)
            return {"ok": True, "rows": len(values)}

        with (
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                side_effect=[
                    [
                        {
                            "billNumberMain": "R0001",
                            "planSignTime": "2026-05-20 23:59:59",
                            "goodsName": "瓦",
                            "packTypeDesc": "托盘袋",
                            "pcs": 2,
                            "dispAddress": "湖南省******",
                            "dispatchMode": "送货（不含上楼）",
                        }
                    ],
                    {
                        "ok": True,
                        "data": [
                            {
                                "tracking_number": "R0001",
                                "recipient_address": "湖南省邵阳市大祥区雨溪镇",
                            }
                        ],
                    },
                ],
            ) as call_tms,
            patch("tools.daily_sign_sync_tool.sync_bitable_snapshot", side_effect=capture_bitable),
            patch("tools.daily_sign_sync_tool.sync_sheet_snapshot", side_effect=capture_sheet),
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync({"enrich_arrival_counts": False})

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["address_enrichment"]["updated"])
        self.assertEqual("湖南省邵阳市大祥区雨溪镇", written_records[0]["fields"]["收件人地址"])
        self.assertEqual("湖南省邵阳市大祥区雨溪镇", written_values[0][5])
        self.assertEqual("/query_waybill_detail", call_tms.call_args_list[1].args[0])
        self.assertEqual(
            [{"bill_code": "R0001"}],
            call_tms.call_args_list[1].args[1]["params"]["items"],
        )

    def test_sheet_snapshot_can_clear_wider_range_than_write_range(self):
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:G3",
            "clear_range": "Sheet1!A2:H3",
        }

        with (
            patch("tools.phase7_sync_common.get_workflow_resource", return_value=resource),
            patch("tools.phase7_sync_common.feishu_operation", return_value={"ok": True}) as feishu_op,
        ):
            result = phase7_sync_common.sync_sheet_snapshot(
                "phase7.daily_sign_sheet",
                [["bill", "time", "goods", "pack", 1, "addr", "mode"]],
                {},
            )

        self.assertTrue(result["ok"])
        self.assertEqual("clear_sheet", feishu_op.call_args_list[0].args[0])
        self.assertEqual("Sheet1!A2:H3", feishu_op.call_args_list[0].args[1]["range"])
        self.assertEqual("write_sheet", feishu_op.call_args_list[1].args[0])
        self.assertEqual("Sheet1!A2:G3", feishu_op.call_args_list[1].args[1]["range"])

    def test_sheet_snapshot_includes_clear_error_detail(self):
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:G3",
            "clear_range": "Sheet1!A2:H3",
        }

        with (
            patch("tools.phase7_sync_common.get_workflow_resource", return_value=resource),
            patch(
                "tools.phase7_sync_common.feishu_operation",
                return_value={"error": "range not found"},
            ),
        ):
            result = phase7_sync_common.sync_sheet_snapshot(
                "phase7.daily_sign_sheet",
                [["bill", "time", "goods", "pack", 1, "addr", "mode"]],
                {},
            )

        self.assertIn("飞书清空电子表格失败", result["error"])
        self.assertIn("range not found", result["error"])

    def test_feishu_clear_sheet_uses_dimension_apis(self):
        result = feishu_cli_tool.feishu_operation(
            "clear_sheet",
            {
                "spreadsheet_token": "sheet-token",
                "range": "Sheet1!A2:H3",
                "dry_run": True,
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual("DELETE", result["api"][0]["method"])
        self.assertEqual(
            "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range",
            result["api"][0]["url"],
        )
        self.assertEqual(
            {
                "dimension": {
                    "sheetId": "Sheet1",
                    "majorDimension": "ROWS",
                    "startIndex": 2,
                    "endIndex": 3,
                }
            },
            result["api"][0]["body"],
        )
        self.assertEqual(1, len(result["api"]))

    def test_feishu_clear_sheet_resolves_sheet_title_before_delete(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {"sheet_id": "abc123", "title": "Sheet1"},
                        ],
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:H3",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("abc123!A2:H3", result["range"])
        self.assertEqual("GET", calls[0][0])
        self.assertEqual(
            "/open-apis/sheets/v3/spreadsheets/sheet-token/sheets/query",
            calls[0][1],
        )
        self.assertEqual("DELETE", calls[1][0])
        self.assertEqual(
            "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range",
            calls[1][1],
        )
        self.assertEqual("abc123", calls[1][2]["dimension"]["sheetId"])
        self.assertEqual("ROWS", calls[1][2]["dimension"]["majorDimension"])
        self.assertEqual(2, calls[1][2]["dimension"]["startIndex"])
        self.assertEqual(3, calls[1][2]["dimension"]["endIndex"])
        self.assertEqual(2, len(calls))

    def test_feishu_clear_sheet_uses_only_sheet_when_title_changed(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "4103ec",
                                "title": "Yunda data",
                                "gridProperties": {"rowCount": 12},
                            }
                        ]
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:Y5000",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("4103ec!A2:Y12", result["range"])
        self.assertEqual("4103ec", calls[1][2]["dimension"]["sheetId"])
        self.assertEqual(12, calls[1][2]["dimension"]["endIndex"])

    def test_feishu_clear_sheet_caps_end_row_to_sheet_row_count(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "abc123",
                                "title": "Sheet1",
                                "grid_properties": {"row_count": 200},
                            },
                        ],
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:Y5000",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("abc123!A2:Y200", result["range"])
        self.assertEqual("DELETE", calls[1][0])
        self.assertEqual(2, calls[1][2]["dimension"]["startIndex"])
        self.assertEqual(200, calls[1][2]["dimension"]["endIndex"])
        self.assertEqual(2, len(calls))

    def test_feishu_clear_sheet_caps_end_row_from_camel_case_grid_properties(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "Sheet1",
                                "title": "Sheet1",
                                "gridProperties": {"rowCount": 10},
                            },
                        ],
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:Y5000",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("Sheet1!A2:Y10", result["range"])
        self.assertEqual(10, calls[1][2]["dimension"]["endIndex"])
        self.assertEqual(2, len(calls))

    def test_sheet_snapshot_deletes_then_adds_rows_before_writing(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:Y10",
            "clear_range": "Sheet1!A2:Y5000",
        }
        calls = []
        query_count = 0

        def fake_call_open_api(method, path, payload=None, timeout=30):
            nonlocal query_count
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                query_count += 1
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "4103ec",
                                "title": "Sheet1",
                                "gridProperties": {"rowCount": 5 if query_count == 1 else 1},
                            }
                        ]
                    },
                }
            return {"code": 0, "data": {}}

        values = [[f"r{row}c{col}" for col in range(25)] for row in range(9)]
        with (
            patch("tools.phase7_sync_common.get_workflow_resource", return_value=resource),
            patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api),
        ):
            result = phase7_sync_common.sync_sheet_snapshot("phase7.yunda_send_waybills_sheet", values, {})

        self.assertTrue(result["ok"])
        self.assertEqual(("DELETE", "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range"), calls[1][:2])
        self.assertEqual(("POST", "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range"), calls[3][:2])
        self.assertEqual({"sheetId": "4103ec", "majorDimension": "ROWS", "length": 9}, calls[3][2]["dimension"])
        self.assertEqual(("PUT", "/open-apis/sheets/v2/spreadsheets/sheet-token/values"), calls[4][:2])

    def test_feishu_clear_sheet_skips_when_range_starts_after_row_count(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "abc123",
                                "title": "Sheet1",
                                "grid_properties": {"row_count": 1},
                            },
                        ],
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:Y5000",
                },
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual("abc123!A2:Y5000", result["range"])
        self.assertEqual(1, len(calls))

    def test_pre_arrive_site_code_falls_back_to_default(self):
        session = _DummySession(_DummyResponse(status_code=200, text="<html></html>"))
        with patch.dict(fetch_pre_arrive_list.os.environ, {}, clear=True):
            site_code, source = fetch_pre_arrive_list._resolve_site_code_http(
                session,
                {},
                timeout_sec=1,
            )

        self.assertEqual("7390004", site_code)
        self.assertEqual("default", source)

    def test_arrive_list_sheet_result_summary_does_not_expose_tokens(self):
        result = arrive_list_sync_tool._summarize_feishu_result(
            {
                "ok": True,
                "identity": "bot",
                "data": {
                    "spreadsheetToken": "sensitive-token",
                    "updatedCells": 18,
                    "updatedRows": 1,
                    "updatedColumns": 18,
                    "updatedRange": "Sheet1!A1:R1",
                },
            }
        )

        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sensitive-token", dumped)
        self.assertNotIn("spreadsheetToken", dumped)
        self.assertEqual(18, result["updatedCells"])

    def test_arrival_stats_counts_from_scan_index_for_all_trackings(self):
        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            [
                {"raw_code": "R000143402890001", "destination": "demo", "code_type": "child"},
                {"raw_code": "R000143402890002", "destination": "demo", "code_type": "child"},
                {"raw_code": "2001513259", "destination": "demo", "code_type": "main"},
            ],
            [
                {"tracking_number": "R00014340289", "quantity": 2},
                {"tracking_number": "2001513259", "quantity": 20},
                {"tracking_number": "2003503200", "quantity": 5},
            ],
            ["R00014340289", "2001513259", "2003503200"],
        )

        self.assertEqual(2, count_map["R00014340289"])
        self.assertEqual(0, count_map["2001513259"])
        self.assertEqual(0, count_map["2003503200"])
        self.assertEqual("scan_index", result["source"])
        self.assertEqual(3, result["counted"])
        self.assertEqual(1, result["arrived_nonzero"])
        self.assertEqual(0, result["quantity_adjustments"])
        self.assertEqual(1, result["quantity_gaps"])

    def test_arrival_stats_refreshes_existing_masked_waybill_records(self):
        tracking_numbers, plan = arrival_stats_sync_tool._detail_tracking_numbers(
            [
                {
                    "tracking_number": "R0001",
                    "goods_name": "配件",
                    "recipient_name": "张三",
                    "recipient_phone": "13800000000",
                    "recipient_address": "湖南省邵阳市大祥区测试路1号",
                    "destination_station": "邵阳大祥S站",
                },
                {
                    "tracking_number": "R0002",
                    "goods_name": "大米",
                    "recipient_name": "李*",
                    "recipient_phone": "158****7398",
                    "recipient_address": "湖南省邵阳市大祥区测试路2号",
                    "destination_station": "邵阳大祥S站",
                },
            ],
            ["R0003", "R0002"],
            {},
        )

        self.assertEqual(["R0003", "R0002"], tracking_numbers)
        self.assertEqual({"missing": 2, "stale": 1, "total": 2}, plan)

    def test_arrival_stats_adds_current_scan_missing_main_trackings(self):
        existing_records = [
            {
                "tracking_number": "R00014600001",
                "goods_name": "货物1",
                "quantity": 1,
                "recipient_name": "张三",
                "recipient_phone": "13800000000",
                "recipient_address": "湖南省邵阳市大祥区测试路1号",
                "destination_station": "邵阳",
            },
            {
                "tracking_number": "R00014600002",
                "goods_name": "货物2",
                "quantity": 1,
                "recipient_name": "李四",
                "recipient_phone": "13900000000",
                "recipient_address": "湖南省邵阳市大祥区测试路2号",
                "destination_station": "邵阳",
            },
        ]
        current_scan_rows = [
            {"raw_code": "R00014600001", "destination": "邵阳", "code_type": "main"},
            {"raw_code": "R00014600002", "destination": "邵阳", "code_type": "main"},
            {"raw_code": "R00014600003", "destination": "邵阳", "code_type": "main"},
            {"raw_code": "R000146000040001", "destination": "邵阳", "code_type": "child"},
            {"raw_code": "R000146000050001", "destination": "邵阳", "code_type": "child"},
        ]
        fetched_records = [
            {"tracking_number": "R00014600003", "goods_name": "补抓3", "quantity": 1, "destination_station": "邵阳"},
            {"tracking_number": "R00014600004", "goods_name": "补抓4", "quantity": 1, "destination_station": "邵阳"},
            {"tracking_number": "R00014600005", "goods_name": "补抓5", "quantity": 1, "destination_station": "邵阳"},
        ]
        written_values = []

        def fake_write_stats(resource_key, values, params):
            written_values.append(values)
            return {"ok": True, "rows": len(values)}

        with (
            patch("tools.arrival_stats_sync_tool._refresh_scan_index", return_value=(current_scan_rows, {"ok": True})),
            patch("tools.arrival_stats_sync_tool.list_scan_codes", return_value=current_scan_rows),
            patch("tools.arrival_stats_sync_tool.list_waybill_records", return_value=existing_records),
            patch("tools.arrival_stats_sync_tool._fetch_waybill_details", return_value=(fetched_records, {"ok": True, "requested": 3, "fetched": 3})) as fetch_details,
            patch("tools.arrival_stats_sync_tool._write_stats_sheet", side_effect=fake_write_stats),
        ):
            result = arrival_stats_sync_tool.run_arrival_stats_sync(
                {
                    "dry_run": True,
                    "archive_snapshot": False,
                    "pending_sheet_disabled": True,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["R00014600003", "R00014600004", "R00014600005"], fetch_details.call_args.args[0])
        self.assertEqual(5, result["records"])
        self.assertEqual("dry_run", result["split_pending_result"]["reason"])
        self.assertEqual(5, result["split_pending_result"]["source_rows"])
        self.assertEqual(6, len(written_values[0]))
        self.assertEqual(
            ["R00014600001", "R00014600002", "R00014600003", "R00014600004", "R00014600005"],
            [row[0] for row in written_values[0][1:]],
        )

    def test_arrival_stats_does_not_use_total_quantity_for_partial_child_arrivals(self):
        scan_rows = [
            {
                "raw_code": f"R00014371325{index:04d}",
                "destination": "邵阳自提部",
                "code_type": "child",
            }
            for index in range(1, 30)
        ]

        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            scan_rows,
            [{"tracking_number": "R00014371325", "quantity": 31}],
            ["R00014371325"],
        )

        self.assertEqual(29, count_map["R00014371325"])
        self.assertEqual(0, result["quantity_adjustments"])
        self.assertEqual(1, result["quantity_gaps"])

    def test_arrival_stats_counts_accumulate_across_days(self):
        # 11-piece shipment: 5 children scanned yesterday + 6 today should sum to 11
        yesterday_rows = [
            {"raw_code": f"R0001437132500{idx:02d}", "destination": "邵阳", "code_type": "child"}
            for idx in range(1, 6)
        ]
        today_rows = [
            {"raw_code": f"R0001437132500{idx:02d}", "destination": "邵阳", "code_type": "child"}
            for idx in range(6, 12)
        ]
        cumulative_rows = yesterday_rows + today_rows

        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            cumulative_rows,
            [{"tracking_number": "R00014371325", "quantity": 11}],
            ["R00014371325"],
        )

        self.assertEqual(11, count_map["R00014371325"])
        self.assertEqual(1, result["arrived_nonzero"])
        self.assertEqual(0, result["quantity_gaps"])

    def test_normalize_scan_rows_emits_main_tracking(self):
        normalized = phase7_mysql_store.normalize_scan_rows(
            [
                {"扫描单号": "2001513259", "目的地": "邵阳"},
                {"扫描单号": "R000143402890001", "目的地": "邵阳"},
                {"扫描单号": "20055750680002", "目的地": "邵阳武冈站"},
            ]
        )
        by_code = {row["raw_code"]: row for row in normalized}
        self.assertEqual("2001513259", by_code["2001513259"]["main_tracking"])
        self.assertEqual("main", by_code["2001513259"]["code_type"])
        self.assertEqual("R00014340289", by_code["R000143402890001"]["main_tracking"])
        self.assertEqual("child", by_code["R000143402890001"]["code_type"])
        self.assertEqual("2005575068", by_code["20055750680002"]["main_tracking"])
        self.assertEqual("child", by_code["20055750680002"]["code_type"])

    def test_arrival_stats_collapses_numeric_ronghui_child_trackings(self):
        scan_rows = [
            {"raw_code": "20055750680002", "destination": "邵阳武冈站", "code_type": "child"},
            {"raw_code": "20055750680004", "destination": "邵阳武冈站", "code_type": "child"},
            {"raw_code": "20055750680020", "destination": "邵阳武冈站", "code_type": "child"},
        ]

        missing = arrival_stats_sync_tool._missing_trackings_from_current_scan(scan_rows, [], {})
        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            scan_rows,
            [{"tracking_number": "2005575068", "quantity": 20}],
            ["2005575068"],
        )

        self.assertEqual(["2005575068"], missing)
        self.assertEqual(3, count_map["2005575068"])
        self.assertEqual(1, result["arrived_nonzero"])
        self.assertEqual(1, result["quantity_gaps"])
        self.assertFalse(phase7_mysql_store.should_include_waybill_tracking("20055750680002"))
        self.assertTrue(phase7_mysql_store.should_include_waybill_tracking("2005575068"))

    def test_phase7_mysql_wsl_gateway_uses_localhost_outside_wsl(self):
        with (
            patch.dict(os.environ, {"DOCFLOW_MYSQL_HOST": "wsl-gateway"}, clear=True),
            patch.object(phase7_mysql_store, "_running_in_wsl", return_value=False),
        ):
            self.assertEqual("127.0.0.1", phase7_mysql_store._resolve_mysql_host())

    def test_phase7_mysql_wsl_gateway_uses_gateway_inside_wsl(self):
        with (
            patch.dict(os.environ, {"DOCFLOW_MYSQL_HOST": "wsl-gateway"}, clear=True),
            patch.object(phase7_mysql_store, "_running_in_wsl", return_value=True),
            patch.object(phase7_mysql_store, "_wsl_gateway_ip", return_value="172.25.63.253"),
        ):
            self.assertEqual("172.25.63.253", phase7_mysql_store._resolve_mysql_host())

    def test_phase7_mysql_prefers_agent_db_host(self):
        with patch.dict(
            os.environ,
            {"AGENT_DB_HOST": "agent-db.internal", "DOCFLOW_MYSQL_HOST": "wsl-gateway"},
            clear=True,
        ):
            self.assertEqual("agent-db.internal", phase7_mysql_store._resolve_mysql_host())

    def test_apply_scan_window_default_does_not_inject_dates(self):
        params = arrival_stats_sync_tool._apply_scan_window({"output_format": "json"}, 1)
        self.assertNotIn("start", params)
        self.assertNotIn("end", params)

    def test_apply_scan_window_uses_target_date_for_single_day(self):
        params = arrival_stats_sync_tool._apply_scan_window(
            {"output_format": "json"},
            1,
            date(2026, 5, 4),
        )
        self.assertEqual("2026/05/04", params["date"])

    def test_apply_scan_window_widens_to_n_days(self):
        from datetime import datetime as _dt

        params = arrival_stats_sync_tool._apply_scan_window({"output_format": "json"}, 30)
        self.assertIn("start", params)
        self.assertIn("end", params)
        start_dt = _dt.strptime(params["start"], "%Y/%m/%d %H:%M:%S")
        end_dt = _dt.strptime(params["end"], "%Y/%m/%d %H:%M:%S")
        # Inclusive 30-day span -> 29 days between start and end
        self.assertEqual(29, (end_dt.date() - start_dt.date()).days)

    def test_render_stats_sheet_values_uses_target_date_header(self):
        values = phase7_mysql_store.render_stats_sheet_values(
            [{"tracking_number": "R0001"}],
            {},
            target_date="2026-05-04",
        )
        self.assertTrue(str(values[0][0]).startswith("05.04"))

    def test_apply_scan_window_respects_user_override(self):
        params = arrival_stats_sync_tool._apply_scan_window(
            {"output_format": "json", "start": "2026/04/20 00:00:00"},
            30,
        )
        self.assertEqual("2026/04/20 00:00:00", params["start"])
        self.assertNotIn("end", params)

    def test_render_pending_sheet_values_formats_status_and_counts(self):
        from datetime import datetime as _dt

        values = phase7_mysql_store.render_pending_sheet_values(
            [
                {
                    "tracking_number": "R0001",
                    "destination_station": "邵阳大祥S站",
                    "expected_quantity": 11,
                    "arrived_quantity": 5,
                    "pending_quantity": 6,
                    "arrival_status": "partial",
                    "first_arrival_at": _dt(2026, 4, 25, 9, 30, 0),
                    "last_arrival_at": _dt(2026, 4, 26, 14, 15, 30),
                },
                {
                    "tracking_number": "R0002",
                    "destination_station": "邵阳大祥S站",
                    "expected_quantity": 3,
                    "arrived_quantity": 0,
                    "pending_quantity": 3,
                    "arrival_status": "pending",
                    "first_arrival_at": None,
                    "last_arrival_at": None,
                },
            ]
        )

        self.assertEqual(phase7_mysql_store.PENDING_ARRIVAL_HEADERS, values[0])
        self.assertEqual(["R0001", "邵阳大祥S站", 11, 5, 6, "部分到货", "2026-04-25 09:30:00", "2026-04-26 14:15:30"], values[1])
        self.assertEqual(["R0002", "邵阳大祥S站", 3, 0, 3, "未到货", "", ""], values[2])

    def test_arrive_and_stats_sheet_numeric_cells_are_numbers(self):
        record = {
            "tracking_number": "R0001",
            "goods_name": "配件",
            "package_type": "纸箱",
            "delivery_method": "派送",
            "quantity": 2,
            "receipt_number": "",
            "actual_weight": Decimal("12.50"),
            "volume": Decimal("0.30"),
            "remarks": "",
            "destination_station": "邵阳",
            "recipient_name": "张三",
            "recipient_phone": "13800000000",
            "recipient_address": "湖南省邵阳市",
            "settlement_weight": Decimal("13.00"),
            "volumetric_weight": Decimal("10.50"),
            "shipping_fee": Decimal("21.30"),
            "payment_type": "现金",
            "pay_on_arrival": Decimal("0.00"),
        }

        arrive_row = phase7_mysql_store.render_arrive_sheet_rows([record])[0]
        stats_row = phase7_mysql_store.render_stats_sheet_values([record], {"R0001": 2})[1]

        for row in (arrive_row, stats_row):
            self.assertIsInstance(row[4], int)
            self.assertIsInstance(row[6], float)
            self.assertIsInstance(row[7], float)
            self.assertIsInstance(row[13], int)
            self.assertIsInstance(row[14], float)
            self.assertIsInstance(row[15], float)
            self.assertIsInstance(row[17], int)
            self.assertIsInstance(row[0], str)
            self.assertIsInstance(row[11], str)
        self.assertIsInstance(stats_row[18], int)

    def test_waybill_export_headers_use_累计_label(self):
        self.assertEqual("累计到货件数", phase7_mysql_store.WAYBILL_EXPORT_HEADERS[-1])

    def test_yunda_waybill_tracking_maps_route_rows(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def json(self):
                return {
                    "total": 1,
                    "rows": [
                        {
                            "977808459": {
                                "smi": {
                                    "1": {
                                        "Scan_Time": "2026-05-10 17:28:54",
                                        "description": (
                                            "快件在<span data-original-title=\"56512000&lt;br&gt;"
                                            "江苏无锡分拨中心&lt;br&gt;分拨经理【戴昭刚】"
                                            "&lt;br&gt;分拨客服电话【0512-87830060】\">"
                                            "【湖南邵阳双清滨江公司】</span>已揽件开单"
                                        ),
                                        "SR": "网点系统",
                                        "DV": "81102202005961",
                                    },
                                    "2": {
                                        "Scan_Time": "2026-05-12 13:11:45",
                                        "description": "快件已被客户<span>【指定位置】</span>签收",
                                        "SR": "02",
                                        "DV": "56571150217797",
                                    },
                                    "3": {
                                        "Scan_Time": "2026-05-13 10:00:00",
                                        "description": (
                                            "快件到达<span class=\"siteName\">【江苏无锡分拨中心】</span>"
                                            "上一站是<span class=\"siteName\">【湖南长沙分拨中心】</span>"
                                        ),
                                        "SR": "01",
                                        "DV": "81102439001556",
                                    },
                                    "info": {"ignored": True},
                                }
                            }
                        },
                    ],
                    "site": {
                        "江苏无锡分拨中心": {
                            "site_code": 56512000,
                            "site_name": "江苏无锡分拨中心",
                            "type": 3,
                            "fzr": "赖照刚",
                            "problem_phone": "0512-87830060",
                        },
                        "湖南长沙分拨中心": {
                            "site_code": 56731000,
                            "site_name": "湖南长沙分拨中心",
                            "type": 3,
                            "fzr": "邓鑫",
                            "problem_phone": "0731-89512469",
                        },
                    },
                }

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"method": "POST", "url": url, "data": data})
                return Response()

        session = Session()
        result = yunda_waybill_tracking.query_yunda_tracking(
            session,
            "977808459",
            {},
        )

        self.assertTrue(result["ok"])
        self.assertEqual("yunda", result["type"])
        self.assertEqual("977808459", result["tracking_number"])
        self.assertEqual(3, len(result["route_rows"]))
        self.assertEqual("2026-05-10 17:28:54", result["route_rows"][0]["scan_time"])
        self.assertEqual(
            "江苏无锡分拨中心：分拨经理【戴昭刚】；分拨客服电话【0512-87830060】",
            result["route_rows"][0]["contact"],
        )
        self.assertEqual("网点系统", result["route_rows"][0]["data_source"])
        self.assertEqual("签收", result["route_rows"][1]["status"])
        self.assertEqual("56571150217797", result["route_rows"][1]["device_no"])
        self.assertEqual(
            "江苏无锡分拨中心：分拨经理【赖照刚】；分拨客服电话【0512-87830060】\n"
            "湖南长沙分拨中心：分拨经理【邓鑫】；分拨客服电话【0731-89512469】",
            result["route_rows"][2]["contact"],
        )
        self.assertEqual("977808459", session.calls[0]["data"]["Ids[]"])

    def test_yunda_waybill_tracking_maps_waybill_details(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def json(self):
                return {
                    "rows": [
                        {
                            "980077246": {
                                "logistics": {
                                    "Logistics_Id": "980077246",
                                    "Sender_Name": "勇胜",
                                    "Sender_Phone": "073*****128",
                                    "Buyer_Name": "洪师傅",
                                    "Buyer_Mobile": "158****9716",
                                    "Buyer_Destination_Dot_Code": "安徽铜陵公司三分部",
                                    "Buyer_Address": "安徽省铜陵市郊区铜都大道中段",
                                    "Item_Name": "吨袋",
                                    "Packing_Type": "编织袋:12",
                                    "Item_Total_Number": 12,
                                    "Gross_Weight": "522.00",
                                    "Settlement_Total_Number": "557.90",
                                    "Volume": "2.7895",
                                    "Payment_Type": "现金",
                                    "Freight": "12.00",
                                    "Shipping_Methods": "180",
                                    "COD": "0.00",
                                    "Remarks": "测试备注",
                                },
                                "smi": {
                                    "1": {
                                        "Scan_Time": "2026-05-10 17:28:54",
                                        "description": "快件已揽件开单",
                                    }
                                },
                            }
                        }
                    ]
                }

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        result = yunda_waybill_tracking.query_yunda_tracking(
            Session(),
            "980077246",
            {},
        )

        self.assertEqual("980077246", result["waybill_stub"]["waybill_no"])
        self.assertEqual("勇胜", result["waybill_stub"]["sender_name"])
        self.assertEqual("洪师傅", result["waybill_stub"]["recipient_name"])
        self.assertEqual("安徽铜陵公司三分部", result["waybill_stub"]["disp_site"])
        self.assertEqual("吨袋", result["waybill_stub"]["goods_name"])
        self.assertEqual("派送", result["waybill_stub"]["delivery_method"])
        self.assertEqual("522.00 kg", result["waybill_stub"]["weight"])
        self.assertEqual("测试备注", result["waybill_stub"]["remark"])
        info_sections = {section["title"]: section["items"] for section in result["waybill_info"]}
        self.assertIn({"label": "寄件人", "value": "勇胜"}, info_sections["发货信息"])
        self.assertIn({"label": "收货人", "value": "洪师傅"}, info_sections["收货信息"])
        self.assertIn({"label": "目的网点", "value": "安徽铜陵公司三分部"}, info_sections["收货信息"])
        self.assertIn({"label": "货物名称", "value": "吨袋"}, info_sections["货物信息"])
        self.assertIn({"label": "派送方式", "value": "派送"}, info_sections["货物信息"])
        self.assertIn({"label": "运费", "value": "12.00"}, info_sections["费用信息"])

    def test_yunda_waybill_tracking_decrypts_masked_contact_details(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def __init__(self, payload):
                self._payload = payload
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"url": url, "data": data})
                if url.endswith("/system/mail/getOriginalData.html"):
                    return Response(
                        {
                            "data": {
                                "Sender_Name": "勇胜",
                                "Sender_Phone": "07315186128",
                                "Buyer_Name": "振杰",
                                "Buyer_Mobile": "13700003310",
                            }
                        }
                    )
                return Response(
                    {
                        "rows": [
                            {
                                "980520249": {
                                    "logistics": {
                                        "Logistics_Id": "980520249",
                                        "Sender_Name": "勇*",
                                        "Sender_Phone": "073*****128",
                                        "Buyer_Name": "振*",
                                        "Buyer_Mobile": "137*****3310",
                                        "Buyer_Address": "四川省成都市新都区中集大道",
                                        "Shipping_Methods": "自提",
                                    },
                                    "smi": {
                                        "1": {
                                            "Scan_Time": "2026-05-31 10:00:00",
                                            "description": "快件已到达",
                                        }
                                    },
                                }
                            }
                        ]
                    }
                )

        session = Session()
        result = yunda_waybill_tracking.query_yunda_tracking(session, "980520249", {})

        self.assertEqual("勇胜", result["waybill_stub"]["sender_name"])
        self.assertEqual("振杰", result["waybill_stub"]["recipient_name"])
        info_sections = {section["title"]: section["items"] for section in result["waybill_info"]}
        self.assertIn({"label": "寄件人", "value": "勇胜"}, info_sections["发货信息"])
        self.assertIn({"label": "寄件电话", "value": "07315186128"}, info_sections["发货信息"])
        self.assertIn({"label": "收货人", "value": "振杰"}, info_sections["收货信息"])
        self.assertIn({"label": "收货电话", "value": "13700003310"}, info_sections["收货信息"])
        self.assertEqual(
            ["/system/mail/list.html", "/system/mail/getOriginalData.html"],
            [call["url"][call["url"].find("/system/mail/") :] for call in session.calls],
        )

    def test_tracking_query_detects_providers(self):
        self.assertEqual("yunda", tracking_query.detect_tracking_provider("977808459"))
        self.assertEqual("yunda", tracking_query.detect_tracking_provider("298861675"))
        self.assertEqual("yunda", tracking_query.detect_tracking_provider("708429045"))
        self.assertEqual("ronghui", tracking_query.detect_tracking_provider("200123456"))
        self.assertEqual("zhuanxian", tracking_query.detect_tracking_provider("000123456"))

    def test_yunda_waybill_tracking_accepts_empty_route_response(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def json(self):
                return {"total": 0, "rows": []}

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        result = yunda_waybill_tracking.query_yunda_tracking(
            Session(),
            "977808459",
            {},
        )

        self.assertTrue(result["ok"])
        self.assertEqual("yunda", result["type"])
        self.assertEqual([], result["route_rows"])

    def test_track_waybill_tool_calls_unified_tms_endpoint(self):
        with patch(
            "tools.track_waybill_tool.call_http_service",
            return_value={
                "ok": True,
                "cost_sec": 0.01,
                "data": {
                    "ok": True,
                    "type": "yunda",
                    "tracking_number": "977808459",
                    "route_rows": [],
                },
            },
        ) as call_http:
            result = track_waybill_tool.run_track_waybill({"tracking_number": " 977808459 "})

        self.assertEqual("yunda", result["type"])
        self.assertEqual("977808459", result["tracking_number"])
        self.assertEqual("/tms/tracking_query", call_http.call_args.args[0])
        self.assertEqual("977808459", call_http.call_args.args[1]["params"]["tracking_number"])

    def test_track_waybill_tool_rejects_invalid_r_tracking_number_without_http_call(self):
        with patch("tools.track_waybill_tool.call_http_service") as call_http:
            result = track_waybill_tool.run_track_waybill({"tracking_number": "R000016211453"})

        self.assertEqual(
            {
                "error": "单号格式错误：R 开头融辉单号应为 R+11位主单或 R+15位子单，请检查是否多输/少输数字。",
                "error_code": "INVALID_TRACKING_NUMBER",
            },
            result,
        )
        call_http.assert_not_called()

    def test_tms_runtime_exposes_ronghui_tms_tracking_target(self):
        from agent.tms_runtime.dispatch import TARGETS, TARGET_ACCOUNT_SYSTEMS

        self.assertIn("ronghui_tms_tracking", TARGETS)
        self.assertIn("yunda_waybill_tracking", TARGETS)
        self.assertIn("yunda_waybill_entry", TARGETS)
        self.assertIn("yunda_price", TARGETS)
        self.assertIn("tracking_query", TARGETS)
        self.assertIn("yunda_dispatch_forecast", TARGETS)
        self.assertIn("yunda_send_waybills", TARGETS)
        self.assertEqual("ronghui", TARGET_ACCOUNT_SYSTEMS["ronghui_tms_tracking"])
        self.assertEqual("yunda", TARGET_ACCOUNT_SYSTEMS["yunda_price"])
        self.assertIn("/tms/ronghui_tms_tracking", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_waybill_tracking", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_waybill_entry", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_price", {route.path for route in router.routes})
        self.assertIn("/tms/tracking_query", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_dispatch_forecast", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_send_waybills", {route.path for route in router.routes})

    def test_query_waybill_detail_requests_decrypted_view(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"result": {"data": [{"BILL_CODE": "R0001"}]}}

        class Session:
            def __init__(self):
                self.data = None

            def post(self, *args, **kwargs):
                self.data = kwargs["data"]
                return Response()

        session = Session()

        row = tms_query_waybill_detail._query_one(session, "R0001")

        self.assertEqual("R0001", row["tracking_number"])
        self.assertEqual({"billCode": "R0001", "isView": "true"}, session.data)

    def test_query_waybill_detail_skips_browser_when_api_row_is_complete(self):
        api_row = {
            "requested_bill_code": "R0001",
            "tracking_number": "R0001",
            "goods_name": "配件",
            "recipient_name": "张三",
            "recipient_phone": "13800000000",
            "recipient_address": "湖南省邵阳市大祥区测试路1号",
            "destination_station": "邵阳大祥S站",
        }

        with (
            patch("query_waybill_detail._run_single_session", return_value=[api_row]),
            patch("query_waybill_detail._overlay_with_browser", return_value={}) as overlay,
        ):
            rows = tms_query_waybill_detail.query_waybill_details(
                bill_codes=["R0001"],
                decrypt_masked=True,
            )

        self.assertEqual("R0001", rows[0]["tracking_number"])
        overlay.assert_called_once_with(
            bill_codes=[],
            headless=True,
            timeout_ms=30_000,
            batch_size=1,
            max_workers=1,
        )

    def test_waybill_tracking_click_decrypt_uses_miniui_component(self):
        class Frame:
            def __init__(self):
                self.evaluated = False

            def evaluate(self, script):
                self.evaluated = True
                return True

        frame = Frame()

        waybill_tracking._click_decrypt(frame)

        self.assertTrue(frame.evaluated)

    def test_arrival_stats_sheet_write_skips_header_when_range_starts_at_row_two(self):
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:B3",
            "clear_range": "Sheet1!A2:B3",
        }
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch("tools.arrival_stats_sync_tool.feishu_operation", return_value={"ok": True}) as feishu_op,
        ):
            result = arrival_stats_sync_tool._write_stats_sheet(
                "phase7.arrive_primary_sheet",
                values,
                {},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["rows"])
        self.assertEqual("Sheet1!A2:B3", feishu_op.call_args_list[0].args[1]["range"])
        self.assertEqual("Sheet1!A1:B1", feishu_op.call_args_list[1].args[1]["range"])
        self.assertEqual([["header-a", "header-b"]], feishu_op.call_args_list[1].args[1]["values"])
        self.assertEqual("Sheet1!A2:B2", feishu_op.call_args_list[2].args[1]["range"])
        self.assertEqual([["row-a", "row-b"]], feishu_op.call_args_list[2].args[1]["values"])

    def test_arrival_stats_sheet_clear_preserves_header_and_extends_to_arrival_count(self):
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "8fc516!A2:S31",
            "clear_range": "8fc516!A1:R200",
            "title_range": "8fc516!A1:R1",
        }
        values = [
            [f"header-{index}" for index in range(19)],
            [f"row-{index}" for index in range(19)],
        ]

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch("tools.arrival_stats_sync_tool.feishu_operation", return_value={"ok": True}) as feishu_op,
        ):
            result = arrival_stats_sync_tool._write_stats_sheet(
                "phase7.arrive_secondary_sheet",
                values,
                {},
            )

        self.assertTrue(result["ok"])
        self.assertEqual("8fc516!A2:S200", feishu_op.call_args_list[0].args[1]["range"])
        self.assertEqual("8fc516!A1:S1", feishu_op.call_args_list[1].args[1]["range"])
        self.assertEqual("8fc516!A2:S2", feishu_op.call_args_list[2].args[1]["range"])

    def test_arrival_stats_sheet_write_keeps_header_when_range_starts_at_row_one(self):
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        self.assertEqual(
            values,
            arrival_stats_sync_tool._values_for_stats_write("Sheet1!A1:B3", values),
        )

    def test_arrival_stats_archive_creates_missing_date_sheet(self):
        resource = {
            "spreadsheet_token": "archive-token",
            "default_write_range": "A1:B20",
        }
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        def _fake_feishu_operation(action, params):
            if action == "add_sheet":
                return {
                    "data": {
                        "replies": [
                            {
                                "addSheet": {
                                    "properties": {
                                        "sheetId": "sheet-new",
                                    }
                                }
                            }
                        ]
                    }
                }
            if action == "write_sheet":
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch("tools.arrival_stats_sync_tool._find_archive_sheet", return_value=None),
            patch("tools.arrival_stats_sync_tool.feishu_operation", side_effect=_fake_feishu_operation) as feishu_op,
        ):
            result = arrival_stats_sync_tool._archive_snapshot(
                values,
                {"archive_title": "2026-05-22"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual("sheet-new", result["sheet_id"])
        self.assertFalse(result["reused_existing_sheet"])
        self.assertEqual(["add_sheet", "write_sheet"], [call.args[0] for call in feishu_op.call_args_list])
        self.assertEqual("sheet-new!A1:B2", feishu_op.call_args_list[1].args[1]["range"])

    def test_arrival_stats_archive_reuses_existing_date_sheet_and_clears_old_rows(self):
        resource = {
            "spreadsheet_token": "archive-token",
            "default_write_range": "Sheet1!A1:B3",
        }
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch(
                "tools.arrival_stats_sync_tool._find_archive_sheet",
                return_value={"sheet_id": "sheet-existing", "title": "2026-05-22", "row_count": 6},
            ),
            patch("tools.arrival_stats_sync_tool.feishu_operation", return_value={"ok": True}) as feishu_op,
        ):
            result = arrival_stats_sync_tool._archive_snapshot(
                values,
                {"archive_title": "2026-05-22"},
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["reused_existing_sheet"])
        self.assertEqual("sheet-existing", result["sheet_ref"])
        self.assertEqual(["write_sheet", "write_sheet"], [call.args[0] for call in feishu_op.call_args_list])
        self.assertEqual("sheet-existing!A1:B6", feishu_op.call_args_list[0].args[1]["range"])
        self.assertEqual(6, len(feishu_op.call_args_list[0].args[1]["values"]))
        self.assertEqual("sheet-existing!A1:B2", feishu_op.call_args_list[1].args[1]["range"])

    def test_arrival_stats_archive_resolves_add_sheet_conflict_by_requerying(self):
        resource = {
            "spreadsheet_token": "archive-token",
            "default_write_range": "A1:B3",
        }
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        def _fake_feishu_operation(action, params):
            if action == "add_sheet":
                return {"error": "sheet title already exists"}
            if action == "write_sheet":
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch(
                "tools.arrival_stats_sync_tool._find_archive_sheet",
                side_effect=[
                    None,
                    {"sheet_id": "sheet-existing", "title": "2026-05-22", "row_count": 4},
                ],
            ) as find_sheet,
            patch("tools.arrival_stats_sync_tool.feishu_operation", side_effect=_fake_feishu_operation) as feishu_op,
        ):
            result = arrival_stats_sync_tool._archive_snapshot(
                values,
                {"archive_title": "2026-05-22"},
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["reused_existing_sheet"])
        self.assertEqual("sheet-existing", result["sheet_id"])
        self.assertEqual(2, find_sheet.call_count)
        self.assertEqual(["add_sheet", "write_sheet", "write_sheet"], [call.args[0] for call in feishu_op.call_args_list])
        self.assertEqual("sheet-existing!A1:B4", feishu_op.call_args_list[1].args[1]["range"])

    def test_arrival_stats_public_result_removes_tokens(self):
        result = arrival_stats_sync_tool._public_result(
            {
                "ok": True,
                "data": {
                    "spreadsheetToken": "sensitive-token",
                    "updatedCells": 10,
                },
                "nested": [{"webhook": "https://example.invalid/hook"}],
            }
        )

        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sensitive-token", dumped)
        self.assertNotIn("spreadsheetToken", dumped)
        self.assertNotIn("example.invalid", dumped)
        self.assertEqual(10, result["data"]["updatedCells"])


if __name__ == "__main__":
    unittest.main()
