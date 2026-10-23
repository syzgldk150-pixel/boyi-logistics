"""Raw platform statement capture: completeness, consistency and projection."""
from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal

import pytest

from shared.finance.platform_capture import PlatformCaptureError, build_platform_capture


MAPPING = {
    "detail": {
        "source_key": "GUID", "settled_at": "BALANCE_DATE", "billed_at": "FINANCE_DATE", "item_name": "BALANCE_TYPE",
        "amount": "BALANCE_CUR_MONEY", "before_balance": "BALANCE_PRE_CONFIRM_MONEY",
        "after_balance": "BALANCE_BACK_CONFIRM_MONEY", "waybill_no": "BILL_CODE", "order_no": "BALANCE_ORDER",
        "site_code": "SITE_NAME_CODE", "site_name": "SITE_NAME", "source_type": "SOURCE_TYPE", "remark": "REMARK",
        "customer_name": "CUSTOMER_NAME", "goods_name": "GOODS_NAME",
    },
    "statement": {"item_name": "BALANCE_TYPE", "amount": "BALANCE_CUR_MONEY", "site_code": "SITE_NAME_CODE",
                  "site_name": "SITE_NAME"},
}
PROFILE = {"rows_pointer": "/data", "total_pointer": "/total", "page_fields": ["pageIndex", "pageSize"],
           "site_field": "SITE_NAME_CODE", "settlement_period_field": "BALANCE_DATE",
           "billing_period_field": "FINANCE_DATE", "waybill_number_shapes": ["R+11", "10"]}
SITE = "7000001"
PERIOD = '{"start": "2026/03/01 00:00:00", "end": "2026/03/31 23:59:59"}'


def detail_rows():
    rows, balance, order = [], Decimal("100.00"), 1000
    for day in (1, 2, 31):
        for item, amount, code in (("合成派送费", Decimal("-12.50"), "R00000000001"), ("合成到付款", Decimal("30.00"), "R00000000002"),
                                   ("合成罚款", Decimal("0"), "R00000000003")):
            order += 1
            after = balance + amount
            rows.append({"GUID": f"g-{day}-{item}", "BALANCE_DATE": f"2026-03-{day:02d} 09:{order % 60:02d}:00",
                         "FINANCE_DATE": f"2026-03-{day:02d} 08:00:00", "BALANCE_TYPE": item,
                         "BALANCE_CUR_MONEY": float(amount), "BALANCE_PRE_CONFIRM_MONEY": float(balance),
                         "BALANCE_BACK_CONFIRM_MONEY": float(after), "BILL_CODE": code, "BALANCE_ORDER": order,
                         "SITE_NAME_CODE": SITE, "SITE_NAME": "合成网点", "SOURCE_TYPE": "系统", "REMARK": "",
                         "CUSTOMER_NAME": "合成客户"})
            balance = after
    return rows


def statement_rows(rows):
    totals = {}
    for row in rows:
        totals[row["BALANCE_TYPE"]] = totals.get(row["BALANCE_TYPE"], Decimal(0)) + Decimal(str(row["BALANCE_CUR_MONEY"]))
    return [{"BALANCE_TYPE": name, "BALANCE_CUR_MONEY": float(amount), "SITE_NAME_CODE": SITE, "SITE_NAME": "合成网点"}
            for name, amount in totals.items()]


