"""Fetch Yunda waybill-entry prices for one address/weight/volume."""

from __future__ import annotations

import time
import uuid
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from agent.tms_runtime.session_broker import get_session_broker
from agent.tms_runtime.scripts.yunda_waybill_entry import (
    CHECK_SERVICE_SCOPE_URL,
    PRICE_URL as ENTRY_PRICE_URL,
    _clean_text,
    _decode_json_response,
    _fetch_entry_context,
    _fetch_remote_context,
    _post_form,
    _prepare_form,
)


YUNDA_INMS_ORIGIN = "https://kyinms.yunda56.com"
BATCH_TRIAL_INDEX_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/batch_trial/index.html"
    "?page=tab&p=nil"
)
BATCH_TRIAL_CHECK_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/batch_trial/check.html"
)
BATCH_TRIAL_TRIAL_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/batch_trial/trial.html"
)
BATCH_TRIAL_LIST_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/batch_trial/list.html"
)
ENTRY_INDEX_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/entry/indexNew.html"
    "?page=tab&p=nil"
)
ENTRY_WEIGHT_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/weight.html"
ENTRY_INSURED_AMOUNT_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/joinlgs/MakeLogisticsApi/getInsuredAmount.html"
)
ADDRESS_ANALYSIS_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/joinlgs/MakeLogisticsApi/getAddressAnalysis.html"
)
ADDRESS_SITE_URL = (
    f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/joinlgs/MakeLogisticsApi/getAchieveSiteByAddress.html"
)

PRICE_KEYS = {
    "自提": "韵达自提",
    "派送": "韵达派送",
}
ENTRY_SERVICE_MODES = {
    "派送": {"ServiceType": "111", "ShippingMethods": "180"},
    "自提": {"ServiceType": "112", "ShippingMethods": ""},
}
ENTRY_PRICE_DEFAULTS = {
    "LogisticsId": "0",
    "GoodsType": "184",
    "PaymentType": "102",
    "ProductType": "24",
    "IsInternational": "0",
    "ItemTotalNumber": "0",
    "Freight": "0.00",
    "ShippingType": "",
    "NoElevator": "",
    "ShippingFloor": "",
    "DeliversReturn": "0",
    "VipService": "0",
    "IsCod": "0",
    "CollectionMoney": "0",
    "ReturnClass": "",
    "WarehouseCode": "",
    "ExtendField6": "",
    "IsPreferential": "0",
    "IsDiscount": "2",
    "IsFbzp": "0",
    "DestinationDotScope": "",
    "PageType": "1",
    "Distance": "",
    "CrmCustomerId": "",
    "InGoodsType": "184",
    "CheckHeavyWeight": "1",
    "CheckFixedCost": "1",
    "CheckSafeArrive": "0",
    "LongestSideLength": "",
    "UnpackWoodenBoxNum": "0",
    "UnpackBracketNum": "0",
    "ClassificationPlacementNum": "0",
    "UnpackingCountingNum": "0",
    "IsUnpacking": "0",
    "RepaymentPrescription": "",
    "IsHomeDecoration": "0",
    "ThirdCategory": "",
    "InstallPiece": "0",
    "SpecialAreaCode": "",
    "IsFoldableBox": "0",
    "OrderSource": "",
    "ReplaceId": "",
    "SenderTown": "",
    "SenderMustSend": "",
    "DispatchSms": "1",
    "DeliversSms1": "0",
    "DeliversSms2": "0",
    "IsSendMsg": "0",
}
ENTRY_PRICE_REQUIRED_FIELDS = (
    "CreatedDotCode",
    "SenderDistributionCode",
    "CreatedByCode",
    "BuyerDestinationDotCode",
    "BuyerProvince",
    "BuyerCity",
    "BuyerArea",
    "BuyerAddress",
    "GrossWeight",
    "SettlementTotalNumber",
    "Volume",
    "current_time",
)
ENTRY_MESSAGE_FEE_KEYS = ("DeliversSms1", "DeliversSms2", "DispatchSms", "IsSendMsg", "IsCod")
ENTRY_PAGE_TOTAL_KEYS = ("TotalMoney", "Total_Money", "totalMoney", "total_money", "Total")
ENTRY_COST_TOTAL_KEYS = ("CostTotal", "Total_Cost", "costTotal", "total")
ENTRY_PLATFORM_FEE_KEYS = ("PlatformCost", "platformCost", "PlatformFee", "platformFee", "5")
SPECIAL_SCOPE_EMPTY_VALUES = {"", "/", "-", "无", "暂无", "null", "none"}
SPECIAL_SCOPE_CODE_KEYS = (
    "SpecialAreaCode",
    "specialAreaCode",
    "special_area_code",
    "SpecialCode",
    "specialCode",
)
SPECIAL_SCOPE_RANGE_KEYS = (
    "special_range",
    "specialRange",
    "SpecialAreaName",
    "specialAreaName",
    "special_area_name",
    "SpecialArea",
    "specialArea",
    "special_area",
    "SpecialAreaAddress",
    "specialAreaAddress",
)
SPECIAL_SCOPE_REMARK_KEYS = (
    "remark",
    "remarks",
    "Remark",
    "SpecialAreaRemark",
    "specialAreaRemark",
    "special_area_remark",
    "specialRangeRemark",
    "feeRemark",
    "chargeRemark",
    "surchargeRemark",
    "SpecialAreaFee",
    "specialAreaFee",
)
SPECIAL_SCOPE_MESSAGE_KEYS = (
    "message",
    "msg",
    "tip",
    "tips",
    "prompt",
    "alert",
    "warning",
    "ServiceMsg",
    "serviceMsg",
    "SpecialAreaMsg",
    "specialAreaMsg",
)


