"""Yunda waybill entry runtime backed by the shared Yunda session broker."""

from __future__ import annotations

import json
import base64
import binascii
import re
from html import escape, unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker


YUNDA_INMS_ORIGIN = "https://kyinms.yunda56.com"
ENTRY_INDEX_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&p=nil"
)
ENTRY_BASE = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/entry"
JOIN_LGS_BASE = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/joinlgs/MakeLogisticsApi"
SAVE_URL = f"{ENTRY_BASE}/save.html"
DRAFT_SAVE_URL = f"{ENTRY_BASE}/draftsave.html"
DRAFT_LIST_URL = f"{ENTRY_BASE}/getDraftList.html"
DRAFT_DELETE_URL = f"{ENTRY_BASE}/delDraft.html"
TEMPLATE_LIST_URL = f"{ENTRY_BASE}/getTemplateList.html"
TEMPLATE_SAVE_URL = f"{ENTRY_BASE}/saveTemplate.html"
TEMPLATE_DELETE_URL = f"{ENTRY_BASE}/delTemplate.html"
TEMPLATE_DEFAULT_URL = f"{ENTRY_BASE}/setDefaultTemplate.html"
FEEDBACK_ADDRESS_URL = f"{ENTRY_BASE}/feedbackAddress.html"
FEEDBACK_COST_URL = f"{ENTRY_BASE}/feedbackCost.html"
DOWNLOAD_TEMPLATE_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/download/logisticstemplate.xlsx"
RETURN_UPLOAD_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/ReturnUpload.html"
FEEDBACK_COST_UPLOAD_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/reportforms/sendCostFeedback/upload.html"
)
GET_LOGISTICS_NUM_URL = f"{JOIN_LGS_BASE}/getLogisticsNum.html"
ADDRESS_ANALYSIS_URL = f"{JOIN_LGS_BASE}/getAddressAnalysis.html"
ADDRESS_SITE_URL = f"{JOIN_LGS_BASE}/getAchieveSiteByAddress.html"
CHECK_LOGISTICS_URL = f"{JOIN_LGS_BASE}/checkLogisticsId.html"
CHECK_CLOSE_ROUTE_URL = f"{JOIN_LGS_BASE}/checkCloseRoute.html"
CHECK_LIMIT_WEIGHT_URL = f"{JOIN_LGS_BASE}/checkLimitWeight.html"
CHECK_PAYMENT_URL = f"{JOIN_LGS_BASE}/checkPaymentAndLmpsb.html"
CHECK_SERVICE_SCOPE_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/checkServiceScope.html"
PRICE_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/price.html"
ELEC_STOCK_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/elecStock.html"
CURRENT_TIME_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/getCurrentTime.html"

PRINTER_ENDPOINT_NAMES = {
    "child": "printer_sub_index",
    "master": "printer_main_index",
    "triplicate": "printer_san_index",
    "receipt-label": "printer_returnLabel_tip",
}
PRINT_ENDPOINTS = {
    "child": f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/printer/sub/index.html",
    "master": f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/printer/main/index.html",
    "triplicate": f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/printer/san/index.html",
    "receipt-label": f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/printer/returnLabel/tip.html",
}

DEFAULT_TIMEOUT_SEC = 30

SECTION_FIELDS = [
    ("基础信息", ("LogisticsId", "ProductType", "IsInternational", "PackageByCode", "OpenDate")),
    (
        "寄方信息",
        (
            "SenderName",
            "CreatedDotname",
            "SenderCompany",
            "SenderAddress",
            "SenderMobile",
            "SenderPhone",
            "SenderDistributionName",
            "CrmCustomerId",
            "IdNumber",
            "DeliversSms",
        ),
    ),
    (
        "货物信息",
        (
            "VolDetail",
            "ItemName",
            "ItemTotalNumber",
            "PackingType1",
            "PackingType2",
            "PackingType3",
            "PackingType4",
            "Piece",
            "GoodsType",
            "GrossWeight",
            "SettlementTotalNumber",
            "Volume",
            "Tfr",
            "DeliversReturn",
            "ReturnLogisticsId",
        ),
    ),
    (
        "收费信息",
        (
            "PaymentType",
            "Freight",
            "InsuredAmount",
            "ServiceCharge",
            "OtherFee",
            "TotalMoney",
            "CollectionMoney",
            "COD",
            "Remarks",
        ),
    ),
    (
        "收方信息",
        (
            "BuyerName",
            "BuyerCompany",
            "BuyerProvince",
            "BuyerCity",
            "BuyerArea",
            "BuyerTown",
            "BuyerAddress",
            "BuyerDestinationDotName",
            "BuyerDestinationDistributionName",
            "DispatchRemark",
            "BuyerMobile",
            "BuyerPhone",
            "BuyerSms",
        ),
    ),
    (
        "服务信息",
        ("Unpacking", "ServiceMode", "DispatchMode", "CollectionMoney", "VipService"),
    ),
    (
        "辅助信息",
        (
            "CreatedDotCode",
            "SenderDistributionCode",
            "BuyerDestinationDotCode",
            "BuyerDestinationDistributionCode",
            "RouteName",
            "SpecialAreaName",
            "DotPhone",
        ),
    ),
]

FIELD_LABELS = {
    "LogisticsId": "电子单号",
    "ProductType": "产品类型",
    "IsInternational": "国际件",
    "PackageByCode": "收件业务员",
    "OpenDate": "开单日期",
    "SenderName": "寄件人",
    "CreatedDotname": "寄件网点",
    "SenderCompany": "寄件公司",
    "SenderAddress": "寄件地址",
    "SenderMobile": "寄件手机",
    "SenderPhone": "寄件座机",
    "SenderDistributionName": "首发分拨",
    "CrmCustomerId": "客户名称",
    "IdNumber": "寄件身份证",
    "DeliversSms": "签收短信",
    "VolDetail": "体积明细",
    "ItemName": "物品名称",
    "ItemTotalNumber": "总件数",
    "PackingType1": "包装类型1",
    "PackingType2": "包装类型2",
    "PackingType3": "包装类型3",
    "PackingType4": "包装类型4",
    "Piece": "件数",
    "GoodsType": "货物类型",
    "GrossWeight": "实际重量",
    "SettlementTotalNumber": "结算重量",
    "Volume": "体积",
    "Tfr": "体积重",
    "DeliversReturn": "回单服务",
    "ReturnLogisticsId": "回单号",
    "PaymentType": "支付类型",
    "Freight": "运费",
    "InsuredAmount": "申明价值",
    "ServiceCharge": "服务保障费",
    "OtherFee": "其他费用",
    "TotalMoney": "总金额",
    "CollectionMoney": "代收货款",
    "COD": "到付款",
    "Remarks": "备注",
    "BuyerName": "收件人",
    "BuyerCompany": "收件公司",
    "BuyerProvince": "收方省",
    "BuyerCity": "收方市",
    "BuyerArea": "收方区县",
    "BuyerTown": "收方街道",
    "BuyerAddress": "详细地址",
    "BuyerDestinationDotName": "目的网点",
    "BuyerDestinationDistributionName": "目的分拨",
    "DispatchRemark": "派件说明",
    "BuyerMobile": "收件手机",
    "BuyerPhone": "收件座机",
    "BuyerSms": "收件短信",
    "Unpacking": "拆包服务",
    "ServiceMode": "服务方式",
    "DispatchMode": "送货方式",
    "VipService": "增值服务",
    "RouteName": "时效线路",
    "SpecialAreaName": "特殊范围",
    "DotPhone": "网点联系方式",
}

