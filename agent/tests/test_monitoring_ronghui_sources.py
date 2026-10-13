"""Synthetic contracts for Ronghui notices and received work orders."""

import unittest
from unittest.mock import Mock, patch

from agent.tms_runtime import monitoring, monitoring_ronghui as source
from agent.tms_runtime.errors import TMSAuthStateError


class Response:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("synthetic-http-error")


def work_order(**kwargs):
    return {
        "WORK_ORDER_CODE": "SYNTHETIC-ORDER", "WORK_CONTENT": "<p>虚构工单正文</p>",
        "WORK_NAME": "测试工单", "WORK_STATE": "待责任方响应",
        "REGISTER_DATE": "2026-10-14 09:00:00", "FIRST_RESPONSE_END_DATE": "2026-10-15 09:00:00",
        "BILL_CODE": "SYNTHETIC-WAYBILL", **kwargs,
    }


class RonghuiSourcesTests(unittest.TestCase):
    def setUp(self):
        self.current = Mock()
        self.context = {
            "url": source.HOME, "authentication_key": "synthetic-key", "page_id": "synthetic-page",
        }
        self.context_patch = patch.object(source, "_context", return_value=self.context)
        self.resolver = self.context_patch.start()
        self.addCleanup(self.context_patch.stop)

    def test_categories_query_received_states_without_date_filters(self):
        self.current.post.side_effect = [
            Response([]), Response({"data": [work_order()], "total": 22}),
            Response({"data": [work_order(WORK_STATE="待责任方处理")], "total": 3}),
        ]
        result = source.collect_categories(self.current)
        self.assertEqual([0, 22, 3], [category["count"] for category in result])
        self.assertEqual(2, self.resolver.call_count)
        for index, state in ((1, "2"), (2, "3")):
            call = self.current.post.call_args_list[index]
            self.assertEqual("FIND_TAB_WORK_ORDER", call.kwargs["params"]["id"])
            self.assertEqual({
                "ZRF": 1, "IS_DEL1": 1, "WORK_STATE": state,
                "ORDER_BY_CREATE_DATE": "ORDER_BY_DATE", "pageIndex": 0, "pageSize": 1,
            }, call.kwargs["data"])
            self.assertEqual(20, call.kwargs["timeout"])
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_wrong_state_is_failure_not_zero(self):
        self.current.post.side_effect = [
            Response([]), Response({"data": [work_order(WORK_STATE="已完结")], "total": 1}),
            Response({"data": [], "total": 0}),
        ]
        result = source.collect_categories(self.current)
        self.assertEqual("error", result[1]["status"])
        self.assertIsNone(result[1]["count"])
        self.assertEqual(0, result[2]["count"])

    def test_work_order_body_and_history_are_text_only_read_queries(self):
        self.current.post.side_effect = [
            Response({"data": [work_order()], "total": 23}),
            Response({"data": [{
                "GUID": "synthetic-reply", "WORK_ORDER_CODE": "SYNTHETIC-ORDER",
                "HANDLE_DATE": "2026-10-14 12:00:00", "HANDLE_STATE": "测试处理",
                "HANDLE_CONTENT": "<script>bad()</script><p>虚构回复内容 &amp; 提醒</p>",
            }], "total": 1}),
        ]
        result = source.collect_items(self.current, "ronghui:work_response", 2)
        self.assertEqual("ok", result["status"])
        self.assertEqual("2026-10-14 12:00:00", result["items"][0]["occurred_at"])
        content = result["items"][0]["content"]
        self.assertIn("虚构工单正文", content)
        self.assertIn("虚构回复内容 & 提醒", content)
        self.assertIn("原系统未提供未读标识", content)
        self.assertIn("响应截止", content)
        self.assertNotIn("bad()", content)
        self.assertNotIn("<p>", content)
        self.assertEqual(1, self.current.post.call_args_list[0].kwargs["data"]["pageIndex"])
        for call in self.current.post.call_args_list:
            self.assertIn("/dataQuery/findPageByCallId", call.args[0])
        self.assertEqual("FIND_TAB_WORK_ORDER_DETAILS", self.current.post.call_args_list[1].kwargs["params"]["id"])

    def test_announcements_sort_then_paginate_and_include_all_content_columns(self):
        self.current.post.return_value = Response([
            {"GUID": "older", "TITLE": "旧公告", "CONTENT": "一", "NOTICE_DATETIME": "2026-10-13 08:00:00"},
            {"GUID": "newer", "TITLE": "新公告", "CONTENT": "<p>正文</p>", "CONTENT1": "补充一", "CONTENT2": "补充二",
             "NOTICE_DATETIME": "2026-10-14 08:00:00"},
        ])
        first = source.collect_items(self.current, "ronghui:announcements", 1, 1)
        self.assertEqual(2, first["total"])
        self.assertTrue(first["has_more"])
        self.assertEqual("newer", first["items"][0]["id"])
        self.assertEqual("正文\n补充一\n补充二", first["items"][0]["content"])
        second = source.collect_items(self.current, "ronghui:announcements", 2, 1)
        self.assertEqual("older", second["items"][0]["id"])
        self.assertFalse(second["has_more"])

    def test_missing_body_and_changed_state_stop_list_projection(self):
        for row in (work_order(WORK_STATE="已完结"), {"WORK_ORDER_CODE": "SYNTHETIC-ORDER", "WORK_STATE": "待责任方响应"}):
            with self.subTest(row=row):
                self.current.post.return_value = Response({"data": [row], "total": 1})
                with self.assertRaises(source.RonghuiMonitoringError):
                    source.collect_items(self.current, "ronghui:work_response", 1)

    def test_duplicate_ids_fail_instead_of_double_counting(self):
        self.current.post.side_effect = [
            Response({"data": [work_order(), work_order()], "total": 2}),
            Response({"data": [], "total": 0}),
        ]
        with self.assertRaises(source.RonghuiMonitoringError):
            source.collect_items(self.current, "ronghui:work_response", 1)

    def test_history_failure_keeps_verified_main_body_with_partial_status(self):
        self.current.post.side_effect = [
            Response({"data": [work_order()], "total": 1}), Response(status_code=500),
        ]
        result = source.collect_items(self.current, "ronghui:work_response", 1)
        self.assertEqual("partial", result["status"])
        self.assertIn("虚构工单正文", result["items"][0]["content"])
        self.assertIn("暂时读取失败", result["items"][0]["content"])

    def test_empty_and_out_of_range_pages_remain_empty(self):
        for total, page in ((0, 1), (2, 2)):
            with self.subTest(total=total, page=page):
                self.current.post.return_value = Response({"data": [], "total": total})
                result = source.collect_items(self.current, "ronghui:work_response", page)
                self.assertEqual("ok", result["status"])
                self.assertEqual([], result["items"])
                self.assertFalse(result["has_more"])

    def test_redirect_is_auth_failure(self):
        self.current.post.return_value = Response(status_code=302)
        with self.assertRaises(TMSAuthStateError):
            source.collect_items(self.current, "ronghui:work_response", 1)

    def test_login_html_is_auth_failure(self):
        self.current.post.return_value = Response(ValueError(), text='<html><title>用户登录</title><form action="/system/login">')
        with self.assertRaises(TMSAuthStateError):
            source.collect_items(self.current, "ronghui:work_response", 1)

    def test_pagination_inputs_are_bounded_before_queries(self):
        for page, size in ((0, 20), (1, 21), (1, 0)):
            with self.subTest(page=page, size=size):
                self.current.post.reset_mock()
                with self.assertRaises((ValueError, source.RonghuiMonitoringError)):
                    source.collect_items(self.current, "ronghui:work_response", page, size)
                self.current.post.assert_not_called()

    def test_source_ignoring_page_size_is_rejected_before_history_expansion(self):
        self.current.post.return_value = Response({
            "data": [work_order(WORK_ORDER_CODE=f"SYNTHETIC-{index}") for index in range(21)], "total": 21,
        })
        with self.assertRaises(source.RonghuiMonitoringError):
            source.collect_items(self.current, "ronghui:work_response", 1)
        self.assertEqual(1, self.current.post.call_count)

    def test_nonzero_total_without_expected_rows_is_not_successful_empty(self):
        self.current.post.return_value = Response({"data": [], "total": 5})
        with self.assertRaises(source.RonghuiMonitoringError):
            source.collect_items(self.current, "ronghui:work_response", 1)

    def test_history_from_other_work_order_is_not_displayed(self):
        self.current.post.side_effect = [
            Response({"data": [work_order()], "total": 1}),
            Response({"data": [{
                "GUID": "synthetic-foreign", "WORK_ORDER_CODE": "OTHER-SYNTHETIC-ORDER",
                "HANDLE_DATE": "2026-10-14 12:00:00", "HANDLE_CONTENT": "不属于当前工单的虚构内容",
            }], "total": 1}),
        ]
        result = source.collect_items(self.current, "ronghui:work_response", 1)
        self.assertEqual("partial", result["status"])
        self.assertNotIn("不属于当前工单", str(result))


