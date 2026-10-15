"""Synthetic source contracts for read-only Yunda monitoring."""

import json
import unittest
from unittest.mock import Mock

import requests

from agent.tms_runtime import monitoring_yunda as source


class Response:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def session():
    result = Mock()
    result.cookies = requests.cookies.RequestsCookieJar()
    result.cookies.set("ydky_rabbit_web_cip_sessionid", "synthetic-session", domain="ky-export.yunda56.com", path="/")
    return result


def message_row(**kwargs):
    return {
        "msgId": "synthetic-message", "msgTitle": "测试通知", "msgContent": "<p>请核实虚构运单</p>",
        "createdTime": "2026-10-14 10:00:00", "readStatus": "未读", "handleStatus": "0",
        "logisticsId": "SYNTHETIC-WAYBILL", "typeName": "测试类型", **kwargs,
    }


class YundaSourceTests(unittest.TestCase):
    def test_notifications_use_exact_username_and_string_status(self):
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user", "identity": "wrong-id"}})
        current.post.return_value = Response({"errorCode": 0, "rows": [message_row()], "total": 21})

        result = source.collect_items(current, "yunda:notifications", 1)

        self.assertEqual("ok", result["status"])
        self.assertEqual(21, result["total"])
        self.assertTrue(result["has_more"])
        self.assertEqual("请核实虚构运单", result["items"][0]["content"])
        self.assertEqual("待处理", result["items"][0]["status_label"])
        call = current.post.call_args
        self.assertTrue(call.args[0].endswith("/getAdviseReadByStatus.do"))
        condition = json.loads(call.kwargs["data"]["search_condition"])
        self.assertEqual({"user": "synthetic-user", "handleStatus": "0", "showModel": "1"}, condition)
        self.assertEqual({"actionId": "kyimport_message_notice_info_urlSearchAdviseData", "ssid": "null"},
                         call.kwargs["params"])
        prepared_url = requests.Request("POST", call.args[0], params=call.kwargs["params"]).prepare().url
        self.assertNotIn("synthetic-session", prepared_url)
        self.assertEqual(15, call.kwargs["timeout"])
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertNotIn("synthetic-session", str(result))
        self.assertNotIn("raw", result["items"][0])

    def test_client_push_messages_show_readable_text(self):
        push = {"data": {"msgid": "", "businesstype": "problemShipment", "type": "",
                         "text": {"title": "问题件", "content": "您有一条虚构问题件", "orderId": ""}}}
        receipt = {"data": {"msgId": "", "text": {"title": "消息提醒", "content": "虚构回单提醒"}, "type": "0"}}
        rows = [
            message_row(msgId="push", msgContent=json.dumps(push, ensure_ascii=False)),
            message_row(msgId="receipt", msgContent=json.dumps(receipt, ensure_ascii=False)),
            message_row(msgId="unknown", msgContent='{"other": 1}'),
            message_row(msgId="untitled", msgTitle="", msgContent="", typeName="虚构问题件消息"),
        ]
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        current.post.return_value = Response({"errorCode": 0, "rows": rows, "total": 4})

        result = source.collect_items(current, "yunda:notifications", 1)

        items = {item["id"]: item for item in result["items"]}
        self.assertEqual("您有一条虚构问题件", items["push"]["content"])
        self.assertEqual("虚构回单提醒", items["receipt"]["content"])
        # Unrecognised shapes stay visible as received instead of being guessed.
        self.assertEqual('{"other": 1}', items["unknown"]["content"])
        self.assertEqual("虚构问题件消息", items["untitled"]["title"])
        self.assertEqual("", items["untitled"]["content"])
        self.assertEqual(source.NOTIFICATION_SCOPE_NOTE, result["message"])

    def test_announcements_are_unread_and_do_not_mark_read(self):
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        current.post.return_value = Response({"errorCode": 0, "rows": [message_row()], "total": 1})

        result = source.collect_items(current, "yunda:announcements", 1)

        self.assertEqual("未读", result["items"][0]["status_label"])
        self.assertEqual(1, current.post.call_count)
        call = current.post.call_args
        self.assertTrue(call.args[0].endswith("/getNoticeReadByStatus.do"))
        self.assertEqual("0", json.loads(call.kwargs["data"]["search_condition"])["readStatus"])

    def test_user_id_is_not_guessed_from_identity(self):
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"identity": "synthetic-id"}})
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual("error", result["status"])
        self.assertIsNone(result["total"])
        self.assertIn("details.username", result["message"])
        current.post.assert_not_called()

    def test_business_error_is_not_empty_list(self):
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        current.post.return_value = Response({"errorCode": 1, "rows": [], "total": 0, "msg": "synthetic-session"})
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual("error", result["status"])
        self.assertIsNone(result["total"])
        self.assertNotIn("synthetic-session", str(result))

    def test_login_html_and_redirect_are_auth_required(self):
        for response in (Response(status_code=302), Response(ValueError(), text='<form action="/login">用户登录</form>')):
            with self.subTest(response=response):
                current = session()
                current.get.return_value = response
                result = source.collect_items(current, "yunda:notifications", 1)
                self.assertEqual("auth_required", result["status"])
                self.assertIsNone(result["total"])

    def test_timeout_does_not_expose_request_url(self):
        current = session()
        current.get.side_effect = requests.Timeout("URL?ssid=synthetic-session")
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual("error", result["status"])
        self.assertIn("超时", result["message"])
        self.assertNotIn("synthetic-session", str(result))

    def test_missing_total_and_duplicate_rows_fail_explicitly(self):
        for payload in (
            {"errorCode": 0, "rows": []},
            {"errorCode": 0, "rows": [message_row(), message_row()], "total": 2},
            {"errorCode": 0, "rows": [], "total": "unknown"},
        ):
            with self.subTest(payload=payload):
                current = session()
                current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
                current.post.return_value = Response(payload)
                result = source.collect_items(current, "yunda:notifications", 1)
                self.assertEqual("error", result["status"])
                self.assertEqual([], result["items"])

    def test_full_categories_use_down_and_keep_different_count_scopes(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 200, "details": {"username": "synthetic-user"}}),
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 3, "rows": []}),
            Response({"total": 0, "rows": []}),
        ]
        current.post.side_effect = [
            Response({"errorCode": 0, "num": 99, "rows": [{
                "name": "问题件", "typeCode": "001", "sumValue": 5,
                "newClientResourceId": "4768", "noticeSystemName": "问题件查询",
                "chidList": [{"typeName": "问题件", "typeCode": "001", "sumValue": 5}],
            }]}),
            Response({"errorCode": 0, "rows": [], "total": 7}),
            Response({"errorCode": 0, "rows": [], "total": 11}),
        ]

        categories = source.collect_categories(current)

        self.assertEqual("down", current.post.call_args_list[0].kwargs["data"]["sourceFlag"])
        self.assertEqual({"actionId": "kyimport_message_notice_info_getTypes", "ssid": "null"},
                         current.post.call_args_list[0].kwargs["params"])
        self.assertNotIn("synthetic-session", str(current.post.call_args_list))
        self.assertEqual([5, 7, 11, 3, 0], [item["count"] for item in categories])
        self.assertTrue(all(not item["include_in_total"] for item in categories))
        self.assertEqual("pending_notifications", categories[1]["count_scope"])
        self.assertFalse(categories[0]["items_supported"])

    def test_one_source_failure_does_not_hide_other_sources(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 200, "details": {"username": "synthetic-user"}}),
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 2, "rows": []}),
            Response({"total": 0, "rows": []}),
        ]
        current.post.side_effect = [
            Response({"errorCode": 7}),
            Response({"errorCode": 0, "rows": [], "total": 0}),
            Response({"errorCode": 0, "rows": [], "total": 0}),
        ]
        categories = source.collect_categories(current)
        self.assertEqual("error", categories[0]["status"])
        self.assertIsNone(categories[0]["count"])
        self.assertEqual(2, categories[3]["count"])

    def test_work_order_page_uses_default_pending_list_and_read_only_details(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 25, "rows": [{
                "complaintId": "synthetic-order", "listId": "SYNTHETIC-WAYBILL",
                "compTime": "2026-10-14 12:00:00", "compTypeName": "测试工单",
                "compStatusName": "已响应待首回", "dealOverTime": "2026-10-15 12:00:00",
            }]}),
            Response({"complaintId": "synthetic-order", "compContent": "<p>请处理虚构记录</p>"}),
        ]
        result = source.collect_items(current, "yunda:work_orders", 2)
        self.assertEqual("ok", result["status"])
        self.assertEqual({"offset": 20, "limit": 20}, current.get.call_args_list[1].kwargs["params"])
        self.assertEqual(8, current.get.call_args_list[2].kwargs["timeout"])
        self.assertEqual("请处理虚构记录", result["items"][0]["content"])
        self.assertEqual("已响应待首回", result["items"][0]["status_label"])
        self.assertEqual("2026-10-15 12:00:00", result["items"][0]["deadline"])
        current.post.assert_not_called()

    def test_reply_field_changes_are_not_guessed(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 1, "rows": [{"unexpected": "synthetic"}]}),
        ]
        result = source.collect_items(current, "yunda:work_order_replies", 1)
        self.assertEqual("error", result["status"])
        self.assertEqual(1, result["total"])
        self.assertIn("缺少", result["message"])
        self.assertEqual([], result["items"])

    def test_replies_use_mine_user_scope_and_verified_content(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user", "deptId": "not-the-scope"}}),
            Response({"total": 1, "rows": [{
                "complaintId": "synthetic-order", "colTime": "2026-10-14 13:00:00",
                "remark": "<p>虚构催单内容</p>", "listId": "SYNTHETIC-WAYBILL",
            }]}),
        ]
        result = source.collect_items(current, "yunda:work_order_replies", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual("虚构催单内容", result["items"][0]["content"])
        self.assertEqual("待回复", result["items"][0]["status_label"])
        self.assertEqual("synthetic-cssp-user", current.get.call_args.kwargs["params"]["userId"])
        self.assertNotIn("orgCode", current.get.call_args.kwargs["params"])
        current.post.assert_not_called()

    def test_missing_cssp_identity_does_not_query_all_replies(self):
        current = session()
        current.get.return_value = Response({"code": 0, "User": {"deptId": "not-the-scope"}})
        result = source.collect_items(current, "yunda:work_order_replies", 1)
        self.assertEqual("error", result["status"])
        self.assertIsNone(result["total"])
        self.assertEqual(1, current.get.call_count)

    def test_empty_work_orders_are_explicitly_zero(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 0, "rows": []}),
        ]
        result = source.collect_items(current, "yunda:work_orders", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual(0, result["total"])
        self.assertFalse(result["has_more"])

    def test_work_order_business_failure_cannot_look_empty(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"code": 500, "total": 0, "rows": []}),
        ]
        result = source.collect_items(current, "yunda:work_orders", 1)
        self.assertEqual("error", result["status"])
        self.assertIsNone(result["total"])

    def test_missing_message_cookie_is_auth_required(self):
        current = session()
        current.cookies.clear()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual("auth_required", result["status"])
        current.post.assert_not_called()

    def test_conflicting_message_cookies_are_not_arbitrarily_chosen(self):
        current = session()
        current.cookies.set("ydky_rabbit_web_cip_sessionid", "synthetic-other",
                            domain="ky-export.yunda56.com", path="/ydky_rabbit_web")
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual("auth_required", result["status"])
        self.assertNotIn("synthetic-other", str(result))
        current.post.assert_not_called()

    def test_wrong_name_domain_or_empty_export_cookie_is_auth_required(self):
        for name, domain, value in (
            ("cip_sessionid", "ky-export.yunda56.com", "synthetic-session"),
            ("ydky_rabbit_web_cip_sessionid", "ky-client.yunda56.com", "synthetic-session"),
            ("ydky_rabbit_web_cip_sessionid", ".yunda56.com", "synthetic-session"),
            ("ydky_rabbit_web_cip_sessionid", "ky-export.yunda56.com", ""),
        ):
            with self.subTest(name=name, domain=domain, empty=not value):
                current = session()
                current.cookies.clear()
                current.cookies.set(name, value, domain=domain, path="/")
                current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
                result = source.collect_items(current, "yunda:notifications", 1)
                self.assertEqual("auth_required", result["status"])
                current.post.assert_not_called()

    def test_export_cookie_does_not_override_business_auth_failure(self):
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        current.post.return_value = Response({"errorCode": 1001, "rows": [], "total": 0})
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual("auth_required", result["status"])
        self.assertIsNone(result["total"])

    def test_missing_work_order_body_does_not_fabricate_content(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 1, "rows": [{"complaintId": "synthetic-order"}]}),
            Response({"unexpected": "synthetic"}),
        ]
        result = source.collect_items(current, "yunda:work_orders", 1)
        self.assertEqual("error", result["status"])
        self.assertIn("compContent", result["message"])
        self.assertEqual([], result["items"])

    def test_detail_work_is_bounded_to_page_size(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 21, "rows": [{"complaintId": str(i)} for i in range(21)]}),
        ]
        result = source.collect_items(current, "yunda:work_orders", 1)
        self.assertEqual("error", result["status"])
        self.assertEqual(2, current.get.call_count)
        with self.assertRaises(ValueError):
            source.collect_items(current, "yunda:work_orders", 1, 21)

    def test_work_order_details_cannot_be_attached_to_another_id(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 1, "rows": [{"complaintId": "synthetic-order"}]}),
            Response({"complaintId": "synthetic-other", "compContent": "不应展示的虚构内容"}),
        ]
        result = source.collect_items(current, "yunda:work_orders", 1)
        self.assertEqual("error", result["status"])
        self.assertNotIn("不应展示", str(result))

    def test_message_source_must_obey_page_size(self):
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        current.post.return_value = Response({"errorCode": 0, "total": 21, "rows": [message_row(msgId=str(i)) for i in range(21)]})
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual("error", result["status"])
        self.assertEqual([], result["items"])

    def test_messages_sort_current_page_by_latest_time(self):
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        current.post.return_value = Response({"errorCode": 0, "total": 2, "rows": [
            message_row(msgId="older", createdTime="2026-10-13 10:00:00"),
            message_row(msgId="newer", createdTime="2026-10-14 10:00:00"),
        ]})
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual(["newer", "older"], [item["id"] for item in result["items"]])

    def test_html_is_plain_text_and_links_do_not_use_upstream_queries(self):
        current = session()
        current.get.return_value = Response({"code": 200, "details": {"username": "synthetic-user"}})
        current.post.return_value = Response({"errorCode": 0, "total": 1, "rows": [message_row(
            msgContent='<script>bad()</script><p>测试 &amp; 提醒</p>',
            redirectUrl="https://example.invalid/?token=synthetic-token",
        )]})
        result = source.collect_items(current, "yunda:notifications", 1)
        self.assertEqual("测试 & 提醒", result["items"][0]["content"])
        self.assertEqual(source.MESSAGE_PAGE, result["items"][0]["open_url"])
        self.assertNotIn("synthetic-token", str(result))


if __name__ == "__main__":
    unittest.main()
