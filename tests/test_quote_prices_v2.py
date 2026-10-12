"""Synthetic quote inputs and provider isolation; no platform network calls."""

from decimal import Decimal

import pytest

from service_v2_plugins.quote_prices_v2.payload.action import run
from service_v2_plugins.quote_prices_v2.payload.common import QuoteClient, QuoteError, money, quote_inputs


INPUT = {"address": "测试省测试市测试路1号", "weight": 100, "volume": 0.001}


class Observed(dict):
    host_evidence_ref = "synthetic-read-evidence"


def broker(operation, *, action, role, arguments):
    assert operation == "http.request"
    assert action == "read_json"
    assert role in {"ronghui_quote", "yunda_quote", "best_quote"}
    return Observed(response={"price": "12.30"})


def provider(client, request):
    data = client.request("synthetic_price", request)
    return {"quotes": [{"label": "测试产品·自提", "amount": data["price"], "delivery_method": "自提"}]}


def providers(**overrides):
    return {"ronghui": provider, "yunda": provider, "best": provider, **overrides}


@pytest.mark.parametrize("field", ["address", "weight"])
def test_address_and_weight_are_required(field):
    value = dict(INPUT)
    del value[field]
    with pytest.raises(QuoteError):
        quote_inputs(value)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, "", "NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["weight", "volume"])
def test_weight_and_volume_are_positive_finite_numbers(field, value):
    with pytest.raises(QuoteError):
        quote_inputs({**INPUT, field: value})


def test_input_preserves_small_volume_without_money_rounding():
    assert quote_inputs(INPUT)["volume"] == "0.001"
    assert "volume_defaulted" not in quote_inputs(INPUT)
    with pytest.raises(QuoteError):
        quote_inputs({**INPUT, "account_id": "synthetic-override"})


def test_omitted_volume_uses_plugin_default_and_records_it():
    request = quote_inputs({"address": INPUT["address"], "weight": INPUT["weight"]})
    assert request == {"address": INPUT["address"], "weight": "100", "volume": "0.1", "volume_defaulted": True}


def test_defaulted_volume_reaches_all_providers_and_returned_request():
    requests = []

    def capturing_provider(client, request):
        requests.append(dict(request))
        return provider(client, request)

    result = run(broker, {"address": INPUT["address"], "weight": INPUT["weight"]},
                 providers={key: capturing_provider for key in providers()})
    assert result["status"] == "SUCCESS"
    assert len(requests) == 3
    assert all(request == result["data"]["request"] for request in requests)
    assert result["data"]["request"]["volume"] == "0.1"
    assert result["data"]["request"]["volume_defaulted"] is True


def test_money_does_not_replace_missing_amount_with_zero():
    for value in (None, "", True, "NaN", "Infinity", -1):
        with pytest.raises(QuoteError):
            money(value)
    assert money("0") == Decimal("0")


def test_quote_client_requires_current_host_observation():
    client = QuoteClient(lambda *a, **kw: {"response": {"price": "12.30"}}, "best_quote")
    with pytest.raises(QuoteError, match="请求凭证"):
        client.request("synthetic_price")


def test_quote_context_uses_exact_role_and_current_host_observation():
    calls = []

    def context_broker(operation, *, action, role, arguments):
        calls.append((operation, action, role, arguments))
        return Observed(loginSiteCode="synthetic-site", loginEmpCode="synthetic-employee")

    client = QuoteClient(context_broker, "ronghui_quote")
    result = client.context()
    assert result["loginSiteCode"] == "synthetic-site"
    assert calls == [("account.context", "read", "ronghui_quote", {})]
    assert client.refs == ["synthetic-read-evidence"]


@pytest.mark.parametrize("response", [{"loginSiteCode": "synthetic-site"}, None, []])
def test_quote_context_rejects_unobserved_or_invalid_result(response):
    client = QuoteClient(lambda *args, **kwargs: response, "ronghui_quote")
    with pytest.raises(QuoteError, match="本次请求凭证"):
        client.context()
    assert client.refs == []


def test_quotes_return_all_three_providers():
    result = run(broker, INPUT, providers=providers())
    assert result["status"] == "SUCCESS"
    assert result["meta"]["record_count"] == 3
    for key in ("ronghui", "yunda", "best"):
        assert result["data"][key]["quotes"][0]["amount"] == "12.30"
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"


def test_one_provider_failure_preserves_other_current_quotes():
    calls = []

    def unavailable(client, request):
        calls.append(client.role)
        raise RuntimeError("BLOCKED_LOGIN")

    result = run(broker, INPUT, providers=providers(best=unavailable))
    assert result["status"] == "SUCCESS"
    assert result["data"]["best"]["status"] == "failed"
    assert result["data"]["best"]["quotes"] == []
    assert "登录" in result["data"]["best"]["error"]
    assert result["data"]["ronghui"]["status"] == "success"
    assert calls == ["best_quote"]


def test_all_failures_keep_three_diagnostics_and_failed_status():
    def failed(client, request):
        raise QuoteError("测试平台没有可用线路")

    result = run(broker, INPUT, providers={key: failed for key in providers()})
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "QUOTE_UNAVAILABLE"
    assert result["meta"]["record_count"] == 0
    assert all(result["data"][key]["error"] == "测试平台没有可用线路" for key in providers())


def test_unexpected_provider_exception_does_not_expose_response_body():
    def failed(client, request):
        raise RuntimeError("synthetic-private-upstream-body")

    result = run(broker, INPUT, providers=providers(best=failed))
    assert "synthetic-private-upstream-body" not in str(result)


def test_unobserved_success_and_missing_delivery_method_are_rejected():
    def unobserved(client, request):
        return {"quotes": [{"label": "测试产品", "amount": "12.30", "delivery_method": "自提"}]}

    def unspecified(client, request):
        client.request("synthetic_price", request)
        return {"quotes": [{"label": "测试产品", "amount": "12.30"}]}

    result = run(broker, INPUT, providers=providers(best=unobserved, ronghui=unspecified))
    assert result["data"]["best"]["status"] == "failed"
    assert result["data"]["ronghui"]["status"] == "failed"
    assert result["data"]["yunda"]["status"] == "success"


def test_observed_site_details_survive_provider_restriction():
    details = ["匹配网点：测试网点", "特殊区域：测试园区需人工核实"]

    def restricted(client, request):
        client.request("synthetic_price", request)
        raise QuoteError("特殊区域暂不能自动报价", details=details)

    result = run(broker, INPUT, providers=providers(best=restricted))
    assert result["status"] == "SUCCESS"
    assert result["data"]["best"]["quotes"] == []
    assert result["data"]["best"]["details"] == details
    assert result["meta"]["record_count"] == 2


@pytest.mark.parametrize("quotes", [[], [{"label": "测试派送", "delivery_method": "派送", "amount": None}]])
@pytest.mark.parametrize("observed", [False, True])
def test_invalid_quote_keeps_only_observed_site_details(quotes, observed):
    details = ["匹配网点：测试网点", "特殊区域：测试园区"]

    def invalid(client, request):
        if observed:
            client.request("synthetic_price", request)
        return {"quotes": quotes, "details": details}

    result = run(broker, INPUT, providers=providers(best=invalid))["data"]["best"]
    assert result["status"] == "failed" and result["quotes"] == []
    assert result.get("details") == (details if observed else None)
