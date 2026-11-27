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


SITE = {"default": ("邵阳操作场", "739010002", "分拨中心"), "price_default": ("邵阳大祥站", "7390101", "一级网点"),
        "daxiang_s": ("邵阳大祥S站", "7390102", "一级网点"), "self_pickup_problem_upload": ("邵阳自提部", "7390103", "一级网点")}
ONE = (("price_default", "大祥站账号", "price_default"),)


def complaint(**kwargs):
    return {"EXCEPTION_ID": "SYNTHETIC-EXC", "BILL_CODE": "SYNTHETIC-WAYBILL", "EXCEPTION_TYPE": "货物破损",
            "STATUS": "待审核", "ARBITRATE_STATUS": None, "EXCEPTION_SITE": "虚构投诉站", "REGISTER_SITE": "虚构投诉站",
            "EXCEPTIONSITE_SIDE": "邵阳大祥站", "EXCEPTION_DATE": "2026-10-14 09:00:00", "REMARK": "虚构投诉内容", **kwargs}


def appeal(**kwargs):
    return {"EXCEPTION_ID": "SYNTHETIC-EXC", "CREATE_DATE": "2026-10-14 10:00:00", "COMPLAINT_STATUS": "待审核",
            "APPEAL_SITE": "邵阳大祥站", "CREATE_SITE": "邵阳大祥站", "APPEAL_CONTENT": "虚构申诉", "PROCESS_TYPE": "差错处理",
            "BILL_CODE": "SYNTHETIC-WAYBILL", **kwargs}


def pickup(**kwargs):
    return {"TX_LOGISTIC_ID": "SYNTHETIC-ORDER-1", "BILL_CODE": "", "STATUS": "4", "SEND_NAME": "虚构寄件人",
            "CREATE_DATE": "2026-10-14 11:00:00", **kwargs}


def problem(external_id="P-1", reply="", **raw):
    return {"external_id": external_id, "waybill_no": "SYNTHETIC-WAYBILL", "problem_type": "催查/加急件",
            "problem_text": "虚构问题件", "reply_text": reply, "registered_at": "2026-10-14 08:00:00",
            "registration_saved_at": "", "updated_at": "",
            "raw": {"REGISTER_SITE": "虚构发布站", "SEND_SITE": "邵阳大祥站", **raw}}


