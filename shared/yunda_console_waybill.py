from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


_MONEY_CLEAN_RE = re.compile(r"[\s,￥¥元]")


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean_text(row.get(key))
        if value:
            return value
    return ""


def _normalize_waybill_no(value: Any) -> str:
    text = _clean_text(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def _normalize_money_text(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    cleaned = _MONEY_CLEAN_RE.sub("", text)
    if not cleaned:
        return ""
    try:
        return f"{Decimal(str(cleaned)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    except (InvalidOperation, ValueError):
        return text


def _normalize_date_text(value: Any, *, target_date: dt.date | str | None = None) -> str:
    text = _clean_text(value)
    if text:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if match:
            year, month, day = match.groups()
            try:
                return dt.date(int(year), int(month), int(day)).isoformat()
            except ValueError:
                pass
        compact = re.sub(r"\D+", "", text)
        if len(compact) == 8 and compact.startswith("20"):
            try:
                return dt.date(
                    int(compact[:4]),
                    int(compact[4:6]),
                    int(compact[6:8]),
                ).isoformat()
            except ValueError:
                pass
    if isinstance(target_date, dt.date):
        return target_date.isoformat()
    return _clean_text(target_date)


def _build_weight_volume(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for label, keys in (
        ("实际重量", ("GrossWeight", "actual_weight", "实际重量")),
        ("体积", ("Volume", "volume", "体积")),
        ("结算重量", ("SettlementTotalNumber", "settlement_weight", "结算重量")),
        ("体积重", ("Tfr", "volumetric_weight", "体积重")),
    ):
        value = _first_text(row, *keys)
        if value:
            parts.append(f"{label} {value}")
    return " / ".join(parts)


def _build_package_type_lines(row: dict[str, Any]) -> str:
    direct = _first_text(row, "Packing", "package_type", "包装类型")
    if direct:
        return direct
    parts: list[str] = []
    for index in range(1, 5):
        value = _first_text(
            row,
            f"PackingType{index}",
            f"PackingType{index}Name",
            f"package_type_{index}",
        )
        if value:
            parts.append(value)
    return " / ".join(parts)


def _build_quantity_lines(row: dict[str, Any]) -> str:
    return _first_text(
        row,
        "ItemTotalNumber",
        "Piece",
        "quantity",
        "件数",
    )


_DELIVERY_METHOD_CODE_MAP = {
    "180": "派送",
    "179": "送货上楼",
    "231": "送货进仓",
}


def _normalize_delivery_method_text(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if text in _DELIVERY_METHOD_CODE_MAP:
        return _DELIVERY_METHOD_CODE_MAP[text]
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return ""
    if "自提" in text:
        return "自提"
    if "派送" in text:
        return "派送"
    if "送货" in text or "配送" in text:
        return text
    return ""


def _first_delivery_method(row: dict[str, Any]) -> str:
    for key in ("DispatchMode", "delivery_method", "派送方式"):
        method = _normalize_delivery_method_text(row.get(key))
        if method:
            return method
    return ""


def _first_scan_status(row: dict[str, Any]) -> str:
    return _first_text(
        row,
        "scan_status",
        "current_scan_status",
        "scan_type",
        "SCAN_TYPE",
        "当前扫描状态",
        "最新扫描状态",
        "扫描状态",
        "最新扫描类型",
    )


def build_console_waybill_from_yunda_data(
    row: dict[str, Any],
    *,
    remote_waybill_no: str = "",
    target_date: dt.date | str | None = None,
) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    waybill_no = _normalize_waybill_no(
        _first_text(row, "LogisticsId", "tracking_number", "waybill_no", "5.14编号", "运单号")
        or remote_waybill_no
    )
    if not waybill_no:
        return None
    payment_method = _first_text(row, "PaymentType", "payment_method", "支付类型")
    freight_fee = _normalize_money_text(
        _first_text(row, "Freight", "shipping_fee", "现付", "月结", "提付")
    )
    return {
        "waybill_no": waybill_no,
        "destination_site": _first_text(
            row,
            "BuyerDestinationDotName",
            "DestinationDotName",
            "destination_site",
            "目的网点",
            "目的站点",
        ),
        "open_date": _normalize_date_text(
            _first_text(row, "OpenDate", "open_date", "日期"),
            target_date=target_date,
        ),
        "receiver_address": _first_text(
            row,
            "BuyerAddress",
            "receiver_address",
            "收件地址",
        ),
        "receiver_name": _first_text(
            row,
            "BuyerName",
            "receiver_name",
            "收货人",
        ),
        "receiver_phone": _first_text(
            row,
            "BuyerMobile",
            "BuyerPhone",
            "receiver_phone",
            "收货电话",
        ),
        "sender_name": _first_text(
            row,
            "SenderName",
            "sender_name",
            "寄件人",
        ),
        "sender_phone": _first_text(
            row,
            "SenderMobile",
            "SenderPhone",
            "sender_phone",
            "寄件手机",
        ),
        "goods_name_lines": _first_text(
            row,
            "ItemName",
            "goods_name",
            "货物名称",
        ),
        "package_type_lines": _build_package_type_lines(row),
        "quantity_lines": _build_quantity_lines(row),
        "weight_volume": _build_weight_volume(row),
        "delivery_method": _first_delivery_method(row),
        "freight_fee": freight_fee,
        "pickup_fee": _normalize_money_text(_first_text(row, "PickupFee", "pickup_fee")),
        "delivery_fee": _normalize_money_text(_first_text(row, "DeliveryFee", "delivery_fee")),
        "transfer_fee": _normalize_money_text(
            _first_text(row, "TransferFee", "transfer_fee", "中转运费")
        ),
        "payment_method": payment_method,
        "insurance_amount": _normalize_money_text(
            _first_text(row, "InsuredAmount", "InsuranceAmount", "保价金额", "申明价值")
        ),
        "cod_amount": _normalize_money_text(
            _first_text(row, "COD", "CollectionMoney", "cod_amount", "到付款")
        ),
        "remark": _first_text(row, "Remarks", "remark", "备注"),
        "scan_status": _first_scan_status(row),
        "status": "in_transit",
    }
