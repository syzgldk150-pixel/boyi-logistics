"""财务 · 派件账单的纯规则（shared.finance.delivery_bills）。数据均为虚构。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from shared.fee_engine import QuoteRule, parse_rule
from shared.finance.delivery_bills import (
    build_bill,
    date_chunks,
    delivery_fee,
    describe_rule,
    parse_fee_choice,
    parse_rule_form,
    parse_settings,
    settlement_entries,
    settlement_window,
    summarize_settlement,
    type_counted,
    value_added,
)
from shared.finance.errors import FinanceValidationError

D = dt.date


def seg(start, first_weight, first_price, extra_price):
    return {"from": start, "first_weight": first_weight, "first_price": first_price, "extra_price": extra_price}


def rule(effective="2026-11-01", start="10", base="30", extra="0.5", rounding="exact", rule_id=1, segments=None):
    segments = segments or [seg("0", base, start, extra)]
    parsed = parse_rule_form({"effective_from": effective, "segments": segments, "rounding": rounding})
    return {"id": rule_id, "effective_from": effective, "status": "active", "content": parsed["content"],
            "segments": segments, "rounding": rounding}


# 用户 2026-11-25 给的形状（数字虚构）：每段有自己的首重价和续重单价，正好等于下一段起点按下一段算。
SEGMENTS = [seg("0", "1", "5", "1"), seg("50", "50", "30", "0.5"), seg("200", "200", "100", "0.3")]


def settle(code, kind, amount, date="2026-11-05", weight="45.5", pieces="3", deleted="否"):
    return {"BILL_CODE": code, "BALANCE_TYPE": kind, "BALANCE_CUR_MONEY": amount, "BALANCE_DATE": f"{date} 10:00:00",
            "SETTLEMENT_WEIGHT": weight, "PIECE_NUMBER": pieces, "BL_DEL": deleted}


@pytest.mark.parametrize(("weight", "rounding", "expected"), [
    ("20", "exact", "10.00"), ("30", "exact", "10.00"), ("45.5", "exact", "17.75"),
    ("30.2", "ceil_kg", "10.50"), ("31", "ceil_kg", "10.50"), ("0", "exact", "10.00"),
])
def test_delivery_fee_start_price_plus_overweight(weight, rounding, expected):
    amount, steps = delivery_fee(rule(rounding=rounding), Decimal(weight))
    assert str(amount) == expected
    assert steps[-1] == f"合计 {expected}"


@pytest.mark.parametrize(("weight", "expected"), [
    ("0.5", "5.00"), ("1", "5.00"), ("49.9", "53.90"),  # 第 1 段：5 + 超过 1kg 部分 × 1
    ("50", "30.00"), ("120", "65.00"), ("199.99", "105.00"),  # 正好 50 按第 2 段：30 + 超过 50kg 部分 × 0.5
    ("200", "100.00"), ("250", "115.00"),  # 第 3 段不设上限
])
def test_delivery_fee_by_weight_segments(weight, expected):
    amount, steps = delivery_fee(rule(segments=SEGMENTS), Decimal(weight))
    assert str(amount) == expected
    assert steps[-1] == f"合计 {expected}" and "这一档" in steps[0]


def test_segments_with_whole_kilogram_rounding():
    amount, _ = delivery_fee(rule(segments=SEGMENTS, rounding="ceil_kg"), Decimal("120.2"))
    assert str(amount) == "65.50"  # 30 + 71 × 0.5


def test_describe_rule_lists_every_segment():
    text = describe_rule({"segments": SEGMENTS, "rounding": "exact"})
    assert text.startswith("0–50 公斤（不含）：首重 1 公斤 5 元，续重每公斤 1 元；50–200 公斤（不含）")
    assert "200 公斤起：首重 200 公斤 100 元，续重每公斤 0.3 元；按实际重量" in text


def test_rule_form_rejects_bad_input():
    def form(**changes):
        return {"effective_from": "2026-11-01", "segments": SEGMENTS, "rounding": "exact", **changes}

    for bad in (form(segments=[seg("0", "30", "-1", "1")]), form(rounding="round"), form(effective_from="2026/11/01"),
                form(segments=[seg("0", "30", "1.00001", "1")]), form(segments=[]),
                form(segments=[seg("5", "1", "5", "1")]),  # 第 1 段必须从 0 起
                form(segments=[seg("0", "1", "5", "1"), seg("50", "1", "5", "1"), seg("50", "1", "5", "1")]),
                form(segments=[seg("0", "1", "5", "1"), {"from": "50", "first_weight": "50"}]),
                form(start_price="10")):
        with pytest.raises(FinanceValidationError):
            parse_rule_form(bad)


def test_counted_types_default_to_payments_with_overrides():
    assert type_counted("付派送费", [], [])
    assert type_counted("付上楼费收入", [], [])
    assert not type_counted("派到付款", [], [])
    assert not type_counted("收工单罚款", [], [])
    assert type_counted("收工单罚款", ["收工单罚款"], [])
    assert not type_counted("付回单服务费", [], ["付回单服务费"])


def test_settlement_rows_group_by_waybill_and_skip_unrelated():
    grouped = settlement_entries([
        settle("r001", "付派送费", "40.5"), settle("R001", "派到付款", "-300"), settle("R001", "付到付款手续费", "3"),
        settle("", "收系统费", "-15"), settle("R002", "付派送费", "20", deleted="是"),
    ])
    assert set(grouped) == {"R001"}
    summary = summarize_settlement(grouped["R001"], [], [])
    assert summary["amount"] == Decimal("43.5")
    assert summary["settled"] and summary["settled_on"] == "2026-11-05"
    assert summary["settlement_weight"] == Decimal("45.5") and summary["pieces"] == 3
    assert [item["type"] for item in summary["types"]] == ["付派送费", "付到付款手续费", "派到付款"]


def test_settlement_rows_fail_closed_on_changed_page():
    with pytest.raises(FinanceValidationError):
        settlement_entries([{"BILL_CODE": "R1", "BALANCE_TYPE": "付派送费"}])
    with pytest.raises(FinanceValidationError):
        settlement_entries([settle("R1", "付派送费", "abc")])
    with pytest.raises(FinanceValidationError):
        settlement_entries([settle("R1", "付派送费", "1", deleted="未知")])


def test_settlement_window_and_chunks():
    assert settlement_window(D(2026, 11, 1), D(2026, 11, 30), 10, D(2026, 12, 31)) == (D(2026, 11, 1), D(2026, 12, 10))
    assert settlement_window(D(2026, 11, 1), D(2026, 11, 24), 10, D(2026, 11, 24)) == (D(2026, 11, 1), D(2026, 11, 24))
    assert date_chunks(D(2026, 11, 1), D(2026, 12, 10)) == [(D(2026, 11, 1), D(2026, 12, 1)), (D(2026, 12, 2), D(2026, 12, 10))]


def test_settings_validation():
    assert parse_settings({"lookahead_days": 10, "include_types": ["收工单罚款"], "exclude_types": []})["lookahead_days"] == 10
    for bad in ({"lookahead_days": 40, "include_types": [], "exclude_types": []},
                {"lookahead_days": True, "include_types": [], "exclude_types": []},
                {"lookahead_days": 5, "include_types": ["a"], "exclude_types": ["a"]},
                {"lookahead_days": 5, "include_types": [], "exclude_types": [], "other": 1}):
        with pytest.raises(FinanceValidationError):
            parse_settings(bad)


def arrivals():
    return [
        {"tracking_number": "R001", "arrival_date": "2026-11-04", "goods_name": "家具", "delivery_method": "派送"},
        {"tracking_number": "R002", "arrival_date": "2026-11-04", "goods_name": "百货", "delivery_method": "自提"},
        {"tracking_number": "R003", "arrival_date": "2026-11-05", "goods_name": "床垫", "delivery_method": "派送"},
    ]


def test_build_bill_only_settled_waybills_with_totals():
    settlements = settlement_entries([
        settle("R001", "付派送费", "40.5"), settle("R001", "付上楼费收入", "12"),
        settle("R002", "付回单服务费", "3"),  # 只有回单服务费：派送费没结算，不进账单
        settle("R003", "付派送费", "18", weight="20", pieces="1"),
        settle("R009", "付派送费", "25", date="2026-11-05"),  # 到货表里没有
    ])
    items = {"R003": {"status": "open", "value_added_total": "15.0000", "revision": 2, "lines": [{"fee_name": "上楼费"}]}}
    bill = build_bill(start=D(2026, 11, 1), end=D(2026, 11, 30), arrivals=arrivals(), settlements=settlements,
                      known_arrivals=["R001", "R002", "R003"], missing_dates=["2026-11-20"],
                      settings={"include_types": [], "exclude_types": []}, rules=[rule()], items=items)
    rows = {row["tracking_number"]: row for row in bill["rows"]}
    assert set(rows) == {"R001", "R003"}
    assert rows["R001"]["ronghui_amount"] == "52.5000" and rows["R001"]["delivery_fee"] == "17.7500"
    assert rows["R001"]["payable_total"] == "17.7500" and rows["R001"]["can_confirm"]
    assert rows["R003"]["delivery_fee"] == "10.0000" and rows["R003"]["payable_total"] == "25.0000"
    assert rows["R003"]["item_revision"] == 2
    assert [row["tracking_number"] for row in bill["unsettled"]] == ["R002"]
    assert bill["missing_in_arrival"] == [{"tracking_number": "R009", "settled_on": "2026-11-05", "ronghui_amount": "25.0000"}]
    summary = bill["summary"]
    assert (summary["arrival_count"], summary["settled_count"], summary["unsettled_count"]) == (3, 2, 1)
    assert summary["ronghui_total"] == "70.5000" and summary["payable_total"] == "42.7500"
    assert summary["difference_total"] == "27.7500" and summary["value_added_total"] == "15.0000"


def test_build_bill_without_rule_blocks_confirmation_and_keeps_confirmed_amounts():
    settlements = settlement_entries([settle("R001", "付派送费", "40.5"), settle("R003", "付派送费", "30")])
    confirmed = {"R003": {"status": "confirmed", "value_added_total": "0", "revision": 1, "delivery_fee": "9.0000",
                          "delivery_rule_id": 7, "delivery_steps": ["旧规则"], "payable_total": "9.0000",
                          "ronghui_amount": "28.0000", "lines": []}}
    bill = build_bill(start=D(2026, 11, 1), end=D(2026, 11, 30), arrivals=arrivals(), settlements=settlements,
                      known_arrivals=[], missing_dates=[], settings={"include_types": [], "exclude_types": []},
                      rules=[rule(effective="2026-12-01")], items=confirmed)
    rows = {row["tracking_number"]: row for row in bill["rows"]}
    assert not rows["R001"]["can_confirm"] and rows["R001"]["delivery_fee"] is None
    assert "派送费规则" in rows["R001"]["problems"][0]
    assert rows["R003"]["status"] == "confirmed" and rows["R003"]["delivery_fee"] == "9.0000"
    assert "与确认时" in rows["R003"]["problems"][0]
    assert bill["summary"]["blocked_count"] == 1


def test_latest_rule_on_or_before_arrival_date_applies():
    settlements = settlement_entries([settle("R001", "付派送费", "40", weight="10")])
    rules = [rule(effective="2026-10-01", start="8", rule_id=1), rule(effective="2026-11-04", start="12", rule_id=2),
             rule(effective="2026-11-10", start="99", rule_id=3)]
    bill = build_bill(start=D(2026, 11, 1), end=D(2026, 11, 30), arrivals=arrivals()[:1], settlements=settlements,
                      known_arrivals=[], missing_dates=[], settings={"include_types": [], "exclude_types": []},
                      rules=rules, items={})
    row = bill["rows"][0]
    assert row["delivery_rule_id"] == 2 and row["delivery_fee"] == "12.0000"


def test_fingerprint_changes_with_saved_state():
    settlements = settlement_entries([settle("R001", "付派送费", "40")])
    kwargs = dict(start=D(2026, 11, 1), end=D(2026, 11, 30), arrivals=arrivals()[:1], settlements=settlements,
                  known_arrivals=[], missing_dates=[], settings={"include_types": [], "exclude_types": []}, rules=[rule()])
    first = build_bill(**kwargs, items={})["rows"][0]["fingerprint"]
    second = build_bill(**kwargs, items={"R001": {"status": "open", "value_added_total": "5", "revision": 1}})["rows"][0]
    assert first != second["fingerprint"]


def fee(kind, name, content, group=None):
    if group:
        content = {**content, "group": group}
    return QuoteRule(kind, name, "V1（执行日期 2026-01-01）", parse_rule(content), f"id-{kind}")


FIXED = {"schema": 1, "params": [], "formula": {"type": "fixed", "amount": "15"}}
NEGOTIATE = {"schema": 1, "params": [], "formula": {"type": "none", "outcome": "NEGOTIATE", "reason": "按实际协商"}}
PER_FLOOR = {"schema": 1, "params": [{"key": "floors", "label": "楼层", "type": "number", "integer": True, "min": "1"}],
             "formula": {"type": "rate", "rate": "5", "of": "floors"}}


def test_value_added_amounts_groups_and_manual_amounts():
    catalog = [fee("a", "卸货费", FIXED, "上楼卸货"), fee("b", "上楼费", NEGOTIATE, "上楼卸货"), fee("c", "夜送", PER_FLOOR)]
    preview = value_added(catalog, ["a", "b", "c"], {}, {}, strict=False)
    assert not preview["complete"] and len(preview["missing"]) == 2
    with pytest.raises(FinanceValidationError):
        value_added(catalog, ["a", "b", "c"], {}, {}, strict=True)
    kinds, values, manual = parse_fee_choice({"fee_kinds": ["a", "b", "c"], "values": {"floors": 3},
                                              "manual": {"b": {"amount": "20", "note": "司机说明"}}})
    result = value_added(catalog, kinds, values, manual, strict=True)
    lines = {line["fee_kind"]: line for line in result["lines"]}
    assert lines["b"]["counted"] and lines["b"]["amount"] == "20.0000" and lines["b"]["note"] == "司机说明"
    assert not lines["a"]["counted"] and "取大" in lines["a"]["note"]
    assert lines["c"]["amount"] == "15.0000" and lines["c"]["rule_id"] == "id-c"
    assert result["total"] == "35.0000"


def test_value_added_rejects_unknown_fee_and_bad_manual_amount():
    catalog = [fee("a", "卸货费", FIXED)]
    with pytest.raises(FinanceValidationError):
        value_added(catalog, ["zzz"], {}, {}, strict=False)
    with pytest.raises(FinanceValidationError):
        parse_fee_choice({"fee_kinds": ["a"], "manual": {"a": {"amount": "-3"}}})
    assert value_added(catalog, [], {}, {}, strict=True)["total"] == "0.0000"
