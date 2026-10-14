"""Reviewed Yunda send fields shared by the package and direct source reader."""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation


FIELD_NAMES = (
    "运单编号", "目的网点", "收件区/县", "收件地址", "寄件人", "寄件手机",
    "收货人", "收货电话", "货物名称", "包装类型", "派送方式", "件数",
    "实际重量", "现付", "月结", "提付", "录单运费", "回单号", "备注",
    "结算重量", "体积", "支付类型", "体积重量", "到付款", "发件日期",
    "运费", "代收货款", "寄件地址", "需要回单", "保价金额",
)


def text(value: object) -> str:
    return "" if value is None else str(value).strip()


def first(*values: object) -> object:
    return next((value for value in values if value is not None and text(value)), "")


def decimal_text(value: object, *, required: bool = False) -> str:
    value_text = text(value).replace(",", "")
    if value_text in {"", "*"}:
        if required:
            raise ValueError("Yunda send recorded cost is missing")
        return ""
    try:
        number = Decimal(value_text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Yunda send numeric value is invalid") from exc
    if not number.is_finite():
        raise ValueError("Yunda send numeric value is invalid")
    return value_text


def delivery_method(row: Mapping[str, object], detail: Mapping[str, object]) -> str:
    raw = text(first(row.get("Shipping_Methods"), detail.get("Shipping_Methods")))
    if raw in {"231", "179", "180"}:
        return {"231": "送货进仓", "179": "送货上楼", "180": "派送"}[raw]
    if raw in {"自提", "不上楼", "送货", "派送", "送货进仓", "送货上楼"}:
        return raw
    method = text(first(row.get("Pickup_Method"), detail.get("Pickup_Method"), row.get("Shipping_Type_Name")))
    if not method:
        raise ValueError("Yunda send delivery method is missing")
    return method


def receipt_text(value: object) -> str:
    raw = text(value)
    if raw in {"1", "True"}:
        return "是"
    if raw in {"0", "False"}:
        return "否"
    if not raw:
        return ""
    raise ValueError("Yunda send receipt flag is invalid")


def normalize_record(
    row: Mapping[str, object], detail: Mapping[str, object],
    original: Mapping[str, object], renderer: Mapping[str, object], *,
    target_date: str | date, source: str = "send_waybill",
) -> dict[str, str]:
    bill_code = text(first(row.get("Logistics_Id"), detail.get("Logistics_Id")))
    if not bill_code or (text(detail.get("Logistics_Id")) and text(detail.get("Logistics_Id")) != bill_code):
        raise ValueError("Yunda send detail changed the waybill identity")
    dates = [text(value)[:10] for value in (row.get("Mail_Date"), detail.get("Mail_Date")) if text(value)]
    if not dates:
        raise ValueError("Yunda send Mail_Date is missing")
    expected_date = target_date.isoformat() if isinstance(target_date, date) else target_date
    if any(date.fromisoformat(value).isoformat() != expected_date for value in dates):
        raise ValueError("WAYBILL_SOURCE_DATE_MISMATCH")
    payment_type = text(first(row.get("Payment_Type"), detail.get("Payment_Type")))
    payment_type = {"102": "现金", "104": "月结", "103": "到付"}.get(payment_type, payment_type)
    freight = decimal_text(first(
        row.get("Special_Freight") if source == "special_line" else None,
        row.get("Freight"), detail.get("Freight"),
    ))
    if source == "send_waybill":
        if text(renderer.get("Logistics_Id")) != bill_code:
            raise ValueError("Yunda send renderer identity is missing or mismatched")
        price = renderer.get("price")
        if not isinstance(price, Mapping):
            raise ValueError("Yunda send renderer cost is missing")
        recorded_cost = decimal_text(price.get("Total"), required=True)
    elif source == "special_line":
        recorded_cost = decimal_text(row.get("Total_Cost_Money"), required=True)
    else:
        raise ValueError("Yunda send source is invalid")
    record = {
        "运单编号": bill_code,
        "目的网点": text(first(row.get("Buyer_Destination_Dot_Name"), detail.get("Buyer_Destination_Dot_Name"))),
        "收件区/县": text(first(row.get("Buyer_Area_Name"), row.get("Buyer_Area"), detail.get("Buyer_Area_Name"))),
        "收件地址": text(first(original.get("Buyer_Address"), row.get("Buyer_Address"), detail.get("Buyer_Address"))),
        "寄件人": text(first(original.get("Sender_Name"), row.get("Sender_Name"), detail.get("Sender_Name"))),
        "寄件手机": text(first(original.get("Sender_Mobile"), row.get("Sender_Mobile"), detail.get("Sender_Mobile"),
                              original.get("Sender_Phone"), row.get("Sender_Phone"), detail.get("Sender_Phone"))),
        "收货人": text(first(original.get("Buyer_Name"), row.get("Buyer_Name"), detail.get("Buyer_Name"))),
        "收货电话": text(first(original.get("Buyer_Mobile"), row.get("Buyer_Mobile"), detail.get("Buyer_Mobile"),
                              original.get("Buyer_Phone"), row.get("Buyer_Phone"), detail.get("Buyer_Phone"))),
        "货物名称": text(first(row.get("Item_Name"), detail.get("Item_Name"))),
        "包装类型": text(first(row.get("Packing_Type"), detail.get("Packing_Type"))),
        "派送方式": delivery_method(row, detail),
        "件数": decimal_text(first(row.get("Item_Total_Number"), detail.get("Item_Total_Number"))),
        "实际重量": decimal_text(first(row.get("Gross_Weight"), detail.get("Gross_Weight"))),
        "现付": freight if payment_type == "现金" else "",
        "月结": freight if payment_type == "月结" else "",
        "提付": freight if payment_type == "到付" else "",
        "录单运费": recorded_cost,
        "回单号": text(first(row.get("Return_Logistics_Id"), detail.get("Return_Logistics_Id"))),
        "备注": text(first(row.get("Remarks"), detail.get("Remarks"))),
        "结算重量": decimal_text(first(row.get("Settlement_Total_Number"), detail.get("Settlement_Total_Number"))),
        "体积": decimal_text(first(row.get("Volume"), detail.get("Volume"))),
        "支付类型": payment_type,
        "体积重量": decimal_text(first(detail.get("Extend_Field1"), row.get("Extend_Field1"))),
        "到付款": decimal_text(first(detail.get("COD"), row.get("COD"))),
        "发件日期": dates[0],
        "运费": freight,
        "代收货款": decimal_text(first(detail.get("Collection_Money"), row.get("Collection_Money"))),
        "寄件地址": text(first(original.get("Sender_Address"), row.get("Sender_Address"), detail.get("Sender_Address"))),
        "需要回单": receipt_text(first(renderer.get("Delivers_Return"), row.get("Delivers_Return"), detail.get("Delivers_Return"))),
        "保价金额": decimal_text(first(row.get("Insured_Amount"), detail.get("Insured_Amount"))),
    }
    if tuple(record) != FIELD_NAMES:
        raise ValueError("Yunda send normalized field order drifted")
    record["scan_status"] = text(first(detail.get("Scan_Description"), row.get("Scan_Description")))
    if source == "special_line":
        flags = detail.get("Scan_Flag_Keys")
        if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
            raise ValueError("Yunda special-line scan flags are missing or invalid")
        if "signed" in flags:
            record["status"] = "signed"
        elif any(flag in flags for flag in ("send", "transfer", "deliver")):
            record["status"] = "in_transit"
        else:
            raise ValueError("Yunda special-line signing status is unknown")
    else:
        sign_state = text(row.get("Sign_State"))
        if sign_state not in {"未签收", "已签收"}:
            raise ValueError("Yunda send signing status is missing or unknown")
        record["status"] = {"未签收": "in_transit", "已签收": "signed"}[sign_state]
    return record
