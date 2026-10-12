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


def _display(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    if type(value) in (int, float) and Decimal(str(value)).is_finite():
        return str(value)
    return "无数据"


def _flag(value):
    return "是" if value is True else "否" if value is False else "无数据"


_SPECIAL_SERVICES = {
    "DISP": "派送", "DOOR_SELF": "门口自提", "TOWN_SELF": "镇上自提",
    "SITE_SELF": "站点自提", "DOOR_PICK_UP": "门口提货", "ENTRY_PICK_UP": "进门提货",
}
_SITE_EXCEPTIONS = {"CAUTION": "慎发", "DISCONTINUATION": "停发", "FOCUS": "关注"}


def _site_details(related):
    """Project the same site/service fields shown by the original V5 entry page."""
    site = related.get("parseSiteVo")
    site = site if isinstance(site, dict) else {}
    special = related.get("specialServiceVo")
    special = special if isinstance(special, dict) else {}
    details = [
        f"网点：{_display(site.get('name'))}",
        f"网点地址：{_display(site.get('location'))}",
        f"联系电话：{_display(site.get('phone'))}；手机：{_display(site.get('salePhone'))}",
        f"覆盖范围：{_display(site.get('dispatchRange'))}",
        f"覆盖方式：{_display(related.get('specialServiceName'))}；时效：{_display(special.get('dispatchTimeLimitName'))}",
        f"服务说明：{_display(special.get('specialServiceDisplay'))}",
        f"乡镇收费：{_flag(special.get('whetherChargeTownFee'))}；特殊加价：{_flag(special.get('hasAddPrice'))}",
    ]
    if related.get("isBlindArea") is not False:
        details.append(f"盲区：{_flag(related.get('isBlindArea'))}")
    if special.get("whetherChargeTownFee") is True and _display(special.get("townFeeRemark")) == "无数据":
        details.append("乡镇费用标准：无数据")
    if site.get("siteStr3"):
        host = _display(site.get("hostNum"))
        extension = _display(site.get("siteStr3"))
        details.append(f"客服分机：{host + '-' if host != '无数据' else ''}{extension}")
    for row, key, label in (
        (site, "principal", "负责人"), (site, "siteStr2", "网点派送说明"),
        (special, "dispServiceDisplay", "覆盖详情"), (special, "townFeeRemark", "乡镇费用标准"),
        (special, "reMark", "服务备注"),
    ):
        if _display(row.get(key)) != "无数据":
            details.append(f"{label}：{_display(row[key])}")
    limits = site.get("siteExtendVo")
    if isinstance(limits, dict) and limits.get("dispSiteWeightSwitch") is True:
        details.extend([
            f"派件重量范围：{_display(limits.get('dispSiteWeightLimitMin'))} ~ {_display(limits.get('dispSiteWeightLimit'))}公斤",
            f"重量限制时间：{_display(limits.get('dispSiteWeightStartTime'))} ~ {_display(limits.get('dispSiteWeightEndTime'))}",
        ])
    areas = related.get("specialAreaList")
    if isinstance(areas, list) and not areas:
        details.append("特殊区域：未匹配到特殊区域")
    elif isinstance(areas, list) and all(isinstance(area, dict) for area in areas):
        for area in areas:
            services = area.get("selectDispatchDetailTypes")
            service_text = "、".join(_SPECIAL_SERVICES.get(key, key) for key in services) if (
                isinstance(services, list) and all(isinstance(key, str) for key in services)
            ) else ""
            details.extend([
                f"特殊区域：{_display(area.get('specialAreaName'))}；精确匹配：{_flag(area.get('isMatchUp'))}",
                f"特殊区域地址：{_display(area.get('standardAddress'))}",
                f"特殊区域服务：{service_text or '无数据'}",
                f"特殊区域收费标准：{_display(area.get('chargesStandard'))}；计费方式：{_display(area.get('chargesMode'))}",
                f"特殊区域说明：{_display(area.get('remark'))}",
                f"特殊区域有效期：{_display(area.get('effectDate'))} ~ {_display(area.get('expireDate'))}",
            ])
    else:
        details.append("特殊区域：无数据（平台未返回有效列表）")
    exceptions = related.get("siteExceptionInterceptionVoList")
    if isinstance(exceptions, list) and not exceptions:
        details.append("站点异常：无")
    elif isinstance(exceptions, list) and all(isinstance(row, dict) for row in exceptions):
        for row in exceptions:
            kind = _display(row.get("handleType"))
            details.append(
                f"站点异常：{_SITE_EXCEPTIONS.get(kind, kind)}；站点：{_display(row.get('siteName'))}；"
                f"范围：{_display(row.get('destSiteNamePath'))}；"
                f"时间：{_display(row.get('startDate'))} ~ {_display(row.get('endDate'))}；"
                f"说明：{_display(row.get('reMark'))}"
            )
    else:
        details.append("站点异常：无数据（平台未返回有效列表）")
    return details


def _mode_details(groups):
    if len(groups) == 2 and groups[0][1] == groups[1][1]:
        return [f"派送/自提{line}" for line in groups[0][1]]
    return [f"{mode}{line}" for mode, lines in groups for line in lines]


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


def _address(client, request, site, product_id, service_id, basic_service, details):
    response = _request(client, "best_address", {
        "address": request["address"], "addressAIType": "TEXT", "parseType": "DEFAULT",
        "sendSiteId": site, "networkProductId": product_id, "networkServiceId": str(service_id),
    })
    value = _object(response.get("vo"), "地址解析")
    related = _object(value.get("siteRelatedInfoVo"), "地址站点解析")
    details.extend(_site_details(related))
    ai = _object(value.get("aiAddressVo"), "行政区划解析")
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
    }, _display(related.get("parseSiteVo", {}).get("name"))


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


def _mode(client, request, site, product, services, basic_service, sequence, details):
    service = _one([item for item in services if str(item.get("oldServiceId")) == str(basic_service)], "送货方式")
    if service.get("networkServiceType") != "BASIC_SERVICE":
        raise QuoteError("百世送货方式不是基础服务")
    product_id, service_id = _id(product, "id"), _id(service, "networkServiceId")
    address, site_name = _address(client, request, site, product_id, service_id, basic_service, details)
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
        **({"site_name": site_name} if site_name != "无数据" else {}),
    }, format(minimum, "f"), address["dispSiteId"]


def quote(client, request):
    site = _site(client)
    product, services = _product(client, site)
    quotes, warnings, insured_values, detail_groups, destinations = [], [], [], [], []
    for sequence, basic_service in enumerate((1, 2), start=1):
        details = []
        try:
            result, insured, destination = _mode(client, request, site, product, services, basic_service, sequence, details)
            quotes.append(result)
            insured_values.append(insured)
            destinations.append((destination, result.get("site_name")))
        except QuoteError as exc:
            warnings.append(f"{'派送' if basic_service == 1 else '站点自提'}：{exc}")
        if details:
            detail_groups.append(("派送" if basic_service == 1 else "自提", details))
    details = _mode_details(detail_groups)
    if not quotes:
        raise QuoteError("；".join(warnings), details=details)
    declared = "、".join(dict.fromkeys(insured_values))
    warnings.insert(0, f"按现付、普通保价测算，本次采用平台最低保价{declared}元；上楼、进仓等附加服务须另行核实")
    result = {"quotes": quotes, "warnings": warnings, "details": details}
    if len(destinations) == 2 and len(set(destinations)) == 1 and destinations[0][1]:
        result["site_name"] = destinations[0][1]
    return result
