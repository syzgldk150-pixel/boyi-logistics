"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403


class TmsWaybillRuntimeTests(unittest.TestCase):
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
