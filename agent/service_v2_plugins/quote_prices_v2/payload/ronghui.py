"""Ronghui quote flow observed on the live cost-entry page on 2026-10-11.

Only calculation/query procedures are declared. No waybill is created or saved.
The ordinary shipment uses the platform's current minimum declared value.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

if __package__:
    from .common import QuoteError, money, positive_number
else:
    from common import QuoteError, money, positive_number

_CENTRE_EXCLUSIONS = {"571359", "5710263", "5710275"}
_DISCOUNTS = {
    "TRANSPORT_FEE_DIS", "OPERATE_FEE_DIS", "REC_DISPATCH_FEE_DIS",
    "TRANSFER_FEE_DIS", "REC_SHORTHAUL_FEE_DIS", "REC_PERIOD_FEE_DIS",
}
_FEE_FIELDS = {
    "REC_DISPATCH_FEE", "SCAN_FEE", "TRANSPORT_ALIEN_FEE", "PERIOD_FEE",
    "RELEASE_FEE", "REC_SPECIAL_CAR", "OPERATE_FEE", "BIG_GOODS_FEE",
    "REC_TOPAYMENT_CHARGE", "TRANSPORT_FEE", "SHORTHAUL_FEE_ADD",
    "REC_GOODS_CHARGE", "OPERATE_FEE_ADD", "REC_SHORTHAUL_FEE",
    "TRANSFER_FEE_ADD", "DISPATCH_FEE_ADD", "BOOT_INFORMATION_FEE",
    "REC_DELIVERY_FEE", "BOOT_TAXES_FEE", "INSURANCE_FEE", "SIGN_RETURN_FEE",
    "TARIFF_FEE", "OTHER_FEE1", "TRANSPORT_FEE_ADD", "LONG_GOODS_FEE",
    "TRANSFER_FEE", "OTHER_FEES_DKH", "OTHER_FEES_WB", "OTHER_FEE2",
    "OTHER_FEE3", "SUM_OTHER_FEE", "PICKUP_FEE_VALUE", "PAY_SUBSIDY_FEE",
}
_PRODUCTS = (("001", "融惠达"), ("002", "精准零担"), ("003", "融安达"), ("004", "融速达"))


def _text(row, key):
    value = row.get(key) if isinstance(row, dict) else None
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
        raise QuoteError(f"融辉未返回必要字段：{key}")
    return str(value).strip()


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise QuoteError(f"融辉{label}返回格式无效")
    return value


def _one(value, label):
    rows = _rows(value, label)
    if len(rows) != 1:
        raise QuoteError(f"融辉{label}无数据" if not rows else f"融辉{label}匹配不唯一")
    return rows[0]


def _query(client, name, body, label):
    return _one(client.request(name, body), label)


def _procedure(client, name, procedure, rows):
    result = client.request(name, {"procedureName": procedure, "data": json.dumps(rows, ensure_ascii=False)})
    if not isinstance(result, dict) or result.get("success") is not True or not isinstance(result.get("result"), dict):
        raise QuoteError("融辉查询或计算未成功")
    return result["result"]


def _number(value):
    return format(value, "f")


def _two(value):
    return _number(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _destination(client, request, context):
    response = client.request("ronghui_address", {
        "address": request["address"], "createMan": context["loginEmpName"],
        "createManCode": context["loginEmpCode"], "createSite": context["loginSiteName"],
        "createSiteCode": context["loginSiteCode"],
    })
    if not isinstance(response, dict) or response.get("success") is not True:
        raise QuoteError("融辉地址解析失败")
    data = response.get("data")
    if not isinstance(data, dict) or data.get("code") != "S001":
        raise QuoteError("融辉地址解析无有效结果")
    resolved = _one(data.get("result"), "地址解析")
    geo = resolved.get("geoResult")
    area = {key: _text(geo, key) for key in ("province", "city", "county")}
    area["town"] = _text(geo, "town")
    areas = _rows(resolved.get("areaResults"), "服务区域")
    names = [_text(row, "areaName") for row in areas]
    if not names:
        raise QuoteError("融辉该地址暂无服务网点")
    match = _procedure(client, "ronghui_match_site", "GET_FIRST_MATCH_SITE_ZB", [{"p_site_names": ", ".join(names)}])
    index = match.get("p_match_index")
    # The source endpoint emits integral JSON numbers such as 2.0. Browser
    # JSON.parse/CDP normalizes these to int, whereas Host json.loads preserves
    # float. Validate the numeric value without accepting strings or rounding.
    if isinstance(index, bool) or not isinstance(index, (int, float)):
        raise QuoteError("融辉未确定服务网点")
    if isinstance(index, float):
        if not math.isfinite(index) or not index.is_integer():
            raise QuoteError("融辉未确定服务网点")
        index = int(index)
    if not 1 <= index <= len(names):
        raise QuoteError("融辉未确定服务网点")
    name = _text(match, "p_match_name")
    if name != names[index - 1]:
        raise QuoteError("融辉服务网点匹配结果不一致")
    found = _query(client, "ronghui_destination_name", {"DESTINATION_NAME": name}, "目的网点")
    code = _text(found, "DESTINATION_CODE")
    if found.get("TYPE") == "二级网点":
        enabled = _text(found, "ENABLE_ACCOUNT_PURPOSE")
        purpose = _text(found, "BL_PURPOSE_SITE")
        if enabled == "1" and purpose == "0":
            code = _text(found, "SUPERIOR_SITE_CODE")
        elif enabled == "0":
            raise QuoteError("融辉目的网点未开放派送")
    destination = _query(client, "ronghui_destination", {"DESTINATION_CODE": code}, "目的网点详情")
    if _text(destination, "DESTINATION_CODE") != code:
        raise QuoteError("融辉目的网点详情与匹配结果不一致")
    return area, destination


def _centre(value, label):
    rows = _rows(value, label)
    return _one([row for row in rows if row.get("SITE_CODE") not in _CENTRE_EXCLUSIONS], label)


def _dispatch(destination):
    result = {
        "DISPATCH_UNDERLING_SITE_CODE": _text(destination, "SITE_CODE"),
        "DISPATCH_UNDERLING_SITE": _text(destination, "SITE_NAME"),
    }
    if _text(destination, "SITE_TYPE") == "二级网点":
        result.update({
            "DISPATCH_SITE_CODE": _text(destination, "SUPERIOR_FINANCE_CENTER_CODE"),
            "DISPATCH_SITE": _text(destination, "SUPERIOR_FINANCE_CENTER"),
            "DISPATCH_FINANCE_CENTER_CODE": _text(destination, "FINANCE_CENTER_CODE"),
            "DISPATCH_FINANCE_CENTER": _text(destination, "FINANCE_CENTER"),
        })
        if str(destination.get("ENABLE_ACCOUNT_PURPOSE")) == "1" and (
            str(destination.get("BL_PURPOSE_SITE")) == "1" or str(destination.get("BL_OPEN")) == "1"
        ):
            result["DISPATCH_UNDERLING_SITE_CODE"] = _text(destination, "SUPERIOR_SITE_CODE")
            result["DISPATCH_UNDERLING_SITE"] = _text(destination, "SUPERIOR_SITE")
    else:
        result.update({
            "DISPATCH_SITE_CODE": _text(destination, "SITE_CODE"),
            "DISPATCH_SITE": _text(destination, "SITE_NAME"),
            "DISPATCH_FINANCE_CENTER_CODE": _text(destination, "SUPERIOR_FINANCE_CENTER_CODE"),
            "DISPATCH_FINANCE_CENTER": _text(destination, "SUPERIOR_FINANCE_CENTER"),
        })
    return result


def _base(client, request, context, area, destination):
    site = context["loginSiteCode"]
    sender = _query(client, "ronghui_send_site", {"SITE_CODE": site}, "寄件网点")
    if _text(sender, "SITE_CODE") != site:
        raise QuoteError("融辉寄件网点与绑定账号不一致")
    send = _centre(client.request("ronghui_send_center", {"SEND_SITE_CODE": site}), "寄件中心")
    send_code = _text(send, "SITE_CODE")
    dispatch = _dispatch(destination)
    hubs = _rows(client.request("ronghui_destination_hub", {
        "SITE_CODE": dispatch["DISPATCH_SITE_CODE"], "SEND_CENTER_CODE": send_code,
    }), "目的中心指定路由")
    if hubs:
        hub = _one([row for row in hubs if row.get("SITE_CODE") not in _CENTRE_EXCLUSIONS], "目的中心指定路由")
        destination_code, destination_name = _text(hub, "DESTINATION_CENTER_CODE"), _text(hub, "DESTINATION_CENTER")
    else:
        # The live page takes this declared query branch only when no hub is configured.
        centre = _centre(client.request("ronghui_destination_center", {
            "DISPATCH_SITE_CODE": dispatch["DISPATCH_SITE_CODE"], "SEND_SITE_CENTER_CODE": send_code,
        }), "目的中心")
        destination_code, destination_name = _text(centre, "SITE_CODE"), _text(centre, "SITE_NAME")
    route = _query(client, "ronghui_route", {
        "SEND_CENTER_CODE": send_code, "DESTINATION_CENTER_CODE": destination_code,
    }, "计划路由")
    ratio = _query(client, "ronghui_weight_ratio", {
        "SEND_SITE_CODE": site, "DESTINATION_CODE": _text(destination, "DESTINATION_CODE"),
    }, "体积重比率")
    weight, volume = positive_number(request["weight"], "重量"), positive_number(request["volume"], "体积")
    volume_weight = (volume * 1000000 / positive_number(ratio.get("WEIGHT_RATIO"), "融辉体积重比率")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    settlement = max(weight, volume_weight).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    density = (weight / volume).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    goods = _query(client, "ronghui_goods_type", {"BEGIN_WEIGHT": _number(density), "END_WEIGHT": _number(density)}, "货物类型")
    now = datetime.now(timezone(timedelta(hours=8)))
    data = {
        "SEND_DATE": now.isoformat(timespec="milliseconds"), "USE_DATE": now.strftime("%Y-%m-%d %H:%M:%S"),
        "SEND_SITE_CODE": site, "SEND_SITE": context["loginSiteName"],
        "REGISTER_SITE_CODE": site, "REGISTER_SITE": context["loginSiteName"],
        "TAKE_PIECE_EMPLOYEE_CODE": context["loginEmpCode"], "TAKE_PIECE_EMPLOYEE": context["loginEmpName"],
        "REC_MAN_CODE": context["loginEmpCode"], "SEND_FINANCE_CENTER": _text(sender, "SUPERIOR_FINANCE_CENTER"),
        "SEND_FINANCE_CENTER_CODE": _text(sender, "SUPERIOR_FINANCE_CENTER_CODE"),
        "SEND_PROVINCE": _text(sender, "PROVINCE"),
        "DESTINATION_CODE": _text(destination, "DESTINATION_CODE"), "DESTINATION": _text(destination, "DESTINATION_NAME"),
        "SEND_CENTER_CODE": send_code, "SEND_CENTER": _text(send, "SITE_NAME"),
        "DESTINATION_CENTER_CODE": destination_code, "DESTINATION_CENTER": destination_name,
        "ROUTE_NAME": _text(route, "ROUTE_NAME"),
        "ACCEPT_PROVINCE": area["province"], "ACCEPT_CITY": area["city"],
        "ACCEPT_COUNTY": "|".join(area[key] for key in ("province", "city", "county")),
        "ACCEPT_TOWN": area["town"], "ACCEPT_MAN_ADDRESS": request["address"],
        "TOWN_NAME": "-".join(area[key] for key in ("city", "county", "town")),
        "CITY_NAME": "-".join(area[key] for key in ("province", "city")),
        "COUNTY_NAME": "-".join(area[key] for key in ("city", "county")),
        "BILL_WEIGHT": _two(weight), "FEE_WEIGHT": _number(weight),
        "SETTLEMENT_WEIGHT": _two(settlement), "WEIGHT": _two(settlement),
        "VOLUME": _number(volume), "VOLUME_WEIGHT": _two(volume_weight), "PAYLOAD_WEIGHT": _two(volume * 200),
        "TON_SQUARE": _two(weight / 1000 / volume),
        "GOODS_CODE": _text(goods, "GOODS_CODE"), "GOODS_TYPE": _text(goods, "GOODS_NAME"),
        "PIECE_NUMBER": "1", "CLASS_TYPE": "汽运", "SOON_PIECE_TYPE": "汽运", "CLASS": "汽运",
        "DESTINATION_TYPE": "1", "ORDER_TYPE": "", "PAYMENT_TYPE": "现金", "REBATE_CYCLE": "T+3",
        "BL_ELECTRON": "1", "BL_MESSAGE": "1", "BL_ACCEPT_MESSAGE": "1", "BL_GIS": "1",
        "COLLECTING_IS_NOT": "0", "ONLINE_PAY": "0", "TOPAYMENT": "0", "BL_UPSTAIRS": "0",
        "BL_INTO_WAREHOUSE": "0", "BL_INDENTURE": "0", "PAY_SUBSIDY_FEE": "0",
        "BOX_WEIGHT": "0", "EXCEPT_BOX_WEIGHT": "0", "INSURANCE_FEE": "0",
        "ALIEN_TYPE": "", "BL_INSURESTATUS": "", "USE_COLLAR": "",
    }
    data.update(dispatch)
    data["DISP_UNDERLING_SITE_CODE"] = dispatch["DISPATCH_UNDERLING_SITE_CODE"]
    if data["SEND_PROVINCE"] != "安徽省" and destination_code == "5510239":
        raise QuoteError("融辉当前仅开放安徽下属网点发往该海南中心")
    return data, settlement


def _sum_fees(row):
    if not isinstance(row, dict) or not row:
        raise QuoteError("融辉未返回费用明细")
    unknown = set(row) - _FEE_FIELDS - _DISCOUNTS - {"INSURANCE"}
    if unknown:
        raise QuoteError("融辉费用字段已变化，需核实原页")
    fee_keys = set(row) & (_FEE_FIELDS | _DISCOUNTS)
    if not fee_keys or all(row[key] is None for key in fee_keys):
        raise QuoteError("融辉未返回有效费用金额")
    total = Decimal("0")
    for key in fee_keys:
        if row[key] is not None:
            # The original page adds signed fee adjustments (including negative
            # TRANSFER_FEE_ADD) and separately subtracts the six discount fields.
            try:
                if isinstance(row[key], bool):
                    raise InvalidOperation
                amount = Decimal(str(row[key]))
                if not amount.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                raise QuoteError("融辉费用金额无法解析") from None
            total += amount * (-1 if key in _DISCOUNTS else 1)
    if total < 0:
        raise QuoteError("融辉费用汇总为负数")
    return _two(total)


def quote(client, request):
    context = client.context()
    context = {key: _text(context, key) for key in ("loginEmpName", "loginEmpCode", "loginSiteName", "loginSiteCode")}
    area, destination = _destination(client, request, context)
    base, settlement = _base(client, request, context, area, destination)
    level = _text(destination, "LEVELS")
    modes = ["自提"] if level == "自提部" else ["派送"] if level in {"直派部", "分拨服务大厅"} else ["自提", "派送"]
    if level in {"分拨服务大厅", "分拨代派部"}:
        raise QuoteError("融辉该目的网点需要选择专属配送服务，请在原站核实后报价")
    products = [p for p in _PRODUCTS if p[0] in ({"003", "004"} if settlement <= 800 else {"002", "003", "004"} if settlement <= 3000 else {"001", "002", "003", "004"})]
    quotes = []
    for code, name in products:
        insured = _query(client, "ronghui_insurance", {"PRODUCT_CODE": code, "WEIGHT": _two(settlement)}, "产品保价规则")
        minimum = money(insured.get("MIN_LNSURED_FEE"))
        maximum = money(insured.get("MAX_LNSURED_FEE"))
        if minimum > maximum:
            raise QuoteError("融辉产品保价规则无效")
        for mode in modes:
            payload = dict(base, PRODUCT_CODE=code, PRODUCT_TYPE=name, DISPATCH_MODE=mode, INSURANCE=_number(minimum))
            calculated = _procedure(client, "ronghui_quote", "P_CALC_CLIENT_PRICE_SHOW4", [payload])
            row = _one(calculated.get("data"), "费用计算")
            quotes.append({"label": f"{name}（{mode}）", "product_name": name, "delivery_method": mode, "amount": _sum_fees(row)})
    return {"quotes": quotes, "warnings": ["按普通货物、平台当前最低保价计算；上楼、进仓等附加服务须另行核实"]}
