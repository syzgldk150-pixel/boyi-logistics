"""报价 P3 (2026-11-20): the robot computes any fees approved in 业务手册 → 计算规则.

Billing date, settlement weight and piece count come from the live waybill; fees and
the other parameters only from the person's words (asked when missing); only rule
versions approved in 业务手册 → 计算规则 give an amount. The waybill numbers and
remarks here are invented.
"""
import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from agent.agent_actions import fee_actions
from agent.agent_actions.business_actions import BusinessActions
from agent.orchestration.models import Actor, ActorType
ROOT = Path(__file__).resolve().parents[1]
BATCH = json.loads((ROOT / "tests" / "fixtures" / "fee_rules" / "v61_first_batch.json").read_text(encoding="utf-8"))


def approved(kind, effective="2026-10-01"):
    return {"rule_id": f"r-{kind}", "fee_kind": kind, "fee_name": BATCH[kind]["name"], "content": BATCH[kind]["content"],
            "checks": [], "status": "APPROVED", "version_label": "V6.1", "effective_from": effective}


APPROVED = approved("upstairs")
ALL = (APPROVED, approved("unloading"), approved("count_check"), approved("unpacking"))
ACTOR = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin", "super_admin"), authenticated_by="feishu_admin_binding")
CODE = "R00099990001"


def row(**changes):
    base = {"requested_bill_code": CODE, "tracking_number": CODE, "billing_date": "2026-11-12 16:27:26",
            "settlement_weight": 195, "actual_weight": 195, "quantity": 1, "upstairs_service": 0,
            "remark": "客户要求拆托上楼，费用客户自行承担"}
    base.update(changes)
    return base


class Platform:
    def __init__(self, rows=None, fail=None):
        self.calls, self.rows, self.fail = [], rows if rows is not None else [row()], fail

    def __call__(self, operation, params, principal, request_id):
        self.calls.append((operation, dict(params)))
        assert operation == "query_waybill_detail"
        if self.fail:
            return {"ok": False, "error_code": "AUTH_REQUIRED", "error": self.fail}
        return {"ok": True, "data": {"ok": True, "data": deepcopy(self.rows)}}


def _turn(platform, rules=(APPROVED,), recorded=None):
    actions = BusinessActions(invoke=platform, cs_accounts=lambda: [], fee_rules=lambda kind: list(rules),
                              today=lambda: date(2026, 11, 16),
                              fee_quote_recorder=(lambda **kwargs: recorded.append(kwargs)) if recorded is not None else None)
    return actions.turn(ACTOR)


def ask(turn, text, tracking=None):
    arguments = {"tracking_number": tracking} if tracking else {}
    return turn.execute("biz_fee_quote", arguments, message=text)


@pytest.mark.parametrize("text,floor", [("5楼", 5), ("送到5层", 5), ("五楼", 5), ("十二楼", 12), ("二十层", 20),
                                        ("R00099990001", None), ("3楼还是5楼", None)])
def test_floor_is_read_from_the_persons_words(text, floor):
    assert fee_actions.parse_floor(text) == floor


@pytest.mark.parametrize("text,elevator", [("无电梯", False), ("没有电梯", False), ("步梯", False), ("有电梯", True),
                                           ("电梯房", True), ("5楼", None), ("有电梯吗没电梯", None)])
def test_elevator_is_read_from_the_persons_words(text, elevator):
    assert fee_actions.parse_elevator(text) is elevator


