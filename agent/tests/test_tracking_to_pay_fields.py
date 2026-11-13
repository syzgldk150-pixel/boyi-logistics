"""To-pay amount and goods payment stay separate in both tracking results."""

from agent.tms_runtime.scripts import ronghui_tms_tracking as ronghui
from agent.tms_runtime.scripts import yunda_waybill_tracking as yunda


def _info_items(info):
    return {item["label"]: item["value"] for section in info for item in section["items"]}


def test_yunda_cod_is_the_to_pay_amount_and_collection_money_is_goods_payment():
    detail = {"Logistics_Id": "900000001", "Payment_Type": "到付", "COD": "90.00", "Collection_Money": "0.00"}

    stub = yunda._build_waybill_stub(detail, "900000001")
    items = _info_items(yunda._build_waybill_info(detail, "900000001"))

    assert stub["payment_type"] == "到付"
    assert stub["to_pay_amount"] == "90.00"
    assert stub["cod_amount"] == "0.00"
    assert items["到付款"] == "90.00"
    assert items["代收货款"] == "0.00"


def test_ronghui_topayment_is_the_to_pay_amount():
    row = {"BILL_CODE": "R900000001", "PAYMENT_TYPE": "到付", "TOPAYMENT": "649", "GOODS_PAYMENT": None}

    detail = ronghui._normalize_api_detail_row(row, "R900000001")
    stub = ronghui._build_waybill_stub(detail, "R900000001")
    items = _info_items(ronghui._build_waybill_info(detail, "R900000001"))

    assert stub["payment_type"] == "到付"
    assert stub["to_pay_amount"] == "649"
    assert items["到付款"] == "649"


def test_receipt_number_reaches_both_tracking_stubs():
    ronghui_detail = ronghui._normalize_api_detail_row({"BILL_CODE": "R900000001", "R_BILLCODE": "hd0000001"}, "R900000001")
    assert ronghui._build_waybill_stub(ronghui_detail, "R900000001")["receipt_number"] == "hd0000001"
    assert yunda._build_waybill_stub({"Logistics_Id": "900000001", "ReturnLogisticsId": "900000009"}, "900000001")["receipt_number"] == "900000009"
    assert ronghui._build_waybill_stub(ronghui._normalize_api_detail_row({"BILL_CODE": "R900000001"}, "R900000001"), "R900000001")["receipt_number"] == ""
