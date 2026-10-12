"""Synthetic examples for Ronghui's live-page quote contract; no credentials."""

import copy
import json

import pytest

from service_v2_plugins.quote_prices_v2.payload.common import QuoteError
from service_v2_plugins.quote_prices_v2.payload.ronghui import _sum_fees, quote


INPUT = {"address": "测试省测试市测试区测试街1号", "weight": "1000", "volume": "1"}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.responses = {
            "ronghui_address": {"success": True, "data": {"code": "S001", "result": [{
                "geoResult": {"province": "测试省", "city": "测试市", "county": "测试区", "town": "测试街"},
                "areaResults": [{"areaName": "测试候选甲"}, {"areaName": "测试目的网点"}],
            }]}},
            "ronghui_match_site": {"success": True, "result": {"p_match_index": 2.0, "p_match_name": "测试目的网点"}},
            "ronghui_destination_name": [{"DESTINATION_NAME": "测试目的网点", "DESTINATION_CODE": "D-TEST", "TYPE": "一级网点"}],
            "ronghui_destination": [{
                "DESTINATION_NAME": "测试目的网点", "DESTINATION_CODE": "D-TEST", "SITE_CODE": "DS-TEST",
                "SITE_NAME": "测试派件网点", "SITE_TYPE": "一级网点", "LEVELS": "一级网点",
                "SUPERIOR_FINANCE_CENTER_CODE": "DF-TEST", "SUPERIOR_FINANCE_CENTER": "测试派件财务中心",
            }],
            "ronghui_site_info": [{
                "SITE_CODE": "DS-TEST", "SITE_NAME": "测试派件网点", "ADDRESS": "测试路88号",
                "PHONE": "010-00000001", "MANAGER_PHONE": "010-00000002",
                "DISPATCH_RANGE": "测试街道", "SPECSERVICE_RANGE": "测试园区需预约",
                "NOT_DISPATCH_RANGE": "测试山村", "EXCEED_TOWN": "测试镇",
                "EXCEED_TOWN_DETAIL": "测试镇须联系网点确认费用", "TIME_OF_ARRIVAL": "隔日",
            }],
            "ronghui_send_site": [{"SITE_CODE": "S-TEST", "SITE_NAME": "测试寄件网点", "PROVINCE": "测试省", "SUPERIOR_FINANCE_CENTER_CODE": "SF-TEST", "SUPERIOR_FINANCE_CENTER": "测试寄件财务中心"}],
            "ronghui_send_center": [{"SITE_CODE": "SC-TEST", "SITE_NAME": "测试寄件中心"}],
            "ronghui_destination_hub": [],
            "ronghui_destination_center": [{"SITE_CODE": "DC-TEST", "SITE_NAME": "测试目的中心"}],
            "ronghui_route": [{"ROUTE_NAME": "测试路线"}],
            "ronghui_weight_ratio": [{"WEIGHT_RATIO": 5000}],
            "ronghui_goods_type": [{"GOODS_CODE": "G-TEST", "GOODS_NAME": "测试货物"}],
            "ronghui_insurance": [{"MIN_LNSURED_FEE": 2000, "MAX_LNSURED_FEE": 50000}],
            "ronghui_quote": {"success": True, "result": {"data": [{"TRANSPORT_FEE": 120, "TRANSFER_FEE_ADD": -5, "REC_PERIOD_FEE_DIS": 7, "INSURANCE_FEE": 3, "INSURANCE": 2000}]}},
        }

    def context(self):
        return {"loginEmpCode": "EMP-TEST", "loginEmpName": "测试员工", "loginSiteCode": "S-TEST", "loginSiteName": "测试寄件网点"}

    def request(self, name, body):
        self.calls.append((name, copy.deepcopy(body)))
        response = self.responses[name]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


def test_quote_matches_live_contract_not_retired_price_procedure():
    client = FakeClient()
    result = quote(client, INPUT)
    assert len(result["quotes"]) == 6
    assert all(row["amount"] == "111.00" for row in result["quotes"])
    assert {row["product_name"] for row in result["quotes"]} == {"精准零担", "融安达", "融速达"}
    selected = next(body for name, body in client.calls if name == "ronghui_destination_name")
    assert selected == {"DESTINATION_NAME": "测试目的网点"}
    requests = [body for name, body in client.calls if name == "ronghui_quote"]
    assert all(body["procedureName"] == "P_CALC_CLIENT_PRICE_SHOW4" for body in requests)
    payload = json.loads(requests[0]["data"])[0]
    assert payload["TON_SQUARE"] == "1.00"
    assert payload["INSURANCE"] == "2000"
    assert payload["WEIGHT"] == "1000.00"
    assert payload["VOLUME_WEIGHT"] == "200.00"
    assert payload["REC_MAN_CODE"] == "EMP-TEST"
    assert payload["ACCEPT_COUNTY"] == "测试省|测试市|测试区"
    assert payload["TOWN_NAME"] == "测试市-测试区-测试街"
    assert payload["DISP_UNDERLING_SITE_CODE"] == "DS-TEST"


