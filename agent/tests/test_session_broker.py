"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403


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

    def test_auto_login_ronghui_falls_back_to_manual_after_three_failed_attempts(self):
        session = _CaptchaPostSession([
            _DummyResponse(status_code=200, text="validateCode system/login", headers={"Content-Type": "text/html"})
            for _ in range(session_broker_module.MAX_AUTO_CAPTCHA_ATTEMPTS)
        ])
        fetch_side_effect = [
            (f"captcha-{idx}".encode("utf-8"), f"data:image/png;base64,Y2FwdGNoYS0{idx}", "image/png")
            for idx in range(1, session_broker_module.MAX_AUTO_CAPTCHA_ATTEMPTS + 1)
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
        self.assertIn(str(session_broker_module.MAX_AUTO_CAPTCHA_ATTEMPTS), result["last_error_summary"])
        self.assertTrue(result["auto_login_attempts_exhausted"])
        self.assertTrue(self.broker._pending_storage_state_path.exists())
        self.assertTrue(self.broker._pending_login_state_path.exists())
        self.assertEqual(session_broker_module.MAX_AUTO_CAPTCHA_ATTEMPTS, len(session.post_calls))

    def test_price_profile_send_code_reuses_auto_image_login_flow(self):
        price_broker = self._configure_broker_state(
            SessionBroker(
                profile_name="price",
                username_envs=(),
                password_envs=(),
                phone_envs=(),
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

    def test_yunda_image_captcha_auto_login_falls_back_after_three_failures(self):
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
        self.assertIn(str(session_broker_module.MAX_AUTO_CAPTCHA_ATTEMPTS), result["last_error_summary"])
        self.assertTrue(result["auto_login_attempts_exhausted"])
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

    def test_managed_ronghui_profiles_use_saved_credentials_and_separate_state_dirs(self):
        session_broker_module._SESSION_BROKERS.clear()
        try:
            price_broker = get_session_broker("price")
            custom_broker = get_session_broker("ronghui_ops_01")

            self.assertEqual(price_broker.profile_name, "price")
            self.assertEqual(price_broker._username_envs, ())
            self.assertEqual(price_broker._password_envs, ())
            self.assertEqual(custom_broker._username_envs, ())
            self.assertNotEqual(price_broker._state_dir, custom_broker._state_dir)
            self.assertIn("price", str(price_broker._state_dir))
        finally:
            session_broker_module._SESSION_BROKERS.clear()

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
                custom_yunda_broker = get_session_broker("yunda_ops_01")
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
            self.assertEqual(custom_yunda_broker._login_mode, "yunda_password")
            self.assertEqual(custom_yunda_broker._username_envs, ())
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
            manual_credentials = yunda_broker.get_manual_credentials()
            config = yunda_broker.resolve_login_config()
            status = yunda_broker.describe_status(validate=False)

        self.assertTrue(credentials["has_saved_credentials"])
        self.assertFalse(credentials["has_manual_credentials"])
        self.assertTrue(credentials["has_env_credentials"])
        self.assertEqual(credentials["credential_source"], "env")
        self.assertEqual(credentials["username"], "")
        self.assertEqual(credentials["password"], "")
        self.assertFalse(manual_credentials["has_saved_credentials"])
        self.assertFalse(manual_credentials["has_manual_credentials"])
        self.assertFalse(manual_credentials["has_env_credentials"])
        self.assertEqual(manual_credentials["credential_source"], "")
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
        broker = SessionBroker(profile_name="yunda", login_mode="yunda_password", require_phone=False)
        self._configure_broker_state(broker, "yunda-submit-code")
        broker._pending_storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}),
            encoding="utf-8",
        )
        broker._pending_login_state_path.write_text(
            json.dumps({"challenge_type": "sms", "login_url": "https://ky-sso.yunda56.com/public/sms/sms_valid"}),
            encoding="utf-8",
        )
        broker._save_meta(
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
            patch.object(broker, "resolve_login_config", return_value=self.login_config),
            patch.object(broker, "_run_in_isolated_thread", side_effect=lambda func: func()),
            patch.object(broker, "_submit_yunda_sms_login", return_value=expected) as submit_sms,
            patch.object(broker, "_submit_yunda_captcha_login") as submit_captcha,
        ):
            result = broker.submit_code("123456")

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
