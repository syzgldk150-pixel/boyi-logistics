"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403


class ToolPriceAndRoutingTests(unittest.TestCase):
    def setUp(self):
        self.internal_token_patch = patch.dict(
            os.environ,
            {
                "AGENT_INTERNAL_API_TOKEN": "test-internal-token",
                "AGENT_EXECUTION_CAPABILITY": "test-execution-capability",
            },
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

    def test_fetch_dispatch_uses_selected_profile_and_session_user_context(self):
        class Cookie:
            name = "userInfo"
            value = json.dumps(
                {
                    "loginUserName": "operator",
                    "loginUserAccount": "account",
                    "loginSiteName": "site",
                    "loginSiteCode": "real-site-code",
                }
            )

        session = Mock(cookies=[Cookie()])
        auth = Mock()
        auth.login_and_get_session.return_value = session
        with (
            patch("fetch_dispatch.TMSAuth", return_value=auth) as auth_type,
            patch("fetch_dispatch.collect_dispatch_records", return_value=[]) as collect,
        ):
            result = fetch_dispatch.run_once({"session_profile": "selected-profile"})

        self.assertEqual([], result)
        auth_type.assert_called_once_with(profile="selected-profile")
        self.assertEqual("real-site-code", collect.call_args.kwargs["login_site_code"])

    def test_fetch_dispatch_rejects_missing_or_conflicting_session_identity(self):
        with self.assertRaisesRegex(
            fetch_dispatch.TMSAuthStateError,
            "explicit account session profile",
        ):
            fetch_dispatch.run_once({})

        class Cookie:
            name = "userInfo"
            value = json.dumps(
                {
                    "loginUserName": "operator",
                    "loginUserAccount": "account",
                    "loginSiteName": "site",
                    "loginSiteCode": "real-site-code",
                }
            )

        session = Mock(cookies=[Cookie()])
        auth = Mock()
        auth.login_and_get_session.return_value = session
        with (
            patch("fetch_dispatch.TMSAuth", return_value=auth),
            self.assertRaisesRegex(fetch_dispatch.TMSAuthStateError, "does not match"),
        ):
            fetch_dispatch.run_once(
                {
                    "session_profile": "selected-profile",
                    "login_site_code": "wrong-site-code",
                }
            )

    def test_default_http_service_urls_point_to_agent_tms(self):
        self.assertEqual(tms_tool.HTTP_SERVICE_URL, "http://127.0.0.1:9000/tms")
        self.assertEqual(query_tool.HTTP_SERVICE_URL, "http://127.0.0.1:9000/tms")
        self.assertEqual(price_tool.HTTP_SERVICE_URL, "http://127.0.0.1:9000/tms")

    def test_price_tool_defaults_to_managed_price_account(self):
        with patch.object(
            price_tool,
            "get_combined_price",
            return_value={"ok": True},
        ) as get_combined_price:
            result = price_tool.run_price_tool(
                {
                    "address": "浙江省台州市椒江区",
                    "weight": 2000,
                }
            )

        self.assertEqual({"ok": True}, result)
        self.assertEqual("price_default", get_combined_price.call_args.kwargs["account_id"])

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

    def test_dispatch_rebuilds_stale_script_auth_module_before_execution(self):
        from agent.tms_runtime import dispatch

        target_module_name = dispatch.TARGETS["fetch_dispatch"].module
        auth_module_name = "agent.tms_runtime.scripts.login_manager"
        target_module = importlib.import_module(target_module_name)
        original_auth = target_module.TMSAuth
        original_auth_module = sys.modules[auth_module_name]

        class WrongAuth:
            pass

        stale_auth_module = types.ModuleType(auth_module_name)
        stale_auth_module.TMSAuth = WrongAuth
        stale_auth_module.__file__ = str(
            Path(price_tool.PRICE_GET_MODULE).with_name("login_manager.py")
        )
        sys.modules[auth_module_name] = stale_auth_module
        target_module.TMSAuth = WrongAuth
        try:
            fn = dispatch._load_callable(dispatch.TARGETS["fetch_dispatch"])
            loaded_module = sys.modules[fn.__module__]
            auth_module = sys.modules[loaded_module.TMSAuth.__module__]

            self.assertIsNot(WrongAuth, loaded_module.TMSAuth)
            self.assertTrue(Path(auth_module.__file__).resolve().is_relative_to(SCRIPTS_DIR))
        finally:
            sys.modules[auth_module_name] = original_auth_module
            restored_target_module = sys.modules.get(target_module_name)
            if restored_target_module is not None:
                restored_target_module.TMSAuth = original_auth

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
            def get_storage_state_path(self, validate=True):
                calls.append(f"path_validate={validate}")
                return "/tmp/test-storage-state.json"

        with patch("browser_manager.get_session_broker", return_value=Broker()) as get_broker:
            auth.login(page, username="", password="")

        get_broker.assert_called_once_with("price")
        self.assertEqual(["path_validate=False"], calls)

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

            def __init__(self, *args, **kwargs):
                pass

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

            def __init__(self, *args, **kwargs):
                pass

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
        self.assertNotIn("X-Agent-Internal-Token", captured["headers"])
        self.assertEqual(
            "test-execution-capability",
            captured["headers"]["X-Agent-Execution-Capability"],
        )
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
            result = get_qianshou.run_once(
                {
                    "r13_account_id": "r13-bound",
                    "accountKey": "r13",
                }
            )

        self.assertEqual([], result)
        self.assertEqual("r13", captured["account_key"])
        self.assertEqual("r13-bound", captured["account_id"])
        self.assertNotIn("disp_site_code", captured)

    def test_get_qianshou_rejects_missing_account_or_site_override(self):
        with self.assertRaisesRegex(RuntimeError, "account identity"):
            get_qianshou.run_once({})
        with self.assertRaisesRegex(RuntimeError, "account identity"):
            get_qianshou.run_once(
                {
                    "account_id": "generic-account-is-not-an-r13-role",
                }
            )
        with self.assertRaisesRegex(RuntimeError, "selected account session"):
            get_qianshou.run_once(
                {
                    "r13_account_id": "r13-bound",
                    "disp_site_code": "caller-site",
                }
            )

    def test_get_qianshou_uses_actual_sign_time_or_sign_site_as_sign_evidence(self):
        self.assertTrue(get_qianshou._has_confirmed_sign_signal({"signTime": "2026-08-11 16:54:54"}))
        self.assertTrue(get_qianshou._has_confirmed_sign_signal({"signSiteName": "长垣魏庄站"}))
        self.assertFalse(
            get_qianshou._has_confirmed_sign_signal(
                {"dispTime": "2026-08-11 16:54:54", "signTime": "", "signSiteName": ""}
            )
        )

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
        # R7 implementations remain archived in the registry, but current
        # automation, scheduler and Feishu entrypoints do not expose them.
        self.assertIn("r7_arrival_checkin", registry_text)
        self.assertIn("r7_departure_checkin", registry_text)
        self.assertIn("sql_only", registry_text)
        self.assertIn("sync_sql", registry_text)

    def test_feishu_fixed_command_registry_is_complete_and_read_only(self):
        registrations = direct_tool_router.FEISHU_COMMAND_REGISTRATIONS
        self.assertEqual(
            {
                "sync_scan_codes": "builtin.scan_codes",
                "sync_arrive_list": "builtin.arrive_list",
                "sync_daily_send_orders": "builtin.send_order",
                "sync_yunda_send_waybills": "builtin.yunda_send_waybills",
                "sync_yunda_dispatch_forecast": "builtin.yunda_dispatch_forecast",
                "sync_arrival_stats": "builtin.arrival_stats",
                "preview_self_pickup_problems": "builtin.self_pickup_problem_upload",
                "self_pickup_problem_upload": "builtin.self_pickup_problem_upload",
                "preview_split_pending_problems": "builtin.split_pending_problem_upload",
                "split_pending_problem_upload": "builtin.split_pending_problem_upload",
            },
            dict(direct_tool_router.FIRST_PARTY_FEISHU_ROUTE_KEYS),
        )
        self.assertEqual(8, len(registrations))
        with self.assertRaises(TypeError):
            direct_tool_router.FIRST_PARTY_FEISHU_ROUTE_KEYS["unexpected"] = "route"

    def test_feishu_fixed_command_registry_rejects_duplicate_identity(self):
        registration = direct_tool_router.FEISHU_COMMAND_REGISTRATIONS[0]
        duplicate_id = direct_tool_router.FeishuCommandRegistration(
            registration.command_id,
            "builtin.other",
            ("other_tool",),
        )
        duplicate_route = direct_tool_router.FeishuCommandRegistration(
            "other_command",
            registration.route_key,
            ("other_tool",),
        )

        with self.assertRaisesRegex(RuntimeError, "ids.*unique"):
            direct_tool_router._validate_feishu_command_registrations(
                (registration, duplicate_id)
            )
        with self.assertRaisesRegex(RuntimeError, "routes.*unique"):
            direct_tool_router._validate_feishu_command_registrations(
                (registration, duplicate_route)
            )

    def test_reserved_feishu_command_uses_the_fixed_router_contract(self):
        for text in (
            "同步到货清单",
            "报价 湖南省长沙市,1kg",
            "分批问题件",
            "韵达网点派件量预测主单表",
            "登录",
            "重新登录",
            "发送验证码",
            "韵达验证码登录",
            "取消扫描",
            "取消到货统计",
            "确认扫描",
            "绑定审批 23456789AB",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    direct_tool_router.is_reserved_feishu_command_text(text)
                )

        self.assertFalse(
            direct_tool_router.is_reserved_feishu_command_text("插件自定义只读日报")
        )
        self.assertFalse(direct_tool_router.is_reserved_feishu_command_text("1"))
        self.assertFalse(direct_tool_router.is_reserved_feishu_command_text("2"))

    def test_direct_router_does_not_expose_removed_r7_commands(self):
        for text in ("到达打卡", "R7 到达打卡", "发车", "R7 发车打卡"):
            with self.subTest(text=text):
                self.assertIsNone(direct_tool_router.direct_tool_request_from_text(text))
                self.assertFalse(direct_tool_router.is_reserved_feishu_command_text(text))

    def test_direct_router_maps_arrive_list_command_to_sync_tool(self):
        for text in ("执行一次arrivelist脚本", "同步到货清单", "拉取预到达清单", "arrive-list"):
            with self.subTest(text=text):
                request = direct_tool_router.direct_tool_request_from_text(text)

                self.assertIsNotNone(request)
                self.assertEqual("sync_arrive_list", request["tool_name"])
                self.assertEqual({}, request["params"])
                self.assertEqual("automation_project", request["mode"])

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
                self.assertEqual("preview_self_pickup_problems", request["tool_name"])
                self.assertEqual({}, request["params"])
                self.assertEqual("automation_preview", request["mode"])
                self.assertEqual(
                    {"dry_run": False},
                    request["confirm_intent"]["dynamic_inputs"],
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
        self.assertEqual({}, request["params"])
        self.assertEqual({}, request["dynamic_inputs"])
        self.assertEqual("automation_project", request["mode"])

    def test_direct_router_maps_yunda_send_waybills_command(self):
        request = direct_tool_router.direct_tool_request_from_text("韵达寄件运单管理")

        self.assertIsNotNone(request)
        self.assertEqual("sync_yunda_send_waybills", request["tool_name"])
        self.assertEqual({}, request["params"])
        self.assertEqual({}, request["dynamic_inputs"])
        self.assertEqual("automation_project", request["mode"])

        range_request = direct_tool_router.direct_tool_request_from_text(
            "韵达寄件运单同步从2026年5月6日到2026年5月16日"
        )
        self.assertIsNotNone(range_request)
        self.assertEqual("sync_yunda_send_waybills", range_request["tool_name"])
        self.assertEqual({}, range_request["params"])
        self.assertEqual(
            {"start_date": "2026-05-06", "end_date": "2026-05-16"},
            range_request["dynamic_inputs"],
        )

    def test_direct_router_maps_send_order_range_command(self):
        request = direct_tool_router.direct_tool_request_from_text(
            "获取当日寄件数据从2026年5月6日到2026年5月16日"
        )

        self.assertIsNotNone(request)
        self.assertEqual("sync_daily_send_orders", request["tool_name"])
        self.assertEqual({}, request["params"])
        self.assertEqual(
            {"start_date": "2026-05-06", "end_date": "2026-05-16"},
            request["dynamic_inputs"],
        )
        self.assertEqual("automation_project", request["mode"])

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

    def test_direct_router_keeps_departure_checkin_command_removed(self):
        for text in ("发车", "R7发车", "发车打卡"):
            with self.subTest(text=text):
                self.assertIsNone(direct_tool_router.direct_tool_request_from_text(text))

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
                self.assertEqual("automation_project", request["mode"])
