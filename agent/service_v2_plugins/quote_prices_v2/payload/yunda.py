"""Yunda's logged-in waybill-page quote protocol (verified 2026-10-12).

Only calculation endpoints are used. The fresh page supplies account context and
ordinary-shipment defaults; no waybill is created and no old quote is reused.
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_DOWN
from html.parser import HTMLParser

if __package__:
    from .common import QuoteError, money
else:
    from common import QuoteError, money


FORM_FIELDS = tuple("""
CreatedDotCode SenderDistributionCode current_time ProductType PaymentType
GoodsType ServiceType ShippingMethods LogisticsId ItemTotalNumber Freight
ShippingType NoElevator ShippingFloor InsuredAmount DeliversReturn VipService
IsCod CollectionMoney ReturnClass WarehouseCode ExtendField6 IsPreferential
IsDiscount CrmCustomerId InGoodsType CheckHeavyWeight CheckFixedCost CheckSafeArrive
LongestSideLength UnpackWoodenBoxNum UnpackBracketNum ClassificationPlacementNum
UnpackingCountingNum IsUnpacking RepaymentPrescription IsHomeDecoration
IsFoldableBox FoldableBoxNumber OrderSource CheckBulkCargo SignType
DeliversSms1 DeliversSms2 DispatchSms IsSendMsg Del
""".split())
SMS_FIELDS = ("DeliversSms1", "DeliversSms2", "DispatchSms", "IsSendMsg", "IsCod")
LOCAL_ONLY = {"SignType", "Del", *SMS_FIELDS} - {"IsCod"}


class _Page(HTMLParser):
    """Read only the observed quote fields, never authentication/form secrets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.options = {}
        self._select = None
        self._option = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name")
        if tag == "input" and name in FORM_FIELDS:
            value = attrs.get("value", "")
            if name in self.fields and self.fields[name] != value:
                raise QuoteError("韵达原页报价字段存在歧义")
            self.fields[name] = value
        elif tag == "select":
            self._select = name if name in FORM_FIELDS else None
            if self._select:
                self.options[self._select] = []
        elif tag == "option" and self._select:
            self._option = {
                "value": attrs.get("value", ""), "text": "",
                "selected": "selected" in attrs,
            }

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"] += data

    def handle_endtag(self, tag):
        if tag == "option" and self._option is not None:
            self.options[self._select].append(self._option)
            self._option = None
        elif tag == "select" and self._select:
            options = self.options[self._select]
            selected = [option for option in options if option["selected"]]
            if len(selected) > 1 or not options:
                raise QuoteError("韵达原页报价选项存在歧义")
            self.fields[self._select] = (selected or options)[0]["value"]
            self._select = None

    def option(self, name, label):
        values = [x["value"] for x in self.options.get(name, []) if x["text"].strip() == label]
        if len(values) != 1 or not values[0]:
            raise QuoteError(f"韵达原页缺少唯一的{label}选项")
        return values[0]