def test_full_question_gives_the_amount_with_its_basis_and_the_remark_reminder():
    platform, recorded = Platform(), []
    text, decision = ask(_turn(platform, recorded=recorded), f"{CODE} 送5楼 无电梯 普货 上楼费多少", CODE)
    assert decision.decision == "EXECUTE" and decision.action_id == "fee.quote"
    assert platform.calls == [("query_waybill_detail", {"bill_codes": [CODE], "decrypt_masked": False})]
    # 195 kg, one piece of 195 kg (over the 120 kg limit) -> negotiate, no fixed amount
    assert "· 上楼费：需协商" in text and "单件实际重量超过 120 公斤" in text
    assert "开单日期 2026-11-12" in text and "结算重量 195 公斤" in text and "件数 1" in text
    assert "规则 V6.1（执行日期 2026-10-01）" in text and "货物类型 普货" in text
    assert "单上没有勾选上楼服务，但备注写了" in text
    assert recorded[0]["channel"] == "feishu" and recorded[0]["fee_kinds"] == ["upstairs"]
    assert recorded[0]["tracking_number"] == CODE and recorded[0]["billing_date"] == date(2026, 11, 12)


def test_missing_parameters_are_asked_in_one_list_then_the_same_waybill_is_used():
    platform = Platform(rows=[row(actual_weight=80, settlement_weight=80)])
    turn = _turn(platform)
    text, decision = ask(turn, f"算一下 {CODE} 的上楼费", CODE)
    assert decision.decision == "CLARIFY" and "送到几楼" in text and "有电梯（有/无）" in text and "货物类型（普货 / 床垫" in text
    text, decision = ask(turn, "5楼")
    assert decision.decision == "CLARIFY" and "有电梯" in text and "送到几楼" not in text
    text, decision = ask(turn, "没电梯，普货")
    assert decision.decision == "EXECUTE"
    # base 10 + 0.12×80 = 19.6, floor 5: 0.10×80 = 8, one piece 50–100 kg: 10 → 37.60
    assert "· 上楼费：37.60 元" in text and "合计：37.60 元" in text
    text, _ = ask(turn, "那3楼呢")
    assert "· 上楼费：33.60 元" in text  # 19.6 + 0.05×80 + 10
    assert len(platform.calls) == 1  # the waybill is read once for the thread


def test_with_a_lift_the_floor_is_not_asked():
    turn = _turn(Platform(rows=[row(actual_weight=80, settlement_weight=80)]))
    text, decision = ask(turn, f"{CODE} 有电梯上楼，普货", CODE)
    assert decision.decision == "EXECUTE" and "送到几楼" not in text and "· 上楼费：" in text


def test_floor_and_lift_without_a_fee_name_mean_the_upstairs_fee():
    turn = _turn(Platform(rows=[row(actual_weight=80, settlement_weight=80)]), rules=ALL)
    text, decision = ask(turn, f"{CODE} 普货 有电梯 上2楼", CODE)
    assert decision.decision == "EXECUTE" and "· 上楼费：" in text and "要算哪些费用" not in text


def test_without_a_lift_the_floor_is_still_asked():
    turn = _turn(Platform(rows=[row(actual_weight=80, settlement_weight=80)]))
    text, decision = ask(turn, f"{CODE} 无电梯上楼，普货", CODE)
    assert decision.decision == "CLARIFY" and "送到几楼" in text
    text, decision = ask(turn, "5")
    assert decision.decision == "EXECUTE" and "· 上楼费：37.60 元" in text


def test_without_named_fees_the_approved_ones_are_listed_and_fees_can_be_added():
    turn = _turn(Platform(rows=[row(actual_weight=80, settlement_weight=80)]), rules=ALL)
    text, decision = ask(turn, f"{CODE} 要收多少钱", CODE)
    assert decision.decision == "CLARIFY" and "上楼费、卸货费、开箱清点、拆托/拆木架木箱" in text
    text, _ = ask(turn, "上楼费 5楼 无电梯 普货")
    assert "合计：37.60 元" in text and "· 卸货费" not in text
    text, _ = ask(turn, "再加卸货费")
    # one group 上楼卸货: the larger counts; 80 kg unloading is 0
    assert "· 上楼费：37.60 元" in text and "· 卸货费：0.00 元（不计入合计）" in text and "合计：37.60 元" in text