class YundaPriceError(RuntimeError):
    """Raised when the Yunda pricing API cannot produce a usable price."""


class YundaUnavailableError(YundaPriceError):
    """Raised when the real Yunda site response marks an address as out of range."""


def _decimal_amount(value: Any, *, field_name: str) -> Decimal:
    text = _clean_text(value).replace(",", "")
    if not text:
        raise YundaPriceError(f"韵达试算结果缺少 {field_name}")
    try:
        return Decimal(str(text)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise YundaPriceError(f"韵达试算结果 {field_name} 非数字: {text}") from exc


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        return Decimal(str(text))
    except (InvalidOperation, ValueError):
        return None


def _entry_money_form_text(amount: Decimal) -> str:
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize(), "f")


def _amount_text(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}元"


def _entry_page_total_text(base_total: Decimal, message_fee: Decimal) -> str:
    # The Yunda page displays Number(CostTotal) + Number(MessageCost), then truncates with getFloatStr_1().
    text = str(float(base_total) + float(message_fee))
    text = re.sub(r"[^0-9|\.]", "", text)
    text = re.sub(r"^0+", "", text)
    if "." not in text:
        text += ".00"
    if text.startswith("."):
        text = "0" + text
    text += "00"
    match = re.search(r"\d+\.\d{2}", text)
    if match:
        return f"{match.group(0)}元"
    return _amount_text(base_total + message_fee)


def _decimal_text(value: Any, *, field_name: str) -> str:
    amount = _decimal_amount(value, field_name=field_name)
    return f"{amount:.2f}元"