class RonghuiSourcesTests(unittest.TestCase):
    """Synthetic Ronghui accounts: each profile has its own Mock session and login site."""

    def setUp(self):
        self.sessions = {profile: Mock(name=profile) for profile in SITE}
        self.data = {profile: {} for profile in SITE}
        for profile, current in self.sessions.items():
            current.post.side_effect = self._poster(profile)
        self.context = {"url": source.HOME, "authentication_key": "synthetic-key", "page_id": "synthetic-page"}
        self.missing_menus = {}
        self.resolver = self.patch(source, "_context", side_effect=self._resolve)
        self.patch(source, "_read_user_info_cookie", side_effect=self._user_info)
        self.problems = self.patch(source, "_ronghui_query", side_effect=self._problems)
        self.patch(monitoring_window, "today", return_value=dt.date(2026, 10, 15))
        self.failing_open = {}
        self.current = self.sessions["price_default"]

    def patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def open(self, profile):
        if profile in self.failing_open:
            raise self.failing_open[profile]
        return self.sessions[profile]

    def _user_info(self, current):
        profile = next(name for name, value in self.sessions.items() if value is current)
        site, code, kind = SITE[profile]
        return {"loginSiteName": site, "loginSiteCode": code, "loginSiteType": kind}

    def _resolve(self, current, menu):
        profile = next(name for name, value in self.sessions.items() if value is current)
        if menu in self.missing_menus.get(profile, ()):
            raise source.MenuMissing(f"账号菜单里没有融辉“{menu}”页面。")
        return self.context

    def _problems(self, current, params):
        profile = next(name for name, value in self.sessions.items() if value is current)
        rows = self.data[profile].get("problems", [])
        return {"ok": True, "rows": rows, "stats": {"total": len(rows), "returned": len(rows)}}

    def _poster(self, profile):
        def post(url, params, data, **kwargs):
            table = self.data[profile]
            call_id = params["id"]
            if "post" in table:
                return table["post"](url, params, data, **kwargs)
            if call_id == "FIND_TAB_NOTICE_NOT_SEE":
                return Response(table.get("notices", []))
            if call_id == "FIND_TAB_WORK_ORDER":
                rows = table.get("work", {}).get(data["WORK_STATE"], [])
                start = data["pageIndex"] * data["pageSize"]
                return Response({"data": rows[start:start + data["pageSize"]], "total": len(rows)})
            if call_id == "FIND_TAB_WORK_ORDER_DETAILS":
                if data["WORK_ORDER_CODE"] in table.get("failing_history", ()):
                    return Response(status_code=500)
                entries = table.get("history", {}).get(data["WORK_ORDER_CODE"], [])
                return Response({"data": entries, "total": len(entries)})
            key = {"FIND_TAB_EXCEPTION_SIDE": "against", "FIND_TAB_EXCEPTION_REGISTER_CS": "filed",
                   "FIND_TAB_EXCEPTION_APPEAL": "appeals", "FIND_TAB_UNITE_ORDERS_DQJ": "pickups"}.get(call_id)
            if key:
                rows = table.get(key, [])
                start = data["pageIndex"] * data["pageSize"]
                return Response({"data": rows[start:start + data["pageSize"]], "total": len(rows)})
            raise AssertionError(call_id)
        return post

    def route(self, work=None, notices=None, history=None, failing_history=(), profile="price_default"):
        self.data[profile].update(work=work or {}, notices=notices or [], history=history or {},
                                  failing_history=set(failing_history))

    def calls(self, call_id, profile="price_default"):
        return [call for call in self.sessions[profile].post.call_args_list if call.kwargs["params"]["id"] == call_id]

    def categories(self, accounts=ONE):
        return {item["category_id"]: item for item in source.collect_categories(self.open, accounts=accounts)}

    def items(self, category_id, page=1, size=20, accounts=ONE):
        return source.collect_items(self.open, category_id, page, size, accounts=accounts)

    # -- 收到工单 --------------------------------------------------------------------------

    def test_categories_cover_every_source_and_query_received_states(self):
        self.route(work={
            "2": [work_order(WORK_ORDER_CODE=f"R-{index}", RESPONSIBLE_PARTY="邵阳大祥站") for index in range(2)],
            "3": [work_order(WORK_STATE="待责任方处理", WORK_ORDER_CODE="R-3")],
        })
        result = self.categories()
        self.assertEqual(list(source.SOURCES), list(result))
        self.assertEqual(2, result["ronghui:work_response"]["count"])
        self.assertEqual(1, result["ronghui:work_processing"]["count"])
        self.assertIn("最近3天", result["ronghui:work_response"]["message"])
        for call, state in zip(self.calls("FIND_TAB_WORK_ORDER"), ("2", "3")):
            self.assertEqual({
                "ZRF": 1, "IS_DEL1": 1, "WORK_STATE": state,
                "ORDER_BY_CREATE_DATE": "ORDER_BY_DATE", "pageIndex": 0, "pageSize": source.WORK_FETCH_PAGE_SIZE,
            }, call.kwargs["data"])
            self.assertFalse(call.kwargs["allow_redirects"])
        self.sessions["price_default"].close.assert_called_once()

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
        result = self.items("ronghui:work_response")
        self.assertEqual(["OLD-URGED", "NEW", "OLD-UNKNOWN"], [item["id"] for item in result["items"]])
        self.assertEqual("2026-10-15 08:00:00", result["items"][0]["occurred_at"])
        self.assertEqual("partial", result["status"])
        self.assertIn("暂时读取失败", result["items"][2]["content"])
        self.assertNotIn("partial", result["items"][0])

    def test_finished_orders_are_hidden_but_unknown_state_is_failure(self):
        self.route(work={"2": [work_order(WORK_STATE="已完结")],
                         "3": [work_order(WORK_STATE="待受理", WORK_ORDER_CODE="X")]})
        result = self.categories()
        self.assertEqual(0, result["ronghui:work_response"]["count"])
        self.assertEqual("error", result["ronghui:work_processing"]["status"])
        self.assertIsNone(result["ronghui:work_processing"]["count"])

    def test_center_account_keeps_only_operations_yard_records(self):
        """用户 2026-11-27：操作场账号每一类都只保留责任方/发起方是邵阳操作场的记录。"""
        center = (("default", "操作场账号", "ronghui_default"),)
        parties = [("邵阳操作场", "虚构发起站"), ("邵阳大祥站", "邵阳操作场"), ("邵阳隆回站", "虚构发起站")]
        self.route(work={"2": [work_order(WORK_ORDER_CODE=f"W-{index}", RESPONSIBLE_PARTY=party, REGISTER_SITE=sponsor)
                               for index, (party, sponsor) in enumerate(parties)]}, profile="default")
        self.data["default"].update(
            problems=[problem("P-own", SEND_SITE="邵阳操作场"), problem("P-other", SEND_SITE="邵阳隆回站"),
                      problem("P-sent", REGISTER_SITE="邵阳操作场", SEND_SITE="虚构站")],
            against=[complaint(EXCEPTION_ID="C-own", EXCEPTIONSITE_SIDE="邵阳操作场"),
                     complaint(EXCEPTION_ID="C-other", EXCEPTIONSITE_SIDE="邵阳隆回站")],
            appeals=[appeal(EXCEPTION_ID="A-own", APPEAL_SITE="邵阳操作场", CREATE_SITE="邵阳操作场"),
                     appeal(EXCEPTION_ID="A-other", APPEAL_SITE="邵阳武冈站", CREATE_SITE="邵阳武冈站")])
        result = self.categories(center)
        self.assertEqual(2, result["ronghui:work_response"]["count"])
        self.assertEqual(2, result["ronghui:problems"]["count"])
        self.assertEqual(1, result["ronghui:complaints_against"]["count"])
        self.assertEqual(1, result["ronghui:appeals"]["count"])
        self.assertIn("操作场", result["ronghui:problems"]["message"])
        self.assertEqual({"W-0", "W-1"}, {item["id"] for item in self.items("ronghui:work_response", accounts=center)["items"]})

    def test_site_accounts_keep_the_original_page_results(self):
        self.route(work={"2": [work_order(WORK_ORDER_CODE="W-1", RESPONSIBLE_PARTY="邵阳武冈站")]})
        self.assertEqual(1, self.categories()["ronghui:work_response"]["count"])

    def test_missing_responsible_party_field_fails_closed(self):
        row = work_order()
        row.pop("RESPONSIBLE_PARTY")
        self.route(work={"2": [row]})
        with self.assertRaises(source.RonghuiMonitoringError):
            self.items("ronghui:work_response")

    def test_all_pages_are_read_before_filtering(self):
        size = source.WORK_FETCH_PAGE_SIZE
        rows = [work_order(WORK_ORDER_CODE=f"P1-{index}", RESPONSIBLE_PARTY="邵阳洞口站") for index in range(size)]
        rows.append(work_order(WORK_ORDER_CODE="P2"))
        self.route(work={"2": rows}, profile="default")
        account = source.Account("default", "操作场账号", self.sessions["default"], site="邵阳操作场")
        result = source._own_work_orders(account, self.context, "2")
        self.assertEqual(["P2"], [row["WORK_ORDER_CODE"] for row in result])
        self.assertEqual(1, self.calls("FIND_TAB_WORK_ORDER", "default")[1].kwargs["data"]["pageIndex"])

    def test_work_order_body_and_history_are_text_only_read_queries(self):
        self.route(work={"2": [work_order()]}, history={"SYNTHETIC-ORDER": [{
            "GUID": "synthetic-reply", "WORK_ORDER_CODE": "SYNTHETIC-ORDER",
            "HANDLE_DATE": "2026-10-14 12:00:00", "HANDLE_STATE": "测试处理",
            "HANDLE_CONTENT": "<script>bad()</script><p>虚构回复内容 &amp; 提醒</p>",
        }]})
        result = self.items("ronghui:work_response")
        self.assertEqual("ok", result["status"])
        item = result["items"][0]
        self.assertEqual("2026-10-14 12:00:00", item["occurred_at"])
        self.assertEqual("融辉 · 收到工单 · 待响应 · 邵阳大祥站", item["source_label"])
        for text in ("虚构工单正文", "虚构回复内容 & 提醒", "原系统未提供未读标识", "响应截止"):
            self.assertIn(text, item["content"])
        self.assertNotIn("bad()", item["content"])
        for call in self.current.post.call_args_list:
            self.assertIn("/dataQuery/findPageByCallId", call.args[0])
        self.assertEqual(1, len(self.calls("FIND_TAB_WORK_ORDER_DETAILS")))

    def test_missing_body_and_changed_state_stop_list_projection(self):
        for row in (work_order(WORK_STATE="待受理"), {"WORK_ORDER_CODE": "SYNTHETIC-ORDER", "WORK_STATE": "待责任方响应"}):
            with self.subTest(row=row):
                self.route(work={"2": [row]})
                with self.assertRaises(source.RonghuiMonitoringError):
                    self.items("ronghui:work_response")

    def test_duplicate_ids_within_one_account_fail_instead_of_double_counting(self):
        self.route(work={"2": [work_order(), work_order()]})
        with self.assertRaises(source.RonghuiMonitoringError):
            self.items("ronghui:work_response")

    def test_history_failure_keeps_verified_main_body_with_partial_status(self):
        self.route(work={"2": [work_order()]}, failing_history={"SYNTHETIC-ORDER"})
        result = self.items("ronghui:work_response")
        self.assertEqual("partial", result["status"])
        self.assertIn("虚构工单正文", result["items"][0]["content"])
        self.assertIn("暂时读取失败", result["items"][0]["content"])

    def test_history_from_other_work_order_is_not_displayed(self):
        self.route(work={"2": [work_order()]}, history={"SYNTHETIC-ORDER": [{
            "GUID": "synthetic-foreign", "WORK_ORDER_CODE": "OTHER-SYNTHETIC-ORDER",
            "HANDLE_DATE": "2026-10-14 12:00:00", "HANDLE_CONTENT": "不属于当前工单的虚构内容",
        }]})
        result = self.items("ronghui:work_response")
        self.assertEqual("partial", result["status"])
        self.assertNotIn("不属于当前工单", str(result))

    def test_empty_and_out_of_range_pages_remain_empty(self):
        for total, page in ((0, 1), (2, 2)):
            with self.subTest(total=total, page=page):
                self.route(work={"2": [work_order(WORK_ORDER_CODE=f"R-{index}") for index in range(total)]})
                result = self.items("ronghui:work_response", page)
                self.assertEqual("ok", result["status"])
                self.assertEqual([], result["items"])
                self.assertFalse(result["has_more"])

    def test_redirect_and_login_html_are_auth_failures(self):
        for response in (Response(status_code=302),
                         Response(ValueError(), text='<html><title>用户登录</title><form action="/system/login">')):
            with self.subTest(status=response.status_code):
                self.data["price_default"]["post"] = lambda *args, response=response, **kwargs: response
                with self.assertRaises(TMSAuthStateError):
                    self.items("ronghui:work_response")

    def test_pagination_inputs_are_bounded_before_queries(self):
        for page, size in ((0, 20), (1, 21), (1, 0)):
            with self.subTest(page=page, size=size):
                self.current.post.reset_mock()
                with self.assertRaises((ValueError, source.RonghuiMonitoringError)):
                    self.items("ronghui:work_response", page, size)
                self.current.post.assert_not_called()

    def test_source_ignoring_page_size_or_total_is_rejected(self):
        oversize = [work_order(WORK_ORDER_CODE=f"S-{index}") for index in range(source.WORK_FETCH_PAGE_SIZE + 1)]
        for payload in ({"data": oversize, "total": len(oversize)}, {"data": [], "total": 5}):
            with self.subTest(total=payload["total"]):
                self.data["price_default"]["post"] = lambda *args, payload=payload, **kwargs: Response(payload)
                with self.assertRaises(source.RonghuiMonitoringError):
                    self.items("ronghui:work_response")

    # -- 公告 --------------------------------------------------------------------------------

    def test_announcements_sort_then_paginate_and_include_all_content_columns(self):
        self.route(notices=[
            {"GUID": "older", "TITLE": "旧公告", "CONTENT": "一", "NOTICE_DATETIME": "2026-10-13 08:00:00"},
            {"GUID": "newer", "TITLE": "新公告", "CONTENT": "<p>正文</p>", "CONTENT1": "补充一", "CONTENT2": "补充二",
             "NOTICE_DATETIME": "2026-10-14 08:00:00"},
        ])
        first = self.items("ronghui:announcements", 1, 1)
        self.assertEqual(2, first["total"])
        self.assertTrue(first["has_more"])
        self.assertEqual("newer", first["items"][0]["id"])
        self.assertEqual("正文\n补充一\n补充二", first["items"][0]["content"])
        self.assertEqual("older", self.items("ronghui:announcements", 2, 1)["items"][0]["id"])

    def test_problem_notice_without_content_column_shows_summary(self):
        self.route(notices=[{
            "GUID": "notice-1", "TITLE": "问题件:SYNTHETIC-WAYBILL", "NOTICE_DATETIME": "2026-10-15 08:00:00",
            "NOTICE_NAME_TEXT": "问题件", "SEND_SITE": "虚构网点", "PERSON": "虚构网点(管理员)", "BILL_CODE": "SYNTHETIC-WAYBILL",
        }])
        result = self.items("ronghui:announcements")
        self.assertEqual("类型：问题件\n发送网点：虚构网点\n发送人：虚构网点(管理员)\n运单号：SYNTHETIC-WAYBILL",
                         result["items"][0]["content"])
        self.route(notices=[{"TITLE": "缺编号", "NOTICE_DATETIME": "2026-10-15 08:00:00"}])
        with self.assertRaises(source.RonghuiMonitoringError):
            self.items("ronghui:announcements")

    def test_old_announcements_are_hidden(self):
        self.route(notices=[
            {"GUID": "old", "TITLE": "旧公告", "CONTENT": "一", "NOTICE_DATETIME": "2026-10-12 23:59:59"},
            {"GUID": "new", "TITLE": "新公告", "CONTENT": "二", "NOTICE_DATETIME": "2026-10-13 00:00:00"},
        ])
        self.assertEqual(["new"], [item["id"] for item in self.items("ronghui:announcements")["items"]])
        self.assertEqual(1, self.categories()["ronghui:announcements"]["count"])

    # -- 收到问题件 ----------------------------------------------------------------------------

    def test_problems_show_only_recent_unreplied_received_ones(self):
        self.data["price_default"]["problems"] = [
            problem("P-open"), problem("P-replied", reply="已联系"),
            problem("P-old") | {"registered_at": "2026-10-01 08:00:00"},
        ]
        result = self.items("ronghui:problems")
        self.assertEqual(["P-open"], [item["id"] for item in result["items"]])
        self.assertEqual("未回复", result["items"][0]["status_label"])
        self.assertIn("通知网点：邵阳大祥站", result["items"][0]["content"])
        self.assertEqual({"platform": "ronghui", "account_id": "price_default", "external_id": "P-open",
                          "source_direction": "received", "waybill_no": "SYNTHETIC-WAYBILL", "status": ""},
                         result["items"][0]["reply"])
        params = self.problems.call_args.args[1]
        self.assertEqual({"direction": "received", "date_from": "2026-10-13", "date_to": "2026-10-15", "page": 1,
                          "page_size": source.FETCH_PAGE_SIZE, "LOGIN_SITE_CODE": "7390101"}, params["filters"])

    # -- 投诉、申诉、代取件 --------------------------------------------------------------------

    def test_complaints_follow_the_open_status_rule_and_page_parameters(self):
        """用户 2026-11-27：投诉状态“待审核”，或仲裁状态“待审核/待复审”才算未完结。"""
        self.data["price_default"]["against"] = [
            complaint(EXCEPTION_ID="C-1"), complaint(EXCEPTION_ID="C-2", STATUS="审核通过", ARBITRATE_STATUS="待复审"),
            complaint(EXCEPTION_ID="C-3", STATUS="审核通过", ARBITRATE_STATUS="待审核"),
            complaint(EXCEPTION_ID="C-4", STATUS="已理赔", ARBITRATE_STATUS="待理赔"),
            complaint(EXCEPTION_ID="C-5", STATUS="取消投诉"),
            complaint(EXCEPTION_ID="C-6", EXCEPTION_DATE="2026-10-01 00:00:00"),
        ]
        result = self.items("ronghui:complaints_against")
        self.assertEqual({"C-1", "C-2", "C-3"}, {item["id"] for item in result["items"]})
        self.assertIn("被投诉方：邵阳大祥站", result["items"][0]["content"])
        data = self.calls("FIND_TAB_EXCEPTION_SIDE")[0].kwargs["data"]
        self.assertEqual("7390101", data["CURRENT_LOGIN_SITE"])
        self.assertEqual("7390101", data["LOGIN_SITE_CODE"])
        self.assertEqual("EXCEPTION_DATE", data["searchDateType"])
        self.assertEqual('{"start":"2026/09/15 00:00:00","end":"2026/10/15 23:59:59"}', data["EXCEPTION_DATE"])
        self.data["price_default"]["filed"] = [complaint(EXCEPTION_ID="F-1", ARBITRATE_STATUS="待复审", STATUS=None)]
        self.assertEqual(["F-1"], [item["id"] for item in self.items("ronghui:complaints_filed")["items"]])

    def test_appeals_waiting_review_or_handling_are_open(self):
        self.data["price_default"]["appeals"] = [
            appeal(), appeal(CREATE_DATE="2026-10-14 11:00:00", COMPLAINT_STATUS="待处理"),
            appeal(CREATE_DATE="2026-10-14 12:00:00", COMPLAINT_STATUS="申诉驳回"),
            appeal(CREATE_DATE="2026-10-14 13:00:00", COMPLAINT_STATUS="处理通过"),
        ]
        result = self.items("ronghui:appeals")
        self.assertEqual(["SYNTHETIC-EXC:2026-10-14 11:00:00", "SYNTHETIC-EXC:2026-10-14 10:00:00"],
                         [item["id"] for item in result["items"]])
        self.data["price_default"]["appeals"] = [appeal(CREATE_DATE="")]
        with self.assertRaises(source.RonghuiMonitoringError):
            self.items("ronghui:appeals")

    def test_pickups_not_yet_collected_are_open_and_unknown_status_fails(self):
        self.data["price_default"]["pickups"] = [
            pickup(TX_LOGISTIC_ID="O-0", STATUS="0"), pickup(TX_LOGISTIC_ID="O-4"), pickup(TX_LOGISTIC_ID="O-1", STATUS="待揽收"),
            pickup(TX_LOGISTIC_ID="O-2", STATUS="2"), pickup(TX_LOGISTIC_ID="O-3", STATUS="已取消"),
        ]
        result = self.items("ronghui:pickups")
        self.assertEqual({"O-0", "O-4", "O-1"}, {item["id"] for item in result["items"]})
        self.assertEqual({"待分配", "待接单", "待揽收"}, {item["status_label"] for item in result["items"]})
        self.data["price_default"]["pickups"] = [pickup(STATUS="9")]
        with self.assertRaises(source.RonghuiMonitoringError):
            self.items("ronghui:pickups")

    # -- 四个账号合并 ----------------------------------------------------------------------------

    def test_accounts_are_merged_and_a_shared_record_counts_once(self):
        accounts = source.ACCOUNTS
        self.data["price_default"]["against"] = [complaint(EXCEPTION_ID="C-shared")]
        self.data["daxiang_s"]["against"] = [complaint(EXCEPTION_ID="C-shared", EXCEPTIONSITE_SIDE="邵阳大祥S站"),
                                             complaint(EXCEPTION_ID="C-s", EXCEPTIONSITE_SIDE="邵阳大祥S站")]
        result = self.categories(accounts)
        self.assertEqual(2, result["ronghui:complaints_against"]["count"])
        items = self.items("ronghui:complaints_against", accounts=accounts)["items"]
        self.assertEqual({"C-shared", "C-s"}, {item["id"] for item in items})
        for current in self.sessions.values():
            self.assertGreaterEqual(current.close.call_count, 2)

    def test_one_failing_account_gives_a_partial_count_that_names_it(self):
        accounts = source.ACCOUNTS
        self.failing_open["daxiang_s"] = TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请重新登录。")
        self.data["price_default"]["against"] = [complaint()]
        result = self.categories(accounts)["ronghui:complaints_against"]
        self.assertEqual((1, "partial", "部分账号读取失败"), (result["count"], result["status"], result["status_label"]))
        self.assertIn("大祥S站账号", result["message"])
        page = self.items("ronghui:complaints_against", accounts=accounts)
        self.assertEqual("partial", page["status"])
        self.assertIn("大祥S站账号", page["message"])

    def test_every_account_failing_leaves_the_count_unknown(self):
        for profile in SITE:
            self.failing_open[profile] = TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请重新登录。")
        result = self.categories(source.ACCOUNTS)["ronghui:work_response"]
        self.assertEqual((None, "auth_required"), (result["count"], result["status"]))
        with self.assertRaises(TMSAuthStateError):
            self.items("ronghui:work_response", accounts=source.ACCOUNTS)

    def test_accounts_without_the_pickup_page_are_skipped_not_failed(self):
        self.missing_menus = {"default": {"网点代取件业务"}, "self_pickup_problem_upload": {"网点代取件业务"}}
        self.data["price_default"]["pickups"] = [pickup()]
        result = self.categories(source.ACCOUNTS)["ronghui:pickups"]
        self.assertEqual((1, "ok"), (result["count"], result["status"]))
        self.missing_menus["price_default"] = {"被投诉方查询"}
        result = self.categories(source.ACCOUNTS)["ronghui:complaints_against"]
        self.assertEqual("partial", result["status"])


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


    def test_empty_menu_with_login_home_reports_expired_login(self):
        current = Mock()
        current.request.return_value = Response(ValueError("empty"), text="")
        login = Response(status_code=302)
        login.headers = {"Location": "/system/login"}
        current.get.return_value = login
        with self.assertRaises(TMSAuthStateError):
            source._context(current, source.WORK_MENU)

    def test_empty_menu_with_live_home_keeps_menu_error(self):
        current = Mock()
        current.request.return_value = Response(ValueError("empty"), text="")
        current.get.return_value = Response(text="<html>synthetic home</html>")
        with self.assertRaises(source.RonghuiMonitoringError):
            source._context(current, source.WORK_MENU)


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
