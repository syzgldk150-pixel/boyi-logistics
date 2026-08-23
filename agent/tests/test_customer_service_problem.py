import json
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "agent" / "tms_runtime" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import customer_service_problem


class CustomerServiceProblemTargetTests(unittest.TestCase):
    def test_target_is_registered_for_runtime_routing(self):
        dispatch_source = (Path(__file__).resolve().parents[1] / "agent" / "tms_runtime" / "dispatch.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"customer_service_problem"', dispatch_source)
        self.assertIn("agent.tms_runtime.scripts.customer_service_problem", dispatch_source)

    def test_direction_aliases_map_two_business_options_per_platform(self):
        self.assertEqual(
            "received",
            customer_service_problem._normalize_direction("published_to_me", platform="ronghui"),
        )
        self.assertEqual(
            "query",
            customer_service_problem._normalize_direction("published_to_me", platform="yunda"),
        )
        self.assertEqual(
            "registered",
            customer_service_problem._normalize_direction("my_published", platform="ronghui"),
        )
        self.assertEqual(
            "published",
            customer_service_problem._normalize_direction("my_published", platform="yunda"),
        )

    def test_ronghui_default_session_uses_managed_price_account_profile(self):
        self.assertEqual(
            "price_default",
            customer_service_problem._session_profile("ronghui", {}),
        )
        self.assertEqual(
            "ronghui_custom_profile",
            customer_service_problem._session_profile(
                "ronghui",
                {"session_profile": "ronghui_custom_profile"},
            ),
        )

    def test_dispatch_resolves_explicit_customer_service_account_id(self):
        from agent.tms_runtime import dispatch as dispatch_module

        observed = {}

        def fake_resolve(params, *, default_system="", default_purpose=""):
            observed["params"] = dict(params)
            observed["default_system"] = default_system
            observed["default_purpose"] = default_purpose
            resolved = dict(params)
            resolved["session_profile"] = "ronghui_custom_profile"
            return resolved

        def fake_run(params):
            return {
                "ok": True,
                "account_id": params["account_id"],
                "session_profile": params["session_profile"],
            }

        with patch.object(dispatch_module, "resolve_account_params", side_effect=fake_resolve), patch.object(
            dispatch_module, "_load_callable", return_value=fake_run
        ):
            status, payload = asyncio.run(
                dispatch_module.execute_target(
                    "customer_service_problem",
                    dispatch_module.TaskRequest(
                        params={
                            "platform": "ronghui",
                            "account_id": "ronghui-a",
                            "action": "query",
                        },
                        timeout_sec=5,
                    ),
                )
            )

        self.assertEqual(200, status)
        self.assertEqual("ronghui-a", observed["params"]["account_id"])
        self.assertEqual("", observed["default_system"])
        self.assertEqual("", observed["default_purpose"])
        self.assertEqual("ronghui_custom_profile", payload["data"]["session_profile"])

    def test_ronghui_rows_use_guid_as_only_external_id(self):
        rows = customer_service_problem.normalize_problem_rows(
            "ronghui",
            [
                {
                    "GUID": "RH-GUID-1",
                    "BILL_CODE": "2606000040",
                    "PROBLEM_CAUSE": "外包装破损",
                    "REVERSION": "处理中",
                }
            ],
            account_id="ronghui-a",
            account_label="融辉 A",
            source_direction="received",
        )

        self.assertEqual("RH-GUID-1", rows[0]["external_id"])
        self.assertEqual("2606000040", rows[0]["waybill_no"])
        self.assertEqual("ronghui", rows[0]["platform"])
        self.assertEqual("ronghui-a", rows[0]["account_id"])
        self.assertEqual("received", rows[0]["source_direction"])

    def test_ronghui_row_with_reply_content_is_not_displayed_as_unreplied(self):
        rows = customer_service_problem.normalize_problem_rows(
            "ronghui",
            [
                {
                    "GUID": "RH-GUID-2",
                    "BILL_CODE": "R00016785191",
                    "PROBLEM_CAUSE": "请及时派送",
                    "REVERSION_STATUS": "未回复",
                    "REVERSION": "知悉",
                }
            ],
            account_id="ronghui-a",
            account_label="融辉 A",
            source_direction="received",
        )

        self.assertEqual("已回复", rows[0]["status"])
        self.assertEqual("未回复", rows[0]["raw"]["REVERSION_STATUS"])

    def test_fetch_attachment_returns_base64_image_from_allowed_origin(self):
        class Response:
            status_code = 200
            url = "https://kyproblem.yunda56.com/ky_problem/public/static/problem/image/a.png"
            headers = {"Content-Type": "image/png"}
            content = b"\x89PNG\r\n\x1a\npayload"
            text = ""

            def raise_for_status(self):
                return None

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, headers=None, timeout=0):
                self.calls.append((url, headers or {}, timeout))
                return Response()

        session = Session()
        result = customer_service_problem._fetch_problem_attachment(
            session,
            {
                "platform": "yunda",
                "payload": {"source_url": "/ky_problem/public/static/problem/image/a.png"},
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual("image/png", result["content_type"])
        self.assertEqual("iVBORw0KGgpwYXlsb2Fk", result["body_base64"])
        self.assertEqual("https://kyproblem.yunda56.com/ky_problem/public/static/problem/image/a.png", session.calls[0][0])

    def test_fetch_yunda_download_attachment_sniffs_octet_stream_jpeg(self):
        class Response:
            status_code = 200
            url = "https://kyproblem.yunda56.com/ky_problem/public/index.php/base/downloadOutImg.html?url=redacted"
            headers = {"Content-Type": "application/octet-stream"}
            content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01payload"
            text = "JFIF payload"

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, headers=None, timeout=0):
                return Response()

        result = customer_service_problem._fetch_problem_attachment(
            Session(),
            {
                "platform": "yunda",
                "payload": {
                    "source_url": "/ky_problem/public/index.php/base/downloadOutImg.html?url=redacted",
                },
            },
        )

        self.assertEqual("image/jpeg", result["content_type"])

    def test_fetch_yunda_app_root_download_attachment_uses_public_index_root(self):
        class Response:
            status_code = 200
            url = "https://kyproblem.yunda56.com/ky_problem/public/index.php/base/downloadOutImg.html?url=redacted"
            headers = {"Content-Type": "application/octet-stream"}
            content = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01payload"
            text = "JFIF payload"

            def raise_for_status(self):
                return None

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, headers=None, timeout=0):
                self.calls.append((url, headers or {}, timeout))
                return Response()

        session = Session()
        customer_service_problem._fetch_problem_attachment(
            session,
            {
                "platform": "yunda",
                "payload": {"source_url": "/base/downloadOutImg.html?url=redacted"},
            },
        )

        self.assertEqual(
            "https://kyproblem.yunda56.com/ky_problem/public/index.php/base/downloadOutImg.html?url=redacted",
            session.calls[0][0],
        )

    def test_ronghui_detail_fetches_picture_scan_save_pos_by_guid(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [
                    {
                        "SAVE_POS": "/unauth/download/group1/M00/C2/43/demo.jpg",
                        "FILE_NAME": "demo.jpg",
                        "PIC_TYPE": 3,
                    }
                ]

        class Session:
            def __init__(self):
                self.post_calls = []

            def post(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                return Response()

        session = Session()
        page_context = {
            "menu_text": "received",
            "url": "https://tms.ronghuiwl.com/widget/home?authenticationKey=auth&pageId=page",
            "authentication_key": "auth",
            "page_id": "page",
        }

        with patch.object(customer_service_problem, "_resolve_ronghui_page_context", return_value=page_context):
            result = customer_service_problem._ronghui_detail(
                session,
                {
                    "item": {
                        "external_id": "RH-GUID-1",
                        "source_direction": "received",
                        "raw": {
                            "GUID": "RH-GUID-1",
                            "FILE_PATH": "demo.jpg",
                            "PIC_NUM": 1,
                        },
                    }
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(customer_service_problem.RONGHUI_FIND_ALL_URL, session.post_calls[0][0])
        self.assertEqual("FIND_PIC_SCAN_BY_BILL_CODE", session.post_calls[0][1]["params"]["id"])
        self.assertEqual("RH-GUID-1", session.post_calls[0][1]["data"]["OUT_GUID"])
        self.assertEqual("3", session.post_calls[0][1]["data"]["PIC_TYPE"])
        attachments = result["details"][1]["attachments"]
        self.assertEqual("/unauth/download/group1/M00/C2/43/demo.jpg", attachments[0]["path"])
        self.assertEqual("demo.jpg", attachments[0]["filename"])
        self.assertIs(True, attachments[0]["is_image"])

    def test_fetch_attachment_rejects_cross_origin_url(self):
        with self.assertRaises(customer_service_problem.CustomerServiceProblemError) as ctx:
            customer_service_problem._normalize_attachment_source_url(
                "yunda",
                "https://example.com/a.png",
            )

        self.assertEqual("INVALID_ATTACHMENT_URL", ctx.exception.code)

    def test_ronghui_rows_fail_without_guid_instead_of_guessing(self):
        with self.assertRaises(customer_service_problem.CustomerServiceProblemError) as ctx:
            customer_service_problem.normalize_problem_rows(
                "ronghui",
                [{"BILL_CODE": "2606000040"}],
                account_id="ronghui-a",
                account_label="融辉 A",
                source_direction="received",
            )

        self.assertEqual("MISSING_EXTERNAL_ID", ctx.exception.code)

    def test_yunda_rows_use_prob_main_id_as_only_external_id(self):
        rows = customer_service_problem.normalize_problem_rows(
            "yunda",
            [
                {
                    "prob_main_id": "YD-1001",
                    "ship_no": "980000001",
                    "prob_status": "处理中",
                    "prob_text": "客户催件",
                }
            ],
            account_id="yunda-a",
            account_label="韵达 A",
            source_direction="query",
        )

        self.assertEqual("YD-1001", rows[0]["external_id"])
        self.assertEqual("980000001", rows[0]["waybill_no"])
        self.assertEqual("yunda", rows[0]["platform"])

    def test_build_yunda_query_payload_keeps_captured_keys(self):
        payload = customer_service_problem.build_yunda_query_payload(
            {
                "direction": "query",
                "issuer_site": "2",
                "time": "created_time",
                "page": 2,
                "rows": 50,
                "is_replay": "0",
            }
        )

        for key in (
            "bl_attachment_status",
            "check_status",
            "damage_degree",
            "damage_link",
            "damage_type",
            "end_date",
            "end_time",
            "is_replay",
            "issuer_site",
            "page",
            "prob_status",
            "problem_type",
            "problem_type_classes",
            "reply_by",
            "rows",
            "scan_source",
            "source",
            "start_date",
            "start_time",
            "sum_site",
            "sum_type",
            "time",
            "udf012",
            "udf015",
            "udf016",
        ):
            self.assertIn(key, payload)
        self.assertEqual(2, payload["page"])
        self.assertEqual(50, payload["rows"])
        self.assertEqual("0", payload["is_replay"])

    def test_build_yunda_query_payload_maps_console_date_range_to_origin_fields(self):
        payload = customer_service_problem.build_yunda_query_payload(
            {
                "date_from": "2026-06-18",
                "date_to": "2026-06-18",
            }
        )

        self.assertEqual("2026-06-18", payload["start_date"])
        self.assertEqual("00:00:00", payload["start_time"])
        self.assertEqual("2026-06-18", payload["end_date"])
        self.assertEqual("23:59:59", payload["end_time"])

    def test_build_yunda_issue_payload_maps_console_date_range_to_origin_fields(self):
        payload = customer_service_problem.build_yunda_issue_list_payload(
            {
                "date_from": "2026-06-17",
                "date_to": "2026-06-19",
            }
        )

        self.assertEqual("2026-06-17", payload["start_date"])
        self.assertEqual("00:00:00", payload["start_time"])
        self.assertEqual("2026-06-19", payload["end_date"])
        self.assertEqual("23:59:59", payload["end_time"])

    def test_build_yunda_issue_payload_uses_original_page_date_window(self):
        with patch.object(
            customer_service_problem,
            "_yunda_issue_today",
            return_value=customer_service_problem.dt.date(2026, 8, 23),
        ):
            payload = customer_service_problem.build_yunda_issue_list_payload({})

        self.assertEqual("2026-08-21", payload["start_date"])
        self.assertEqual("00:00:00", payload["start_time"])
        self.assertEqual("2026-08-23", payload["end_date"])
        self.assertEqual("23:59:59", payload["end_time"])

    def test_build_yunda_issue_payload_rejects_half_date_range(self):
        with self.assertRaisesRegex(
            customer_service_problem.CustomerServiceProblemError,
            "同时提供开始和结束日期",
        ):
            customer_service_problem.build_yunda_issue_list_payload(
                {"date_from": "2026-08-21"}
            )

    def test_yunda_post_json_omits_empty_strings_but_keeps_zero_values(self):
        captured = {}

        class Response:
            status_code = 200
            text = '{"total": 0, "rows": [], "footer": []}'
            url = customer_service_problem.YUNDA_QUERY_LIST_URL
            headers = {"content-type": "application/json; charset=utf-8"}

            def raise_for_status(self):
                return None

            def json(self):
                return {"total": 0, "rows": [], "footer": []}

        class Session:
            def post(self, url, data=None, headers=None, timeout=None):
                captured["data"] = dict(data or {})
                return Response()

        customer_service_problem._yunda_post_json(
            Session(),
            customer_service_problem.YUNDA_QUERY_LIST_URL,
            {
                "start_date": "2026-06-18",
                "start_time": "00:00:00",
                "end_date": "2026-06-18",
                "end_time": "23:59:59",
                "time": "",
                "issuer_site": "",
                "is_replay": "0",
                "page": 1,
                "rows": 100,
            },
            referer=f"{customer_service_problem.YUNDA_PUBLIC_ROOT}/query/index.html",
            label="查询列表",
        )

        self.assertNotIn("time", captured["data"])
        self.assertNotIn("issuer_site", captured["data"])
        self.assertEqual("0", captured["data"]["is_replay"])
        self.assertEqual(1, captured["data"]["page"])

        customer_service_problem._yunda_post_json(
            Session(),
            customer_service_problem.YUNDA_ISSUE_LIST_URL,
            {"start_date": "2026-08-21", "problem_type": "", "page": 1},
            referer=f"{customer_service_problem.YUNDA_PUBLIC_ROOT}/issue/index.html",
            label="发布列表",
            preserve_empty=True,
        )
        self.assertIn("problem_type", captured["data"])
        self.assertEqual("", captured["data"]["problem_type"])

    def test_build_ronghui_query_payload_uses_date_range_json_value(self):
        payload = customer_service_problem.build_ronghui_query_payload(
            {
                "date_from": "2026-06-18",
                "date_to": "2026-06-18",
            },
            direction="received",
            login_site_code="7390004",
        )

        date_value = json.loads(payload["REGISTER_DATE"])
        self.assertEqual("2026/06/18 00:00:00", date_value["start"])
        self.assertEqual("2026/06/18 23:59:59", date_value["end"])
        self.assertEqual(payload["REGISTER_DATE"], payload["SEARCH_DATE_RANGE"])

    def test_build_ronghui_query_payload_converts_console_page_to_miniui_page_index(self):
        first_page = customer_service_problem.build_ronghui_query_payload(
            {"page": 1, "rows": 80},
            direction="received",
            login_site_code="7390004",
        )
        second_page = customer_service_problem.build_ronghui_query_payload(
            {"page": 2, "rows": 80},
            direction="received",
            login_site_code="7390004",
        )
        explicit_page_index = customer_service_problem.build_ronghui_query_payload(
            {"page": 1, "pageIndex": 3, "rows": 80},
            direction="received",
            login_site_code="7390004",
        )

        self.assertEqual(0, first_page["pageIndex"])
        self.assertEqual(1, second_page["pageIndex"])
        self.assertEqual(3, explicit_page_index["pageIndex"])

    def test_build_ronghui_save_tables_envelope_uses_operation_key(self):
        envelope = customer_service_problem.build_ronghui_save_tables_envelope(
            "TAB_PROBLEM_UPT",
            {"GUID": "RH-GUID-1", "REVERSION": "已联系客户"},
        )

        self.assertEqual("TAB_PROBLEM_UPT", envelope[0]["operationKey"])
        self.assertEqual("RH-GUID-1", envelope[0]["data"][0]["GUID"])
        self.assertEqual([], envelope[0]["idFields"])

    def test_ronghui_reply_preserves_source_row_and_submits_explicit_status(self):
        captured = {}

        def fake_save(session, page_context, envelope):
            captured["envelope"] = envelope
            return {"success": True, "message": "ok"}

        with patch.object(
            customer_service_problem,
            "_resolve_ronghui_page_context",
            return_value={"menu_text": "收到问题件查询", "authentication_key": "auth", "page_id": "page", "url": "u"},
        ), patch.object(customer_service_problem, "_save_ronghui_tables", side_effect=fake_save):
            result = customer_service_problem._ronghui_reply(
                object(),
                {
                    "item": {
                        "external_id": "RH-GUID-1",
                        "waybill_no": "R0001",
                        "status": "未回复",
                        "raw": {
                            "GUID": "RH-GUID-1",
                            "BILL_CODE": "R0001",
                            "REGISTER_SITE": "登记网点",
                            "SEND_SITE": "被通知网点",
                        },
                    },
                    "payload": {"reply_text": "已联系处理", "prob_status": "已处理"},
                },
            )

        row = captured["envelope"][0]["data"][0]
        self.assertTrue(result["ok"])
        self.assertEqual("TAB_PROBLEM_UPT", captured["envelope"][0]["operationKey"])
        self.assertEqual("RH-GUID-1", row["GUID"])
        self.assertEqual("R0001", row["BILL_CODE"])
        self.assertEqual("登记网点", row["REGISTER_SITE"])
        self.assertEqual("被通知网点", row["SEND_SITE"])
        self.assertEqual("已联系处理", row["REVERSION"])
        self.assertEqual("已处理", row["REVERSION_STATUS"])

    def test_ronghui_reply_surfaces_source_failure(self):
        with patch.object(
            customer_service_problem,
            "_resolve_ronghui_page_context",
            return_value={"menu_text": "收到问题件查询", "authentication_key": "auth", "page_id": "page", "url": "u"},
        ), patch.object(
            customer_service_problem,
            "_save_ronghui_tables",
            return_value={"success": False, "message": "保存失败"},
        ):
            with self.assertRaises(customer_service_problem.CustomerServiceProblemError) as ctx:
                customer_service_problem._ronghui_reply(
                    object(),
                    {
                        "item": {"external_id": "RH-GUID-1", "waybill_no": "R0001"},
                        "payload": {"reply_text": "已联系处理", "prob_status": "已处理"},
                    },
                )

        self.assertEqual("SOURCE_REPLY_FAILED", ctx.exception.code)
        self.assertIn("保存失败", str(ctx.exception))

    def test_yunda_reply_surfaces_source_failure(self):
        with patch.object(
            customer_service_problem,
            "_yunda_post_json",
            return_value={"success": False, "message": "回复失败"},
        ):
            with self.assertRaises(customer_service_problem.CustomerServiceProblemError) as ctx:
                customer_service_problem._yunda_reply(
                    object(),
                    {
                        "item": {"external_id": "YD-1", "status": "未处理"},
                        "payload": {"reply_text": "已联系处理", "prob_status": "已处理"},
                    },
                )

        self.assertEqual("SOURCE_REPLY_FAILED", ctx.exception.code)
        self.assertIn("回复失败", str(ctx.exception))

    def test_resolve_ronghui_page_context_uses_post_menu_tree_and_children_wrapper(self):
        menu_payload = {
            "children": [
                {
                    "text": "问题件管理",
                    "children": [
                        {
                            "text": "收到问题件查询",
                            "url": "/widget/home?authenticationKey=auth-token&pageId=page-token",
                        }
                    ],
                }
            ]
        }

        class Response:
            def __init__(self, payload=None, text=""):
                self._payload = payload
                self.text = text

            def raise_for_status(self):
                return None

            def json(self):
                if self._payload is None:
                    raise ValueError("not json")
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return Response(menu_payload)

            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                return Response(None, "<html></html>")

        session = Session()

        context = customer_service_problem._resolve_ronghui_page_context(session, "收到问题件查询")

        self.assertEqual("auth-token", context["authentication_key"])
        self.assertEqual("page-token", context["page_id"])
        self.assertEqual("POST", session.calls[0][0])
        self.assertEqual(customer_service_problem.RONGHUI_MENU_URL, session.calls[0][1])
        self.assertEqual("XMLHttpRequest", session.calls[0][2]["headers"]["X-Requested-With"])

    def test_ronghui_menu_nodes_decodes_result_data_string(self):
        payload = {
            "success": True,
            "result": {
                "data": json.dumps(
                    [
                        {
                            "text": "问题件管理",
                            "children": [
                                {
                                    "text": "收到问题件查询",
                                    "url": "/widget/home?authenticationKey=auth&pageId=page",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                )
            },
        }

        paths = [path for _node, path in customer_service_problem._walk_menu(customer_service_problem._menu_nodes(payload))]

        self.assertIn("问题件管理/收到问题件查询", paths)

    def test_select_ronghui_grid_url_prefers_real_problem_datagrid(self):
        html = """
        <div id="siteLookup" class="mini-datagrid" url="/dataQuery/findPageByCallId?id=LOOKUP"></div>
        <div id="datagrid" class="mini-datagrid" url="/dataQuery/findPageByCallId?id=PROBLEM"></div>
        """

        url = customer_service_problem._select_ronghui_grid_url(
            {"html": html, "menu_text": "收到问题件查询"}
        )

        self.assertEqual("https://tms.ronghuiwl.com/dataQuery/findPageByCallId?id=PROBLEM", url)

    def test_select_ronghui_grid_url_still_fails_for_ambiguous_problem_grids(self):
        html = """
        <div class="mini-datagrid" url="/dataQuery/findPageByCallId?id=ONE"></div>
        <div class="mini-datagrid" url="/dataQuery/findPageByCallId?id=TWO"></div>
        """

        with self.assertRaises(customer_service_problem.CustomerServiceProblemError) as ctx:
            customer_service_problem._select_ronghui_grid_url(
                {"html": html, "menu_text": "收到问题件查询"}
            )

        self.assertEqual("AMBIGUOUS_GRID_URL", ctx.exception.code)

    def test_ronghui_query_surfaces_source_success_false(self):
        class Response:
            def __init__(self, payload):
                self._payload = payload
                self.text = json.dumps(payload, ensure_ascii=False)

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.post_calls = []

            def post(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                return Response({"success": False, "message": "json数据格式错误\r\n"})

        session = Session()
        page_context = {
            "menu_text": "收到问题件查询",
            "url": "https://tms.ronghuiwl.com/widget/home?authenticationKey=auth&pageId=page",
            "html": '<div id="datagrid" class="mini-datagrid" url="/dataQuery/findPageByCallId?id=PROBLEM"></div>',
            "authentication_key": "auth",
            "page_id": "page",
        }

        with patch.object(customer_service_problem, "_resolve_ronghui_page_context", return_value=page_context), patch.object(
            customer_service_problem, "_resolve_ronghui_login_site_code", return_value="7390004"
        ):
            with self.assertRaises(customer_service_problem.CustomerServiceProblemError) as ctx:
                customer_service_problem._ronghui_query(
                    session,
                    {
                        "account_id": "ronghui-a",
                        "account_label": "ronghui-a",
                        "filters": {"direction": "received"},
                    },
                )

        self.assertEqual("SOURCE_QUERY_FAILED", ctx.exception.code)
        self.assertIn("json", str(ctx.exception))

    def test_run_once_yunda_query_detects_auth_required(self):
        class Response:
            status_code = 302
            text = "<html>login</html>"
            url = "https://ky-sso.yunda56.com/login"
            headers = {"content-type": "text/html", "location": "https://ky-sso.yunda56.com/login"}

            def raise_for_status(self):
                return None

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        broker = types.SimpleNamespace(build_requests_session=lambda validate=True: Session())

        with patch.object(customer_service_problem, "get_session_broker", return_value=broker):
            result = customer_service_problem.run_once(
                {
                    "platform": "yunda",
                    "action": "query",
                    "account_id": "yunda-a",
                    "account_label": "韵达 A",
                    "session_profile": "yunda",
                    "filters": {"direction": "query"},
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual("AUTH_REQUIRED", result["error_code"])
        self.assertNotIn("ky-sso.yunda56.com/login", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
