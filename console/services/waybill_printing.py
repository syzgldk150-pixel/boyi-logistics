"""Shared, JSON-safe waybill fields consumed by the existing label renderer."""

from collections.abc import Mapping
from typing import Any


WAYBILL_PRINT_FIELDS = (
    "waybill_no", "open_date", "destination_site", "receiver_name",
    "receiver_phone", "receiver_address", "sender_name", "sender_phone",
    "sender_address", "goods_name_lines", "package_type_lines", "quantity_lines",
    "weight_volume", "delivery_method", "freight_fee", "transfer_fee",
    "payment_method", "insurance_amount", "cod_amount", "remark",
)


def build_waybill_print_data(waybill: Mapping[str, Any]) -> dict[str, str | bool]:
    if not str(waybill.get("waybill_no") or "").strip():
        raise ValueError("Waybill number is required for printing")
    data: dict[str, str | bool] = {
        name: str(waybill[name]) if waybill.get(name) is not None else ""
        for name in WAYBILL_PRINT_FIELDS
    }
    data["receipt_required"] = waybill.get("receipt_required") == 1
    return data