def _money_text_or_empty(value: Any) -> str:
    text = _clean_text(value).replace(",", "")
    if not text:
        return ""
    try:
        amount = Decimal(str(text)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return text
    return f"{amount:.2f}"


def _number_text(value: Any, *, field_name: str) -> str:
    text = _clean_text(value).replace(",", "")
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    try:
        number = Decimal(str(text))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数字") from exc
    if number <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return format(number.normalize(), "f")


def _entry_flag_enabled(value: Any) -> bool:
    return _clean_text(value).lower() in {"1", "true", "on", "yes", "是"}


def _entry_message_fee(form: dict[str, Any]) -> Decimal:
    fee = Decimal("0.00")
    for key in ENTRY_MESSAGE_FEE_KEYS:
        if _entry_flag_enabled(form.get(key)):
            fee += Decimal("0.05")
    return fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _require_entry_price_success(payload: Any, *, service_mode: str) -> None:
    if not isinstance(payload, dict):
        raise YundaPriceError(f"韵达{service_mode}录单页报价接口返回格式异常: {type(payload).__name__}")
    info = _clean_text(payload.get("info"))
    if info and info.lower() not in {"1", "true", "ok", "success"}:
        message = _clean_text(payload.get("msg") or payload.get("message") or payload.get("error"))
        raise YundaPriceError(f"韵达{service_mode}录单页报价失败: {message or info}")
    if payload.get("ok") is False or payload.get("success") is False:
        message = _clean_text(payload.get("msg") or payload.get("message") or payload.get("error"))
        raise YundaPriceError(f"韵达{service_mode}录单页报价失败: {message or 'ok=false'}")


def _entry_direct_amount(mapping: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if _clean_text(value):
            return value
    key_set = set(keys)
    for key, value in mapping.items():
        if _clean_text(key) in key_set and _clean_text(value):
            return value
    return None


def _entry_classify_amount(payload: Any, keys: tuple[str, ...], name_markers: tuple[str, ...]) -> Any:
    if not isinstance(payload, dict):
        return None
    show_cost = payload.get("showCost")
    if isinstance(show_cost, dict):
        classify_cost = show_cost.get("classifyCost")
        if isinstance(classify_cost, list):
            for row in classify_cost:
                if not isinstance(row, dict):
                    continue
                code = _clean_text(row.get("parentCode") or row.get("code") or row.get("CostCode"))
                name = _clean_text(row.get("name") or row.get("CostName") or row.get("title"))
                if code not in keys and not any(marker in name for marker in name_markers):
                    continue
                for key in ("oneTotal", "TotalMoney", "CostTotal", "money", "amount"):
                    value = row.get(key)
                    if _clean_text(value):
                        return value
    return None


def _entry_total_candidate(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    cost_total = _entry_direct_amount(data, ENTRY_COST_TOTAL_KEYS)
    if cost_total is None:
        cost_total = _entry_direct_amount(payload, ENTRY_COST_TOTAL_KEYS)
    if cost_total is not None:
        return cost_total

    page_total = _entry_direct_amount(data, ENTRY_PAGE_TOTAL_KEYS)
    if page_total is None:
        page_total = _entry_direct_amount(payload, ENTRY_PAGE_TOTAL_KEYS)
    if page_total is not None:
        return page_total

    if cost_total is None:
        cost_total = _entry_classify_amount(payload, ENTRY_COST_TOTAL_KEYS, ())
    if cost_total is not None:
        return cost_total
    return _entry_classify_amount(payload, ENTRY_PAGE_TOTAL_KEYS, ("合计", "总计", "总金额"))


def _entry_platform_fee(payload: Any) -> Decimal:
    if not isinstance(payload, dict):
        return Decimal("0.00")
    data = payload.get("data")
    value = _entry_direct_amount(data, ENTRY_PLATFORM_FEE_KEYS)
    if value is None:
        value = _entry_direct_amount(payload, ENTRY_PLATFORM_FEE_KEYS)
    if value is None:
        value = _entry_classify_amount(payload, ENTRY_PLATFORM_FEE_KEYS, ("平台费", "平台"))
    if value is None:
        return Decimal("0.00")
    return _decimal_amount(value, field_name="PlatformCost")


def _entry_total_text(payload: Any, *, service_mode: str, form: dict[str, Any]) -> str:
    _require_entry_price_success(payload, service_mode=service_mode)
    candidate = _entry_total_candidate(payload)
    if candidate is None:
        raise YundaPriceError(f"韵达{service_mode}录单页报价缺少 CostTotal/TotalMoney")
    base_total = _decimal_amount(candidate, field_name=f"{service_mode} CostTotal")
    return _entry_page_total_text(base_total, _entry_message_fee(form))


def _entry_apply_insured_amount_range(form: dict[str, str], payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    info = _clean_text(payload.get("info"))
    if info and info.lower() not in {"1", "true", "ok", "success"}:
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    min_amount = _decimal_or_none(data.get("MIN"))
    max_amount = _decimal_or_none(data.get("MAX"))
    if min_amount is None:
        return False

    current = _decimal_or_none(form.get("InsuredAmount"))
    if current is not None and current >= min_amount and (max_amount is None or current <= max_amount):
        return False

    form["InsuredAmount"] = _entry_money_form_text(min_amount)
    return True


def _sync_entry_insured_amount(session: Any, *, form: dict[str, str], referer: str) -> None:
    goods_type = _clean_text(form.get("GoodsType"))
    settlement_total_number = _clean_text(form.get("SettlementTotalNumber"))
    if not goods_type or not settlement_total_number:
        return
    payload = _decode_json_response(
        _post_form(
            session,
            ENTRY_INSURED_AMOUNT_URL,
            form={"GoodsType": goods_type, "SettlementTotalNumber": settlement_total_number},
            referer=referer,
        ),
        label="韵达申明价值范围",
    )
    _entry_apply_insured_amount_range(form, payload)


def _set_default_if_blank(form: dict[str, str], key: str, value: Any) -> None:
    if _clean_text(form.get(key)):
        return
    text = _clean_text(value)
    if text or value == 0:
        form[key] = text


def _entry_script_default(page_context: dict[str, Any], key: str) -> str:
    html = _clean_text(page_context.get("html") if isinstance(page_context, dict) else "")
    if not html:
        return ""
    patterns = (
        rf"(?:var|let|const)\s+{re.escape(key)}\s*=\s*['\"]([^'\"]*)",
        rf"{re.escape(key)}\s*:\s*['\"]([^'\"]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return _clean_text(match.group(1))
    return ""


def _entry_script_decimal(page_context: dict[str, Any], key: str) -> Decimal | None:
    html = _clean_text(page_context.get("html") if isinstance(page_context, dict) else "")
    if not html:
        return None
    escaped_key = re.escape(key)
    patterns = (
        rf"(?:var|let|const)\s+{escaped_key}\s*=\s*['\"]?([0-9.]+)",
        rf"{escaped_key}\s*:\s*['\"]?([0-9.]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if not match:
            continue
        try:
            return Decimal(str(match.group(1)))
        except (InvalidOperation, ValueError):
            return None
    return None


def _entry_heavy_weight_flag(page_context: dict[str, Any], *, weight: Any, volume: Any) -> str:
    bubble_ratio = _entry_script_decimal(page_context, "$BubbleRatio")
    heavy_min_weight = _entry_script_decimal(page_context, "$HeavyMinWeight")
    if bubble_ratio is None or heavy_min_weight is None:
        return "1"
    if bubble_ratio <= 0:
        return "0"
    weight_value = Decimal(_number_text(weight, field_name="weight"))
    volume_value = Decimal(_number_text(volume, field_name="volume"))
    bubble_ratio_weight = volume_value * Decimal("1000000") / bubble_ratio
    if weight_value > bubble_ratio_weight and weight_value > heavy_min_weight:
        return "1"
    return "0"


def _max_number_text(left: Any, right: Any, *, field_name: str) -> str:
    left_value = Decimal(_number_text(left, field_name=field_name))
    right_value = Decimal(_number_text(right, field_name=field_name))
    return format(max(left_value, right_value).normalize(), "f")


def _require_entry_weight_success(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise YundaPriceError(f"韵达重量接口返回格式异常: {type(payload).__name__}")
    info = _clean_text(payload.get("info"))
    if info and info.lower() not in {"1", "true", "ok", "success"}:
        message = _clean_text(
            payload.get("msg")
            or payload.get("message")
            or payload.get("error")
            or payload.get("data")
        )
        raise YundaPriceError(f"韵达重量接口失败: {message or info}")
    if payload.get("ok") is False or payload.get("success") is False:
        message = _clean_text(
            payload.get("msg")
            or payload.get("message")
            or payload.get("error")
            or payload.get("data")
        )
        raise YundaPriceError(f"韵达重量接口失败: {message or 'ok=false'}")
    for key in ("data", "Tfr", "Del"):
        if not _clean_text(payload.get(key)) and payload.get(key) != 0:
            raise YundaPriceError(f"韵达重量接口缺少 {key}")
    return payload


def _entry_weight_fields(weight_text: str, weight_payload: dict[str, Any] | None) -> dict[str, str]:
    if not weight_payload:
        return {
            "SettlementTotalNumber": weight_text,
            "Tfr": weight_text,
            "Del": weight_text,
            "VolWeight": "",
        }
    payload = _require_entry_weight_success(weight_payload)
    vol_weight = _number_text(payload.get("data"), field_name="VolWeight")
    tfr = _number_text(payload.get("Tfr"), field_name="Tfr")
    del_weight = _number_text(payload.get("Del"), field_name="Del")
    return {
        "SettlementTotalNumber": _max_number_text(weight_text, vol_weight, field_name="SettlementTotalNumber"),
        "Tfr": tfr,
        "Del": del_weight,
        "VolWeight": vol_weight,
    }


def _entry_base_form(page_context: dict[str, Any], remote_context: dict[str, Any]) -> dict[str, str]:
    form = _prepare_form({}, page_context=page_context)
    for key, value in ENTRY_PRICE_DEFAULTS.items():
        _set_default_if_blank(form, key, value)
    for key in ("SenderDistributionCode", "SenderDistributionName", "CreatedByCode"):
        _set_default_if_blank(form, key, _entry_script_default(page_context, key))
    current_time = _clean_text(remote_context.get("current_time") if isinstance(remote_context, dict) else "")
    _set_default_if_blank(form, "current_time", current_time)
    _set_default_if_blank(form, "CreatedByCode", form.get("PackageByCode"))
    _set_default_if_blank(form, "InGoodsType", form.get("GoodsType"))
    return form


def _build_entry_price_form(
    *,
    page_context: dict[str, Any],
    remote_context: dict[str, Any],
    address_detail: dict[str, Any],
    address: str,
    weight: Any,
    volume: Any,
    service_mode: str,
    weight_payload: dict[str, Any] | None = None,
) -> dict[str, str]:
    if service_mode not in ENTRY_SERVICE_MODES:
        raise ValueError("service_mode 必须是 自提 或 派送")
    form = _entry_base_form(page_context, remote_context)
    site = address_detail.get("raw") if isinstance(address_detail.get("raw"), dict) else {}
    analysis = address_detail.get("地址解析明细") if isinstance(address_detail.get("地址解析明细"), dict) else {}
    province = _clean_text(analysis.get("Buyer_Province")) or _clean_text(address_detail.get("省"))
    city = _clean_text(analysis.get("Buyer_City")) or _clean_text(address_detail.get("市"))
    area = _clean_text(analysis.get("Buyer_Area")) or _clean_text(address_detail.get("区县"))
    short_address = _clean_text(address_detail.get("详细地址")) or _clean_text(address)
    weight_text = _number_text(weight, field_name="weight")
    volume_text = _number_text(volume, field_name="volume")
    weight_fields = _entry_weight_fields(weight_text, weight_payload)
    service_values = ENTRY_SERVICE_MODES[service_mode]
    updates = {
        "BuyerProvince": province,
        "BuyerCity": city,
        "BuyerArea": area,
        "BuyerTown": _clean_text(site.get("BuyerTownCode") or site.get("TownCode") or site.get("BuyerTown")),
        "BuyerAddress": short_address,
        "BuyerDestinationDotCode": _clean_text(site.get("target_center_code")),
        "BuyerDestinationDotName": _clean_text(site.get("target_center")),
        "BuyerDestinationDistributionCode": _clean_text(site.get("business_center_code")),
        "BuyerDestinationDistributionName": _clean_text(site.get("business_center")),
        "DestinationDotScope": _clean_text(site.get("business_center_code") or site.get("target_center_code")),
        "DispatchRemark": _clean_text(address_detail.get("派送说明")),
        "GrossWeight": weight_text,
        "SettlementTotalNumber": weight_fields["SettlementTotalNumber"],
        "Tfr": weight_fields["Tfr"],
        "Del": weight_fields["Del"],
        "VolWeight": weight_fields["VolWeight"],
        "Volume": volume_text,
        "ItemTotalNumber": "0",
        "ServiceType": service_values["ServiceType"],
        "ShippingMethods": service_values["ShippingMethods"],
        "CheckHeavyWeight": _entry_heavy_weight_flag(page_context, weight=weight, volume=volume),
        "CheckFixedCost": "1",
        "CheckSafeArrive": "0",
    }
    for key, value in updates.items():
        form[key] = _clean_text(value)
    _set_default_if_blank(form, "CreatedByCode", form.get("PackageByCode"))
    _set_default_if_blank(form, "InGoodsType", form.get("GoodsType"))
    missing = [key for key in ENTRY_PRICE_REQUIRED_FIELDS if not _clean_text(form.get(key))]
    if missing:
        raise YundaPriceError(f"韵达录单页报价缺少必要字段: {', '.join(missing)}")
    return form


def _fetch_entry_weight(
    session: Any,
    *,
    referer: str,
    base_form: dict[str, str],
    address_detail: dict[str, Any],
    weight: Any,
    volume: Any,
    remote_context: dict[str, Any],
) -> dict[str, Any]:
    site = address_detail.get("raw") if isinstance(address_detail.get("raw"), dict) else {}
    analysis = address_detail.get("地址解析明细") if isinstance(address_detail.get("地址解析明细"), dict) else {}
    payload = _post_simple_form_with_referer(
        session,
        ENTRY_WEIGHT_URL,
        {
            "current_time": _clean_text(remote_context.get("current_time")),
            "vol": _number_text(volume, field_name="volume"),
            "CrmCustomerId": _clean_text(base_form.get("CrmCustomerId")),
            "CreatedDotCode": _clean_text(base_form.get("CreatedDotCode")),
            "BuyerDestinationDotCode": _clean_text(site.get("target_center_code")),
            "ProductType": _clean_text(base_form.get("ProductType")),
            "InGoodsType": _clean_text(base_form.get("InGoodsType") or base_form.get("GoodsType")),
            "GrossWeight": _number_text(weight, field_name="weight"),
            "OrderSource": _clean_text(base_form.get("OrderSource")),
            "LogisticsId": _clean_text(base_form.get("LogisticsId")) or "0",
            "BuyerCity": _clean_text(analysis.get("Buyer_City") or address_detail.get("市")),
            "SignType": _clean_text(base_form.get("SignType")),
        },
        label="重量计算",
        referer=referer,
    )
    return _require_entry_weight_success(payload)


def build_trial_task(
    *,
    address: str,
    weight: Any,
    volume: Any,
    service_mode: str,
    uuid_value: str,
    sort: int,
) -> dict[str, Any]:
    address_text = _clean_text(address)
    if not address_text:
        raise ValueError("address 不能为空")
    if service_mode not in PRICE_KEYS:
        raise ValueError("service_mode 必须是 自提 或 派送")

    return {
        "Buyer_Address": address_text,
        "Item_Total_Number": "1",
        "Gross_Weight": _number_text(weight, field_name="weight"),
        "Volume": _number_text(volume, field_name="volume"),
        "Created_Dot_Code": "",
        "Service_Type": "自提" if service_mode == "自提" else "",
        "Shipping_Methods": "",
        "Sender_Distribution_Code": "",
        "Goods_Type": "",
        "Payment_Type": "",
        "Freight": "",
        "Insured_Amount": "",
        "Delivers_Return": "",
        "In_Goods_Type": "",
        "Check_Heavy_Weight": "是",
        "Check_Fixed_Cost": "是",
        "Shipping_Type": "",
        "No_Elevator": "",
        "Shipping_Floor": "",
        "Crm_Customer_Id": "",
        "Longest_Side_Length": "",
        "Remark": f"YD_PRICE_{'ZT' if service_mode == '自提' else 'PS'}",
        "IsFoldableBox": "",
        "Sort": sort,
        "Uuid": uuid_value,
    }


def _form_pairs(name: str, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        pairs: list[tuple[str, str]] = []
        for key, item in value.items():
            pairs.extend(_form_pairs(f"{name}[{key}]", item))
        return pairs
    if isinstance(value, (list, tuple)):
        pairs = []
        for index, item in enumerate(value):
            pairs.extend(_form_pairs(f"{name}[{index}]", item))
        return pairs
    return [(name, "" if value is None else str(value))]


def _post_batch_form(session: Any, url: str, data: Any, *, label: str) -> Any:
    headers = {
        "Referer": BATCH_TRIAL_INDEX_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        url,
        data=_form_pairs("Data", data),
        headers=headers,
        allow_redirects=True,
        timeout=60,
    )
    return _decode_json_response(response, label=label)


def _post_simple_form(session: Any, url: str, data: dict[str, Any], *, label: str) -> Any:
    return _post_simple_form_with_referer(session, url, data, label=label, referer=BATCH_TRIAL_INDEX_URL)


def _post_simple_form_with_referer(
    session: Any,
    url: str,
    data: dict[str, Any],
    *,
    label: str,
    referer: str,
) -> Any:
    headers = {
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        url,
        data=data,
        headers=headers,
        allow_redirects=True,
        timeout=60,
    )
    return _decode_json_response(response, label=label)


def _payload_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        return [row for row in data["rows"] if isinstance(row, dict)]
    return []


def _require_success(payload: Any, *, label: str) -> None:
    if not isinstance(payload, dict):
        raise YundaPriceError(f"韵达{label}接口返回格式异常: {type(payload).__name__}")
    info = _clean_text(payload.get("info"))
    if info and info != "1":
        message = _clean_text(payload.get("message") or payload.get("msg") or payload.get("error"))
        raise YundaPriceError(f"韵达{label}失败: {message or info}")
    if payload.get("ok") is False:
        message = _clean_text(payload.get("message") or payload.get("error"))
        raise YundaPriceError(f"韵达{label}失败: {message or 'ok=false'}")


def _scope_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean_text(key).lower())


def _scope_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    text = _clean_text(value)
    if text.lower() in SPECIAL_SCOPE_EMPTY_VALUES:
        return ""
    return text


def _walk_mappings(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(value, dict):
        rows = [value]
        for item in value.values():
            rows.extend(_walk_mappings(item, depth=depth + 1))
        return rows
    if isinstance(value, list):
        rows: list[dict[str, Any]] = []
        for item in value[:100]:
            rows.extend(_walk_mappings(item, depth=depth + 1))
        return rows
    return []


def _walk_texts(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, dict):
        texts: list[str] = []
        for item in value.values():
            texts.extend(_walk_texts(item, depth=depth + 1))
        return texts
    if isinstance(value, list):
        texts: list[str] = []
        for item in value[:100]:
            texts.extend(_walk_texts(item, depth=depth + 1))
        return texts
    if isinstance(value, (str, int, float)):
        text = _scope_text(value)
        return [text] if text else []
    return []


def _first_scope_value(payload: Any, keys: tuple[str, ...]) -> str:
    key_set = {_scope_key(key) for key in keys}
    for row in _walk_mappings(payload):
        for key, value in row.items():
            if _scope_key(key) not in key_set:
                continue
            text = _scope_text(value)
            if text:
                return text
    return ""


def _special_scope_message(payload: Any) -> str:
    text = _first_scope_value(payload, SPECIAL_SCOPE_MESSAGE_KEYS)
    if text and ("特殊区域" in text or "加收" in text):
        return text
    for candidate in _walk_texts(payload):
        if "特殊区域" in candidate or "加收" in candidate:
            return candidate
    return ""


def _message_special_parts(message: str) -> tuple[str, str]:
    parts = [_scope_text(item) for item in re.findall(r"【([^】]+)】", message)]
    parts = [item for item in parts if item]
    if not parts:
        return "", ""
    fee = next((item for item in parts if "加收" in item or "元" in item), "")
    area = next((item for item in parts if item != fee), "")
    return area, fee


def _special_area_entry(payload: Any) -> tuple[str, str]:
    for row in _walk_mappings(payload):
        special_area = row.get("SpecialArea")
        if not isinstance(special_area, dict):
            special_area = row.get("specialArea")
        if not isinstance(special_area, dict):
            continue
        for area, detail in special_area.items():
            area_text = _scope_text(area)
            if not area_text:
                continue
            remark = ""
            if isinstance(detail, dict):
                remark = _first_scope_value(detail, SPECIAL_SCOPE_REMARK_KEYS)
            return area_text, remark
    return "", ""


def _require_service_scope_success(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise YundaPriceError(f"韵达特殊区域校验接口返回格式异常: {type(payload).__name__}")
    info = _clean_text(payload.get("info"))
    if info and info.lower() not in {"1", "true", "ok", "success"}:
        message = _clean_text(payload.get("msg") or payload.get("message") or payload.get("error"))
        raise YundaPriceError(f"韵达特殊区域校验失败: {message or info}")
    if payload.get("ok") is False or payload.get("success") is False:
        message = _clean_text(payload.get("msg") or payload.get("message") or payload.get("error"))
        raise YundaPriceError(f"韵达特殊区域校验失败: {message or 'ok=false'}")


def _fetch_service_scope(session: Any, *, form: dict[str, str], referer: str) -> Any:
    payload = _decode_json_response(
        _post_form(session, CHECK_SERVICE_SCOPE_URL, form=form, referer=referer),
        label="特殊区域校验",
    )
    _require_service_scope_success(payload)
    return payload


def _normalize_special_scope(payload: Any, *, fallback_range: Any = "") -> dict[str, str]:
    message = _special_scope_message(payload)
    message_area, message_fee = _message_special_parts(message)
    special_area, special_remark = _special_area_entry(payload)
    area = special_area or _first_scope_value(payload, SPECIAL_SCOPE_RANGE_KEYS) or message_area
    fee = _first_scope_value(payload, SPECIAL_SCOPE_REMARK_KEYS) or special_remark or message_fee
    code = _first_scope_value(payload, SPECIAL_SCOPE_CODE_KEYS)
    if not area:
        area = _scope_text(fallback_range)
    if not any((area, fee, code, message)):
        return {}
    if not message and area and fee:
        message = f"该地址涉及特殊区域【{area}】【{fee}】，请核实！"
    output: dict[str, str] = {}
    if code:
        output["code"] = code
    if area:
        output["range"] = area
    if fee:
        output["remark"] = fee
    if message:
        output["message"] = message
    return output


def _apply_special_scope_to_form(form: dict[str, str], special_scope: dict[str, str]) -> None:
    if special_scope.get("code"):
        form["SpecialAreaCode"] = special_scope["code"]
    if special_scope.get("range"):
        form["SpecialAreaName"] = special_scope["range"]


def _row_success(row: dict[str, Any]) -> bool:
    status = _clean_text(row.get("Trial_Status"))
    if status == "1":
        return True
    if not status and _clean_text(row.get("Total_Cost")):
        return True
    return False


def _extract_prices(rows: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, str]:
    rows_by_remark = {_clean_text(row.get("Remark")): row for row in rows if _clean_text(row.get("Remark"))}
    output: dict[str, str] = {}
    for task in tasks:
        remark = _clean_text(task.get("Remark"))
        row = rows_by_remark.get(remark)
        if row is None:
            raise YundaPriceError(f"韵达试算结果缺少任务: {remark}")
        service_mode = "自提" if _clean_text(task.get("Service_Type")) == "自提" else "派送"
        if not _row_success(row):
            message = _clean_text(row.get("Trial_Description")) or _clean_text(row.get("Trial_Status")) or "未知错误"
            raise YundaPriceError(f"韵达{service_mode}试算失败: {message}")
        output[PRICE_KEYS[service_mode]] = _decimal_text(row.get("Total_Cost"), field_name="Total_Cost")
    return output


def _extract_row_details(rows: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    rows_by_remark = {_clean_text(row.get("Remark")): row for row in rows if _clean_text(row.get("Remark"))}
    output: dict[str, Any] = {}
    for task in tasks:
        remark = _clean_text(task.get("Remark"))
        row = rows_by_remark.get(remark)
        if not isinstance(row, dict):
            continue
        service_mode = "自提" if _clean_text(task.get("Service_Type")) == "自提" else "派送"
        cost_detail = _parse_cost_detail(row.get("Cost_Detail"))
        output[service_mode] = {
            "目的网点": _clean_text(row.get("Buyer_Destination_Dot_Name")),
            "目的网点编码": _clean_text(row.get("Buyer_Destination_Dot_Code")),
            "是否派送": _clean_text(row.get("Send_Msg")),
            "服务方式": _clean_text(row.get("Service_Type")),
            "送货方式": _clean_text(row.get("Shipping_Methods")),
            "首发分拨": _clean_text(row.get("Sender_Distribution_Name")),
            "寄件网点": _clean_text(row.get("Created_Dot_Name")),
            "结算重量": _clean_text(row.get("Tfr_Weight")),
            "实际重量": _clean_text(row.get("Gross_Weight")),
            "体积": _clean_text(row.get("Volume")),
            "单公斤成本": _decimal_text(row.get("1Kg_Cost"), field_name="1Kg_Cost")
            if _clean_text(row.get("1Kg_Cost"))
            else "",
            "费用明细": _cost_summary(cost_detail),
            "raw": row,
        }
    return output


def _parse_cost_detail(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = _clean_text(value)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cost_summary(cost_detail: dict[str, Any]) -> dict[str, str]:
    labels = (
        ("FixedCost", "特惠一口价"),
        ("SendCost", "派送费"),
        ("TownSendCost", "乡镇派费"),
        ("TownBalancedCost", "乡镇平衡费"),
        ("InsuredCost", "服务保障费"),
        ("SubOrderCost", "子单费"),
        ("ScanCost", "韵心达服务费"),
        ("MeetingCost", "会务费"),
        ("CostTotal", "合计"),
    )
    output: dict[str, str] = {}
    for key, label in labels:
        amount = _money_text_or_empty(cost_detail.get(key))
        if not amount:
            continue
        try:
            if Decimal(str(amount)) == 0:
                continue
        except Exception:
            pass
        output[label] = f"{amount}元"
    return output


def _analysis_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise YundaPriceError(f"韵达地址解析接口返回格式异常: {type(payload).__name__}")
    if _clean_text(payload.get("info")) != "1":
        message = _clean_text(payload.get("msg") or payload.get("message") or payload.get("data"))
        raise YundaPriceError(f"韵达地址解析失败: {message or 'info!=1'}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise YundaPriceError("韵达地址解析结果缺少 data")
    return data


def _site_row(payload: Any, *, destination_code: str = "") -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise YundaPriceError(f"韵达网点匹配接口返回格式异常: {type(payload).__name__}")
    if _clean_text(payload.get("info")) != "1":
        message = _clean_text(payload.get("msg") or payload.get("message") or payload.get("data"))
        raise YundaPriceError(f"韵达网点匹配失败: {message or 'info!=1'}")
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise YundaPriceError("韵达网点匹配结果缺少 data")
    rows = [row for row in data.values() if isinstance(row, dict)]
    if not rows:
        raise YundaPriceError("韵达网点匹配结果缺少可用网点")
    destination_code = _clean_text(destination_code)
    if destination_code:
        matches = [
            row
            for row in rows
            if _clean_text(row.get("target_center_code")) == destination_code
        ]
        if len(matches) != 1:
            raise YundaPriceError(
                f"韵达网点匹配结果无法唯一定位目的网点: destination_code={destination_code} candidates={len(matches)}"
            )
        row = matches[0]
    else:
        if len(rows) != 1:
            raise YundaPriceError(f"韵达网点匹配结果存在多个候选: candidates={len(rows)}")
        row = rows[0]

    site_code = _clean_text(row.get("target_center_code")).upper()
    site_name = _clean_text(row.get("target_center"))
    if site_code == "OR" or site_name == "超区":
        raise YundaUnavailableError("韵达网点匹配结果为超区")
    return row


def fetch_yunda_address_detail(
    session: Any,
    *,
    address: str,
    created_dot_code: Any = "",
    destination_code: Any = "",
    settle_number: Any = "0.00",
) -> dict[str, Any]:
    analysis_payload = _post_simple_form_with_referer(
        session,
        ADDRESS_ANALYSIS_URL,
        {"IsSend": "1", "AddressInfo": _clean_text(address)},
        label="地址解析",
        referer=ENTRY_INDEX_URL,
    )
    analysis = _analysis_data(analysis_payload)
    province = _clean_text(analysis.get("Buyer_Province_Name"))
    city = _clean_text(analysis.get("Buyer_City_Name"))
    area = _clean_text(analysis.get("Buyer_Area_Name"))
    short_address = _clean_text(analysis.get("Buyer_Address")) or _clean_text(address)
    if not province or not city or not area or not short_address:
        raise YundaPriceError("韵达地址解析结果缺少省市区或详细地址")

    site_payload = _post_simple_form_with_referer(
        session,
        ADDRESS_SITE_URL,
        {
            "BuyerProvince": province,
            "BuyerCity": city,
            "BuyerArea": area,
            "CreatedDotCode": _clean_text(created_dot_code),
            "IsCod": "0",
            "PaymentType": "102",
            "ServiceType": "111",
            "GetIsProvinceSite": "1",
            "IsOrderAddress": "1",
            "LogisticsId": "",
            "IsInternational": "0",
            "SplitWord": "0",
            "ShortAddress": short_address,
            "BuyerAddress": short_address,
            "SettleNumber": _clean_text(settle_number) or "0.00",
        },
        label="网点匹配",
        referer=ENTRY_INDEX_URL,
    )
    site = _site_row(site_payload, destination_code=_clean_text(destination_code))
    estimate_rows = site.get("EstimateArrivalTime") if isinstance(site.get("EstimateArrivalTime"), list) else []
    first_estimate = estimate_rows[0] if estimate_rows and isinstance(estimate_rows[0], dict) else {}
    return {
        "省": province,
        "市": city,
        "区县": area,
        "详细地址": short_address,
        "目的网点": _clean_text(site.get("target_center")),
        "目的网点编码": _clean_text(site.get("target_center_code")),
        "目的分拨": _clean_text(site.get("business_center")),
        "是否派送": _clean_text(site.get("SendMsg")),
        "派送说明": _clean_text(site.get("TownMsg") or site.get("servicemsg")),
        "查询电话": _clean_text(site.get("qry_phone") or site.get("cxdh")),
        "客服电话": _clean_text(site.get("cxdh")),
        "业务电话": _clean_text(site.get("sale_phone")),
        "经理姓名": _clean_text(site.get("site_manager_name")),
        "经理电话": _clean_text(site.get("site_manager_phone")),
        "门店地址": _clean_text(site.get("SiteAddress")),
        "派送范围": _clean_text(site.get("remark")),
        "特殊区域": _clean_text(site.get("special_range")),
        "线路": _clean_text(site.get("Route")),
        "到件时效": _clean_text(site.get("deliverprescription") or first_estimate.get("allUseTm")),
        "乡镇": _clean_text(site.get("BuyerTown")),
        "地址解析明细": analysis,
        "raw": site,
    }


def fetch_yunda_prices(
    session: Any,
    *,
    address: str,
    weight: Any,
    volume: Any,
) -> dict[str, Any]:
    address_text = _clean_text(address)
    if not address_text:
        raise ValueError("address 不能为空")
    weight_text = _number_text(weight, field_name="weight")
    _number_text(volume, field_name="volume")
    page_context = _fetch_entry_context(session)
    referer = _clean_text(page_context.get("page_url")) or ENTRY_INDEX_URL
    remote_context = _fetch_remote_context(session, referer=referer)
    entry_defaults = _entry_base_form(page_context, remote_context)
    try:
        address_detail = fetch_yunda_address_detail(
            session,
            address=address_text,
            created_dot_code=entry_defaults.get("CreatedDotCode"),
            settle_number=weight_text,
        )
    except YundaUnavailableError as exc:
        return {
            "source": "yunda_price",
            "网点不可达": "网点不可达",
            "unavailable": True,
            "unavailable_reason": _clean_text(exc),
        }
    weight_payload = _fetch_entry_weight(
        session,
        referer=referer,
        base_form=entry_defaults,
        address_detail=address_detail,
        weight=weight,
        volume=volume,
        remote_context=remote_context,
    )
    prices: dict[str, str] = {}
    entry_details: dict[str, Any] = {}
    special_scope: dict[str, str] = _normalize_special_scope(
        address_detail.get("raw", {}),
        fallback_range=address_detail.get("特殊区域"),
    )
    for service_mode in ("派送", "自提"):
        form = _build_entry_price_form(
            page_context=page_context,
            remote_context=remote_context,
            address_detail=address_detail,
            address=address_text,
            weight=weight,
            volume=volume,
            service_mode=service_mode,
            weight_payload=weight_payload,
        )
        _sync_entry_insured_amount(session, form=form, referer=referer)
        if service_mode == "派送":
            service_scope_payload = _fetch_service_scope(session, form=form, referer=referer)
            service_scope = _normalize_special_scope(
                service_scope_payload,
                fallback_range=address_detail.get("特殊区域"),
            )
            if service_scope:
                special_scope.update(service_scope)
            _apply_special_scope_to_form(form, special_scope)
        price_payload = _decode_json_response(
            _post_form(session, ENTRY_PRICE_URL, form=form, referer=referer),
            label=f"{service_mode}录单页价格计算",
        )
        prices[PRICE_KEYS[service_mode]] = _entry_total_text(
            price_payload,
            service_mode=service_mode,
            form=form,
        )
        entry_details[service_mode] = {
            "服务方式": service_mode,
            "录单页总价": prices[PRICE_KEYS[service_mode]],
            "短信费": _amount_text(_entry_message_fee(form)),
            "form": {
                "ServiceType": form.get("ServiceType", ""),
                "ShippingMethods": form.get("ShippingMethods", ""),
                "CheckHeavyWeight": form.get("CheckHeavyWeight", ""),
                "CheckFixedCost": form.get("CheckFixedCost", ""),
                "DispatchSms": form.get("DispatchSms", ""),
                "InsuredAmount": form.get("InsuredAmount", ""),
                "SpecialAreaCode": form.get("SpecialAreaCode", ""),
                "SpecialAreaName": form.get("SpecialAreaName", ""),
            },
            "raw": price_payload,
        }
    special_output = {}
    if special_scope.get("range"):
        special_output["特殊区域"] = special_scope["range"]
    if special_scope.get("remark"):
        special_output["特殊区域加收"] = special_scope["remark"]
    if special_scope.get("message"):
        special_output["特殊区域提醒"] = special_scope["message"]
    return {
        "ok": True,
        "source": "yunda_price",
        "price_source": "waybill_entry",
        **{key: value for key, value in address_detail.items() if key != "raw" and value not in (None, "", "/")},
        **special_output,
        **prices,
        "韵达明细": entry_details,
        "网点明细": address_detail,
    }


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    address = _clean_text(params.get("address"))
    weight = params.get("weight")
    volume = params.get("volume", 0.1)
    session_profile = _clean_text(params.get("session_profile")) or "yunda"
    broker = get_session_broker(session_profile)
    session = broker.build_requests_session(validate=not bool(params.get("skip_session_validate", False)))
    result = fetch_yunda_prices(
        session,
        address=address,
        weight=weight,
        volume=volume,
    )
    result["session_profile"] = session_profile
    return result