@pytest.mark.parametrize("weight,volume,count", [(800, 1, 4), (800.01, 1, 6), (3000, 1, 6), (3000.01, 1, 8), (10, 20, 8)])
def test_product_bands_use_settlement_weight(weight, volume, count):
    assert len(quote(FakeClient(), dict(INPUT, weight=str(weight), volume=str(volume)))["quotes"]) == count


@pytest.mark.parametrize("endpoint", ["ronghui_destination_name", "ronghui_destination", "ronghui_send_site", "ronghui_send_center", "ronghui_destination_center", "ronghui_route", "ronghui_weight_ratio", "ronghui_goods_type", "ronghui_insurance"])
def test_empty_essential_lookup_does_not_use_default_or_old_result(endpoint):
    client = FakeClient()
    client.responses[endpoint] = []
    with pytest.raises(QuoteError, match="无数据"):
        quote(client, INPUT)
    assert not any(name == "ronghui_quote" for name, _ in client.calls)


@pytest.mark.parametrize("endpoint", ["ronghui_destination_name", "ronghui_destination", "ronghui_send_center", "ronghui_destination_center", "ronghui_route"])
def test_ambiguous_lookup_does_not_take_first(endpoint):
    client = FakeClient()
    client.responses[endpoint] *= 2
    with pytest.raises(QuoteError, match="不唯一"):
        quote(client, INPUT)


@pytest.mark.parametrize("index", [0, -1, 3, 1.5, 2.00000001, float("nan"), float("inf"), float("-inf"), True, "2"])
def test_invalid_authoritative_area_index_is_rejected(index):
    client = FakeClient()
    client.responses["ronghui_match_site"]["result"]["p_match_index"] = index
    with pytest.raises(QuoteError, match="服务网点"):
        quote(client, INPUT)


@pytest.mark.parametrize("literal", ["2", "2.0"])
def test_source_json_integral_number_selects_the_authoritative_area(literal):
    client = FakeClient()
    client.responses["ronghui_match_site"] = json.loads(
        '{"success":true,"result":{"p_match_index":' + literal + ',"p_match_name":"测试目的网点"}}'
    )
    index = client.responses["ronghui_match_site"]["result"]["p_match_index"]
    assert type(index) is (float if literal == "2.0" else int)
    result = quote(client, INPUT)
    assert len(result["quotes"]) == 6
    selected = next(body for name, body in client.calls if name == "ronghui_destination_name")
    assert selected == {"DESTINATION_NAME": "测试目的网点"}


def test_area_index_and_name_must_agree():
    client = FakeClient()
    client.responses["ronghui_match_site"]["result"]["p_match_name"] = "测试候选甲"
    with pytest.raises(QuoteError, match="不一致"):
        quote(client, INPUT)


def test_explicit_hub_query_skips_unneeded_centre_query():
    client = FakeClient()
    client.responses["ronghui_destination_hub"] = [{"DESTINATION_CENTER_CODE": "H-TEST", "DESTINATION_CENTER": "测试指定中心"}]
    quote(client, INPUT)
    assert not any(name == "ronghui_destination_center" for name, _ in client.calls)
    route = next(body for name, body in client.calls if name == "ronghui_route")
    assert route["DESTINATION_CENTER_CODE"] == "H-TEST"


@pytest.mark.parametrize("level,mode", [("自提部", "自提"), ("直派部", "派送")])
def test_destination_delivery_mode_constraints(level, mode):
    client = FakeClient()
    client.responses["ronghui_destination"][0]["LEVELS"] = level
    result = quote(client, INPUT)
    assert len(result["quotes"]) == 3
    assert {row["delivery_method"] for row in result["quotes"]} == {mode}


@pytest.mark.parametrize("level", ["分拨服务大厅", "分拨代派部"])
def test_required_special_service_is_not_guessed(level):
    client = FakeClient()
    client.responses["ronghui_destination"][0]["LEVELS"] = level
    with pytest.raises(QuoteError, match="专属配送服务") as caught:
        quote(client, INPUT)
    assert "匹配网点：测试派件网点" in caught.value.details
    assert "特殊区域：测试园区需预约" in caught.value.details
    assert not any(name == "ronghui_quote" for name, _ in client.calls)


def test_site_details_are_bound_to_the_same_underling_as_each_quote():
    client = FakeClient()
    result = quote(client, INPUT)
    assert result["site_name"] == "测试派件网点"
    assert {row["site_name"] for row in result["quotes"]} == {"测试派件网点"}
    assert result["details"] == [
        "匹配网点：测试派件网点", "门店地址：测试路88号", "查询电话：010-00000001",
        "经理电话：010-00000002", "派送范围：测试街道", "特殊区域：测试园区需预约",
        "不派送范围：测试山村", "超远乡镇：测试镇", "超远乡镇详情：测试镇须联系网点确认费用",
        "到件时效：隔日",
    ]
    assert ("ronghui_site_info", {"SITE_CODE": "DS-TEST"}) in client.calls


