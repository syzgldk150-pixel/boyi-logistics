"""Original-site labels: JS semantics, template parsing, the Ronghui port and the print record (synthetic data)."""

import datetime as dt

import pytest

from agent.tms_runtime import platform_waybill_print as printing
from agent.tms_runtime.direct_business import DirectBusinessError

NOW = dt.datetime(2026, 1, 2, 9, 5, 7, tzinfo=printing.CHINA_TZ)


def test_js_string_and_first_replace_follow_the_page_script():
    assert [printing.js_string(value) for value in (None, True, 2, 2.0, 2.57, "a")] == [
        "null", "true", "2", "2", "2.57", "a"]
    # String.prototype.replace with a string pattern: first occurrence only, "$" codes expanded.
    assert printing.js_replace_first('"Id" "Id1" "Id"', "Id", "9") == '"9" "Id1" "Id"'
    assert printing.js_replace_first("a-K-b", "K", "[$&|$$|$`|$'|$1]") == "a-[K|$|a-|-b|$1]-b"
    assert printing.js_replace_first("abc", "x", "y") == "abc"


def test_template_is_parsed_into_literal_calls_and_rejects_other_code():
    calls = printing.parse_lodop_template(
        'LODOP.PRINT_INITA(1,-4,280,351,"主单");\nLODOP.SET_SHOW_MODE ("HIDE_PBUTTIN_PREVIEW",true);\n'
        "LODOP.ADD_PRINT_TEXT(14,63.5,137,20,'a\\'b');\nLODOP.NEWPAGE();\n")
    assert calls == [["PRINT_INITA", [1, -4, 280, 351, "主单"]],
                     ["SET_SHOW_MODE", ["HIDE_PBUTTIN_PREVIEW", True]],
                     ["ADD_PRINT_TEXT", [14, 63.5, 137, 20, "a'b"]], ["NEWPAGE", []]]
    for bad in ('LODOP.PRINT_INITA(1);alert(1);', 'LODOP.ADD_PRINT_TEXT(1,x);', 'window.LODOP=1;'):
        with pytest.raises(DirectBusinessError):
            printing.parse_lodop_template(bad)
    with pytest.raises(DirectBusinessError, match="PRINT"):
        printing._check_calls([["PRINT_INITA", []], ["PRINT", []]])
    with pytest.raises(DirectBusinessError):
        printing._check_calls([["ADD_PRINT_TEXT", [1, 1, 1, 1, "x"]]])


def test_yunda_label_replaces_keys_in_data_order_and_keeps_the_preview_print_button():
    template = ('LODOP.PRINT_INITA(1,-4,280,351,"主单打印");\nLODOP.SET_SHOW_MODE ("HIDE_PBUTTIN_PREVIEW",true);\n'
                'LODOP.ADD_PRINT_TEXT(14,63,137,20,"Logistics_Id");\n'
                'LODOP.ADD_PRINT_BARCODE(47,46,236,30,"128Auto","Logistics_Id1");\n'
                'LODOP.ADD_PRINT_TEXT(14,25,27,20,"Mark_Type");\n')
    calls = printing.yunda_label_calls({"Logistics_Id": "900000001", "Mark_Type": "签", "Logistics_Id1": "900000001"},
                                       template)
    assert calls == [["PRINT_INITA", [1, -4, 280, 351, "主单打印"]],
                     ["ADD_PRINT_TEXT", [14, 63, 137, 20, "900000001"]],
                     ["ADD_PRINT_BARCODE", [47, 46, 236, 30, "128Auto", "900000001"]],
                     ["ADD_PRINT_TEXT", [14, 25, 27, 20, "签"]]]