class RonghuiContextTests(unittest.TestCase):
    def test_existing_context_resolver_supports_exact_work_order_menu(self):
        current = Mock()
        current.request.return_value = Response([
            {"text": "其他菜单", "url": "/widget/home?pageId=unrelated&authenticationKey=unused"},
            {"text": source.WORK_MENU, "url": "/widget/home?pageId=synthetic-page&authenticationKey=synthetic-key"},
        ])
        current.get.return_value = Response(text="<html>synthetic source page</html>")
        result = source._context(current, source.WORK_MENU)
        self.assertEqual("synthetic-page", result["page_id"])
        self.assertEqual("synthetic-key", result["authentication_key"])
        self.assertEqual(1, current.get.call_count)

    def test_ambiguous_menu_is_not_arbitrarily_selected(self):
        current = Mock()
        current.request.return_value = Response([
            {"text": source.WORK_MENU, "url": "/widget/home?pageId=first&authenticationKey=synthetic-key"},
            {"text": source.WORK_MENU, "url": "/widget/home?pageId=second&authenticationKey=synthetic-key"},
        ])
        with self.assertRaises(source.RonghuiMonitoringError):
            source._context(current, source.WORK_MENU)
        current.get.assert_not_called()


class MonitoringAdapterIntegrationTests(unittest.TestCase):
    def tearDown(self):
        monitoring._ITEM_CACHE.clear()
        monitoring._ITEM_LOCKS.clear()

    def test_yunda_error_result_is_not_successful_monitoring_payload(self):
        current = Mock()
        broker = Mock()
        broker.build_requests_session.return_value = current
        with patch.object(monitoring, "get_session_broker", return_value=broker), patch(
            "agent.tms_runtime.monitoring_yunda.collect_items",
            return_value={"status": "auth_required", "items": [], "total": None, "message": "登录失效"},
        ):
            result = monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications", force=True)
        self.assertFalse(result["ok"])
        self.assertEqual("auth_required", result["status"])
        self.assertEqual("AUTH_REQUIRED", result["error_code"])
        current.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