CHECKBOX_FIELDS = {"IsInternational", "DeliversSms", "BuyerSms", "DeliversReturn", "Unpacking"}

UI_OPTION_ALIASES = {
    "ProductType": ("ProductType", "ProductType_", "ProductTypeText"),
    "IsInternational": ("IsInternational", "IsInternational_", "International"),
    "PackageByCode": ("PackageByCode", "PackageByCodeText", "BusinessUser", "ReceiverStaff"),
    "SenderDistributionName": ("SenderDistributionName", "SubLogisticsName", "SubLogistics"),
    "BuyerProvince": ("BuyerProvince", "ActualBuyerProvince"),
    "BuyerCity": ("BuyerCity", "ActualBuyerCity"),
    "BuyerArea": ("BuyerArea", "BuyerCounty", "ActualBuyerTown"),
    "BuyerTown": ("BuyerTown", "ActualBuyerVillage"),
    "PackingType1": ("PackingType1",),
    "PackingType2": ("PackingType2",),
    "PackingType3": ("PackingType3",),
    "PackingType4": ("PackingType4",),
    "GoodsType": ("GoodsType", "InGoodsType", "InGoodsTypeText"),
    "ReturnType": ("ReturnType", "ReceiptType"),
    "ServiceMode": ("ServiceMode", "ServiceType", "ServiceType_"),
    "DispatchMode": ("DispatchMode", "ShippingMethods", "DispatchType"),
    "PaymentType": ("PaymentType", "SettlementType", "SettlementType_"),
}

FIELD_ALIASES = {
    "OpenDate": ("OpenDate", "current_time", "start"),
    "SenderName": ("SenderName", "SenderMan", "Sender"),
    "SenderMobile": ("SenderMobile", "SenderMoblie", "SenderTelephone"),
    "SenderDistributionName": ("SenderDistributionName", "SubLogisticsName", "SubLogistics"),
    "CrmCustomerId": ("CrmCustomerId", "CustomerName", "CustomerId"),
    "BuyerName": ("BuyerName", "ActualBuyerName"),
    "BuyerMobile": ("BuyerMobile", "BuyerMoblie", "ActualBuyerMobile"),
    "BuyerProvince": ("BuyerProvince", "ActualBuyerProvince"),
    "BuyerCity": ("BuyerCity", "ActualBuyerCity"),
    "BuyerArea": ("BuyerArea", "BuyerCounty", "ActualBuyerTown"),
    "BuyerTown": ("BuyerTown", "ActualBuyerVillage"),
    "BuyerDestinationDotName": ("BuyerDestinationDotName", "DestinationDotName", "BuyerDestinationDotNamefeedback"),
    "BuyerDestinationDistributionName": ("BuyerDestinationDistributionName", "DestinationSubLogisticsName"),
    "DispatchRemark": ("DispatchRemark", "DeliveryRemark", "BuyerDestinationDotNamefeedback"),
    "GoodsType": ("GoodsType", "InGoodsType", "InGoodsTypeText"),
    "ServiceMode": ("ServiceMode", "ServiceType", "ServiceType_"),
    "DispatchMode": ("DispatchMode", "ShippingMethods", "DispatchType"),
    "PaymentType": ("PaymentType", "SettlementType", "SettlementType_"),
    "InsuredAmountMoney": ("InsuredAmountMoney", "ServiceGuaranteeFee"),
    "OtherMoney": ("OtherMoney", "OtherFee"),
    "TransferCost": ("TransferCost", "DotTransferCost"),
    "AddedServiceCost": ("AddedServiceCost", "ValueAddedCost", "33"),
    "PlatformCost": ("PlatformCost", "5"),
    "OtherCost": ("OtherCost", "333"),
}

ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _aliases in {**UI_OPTION_ALIASES, **FIELD_ALIASES}.items():
    ALIAS_TO_CANONICAL[_canonical] = _canonical
    for _alias in _aliases:
        ALIAS_TO_CANONICAL[_alias] = _canonical


class _EntryFieldParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.current_select_key: str | None = None
        self.current_option: dict[str, Any] | None = None
        self.current_textarea_key: str | None = None
        self._textarea_chunks: list[str] = []

    def _field_key(self, attrs: dict[str, str]) -> str:
        return str(attrs.get("name") or attrs.get("id") or "").strip()

    def _ensure_field(self, attrs: dict[str, str], *, tag: str) -> str | None:
        key = self._field_key(attrs)
        if not key:
            return None
        value = attrs.get("value", "")
        if key not in self.fields:
            field_type = attrs.get("type", "text").lower() if tag == "input" else tag
            self.fields[key] = {
                "name": attrs.get("name", ""),
                "id": attrs.get("id", ""),
                "tag": tag,
                "type": field_type,
                "value": value,
                "placeholder": attrs.get("placeholder", ""),
                "readonly": "readonly" in attrs,
                "disabled": "disabled" in attrs,
                "hidden": field_type == "hidden",
                "checked": "checked" in attrs,
                "label": FIELD_LABELS.get(key, key),
                "options": [],
            }
            self.order.append(key)
        else:
            field = self.fields[key]
            if value and not _clean_text(field.get("value")):
                field["value"] = value
            if attrs.get("name") and not _clean_text(field.get("name")):
                field["name"] = attrs.get("name", "")
            if attrs.get("id") and not _clean_text(field.get("id")):
                field["id"] = attrs.get("id", "")
            if "checked" in attrs:
                field["checked"] = True
        return key

    @staticmethod
    def _attrs_map(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        mapped: dict[str, str] = {}
        for key, value in attrs:
            mapped[key.lower()] = "" if value is None else value
        return mapped

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = self._attrs_map(attrs)
        if tag == "input":
            self._ensure_field(attr_map, tag=tag)
            return
        if tag == "select":
            self.current_select_key = self._ensure_field(attr_map, tag=tag)
            return
        if tag == "textarea":
            self.current_textarea_key = self._ensure_field(attr_map, tag=tag)
            self._textarea_chunks = []
            return
        if tag == "option" and self.current_select_key:
            self.current_option = {
                "value": attr_map.get("value", ""),
                "selected": "selected" in attr_map,
                "text": "",
            }

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self.current_select_key = None
            return
        if tag == "option" and self.current_select_key and self.current_option:
            option = dict(self.current_option)
            option["text"] = option["text"].strip()
            self.fields[self.current_select_key]["options"].append(option)
            if option["selected"] and option["value"]:
                self.fields[self.current_select_key]["value"] = option["value"]
            self.current_option = None
            return
        if tag == "textarea" and self.current_textarea_key:
            self.fields[self.current_textarea_key]["value"] = "".join(self._textarea_chunks).strip()
            self.current_textarea_key = None
            self._textarea_chunks = []

    def handle_data(self, data: str) -> None:
        if self.current_option is not None:
            self.current_option["text"] += data
        if self.current_textarea_key:
            self._textarea_chunks.append(data)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _auth_if_login_response(response: Any, body: str) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {301, 302, 401, 403}:
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录韵达账号。")
    content_type = str(getattr(response, "headers", {}).get("content-type") or "").lower()
    lower_body = body.lower()
    response_url = str(getattr(response, "url", "") or "").lower()
    location = str(getattr(response, "headers", {}).get("location") or "").lower()
    login_url = any(marker in response_url or marker in location for marker in ("ky-sso", "/login", "login.html"))
    password_form = any(
        marker in lower_body
        for marker in ('type="password"', "type='password'", 'name="password"', "name='password'")
    )
    sso_redirect = re.search(
        r"(?:window\.)?location(?:\.href)?\s*=\s*['\"][^'\"]*(?:ky-sso|sso\.yunda56\.com)[^'\"]*(?:login|passport)",
        lower_body,
    ) is not None
    explicit_login_page = (
        "login-form" in lower_body
        or "loginform" in lower_body
        or "\u9a8c\u8bc1\u7801\u767b\u5f55" in body
        or "\u5bc6\u7801\u767b\u5f55" in body
    )
    if "text/html" in content_type and (login_url or password_form or sso_redirect or explicit_login_page):
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录韵达账号。")
    if status_code == 200 and not body.strip():
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达接口返回空响应，请重新登录韵达账号。")


def _decode_json_response(response: Any, *, label: str) -> Any:
    body = str(getattr(response, "text", "") or "")
    _auth_if_login_response(response, body)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    try:
        return response.json()
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"韵达{label}接口返回非 JSON: {body[:200]}") from exc