def test_second_level_details_use_original_page_underling_not_finance_centre():
    client = FakeClient()
    client.responses["ronghui_destination"][0].update({
        "SITE_TYPE": "二级网点", "ENABLE_ACCOUNT_PURPOSE": 1, "BL_PURPOSE_SITE": 1,
        "SUPERIOR_SITE_CODE": "US-TEST", "SUPERIOR_SITE": "测试所属网点",
        "FINANCE_CENTER_CODE": "UF-TEST", "FINANCE_CENTER": "测试财务中心",
    })
    client.responses["ronghui_site_info"][0].update(SITE_CODE="US-TEST", SITE_NAME="测试所属网点")
    result = quote(client, INPUT)
    assert result["site_name"] == "测试所属网点"
    assert ("ronghui_site_info", {"SITE_CODE": "US-TEST"}) in client.calls


@pytest.mark.parametrize("value", [None, "", "  ", "0", 0, 0.0, False])
def test_missing_site_field_is_no_data_not_no_special_area(value):
    client = FakeClient()
    client.responses["ronghui_site_info"][0]["SPECSERVICE_RANGE"] = value
    result = quote(client, INPUT)
    assert "特殊区域：无数据" in result["details"]
    assert len(result["quotes"]) == 6


def test_explicit_no_special_area_is_displayed_only_when_source_says_so():
    client = FakeClient()
    client.responses["ronghui_site_info"][0]["SPECSERVICE_RANGE"] = "无"
    assert "特殊区域：无" in quote(client, INPUT)["details"]


@pytest.mark.parametrize("response,reason", [
    ([], "融辉网点资料无数据"),
    ([{"SITE_CODE": "OTHER-TEST", "ADDRESS": "不得显示的其他网点地址"}], "融辉网点资料与匹配网点不一致"),
    ([{"SITE_CODE": "DS-TEST"}, {"SITE_CODE": "DS-TEST"}], "融辉网点资料匹配不唯一"),
    ({"success": False}, "融辉网点资料返回格式无效"),
    (RuntimeError("BLOCKED_LOGIN"), "账号未登录或登录已失效"),
    (RuntimeError("transport-secret-must-not-appear"), "平台资料请求失败，请稍后重试"),
])
def test_site_detail_failure_keeps_valid_quotes_without_substituting_data(response, reason):
    client = FakeClient()
    client.responses["ronghui_site_info"] = response
    result = quote(client, INPUT)
    assert len(result["quotes"]) == 6
    assert all(row["amount"] == "111.00" for row in result["quotes"])
    assert "门店地址：无数据" in result["details"]
    assert "特殊区域：无数据" in result["details"]
    assert f"网点资料查询失败：{reason}" in result["details"]
    assert "不得显示" not in str(result["details"])
    assert "transport-secret" not in str(result["details"])


def test_quote_calculation_failure_keeps_matched_site_details():
    client = FakeClient()
    client.responses["ronghui_quote"] = {"success": False}
    with pytest.raises(QuoteError, match="未成功") as caught:
        quote(client, INPUT)
    assert "查询电话：010-00000001" in caught.value.details
    assert "特殊区域：测试园区需预约" in caught.value.details


@pytest.mark.parametrize("value", [None, False, 0, -1, "NaN", "Infinity"])
def test_invalid_volume_ratio_is_not_replaced_with_5000(value):
    client = FakeClient()
    client.responses["ronghui_weight_ratio"][0]["WEIGHT_RATIO"] = value
    with pytest.raises(QuoteError):
        quote(client, INPUT)


def test_failed_price_response_does_not_produce_zero():
    client = FakeClient()
    client.responses["ronghui_quote"] = {"success": False, "result": {"data": []}}
    with pytest.raises(QuoteError, match="未成功"):
        quote(client, INPUT)


def test_fee_sum_matches_page_signed_adjustment_and_six_discounts():
    assert _sum_fees({"TRANSPORT_FEE": 50, "TRANSFER_FEE_ADD": -5, "REC_PERIOD_FEE_DIS": 7, "INSURANCE": 3000, "TRANSPORT_ALIEN_FEE": None}) == "38.00"
    assert _sum_fees({"TRANSPORT_FEE": 0}) == "0.00"


@pytest.mark.parametrize("row", [{}, {"INSURANCE": 3000}, {"TRANSPORT_FEE": None}, {"TRANSPORT_FEE": ""}, {"TRANSPORT_FEE": True}, {"TRANSPORT_FEE": "NaN"}, {"TRANSPORT_FEE": 5, "REC_PERIOD_FEE_DIS": 8}, {"NEW_FEE": 3}])
def test_invalid_or_changed_fee_shape_fails(row):
    with pytest.raises(QuoteError):
        _sum_fees(row)