def test_a_delivery_without_a_waybill_uses_todays_rules():
    recorded = []
    text, decision = ask(_turn(Platform(), rules=ALL, recorded=recorded), "开箱清点 30件 多少钱")
    assert decision.decision == "EXECUTE" and "费用估算" in text and "按今天（2026-11-16）" in text
    assert "· 开箱清点：5.00 元" in text  # 0.1 × 30 = 3, minimum 5
    assert recorded[0]["tracking_number"] is None and recorded[0]["values"] == {"check_count": "30"}


def test_parameters_are_told_apart_by_label_and_unit():
    rules = {item["fee_kind"]: item for item in ALL}
    from shared.handbook_fee_rules import rule_from_record

    params = rule_from_record(rules["unpacking"]).content.params
    assert fee_actions.parse_values(params, "拆托3个，木箱2件") == {"pallet_count": "3", "crate_count": "2"}
    assert fee_actions.parse_values(params, "3") == {}  # a bare number is taken only for the one thing asked
    assert fee_actions.parse_values(params, "3", asked=["pallet_count"]) == {"pallet_count": "3"}
    assert fee_actions.fee_aliases("拆托/拆木架木箱") == ["拆托/拆木架木箱", "拆木架木箱", "拆木架木", "拆木架", "拆木箱", "拆托"]
    assert "二次上门" in fee_actions.fee_aliases("二次上门签收/取回单")


def test_a_fee_named_by_part_of_its_name_is_counted_and_its_count_asked_not_taken_as_zero():
    platform = Platform(rows=[row(settlement_weight=420, actual_weight=420, quantity=3)])
    turn = _turn(platform, rules=ALL)
    text, decision = ask(turn, f"{CODE}，拆木架，有电梯，5楼，普货", CODE)
    assert decision.decision == "CLARIFY" and "拆木架/木箱件数" in text and "拆托个数" not in text
    assert "上楼费、拆托/拆木架木箱" in text
    text, decision = ask(turn, "2")
    assert decision.decision == "EXECUTE"
    assert "· 上楼费：30.20 元" in text  # 5 + 0.06 × 420
    assert "· 拆托/拆木架木箱：20.00 元" in text and "合计：50.20 元" in text  # 10 元/件 × 2


def test_a_count_said_with_the_fee_or_negated_is_not_asked_again():
    rules = {item["fee_kind"]: item for item in ALL}
    from shared.handbook_fee_rules import rule_from_record

    params = rule_from_record(rules["unpacking"]).content.params
    assert [p.key for p in fee_actions.mentioned(params, "拆木架", {})] == ["crate_count"]
    assert fee_actions.mentioned(params, "拆木架", {"crate_count": "2"}) == []
    assert fee_actions.mentioned(params, "不拆托，拆木箱", {"crate_count": "1"}) == []
    assert fee_actions.mentioned(params, "上楼费多少", {}) == []


def test_no_approved_rule_for_the_billing_date_gives_no_amount():
    text, decision = ask(_turn(Platform(), rules=()), f"{CODE} 上楼费 5楼 有电梯 普货", CODE)
    assert decision.decision == "EXECUTE" and "没有已审核生效的收费规则，不能给出金额" in text and "元" not in text
    later = approved("upstairs", effective="2026-12-01")
    text, _ = ask(_turn(Platform(), rules=(later,)), f"{CODE} 上楼费 5楼 有电梯 普货", CODE)
    assert "不能给出金额" in text  # billing date 2026-11-12 is before that version


def test_several_pieces_are_taken_at_their_average_weight_and_the_reply_says_so():
    facts = fee_actions.waybill_facts(CODE, {"data": [row(quantity=17, actual_weight=195, settlement_weight=195)]})
    rows, note = fee_actions.piece_rows(facts)
    assert rows == [{"count": 17, "weight": "11.47"}] and "按平均每件 11.47 公斤计算单件加收" in note
    single = fee_actions.waybill_facts(CODE, {"data": [row(quantity=1, actual_weight=Decimal("100"))]})
    assert fee_actions.piece_rows(single) == ([{"count": 1, "weight": "100"}], "")
    no_actual = fee_actions.waybill_facts(CODE, {"data": [row(quantity=1, actual_weight=None, settlement_weight=60)]})
    assert fee_actions.piece_rows(no_actual)[0] == [{"count": 1, "weight": "60"}]


