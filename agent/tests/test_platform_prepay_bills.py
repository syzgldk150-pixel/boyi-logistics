"""融辉/韵达对账单原样读取：参数边界、页面列解析、平台响应解包（虚构数据）。"""

import json
from types import SimpleNamespace

import pytest

from agent.tms_runtime import platform_prepay_bills as bills
from agent.tms_runtime.direct_business import DirectBusinessError

PAGE = """
<div viewtype="miniDatagrid" id="datagridTJ" url="/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_TJ_WST">
 <div property="columns">
  <div type="indexcolumn" width="70px"></div>
  <div header="财务中心" field="CENTER_NAME" width="1"></div>
  <div header="网点CODE" field="SITE_NAME_CODE" visible="false"></div>
  <div header="结算类型" field="BALANCE_TYPE"></div>
  <div header="结算金额" field="BALANCE_CUR_MONEY" dataType="float"></div>
 </div>
</div>
<div viewtype="miniDatagrid" id="datagridTJDetails" url="/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_TJ_DETAIL">
 <div header="运单编号" field="BILL_CODE"></div><div header="前余额 &amp; 后" field="BALANCE_PRE_CONFIRM_MONEY"></div>
</div>
<div viewtype="miniCombobox" id="other"><div header="网点编号" field="SITE_CODE"></div></div>
<div viewtype="miniDatagrid" id="accountDatagrid" url="/dataQuery/findPageByCallId?id=FIND_BALANCE_ACCOUNT_INFO">
 <div header="目前结算余额" field="CUR_MONEY"></div></div>
<div viewtype="miniDatagrid" id="datagrid" url="/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_WST">
 <div header="流水号" field="BALANCE_ORDER"></div></div>
<script>setUrl('/dataQuery/findPageByCallId?id=FIND_BALANCE_QRY_WST_WITH_SITE')</script>
"""


def _query(**changes):
    params = {"platform": "ronghui", "account_id": "price_default", "action": "summary",
              "start": "2026-01-01", "end": "2026-01-31"}
    params.update(changes)
    return params


def test_validation_limits_accounts_span_and_keys():
    assert bills.validate_query(_query())["date_type"] == "BALANCE_DATE"
    for bad in (_query(account_id="ronghui_default"), _query(end="2026-02-01"), _query(start="2026-02-01"),
                _query(action="delete"), _query(extra="1"), _query(platform="best"),
                _query(action="summary_detail", center_code="1", site_code="2", balance_type="")):
        with pytest.raises(DirectBusinessError):
            bills.validate_query(bad)
    detail = bills.validate_query(_query(action="summary_detail", center_code="66666", site_code="10001",
                                         balance_type="收某费", page="2", page_size="500"))
    assert (detail["page"], detail["page_size"], detail["balance_type"]) == (2, 500, "收某费")
    balance = bills.validate_query({"platform": "ronghui", "account_id": "ronghui_daxiang_s", "action": "balance"})
    assert "start" not in balance
    yunda = bills.validate_query({"platform": "yunda", "account_id": "yunda_default", "action": "detail",
                                  "start": "2026-01-01", "end": "2026-01-02",
                                  "filters": {"account": 123, "sub_data_display": "21", "origin": None}})
    assert yunda["filters"] == {"account": "123", "sub_data_display": "21"}
    with pytest.raises(DirectBusinessError):
        bills.validate_query({"platform": "yunda", "account_id": "yunda_default", "action": "detail",
                              "start": "2026-01-01", "end": "2026-01-02", "filters": {"menu_code": "x"}})
    assert bills.validate_query({"platform": "yunda", "account_id": "yunda_default", "action": "branches"})["page"] == 1


def test_ronghui_columns_follow_the_page_grid_only():
    assert bills.ronghui_columns(PAGE, "datagridTJ") == [
        {"field": "CENTER_NAME", "title": "财务中心"}, {"field": "BALANCE_TYPE", "title": "结算类型"},
        {"field": "BALANCE_CUR_MONEY", "title": "结算金额"}]
    assert bills.ronghui_columns(PAGE, "datagridTJDetails")[1]["title"] == "前余额 & 后"
    with pytest.raises(DirectBusinessError, match="页面已变化"):
        bills.ronghui_columns(PAGE, "missingGrid")


