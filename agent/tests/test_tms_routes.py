"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403


class TMSRoutesTests(unittest.TestCase):
    def setUp(self):
        if hasattr(routes_module, "_ACCOUNT_LIST_CACHE"):
            routes_module._ACCOUNT_LIST_CACHE.clear()
        if hasattr(routes_module, "_ACCOUNT_LIST_REFRESHING"):
            routes_module._ACCOUNT_LIST_REFRESHING = False
        app = FastAPI()
        app.include_router(router)
        app.include_router(router, prefix="/internal/v1")
        self.client = TestClient(app)

    def test_versioned_admin_route_uses_standard_envelope(self):
        class FakeAccountManager:
            def list_accounts(self, *, include_status=True, validate=True, force=False):
                return [{"account_id": "ronghui_default", "system": "ronghui"}]

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            response = self.client.get("/internal/v1/admin/accounts?force=1")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"ok": True, "data": {"ok": True, "accounts": [{"account_id": "ronghui_default", "system": "ronghui"}], "cached": False, "stale": False, "refreshing": False, "cache_age_sec": 0}, "error": None},
            response.json(),
        )

    def test_versioned_validation_error_does_not_echo_request_values(self):
        response = self.client.post(
            "/internal/v1/admin/accounts",
            content="not-json-sensitive-value",
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(422, response.status_code)
        payload = response.json()
        self.assertEqual(False, payload["ok"])
        self.assertEqual("validation_error", payload["error"]["code"])
        self.assertNotIn("not-json-sensitive-value", response.text)

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

    def test_admin_account_auto_login_route_persists_checkbox_value(self):
        calls = []

        class FakeAccountManager:
            def set_auto_login(self, account_id, enabled):
                calls.append((account_id, enabled))
                return {
                    "account_id": account_id,
                    "auto_login_enabled": enabled,
                    "auto_login_failure_count": 0,
                    "auto_login_blocked": False,
                }

        routes_module._ACCOUNT_LIST_CACHE.update(
            {"payload": {"accounts": [{"account_id": "ronghui_default"}]}, "cached_at": 1}
        )
        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            response = self.client.post(
                "/internal/v1/admin/accounts/ronghui_default/auto-login",
                json={"enabled": False},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual([("ronghui_default", False)], calls)
        self.assertFalse(response.json()["data"]["account"]["auto_login_enabled"])
        self.assertEqual({}, routes_module._ACCOUNT_LIST_CACHE)

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

    def test_legacy_clear_routes_disable_auto_login_through_account_manager(self):
        calls = []

        class FakeAccountManager:
            def clear_session(self, account_id):
                calls.append(account_id)
                return {
                    "account_id": account_id,
                    "status": "logged_out",
                    "auto_login_enabled": False,
                }

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            default_response = self.client.post("/admin/tms/session/clear")
            price_response = self.client.post("/admin/tms/price-session/clear")
            yunda_response = self.client.post("/admin/tms/yunda-session/clear")

        self.assertEqual(
            ["ronghui_default", "price_default", "yunda_default"],
            calls,
        )
        self.assertFalse(default_response.json()["auto_login_enabled"])
        self.assertFalse(price_response.json()["auto_login_enabled"])
        self.assertFalse(yunda_response.json()["auto_login_enabled"])

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