def _scalar(value, label, *, blank=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise QuoteError(f"韵达{label}字段无效")
    text = str(value).strip()
    if not blank and (not text or "redacted" in text.lower()):
        raise QuoteError(f"韵达缺少{label}")
    return text


def _context(html):
    page = _Page()
    page.feed(html)
    missing = set(FORM_FIELDS) - page.fields.keys()
    if missing:
        raise QuoteError("韵达原页报价字段不完整")
    form = dict(page.fields)
    # ini_set() selects normal goods; other selections retain this fresh page's defaults.
    form["GoodsType"] = page.option("GoodsType", "正常货物")
    for field in ("CreatedDotCode", "SenderDistributionCode", "current_time", "ProductType", "PaymentType"):
        _scalar(form[field], field)
    codes = set(re.findall(r'\b(?:var|let|const)\s+CreatedByCode\s*=\s*[\'"]([^\'"]*)[\'"]', html))
    if len(codes) != 1:
        raise QuoteError("韵达原页缺少唯一的开单身份")
    form["CreatedByCode"] = _scalar(codes.pop(), "开单身份")
    page.thresholds = {}
    for name in ("$BubbleRatio", "$HeavyMinWeight"):
        values = set(re.findall(
            r"\b(?:var|let|const)\s+" + re.escape(name) + r"\s*=\s*['\"]([0-9.]+)['\"]", html,
        ))
        if len(values) != 1:
            raise QuoteError("韵达原页缺少大货计费阈值")
        page.thresholds[name] = money(values.pop())
    # Quote-only calls must start with a fresh empty waybill and ordinary options.
    if form["LogisticsId"] or form["CrmCustomerId"]:
        raise QuoteError("韵达原页不是空白报价上下文")
    for name in ("CheckHeavyWeight", "CheckFixedCost", "CheckSafeArrive", "CheckBulkCargo", *SMS_FIELDS):
        if form[name] not in {"0", "1", "2"}:
            raise QuoteError("韵达原页报价开关无效")
    form.update({
        "LogisticsId": "0", "PageType": "1", "PageName": "WaybillEntry",
        "ThirdCategory": "", "InstallPiece": "", "ReplaceId": "",
        "SenderTown": "", "SenderMustSend": "",
    })
    return page, form


def _success(response, label):
    if not isinstance(response, dict) or str(response.get("info")) != "1":
        raise QuoteError(f"韵达{label}失败，请核对完整省市区地址及账号可用范围")
    return response


def _detail_text(value):
    if value is None or value == "":
        return "无数据"
    if not isinstance(value, str):
        return "无数据（平台字段格式异常）"
    return value.strip() or "无数据"


def _site_details(row):
    # The live entry page displays these fields from the address matcher.
    details = [
        f"{label}：{_detail_text(row.get(field))}"
        for label, field in (
            ("匹配网点", "target_center"), ("网点地址", "SiteAddress"),
            ("查件电话", "qry_phone"), ("客服电话", "cxdh"),
            ("派送范围", "remark"), ("特殊区域", "special_range"),
        )
    ]
    for label, field in (
        ("业务电话", "sale_phone"), ("经理电话", "site_manager_phone"),
        ("目的分拨", "business_center"), ("覆盖类型", "CoverageName"),
        ("派送情况", "SendMsg"), ("乡镇派送说明", "TownMsg"),
        ("服务限制", "servicemsg"), ("重量限制", "LimitingMsg"),
        ("特殊区域提示", "SpecialAreaMsg"), ("大票货服务范围", "ServiceScope"),
        ("派送时效", "deliverprescription"),
    ):
        if row.get(field) not in (None, ""):
            details.append(f"{label}：{_detail_text(row[field])}")
    areas = row.get("SpecialArea")
    if areas not in (None, {}, []):
        if not isinstance(areas, dict):
            details.append("特殊区域参考：无数据（平台字段格式异常）")
        else:
            # The page lists every candidate and highlights nonzero Similarity.
            # A candidate is not evidence that this address is in that area.
            for name, area in areas.items():
                if not isinstance(area, dict):
                    details.append("特殊区域参考：无数据（平台字段格式异常）")
                    continue
                details.append(
                    f"特殊区域参考：{_detail_text(name)}"
                    f"（平台相似度：{_detail_text(area.get('Similarity'))}）；"
                    f"说明：{_detail_text(area.get('remark'))}"
                )
    return details


def _match_details(matches):
    groups = []
    for delivery, match in matches:
        if not match:
            continue
        same = next((group for group in groups if (
            match["code"] and match == group[1]
        )), None)
        if same is None:
            groups.append(([delivery], match))
        else:
            same[0].append(delivery)
    details = []
    for deliveries, match in groups:
        first, *rest = match["details"]
        details.extend(["/".join(deliveries) + first, *rest])
    return details


def _address(client, form, request, mode, match):
    body = {
        "BuyerProvince": "", "BuyerCity": "", "BuyerArea": "",
        "CreatedDotCode": form["CreatedDotCode"], "IsCod": form["IsCod"],
        "PaymentType": form["PaymentType"], "ServiceType": mode,
        "GetIsProvinceSite": "1", "IsOrderAddress": "1", "LogisticsId": "",
        "IsInternational": "0", "OrderSource": form["OrderSource"],
        "SplitWord": "1", "BuyerAddress": request["address"], "SettleNumber": form["Del"],
    }
    result = _success(client.request("yunda_address", body), "地址识别")
    data = result.get("data")
    rows = list(data.values()) if isinstance(data, dict) else data
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise QuoteError("韵达地址未匹配唯一目的网点，请补充完整地址")
    row = rows[0]
    # Retain this exact mode's matched site even when its service is restricted.
    match.update({
        "code": str(row["target_center_code"]) if isinstance(row.get("target_center_code"), (str, int))
        and not isinstance(row.get("target_center_code"), bool) else "",
        "site_name": row["target_center"].strip() if isinstance(row.get("target_center"), str) else "",
        "details": _site_details(row),
    })
    if str(row.get("info")) not in {"0", "1"}:
        raise QuoteError("韵达目的网点存在服务限制，请在原页核实")
    code = _scalar(row.get("target_center_code"), "目的网点")
    if code in {"BA", "OR", "RF", "AE", "AY"}:
        raise QuoteError("韵达地址属于盲区、超区或不可报价范围")
    if mode == "111" and str(row.get("Coverage")) == "4":
        raise QuoteError("韵达该地址仅支持自提")
    updates = {"BuyerDestinationDotCode": code}
    for dest, source in (
        ("BuyerProvince", "BuyerProvinceCode"), ("BuyerCity", "BuyerCityCode"),
        ("BuyerArea", "BuyerAreaCode"), ("DestinationDotScope", "business_center_code"),
    ):
        updates[dest] = _scalar(row.get(source), source)
    for dest, source in (("BuyerTown", "TownCode"), ("ExtendField6", "IsMustSend"), ("SpecialAreaCode", "SpecialAreaCode")):
        updates[dest] = _scalar(row.get(source), source, blank=True)
    # GisAssignment strips the exact region returned by the current address query.
    short = request["address"].strip()
    for field in ("BuyerProvince", "BuyerCity", "BuyerArea"):
        region = _scalar(row.get(field), field)
        if short.startswith(region):
            short = short[len(region):]
    if not short:
        raise QuoteError("韵达缺少省市区以外的详细地址")
    updates["BuyerAddress"] = short
    return updates


def _page_number(value):
    return format(money(value).quantize(Decimal("0.01"), rounding=ROUND_DOWN), "f")


def _amount(response, form):
    result = _success(response, "费用计算")
    data = result.get("data")
    if not isinstance(data, dict) or str(result.get("lb")) not in {"1", "2"}:
        raise QuoteError("韵达报价缺少费用层级")
    key = "CostTotal" if str(result["lb"]) == "2" else "DotCostTotal"
    total = money(data.get(key))
    message = sum((Decimal("0.05") for name in SMS_FIELDS if form[name] == "1"), Decimal(0))
    # The page adds JavaScript Numbers, then getFloatStr_1 truncates two places.
    return _page_number(str(float(total) + float(message)))


def _quote_mode(client, page, base, request, delivery, match):
    form = dict(base)
    form["ServiceType"] = page.option("ServiceType", delivery)
    form["ShippingMethods"] = page.option("ShippingMethods", "送货") if delivery == "派送" else ""
    form.update(_address(client, form, request, form["ServiceType"], match))
    form.update(GrossWeight=request["weight"], Volume=request["volume"])
    weight = _success(client.request("yunda_weight", {
        "current_time": form["current_time"], "vol": request["volume"],
        **{key: form[key] for key in (
            "CrmCustomerId", "CreatedDotCode", "BuyerDestinationDotCode", "ProductType",
            "InGoodsType", "GrossWeight", "BuyerCity", "SignType", "CheckBulkCargo",
            "CheckFixedCost", "CheckHeavyWeight",
        )},
        "LogisticsId": "",
    }), "计费重量计算")
    for field in ("data", "Tfr", "Del"):
        money(weight.get(field))
    bulk = _scalar(weight.get("CheckBulkCargo"), "大票货开关")
    if bulk not in {"0", "1", "2"}:
        raise QuoteError("韵达计费重量返回的开关无效")
    if form["CheckBulkCargo"] != "2":
        form["CheckBulkCargo"] = "1" if bulk == "1" else "0"
        if bulk == "1":
            form["CheckFixedCost"] = form["CheckHeavyWeight"] = "0"
    ratio = page.thresholds["$BubbleRatio"]
    if (
        ratio == 0 or form["CheckBulkCargo"] == "1"
        or money(request["weight"]) <= money(request["volume"]) * Decimal(1000000) / ratio
        or money(request["weight"]) <= page.thresholds["$HeavyMinWeight"]
    ):
        form["CheckHeavyWeight"] = "0"
    form["SettlementTotalNumber"] = _page_number(max(money(request["weight"]), money(weight["data"])))
    if money(form["SettlementTotalNumber"]) <= 0:
        raise QuoteError("韵达计费重量不足原页最小精度")
    insured = _success(client.request("yunda_insured", {
        "GoodsType": form["GoodsType"], "SettlementTotalNumber": form["SettlementTotalNumber"],
    }), "保价范围查询").get("data")
    if not isinstance(insured, dict):
        raise QuoteError("韵达保价范围缺失")
    minimum, maximum = money(insured.get("MIN")), money(insured.get("MAX"))
    if minimum > maximum:
        raise QuoteError("韵达保价范围无效")
    if not minimum <= money(form["InsuredAmount"]) <= maximum:
        form["InsuredAmount"] = format(minimum, "f")
    result = client.request("yunda_price", {key: value for key, value in form.items() if key not in LOCAL_ONLY})
    quote = {"label": "韵达" + delivery, "amount": _amount(result, form), "delivery_method": delivery}
    if match["site_name"]:
        quote["site_name"] = match["site_name"]
    return quote


def quote(client, request):
    page, base = _context(client.request("yunda_entry", text=True))
    quotes, warnings, matches = [], [], []
    for delivery in ("自提", "派送"):
        match = {}
        matches.append((delivery, match))
        try:
            quotes.append(_quote_mode(client, page, base, request, delivery, match))
        except QuoteError as exc:
            warnings.append(f"韵达{delivery}：{exc}")
    details = _match_details(matches)
    if not quotes:
        raise QuoteError("；".join(warnings), details=details)
    result = {"quotes": quotes, "warnings": warnings, "details": details}
    identities = {(match.get("code"), match.get("site_name")) for _, match in matches}
    if len(identities) == 1:
        code, name = identities.pop()
        if code and name:
            result["site_name"] = name
    return result