ROW = {
    "BILL_CODE": "R00000000001", "DISPATCH_MODE": "派送", "BL_UPSTAIRS": 1, "GOODS_TYPE": "重货",
    "BL_INSURESTATUS": None, "BOROUGH": None, "REGISTER_DATE": "2026-01-01 17:46:16",
    "CONTACT_PHONE": "00000000000", "SEND_MAN": "合成寄件人甲乙", "SEND_MAN_PHONE": "00000000001",
    "SEND_MAN_PHONE_EXT": None, "SEND_MAN_ADDRESS": "测试省测试市", "ACCEPT_MAN": "合成收件人",
    "ACCEPT_MAN_PHONE": "00000000002", "ACCEPT_MAN_PHONE_EXT": "8", "ACCEPT_MAN_ADDRESS": "测试路1号",
    "WAITNOTIFY_SEND_TEXT": "否", "WAITNOTIFY_SEND_STATUS_TEXT": None, "WAITNOTIFY_SEND": 1,
    "BL_INTO_WAREHOUSE": 0, "GOODS_PAYMENT": None, "R_BILLCODE": "", "TOPAYMENT": 5, "BL_GOOD_PRINT": None,
    "INSURANCE": 15000, "PACK_TYPE": "托盘", "PIECE_NUMBER": 5, "FREIGHT": 100, "SETTLEMENT_WEIGHT": 2145,
    "VOLUME": 2.57, "REMARK": "合成备注", "BILL_WEIGHT": 2145, "PRODUCT_TYPE": "精准零担", "GOODS_NAME": "合成货物名称",
}


def test_ronghui_label_data_matches_print_bill_new_defaults():
    data = printing.ronghui_label_data(dict(ROW), NOW)
    assert data["DISPATCH_MODE"] == "送货上楼"
    assert data["SEND_MAN"] == "委托人：合成寄件人"  # first five characters
    assert data["ACCEPT_MAN_PHONE"] == "联系方式：00000000002 8"
    assert data["GOODS_NAME"] == "物品名称：合成货物"
    assert data["FREIGHT"] == " "  # 页面默认不打印运费
    assert data["NOW"] == "打印时间:2026/01/02 09:05:07"
    assert data["REGISTER_DATE"] == "录单时间:2026/01/01 17:46:16"
    assert data["VOLUME"] == "体积：2.57" and data["INSURANCE"] == "保价金额:15000"
    assert data["GOODS_PAYMENT"] == "代收金额:0"
    # An empty 回单号 is still “not null” on the page, so 回 is marked.
    assert data["r3Arr"] == ["等", "回", "到"]


def test_waiting_release_masks_the_receiver_like_the_page():
    data = printing.ronghui_label_data({**ROW, "WAITNOTIFY_SEND_TEXT": "是", "WAITNOTIFY_SEND_STATUS_TEXT": "未放货"}, NOW)
    assert data["ACCEPT_MAN"] == "收件人：合****"
    assert data["ACCEPT_MAN_PHONE"] == "联系方式：000******** 8"
    assert data["ACCEPT_MAN_ADDRESS"] == "测试路1*"


def test_ronghui_calls_draw_the_customer_copy():
    data = printing.ronghui_label_data({**ROW, "BL_INTO_WAREHOUSE": 1, "GOODS_PAYMENT": 3, "BL_GOOD_PRINT": 1}, NOW)
    calls = printing.ronghui_label_calls(data, xcx_image="data:image/jpeg;base64,AAAA", site_type="二级网点")
    assert calls[:2] == [["PRINT_INITA", ["0mm", "0mm", "80mm", "100mm", "print"]],
                         ["SET_PRINT_PAGESIZE", [0, "75mm", "90mm", "CreateCustomPage"]]]
    assert calls[-1] == ["SET_PREVIEW_WINDOW", [1, 2, 0, 550, 800, ""]]
    texts = [call[1][4] for call in calls if call[0] == "ADD_PRINT_TEXT"]
    assert texts[0] == "客户联" and "发货联系网点:00000000000" in texts
    # Six marks: the page draws the fifth twice and never the sixth.
    marks = [call[1][1] for call in calls if call[0] == "ADD_PRINT_HTM" and call[1][0] == 43]
    assert marks == [7.5, 30, 52, 74, 96, 118]
    assert ["ADD_PRINT_BARCODE", [200, 20, 160, 25, "128A", "R00000000001"]] in calls
    printing._check_calls(calls)
    without_site = printing.ronghui_label_calls(data, xcx_image="data:image/jpeg;base64,AAAA", site_type="分拨中心")
    assert "发货联系网点:00000000000" not in [call[1][4] for call in without_site if call[0] == "ADD_PRINT_TEXT"]


