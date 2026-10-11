"""Best ordinary-shipment pricing, verified against the live V5 entry page."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

if __package__:
    from .common import QuoteError, money, positive_number
else:
    from common import QuoteError, money, positive_number


def _text(row, key):
    value = row.get(key) if isinstance(row, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise QuoteError(f"百世未返回必要字段：{key}")
    return value.strip()


def _id(row, key):
    value = row.get(key) if isinstance(row, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) <= 0:
        raise QuoteError(f"百世未返回有效标识：{key}")
    return int(value)


def _object(value, label):
    if not isinstance(value, dict):
        raise QuoteError(f"百世{label}返回格式无效")
    return value


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise QuoteError(f"百世{label}返回格式无效")
    return value


def _one(value, label):
    rows = _rows(value, label)
    if len(rows) != 1:
        raise QuoteError(f"百世{label}无数据" if not rows else f"百世{label}匹配不唯一")
    return rows[0]


def _request(client, name, body):
    result = client.request(name, body)
    if not isinstance(result, dict) or result.get("code") != "200":
        raise QuoteError("百世查询或计算未成功")
    return result


def _site(client):
    raw = client.request("best_site", {}, text=True)
    if not isinstance(raw, str):
        raise QuoteError("百世当前站点响应无效")
    match = re.fullmatch(r"\s*quoteContext\s*\(([\s\S]*)\)\s*;?\s*", raw)
    if match is None:
        raise QuoteError("百世当前站点响应无效，请核实登录状态")
    try:
        data = json.loads(match.group(1))
    except (ValueError, TypeError):
        raise QuoteError("百世当前站点响应无法解析") from None
    # User metadata in the JSONP is irrelevant to the quote and is not retained.
    return _id(_object(data, "当前站点").get("siteVo"), "id")


def _product(client, site):
    data = _request(client, "best_products", {"siteId": site})
    products = _rows(data.get("voList"), "可用产品")
    product = _one([item for item in products if item.get("defaultProduct") is True], "默认产品")
    if product.get("status") != "ACTIVE":
        raise QuoteError("百世默认产品当前不可用")
    links = _rows(product.get("linkList"), "产品服务")
    dispatch = _one([link for link in links if link.get("linkName") == "派送"], "派送环节")
    services = _rows(dispatch.get("networkServiceList"), "派送服务")
    return product, services


def _address(client, request, site, product_id, service_id, basic_service):
    response = _request(client, "best_address", {
        "address": request["address"], "addressAIType": "TEXT", "parseType": "DEFAULT",
        "sendSiteId": site, "networkProductId": product_id, "networkServiceId": str(service_id),
    })
    value = _object(response.get("vo"), "地址解析")
    ai = _object(value.get("aiAddressVo"), "行政区划解析")
    related = _object(value.get("siteRelatedInfoVo"), "地址站点解析")
    if related.get("isBlindArea") is not False:
        raise QuoteError("百世地址处于盲区或覆盖状态不明确")
    if _rows(related.get("siteExceptionInterceptionVoList"), "站点异常"):
        raise QuoteError("百世目的站点存在异常限制，需在原站核实")
    if _rows(related.get("specialAreaList"), "特殊区域"):
        raise QuoteError("百世该地址涉及特殊区域，需在原站选择服务后报价")
    special = _object(related.get("specialServiceVo"), "派送覆盖规则")
    special_name = _text(related, "specialServiceName")
    if special_name != "正常派送" and not (special_name == "站点自提" and basic_service == 2):
        raise QuoteError("百世该地址的覆盖方式不支持本次送货方式")
    if special.get("whetherChargeTownFee") is not False or special.get("hasAddPrice") is not False:
        raise QuoteError("百世该地址存在乡镇或特殊加价，需在原站核实")
    region = [_text(ai, key) for key in ("province", "city", "district")]
    canton = _text(related, "addressThreeCanton")
    if canton != "|".join(region):
        raise QuoteError("百世地址行政区划解析结果不一致")
    detail = _text(ai, "restaddr")
    destination = _id(related.get("parseSiteVo"), "id")
    route = _object(related.get("cbSiteRouteVo"), "站点路线")
    if _id(route, "dispSiteId") != destination or _id(route, "sendSiteId") != site:
        raise QuoteError("百世地址匹配站点与路线不一致")
    return {
        "acceptCanton": canton, "acceptAddressDetail": detail, "acceptAddress": "".join(region) + detail,
        "townId": _id(related.get("parseTownVo"), "id"), "dispSiteId": destination,
    }


def _fee_sum(rows):
    rows = _rows(rows, "费用明细")
    amounts = []
    for row in rows:
        # The original measurement panel omits money for services not selected.
        if row.get("money") is None:
            continue
        try:
            if isinstance(row["money"], bool):
                raise InvalidOperation
            value = Decimal(str(row["money"]))
            if not value.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise QuoteError("百世费用金额无法解析") from None
        amounts.append(value)
    if not amounts:
        raise QuoteError("百世未返回有效费用金额")
    total = sum(amounts, Decimal("0"))
    if total < 0:
        raise QuoteError("百世费用汇总为负数")
    return format(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _mode(client, request, site, product, services, basic_service, sequence):
    service = _one([item for item in services if str(item.get("oldServiceId")) == str(basic_service)], "送货方式")
    if service.get("networkServiceType") != "BASIC_SERVICE":
        raise QuoteError("百世送货方式不是基础服务")
    product_id, service_id = _id(product, "id"), _id(service, "networkServiceId")
    address = _address(client, request, site, product_id, service_id, basic_service)
    allowed = _request(client, "best_services", {
        "networkProductId": product_id, "siteId": address["dispSiteId"], "linkCode": "DISPATCH",
    }).get("voList")
    if not isinstance(allowed, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in allowed):
        raise QuoteError("百世服务可用性响应无效")
    if service_id not in allowed:
        raise QuoteError("百世该目的站点暂不支持本次送货方式")
    volume, actual = float(positive_number(request["volume"], "体积")), float(positive_number(request["weight"], "重量"))
    calculated = _request(client, "best_weight", {
        "cubic": volume, "actualWeight": actual, "acceptCanton": address["acceptCanton"],
        "insuranceTypeId": 648, "insuranceType": "普通", "sendSiteId": site,
        "pickupSiteId": None, "queryInsureCount": sequence,
    })
    weights = _object(calculated.get("vo"), "计费重量和保价")
    if type(weights.get("queryInsureCount")) is not int or weights["queryInsureCount"] != sequence:
        raise QuoteError("百世纪费重量响应与本次请求不一致")
    insured = _object(weights.get("insureInterval"), "保价区间")
    minimum, maximum = money(insured.get("minPrice")), money(insured.get("maxPrice"))
    if minimum > maximum:
        raise QuoteError("百世当前保价区间无效")
    body = {
        "scene": "YDLRX", "networkProductId": product_id, "sendSiteId": site,
        "basicServiceId": basic_service, **address, "cubic": volume, "actualWeight": actual,
        "paymentTypeId": 30, "insuranceTypeId": 648, "insureAmount": float(minimum),
        "networkServiceId": str(service_id),
    }
    for key in ("weight", "upstairsWeight", "grossWeight", "standardFeeWeight"):
        body[key] = float(positive_number(weights.get(key), f"百世{key}"))
    calculated = _request(client, "best_quote", body)
    mode = "派送" if basic_service == 1 else "自提"
    return {
        "label": f"{_text(product, 'name')}（{'派送' if basic_service == 1 else '站点自提'}）",
        "product_name": _text(product, "name"), "delivery_method": mode,
        "amount": _fee_sum(calculated.get("voList")),
    }, format(minimum, "f")


def quote(client, request):
    site = _site(client)
    product, services = _product(client, site)
    quotes, warnings, insured_values = [], [], []
    for sequence, basic_service in enumerate((1, 2), start=1):
        try:
            result, insured = _mode(client, request, site, product, services, basic_service, sequence)
            quotes.append(result)
            insured_values.append(insured)
        except QuoteError as exc:
            warnings.append(f"{'派送' if basic_service == 1 else '站点自提'}：{exc}")
    if not quotes:
        raise QuoteError("；".join(warnings))
    declared = "、".join(dict.fromkeys(insured_values))
    warnings.insert(0, f"按现付、普通保价测算，本次采用平台最低保价{declared}元；上楼、进仓等附加服务须另行核实")
    return {"quotes": quotes, "warnings": warnings}
