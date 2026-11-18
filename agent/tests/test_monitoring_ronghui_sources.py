"""Synthetic contracts for Ronghui notices and received work orders."""

import unittest
from unittest.mock import Mock, patch

import datetime as dt

from agent.tms_runtime import monitoring, monitoring_ronghui as source, monitoring_window
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
        "BILL_CODE": "SYNTHETIC-WAYBILL", "RESPONSIBLE_PARTY": "邵阳操作场", **kwargs,
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
        today_patch = patch.object(monitoring_window, "today", return_value=dt.date(2026, 10, 15))
        today_patch.start()
        self.addCleanup(today_patch.stop)

    def route(self, work=None, notices=None, history=None, failing_history=()):
        work, history = work or {}, history or {}

        def post(url, params, data, **kwargs):
            call_id = params["id"]
            if call_id == "FIND_TAB_NOTICE_NOT_SEE":
                return Response(notices or [])
            if call_id == "FIND_TAB_WORK_ORDER":
                rows = work.get(data["WORK_STATE"], [])
                start = data["pageIndex"] * data["pageSize"]
                return Response({"data": rows[start:start + data["pageSize"]], "total": len(rows)})
            if call_id == "FIND_TAB_WORK_ORDER_DETAILS":
                if data["WORK_ORDER_CODE"] in failing_history:
                    return Response(status_code=500)
                entries = history.get(data["WORK_ORDER_CODE"], [])
                return Response({"data": entries, "total": len(entries)})
            raise AssertionError(call_id)

        self.current.post.side_effect = post

    def calls(self, call_id):
        return [call for call in self.current.post.call_args_list if call.kwargs["params"]["id"] == call_id]

    def test_categories_query_received_states_without_date_filters(self):
        self.route(work={
            "2": [work_order(WORK_ORDER_CODE=f"R-{index}", RESPONSIBLE_PARTY=party)
                  for index, party in enumerate(("邵阳操作场", "邵阳洞口站", "邵阳大祥S站"))],
            "3": [work_order(WORK_STATE="待责任方处理", RESPONSIBLE_PARTY="新邵营业部")],
        })
        result = source.collect_categories(self.current)
        self.assertEqual([0, 2, 0], [category["count"] for category in result])
        self.assertIn("邵阳自提部", result[1]["message"])
        self.assertIn("最近3天", result[1]["message"])
        self.assertEqual(2, self.resolver.call_count)
        order_calls = self.calls("FIND_TAB_WORK_ORDER")
        for call, state in zip(order_calls, ("2", "3")):
            self.assertEqual({
                "ZRF": 1, "IS_DEL1": 1, "WORK_STATE": state,
                "ORDER_BY_CREATE_DATE": "ORDER_BY_DATE", "pageIndex": 0, "pageSize": source.WORK_FETCH_PAGE_SIZE,
            }, call.kwargs["data"])
            self.assertEqual(20, call.kwargs["timeout"])
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(["R-0", "R-2"], [call.kwargs["data"]["WORK_ORDER_CODE"] for call in self.calls("FIND_TAB_WORK_ORDER_DETAILS")])

    def test_only_recent_activity_is_shown(self):
        old = "2026-10-01 09:00:00"
        self.route(work={"2": [
            work_order(WORK_ORDER_CODE="OLD", REGISTER_DATE=old),
            work_order(WORK_ORDER_CODE="OLD-URGED", REGISTER_DATE=old),
            work_order(WORK_ORDER_CODE="NEW", REGISTER_DATE="2026-10-13 00:00:00"),
            work_order(WORK_ORDER_CODE="OLD-UNKNOWN", REGISTER_DATE=old),
        ]}, history={
            "OLD": [{"WORK_ORDER_CODE": "OLD", "HANDLE_DATE": "2026-10-02 10:00:00", "HANDLE_CONTENT": "旧回复"}],
            "OLD-URGED": [{"WORK_ORDER_CODE": "OLD-URGED", "HANDLE_DATE": "2026-10-15 08:00:00",
                           "HANDLE_STATE": "催工单", "HANDLE_CONTENT": "再次催单"}],
        }, failing_history={"OLD-UNKNOWN"})
        result = source.collect_items(self.current, "ronghui:work_response", 1)
        self.assertEqual(["OLD-URGED", "NEW", "OLD-UNKNOWN"], [item["id"] for item in result["items"]])
        self.assertEqual("2026-10-15 08:00:00", result["items"][0]["occurred_at"])
        self.assertEqual("partial", result["status"])
        self.assertIn("暂时读取失败", result["items"][2]["content"])
    def test_finished_orders_are_hidden_but_unknown_state_is_failure(self):
        self.current.post.side_effect = [
            Response([]), Response({"data": [work_order(WORK_STATE="已完结")], "total": 1}),
            Response({"data": [work_order(WORK_STATE="待受理")], "total": 1}),
        ]
        result = source.collect_categories(self.current)
        self.assertEqual(0, result[1]["count"])
        self.assertEqual("error", result[2]["status"])
        self.assertIsNone(result[2]["count"])

    def test_only_boyi_responsible_parties_are_listed(self):
        own = ("邵阳大祥站", "邵阳大祥S站", "邵阳操作场", "邵阳自提部")
        rows = [work_order(WORK_ORDER_CODE=f"R-{party}", RESPONSIBLE_PARTY=party) for party in own + ("邵阳武冈站", "", None)]
        self.current.post.side_effect = [Response({"data": rows, "total": len(rows)})] + [Response({"data": [], "total": 0})] * 4
        result = source.collect_items(self.current, "ronghui:work_response", 1)
        self.assertEqual(4, result["total"])
        self.assertEqual({f"R-{party}" for party in own}, {item["id"] for item in result["items"]})

    def test_missing_responsible_party_field_fails_closed(self):
        row = work_order()
        row.pop("RESPONSIBLE_PARTY")
        self.current.post.return_value = Response({"data": [row], "total": 1})
        with self.assertRaises(source.RonghuiMonitoringError):
            source.collect_items(self.current, "ronghui:work_response", 1)

    def test_all_pages_are_read_before_filtering(self):
        size = source.WORK_FETCH_PAGE_SIZE
        first = [work_order(WORK_ORDER_CODE=f"P1-{index}", RESPONSIBLE_PARTY="邵阳洞口站") for index in range(size)]
        self.current.post.side_effect = [
            Response({"data": first, "total": size + 1}),
            Response({"data": [work_order(WORK_ORDER_CODE="P2")], "total": size + 1}),
        ]
        result = source._own_work_orders(self.current, self.context, "2")
        self.assertEqual(["P2"], [row["WORK_ORDER_CODE"] for row in result])
        self.assertEqual(1, self.current.post.call_args_list[1].kwargs["data"]["pageIndex"])

    def test_work_order_body_and_history_are_text_only_read_queries(self):
        self.route(work={"2": [work_order()]}, history={"SYNTHETIC-ORDER": [{
            "GUID": "synthetic-reply", "WORK_ORDER_CODE": "SYNTHETIC-ORDER",
            "HANDLE_DATE": "2026-10-14 12:00:00", "HANDLE_STATE": "测试处理",
            "HANDLE_CONTENT": "<script>bad()</script><p>虚构回复内容 &amp; 提醒</p>",
        }]})
        result = source.collect_items(self.current, "ronghui:work_response", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual("2026-10-14 12:00:00", result["items"][0]["occurred_at"])
        content = result["items"][0]["content"]
        self.assertIn("虚构工单正文", content)
        self.assertIn("虚构回复内容 & 提醒", content)
        self.assertIn("原系统未提供未读标识", content)
        self.assertIn("响应截止", content)
        self.assertNotIn("bad()", content)
        self.assertNotIn("<p>", content)
        for call in self.current.post.call_args_list:
            self.assertIn("/dataQuery/findPageByCallId", call.args[0])
        self.assertEqual(1, len(self.calls("FIND_TAB_WORK_ORDER_DETAILS")))
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

    def test_problem_notice_without_content_column_shows_summary(self):
        self.route(notices=[{
            "GUID": "notice-1", "TITLE": "问题件:SYNTHETIC-WAYBILL", "NOTICE_DATETIME": "2026-10-15 08:00:00",
            "NOTICE_NAME_TEXT": "问题件", "SEND_SITE": "虚构网点", "PERSON": "虚构网点(管理员)", "BILL_CODE": "SYNTHETIC-WAYBILL",
        }])
        result = source.collect_items(self.current, "ronghui:announcements", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual("类型：问题件\n发送网点：虚构网点\n发送人：虚构网点(管理员)\n运单号：SYNTHETIC-WAYBILL",
                         result["items"][0]["content"])
        self.assertEqual("SYNTHETIC-WAYBILL", result["items"][0]["waybill_no"])
        self.route(notices=[{"TITLE": "缺编号", "NOTICE_DATETIME": "2026-10-15 08:00:00"}])
        with self.assertRaises(source.RonghuiMonitoringError):
            source.collect_items(self.current, "ronghui:announcements", 1)

    def test_old_announcements_are_hidden(self):
        self.route(notices=[
            {"GUID": "old", "TITLE": "旧公告", "CONTENT": "一", "NOTICE_DATETIME": "2026-10-12 23:59:59"},
            {"GUID": "new", "TITLE": "新公告", "CONTENT": "二", "NOTICE_DATETIME": "2026-10-13 00:00:00"},
        ])
        result = source.collect_items(self.current, "ronghui:announcements", 1)
        self.assertEqual(["new"], [item["id"] for item in result["items"]])
        self.assertEqual(1, source.collect_categories(self.current)[0]["count"])

    def test_missing_body_and_changed_state_stop_list_projection(self):
        for row in (work_order(WORK_STATE="待受理"), {"WORK_ORDER_CODE": "SYNTHETIC-ORDER", "WORK_STATE": "待责任方响应"}):
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
                self.route(work={"2": [work_order(WORK_ORDER_CODE=f"R-{index}") for index in range(total)]})
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
            "data": [work_order(WORK_ORDER_CODE=f"SYNTHETIC-{index}") for index in range(source.WORK_FETCH_PAGE_SIZE + 1)],
            "total": source.WORK_FETCH_PAGE_SIZE + 1,
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
            result = monitoring.build_monitoring_items(system="yunda", category_id="yunda:problems", force=True)
        self.assertFalse(result["ok"])
        self.assertEqual("auth_required", result["status"])
        self.assertEqual("AUTH_REQUIRED", result["error_code"])
        current.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
