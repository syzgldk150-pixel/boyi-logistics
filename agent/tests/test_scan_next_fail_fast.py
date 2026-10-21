"""Regression tests for Ronghui outgoing scans through the page's HTTP calls."""

from _tms_runtime_test_support import *  # noqa: F403

from agent.tms_runtime.scripts import scan_next


USER_INFO = {
    "loginUserName": "扫描员",
    "loginUserAccount": "scanner01",
    "loginSiteName": "测试操作场",
    "loginSiteCode": "90001",
    "loginSiteType": "分拨中心",
}
PAGE = {"url": "https://tms.example/widget/home", "authentication_key": "auth", "page_id": "page"}
STATIONS = {
    "甲站": [{"SITE_CODE": "1001", "SITE_NAME": "甲站", "TYPE": "一级网点"}],
    "乙站": [
        {"SITE_CODE": "1002", "SITE_NAME": "乙站", "TYPE": "一级网点"},
        {"SITE_CODE": "1003", "SITE_NAME": "乙站东", "TYPE": "一级网点"},
    ],
}


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    """Fake of the page's three query calls and the saveTables upload."""

    def __init__(self, *, signed=(), stations=STATIONS, saves=None, sign_payload=None, sent=(), sent_payload=None):
        self.headers = {}
        self.signed = set(signed)
        self.sent = list(sent)
        self.sent_payload = sent_payload
        self.stations = stations
        self.saves = list(saves or [])
        self.sign_payload = sign_payload
        self.calls = []
        self.uploads = []

    def post(self, url, data=None, files=None, headers=None, timeout=None, params=None, allow_redirects=True):
        if params and params.get("id") == "FIND_SEND_SCAN_RECORD":
            self.calls.append(("FIND_SEND_SCAN_RECORD", dict(data or {}), headers))
            if self.sent_payload is not None:
                return _Response(self.sent_payload)
            return _Response({"data": self.sent, "total": len(self.sent)})
        if url.endswith("/dataOperation/saveTables"):
            self.uploads.append({"params": json.loads(files["params"][1]), "headers": headers})
            return _Response(self.saves.pop(0) if self.saves else {"success": True, "message": "保存成功"})
        call_id = url.split("id=", 1)[1]
        self.calls.append((call_id, dict(data or {}), headers))
        if call_id == "FIND_TMS_SYS_SHARE_SET":
            return _Response([{"SHARE_CODE": "SubBill", "SHARE_VALUE": "^((1|2|R)[0-9]{13})$"}])
        if call_id == "FIND_WAYBILL_SIGN_STATE":
            if self.sign_payload is not None:
                return _Response(self.sign_payload)
            return _Response([{"BILL_CODE": data["BILL_CODE"]}] if data["BILL_CODE"] in self.signed else [])
        raise AssertionError(f"unexpected query {call_id}")

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((params["id"], dict(params), headers))
        rows = self.stations.get(params["key"], [])
        return _Response({"data": rows, "total": len(rows)})


