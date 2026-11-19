"""Block-built fee rules (shared.fee_engine) against the V6.1 manual's first batch of fees."""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from shared.fee_engine import QuoteRule, boundary_rows, calculate, parse_rule, quote, shared_params
from shared.handbook_fee_rules import FeeRuleError

ROOT = Path(__file__).resolve().parents[1]
BATCH = json.loads((ROOT / "tests" / "fixtures" / "fee_rules" / "v61_first_batch.json").read_text(encoding="utf-8"))
RULES = {kind: parse_rule(item["content"]) for kind, item in BATCH.items()}


def fee(kind: str, **values):
    return calculate(RULES[kind], values)


def upstairs(weight="200", floor=5, elevator=False, goods="general", pieces=None):
    return fee("upstairs", settlement_weight=weight, floor=floor, elevator=elevator, goods_type=goods,
               pieces=pieces if pieces is not None else [{"count": 1, "weight": "30"}])


def test_every_first_batch_fee_is_a_valid_rule():
    assert set(RULES) >= {"upstairs", "unloading", "unpacking", "count_check", "stacking", "night_delivery"}
    shared_params([QuoteRule(kind, item["name"], "V6.1", RULES[kind]) for kind, item in BATCH.items()])


# -- upstairs: the V6.1 table (p.50) as decided with the user on 2026-11-19 ------------------------------

@pytest.mark.parametrize("floor,expected", [(1, "0.00"), (2, "34.00"), (3, "44.00"), (4, "44.00"), (5, "54.00"), (6, "54.00"),
                                            (7, "64.00"), (8, None), (9, None)])
@pytest.mark.parametrize("pieces,surcharge", [([], "0"), ([{"count": 2, "weight": "80"}], "20"), ([{"count": 1, "weight": "110"}], "30"),
                                              ([{"count": 1, "weight": "120"}], "30"), ([{"count": 1, "weight": "50"}], "0")])
def test_upstairs_without_elevator_table(floor, expected, pieces, surcharge):
    result = upstairs("200", floor, pieces=pieces + [{"count": 2, "weight": "10"}])
    if expected is None:
        assert result.outcome == "NEGOTIATE"
    elif floor == 1:
        assert result.amount == Decimal("0.00")  # the 1st floor is not upstairs (p.50)
    else:
        assert result.amount == Decimal(expected) + Decimal(surcharge)


@pytest.mark.parametrize("floor", [1, 5, 9, 30])
def test_upstairs_with_elevator_ignores_floor_and_heavy_pieces(floor):
    heavy = [{"count": 1, "weight": "130"}]
    assert upstairs("400", floor, elevator=True, pieces=heavy).amount == Decimal("29.00")  # 5 + 0.06 × 400
    assert upstairs("400", 3, pieces=heavy).outcome == "NEGOTIATE"  # without an elevator a 130 kg piece is refused


def test_upstairs_steps_and_sources_explain_the_amount():
    result = upstairs("200", 5, pieces=[{"count": 2, "weight": "80"}, {"count": 1, "weight": "40"}])
    assert result.amount == Decimal("74.00")
    assert "0.1 元/kg × 结算重量 200 = 20" in result.steps
    assert "单件实际重量 80kg 的 2 件：10 × 2 = 20" in result.steps
    assert result.steps[-1] == "合计 74.00"
    assert result.citations == ("融辉物流网络管理手册 V6.1 第五篇第五章 第 50 页",)
    assert "上楼费和卸货费不可同时收取，同时产生时取大值。" in result.reminders


def test_upstairs_first_floor_and_exactly_120_kg():
    assert upstairs("200", 1).amount == Decimal("0.00")
    assert upstairs("200", 3, pieces=[{"count": 1, "weight": "120"}]).amount == Decimal("74.00")  # 30 元/件 surcharge
    assert upstairs("200", 3, pieces=[{"count": 1, "weight": "120.01"}]).outcome == "NEGOTIATE"
    assert upstairs("200", 3, elevator=True, pieces=[{"count": 1, "weight": "130"}]).amount == Decimal("17.00")


@pytest.mark.parametrize("stairs,floor", [(0, 1), (16, 1), (17, 2), (25, 2), (32, 2), (33, 3), (112, 7), (113, 8)])
def test_upstairs_stairs_count_as_floors_page_50(stairs, floor):
    by_stairs = fee("upstairs", settlement_weight="200", stairs=stairs, elevator=False, goods_type="general",
                    pieces=[{"count": 1, "weight": "30"}])
    assert (by_stairs.outcome, by_stairs.amount) == (upstairs("200", floor).outcome, upstairs("200", floor).amount)