def _decode_text_response(response: Any, *, label: str) -> str:
    body = str(getattr(response, "text", "") or "")
    _auth_if_login_response(response, body)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    return body


def _decode_remote_action_response(response: Any, *, label: str) -> dict[str, Any]:
    body = str(getattr(response, "text", "") or "")
    _auth_if_login_response(response, body)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    content_type = str(getattr(response, "headers", {}).get("content-type") or "").lower()
    payload = None
    if "json" in content_type or body.strip().startswith(("{", "[")):
        try:
            payload = response.json() if hasattr(response, "json") else json.loads(body)
        except Exception as exc:
            raise RuntimeError(f"韵达{label}接口返回异常 JSON: {body[:200]}") from exc
    return {
        "content_type": content_type,
        "text": body,
        "json": payload,
    }


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for value in (
        payload.get("rows"),
        data.get("rows"),
        data.get("list"),
        data.get("records"),
        payload.get("records"),
        payload.get("items"),
        data.get("items"),
    ):
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _extract_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("error", "message", "msg", "info_msg", "resultMsg"):
            value = _clean_text(payload.get(key))
            if value:
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("error", "message", "msg", "info_msg", "resultMsg"):
                value = _clean_text(data.get(key))
                if value:
                    return value
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback


def _normalize_field_value(field: dict[str, Any], value: Any) -> str:
    text = _clean_text(value)
    if field.get("type") == "checkbox":
        if isinstance(value, bool):
            return "1" if value else "0"
        if text.lower() in {"1", "true", "on", "yes"}:
            return "1"
        return "0"
    return text


def _build_entry_sections(fields: dict[str, dict[str, Any]], order: list[str]) -> list[dict[str, Any]]:
    visible = [name for name in order if name in fields and not fields[name].get("hidden")]
    assigned: set[str] = set()
    sections: list[dict[str, Any]] = []
    for title, names in SECTION_FIELDS:
        section_fields = [fields[name] for name in names if name in fields and not fields[name].get("hidden")]
        if section_fields:
            sections.append({"title": title, "fields": section_fields})
            assigned.update(field["name"] or field["id"] or field["label"] for field in section_fields)
    extras = [
        fields[name]
        for name in visible
        if (fields[name].get("name") or fields[name].get("id") or fields[name].get("label")) not in assigned
    ]
    if extras:
        sections.append({"title": "补充字段", "fields": extras})
    return sections


def _field_matches(field: dict[str, Any], alias: str) -> bool:
    return alias in {
        _clean_text(field.get("name")),
        _clean_text(field.get("id")),
        _clean_text(field.get("label")),
    }


def _find_field(fields: dict[str, dict[str, Any]], aliases: tuple[str, ...]) -> dict[str, Any] | None:
    for alias in aliases:
        direct = fields.get(alias)
        if isinstance(direct, dict):
            return direct
        for field in fields.values():
            if isinstance(field, dict) and _field_matches(field, alias):
                return field
    return None


def _normalize_options(options: Any) -> list[dict[str, str]]:
    if not isinstance(options, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for option in options:
        if not isinstance(option, dict):
            continue
        value = _clean_text(option.get("value") or option.get("id") or option.get("code") or option.get("text"))
        text = _clean_text(option.get("text") or option.get("name") or option.get("label") or option.get("value"))
        key = (value, text)
        if not value and not text:
            continue
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"value": value, "text": text, "selected": bool(option.get("selected"))})
    return normalized


