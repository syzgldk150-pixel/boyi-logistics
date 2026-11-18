"""Synthetic source contracts for read-only Yunda monitoring."""

import datetime as dt
import unittest
from unittest.mock import Mock, patch

import requests

from agent.tms_runtime import monitoring_window, monitoring_yunda as source


class Response:
    def __init__(self, payload=None, status_code=200, text="", url=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.url = url

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def session():
    result = Mock()
    result.cookies = requests.cookies.RequestsCookieJar()
    return result


TODAY = dt.date(2026, 11, 18)
RECENT = "2026-11-17 09:00:00"
OLD = "2026-11-15 23:59:59"
ARB_LANDING = Response(status_code=200, text="<title>快运仲裁查询</title>",
                       url=source.ARBITRATION_PAGE + "?sso=sso")


def problem_row(**kwargs):
    return {"prob_main_id": "1001", "ship_no": "SYNTHETIC-WAYBILL", "prob_type_nm": "催派送",
            "prob_text": "<p>请尽快派送虚构件</p>", "prob_status": "处理中", "site_id": "虚构发布网点",
            "created_time": "2026-11-18 09:00:00", "reply_text": "", "reply_time": "", **kwargs}


def arbitration_row(**kwargs):
    return {"declId": "ARB-1", "waybillCode": "SYNTHETIC-WAYBILL", "declType": "时效类", "declCategory": "超时",
            "disposeState": "已接单,处理中", "declareDept": "虚构申报网点", "money": "50",
            "createDate": "2026-11-10 10:00:00", "assignmentUpdateTime": "2026-11-11 10:00:00", **kwargs}


def transfer_row(**kwargs):
    return {"Replace_Id": "TR-1", "Logistics_Id": "SYNTHETIC-WAYBILL", "Replace_Status": "待接单",
            "Replace_Status_Code": "0", "Apply_Site_Name": "虚构发布网点", "Create_Time": "2026-11-17 08:00:00", **kwargs}


def empty_page():
    return Response({"total": 0, "rows": []})


class FixedToday(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(monitoring_window, "today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)


class WindowedSourceTests(FixedToday):
    def test_recent_range_is_today_and_previous_two_days(self):
        self.assertEqual((dt.date(2026, 11, 16), TODAY), source._recent_range())
        self.assertTrue(monitoring_window.is_recent(["2026-11-16 00:00:00"]))
        self.assertFalse(monitoring_window.is_recent([OLD]))
        self.assertTrue(monitoring_window.is_recent([OLD, RECENT]))
        self.assertTrue(monitoring_window.is_recent(["", None]))

    def test_problems_read_only_open_statuses_published_to_me(self):
        current = session()
        current.post.side_effect = [Response({"total": 1, "rows": [problem_row()]})] + [empty_page()] * 5
        result = source.collect_items(current, "yunda:problems", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, result["total"])
        item = result["items"][0]
        self.assertIn("请尽快派送虚构件", item["content"])
        self.assertIn("本网点尚未回复", item["content"])
        self.assertEqual("处理中", item["status_label"])
        self.assertEqual(6, current.post.call_count)
        queries = set()
        for call in current.post.call_args_list:
            self.assertEqual(source.PROBLEM_LIST, call.args[0])
            data = call.kwargs["data"]
            self.assertEqual("1", data["issuer_site"])
            self.assertEqual(("2026-11-16", "2026-11-18"), (data["start_date"], data["end_date"]))
            queries.add((data["time"], data["prob_status"]))
            self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual({(time, status) for time in "12" for status in "013"}, queries)
        current.get.assert_not_called()

    def test_problem_reply_is_shown_and_finished_record_fails_closed(self):
        current = session()
        current.post.side_effect = [Response({"total": 1, "rows": [problem_row(reply_text="已安排", reply_time="2026-11-18 10:00:00")]})] + [empty_page()] * 5
        result = source.collect_items(current, "yunda:problems", 1)
        self.assertIn("最新回复（2026-11-18 10:00:00）：已安排", result["items"][0]["content"])
        current = session()
        current.post.side_effect = [Response({"total": 1, "rows": [problem_row(prob_status="已完结")]})] + [empty_page()] * 5
        result = source.collect_items(current, "yunda:problems", 1)
        self.assertEqual("error", result["status"])
        self.assertEqual([], result["items"])

    def test_problem_pages_are_read_until_total_and_duplicates_count_once(self):
        first = [problem_row(prob_main_id=str(i)) for i in range(source.FETCH_PAGE_SIZE)]
        current = session()
        current.post.side_effect = [
            Response({"total": source.FETCH_PAGE_SIZE + 1, "rows": first}),
            Response({"total": source.FETCH_PAGE_SIZE + 1, "rows": [problem_row(prob_main_id="0")]}),
        ] + [empty_page()] * 5
        rows = source._problem_rows(current)
        self.assertEqual(source.FETCH_PAGE_SIZE, len(rows))
        self.assertEqual(2, current.post.call_args_list[1].kwargs["data"]["page"])

    def test_short_page_with_larger_total_is_failure_not_partial(self):
        current = session()
        current.post.side_effect = [Response({"total": 5, "rows": []})]
        result = source.collect_items(current, "yunda:problems", 1)
        self.assertEqual("error", result["status"])
        self.assertIsNone(result["total"])

    def test_arbitration_opens_sso_page_then_queries_only_open_states(self):
        current = session()
        current.get.side_effect = [ARB_LANDING, Response({"total": 1, "rows": [arbitration_row()]}),
                                   Response({"total": 1, "rows": [arbitration_row()]})]
        result = source.collect_items(current, "yunda:arbitrations", 1)
        self.assertEqual(1, result["total"])
        self.assertEqual(["k.declare_date", "g.assignment_update_time"],
                         [call.kwargs["params"]["searchDate"] for call in current.get.call_args_list[1:]])
        self.assertEqual("ok", result["status"])
        self.assertEqual("已接单,处理中", result["items"][0]["status_label"])
        self.assertIn("虚构申报网点", result["items"][0]["content"])
        self.assertTrue(current.get.call_args_list[0].kwargs["allow_redirects"])
        list_call = current.get.call_args_list[1]
        self.assertEqual(source.ARBITRATION_LIST, list_call.args[0])
        codes = set(list_call.kwargs["params"]["disposeState"].split(","))
        self.assertTrue({"jsshtg", "jsshbtg", "zxbsl"}.isdisjoint(codes))
        self.assertIn("tdkjxsb", codes)
        self.assertFalse(list_call.kwargs["allow_redirects"])
        current.post.assert_not_called()

    def test_arbitration_final_state_and_login_landing_fail_closed(self):
        current = session()
        current.get.side_effect = [ARB_LANDING, Response({"total": 1, "rows": [arbitration_row(disposeState="结算审核通过")]}),
                                   empty_page()]
        self.assertEqual("error", source.collect_items(current, "yunda:arbitrations", 1)["status"])
        current = session()
        current.get.side_effect = [Response(status_code=200, text="<title>登录</title>", url="https://ky-sso.yunda56.com/login")]
        result = source.collect_items(current, "yunda:arbitrations", 1)
        self.assertEqual("auth_required", result["status"])
        self.assertEqual(1, current.get.call_count)

    def test_transfers_only_read_pending_acceptance(self):
        current = session()
        current.post.side_effect = [Response({"total": 1, "rows": [transfer_row()]})]
        result = source.collect_items(current, "yunda:transfers", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual("TR-1", result["items"][0]["id"])
        for call in current.post.call_args_list:
            self.assertEqual(source.TRANSFER_LIST, call.args[0])
            self.assertEqual("0", call.kwargs["data"]["Replace_Status"])
        current = session()
        current.post.side_effect = [Response({"total": 1, "rows": [transfer_row(Replace_Status_Code="2")]})]
        self.assertEqual("error", source.collect_items(current, "yunda:transfers", 1)["status"])

    def test_windowed_items_sort_then_paginate(self):
        rows = [problem_row(prob_main_id=str(i), created_time=f"2026-11-{10 + i:02d} 08:00:00") for i in range(3)]
        current = session()
        current.post.side_effect = [Response({"total": 3, "rows": rows})] + [empty_page()] * 5
        result = source.collect_items(current, "yunda:problems", 2, 2)
        self.assertEqual(3, result["total"])
        self.assertEqual(["0"], [item["id"] for item in result["items"]])
        self.assertFalse(result["has_more"])

    def test_redirect_and_timeout_are_reported_without_request_details(self):
        current = session()
        current.post.return_value = Response(status_code=302)
        self.assertEqual("auth_required", source.collect_items(current, "yunda:transfers", 1)["status"])
        current = session()
        current.post.side_effect = requests.Timeout("URL?token=synthetic-token")
        result = source.collect_items(current, "yunda:problems", 1)
        self.assertIn("超时", result["message"])
        self.assertNotIn("synthetic-token", str(result))

    def test_categories_are_independent_and_message_center_is_not_read(self):
        current = session()

        def post(url, **kwargs):
            if url == source.PROBLEM_LIST:
                return Response({"code": 500, "total": 0, "rows": []})
            return Response({"total": 2, "rows": [transfer_row(Replace_Id="A"), transfer_row(Replace_Id="B")]})

        def get(url, **kwargs):
            if url == source.ARBITRATION_PAGE:
                return ARB_LANDING
            if url == source.ARBITRATION_LIST:
                return empty_page()
            if url.endswith("/getUser"):
                return Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}})
            if "cuiPageQuery" in url:
                return empty_page()
            return Response({"total": 3, "rows": [{"complaintId": "new", "compTime": RECENT},
                                                  {"complaintId": "older", "compTime": OLD},
                                                  {"complaintId": "same-day", "compTime": "2026-11-16 08:00:00"}]})

        current.post.side_effect = post
        current.get.side_effect = get
        categories = {item["category_id"]: item for item in source.collect_categories(current)}
        self.assertEqual(set(source._SOURCES), set(categories))
        self.assertEqual("error", categories["yunda:problems"]["status"])
        self.assertIsNone(categories["yunda:problems"]["count"])
        self.assertEqual(0, categories["yunda:arbitrations"]["count"])
        self.assertEqual(2, categories["yunda:transfers"]["count"])
        self.assertEqual(2, categories["yunda:work_orders"]["count"])
        self.assertTrue(all(not item["include_in_total"] for item in categories.values()))
        urls = [call.args[0] for call in current.get.call_args_list + current.post.call_args_list]
        self.assertFalse(any("ky-export" in url or "ky-client" in url for url in urls))


class YundaSourceTests(FixedToday):
    def test_work_order_page_uses_default_pending_list_and_read_only_details(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 1, "rows": [{
                "complaintId": "synthetic-order", "listId": "SYNTHETIC-WAYBILL",
                "compTime": RECENT, "compTypeName": "测试工单",
                "compStatusName": "已响应待首回", "dealOverTime": "2026-10-15 12:00:00",
            }]}),
            Response({"complaintId": "synthetic-order", "compContent": "<p>请处理虚构记录</p>"}),
        ]
        result = source.collect_items(current, "yunda:work_orders", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual({"offset": 0, "limit": source.FETCH_PAGE_SIZE}, current.get.call_args_list[1].kwargs["params"])
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
        self.assertIsNone(result["total"])
        self.assertIn("缺少", result["message"])
        self.assertEqual([], result["items"])

    def test_replies_use_mine_user_scope_and_verified_content(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user", "deptId": "not-the-scope"}}),
            Response({"total": 1, "rows": [{
                "complaintId": "synthetic-order", "colTime": RECENT,
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
        rows = [{"complaintId": str(i), "compTime": RECENT} for i in range(21)]

        def get(url, **kwargs):
            if url.endswith("/getUser"):
                return Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}})
            if "/compInfo/" in url:
                complaint_id = url.rsplit("/", 1)[1]
                return Response({"complaintId": complaint_id, "compContent": "虚构正文"})
            return Response({"total": 21, "rows": rows})

        current.get.side_effect = get
        result = source.collect_items(current, "yunda:work_orders", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual(21, result["total"])
        self.assertTrue(result["has_more"])
        details = [call for call in current.get.call_args_list if "/compInfo/" in call.args[0]]
        self.assertEqual(20, len(details))
        with self.assertRaises(ValueError):
            source.collect_items(current, "yunda:work_orders", 1, 21)

    def test_old_work_orders_and_reminders_are_hidden(self):
        current = session()
        current.get.side_effect = [
            Response({"code": 0, "User": {"userId": "synthetic-cssp-user"}}),
            Response({"total": 1, "rows": [{"complaintId": "old", "colTime": OLD, "remark": "旧催单"}]}),
        ]
        result = source.collect_items(current, "yunda:work_order_replies", 1)
        self.assertEqual("ok", result["status"])
        self.assertEqual(0, result["total"])
        self.assertEqual([], result["items"])

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


if __name__ == "__main__":
    unittest.main()
