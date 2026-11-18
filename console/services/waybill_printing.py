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


PLATFORM_LABEL_SOURCES = {"ronghui": "融辉", "yunda": "韵达"}


def _open_date(value: Any) -> str:
    text = value.isoformat() if hasattr(value, "isoformat") else str(value or "")
    return text.strip()[:10]


def platform_print_target(waybill: Mapping[str, Any], linked_lookup) -> dict[str, str] | None:
    """Which original-site label a waybill prints, or None for the Boyi label.

    融辉/韵达寄件 print their own platform label. A Boyi waybill linked to a
    融辉/韵达 waybill prints that platform label (2026-11-18, 用户确认); other
    Boyi/OCR waybills, and those linked to 百世, keep the Boyi label. The
    platform account is the one that synchronised the row; it is never guessed.
    """
    source = str(waybill.get("source") or "")
    if source in PLATFORM_LABEL_SOURCES:
        platform, row = source, waybill
    else:
        platform = str(waybill.get("linked_platform") or "")
        linked_no = str(waybill.get("linked_waybill_no") or "").strip()
        if source != "manual" or platform not in PLATFORM_LABEL_SOURCES or not linked_no:
            return None
        row = linked_lookup(platform, linked_no)
    waybill_no = str(row.get("waybill_no") or "").strip()
    account_id = str(row.get("source_account_id") or "").strip()
    if not waybill_no:
        raise LookupError("运单号缺失，无法打印原站面单。")
    if not account_id:
        raise LookupError(f"这张{PLATFORM_LABEL_SOURCES[platform]}运单没有记录同步账号，无法确定用哪个账号读取原站面单；"
                          "请重新查询一次该日期的运单后再打印。")
    return {"platform": platform, "waybill_no": waybill_no, "account_id": account_id,
            "open_date": _open_date(row.get("open_date"))}