class _Response:
    def __init__(self, payload, url=""):
        self.text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.url = url
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(self.text)


class _RonghuiSession:
    def __init__(self, pages):
        self.pages, self.posts = pages, []
        self.cookies = []

    def get(self, url, **_):
        if url.endswith("/menuTreeExtend/loadMenu"):
            return _Response({"result": {"data": [{"text": "预付款管理", "children": [
                {"text": "结算明细查询", "url": "/widget/home?authenticationKey=K1&pageId=P1"}]}]}})
        return _Response(PAGE)

    def post(self, url, *, headers, data, timeout):
        self.posts.append((url, headers, dict(data)))
        return _Response(self.pages.pop(0))


def _ronghui(monkeypatch, pages):
    session = _RonghuiSession(pages)
    monkeypatch.setattr(bills, "get_session_broker", lambda profile: SimpleNamespace(
        open_capability_session=lambda capability, validate: session))
    monkeypatch.setattr(bills, "_read_user_info", lambda _: {"loginSiteCode": "10001", "loginSiteName": "示例网点"})
    return session


def test_ronghui_summary_reads_all_pages_and_keeps_decimals_as_text(monkeypatch):
    session = _ronghui(monkeypatch, [
        '{"total": 2, "data": [{"BALANCE_TYPE": "收甲费", "BALANCE_CUR_MONEY": -1100.80}], "totalInfo": {"COUNT": 2}}',
        '{"total": 2, "data": [{"BALANCE_TYPE": "收乙费", "BALANCE_CUR_MONEY": 12}]}'])
    monkeypatch.setattr(bills, "MAX_PAGES", 5)
    query = bills.validate_query(_query(date_type="FINANCE_DATE"))
    result = bills.ronghui_bills("p", query, timeout_sec=5)
    assert [row["BALANCE_CUR_MONEY"] for row in result["rows"]] == ["-1100.80", 12]
    assert (result["total"], result["site_name"], result["footer"]) == (2, "示例网点", {})
    url, headers, form = session.posts[0]
    assert url.endswith("?id=FIND_BALANCE_QRY_TJ_WST") and headers["pageId"] == "P1"
    assert form["DATE_TYPE"] == "FINANCE_DATE" and json.loads(form["FINANCE_DATE"])["end"] == "2026-01-31 23:59:59"
    assert [post[2]["pageIndex"] for post in session.posts] == [0, 1]


def test_ronghui_detail_is_paged_and_platform_rejection_is_reported(monkeypatch):
    session = _ronghui(monkeypatch, ['{"total": 900, "data": [{"BALANCE_ORDER": 1}]}'])
    query = bills.validate_query(_query(action="detail", page="3", page_size="200"))
    result = bills.ronghui_bills("p", query, timeout_sec=5)
    assert (result["page"], result["page_size"], result["total"]) == (3, 200, 900)
    assert session.posts[0][0].endswith("FIND_BALANCE_QRY_WST_WITH_SITE")
    assert session.posts[0][2]["SITE_NAME_CODE"] == "10001" and session.posts[0][2]["pageIndex"] == 2
    _ronghui(monkeypatch, ['{"success": false, "message": "非法的请求\\r\\n"}'])
    with pytest.raises(DirectBusinessError, match="非法的请求"):
        bills.ronghui_bills("p", bills.validate_query(_query()), timeout_sec=5)
    _ronghui(monkeypatch, ['{"rows": []}'])
    with pytest.raises(ValueError):
        bills.ronghui_bills("p", bills.validate_query(_query()), timeout_sec=5)


def test_yunda_columns_keep_drill_targets():
    columns = bills.yunda_columns([
        {"field": "trade_date", "title": "交易时间", "fieldHide": 0},
        {"field": "hidden", "title": "隐藏", "fieldHide": 1},
        {"field": "operation_sec", "title": "操作", "fieldHide": 0, "skipFieldStatus": 1,
         "skipData": '{"start_time": "", "branch": "", "account": ""}',
         "skipTab": [{"skip_tab": "ac_trade_detail_es"}]},
        {"title": "金额", "childList": [{"field": "income", "title": "收入"}]},
    ])
    assert columns == [
        {"field": "trade_date", "title": "交易时间"},
        {"field": "operation_sec", "title": "操作",
         "drill": {"tab": "ac_trade_detail_es", "keys": ["start_time", "branch", "account"]}},
        {"field": "income", "title": "金额/收入"}]


