"""Synthetic protocol fixtures; no real customer, account, or price data."""

from copy import deepcopy
from html import escape

import pytest

from service_v2_plugins.quote_prices_v2.payload import yunda
from service_v2_plugins.quote_prices_v2.payload.common import QuoteError


REQUEST = {"address": "测试省测试市测试区虚构路1号", "weight": "100", "volume": "0.001"}
SITE = {
    "info": "0", "target_center_code": "fictional-destination", "Coverage": "1",
    "target_center": "虚构网点甲", "SiteAddress": "测试市虚构站点路1号",
    "qry_phone": "000-00000001", "cxdh": "000-00000002",
    "remark": "测试区内", "special_range": "虚构园区需预约",
    "BuyerProvince": "测试省", "BuyerCity": "测试市", "BuyerArea": "测试区",
    "BuyerProvinceCode": "fictional-province", "BuyerCityCode": "fictional-city",
    "BuyerAreaCode": "fictional-area", "TownCode": "fictional-town",
    "business_center_code": "fictional-center", "IsMustSend": "0", "SpecialAreaCode": "",
}


def page_html(*, bubble_ratio="3000", heavy_min_weight="5", **overrides):
    fields = dict.fromkeys(yunda.FORM_FIELDS, "")
    fields.update({
        "CreatedDotCode": "fictional-origin", "SenderDistributionCode": "fictional-origin-center",
        "current_time": "2026-01-02 03:04:05", "InsuredAmount": "0.00",
        "Freight": "0.00", "OrderSource": "65", "Del": "0.00", "ItemTotalNumber": "0",
        "CheckHeavyWeight": "0", "CheckFixedCost": "0", "CheckSafeArrive": "0",
        "CheckBulkCargo": "0", "DeliversSms1": "0", "DeliversSms2": "0",
        "DispatchSms": "1", "IsSendMsg": "0", "IsCod": "0",
    })
    fields.update(overrides)
    selects = {
        "GoodsType": [("", ""), ("184", "正常货物")],
        "ProductType": [("24", "标准快运")], "PaymentType": [("102", "现金")],
        "ServiceType": [("111", "派送"), ("112", "自提")],
        "ShippingMethods": [("180", "送货"), ("179", "送货上楼")],
    }
    html = "".join(
        f'<input name="{name}" value="{escape(value)}">'
        for name, value in fields.items() if name not in selects
    )
    html += "".join(
        f'<select name="{name}">' + "".join(
            f'<option value="{value}">{label}</option>' for value, label in options
        ) + "</select>" for name, options in selects.items()
    )
    return html + f'''<script>var CreatedByCode="fictional-operator";
        var $BubbleRatio="{bubble_ratio}"; var $HeavyMinWeight="{heavy_min_weight}";</script>'''


class Client:
    def __init__(self, *, html=None, replacements=None):
        self.html = page_html() if html is None else html
        self.replacements = replacements or {}
        self.calls = []
        self.mode = None

    def request(self, name, body=None, *, text=False):
        self.calls.append((name, deepcopy(body), text))
        if name == "yunda_entry":
            assert text is True
            return self.html
        assert text is False
        if name == "yunda_address":
            self.mode = body["ServiceType"]
        override = self.replacements.get((name, self.mode), self.replacements.get(name))
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return deepcopy(override)
        return deepcopy({
            "yunda_address": {"info": 1, "data": {"fictional-destination": SITE}},
            "yunda_weight": {"info": 1, "data": 2, "Tfr": 100, "Del": 100, "CheckBulkCargo": 0},
            "yunda_insured": {"info": 1, "data": {"MIN": "200", "MAX": "20000"}},
            "yunda_price": {"info": 1, "lb": 2, "data": {"CostTotal": "12.34", "DotCostTotal": "8.20"}},
        }[name])