def test_average_weight_reaches_the_rule():
    platform = Platform(rows=[row(quantity=2, actual_weight=160, settlement_weight=160)])
    text, _ = ask(_turn(platform), f"{CODE} 上楼费 3楼 无电梯 普货", CODE)
    # base 10 + 0.12×160 = 29.2, floor 3: 0.05×160 = 8, two pieces of 80 kg: 10×2 → 57.20
    assert "· 上楼费：57.20 元" in text and "按平均每件 80.00 公斤" in text


def test_ticked_upstairs_service_has_no_remark_reminder():
    facts = fee_actions.waybill_facts(CODE, {"data": [row(upstairs_service=1)]})
    assert not any("没有勾选" in item for item in fee_actions.reminders(facts))


@pytest.mark.parametrize("changes,message", [
    ({"billing_date": ""}, "没有读到开单日期"),
    ({"settlement_weight": None}, "没有读到结算重量"),
    ({"quantity": 0}, "没有读到件数"),
    ({"tracking_number": "R00099990002", "requested_bill_code": "R00099990002"}, "没有查到运单"),
])
def test_missing_waybill_facts_stop_the_calculation(changes, message):
    text, decision = ask(_turn(Platform(rows=[row(**changes)])), f"{CODE} 上楼费 5楼 无电梯 普货", CODE)
    assert decision.decision == "REJECT" and message in text


def test_platform_failure_and_other_platforms_are_reported_plainly():
    text, decision = ask(_turn(Platform(fail="融辉登录已失效")), f"{CODE} 上楼费 5楼 无电梯", CODE)
    assert decision.decision == "REJECT" and "融辉登录已失效" in text
    yunda = "4312345678901"
    text, decision = ask(_turn(Platform()), f"{yunda} 上楼费 5楼 无电梯", yunda)
    assert decision.decision in {"REJECT", "CLARIFY"} and "元" not in text


# -- the model's understanding (2026-11-27): fees wanted and conditions in standard wording ---------------

def ask_understood(turn, text, tracking=None, **understood):
    arguments = {"tracking_number": tracking} if tracking else {}
    arguments.update(understood)
    return turn.execute("biz_fee_quote", arguments, message=text)


def test_the_models_understanding_names_the_fee_and_conditions_code_cannot_read():
    platform = Platform(rows=[row(actual_weight=80, settlement_weight=80)])
    text, decision = ask(_turn(platform, rules=ALL), f"{CODE} 普货 电梯能用", CODE)
    assert decision.decision == "CLARIFY"
    text, decision = ask_understood(_turn(platform, rules=ALL), f"{CODE} 普货 电梯能用", CODE,
                                    fees=["上楼费"], conditions=["有电梯=电梯能用", "普货=普货"])
    assert decision.decision == "EXECUTE" and "· 上楼费：" in text and "· 卸货费" not in text


def test_a_condition_not_tied_to_the_persons_words_is_not_used():
    platform = Platform(rows=[row(actual_weight=80, settlement_weight=80)])
    # “3楼” cites words that say 二楼; “每件80公斤” cites nothing: both are dropped, the floor read by code stays.
    text, decision = ask_understood(_turn(platform, rules=ALL), f"{CODE} 电梯坏了 搬上五楼 普货", CODE,
                                    fees=["上楼费"], conditions=["3楼=搬上五楼", "无电梯=电梯坏了", "每件80公斤"])
    assert decision.decision == "EXECUTE" and "送到几楼 5 楼" in text and "有电梯 否" in text
    text, decision = ask_understood(_turn(platform, rules=ALL), f"{CODE} 无电梯 搬上二楼 普货", CODE,
                                    fees=["上楼费"], conditions=["2楼=搬上二楼", "无电梯=无电梯", "普货=普货"])
    assert decision.decision == "EXECUTE" and "· 上楼费：" in text