def test_upstairs_floor_wins_over_stairs_and_one_of_them_is_needed():
    both = fee("upstairs", settlement_weight="200", floor=5, stairs=20, elevator=False, goods_type="general",
               pieces=[{"count": 1, "weight": "30"}])
    assert both.amount == upstairs("200", 5).amount
    neither = fee("upstairs", settlement_weight="200", elevator=False, goods_type="general",
                  pieces=[{"count": 1, "weight": "30"}])
    assert neither.outcome == "MISSING" and neither.missing == ("送到几楼或步梯台阶数",)


def test_upstairs_rounds_half_up_at_the_end():
    assert upstairs("123.45", 3, elevator=True).amount == Decimal("12.41")  # 5 + 0.06 × 123.45 = 12.407


def test_upstairs_pallet_and_mixed_packaging_with_elevator():
    heavy = [{"count": 3, "weight": "160"}]
    assert upstairs("480", 3, True, "pallet", heavy).amount == Decimal("45.00")  # 15 元/件 × 3
    light = [{"count": 2, "weight": "100"}]
    assert upstairs("200", 3, True, "pallet", light).amount == Decimal("17.00")  # 普货有电梯 5 + 0.06 × 200
    mixed = [{"count": 1, "weight": "160"}, {"count": 1, "weight": "100"}]
    assert upstairs("260", 3, True, "pallet", mixed).outcome == "MANUAL"
    assert upstairs("200", 3, True, "mixed_pallet", light).amount == Decimal("17.00")
    assert upstairs("200", 3, False, "pallet", light).amount == upstairs("200", 3, False, "general", light).amount


def test_upstairs_mattress_table():
    beds = [{"count": 3, "weight": "25", "length": "2", "width": "1.8"}]
    assert upstairs("75", 4, False, "mattress", beds).amount == Decimal("100.00")  # 60 + 20 × 2
    assert upstairs("75", 4, True, "mattress", beds).amount == Decimal("50.00")  # 30 + 10 × 2
    many = [{"count": 20, "weight": "25", "length": "2", "width": "1.8"}]
    assert upstairs("500", 4, False, "mattress", many).amount == Decimal("400.00")  # capped
    small = [{"count": 2, "weight": "15", "length": "1.5", "width": "1"}]
    assert upstairs("30", 4, False, "mattress", small).amount == upstairs("30", 4, False, "general", small).amount
    assert upstairs("75", 4, False, "mattress", [{"count": 1, "weight": "25", "length": "2.5", "width": "1"}]).outcome == "MANUAL"
    assert upstairs("75", 4, False, "mattress", beds + small).outcome == "MANUAL"


def test_a_missing_piece_detail_is_asked_not_guessed():
    result = upstairs("30", 4, False, "mattress", [{"count": 1, "weight": "15"}])
    assert result.outcome == "MISSING" and result.missing == ("货物明细的单边最长",)
    # a weight above 20 kg already makes it a mattress, but the 2.4 m limit still needs the length
    assert upstairs("30", 4, False, "mattress", [{"count": 1, "weight": "25"}]).outcome == "MISSING"


# -- the other fees, with the manual's own examples ------------------------------------------------------

def test_unloading_manual_example_page_48():
    result = fee("unloading", settlement_weight="4000", pieces=[{"count": 2, "weight": "1800"}, {"count": 1, "weight": "400"}])
    assert result.amount == Decimal("100.00")  # 50 元/件 × 2 beats 0.015 × 4000 = 60
    assert any("按单件超重超大 100" in step for step in result.steps)
    assert fee("unloading", settlement_weight="1500", pieces=[{"count": 3, "weight": "500"}]).amount == Decimal("0.00")
    assert fee("unloading", settlement_weight="1600", pieces=[{"count": 4, "weight": "400"}]).amount == Decimal("25.00")
    assert fee("unloading", settlement_weight="30000", pieces=[{"count": 30, "weight": "1000"}]).amount == Decimal("300.00")


@pytest.mark.parametrize("pallets,crates,expected", [(2, 0, "0"), (3, 0, "10"), (5, 0, "20"), (0, 4, "40"), (5, 1, "30")])
def test_unpacking_manual_example_page_59(pallets, crates, expected):
    assert fee("unpacking", pallet_count=pallets, crate_count=crates).amount == Decimal(expected)