def test_both_options_are_selected_before_weight_and_preserved_in_both_quotes():
    client = Client()
    result = yunda.quote(client, REQUEST)
    assert result["warnings"] == []
    assert result["quotes"] == [
        {"label": "韵达自提", "amount": "12.39", "delivery_method": "自提", "site_name": "虚构网点甲"},
        {"label": "韵达派送", "amount": "12.39", "delivery_method": "派送", "site_name": "虚构网点甲"},
    ]
    assert result["site_name"] == "虚构网点甲"
    assert result["details"] == [
        "自提/派送匹配网点：虚构网点甲", "网点地址：测试市虚构站点路1号",
        "查件电话：000-00000001", "客服电话：000-00000002",
        "派送范围：测试区内", "特殊区域：虚构园区需预约",
        "韵达自提报价选项：韵重货＋一口价", "韵达派送报价选项：韵重货＋一口价",
    ]
    assert [call[0] for call in client.calls].count("yunda_entry") == 1
    prices = [body for name, body, _ in client.calls if name == "yunda_price"]
    assert [p["ShippingMethods"] for p in prices] == ["", "180"]
    for body in prices:
        assert body["InsuredAmount"] == "200"
        assert body["BuyerAddress"] == "虚构路1号"
        assert body["Volume"] == "0.001"
        assert body["CreatedByCode"] == "fictional-operator"
        assert body["DestinationDotScope"] == "fictional-center"
        assert body["CheckHeavyWeight"] == body["CheckFixedCost"] == "1"
        assert body["CheckBulkCargo"] == "0"
        assert body["InGoodsType"] == ""  # The current original page leaves this empty.
        assert "DispatchSms" not in body  # Added by the page after pricing, not by the API.
    weights = [body for name, body, _ in client.calls if name == "yunda_weight"]
    assert len(weights) == 2
    assert all(body["CheckHeavyWeight"] == body["CheckFixedCost"] == "1" for body in weights)
    assert all(body["CheckBulkCargo"] == "0" for body in weights)


def test_large_volume_uses_only_fixed_cost_and_its_fresh_mode_weight():
    client = Client(
        html=page_html(CheckHeavyWeight="1", CheckFixedCost="0", CheckBulkCargo="1"),
        replacements={
            ("yunda_weight", "112"): {"info": 1, "data": 500, "Tfr": 510, "Del": 500, "CheckBulkCargo": 0},
            ("yunda_weight", "111"): {"info": 1, "data": 600, "Tfr": 610, "Del": 600, "CheckBulkCargo": 0},
        },
    )
    result = yunda.quote(client, {**REQUEST, "volume": "2"})
    prices = [body for name, body, _ in client.calls if name == "yunda_price"]
    assert [(body["ServiceType"], body["SettlementTotalNumber"]) for body in prices] == [
        ("112", "500.00"), ("111", "600.00"),
    ]
    for name, body, _ in client.calls:
        if name in {"yunda_weight", "yunda_price"}:
            assert body["CheckHeavyWeight"] == "0"
            assert body["CheckFixedCost"] == "1"
            assert body["CheckBulkCargo"] == "0"
    assert result["details"][-2:] == [
        "韵达自提报价选项：仅一口价（韵重货不适用）",
        "韵达派送报价选项：仅一口价（韵重货不适用）",
    ]


@pytest.mark.parametrize("weight,volume,ratio,minimum,expected_heavy", [
    ("10", "0.03", "3000", "5", "0"),  # Equal to density threshold.
    ("10.01", "0.03", "3000", "5", "1"),
    ("5", "0.001", "3000", "5", "0"),  # Equal to minimum weight.
    ("5.01", "0.001", "3000", "5", "1"),
    ("100", "0.001", "0", "5", "0"),
    ("100", "0.001", "3000", "101", "0"),  # Fresh page threshold changes.
    ("10", "0.03", "6000", "5", "1"),
])
def test_heavy_eligibility_uses_strict_current_page_thresholds(
    weight, volume, ratio, minimum, expected_heavy,
):
    client = Client(html=page_html(bubble_ratio=ratio, heavy_min_weight=minimum))
    result = yunda.quote(client, {**REQUEST, "weight": weight, "volume": volume})
    assert len(result["quotes"]) == 2
    for name, body, _ in client.calls:
        if name in {"yunda_weight", "yunda_price"}:
            assert body["CheckHeavyWeight"] == expected_heavy
            assert body["CheckFixedCost"] == "1"
            assert body["CheckBulkCargo"] == "0"