class ScanNextHttpTests(unittest.TestCase):
    def _run(self, items, session=None, user_info=None):
        session = session or _Session()
        auth = Mock()
        auth.return_value.login_and_get_session.return_value = session
        with (
            patch.object(scan_next, "TMSAuth", auth),
            patch.object(scan_next, "_read_user_info_cookie", return_value=dict(user_info or USER_INFO)),
            patch.object(scan_next, "_resolve_menu_page_context", return_value=PAGE) as page_context,
            patch.object(scan_next, "log"),
        ):
            result = scan_next.run_flow(items=items, session_profile="ronghui_account_a")
        return result, session, auth, page_context

    @staticmethod
    def _items(*pairs):
        return [{"station_name": station, "bill_code": bill} for station, bill in pairs]

    def test_run_once_forwards_selected_session_profile(self):
        with patch.object(scan_next, "run_flow", return_value={"ok": True}) as run_flow:
            scan_next.run_once(
                {
                    "session_profile": "ronghui_account_a",
                    "items": [{"station_name": "甲站", "bill_code": "20000000000001"}],
                }
            )

        self.assertEqual(run_flow.call_args.kwargs["session_profile"], "ronghui_account_a")

    def test_blank_session_profile_does_not_fall_back_to_default(self):
        with self.assertRaisesRegex(ValueError, "session_profile 不能为空"):
            scan_next.run_once(
                {"session_profile": " ", "items": [{"station_name": "甲站", "bill_code": "20000000000001"}]}
            )

    def test_scan_opens_no_browser(self):
        self.assertFalse(hasattr(scan_next, "launch_browser"))
        self.assertFalse(hasattr(scan_next, "TMSBrowserAuth"))

    def test_rows_match_the_page_form_and_upload_once_per_station(self):
        result, session, auth, page_context = self._run(
            self._items(("甲站", "20000000000001"), ("甲站", "20000000000002"), ("乙站", "20000000000003"))
        )

        self.assertTrue(result["ok"], result)
        auth.assert_called_once_with(profile="ronghui_account_a")
        self.assertEqual(page_context.call_args.kwargs["menu_text"], "发件扫描")
        self.assertEqual(page_context.call_args.kwargs["content_markers"], ("TAB_SCAN_SEND_ADD",))
        self.assertEqual(len(session.uploads), 2)
        operation = session.uploads[0]["params"]
        self.assertEqual(len(operation), 1)
        self.assertEqual(operation[0]["operationKey"], "TAB_SCAN_SEND_ADD")
        self.assertEqual(session.uploads[0]["headers"]["authenticationKey"], "auth")
        self.assertEqual(session.uploads[0]["headers"]["pageId"], "page")
        row = operation[0]["data"][0]
        self.assertEqual(
            {key: row[key] for key in (
                "BILL_CODE", "PRE_OR_NEXT_STATION_CODE", "PRE_OR_NEXT_STATION", "SCAN_SITE",
                "SCAN_SITE_CODE", "SCAN_MAN", "SCAN_MAN_CODE", "SCAN_TYPE", "DATA_FROM",
                "FAST_TYPE", "SCAN_STATUS", "DISPATCH_SITE", "_state",
            )},
            {
                "BILL_CODE": "20000000000001",
                "PRE_OR_NEXT_STATION_CODE": "1001",
                "PRE_OR_NEXT_STATION": "甲站",
                "SCAN_SITE": "测试操作场",
                "SCAN_SITE_CODE": "90001",
                "SCAN_MAN": "扫描员",
                "SCAN_MAN_CODE": "scanner01",
                "SCAN_TYPE": "发件",
                "DATA_FROM": "K13",
                "FAST_TYPE": "汽运",
                "SCAN_STATUS": 3,
                "DISPATCH_SITE": "",
                "_state": "added",
            },
        )
        self.assertRegex(row["LISTING_CODE"], r"^\d{14}$")
        self.assertRegex(row["SCAN_DATE"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual([item["_id"] for item in operation[0]["data"]], [1, 2])
        self.assertEqual(session.uploads[1]["params"][0]["data"][0]["_id"], 3)
        self.assertEqual(
            result["detail"]["stations"],
            [
                {"station_name": "甲站", "count": 2, "bill_codes": ["20000000000001", "20000000000002"]},
                {"station_name": "乙站", "count": 1, "bill_codes": ["20000000000003"]},
            ],
        )
        self.assertEqual(result["detail"]["total_scanned"], 3)

    def test_center_and_site_accounts_use_the_page_station_lookup(self):
        _, center_session, _, _ = self._run(self._items(("甲站", "20000000000001")))
        site_info = {**USER_INFO, "loginSiteType": "一级网点"}
        _, site_session, _, _ = self._run(self._items(("甲站", "20000000000001")), user_info=site_info)

        lookups = [call for call in center_session.calls if call[0].startswith("FIND_SCAN_SEND_SITE")]
        self.assertEqual(lookups[0][0], "FIND_SCAN_SEND_SITE_COMBOBOX_BY_CEN1")
        self.assertEqual(lookups[0][1]["key"], "甲站")
        site_lookup = [call for call in site_session.calls if call[0].startswith("FIND_SCAN_SEND_SITE")][0]
        self.assertEqual(site_lookup[0], "FIND_SCAN_SEND_SITE_COMBOBOX_NEW")
        self.assertEqual(site_session.uploads[0]["params"][0]["data"][0]["SCAN_STATUS"], 2)

    def test_signed_items_are_skipped_without_stopping_later_items(self):
        session = _Session(signed={"20000000000002"})
        result, session, _, _ = self._run(
            self._items(("甲站", "20000000000001"), ("甲站", "20000000000002"), ("甲站", "20000000000003")),
            session,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["detail"]["skipped_signed_codes"], ["20000000000002"])
        uploaded = [row["BILL_CODE"] for row in session.uploads[0]["params"][0]["data"]]
        self.assertEqual(uploaded, ["20000000000001", "20000000000003"])
        self.assertEqual(result["detail"]["total_scanned"], 2)

    def test_station_with_only_signed_items_uploads_nothing(self):
        session = _Session(signed={"20000000000001"})
        result, session, _, _ = self._run(self._items(("甲站", "20000000000001")), session)

        self.assertTrue(result["ok"], result)
        self.assertEqual(session.uploads, [])
        self.assertEqual(result["detail"]["stations"], [])
        self.assertEqual(result["detail"]["total_scanned"], 0)

    def test_bill_rules_are_read_once_but_signed_state_per_bill(self):
        _, session, _, _ = self._run(self._items(("甲站", "20000000000001"), ("甲站", "20000000000002")))

        names = [call[0] for call in session.calls]
        self.assertEqual(names.count("FIND_TMS_SYS_SHARE_SET"), 1)
        self.assertEqual(names.count("FIND_WAYBILL_SIGN_STATE"), 2)
        rule_call = next(call for call in session.calls if call[0] == "FIND_TMS_SYS_SHARE_SET")
        self.assertEqual(rule_call[1], {"SHARE_CODE_IN": "MainBill,AllBill,ReturnBill,SubBill,VehicleCode"})

    def test_bill_outside_ronghui_rules_fails_before_any_upload(self):
        result, session, _, _ = self._run(self._items(("甲站", "20000000000001"), ("甲站", "ABC")))

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "check_bill")
        self.assertIn("单号不符合融辉单号规则: ABC", result["message"])
        self.assertEqual(session.uploads, [])

    def test_station_lookup_requires_one_exact_site_code(self):
        other = {"SITE_CODE": "1009", "SITE_NAME": "甲站", "TYPE": "一级网点"}
        for stations in ({}, {"甲站": STATIONS["甲站"] + [other]}):
            with self.subTest(stations=stations):
                result, session, _, _ = self._run(
                    self._items(("甲站", "20000000000001")), _Session(stations=stations)
                )

                self.assertFalse(result["ok"])
                self.assertEqual(result["stage"], "select_station")
                self.assertIn("下一站需唯一精确匹配", result["message"])
                self.assertEqual(result["detail"]["bill_code"], "")
                self.assertEqual(session.uploads, [])

    def test_same_site_listed_twice_by_the_lookup_is_one_station(self):
        session = _Session(stations={"甲站": STATIONS["甲站"] * 2})
        result, session, _, _ = self._run(self._items(("甲站", "20000000000001")), session)

        self.assertTrue(result["ok"], result)
        self.assertEqual(session.uploads[0]["params"][0]["data"][0]["PRE_OR_NEXT_STATION_CODE"], "1001")

    def _sent_row(self, bill, station="甲站", **overrides):
        return {"BILL_CODE": bill, "SCAN_TYPE": "发件", "SCAN_SITE_CODE": "90001",
                "PRE_OR_NEXT_STATION": station, **overrides}

    def test_bills_sent_today_to_the_same_station_are_skipped(self):
        session = _Session(sent=[
            self._sent_row("20000000000001"),
            self._sent_row("20000000000002", station="乙站"),
            self._sent_row("20000000000003", SCAN_SITE_CODE="80001"),
            self._sent_row("20000000000004", SCAN_TYPE="到件"),
        ])
        result, session, _, _ = self._run(
            self._items(*(("甲站", f"2000000000000{index}") for index in range(1, 5))), session
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["detail"]["skipped_signed_codes"], ["20000000000001"])
        uploaded = [row["BILL_CODE"] for row in session.uploads[0]["params"][0]["data"]]
        self.assertEqual(uploaded, ["20000000000002", "20000000000003", "20000000000004"])
        query = next(call for call in session.calls if call[0] == "FIND_SEND_SCAN_RECORD")
        self.assertEqual(query[1]["SCAN_TYPE"], "发件")
        self.assertEqual(query[1]["SCAN_SITE_CODE"], "90001")
        self.assertEqual(query[1]["searchOrderInput"].split("\n"), [f"2000000000000{index}" for index in range(1, 5)])

    def test_malformed_send_record_fails_before_any_upload(self):
        session = _Session(sent_payload={"success": False})
        result, session, _, _ = self._run(self._items(("甲站", "20000000000001")), session)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "sent_today")
        self.assertEqual(session.uploads, [])

    def test_partial_station_name_is_not_accepted(self):
        result, _, _, _ = self._run(self._items(("乙站", "20000000000001")))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["detail"]["stations"][0]["station_name"], "乙站")

    def test_malformed_signed_state_fails_instead_of_scanning(self):
        session = _Session(sign_payload={"success": False, "message": "服务器内部错误"})
        result, session, _, _ = self._run(self._items(("甲站", "20000000000001")), session)

        self.assertFalse(result["ok"])
        self.assertIn("FIND_WAYBILL_SIGN_STATE 响应结构异常", result["message"])
        self.assertEqual(session.uploads, [])

    def test_rejected_upload_fails_and_keeps_earlier_station_results(self):
        session = _Session(saves=[{"success": True, "message": "ok"}, {"success": False, "message": "单号重复"}])
        result, session, _, _ = self._run(
            self._items(("甲站", "20000000000001"), ("乙站", "20000000000002")), session
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "upload")
        self.assertIn("上传失败: 单号重复", result["message"])
        self.assertEqual([item["station_name"] for item in result["detail"]["stations"]], ["甲站"])

    def test_missing_login_identity_fails_before_page_access(self):
        info = {**USER_INFO, "loginSiteCode": ""}
        result, session, _, page_context = self._run(self._items(("甲站", "20000000000001")), user_info=info)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "login_context")
        self.assertIn("loginSiteCode", result["message"])
        page_context.assert_not_called()
        self.assertEqual(session.calls, [])

    def test_scan_user_code_longer_than_page_limit_is_rejected(self):
        info = {**USER_INFO, "loginUserAccount": "账" * 7}
        result, session, _, _ = self._run(self._items(("甲站", "20000000000001")), user_info=info)

        self.assertFalse(result["ok"])
        self.assertIn("扫描员编码超过 20 字节: 21", result["message"])
        self.assertEqual(session.uploads, [])

    def test_duplicate_and_invalid_items_are_rejected(self):
        result, _, auth, _ = self._run(self._items(("甲站", "20000000000001"), ("乙站", "20000000000001")))
        self.assertFalse(result["ok"])
        self.assertIn("重复单号", result["message"])
        auth.assert_not_called()

        result, _, _, _ = self._run([{"station_name": "甲站"}])
        self.assertFalse(result["ok"])
        self.assertIn("items 存在格式错误项，索引: 0", result["message"])


if __name__ == "__main__":
    unittest.main()
