"""Quote input and account-bound transport shared by the three providers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


DEFAULT_VOLUME = "0.1"


class QuoteError(ValueError):
    """A provider's explicit, public business failure."""


def positive_number(value, name):
    if isinstance(value, bool) or value is None:
        raise QuoteError(f"{name}必须是大于0的数字")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise QuoteError(f"{name}必须是大于0的数字") from None
    if not number.is_finite() or number <= 0:
        raise QuoteError(f"{name}必须是大于0的数字")
    return number


def quote_inputs(arguments):
    if (not isinstance(arguments, dict) or not {"address", "weight"} <= set(arguments)
            or set(arguments) - {"address", "weight", "volume"}):
        raise QuoteError("请提供完整收货地址和重量（公斤），体积（立方米）可省略")
    address = arguments["address"]
    if not isinstance(address, str) or not address.strip() or len(address) > 500:
        raise QuoteError("请提供有效的完整收货地址")
    volume_defaulted = "volume" not in arguments
    request = {
        "address": address.strip(),
        "weight": format(positive_number(arguments["weight"], "重量"), "f"),
        "volume": format(positive_number(DEFAULT_VOLUME if volume_defaulted else arguments["volume"], "体积"), "f"),
    }
    if volume_defaulted:
        request["volume_defaulted"] = True
    return request


def money(value):
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise QuoteError("平台未返回有效报价金额")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise QuoteError("平台报价金额无法解析") from None
    if not amount.is_finite() or amount < 0:
        raise QuoteError("平台报价金额无效")
    return amount


class QuoteClient:
    def __init__(self, broker, role):
        self.broker = broker
        self.role = role
        self.refs = []

    def context(self):
        result = self.broker("account.context", action="read", role=self.role, arguments={})
        reference = getattr(result, "host_evidence_ref", None)
        if not isinstance(result, dict) or not isinstance(reference, str) or not reference:
            raise QuoteError("账号上下文缺少本次请求凭证")
        self.refs.append(reference)
        return result

    def request(self, name, body=None, *, text=False):
        result = self.broker(
            "http.request",
            action="read_text" if text else "read_json",
            role=self.role,
            arguments={"request": name, "body": {} if body is None else body},
        )
        reference = getattr(result, "host_evidence_ref", None)
        if not isinstance(result, dict) or not isinstance(reference, str) or not reference:
            raise QuoteError("平台查询缺少本次请求凭证")
        self.refs.append(reference)
        response = result.get("response")
        expected = (str,) if text else (dict, list)
        if not isinstance(response, expected):
            raise QuoteError("平台返回的数据格式无效")
        return response