@pytest.mark.parametrize("field", ["CostTotal", "DotCostTotal"])
def test_missing_total_fails_instead_of_reusing_other_total(field):
    data = {"CostTotal": "10", "DotCostTotal": "20"}
    del data[field]
    response = {"info": 1, "lb": 2 if field == "CostTotal" else 1, "data": data}
    with pytest.raises(QuoteError, match="报价金额"):
        yunda.quote(Client(replacements={"yunda_price": response}), REQUEST)


def test_dot_total_and_all_enabled_sms_flags_match_original_page():
    client = Client(
        html=page_html(DeliversSms1="1", DeliversSms2="1", IsSendMsg="1", IsCod="1"),
        replacements={"yunda_price": {"info": 1, "lb": 1, "data": {"DotCostTotal": "8.20"}}},
    )
    assert yunda.quote(client, REQUEST)["quotes"][0]["amount"] == "8.45"


def test_total_uses_page_number_addition_and_truncation():
    form = dict.fromkeys(yunda.SMS_FIELDS, "0")
    form["DispatchSms"] = "1"
    form["DeliversSms1"] = "1"
    # JS Number(6.1) + Number(0.1) is 6.199999999999999; original truncates it.
    assert yunda._amount({"info": 1, "lb": 2, "data": {"CostTotal": "6.1"}}, form) == "6.19"


@pytest.mark.parametrize("data", [{}, {"a": SITE, "b": SITE}, [], [SITE, SITE]])
def test_address_must_have_exactly_one_candidate(data):
    client = Client(replacements={"yunda_address": {"info": 1, "data": data}})
    with pytest.raises(QuoteError, match="唯一目的网点"):
        yunda.quote(client, REQUEST)
    assert not any(name == "yunda_price" for name, _, _ in client.calls)


@pytest.mark.parametrize("code", ["BA", "OR", "RF", "AE", "AY"])
def test_unserviceable_area_does_not_produce_a_price(code):
    site = {**SITE, "target_center_code": code}
    client = Client(replacements={"yunda_address": {"info": 1, "data": {"a": site}}})
    with pytest.raises(QuoteError, match="不可报价"):
        yunda.quote(client, REQUEST)


def test_one_delivery_mode_failure_keeps_other_fresh_mode():
    client = Client(replacements={("yunda_price", "111"): {"info": 0, "data": "synthetic failure"}})
    result = yunda.quote(client, REQUEST)
    assert len(result["quotes"]) == 1
    assert result["quotes"][0]["delivery_method"] == "自提"
    assert "派送" in result["warnings"][0]


def test_pickup_only_area_does_not_quote_delivery():
    site = {**SITE, "Coverage": "4"}
    result = yunda.quote(Client(replacements={"yunda_address": {"info": 1, "data": {"a": site}}}), REQUEST)
    assert len(result["quotes"]) == 1
    assert "仅支持自提" in result["warnings"][0]


@pytest.mark.parametrize("field", ["data", "Tfr", "Del", "CheckBulkCargo"])
def test_missing_weight_response_stops_pricing(field):
    response = {"info": 1, "data": 50, "Tfr": 100, "Del": 100, "CheckBulkCargo": 0}
    del response[field]
    client = Client(replacements={"yunda_weight": response})
    with pytest.raises(QuoteError):
        yunda.quote(client, REQUEST)
    assert not any(name == "yunda_price" for name, _, _ in client.calls)


def test_insured_range_must_be_valid_and_live():
    client = Client(replacements={"yunda_insured": {"info": 1, "data": {"MIN": "200", "MAX": "100"}}})
    with pytest.raises(QuoteError, match="保价范围无效"):
        yunda.quote(client, REQUEST)
    assert not any(name == "yunda_price" for name, _, _ in client.calls)


def test_bulk_calculation_cannot_silently_replace_requested_fixed_cost_product():
    client = Client(html=page_html(CheckHeavyWeight="1", CheckFixedCost="1"), replacements={
        "yunda_weight": {"info": 1, "data": 250, "Tfr": 250, "Del": 250, "CheckBulkCargo": 1},
    })
    with pytest.raises(QuoteError, match="无法按一口价报价") as caught:
        yunda.quote(client, REQUEST)
    assert not any(name in {"yunda_insured", "yunda_price"} for name, _, _ in client.calls)
    assert "自提/派送匹配网点：虚构网点甲" in caught.value.details
    assert not any("报价选项" in line for line in caught.value.details)


