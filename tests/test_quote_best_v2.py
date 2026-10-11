"""Synthetic Best protocol fixtures. All names, IDs and amounts are invented."""

import copy
import json

import pytest

from service_v2_plugins.quote_prices_v2.payload.best import _fee_sum, quote
from service_v2_plugins.quote_prices_v2.payload.common import QuoteError


INPUT = {"address": "测试省测试市测试区测试路1号", "weight": "1000", "volume": "1"}


class FakeClient:
    """Reusable synthetic transport for direct and real sandbox integration tests."""

    def __init__(self):
        self.calls = []
        self.weight_sequence_override = None
        self.destination_by_service = {101: 22, 102: 23}
        self.responses = {
            "best_site": 'quoteContext({"siteVo":{"id":11},"userVo":{"display":"测试账号元数据"}});',
            "best_products": {"code": "200", "voList": [{
                "id": 501, "name": "测试标准产品", "defaultProduct": True, "status": "ACTIVE",
                "linkList": [{"linkName": "派送", "networkServiceList": [
                    {"oldServiceId": 1, "networkServiceId": 101, "networkServiceName": "派送服务", "networkServiceType": "BASIC_SERVICE", "type": "DEFAULT"},
                    {"oldServiceId": 2, "networkServiceId": 102, "networkServiceName": "站点自提", "networkServiceType": "BASIC_SERVICE", "type": "OPTIONAL"},
                ]}],
            }]},
            "best_address": {"code": "200", "vo": {
                "aiAddressVo": {"province": "测试省", "city": "测试市", "district": "测试区", "restaddr": "测试路1号"},
                "siteRelatedInfoVo": {
                    "addressThreeCanton": "测试省|测试市|测试区", "isBlindArea": False,
                    "parseCount": 37, "siteExceptionInterceptionVoList": [], "specialAreaList": [],
                    "specialServiceName": "正常派送", "specialServiceVo": {"whetherChargeTownFee": False, "hasAddPrice": False},
                    "parseSiteVo": {"id": 22}, "parseTownVo": {"id": 33},
                    "cbSiteRouteVo": {"sendSiteId": 11, "dispSiteId": 22},
                },
            }},
            "best_services": {"code": "200", "voList": [101, 102]},
            "best_weight": {"code": "200", "vo": {
                "weight": 1000, "grossWeight": 1000, "standardFeeWeight": 1000, "upstairsWeight": 1000,
                "insureInterval": {"minPrice": 1234, "maxPrice": 50000},
            }},
            "best_quote": {"code": "200", "voList": [{"name": "测试运输费", "money": 100}, {"name": "测试调减", "money": -5}, {"name": "测试保价费", "money": 1.25}, {"name": "未选上楼"}]},
        }

    def request(self, name, body=None, *, text=False):
        body = body or {}
        self.calls.append((name, copy.deepcopy(body), text))
        response = copy.deepcopy(self.responses[name])
        if name == "best_address" and self.destination_by_service:
            destination = self.destination_by_service[int(body["networkServiceId"])]
            response["vo"]["siteRelatedInfoVo"]["parseSiteVo"]["id"] = destination
            response["vo"]["siteRelatedInfoVo"]["cbSiteRouteVo"]["dispSiteId"] = destination
        if name == "best_weight" and response.get("vo"):
            response["vo"]["queryInsureCount"] = body["queryInsureCount"] if self.weight_sequence_override is None else self.weight_sequence_override
        return response


def test_best_quote_resolves_dynamic_site_product_services_and_both_routes():
    client = FakeClient()
    result = quote(client, INPUT)
    assert len(result["quotes"]) == 2
    assert {row["delivery_method"] for row in result["quotes"]} == {"派送", "自提"}
    assert all(row["amount"] == "96.25" for row in result["quotes"])
    assert "最低保价1234元" in result["warnings"][0]
    assert client.calls[0] == ("best_site", {}, True)
    addresses = [body for name, body, _ in client.calls if name == "best_address"]
    assert [body["networkServiceId"] for body in addresses] == ["101", "102"]
    payloads = [body for name, body, _ in client.calls if name == "best_quote"]
    assert [body["dispSiteId"] for body in payloads] == [22, 23]
    assert [body["basicServiceId"] for body in payloads] == [1, 2]
    assert all(body["sendSiteId"] == 11 and body["networkProductId"] == 501 for body in payloads)
    assert all(body["insureAmount"] == 1234 and body["weight"] == 1000 for body in payloads)
    assert all("userVo" not in body and "display" not in body for body in payloads)
    assert all(body["scene"] == "YDLRX" for body in payloads)
    # parseCount is an API usage counter, not a count of alternative candidates.
    assert client.responses["best_address"]["vo"]["siteRelatedInfoVo"]["parseCount"] == 37


@pytest.mark.parametrize("context", ["{}", "otherCallback({\"siteVo\":{\"id\":11}})", 'quoteContext({"siteVo":{"id":11}});alert(1)', 'quoteContext({"siteVo":{}})', 'quoteContext({"siteVo":{"id":true}})', 'quoteContext({"siteVo":{"id":-1}})', 'quoteContext(notJSON)'])
def test_invalid_site_response_is_not_used(context):
    client = FakeClient()
    client.responses["best_site"] = context
    with pytest.raises(QuoteError):
        quote(client, INPUT)
    assert len(client.calls) == 1


def test_jsonp_allows_only_expected_callback_and_optional_semicolon():
    client = FakeClient()
    client.responses["best_site"] = ' \nquoteContext ( {"siteVo":{"id":"11"}} )\n'
    assert len(quote(client, INPUT)["quotes"]) == 2