def test_unpacking_defaults_to_zero_for_the_count_not_given():
    assert fee("unpacking", pallet_count=5).amount == Decimal("20.00")


@pytest.mark.parametrize("count,expected", [(20, "0"), (21, "5"), (60, "6"), (1500, "100")])
def test_count_check(count, expected):
    assert fee("count_check", check_count=count).amount == Decimal(expected)


def test_stacking_takes_the_highest_of_three():
    assert fee("stacking", sort_count=31).amount == Decimal("10.00")  # 6.2 → minimum 10
    assert fee("stacking", sort_count=100).amount == Decimal("20.00")
    assert fee("stacking", shelf_count=2000, shelf_high=True).amount == Decimal("500.00")
    assert fee("stacking", shelf_count=100).amount == Decimal("20.00")  # low shelf uses the sorting standard
    assert fee("stacking", room_count=4, room_piece_count=60).amount == Decimal("100.00")
    assert fee("stacking", room_count=3, room_piece_count=60).amount == Decimal("0.00")
    assert fee("stacking", sort_count=100, room_count=5, room_piece_count=20).amount == Decimal("40.00")


def test_other_value_added_services_page_63():
    assert fee("night_delivery").amount == Decimal("50.00")
    assert fee("revisit").amount == Decimal("10.00")
    assert fee("disinfect", address_count=2).amount == Decimal("10.00")
    assert fee("disinfect", address_count=4).amount == Decimal("30.00")
    assert fee("strip_packaging", is_pallet=False, strip_count=5).amount == Decimal("10.00")
    assert fee("strip_packaging", is_pallet=False, strip_count=300).amount == Decimal("100.00")
    assert fee("strip_packaging", is_pallet=True, strip_count=5).amount == Decimal("20.00")
    assert fee("redelivery", delivery_times=2, base_delivery_fee="12").amount == Decimal("0.00")
    result = fee("redelivery", delivery_times=4, base_delivery_fee="12")
    assert result.amount == Decimal("44.00") and "22" in result.steps[0]
    toll = fee("highway_toll")
    assert toll.outcome == "ACTUAL" and toll.amount is None and "实报实销" in toll.reason


# -- inputs ----------------------------------------------------------------------------------------------

def test_missing_values_are_all_listed():
    result = calculate(RULES["upstairs"], {"floor": 3})
    assert result.outcome == "MISSING"  # floor and stairs are either-or, so neither is listed here
    assert result.missing == ("结算重量", "有电梯", "货物类型", "货物明细")


def test_invalid_values_are_all_reported_at_once():
    with pytest.raises(FeeRuleError) as caught:
        calculate(RULES["upstairs"], {"settlement_weight": "abc", "floor": 2.5, "elevator": "yes", "goods_type": "sofa",
                                      "pieces": [{"count": 0, "weight": "10"}]})
    message = caught.value.message
    for text in ("结算重量不是有效数字", "送到几楼必须是整数", "有电梯只能选是或否", "货物类型的选项无效", "第 1 行的件数"):
        assert text in message


# -- rule validation -------------------------------------------------------------------------------------

def _broken(change):
    content = copy.deepcopy(BATCH["unpacking"]["content"])
    change(content)
    return content


def test_validation_reports_every_problem_at_once():
    def change(content):
        tiers = content["formula"]["items"][0]["tiers"]
        tiers[1]["over"] = "3"  # gap between 2 and 3
        content["formula"]["items"][1]["of"] = "nope"
        content["params"].append({"key": "pallet_count", "label": "重复", "type": "number"})
        content["formula"]["items"].append({"type": "clamp", "item": {"type": "fixed", "amount": "1"}})
    with pytest.raises(FeeRuleError) as caught:
        parse_rule(_broken(change))
    message = caught.value.message
    assert "起点应等于上一档的终点 2" in message
    assert "参数“nope”不在参数清单里" in message
    assert "参数标识“pallet_count”重复" in message
    assert "至少要填最低或最高金额" in message


