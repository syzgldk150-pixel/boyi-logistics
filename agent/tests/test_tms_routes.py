"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403
from agent.api_contracts import EnvelopedRoute
from agent.execution_boundary import (
    EXECUTION_CAPABILITY_HEADER,
    issue_execution_capability,
    revoke_execution_capability,
)
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.scripts import ronghui_waybill_proxy, yunda_waybill_proxy
from console.services.agent_api import AgentApiServiceMixin
from fastapi.responses import JSONResponse


class TMSRoutesTests(unittest.TestCase):
    def setUp(self):
        if hasattr(routes_module, "_ACCOUNT_LIST_CACHE"):
            routes_module._ACCOUNT_LIST_CACHE.clear()
        app = FastAPI()
        app.include_router(router)
        app.include_router(router, prefix="/internal/v1")
        self.client = TestClient(app)

    def test_dispatch_auth_failure_reaches_console_with_code_and_redacted_reason(self):
        """Exercise dispatcher -> versioned route -> real Console client parsing."""
        app = FastAPI()

        @app.middleware("http")
        async def verified_principal(request, call_next):
            request.state.console_principal = {
                "actor_id": "fixture-admin", "roles": ["super_admin"],
            }
            return await call_next(request)

        app.include_router(router, prefix="/internal/v1")
        client = TestClient(app)
        console = AgentApiServiceMixin()
        console.settings = types.SimpleNamespace(
            agent_base_url="http://agent.test", agent_timeout_seconds=10,
            agent_internal_api_token="fixture-internal-token",
        )
        responses = []

        class TransportResponse:
            def __init__(self, response):
                self.status = response.status_code
                self.body = response.content

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.body

        def local_transport(request, **_kwargs):
            response = client.post(request.full_url, content=request.data)
            responses.append(response)
            return TransportResponse(response)

        with patch(
            "agent.tms_runtime.dispatch.resolve_account_params",
            side_effect=TMSAuthStateError("AUTH_REQUIRED", "请重新登录。 token=synthetic-private-value"),
        ), patch(
            "agent.tms_runtime.dispatch._load_callable",
            side_effect=AssertionError("auth failure must not execute a TMS script"),
        ) as load_callable, patch("console.services.agent_api.urlopen", side_effect=local_transport):
            for provider, path in (("yunda", "/ky_inms/public/index.php/elecStock.html"),
                                   ("ronghui", "/module/index")):
                with self.subTest(provider=provider):
                    result = console._agent_request(
                        "POST", f"/internal/v1/tms/{provider}_waybill_proxy",
                        payload={"source": "console", "params": {
                            "method": "GET", "path": path,
                            "proxy_prefix": f"/original/{provider}",
                        }},
                    )
                    self.assertEqual(200, result["status"])
                    self.assertFalse(result["ok"])
                    self.assertEqual("AUTH_REQUIRED", result["error_code"])
                    self.assertEqual("请重新登录。 token=[REDACTED]", result["error"])
                    self.assertEqual({}, result["data"])
                    self.assertNotIn("synthetic-private-value", responses[-1].text)
                    self.assertEqual("AUTH_REQUIRED", responses[-1].json()["error"]["code"])
            load_callable.assert_not_called()

    def test_versioned_route_preserves_standard_envelopes_and_normalizes_flat_failures(self):
        cases = (
            (200, {"ok": True, "data": {"rows": []}, "error": None}),
            (409, {"ok": False, "data": {"run_id": "fixture-run"},
                   "error": {"code": "EXISTING_RUN", "message": "Already running"}}),
            (200, {"ok": False, "error_code": "AUTH_REQUIRED", "message": "Login required"}),
            (503, {"ok": False, "error_code": "SOURCE_UNAVAILABLE", "error": "Unavailable", "data": {}}),
            (200, {"ok": False, "error_code": "AUTH_REQUIRED", "error": "Login required token=synthetic-private-value"}),
        )
        for status, payload in cases:
            with self.subTest(status=status, payload=payload):
                app = FastAPI()
                app.router.route_class = EnvelopedRoute

                @app.get("/internal/v1/fixture")
                def endpoint():
                    return JSONResponse(status_code=status, content=payload, headers={"X-Fixture": "retained"})

                response = TestClient(app).get("/internal/v1/fixture")
                self.assertEqual(status, response.status_code)
                self.assertEqual("retained", response.headers["X-Fixture"])
                if "error_code" not in payload:
                    self.assertEqual(payload, response.json())
                else:
                    self.assertFalse(response.json()["ok"])
                    self.assertEqual(payload["error_code"], response.json()["error"]["code"])
                    expected_message = (payload.get("error") or payload["message"]).replace(
                        "synthetic-private-value", "[REDACTED]",
                    )
                    self.assertEqual(expected_message, response.json()["error"]["message"])
                    self.assertNotIn("synthetic-private-value", response.text)

    def test_legacy_yunda_entry_never_executes_even_if_capability_check_would_allow(self):
        targets = ("yunda_waybill_entry",)
        with patch(
            "agent.tms_runtime.routes.authorize_tms_target",
            return_value=True,
        ), patch(
            "agent.tms_runtime.routes.execute_target",
            side_effect=AssertionError("disabled target must never execute"),
        ):
            for target in targets:
                with self.subTest(target=target):
                    response = self.client.post(
                        f"/internal/v1/tms/{target}",
                        json={"params": {}},
                        headers={EXECUTION_CAPABILITY_HEADER: "would-otherwise-pass"},
                    )
                    self.assertEqual(410, response.status_code)
                    self.assertEqual(
                        "ACTIVE_ORIGINAL_PAGE_DISABLED",
                        response.json()["data"]["error_code"],
                    )

    def test_isolated_original_page_proxy_requires_exact_reviewed_prefix(self):
        self.assertTrue(
            routes_module.authorize_direct_manual_target(
                "yunda_waybill_proxy",
                {
                    "method": "GET",
                    "path": "/ky_inms/public/index.php/business/waybill/entry/indexNew.html",
                    "proxy_prefix": "/original/yunda",
                },
                console_principal_verified=True,
            )
        )
        self.assertTrue(
            routes_module.authorize_direct_manual_target(
                "ronghui_waybill_proxy",
                {"method": "GET", "path": "/module/index", "proxy_prefix": "/original/ronghui"},
                console_principal_verified=True,
            )
        )
        self.assertFalse(
            routes_module.authorize_direct_manual_target(
                "yunda_waybill_proxy",
                {
                    "method": "GET",
                    "path": "/ky_inms/public/index.php/business/waybill/entry/indexNew.html",
                    "proxy_prefix": "/ocr/yunda/live",
                },
                console_principal_verified=True,
            )
        )

        self.assertFalse(
            routes_module.authorize_direct_manual_target(
                "yunda_waybill_proxy",
                {
                    "method": "GET",
                    "path": "/ky_inms/public/../private/admin",
                    "proxy_prefix": "/original/yunda",
                },
                console_principal_verified=True,
            )
        )

    def test_isolated_lookup_route_preserves_real_adapter_transport(self):
        """Use real route authorization and proxy code; replace only network I/O."""
        app = FastAPI()

        @app.middleware("http")
        async def verified_principal(request, call_next):
            request.state.console_principal = {
                "actor_id": "fixture-admin", "roles": ["super_admin"],
            }
            return await call_next(request)

        app.include_router(router, prefix="/internal/v1")
        client = TestClient(app)
        calls = []

        class Session:
            cookies = []

            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return types.SimpleNamespace(
                    status_code=200, content=b'{"fixture_rows":[]}', text='{"fixture_rows":[]}',
                    headers={"Content-Type": "application/json"}, url=url,
                )

        broker = types.SimpleNamespace(build_requests_session=lambda validate: Session())
        modules = {"ronghui_waybill_proxy": ronghui_waybill_proxy, "yunda_waybill_proxy": yunda_waybill_proxy}

        async def execute_proxy(endpoint, req):
            return 200, modules[endpoint].run_once(req.params)

        requests = (
            ("ronghui", "/dataQuery/findAllByCallId", "id=FIND_SYS_DATE", b"", ""),
            ("ronghui", "/dataQuery/findAllByCallId", "id=FIND_TAB_SITE_BUSINESS_TYPE",
             b"SITE_CODE=fixture-site", "application/x-www-form-urlencoded; charset=UTF-8"),
            ("ronghui", "/dataQuery/findAllByCallId", "",
             b'{"id":"FIND_TMS_SYS_SHARE_SET","SHARE_CODE_IN":"fixture-rule"}', "application/json"),
            ("ronghui", "/minic/combobox", "optionCode=WEIGHT_RATIO", b"", ""),
            ("yunda", "/ky_inms/public/index.php/elecStock.html", "", b"", ""),
            ("yunda", "/ky_inms/public/index.php/getCostInfoPrompt.html", "", b"", ""),
            ("yunda", "/ky_inms/public/index.php/business/waybill/entry/getTemplateList.html", "",
             b"CreatedDotCode=fixture-site&IsNew=1&queryType=fixture-type", "application/x-www-form-urlencoded"),
        )
        with patch.object(ronghui_waybill_proxy, "get_session_broker", return_value=broker), patch.object(
            yunda_waybill_proxy, "get_session_broker", return_value=broker,
        ), patch("agent.tms_runtime.routes.execute_target", side_effect=execute_proxy) as dispatched:
            for provider, path, query, body, content_type in requests:
                with self.subTest(provider=provider, path=path, query=query):
                    params = {
                        "method": "POST", "path": path, "query": query,
                        "proxy_prefix": f"/original/{provider}",
                        "headers": {"Content-Type": content_type} if content_type else {},
                        "content_type": content_type,
                    }
                    if body:
                        params["body_base64"] = base64.b64encode(body).decode()
                    response = client.post(
                        f"/internal/v1/tms/{provider}_waybill_proxy",
                        json={"source": "console", "params": params},
                    )
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertTrue(response.json()["data"]["ok"])
                    method, remote_url, transport = calls[-1]
                    self.assertEqual("POST", method)
                    self.assertTrue(remote_url.endswith(path + (f"?{query}" if query else "")))
                    self.assertEqual(body or None, transport["data"])
                    if content_type:
                        self.assertEqual(content_type, transport["headers"]["Content-Type"])
                    self.assertEqual(b'{"fixture_rows":[]}', base64.b64decode(
                        response.json()["data"]["body_base64"],
                    ))
            self.assertEqual(len(requests), dispatched.call_count)
            self.assertEqual(len(requests), len(calls))

    def test_direct_agent_lookup_rejects_bypasses_before_dispatch(self):
        app = FastAPI()
        principal_present = True

        @app.middleware("http")
        async def verified_principal(request, call_next):
            if principal_present:
                request.state.console_principal = {
                    "actor_id": "fixture-admin", "roles": ["super_admin"],
                }
            return await call_next(request)

        app.include_router(router, prefix="/internal/v1")
        client = TestClient(app)
        safe = {
            "method": "POST", "path": "/dataQuery/findAllByCallId", "query": "id=FIND_SYS_DATE",
            "proxy_prefix": "/original/ronghui", "content_type": "application/x-www-form-urlencoded",
        }
        attempts = (
            {**safe, "query": "id=FIND_UNREVIEWED"},
            {**safe, "query": "id=FIND_SYS_DATE&id=DELETE_TABLE"},
            {**safe, "body_base64": base64.b64encode(b"id=DELETE_TABLE").decode()},
            {**safe, "query": "", "body": "id=FIND_SYS_DATE",
             "body_base64": base64.b64encode(b"id=DELETE_TABLE").decode()},
            {**safe, "query": "", "body": "id=FIND_SYS_DATE", "headers": {"Content-Type": "application/json"}},
            {**safe, "query": "", "body": "id=FIND_SYS_DATE", "headers": {
                "Content-Type": "application/x-www-form-urlencoded", "content-type": "application/json",
            }},
            {**safe, "body": "action=delete"},
            {**safe, "path": "/dataQuery/findAllByCallId/extra"},
            {**safe, "path": "/dataQuery/../dataOperation/deleteTables"},
            {**safe, "proxy_prefix": "/ocr/ronghui/live"},
            {**safe, "method": "DELETE"},
        )
        runtime = types.SimpleNamespace(execute_tool=Mock(side_effect=AssertionError("no Command allowed")))
        with patch("agent.tms_runtime.routes._agent_command_runtime", runtime), patch(
            "agent.tms_runtime.routes.execute_target", side_effect=AssertionError("rejected proxy dispatched"),
        ) as dispatched:
            for params in attempts:
                with self.subTest(params=params):
                    response = client.post(
                        "/internal/v1/tms/ronghui_waybill_proxy",
                        json={"source": "console", "params": params, "idempotency_key": "fixture-request"},
                    )
                    self.assertEqual(410, response.status_code, response.text)
                    self.assertEqual("DIRECT_TMS_ENTRY_DISABLED", response.json()["data"]["error_code"])
            principal_present = False
            response = client.post(
                "/internal/v1/tms/ronghui_waybill_proxy",
                json={"source": "console", "params": safe, "idempotency_key": "fixture-request"},
            )
            self.assertEqual(403, response.status_code)
            self.assertEqual("TRUSTED_CONSOLE_ACTOR_REQUIRED", response.json()["data"]["error_code"])
            dispatched.assert_not_called()
            runtime.execute_tool.assert_not_called()
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

    def test_admin_account_name_route_updates_visible_note(self):
        calls = []

        class FakeAccountManager:
            def update_name(self, account_id, name):
                calls.append((account_id, name))
                return {"account_id": account_id, "name": name}

        routes_module._ACCOUNT_LIST_CACHE.update(
            {"payload": {"accounts": [{"account_id": "ronghui_default"}]}, "cached_at": 1}
        )
        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            response = self.client.post(
                "/internal/v1/admin/accounts/ronghui_default/name",
                json={"name": "融辉自提专用账号"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual([("ronghui_default", "融辉自提专用账号")], calls)
        self.assertEqual("融辉自提专用账号", response.json()["data"]["account"]["name"])
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
                        "auto_login_enabled": True,
                        "auto_login_failure_count": 3,
                        "auto_login_failure_limit": 3,
                        "auto_login_blocked": True,
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

    def test_admin_accounts_prefer_cached_force_is_passive_on_cache_miss(self):
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

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            response = self.client.get("/admin/accounts?force=1&prefer_cached=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["cached"])
        self.assertFalse(payload["stale"])
        self.assertFalse(payload["refreshing"])
        self.assertEqual(calls, [(True, False, False)])

    def test_admin_accounts_prefer_cached_force_does_not_refresh_stale_cache(self):
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

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
            first = self.client.get("/admin/accounts?force=1")
            routes_module._ACCOUNT_LIST_CACHE["cached_at"] = 1.0
            second = self.client.get("/admin/accounts?force=1&prefer_cached=1")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        payload = second.json()
        self.assertTrue(payload["cached"])
        self.assertTrue(payload["stale"])
        self.assertFalse(payload["refreshing"])
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

        routes_module._ACCOUNT_LIST_CACHE["cached_at"] = 1.0

        routes_module.update_account_list_cache_status(
            {
                "profile": "default",
                "account_id": "ronghui_default",
                "status": "error",
                "last_error_summary": "缺少登录配置",
                "auto_login_enabled": True,
                "auto_login_failure_count": 0,
                "auto_login_failure_limit": 3,
                "auto_login_blocked": False,
            }
        )

        second = self.client.get("/admin/accounts?prefer_cached=1")

        self.assertEqual(second.status_code, 200)
        payload = second.json()
        self.assertTrue(payload["cached"])
        self.assertEqual(payload["accounts"][0]["status"]["status"], "error")
        self.assertEqual(payload["accounts"][0]["status"]["last_error_summary"], "缺少登录配置")
        self.assertEqual(payload["accounts"][0]["auto_login_failure_count"], 0)
        self.assertFalse(payload["accounts"][0]["auto_login_blocked"])
        self.assertEqual(payload["accounts"][1]["status"]["status"], "authenticated")
        self.assertGreater(routes_module._ACCOUNT_LIST_CACHE["cached_at"], 1.0)

    def test_legacy_credentials_routes_use_account_manager(self):
        calls = []

        class FakeAccountManager:
            def public_credentials(self, account_id):
                calls.append(("public", account_id))
                return {
                    "username": "demo-user",
                    "password": "",
                    "phone": "13800000000",
                    "updated_at": "2026-04-22 12:00:00",
                    "has_saved_credentials": True,
                }

            def save_credentials(self, account_id, *, username, password, phone):
                calls.append(("save", account_id, username, password, phone))
                return {
                    "username": username,
                    "password": "",
                    "phone": phone,
                    "updated_at": "2026-04-22 12:00:00",
                    "has_saved_credentials": True,
                }

            def clear_credentials(self, account_id):
                calls.append(("clear", account_id))
                return {
                    "username": "",
                    "password": "",
                    "phone": "",
                    "updated_at": "",
                    "has_saved_credentials": False,
                }

        with patch("agent.tms_runtime.routes.get_account_manager", return_value=FakeAccountManager()):
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
        self.assertEqual(calls[0], ("public", "ronghui_default"))
        self.assertEqual(calls[1][0:3], ("save", "ronghui_default", "saved-user"))
        self.assertEqual(calls[2], ("clear", "ronghui_default"))

    def test_send_code_route_returns_auth_unavailable_payload(self):
        def _raise():
            from agent.tms_runtime.errors import TMSAuthStateError

            raise TMSAuthStateError("AUTH_UNAVAILABLE", "Playwright Python 依赖未安装。")

        fake_manager = types.SimpleNamespace(login=lambda _account_id: _raise())
        with patch("agent.tms_runtime.routes.get_account_manager", return_value=fake_manager):
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

    def test_price_session_routes_use_price_account(self):
        calls: list[str] = []
        fake_manager = types.SimpleNamespace(
            login=lambda account_id: calls.append(account_id) or {"status": "pending_code", "profile": "price"},
            submit_code=lambda account_id, code: calls.append(account_id) or {
                "status": "authenticated",
                "submitted": code,
                "profile": "price",
            },
        )
        with patch("agent.tms_runtime.routes.get_account_manager", return_value=fake_manager):
            send_response = self.client.post("/admin/tms/price-session/send-code")
            submit_response = self.client.post("/admin/tms/price-session/submit-code", json={"code": "123456"})

        self.assertEqual(send_response.status_code, 200)
        self.assertEqual(submit_response.status_code, 200)
        self.assertEqual(send_response.json()["profile"], "price")
        self.assertEqual(submit_response.json()["submitted"], "123456")
        self.assertEqual(["price_default", "price_default"], calls)

    def test_yunda_session_routes_use_yunda_account(self):
        calls: list[str] = []
        fake_manager = types.SimpleNamespace(
            login=lambda account_id: calls.append(account_id) or {"status": "pending_code", "profile": "yunda"},
            submit_code=lambda account_id, code: calls.append(account_id) or {
                "status": "authenticated",
                "submitted": code,
                "profile": "yunda",
            },
        )
        with patch("agent.tms_runtime.routes.get_account_manager", return_value=fake_manager):
            send_response = self.client.post("/admin/tms/yunda-session/send-code")
            submit_response = self.client.post("/admin/tms/yunda-session/submit-code", json={"code": "123456"})

        self.assertEqual(send_response.status_code, 200)
        self.assertEqual(submit_response.status_code, 200)
        self.assertEqual(send_response.json()["profile"], "yunda")
        self.assertEqual(submit_response.json()["submitted"], "123456")
        self.assertEqual(["yunda_default", "yunda_default"], calls)

    def test_get_price_route_uses_dispatch_layer(self):
        async def fake_execute_target(name, req):
            self.assertEqual(name, "get_price")
            self.assertEqual(req.params["address"], "长沙")
            return 200, {"ok": True, "data": {"目的网点": "测试站"}}

        capability = issue_execution_capability("get_price", ttl_seconds=30)
        try:
            with patch("agent.tms_runtime.routes.execute_target", side_effect=fake_execute_target):
                response = self.client.post(
                    "/tms/get_price",
                    json={"params": {"address": "长沙"}, "timeout_sec": 30},
                    headers={EXECUTION_CAPABILITY_HEADER: capability},
                )
        finally:
            revoke_execution_capability(capability)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["目的网点"], "测试站")

    def test_post_route_returns_auth_required_payload(self):
        async def fake_execute_target(name, req):
            return 200, {"ok": False, "error_code": "AUTH_REQUIRED", "message": "当前未登录或登录态已过期。"}

        capability = issue_execution_capability("sync_scan_codes", ttl_seconds=30)
        try:
            with patch("agent.tms_runtime.routes.execute_target", side_effect=fake_execute_target):
                response = self.client.post(
                    "/tms/scan_next",
                    json={"params": {"items": []}, "timeout_sec": 30},
                    headers={EXECUTION_CAPABILITY_HEADER: capability},
                )
        finally:
            revoke_execution_capability(capability)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "AUTH_REQUIRED")

    def test_tms_route_accepts_legacy_raw_payload(self):
        async def fake_execute_target(name, req):
            self.assertEqual(name, "get_qianshou")
            self.assertEqual(req.params["r13_account_id"], "r13-project-selected")
            self.assertEqual(req.params["page_size"], 1)
            return 200, {"ok": True, "data": []}

        capability = issue_execution_capability("sync_daily_should_sign", ttl_seconds=30)
        try:
            with patch("agent.tms_runtime.routes.execute_target", side_effect=fake_execute_target):
                response = self.client.post(
                    "/tms/get_qianshou",
                    json={
                        "r13_account_id": "r13-project-selected",
                        "page_size": 1,
                    },
                    headers={EXECUTION_CAPABILITY_HEADER: capability},
                )
        finally:
            revoke_execution_capability(capability)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_daily_sign_capability_cannot_invoke_customer_service_write_action(self):
        capability = issue_execution_capability("sync_daily_should_sign", ttl_seconds=30)
        try:
            with patch("agent.tms_runtime.routes.execute_target") as execute_target:
                response = self.client.post(
                    "/tms/customer_service_problem",
                    json={"params": {"action": "reply", "account_id": "one"}},
                    headers={EXECUTION_CAPABILITY_HEADER: capability},
                )
        finally:
            revoke_execution_capability(capability)

        self.assertNotEqual(response.status_code, 200)
        execute_target.assert_not_called()

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

    def test_account_bound_targets_reject_missing_project_account_binding(self):
        async def _run_target(target_name):
            return await dispatch_module.execute_target(
                target_name,
                dispatch_module.TaskRequest(params={}, timeout_sec=30),
            )

        for target_name in (
            "get_qianshou",
            "self_pickup_problem_upload",
            "split_pending_problem_upload",
        ):
            with self.subTest(target_name=target_name):
                status_code, payload = asyncio.run(_run_target(target_name))

                self.assertEqual(200, status_code)
                self.assertFalse(payload["ok"])
                self.assertEqual("AUTH_REQUIRED", payload["error_code"])
                self.assertIn("项目设置显式传入绑定账号", payload["error"])

    def test_get_qianshou_resolves_the_exact_r13_role_without_default_fallback(self):
        captured = {}

        def resolve_role(params, **kwargs):
            captured["role_params"] = dict(params)
            captured["role_kwargs"] = dict(kwargs)
            return {
                **params,
                "username": "selected-r13-user",
                "password": "selected-r13-password",
            }

        def run_target(params):
            captured["target_params"] = dict(params)
            return {
                "account_id": params["r13_account_id"],
                "username": params["username"],
            }

        request = dispatch_module.TaskRequest(
            params={
                "r13_account_id": "r13-non-default",
            },
            timeout_sec=30,
        )
        with (
            patch.object(dispatch_module, "resolve_account_params") as generic_resolve,
            patch.object(
                dispatch_module,
                "resolve_role_account_params",
                side_effect=resolve_role,
            ),
            patch.object(dispatch_module, "_load_callable", return_value=run_target),
        ):
            status_code, payload = asyncio.run(
                dispatch_module.execute_target("get_qianshou", request)
            )

        self.assertEqual(200, status_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("r13-non-default", payload["data"]["account_id"])
        self.assertEqual("selected-r13-user", payload["data"]["username"])
        generic_resolve.assert_not_called()
        self.assertEqual("r13-non-default", captured["role_params"]["r13_account_id"])
        self.assertEqual("r13_account_id", captured["role_kwargs"]["account_field"])
        self.assertEqual("r13_account_id", captured["role_kwargs"]["output_account_field"])
        self.assertEqual("", captured["role_kwargs"]["output_session_profile_field"])
        self.assertEqual("selected-r13-user", captured["target_params"]["username"])

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