@pytest.mark.parametrize("mutation", ["none", "two", "inactive", "historical"])
def test_default_product_must_be_unique_and_active(mutation):
    client = FakeClient()
    products = client.responses["best_products"]["voList"]
    if mutation == "none":
        products[0]["defaultProduct"] = False
    elif mutation == "two":
        products.append(copy.deepcopy(products[0]))
    else:
        products[0]["status"] = "INACTIVE" if mutation == "inactive" else "COPY"
    with pytest.raises(QuoteError, match="默认产品"):
        quote(client, INPUT)
    assert not any(name == "best_quote" for name, _, _ in client.calls)


def test_delivery_mode_availability_keeps_supported_self_pickup_only():
    client = FakeClient()
    client.responses["best_services"]["voList"] = [102]
    result = quote(client, INPUT)
    assert [row["delivery_method"] for row in result["quotes"]] == ["自提"]
    assert any("派送" in warning and "不支持" in warning for warning in result["warnings"])


def test_missing_or_ambiguous_mode_is_not_selected_arbitrarily():
    client = FakeClient()
    services = client.responses["best_products"]["voList"][0]["linkList"][0]["networkServiceList"]
    services.append(copy.deepcopy(services[0]))
    result = quote(client, INPUT)
    assert [row["delivery_method"] for row in result["quotes"]] == ["自提"]
    assert any("不唯一" in warning for warning in result["warnings"])


@pytest.mark.parametrize("change", ["blind", "unknown_blind", "special_area", "exception", "town_charge", "special_charge", "canton", "town", "route", "send_route", "detail", "unknown_service"])
def test_ambiguous_missing_or_special_address_stops_quote(change):
    client = FakeClient()
    client.destination_by_service = {}
    value = client.responses["best_address"]["vo"]
    related = value["siteRelatedInfoVo"]
    if change == "blind":
        related["isBlindArea"] = True
    elif change == "unknown_blind":
        del related["isBlindArea"]
    elif change == "special_area":
        related["specialAreaList"] = [{"name": "测试特殊区域"}]
    elif change == "exception":
        related["siteExceptionInterceptionVoList"] = [{"name": "测试限制"}]
    elif change == "town_charge":
        related["specialServiceVo"]["whetherChargeTownFee"] = True
    elif change == "special_charge":
        related["specialServiceVo"]["hasAddPrice"] = True
    elif change == "canton":
        related["addressThreeCanton"] = "另一个测试区"
    elif change == "town":
        related["parseTownVo"] = {}
    elif change == "route":
        related["cbSiteRouteVo"]["dispSiteId"] = 99
    elif change == "send_route":
        related["cbSiteRouteVo"]["sendSiteId"] = 99
    elif change == "detail":
        value["aiAddressVo"]["restaddr"] = ""
    else:
        related["specialServiceName"] = "测试未知服务"
    with pytest.raises(QuoteError):
        quote(client, INPUT)
    assert not any(name == "best_quote" for name, _, _ in client.calls)


def test_coverage_requires_station_self_pickup():
    client = FakeClient()
    client.responses["best_address"]["vo"]["siteRelatedInfoVo"]["specialServiceName"] = "站点自提"
    result = quote(client, INPUT)
    assert [row["delivery_method"] for row in result["quotes"]] == ["自提"]


@pytest.mark.parametrize("value", [True, 0, 999, "1"])
def test_weight_response_must_match_current_request(value):
    client = FakeClient()
    client.weight_sequence_override = value
    with pytest.raises(QuoteError, match="本次请求"):
        quote(client, INPUT)


@pytest.mark.parametrize("key", ["weight", "upstairsWeight", "grossWeight", "standardFeeWeight"])
def test_required_platform_weights_are_not_replaced_with_user_weight(key):
    client = FakeClient()
    del client.responses["best_weight"]["vo"][key]
    with pytest.raises(QuoteError):
        quote(client, INPUT)


@pytest.mark.parametrize("interval", [{}, {"minPrice": None, "maxPrice": 50000}, {"minPrice": -1, "maxPrice": 50000}, {"minPrice": 50000, "maxPrice": 1234}])
def test_invalid_minimum_insurance_is_not_given_a_default(interval):
    client = FakeClient()
    client.responses["best_weight"]["vo"]["insureInterval"] = interval
    with pytest.raises(QuoteError):
        quote(client, INPUT)


@pytest.mark.parametrize("code", [200, "40001", None, ""])
def test_platform_business_failure_is_not_used(code):
    client = FakeClient()
    client.responses["best_quote"]["code"] = code
    with pytest.raises(QuoteError, match="未成功"):
        quote(client, INPUT)


def test_fee_sum_omits_unselected_fees_but_keeps_signed_adjustments_and_real_zero():
    assert _fee_sum([{"money": 20}, {"money": -2}, {"money": "1.25"}, {"notSelected": True}, {"money": None}]) == "19.25"
    assert _fee_sum([{"money": 0}]) == "0.00"


@pytest.mark.parametrize("rows", [[], [{}], [{"money": None}], [{"money": ""}], [{"money": True}], [{"money": "NaN"}], [{"money": "Infinity"}], [{"money": -1}], ["invalid"]])
def test_invalid_fees_never_look_like_free_shipping(rows):
    with pytest.raises(QuoteError):
        _fee_sum(rows)


def test_request_does_not_copy_customer_metadata_from_parsed_address():
    client = FakeClient()
    ai = client.responses["best_address"]["vo"]["aiAddressVo"]
    ai.update(username="测试姓名", cellphone="测试号码")
    quote(client, INPUT)
    serialized = json.dumps([body for name, body, _ in client.calls if name == "best_quote"], ensure_ascii=False)
    assert "测试姓名" not in serialized and "测试号码" not in serialized