def test_bulk_failure_in_one_mode_keeps_only_other_mode_quote_and_options():
    client = Client(replacements={
        ("yunda_weight", "111"): {"info": 1, "data": 250, "Tfr": 250, "Del": 250, "CheckBulkCargo": 1},
    })
    result = yunda.quote(client, REQUEST)
    assert [item["delivery_method"] for item in result["quotes"]] == ["自提"]
    assert result["warnings"] == ["韵达派送：平台计重结果要求大票货，无法按一口价报价，请在原页核实"]
    assert [line for line in result["details"] if "报价选项" in line] == ["韵达自提报价选项：韵重货＋一口价"]
    assert [body["ServiceType"] for name, body, _ in client.calls if name == "yunda_price"] == ["112"]


@pytest.mark.parametrize("field", ["data", "Tfr", "Del"])
@pytest.mark.parametrize("value", [None, "", "not-a-number", "NaN", "-1", True])
def test_invalid_fresh_weight_response_stops_pricing(field, value):
    response = {"info": 1, "data": 50, "Tfr": 100, "Del": 100, "CheckBulkCargo": 0}
    response[field] = value
    client = Client(replacements={"yunda_weight": response})
    with pytest.raises(QuoteError):
        yunda.quote(client, REQUEST)
    assert not any(name == "yunda_price" for name, _, _ in client.calls)


@pytest.mark.parametrize("value", [None, "", "not-a-number", "3", True])
def test_invalid_bulk_response_stops_pricing(value):
    client = Client(replacements={
        "yunda_weight": {"info": 1, "data": 50, "Tfr": 100, "Del": 100, "CheckBulkCargo": value},
    })
    with pytest.raises(QuoteError):
        yunda.quote(client, REQUEST)
    assert not any(name == "yunda_price" for name, _, _ in client.calls)


@pytest.mark.parametrize("threshold", ["bubble_ratio", "heavy_min_weight"])
@pytest.mark.parametrize("value", ["", "not-a-number", "NaN", "."])
def test_invalid_current_page_threshold_stops_before_weight(threshold, value):
    client = Client(html=page_html(**{threshold: value}))
    with pytest.raises(QuoteError):
        yunda.quote(client, REQUEST)
    assert len(client.calls) == 1


def test_page_context_missing_identity_or_threshold_fails_before_api():
    for old in ('var CreatedByCode="fictional-operator";', 'var $BubbleRatio="3000";'):
        client = Client(html=page_html().replace(old, ""))
        with pytest.raises(QuoteError):
            yunda.quote(client, REQUEST)
        assert len(client.calls) == 1


def test_login_failure_is_not_retried_or_exposed_as_a_quote():
    client = Client(replacements={"yunda_address": RuntimeError("BLOCKED_LOGIN")})
    with pytest.raises(RuntimeError, match="BLOCKED_LOGIN"):
        yunda.quote(client, REQUEST)
    assert len(client.calls) == 2


def test_different_mode_sites_keep_their_own_details_and_quote_names():
    other = {
        **SITE, "target_center_code": "fictional-other", "target_center": "虚构网点乙",
        "SiteAddress": "测试市虚构站点路2号", "qry_phone": "000-00000003",
    }
    client = Client(replacements={("yunda_address", "111"): {"info": 1, "data": {"other": other}}})
    result = yunda.quote(client, REQUEST)
    assert "site_name" not in result
    assert [item["site_name"] for item in result["quotes"]] == ["虚构网点甲", "虚构网点乙"]
    details = result["details"]
    split = details.index("派送匹配网点：虚构网点乙")
    assert "网点地址：测试市虚构站点路1号" in details[:split]
    assert "网点地址：测试市虚构站点路2号" not in details[:split]
    assert "查件电话：000-00000003" in details[split:]
    assert "查件电话：000-00000001" not in details[split:]