def _build_ui_options(page_context: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    fields = page_context.get("fields") if isinstance(page_context.get("fields"), dict) else {}
    output: dict[str, list[dict[str, str]]] = {}
    for canonical, aliases in UI_OPTION_ALIASES.items():
        options: list[dict[str, str]] = []
        for alias in (canonical, *aliases):
            field = _find_field(fields, (alias,))
            if field:
                options.extend(_normalize_options(field.get("options")))
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for option in options:
            key = (option.get("value", ""), option.get("text", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(option)
        if deduped:
            output[canonical] = deduped
    return output


def _build_visible_defaults(page_context: dict[str, Any]) -> dict[str, str]:
    defaults = page_context.get("default_form") if isinstance(page_context.get("default_form"), dict) else {}
    output: dict[str, str] = {}
    for canonical, aliases in {**UI_OPTION_ALIASES, **FIELD_ALIASES}.items():
        for alias in (canonical, *aliases):
            value = _clean_text(defaults.get(alias))
            if value:
                output[canonical] = value
                break
    for key in FIELD_LABELS:
        value = _clean_text(defaults.get(key))
        if value and key not in output:
            output[key] = value
    return output


def _parse_entry_page(html: str) -> dict[str, Any]:
    parser = _EntryFieldParser()
    parser.feed(html)
    fields = parser.fields
    default_form: dict[str, str] = {}
    hidden_fields: dict[str, str] = {}
    for key in parser.order:
        field = fields[key]
        name = str(field.get("name") or key)
        value = _clean_text(field.get("value"))
        default_form[name] = value
        if field.get("hidden"):
            hidden_fields[name] = value
    return {
        "fields": fields,
        "field_order": parser.order,
        "default_form": default_form,
        "hidden_fields": hidden_fields,
        "sections": _build_entry_sections(fields, parser.order),
    }


def _fetch_entry_context(session: Any) -> dict[str, Any]:
    response = session.get(
        ENTRY_INDEX_URL,
        headers={"Referer": ENTRY_INDEX_URL},
        allow_redirects=True,
        timeout=DEFAULT_TIMEOUT_SEC,
    )
    html = _decode_text_response(response, label="运单录入页")
    parsed = _parse_entry_page(html)
    return {
        **parsed,
        "html": html,
        "page_url": str(getattr(response, "url", "") or ENTRY_INDEX_URL),
    }


def _optional_json_get(session: Any, url: str, *, referer: str, label: str) -> Any:
    try:
        response = session.get(
            url,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=True,
            timeout=DEFAULT_TIMEOUT_SEC,
        )
        return _decode_json_response(response, label=label)
    except TMSAuthStateError:
        raise
    except Exception:
        return None


def _optional_json_post(session: Any, url: str, *, form: dict[str, str] | None = None, referer: str, label: str) -> Any:
    try:
        response = _post_form(session, url, form=form or {}, referer=referer)
        return _decode_json_response(response, label=label)
    except TMSAuthStateError:
        raise
    except Exception:
        return None


def _extract_current_time(payload: Any) -> str:
    if isinstance(payload, str):
        return _clean_text(payload)
    if isinstance(payload, dict):
        for key in ("current_time", "time", "date", "data", "result"):
            value = payload.get(key)
            if isinstance(value, (str, int, float)) and _clean_text(value):
                return _clean_text(value)
            if isinstance(value, dict):
                nested = _extract_current_time(value)
                if nested:
                    return nested
    return ""


def _fetch_remote_context(session: Any, *, referer: str) -> dict[str, Any]:
    electronic_stock = _optional_json_get(session, ELEC_STOCK_URL, referer=referer, label="电子余量")
    current_time_payload = _optional_json_post(session, CURRENT_TIME_URL, referer=referer, label="当前时间")
    current_time = _extract_current_time(current_time_payload)
    return {
        "electronic_stock": electronic_stock,
        "current_time": current_time,
        "printer_endpoint_names": PRINTER_ENDPOINT_NAMES,
    }


def _prepare_form(
    form: dict[str, Any] | None,
    *,
    page_context: dict[str, Any],
) -> dict[str, str]:
    defaults = dict(page_context.get("default_form") or {})
    fields = page_context.get("fields") if isinstance(page_context.get("fields"), dict) else {}
    form = form or {}
    for key, value in form.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        field = fields.get(key_text) if isinstance(fields.get(key_text), dict) else {"type": "text"}
        defaults[key_text] = _normalize_field_value(field, value)
    defaults.setdefault("page", "tab")
    defaults.setdefault("p", "nil")
    defaults.setdefault("IsNew", defaults.get("IsNew") or "1")
    defaults.setdefault("ReplaceCheckMsg", defaults.get("ReplaceCheckMsg") or "")
    defaults.setdefault("checkData", defaults.get("checkData") or "")
    if "LogisticsId" in defaults:
        defaults["LogisticsId"] = re.sub(r"\s+", "", defaults["LogisticsId"])
    return defaults


def _post_form(session: Any, url: str, *, form: dict[str, str], referer: str) -> Any:
    return session.post(
        url,
        data=form,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": YUNDA_INMS_ORIGIN,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        },
        allow_redirects=True,
        timeout=DEFAULT_TIMEOUT_SEC,
    )


def _get_html(session: Any, url: str, *, referer: str) -> Any:
    return session.get(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer,
        },
        allow_redirects=True,
        timeout=DEFAULT_TIMEOUT_SEC,
    )


def _decode_upload_file(context: dict[str, Any]) -> tuple[str, str, bytes]:
    upload = context.get("upload_file") if isinstance(context.get("upload_file"), dict) else {}
    filename = _clean_text(upload.get("filename")) or "upload.bin"
    filename = re.sub(r"[\\/\r\n]+", "_", filename).strip("._ ") or "upload.bin"
    content_type = _clean_text(upload.get("content_type")) or "application/octet-stream"
    raw_data = _clean_text(upload.get("data_base64") or upload.get("base64"))
    if "," in raw_data and raw_data.lower().startswith("data:"):
        raw_data = raw_data.split(",", 1)[1]
    if not raw_data:
        raise ValueError("请选择要上传的文件。")
    try:
        content = base64.b64decode(raw_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("上传文件内容格式不正确。") from exc
    if not content:
        raise ValueError("上传文件为空。")
    return filename, content_type, content


def _post_upload(session: Any, url: str, *, context: dict[str, Any], referer: str) -> Any:
    filename, content_type, content = _decode_upload_file(context)
    return session.post(
        url,
        files={"file": (filename, content, content_type)},
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": YUNDA_INMS_ORIGIN,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        },
        allow_redirects=True,
        timeout=DEFAULT_TIMEOUT_SEC,
    )


def _build_print_url(print_kind: str, prepared_form: dict[str, str]) -> str:
    base_url = PRINT_ENDPOINTS.get(print_kind, "")
    waybill_no = _clean_text(prepared_form.get("LogisticsId"))
    if not base_url or not waybill_no:
        return base_url
    query: dict[str, str] = {"ids": waybill_no}
    if print_kind == "child":
        query.update({"method": "sub", "getTemplateType": "new"})
    elif print_kind == "triplicate":
        query.update({"method": "san", "getTemplateType": "new"})
    elif print_kind == "receipt-label":
        query.update({"getTemplateType": "new"})
    return f"{base_url}?{urlencode(query)}"


def _feedback_address_payload(prepared_form: dict[str, str]) -> dict[str, str]:
    return {
        "BuyerAddress": _clean_text(prepared_form.get("BuyerAddress")),
        "BuyerProvince": _clean_text(prepared_form.get("BuyerProvince")),
        "BuyerCity": _clean_text(prepared_form.get("BuyerCity")),
        "BuyerArea": _clean_text(prepared_form.get("BuyerArea")),
        "MatchingBuyerAddress": _clean_text(
            prepared_form.get("MatchingBuyerAddress") or prepared_form.get("BuyerAddress")
        ),
        "MatchingBuyerDotCode": _clean_text(
            prepared_form.get("MatchingBuyerDotCode") or prepared_form.get("BuyerDestinationDotCode")
        ),
        "MatchingBuyerTownCode": _clean_text(
            prepared_form.get("MatchingBuyerTownCode") or prepared_form.get("BuyerTown")
        ),
        "AbnormalType": _clean_text(prepared_form.get("AbnormalType")) or "1",
        "ActualBuyerDotCode": _clean_text(prepared_form.get("ActualBuyerDotCode")),
        "ActualBuyerTown": _clean_text(prepared_form.get("ActualBuyerTown")),
        "PreWay": _clean_text(prepared_form.get("PreWay")),
    }


def _feedback_cost_payload(prepared_form: dict[str, str]) -> dict[str, str]:
    return {
        "CreatedDotCode": _clean_text(prepared_form.get("CreatedDotCode")),
        "PictureUrl1": _clean_text(prepared_form.get("PictureUrl1")),
        "PictureUrl2": _clean_text(prepared_form.get("PictureUrl2")),
        "SendCost": _clean_text(prepared_form.get("SendCost") or prepared_form.get("DotSendCost")),
        "SettlementTotalNumber": _clean_text(prepared_form.get("SettlementTotalNumber")),
        "SenderDistributionCode": _clean_text(prepared_form.get("SenderDistributionCode")),
        "BuyerDestinationDotCode": _clean_text(prepared_form.get("BuyerDestinationDotCode")),
    }


def _with_base_href(html: str, *, base_url: str) -> str:
    html = str(html or "")
    if not html.strip():
        return html
    base = f'<base href="{escape(base_url, quote=True)}">'
    if re.search(r"<base\b", html, flags=re.I):
        return html
    head_match = re.search(r"<head[^>]*>", html, flags=re.I)
    if head_match:
        return html[: head_match.end()] + base + html[head_match.end() :]
    return f"<!doctype html><html><head>{base}<meta charset='utf-8'></head><body>{html}</body></html>"


def _json_preview_html(payload: Any, *, title: str) -> str:
    body = escape(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:Arial,'Microsoft YaHei',sans-serif;padding:24px;}"
        "pre{white-space:pre-wrap;background:#f8fafc;border:1px solid #d1d5db;border-radius:8px;padding:16px;}"
        "</style></head><body>"
        f"<h1>{escape(title)}</h1><pre>{body}</pre></body></html>"
    )


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _set_patch_value(patch: dict[str, str], key: str, value: Any, *, prepared_form: dict[str, str]) -> None:
    key = _clean_text(key)
    if not key:
        return
    canonical = ALIAS_TO_CANONICAL.get(key, key)
    if canonical not in FIELD_LABELS and canonical not in prepared_form and key not in prepared_form:
        return
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "1" if value else "0"
    else:
        text = _clean_text(value)
    patch[canonical] = text


def _patch_from_payload(payload: Any, *, prepared_form: dict[str, str], depth: int = 0) -> dict[str, str]:
    patch: dict[str, str] = {}
    if payload is None or depth > 4:
        return patch
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return _patch_from_payload(json.loads(text), prepared_form=prepared_form, depth=depth + 1)
            except Exception:
                return patch
        return patch
    if isinstance(payload, list):
        for item in payload[:20]:
            patch.update(_patch_from_payload(item, prepared_form=prepared_form, depth=depth + 1))
        return patch
    if not isinstance(payload, dict):
        return patch
    for key, value in payload.items():
        if _is_scalar(value):
            _set_patch_value(patch, key, value, prepared_form=prepared_form)
        else:
            patch.update(_patch_from_payload(value, prepared_form=prepared_form, depth=depth + 1))
    return patch


def _extract_logistics_no(payload: Any) -> str:
    if isinstance(payload, str):
        text = _clean_text(payload)
        return text if re.fullmatch(r"[A-Za-z0-9-]{6,}", text) else ""
    if isinstance(payload, dict):
        for key in ("LogisticsId", "logisticsId", "mailno", "MailNo", "waybill_no", "waybillNo", "data", "result"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                text = _clean_text(value)
                if re.fullmatch(r"[A-Za-z0-9-]{6,}", text):
                    return text
            elif isinstance(value, (dict, list)):
                nested = _extract_logistics_no(value)
                if nested:
                    return nested
    if isinstance(payload, list):
        for item in payload:
            nested = _extract_logistics_no(item)
            if nested:
                return nested
    return ""


def _extract_child_waybills(payload: Any, *, depth: int = 0) -> list[dict[str, str]]:
    if payload is None or depth > 5:
        return []
    rows: list[dict[str, str]] = []
    child_key_re = re.compile(r"(?:child|sub|son|zidan|子单)", re.IGNORECASE)
    waybill_re = re.compile(r"[A-Za-z0-9-]{6,}")

    if isinstance(payload, str):
        for value in waybill_re.findall(payload):
            rows.append({"waybill_no": value})
        return rows
    if isinstance(payload, list):
        for item in payload[:200]:
            rows.extend(_extract_child_waybills(item, depth=depth + 1))
        return rows
    if not isinstance(payload, dict):
        return rows

    def row_from_dict(value: dict[str, Any]) -> dict[str, str] | None:
        waybill_no = (
            _clean_text(value.get("LogisticsId"))
            or _clean_text(value.get("logisticsId"))
            or _clean_text(value.get("waybill_no"))
            or _clean_text(value.get("waybillNo"))
            or _clean_text(value.get("mailno"))
            or _clean_text(value.get("billCode"))
            or _clean_text(value.get("code"))
        )
        if not waybill_no:
            return None
        return {
            "waybill_no": waybill_no,
            "destination": _clean_text(value.get("destination") or value.get("dotName") or value.get("siteName")),
            "remark": _clean_text(value.get("remark") or value.get("remarks") or value.get("memo")),
        }

    for key, value in payload.items():
        key_text = _clean_text(key)
        if child_key_re.search(key_text):
            if isinstance(value, dict):
                row = row_from_dict(value)
                if row:
                    rows.append(row)
            elif isinstance(value, list):
                for item in value[:200]:
                    if isinstance(item, dict):
                        row = row_from_dict(item)
                        if row:
                            rows.append(row)
                        else:
                            rows.extend(_extract_child_waybills(item, depth=depth + 1))
                    else:
                        rows.extend(_extract_child_waybills(item, depth=depth + 1))
            else:
                for waybill_no in waybill_re.findall(_clean_text(value)):
                    rows.append({"waybill_no": waybill_no})
        elif isinstance(value, (dict, list)):
            rows.extend(_extract_child_waybills(value, depth=depth + 1))

    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        waybill_no = _clean_text(row.get("waybill_no"))
        if not waybill_no or waybill_no in seen:
            continue
        seen.add(waybill_no)
        unique.append({**row, "waybill_no": waybill_no})
    return unique


def _panels_from_data(
    *,
    action: str,
    prepared_form: dict[str, str],
    payload: Any = None,
    checks: Any = None,
    price: Any = None,
) -> dict[str, Any]:
    patch = {**prepared_form, **_patch_from_payload(payload, prepared_form=prepared_form)}
    destination = {
        "dot_name": _clean_text(patch.get("BuyerDestinationDotName")),
        "distribution": _clean_text(patch.get("BuyerDestinationDistributionName")),
        "town": _clean_text(patch.get("BuyerTown")),
        "dot_address": _clean_text(patch.get("Site_Address")),
    }
    route = {
        "route": _clean_text(patch.get("RouteName") or patch.get("route") or "站"),
        "sign_time": _clean_text(patch.get("EstimatedSignTime")),
        "total_time": _clean_text(patch.get("TotalTime")),
    }
    contacts = {
        "manager_name": _clean_text(patch.get("manager_name")),
        "manager_employee_name": _clean_text(patch.get("manager_employee_name")),
        "cxdh": _clean_text(patch.get("cxdh")),
        "qry_phone": _clean_text(patch.get("qry_phone")),
        "sale_phone": _clean_text(patch.get("sale_phone")),
        "Site_Address": _clean_text(patch.get("Site_Address")),
    }
    children = _extract_child_waybills(payload)
    panels: dict[str, Any] = {}
    if action == "address-analysis":
        panels["addressAnalysis"] = payload
    if action == "address-resolution":
        panels["addressResolution"] = payload
    if checks is not None:
        panels["checks"] = checks
    if price is not None:
        panels["price"] = price
    if any(destination.values()):
        panels["destination"] = destination
    if any(route.values()):
        panels["route"] = route
    if any(contacts.values()):
        panels["contacts"] = contacts
    if children:
        panels["children"] = children
    return panels


def _list_by_id(payload: Any, item_id: str) -> dict[str, Any] | None:
    item_id = _clean_text(item_id)
    if not item_id:
        return None
    for row in _extract_rows(payload):
        for key in ("id", "Id", "draft_id", "template_id", "ModuleId", "moduleId"):
            if _clean_text(row.get(key)) == item_id:
                return row
    return None


def _normalize_save_result(payload: Any, *, prepared_form: dict[str, str]) -> dict[str, Any]:
    info = _clean_text(payload.get("info")) if isinstance(payload, dict) else ""
    error_code = _clean_text(payload.get("error_code")) if isinstance(payload, dict) else ""
    if error_code in {"7777", "7418"}:
        return {
            "ok": False,
            "message": _extract_message(payload, "韵达开单失败。"),
            "field_errors": {},
            "data": {
                "waybill_no": _clean_text(payload.get("LogisticsId")) or prepared_form.get("LogisticsId", ""),
                "normalized_form": prepared_form,
            },
            "raw": payload,
        }
    if info == "1" or (isinstance(payload, dict) and payload.get("ok") is True):
        return {
            "ok": True,
            "message": _extract_message(payload, "韵达开单成功。"),
            "data": {
                "waybill_no": _clean_text(payload.get("LogisticsId")) or prepared_form.get("LogisticsId", ""),
                "normalized_form": prepared_form,
                "print_enabled": True,
            },
            "raw": payload,
        }
    if info in {"3", "5"}:
        return {
            "ok": False,
            "message": _extract_message(payload, "韵达校验未通过。"),
            "field_errors": {},
            "data": {
                "waybill_no": prepared_form.get("LogisticsId", ""),
                "normalized_form": prepared_form,
            },
            "raw": payload,
        }
    return {
        "ok": False,
        "message": _extract_message(payload, "韵达开单失败。"),
        "field_errors": {},
        "data": {
            "waybill_no": prepared_form.get("LogisticsId", ""),
            "normalized_form": prepared_form,
        },
        "raw": payload,
    }


def _run_checks(session: Any, *, form: dict[str, str], referer: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for label, url in (
        ("close_route", CHECK_CLOSE_ROUTE_URL),
        ("limit_weight", CHECK_LIMIT_WEIGHT_URL),
        ("payment", CHECK_PAYMENT_URL),
        ("service_scope", CHECK_SERVICE_SCOPE_URL),
        ("feedback_address", FEEDBACK_ADDRESS_URL),
        ("feedback_cost", FEEDBACK_COST_URL),
    ):
        try:
            response = _post_form(session, url, form=form, referer=referer)
            results[label] = _decode_json_response(response, label=label)
        except TMSAuthStateError:
            raise
        except Exception as exc:
            results[label] = {"ok": False, "error": str(exc)}
    return results


def _preview_html(prepared_form: dict[str, str], *, print_kind: str) -> str:
    rows = []
    for key in ("LogisticsId", "BuyerName", "BuyerMobile", "BuyerAddress", "SenderName", "SenderMobile", "ItemName", "ItemTotalNumber"):
        value = _clean_text(prepared_form.get(key))
        if not value:
            continue
        label = FIELD_LABELS.get(key, key)
        rows.append(f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>")
    body = "".join(rows) or "<tr><td colspan='2'>暂无可打印字段</td></tr>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>韵达打印预览</title>"
        "<style>body{font-family:Arial,sans-serif;padding:24px;}table{border-collapse:collapse;width:100%;}"
        "th,td{border:1px solid #d1d5db;padding:8px 10px;text-align:left;}th{width:180px;background:#f3f4f6;}"
        "h1{font-size:18px;margin:0 0 16px;}</style></head><body>"
        f"<h1>韵达{escape(print_kind)}打印预览</h1><table>{body}</table></body></html>"
    )


def _action_message(action: str) -> str:
    return {
        "get-logistics-num": "电子单号获取成功。",
        "address-analysis": "地址解析完成。",
        "address-resolution": "地址匹配完成。",
        "quote-checks": "价格与范围校验完成。",
        "drafts/save": "草稿保存成功。",
        "drafts/list": "草稿列表获取成功。",
        "drafts/load": "草稿加载成功。",
        "drafts/delete": "草稿删除成功。",
        "templates/save": "模板保存成功。",
        "templates/list": "模板列表获取成功。",
        "templates/load": "模板加载成功。",
        "templates/delete": "模板删除成功。",
        "templates/set-default": "默认模板设置成功。",
        "feedback/address": "GIS错误反馈已提交。",
        "feedback/cost": "超高派费反馈已提交。",
        "feedback/cost/upload": "超高派费反馈图片已上传。",
        "return-upload": "电子回单附件已上传。",
        "download-template": "韵达模板下载地址已获取。",
        "print/child": "韵达子单打印接口已调用。",
        "print/master": "韵达主单打印接口已调用。",
        "print/triplicate": "韵达三联单打印接口已调用。",
        "print/receipt-label": "韵达回单标签打印接口已调用。",
    }.get(action, "操作成功。")


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    action = _clean_text(params.get("action")) or "bootstrap"
    form = params.get("form") if isinstance(params.get("form"), dict) else {}
    context = params.get("context") if isinstance(params.get("context"), dict) else {}
    broker = get_session_broker("yunda")
    # The entry page and its exact pricing/save endpoints are the capability
    # preflight.  Do not couple a waybill write to the report/message/problem
    # health matrix before reaching the real target.
    session = broker.build_requests_session(validate=False)
    page_context = _fetch_entry_context(session)
    prepared_form = _prepare_form(form, page_context=page_context)
    referer = _clean_text(context.get("page_url")) or page_context["page_url"]

    if action == "bootstrap":
        remote_context = _fetch_remote_context(session, referer=referer)
        defaults = _build_visible_defaults(page_context)
        if remote_context.get("current_time"):
            defaults["OpenDate"] = _clean_text(remote_context.get("current_time"))
        return {
            "ok": True,
            "action": action,
            "message": "韵达运单录入页已加载。",
            "auth_state": {"code": "AUTHENTICATED"},
            "data": {
                "page_url": page_context["page_url"],
                "fields": page_context["fields"],
                "field_order": page_context["field_order"],
                "hidden_fields": page_context["hidden_fields"],
                "default_form": page_context["default_form"],
                "defaults": defaults,
                "ui_options": _build_ui_options(page_context),
                "remote_context": remote_context,
                "sections": page_context["sections"],
                "print_enabled": bool(prepared_form.get("LogisticsId")),
            },
            "raw": {},
        }

    if action == "get-logistics-num":
        payload = _decode_json_response(
            _post_form(session, GET_LOGISTICS_NUM_URL, form=prepared_form, referer=referer),
            label="电子单号",
        )
        patch_form = _patch_from_payload(payload, prepared_form=prepared_form)
        logistics_no = _extract_logistics_no(payload)
        if logistics_no:
            patch_form["LogisticsId"] = logistics_no
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": patch_form,
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=payload),
                "print_enabled": bool(logistics_no or prepared_form.get("LogisticsId")),
            },
            "raw": payload,
        }

    if action == "address-analysis":
        payload = _decode_json_response(
            _post_form(session, ADDRESS_ANALYSIS_URL, form=prepared_form, referer=referer),
            label="地址解析",
        )
        patch_form = _patch_from_payload(payload, prepared_form=prepared_form)
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": patch_form,
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=payload),
            },
            "raw": payload,
        }

    if action == "address-resolution":
        payload = _decode_json_response(
            _post_form(session, ADDRESS_SITE_URL, form=prepared_form, referer=referer),
            label="地址匹配",
        )
        patch_form = _patch_from_payload(payload, prepared_form=prepared_form)
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": patch_form,
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=payload),
            },
            "raw": payload,
        }

    if action == "quote-checks":
        checks = _run_checks(session, form=prepared_form, referer=referer)
        price_payload = _decode_json_response(
            _post_form(session, PRICE_URL, form=prepared_form, referer=referer),
            label="价格计算",
        )
        patch_form = {
            **_patch_from_payload(checks, prepared_form=prepared_form),
            **_patch_from_payload(price_payload, prepared_form=prepared_form),
        }
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "checks": checks,
                "price": price_payload,
                "normalized_form": prepared_form,
                "patch_form": patch_form,
                "panels": _panels_from_data(
                    action=action,
                    prepared_form=prepared_form,
                    checks=checks,
                    price=price_payload,
                ),
            },
            "raw": {"checks": checks, "price": price_payload},
        }

    if action == "download-template":
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "download_url": DOWNLOAD_TEMPLATE_URL,
                "normalized_form": prepared_form,
                "patch_form": {},
                "panels": {},
            },
            "raw": {"download_url": DOWNLOAD_TEMPLATE_URL},
        }

    if action == "return-upload":
        try:
            payload = _decode_json_response(
                _post_upload(session, RETURN_UPLOAD_URL, context=context, referer=referer),
                label="电子回单上传",
            )
        except ValueError as exc:
            return {
                "ok": False,
                "action": action,
                "message": str(exc),
                "data": {"normalized_form": prepared_form},
                "raw": {},
            }
        file_name = _clean_text(payload.get("fileName") if isinstance(payload, dict) else "")
        file_path = _clean_text(payload.get("filePath") if isinstance(payload, dict) else "")
        return_item = {"ReturnAdjunct": file_name, "ReturnAdjunctAddr": file_path}
        patch_form = {
            "ReturnAdjunct": file_name,
            "ReturnAdjunctAddr": file_path,
            "ReturnAdjunctArr": json.dumps([return_item], ensure_ascii=False) if file_name or file_path else "",
        }
        return {
            "ok": True,
            "action": action,
            "message": _extract_message(payload, _action_message(action)),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": patch_form,
                "panels": {"return_uploads": [return_item] if file_name or file_path else []},
            },
            "raw": payload,
        }

    if action == "feedback/cost/upload":
        try:
            payload = _decode_json_response(
                _post_upload(session, FEEDBACK_COST_UPLOAD_URL, context=context, referer=referer),
                label="超高派费反馈图片上传",
            )
        except ValueError as exc:
            return {
                "ok": False,
                "action": action,
                "message": str(exc),
                "data": {"normalized_form": prepared_form},
                "raw": {},
            }
        target_field = _clean_text(context.get("target_field")) or "PictureUrl1"
        if target_field not in {"PictureUrl1", "PictureUrl2"}:
            target_field = "PictureUrl1"
        file_path = _clean_text(payload.get("filePath") if isinstance(payload, dict) else "")
        patch_form = {target_field: file_path}
        return {
            "ok": True,
            "action": action,
            "message": _extract_message(payload, _action_message(action)),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": patch_form,
                "panels": {"feedback_cost_upload": {target_field: file_path}},
            },
            "raw": payload,
        }

    if action == "feedback/address":
        feedback_form = _feedback_address_payload(prepared_form)
        payload = _decode_json_response(
            _post_form(session, FEEDBACK_ADDRESS_URL, form=feedback_form, referer=referer),
            label="GIS错误反馈",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "feedback_form": feedback_form,
                "patch_form": _patch_from_payload(payload, prepared_form=prepared_form),
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=payload),
            },
            "raw": payload,
        }

    if action == "feedback/cost":
        feedback_form = _feedback_cost_payload(prepared_form)
        missing_pictures = [key for key in ("PictureUrl1", "PictureUrl2") if not feedback_form.get(key)]
        if missing_pictures:
            return {
                "ok": False,
                "action": action,
                "message": "请先上传超高派费反馈所需的两张图片。",
                "data": {
                    "normalized_form": prepared_form,
                    "feedback_form": feedback_form,
                    "missing_fields": missing_pictures,
                },
                "raw": {},
            }
        payload = _decode_json_response(
            _post_form(session, FEEDBACK_COST_URL, form=feedback_form, referer=referer),
            label="超高派费反馈",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "feedback_form": feedback_form,
                "patch_form": _patch_from_payload(payload, prepared_form=prepared_form),
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=payload),
            },
            "raw": payload,
        }

    if action == "save":
        checks = _run_checks(session, form=prepared_form, referer=referer)
        save_payload = _decode_json_response(
            _post_form(session, SAVE_URL, form=prepared_form, referer=referer),
            label="开单保存",
        )
        patch_form = _patch_from_payload(save_payload, prepared_form=prepared_form)
        logistics_no = _extract_logistics_no(save_payload)
        if logistics_no:
            patch_form["LogisticsId"] = logistics_no
        result = _normalize_save_result(save_payload, prepared_form=prepared_form)
        result.update(
            {
                "action": action,
                "auth_state": {"code": "AUTHENTICATED"},
            }
        )
        result["data"] = {
            **(result.get("data") if isinstance(result.get("data"), dict) else {}),
            "checks": checks,
            "patch_form": patch_form,
            "panels": _panels_from_data(
                action=action,
                prepared_form=prepared_form,
                payload=save_payload,
                checks=checks,
            ),
        }
        return result

    if action == "drafts/save":
        payload = _decode_json_response(
            _post_form(session, DRAFT_SAVE_URL, form=prepared_form, referer=referer),
            label="草稿保存",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": _patch_from_payload(payload, prepared_form=prepared_form),
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=payload),
            },
            "raw": payload,
        }

    if action == "drafts/list":
        payload = _decode_json_response(
            _post_form(session, DRAFT_LIST_URL, form=prepared_form, referer=referer),
            label="草稿列表",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "items": _extract_rows(payload),
                "normalized_form": prepared_form,
                "patch_form": {},
                "panels": {},
            },
            "raw": payload,
        }

    if action == "drafts/load":
        payload = _decode_json_response(
            _post_form(session, DRAFT_LIST_URL, form=prepared_form, referer=referer),
            label="草稿加载",
        )
        item = _list_by_id(payload, _clean_text(context.get("item_id")) or _clean_text(form.get("item_id")))
        if not item:
            return {
                "ok": False,
                "action": action,
                "message": "未找到指定草稿。",
                "data": {"items": _extract_rows(payload)},
                "raw": payload,
            }
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "item": item,
                "normalized_form": prepared_form,
                "patch_form": _patch_from_payload(item, prepared_form=prepared_form),
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=item),
            },
            "raw": payload,
        }

    if action == "drafts/delete":
        payload = _decode_json_response(
            _post_form(session, DRAFT_DELETE_URL, form=prepared_form, referer=referer),
            label="草稿删除",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": {},
                "panels": {},
            },
            "raw": payload,
        }

    if action == "templates/save":
        payload = _decode_json_response(
            _post_form(session, TEMPLATE_SAVE_URL, form=prepared_form, referer=referer),
            label="模板保存",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": _patch_from_payload(payload, prepared_form=prepared_form),
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=payload),
            },
            "raw": payload,
        }

    if action == "templates/list":
        payload = _decode_json_response(
            _post_form(session, TEMPLATE_LIST_URL, form=prepared_form, referer=referer),
            label="模板列表",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "items": _extract_rows(payload),
                "normalized_form": prepared_form,
                "patch_form": {},
                "panels": {},
            },
            "raw": payload,
        }

    if action == "templates/load":
        payload = _decode_json_response(
            _post_form(session, TEMPLATE_LIST_URL, form=prepared_form, referer=referer),
            label="模板加载",
        )
        item = _list_by_id(payload, _clean_text(context.get("item_id")) or _clean_text(form.get("item_id")))
        if not item:
            return {
                "ok": False,
                "action": action,
                "message": "未找到指定模板。",
                "data": {"items": _extract_rows(payload)},
                "raw": payload,
            }
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "item": item,
                "normalized_form": prepared_form,
                "patch_form": _patch_from_payload(item, prepared_form=prepared_form),
                "panels": _panels_from_data(action=action, prepared_form=prepared_form, payload=item),
            },
            "raw": payload,
        }

    if action == "templates/delete":
        payload = _decode_json_response(
            _post_form(session, TEMPLATE_DELETE_URL, form=prepared_form, referer=referer),
            label="模板删除",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": {},
                "panels": {},
            },
            "raw": payload,
        }

    if action == "templates/set-default":
        payload = _decode_json_response(
            _post_form(session, TEMPLATE_DEFAULT_URL, form=prepared_form, referer=referer),
            label="默认模板",
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "result": payload,
                "normalized_form": prepared_form,
                "patch_form": {},
                "panels": {},
            },
            "raw": payload,
        }

    if action.startswith("print/"):
        print_kind = action.split("/", 1)[1]
        endpoint_name = PRINTER_ENDPOINT_NAMES.get(print_kind, "")
        remote_url = _build_print_url(print_kind, prepared_form)
        if not remote_url:
            return {
                "ok": False,
                "action": action,
                "message": f"Unsupported print kind: {print_kind}",
                "data": {"normalized_form": prepared_form},
                "raw": {},
            }
        if not _clean_text(prepared_form.get("LogisticsId")):
            return {
                "ok": False,
                "action": action,
                "message": "请先填写或获取韵达运单号后再打印。",
                "data": {"normalized_form": prepared_form},
                "raw": {},
            }
        remote = _decode_remote_action_response(
            _get_html(session, remote_url, referer=referer),
            label=f"打印{print_kind}",
        )
        remote_payload = remote.get("json")
        remote_html = "" if remote_payload is not None else str(remote.get("text") or "")
        preview_html = (
            _with_base_href(remote_html, base_url=remote_url)
            if remote_html.strip()
            else _json_preview_html(remote_payload, title=f"韵达{print_kind}打印响应")
        )
        return {
            "ok": True,
            "action": action,
            "message": _action_message(action),
            "data": {
                "waybill_no": prepared_form.get("LogisticsId", ""),
                "print_kind": print_kind,
                "remote_url": remote_url,
                "remote_endpoint_name": endpoint_name,
                "remote_content_type": remote.get("content_type", ""),
                "remote_payload": remote_payload,
                "preview_html": preview_html,
                "patch_form": {},
                "panels": {
                    "print": {
                        "kind": print_kind,
                        "waybill_no": prepared_form.get("LogisticsId", ""),
                        "remote_url": remote_url,
                        "preview_html": preview_html,
                        "remote_endpoint_name": endpoint_name,
                        "remote_content_type": remote.get("content_type", ""),
                        "remote_payload": remote_payload,
                    }
                },
                "normalized_form": prepared_form,
            },
            "raw": {
                "print_kind": print_kind,
                "remote_endpoint_name": endpoint_name,
                "remote_url": remote_url,
                "remote_content_type": remote.get("content_type", ""),
                "remote_payload": remote_payload,
            },
        }
    if action == "check-logistics-id":
        payload = _decode_json_response(
            _post_form(session, CHECK_LOGISTICS_URL, form=prepared_form, referer=referer),
            label="单号校验",
        )
        return {
            "ok": True,
            "action": action,
            "message": "电子单号校验完成。",
            "data": {"result": payload, "normalized_form": prepared_form},
            "raw": payload,
        }

    return {
        "ok": False,
        "action": action,
        "message": f"Unsupported action: {action}",
        "data": {},
        "raw": {},
    }


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run_once(json.loads(sys.stdin.read() or "{}")), ensure_ascii=False, default=str))