def _wrap(table):
    text = table if isinstance(table, str) else json.dumps(table, ensure_ascii=False)
    inner = json.dumps({"success": "true", "dataMap": text, "msg": "成功"},
                       ensure_ascii=False)
    return {"success": True, "dataMap": json.dumps({"rows": inner}, ensure_ascii=False), "code": 200}


class _YundaSession:
    def __init__(self, data_response, landed="https://ky-appept.yunda56.com/export/public/index.html#/test"):
        self.data_response, self.landed, self.posts = data_response, landed, []

    def get(self, url, **_):
        if "logins?" in url:
            return _Response("<html></html>", url=self.landed)
        return _Response({"success": True, "dataMap": {"username": 900001}})

    def post(self, url, *, headers, timeout, files):
        message = json.loads(files["message"][1])
        self.posts.append((url.rsplit("/", 1)[-1], message))
        if url.endswith("selectDynamicFileds"):
            return _Response({"success": True, "dataMap": json.dumps(
                {"rows": [{"field": "income", "title": "收入", "fieldHide": 0}]})})
        return _Response(self.data_response)


def _yunda(monkeypatch, session):
    monkeypatch.setattr(bills, "get_session_broker", lambda profile: SimpleNamespace(
        open_capability_session=lambda capability, validate: session))


def test_yunda_trade_query_unwraps_nested_json_and_sends_filters(monkeypatch):
    session = _YundaSession(_wrap('{"total": 3, "footer": ["收入:", "10.50", "支出:", 2],'
                                  ' "rows": [{"income": 10.50}]}'))
    _yunda(monkeypatch, session)
    query = bills.validate_query({"platform": "yunda", "account_id": "yunda_default", "action": "daily",
                                  "start": "2026-01-01", "end": "2026-01-31", "filters": {"account": "77"}})
    result = bills.yunda_bills("yunda", query, timeout_sec=5)
    assert result["rows"] == [{"income": "10.50"}] and result["footer"][1] == "10.50"
    assert (result["total"], result["user_id"]) == (3, "900001")
    name, message = session.posts[-1]
    inner = json.loads(message["message"])
    assert name == "selectInterface" and message["tab_code"] == "ac_trade_sum_day"
    assert (inner["start_time"], inner["end_time"], inner["account"], inner["userId"]) == (
        "2026-01-01", "2026-01-31", "77", "900001")


def test_yunda_branches_and_failures(monkeypatch):
    session = _YundaSession({"success": True, "dataMap": json.dumps({"total": 1, "rows": [{"account": 1}]})})
    _yunda(monkeypatch, session)
    result = bills.yunda_bills("yunda", bills.validate_query(
        {"platform": "yunda", "account_id": "yunda_default", "action": "branches"}), timeout_sec=5)
    assert result["rows"] == [{"account": 1}]
    sent = session.posts[-1][1]
    assert sent["interface_skip"] == 0 and sent["auth_type"] == " " and sent["workType"] == "branch"

    rejected = {"success": True, "dataMap": json.dumps({"rows": json.dumps(
        {"success": False, "dataMap": "只可查询最长31天数据", "msg": "请求失败"}, ensure_ascii=False)}, ensure_ascii=False)}
    _yunda(monkeypatch, _YundaSession(rejected))
    query = bills.validate_query({"platform": "yunda", "account_id": "yunda_default", "action": "summary",
                                  "start": "2026-01-01", "end": "2026-01-02"})
    with pytest.raises(DirectBusinessError, match="31天"):
        bills.yunda_bills("yunda", query, timeout_sec=5)
    _yunda(monkeypatch, _YundaSession({}, landed="https://ky-sso.yunda56.com/login"))
    with pytest.raises(DirectBusinessError, match="登录已失效"):
        bills.yunda_bills("yunda", query, timeout_sec=5)