@pytest.mark.parametrize("node,text", [
    ({"type": "rate", "rate": "1", "of": "piece.weight"}, "不在“逐件计费”"),
    ({"type": "fixed", "amount": "-1"}, "金额必须是 0 到"),
    ({"type": "none", "outcome": "FREE", "reason": "x"}, "结果必须是"),
    ({"type": "use", "name": "没有"}, "公共部分“没有”不存在"),
    ({"type": "when", "cases": [{"if": {"left": "pallet_count", "op": "in", "value": ["1"]},
                                 "then": {"type": "fixed", "amount": "1"}}]}, "数字条件不能用“属于”"),
    ({"type": "tiers", "by": "pallet_count", "tiers": [{"over": None, "up_to": "5", "then": {"type": "fixed", "amount": "1"}},
                                                       {"over": "5", "up_to": "3", "then": {"type": "fixed", "amount": "1"}}]},
     "终点必须大于起点"),
    ({"type": "magic"}, "不是有效的积木"),
])
def test_validation_rejects_bad_blocks(node, text):
    content = _broken(lambda c: c.update(formula=node))
    with pytest.raises(FeeRuleError) as caught:
        parse_rule(content)
    assert text in caught.value.message


def test_definitions_may_not_use_definitions():
    content = copy.deepcopy(BATCH["upstairs"]["content"])
    content["definitions"]["别名"] = {"type": "use", "name": "普货"}
    with pytest.raises(FeeRuleError, match="公共部分里不能再引用公共部分"):
        parse_rule(content)


def test_rule_size_is_bounded():
    deep = {"type": "fixed", "amount": "1"}
    for _ in range(20):
        deep = {"type": "sum", "items": [deep]}
    with pytest.raises(FeeRuleError, match="嵌套超过"):
        parse_rule(_broken(lambda c: c.update(formula=deep)))


# -- quotes ----------------------------------------------------------------------------------------------

def _quote(kinds, values):
    return quote([QuoteRule(kind, BATCH[kind]["name"], "V6.1", RULES[kind]) for kind in kinds], values)


def test_quote_group_takes_the_larger_fee():
    values = {"settlement_weight": "1600", "floor": 3, "elevator": True, "goods_type": "general",
              "pieces": [{"count": 4, "weight": "100"}], "check_count": 50, "sort_count": 100}
    result = _quote(["upstairs", "unloading", "count_check", "stacking", "night_delivery"], values)
    items = {item.fee_kind: item for item in result.items}
    assert items["upstairs"].result.amount == Decimal("101.00") and items["upstairs"].counted  # 5 + 0.06 × 1600
    assert not items["unloading"].counted and "按上楼费收取" in items["unloading"].note
    assert items["stacking"].counted and not items["count_check"].counted
    assert result.total == Decimal("171.00") and result.complete  # 101 + 20 + 50


def test_quote_group_with_an_undecided_fee_counts_neither():
    values = {"settlement_weight": "2000", "floor": 3, "elevator": False, "goods_type": "general",
              "pieces": [{"count": 1, "weight": "1600"}]}
    result = _quote(["upstairs", "unloading", "night_delivery"], values)
    assert [item.counted for item in result.items] == [False, False, True]
    assert "需人工判断" in result.items[1].note
    assert result.total == Decimal("50.00") and not result.complete


def test_quote_lists_missing_values_once():
    result = _quote(["upstairs", "unloading"], {"floor": 3})
    assert result.missing == ("结算重量", "有电梯", "货物类型", "货物明细")
    assert result.total == Decimal("0") and not result.complete


def test_shared_parameters_must_mean_the_same_thing():
    other = copy.deepcopy(BATCH["unloading"]["content"])
    other["params"][0]["unit"] = "吨"
    rules = [QuoteRule("upstairs", "上楼费", "V6.1", RULES["upstairs"]), QuoteRule("x", "另一种", "1", parse_rule(other))]
    with pytest.raises(FeeRuleError, match="参数“settlement_weight”"):
        quote(rules, {})


# -- review aids -----------------------------------------------------------------------------------------

def test_boundary_rows_show_each_tier_change():
    base = {"settlement_weight": "200", "floor": 3, "elevator": False, "goods_type": "general",
            "pieces": [{"count": 1, "weight": "30"}]}
    rows = {(row["param"], row["value"]): row for row in boundary_rows(RULES["upstairs"], base)}
    assert rows[("送到几楼", "1")]["amount"] == "0.00"
    assert rows[("送到几楼", "7")]["amount"] == "64.00" and rows[("送到几楼", "8")]["outcome"] == "NEGOTIATE"
    assert rows[("单件实际重量", "100")]["amount"] == "54.00" and rows[("单件实际重量", "100.01")]["amount"] == "74.00"
    assert rows[("单件实际重量", "120.01")]["outcome"] == "NEGOTIATE"