def test_changed_page_code_stops_printing():
    with pytest.raises(DirectBusinessError) as error:
        printing.ronghui_print_code("function printBillNew(){}\nvar xcxImg='';\nfunction printKH(){}\nfunction searchMethod(){}")
    assert error.value.code == "PLATFORM_PRINT_CODE_CHANGED"


def test_print_count_row_is_the_page_update_bill_entry():
    info = {"loginEmpName": "合成员工", "loginEmpCode": "E1", "loginSiteName": "合成网点", "loginSiteCode": "S1"}
    row = printing.ronghui_print_count_row("R00000000001", info, NOW)
    assert row == {"MAIN_BILL_CODE": "R00000000001", "BILL_CODE": "R00000000001", "CREATE_MAN": "合成员工",
                   "CREATE_MAN_CODE": "E1", "CREATE_SITE": "合成网点", "CREATE_SITE_CODE": "S1",
                   "CREATE_DATE": "2026-01-02T01:05:07.000Z", "BILL_TYPE": "主单", "PRINT_TYPE": "寄件存根联",
                   "DATA_FROM": "K13", "PAGE_FROM": "主面单打印"}
    with pytest.raises(DirectBusinessError):
        printing.ronghui_print_count_row("R00000000001", {**info, "loginEmpCode": ""}, NOW)


class _Response:
    def __init__(self, payload):
        self.payload, self.content, self.status_code = payload, b"x", 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, counts):
        self.counts, self.saved = list(counts), []

    def post(self, url, *, params=None, data=None, files=None, headers=None, timeout=None):
        if url == printing.RONGHUI_SAVE_TABLES_URL:
            self.saved.append(files["params"][1])
            return _Response({"success": True})
        return _Response([{"BILL_CODE": data["BILL_CODE"], "PRINT_NUM": self.counts.pop(0)}])


@pytest.mark.parametrize("after, verified", [(2, True), (1, False)])
def test_print_record_is_verified_by_the_print_count(monkeypatch, after, verified):
    session = _Session([1, after])
    monkeypatch.setattr(printing, "_ronghui_session", lambda profile: session)
    monkeypatch.setattr(printing, "_ronghui_print_page", lambda s, t: {
        "url": "https://tms.ronghuiwl.com/widget/home", "authentication_key": "k", "page_id": "p", "html": ""})
    monkeypatch.setattr(printing, "ronghui_print_code", lambda html: "data:image/jpeg;base64,AAAA")
    monkeypatch.setattr(printing, "_read_user_info", lambda s: {
        "loginEmpName": "合成员工", "loginEmpCode": "E1", "loginSiteName": "合成网点", "loginSiteCode": "S1"})
    result = printing.ronghui_record_print("price_default", "R00000000001", timeout_sec=5)
    assert result["verified"] is verified and result["print_count"] == after
    assert '"operationKey":"TAB_PRINT_COUNT_ADD"' in session.saved[0]


@pytest.mark.parametrize("params", [
    {"platform": "best", "waybill_no": "900000001", "account_id": "a", "open_date": "2026-01-02"},
    {"platform": "yunda", "waybill_no": "900000001", "account_id": "a", "open_date": ""},
    {"platform": "ronghui", "waybill_no": "R 1", "account_id": "a", "open_date": ""},
    {"platform": "ronghui", "waybill_no": "R00000000001", "account_id": "", "open_date": ""},
    {"platform": "ronghui", "waybill_no": "R00000000001", "account_id": "a"},
])
def test_only_platform_number_account_and_date_are_accepted(params):
    with pytest.raises(DirectBusinessError):
        printing._validated(params)