def pages(rows, *, field="BALANCE_DATE", size=4, site=SITE, period=PERIOD):
    result = []
    for index in range(0, max(len(rows), 1), size):
        body = {field: period, "SITE_NAME_CODE": site, "pageIndex": str(index // size), "pageSize": str(size)}
        result.append({"body": body, "response": {"total": len(rows), "data": rows[index:index + size]}})
    return result


def capture(**overrides):
    rows = detail_rows()
    values = {
        "site_code": SITE, "period_start": dt.date(2026, 3, 1), "period_end": dt.date(2026, 3, 31),
        "field_mapping": MAPPING, "request_profile": PROFILE,
        "statement_pages": pages(statement_rows(rows), size=50),
        "billing_statement_pages": pages(statement_rows(rows), field="FINANCE_DATE", size=50),
        "detail_pages": pages(rows),
    }
    values.update(overrides)
    return build_platform_capture(**values)


def test_complete_capture_keeps_raw_rows_and_closes_statement_by_item():
    result = capture()
    assert result.metrics["detail_row_count"] == 9 and result.metrics["statement_item_count"] == 3
    assert result.metrics["detail_net"] == result.metrics["statement_net"] == Decimal("52.5000")
    assert result.metrics["billing_statement_net"] == Decimal("52.5000")
    assert result.site_name == "合成网点"
    first = result.detail_rows[0]
    assert first["raw"]["CUSTOMER_NAME"] == first["customer_name"] == "合成客户" and first["goods_name"] is None
    assert first["waybill_no"] == "R00000000001"
    assert first["settled_at"] == dt.datetime(2026, 3, 1, 9, 41)
    assert sum(1 for row in result.detail_rows if row["amount"] == 0) == 3
    assert capture().content_sha256 == result.content_sha256


@pytest.mark.parametrize("mutate, code", [
    (lambda v: v["detail_pages"].pop(), "PAGINATION_INCOMPLETE"),
    (lambda v: v["detail_pages"][1]["body"].update(BALANCE_DATE='{"start": "2026/03/02"}'), "PAGINATION_DRIFT"),
    (lambda v: v["detail_pages"][1]["response"].update(total=99), "PAGINATION_TOTAL_DRIFT"),
    (lambda v: v["detail_pages"][0]["body"].update(SITE_NAME_CODE="7000002"), "SITE_MISMATCH"),
    (lambda v: [page["body"].update(BALANCE_DATE='{"start": "2026/03/01", "end": "2026/03/15"}')
                for page in v["detail_pages"]], "PERIOD_MISMATCH"),
    (lambda v: v["statement_pages"][0]["response"]["data"][0].update(BALANCE_CUR_MONEY=1.0), "STATEMENT_MISMATCH"),
    (lambda v: v["detail_pages"][0]["response"]["data"][1].update(GUID="g-1-合成派送费"), "DUPLICATE_SOURCE_KEY"),
    (lambda v: v["detail_pages"][0]["response"]["data"][1].update(BALANCE_BACK_CONFIRM_MONEY=1.0), "BALANCE_EQUATION_MISMATCH"),
    (lambda v: v["detail_pages"][0]["response"]["data"][0].update(SITE_NAME_CODE="7000002"), "SITE_MISMATCH"),
    (lambda v: v["detail_pages"][0]["response"]["data"][0].update(BALANCE_DATE="2026-04-01 00:00:00"), "PERIOD_MISMATCH"),
    (lambda v: v["field_mapping"]["detail"].pop("remark"), "MAPPING_INVALID"),
    (lambda v: v["request_profile"].update(rows_pointer="data"), "PROFILE_INVALID"),
    (lambda v: v["request_profile"].update(waybill_number_shapes=["R+11", "R+11"]), "PROFILE_INVALID"),
    (lambda v: v["request_profile"].update(waybill_number_shapes=["^R.*$"]), "PROFILE_INVALID"),
])
def test_incomplete_or_inconsistent_captures_are_rejected(mutate, code):
    rows = detail_rows()
    values = {
        "site_code": SITE, "period_start": dt.date(2026, 3, 1), "period_end": dt.date(2026, 3, 31),
        "field_mapping": copy.deepcopy(MAPPING), "request_profile": copy.deepcopy(PROFILE),
        "statement_pages": pages(statement_rows(rows), size=50),
        "billing_statement_pages": [], "detail_pages": pages(rows),
    }
    mutate(values)
    with pytest.raises(PlatformCaptureError) as error:
        build_platform_capture(**values)
    assert error.value.code == code


def test_balance_chain_must_continue_across_days():
    rows = detail_rows()
    shift = rows[4:]
    for row in shift:
        row["BALANCE_PRE_CONFIRM_MONEY"] += 1
        row["BALANCE_BACK_CONFIRM_MONEY"] += 1
    with pytest.raises(PlatformCaptureError) as error:
        capture(detail_pages=pages(rows), statement_pages=pages(statement_rows(rows), size=50))
    assert error.value.code == "BALANCE_CHAIN_BREAK"


@pytest.mark.parametrize("value, shape, label", [
    ("R00000000001", "R+11", "R+11位数字"), ("0123456789", "10", "10位数字"),
    ("S2026000000000000001", "S+19", "S+19位数字"), ("", "", "无单号"), (None, "", "无单号"),
    ("12-34", "other", "其他格式"),
])
def test_number_shape_describes_letters_and_digit_count(value, shape, label):
    from shared.finance.platform_capture import number_shape, shape_label

    assert number_shape(value) == shape and shape_label(shape) == label


def test_period_is_one_to_thirty_one_days():
    with pytest.raises(PlatformCaptureError) as error:
        capture(period_end=dt.date(2026, 4, 30))
    assert error.value.code == "PERIOD_INVALID"
