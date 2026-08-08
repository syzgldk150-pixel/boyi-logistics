import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "agent" / "tms_runtime" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tracking_query  # noqa: E402
from agent.tms_runtime.scripts import query_waybill_detail  # noqa: E402
from agent.tms_runtime.scripts import ronghui_tms_tracking  # noqa: E402


class RonghuiTmsTrackingTests(unittest.TestCase):
    def test_resolves_tracking_menu_from_real_tms_menu_shape(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "result": [
                        {
                            "TEXT": "客服管理",
                            "CHILDREN": [
                                {
                                    "TEXT": "快件跟踪",
                                    "URL": "/widget/home?authenticationKey=test&pageId=track",
                                }
                            ],
                        }
                    ],
                    "success": True,
                }

        class Session:
            def get(self, url, timeout=None):
                return Response()

        self.assertEqual(
            "https://tms.ronghuiwl.com/widget/home?authenticationKey=test&pageId=track",
            ronghui_tms_tracking._resolve_widget_menu_url(Session()),
        )

    def test_tracking_query_uses_tms_adapter_for_ronghui_numbers(self):
        calls = []

        def fake_tms(params):
            calls.append(params)
            return {
                "ok": True,
                "type": "ronghui_tms",
                "tracking_number": params["tracking_number"],
                "route_rows": [{"scan_time": "2026-06-22 11:30:27", "description": "from tms"}],
                "child_detail_rows": [],
            }

        with patch.object(tracking_query.ronghui_tms_tracking, "run_once", side_effect=fake_tms):
            result = tracking_query.query_tracking({"tracking_number": "2001561671"})

        self.assertEqual("ronghui_tms", result["type"])
        self.assertEqual("2001561671", result["tracking_number"])
        self.assertEqual("from tms", result["route_rows"][0]["description"])
        self.assertEqual("2001561671", calls[0]["tracking_number"])

    def test_normalizes_tms_scan_and_child_rows(self):
        route = ronghui_tms_tracking.normalize_route_row(
            {
                "scan_station": "邵阳操作场",
                "scan_time": "2026-06-22 11:30:27",
                "upload_time": "2026-06-22 11:32:23",
                "transport_method": "汽运",
                "description": "快件在[邵阳操作场]已装车",
                "contact": "邵阳操作场:0739-5455259",
                "remark": "",
                "scan_type": "发件",
                "scan_user": "TMS",
                "source": "子单发件产生",
            }
        )
        child = ronghui_tms_tracking.normalize_child_detail_row(
            {
                "bill": "20015616710001",
                "site": "邵阳操作场",
                "type": "发件",
                "date": "2026-06-22 11:30:27",
                "desc": "快件在[邵阳操作场]已装车",
            }
        )

        self.assertEqual("邵阳操作场", route["scan_station"])
        self.assertEqual("2026-06-22 11:32:23", route["upload_time"])
        self.assertEqual("子单发件产生", route["source"])
        self.assertEqual("20015616710001", child["child_waybill_no"])
        self.assertEqual("发件", child["scan_type"])
        self.assertEqual("快件在[邵阳操作场]已装车", child["description"])

    def test_normalizes_real_tms_api_rows(self):
        route = ronghui_tms_tracking.normalize_route_row(
            {
                "BILL_CODE": "2001561671",
                "SCAN_DATE": "2026-06-22 11:30:27",
                "REGISTER_DATE": "2026-06-22 11:32:23",
                "SCAN_SITE": "邵阳操作场",
                "SCAN_TYPE": "发件",
                "SCAN_MAN": "TMS",
                "PRE_OR_NEXT_STATION": "邵阳大祥S站",
                "SCAN_SITE_PHONE": "0739-5455259",
                "PRE_OR_NEXT_STATION_PHONE": "07395186128",
                "CLASS": "汽运",
                "DATA_FROM": "子单发件产生",
            }
        )
        child_rows = ronghui_tms_tracking._latest_child_scan_rows(
            [
                {"BILL_CODE": "2001561671", "SCAN_DATE": "2026-06-22 10:00:00"},
                {"BILL_CODE": "20015616710001", "SCAN_DATE": "2026-06-22 10:00:00", "SCAN_SITE": "长沙分拨"},
                {"BILL_CODE": "20015616710001", "SCAN_DATE": "2026-06-22 11:30:27", "SCAN_SITE": "邵阳操作场"},
            ],
            "2001561671",
        )
        detail = ronghui_tms_tracking._normalize_api_detail_row(
            {
                "BILL_CODE": "2001561671",
                "SEND_SITE": "临海大田站",
                "DESTINATION": "邵阳大祥S站",
                "DISPATCH_MODE_TEXT": "派送",
                "SEND_MAN": "发货人",
                "SEND_MAN_PHONE": "13800000000",
                "ACCEPT_MAN": "收货人",
                "ACCEPT_MAN_PHONE": "13900000000",
                "ACCEPT_MAN_ADDRESS": "湖南省邵阳市测试地址",
                "GOODS_NAME": "配件",
                "GOODS_COUNT": 3,
                "SETTLEMENT_WEIGHT": 1131,
                "FREIGHT": 0,
            },
            "2001561671",
        )

        self.assertEqual("邵阳操作场", route["scan_station"])
        self.assertEqual("2026-06-22 11:32:23", route["upload_time"])
        self.assertEqual("汽运", route["transport_method"])
        self.assertIn("正发往【邵阳大祥S站】", route["description"])
        self.assertEqual(1, len(child_rows))
        self.assertEqual("邵阳操作场", child_rows[0]["SCAN_SITE"])
        self.assertEqual("邵阳大祥S站", detail["destination_station"])
        self.assertEqual("派送", detail["delivery_method"])
        self.assertEqual("3", detail["quantity"])
        self.assertEqual("1131", detail["actual_weight"])

    def test_normalizes_english_tms_delivery_method(self):
        detail = ronghui_tms_tracking._normalize_api_detail_row(
            {
                "BILL_CODE": "R00017164845",
                "DESTINATION": "邵阳大祥S站",
                "DISPATCH_MODE": "Doorstep Delivery",
            },
            "R00017164845",
        )

        self.assertEqual("派送", detail["delivery_method"])

    def test_collect_api_tracking_rows_uses_matching_scan_rows_when_main_rows_empty(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Session:
            def post(self, url, data=None, headers=None, timeout=None):
                if "FIND_SACN_TRACK_BY_CODE_MAIN" in url:
                    return Response({"rows": []})
                if "FIND_SACN_TRACK_BY_CODE" in url:
                    return Response(
                        {
                            "rows": [
                                {
                                    "BILL_CODE": "R00017265400",
                                    "SCAN_DATE": "2026-06-26 16:57:47",
                                    "SCAN_SITE": "康仙庄一部",
                                    "SCAN_TYPE": "收件",
                                },
                                {
                                    "BILL_CODE": "R000172654000001",
                                    "SCAN_DATE": "2026-06-27 09:02:54",
                                    "SCAN_SITE": "康仙庄一部",
                                    "SCAN_TYPE": "发件",
                                },
                            ]
                        }
                    )
                return Response({"rows": [{"BILL_CODE": "R00017265400", "GOODS_COUNT": 12}]})

        route_rows, child_rows, detail = ronghui_tms_tracking._collect_api_tracking_rows(
            Session(),
            "R00017265400",
        )

        self.assertEqual(1, len(route_rows))
        self.assertEqual("R00017265400", route_rows[0]["BILL_CODE"])
        self.assertEqual(1, len(child_rows))
        self.assertEqual("R000172654000001", child_rows[0]["BILL_CODE"])
        self.assertEqual("R00017265400", detail["tracking_number"])

    def test_tracking_query_sends_widget_auth_headers_to_scan_api(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Session:
            def post(self, url, data=None, headers=None, timeout=None):
                headers = headers or {}
                has_page_context = (
                    headers.get("authenticationKey") == "auth-1"
                    and headers.get("pageId") == "page-1"
                    and headers.get("Referer", "").endswith("authenticationKey=auth-1&pageId=page-1")
                )
                if "FIND_SACN_TRACK_BY_CODE" in url:
                    if not has_page_context:
                        return Response({"success": False, "message": "非法的请求\r\n"})
                    return Response(
                        {
                            "rows": [
                                {
                                    "BILL_CODE": "R00017265400",
                                    "SCAN_DATE": "2026-06-26 16:57:47",
                                    "SCAN_SITE": "康仙庄一部",
                                    "SCAN_TYPE": "收件",
                                }
                            ]
                        }
                    )
                return Response({"rows": [{"BILL_CODE": "R00017265400", "GOODS_COUNT": 12}]})

        class FakeAuth:
            def __init__(self, profile="default"):
                self.profile = profile

            def login_and_get_session(self):
                return Session()

        with (
            patch.object(ronghui_tms_tracking, "TMSAuth", FakeAuth),
            patch.object(
                ronghui_tms_tracking,
                "_resolve_widget_menu_url",
                return_value="https://tms.ronghuiwl.com/widget/home?authenticationKey=auth-1&pageId=page-1",
            ),
        ):
            result = ronghui_tms_tracking.query_ronghui_tms_tracking(
                {"tracking_number": "R00017265400", "decrypt_masked": False}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["counts"]["route_rows"])

    def test_normalizes_self_collection_tms_delivery_method(self):
        detail = ronghui_tms_tracking._normalize_api_detail_row(
            {
                "BILL_CODE": "R00017224308",
                "DESTINATION": "邵阳自提部",
                "DISPATCH_MODE": "Self-collection",
            },
            "R00017224308",
        )

        self.assertEqual("自提", detail["delivery_method"])

    def test_ignores_download_path_as_tms_route_description(self):
        route = ronghui_tms_tracking.normalize_route_row(
            {
                "BILL_CODE": "2003441560",
                "SCAN_DATE": "2026-06-26 14:58:56",
                "SCAN_SITE": "淮北濉溪S站",
                "SCAN_TYPE": "派件",
                "SCAN_DESC": "/unauth/download/group1/M00/36/8A/wKgAQGo-IzGAI0BeAARmyOjHxfQ875.jpg",
            }
        )

        self.assertEqual("快件在【淮北濉溪S站】进行派件扫描;", route["description"])
        self.assertNotIn("/unauth/download", route["description"])

    def test_builds_result_from_tms_route_detail_and_child_rows(self):
        result = ronghui_tms_tracking.build_tracking_result(
            tracking_number="2001561671",
            route_rows=[
                {
                    "scan_station": "邵阳操作场",
                    "scan_time": "2026-06-22 11:30:27",
                    "description": "快件在[邵阳操作场]已装车",
                }
            ],
            detail_row={
                "tracking_number": "2001561671",
                "recipient_name": "刘家侯",
                "destination_station": "邵阳大祥S站",
                "goods_name": "壁扇750型*40",
                "quantity": 20,
                "actual_weight": "230",
                "shipping_fee": "300.00",
            },
            child_rows=[
                {
                    "bill": "20015616710001",
                    "site": "邵阳操作场",
                    "type": "发件",
                    "date": "2026-06-22 11:30:27",
                    "desc": "快件在[邵阳操作场]已装车",
                }
            ],
        )

        self.assertEqual("ronghui_tms", result["type"])
        self.assertEqual("邵阳大祥S站", result["waybill_stub"]["disp_site"])
        self.assertEqual("壁扇750型*40", result["waybill_stub"]["goods_name"])
        self.assertEqual("20件", result["waybill_stub"]["pieces"])
        self.assertEqual("300.00", result["waybill_stub"]["shipping_fee"])
        self.assertEqual("20015616710001", result["child_detail_rows"][0]["child_waybill_no"])
        self.assertEqual(1, result["counts"]["route_rows"])
        self.assertEqual(1, result["counts"]["child_detail_rows"])

    def test_builds_live_arrival_progress_from_100_unique_child_unloads(self):
        tracking_number = "R00018097100"
        child_rows = [
            {
                "BILL_CODE": f"{tracking_number}{index:04d}",
                "SCAN_SITE": "邵阳操作场",
                "SCAN_TYPE": "卸车",
                "SCAN_DATE": f"2026-07-16 12:{index % 60:02d}:00",
            }
            for index in range(1, 101)
        ]
        child_rows.append(dict(child_rows[0]))

        result = ronghui_tms_tracking.build_tracking_result(
            tracking_number=tracking_number,
            route_rows=[
                {
                    "SCAN_SITE": "邵阳操作场",
                    "SCAN_TYPE": "卸车",
                    "SCAN_DATE": "2026-07-16 12:59:10",
                },
                {
                    "SCAN_SITE": "宜春高安市站",
                    "SCAN_TYPE": "收件",
                    "SCAN_DATE": "2026-07-14 17:10:08",
                },
            ],
            detail_row={"tracking_number": tracking_number, "quantity": "100"},
            child_rows=child_rows,
        )

        self.assertEqual(100, result["arrival_progress"]["arrived_quantity"])
        self.assertEqual(100, result["arrival_progress"]["expected_quantity"])
        self.assertEqual(0, result["arrival_progress"]["pending_quantity"])
        self.assertEqual("邵阳操作场", result["arrival_progress"]["arrival_station"])
        self.assertEqual("ronghui_tms_child_distribution", result["arrival_progress"]["source"])

    def test_live_arrival_progress_requires_exact_child_scope_station_and_arrival_type(self):
        tracking_number = "R00018097100"
        result = ronghui_tms_tracking.build_tracking_result(
            tracking_number=tracking_number,
            route_rows=[
                {
                    "scan_station": "邵阳操作场",
                    "scan_type": "卸车",
                    "scan_time": "2026-07-16 12:59:10",
                }
            ],
            detail_row={"tracking_number": tracking_number, "quantity": "6"},
            child_rows=[
                {
                    "bill": f"{tracking_number}0001",
                    "site": "邵阳操作场",
                    "type": "卸车",
                    "date": "2026-07-16 12:58:01",
                },
                {
                    "bill": f"{tracking_number}0002",
                    "site": "邵阳操作场",
                    "type": "装车",
                    "date": "2026-07-16 12:58:02",
                },
                {
                    "bill": f"{tracking_number}0003",
                    "site": "长沙分拨",
                    "type": "卸车",
                    "date": "2026-07-16 12:58:03",
                },
                {
                    "bill": f"{tracking_number}001",
                    "site": "邵阳操作场",
                    "type": "卸车",
                    "date": "2026-07-16 12:58:04",
                },
                {
                    "bill": "R000180972220001",
                    "site": "邵阳操作场",
                    "type": "卸车",
                    "date": "2026-07-16 12:58:05",
                },
                {
                    "bill": tracking_number,
                    "site": "邵阳操作场",
                    "type": "卸车",
                    "date": "2026-07-16 12:58:06",
                },
            ],
        )

        self.assertEqual(1, result["arrival_progress"]["arrived_quantity"])
        self.assertEqual(5, result["arrival_progress"]["pending_quantity"])

    def test_live_arrival_progress_keeps_explicit_zero_from_valid_child_distribution(self):
        tracking_number = "R00018097100"
        result = ronghui_tms_tracking.build_tracking_result(
            tracking_number=tracking_number,
            route_rows=[
                {
                    "scan_station": "邵阳操作场",
                    "scan_type": "卸车",
                    "scan_time": "2026-07-16 12:59:10",
                }
            ],
            detail_row={"tracking_number": tracking_number, "quantity": "1"},
            child_rows=[
                {
                    "bill": f"{tracking_number}0001",
                    "site": "邵阳操作场",
                    "type": "装车",
                    "date": "2026-07-16 12:58:01",
                }
            ],
        )

        self.assertEqual(0, result["arrival_progress"]["arrived_quantity"])
        self.assertEqual(1, result["arrival_progress"]["pending_quantity"])
        self.assertEqual("pending", result["arrival_progress"]["arrival_status"])

    def test_live_arrival_progress_is_unavailable_when_latest_main_route_is_not_arrival(self):
        tracking_number = "R00018097100"
        result = ronghui_tms_tracking.build_tracking_result(
            tracking_number=tracking_number,
            route_rows=[
                {
                    "scan_station": "邵阳操作场",
                    "scan_type": "发件",
                    "scan_time": "2026-07-16 13:00:00",
                }
            ],
            detail_row={"tracking_number": tracking_number, "quantity": "1"},
            child_rows=[
                {
                    "bill": f"{tracking_number}0001",
                    "site": "邵阳操作场",
                    "type": "卸车",
                    "date": "2026-07-16 12:58:01",
                },
            ],
        )

        self.assertNotIn("arrival_progress", result)

    def test_tracking_query_overlays_decrypted_ronghui_party_fields(self):
        class FakeAuth:
            def __init__(self, profile="default"):
                self.profile = profile

            def login_and_get_session(self):
                return object()

        route_rows = [
            {
                "SCAN_SITE": "邵阳操作场",
                "SCAN_DATE": "2026-06-22 11:30:27",
                "SCAN_TYPE": "发件",
            }
        ]
        masked_detail = {
            "tracking_number": "R0000000001",
            "sender_name": "张*",
            "sender_phone": "138****0000",
            "recipient_name": "李*",
            "recipient_phone": "139****0000",
            "recipient_address": "湖南省邵阳市测试地址",
        }
        decrypted_detail = {
            "tracking_number": "R0000000001",
            "sender_name": "完整发货人",
            "sender_phone": "13800000000",
            "recipient_name": "完整收货人",
            "recipient_phone": "13900000000",
        }

        with (
            patch.object(ronghui_tms_tracking, "TMSAuth", FakeAuth),
            patch.object(ronghui_tms_tracking, "_resolve_widget_menu_url", return_value="https://tms.ronghuiwl.com/widget/home"),
            patch.object(
                ronghui_tms_tracking,
                "_collect_api_tracking_rows",
                return_value=(route_rows, [], masked_detail),
            ),
            patch.object(query_waybill_detail, "query_waybill_details", return_value=[decrypted_detail]),
        ):
            result = tracking_query.query_tracking({"tracking_number": "R0000000001", "decrypt_masked": True})

        stub = result["waybill_stub"]
        self.assertEqual("完整发货人", stub["sender_name"])
        self.assertEqual("13800000000", stub["sender_phone"])
        self.assertEqual("完整收货人", stub["recipient_name"])
        self.assertEqual("13900000000", stub["recipient_phone"])


if __name__ == "__main__":
    unittest.main()