def test_the_persons_own_words_win_over_the_models_restatement():
    platform = Platform(rows=[row(actual_weight=80, settlement_weight=80)])
    plain, _ = ask(_turn(platform, rules=ALL), f"{CODE} 上楼费 5楼 无电梯 普货", CODE)
    understood, _ = ask_understood(_turn(platform, rules=ALL), f"{CODE} 上楼费 5楼 无电梯 普货", CODE,
                                   fees=["上楼费"], conditions=["5楼=5楼", "有电梯=上楼费", "普货=普货"])
    assert understood == plain and "· 上楼费：37.60 元" in plain


def test_a_follow_up_keeps_the_fees_already_chosen():
    turn = _turn(Platform(rows=[row(actual_weight=80, settlement_weight=80)]), rules=ALL)
    ask(turn, f"{CODE} 上楼费 5楼 无电梯 普货", CODE)
    text, _ = ask_understood(turn, "那3楼呢", fees=["卸货费"], conditions=["3楼=3楼"])
    assert "· 上楼费：33.60 元" in text and "· 卸货费" not in text


def test_the_model_sees_plain_json_schema():
    from agent.agent_actions.business_actions import BusinessTurn

    tools = {tool["function"]["name"]: tool["function"]["parameters"] for tool in BusinessTurn.model_tools()}
    assert "x-ground" not in json.dumps(tools)
    assert set(tools["biz_fee_quote"]["properties"]) == {"tracking_number", "fees", "conditions"}


# -- rule defaults (2026-11-27): 没说明就按普货、有电梯；说“无电梯 / 楼梯上几楼”才按步梯 ----------------

def _with_defaults():
    rule = approved("upstairs")
    rule["content"] = deepcopy(rule["content"])
    for param in rule["content"]["params"]:
        if param["key"] == "elevator":
            param["default"] = True
        elif param["key"] == "goods_type":
            param["default"] = "general"
    return rule


def test_unmentioned_lift_and_goods_take_the_rule_default_and_the_reply_says_so():
    turn = _turn(Platform(rows=[row(actual_weight=80, settlement_weight=80)]), rules=(_with_defaults(),))
    text, decision = ask(turn, f"{CODE} 上楼费 2楼", CODE)
    assert decision.decision == "EXECUTE" and "· 上楼费：" in text
    assert "没说明的按规则默认：有电梯 是 · 货物类型 普货" in text


@pytest.mark.parametrize("said", ["无电梯 上5楼", "楼梯上5楼", "走楼梯 5楼"])
def test_stairs_said_plainly_override_the_lift_default(said):
    turn = _turn(Platform(rows=[row(actual_weight=80, settlement_weight=80)]), rules=(_with_defaults(),))
    text, decision = ask(turn, f"{CODE} 上楼费 {said}", CODE)
    assert decision.decision == "EXECUTE" and "· 上楼费：37.60 元" in text and "有电梯 否" in text
    assert "没说明的按规则默认：货物类型 普货" in text


def test_conditions_the_person_did_not_say_take_the_rule_default_not_the_models_guess():
    """2026-11-27: “R… 上2楼” — the model wrote 有电梯 / 普货 though they were not said; they are
    not used, the rule defaults are, and the reply says so."""
    turn = _turn(Platform(rows=[row(actual_weight=80, settlement_weight=80)]), rules=(_with_defaults(),))
    text, decision = ask_understood(turn, f"{CODE} 上2楼", CODE, fees=["上楼费"],
                                    conditions=["2楼=上2楼", "有电梯", "普货=普货", "无电梯=上2楼", f"无电梯={CODE}"])
    assert decision.decision == "EXECUTE" and "送到几楼 2 楼" in text
    assert "没说明的按规则默认：有电梯 是 · 货物类型 普货" in text