def test_same_site_with_different_service_details_is_not_combined():
    other = {**SITE, "special_range": "测试区北侧不派送"}
    result = yunda.quote(Client(replacements={
        ("yunda_address", "111"): {"info": 1, "data": {"a": other}},
    }), REQUEST)
    assert "自提匹配网点：虚构网点甲" in result["details"]
    assert "派送匹配网点：虚构网点甲" in result["details"]
    assert "特殊区域：虚构园区需预约" in result["details"]
    assert "特殊区域：测试区北侧不派送" in result["details"]


def test_missing_site_fields_are_explicit_without_replacing_a_valid_price():
    site = {key: value for key, value in SITE.items() if key not in {
        "target_center", "SiteAddress", "qry_phone", "cxdh", "remark", "special_range",
    }}
    result = yunda.quote(Client(replacements={"yunda_address": {"info": 1, "data": {"a": site}}}), REQUEST)
    assert len(result["quotes"]) == 2
    assert "site_name" not in result
    assert all("site_name" not in item for item in result["quotes"])
    assert result["details"] == [
        "自提/派送匹配网点：无数据", "网点地址：无数据", "查件电话：无数据",
        "客服电话：无数据", "派送范围：无数据", "特殊区域：无数据",
        "韵达自提报价选项：韵重货＋一口价", "韵达派送报价选项：韵重货＋一口价",
    ]


def test_special_area_candidates_preserve_similarity_and_are_not_called_matches():
    site = {**SITE, "SpecialAreaMsg": "本次地址需核实园区门禁", "SpecialArea": {
        "虚构园区甲": {"Similarity": "16.67%", "remark": "预约后派送", "charge_type": 1},
        "虚构园区乙": {"Similarity": "0.00%", "remark": "特殊服务另询", "charge_type": 0},
    }}
    result = yunda.quote(Client(replacements={"yunda_address": {"info": 1, "data": {"a": site}}}), REQUEST)
    assert "特殊区域提示：本次地址需核实园区门禁" in result["details"]
    assert [line for line in result["details"] if line.startswith("特殊区域参考：")] == [
        "特殊区域参考：虚构园区甲（平台相似度：16.67%）；说明：预约后派送",
        "特殊区域参考：虚构园区乙（平台相似度：0.00%）；说明：特殊服务另询",
    ]


def test_malformed_optional_details_are_reported_without_losing_prices():
    site = {**SITE, "SiteAddress": {"unknown": "must not stringify"}, "SpecialArea": ["bad"]}
    result = yunda.quote(Client(replacements={"yunda_address": {"info": 1, "data": {"a": site}}}), REQUEST)
    assert len(result["quotes"]) == 2
    assert "网点地址：无数据（平台字段格式异常）" in result["details"]
    assert "特殊区域参考：无数据（平台字段格式异常）" in result["details"]
    assert "must not stringify" not in str(result)


def test_restricted_site_details_survive_failure_of_both_modes():
    site = {**SITE, "info": "3", "servicemsg": "虚构站点暂停服务", "LimitingMsg": "虚构道路限制"}
    client = Client(replacements={"yunda_address": {"info": 1, "data": {"a": site}}})
    with pytest.raises(QuoteError, match="服务限制") as caught:
        yunda.quote(client, REQUEST)
    assert "自提/派送匹配网点：虚构网点甲" in caught.value.details
    assert "服务限制：虚构站点暂停服务" in caught.value.details
    assert "重量限制：虚构道路限制" in caught.value.details
    assert not any(name == "yunda_price" for name, _, _ in client.calls)


def test_failed_price_keeps_the_failed_modes_own_site_details():
    site = {**SITE, "target_center_code": "fictional-other", "target_center": "虚构网点乙"}
    client = Client(replacements={
        ("yunda_address", "111"): {"info": 1, "data": {"other": site}},
        ("yunda_price", "111"): {"info": 0},
    })
    result = yunda.quote(client, REQUEST)
    assert len(result["quotes"]) == 1
    assert result["quotes"][0]["site_name"] == "虚构网点甲"
    assert "site_name" not in result
    assert "派送匹配网点：虚构网点乙" in result["details"]
    assert "费用计算失败" in result["warnings"][0]
